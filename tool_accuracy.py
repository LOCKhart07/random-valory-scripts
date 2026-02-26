"""
Tool accuracy statistics script.

Fetches the last N resolved bets from the predict-omen subgraph, matches each bet
to its corresponding mech request (to identify which tool was used), and prints
per-tool accuracy statistics.

Usage:
    python tool_accuracy.py          # uses default of 100 bets
    python tool_accuracy.py 200      # custom number of bets
"""

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OLAS_MECH_SUBGRAPH_URL = (
    "https://api.subgraph.staging.autonolas.tech/api/proxy/mech-marketplace-gnosis"
)
PREDICT_OMEN_URL = "https://api.subgraph.staging.autonolas.tech/api/proxy/predict-omen"
QUESTION_DATA_SEPARATOR = "\u241f"

# Lookback window for mech requests (7 days)
MECH_LOOKBACK_SECONDS = 14 * 24 * 60 * 60

# Disk cache TTL: re-fetch an agent's requests if the cached data is older than this
MECH_CACHE_TTL_SECONDS = 60 * 60  # 1 hour

# Disk cache file (next to this script)
_CACHE_FILE = Path(__file__).parent / ".mech_cache.json"


def _load_cache() -> dict:
    """Load the disk cache from file. Returns an empty dict if file doesn't exist or is corrupt."""
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    """Persist the cache dict to disk."""
    _CACHE_FILE.write_text(json.dumps(cache))


# In-memory cache loaded from disk at startup
# Structure: {agent_address: {"fetched_at": <unix timestamp>, "requests": [...]}}
_mech_cache: dict = _load_cache()

# ---------------------------------------------------------------------------
# GraphQL queries
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

# ---------------------------------------------------------------------------
# Bets fetching
# ---------------------------------------------------------------------------


def fetch_last_bets(n: int = 100, batch_size: int = 1000) -> list[dict]:
    """
    Fetch the last `n` resolved bets from the predict-omen subgraph.
    Only includes bets where the market has a current (resolved) answer.
    Paginates automatically when `n` exceeds the subgraph page limit.
    """
    headers = {"Content-Type": "application/json"}
    all_raw_bets: list[dict] = []
    skip = 0

    while len(all_raw_bets) < n:
        first = min(batch_size, n - len(all_raw_bets))
        query = f"""
    {{
      bets(
        first: {first}
        skip: {skip}
        orderBy: timestamp
        orderDirection: desc
        where: {{fixedProductMarketMaker_: {{currentAnswer_not: null}}}}
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
        response = requests.post(
            PREDICT_OMEN_URL, headers=headers, json={"query": query}, timeout=30
        )
        response.raise_for_status()
        data = response.json()
        batch = data["data"]["bets"]
        if not batch:
            break
        all_raw_bets.extend(batch)
        if len(batch) < first:
            break
        skip += first

    formatted_bets = []
    for bet in all_raw_bets:
        chosen = int(bet["outcomeIndex"])
        correct = int(bet["fixedProductMarketMaker"]["currentAnswer"], 16)
        formatted_bets.append(
            {
                "bet_id": bet["id"],
                "timestamp": int(bet["timestamp"]),
                "bettor": bet["bettor"]["id"],
                "service_id": bet["bettor"]["serviceId"],
                "chosen_outcome": chosen,
                "correct_outcome": correct,
                "is_correct": chosen == correct,
                "question": bet["fixedProductMarketMaker"]["question"],
            }
        )
    return formatted_bets


# ---------------------------------------------------------------------------
# Mech requests fetching (with cache)
# ---------------------------------------------------------------------------


def fetch_all_mech_requests(
    agent_address: str,
    timestamp_gt: int = None,
    batch_size: int = 1000,
) -> list[dict]:
    """
    Fetch all mech requests for a given agent address from the subgraph.
    Paginates automatically. Defaults to the last 7 days.
    """
    if timestamp_gt is None:
        timestamp_gt = int(time.time()) - MECH_LOOKBACK_SECONDS

    all_requests = []
    skip = 0
    while True:
        variables = {
            "id": agent_address,
            "timestamp_gt": timestamp_gt,
            "skip": skip,
            "first": batch_size,
        }
        response = requests.post(
            OLAS_MECH_SUBGRAPH_URL,
            json={"query": GET_MECH_SENDER_QUERY, "variables": variables},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        result = data.get("data", {}).get("sender") or {}
        batch_requests = result.get("requests", [])
        if not batch_requests:
            break
        all_requests.extend(batch_requests)
        if len(batch_requests) < batch_size:
            break
        skip += batch_size
    return all_requests


def get_mech_requests_cached(
    agent_address: str, timestamp_gt: int = None
) -> list[dict]:
    """
    Returns mech requests for the given agent, using a disk-backed cache.
    Re-fetches if the cached entry is missing or older than MECH_CACHE_TTL_SECONDS.
    """
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
# Matching logic
# ---------------------------------------------------------------------------


def extract_question_title(question: str) -> str:
    """Extracts the question title using the separator found in production code."""
    if not question:
        return ""
    return question.split(QUESTION_DATA_SEPARATOR)[0]


def match_bet_to_mech_request(bet: dict, mech_requests: list[dict]) -> list[dict]:
    """
    Given a bet and a list of mech requests for the same agent, return the
    mech requests whose questionTitle matches the bet's question.

    The mech subgraph truncates questionTitle, so an exact match is not always
    possible. A match is accepted when either string is a prefix of the other
    (after stripping whitespace), e.g.:
      mech:    "...captain of the seized oil tanker "   (truncated)
      predict: "...captain of the seized oil tanker \"Grinch\" in connection..."
    """
    bet_title = extract_question_title(bet.get("question", "")).strip()
    if not bet_title:
        return []

    matched = []
    for req in mech_requests:
        mech_title = extract_question_title(
            (req.get("parsedRequest") or {}).get("questionTitle", "")
        ).strip()
        if not mech_title:
            continue
        if bet_title.startswith(mech_title) or mech_title.startswith(bet_title):
            matched.append(req)
    return matched


# ---------------------------------------------------------------------------
# Enrichment and statistics
# ---------------------------------------------------------------------------


def enrich_bets_with_tool(bets: list[dict]) -> list[dict]:
    """
    For each bet, fetch the corresponding agent's mech requests (cached),
    match by question title, and attach the `tool` field.
    Unmatched bets get tool = "unknown".
    """
    enriched = []
    for bet in bets:
        agent_address = bet["bettor"]
        mech_requests = get_mech_requests_cached(agent_address)
        matches = match_bet_to_mech_request(bet, mech_requests)

        if matches:
            # Pick the latest mech request that was made before the bet was placed.
            bet_ts = bet.get("timestamp", 0)
            # print("Matched bet to mech requests, bet timestamp:", bet_ts)

            before_bet = [
                r for r in matches if int(r.get("blockTimestamp") or 0) <= bet_ts
            ]
            chosen = (
                max(before_bet, key=lambda r: int(r.get("blockTimestamp") or 0))
                if before_bet
                else matches[0]
            )
            # print("Chosen mech request timestamp:", chosen.get("blockTimestamp"))
            tool = (chosen.get("parsedRequest") or {}).get("tool") or "unknown"
        else:
            tool = "unknown"

        enriched.append({**bet, "tool": tool})
    return enriched


def compute_tool_accuracy(enriched_bets: list[dict]) -> list[dict]:
    """
    Groups enriched bets by tool and computes accuracy statistics for each.
    Returns a list of dicts sorted by total bets descending.
    """
    totals: dict[str, int] = defaultdict(int)
    corrects: dict[str, int] = defaultdict(int)

    for bet in enriched_bets:
        tool = bet["tool"]
        totals[tool] += 1
        if bet["is_correct"]:
            corrects[tool] += 1

    stats = []
    for tool, total in totals.items():
        correct = corrects[tool]
        stats.append(
            {
                "tool": tool,
                "total": total,
                "correct": correct,
                "accuracy": round(correct / total * 100, 1) if total > 0 else 0.0,
            }
        )

    return sorted(stats, key=lambda x: x["total"], reverse=True)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_stats(stats: list[dict], total_bets: int) -> None:
    """Prints a formatted table of per-tool accuracy statistics."""
    col_tool = max(len(s["tool"]) for s in stats)
    col_tool = max(col_tool, 4)  # min width for "Tool" header

    header = f"{'Tool':<{col_tool}} | {'Total':>7} | {'Correct':>7} | {'Accuracy':>8}"
    separator = "-" * len(header)

    print(f"\nTool accuracy statistics ({total_bets} bets fetched)")
    print(separator)
    print(header)
    print(separator)
    for s in stats:
        print(
            f"{s['tool']:<{col_tool}} | {s['total']:>7} | {s['correct']:>7} | {s['accuracy']:>7.1f}%"
        )
    print(separator)

    # Overall accuracy excluding "unknown"
    known = [s for s in stats if s["tool"] != "unknown"]
    if known:
        total_known = sum(s["total"] for s in known)
        correct_known = sum(s["correct"] for s in known)
        overall_pct = (
            round(correct_known / total_known * 100, 1) if total_known > 0 else 0.0
        )
        print(
            f"\nOverall (known tools): {correct_known}/{total_known} correct "
            f"({overall_pct}% accuracy)"
        )

    unknown_stats = next((s for s in stats if s["tool"] == "unknown"), None)
    if unknown_stats:
        print(f"Unmatched bets (no mech request found): {unknown_stats['total']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(n: int = 100) -> None:
    print(f"Fetching last {n} resolved bets...")
    bets = fetch_last_bets(n)
    unique_agents = len({bet["bettor"] for bet in bets})
    print(
        f"Fetched {len(bets)} bets from {unique_agents} unique agents. Enriching with mech tool data..."
    )

    enriched = enrich_bets_with_tool(bets)
    print(f"Enriched {len(enriched)} bets. Computing statistics...")

    stats = compute_tool_accuracy(enriched)
    print_stats(stats, total_bets=len(enriched))


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    main(n)
