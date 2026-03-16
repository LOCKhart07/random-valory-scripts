"""
Deep single-agent PolyStrat analysis.

Compares a focus agent against the fleet on shared markets, tracks PnL
trajectory, identifies biggest losses, analyzes per-tool accuracy vs fleet,
and examines share price dynamics on wins vs losses.

Usage:
    python polymarket/analyze_agent_deep.py 0x33d20338f1700eda034ea2543933f94a2177ae4c
    python polymarket/analyze_agent_deep.py 0x33d2... --no-charts
    python polymarket/analyze_agent_deep.py 0x33d2... --json
"""

import argparse
import json
import os
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

try:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    _HAS_MATPLOTLIB = True
except ImportError:
    _HAS_MATPLOTLIB = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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

USDC_DECIMALS_DIVISOR = 1_000_000
QUESTION_DATA_SEPARATOR = "\u241f"
REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3
MECH_LOOKBACK_SECONDS = 70 * 24 * 60 * 60


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
            wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
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


def get_all_polystrat_agents():
    if POLYGON_REGISTRY_SUBGRAPH_URL:
        try:
            query = """
{
  services(where: { agentIds_contains: [86] }, first: 1000) {
    id
    multisig
  }
}
"""
            response = call_subgraph(POLYGON_REGISTRY_SUBGRAPH_URL, query)
            return [s["multisig"].lower() for s in response["data"]["services"]]
        except Exception as exc:
            print(f"  Registry failed ({exc}), falling back...")
    query = """
{
  traderAgents(first: 1000, orderBy: totalBets, orderDirection: desc) { id }
}
"""
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
    response = call_subgraph(
        POLYMARKET_BETS_SUBGRAPH_URL, query, {"id": safe_address}
    )
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


# ---------------------------------------------------------------------------
# Bet processing
# ---------------------------------------------------------------------------


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
        question_id = question.get("id", "")
        title = (question.get("metadata") or {}).get("title", "")
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
            "question_id": question_id,
            "title": title,
            "outcome_index": outcome_idx,
            "amount_usdc": amount / USDC_DECIMALS_DIVISOR,
            "shares_usdc": shares / USDC_DECIMALS_DIVISOR,
            "share_price": share_price,
            "timestamp": int(bet.get("blockTimestamp", 0)),
            "is_resolved": is_resolved,
            "is_win": is_win,
        })
    return records


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------


def head_to_head_on_shared_markets(focus_bets, fleet_bets):
    """
    For every market the focus agent bet on, compare outcome and price
    against all other agents who bet on that same market.
    """
    # Build question_id -> focus bet
    focus_by_q = {}
    for b in focus_bets:
        if b["question_id"] and b["is_resolved"]:
            focus_by_q[b["question_id"]] = b

    # Build question_id -> [other agent bets]
    others_by_q = defaultdict(list)
    for addr, bets in fleet_bets.items():
        for b in bets:
            if b["question_id"] in focus_by_q and b["is_resolved"]:
                others_by_q[b["question_id"]].append({**b, "agent": addr})

    results = []
    focus_wins_shared = 0
    focus_total_shared = 0
    fleet_wins_shared = 0
    fleet_total_shared = 0
    same_outcome_count = 0
    diff_outcome_count = 0
    focus_better_price = 0
    fleet_better_price = 0
    focus_pnl_shared = 0.0
    fleet_pnl_shared_per_agent = defaultdict(float)

    for qid, focus_bet in focus_by_q.items():
        others = others_by_q.get(qid, [])
        if not others:
            continue

        focus_total_shared += 1
        if focus_bet["is_win"]:
            focus_wins_shared += 1
            focus_pnl_shared += focus_bet["shares_usdc"] - focus_bet["amount_usdc"]
        else:
            focus_pnl_shared -= focus_bet["amount_usdc"]

        other_outcomes = []
        other_prices = []
        for ob in others:
            fleet_total_shared += 1
            if ob["is_win"]:
                fleet_wins_shared += 1
                fleet_pnl_shared_per_agent[ob["agent"]] += (
                    ob["shares_usdc"] - ob["amount_usdc"]
                )
            else:
                fleet_pnl_shared_per_agent[ob["agent"]] -= ob["amount_usdc"]

            other_outcomes.append(ob["outcome_index"])
            other_prices.append(ob["share_price"])

            if ob["outcome_index"] == focus_bet["outcome_index"]:
                same_outcome_count += 1
            else:
                diff_outcome_count += 1

            if focus_bet["share_price"] < ob["share_price"]:
                focus_better_price += 1
            elif ob["share_price"] < focus_bet["share_price"]:
                fleet_better_price += 1

        results.append({
            "question_id": qid,
            "title": focus_bet["title"][:80],
            "focus_outcome": focus_bet["outcome_index"],
            "focus_win": focus_bet["is_win"],
            "focus_price": focus_bet["share_price"],
            "focus_amount": focus_bet["amount_usdc"],
            "n_other_agents": len(others),
            "other_outcomes": other_outcomes,
            "other_avg_price": statistics.mean(other_prices) if other_prices else 0,
        })

    focus_acc_shared = (
        focus_wins_shared / focus_total_shared * 100
        if focus_total_shared > 0
        else None
    )
    fleet_acc_shared = (
        fleet_wins_shared / fleet_total_shared * 100
        if fleet_total_shared > 0
        else None
    )

    return {
        "shared_markets": len(results),
        "focus_accuracy_shared_pct": round(focus_acc_shared, 1) if focus_acc_shared else None,
        "fleet_accuracy_shared_pct": round(fleet_acc_shared, 1) if fleet_acc_shared else None,
        "same_outcome_comparisons": same_outcome_count,
        "diff_outcome_comparisons": diff_outcome_count,
        "outcome_agreement_pct": round(
            same_outcome_count / (same_outcome_count + diff_outcome_count) * 100, 1
        ) if (same_outcome_count + diff_outcome_count) > 0 else None,
        "focus_better_price_count": focus_better_price,
        "fleet_better_price_count": fleet_better_price,
        "focus_pnl_shared": round(focus_pnl_shared, 2),
        "per_market": results,
    }


def cumulative_pnl_trajectory(bets):
    """
    Compute cumulative PnL over time from chronologically sorted resolved bets.
    Returns list of (timestamp, cumulative_pnl, bet_pnl, title).
    """
    resolved = sorted(
        [b for b in bets if b["is_resolved"]],
        key=lambda b: b["timestamp"],
    )
    trajectory = []
    cum_pnl = 0.0
    for b in resolved:
        if b["is_win"]:
            bet_pnl = b["shares_usdc"] - b["amount_usdc"]
        else:
            bet_pnl = -b["amount_usdc"]
        cum_pnl += bet_pnl
        trajectory.append({
            "timestamp": b["timestamp"],
            "date": datetime.fromtimestamp(b["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d"),
            "bet_pnl": round(bet_pnl, 4),
            "cumulative_pnl": round(cum_pnl, 4),
            "title": b["title"][:80],
            "is_win": b["is_win"],
            "amount_usdc": b["amount_usdc"],
            "share_price": b["share_price"],
        })
    return trajectory


def biggest_losses(trajectory, n=15):
    """Return the N biggest individual losing bets."""
    losses = [t for t in trajectory if t["bet_pnl"] < 0]
    return sorted(losses, key=lambda t: t["bet_pnl"])[:n]


def biggest_wins(trajectory, n=10):
    """Return the N biggest individual winning bets."""
    wins = [t for t in trajectory if t["bet_pnl"] > 0]
    return sorted(wins, key=lambda t: t["bet_pnl"], reverse=True)[:n]


def streak_analysis(bets):
    """Find win/loss streaks and their dollar impact."""
    resolved = sorted(
        [b for b in bets if b["is_resolved"]],
        key=lambda b: b["timestamp"],
    )
    if not resolved:
        return {"longest_win_streak": 0, "longest_loss_streak": 0, "streaks": []}

    streaks = []
    current_type = resolved[0]["is_win"]
    current_len = 1
    current_pnl = 0.0

    def _bet_pnl(b):
        return (b["shares_usdc"] - b["amount_usdc"]) if b["is_win"] else -b["amount_usdc"]

    current_pnl = _bet_pnl(resolved[0])

    for b in resolved[1:]:
        if b["is_win"] == current_type:
            current_len += 1
            current_pnl += _bet_pnl(b)
        else:
            streaks.append({
                "type": "win" if current_type else "loss",
                "length": current_len,
                "pnl": round(current_pnl, 2),
            })
            current_type = b["is_win"]
            current_len = 1
            current_pnl = _bet_pnl(b)

    streaks.append({
        "type": "win" if current_type else "loss",
        "length": current_len,
        "pnl": round(current_pnl, 2),
    })

    win_streaks = [s for s in streaks if s["type"] == "win"]
    loss_streaks = [s for s in streaks if s["type"] == "loss"]

    return {
        "longest_win_streak": max((s["length"] for s in win_streaks), default=0),
        "longest_loss_streak": max((s["length"] for s in loss_streaks), default=0),
        "worst_loss_streak_pnl": min((s["pnl"] for s in loss_streaks), default=0),
        "best_win_streak_pnl": max((s["pnl"] for s in win_streaks), default=0),
        "total_streaks": len(streaks),
        "avg_win_streak": round(
            statistics.mean([s["length"] for s in win_streaks]), 1
        ) if win_streaks else 0,
        "avg_loss_streak": round(
            statistics.mean([s["length"] for s in loss_streaks]), 1
        ) if loss_streaks else 0,
    }


def tool_accuracy_vs_fleet(focus_bets, focus_tools, fleet_bets, fleet_tools):
    """
    Per-tool accuracy and PnL for focus agent vs fleet average.
    """
    # Focus per-tool stats
    focus_tool_stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl": 0.0})
    for b in focus_bets:
        if not b["is_resolved"]:
            continue
        tool = focus_tools.get(b["bet_id"], "unknown")
        focus_tool_stats[tool]["total"] += 1
        pnl = (b["shares_usdc"] - b["amount_usdc"]) if b["is_win"] else -b["amount_usdc"]
        focus_tool_stats[tool]["pnl"] += pnl
        if b["is_win"]:
            focus_tool_stats[tool]["wins"] += 1

    # Fleet per-tool stats
    fleet_tool_stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl": 0.0})
    for addr, bets in fleet_bets.items():
        tools = fleet_tools.get(addr, {})
        for b in bets:
            if not b["is_resolved"]:
                continue
            tool = tools.get(b["bet_id"], "unknown")
            fleet_tool_stats[tool]["total"] += 1
            pnl = (b["shares_usdc"] - b["amount_usdc"]) if b["is_win"] else -b["amount_usdc"]
            fleet_tool_stats[tool]["pnl"] += pnl
            if b["is_win"]:
                fleet_tool_stats[tool]["wins"] += 1

    comparison = []
    all_tools = set(focus_tool_stats.keys()) | set(fleet_tool_stats.keys())
    for tool in sorted(all_tools, key=lambda t: focus_tool_stats[t]["total"], reverse=True):
        f = focus_tool_stats[tool]
        fl = fleet_tool_stats[tool]
        comparison.append({
            "tool": tool,
            "focus_total": f["total"],
            "focus_wins": f["wins"],
            "focus_accuracy": round(f["wins"] / f["total"] * 100, 1) if f["total"] > 0 else None,
            "focus_pnl": round(f["pnl"], 2),
            "fleet_total": fl["total"],
            "fleet_accuracy": round(fl["wins"] / fl["total"] * 100, 1) if fl["total"] > 0 else None,
            "fleet_pnl": round(fl["pnl"], 2),
        })

    return comparison


def share_price_analysis(bets):
    """Analyze share prices on wins vs losses, and by outcome index."""
    resolved = [b for b in bets if b["is_resolved"] and b["share_price"] > 0]
    wins = [b for b in resolved if b["is_win"]]
    losses = [b for b in resolved if not b["is_win"]]

    win_prices = [b["share_price"] for b in wins]
    loss_prices = [b["share_price"] for b in losses]
    win_amounts = [b["amount_usdc"] for b in wins]
    loss_amounts = [b["amount_usdc"] for b in losses]

    # Price buckets
    buckets = [
        (0.0, 0.3, "longshot (0-0.30)"),
        (0.3, 0.5, "underdog (0.30-0.50)"),
        (0.5, 0.7, "slight fav (0.50-0.70)"),
        (0.7, 0.85, "favorite (0.70-0.85)"),
        (0.85, 1.01, "heavy fav (0.85-1.00)"),
    ]
    bucket_stats = []
    for lo, hi, label in buckets:
        in_bucket = [b for b in resolved if lo <= b["share_price"] < hi]
        if in_bucket:
            w = sum(1 for b in in_bucket if b["is_win"])
            pnl = sum(
                (b["shares_usdc"] - b["amount_usdc"]) if b["is_win"] else -b["amount_usdc"]
                for b in in_bucket
            )
            bucket_stats.append({
                "bucket": label,
                "count": len(in_bucket),
                "wins": w,
                "accuracy_pct": round(w / len(in_bucket) * 100, 1),
                "pnl": round(pnl, 2),
            })

    # Yes vs No
    yes_bets = [b for b in resolved if b["outcome_index"] == 0]
    no_bets = [b for b in resolved if b["outcome_index"] == 1]
    yes_wins = sum(1 for b in yes_bets if b["is_win"])
    no_wins = sum(1 for b in no_bets if b["is_win"])
    yes_pnl = sum(
        (b["shares_usdc"] - b["amount_usdc"]) if b["is_win"] else -b["amount_usdc"]
        for b in yes_bets
    )
    no_pnl = sum(
        (b["shares_usdc"] - b["amount_usdc"]) if b["is_win"] else -b["amount_usdc"]
        for b in no_bets
    )

    return {
        "avg_win_price": round(statistics.mean(win_prices), 4) if win_prices else None,
        "avg_loss_price": round(statistics.mean(loss_prices), 4) if loss_prices else None,
        "median_win_price": round(statistics.median(win_prices), 4) if win_prices else None,
        "median_loss_price": round(statistics.median(loss_prices), 4) if loss_prices else None,
        "avg_win_amount": round(statistics.mean(win_amounts), 4) if win_amounts else None,
        "avg_loss_amount": round(statistics.mean(loss_amounts), 4) if loss_amounts else None,
        "total_win_invested": round(sum(win_amounts), 2),
        "total_loss_invested": round(sum(loss_amounts), 2),
        "price_buckets": bucket_stats,
        "yes_bets": len(yes_bets),
        "yes_wins": yes_wins,
        "yes_accuracy": round(yes_wins / len(yes_bets) * 100, 1) if yes_bets else None,
        "yes_pnl": round(yes_pnl, 2),
        "no_bets": len(no_bets),
        "no_wins": no_wins,
        "no_accuracy": round(no_wins / len(no_bets) * 100, 1) if no_bets else None,
        "no_pnl": round(no_pnl, 2),
    }


def weekly_accuracy_trend(bets):
    """Bin resolved bets into weekly buckets and track accuracy + PnL."""
    resolved = sorted(
        [b for b in bets if b["is_resolved"]],
        key=lambda b: b["timestamp"],
    )
    if not resolved:
        return []

    week_seconds = 7 * 86400
    start_ts = resolved[0]["timestamp"]
    weeks = []
    current_start = start_ts

    while True:
        current_end = current_start + week_seconds
        week_bets = [b for b in resolved if current_start <= b["timestamp"] < current_end]
        if not week_bets and current_start > resolved[-1]["timestamp"]:
            break
        if week_bets:
            wins = sum(1 for b in week_bets if b["is_win"])
            pnl = sum(
                (b["shares_usdc"] - b["amount_usdc"]) if b["is_win"] else -b["amount_usdc"]
                for b in week_bets
            )
            weeks.append({
                "week_start": datetime.fromtimestamp(
                    current_start, tz=timezone.utc
                ).strftime("%Y-%m-%d"),
                "count": len(week_bets),
                "wins": wins,
                "accuracy_pct": round(wins / len(week_bets) * 100, 1),
                "pnl": round(pnl, 2),
            })
        current_start = current_end

    return weeks


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_report(focus_addr, focus_bets, h2h, trajectory, loss_list, win_list,
                 streaks, tool_comp, prices, weekly, fleet_size):
    w = 74
    resolved = [b for b in focus_bets if b["is_resolved"]]
    wins = sum(1 for b in resolved if b["is_win"])
    total_pnl = trajectory[-1]["cumulative_pnl"] if trajectory else 0

    print("\n" + "=" * w)
    print(f"  Deep Analysis: {focus_addr[:10]}...{focus_addr[-6:]}")
    print("=" * w)

    # --- Overview ---
    print("\n--- OVERVIEW ---")
    print(f"  Total bets: {len(focus_bets)}  |  Resolved: {len(resolved)}  |"
          f"  Wins: {wins}  |  Losses: {len(resolved) - wins}")
    acc = wins / len(resolved) * 100 if resolved else 0
    print(f"  Accuracy: {acc:.1f}%  |  Net PnL: ${total_pnl:.2f}  |"
          f"  Fleet agents: {fleet_size}")

    # --- Head to Head ---
    print(f"\n--- HEAD-TO-HEAD ON SHARED MARKETS ({h2h['shared_markets']} markets) ---")
    print(f"  Focus accuracy on shared markets:  {h2h['focus_accuracy_shared_pct']}%")
    print(f"  Fleet accuracy on shared markets:  {h2h['fleet_accuracy_shared_pct']}%")
    gap = (h2h['focus_accuracy_shared_pct'] or 0) - (h2h['fleet_accuracy_shared_pct'] or 0)
    print(f"  Accuracy gap:                      {gap:+.1f}pp")
    print(f"  Outcome agreement with fleet:      {h2h['outcome_agreement_pct']}%")
    print(f"  Focus got better price:            {h2h['focus_better_price_count']} times")
    print(f"  Fleet got better price:            {h2h['fleet_better_price_count']} times")
    print(f"  Focus PnL on shared markets:       ${h2h['focus_pnl_shared']:.2f}")

    if gap < -3:
        print(f"  >> Focus agent is {abs(gap):.1f}pp WORSE than fleet on the SAME markets.")
        print("     This is not market selection — the agent picks worse outcomes or enters at worse prices.")
    elif gap > 3:
        print(f"  >> Focus agent is {gap:.1f}pp BETTER than fleet on shared markets.")
    else:
        print("  >> Focus agent performs similarly to fleet on shared markets.")

    # --- Share Price Analysis ---
    print("\n--- SHARE PRICE DYNAMICS ---")
    print(f"  Avg share price on wins:    {prices['avg_win_price']}"
          f"  (median: {prices['median_win_price']})")
    print(f"  Avg share price on losses:  {prices['avg_loss_price']}"
          f"  (median: {prices['median_loss_price']})")
    print(f"  Avg bet size on wins:       ${prices['avg_win_amount']:.4f}")
    print(f"  Avg bet size on losses:     ${prices['avg_loss_amount']:.4f}")
    print(f"  Total invested on wins:     ${prices['total_win_invested']:.2f}")
    print(f"  Total invested on losses:   ${prices['total_loss_invested']:.2f}")
    if prices["avg_loss_price"] and prices["avg_win_price"]:
        if prices["avg_loss_price"] > prices["avg_win_price"] + 0.02:
            print("  >> Paying HIGHER prices on losing bets — confident on wrong picks.")
        elif prices["avg_win_price"] > prices["avg_loss_price"] + 0.02:
            print("  >> Paying higher prices on winning bets — confidence is well-calibrated.")

    print("\n  Price bucket breakdown:")
    for bucket in prices["price_buckets"]:
        sign = "+" if bucket["pnl"] >= 0 else ""
        print(
            f"    {bucket['bucket']:<25} | {bucket['count']:>4} bets | "
            f"{bucket['accuracy_pct']:>5.1f}% acc | {sign}${bucket['pnl']:.2f}"
        )

    print(f"\n  Yes/No bias:")
    print(f"    Yes bets: {prices['yes_bets']} ({prices['yes_accuracy']}% acc, ${prices['yes_pnl']:.2f} PnL)")
    print(f"    No bets:  {prices['no_bets']} ({prices['no_accuracy']}% acc, ${prices['no_pnl']:.2f} PnL)")

    # --- Tool Comparison ---
    print("\n--- TOOL ACCURACY: FOCUS vs FLEET ---")
    tool_header = (
        f"  {'Tool':<35} | {'Foc#':>4} | {'FocAcc':>6} | {'FocPnL':>8} |"
        f" {'Flt#':>5} | {'FltAcc':>6}"
    )
    print(tool_header)
    print("  " + "-" * (len(tool_header) - 2))
    for t in tool_comp:
        if t["focus_total"] == 0 and t["fleet_total"] < 10:
            continue
        fa = f"{t['focus_accuracy']:.1f}%" if t["focus_accuracy"] is not None else "N/A"
        fla = f"{t['fleet_accuracy']:.1f}%" if t["fleet_accuracy"] is not None else "N/A"
        pnl_s = f"${t['focus_pnl']:.2f}"
        print(
            f"  {t['tool']:<35} | {t['focus_total']:>4} | {fa:>6} | {pnl_s:>8} |"
            f" {t['fleet_total']:>5} | {fla:>6}"
        )
        if (t["focus_accuracy"] is not None and t["fleet_accuracy"] is not None
                and t["focus_total"] >= 5):
            diff = t["focus_accuracy"] - t["fleet_accuracy"]
            if abs(diff) > 5:
                marker = ">>" if diff < 0 else "<<"
                print(f"    {marker} {abs(diff):.1f}pp {'below' if diff < 0 else 'above'} fleet")

    # --- Streaks ---
    print("\n--- STREAK ANALYSIS ---")
    print(f"  Longest win streak:    {streaks['longest_win_streak']} "
          f"(best streak PnL: +${streaks['best_win_streak_pnl']:.2f})")
    print(f"  Longest loss streak:   {streaks['longest_loss_streak']} "
          f"(worst streak PnL: ${streaks['worst_loss_streak_pnl']:.2f})")
    print(f"  Avg win streak length: {streaks['avg_win_streak']}")
    print(f"  Avg loss streak length: {streaks['avg_loss_streak']}")

    # --- Biggest Losses ---
    print(f"\n--- TOP {len(loss_list)} BIGGEST LOSSES ---")
    for i, loss in enumerate(loss_list, 1):
        print(
            f"  {i:>2}. ${loss['bet_pnl']:.2f}  |  price={loss['share_price']:.3f}  |"
            f"  amt=${loss['amount_usdc']:.2f}  |  {loss['date']}  |  {loss['title']}"
        )

    # --- Biggest Wins ---
    print(f"\n--- TOP {len(win_list)} BIGGEST WINS ---")
    for i, win in enumerate(win_list, 1):
        print(
            f"  {i:>2}. +${win['bet_pnl']:.2f}  |  price={win['share_price']:.3f}  |"
            f"  amt=${win['amount_usdc']:.2f}  |  {win['date']}  |  {win['title']}"
        )

    # --- Weekly Trend ---
    print("\n--- WEEKLY PERFORMANCE ---")
    print(f"  {'Week':<12} | {'Bets':>5} | {'Wins':>5} | {'Acc%':>6} | {'PnL':>10}")
    print("  " + "-" * 50)
    for wk in weekly:
        sign = "+" if wk["pnl"] >= 0 else ""
        print(
            f"  {wk['week_start']:<12} | {wk['count']:>5} | {wk['wins']:>5} |"
            f" {wk['accuracy_pct']:>5.1f}% | {sign}${wk['pnl']:.2f}"
        )

    # --- Diagnosis ---
    print("\n--- DIAGNOSIS ---")
    issues = []

    if gap < -3:
        issues.append(
            f"On shared markets, {abs(gap):.1f}pp worse than fleet. "
            "This is not bad luck in market selection — the agent makes worse picks "
            "on the same questions other agents face."
        )

    if h2h["outcome_agreement_pct"] and h2h["outcome_agreement_pct"] < 70:
        issues.append(
            f"Only {h2h['outcome_agreement_pct']}% outcome agreement with fleet — "
            "the tool/model is non-deterministic and this agent is getting unlucky "
            "on the coin flips where agents disagree."
        )

    if prices["avg_loss_price"] and prices["avg_win_price"]:
        if prices["avg_loss_price"] > prices["avg_win_price"] + 0.02:
            issues.append(
                f"Paying higher prices on losses ({prices['avg_loss_price']:.3f}) "
                f"than wins ({prices['avg_win_price']:.3f}) — "
                "the agent is most confident when it's wrong."
            )

    if prices["total_loss_invested"] > prices["total_win_invested"] * 1.3:
        issues.append(
            f"${prices['total_loss_invested']:.2f} invested on losses vs "
            f"${prices['total_win_invested']:.2f} on wins — "
            "more capital allocated to losing bets."
        )

    # Check if one tool is dragging down performance
    for t in tool_comp:
        if (t["focus_total"] >= 10 and t["focus_pnl"] < -10
                and t["focus_accuracy"] is not None
                and t["fleet_accuracy"] is not None
                and t["focus_accuracy"] < t["fleet_accuracy"] - 5):
            issues.append(
                f"Tool '{t['tool']}' is underperforming: "
                f"{t['focus_accuracy']:.1f}% vs fleet {t['fleet_accuracy']:.1f}% "
                f"(PnL: ${t['focus_pnl']:.2f} on {t['focus_total']} bets)."
            )

    worst_week = min(weekly, key=lambda w: w["pnl"]) if weekly else None
    if worst_week and worst_week["pnl"] < -10:
        issues.append(
            f"Worst week: {worst_week['week_start']} with ${worst_week['pnl']:.2f} PnL "
            f"({worst_week['accuracy_pct']}% accuracy on {worst_week['count']} bets)."
        )

    if not issues:
        issues.append("No single dominant factor — performance is within expected variance range.")

    for issue in issues:
        print(f"  - {issue}")

    print("\n" + "=" * w)


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def plot_charts(trajectory, weekly, focus_bets, prices):
    if not _HAS_MATPLOTLIB:
        print("\nmatplotlib not available. Install: pip install matplotlib")
        return

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))

    # Chart 1: Cumulative PnL
    ax = axes[0][0]
    dates = [
        datetime.fromtimestamp(t["timestamp"], tz=timezone.utc)
        for t in trajectory
    ]
    pnls = [t["cumulative_pnl"] for t in trajectory]
    colors = ["green" if t["is_win"] else "red" for t in trajectory]
    ax.plot(dates, pnls, color="steelblue", linewidth=1.2, zorder=1)
    ax.scatter(dates, pnls, c=colors, s=8, zorder=2, alpha=0.6)
    ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
    ax.set_ylabel("Cumulative PnL (USDC)")
    ax.set_title("PnL Trajectory")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
    ax.grid(True, alpha=0.3)

    # Chart 2: Weekly accuracy + PnL
    ax = axes[0][1]
    if weekly:
        week_dates = [
            datetime.strptime(w["week_start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            for w in weekly
        ]
        accs = [w["accuracy_pct"] for w in weekly]
        week_pnls = [w["pnl"] for w in weekly]

        ax.bar(
            week_dates, week_pnls, width=5, alpha=0.5,
            color=["green" if p >= 0 else "red" for p in week_pnls],
            label="Weekly PnL",
        )
        ax2 = ax.twinx()
        ax2.plot(
            week_dates, accs, color="navy", marker="o", markersize=4,
            linewidth=1.5, label="Accuracy %",
        )
        ax2.axhline(50, color="red", linewidth=0.5, linestyle=":", alpha=0.5)
        ax2.set_ylabel("Accuracy (%)")
        ax2.set_ylim(0, 100)
        ax.set_ylabel("PnL (USDC)")
        ax.set_title("Weekly Accuracy & PnL")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, fontsize=8)

    # Chart 3: Share price distribution (wins vs losses)
    ax = axes[1][0]
    resolved = [b for b in focus_bets if b["is_resolved"] and b["share_price"] > 0]
    win_prices = [b["share_price"] for b in resolved if b["is_win"]]
    loss_prices = [b["share_price"] for b in resolved if not b["is_win"]]
    if win_prices or loss_prices:
        bins = [i * 0.05 for i in range(21)]
        ax.hist(
            win_prices, bins=bins, alpha=0.6, color="green",
            label=f"Wins (n={len(win_prices)})", edgecolor="white",
        )
        ax.hist(
            loss_prices, bins=bins, alpha=0.6, color="red",
            label=f"Losses (n={len(loss_prices)})", edgecolor="white",
        )
        ax.set_xlabel("Share Price")
        ax.set_ylabel("Count")
        ax.set_title("Share Price Distribution: Wins vs Losses")
        ax.legend(fontsize=8)

    # Chart 4: Bet size on wins vs losses
    ax = axes[1][1]
    win_amounts = [b["amount_usdc"] for b in resolved if b["is_win"]]
    loss_amounts = [b["amount_usdc"] for b in resolved if not b["is_win"]]
    if win_amounts or loss_amounts:
        ax.hist(
            win_amounts, bins=20, alpha=0.6, color="green",
            label=f"Wins (avg=${statistics.mean(win_amounts):.3f})" if win_amounts else "Wins",
            edgecolor="white",
        )
        ax.hist(
            loss_amounts, bins=20, alpha=0.6, color="red",
            label=f"Losses (avg=${statistics.mean(loss_amounts):.3f})" if loss_amounts else "Losses",
            edgecolor="white",
        )
        ax.set_xlabel("Bet Amount (USDC)")
        ax.set_ylabel("Count")
        ax.set_title("Bet Size Distribution: Wins vs Losses")
        ax.legend(fontsize=8)

    fig.suptitle("Deep Agent Analysis", fontsize=14, fontweight="bold")
    fig.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Deep single-agent PolyStrat analysis.")
    parser.add_argument(
        "address",
        nargs="?",
        default="0x33d20338f1700eda034ea2543933f94a2177ae4c",
        help="Safe address to analyze (default: Thomas)",
    )
    parser.add_argument("--no-charts", action="store_true")
    parser.add_argument("--no-tools", action="store_true", help="Skip mech tool enrichment")
    parser.add_argument("--json", dest="json_output", action="store_true")
    args = parser.parse_args()
    focus_addr = args.address.lower()

    print("=" * 60)
    print("  Deep PolyStrat Agent Analysis")
    print("=" * 60)

    # --- Fetch all agents ---
    print("\n[1/5] Fetching PolyStrat agents...")
    all_addresses = get_all_polystrat_agents()
    print(f"  Found {len(all_addresses)} agents.")

    # --- Fetch all bets ---
    print("\n[2/5] Fetching bets for all agents...")
    fleet_bets = {}
    for i, addr in enumerate(all_addresses, 1):
        if i % 20 == 0 or i == len(all_addresses):
            print(f"  Agent {i}/{len(all_addresses)}...")
        try:
            raw = fetch_agent_bets(addr)
            if raw:
                fleet_bets[addr] = process_bets(raw)
        except Exception as exc:
            print(f"  [warn] {addr[:10]}...: {exc}")

    if focus_addr not in fleet_bets:
        print(f"\n  ERROR: Focus agent {focus_addr} not found or has no bets.")
        return

    focus_bets = fleet_bets[focus_addr]
    print(f"  {len(fleet_bets)} agents with bets. Focus agent has {len(focus_bets)} bets.")

    # --- Tool enrichment ---
    fleet_tools = {}
    if not args.no_tools:
        print("\n[3/5] Fetching mech tool data...")
        for i, addr in enumerate(fleet_bets.keys(), 1):
            if i % 20 == 0 or i == len(fleet_bets):
                print(f"  Agent {i}/{len(fleet_bets)}...")
            try:
                mech_reqs = fetch_mech_requests(addr)
            except Exception:
                mech_reqs = []
            tools = {}
            for bet in fleet_bets[addr]:
                tools[bet["bet_id"]] = match_bet_to_tool(
                    bet["title"], bet["timestamp"], mech_reqs
                )
            fleet_tools[addr] = tools
    else:
        print("\n[3/5] Skipping tool enrichment.")

    focus_tools = fleet_tools.get(focus_addr, {})

    # --- Run analyses ---
    print("\n[4/5] Running deep analysis...")

    # Remove focus from fleet for comparison
    fleet_without_focus = {a: b for a, b in fleet_bets.items() if a != focus_addr}

    h2h = head_to_head_on_shared_markets(focus_bets, fleet_without_focus)
    trajectory = cumulative_pnl_trajectory(focus_bets)
    loss_list = biggest_losses(trajectory, n=15)
    win_list = biggest_wins(trajectory, n=10)
    streaks = streak_analysis(focus_bets)
    tool_comp = tool_accuracy_vs_fleet(
        focus_bets, focus_tools, fleet_without_focus,
        {a: t for a, t in fleet_tools.items() if a != focus_addr},
    )
    prices = share_price_analysis(focus_bets)
    weekly = weekly_accuracy_trend(focus_bets)

    # --- Output ---
    print("\n[5/5] Generating report...")
    if args.json_output:
        output = {
            "focus_address": focus_addr,
            "head_to_head": {k: v for k, v in h2h.items() if k != "per_market"},
            "trajectory_summary": {
                "total_bets": len(trajectory),
                "final_pnl": trajectory[-1]["cumulative_pnl"] if trajectory else 0,
                "max_pnl": max(t["cumulative_pnl"] for t in trajectory) if trajectory else 0,
                "min_pnl": min(t["cumulative_pnl"] for t in trajectory) if trajectory else 0,
            },
            "biggest_losses": loss_list,
            "biggest_wins": win_list,
            "streaks": streaks,
            "tool_comparison": tool_comp,
            "share_prices": prices,
            "weekly_trend": weekly,
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print_report(
            focus_addr, focus_bets, h2h, trajectory, loss_list, win_list,
            streaks, tool_comp, prices, weekly, len(fleet_bets),
        )
        if not args.no_charts:
            plot_charts(trajectory, weekly, focus_bets, prices)


if __name__ == "__main__":
    main()
