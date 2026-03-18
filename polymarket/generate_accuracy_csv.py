"""
Generate a tools accuracy CSV from on-chain fleet data.

Produces a CSV in the same format as the IPFS accuracy store
(QmR8etyW3TPFadNtNrW54vfnFqmh8vBrMARWV76EmxCZyk) using actual
on-chain performance data.

Output format:
    tool,tool_accuracy,total_requests,min,max

Where:
    - tool_accuracy is a percentage (0-100)
    - total_requests is the number of resolved bets matched to that tool
    - min/max are the earliest/latest bet timestamps for that tool

Usage:
    # Polymarket — all time
    python polymarket/generate_accuracy_csv.py

    # Polymarket — last 30 days only
    python polymarket/generate_accuracy_csv.py --from 2026-02-16

    # Polymarket — specific window
    python polymarket/generate_accuracy_csv.py --from 2026-02-01 --to 2026-03-01

    # Limit agents for speed
    python polymarket/generate_accuracy_csv.py --sample 50

    # Custom output and minimum bets threshold
    python polymarket/generate_accuracy_csv.py --min-bets 20 -o accuracy.csv

    # Generate and pin to IPFS (requires aea-cli-ipfs)
    python polymarket/generate_accuracy_csv.py --pin
"""

import argparse
import csv
import io
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

POLYMARKET_BETS_SUBGRAPH_URL = (
    "https://predict-polymarket-agents.subgraph.autonolas.tech/"
)
THE_GRAPH_API_KEY = os.getenv("THE_GRAPH_API_KEY")
POLYGON_REGISTRY_SUBGRAPH_URL = (
    f"https://gateway.thegraph.com/api/{THE_GRAPH_API_KEY}/subgraphs/id/"
    f"HHRBjVWFT2bV7eNSRqbCNDtUVnLPt911hcp8mSe4z6KG"
    if THE_GRAPH_API_KEY
    else None
)
OLAS_MECH_SUBGRAPH_URL = (
    "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"
)

IPFS_NODE = "/dns/registry.autonolas.tech/tcp/443/https"
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs/"

USDC_DECIMALS_DIVISOR = 1_000_000
QUESTION_DATA_SEPARATOR = "\u241f"
REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3


# ---------------------------------------------------------------------------
# HTTP / subgraph helpers
# ---------------------------------------------------------------------------


def _post_with_retry(url, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            time.sleep(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
    raise last_exc


def call_subgraph(url, query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = _post_with_retry(
        url, json=payload, headers={"Content-Type": "application/json"}
    )
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Subgraph error: {data['errors']}")
    return data


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def parse_date(date_str):
    """Parse YYYY-MM-DD string to unix timestamp."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def get_all_polystrat_agents():
    if POLYGON_REGISTRY_SUBGRAPH_URL:
        try:
            query = '{ services(where: { agentIds_contains: [86] }, first: 1000) { id multisig } }'
            response = call_subgraph(POLYGON_REGISTRY_SUBGRAPH_URL, query)
            return [s["multisig"].lower() for s in response["data"]["services"]]
        except Exception:
            pass
    query = '{ traderAgents(first: 1000, orderBy: totalBets, orderDirection: desc) { id } }'
    response = call_subgraph(POLYMARKET_BETS_SUBGRAPH_URL, query)
    return [a["id"].lower() for a in response.get("data", {}).get("traderAgents", [])]


def fetch_agent_bets(safe_address):
    query = """
query GetBets($id: ID!) {
  marketParticipants(
    where: {traderAgent_: {id: $id}}
    first: 1000
    orderBy: blockTimestamp
    orderDirection: desc
  ) {
    bets {
      id
      outcomeIndex
      amount
      shares
      blockTimestamp
      question {
        id
        metadata { title }
        resolution { winningIndex }
      }
    }
  }
}
"""
    response = call_subgraph(POLYMARKET_BETS_SUBGRAPH_URL, query, {"id": safe_address})
    participants = response.get("data", {}).get("marketParticipants", [])
    all_bets = []
    for p in participants:
        for bet in p.get("bets", []):
            all_bets.append(bet)
    return all_bets


GET_MECH_SENDER_QUERY = """
query MechSender($id: ID!, $timestamp_gt: Int!, $skip: Int, $first: Int) {
    sender(id: $id) {
        requests(first: $first, skip: $skip,
                 where: { blockTimestamp_gt: $timestamp_gt }) {
            blockTimestamp
            parsedRequest { questionTitle tool }
        }
    }
}
"""


def fetch_mech_requests(agent_address, timestamp_gt, batch_size=1000):
    all_requests = []
    skip = 0
    while True:
        variables = {
            "id": agent_address,
            "timestamp_gt": timestamp_gt,
            "skip": skip,
            "first": batch_size,
        }
        try:
            response = _post_with_retry(
                OLAS_MECH_SUBGRAPH_URL,
                json={"query": GET_MECH_SENDER_QUERY, "variables": variables},
                headers={"Content-Type": "application/json"},
            )
            data = response.json()
            result = (data.get("data") or {}).get("sender") or {}
            batch = result.get("requests", [])
        except Exception:
            break
        if not batch:
            break
        all_requests.extend(batch)
        if len(batch) < batch_size:
            break
        skip += batch_size
    return all_requests


def extract_question_title(question):
    if not question:
        return ""
    return question.split(QUESTION_DATA_SEPARATOR)[0]


def match_bet_to_tool(bet_title, bet_ts, mech_requests):
    bet_title = extract_question_title(bet_title).strip()
    if not bet_title:
        return "unknown"
    matched = []
    for req in mech_requests:
        mech_title = extract_question_title(
            (req.get("parsedRequest") or {}).get("questionTitle", "")
        ).strip()
        if not mech_title:
            continue
        if bet_title.startswith(mech_title) or mech_title.startswith(bet_title):
            matched.append(req)
    if not matched:
        return "unknown"
    before_bet = [r for r in matched if int(r.get("blockTimestamp") or 0) <= bet_ts]
    chosen = (
        max(before_bet, key=lambda r: int(r.get("blockTimestamp") or 0))
        if before_bet
        else matched[0]
    )
    return (chosen.get("parsedRequest") or {}).get("tool") or "unknown"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def collect_fleet_tool_stats(agents, from_ts, to_ts, sample_size=None):
    """Fetch bets + mech requests for the fleet, match tools, aggregate stats."""
    if sample_size:
        agents = agents[:sample_size]

    # Per-tool accumulators
    tool_stats = defaultdict(lambda: {
        "wins": 0,
        "resolved": 0,
        "min_ts": float("inf"),
        "max_ts": 0,
    })

    total_agents = len(agents)
    matched_total = 0
    unmatched_total = 0
    skipped_timerange = 0

    for i, addr in enumerate(agents):
        print(f"\r  [{i + 1}/{total_agents}] {addr[:10]}...", end="", flush=True)
        try:
            raw_bets = fetch_agent_bets(addr)
            if not raw_bets:
                continue

            mech_reqs = fetch_mech_requests(addr, timestamp_gt=from_ts)
            if not mech_reqs:
                continue

            for bet in raw_bets:
                bet_ts = int(bet.get("blockTimestamp", 0))

                # Apply time window filter
                if bet_ts < from_ts:
                    skipped_timerange += 1
                    continue
                if to_ts and bet_ts > to_ts:
                    skipped_timerange += 1
                    continue

                question = bet.get("question") or {}
                resolution = question.get("resolution")
                if resolution is None:
                    continue
                wi = resolution.get("winningIndex")
                if wi is None or int(wi) < 0:
                    continue

                outcome_idx = int(bet.get("outcomeIndex", -1))
                is_win = outcome_idx == int(wi)
                title = (question.get("metadata") or {}).get("title", "")

                tool = match_bet_to_tool(title, bet_ts, mech_reqs)
                if tool == "unknown":
                    unmatched_total += 1
                    continue

                matched_total += 1
                stats = tool_stats[tool]
                stats["resolved"] += 1
                if is_win:
                    stats["wins"] += 1
                if bet_ts < stats["min_ts"]:
                    stats["min_ts"] = bet_ts
                if bet_ts > stats["max_ts"]:
                    stats["max_ts"] = bet_ts

        except Exception as e:
            print(f" ERROR: {e}")
            continue

    print(f"\n  Matched: {matched_total}, Unmatched: {unmatched_total}, "
          f"Outside time range: {skipped_timerange}")
    return dict(tool_stats)


def generate_csv(tool_stats, min_bets=5):
    """Generate CSV string in the same format as the IPFS accuracy store."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["tool", "tool_accuracy", "total_requests", "min", "max"])

    dt_fmt = "%Y-%m-%d %H:%M:%S"
    rows = []
    for tool, stats in sorted(tool_stats.items()):
        if stats["resolved"] < min_bets:
            continue
        accuracy_pct = (stats["wins"] / stats["resolved"]) * 100 if stats["resolved"] else 0
        min_dt = datetime.fromtimestamp(stats["min_ts"], tz=timezone.utc).strftime(dt_fmt)
        max_dt = datetime.fromtimestamp(stats["max_ts"], tz=timezone.utc).strftime(dt_fmt)
        rows.append((tool, accuracy_pct, stats["resolved"], min_dt, max_dt))

    # Sort by accuracy descending for readability
    rows.sort(key=lambda r: -r[1])
    for row in rows:
        writer.writerow(row)

    return buf.getvalue()


def pin_to_ipfs(csv_path):
    """Pin CSV file to IPFS via the Autonolas registry node's HTTP API."""
    url = "https://registry.autonolas.tech/api/v0/add"
    with open(csv_path, "rb") as f:
        resp = requests.post(
            url,
            files={"file": (os.path.basename(csv_path), f)},
            params={"pin": "true", "wrap-with-directory": "false"},
            timeout=60,
        )
    resp.raise_for_status()
    result = resp.json()
    return result["Hash"]


def main():
    parser = argparse.ArgumentParser(
        description="Generate tools accuracy CSV from on-chain fleet data"
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output CSV file path (default: polymarket/tools_accuracy_polymarket.csv)",
    )
    parser.add_argument(
        "--sample", type=int, default=None,
        help="Limit number of agents to process (for speed)",
    )
    parser.add_argument(
        "--min-bets", type=int, default=5,
        help="Minimum resolved bets for a tool to be included (default: 5)",
    )
    parser.add_argument(
        "--from", dest="from_date", default=None,
        help="Start date for bets (YYYY-MM-DD, default: all time)",
    )
    parser.add_argument(
        "--to", dest="to_date", default=None,
        help="End date for bets (YYYY-MM-DD, default: now)",
    )
    parser.add_argument(
        "--pin", action="store_true",
        help="Pin the generated CSV to IPFS (requires aea-cli-ipfs)",
    )
    args = parser.parse_args()

    # Parse time window
    from_ts = parse_date(args.from_date) if args.from_date else 0
    to_ts = parse_date(args.to_date) if args.to_date else None

    # Display time window
    if args.from_date or args.to_date:
        from_str = args.from_date or "beginning"
        to_str = args.to_date or "now"
        print(f"Time window: {from_str} to {to_str}")
    else:
        print("Time window: all time")

    print("Fetching PolyStrat agent list...")
    agents = get_all_polystrat_agents()
    print(f"Found {len(agents)} agents")

    print("Collecting tool accuracy data from on-chain bets...")
    tool_stats = collect_fleet_tool_stats(
        agents, from_ts=from_ts, to_ts=to_ts, sample_size=args.sample
    )

    if not tool_stats:
        print("ERROR: No tool data collected", file=sys.stderr)
        sys.exit(1)

    csv_content = generate_csv(tool_stats, min_bets=args.min_bets)

    # Print summary
    print("\n" + "=" * 80)
    print("TOOL ACCURACY SUMMARY")
    print("=" * 80)
    reader = csv.DictReader(io.StringIO(csv_content))
    print(f"  {'Tool':<45s} {'Accuracy':>10s} {'Requests':>10s}")
    print(f"  {'-'*45} {'-'*10} {'-'*10}")
    for row in reader:
        print(f"  {row['tool']:<45s} {float(row['tool_accuracy']):>9.2f}% {row['total_requests']:>10s}")
    print()

    # Write to file
    output_path = args.output or "polymarket/tools_accuracy_polymarket.csv"
    with open(output_path, "w") as f:
        f.write(csv_content)
    print(f"CSV written to: {output_path}")

    # Pin to IPFS if requested
    if args.pin:
        print("Pinning to IPFS...")
        ipfs_hash = pin_to_ipfs(output_path)
        print(f"\n  IPFS hash: {ipfs_hash}")
        print(f"  Gateway URL: {IPFS_GATEWAY}{ipfs_hash}")
        print(f"\n  To use in service.yaml:")
        print(f"    tools_accuracy_hash: ${{TOOLS_ACCURACY_HASH:str:{ipfs_hash}}}")

    # Compare with old IPFS store
    print("\n" + "=" * 80)
    print("COMPARISON WITH CURRENT IPFS STORE (Omen-era, Apr-Jun 2024)")
    print("=" * 80)
    old_store = {
        "prediction-request-reasoning": 67.11,
        "prediction-offline": 67.41,
        "prediction-request-reasoning-claude": 66.72,
        "prediction-online": 66.01,
        "prediction-online-sme": 65.67,
        "prediction-request-rag-claude": 65.64,
        "prediction-request-rag": 63.58,
        "prediction-url-cot-claude": 61.90,
        "claude-prediction-online": 61.14,
        "claude-prediction-offline": 57.38,
    }
    reader = csv.DictReader(io.StringIO(csv_content))
    print(f"  {'Tool':<40s} {'Old (Omen)':>12s} {'New':>12s} {'Delta':>8s}")
    print(f"  {'-'*40} {'-'*12} {'-'*12} {'-'*8}")
    for row in reader:
        tool = row["tool"]
        new_acc = float(row["tool_accuracy"])
        old_acc = old_store.get(tool)
        if old_acc is not None:
            delta = new_acc - old_acc
            print(f"  {tool:<40s} {old_acc:>11.2f}% {new_acc:>11.2f}% {delta:>+7.2f}%")
        else:
            print(f"  {tool:<40s} {'N/A':>12s} {new_acc:>11.2f}% {'NEW':>8s}")


if __name__ == "__main__":
    main()
