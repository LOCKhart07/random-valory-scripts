"""
PolyStrat Fleet Divergence Analysis.

Investigates why identical PolyStrat agents show persistent performance
divergence when they share the same tools and logic. Tests hypotheses:
market selection, tool assignment, entry pricing, timing, and convergence.

Usage:
    python polymarket/analyze_divergence.py
    python polymarket/analyze_divergence.py --focus 0x33d20338f1700eda034ea2543933f94a2177ae4c
    python polymarket/analyze_divergence.py --min-bets 10
    python polymarket/analyze_divergence.py --json
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
    import matplotlib.ticker as mticker

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
PERCENTAGE_FACTOR = 100.0
QUESTION_DATA_SEPARATOR = "\u241f"

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3

# Lookback for mech requests (70 days, matching tool_accuracy_polymarket.py)
MECH_LOOKBACK_SECONDS = 70 * 24 * 60 * 60


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
            print(f"  Registry fetch failed ({exc}), falling back...")

    query = """
{
  traderAgents(first: 1000, orderBy: totalBets, orderDirection: desc) {
    id
    totalBets
  }
}
"""
    response = call_subgraph(POLYMARKET_BETS_SUBGRAPH_URL, query)
    return [a["id"].lower() for a in response.get("data", {}).get("traderAgents", [])]


def fetch_agent_bets(safe_address):
    """Fetch all bets for an agent with question IDs, amounts, shares, timestamps."""
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


def fetch_trader_agent(safe_address):
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
    response = call_subgraph(
        POLYMARKET_BETS_SUBGRAPH_URL, query, {"id": safe_address}
    )
    return response.get("data", {}).get("traderAgent")


GET_MECH_SENDER_QUERY = """
query MechSender($id: ID!, $timestamp_gt: Int!, $skip: Int, $first: Int) {
    sender(id: $id) {
        requests(first: $first, skip: $skip, where: { blockTimestamp_gt: $timestamp_gt }) {
            blockTimestamp
            parsedRequest {
                questionTitle
                tool
            }
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
# Tool matching (from tool_accuracy_polymarket.py patterns)
# ---------------------------------------------------------------------------


def extract_question_title(question):
    if not question:
        return ""
    return question.split(QUESTION_DATA_SEPARATOR)[0]


def match_bet_to_tool(bet_title, bet_ts, mech_requests):
    """Match a bet to its mech tool by question title prefix."""
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
# Process bets into structured records
# ---------------------------------------------------------------------------


def process_bets(bets):
    """Convert raw subgraph bets into structured records."""
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
        winning_idx = None
        if resolution is not None:
            wi = resolution.get("winningIndex")
            if wi is not None and int(wi) >= 0:
                is_resolved = True
                winning_idx = int(wi)
                is_win = outcome_idx == winning_idx

        records.append({
            "bet_id": bet.get("id", ""),
            "question_id": question_id,
            "title": title,
            "outcome_index": outcome_idx,
            "amount_raw": amount,
            "amount_usdc": amount / USDC_DECIMALS_DIVISOR,
            "shares_raw": shares,
            "shares_usdc": shares / USDC_DECIMALS_DIVISOR,
            "share_price": share_price,
            "timestamp": int(bet.get("blockTimestamp", 0)),
            "is_resolved": is_resolved,
            "is_win": is_win,
            "winning_index": winning_idx,
        })
    return records


# ---------------------------------------------------------------------------
# Analysis 1: Market Overlap
# ---------------------------------------------------------------------------


def analyze_market_overlap(agent_bets):
    """
    Compute how much agents overlap in market selection.
    agent_bets: dict of {address: [bet_records]}
    Returns market participation data and overlap stats.
    """
    # question_id -> set of agents
    market_agents = defaultdict(set)
    # question_id -> {agent: [bets]}
    market_agent_bets = defaultdict(lambda: defaultdict(list))

    for addr, bets in agent_bets.items():
        for bet in bets:
            qid = bet["question_id"]
            if qid:
                market_agents[qid].add(addr)
                market_agent_bets[qid][addr].append(bet)

    total_markets = len(market_agents)
    shared_markets = {qid for qid, agents in market_agents.items() if len(agents) > 1}
    unique_markets = {qid for qid, agents in market_agents.items() if len(agents) == 1}

    # Per-agent: fraction of their bets on shared vs unique markets
    agent_overlap_stats = {}
    for addr, bets in agent_bets.items():
        resolved = [b for b in bets if b["is_resolved"]]
        if not resolved:
            continue
        on_shared = [b for b in resolved if b["question_id"] in shared_markets]
        on_unique = [b for b in resolved if b["question_id"] in unique_markets]
        shared_acc = (
            sum(1 for b in on_shared if b["is_win"]) / len(on_shared) * 100
            if on_shared else None
        )
        unique_acc = (
            sum(1 for b in on_unique if b["is_win"]) / len(on_unique) * 100
            if on_unique else None
        )
        agent_overlap_stats[addr] = {
            "total_resolved": len(resolved),
            "on_shared": len(on_shared),
            "on_unique": len(on_unique),
            "pct_shared": len(on_shared) / len(resolved) * 100 if resolved else 0,
            "shared_accuracy": round(shared_acc, 1) if shared_acc is not None else None,
            "unique_accuracy": round(unique_acc, 1) if unique_acc is not None else None,
        }

    # Pairwise Jaccard similarity (sample if too many agents)
    addrs = list(agent_bets.keys())
    agent_markets = {
        addr: {b["question_id"] for b in bets if b["question_id"]}
        for addr, bets in agent_bets.items()
    }
    pairwise_jaccards = []
    for i in range(len(addrs)):
        for j in range(i + 1, len(addrs)):
            s1 = agent_markets[addrs[i]]
            s2 = agent_markets[addrs[j]]
            if s1 or s2:
                jaccard = len(s1 & s2) / len(s1 | s2) if (s1 | s2) else 0
                pairwise_jaccards.append(jaccard)

    return {
        "total_markets": total_markets,
        "shared_markets": len(shared_markets),
        "unique_markets": len(unique_markets),
        "pct_shared": round(len(shared_markets) / total_markets * 100, 1) if total_markets else 0,
        "avg_jaccard": round(statistics.mean(pairwise_jaccards), 4) if pairwise_jaccards else 0,
        "median_jaccard": round(statistics.median(pairwise_jaccards), 4) if pairwise_jaccards else 0,
        "agent_overlap_stats": agent_overlap_stats,
        "market_agents": market_agents,
        "market_agent_bets": market_agent_bets,
    }


# ---------------------------------------------------------------------------
# Analysis 2: Same-Market Comparison
# ---------------------------------------------------------------------------


def analyze_same_market(market_agent_bets, market_agents):
    """
    For markets with 2+ agents, compare outcomes, prices, and amounts.
    """
    outcome_agreements = []
    price_spreads = []
    amount_spreads = []
    per_market = []

    shared_qids = [qid for qid, agents in market_agents.items() if len(agents) > 1]

    for qid in shared_qids:
        agent_data = market_agent_bets[qid]
        outcomes = {}
        prices = {}
        amounts = {}
        timestamps = {}

        for addr, bets in agent_data.items():
            # Take the first bet per agent per market
            bet = bets[0]
            outcomes[addr] = bet["outcome_index"]
            prices[addr] = bet["share_price"]
            amounts[addr] = bet["amount_usdc"]
            timestamps[addr] = bet["timestamp"]

        if len(outcomes) < 2:
            continue

        # Outcome agreement: did all agents pick the same outcome?
        unique_outcomes = set(outcomes.values())
        all_agree = len(unique_outcomes) == 1
        outcome_agreements.append(all_agree)

        # Price spread
        price_vals = list(prices.values())
        if len(price_vals) >= 2:
            price_spread = max(price_vals) - min(price_vals)
            price_spreads.append(price_spread)

        # Amount spread
        amount_vals = list(amounts.values())
        if len(amount_vals) >= 2 and min(amount_vals) > 0:
            amount_ratio = max(amount_vals) / min(amount_vals)
            amount_spreads.append(amount_ratio)

        per_market.append({
            "question_id": qid,
            "n_agents": len(outcomes),
            "all_agree": all_agree,
            "outcomes": outcomes,
            "prices": prices,
            "amounts": amounts,
            "timestamps": timestamps,
        })

    agreement_rate = (
        sum(outcome_agreements) / len(outcome_agreements) * 100
        if outcome_agreements else None
    )

    return {
        "shared_markets_analyzed": len(per_market),
        "outcome_agreement_rate_pct": round(agreement_rate, 1) if agreement_rate is not None else None,
        "avg_price_spread": round(statistics.mean(price_spreads), 4) if price_spreads else None,
        "median_price_spread": round(statistics.median(price_spreads), 4) if price_spreads else None,
        "avg_amount_ratio": round(statistics.mean(amount_spreads), 2) if amount_spreads else None,
        "per_market": per_market,
    }


# ---------------------------------------------------------------------------
# Analysis 3: Tool Assignment
# ---------------------------------------------------------------------------


def analyze_tool_assignment(agent_bets, agent_tools):
    """
    Check if tool assignment varies across agents and correlates with performance.
    agent_tools: {address: {bet_id: tool_name}}
    """
    # Per-agent tool distribution
    agent_tool_dist = {}
    agent_tool_accuracy = {}

    for addr, bets in agent_bets.items():
        tools = agent_tools.get(addr, {})
        tool_counts = defaultdict(int)
        tool_wins = defaultdict(int)
        tool_total = defaultdict(int)

        for bet in bets:
            tool = tools.get(bet["bet_id"], "unknown")
            tool_counts[tool] += 1
            if bet["is_resolved"]:
                tool_total[tool] += 1
                if bet["is_win"]:
                    tool_wins[tool] += 1

        agent_tool_dist[addr] = dict(tool_counts)
        tool_acc = {}
        for tool, total in tool_total.items():
            if total > 0:
                tool_acc[tool] = round(tool_wins[tool] / total * 100, 1)
        agent_tool_accuracy[addr] = tool_acc

    # Fleet-wide tool accuracy
    fleet_tool_wins = defaultdict(int)
    fleet_tool_total = defaultdict(int)
    for addr, bets in agent_bets.items():
        tools = agent_tools.get(addr, {})
        for bet in bets:
            if bet["is_resolved"]:
                tool = tools.get(bet["bet_id"], "unknown")
                fleet_tool_total[tool] += 1
                if bet["is_win"]:
                    fleet_tool_wins[tool] += 1

    fleet_tool_accuracy = {}
    for tool, total in fleet_tool_total.items():
        fleet_tool_accuracy[tool] = {
            "total": total,
            "wins": fleet_tool_wins[tool],
            "accuracy_pct": round(fleet_tool_wins[tool] / total * 100, 1) if total > 0 else 0,
        }

    return {
        "agent_tool_dist": agent_tool_dist,
        "agent_tool_accuracy": agent_tool_accuracy,
        "fleet_tool_accuracy": fleet_tool_accuracy,
    }


# ---------------------------------------------------------------------------
# Analysis 4: Entry Price / Timing
# ---------------------------------------------------------------------------


def analyze_entry_pricing(agent_bets, market_agent_bets, market_agents):
    """
    For shared markets, check if entry timing correlates with share price and outcome.
    """
    early_vs_late = {"early_wins": 0, "early_total": 0, "late_wins": 0, "late_total": 0}
    price_vs_outcome = []

    shared_qids = [qid for qid, agents in market_agents.items() if len(agents) >= 2]

    for qid in shared_qids:
        agent_data = market_agent_bets[qid]
        entries = []
        for addr, bets in agent_data.items():
            bet = bets[0]
            if bet["is_resolved"]:
                entries.append({
                    "addr": addr,
                    "timestamp": bet["timestamp"],
                    "share_price": bet["share_price"],
                    "is_win": bet["is_win"],
                    "amount_usdc": bet["amount_usdc"],
                })

        if len(entries) < 2:
            continue

        entries.sort(key=lambda x: x["timestamp"])
        median_ts = statistics.median([e["timestamp"] for e in entries])

        for e in entries:
            is_early = e["timestamp"] <= median_ts
            if is_early:
                early_vs_late["early_total"] += 1
                if e["is_win"]:
                    early_vs_late["early_wins"] += 1
            else:
                early_vs_late["late_total"] += 1
                if e["is_win"]:
                    early_vs_late["late_wins"] += 1

            price_vs_outcome.append({
                "share_price": e["share_price"],
                "is_win": e["is_win"],
            })

    early_acc = (
        round(early_vs_late["early_wins"] / early_vs_late["early_total"] * 100, 1)
        if early_vs_late["early_total"] > 0 else None
    )
    late_acc = (
        round(early_vs_late["late_wins"] / early_vs_late["late_total"] * 100, 1)
        if early_vs_late["late_total"] > 0 else None
    )

    # Per-agent average share price
    agent_avg_prices = {}
    for addr, bets in agent_bets.items():
        prices = [b["share_price"] for b in bets if b["share_price"] > 0]
        if prices:
            agent_avg_prices[addr] = round(statistics.mean(prices), 4)

    return {
        "early_entry_accuracy_pct": early_acc,
        "late_entry_accuracy_pct": late_acc,
        "early_entry_count": early_vs_late["early_total"],
        "late_entry_count": early_vs_late["late_total"],
        "agent_avg_prices": agent_avg_prices,
    }


# ---------------------------------------------------------------------------
# Analysis 5: Performance Convergence
# ---------------------------------------------------------------------------


def analyze_convergence(agent_bets, min_bets_per_window=3):
    """
    Split each agent's bet history into time windows and track accuracy rank stability.
    If ranks are sticky, divergence is structural. If they shuffle, it's noise.
    """
    # Find global time range
    all_timestamps = []
    for bets in agent_bets.values():
        for b in bets:
            if b["is_resolved"] and b["timestamp"]:
                all_timestamps.append(b["timestamp"])

    if not all_timestamps:
        return {"windows": 0, "rank_autocorrelation": None}

    min_ts = min(all_timestamps)
    max_ts = max(all_timestamps)
    span = max_ts - min_ts

    # Split into 4 windows
    n_windows = 4
    window_size = span / n_windows if n_windows > 0 else span

    # Per window, compute accuracy per agent
    window_accuracies = []  # list of {addr: accuracy}
    window_labels = []

    for w in range(n_windows):
        w_start = min_ts + w * window_size
        w_end = min_ts + (w + 1) * window_size
        window_labels.append(
            datetime.fromtimestamp(w_start, tz=timezone.utc).strftime("%Y-%m-%d")
        )

        agent_acc = {}
        for addr, bets in agent_bets.items():
            w_bets = [
                b for b in bets
                if b["is_resolved"] and w_start <= b["timestamp"] < w_end
            ]
            if len(w_bets) >= min_bets_per_window:
                wins = sum(1 for b in w_bets if b["is_win"])
                agent_acc[addr] = wins / len(w_bets) * 100
        window_accuracies.append(agent_acc)

    # Compute rank stability: for agents present in consecutive windows,
    # compute Spearman rank correlation of accuracy
    rank_correlations = []
    for i in range(len(window_accuracies) - 1):
        w1 = window_accuracies[i]
        w2 = window_accuracies[i + 1]
        common = set(w1.keys()) & set(w2.keys())
        if len(common) < 3:
            continue

        addrs = sorted(common)
        vals1 = [w1[a] for a in addrs]
        vals2 = [w2[a] for a in addrs]

        # Spearman: rank correlation
        def _rank(values):
            sorted_vals = sorted(enumerate(values), key=lambda x: x[1])
            ranks = [0] * len(values)
            for rank, (idx, _) in enumerate(sorted_vals):
                ranks[idx] = rank
            return ranks

        r1 = _rank(vals1)
        r2 = _rank(vals2)
        n = len(r1)
        if n < 3:
            continue

        # Spearman rho = 1 - 6*sum(d^2) / (n*(n^2-1))
        d_sq = sum((a - b) ** 2 for a, b in zip(r1, r2))
        denom = n * (n ** 2 - 1)
        rho = 1 - (6 * d_sq / denom) if denom > 0 else 0
        rank_correlations.append(rho)

    avg_rho = statistics.mean(rank_correlations) if rank_correlations else None

    return {
        "windows": n_windows,
        "window_labels": window_labels,
        "window_accuracies": window_accuracies,
        "rank_correlations": rank_correlations,
        "avg_rank_correlation": round(avg_rho, 3) if avg_rho is not None else None,
    }


# ---------------------------------------------------------------------------
# Focus agent summary
# ---------------------------------------------------------------------------


def focus_agent_summary(addr, agent_bets, agent_tools, overlap_stats,
                        all_agents_stats):
    """Summarize where the focus agent falls relative to the fleet."""
    bets = agent_bets.get(addr, [])
    resolved = [b for b in bets if b["is_resolved"]]
    if not resolved:
        return None

    wins = sum(1 for b in resolved if b["is_win"])
    accuracy = wins / len(resolved) * 100

    # PnL from individual bets
    pnl = 0.0
    for b in resolved:
        if b["is_win"]:
            pnl += b["shares_usdc"] - b["amount_usdc"]
        else:
            pnl -= b["amount_usdc"]

    # Fleet percentiles
    all_accuracies = sorted([
        s["accuracy"] for s in all_agents_stats.values() if s["accuracy"] is not None
    ])
    all_pnls = sorted([s["pnl"] for s in all_agents_stats.values()])
    all_avg_prices = sorted([
        s["avg_share_price"] for s in all_agents_stats.values()
        if s["avg_share_price"] is not None
    ])

    def percentile_rank(val, sorted_list):
        if not sorted_list:
            return None
        below = sum(1 for v in sorted_list if v < val)
        return round(below / len(sorted_list) * 100, 1)

    avg_price = statistics.mean([b["share_price"] for b in bets if b["share_price"] > 0]) if bets else None

    overlap = overlap_stats.get("agent_overlap_stats", {}).get(addr, {})

    tools = agent_tools.get(addr, {})
    tool_counts = defaultdict(int)
    for bet in bets:
        tool_counts[tools.get(bet["bet_id"], "unknown")] += 1

    return {
        "address": addr,
        "total_bets": len(bets),
        "resolved": len(resolved),
        "wins": wins,
        "losses": len(resolved) - wins,
        "accuracy_pct": round(accuracy, 1),
        "accuracy_percentile": percentile_rank(accuracy, all_accuracies),
        "pnl_usdc": round(pnl, 2),
        "pnl_percentile": percentile_rank(pnl, all_pnls),
        "avg_share_price": round(avg_price, 4) if avg_price else None,
        "price_percentile": percentile_rank(avg_price, all_avg_prices) if avg_price else None,
        "pct_on_shared_markets": round(overlap.get("pct_shared", 0), 1),
        "shared_market_accuracy": overlap.get("shared_accuracy"),
        "unique_market_accuracy": overlap.get("unique_accuracy"),
        "tool_distribution": dict(tool_counts),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_report(overlap, same_market, tool_stats, pricing, convergence,
                 focus, focus_addr, all_agents_stats, min_bets):
    w = 74
    print("\n" + "=" * w)
    print("  PolyStrat Fleet Divergence Analysis")
    print("=" * w)

    # --- 1. Market Overlap ---
    print("\n--- 1. MARKET OVERLAP ---")
    print(f"  Total unique markets bet on:     {overlap['total_markets']}")
    print(f"  Shared (2+ agents):              {overlap['shared_markets']} ({overlap['pct_shared']}%)")
    print(f"  Unique (1 agent only):           {overlap['unique_markets']}")
    print(f"  Avg pairwise Jaccard similarity: {overlap['avg_jaccard']:.4f}")
    print(f"  Median pairwise Jaccard:         {overlap['median_jaccard']:.4f}")

    if overlap["avg_jaccard"] < 0.1:
        print("  >> FINDING: Very low market overlap — agents are betting on mostly different markets.")
        print("     This alone can explain persistent divergence.")
    elif overlap["avg_jaccard"] < 0.3:
        print("  >> FINDING: Moderate market overlap — some shared markets but significant divergence in selection.")
    else:
        print("  >> FINDING: High market overlap — agents bet on similar markets.")

    # Per-agent shared vs unique accuracy
    ov_stats = overlap.get("agent_overlap_stats", {})
    shared_accs = [s["shared_accuracy"] for s in ov_stats.values() if s["shared_accuracy"] is not None]
    unique_accs = [s["unique_accuracy"] for s in ov_stats.values() if s["unique_accuracy"] is not None]
    if shared_accs and unique_accs:
        print(f"\n  Avg accuracy on shared markets:  {statistics.mean(shared_accs):.1f}%")
        print(f"  Avg accuracy on unique markets:  {statistics.mean(unique_accs):.1f}%")

    # --- 2. Same-Market Comparison ---
    print("\n--- 2. SAME-MARKET COMPARISON ---")
    if same_market["shared_markets_analyzed"] > 0:
        print(f"  Shared markets analyzed:         {same_market['shared_markets_analyzed']}")
        agr = same_market["outcome_agreement_rate_pct"]
        print(f"  Outcome agreement rate:          {agr:.1f}%" if agr is not None else "  Outcome agreement rate:          N/A")
        if same_market["avg_price_spread"] is not None:
            print(f"  Avg share price spread:          {same_market['avg_price_spread']:.4f}")
            print(f"  Median share price spread:       {same_market['median_price_spread']:.4f}")
        if same_market["avg_amount_ratio"] is not None:
            print(f"  Avg bet amount ratio (max/min):  {same_market['avg_amount_ratio']:.2f}x")

        if agr is not None and agr < 80:
            print("  >> FINDING: Agents often disagree on outcome for the same market.")
            print("     This means the tool/model produces different predictions for the same question.")
        elif agr is not None:
            print("  >> FINDING: Agents mostly agree on outcome — divergence comes from pricing/timing, not predictions.")
    else:
        print("  No shared markets to analyze.")

    # --- 3. Tool Assignment ---
    print("\n--- 3. TOOL ASSIGNMENT ---")
    fleet_tools = tool_stats["fleet_tool_accuracy"]
    if fleet_tools:
        sorted_tools = sorted(fleet_tools.items(), key=lambda x: x[1]["total"], reverse=True)
        col_t = max(len(t) for t, _ in sorted_tools)
        col_t = max(col_t, 4)
        print(f"  {'Tool':<{col_t}} | {'Total':>7} | {'Wins':>6} | {'Accuracy':>8}")
        print("  " + "-" * (col_t + 30))
        for tool, stats in sorted_tools:
            print(f"  {tool:<{col_t}} | {stats['total']:>7} | {stats['wins']:>6} | {stats['accuracy_pct']:>7.1f}%")

        # Check if tool distribution varies across agents
        agent_dists = tool_stats["agent_tool_dist"]
        tool_names = [t for t, s in fleet_tools.items() if t != "unknown" and s["total"] >= 5]
        if tool_names and len(agent_dists) >= 3:
            print("\n  Tool distribution variance across agents:")
            for tool in tool_names[:5]:
                agent_pcts = []
                for _, dist in agent_dists.items():
                    total = sum(dist.values())
                    if total >= 5:
                        pct = dist.get(tool, 0) / total * 100
                        agent_pcts.append(pct)
                if agent_pcts:
                    print(
                        f"    {tool}: mean={statistics.mean(agent_pcts):.1f}%, "
                        f"stdev={statistics.stdev(agent_pcts):.1f}%, "
                        f"range=[{min(agent_pcts):.0f}%–{max(agent_pcts):.0f}%]"
                    )
    else:
        print("  No tool data available (mech requests not found).")

    # --- 4. Entry Pricing ---
    print("\n--- 4. ENTRY PRICING & TIMING ---")
    if pricing["early_entry_accuracy_pct"] is not None:
        print(f"  Early entry accuracy (shared markets): {pricing['early_entry_accuracy_pct']}% ({pricing['early_entry_count']} bets)")
        print(f"  Late entry accuracy (shared markets):  {pricing['late_entry_accuracy_pct']}% ({pricing['late_entry_count']} bets)")
        diff = (pricing["early_entry_accuracy_pct"] or 0) - (pricing["late_entry_accuracy_pct"] or 0)
        if abs(diff) > 5:
            better = "early" if diff > 0 else "late"
            print(f"  >> FINDING: {better.capitalize()} entrants are {abs(diff):.1f}pp more accurate on shared markets.")
    else:
        print("  Not enough shared market data for timing analysis.")

    avg_prices = pricing.get("agent_avg_prices", {})
    if avg_prices:
        vals = list(avg_prices.values())
        print(f"\n  Agent avg share price: mean={statistics.mean(vals):.4f}, "
              f"stdev={statistics.stdev(vals):.4f}, "
              f"range=[{min(vals):.4f}–{max(vals):.4f}]")

    # --- 5. Convergence ---
    print("\n--- 5. PERFORMANCE CONVERGENCE ---")
    if convergence["avg_rank_correlation"] is not None:
        rho = convergence["avg_rank_correlation"]
        print(f"  Time windows analyzed:           {convergence['windows']}")
        print(f"  Window labels:                   {' → '.join(convergence['window_labels'])}")
        print(f"  Avg rank autocorrelation (rho):  {rho:.3f}")

        if rho > 0.5:
            print("  >> FINDING: STRONG rank persistence — same agents stay top/bottom across windows.")
            print("     Divergence is STRUCTURAL, not random noise.")
        elif rho > 0.2:
            print("  >> FINDING: MODERATE rank persistence — some stickiness in agent ranks.")
            print("     Partial structural effect + some noise.")
        elif rho > -0.1:
            print("  >> FINDING: WEAK rank persistence — ranks shuffle across windows.")
            print("     Divergence is mostly NOISE (regression to mean expected).")
        else:
            print("  >> FINDING: NEGATIVE rank correlation — past losers tend to become winners and vice versa.")
            print("     Classic mean-reversion pattern.")

        for i, corr in enumerate(convergence.get("rank_correlations", [])):
            labels = convergence["window_labels"]
            print(f"    Window {labels[i]}→{labels[i+1]}: rho={corr:.3f}")
    else:
        print("  Not enough data for convergence analysis.")

    # --- 6. Focus Agent ---
    if focus:
        print(f"\n--- 6. FOCUS AGENT: {focus_addr[:10]}...{focus_addr[-6:]} ---")
        print(f"  Total bets:          {focus['total_bets']}")
        print(f"  Resolved:            {focus['resolved']} ({focus['wins']}W / {focus['losses']}L)")
        print(f"  Accuracy:            {focus['accuracy_pct']}% (fleet percentile: {focus['accuracy_percentile']}%)")
        pnl = focus['pnl_usdc']
        sign = "+" if pnl >= 0 else ""
        print(f"  Est. PnL:            {sign}${pnl:.2f} (fleet percentile: {focus['pnl_percentile']}%)")
        if focus["avg_share_price"] is not None:
            print(f"  Avg share price:     {focus['avg_share_price']:.4f} (fleet percentile: {focus['price_percentile']}%)")
        print(f"  % on shared markets: {focus['pct_on_shared_markets']:.1f}%")
        if focus["shared_market_accuracy"] is not None:
            print(f"  Shared mkt accuracy: {focus['shared_market_accuracy']}%")
        if focus["unique_market_accuracy"] is not None:
            print(f"  Unique mkt accuracy: {focus['unique_market_accuracy']}%")
        if focus["tool_distribution"]:
            print(f"  Tool distribution:   {json.dumps(focus['tool_distribution'], indent=None)}")

        # Diagnosis
        print("\n  DIAGNOSIS:")
        reasons = []
        if focus["accuracy_percentile"] is not None and focus["accuracy_percentile"] < 25:
            reasons.append("Accuracy is in the bottom quartile of the fleet.")
        if focus["price_percentile"] is not None and focus["price_percentile"] > 75:
            reasons.append("Paying higher share prices than most agents (worse entry).")
        if focus["pct_on_shared_markets"] < 30:
            reasons.append("Mostly betting on unique markets — less fleet overlap, more variance.")
        sa = focus["shared_market_accuracy"]
        ua = focus["unique_market_accuracy"]
        if sa is not None and ua is not None and ua < sa - 10:
            reasons.append(f"Much worse on unique markets ({ua}%) vs shared ({sa}%) — market selection hurting.")
        if not reasons:
            reasons.append("No single dominant factor — likely a combination of variance and market selection.")
        for r in reasons:
            print(f"    - {r}")

    # --- Fleet distribution ---
    qualified = [s for s in all_agents_stats.values() if s["resolved"] >= min_bets]
    if qualified:
        accs = [s["accuracy"] for s in qualified if s["accuracy"] is not None]
        pnls = [s["pnl"] for s in qualified]
        print(f"\n--- FLEET DISTRIBUTION (agents with >= {min_bets} resolved bets) ---")
        print(f"  Agents:  {len(qualified)}")
        if accs:
            print(f"  Accuracy: mean={statistics.mean(accs):.1f}%, median={statistics.median(accs):.1f}%, "
                  f"stdev={statistics.stdev(accs):.1f}%" if len(accs) > 1 else f"  Accuracy: {accs[0]:.1f}%")
        if pnls:
            print(f"  PnL:      mean=${statistics.mean(pnls):.2f}, median=${statistics.median(pnls):.2f}, "
                  f"stdev=${statistics.stdev(pnls):.2f}" if len(pnls) > 1 else f"  PnL: ${pnls[0]:.2f}")
            profitable = sum(1 for p in pnls if p > 0)
            print(f"  Profitable: {profitable}/{len(pnls)} ({profitable/len(pnls)*100:.0f}%)")

    print("\n" + "=" * w)


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def plot_charts(convergence, all_agents_stats, focus_addr, min_bets):
    if not _HAS_MATPLOTLIB:
        print("\nmatplotlib not available — skipping charts. Install with: pip install matplotlib")
        return

    qualified = {
        addr: s for addr, s in all_agents_stats.items()
        if s["resolved"] >= min_bets and s["accuracy"] is not None
    }
    if not qualified:
        return

    fig, axes = plt.subplots(1, 3, figsize=(18, 5))

    # Chart 1: Accuracy distribution with focus agent marked
    accs = [s["accuracy"] for s in qualified.values()]
    ax = axes[0]
    ax.hist(accs, bins=15, color="steelblue", edgecolor="white", alpha=0.8)
    focus_stats = qualified.get(focus_addr)
    if focus_stats:
        ax.axvline(
            focus_stats["accuracy"], color="red", linewidth=2,
            linestyle="--", label=f"Focus: {focus_stats['accuracy']:.1f}%",
        )
        ax.legend()
    ax.set_xlabel("Accuracy (%)")
    ax.set_ylabel("Number of Agents")
    ax.set_title("Fleet Accuracy Distribution")

    # Chart 2: PnL distribution
    pnls = [s["pnl"] for s in qualified.values()]
    ax = axes[1]
    ax.hist(pnls, bins=15, color="steelblue", edgecolor="white", alpha=0.8)
    if focus_stats:
        ax.axvline(
            focus_stats["pnl"], color="red", linewidth=2,
            linestyle="--", label=f"Focus: ${focus_stats['pnl']:.2f}",
        )
        ax.legend()
    ax.set_xlabel("PnL (USDC)")
    ax.set_ylabel("Number of Agents")
    ax.set_title("Fleet PnL Distribution")
    ax.xaxis.set_major_formatter(mticker.FormatStrFormatter('$%.0f'))

    # Chart 3: Accuracy vs PnL scatter
    ax = axes[2]
    for addr, s in qualified.items():
        color = "red" if addr == focus_addr else "steelblue"
        size = 80 if addr == focus_addr else 30
        zorder = 10 if addr == focus_addr else 1
        ax.scatter(
            s["accuracy"], s["pnl"], c=color, s=size, zorder=zorder,
            edgecolors="white", linewidth=0.5,
        )
    ax.set_xlabel("Accuracy (%)")
    ax.set_ylabel("PnL (USDC)")
    ax.set_title("Accuracy vs PnL")
    ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
    ax.axvline(50, color="gray", linewidth=0.5, linestyle="--")
    if focus_stats:
        ax.annotate(
            "Focus", (focus_stats["accuracy"], focus_stats["pnl"]),
            textcoords="offset points", xytext=(10, 5), fontsize=8, color="red",
        )

    fig.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Investigate PolyStrat fleet performance divergence."
    )
    parser.add_argument(
        "--focus", default="0x33d20338f1700eda034ea2543933f94a2177ae4c",
        help="Safe address to highlight (default: Thomas' agent)",
    )
    parser.add_argument(
        "--min-bets", type=int, default=5,
        help="Min resolved bets for inclusion (default: 5)",
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--no-charts", action="store_true",
        help="Skip matplotlib charts",
    )
    parser.add_argument(
        "--no-tools", action="store_true",
        help="Skip mech tool enrichment (faster)",
    )
    args = parser.parse_args()
    focus_addr = args.focus.lower()

    print("=" * 60)
    print("  PolyStrat Fleet Divergence Analysis")
    print("=" * 60)

    # --- Fetch agents ---
    print("\n[1/6] Fetching all PolyStrat agents...")
    safe_addresses = get_all_polystrat_agents()
    print(f"  Found {len(safe_addresses)} registered agents.")

    # --- Fetch all bets ---
    print("\n[2/6] Fetching bets for all agents...")
    agent_raw_bets = {}
    agent_bets = {}
    agent_traders = {}

    for i, addr in enumerate(safe_addresses, 1):
        if i % 20 == 0 or i == len(safe_addresses):
            print(f"  Agent {i}/{len(safe_addresses)}...")
        try:
            raw = fetch_agent_bets(addr)
            if not raw:
                continue
            trader = fetch_trader_agent(addr)
            agent_raw_bets[addr] = raw
            agent_bets[addr] = process_bets(raw)
            agent_traders[addr] = trader
        except Exception as exc:
            print(f"  [warn] {addr[:10]}...: {exc}")

    print(f"  {len(agent_bets)} agents with bets.")

    # --- Tool enrichment ---
    agent_tools = {}  # {addr: {bet_id: tool}}
    if not args.no_tools:
        print("\n[3/6] Fetching mech tool data...")
        for i, addr in enumerate(agent_bets.keys(), 1):
            if i % 10 == 0 or i == len(agent_bets):
                print(f"  Agent {i}/{len(agent_bets)}...")
            try:
                mech_reqs = fetch_mech_requests(addr)
            except Exception:
                mech_reqs = []

            tools = {}
            for bet in agent_bets[addr]:
                tool = match_bet_to_tool(bet["title"], bet["timestamp"], mech_reqs)
                tools[bet["bet_id"]] = tool
            agent_tools[addr] = tools
    else:
        print("\n[3/6] Skipping tool enrichment (--no-tools).")

    # --- Analyses ---
    print("\n[4/6] Analyzing market overlap...")
    overlap = analyze_market_overlap(agent_bets)

    print("[5/6] Analyzing same-market behavior, pricing, and convergence...")
    same_market = analyze_same_market(
        overlap["market_agent_bets"], overlap["market_agents"]
    )
    pricing = analyze_entry_pricing(
        agent_bets, overlap["market_agent_bets"], overlap["market_agents"]
    )
    convergence = analyze_convergence(agent_bets)

    tool_analysis = analyze_tool_assignment(agent_bets, agent_tools)

    # Build per-agent summary stats for fleet distribution
    all_agents_stats = {}
    for addr, bets in agent_bets.items():
        resolved = [b for b in bets if b["is_resolved"]]
        wins = sum(1 for b in resolved if b["is_win"])
        accuracy = wins / len(resolved) * 100 if resolved else None
        pnl = 0.0
        for b in resolved:
            if b["is_win"]:
                pnl += b["shares_usdc"] - b["amount_usdc"]
            else:
                pnl -= b["amount_usdc"]
        prices = [b["share_price"] for b in bets if b["share_price"] > 0]
        all_agents_stats[addr] = {
            "resolved": len(resolved),
            "wins": wins,
            "accuracy": round(accuracy, 1) if accuracy is not None else None,
            "pnl": round(pnl, 2),
            "avg_share_price": round(statistics.mean(prices), 4) if prices else None,
        }

    # Focus agent
    print("\n[6/6] Building focus agent report...")
    focus = None
    if focus_addr in agent_bets:
        focus = focus_agent_summary(
            focus_addr, agent_bets, agent_tools, overlap, all_agents_stats
        )
    else:
        print(f"  Focus agent {focus_addr} not found in fleet data.")

    # --- Output ---
    # Clean non-serializable data from overlap before potential JSON output
    overlap_clean = {k: v for k, v in overlap.items()
                     if k not in ("market_agents", "market_agent_bets")}

    if args.json_output:
        output = {
            "market_overlap": overlap_clean,
            "same_market": {k: v for k, v in same_market.items() if k != "per_market"},
            "tool_assignment": {
                "fleet_tool_accuracy": tool_analysis["fleet_tool_accuracy"],
            },
            "entry_pricing": pricing,
            "convergence": {k: v for k, v in convergence.items() if k != "window_accuracies"},
            "focus_agent": focus,
            "fleet_stats": all_agents_stats,
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print_report(
            overlap_clean, same_market, tool_analysis, pricing, convergence,
            focus, focus_addr, all_agents_stats, args.min_bets,
        )
        if not args.no_charts:
            plot_charts(convergence, all_agents_stats, focus_addr, args.min_bets)


if __name__ == "__main__":
    main()
