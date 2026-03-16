"""
Deep persistence mechanism analysis for PolyStrat fleet.

Reconstructs internal feedback loops (accuracy store, Kelly dynamic fraction,
tool quarantine) using on-chain data to explain why some agents persistently
over/underperform despite sharing identical logic.

Tests 6 hypotheses:
  H1: Accuracy store feedback loop (epsilon-greedy lock-in)
  H2: Kelly dynamic fraction amplification
  H3: Tool quarantine cascade
  H4: Longshot exposure as tool symptom
  H5: The 0.80 price threshold gap
  H6: Missing minimum edge requirement

Usage:
    python polymarket/analyze_persistence_deep.py
    python polymarket/analyze_persistence_deep.py --no-charts --no-tools
    python polymarket/analyze_persistence_deep.py --json
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

# Trader config constants (from polymarket_trader/service.yaml)
POLICY_EPSILON = 0.25
STATIC_KELLY_FRACTION = 1.5
VOLUME_FACTOR_REGULARIZATION = 0.1
UNSCALED_RANGE = (-0.5, 80.5)
SCALED_RANGE = (0.0, 1.0)
QUARANTINE_DURATION_SECONDS = 10800  # 3 hours


# ---------------------------------------------------------------------------
# Helpers replicated from trader codebase
# ---------------------------------------------------------------------------


def scale_value(value, min_max_bounds, scale_bounds=(0.0, 1.0)):
    """Replicate scaling.py scale_value."""
    min_, max_ = min_max_bounds
    if max_ == min_:
        return scale_bounds[0]
    std = (value - min_) / (max_ - min_)
    min_bound, max_bound = scale_bounds
    return std * (max_bound - min_bound) + min_bound


def compute_weighted_accuracy(accuracy_store):
    """Replicate policy.py update_weighted_accuracy."""
    n_requests = sum(info["requests"] for info in accuracy_store.values())
    weighted = {}
    for tool, info in accuracy_store.items():
        raw = info["accuracy"] + (info["requests"] / (n_requests or 1)) * VOLUME_FACTOR_REGULARIZATION
        weighted[tool] = scale_value(raw, UNSCALED_RANGE, SCALED_RANGE)
    return weighted


# ---------------------------------------------------------------------------
# HTTP / subgraph helpers (same as analyze_divergence.py)
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
# Data fetching (same as analyze_divergence.py)
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
        pnl = 0.0
        if is_resolved:
            if is_win:
                pnl = shares / USDC_DECIMALS_DIVISOR - amount / USDC_DECIMALS_DIVISOR
            else:
                pnl = -(amount / USDC_DECIMALS_DIVISOR)
        records.append({
            "bet_id": bet.get("id", ""),
            "question_id": (question.get("id") or ""),
            "title": (question.get("metadata") or {}).get("title", ""),
            "outcome_index": outcome_idx,
            "amount_usdc": amount / USDC_DECIMALS_DIVISOR,
            "shares_usdc": shares / USDC_DECIMALS_DIVISOR,
            "share_price": share_price,
            "timestamp": int(bet.get("blockTimestamp", 0)),
            "is_resolved": is_resolved,
            "is_win": is_win,
            "pnl": pnl,
        })
    return records


def spearman(x, y):
    """Spearman rank correlation."""
    def rank(vals):
        s = sorted(enumerate(vals), key=lambda v: v[1])
        r = [0.0] * len(vals)
        for i, (idx, _) in enumerate(s):
            r[idx] = float(i)
        return r
    if len(x) < 3:
        return None
    r1, r2 = rank(x), rank(y)
    n = len(r1)
    d_sq = sum((a - b) ** 2 for a, b in zip(r1, r2))
    denom = n * (n ** 2 - 1)
    return round(1 - (6 * d_sq / denom), 3) if denom > 0 else None


# ---------------------------------------------------------------------------
# H1: Accuracy Store Feedback Loop
# ---------------------------------------------------------------------------


def simulate_accuracy_store(agent_bets, agent_tools):
    """
    Replay each agent's resolved bets chronologically and reconstruct the
    accuracy store trajectory. Track when best_tool locks in and how
    weighted_accuracy diverges.
    """
    results = {}

    for addr, bets in agent_bets.items():
        tools_map = agent_tools.get(addr, {})
        resolved = sorted(
            [b for b in bets if b["is_resolved"] and tools_map.get(b["bet_id"], "unknown") != "unknown"],
            key=lambda b: b["timestamp"],
        )
        if len(resolved) < 5:
            continue

        # Discover tools used by this agent
        used_tools = set(tools_map.get(b["bet_id"], "unknown") for b in resolved)
        used_tools.discard("unknown")
        if not used_tools:
            continue

        # Initialize accuracy store (all zeros, like a fresh agent)
        store = {tool: {"requests": 0, "accuracy": 0.0} for tool in used_tools}

        best_tool_history = []
        weighted_acc_history = []  # weighted_accuracy of best tool over time
        lock_in_round = -1
        last_best = None
        last_change_round = 0

        for i, bet in enumerate(resolved):
            tool = tools_map.get(bet["bet_id"], "unknown")
            if tool == "unknown" or tool not in store:
                continue

            # Update accuracy store (replicate policy.py lines 259-269)
            info = store[tool]
            total_correct = info["accuracy"] * info["requests"]
            if bet["is_win"]:
                total_correct += 1
            info["requests"] += 1
            info["accuracy"] = total_correct / info["requests"]

            # Compute weighted accuracy
            weighted = compute_weighted_accuracy(store)
            best = max(weighted, key=weighted.get) if weighted else None
            best_tool_history.append(best)
            weighted_acc_history.append(weighted.get(best, 0) if best else 0)

            if best != last_best:
                last_best = best
                last_change_round = i

        if best_tool_history:
            lock_in_round = last_change_round

        # Early luck: accuracy of first 10 bets
        first_n = min(10, len(resolved))
        early_wins = sum(1 for b in resolved[:first_n] if b["is_win"])
        early_acc = early_wins / first_n * 100

        total_pnl = sum(b["pnl"] for b in resolved)

        results[addr] = {
            "n_resolved": len(resolved),
            "lock_in_round": lock_in_round,
            "final_best_tool": best_tool_history[-1] if best_tool_history else None,
            "final_weighted_acc": weighted_acc_history[-1] if weighted_acc_history else None,
            "early_accuracy": round(early_acc, 1),
            "total_pnl": round(total_pnl, 2),
            "weighted_acc_history": weighted_acc_history,
            "best_tool_history": best_tool_history,
            "store": {t: {"requests": s["requests"], "accuracy": round(s["accuracy"], 3)}
                      for t, s in store.items()},
        }

    return results


# ---------------------------------------------------------------------------
# H2: Kelly Dynamic Fraction Amplification
# ---------------------------------------------------------------------------


def analyze_kelly_amplification(agent_bets, agent_tools, store_results):
    """
    Using simulated accuracy stores, compute the dynamic_kelly_fraction at each
    bet and estimate its impact on PnL divergence.
    """
    agent_fractions = {}

    for addr, bets in agent_bets.items():
        if addr not in store_results:
            continue
        sr = store_results[addr]
        tools_map = agent_tools.get(addr, {})
        resolved = sorted(
            [b for b in bets if b["is_resolved"] and tools_map.get(b["bet_id"], "unknown") != "unknown"],
            key=lambda b: b["timestamp"],
        )

        fractions = []
        # Use the final weighted accuracy as a proxy
        # (in reality it changes over time, but final state captures the settled value)
        final_wa = sr.get("final_weighted_acc", 0.5)
        dynamic_fraction = STATIC_KELLY_FRACTION + (final_wa if final_wa else 0.5)

        for b in resolved:
            fractions.append(dynamic_fraction)

        if fractions:
            agent_fractions[addr] = {
                "avg_fraction": round(statistics.mean(fractions), 3),
                "pnl": sr["total_pnl"],
                "n_bets": len(resolved),
                "final_weighted_acc": final_wa,
            }

    # Counterfactual: if everyone had static fraction
    # Approximate: actual_pnl * (STATIC_KELLY_FRACTION / agent_dynamic_fraction)
    counterfactual_pnl = {}
    for addr, data in agent_fractions.items():
        if data["avg_fraction"] > 0:
            ratio = STATIC_KELLY_FRACTION / data["avg_fraction"]
            counterfactual_pnl[addr] = round(data["pnl"] * ratio, 2)

    return {
        "agent_fractions": agent_fractions,
        "counterfactual_pnl": counterfactual_pnl,
    }


# ---------------------------------------------------------------------------
# H3: Tool Quarantine Signals
# ---------------------------------------------------------------------------


def analyze_quarantine_signals(agent_bets, agent_tools):
    """
    Look for quarantine-length gaps (3hrs) in per-tool usage patterns.
    Check if early superforcaster usage correlates with PnL.
    """
    agent_stats = {}

    for addr, bets in agent_bets.items():
        tools_map = agent_tools.get(addr, {})
        resolved = sorted(
            [b for b in bets if b["is_resolved"]],
            key=lambda b: b["timestamp"],
        )
        if len(resolved) < 10:
            continue

        # Per-tool usage timestamps
        tool_timestamps = defaultdict(list)
        for b in resolved:
            tool = tools_map.get(b["bet_id"], "unknown")
            if tool != "unknown":
                tool_timestamps[tool].append(b["timestamp"])

        # Detect 3hr gaps per tool
        quarantine_gaps = {}
        for tool, timestamps in tool_timestamps.items():
            if len(timestamps) < 2:
                continue
            ts_sorted = sorted(timestamps)
            gaps_3h = 0
            for i in range(1, len(ts_sorted)):
                gap = ts_sorted[i] - ts_sorted[i - 1]
                if QUARANTINE_DURATION_SECONDS * 0.8 <= gap <= QUARANTINE_DURATION_SECONDS * 1.5:
                    gaps_3h += 1
            quarantine_gaps[tool] = gaps_3h

        # Early superforcaster usage (first 20 bets)
        first_20 = resolved[:20]
        sf_early = sum(1 for b in first_20 if tools_map.get(b["bet_id"]) == "superforcaster")
        sf_early_pct = sf_early / len(first_20) * 100 if first_20 else 0

        total_pnl = sum(b["pnl"] for b in resolved)

        agent_stats[addr] = {
            "quarantine_gaps": quarantine_gaps,
            "sf_early_pct": round(sf_early_pct, 1),
            "total_pnl": round(total_pnl, 2),
        }

    return agent_stats


# ---------------------------------------------------------------------------
# H4: Longshot Exposure by Tool
# ---------------------------------------------------------------------------


def analyze_longshot_by_tool(agent_bets, agent_tools):
    """
    Cross-tabulate share price distributions by tool to test if longshot
    exposure is really a tool effect.
    """
    tool_stats = defaultdict(lambda: {
        "total": 0, "longshot": 0, "wins": 0, "longshot_wins": 0,
        "pnl": 0.0, "longshot_pnl": 0.0,
    })

    for addr, bets in agent_bets.items():
        tools_map = agent_tools.get(addr, {})
        for b in bets:
            if not b["is_resolved"]:
                continue
            tool = tools_map.get(b["bet_id"], "unknown")
            is_longshot = b["share_price"] < 0.3

            tool_stats[tool]["total"] += 1
            tool_stats[tool]["pnl"] += b["pnl"]
            if b["is_win"]:
                tool_stats[tool]["wins"] += 1
            if is_longshot:
                tool_stats[tool]["longshot"] += 1
                tool_stats[tool]["longshot_pnl"] += b["pnl"]
                if b["is_win"]:
                    tool_stats[tool]["longshot_wins"] += 1

    results = {}
    for tool, s in tool_stats.items():
        results[tool] = {
            "total": s["total"],
            "longshot_count": s["longshot"],
            "longshot_pct": round(s["longshot"] / s["total"] * 100, 1) if s["total"] else 0,
            "accuracy_pct": round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0,
            "longshot_accuracy_pct": (
                round(s["longshot_wins"] / s["longshot"] * 100, 1) if s["longshot"] else None
            ),
            "pnl": round(s["pnl"], 2),
            "longshot_pnl": round(s["longshot_pnl"], 2),
        }

    return results


# ---------------------------------------------------------------------------
# H5: The 0.80 Price Threshold Gap
# ---------------------------------------------------------------------------


def analyze_threshold_gap(agent_bets):
    """
    Fine-grained price bucket analysis with counterfactuals for different thresholds.
    """
    # Fine buckets
    bucket_edges = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40,
                    0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.01]
    buckets = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0, "invested": 0.0})

    all_resolved = []
    for bets in agent_bets.values():
        for b in bets:
            if b["is_resolved"]:
                all_resolved.append(b)

    for b in all_resolved:
        for i in range(len(bucket_edges) - 1):
            if bucket_edges[i] <= b["share_price"] < bucket_edges[i + 1]:
                label = f"{bucket_edges[i]:.2f}-{bucket_edges[i+1]:.2f}"
                buckets[label]["count"] += 1
                buckets[label]["pnl"] += b["pnl"]
                buckets[label]["invested"] += b["amount_usdc"]
                if b["is_win"]:
                    buckets[label]["wins"] += 1
                break

    # Counterfactuals
    total_pnl = sum(b["pnl"] for b in all_resolved)
    thresholds = [0.80, 0.75, 0.70, 0.65]
    counterfactuals = {}
    for thresh in thresholds:
        filtered = [b for b in all_resolved if thresh >= b["share_price"] >= (1 - thresh)]
        cf_pnl = sum(b["pnl"] for b in filtered)
        cf_count = len(filtered)
        counterfactuals[f"{thresh:.2f}"] = {
            "pnl": round(cf_pnl, 2),
            "count": cf_count,
            "removed": len(all_resolved) - cf_count,
        }

    bucket_results = {}
    for label in sorted(buckets.keys()):
        s = buckets[label]
        bucket_results[label] = {
            "count": s["count"],
            "wins": s["wins"],
            "accuracy_pct": round(s["wins"] / s["count"] * 100, 1) if s["count"] else 0,
            "pnl": round(s["pnl"], 2),
            "roi_pct": round(s["pnl"] / s["invested"] * 100, 1) if s["invested"] else 0,
        }

    return {
        "total_pnl": round(total_pnl, 2),
        "total_bets": len(all_resolved),
        "buckets": bucket_results,
        "counterfactuals": counterfactuals,
    }


# ---------------------------------------------------------------------------
# H6: Missing Minimum Edge
# ---------------------------------------------------------------------------


def analyze_edge_distribution(agent_bets):
    """
    Analyze the distribution of share prices (proxy for market odds at entry)
    and compute counterfactuals for minimum edge requirements.
    """
    all_resolved = []
    for bets in agent_bets.values():
        for b in bets:
            if b["is_resolved"]:
                all_resolved.append(b)

    prices = [b["share_price"] for b in all_resolved if b["share_price"] > 0]
    total_pnl = sum(b["pnl"] for b in all_resolved)

    # Thin edge zone analysis
    thin_edge_zones = [0.03, 0.05, 0.10]
    counterfactuals = {}
    for edge in thin_edge_zones:
        # Remove bets where share_price is close to 0.50 (thin edge)
        filtered = [b for b in all_resolved if abs(b["share_price"] - 0.50) >= edge]
        cf_pnl = sum(b["pnl"] for b in filtered)
        removed = len(all_resolved) - len(filtered)
        counterfactuals[f"min_edge_{edge:.2f}"] = {
            "pnl": round(cf_pnl, 2),
            "count": len(filtered),
            "removed": removed,
        }

    # Distribution stats
    thin_bets = [b for b in all_resolved if 0.45 <= b["share_price"] <= 0.55]
    thin_pnl = sum(b["pnl"] for b in thin_bets)

    return {
        "total_bets": len(all_resolved),
        "total_pnl": round(total_pnl, 2),
        "mean_price": round(statistics.mean(prices), 4) if prices else None,
        "median_price": round(statistics.median(prices), 4) if prices else None,
        "thin_edge_bets": len(thin_bets),
        "thin_edge_pct": round(len(thin_bets) / len(all_resolved) * 100, 1) if all_resolved else 0,
        "thin_edge_pnl": round(thin_pnl, 2),
        "counterfactuals": counterfactuals,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_report(store_results, kelly_results, quarantine_results,
                 longshot_results, threshold_results, edge_results):
    w = 74
    print("\n" + "=" * w)
    print("  Deep Persistence Mechanism Analysis")
    print("=" * w)

    # --- H1: Accuracy Store ---
    print("\n--- H1: ACCURACY STORE FEEDBACK LOOP ---")
    if store_results:
        lock_ins = [r["lock_in_round"] for r in store_results.values()]
        early_accs = [r["early_accuracy"] for r in store_results.values()]
        pnls = [r["total_pnl"] for r in store_results.values()]

        print(f"  Agents simulated: {len(store_results)}")
        print(f"  Store lock-in round: mean={statistics.mean(lock_ins):.0f}, "
              f"median={statistics.median(lock_ins):.0f}, "
              f"range=[{min(lock_ins)}-{max(lock_ins)}]")

        rho = spearman(early_accs, pnls)
        print(f"  Early luck (first 10 bets accuracy) vs final PnL: rho={rho}")

        # Final best tool distribution
        final_tools = defaultdict(int)
        for r in store_results.values():
            if r["final_best_tool"]:
                final_tools[r["final_best_tool"]] += 1
        print("\n  Final best_tool distribution across agents:")
        for tool, count in sorted(final_tools.items(), key=lambda x: -x[1]):
            print(f"    {tool}: {count} agents")

        # Top 5 vs bottom 5 by PnL
        sorted_agents = sorted(store_results.items(), key=lambda x: x[1]["total_pnl"])
        bottom5 = sorted_agents[:5]
        top5 = sorted_agents[-5:]
        print("\n  Top 5 agents by PnL:")
        for addr, r in reversed(top5):
            print(f"    {addr[:14]}... | PnL=${r['total_pnl']:>7.2f} | "
                  f"best_tool={r['final_best_tool']} | early_acc={r['early_accuracy']}% | "
                  f"lock_in_round={r['lock_in_round']}")
        print("  Bottom 5 agents by PnL:")
        for addr, r in bottom5:
            print(f"    {addr[:14]}... | PnL=${r['total_pnl']:>7.2f} | "
                  f"best_tool={r['final_best_tool']} | early_acc={r['early_accuracy']}% | "
                  f"lock_in_round={r['lock_in_round']}")

        if rho and rho > 0.2:
            print("  >> FINDING: Early luck DOES predict lifetime PnL. The feedback loop is real.")
        elif rho and rho < -0.1:
            print("  >> FINDING: Early luck has INVERSE relationship with PnL — mean reversion.")
        else:
            print("  >> FINDING: Early luck has weak relationship with PnL.")
    else:
        print("  Not enough data.")

    # --- H2: Kelly Amplification ---
    print("\n--- H2: KELLY DYNAMIC FRACTION AMPLIFICATION ---")
    if kelly_results and kelly_results["agent_fractions"]:
        fracs = kelly_results["agent_fractions"]
        frac_vals = [f["avg_fraction"] for f in fracs.values()]
        frac_pnls = [f["pnl"] for f in fracs.values()]

        print(f"  Dynamic fraction range: {min(frac_vals):.3f} – {max(frac_vals):.3f} "
              f"(static baseline: {STATIC_KELLY_FRACTION})")
        print(f"  Mean: {statistics.mean(frac_vals):.3f}, stdev: {statistics.stdev(frac_vals):.3f}")
        rho = spearman(frac_vals, frac_pnls)
        print(f"  Dynamic fraction vs PnL: rho={rho}")

        # Counterfactual
        cf = kelly_results["counterfactual_pnl"]
        actual_total = sum(f["pnl"] for f in fracs.values())
        cf_total = sum(cf.values())
        print(f"\n  Counterfactual (static {STATIC_KELLY_FRACTION}x for all):")
        print(f"    Actual fleet PnL:         ${actual_total:.2f}")
        print(f"    Counterfactual fleet PnL: ${cf_total:.2f}")
        print(f"    Difference:               ${cf_total - actual_total:.2f}")

        if rho and abs(rho) > 0.2:
            print("  >> FINDING: Dynamic fraction significantly correlates with PnL — it amplifies divergence.")
        else:
            print("  >> FINDING: Dynamic fraction has limited impact on PnL divergence.")

    # --- H3: Quarantine ---
    print("\n--- H3: TOOL QUARANTINE SIGNALS ---")
    if quarantine_results:
        sf_early_pcts = [s["sf_early_pct"] for s in quarantine_results.values()]
        qr_pnls = [s["total_pnl"] for s in quarantine_results.values()]
        rho = spearman(sf_early_pcts, qr_pnls)
        print(f"  Early superforcaster usage (first 20 bets) vs PnL: rho={rho}")

        # Quarantine gap counts
        total_gaps = sum(
            sum(g.values()) for s in quarantine_results.values()
            for g in [s["quarantine_gaps"]]
        )
        agents_with_gaps = sum(
            1 for s in quarantine_results.values()
            if any(v > 0 for v in s["quarantine_gaps"].values())
        )
        print(f"  Agents with quarantine-length gaps: {agents_with_gaps}/{len(quarantine_results)}")
        print(f"  Total quarantine-like gaps detected: {total_gaps}")

        if rho and rho > 0.2:
            print("  >> FINDING: Early superforcaster access correlates with better PnL.")
        else:
            print("  >> FINDING: Early superforcaster usage doesn't strongly predict PnL.")

    # --- H4: Longshot by Tool ---
    print("\n--- H4: LONGSHOT EXPOSURE BY TOOL ---")
    if longshot_results:
        sorted_tools = sorted(longshot_results.items(), key=lambda x: -x[1]["total"])
        print(f"  {'Tool':<35} | {'Total':>5} | {'LS%':>5} | {'LSAcc':>5} | {'Acc':>5} | {'LS PnL':>8} | {'PnL':>8}")
        print("  " + "-" * 85)
        for tool, s in sorted_tools:
            ls_acc = f"{s['longshot_accuracy_pct']:.0f}%" if s["longshot_accuracy_pct"] is not None else "N/A"
            print(
                f"  {tool:<35} | {s['total']:>5} | {s['longshot_pct']:>4.1f}% | "
                f"{ls_acc:>5} | {s['accuracy_pct']:>4.1f}% | ${s['longshot_pnl']:>7.2f} | ${s['pnl']:>7.2f}"
            )

        # Check if PRR has more longshots than SF
        prr = longshot_results.get("prediction-request-reasoning", {})
        sf = longshot_results.get("superforcaster", {})
        if prr and sf:
            print(f"\n  prediction-request-reasoning longshot rate: {prr.get('longshot_pct', 0)}%")
            print(f"  superforcaster longshot rate:               {sf.get('longshot_pct', 0)}%")
            if prr.get("longshot_pct", 0) > sf.get("longshot_pct", 0) + 2:
                print("  >> FINDING: PRR produces more longshot predictions than superforcaster.")
            else:
                print("  >> FINDING: Longshot rates are similar across tools — not a tool-specific effect.")

    # --- H5: Threshold Gap ---
    print("\n--- H5: THE 0.80 PRICE THRESHOLD GAP ---")
    if threshold_results:
        print(f"  Total fleet bets: {threshold_results['total_bets']}, PnL: ${threshold_results['total_pnl']:.2f}")
        print("\n  Fine-grained price buckets:")
        for label, s in threshold_results["buckets"].items():
            if s["count"] == 0:
                continue
            sign = "+" if s["pnl"] >= 0 else ""
            print(f"    {label:<10} | {s['count']:>5} bets | {s['accuracy_pct']:>5.1f}% acc | "
                  f"{sign}${s['pnl']:>7.2f} | ROI: {s['roi_pct']:>+6.1f}%")

        print("\n  Counterfactual thresholds (filter both sides):")
        for label, cf in threshold_results["counterfactuals"].items():
            diff = cf["pnl"] - threshold_results["total_pnl"]
            sign = "+" if diff >= 0 else ""
            print(f"    threshold={label}: PnL=${cf['pnl']:.2f} ({sign}${diff:.2f}), "
                  f"removed {cf['removed']} bets")

    # --- H6: Edge ---
    print("\n--- H6: MISSING MINIMUM EDGE ---")
    if edge_results:
        print(f"  Mean share price at entry: {edge_results['mean_price']}")
        print(f"  Median share price:        {edge_results['median_price']}")
        print(f"  Thin-edge bets (0.45-0.55): {edge_results['thin_edge_bets']} "
              f"({edge_results['thin_edge_pct']}%), PnL: ${edge_results['thin_edge_pnl']:.2f}")

        print("\n  Counterfactual minimum edge requirements:")
        for label, cf in edge_results["counterfactuals"].items():
            diff = cf["pnl"] - edge_results["total_pnl"]
            sign = "+" if diff >= 0 else ""
            print(f"    {label}: PnL=${cf['pnl']:.2f} ({sign}${diff:.2f}), removed {cf['removed']} bets")

    # --- Synthesis ---
    print("\n--- SYNTHESIS ---")
    print("  Ranked mechanisms behind persistent PnL divergence:\n")

    findings = []

    if store_results:
        early_accs = [r["early_accuracy"] for r in store_results.values()]
        pnls = [r["total_pnl"] for r in store_results.values()]
        rho_h1 = spearman(early_accs, pnls)
        findings.append(("H1: Accuracy store feedback loop",
                         f"rho(early_luck, PnL)={rho_h1}",
                         abs(rho_h1) if rho_h1 else 0))

    if threshold_results:
        best_cf = max(
            threshold_results["counterfactuals"].items(),
            key=lambda x: x[1]["pnl"],
        )
        savings = best_cf[1]["pnl"] - threshold_results["total_pnl"]
        findings.append(("H5: Price threshold gap",
                         f"best threshold {best_cf[0]} saves ${savings:.2f}",
                         abs(savings) / 100))

    if longshot_results:
        prr = longshot_results.get("prediction-request-reasoning", {})
        sf = longshot_results.get("superforcaster", {})
        ls_diff = abs(prr.get("longshot_pct", 0) - sf.get("longshot_pct", 0))
        findings.append(("H4: Tool-specific longshot exposure",
                         f"PRR vs SF longshot gap: {ls_diff:.1f}pp",
                         ls_diff / 10))

    if kelly_results and kelly_results["agent_fractions"]:
        fracs = kelly_results["agent_fractions"]
        frac_vals = [f["avg_fraction"] for f in fracs.values()]
        frac_pnls = [f["pnl"] for f in fracs.values()]
        rho_h2 = spearman(frac_vals, frac_pnls)
        findings.append(("H2: Kelly dynamic fraction amplification",
                         f"rho(fraction, PnL)={rho_h2}",
                         abs(rho_h2) if rho_h2 else 0))

    if edge_results:
        thin_impact = abs(edge_results["thin_edge_pnl"])
        findings.append(("H6: Missing minimum edge",
                         f"thin-edge PnL impact: ${thin_impact:.2f}",
                         thin_impact / 100))

    findings.sort(key=lambda x: x[2], reverse=True)
    for i, (name, evidence, _) in enumerate(findings, 1):
        print(f"  {i}. {name}")
        print(f"     Evidence: {evidence}")

    print("\n" + "=" * w)


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def plot_charts(store_results, longshot_results, threshold_results, edge_results):
    if not _HAS_MATPLOTLIB:
        print("\nmatplotlib not available. Install: pip install matplotlib")
        return

    fig, axes = plt.subplots(2, 3, figsize=(18, 10))

    # Chart 1: Early luck vs final PnL scatter
    ax = axes[0][0]
    if store_results:
        x = [r["early_accuracy"] for r in store_results.values()]
        y = [r["total_pnl"] for r in store_results.values()]
        ax.scatter(x, y, c="steelblue", s=30, alpha=0.7)
        ax.set_xlabel("Early Accuracy (first 10 bets) %")
        ax.set_ylabel("Total PnL (USDC)")
        ax.set_title("H1: Early Luck vs Final PnL")
        ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")

    # Chart 2: Lock-in round distribution
    ax = axes[0][1]
    if store_results:
        lock_ins = [r["lock_in_round"] for r in store_results.values()]
        ax.hist(lock_ins, bins=20, color="steelblue", edgecolor="white", alpha=0.8)
        ax.set_xlabel("Lock-in Round")
        ax.set_ylabel("Count")
        ax.set_title("H1: When Does best_tool Lock In?")

    # Chart 3: Tool longshot rates
    ax = axes[0][2]
    if longshot_results:
        tools = sorted(
            [(t, s) for t, s in longshot_results.items() if s["total"] >= 10 and t != "unknown"],
            key=lambda x: -x[1]["total"],
        )[:8]
        names = [t[:20] for t, _ in tools]
        ls_rates = [s["longshot_pct"] for _, s in tools]
        accs = [s["accuracy_pct"] for _, s in tools]
        x_pos = range(len(names))
        bars1 = ax.bar([p - 0.2 for p in x_pos], ls_rates, 0.4, label="Longshot %", color="red", alpha=0.7)
        bars2 = ax.bar([p + 0.2 for p in x_pos], accs, 0.4, label="Accuracy %", color="green", alpha=0.7)
        ax.set_xticks(list(x_pos))
        ax.set_xticklabels(names, rotation=45, ha="right", fontsize=7)
        ax.set_ylabel("%")
        ax.set_title("H4: Tool Longshot Rate vs Accuracy")
        ax.legend(fontsize=8)

    # Chart 4: Price bucket PnL
    ax = axes[1][0]
    if threshold_results:
        buckets = threshold_results["buckets"]
        labels = list(buckets.keys())
        pnls = [buckets[l]["pnl"] for l in labels]
        colors = ["red" if p < 0 else "green" for p in pnls]
        ax.bar(range(len(labels)), pnls, color=colors, alpha=0.8)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=6)
        ax.set_ylabel("PnL (USDC)")
        ax.set_title("H5: PnL by Price Bucket")
        ax.axhline(0, color="gray", linewidth=0.5)

    # Chart 5: Share price distribution
    ax = axes[1][1]
    if edge_results:
        all_prices = []
        # We need to reconstruct this from threshold buckets
        for label, s in threshold_results["buckets"].items():
            lo = float(label.split("-")[0])
            mid = lo + 0.025
            all_prices.extend([mid] * s["count"])
        ax.hist(all_prices, bins=20, color="steelblue", edgecolor="white", alpha=0.8)
        ax.axvline(0.50, color="red", linewidth=1, linestyle="--", label="50/50 line")
        ax.set_xlabel("Share Price at Entry")
        ax.set_ylabel("Count")
        ax.set_title("H6: Entry Price Distribution")
        ax.legend(fontsize=8)

    # Chart 6: Weighted accuracy trajectory (top vs bottom 3 agents)
    ax = axes[1][2]
    if store_results:
        sorted_agents = sorted(store_results.items(), key=lambda x: x[1]["total_pnl"])
        bottom3 = sorted_agents[:3]
        top3 = sorted_agents[-3:]
        for addr, r in top3:
            if r["weighted_acc_history"]:
                ax.plot(r["weighted_acc_history"], color="green", alpha=0.5, linewidth=1)
        for addr, r in bottom3:
            if r["weighted_acc_history"]:
                ax.plot(r["weighted_acc_history"], color="red", alpha=0.5, linewidth=1)
        ax.set_xlabel("Bet Number")
        ax.set_ylabel("Weighted Accuracy of Best Tool")
        ax.set_title("H1: Store Trajectory (green=top3, red=bottom3)")

    fig.suptitle("Deep Persistence Mechanism Analysis", fontsize=14, fontweight="bold")
    fig.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Deep persistence mechanism analysis for PolyStrat fleet."
    )
    parser.add_argument("--min-bets", type=int, default=5)
    parser.add_argument("--json", dest="json_output", action="store_true")
    parser.add_argument("--no-charts", action="store_true")
    parser.add_argument("--no-tools", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("  Deep Persistence Mechanism Analysis")
    print("=" * 60)

    # [1/6] Fetch agents
    print("\n[1/6] Fetching agents...")
    all_addresses = get_all_polystrat_agents()
    print(f"  Found {len(all_addresses)} agents.")

    # [2/6] Fetch all bets
    print("\n[2/6] Fetching bets...")
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

    # [3/6] Tool enrichment
    agent_tools = {}
    if not args.no_tools:
        print("\n[3/6] Fetching mech tool data...")
        for i, addr in enumerate(agent_bets.keys(), 1):
            if i % 20 == 0 or i == len(agent_bets):
                print(f"  Agent {i}/{len(agent_bets)}...")
            try:
                mech_reqs = fetch_mech_requests(addr)
            except Exception:
                mech_reqs = []
            tools = {}
            for bet in agent_bets[addr]:
                tools[bet["bet_id"]] = match_bet_to_tool(
                    bet["title"], bet["timestamp"], mech_reqs
                )
            agent_tools[addr] = tools
    else:
        print("\n[3/6] Skipping tool enrichment.")

    # [4/6] Run hypotheses
    print("\n[4/6] Running hypothesis tests...")

    print("  H1: Simulating accuracy stores...")
    store_results = simulate_accuracy_store(agent_bets, agent_tools) if agent_tools else {}

    print("  H2: Analyzing Kelly amplification...")
    kelly_results = analyze_kelly_amplification(agent_bets, agent_tools, store_results) if store_results else {}

    print("  H3: Scanning quarantine signals...")
    quarantine_results = analyze_quarantine_signals(agent_bets, agent_tools) if agent_tools else {}

    print("  H4: Analyzing longshot exposure by tool...")
    longshot_results = analyze_longshot_by_tool(agent_bets, agent_tools) if agent_tools else {}

    print("  H5: Analyzing price threshold gap...")
    threshold_results = analyze_threshold_gap(agent_bets)

    print("  H6: Analyzing edge distribution...")
    edge_results = analyze_edge_distribution(agent_bets)

    # [5/6] Output
    print("\n[5/6] Generating report...")
    if args.json_output:
        output = {
            "h1_accuracy_store": {
                a: {k: v for k, v in r.items() if k not in ("weighted_acc_history", "best_tool_history")}
                for a, r in store_results.items()
            },
            "h2_kelly_amplification": {
                k: v for k, v in kelly_results.items() if k != "agent_fractions"
            } if kelly_results else {},
            "h3_quarantine": quarantine_results,
            "h4_longshot_by_tool": longshot_results,
            "h5_threshold_gap": threshold_results,
            "h6_edge_distribution": edge_results,
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print_report(
            store_results, kelly_results, quarantine_results,
            longshot_results, threshold_results, edge_results,
        )

    # [6/6] Charts
    if not args.no_charts and not args.json_output:
        print("\n[6/6] Plotting charts...")
        plot_charts(store_results, longshot_results, threshold_results, edge_results)
    else:
        print("\n[6/6] Skipping charts.")


if __name__ == "__main__":
    main()
