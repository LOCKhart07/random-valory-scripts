"""
Verify the accuracy store lock-in claim from the divergence report.

Key question: Do agents lock into PRR at ~36 bets (as the report claims),
or are they biased toward PRR from bet #1 due to the pre-loaded IPFS
accuracy store?

The IPFS accuracy store (QmR8etyW3TPFadNtNrW54vfnFqmh8vBrMARWV76EmxCZyk) contains:
  - prediction-request-reasoning: 67.11% accuracy, 17,372 requests (HIGHEST volume)
  - prediction-offline: 67.41% accuracy, 4,465 requests
  - prediction-online: 66.01% accuracy, 9,490 requests
  - superforcaster: NOT PRESENT (starts at 0/0)

If agents start with this pre-loaded store, PRR would be best_tool from bet #1,
and 75% of bets would use PRR via exploitation. SF would only appear during the
25% exploration phase, randomly selected among all available tools.

This script checks on-chain data to verify:
  1. Tool distribution in first 10/20/50 bets per agent
  2. Whether PRR dominates from the very start
  3. Expected vs actual SF usage rates

Usage:
    python polymarket/verify_lockin.py
    python polymarket/verify_lockin.py --sample 20
"""

import argparse
import os
import statistics
import time
from collections import defaultdict

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

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3
QUESTION_DATA_SEPARATOR = "\u241f"
MECH_LOOKBACK_SECONDS = 70 * 24 * 60 * 60

# Trader config
POLICY_EPSILON = 0.25


def _post_with_retry(url, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            time.sleep(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
    raise last_exc


def call_subgraph(url, query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = _post_with_retry(url, json=payload, headers={"Content-Type": "application/json"})
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Subgraph error: {data['errors']}")
    return data


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


def fetch_mech_requests(agent_address, timestamp_gt=None, batch_size=1000):
    if timestamp_gt is None:
        timestamp_gt = int(time.time()) - MECH_LOOKBACK_SECONDS
    all_requests = []
    skip = 0
    while True:
        variables = {
            "id": agent_address, "timestamp_gt": timestamp_gt,
            "skip": skip, "first": batch_size,
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


def process_bets(bets):
    records = []
    for bet in bets:
        question = bet.get("question") or {}
        resolution = question.get("resolution")
        amount = int(bet.get("amount", 0))
        shares = int(bet.get("shares", 0))
        share_price = amount / shares if shares > 0 else 0
        outcome_idx = int(bet.get("outcomeIndex", -1))
        is_resolved = False
        is_win = None
        if resolution is not None:
            wi = resolution.get("winningIndex")
            if wi is not None and int(wi) >= 0:
                is_resolved = True
                is_win = outcome_idx == int(wi)
        records.append({
            "bet_id": bet.get("id", ""),
            "title": (question.get("metadata") or {}).get("title", ""),
            "timestamp": int(bet.get("blockTimestamp", 0)),
            "share_price": share_price,
            "is_resolved": is_resolved,
            "is_win": is_win,
        })
    return records


def analyze_early_tool_usage(agents, sample_size=None):
    """Analyze tool distribution in first N bets per agent."""
    if sample_size:
        # Take agents with most bets for better signal
        agents = agents[:sample_size]

    print(f"\nFetching data for {len(agents)} agents...")
    print("=" * 80)

    # Collect per-agent early tool distributions
    windows = [10, 20, 50]
    window_tool_counts = {w: defaultdict(list) for w in windows}
    all_agent_results = []
    first_bet_tools = []  # tool used on literally the first bet

    for i, addr in enumerate(agents):
        print(f"\r  [{i+1}/{len(agents)}] {addr[:10]}...", end="", flush=True)
        try:
            raw_bets = fetch_agent_bets(addr)
            bets = process_bets(raw_bets)
            bets.sort(key=lambda b: b["timestamp"])

            if len(bets) < 20:
                continue

            mech_reqs = fetch_mech_requests(addr)
            if not mech_reqs:
                continue

            # Assign tools to bets
            for b in bets:
                b["tool"] = match_bet_to_tool(b["title"], b["timestamp"], mech_reqs)

            known_bets = [b for b in bets if b["tool"] != "unknown"]
            if len(known_bets) < 20:
                continue

            # Analyze tool distribution at different windows
            agent_result = {"addr": addr, "total_bets": len(known_bets)}
            for w in windows:
                window_bets = known_bets[:w]
                tool_counts = defaultdict(int)
                for b in window_bets:
                    tool_counts[b["tool"]] += 1
                n = len(window_bets)
                tool_pcts = {t: c / n * 100 for t, c in tool_counts.items()}
                agent_result[f"window_{w}"] = tool_pcts

                for tool, pct in tool_pcts.items():
                    window_tool_counts[w][tool].append(pct)

            # First bet tool
            if known_bets:
                first_bet_tools.append(known_bets[0]["tool"])

            # Track when PRR first becomes >75% of cumulative bets (lock-in proxy)
            prr_cumulative = 0
            total_cumulative = 0
            lockin_bet = None
            for j, b in enumerate(known_bets):
                total_cumulative += 1
                if b["tool"] == "prediction-request-reasoning":
                    prr_cumulative += 1
                if total_cumulative >= 10 and prr_cumulative / total_cumulative >= 0.75:
                    if lockin_bet is None:
                        lockin_bet = j + 1
            agent_result["prr_lockin_bet"] = lockin_bet

            # Track tool switching: how many unique tools in first 20 bets?
            first_20_tools = set(b["tool"] for b in known_bets[:20])
            agent_result["unique_tools_first_20"] = len(first_20_tools)

            all_agent_results.append(agent_result)

        except Exception as e:
            print(f" ERROR: {e}")
            continue

    print(f"\n\nAnalyzed {len(all_agent_results)} agents with sufficient data")
    print("=" * 80)

    # --- Report: Tool distribution in first N bets ---
    print("\n### Tool Distribution in First N Bets (avg % across agents)")
    print("-" * 70)
    for w in windows:
        print(f"\n  First {w} bets:")
        tool_avgs = {}
        for tool, pcts in sorted(window_tool_counts[w].items(), key=lambda x: -statistics.mean(x[1])):
            avg = statistics.mean(pcts)
            n = len(pcts)
            tool_avgs[tool] = avg
            if avg >= 1.0:
                print(f"    {tool:45s}  {avg:5.1f}%  (n={n})")
        prr_pct = tool_avgs.get("prediction-request-reasoning", 0)
        sf_pct = tool_avgs.get("superforcaster", 0)
        print(f"    --- PRR: {prr_pct:.1f}%, SF: {sf_pct:.1f}%")

    # --- Report: First bet tool distribution ---
    print(f"\n### First Bet Tool Distribution (n={len(first_bet_tools)})")
    print("-" * 70)
    first_tool_counts = defaultdict(int)
    for t in first_bet_tools:
        first_tool_counts[t] += 1
    for tool, count in sorted(first_tool_counts.items(), key=lambda x: -x[1]):
        pct = count / len(first_bet_tools) * 100
        print(f"  {tool:45s}  {count:3d} ({pct:5.1f}%)")

    # --- Report: PRR lock-in timing ---
    lockin_bets = [r["prr_lockin_bet"] for r in all_agent_results if r.get("prr_lockin_bet")]
    prr_dominant = [r for r in all_agent_results
                    if r.get("window_10", {}).get("prediction-request-reasoning", 0) >= 60]
    print(f"\n### PRR Lock-in Analysis")
    print("-" * 70)
    if lockin_bets:
        print(f"  Agents where PRR reaches >75% cumulative: {len(lockin_bets)}/{len(all_agent_results)}")
        print(f"  Median bet at which PRR hits 75%: {statistics.median(lockin_bets):.0f}")
        print(f"  Mean: {statistics.mean(lockin_bets):.0f}, Min: {min(lockin_bets)}, Max: {max(lockin_bets)}")
    print(f"  Agents with PRR >= 60% in first 10 bets: {len(prr_dominant)}/{len(all_agent_results)}")

    # --- Report: Tool diversity in first 20 bets ---
    unique_counts = [r["unique_tools_first_20"] for r in all_agent_results]
    if unique_counts:
        print(f"\n### Tool Diversity in First 20 Bets")
        print("-" * 70)
        print(f"  Mean unique tools: {statistics.mean(unique_counts):.1f}")
        print(f"  Median: {statistics.median(unique_counts):.0f}")
        print(f"  Range: {min(unique_counts)} - {max(unique_counts)}")

    # --- Expected vs Actual ---
    print(f"\n### Expected vs Actual PRR Rate")
    print("-" * 70)
    print("  If IPFS store pre-loads PRR as best_tool:")
    print(f"    Expected PRR rate = 75% exploit + 25% * (1/N_tools) explore")
    print(f"    With ~10 tools: expected ~77.5% PRR")
    prr_first10 = [r.get("window_10", {}).get("prediction-request-reasoning", 0) for r in all_agent_results]
    prr_first50 = [r.get("window_50", {}).get("prediction-request-reasoning", 0) for r in all_agent_results]
    if prr_first10:
        print(f"    Actual PRR rate in first 10 bets: {statistics.mean(prr_first10):.1f}%")
    if prr_first50:
        print(f"    Actual PRR rate in first 50 bets: {statistics.mean(prr_first50):.1f}%")
    print("  If store started EMPTY (report's assumption):")
    print(f"    Expected PRR rate in first 10 = ~1/N_tools = ~10% (random)")


def main():
    parser = argparse.ArgumentParser(description="Verify accuracy store lock-in claims")
    parser.add_argument("--sample", type=int, default=30, help="Number of agents to sample")
    args = parser.parse_args()

    print("Fetching PolyStrat agent list...")
    agents = get_all_polystrat_agents()
    print(f"Found {len(agents)} agents")

    analyze_early_tool_usage(agents, sample_size=args.sample)


if __name__ == "__main__":
    main()
