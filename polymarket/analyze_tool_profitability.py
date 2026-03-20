"""
Fleet-wide tool profitability analysis for PolyStrat agents.

Answers: which mech tools are actually profitable across the fleet?
Breaks down accuracy, PnL, ROI, avg share price, and bet sizing per tool.

Usage:
    python polymarket/analyze_tool_profitability.py
    python polymarket/analyze_tool_profitability.py --min-bets 20
    python polymarket/analyze_tool_profitability.py --csv tool_profitability.csv
"""

import argparse
import csv
import os
import statistics
import sys
import time
from collections import defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

BETS_URL = "https://predict-polymarket-agents.subgraph.autonolas.tech/"
MECH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"

USDC_DIV = 1_000_000
SEP = "\u241f"
LOOKBACK = 120 * 24 * 60 * 60  # 120 days


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

def get_agents():
    data = post(BETS_URL, """
    {
      traderAgents(first: 1000, orderBy: totalBets, orderDirection: desc) {
        id
        totalBets
      }
    }
    """)
    return [a["id"] for a in data["traderAgents"] if int(a["totalBets"]) > 0]


def fetch_bets(agent):
    data = post(BETS_URL, """
    query($id: ID!) {
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
    """, {"id": agent})

    bets = []
    for mp in data.get("marketParticipants", []):
        for b in mp.get("bets", []):
            q = b.get("question") or {}
            res = q.get("resolution")
            amount = int(b.get("amount", 0))
            shares = int(b.get("shares", 0))
            sp = amount / shares if shares > 0 else 0

            is_resolved = False
            is_win = None
            if res and res.get("winningIndex") is not None:
                wi = int(res["winningIndex"])
                if wi >= 0:
                    is_resolved = True
                    is_win = int(b.get("outcomeIndex", -1)) == wi

            title = (q.get("metadata") or {}).get("title", "")
            bets.append({
                "title": title.split(SEP)[0].strip(),
                "ts": int(b.get("blockTimestamp", 0)),
                "amount": amount / USDC_DIV,
                "shares": shares / USDC_DIV,
                "share_price": sp,
                "resolved": is_resolved,
                "win": is_win,
            })
    return bets


def fetch_mech(agent):
    ts_gt = int(time.time()) - LOOKBACK
    all_reqs = []
    skip = 0
    while True:
        data = post(MECH_URL, """
        query($id: ID!, $ts: Int!, $skip: Int, $first: Int) {
          sender(id: $id) {
            requests(first: $first, skip: $skip, where: {blockTimestamp_gt: $ts}) {
              blockTimestamp
              parsedRequest { questionTitle tool }
            }
          }
        }
        """, {"id": agent, "ts": ts_gt, "skip": skip, "first": 1000})
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
    parser = argparse.ArgumentParser(description="Tool profitability analysis")
    parser.add_argument("--min-bets", type=int, default=5,
                        help="Minimum resolved bets per tool to display (default: 5)")
    parser.add_argument("--csv", type=str, default=None,
                        help="Write results to CSV file")
    args = parser.parse_args()

    print("Fetching agents...")
    agents = get_agents()
    print(f"Found {len(agents)} agents with bets\n")

    # Accumulate per-tool stats across the fleet
    tool_bets = defaultdict(list)  # tool -> list of bet dicts
    tool_agents = defaultdict(set)  # tool -> set of agent addresses

    for i, agent in enumerate(agents):
        print(f"  [{i+1}/{len(agents)}] {agent[:10]}...", end=" ", flush=True)

        bets = fetch_bets(agent)
        resolved = [b for b in bets if b["resolved"]]
        if len(resolved) < 5:
            print(f"skip ({len(resolved)} resolved)")
            continue

        mech = fetch_mech(agent)
        matched = 0
        for b in resolved:
            b["tool"] = match_tool(b, mech)
            b["agent"] = agent
            tool_bets[b["tool"]].append(b)
            tool_agents[b["tool"]].add(agent)
            if b["tool"] != "unknown":
                matched += 1

        print(f"resolved={len(resolved)} matched={matched}/{len(resolved)}")

    if not tool_bets:
        print("\nNo data collected!")
        return

    # ---------------------------------------------------------------------------
    # Compute per-tool stats
    # ---------------------------------------------------------------------------

    tool_stats = []
    for tool, bets in tool_bets.items():
        n = len(bets)
        wins = [b for b in bets if b["win"]]
        losses = [b for b in bets if not b["win"]]

        total_invested = sum(b["amount"] for b in bets)
        pnl = sum(
            (b["shares"] - b["amount"]) if b["win"] else -b["amount"]
            for b in bets
        )
        roi = (pnl / total_invested * 100) if total_invested > 0 else 0

        share_prices = [b["share_price"] for b in bets if b["share_price"] > 0]
        avg_sp = statistics.mean(share_prices) if share_prices else 0
        median_sp = statistics.median(share_prices) if share_prices else 0

        amounts = [b["amount"] for b in bets]
        avg_bet = statistics.mean(amounts) if amounts else 0

        # Win/loss share price breakdown
        win_sps = [b["share_price"] for b in wins if b["share_price"] > 0]
        loss_sps = [b["share_price"] for b in losses if b["share_price"] > 0]
        avg_win_sp = statistics.mean(win_sps) if win_sps else 0
        avg_loss_sp = statistics.mean(loss_sps) if loss_sps else 0

        # Per-agent profitability
        agent_pnls = defaultdict(float)
        for b in bets:
            agent_pnls[b["agent"]] += (b["shares"] - b["amount"]) if b["win"] else -b["amount"]
        profitable_agents = sum(1 for p in agent_pnls.values() if p > 0)

        tool_stats.append({
            "tool": tool,
            "bets": n,
            "wins": len(wins),
            "losses": len(losses),
            "accuracy": len(wins) / n * 100 if n else 0,
            "total_invested": total_invested,
            "pnl": pnl,
            "roi": roi,
            "avg_share_price": avg_sp,
            "median_share_price": median_sp,
            "avg_bet_size": avg_bet,
            "avg_win_share_price": avg_win_sp,
            "avg_loss_share_price": avg_loss_sp,
            "n_agents": len(tool_agents[tool]),
            "profitable_agents": profitable_agents,
        })

    # Sort by PnL
    tool_stats.sort(key=lambda t: t["pnl"], reverse=True)

    # ---------------------------------------------------------------------------
    # Print report
    # ---------------------------------------------------------------------------

    print("\n" + "=" * 90)
    print("TOOL PROFITABILITY ANALYSIS — FLEET-WIDE")
    print("=" * 90)

    total_fleet_bets = sum(t["bets"] for t in tool_stats)
    total_fleet_pnl = sum(t["pnl"] for t in tool_stats)
    total_fleet_invested = sum(t["total_invested"] for t in tool_stats)

    print(f"\nFleet totals: {total_fleet_bets} resolved bets, "
          f"${total_fleet_invested:,.2f} invested, "
          f"PnL ${total_fleet_pnl:+,.2f}")

    # Main table
    print(f"\n{'Tool':<40} {'Bets':>6} {'Acc%':>6} {'PnL':>10} {'ROI%':>7} "
          f"{'AvgSP':>6} {'Agents':>7}")
    print("-" * 90)

    for t in tool_stats:
        if t["bets"] < args.min_bets:
            continue
        print(f"  {t['tool']:<38} {t['bets']:>6} {t['accuracy']:>5.1f}% "
              f"${t['pnl']:>+9.2f} {t['roi']:>+6.1f}% "
              f"{t['avg_share_price']:>5.3f} {t['n_agents']:>4}/{t['profitable_agents']:>2}p")

    # Detailed breakdown for top tools
    significant = [t for t in tool_stats if t["bets"] >= args.min_bets]
    if significant:
        print(f"\n{'DETAILED BREAKDOWN (tools with >= ' + str(args.min_bets) + ' bets)':^90}")
        print("=" * 90)

        for t in significant:
            print(f"\n  {t['tool']}")
            print(f"    Bets:           {t['bets']:>8} ({t['wins']}W / {t['losses']}L)")
            print(f"    Accuracy:       {t['accuracy']:>7.1f}%")
            print(f"    Total invested: ${t['total_invested']:>10,.2f}")
            print(f"    Net PnL:        ${t['pnl']:>+10,.2f}")
            print(f"    ROI:            {t['roi']:>+7.1f}%")
            print(f"    Avg bet size:   ${t['avg_bet_size']:>10,.4f}")
            print(f"    Share price:    avg={t['avg_share_price']:.3f}  "
                  f"median={t['median_share_price']:.3f}")
            print(f"    Win  avg SP:    {t['avg_win_share_price']:.3f}")
            print(f"    Loss avg SP:    {t['avg_loss_share_price']:.3f}")
            print(f"    Agents:         {t['n_agents']} total, "
                  f"{t['profitable_agents']} profitable "
                  f"({t['profitable_agents']/t['n_agents']*100:.0f}%)")

    # Ranking summary
    profitable_tools = [t for t in significant if t["pnl"] > 0]
    losing_tools = [t for t in significant if t["pnl"] <= 0]

    if profitable_tools:
        print(f"\n{'PROFITABLE TOOLS':^90}")
        print("-" * 90)
        for t in profitable_tools:
            print(f"  {t['tool']:<38} PnL=${t['pnl']:>+9.2f}  "
                  f"acc={t['accuracy']:.1f}%  ROI={t['roi']:+.1f}%")

    if losing_tools:
        print(f"\n{'LOSING TOOLS':^90}")
        print("-" * 90)
        for t in losing_tools:
            print(f"  {t['tool']:<38} PnL=${t['pnl']:>+9.2f}  "
                  f"acc={t['accuracy']:.1f}%  ROI={t['roi']:+.1f}%")

    # CSV output
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "tool", "bets", "wins", "losses", "accuracy",
                "total_invested", "pnl", "roi",
                "avg_share_price", "median_share_price", "avg_bet_size",
                "avg_win_share_price", "avg_loss_share_price",
                "n_agents", "profitable_agents",
            ])
            writer.writeheader()
            for t in tool_stats:
                writer.writerow({k: round(v, 4) if isinstance(v, float) else v
                                 for k, v in t.items()})
        print(f"\nCSV written to {args.csv}")

    print("\nDone.")


if __name__ == "__main__":
    main()
