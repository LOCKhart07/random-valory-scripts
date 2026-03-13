"""
Comprehensive PolyStrat agent performance analysis.

Fetches all bets, mech requests, and aggregate stats for a single agent
and produces a diagnostic report covering accuracy, tool usage, bet sizing,
and temporal trends.

Usage:
    python polymarket/analyze_agent.py 0x33d20338f1700eda034ea2543933f94a2177ae4c
    python polymarket/analyze_agent.py 0x33d20338f1700eda034ea2543933f94a2177ae4c --json
"""

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLYMARKET_BETS_SUBGRAPH_URL = (
    "https://predict-polymarket-agents.subgraph.autonolas.tech/"
)
OLAS_MECH_SUBGRAPH_URL = (
    "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"
)

USDC_DECIMALS_DIVISOR = 1_000_000
PERCENTAGE_FACTOR = 100.0
QUESTION_DATA_SEPARATOR = "\u241f"

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3

# How far back to look for mech requests (days)
MECH_LOOKBACK_DAYS = 180


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post_with_retry(url: str, **kwargs) -> requests.Response:
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
            wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            print(f"  [retry {attempt}/{MAX_RETRIES - 1}] {exc}, retrying in {wait}s...")
            time.sleep(wait)
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
# Data fetching
# ---------------------------------------------------------------------------


def fetch_agent_bets(safe_address: str) -> list[dict]:
    """Fetch all bets for an agent, including question metadata and timestamps."""
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
        metadata {
          title
        }
        resolution {
          winningIndex
        }
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


def fetch_trader_agent(safe_address: str) -> dict | None:
    """Fetch aggregate trading stats."""
    query = """
query GetTrader($id: ID!) {
  traderAgent(id: $id) {
    serviceId
    totalBets
    totalPayout
    totalTraded
    totalTradedSettled
  }
}
"""
    response = call_subgraph(POLYMARKET_BETS_SUBGRAPH_URL, query, {"id": safe_address})
    return response.get("data", {}).get("traderAgent")


def fetch_mech_requests(safe_address: str) -> list[dict]:
    """Fetch mech requests for the agent from the polygon marketplace subgraph."""
    query = """
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
    timestamp_gt = int(time.time()) - MECH_LOOKBACK_DAYS * 86400
    all_requests = []
    skip = 0
    batch_size = 1000
    while True:
        variables = {
            "id": safe_address,
            "timestamp_gt": timestamp_gt,
            "skip": skip,
            "first": batch_size,
        }
        data = call_subgraph(OLAS_MECH_SUBGRAPH_URL, query, variables)
        result = (data.get("data") or {}).get("sender") or {}
        batch = result.get("requests", [])
        if not batch:
            break
        all_requests.extend(batch)
        if len(batch) < batch_size:
            break
        skip += batch_size
    return all_requests


# ---------------------------------------------------------------------------
# Bet classification helpers
# ---------------------------------------------------------------------------


def classify_bets(bets: list[dict]) -> dict:
    """Split bets into resolved/pending and compute win/loss."""
    resolved = []
    pending = []
    for bet in bets:
        resolution = (bet.get("question") or {}).get("resolution")
        if resolution is not None:
            winning_index = resolution.get("winningIndex")
            if winning_index is not None and int(winning_index) >= 0:
                outcome_index = int(bet["outcomeIndex"])
                is_win = outcome_index == int(winning_index)
                resolved.append({**bet, "is_win": is_win})
            else:
                pending.append(bet)
        else:
            pending.append(bet)
    return {"resolved": resolved, "pending": pending}


def bet_amount_usdc(bet: dict) -> float:
    return int(bet.get("amount", 0)) / USDC_DECIMALS_DIVISOR


def share_price(bet: dict) -> float | None:
    amount = int(bet.get("amount", 0))
    shares = int(bet.get("shares", 0))
    if shares == 0:
        return None
    return amount / shares


# ---------------------------------------------------------------------------
# Tool matching (reused from tool_accuracy_polymarket.py)
# ---------------------------------------------------------------------------


def extract_question_title(question: str) -> str:
    if not question:
        return ""
    return question.split(QUESTION_DATA_SEPARATOR)[0]


def match_bet_to_mech_request(bet: dict, mech_requests: list[dict]) -> dict | None:
    """Match a bet to the best mech request by question title prefix."""
    bet_title = (
        (bet.get("question") or {}).get("metadata", {}).get("title", "")
    ).strip()
    if not bet_title:
        return None

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
        return None

    # Pick mech request closest before the bet timestamp
    bet_ts = int(bet.get("blockTimestamp", 0))
    before_bet = [r for r in matched if int(r.get("blockTimestamp") or 0) <= bet_ts]
    if before_bet:
        return max(before_bet, key=lambda r: int(r.get("blockTimestamp") or 0))
    return matched[0]


def enrich_bets_with_tools(bets: list[dict], mech_requests: list[dict]) -> list[dict]:
    """Attach tool name to each bet."""
    enriched = []
    for bet in bets:
        match = match_bet_to_mech_request(bet, mech_requests)
        tool = (match.get("parsedRequest") or {}).get("tool", "unknown") if match else "unknown"
        enriched.append({**bet, "tool": tool})
    return enriched


# ---------------------------------------------------------------------------
# Analysis sections
# ---------------------------------------------------------------------------


def overall_summary(bets: list[dict], classified: dict, trader: dict | None) -> dict:
    """Section 1: Overall summary stats."""
    resolved = classified["resolved"]
    pending = classified["pending"]
    wins = [b for b in resolved if b["is_win"]]
    losses = [b for b in resolved if not b["is_win"]]

    total_invested_raw = sum(int(b.get("amount", 0)) for b in bets)
    total_invested = total_invested_raw / USDC_DECIMALS_DIVISOR

    # From aggregate data
    total_payout = int(trader.get("totalPayout", 0)) / USDC_DECIMALS_DIVISOR if trader else 0
    total_traded = int(trader.get("totalTraded", 0)) / USDC_DECIMALS_DIVISOR if trader else 0
    total_traded_settled = int(trader.get("totalTradedSettled", 0)) / USDC_DECIMALS_DIVISOR if trader else 0
    total_costs = total_traded_settled
    net_pnl = total_payout - total_costs
    roi = (net_pnl / total_costs * PERCENTAGE_FACTOR) if total_costs > 0 else None
    accuracy = (len(wins) / len(resolved) * PERCENTAGE_FACTOR) if resolved else None

    return {
        "total_bets": len(bets),
        "resolved_bets": len(resolved),
        "pending_bets": len(pending),
        "wins": len(wins),
        "losses": len(losses),
        "accuracy_pct": round(accuracy, 2) if accuracy is not None else None,
        "total_invested_usdc": round(total_invested, 2),
        "total_payout_usdc": round(total_payout, 2),
        "total_traded_settled_usdc": round(total_traded_settled, 2),
        "net_pnl_usdc": round(net_pnl, 2),
        "roi_pct": round(roi, 2) if roi is not None else None,
    }


def accuracy_analysis(classified: dict) -> dict:
    """Section 2: Prediction accuracy breakdown."""
    resolved = classified["resolved"]
    if not resolved:
        return {"error": "No resolved bets"}

    # By outcome index
    by_outcome = defaultdict(lambda: {"total": 0, "wins": 0})
    for bet in resolved:
        idx = int(bet["outcomeIndex"])
        by_outcome[idx]["total"] += 1
        if bet["is_win"]:
            by_outcome[idx]["wins"] += 1

    outcome_stats = {}
    for idx, counts in sorted(by_outcome.items()):
        label = "Yes" if idx == 0 else "No" if idx == 1 else f"Outcome {idx}"
        acc = counts["wins"] / counts["total"] * PERCENTAGE_FACTOR if counts["total"] > 0 else 0
        outcome_stats[label] = {
            "total": counts["total"],
            "wins": counts["wins"],
            "accuracy_pct": round(acc, 1),
        }

    # Weekly accuracy
    sorted_bets = sorted(resolved, key=lambda b: int(b.get("blockTimestamp", 0)))
    weekly = defaultdict(lambda: {"total": 0, "wins": 0})
    for bet in sorted_bets:
        ts = int(bet.get("blockTimestamp", 0))
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        week_key = dt.strftime("%Y-W%W")
        weekly[week_key]["total"] += 1
        if bet["is_win"]:
            weekly[week_key]["wins"] += 1

    weekly_accuracy = {}
    for week, counts in sorted(weekly.items()):
        acc = counts["wins"] / counts["total"] * PERCENTAGE_FACTOR if counts["total"] > 0 else 0
        weekly_accuracy[week] = {
            "total": counts["total"],
            "wins": counts["wins"],
            "accuracy_pct": round(acc, 1),
        }

    # Streaks
    max_win_streak = max_loss_streak = 0
    cur_win = cur_loss = 0
    for bet in sorted_bets:
        if bet["is_win"]:
            cur_win += 1
            cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            max_loss_streak = max(max_loss_streak, cur_loss)

    return {
        "by_outcome": outcome_stats,
        "weekly_accuracy": weekly_accuracy,
        "longest_win_streak": max_win_streak,
        "longest_loss_streak": max_loss_streak,
    }


def tool_analysis(enriched_resolved: list[dict]) -> dict:
    """Section 3: Per-tool performance breakdown."""
    tool_stats = defaultdict(lambda: {
        "total": 0, "wins": 0, "total_invested": 0, "total_payout_est": 0,
        "bet_amounts": [],
    })

    for bet in enriched_resolved:
        tool = bet["tool"]
        amount = bet_amount_usdc(bet)
        shares_val = int(bet.get("shares", 0)) / USDC_DECIMALS_DIVISOR

        tool_stats[tool]["total"] += 1
        tool_stats[tool]["total_invested"] += amount
        tool_stats[tool]["bet_amounts"].append(amount)

        if bet["is_win"]:
            tool_stats[tool]["wins"] += 1
            tool_stats[tool]["total_payout_est"] += shares_val

    results = {}
    for tool, stats in sorted(tool_stats.items(), key=lambda x: x[1]["total"], reverse=True):
        acc = stats["wins"] / stats["total"] * PERCENTAGE_FACTOR if stats["total"] > 0 else 0
        avg_bet = statistics.mean(stats["bet_amounts"]) if stats["bet_amounts"] else 0
        pnl = stats["total_payout_est"] - stats["total_invested"]
        results[tool] = {
            "total_bets": stats["total"],
            "wins": stats["wins"],
            "accuracy_pct": round(acc, 1),
            "avg_bet_usdc": round(avg_bet, 4),
            "total_invested_usdc": round(stats["total_invested"], 2),
            "est_pnl_usdc": round(pnl, 2),
        }
    return results


def bet_sizing_analysis(bets: list[dict], classified: dict) -> dict:
    """Section 4: Bet sizing patterns."""
    amounts = [bet_amount_usdc(b) for b in bets]
    prices = [p for b in bets if (p := share_price(b)) is not None]

    resolved = classified["resolved"]
    win_amounts = [bet_amount_usdc(b) for b in resolved if b["is_win"]]
    loss_amounts = [bet_amount_usdc(b) for b in resolved if not b["is_win"]]

    win_prices = [p for b in resolved if b["is_win"] and (p := share_price(b)) is not None]
    loss_prices = [p for b in resolved if not b["is_win"] and (p := share_price(b)) is not None]

    # Bet size distribution buckets
    buckets = {"<1": 0, "1-5": 0, "5-10": 0, "10-25": 0, "25-50": 0, "50-100": 0, "100+": 0}
    for a in amounts:
        if a < 1:
            buckets["<1"] += 1
        elif a < 5:
            buckets["1-5"] += 1
        elif a < 10:
            buckets["5-10"] += 1
        elif a < 25:
            buckets["10-25"] += 1
        elif a < 50:
            buckets["25-50"] += 1
        elif a < 100:
            buckets["50-100"] += 1
        else:
            buckets["100+"] += 1

    def _stats(values):
        if not values:
            return None
        return {
            "mean": round(statistics.mean(values), 4),
            "median": round(statistics.median(values), 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "stdev": round(statistics.stdev(values), 4) if len(values) > 1 else 0,
        }

    return {
        "bet_amounts": _stats(amounts),
        "share_prices": _stats(prices),
        "bet_size_distribution": buckets,
        "win_bet_amounts": _stats(win_amounts),
        "loss_bet_amounts": _stats(loss_amounts),
        "win_share_prices": _stats(win_prices),
        "loss_share_prices": _stats(loss_prices),
    }


def temporal_analysis(bets: list[dict], classified: dict) -> dict:
    """Section 5: Activity and PnL over time."""
    resolved = classified["resolved"]
    all_sorted = sorted(bets, key=lambda b: int(b.get("blockTimestamp", 0)))

    # Daily activity
    daily = defaultdict(int)
    for bet in all_sorted:
        ts = int(bet.get("blockTimestamp", 0))
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        daily[day] += 1

    # Cumulative PnL over resolved bets (chronological)
    resolved_sorted = sorted(resolved, key=lambda b: int(b.get("blockTimestamp", 0)))
    cumulative_pnl = []
    running_pnl = 0.0
    for bet in resolved_sorted:
        amount = bet_amount_usdc(bet)
        shares_val = int(bet.get("shares", 0)) / USDC_DECIMALS_DIVISOR
        if bet["is_win"]:
            running_pnl += shares_val - amount
        else:
            running_pnl -= amount
        ts = int(bet.get("blockTimestamp", 0))
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        cumulative_pnl.append({"date": day, "cumulative_pnl_usdc": round(running_pnl, 2)})

    # First/last bet dates
    if all_sorted:
        first_ts = int(all_sorted[0].get("blockTimestamp", 0))
        last_ts = int(all_sorted[-1].get("blockTimestamp", 0))
        first_date = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        last_date = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        active_days = len(daily)
        total_span_days = (last_ts - first_ts) // 86400 + 1
    else:
        first_date = last_date = None
        active_days = total_span_days = 0

    return {
        "first_bet": first_date,
        "last_bet": last_date,
        "active_days": active_days,
        "total_span_days": total_span_days,
        "bets_per_day": dict(sorted(daily.items())),
        "avg_bets_per_active_day": round(len(bets) / active_days, 1) if active_days else 0,
        "cumulative_pnl": cumulative_pnl,
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def print_report(
    address: str,
    summary: dict,
    accuracy: dict,
    tools: dict,
    sizing: dict,
    temporal: dict,
):
    w = 60
    print("\n" + "=" * w)
    print(f"  PolyStrat Agent Analysis: {address}")
    print("=" * w)

    # --- Overall Summary ---
    print("\n--- OVERALL SUMMARY ---")
    print(f"  Total bets:        {summary['total_bets']}")
    print(f"  Resolved:          {summary['resolved_bets']}")
    print(f"  Pending:           {summary['pending_bets']}")
    print(f"  Wins / Losses:     {summary['wins']} / {summary['losses']}")
    acc_str = f"{summary['accuracy_pct']}%" if summary['accuracy_pct'] is not None else "N/A"
    print(f"  Accuracy:          {acc_str}")
    print(f"  Total invested:    ${summary['total_invested_usdc']:.2f}")
    print(f"  Total payout:      ${summary['total_payout_usdc']:.2f}")
    print(f"  Settled traded:    ${summary['total_traded_settled_usdc']:.2f}")
    pnl = summary['net_pnl_usdc']
    pnl_sign = "+" if pnl >= 0 else ""
    print(f"  Net PnL:           {pnl_sign}${pnl:.2f}")
    roi_str = f"{summary['roi_pct']}%" if summary['roi_pct'] is not None else "N/A"
    print(f"  ROI:               {roi_str}")

    # --- Accuracy Analysis ---
    if "error" not in accuracy:
        print("\n--- PREDICTION ACCURACY ---")
        print("\n  By outcome:")
        for label, stats in accuracy["by_outcome"].items():
            print(f"    {label:>6}: {stats['wins']}/{stats['total']} ({stats['accuracy_pct']}%)")

        print(f"\n  Longest win streak:  {accuracy['longest_win_streak']}")
        print(f"  Longest loss streak: {accuracy['longest_loss_streak']}")

        print("\n  Weekly accuracy:")
        for week, stats in accuracy["weekly_accuracy"].items():
            bar = "#" * int(stats["accuracy_pct"] / 5)
            print(f"    {week}: {stats['accuracy_pct']:5.1f}% ({stats['wins']}/{stats['total']}) {bar}")

    # --- Tool Analysis ---
    if tools:
        print("\n--- TOOL USAGE ---")
        col_t = max(len(t) for t in tools)
        col_t = max(col_t, 4)
        header = f"  {'Tool':<{col_t}} | {'Bets':>5} | {'Wins':>5} | {'Acc%':>6} | {'Avg Bet':>9} | {'Invested':>10} | {'Est PnL':>10}"
        sep = "  " + "-" * (len(header) - 2)
        print(sep)
        print(header)
        print(sep)
        for tool, stats in tools.items():
            pnl_val = stats['est_pnl_usdc']
            pnl_s = f"{'+'if pnl_val>=0 else ''}${pnl_val:.2f}"
            print(
                f"  {tool:<{col_t}} | {stats['total_bets']:>5} | {stats['wins']:>5} | "
                f"{stats['accuracy_pct']:>5.1f}% | ${stats['avg_bet_usdc']:>7.2f} | "
                f"${stats['total_invested_usdc']:>8.2f} | {pnl_s:>10}"
            )
        print(sep)

    # --- Bet Sizing ---
    print("\n--- BET SIZING ---")
    if sizing["bet_amounts"]:
        ba = sizing["bet_amounts"]
        print(f"  Amount (USDC):  mean=${ba['mean']:.4f}  median=${ba['median']:.4f}  min=${ba['min']:.4f}  max=${ba['max']:.4f}")
    if sizing["share_prices"]:
        sp = sizing["share_prices"]
        print(f"  Share price:    mean={sp['mean']:.4f}  median={sp['median']:.4f}  min={sp['min']:.4f}  max={sp['max']:.4f}")

    print("\n  Bet size distribution:")
    for bucket, count in sizing["bet_size_distribution"].items():
        bar = "#" * count
        print(f"    ${bucket:>7}: {count:>4}  {bar}")

    if sizing["win_bet_amounts"] and sizing["loss_bet_amounts"]:
        w_amt = sizing["win_bet_amounts"]
        l_amt = sizing["loss_bet_amounts"]
        print(f"\n  Avg bet on wins:   ${w_amt['mean']:.4f}")
        print(f"  Avg bet on losses: ${l_amt['mean']:.4f}")
    if sizing["win_share_prices"] and sizing["loss_share_prices"]:
        w_sp = sizing["win_share_prices"]
        l_sp = sizing["loss_share_prices"]
        print(f"  Avg share price on wins:   {w_sp['mean']:.4f}")
        print(f"  Avg share price on losses: {l_sp['mean']:.4f}")

    # --- Temporal ---
    print("\n--- TEMPORAL ANALYSIS ---")
    print(f"  First bet:     {temporal['first_bet']}")
    print(f"  Last bet:      {temporal['last_bet']}")
    print(f"  Active days:   {temporal['active_days']} / {temporal['total_span_days']} days")
    print(f"  Avg bets/day:  {temporal['avg_bets_per_active_day']}")

    if temporal["cumulative_pnl"]:
        print("\n  Cumulative PnL timeline:")
        # Show sampled points if too many
        pnl_points = temporal["cumulative_pnl"]
        step = max(1, len(pnl_points) // 20)
        for i in range(0, len(pnl_points), step):
            p = pnl_points[i]
            sign = "+" if p["cumulative_pnl_usdc"] >= 0 else ""
            print(f"    {p['date']}: {sign}${p['cumulative_pnl_usdc']:.2f}")
        # Always show last point
        if len(pnl_points) % step != 1:
            p = pnl_points[-1]
            sign = "+" if p["cumulative_pnl_usdc"] >= 0 else ""
            print(f"    {p['date']}: {sign}${p['cumulative_pnl_usdc']:.2f}")

    print("\n" + "=" * w)


# ---------------------------------------------------------------------------
# Matplotlib charts (optional)
# ---------------------------------------------------------------------------


def plot_charts(temporal: dict, accuracy: dict, sizing: dict):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("\nmatplotlib not installed, skipping charts.")
        return

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))

    # 1. Cumulative PnL
    ax = axes[0][0]
    pnl_points = temporal.get("cumulative_pnl", [])
    if pnl_points:
        dates = [datetime.strptime(p["date"], "%Y-%m-%d") for p in pnl_points]
        values = [p["cumulative_pnl_usdc"] for p in pnl_points]
        ax.plot(dates, values, linewidth=1.5, color="blue")
        ax.axhline(0, color="red", linestyle=":", alpha=0.5)
        ax.fill_between(dates, values, 0, alpha=0.1, color="blue")
    ax.set_title("Cumulative PnL (USDC)")
    ax.set_ylabel("PnL ($)")
    ax.tick_params(axis="x", rotation=30)

    # 2. Weekly accuracy
    ax = axes[0][1]
    weekly = accuracy.get("weekly_accuracy", {})
    if weekly:
        weeks = list(weekly.keys())
        accs = [weekly[w]["accuracy_pct"] for w in weeks]
        counts = [weekly[w]["total"] for w in weeks]
        ax.bar(range(len(weeks)), accs, alpha=0.7, color="steelblue")
        ax.axhline(50, color="red", linestyle=":", alpha=0.5, label="50% (random)")
        ax.set_xticks(range(len(weeks)))
        ax.set_xticklabels(weeks, rotation=45, ha="right", fontsize=7)
        # Annotate bet counts
        for i, c in enumerate(counts):
            ax.annotate(f"n={c}", (i, accs[i] + 1), ha="center", fontsize=6)
    ax.set_title("Weekly Accuracy (%)")
    ax.set_ylabel("Accuracy (%)")
    ax.set_ylim(0, 105)

    # 3. Bet size distribution
    ax = axes[1][0]
    dist = sizing.get("bet_size_distribution", {})
    if dist:
        labels = list(dist.keys())
        vals = list(dist.values())
        ax.bar(labels, vals, color="teal", alpha=0.7)
    ax.set_title("Bet Size Distribution (USDC)")
    ax.set_ylabel("Count")

    # 4. Daily bet count
    ax = axes[1][1]
    daily = temporal.get("bets_per_day", {})
    if daily:
        days = list(daily.keys())
        counts = list(daily.values())
        dates = [datetime.strptime(d, "%Y-%m-%d") for d in days]
        ax.bar(dates, counts, width=0.8, color="coral", alpha=0.7)
    ax.set_title("Daily Bet Activity")
    ax.set_ylabel("Bets")
    ax.tick_params(axis="x", rotation=30)

    fig.suptitle("PolyStrat Agent Performance", fontsize=14, fontweight="bold")
    fig.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive PolyStrat agent performance analysis."
    )
    parser.add_argument("address", help="Agent safe address (0x...)")
    parser.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output results as JSON instead of formatted tables",
    )
    parser.add_argument(
        "--no-charts", dest="no_charts", action="store_true",
        help="Skip matplotlib charts",
    )
    args = parser.parse_args()

    address = args.address.lower()

    print(f"Analyzing PolyStrat agent: {address}")
    print()

    # Fetch data
    print("[1/3] Fetching bets...")
    bets = fetch_agent_bets(address)
    print(f"  Found {len(bets)} bets.")

    print("[2/3] Fetching aggregate trader stats...")
    trader = fetch_trader_agent(address)
    if trader:
        print(f"  Service ID: {trader.get('serviceId')}")
    else:
        print("  No trader agent found (aggregate stats unavailable).")

    print("[3/3] Fetching mech requests...")
    mech_requests = fetch_mech_requests(address)
    print(f"  Found {len(mech_requests)} mech requests.")

    if not bets:
        print("\nNo bets found for this address. Exiting.")
        sys.exit(0)

    # Classify and enrich
    classified = classify_bets(bets)
    enriched_resolved = enrich_bets_with_tools(classified["resolved"], mech_requests)

    # Run analyses
    summary = overall_summary(bets, classified, trader)
    acc = accuracy_analysis(classified)
    tools = tool_analysis(enriched_resolved)
    sizing = bet_sizing_analysis(bets, classified)
    temporal = temporal_analysis(bets, classified)

    if args.json_output:
        output = {
            "address": address,
            "summary": summary,
            "accuracy": acc,
            "tool_analysis": tools,
            "bet_sizing": sizing,
            "temporal": temporal,
        }
        print(json.dumps(output, indent=2))
    else:
        print_report(address, summary, acc, tools, sizing, temporal)
        if not args.no_charts:
            plot_charts(temporal, acc, sizing)


if __name__ == "__main__":
    main()
