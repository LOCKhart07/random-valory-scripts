"""
Fleet-wide PolyStrat agent analysis.

Fetches all PolyStrat agents from the registry, pulls their bets and aggregate
stats, and produces a comprehensive profitability report across the entire fleet.

Usage:
    python polymarket/analyze_fleet.py
    python polymarket/analyze_fleet.py --json
    python polymarket/analyze_fleet.py --min-bets 10   # only agents with >= 10 bets
"""

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLYMARKET_BETS_SUBGRAPH_URL = (
    "https://predict-polymarket-agents.subgraph.autonolas.tech/"
)
THE_GRAPH_API_KEY = os.getenv("THE_GRAPH_API_KEY")
POLYGON_REGISTRY_SUBGRAPH_URL = (
    f"https://gateway.thegraph.com/api/{THE_GRAPH_API_KEY}/subgraphs/id/HHRBjVWFT2bV7eNSRqbCNDtUVnLPt911hcp8mSe4z6KG"
    if THE_GRAPH_API_KEY else None
)

USDC_DECIMALS_DIVISOR = 1_000_000
PERCENTAGE_FACTOR = 100.0

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3


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
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
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
    """Fetch all PolyStrat agents. Tries registry first, falls back to Polymarket subgraph."""
    if POLYGON_REGISTRY_SUBGRAPH_URL:
        try:
            query = """
{
  services(where: {
    agentIds_contains: [86]
  }, first: 1000) {
    id
    multisig
    agentIds
  }
}
"""
            response = call_subgraph(POLYGON_REGISTRY_SUBGRAPH_URL, query)
            return [s["multisig"] for s in response["data"]["services"]]
        except Exception as exc:
            print(f"  Registry fetch failed ({exc}), falling back to Polymarket subgraph...")

    # Fallback: get all traderAgents from the Polymarket bets subgraph
    query = """
{
  traderAgents(first: 1000, orderBy: totalBets, orderDirection: desc) {
    id
    serviceId
    totalBets
  }
}
"""
    response = call_subgraph(POLYMARKET_BETS_SUBGRAPH_URL, query)
    agents = response.get("data", {}).get("traderAgents", [])
    return [a["id"] for a in agents]


def fetch_agent_bets(safe_address, since_ts=None):
    """Fetch all bets for an agent with full metadata, optionally filtered by timestamp."""
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
            if since_ts and int(bet.get("blockTimestamp", 0)) < since_ts:
                continue
            all_bets.append(bet)
    return all_bets


def fetch_trader_agent(safe_address):
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


# ---------------------------------------------------------------------------
# Per-agent analysis
# ---------------------------------------------------------------------------


def analyze_agent(safe_address, bets, trader, time_filtered=False):
    """Compute comprehensive stats for a single agent."""
    resolved = []
    pending = []
    for bet in bets:
        resolution = (bet.get("question") or {}).get("resolution")
        if resolution is not None:
            wi = resolution.get("winningIndex")
            if wi is not None and int(wi) >= 0:
                is_win = int(bet["outcomeIndex"]) == int(wi)
                resolved.append({**bet, "is_win": is_win})
            else:
                pending.append(bet)
        else:
            pending.append(bet)

    wins = [b for b in resolved if b["is_win"]]
    losses = [b for b in resolved if not b["is_win"]]

    # Amounts
    amounts = [int(b.get("amount", 0)) / USDC_DECIMALS_DIVISOR for b in bets]
    total_invested = sum(amounts)
    avg_bet = statistics.mean(amounts) if amounts else 0

    # Share prices
    share_prices = []
    for b in bets:
        amt = int(b.get("amount", 0))
        shares = int(b.get("shares", 0))
        if shares > 0:
            share_prices.append(amt / shares)
    avg_share_price = statistics.mean(share_prices) if share_prices else 0

    # Win/loss bet sizes
    win_amounts = [int(b.get("amount", 0)) / USDC_DECIMALS_DIVISOR for b in wins]
    loss_amounts = [int(b.get("amount", 0)) / USDC_DECIMALS_DIVISOR for b in losses]
    avg_win_bet = statistics.mean(win_amounts) if win_amounts else 0
    avg_loss_bet = statistics.mean(loss_amounts) if loss_amounts else 0

    # Outcome bias
    yes_bets = [b for b in resolved if int(b["outcomeIndex"]) == 0]
    no_bets = [b for b in resolved if int(b["outcomeIndex"]) == 1]
    yes_wins = sum(1 for b in yes_bets if b["is_win"])
    no_wins = sum(1 for b in no_bets if b["is_win"])

    accuracy = (len(wins) / len(resolved) * PERCENTAGE_FACTOR) if resolved else None

    # Estimated PnL from individual bets
    est_pnl = 0.0
    for b in resolved:
        amt = int(b.get("amount", 0)) / USDC_DECIMALS_DIVISOR
        shares_val = int(b.get("shares", 0)) / USDC_DECIMALS_DIVISOR
        if b["is_win"]:
            est_pnl += shares_val - amt
        else:
            est_pnl -= amt

    # Use aggregate subgraph stats for lifetime, estimated PnL for time-filtered
    if time_filtered:
        net_pnl = est_pnl
        total_payout = sum(
            int(b.get("shares", 0)) / USDC_DECIMALS_DIVISOR for b in wins
        )
        total_traded_settled = sum(
            int(b.get("amount", 0)) / USDC_DECIMALS_DIVISOR for b in resolved
        )
    else:
        total_payout = int(trader.get("totalPayout", 0)) / USDC_DECIMALS_DIVISOR if trader else 0
        total_traded_settled = int(trader.get("totalTradedSettled", 0)) / USDC_DECIMALS_DIVISOR if trader else 0
        net_pnl = total_payout - total_traded_settled
    roi = (net_pnl / total_traded_settled * PERCENTAGE_FACTOR) if total_traded_settled > 0 else None

    # Time span
    timestamps = [int(b.get("blockTimestamp", 0)) for b in bets if b.get("blockTimestamp")]
    first_bet = min(timestamps) if timestamps else None
    last_bet = max(timestamps) if timestamps else None
    span_days = ((last_bet - first_bet) // 86400 + 1) if first_bet and last_bet else 0

    return {
        "address": safe_address,
        "service_id": trader.get("serviceId") if trader else None,
        "total_bets": len(bets),
        "resolved": len(resolved),
        "pending": len(pending),
        "wins": len(wins),
        "losses": len(losses),
        "accuracy_pct": round(accuracy, 2) if accuracy is not None else None,
        "total_invested_usdc": round(total_invested, 2),
        "total_payout_usdc": round(total_payout, 2),
        "total_traded_settled_usdc": round(total_traded_settled, 2),
        "net_pnl_usdc": round(net_pnl, 2),
        "est_pnl_usdc": round(est_pnl, 2),
        "roi_pct": round(roi, 2) if roi is not None else None,
        "avg_bet_usdc": round(avg_bet, 4),
        "avg_share_price": round(avg_share_price, 4),
        "avg_win_bet_usdc": round(avg_win_bet, 4),
        "avg_loss_bet_usdc": round(avg_loss_bet, 4),
        "yes_bets": len(yes_bets),
        "yes_wins": yes_wins,
        "yes_accuracy_pct": round(yes_wins / len(yes_bets) * 100, 1) if yes_bets else None,
        "no_bets": len(no_bets),
        "no_wins": no_wins,
        "no_accuracy_pct": round(no_wins / len(no_bets) * 100, 1) if no_bets else None,
        "first_bet": datetime.fromtimestamp(first_bet, tz=timezone.utc).strftime("%Y-%m-%d") if first_bet else None,
        "last_bet": datetime.fromtimestamp(last_bet, tz=timezone.utc).strftime("%Y-%m-%d") if last_bet else None,
        "span_days": span_days,
    }


# ---------------------------------------------------------------------------
# Fleet-level analysis
# ---------------------------------------------------------------------------


def fleet_summary(agents):
    """Aggregate stats across all agents."""
    with_bets = [a for a in agents if a["total_bets"] > 0]
    with_resolved = [a for a in agents if a["resolved"] > 0]
    with_roi = [a for a in agents if a["roi_pct"] is not None]

    profitable = [a for a in with_roi if a["net_pnl_usdc"] > 0]
    unprofitable = [a for a in with_roi if a["net_pnl_usdc"] <= 0]

    total_invested = sum(a["total_invested_usdc"] for a in with_bets)
    total_payout = sum(a["total_payout_usdc"] for a in with_roi)
    total_settled = sum(a["total_traded_settled_usdc"] for a in with_roi)
    fleet_pnl = sum(a["net_pnl_usdc"] for a in with_roi)
    fleet_roi = (fleet_pnl / total_settled * 100) if total_settled > 0 else None

    total_bets = sum(a["total_bets"] for a in with_bets)
    total_resolved = sum(a["resolved"] for a in with_resolved)
    total_wins = sum(a["wins"] for a in with_resolved)
    fleet_accuracy = (total_wins / total_resolved * 100) if total_resolved > 0 else None

    accuracies = [a["accuracy_pct"] for a in with_resolved if a["accuracy_pct"] is not None]
    rois = [a["roi_pct"] for a in with_roi]

    return {
        "total_agents": len(agents),
        "agents_with_bets": len(with_bets),
        "agents_with_resolved": len(with_resolved),
        "profitable_agents": len(profitable),
        "unprofitable_agents": len(unprofitable),
        "total_bets": total_bets,
        "total_resolved": total_resolved,
        "total_wins": total_wins,
        "fleet_accuracy_pct": round(fleet_accuracy, 2) if fleet_accuracy else None,
        "avg_agent_accuracy_pct": round(statistics.mean(accuracies), 2) if accuracies else None,
        "median_agent_accuracy_pct": round(statistics.median(accuracies), 2) if accuracies else None,
        "total_invested_usdc": round(total_invested, 2),
        "total_payout_usdc": round(total_payout, 2),
        "total_settled_usdc": round(total_settled, 2),
        "fleet_pnl_usdc": round(fleet_pnl, 2),
        "fleet_roi_pct": round(fleet_roi, 2) if fleet_roi is not None else None,
        "avg_agent_roi_pct": round(statistics.mean(rois), 2) if rois else None,
        "median_agent_roi_pct": round(statistics.median(rois), 2) if rois else None,
        "best_roi_pct": round(max(rois), 2) if rois else None,
        "worst_roi_pct": round(min(rois), 2) if rois else None,
    }


def find_anomalies(agents):
    """Flag agents with irregular behavior."""
    anomalies = []
    with_resolved = [a for a in agents if a["resolved"] >= 5]

    for a in with_resolved:
        flags = []

        # Extreme accuracy (suspiciously high or terribly low)
        if a["accuracy_pct"] is not None:
            if a["accuracy_pct"] >= 80 and a["resolved"] >= 10:
                flags.append(f"very high accuracy ({a['accuracy_pct']}% on {a['resolved']} bets)")
            if a["accuracy_pct"] <= 30:
                flags.append(f"very low accuracy ({a['accuracy_pct']}% on {a['resolved']} bets)")

        # High accuracy but negative ROI (share price problem)
        if a["accuracy_pct"] and a["accuracy_pct"] > 55 and a["roi_pct"] is not None and a["roi_pct"] < -10:
            flags.append(f"accuracy {a['accuracy_pct']}% but ROI {a['roi_pct']}% — paying too much per share")

        # Profitable agent (rare, worth noting)
        if a["net_pnl_usdc"] > 5:
            flags.append(f"PROFITABLE: +${a['net_pnl_usdc']:.2f} ({a['roi_pct']}% ROI)")

        # Extreme outcome bias
        if a["yes_bets"] and a["no_bets"]:
            ratio = a["no_bets"] / (a["yes_bets"] + a["no_bets"])
            if ratio > 0.85:
                flags.append(f"extreme No bias ({a['no_bets']}/{a['yes_bets'] + a['no_bets']} = {ratio:.0%})")
            elif ratio < 0.15:
                flags.append(f"extreme Yes bias ({a['yes_bets']}/{a['yes_bets'] + a['no_bets']} = {1-ratio:.0%})")

        # Yes/No accuracy divergence
        if a["yes_accuracy_pct"] is not None and a["no_accuracy_pct"] is not None:
            if a["yes_bets"] >= 5 and a["no_bets"] >= 5:
                gap = abs(a["yes_accuracy_pct"] - a["no_accuracy_pct"])
                if gap > 30:
                    flags.append(
                        f"accuracy divergence: Yes={a['yes_accuracy_pct']}% vs No={a['no_accuracy_pct']}% (gap={gap:.0f}pp)"
                    )

        # Very high share prices (buying near certainty)
        if a["avg_share_price"] > 0.85:
            flags.append(f"avg share price {a['avg_share_price']:.2f} — buying near-certainty outcomes")

        # Very low share prices (buying longshots)
        if a["avg_share_price"] < 0.25 and a["total_bets"] >= 10:
            flags.append(f"avg share price {a['avg_share_price']:.2f} — buying longshots")

        # Betting more on losses than wins
        if a["avg_win_bet_usdc"] > 0 and a["avg_loss_bet_usdc"] > 0:
            if a["avg_loss_bet_usdc"] > a["avg_win_bet_usdc"] * 1.5 and a["losses"] >= 5:
                flags.append(
                    f"bets more on losses (avg loss bet ${a['avg_loss_bet_usdc']:.2f} vs win ${a['avg_win_bet_usdc']:.2f})"
                )

        if flags:
            anomalies.append({"address": a["address"], "service_id": a["service_id"], "flags": flags})

    return anomalies


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_report(agents, summary, anomalies, min_bets):
    w = 70
    print("\n" + "=" * w)
    print("  PolyStrat Fleet Analysis")
    print("=" * w)

    # --- Fleet Summary ---
    s = summary
    print("\n--- FLEET SUMMARY ---")
    print(f"  Total agents registered:    {s['total_agents']}")
    print(f"  Agents with bets:           {s['agents_with_bets']}")
    print(f"  Agents with resolved bets:  {s['agents_with_resolved']}")
    print(f"  Profitable / Unprofitable:  {s['profitable_agents']} / {s['unprofitable_agents']}")
    print()
    print(f"  Total bets placed:          {s['total_bets']}")
    print(f"  Total resolved:             {s['total_resolved']}")
    print(f"  Total wins:                 {s['total_wins']}")
    acc_str = f"{s['fleet_accuracy_pct']}%" if s['fleet_accuracy_pct'] else "N/A"
    print(f"  Fleet-wide accuracy:        {acc_str}")
    avg_acc = f"{s['avg_agent_accuracy_pct']}%" if s['avg_agent_accuracy_pct'] else "N/A"
    med_acc = f"{s['median_agent_accuracy_pct']}%" if s['median_agent_accuracy_pct'] else "N/A"
    print(f"  Avg agent accuracy:         {avg_acc}")
    print(f"  Median agent accuracy:      {med_acc}")
    print()
    print(f"  Total invested (sum bets):  ${s['total_invested_usdc']:.2f}")
    print(f"  Total payout:               ${s['total_payout_usdc']:.2f}")
    print(f"  Total settled:              ${s['total_settled_usdc']:.2f}")
    pnl = s['fleet_pnl_usdc']
    sign = "+" if pnl >= 0 else ""
    print(f"  Fleet net PnL:              {sign}${pnl:.2f}")
    roi_str = f"{s['fleet_roi_pct']}%" if s['fleet_roi_pct'] is not None else "N/A"
    print(f"  Fleet ROI:                  {roi_str}")
    print()
    avg_roi = f"{s['avg_agent_roi_pct']}%" if s['avg_agent_roi_pct'] is not None else "N/A"
    med_roi = f"{s['median_agent_roi_pct']}%" if s['median_agent_roi_pct'] is not None else "N/A"
    best_roi = f"{s['best_roi_pct']}%" if s['best_roi_pct'] is not None else "N/A"
    worst_roi = f"{s['worst_roi_pct']}%" if s['worst_roi_pct'] is not None else "N/A"
    print(f"  Avg agent ROI:              {avg_roi}")
    print(f"  Median agent ROI:           {med_roi}")
    print(f"  Best agent ROI:             {best_roi}")
    print(f"  Worst agent ROI:            {worst_roi}")

    # --- Per-agent leaderboard ---
    qualified = [a for a in agents if a["resolved"] >= min_bets and a["roi_pct"] is not None]
    if qualified:
        by_pnl = sorted(qualified, key=lambda a: a["net_pnl_usdc"], reverse=True)

        print(f"\n--- AGENT LEADERBOARD (>= {min_bets} resolved bets, by PnL) ---")
        col_a = 42
        header = (
            f"  {'Address':<{col_a}} | {'Svc':>4} | {'Bets':>5} | {'Res':>4} | "
            f"{'Acc%':>6} | {'Invested':>10} | {'Payout':>10} | {'PnL':>10} | {'ROI%':>7} | {'AvgSP':>6}"
        )
        sep = "  " + "-" * (len(header) - 2)
        print(sep)
        print(header)
        print(sep)
        for a in by_pnl:
            pnl_val = a["net_pnl_usdc"]
            pnl_s = f"{'+'if pnl_val>=0 else ''}${pnl_val:.2f}"
            acc = f"{a['accuracy_pct']:.1f}%" if a["accuracy_pct"] is not None else "N/A"
            svc = str(a["service_id"]) if a["service_id"] else "?"
            print(
                f"  {a['address']:<{col_a}} | {svc:>4} | {a['total_bets']:>5} | "
                f"{a['resolved']:>4} | {acc:>6} | ${a['total_invested_usdc']:>8.2f} | "
                f"${a['total_payout_usdc']:>8.2f} | {pnl_s:>10} | {a['roi_pct']:>6.1f}% | "
                f"{a['avg_share_price']:>5.2f}"
            )
        print(sep)

    # --- Anomalies ---
    if anomalies:
        print(f"\n--- ANOMALIES & IRREGULAR BEHAVIOR ({len(anomalies)} agents flagged) ---")
        for item in anomalies:
            svc = f"svc={item['service_id']}" if item["service_id"] else ""
            print(f"\n  {item['address']} {svc}")
            for flag in item["flags"]:
                print(f"    ! {flag}")

    # --- Profitability verdict ---
    print("\n--- VERDICT ---")
    if s["fleet_pnl_usdc"] > 0:
        print(f"  PolyStrat fleet is NET PROFITABLE: +${s['fleet_pnl_usdc']:.2f}")
    else:
        print(f"  PolyStrat fleet is NET UNPROFITABLE: ${s['fleet_pnl_usdc']:.2f}")
    if s["profitable_agents"] > 0:
        pct = s["profitable_agents"] / max(s["agents_with_resolved"], 1) * 100
        print(f"  {s['profitable_agents']}/{s['agents_with_resolved']} agents ({pct:.0f}%) are profitable")
    else:
        print("  No agents are profitable.")

    print("\n" + "=" * w)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Fleet-wide PolyStrat profitability analysis.")
    parser.add_argument("--min-bets", type=int, default=5, help="Minimum resolved bets to include in leaderboard (default: 5)")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--since", metavar="YYYY-MM-DD", default=None,
        help="Only include bets placed on or after this date (UTC). PnL estimated from individual bets.",
    )
    args = parser.parse_args()

    since_ts = None
    if args.since:
        try:
            since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            since_ts = int(since_dt.timestamp())
        except ValueError:
            parser.error(f"Invalid --since date: {args.since!r}. Expected YYYY-MM-DD.")
        print(f"Filtering to bets since {args.since} (PnL estimated from individual bets)")

    print("Fetching all PolyStrat agents...")
    safe_addresses = get_all_polystrat_agents()
    print(f"Found {len(safe_addresses)} registered agents.\n")

    time_filtered = since_ts is not None
    agents = []
    for i, addr in enumerate(safe_addresses, 1):
        addr = addr.lower()
        if i % 10 == 0 or i == len(safe_addresses):
            print(f"  Processing agent {i}/{len(safe_addresses)}...")

        try:
            bets = fetch_agent_bets(addr, since_ts=since_ts)
            trader = fetch_trader_agent(addr)
            if not bets:
                continue
            result = analyze_agent(addr, bets, trader, time_filtered=time_filtered)
            agents.append(result)
        except Exception as exc:
            print(f"  [warn] Failed for {addr}: {exc}")
            continue

    print(f"\nAnalyzed {len(agents)} agents with bets.\n")

    summary = fleet_summary(agents)
    anomalies = find_anomalies(agents)

    if args.json_output:
        output = {
            "fleet_summary": summary,
            "anomalies": anomalies,
            "agents": sorted(agents, key=lambda a: a.get("net_pnl_usdc", 0), reverse=True),
        }
        print(json.dumps(output, indent=2))
    else:
        print_report(agents, summary, anomalies, args.min_bets)


if __name__ == "__main__":
    main()
