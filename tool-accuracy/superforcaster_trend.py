"""
Superforcaster accuracy trend analysis for Polymarket.

Answers: is superforcaster accuracy actually degrading, or is it converging
to its true value as bet count grows?

Computes and plots:
  1. Cumulative accuracy over time (running total of correct / total)
  2. Rolling-window accuracy (e.g. last 50 bets at each point)
  3. Weekly binned accuracy with bet counts

If accuracy is truly degrading, the rolling window will trend downward.
If it's just converging, the rolling window will stay roughly flat while
cumulative accuracy flattens out.

Usage:
    python superforcaster_trend.py                # all time
    python superforcaster_trend.py --days 90      # last 90 days
    python superforcaster_trend.py --window 30    # rolling window of 30 bets
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.exceptions import ConnectionError, Timeout

try:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    _HAS_MATPLOTLIB = True
except ImportError:
    _HAS_MATPLOTLIB = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OLAS_MECH_SUBGRAPH_URL = (
    "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"
)
PREDICT_POLYMARKET_URL = (
    "https://predict-polymarket-agents.subgraph.autonolas.tech/"
)
QUESTION_DATA_SEPARATOR = "\u241f"

MECH_LOOKBACK_SECONDS = 180 * 24 * 60 * 60  # 180 days
MECH_CACHE_TTL_SECONDS = 60 * 60  # 1 hour

_CACHE_FILE = Path(__file__).parent / ".superforcaster_trend_cache.json"

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3

DEFAULT_ROLLING_WINDOW = 50

# ---------------------------------------------------------------------------
# HTTP / cache helpers
# ---------------------------------------------------------------------------


def _post_with_retry(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (Timeout, ConnectionError, requests.HTTPError) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            print(f"    [retry {attempt}/{MAX_RETRIES - 1}] {exc}, retrying in {wait}s")
            time.sleep(wait)
    raise last_exc


def _load_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE_FILE.write_text(json.dumps(cache))


_cache: dict = _load_cache()


# ---------------------------------------------------------------------------
# Bets fetching
# ---------------------------------------------------------------------------


def fetch_all_resolved_bets(min_ts: int = 0) -> list[dict]:
    """Fetch all resolved Polymarket bets, optionally from min_ts onward."""
    cache_key = f"bets:{min_ts}"
    entry = _cache.get(cache_key)
    if entry and (time.time() - entry["fetched_at"]) < MECH_CACHE_TTL_SECONDS:
        print(f"  Using cached bets (fetched {int(time.time()) - entry['fetched_at']}s ago).")
        return entry["bets"]

    headers = {"Content-Type": "application/json"}
    resolved = []
    last_id = None

    while True:
        where_clause = ""
        if last_id:
            where_clause = f', where: {{ id_lt: "{last_id}" }}'

        query = f"""
        {{
          bets(
            first: 1000
            orderBy: blockTimestamp
            orderDirection: desc
            {where_clause}
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

        resp = _post_with_retry(
            PREDICT_POLYMARKET_URL,
            headers=headers,
            json={"query": query},
        )
        data = resp.json()
        batch = data["data"]["bets"]
        if not batch:
            break

        last_id = batch[-1]["id"]
        cutoff_reached = False

        for bet in batch:
            resolution = bet["question"]["resolution"]
            if resolution is None:
                continue

            ts = int(bet["blockTimestamp"])
            if ts < min_ts:
                cutoff_reached = True
                break

            chosen = int(bet["outcomeIndex"])
            correct = int(resolution["winningIndex"])

            resolved.append({
                "bet_id": bet["id"],
                "timestamp": ts,
                "bettor": bet["bettor"]["id"],
                "service_id": int(bet["bettor"]["serviceId"]),
                "chosen_outcome": chosen,
                "correct_outcome": correct,
                "is_correct": chosen == correct,
                "question_id": bet["question"]["id"],
                "question_title": bet["question"]["metadata"]["title"],
            })

        if cutoff_reached:
            break

    # Sort chronologically
    resolved.sort(key=lambda b: b["timestamp"])

    # Cache the results
    _cache[cache_key] = {"fetched_at": int(time.time()), "bets": resolved}
    _save_cache(_cache)

    return resolved


# ---------------------------------------------------------------------------
# Mech requests fetching
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


def fetch_all_mech_requests(agent_address: str, timestamp_gt: int) -> list[dict]:
    all_requests = []
    skip = 0
    while True:
        variables = {
            "id": agent_address,
            "timestamp_gt": timestamp_gt,
            "skip": skip,
            "first": 1000,
        }
        resp = _post_with_retry(
            OLAS_MECH_SUBGRAPH_URL,
            json={"query": GET_MECH_SENDER_QUERY, "variables": variables},
            headers={"Content-Type": "application/json"},
        )
        data = resp.json()
        result = (data.get("data") or {}).get("sender") or {}
        batch = result.get("requests", [])
        if not batch:
            break
        all_requests.extend(batch)
        if len(batch) < 1000:
            break
        skip += 1000
    return all_requests


def get_mech_requests_cached(agent_address: str, timestamp_gt: int) -> list[dict]:
    key = f"mech:{agent_address}"
    entry = _cache.get(key)
    if (
        entry is None
        or timestamp_gt < entry.get("fetched_from", timestamp_gt + 1)
        or (time.time() - entry["fetched_at"]) > MECH_CACHE_TTL_SECONDS
    ):
        reqs = fetch_all_mech_requests(agent_address, timestamp_gt)
        _cache[key] = {
            "fetched_at": int(time.time()),
            "fetched_from": timestamp_gt,
            "requests": reqs,
        }
        _save_cache(_cache)
    return _cache[key]["requests"]


# ---------------------------------------------------------------------------
# Matching + enrichment
# ---------------------------------------------------------------------------


def extract_question_title(question: str) -> str:
    if not question:
        return ""
    return question.split(QUESTION_DATA_SEPARATOR)[0]


def match_bet_to_mech_request(bet: dict, mech_requests: list[dict]) -> list[dict]:
    bet_title = extract_question_title(bet.get("question_title", "")).strip()
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


def enrich_bets_with_tool(bets: list[dict]) -> list[dict]:
    agents = {bet["bettor"] for bet in bets}
    earliest_ts = min(b["timestamp"] for b in bets) if bets else int(time.time())
    mech_from = earliest_ts - 7 * 24 * 3600  # 7 days before earliest bet

    print(f"  Fetching mech requests for {len(agents)} agents...")
    agent_requests: dict[str, list[dict]] = {}
    for i, agent in enumerate(agents, 1):
        if i % 20 == 0 or i == len(agents):
            print(f"    Agent {i}/{len(agents)}...")
        try:
            agent_requests[agent] = get_mech_requests_cached(agent, mech_from)
        except Exception as exc:
            print(f"    [warn] Failed for {agent}: {exc}")
            agent_requests[agent] = []

    enriched = []
    for bet in bets:
        mech_reqs = agent_requests[bet["bettor"]]
        matches = match_bet_to_mech_request(bet, mech_reqs)

        if matches:
            bet_ts = bet["timestamp"]
            before_bet = [
                r for r in matches if int(r.get("blockTimestamp") or 0) <= bet_ts
            ]
            chosen = (
                max(before_bet, key=lambda r: int(r.get("blockTimestamp") or 0))
                if before_bet
                else matches[0]
            )
            tool = (chosen.get("parsedRequest") or {}).get("tool") or "unknown"
        else:
            tool = "unknown"

        enriched.append({**bet, "tool": tool})
    return enriched


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def compute_cumulative_accuracy(bets: list[dict]) -> list[dict]:
    """Returns a list of {timestamp, bet_number, cumulative_accuracy, is_correct}."""
    points = []
    correct = 0
    for i, bet in enumerate(bets):
        if bet["is_correct"]:
            correct += 1
        points.append({
            "timestamp": bet["timestamp"],
            "bet_number": i + 1,
            "cumulative_accuracy": round(correct / (i + 1) * 100, 2),
            "is_correct": bet["is_correct"],
        })
    return points


def compute_rolling_accuracy(bets: list[dict], window: int) -> list[dict]:
    """Returns rolling window accuracy starting from the window-th bet."""
    points = []
    for i in range(window - 1, len(bets)):
        window_bets = bets[i - window + 1: i + 1]
        correct = sum(1 for b in window_bets if b["is_correct"])
        points.append({
            "timestamp": bets[i]["timestamp"],
            "bet_number": i + 1,
            "rolling_accuracy": round(correct / window * 100, 2),
        })
    return points


def compute_weekly_bins(bets: list[dict]) -> list[dict]:
    """Bin bets into calendar weeks."""
    if not bets:
        return []

    weekly: dict[str, dict] = {}
    for bet in bets:
        dt = datetime.fromtimestamp(bet["timestamp"], tz=timezone.utc)
        week_start = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
        if week_start not in weekly:
            weekly[week_start] = {"total": 0, "correct": 0, "timestamp": bet["timestamp"]}
        weekly[week_start]["total"] += 1
        if bet["is_correct"]:
            weekly[week_start]["correct"] += 1

    bins = []
    for week, stats in sorted(weekly.items()):
        bins.append({
            "week": week,
            "timestamp": stats["timestamp"],
            "total": stats["total"],
            "correct": stats["correct"],
            "accuracy": round(stats["correct"] / stats["total"] * 100, 1),
        })
    return bins


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_analysis(
    sf_bets: list[dict],
    cumulative: list[dict],
    rolling: list[dict],
    weekly: list[dict],
    window: int,
) -> None:
    n = len(sf_bets)
    total_correct = sum(1 for b in sf_bets if b["is_correct"])
    overall_acc = round(total_correct / n * 100, 1) if n else 0

    print(f"\n{'=' * 65}")
    print(f"  SUPERFORCASTER ACCURACY TREND ANALYSIS")
    print(f"{'=' * 65}")
    print(f"  Total resolved bets: {n}")
    print(f"  Overall accuracy:    {total_correct}/{n} ({overall_acc}%)")

    if len(sf_bets) >= 2:
        first_dt = datetime.fromtimestamp(sf_bets[0]["timestamp"], tz=timezone.utc)
        last_dt = datetime.fromtimestamp(sf_bets[-1]["timestamp"], tz=timezone.utc)
        print(f"  Date range:          {first_dt:%Y-%m-%d} to {last_dt:%Y-%m-%d}")

    # Split into halves to detect trend
    if n >= 10:
        mid = n // 2
        first_half = sf_bets[:mid]
        second_half = sf_bets[mid:]
        acc_first = round(sum(1 for b in first_half if b["is_correct"]) / len(first_half) * 100, 1)
        acc_second = round(sum(1 for b in second_half if b["is_correct"]) / len(second_half) * 100, 1)
        print(f"\n  First half  (bets 1-{mid}):      {acc_first}%")
        print(f"  Second half (bets {mid + 1}-{n}):  {acc_second}%")
        diff = acc_second - acc_first
        if abs(diff) < 2:
            print(f"  Delta: {diff:+.1f}pp => Stable (likely convergence)")
        elif diff < 0:
            print(f"  Delta: {diff:+.1f}pp => Degrading")
        else:
            print(f"  Delta: {diff:+.1f}pp => Improving")

    # Thirds for finer granularity
    if n >= 30:
        t = n // 3
        thirds = [sf_bets[:t], sf_bets[t:2*t], sf_bets[2*t:]]
        accs = [round(sum(1 for b in s if b["is_correct"]) / len(s) * 100, 1) for s in thirds]
        print(f"\n  Thirds breakdown:")
        print(f"    First third  (1-{t}):          {accs[0]}%")
        print(f"    Middle third ({t+1}-{2*t}):     {accs[1]}%")
        print(f"    Last third   ({2*t+1}-{n}):     {accs[2]}%")

        trend = accs[2] - accs[0]
        if accs[0] > accs[1] > accs[2]:
            print(f"    Monotonic decline ({trend:+.1f}pp) => True degradation")
        elif abs(trend) < 3 and abs(accs[1] - accs[0]) < 5:
            print(f"    Roughly flat ({trend:+.1f}pp) => Convergence")
        else:
            print(f"    Mixed pattern ({trend:+.1f}pp)")

    # Rolling window stats
    if rolling:
        recent_rolling = rolling[-1]["rolling_accuracy"]
        peak_rolling = max(r["rolling_accuracy"] for r in rolling)
        trough_rolling = min(r["rolling_accuracy"] for r in rolling)
        print(f"\n  Rolling {window}-bet window:")
        print(f"    Current:  {recent_rolling}%")
        print(f"    Peak:     {peak_rolling}%")
        print(f"    Trough:   {trough_rolling}%")
        print(f"    Range:    {round(peak_rolling - trough_rolling, 1)}pp")

    # Weekly breakdown
    if weekly:
        print(f"\n  Weekly breakdown:")
        print(f"  {'Week':<12} | {'Bets':>5} | {'Correct':>7} | {'Accuracy':>8}")
        print(f"  {'-' * 42}")
        for w in weekly:
            print(f"  {w['week']:<12} | {w['total']:>5} | {w['correct']:>7} | {w['accuracy']:>7.1f}%")


def plot_trend(
    sf_bets: list[dict],
    cumulative: list[dict],
    rolling: list[dict],
    weekly: list[dict],
    window: int,
) -> None:
    if not _HAS_MATPLOTLIB:
        print("\nmatplotlib not installed — skipping chart.")
        return

    fig, axes = plt.subplots(2, 1, figsize=(14, 10), sharex=False)

    # --- Panel 1: Cumulative + Rolling accuracy by bet number ---
    ax1 = axes[0]
    cum_x = [p["bet_number"] for p in cumulative]
    cum_y = [p["cumulative_accuracy"] for p in cumulative]
    ax1.plot(cum_x, cum_y, color="steelblue", linewidth=2, label="Cumulative accuracy")

    if rolling:
        roll_x = [p["bet_number"] for p in rolling]
        roll_y = [p["rolling_accuracy"] for p in rolling]
        ax1.plot(roll_x, roll_y, color="darkorange", linewidth=1.5, alpha=0.8,
                 label=f"Rolling {window}-bet accuracy")

    ax1.axhline(50, color="red", linestyle=":", linewidth=1, alpha=0.5, label="50% (random)")
    ax1.set_ylabel("Accuracy (%)")
    ax1.set_xlabel("Bet number (chronological)")
    ax1.set_title("Superforcaster Accuracy: Cumulative vs Rolling Window")
    ax1.legend(loc="lower left", fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(0, 100)

    # --- Panel 2: Weekly accuracy with bet count bars ---
    ax2 = axes[1]
    if weekly:
        weeks = [datetime.strptime(w["week"], "%Y-%m-%d") for w in weekly]
        accs = [w["accuracy"] for w in weekly]
        counts = [w["total"] for w in weekly]

        ax2b = ax2.twinx()
        ax2b.bar(weeks, counts, width=5, alpha=0.2, color="gray", label="Bet count")
        ax2b.set_ylabel("Bets per week", color="gray")

        ax2.plot(weeks, accs, color="steelblue", marker="o", markersize=5,
                 linewidth=2, label="Weekly accuracy", zorder=5)
        ax2.axhline(50, color="red", linestyle=":", linewidth=1, alpha=0.5)

        ax2.set_ylabel("Accuracy (%)")
        ax2.set_xlabel("Week")
        ax2.set_title("Superforcaster Weekly Accuracy + Volume")
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax2.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=40, ha="right")
        ax2.set_ylim(0, 100)
        ax2.grid(True, alpha=0.3)

        # Combine legends
        lines1, labels1 = ax2.get_legend_handles_labels()
        lines2, labels2 = ax2b.get_legend_handles_labels()
        ax2.legend(lines1 + lines2, labels1 + labels2, loc="lower left", fontsize=9)

    fig.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze superforcaster accuracy trend on Polymarket."
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Only look at the last N days (default: all time).",
    )
    parser.add_argument(
        "--window", type=int, default=DEFAULT_ROLLING_WINDOW,
        help=f"Rolling window size in bets (default: {DEFAULT_ROLLING_WINDOW}).",
    )
    args = parser.parse_args()

    min_ts = 0
    if args.days:
        min_ts = int(time.time()) - args.days * 86400

    print("[1/3] Fetching all resolved Polymarket bets...")
    bets = fetch_all_resolved_bets(min_ts)
    print(f"  {len(bets)} resolved bets fetched.")

    if not bets:
        print("No resolved bets found.")
        sys.exit(0)

    print("\n[2/3] Enriching bets with mech tool data...")
    enriched = enrich_bets_with_tool(bets)

    # Filter to superforcaster only
    sf_bets = [b for b in enriched if b["tool"] == "superforcaster"]
    print(f"  {len(sf_bets)} superforcaster bets found out of {len(enriched)} total.")

    if not sf_bets:
        # Show what tools exist
        tools = defaultdict(int)
        for b in enriched:
            tools[b["tool"]] += 1
        print("\n  Available tools:")
        for tool, count in sorted(tools.items(), key=lambda x: -x[1]):
            print(f"    {tool}: {count} bets")
        sys.exit(0)

    print("\n[3/3] Analyzing trend...")
    cumulative = compute_cumulative_accuracy(sf_bets)
    rolling = compute_rolling_accuracy(sf_bets, args.window)
    weekly = compute_weekly_bins(sf_bets)

    print_analysis(sf_bets, cumulative, rolling, weekly, args.window)
    plot_trend(sf_bets, cumulative, rolling, weekly, args.window)


if __name__ == "__main__":
    main()
