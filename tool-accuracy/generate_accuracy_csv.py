"""
Generate a tools accuracy CSV from on-chain Omen (Gnosis) fleet data.

Fetches all resolved bets from the predict-omen subgraph, matches each to its
mech tool via the mech-marketplace-gnosis subgraph, and outputs a CSV in the
same format as the IPFS accuracy store.

Output format:
    tool,tool_accuracy,total_requests,min,max

Unlike the Polymarket version, this fetches bets globally (not per-agent) since
the Omen subgraph supports direct bet enumeration. Mech requests are cached to
disk to avoid redundant fetches across runs.

Usage:
    # All resolved bets (paginated, may take a while)
    python tool-accuracy/generate_accuracy_csv.py

    # Last 30 days
    python tool-accuracy/generate_accuracy_csv.py --from 2026-02-16

    # Specific window
    python tool-accuracy/generate_accuracy_csv.py --from 2026-01-01 --to 2026-03-01

    # Limit bets for speed
    python tool-accuracy/generate_accuracy_csv.py --max-bets 5000

    # Pin to IPFS
    python tool-accuracy/generate_accuracy_csv.py --from 2026-01-01 --pin
"""

import argparse
import csv
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREDICT_OMEN_URL = (
    "https://api.subgraph.staging.autonolas.tech/api/proxy/predict-omen"
)
OLAS_MECH_SUBGRAPH_URL = (
    "https://api.subgraph.staging.autonolas.tech/api/proxy/mech-marketplace-gnosis"
)

IPFS_NODE_URL = "https://registry.autonolas.tech/api/v0/add"
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs/"

QUESTION_DATA_SEPARATOR = "\u241f"
REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3

# Disk cache for mech requests (avoids re-fetching per agent across runs)
_CACHE_FILE = Path(__file__).parent / ".mech_cache_accuracy.json"
MECH_CACHE_TTL_SECONDS = 12 * 60 * 60  # 12 hours


# ---------------------------------------------------------------------------
# HTTP helpers
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


# ---------------------------------------------------------------------------
# Mech request cache
# ---------------------------------------------------------------------------


def _load_cache():
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache):
    _CACHE_FILE.write_text(json.dumps(cache))


_mech_cache = _load_cache()


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def parse_date(date_str):
    """Parse YYYY-MM-DD string to unix timestamp."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# Data fetching — Omen bets
# ---------------------------------------------------------------------------


def fetch_omen_bets(from_ts, to_ts=None, max_bets=None, batch_size=1000):
    """
    Fetch resolved bets from the predict-omen subgraph.
    Paginates through all results ordered by timestamp descending.
    """
    headers = {"Content-Type": "application/json"}
    all_bets = []
    skip = 0
    limit = max_bets or float("inf")

    while len(all_bets) < limit:
        remaining = limit - len(all_bets)
        first = min(batch_size, int(remaining)) if remaining != float("inf") else batch_size

        # Build where clause
        where_parts = ["fixedProductMarketMaker_: {currentAnswer_not: null}"]
        if from_ts:
            where_parts.append(f"timestamp_gte: {from_ts}")
        if to_ts:
            where_parts.append(f"timestamp_lte: {to_ts}")
        where_clause = ", ".join(where_parts)

        query = f"""
        {{
          bets(
            first: {first}
            skip: {skip}
            orderBy: timestamp
            orderDirection: desc
            where: {{{where_clause}}}
          ) {{
            id
            timestamp
            bettor {{
              id
              serviceId
            }}
            outcomeIndex
            fixedProductMarketMaker {{
              currentAnswer
              question
            }}
          }}
        }}
        """
        try:
            resp = _post_with_retry(
                PREDICT_OMEN_URL, json={"query": query}, headers=headers
            )
            data = resp.json()
            batch = data.get("data", {}).get("bets", [])
        except Exception as e:
            print(f"\n  Warning: fetch error at skip={skip}: {e}")
            break

        if not batch:
            break

        for bet in batch:
            chosen = int(bet["outcomeIndex"])
            try:
                correct = int(bet["fixedProductMarketMaker"]["currentAnswer"], 16)
            except (ValueError, TypeError):
                continue
            all_bets.append({
                "bet_id": bet["id"],
                "timestamp": int(bet["timestamp"]),
                "bettor": bet["bettor"]["id"],
                "service_id": bet["bettor"].get("serviceId"),
                "chosen_outcome": chosen,
                "correct_outcome": correct,
                "is_correct": chosen == correct,
                "question": bet["fixedProductMarketMaker"].get("question", ""),
            })

        if len(batch) < first:
            break
        skip += first

        if len(all_bets) % 5000 < batch_size:
            print(f"\r  Fetched {len(all_bets)} bets...", end="", flush=True)

    print(f"\r  Fetched {len(all_bets)} resolved bets total")
    return all_bets


# ---------------------------------------------------------------------------
# Data fetching — mech requests
# ---------------------------------------------------------------------------

GET_MECH_SENDER_QUERY = """
query MechSender($id: ID!, $timestamp_gt: Int!, $skip: Int, $first: Int) {
    sender(id: $id) {
        totalMarketplaceRequests
        requests(first: $first, skip: $skip,
                 where: { blockTimestamp_gt: $timestamp_gt }) {
            blockTimestamp
            parsedRequest {
                questionTitle
                tool
            }
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
            resp = _post_with_retry(
                OLAS_MECH_SUBGRAPH_URL,
                json={"query": GET_MECH_SENDER_QUERY, "variables": variables},
                headers={"Content-Type": "application/json"},
            )
            data = resp.json()
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


def get_mech_requests_cached(agent_address, timestamp_gt):
    """Returns mech requests with disk-backed TTL cache."""
    entry = _mech_cache.get(agent_address)
    if entry and (time.time() - entry.get("fetched_at", 0)) < MECH_CACHE_TTL_SECONDS:
        return entry["requests"]

    reqs = fetch_mech_requests(agent_address, timestamp_gt)
    _mech_cache[agent_address] = {
        "fetched_at": int(time.time()),
        "requests": reqs,
    }
    # Periodically save cache
    if len(_mech_cache) % 50 == 0:
        _save_cache(_mech_cache)
    return reqs


# ---------------------------------------------------------------------------
# Tool matching
# ---------------------------------------------------------------------------


def extract_question_title(question):
    if not question:
        return ""
    return question.split(QUESTION_DATA_SEPARATOR)[0]


def match_bet_to_tool(bet, mech_requests):
    """Match a bet to the mech tool used, by question title prefix matching."""
    bet_title = extract_question_title(bet.get("question", "")).strip()
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

    bet_ts = bet.get("timestamp", 0)
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


def compute_tool_stats(bets, from_ts):
    """Match all bets to tools and aggregate per-tool accuracy stats."""
    # Group bets by agent for efficient mech request fetching
    agent_bets = defaultdict(list)
    for bet in bets:
        agent_bets[bet["bettor"]].append(bet)

    tool_stats = defaultdict(lambda: {
        "wins": 0,
        "resolved": 0,
        "min_ts": float("inf"),
        "max_ts": 0,
    })

    total_agents = len(agent_bets)
    matched_total = 0
    unmatched_total = 0

    print(f"  Matching tools for {len(bets)} bets across {total_agents} agents...")

    for i, (addr, abets) in enumerate(agent_bets.items()):
        if (i + 1) % 25 == 0 or i == 0:
            print(f"\r  Agent [{i + 1}/{total_agents}] matched={matched_total} "
                  f"unmatched={unmatched_total}", end="", flush=True)

        mech_reqs = get_mech_requests_cached(addr, timestamp_gt=from_ts)
        if not mech_reqs:
            unmatched_total += len(abets)
            continue

        for bet in abets:
            tool = match_bet_to_tool(bet, mech_reqs)
            if tool == "unknown":
                unmatched_total += 1
                continue

            matched_total += 1
            ts = bet["timestamp"]
            stats = tool_stats[tool]
            stats["resolved"] += 1
            if bet["is_correct"]:
                stats["wins"] += 1
            if ts < stats["min_ts"]:
                stats["min_ts"] = ts
            if ts > stats["max_ts"]:
                stats["max_ts"] = ts

    # Save cache at the end
    _save_cache(_mech_cache)

    print(f"\n  Matched: {matched_total}, Unmatched: {unmatched_total}")
    return dict(tool_stats)


def generate_csv(tool_stats, min_bets=5):
    """Generate CSV string in the IPFS accuracy store format."""
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

    rows.sort(key=lambda r: -r[1])
    for row in rows:
        writer.writerow(row)

    return buf.getvalue()


def pin_to_ipfs(csv_path):
    """Pin CSV file to IPFS via the Autonolas registry node's HTTP API."""
    with open(csv_path, "rb") as f:
        resp = requests.post(
            IPFS_NODE_URL,
            files={"file": (os.path.basename(csv_path), f)},
            params={"pin": "true", "wrap-with-directory": "false"},
            timeout=60,
        )
    resp.raise_for_status()
    result = resp.json()
    return result["Hash"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Generate tools accuracy CSV from on-chain Omen data"
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output CSV file path (default: tool-accuracy/tools_accuracy_omen.csv)",
    )
    parser.add_argument(
        "--max-bets", type=int, default=None,
        help="Maximum number of bets to fetch (default: all)",
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
        help="Pin the generated CSV to IPFS",
    )
    args = parser.parse_args()

    # Parse time window
    from_ts = parse_date(args.from_date) if args.from_date else 0
    to_ts = parse_date(args.to_date) if args.to_date else None

    if args.from_date or args.to_date:
        from_str = args.from_date or "beginning"
        to_str = args.to_date or "now"
        print(f"Time window: {from_str} to {to_str}")
    else:
        print("Time window: all time")

    # Fetch bets
    print("Fetching resolved Omen bets...")
    bets = fetch_omen_bets(from_ts=from_ts, to_ts=to_ts, max_bets=args.max_bets)

    if not bets:
        print("ERROR: No bets fetched", file=sys.stderr)
        sys.exit(1)

    # Match to tools and aggregate
    tool_stats = compute_tool_stats(bets, from_ts=from_ts)

    if not tool_stats:
        print("ERROR: No tool data collected", file=sys.stderr)
        sys.exit(1)

    csv_content = generate_csv(tool_stats, min_bets=args.min_bets)

    # Print summary
    print("\n" + "=" * 80)
    print("TOOL ACCURACY SUMMARY (Omen on-chain data)")
    print("=" * 80)
    reader = csv.DictReader(io.StringIO(csv_content))
    print(f"  {'Tool':<45s} {'Accuracy':>10s} {'Requests':>10s}")
    print(f"  {'-'*45} {'-'*10} {'-'*10}")
    for row in reader:
        print(f"  {row['tool']:<45s} {float(row['tool_accuracy']):>9.2f}% {row['total_requests']:>10s}")
    print()

    # Write to file
    output_path = args.output or "tool-accuracy/tools_accuracy_omen.csv"
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

    # Compare with current IPFS store
    print("\n" + "=" * 80)
    print("COMPARISON WITH CURRENT IPFS STORE (QmR8etyW3TPF..., Apr-Jun 2024)")
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
    print(f"  {'Tool':<40s} {'Old':>12s} {'New':>12s} {'Delta':>8s}")
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
