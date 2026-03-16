"""
Path persistence analysis for PolyStrat fleet.

Tests whether agent performance is truly random or if there is structural
path persistence — i.e., do the same agents consistently stay at the bottom?

Runs multiple persistence tests:
1. Quartile stickiness: do agents stay in the same performance quartile?
2. Per-agent weekly PnL streaks: how many agents have ALL negative weeks?
3. First-half vs second-half correlation: does early performance predict late?
4. Cumulative PnL trajectories: do any agents EVER recover from negative?
5. Rolling accuracy with more granular windows (8 windows instead of 4)

Usage:
    python polymarket/analyze_persistence.py
    python polymarket/analyze_persistence.py --json
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

# ---------------------------------------------------------------------------
# Constants & helpers (same as analyze_divergence.py)
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

USDC_DECIMALS_DIVISOR = 1_000_000
REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3


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


def get_all_polystrat_agents():
    if POLYGON_REGISTRY_SUBGRAPH_URL:
        try:
            query = """
{
  services(where: { agentIds_contains: [86] }, first: 1000) { id multisig }
}
"""
            response = call_subgraph(POLYGON_REGISTRY_SUBGRAPH_URL, query)
            return [s["multisig"].lower() for s in response["data"]["services"]]
        except Exception:
            pass
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
        pnl = 0.0
        if is_resolved:
            if is_win:
                pnl = shares / USDC_DECIMALS_DIVISOR - amount / USDC_DECIMALS_DIVISOR
            else:
                pnl = -(amount / USDC_DECIMALS_DIVISOR)
        records.append({
            "bet_id": bet.get("id", ""),
            "question_id": (question.get("id") or ""),
            "amount_usdc": amount / USDC_DECIMALS_DIVISOR,
            "shares_usdc": shares / USDC_DECIMALS_DIVISOR,
            "share_price": share_price,
            "timestamp": int(bet.get("blockTimestamp", 0)),
            "is_resolved": is_resolved,
            "is_win": is_win,
            "pnl": pnl,
        })
    return records


# ---------------------------------------------------------------------------
# Test 1: Quartile stickiness
# ---------------------------------------------------------------------------


def quartile_stickiness(agent_bets, n_windows=8, min_bets=5):
    """
    Split time into n_windows. For each window, rank agents into quartiles
    by accuracy. Track how often each agent stays in the same quartile
    across consecutive windows.
    """
    all_ts = []
    for bets in agent_bets.values():
        for b in bets:
            if b["is_resolved"]:
                all_ts.append(b["timestamp"])
    if not all_ts:
        return None

    min_ts = min(all_ts)
    max_ts = max(all_ts)
    window_size = (max_ts - min_ts) / n_windows

    # Per window: {addr: accuracy}
    window_data = []
    window_labels = []
    for w in range(n_windows):
        w_start = min_ts + w * window_size
        w_end = min_ts + (w + 1) * window_size
        window_labels.append(
            datetime.fromtimestamp(w_start, tz=timezone.utc).strftime("%m/%d")
        )
        agent_acc = {}
        agent_pnl = {}
        for addr, bets in agent_bets.items():
            w_bets = [
                b for b in bets
                if b["is_resolved"] and w_start <= b["timestamp"] < w_end
            ]
            if len(w_bets) >= min_bets:
                wins = sum(1 for b in w_bets if b["is_win"])
                agent_acc[addr] = wins / len(w_bets) * 100
                agent_pnl[addr] = sum(b["pnl"] for b in w_bets)
        window_data.append({"accuracy": agent_acc, "pnl": agent_pnl})

    # Assign quartiles per window
    def assign_quartiles(values_dict):
        if len(values_dict) < 4:
            return {}
        sorted_addrs = sorted(values_dict.keys(), key=lambda a: values_dict[a])
        n = len(sorted_addrs)
        quartiles = {}
        for i, addr in enumerate(sorted_addrs):
            q = int(i / n * 4)
            q = min(q, 3)  # 0=bottom, 3=top
            quartiles[addr] = q
        return quartiles

    window_quartiles_acc = [assign_quartiles(wd["accuracy"]) for wd in window_data]
    window_quartiles_pnl = [assign_quartiles(wd["pnl"]) for wd in window_data]

    # Measure stickiness: how often does an agent stay in the same quartile?
    def compute_stickiness(window_quartiles):
        same_q = 0
        total_transitions = 0
        bottom_stays = 0
        bottom_total = 0
        for i in range(len(window_quartiles) - 1):
            q1 = window_quartiles[i]
            q2 = window_quartiles[i + 1]
            common = set(q1.keys()) & set(q2.keys())
            for addr in common:
                total_transitions += 1
                if q1[addr] == q2[addr]:
                    same_q += 1
                if q1[addr] == 0:  # was in bottom quartile
                    bottom_total += 1
                    if q2[addr] == 0:  # stayed in bottom
                        bottom_stays += 1

        return {
            "same_quartile_rate": round(same_q / total_transitions * 100, 1) if total_transitions else None,
            "total_transitions": total_transitions,
            "bottom_quartile_retention_rate": (
                round(bottom_stays / bottom_total * 100, 1) if bottom_total else None
            ),
            "bottom_quartile_transitions": bottom_total,
        }

    acc_stickiness = compute_stickiness(window_quartiles_acc)
    pnl_stickiness = compute_stickiness(window_quartiles_pnl)

    # Track agents who are in bottom quartile in 3+ consecutive windows
    persistent_bottom_acc = find_persistent_bottom(window_quartiles_acc, min_streak=3)
    persistent_bottom_pnl = find_persistent_bottom(window_quartiles_pnl, min_streak=3)

    return {
        "n_windows": n_windows,
        "window_labels": window_labels,
        "accuracy_stickiness": acc_stickiness,
        "pnl_stickiness": pnl_stickiness,
        "persistent_bottom_accuracy": persistent_bottom_acc,
        "persistent_bottom_pnl": persistent_bottom_pnl,
        "window_data": window_data,
        "window_quartiles_acc": window_quartiles_acc,
        "window_quartiles_pnl": window_quartiles_pnl,
    }


def find_persistent_bottom(window_quartiles, min_streak=3):
    """Find agents who stay in bottom quartile for min_streak consecutive windows."""
    # Track consecutive bottom-quartile runs per agent
    agent_streaks = defaultdict(int)
    agent_max_streaks = defaultdict(int)

    for wq in window_quartiles:
        active = set()
        for addr, q in wq.items():
            if q == 0:
                agent_streaks[addr] += 1
                active.add(addr)
            else:
                agent_streaks[addr] = 0
            agent_max_streaks[addr] = max(
                agent_max_streaks[addr], agent_streaks[addr]
            )
        # Reset agents not present in this window
        for addr in list(agent_streaks.keys()):
            if addr not in wq:
                agent_streaks[addr] = 0

    return {
        addr: streak
        for addr, streak in agent_max_streaks.items()
        if streak >= min_streak
    }


# ---------------------------------------------------------------------------
# Test 2: Weekly PnL sign consistency
# ---------------------------------------------------------------------------


def weekly_pnl_signs(agent_bets, min_weeks=3):
    """
    For each agent, compute weekly PnL and check how many agents have
    ALL negative weeks (never had a profitable week).
    """
    all_ts = []
    for bets in agent_bets.values():
        for b in bets:
            if b["is_resolved"]:
                all_ts.append(b["timestamp"])
    if not all_ts:
        return None

    min_ts = min(all_ts)
    max_ts = max(all_ts)
    week_seconds = 7 * 86400

    agent_weekly = {}
    for addr, bets in agent_bets.items():
        resolved = sorted(
            [b for b in bets if b["is_resolved"]],
            key=lambda b: b["timestamp"],
        )
        if not resolved:
            continue

        weeks = []
        current_start = min_ts
        while current_start < max_ts:
            current_end = current_start + week_seconds
            week_bets = [
                b for b in resolved
                if current_start <= b["timestamp"] < current_end
            ]
            if week_bets:
                week_pnl = sum(b["pnl"] for b in week_bets)
                weeks.append({
                    "start": datetime.fromtimestamp(
                        current_start, tz=timezone.utc
                    ).strftime("%m/%d"),
                    "pnl": round(week_pnl, 2),
                    "n_bets": len(week_bets),
                    "positive": week_pnl > 0,
                })
            current_start = current_end

        if len(weeks) >= min_weeks:
            n_positive = sum(1 for w in weeks if w["positive"])
            n_negative = sum(1 for w in weeks if not w["positive"])
            agent_weekly[addr] = {
                "weeks": weeks,
                "n_positive_weeks": n_positive,
                "n_negative_weeks": n_negative,
                "total_weeks": len(weeks),
                "all_negative": n_positive == 0,
                "all_positive": n_negative == 0,
                "pct_negative": round(n_negative / len(weeks) * 100, 1),
            }

    all_neg = {a: w for a, w in agent_weekly.items() if w["all_negative"]}
    all_pos = {a: w for a, w in agent_weekly.items() if w["all_positive"]}

    pct_negative_weeks = [w["pct_negative"] for w in agent_weekly.values()]

    return {
        "total_agents_analyzed": len(agent_weekly),
        "all_negative_weeks": len(all_neg),
        "all_positive_weeks": len(all_pos),
        "all_negative_agents": {
            a: w["total_weeks"] for a, w in all_neg.items()
        },
        "all_positive_agents": {
            a: w["total_weeks"] for a, w in all_pos.items()
        },
        "avg_pct_negative_weeks": round(
            statistics.mean(pct_negative_weeks), 1
        ) if pct_negative_weeks else None,
        "median_pct_negative_weeks": round(
            statistics.median(pct_negative_weeks), 1
        ) if pct_negative_weeks else None,
        "agent_weekly": agent_weekly,
    }


# ---------------------------------------------------------------------------
# Test 3: First-half vs second-half correlation
# ---------------------------------------------------------------------------


def half_split_correlation(agent_bets, min_bets_per_half=10):
    """
    Split each agent's resolved bets in half chronologically.
    Compute accuracy and PnL for each half.
    Measure correlation between first-half and second-half performance.
    """
    agent_halves = {}
    for addr, bets in agent_bets.items():
        resolved = sorted(
            [b for b in bets if b["is_resolved"]],
            key=lambda b: b["timestamp"],
        )
        if len(resolved) < min_bets_per_half * 2:
            continue

        mid = len(resolved) // 2
        first = resolved[:mid]
        second = resolved[mid:]

        def half_stats(half_bets):
            wins = sum(1 for b in half_bets if b["is_win"])
            pnl = sum(b["pnl"] for b in half_bets)
            return {
                "n_bets": len(half_bets),
                "accuracy": round(wins / len(half_bets) * 100, 1),
                "pnl": round(pnl, 2),
            }

        agent_halves[addr] = {
            "first": half_stats(first),
            "second": half_stats(second),
        }

    if len(agent_halves) < 3:
        return None

    # Compute Pearson-ish correlation (using Spearman rank)
    addrs = sorted(agent_halves.keys())

    def spearman(vals1, vals2):
        def _rank(values):
            sorted_vals = sorted(enumerate(values), key=lambda x: x[1])
            ranks = [0.0] * len(values)
            for rank, (idx, _) in enumerate(sorted_vals):
                ranks[idx] = float(rank)
            return ranks
        r1 = _rank(vals1)
        r2 = _rank(vals2)
        n = len(r1)
        if n < 3:
            return None
        d_sq = sum((a - b) ** 2 for a, b in zip(r1, r2))
        denom = n * (n ** 2 - 1)
        return round(1 - (6 * d_sq / denom), 3) if denom > 0 else None

    acc_first = [agent_halves[a]["first"]["accuracy"] for a in addrs]
    acc_second = [agent_halves[a]["second"]["accuracy"] for a in addrs]
    pnl_first = [agent_halves[a]["first"]["pnl"] for a in addrs]
    pnl_second = [agent_halves[a]["second"]["pnl"] for a in addrs]

    acc_rho = spearman(acc_first, acc_second)
    pnl_rho = spearman(pnl_first, pnl_second)

    # How many agents who were bottom-quartile first half stayed bottom second half
    def quartile_transition(first_vals, second_vals):
        n = len(first_vals)
        if n < 4:
            return None
        sorted_first = sorted(range(n), key=lambda i: first_vals[i])
        sorted_second = sorted(range(n), key=lambda i: second_vals[i])
        bottom_q_size = n // 4
        bottom_first = set(sorted_first[:bottom_q_size])
        bottom_second = set(sorted_second[:bottom_q_size])
        stayed = len(bottom_first & bottom_second)
        return {
            "bottom_q_size": bottom_q_size,
            "stayed_bottom": stayed,
            "retention_rate": round(stayed / bottom_q_size * 100, 1) if bottom_q_size else None,
        }

    acc_transition = quartile_transition(acc_first, acc_second)
    pnl_transition = quartile_transition(pnl_first, pnl_second)

    return {
        "agents_analyzed": len(agent_halves),
        "accuracy_rank_correlation": acc_rho,
        "pnl_rank_correlation": pnl_rho,
        "accuracy_quartile_transition": acc_transition,
        "pnl_quartile_transition": pnl_transition,
        "agent_halves": agent_halves,
    }


# ---------------------------------------------------------------------------
# Test 4: Recovery analysis
# ---------------------------------------------------------------------------


def recovery_analysis(agent_bets, min_bets=20):
    """
    For agents that go negative, do they ever recover?
    Track max drawdown and whether cumulative PnL ever crosses back to positive.
    """
    results = {}
    for addr, bets in agent_bets.items():
        resolved = sorted(
            [b for b in bets if b["is_resolved"]],
            key=lambda b: b["timestamp"],
        )
        if len(resolved) < min_bets:
            continue

        cum_pnl = 0.0
        peak = 0.0
        max_drawdown = 0.0
        went_negative = False
        recovered_count = 0
        negative_streak = 0
        max_negative_streak = 0

        for b in resolved:
            cum_pnl += b["pnl"]
            peak = max(peak, cum_pnl)
            drawdown = peak - cum_pnl
            max_drawdown = max(max_drawdown, drawdown)

            if cum_pnl < 0:
                if not went_negative:
                    went_negative = True
                negative_streak += 1
                max_negative_streak = max(max_negative_streak, negative_streak)
            else:
                if went_negative:
                    recovered_count += 1
                    went_negative = False
                negative_streak = 0

        final_pnl = cum_pnl
        results[addr] = {
            "n_bets": len(resolved),
            "final_pnl": round(final_pnl, 2),
            "max_drawdown": round(max_drawdown, 2),
            "went_negative": went_negative or recovered_count > 0,
            "currently_negative": final_pnl < 0,
            "recovery_count": recovered_count,
            "ever_recovered": recovered_count > 0,
            "max_consecutive_bets_negative": max_negative_streak,
        }

    went_neg = [r for r in results.values() if r["went_negative"]]
    never_recovered = [r for r in went_neg if not r["ever_recovered"]]

    return {
        "total_agents": len(results),
        "went_negative": len(went_neg),
        "never_recovered": len(never_recovered),
        "currently_negative": sum(1 for r in results.values() if r["currently_negative"]),
        "pct_never_recovered": (
            round(len(never_recovered) / len(went_neg) * 100, 1)
            if went_neg else None
        ),
        "avg_max_drawdown": round(
            statistics.mean([r["max_drawdown"] for r in results.values()]), 2
        ),
        "agents": results,
    }


# ---------------------------------------------------------------------------
# Test 5: Market assignment stickiness
# ---------------------------------------------------------------------------


def market_category_persistence(agent_bets):
    """
    Check if agents consistently get the same 'type' of market.
    Proxy: extract keywords from titles and check if agents cluster.
    Also check: do agents consistently bet on the same price ranges?
    """
    # Per-agent: distribution of share prices (as a proxy for market difficulty)
    agent_price_profiles = {}
    for addr, bets in agent_bets.items():
        resolved = [b for b in bets if b["is_resolved"] and b["share_price"] > 0]
        if len(resolved) < 10:
            continue

        prices = [b["share_price"] for b in resolved]
        longshot = sum(1 for p in prices if p < 0.3) / len(prices) * 100
        underdog = sum(1 for p in prices if 0.3 <= p < 0.5) / len(prices) * 100
        favorite = sum(1 for p in prices if p >= 0.7) / len(prices) * 100

        agent_price_profiles[addr] = {
            "n_bets": len(resolved),
            "avg_price": round(statistics.mean(prices), 3),
            "pct_longshot": round(longshot, 1),
            "pct_underdog": round(underdog, 1),
            "pct_favorite": round(favorite, 1),
        }

    if not agent_price_profiles:
        return None

    # Check variance in longshot exposure
    longshot_pcts = [p["pct_longshot"] for p in agent_price_profiles.values()]
    fav_pcts = [p["pct_favorite"] for p in agent_price_profiles.values()]

    return {
        "agents_analyzed": len(agent_price_profiles),
        "longshot_exposure_mean": round(statistics.mean(longshot_pcts), 1),
        "longshot_exposure_stdev": round(statistics.stdev(longshot_pcts), 1) if len(longshot_pcts) > 1 else 0,
        "longshot_exposure_range": [round(min(longshot_pcts), 1), round(max(longshot_pcts), 1)],
        "favorite_exposure_mean": round(statistics.mean(fav_pcts), 1),
        "favorite_exposure_stdev": round(statistics.stdev(fav_pcts), 1) if len(fav_pcts) > 1 else 0,
        "favorite_exposure_range": [round(min(fav_pcts), 1), round(max(fav_pcts), 1)],
        "agent_profiles": agent_price_profiles,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_report(quartile, weekly_signs, half_split, recovery, market_cats,
                 focus_addr):
    w = 74
    print("\n" + "=" * w)
    print("  PolyStrat Path Persistence Analysis")
    print("=" * w)

    # --- Test 1: Quartile Stickiness ---
    print("\n--- TEST 1: QUARTILE STICKINESS ---")
    if quartile:
        n = quartile["n_windows"]
        labels = " → ".join(quartile["window_labels"])
        print(f"  Windows ({n}): {labels}")

        a = quartile["accuracy_stickiness"]
        p = quartile["pnl_stickiness"]
        # Random baseline: if quartiles were random, 25% would stay in same quartile
        print(f"\n  ACCURACY quartile retention:")
        print(f"    Same quartile rate:          {a['same_quartile_rate']}% (random baseline: 25%)")
        print(f"    Bottom-Q retention rate:      {a['bottom_quartile_retention_rate']}% "
              f"({a.get('bottom_quartile_transitions', '?')} transitions)")

        print(f"\n  PNL quartile retention:")
        print(f"    Same quartile rate:          {p['same_quartile_rate']}% (random baseline: 25%)")
        print(f"    Bottom-Q retention rate:      {p['bottom_quartile_retention_rate']}% "
              f"({p.get('bottom_quartile_transitions', '?')} transitions)")

        if a["same_quartile_rate"] and a["same_quartile_rate"] > 35:
            print("  >> ACCURACY quartiles are STICKY — agents tend to stay in their quartile.")
        if p["same_quartile_rate"] and p["same_quartile_rate"] > 35:
            print("  >> PNL quartiles are STICKY — agents tend to stay in their PnL quartile.")

        # Persistent bottom agents
        pb_acc = quartile["persistent_bottom_accuracy"]
        pb_pnl = quartile["persistent_bottom_pnl"]
        if pb_acc:
            print(f"\n  Agents in bottom accuracy quartile for 3+ consecutive windows: {len(pb_acc)}")
            for addr, streak in sorted(pb_acc.items(), key=lambda x: -x[1]):
                marker = " <<<" if addr == focus_addr else ""
                print(f"    {addr[:14]}...{addr[-6:]}: {streak} consecutive windows{marker}")
        if pb_pnl:
            print(f"\n  Agents in bottom PnL quartile for 3+ consecutive windows: {len(pb_pnl)}")
            for addr, streak in sorted(pb_pnl.items(), key=lambda x: -x[1]):
                marker = " <<<" if addr == focus_addr else ""
                print(f"    {addr[:14]}...{addr[-6:]}: {streak} consecutive windows{marker}")
    else:
        print("  Not enough data.")

    # --- Test 2: Weekly PnL Signs ---
    print("\n--- TEST 2: WEEKLY PNL CONSISTENCY ---")
    if weekly_signs:
        print(f"  Agents with {weekly_signs['total_agents_analyzed']} analyzable agents ({3}+ weeks):")
        print(f"  ALL negative weeks:    {weekly_signs['all_negative_weeks']} agents")
        print(f"  ALL positive weeks:    {weekly_signs['all_positive_weeks']} agents")
        print(f"  Avg % negative weeks:  {weekly_signs['avg_pct_negative_weeks']}%")
        print(f"  Median % negative weeks: {weekly_signs['median_pct_negative_weeks']}%")

        if weekly_signs["all_negative_agents"]:
            print(f"\n  Agents with ZERO profitable weeks:")
            for addr, n_weeks in weekly_signs["all_negative_agents"].items():
                marker = " <<<" if addr == focus_addr else ""
                print(f"    {addr[:14]}...{addr[-6:]}: {n_weeks} weeks, all negative{marker}")

        if weekly_signs["all_positive_agents"]:
            print(f"\n  Agents with ALL profitable weeks:")
            for addr, n_weeks in weekly_signs["all_positive_agents"].items():
                print(f"    {addr[:14]}...{addr[-6:]}: {n_weeks} weeks, all positive")
    else:
        print("  Not enough data.")

    # --- Test 3: Half-Split Correlation ---
    print("\n--- TEST 3: FIRST-HALF vs SECOND-HALF PERFORMANCE ---")
    if half_split:
        print(f"  Agents analyzed (>= 20 resolved bets): {half_split['agents_analyzed']}")
        print(f"  Accuracy rank correlation (1st vs 2nd half): {half_split['accuracy_rank_correlation']}")
        print(f"  PnL rank correlation (1st vs 2nd half):      {half_split['pnl_rank_correlation']}")

        if half_split["accuracy_quartile_transition"]:
            t = half_split["accuracy_quartile_transition"]
            print(f"\n  Bottom accuracy quartile (1st half → 2nd half):")
            print(f"    {t['stayed_bottom']}/{t['bottom_q_size']} stayed in bottom "
                  f"({t['retention_rate']}%, random baseline: 25%)")

        if half_split["pnl_quartile_transition"]:
            t = half_split["pnl_quartile_transition"]
            print(f"\n  Bottom PnL quartile (1st half → 2nd half):")
            print(f"    {t['stayed_bottom']}/{t['bottom_q_size']} stayed in bottom "
                  f"({t['retention_rate']}%, random baseline: 25%)")

        rho_acc = half_split["accuracy_rank_correlation"]
        rho_pnl = half_split["pnl_rank_correlation"]
        if rho_acc is not None and rho_acc > 0.3:
            print("  >> SIGNIFICANT accuracy persistence: early performance predicts late performance.")
        if rho_pnl is not None and rho_pnl > 0.3:
            print("  >> SIGNIFICANT PnL persistence: early PnL predicts late PnL.")
    else:
        print("  Not enough data.")

    # --- Test 4: Recovery ---
    print("\n--- TEST 4: RECOVERY ANALYSIS ---")
    if recovery:
        print(f"  Agents analyzed (>= 20 bets):  {recovery['total_agents']}")
        print(f"  Went negative at some point:   {recovery['went_negative']}")
        print(f"  Never recovered to positive:   {recovery['never_recovered']}"
              f" ({recovery['pct_never_recovered']}%)")
        print(f"  Currently negative:            {recovery['currently_negative']}")
        print(f"  Avg max drawdown:              ${recovery['avg_max_drawdown']:.2f}")

        if recovery["pct_never_recovered"] and recovery["pct_never_recovered"] > 60:
            print("  >> Most agents that go negative NEVER recover — path dependency is real.")

        # Focus agent
        focus_rec = recovery["agents"].get(focus_addr)
        if focus_rec:
            print(f"\n  Focus agent ({focus_addr[:10]}...):")
            print(f"    Final PnL: ${focus_rec['final_pnl']:.2f}")
            print(f"    Max drawdown: ${focus_rec['max_drawdown']:.2f}")
            print(f"    Max consecutive bets while negative: {focus_rec['max_consecutive_bets_negative']}")
            print(f"    Ever recovered: {focus_rec['ever_recovered']}")
    else:
        print("  Not enough data.")

    # --- Test 5: Market Category Persistence ---
    print("\n--- TEST 5: MARKET DIFFICULTY EXPOSURE ---")
    if market_cats:
        print(f"  Agents analyzed: {market_cats['agents_analyzed']}")
        print(f"\n  Longshot exposure (share price < 0.30):")
        print(f"    Mean:  {market_cats['longshot_exposure_mean']}%")
        print(f"    Stdev: {market_cats['longshot_exposure_stdev']}%")
        print(f"    Range: {market_cats['longshot_exposure_range'][0]}%"
              f" – {market_cats['longshot_exposure_range'][1]}%")
        print(f"\n  Favorite exposure (share price >= 0.70):")
        print(f"    Mean:  {market_cats['favorite_exposure_mean']}%")
        print(f"    Stdev: {market_cats['favorite_exposure_stdev']}%")
        print(f"    Range: {market_cats['favorite_exposure_range'][0]}%"
              f" – {market_cats['favorite_exposure_range'][1]}%")

        if market_cats["longshot_exposure_stdev"] > 5:
            print("  >> Significant variance in longshot exposure across agents.")
            print("     Agents with more longshot bets will have more volatile (and likely worse) PnL.")

        focus_profile = market_cats["agent_profiles"].get(focus_addr)
        if focus_profile:
            print(f"\n  Focus agent profile:")
            print(f"    Longshot exposure: {focus_profile['pct_longshot']}% "
                  f"(fleet mean: {market_cats['longshot_exposure_mean']}%)")
            print(f"    Favorite exposure: {focus_profile['pct_favorite']}% "
                  f"(fleet mean: {market_cats['favorite_exposure_mean']}%)")
    else:
        print("  Not enough data.")

    # --- Verdict ---
    print("\n--- VERDICT ---")
    evidence_for = []
    evidence_against = []

    if quartile:
        a = quartile["accuracy_stickiness"]
        p = quartile["pnl_stickiness"]
        if a["same_quartile_rate"] and a["same_quartile_rate"] > 30:
            evidence_for.append(
                f"Accuracy quartile retention {a['same_quartile_rate']}% (random: 25%)"
            )
        else:
            evidence_against.append(
                f"Accuracy quartile retention {a['same_quartile_rate']}% (near random: 25%)"
            )
        if p["same_quartile_rate"] and p["same_quartile_rate"] > 30:
            evidence_for.append(
                f"PnL quartile retention {p['same_quartile_rate']}% (random: 25%)"
            )

    if half_split:
        if half_split["accuracy_rank_correlation"] and half_split["accuracy_rank_correlation"] > 0.2:
            evidence_for.append(
                f"First/second half accuracy correlation: {half_split['accuracy_rank_correlation']}"
            )
        else:
            evidence_against.append(
                f"First/second half accuracy correlation: {half_split['accuracy_rank_correlation']}"
            )
        if half_split["pnl_rank_correlation"] and half_split["pnl_rank_correlation"] > 0.2:
            evidence_for.append(
                f"First/second half PnL correlation: {half_split['pnl_rank_correlation']}"
            )

    if recovery and recovery["pct_never_recovered"]:
        if recovery["pct_never_recovered"] > 50:
            evidence_for.append(
                f"{recovery['pct_never_recovered']}% of agents that go negative never recover"
            )

    if weekly_signs and weekly_signs["all_negative_weeks"] > 2:
        evidence_for.append(
            f"{weekly_signs['all_negative_weeks']} agents have ZERO profitable weeks"
        )

    if market_cats and market_cats["longshot_exposure_stdev"] > 5:
        evidence_for.append(
            f"Longshot exposure varies widely (stdev={market_cats['longshot_exposure_stdev']}%)"
        )

    print(f"\n  Evidence FOR path persistence ({len(evidence_for)}):")
    for e in evidence_for:
        print(f"    + {e}")
    print(f"\n  Evidence AGAINST path persistence ({len(evidence_against)}):")
    for e in evidence_against:
        print(f"    - {e}")

    if len(evidence_for) > len(evidence_against):
        print("\n  >> CONCLUSION: There IS meaningful path persistence. David is right.")
        print("     Agents are not purely random — performance has some structural stickiness.")
    elif len(evidence_for) == len(evidence_against):
        print("\n  >> CONCLUSION: Mixed evidence. Some persistence but not overwhelming.")
    else:
        print("\n  >> CONCLUSION: Limited persistence — mostly noise.")

    print("\n" + "=" * w)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Test for path persistence in PolyStrat fleet performance."
    )
    parser.add_argument(
        "--focus", default="0x33d20338f1700eda034ea2543933f94a2177ae4c",
        help="Address to highlight (default: Thomas)",
    )
    parser.add_argument("--min-bets", type=int, default=5)
    parser.add_argument("--json", dest="json_output", action="store_true")
    args = parser.parse_args()
    focus_addr = args.focus.lower()

    print("=" * 60)
    print("  PolyStrat Path Persistence Analysis")
    print("=" * 60)

    print("\n[1/3] Fetching agents...")
    all_addresses = get_all_polystrat_agents()
    print(f"  Found {len(all_addresses)} agents.")

    print("\n[2/3] Fetching all bets...")
    agent_bets = {}
    for i, addr in enumerate(all_addresses, 1):
        if i % 20 == 0 or i == len(all_addresses):
            print(f"  Agent {i}/{len(all_addresses)}...")
        try:
            raw = fetch_agent_bets(addr)
            if raw:
                agent_bets[addr] = process_bets(raw)
        except Exception as exc:
            print(f"  [warn] {addr[:10]}...: {exc}")

    print(f"  {len(agent_bets)} agents with bets.")

    print("\n[3/3] Running persistence tests...")
    quartile = quartile_stickiness(agent_bets, n_windows=8, min_bets=args.min_bets)
    weekly_signs = weekly_pnl_signs(agent_bets, min_weeks=3)
    half_split = half_split_correlation(agent_bets, min_bets_per_half=10)
    recovery = recovery_analysis(agent_bets, min_bets=20)
    market_cats = market_category_persistence(agent_bets)

    if args.json_output:
        output = {
            "quartile_stickiness": {
                k: v for k, v in (quartile or {}).items()
                if k not in ("window_data", "window_quartiles_acc", "window_quartiles_pnl")
            },
            "weekly_pnl_signs": {
                k: v for k, v in (weekly_signs or {}).items()
                if k != "agent_weekly"
            },
            "half_split": {
                k: v for k, v in (half_split or {}).items()
                if k != "agent_halves"
            },
            "recovery": {
                k: v for k, v in (recovery or {}).items()
                if k != "agents"
            },
            "market_categories": {
                k: v for k, v in (market_cats or {}).items()
                if k != "agent_profiles"
            },
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print_report(
            quartile, weekly_signs, half_split, recovery, market_cats,
            focus_addr,
        )


if __name__ == "__main__":
    main()
