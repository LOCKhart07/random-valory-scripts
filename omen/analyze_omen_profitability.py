"""
Omen tool profitability and price range analysis for predict-omen agents.

Fetches all resolved bets from the last N days, matches tools via Gnosis mech
marketplace, and produces:
  1. Per-tool profitability (accuracy, PnL in xDAI, ROI)
  2. Per-price-range profitability (bucketed by estimated share price)

Note: Omen bets don't expose a per-bet `shares` field. Share price is estimated
from participant-level totalTraded/totalPayout for winning markets.  For losing
markets, share price is unknown, so price-range analysis uses only won bets for
the price estimate and a PnL proxy for all bets.

Usage:
    python polymarket/analyze_omen_profitability.py
    python polymarket/analyze_omen_profitability.py --days 30
    python polymarket/analyze_omen_profitability.py --csv omen_profitability.csv
"""

import argparse
import csv
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
# Endpoints
# ---------------------------------------------------------------------------

OMEN_BETS_URL = "https://api.subgraph.staging.autonolas.tech/api/proxy/predict-omen"
GNOSIS_MECH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"

WEI_DIV = 10 ** 18
SEP = "\u241f"
INVALID_ANSWER = "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"


def post(url, query, variables=None, retries=4):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    for attempt in range(retries):
        try:
            r = requests.post(
                url, json=payload,
                headers={"Content-Type": "application/json"},
                timeout=90,
            )
            r.raise_for_status()
            d = r.json()
            if "errors" in d:
                raise RuntimeError(d["errors"])
            return d["data"]
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(3 * (2 ** attempt))


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_all_recent_bets(since_ts):
    """Fetch all resolved bets from predict-omen since a timestamp.

    Returns a list of bet dicts grouped by bettor, with per-bet and
    per-participant (per-market) data.
    """
    all_bets = []
    skip = 0

    while True:
        query = f"""
        {{
          bets(
            first: 1000
            skip: {skip}
            orderBy: timestamp
            orderDirection: desc
            where: {{
              timestamp_gte: {since_ts}
              fixedProductMarketMaker_: {{currentAnswer_not: null}}
            }}
          ) {{
            id
            timestamp
            amount
            feeAmount
            outcomeIndex
            bettor {{
              id
              serviceId
            }}
            fixedProductMarketMaker {{
              id
              currentAnswer
              question
              outcomes
            }}
          }}
        }}
        """
        data = post(OMEN_BETS_URL, query)
        batch = data.get("bets", [])
        if not batch:
            break
        all_bets.extend(batch)
        if len(batch) < 1000:
            break
        skip += 1000

    return all_bets


def fetch_agent_participants(agent, since_ts):
    """Fetch market participant data for an agent (has totalPayout/totalTraded)."""
    all_participants = []
    skip = 0

    while True:
        data = post(OMEN_BETS_URL, """
        query($id: ID!, $first: Int!, $skip: Int!) {
          marketParticipants(
            where: { traderAgent_: { id: $id } }
            orderBy: blockTimestamp
            orderDirection: desc
            first: $first
            skip: $skip
          ) {
            id
            totalBets
            totalPayout
            totalTraded
            totalTradedSettled
            fixedProductMarketMaker {
              id
              question
              outcomes
              currentAnswer
              currentAnswerTimestamp
            }
            bets {
              id
              timestamp
              amount
              feeAmount
              outcomeIndex
            }
          }
        }
        """, {"id": agent, "first": 1000, "skip": skip})

        participants = data.get("marketParticipants", [])
        if not participants:
            break
        all_participants.extend(participants)
        if len(participants) < 1000:
            break
        skip += 1000

    # Filter to only markets with resolution and bets in the time window
    result = []
    for p in all_participants:
        fpmm = p.get("fixedProductMarketMaker") or {}
        ca = fpmm.get("currentAnswer")
        if ca is None or ca == INVALID_ANSWER:
            continue

        # Filter bets by timestamp
        recent_bets = [
            b for b in (p.get("bets") or [])
            if int(b.get("timestamp", 0)) >= since_ts
        ]
        if not recent_bets:
            continue

        correct_outcome = int(ca, 16)
        total_payout = float(p.get("totalPayout", 0)) / WEI_DIV
        total_traded = float(p.get("totalTraded", 0)) / WEI_DIV

        for bet in recent_bets:
            outcome_idx = int(bet.get("outcomeIndex", 0))
            amount = float(bet.get("amount", 0)) / WEI_DIV
            is_win = outcome_idx == correct_outcome

            # Estimate share price from participant totals (works best for single-bet markets)
            share_price = None
            if total_payout > 0 and is_win:
                share_price = total_traded / total_payout

            # PnL estimation
            if is_win:
                # Proportional share of the market payout
                if total_traded > 0:
                    pnl = (amount / total_traded) * (total_payout - total_traded)
                else:
                    pnl = 0
            else:
                pnl = -amount

            question = (fpmm.get("question") or "").split(SEP)[0].strip()
            result.append({
                "bet_id": bet.get("id", ""),
                "title": question,
                "ts": int(bet.get("timestamp", 0)),
                "amount": amount,
                "share_price": share_price,
                "resolved": True,
                "win": is_win,
                "pnl": pnl,
                "market_id": fpmm.get("id", ""),
            })

    return result


def fetch_mech(agent, since_ts):
    """Fetch mech requests from Gnosis marketplace."""
    all_reqs = []
    skip = 0
    while True:
        data = post(GNOSIS_MECH_URL, """
        query($id: ID!, $ts: Int!, $skip: Int, $first: Int) {
          sender(id: $id) {
            requests(first: $first, skip: $skip, where: {blockTimestamp_gt: $ts}) {
              blockTimestamp
              parsedRequest { questionTitle tool }
            }
          }
        }
        """, {"id": agent, "ts": since_ts, "skip": skip, "first": 1000})
        sender = (data or {}).get("sender") or {}
        batch = sender.get("requests", [])
        if not batch:
            break
        all_reqs.extend(batch)
        if len(batch) < 1000:
            break
        skip += 1000
    return all_reqs


def match_tool(bet, mech_reqs):
    bt = bet["title"]
    if not bt:
        return "unknown"

    candidates = []
    for r in mech_reqs:
        pr = r.get("parsedRequest") or {}
        mt = (pr.get("questionTitle") or "").split(SEP)[0].strip()
        if not mt:
            continue
        if bt.startswith(mt) or mt.startswith(bt):
            candidates.append(r)

    if not candidates:
        return "unknown"

    before = [c for c in candidates if int(c.get("blockTimestamp", 0)) <= bet["ts"]]
    if before:
        chosen = max(before, key=lambda c: int(c.get("blockTimestamp", 0)))
    else:
        chosen = candidates[0]
    return (chosen.get("parsedRequest") or {}).get("tool") or "unknown"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Omen tool & price range profitability")
    parser.add_argument("--days", type=int, default=30,
                        help="Lookback period in days (default: 30)")
    parser.add_argument("--min-bets", type=int, default=5,
                        help="Minimum bets per tool to display (default: 5)")
    parser.add_argument("--buckets", type=int, default=10,
                        help="Number of price range buckets (default: 10)")
    parser.add_argument("--csv", type=str, default=None,
                        help="Write results to CSV file")
    args = parser.parse_args()

    since_ts = int(time.time()) - args.days * 86400
    since_date = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    print(f"Analyzing Omen bets from {since_date} (last {args.days} days)\n")

    # Step 1: Discover agents from recent bets
    print("Fetching recent resolved bets to discover agents...")
    raw_bets = fetch_all_recent_bets(since_ts)
    print(f"Found {len(raw_bets)} resolved bets")

    # Extract unique bettors
    agents = list(set(b["bettor"]["id"] for b in raw_bets))
    print(f"Found {len(agents)} unique agents\n")

    # Step 2: Per-agent fetch with participant data + mech matching
    all_bets = []
    for i, agent in enumerate(agents):
        print(f"  [{i+1}/{len(agents)}] {agent[:10]}...", end=" ", flush=True)

        bets = fetch_agent_participants(agent, since_ts)
        if len(bets) < 3:
            print(f"skip ({len(bets)} bets)")
            continue

        mech = fetch_mech(agent, since_ts - 7 * 86400)  # mech lookback slightly wider
        matched = 0
        for b in bets:
            b["tool"] = match_tool(b, mech)
            b["agent"] = agent
            if b["tool"] != "unknown":
                matched += 1

        all_bets.extend(bets)
        print(f"bets={len(bets)} matched={matched}/{len(bets)}")

    if not all_bets:
        print("\nNo data collected!")
        return

    # =========================================================================
    # ANALYSIS 1: TOOL PROFITABILITY
    # =========================================================================

    tool_bets = defaultdict(list)
    for b in all_bets:
        tool_bets[b["tool"]].append(b)

    tool_stats = []
    for tool, bets in tool_bets.items():
        n = len(bets)
        wins = [b for b in bets if b["win"]]
        losses = [b for b in bets if not b["win"]]
        accuracy = len(wins) / n * 100 if n else 0

        total_invested = sum(b["amount"] for b in bets)
        pnl = sum(b["pnl"] for b in bets)
        roi = (pnl / total_invested * 100) if total_invested > 0 else 0

        avg_bet = statistics.mean(b["amount"] for b in bets) if bets else 0

        # Share price (winners only)
        win_sps = [b["share_price"] for b in wins if b["share_price"] is not None]
        avg_win_sp = statistics.mean(win_sps) if win_sps else None

        n_agents = len(set(b["agent"] for b in bets))
        agent_pnls = defaultdict(float)
        for b in bets:
            agent_pnls[b["agent"]] += b["pnl"]
        profitable_agents = sum(1 for p in agent_pnls.values() if p > 0)

        tool_stats.append({
            "tool": tool,
            "bets": n,
            "wins": len(wins),
            "losses": len(losses),
            "accuracy": accuracy,
            "total_invested": total_invested,
            "pnl": pnl,
            "roi": roi,
            "avg_bet_size": avg_bet,
            "avg_win_share_price": avg_win_sp,
            "n_agents": n_agents,
            "profitable_agents": profitable_agents,
        })

    tool_stats.sort(key=lambda t: t["pnl"], reverse=True)

    total_bets = sum(t["bets"] for t in tool_stats)
    total_pnl = sum(t["pnl"] for t in tool_stats)
    total_invested = sum(t["total_invested"] for t in tool_stats)

    print(f"\n{'=' * 90}")
    print(f"OMEN TOOL PROFITABILITY — LAST {args.days} DAYS")
    print(f"{'=' * 90}")
    print(f"\nFleet totals: {total_bets} resolved bets, "
          f"{total_invested:,.4f} xDAI invested, "
          f"PnL {total_pnl:+,.4f} xDAI")

    print(f"\n{'Tool':<40} {'Bets':>6} {'Acc%':>6} {'PnL (xDAI)':>12} {'ROI%':>7} {'Agents':>7}")
    print("-" * 90)

    for t in tool_stats:
        if t["bets"] < args.min_bets:
            continue
        print(f"  {t['tool']:<38} {t['bets']:>6} {t['accuracy']:>5.1f}% "
              f"{t['pnl']:>+11.4f} {t['roi']:>+6.1f}% "
              f"{t['n_agents']:>4}/{t['profitable_agents']:>2}p")

    # Detailed breakdown
    significant = [t for t in tool_stats if t["bets"] >= args.min_bets]
    if significant:
        print(f"\n{'DETAILED BREAKDOWN':^90}")
        print("=" * 90)
        for t in significant:
            sp_str = f"{t['avg_win_share_price']:.3f}" if t['avg_win_share_price'] else "N/A"
            print(f"\n  {t['tool']}")
            print(f"    Bets:           {t['bets']:>8} ({t['wins']}W / {t['losses']}L)")
            print(f"    Accuracy:       {t['accuracy']:>7.1f}%")
            print(f"    Total invested: {t['total_invested']:>10.4f} xDAI")
            print(f"    Net PnL:        {t['pnl']:>+10.4f} xDAI")
            print(f"    ROI:            {t['roi']:>+7.1f}%")
            print(f"    Avg bet size:   {t['avg_bet_size']:>10.4f} xDAI")
            print(f"    Win avg SP:     {sp_str}")
            print(f"    Agents:         {t['n_agents']} total, "
                  f"{t['profitable_agents']} profitable "
                  f"({t['profitable_agents']/t['n_agents']*100:.0f}%)" if t['n_agents'] > 0 else "")

    profitable_tools = [t for t in significant if t["pnl"] > 0]
    losing_tools = [t for t in significant if t["pnl"] <= 0]

    if profitable_tools:
        print(f"\n{'PROFITABLE TOOLS':^90}")
        print("-" * 90)
        for t in profitable_tools:
            print(f"  {t['tool']:<38} PnL={t['pnl']:>+10.4f} xDAI  "
                  f"acc={t['accuracy']:.1f}%  ROI={t['roi']:+.1f}%")

    if losing_tools:
        print(f"\n{'LOSING TOOLS':^90}")
        print("-" * 90)
        for t in losing_tools:
            print(f"  {t['tool']:<38} PnL={t['pnl']:>+10.4f} xDAI  "
                  f"acc={t['accuracy']:.1f}%  ROI={t['roi']:+.1f}%")

    # =========================================================================
    # ANALYSIS 2: PRICE RANGE PROFITABILITY
    # =========================================================================

    # Estimate share prices for ALL bets using complement approach:
    # - Won bets: share_price = totalTraded / totalPayout (direct)
    # - Lost bets: share_price ≈ 1 - winning_share_price (binary FPMM complement)
    # Group winning share prices by market to enable cross-inference.

    market_win_sp = defaultdict(list)  # market_id -> [share_price, ...]
    for b in all_bets:
        if b["win"] and b["share_price"] is not None and 0 < b["share_price"] <= 1:
            market_win_sp[b["market_id"]].append(b["share_price"])

    # Compute per-market avg winning share price
    market_avg_win_sp = {}
    for mid, sps in market_win_sp.items():
        market_avg_win_sp[mid] = statistics.mean(sps)

    # Assign estimated share prices to losing bets
    estimated_count = 0
    for b in all_bets:
        if b["share_price"] is None and not b["win"]:
            mid = b["market_id"]
            if mid in market_avg_win_sp:
                b["share_price"] = 1.0 - market_avg_win_sp[mid]
                estimated_count += 1

    bets_with_sp = [b for b in all_bets if b["share_price"] is not None and 0 < b["share_price"] <= 1]

    print(f"\n{'=' * 90}")
    print(f"PRICE RANGE PROFITABILITY (ALL BETS)")
    print(f"{'=' * 90}")
    print(f"\nShare price for winners: totalTraded/totalPayout (direct).")
    print(f"Share price for losers: 1 - avg_winning_share_price in same market (complement).")
    print(f"Estimated {estimated_count} losing-side share prices via complement.")
    print(f"Covering {len(bets_with_sp)} of {len(all_bets)} total bets.\n")

    if bets_with_sp:
        n_buckets = args.buckets
        width = 1.0 / n_buckets
        buckets = defaultdict(list)

        for b in bets_with_sp:
            idx = min(int(b["share_price"] / width), n_buckets - 1)
            lo = idx * width
            hi = lo + width
            label = f"{lo:.2f}-{hi:.2f}"
            buckets[(idx, label)].append(b)

        print(f"  {'Price Range':<14} {'Bets':>6} {'Wins':>6} {'Acc%':>6} "
              f"{'Invested':>12} {'PnL':>12} {'ROI%':>7} {'AvgSP':>6}")
        print("  " + "-" * 80)

        for idx in range(n_buckets):
            lo = idx * width
            hi = lo + width
            label = f"{lo:.2f}-{hi:.2f}"
            key = (idx, label)
            bet_list = buckets.get(key, [])
            if not bet_list:
                continue

            n = len(bet_list)
            wins = sum(1 for b in bet_list if b["win"])
            acc = wins / n * 100
            invested = sum(b["amount"] for b in bet_list)
            pnl = sum(b["pnl"] for b in bet_list)
            roi = (pnl / invested * 100) if invested > 0 else 0
            sps = [b["share_price"] for b in bet_list]
            avg_sp = statistics.mean(sps)

            print(f"  {label:<14} {n:>6} {wins:>6} {acc:>5.1f}% "
                  f"{invested:>11.4f} {pnl:>+11.4f} {roi:>+6.1f}% {avg_sp:>5.3f}")

        # Breakeven analysis
        print(f"\n{'BREAKEVEN ANALYSIS':^90}")
        print("=" * 90)
        print("Breakeven accuracy = avg share price. Edge = actual accuracy - breakeven.\n")

        print(f"  {'Price Range':<14} {'MidSP':>6} {'Breakeven':>10} {'Actual':>8} {'Edge':>8} {'Verdict':>10}")
        print("  " + "-" * 65)

        for idx in range(n_buckets):
            lo = idx * width
            hi = lo + width
            label = f"{lo:.2f}-{hi:.2f}"
            key = (idx, label)
            bet_list = buckets.get(key, [])
            if not bet_list or len(bet_list) < 10:
                continue

            n = len(bet_list)
            wins = sum(1 for b in bet_list if b["win"])
            actual_acc = wins / n * 100
            avg_sp = statistics.mean(b["share_price"] for b in bet_list)
            breakeven = avg_sp * 100
            edge = actual_acc - breakeven
            verdict = "PROFIT" if edge > 0 else "LOSS"

            print(f"  {label:<14} {avg_sp:>5.3f} {breakeven:>9.1f}% {actual_acc:>7.1f}% "
                  f"{edge:>+7.1f}pp  {verdict:>8}")

    # Also show BET SIZE buckets
    print(f"\n{'=' * 90}")
    print(f"BET SIZE PROFITABILITY (ALL {len(all_bets)} BETS)")
    print(f"{'=' * 90}\n")

    amounts = [b["amount"] for b in all_bets]
    if amounts:
        size_buckets = [
            (0, 0.005, "< 0.005 xDAI"),
            (0.005, 0.01, "0.005-0.01"),
            (0.01, 0.05, "0.01-0.05"),
            (0.05, 0.1, "0.05-0.10"),
            (0.1, 0.5, "0.10-0.50"),
            (0.5, 1.0, "0.50-1.00"),
            (1.0, 5.0, "1.00-5.00"),
            (5.0, float("inf"), "> 5.00 xDAI"),
        ]

        print(f"  {'Bet Size':<16} {'Bets':>6} {'Wins':>6} {'Acc%':>6} "
              f"{'Invested':>12} {'PnL':>12} {'ROI%':>7}")
        print("  " + "-" * 75)

        for lo, hi, label in size_buckets:
            bucket_bets = [b for b in all_bets if lo <= b["amount"] < hi]
            if not bucket_bets:
                continue

            n = len(bucket_bets)
            wins = sum(1 for b in bucket_bets if b["win"])
            acc = wins / n * 100
            invested = sum(b["amount"] for b in bucket_bets)
            pnl = sum(b["pnl"] for b in bucket_bets)
            roi = (pnl / invested * 100) if invested > 0 else 0

            print(f"  {label:<16} {n:>6} {wins:>6} {acc:>5.1f}% "
                  f"{invested:>11.4f} {pnl:>+11.4f} {roi:>+6.1f}%")

    # =========================================================================
    # KEY INSIGHTS
    # =========================================================================

    print(f"\n{'KEY INSIGHTS':^90}")
    print("=" * 90)

    if significant:
        best_tool = max(significant, key=lambda t: t["roi"])
        worst_tool = min(significant, key=lambda t: t["roi"])
        most_used = max(significant, key=lambda t: t["bets"])

        print(f"\n  Best ROI tool:   {best_tool['tool']} "
              f"(ROI={best_tool['roi']:+.1f}%, acc={best_tool['accuracy']:.1f}%, "
              f"n={best_tool['bets']})")
        print(f"  Worst ROI tool:  {worst_tool['tool']} "
              f"(ROI={worst_tool['roi']:+.1f}%, acc={worst_tool['accuracy']:.1f}%, "
              f"n={worst_tool['bets']})")
        print(f"  Most used tool:  {most_used['tool']} "
              f"({most_used['bets']} bets, {most_used['bets']/total_bets*100:.0f}% of fleet)")

    fleet_acc = sum(t["wins"] for t in tool_stats) / total_bets * 100 if total_bets else 0
    fleet_roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0
    print(f"\n  Fleet accuracy:  {fleet_acc:.1f}%")
    print(f"  Fleet ROI:       {fleet_roi:+.1f}%")
    print(f"  Fleet PnL:       {total_pnl:+.4f} xDAI")

    # CSV output
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "tool", "bets", "wins", "losses", "accuracy",
                "total_invested", "pnl", "roi", "avg_bet_size",
                "avg_win_share_price", "n_agents", "profitable_agents",
            ])
            writer.writeheader()
            for t in tool_stats:
                writer.writerow({k: round(v, 6) if isinstance(v, float) else v
                                 for k, v in t.items()})
        print(f"\nCSV written to {args.csv}")

    print("\nDone.")


if __name__ == "__main__":
    main()
