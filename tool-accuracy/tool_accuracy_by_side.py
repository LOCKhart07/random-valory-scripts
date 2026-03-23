"""
Tool accuracy broken down by betting side (Yes/No) for Polymarket.

Fetches resolved bets from the last N days, matches to mech requests for tool
identification, and prints per-tool accuracy split by which side was bet on.

Usage:
    python tool-accuracy/tool_accuracy_by_side.py              # last 30 days
    python tool-accuracy/tool_accuracy_by_side.py --days 7     # last 7 days
    python tool-accuracy/tool_accuracy_by_side.py --exclude-valory  # exclude Valory agents
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OLAS_MECH_SUBGRAPH_URL = (
    "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"
)
PREDICT_POLYMARKET_URL = "https://predict-polymarket-agents.subgraph.autonolas.tech/"
QUESTION_DATA_SEPARATOR = "\u241f"

MECH_LOOKBACK_SECONDS = 70 * 24 * 60 * 60
MECH_CACHE_TTL_SECONDS = 60 * 60
_CACHE_FILE = Path(__file__).parent / ".mech_cache.json"

SIDE_LABELS = {0: "Yes", 1: "No"}

# ---------------------------------------------------------------------------
# Valory agent list (reused from analyze_tool_usage.py)
# ---------------------------------------------------------------------------

VALORY_AGENTS = {
    "0x33d20338f1700eda034ea2543933f94a2177ae4c",
    "0x1c7d7dcf45d82050adb2ea79a8063538f41f1d42",
    "0x9070a951ca59a6fdd92e3de526a1dc5f0e1b5f6d",
    "0x7e0a49085f485c6e94b6e03cff7dfb7071090917",
    "0x21cfdb1ee005e3f5f50db5d87fda0eea0c9f97c9",
    "0xa9ab04315b7ebeaf68d48fd250e37083c9622f5a",
    "0x28c51c6c51f56261e5c2e73d0e634dfe3ae78f37",
    "0xe07760c3ae1d94c661c0c867ff75e6a35c9f0b62",
    "0xe72625770f41db05c28b0b07bac1c65f5f64cc29",
    "0x984974e8c1c37d3a8ed8f0cdfc64c60e0fe4e3cd",
    "0x0558c234c78a07ddb27abe033b22d7e56bbebc1c",
    "0x9aa566d13c5a31cd5f84e1f63f76e6ba99a6a30c",
    "0x89826819d6dc033b1f40ec3dce69fdab4e79a63a",
    "0x375c956d6adf81c44f6f6a95dd72c5c0a02f3a96",
    "0xd8da8f33b94b6fbaec0c7e36b40f36f47ed9a97a",
    "0xd5870309b0e61de3b82cb6f1f5bfb67c39ae9e07",
    "0x44ddf64e2e06f18b2cb6ffab67e02b6cff8f16c3",
    "0x48732c4eee90b2cd32e08d70ae7b1c23d0bad044",
    "0xf353d9e87f48fee69b1bb2917d9fa90d6a63dd60",
}

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _load_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE_FILE.write_text(json.dumps(cache))


_mech_cache: dict = _load_cache()

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

GET_MECH_SENDER_QUERY = """
query MechSender($id: ID!, $timestamp_gt: Int!, $skip: Int, $first: Int) {
    sender(id: $id) {
        totalMarketplaceRequests
        requests(first: $first, skip: $skip, where: { blockTimestamp_gt: $timestamp_gt }) {
            blockTimestamp
            parsedRequest {
                questionTitle
                tool
                prompt
            }
        }
    }
}
"""


def _post_with_retry(url, payload, max_retries=4, base_delay=3):
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == max_retries:
                raise
            time.sleep(base_delay * (2 ** attempt))


# ---------------------------------------------------------------------------
# Bets fetching
# ---------------------------------------------------------------------------


def fetch_bets_since(since_ts: int, batch_size: int = 1000) -> list[dict]:
    resolved_bets = []
    last_id = None

    while True:
        where_clause = f'blockTimestamp_gte: {since_ts}'
        if last_id:
            where_clause += f', id_lt: "{last_id}"'

        query = f"""
        {{
          bets(
            first: {batch_size}
            orderBy: id
            orderDirection: desc
            where: {{ {where_clause} }}
          ) {{
            id
            blockTimestamp
            outcomeIndex

            bettor {{
              id
              serviceId
            }}

            question {{
              id
              metadata {{
                title
              }}
              resolution {{
                winningIndex
              }}
            }}
          }}
        }}
        """

        data = _post_with_retry(
            PREDICT_POLYMARKET_URL,
            {"query": query},
        )

        batch = data["data"]["bets"]
        if not batch:
            break

        last_id = batch[-1]["id"]

        for bet in batch:
            resolution = bet["question"]["resolution"]
            if resolution is None:
                continue

            chosen = int(bet["outcomeIndex"])
            correct = int(resolution["winningIndex"])

            resolved_bets.append({
                "bet_id": bet["id"],
                "timestamp": int(bet["blockTimestamp"]),
                "bettor": bet["bettor"]["id"],
                "service_id": int(bet["bettor"]["serviceId"]),
                "chosen_outcome": chosen,
                "correct_outcome": correct,
                "is_correct": chosen == correct,
                "question_id": bet["question"]["id"],
                "question_title": bet["question"]["metadata"]["title"],
            })

        if len(batch) < batch_size:
            break

    return resolved_bets


# ---------------------------------------------------------------------------
# Mech requests
# ---------------------------------------------------------------------------


def fetch_all_mech_requests(agent_address: str, timestamp_gt: int = None) -> list[dict]:
    if timestamp_gt is None:
        timestamp_gt = int(time.time()) - MECH_LOOKBACK_SECONDS

    all_requests = []
    skip = 0
    batch_size = 1000

    while True:
        payload = {
            "query": GET_MECH_SENDER_QUERY,
            "variables": {
                "id": agent_address,
                "timestamp_gt": timestamp_gt,
                "first": batch_size,
                "skip": skip,
            },
        }
        data = _post_with_retry(OLAS_MECH_SUBGRAPH_URL, payload)
        result = data.get("data", {}).get("sender") or {}
        batch_requests = result.get("requests", [])
        if not batch_requests:
            break
        all_requests.extend(batch_requests)
        if len(batch_requests) < batch_size:
            break
        skip += batch_size
    return all_requests


def get_mech_requests_cached(agent_address: str, timestamp_gt: int = None) -> list[dict]:
    entry = _mech_cache.get(agent_address)
    if entry is None or (time.time() - entry["fetched_at"]) > MECH_CACHE_TTL_SECONDS:
        requests_data = fetch_all_mech_requests(agent_address, timestamp_gt)
        _mech_cache[agent_address] = {
            "fetched_at": int(time.time()),
            "requests": requests_data,
        }
        _save_cache(_mech_cache)
    return _mech_cache[agent_address]["requests"]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def extract_question_title(question: str) -> str:
    if not question:
        return ""
    return question.split(QUESTION_DATA_SEPARATOR)[0]


def match_bet_to_tool(bet: dict, mech_requests: list[dict]) -> str:
    bet_title = extract_question_title(bet.get("question_title", "")).strip()
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


def enrich_bets_with_tool(bets: list[dict]) -> list[dict]:
    enriched = []
    for bet in bets:
        tool = match_bet_to_tool(bet, get_mech_requests_cached(bet["bettor"]))
        enriched.append({**bet, "tool": tool})
    return enriched


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze_by_side(enriched_bets: list[dict]) -> None:
    # Structure: tool -> side -> {total, correct}
    stats = defaultdict(lambda: defaultdict(lambda: {"total": 0, "correct": 0}))
    tool_totals = defaultdict(lambda: {"total": 0, "correct": 0})

    for bet in enriched_bets:
        tool = bet["tool"]
        if tool == "unknown":
            continue
        side = bet["chosen_outcome"]
        stats[tool][side]["total"] += 1
        stats[tool][side]["correct"] += 1 if bet["is_correct"] else 0
        tool_totals[tool]["total"] += 1
        tool_totals[tool]["correct"] += 1 if bet["is_correct"] else 0

    # Sort by total bets descending
    sorted_tools = sorted(tool_totals.keys(), key=lambda t: tool_totals[t]["total"], reverse=True)

    # Print header
    print()
    print("=" * 100)
    print("  TOOL ACCURACY BY BETTING SIDE (Polymarket)")
    print("=" * 100)

    # Summary table
    col = 36
    print(f"\n{'Tool':<{col}} | {'Total':>7} | {'Acc':>6} | {'Yes Bets':>8} | {'Yes Acc':>7} | {'No Bets':>7} | {'No Acc':>7} | {'Yes %':>5}")
    print("-" * 100)

    for tool in sorted_tools:
        t = tool_totals[tool]
        yes = stats[tool][0]
        no = stats[tool][1]
        total_acc = f"{t['correct']/t['total']*100:.1f}%" if t["total"] else "N/A"
        yes_acc = f"{yes['correct']/yes['total']*100:.1f}%" if yes["total"] else "N/A"
        no_acc = f"{no['correct']/no['total']*100:.1f}%" if no["total"] else "N/A"
        yes_pct = f"{yes['total']/t['total']*100:.0f}%" if t["total"] else "N/A"

        print(
            f"{tool:<{col}} | {t['total']:>7} | {total_acc:>6} | {yes['total']:>8} | {yes_acc:>7} | {no['total']:>7} | {no_acc:>7} | {yes_pct:>5}"
        )

    print("-" * 100)

    # Overall
    all_total = sum(t["total"] for t in tool_totals.values())
    all_correct = sum(t["correct"] for t in tool_totals.values())
    all_yes = sum(stats[t][0]["total"] for t in sorted_tools)
    all_yes_correct = sum(stats[t][0]["correct"] for t in sorted_tools)
    all_no = sum(stats[t][1]["total"] for t in sorted_tools)
    all_no_correct = sum(stats[t][1]["correct"] for t in sorted_tools)

    overall_acc = f"{all_correct/all_total*100:.1f}%" if all_total else "N/A"
    yes_acc = f"{all_yes_correct/all_yes*100:.1f}%" if all_yes else "N/A"
    no_acc = f"{all_no_correct/all_no*100:.1f}%" if all_no else "N/A"
    yes_pct = f"{all_yes/all_total*100:.0f}%" if all_total else "N/A"

    print(
        f"{'OVERALL':<{col}} | {all_total:>7} | {overall_acc:>6} | {all_yes:>8} | {yes_acc:>7} | {all_no:>7} | {no_acc:>7} | {yes_pct:>5}"
    )

    # Head-to-head: superforcaster vs PRR
    h2h_tools = ["prediction-request-reasoning", "superforcaster"]
    h2h_present = [t for t in h2h_tools if t in tool_totals]
    if len(h2h_present) == 2:
        print()
        print("=" * 100)
        print("  HEAD-TO-HEAD: prediction-request-reasoning vs superforcaster")
        print("=" * 100)

        for tool in h2h_present:
            t = tool_totals[tool]
            yes = stats[tool][0]
            no = stats[tool][1]
            print(f"\n  {tool}")
            print(f"    Overall:  {t['correct']}/{t['total']} = {t['correct']/t['total']*100:.1f}%")
            if yes["total"]:
                print(f"    Yes bets: {yes['correct']}/{yes['total']} = {yes['correct']/yes['total']*100:.1f}%  ({yes['total']/t['total']*100:.0f}% of bets)")
            if no["total"]:
                print(f"    No bets:  {no['correct']}/{no['total']} = {no['correct']/no['total']*100:.1f}%  ({no['total']/t['total']*100:.0f}% of bets)")

    # Winning outcome distribution
    print()
    print("=" * 100)
    print("  MARKET RESOLUTION DISTRIBUTION")
    print("=" * 100)
    yes_wins = sum(1 for b in enriched_bets if b.get("tool", "unknown") != "unknown" and b["correct_outcome"] == 0)
    no_wins = sum(1 for b in enriched_bets if b.get("tool", "unknown") != "unknown" and b["correct_outcome"] == 1)
    total_resolved = yes_wins + no_wins
    if total_resolved:
        print(f"\n  Markets resolved Yes: {yes_wins}/{total_resolved} ({yes_wins/total_resolved*100:.1f}%)")
        print(f"  Markets resolved No:  {no_wins}/{total_resolved} ({no_wins/total_resolved*100:.1f}%)")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Tool accuracy by betting side")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
    parser.add_argument("--exclude-valory", action="store_true", help="Exclude Valory-owned agents")
    args = parser.parse_args()

    since_ts = int(time.time()) - args.days * 24 * 60 * 60
    since_str = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    print(f"Fetching resolved bets since {since_str} ({args.days} days)...")

    bets = fetch_bets_since(since_ts)
    print(f"Fetched {len(bets)} resolved bets from {len({b['bettor'] for b in bets})} agents.")

    if args.exclude_valory:
        before = len(bets)
        bets = [b for b in bets if b["bettor"] not in VALORY_AGENTS]
        print(f"Excluded {before - len(bets)} Valory agent bets, {len(bets)} remaining.")

    print("Enriching with mech tool data...")
    enriched = enrich_bets_with_tool(bets)

    unknown_count = sum(1 for b in enriched if b["tool"] == "unknown")
    print(f"Enriched {len(enriched)} bets ({unknown_count} unmatched).")

    analyze_by_side(enriched)


if __name__ == "__main__":
    main()
