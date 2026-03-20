"""
Share price range profitability analysis for PolyStrat agents.

Answers: at what share prices (probability levels) are bets profitable?
Buckets bets by the share price at time of purchase and computes accuracy,
PnL, and ROI for each bucket.

Usage:
    python polymarket/analyze_price_ranges.py
    python polymarket/analyze_price_ranges.py --buckets 20
    python polymarket/analyze_price_ranges.py --by-tool
    python polymarket/analyze_price_ranges.py --csv price_ranges.csv
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
# Data fetching (same as tool profitability script)
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
# Bucketing
# ---------------------------------------------------------------------------

def bucket_label(sp, n_buckets):
    """Return bucket index and label for a share price."""
    width = 1.0 / n_buckets
    idx = min(int(sp / width), n_buckets - 1)
    lo = idx * width
    hi = lo + width
    return idx, f"{lo:.2f}-{hi:.2f}"


def analyze_bucket(bets):
    """Compute stats for a list of bets in one bucket."""
    n = len(bets)
    if n == 0:
        return None
    wins = sum(1 for b in bets if b["win"])
    losses = n - wins
    accuracy = wins / n * 100

    total_invested = sum(b["amount"] for b in bets)
    pnl = sum(
        (b["shares"] - b["amount"]) if b["win"] else -b["amount"]
        for b in bets
    )
    roi = (pnl / total_invested * 100) if total_invested > 0 else 0

    share_prices = [b["share_price"] for b in bets]
    avg_sp = statistics.mean(share_prices) if share_prices else 0

    avg_bet = statistics.mean(b["amount"] for b in bets)

    return {
        "bets": n,
        "wins": wins,
        "losses": losses,
        "accuracy": accuracy,
        "total_invested": total_invested,
        "pnl": pnl,
        "roi": roi,
        "avg_share_price": avg_sp,
        "avg_bet_size": avg_bet,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Share price range profitability")
    parser.add_argument("--buckets", type=int, default=10,
                        help="Number of price buckets (default: 10)")
    parser.add_argument("--by-tool", action="store_true",
                        help="Also break down price ranges per tool")
    parser.add_argument("--csv", type=str, default=None,
                        help="Write results to CSV file")
    args = parser.parse_args()

    print("Fetching agents...")
    agents = get_agents()
    print(f"Found {len(agents)} agents with bets\n")

    all_resolved = []
    fetch_mech_data = args.by_tool  # only fetch mech if we need tool breakdown

    for i, agent in enumerate(agents):
        print(f"  [{i+1}/{len(agents)}] {agent[:10]}...", end=" ", flush=True)

        bets = fetch_bets(agent)
        resolved = [b for b in bets if b["resolved"]]
        if len(resolved) < 5:
            print(f"skip ({len(resolved)} resolved)")
            continue

        if fetch_mech_data:
            mech = fetch_mech(agent)
            for b in resolved:
                b["tool"] = match_tool(b, mech)

        for b in resolved:
            b["agent"] = agent

        all_resolved.extend(resolved)
        print(f"resolved={len(resolved)}")

    if not all_resolved:
        print("\nNo data collected!")
        return

    n_buckets = args.buckets

    # ---------------------------------------------------------------------------
    # Overall price range analysis
    # ---------------------------------------------------------------------------

    print(f"\n{'=' * 90}")
    print(f"SHARE PRICE RANGE PROFITABILITY — {len(all_resolved)} resolved bets")
    print(f"{'=' * 90}")
    print(f"\nShare price = amount/shares = implied probability the agent paid.")
    print(f"A share price of 0.50 means the agent bought at 50% implied probability.\n")

    # Bucket bets
    buckets = defaultdict(list)
    for b in all_resolved:
        sp = b["share_price"]
        if sp <= 0 or sp > 1:
            continue
        idx, label = bucket_label(sp, n_buckets)
        buckets[(idx, label)].append(b)

    # Print header
    print(f"  {'Price Range':<14} {'Bets':>6} {'Wins':>6} {'Acc%':>6} "
          f"{'Invested':>10} {'PnL':>10} {'ROI%':>7} {'AvgBet':>8}")
    print("  " + "-" * 80)

    csv_rows = []

    for idx in range(n_buckets):
        width = 1.0 / n_buckets
        lo = idx * width
        hi = lo + width
        label = f"{lo:.2f}-{hi:.2f}"
        key = (idx, label)
        bet_list = buckets.get(key, [])

        if not bet_list:
            print(f"  {label:<14} {'—':>6}")
            continue

        stats = analyze_bucket(bet_list)
        print(f"  {label:<14} {stats['bets']:>6} {stats['wins']:>6} "
              f"{stats['accuracy']:>5.1f}% "
              f"${stats['total_invested']:>9,.2f} ${stats['pnl']:>+9,.2f} "
              f"{stats['roi']:>+6.1f}% ${stats['avg_bet_size']:>7,.4f}")

        csv_rows.append({"range": label, "tool": "all", **stats})

    # ---------------------------------------------------------------------------
    # Breakeven analysis
    # ---------------------------------------------------------------------------

    print(f"\n{'BREAKEVEN ANALYSIS':^90}")
    print("=" * 90)
    print("For each price range, the breakeven accuracy is 1/payout_ratio = share_price.")
    print("If accuracy > share_price, the range is profitable in expectation.\n")

    print(f"  {'Price Range':<14} {'MidSP':>6} {'Breakeven':>10} {'Actual':>8} {'Edge':>8} {'Verdict':>10}")
    print("  " + "-" * 65)

    for idx in range(n_buckets):
        width = 1.0 / n_buckets
        lo = idx * width
        hi = lo + width
        label = f"{lo:.2f}-{hi:.2f}"
        mid = (lo + hi) / 2
        key = (idx, label)
        bet_list = buckets.get(key, [])

        if not bet_list:
            continue

        stats = analyze_bucket(bet_list)
        avg_sp = stats["avg_share_price"]
        breakeven = avg_sp * 100  # breakeven accuracy %
        actual = stats["accuracy"]
        edge = actual - breakeven

        verdict = "PROFIT" if edge > 0 else "LOSS"
        print(f"  {label:<14} {avg_sp:>5.3f} {breakeven:>9.1f}% {actual:>7.1f}% "
              f"{edge:>+7.1f}pp  {verdict:>8}")

    # ---------------------------------------------------------------------------
    # Per-tool breakdown (if requested)
    # ---------------------------------------------------------------------------

    if args.by_tool:
        # Get all tools
        tools = sorted(set(b.get("tool", "unknown") for b in all_resolved))
        significant_tools = []
        for tool in tools:
            tool_resolved = [b for b in all_resolved if b.get("tool") == tool]
            if len(tool_resolved) >= 20:
                significant_tools.append(tool)

        for tool in significant_tools:
            tool_resolved = [b for b in all_resolved if b.get("tool") == tool]

            print(f"\n{'─' * 90}")
            print(f"  TOOL: {tool} ({len(tool_resolved)} bets)")
            print(f"{'─' * 90}")

            tool_buckets = defaultdict(list)
            for b in tool_resolved:
                sp = b["share_price"]
                if sp <= 0 or sp > 1:
                    continue
                idx, label = bucket_label(sp, n_buckets)
                tool_buckets[(idx, label)].append(b)

            print(f"  {'Price Range':<14} {'Bets':>6} {'Acc%':>6} {'PnL':>10} "
                  f"{'ROI%':>7} {'Edge':>8}")
            print("  " + "-" * 55)

            for idx in range(n_buckets):
                width = 1.0 / n_buckets
                lo = idx * width
                hi = lo + width
                label = f"{lo:.2f}-{hi:.2f}"
                key = (idx, label)
                bet_list = tool_buckets.get(key, [])

                if not bet_list or len(bet_list) < 3:
                    continue

                stats = analyze_bucket(bet_list)
                avg_sp = stats["avg_share_price"]
                breakeven = avg_sp * 100
                edge = stats["accuracy"] - breakeven

                print(f"  {label:<14} {stats['bets']:>6} {stats['accuracy']:>5.1f}% "
                      f"${stats['pnl']:>+9,.2f} {stats['roi']:>+6.1f}% "
                      f"{edge:>+7.1f}pp")

                csv_rows.append({"range": label, "tool": tool, **stats})

    # ---------------------------------------------------------------------------
    # Summary insights
    # ---------------------------------------------------------------------------

    print(f"\n{'KEY INSIGHTS':^90}")
    print("=" * 90)

    # Find the most/least profitable ranges
    range_stats = []
    for idx in range(n_buckets):
        width = 1.0 / n_buckets
        lo = idx * width
        hi = lo + width
        label = f"{lo:.2f}-{hi:.2f}"
        key = (idx, label)
        bet_list = buckets.get(key, [])
        if len(bet_list) >= 10:
            stats = analyze_bucket(bet_list)
            stats["label"] = label
            stats["avg_sp"] = stats["avg_share_price"]
            stats["edge"] = stats["accuracy"] - stats["avg_sp"] * 100
            range_stats.append(stats)

    if range_stats:
        best_roi = max(range_stats, key=lambda s: s["roi"])
        worst_roi = min(range_stats, key=lambda s: s["roi"])
        best_edge = max(range_stats, key=lambda s: s["edge"])
        most_bets = max(range_stats, key=lambda s: s["bets"])

        print(f"\n  Best ROI range:     {best_roi['label']} "
              f"(ROI={best_roi['roi']:+.1f}%, acc={best_roi['accuracy']:.1f}%, "
              f"n={best_roi['bets']})")
        print(f"  Worst ROI range:    {worst_roi['label']} "
              f"(ROI={worst_roi['roi']:+.1f}%, acc={worst_roi['accuracy']:.1f}%, "
              f"n={worst_roi['bets']})")
        print(f"  Best edge range:    {best_edge['label']} "
              f"(edge={best_edge['edge']:+.1f}pp, acc={best_edge['accuracy']:.1f}%, "
              f"n={best_edge['bets']})")
        print(f"  Most popular range: {most_bets['label']} "
              f"({most_bets['bets']} bets, ROI={most_bets['roi']:+.1f}%)")

        # Where is most money lost?
        biggest_loss = min(range_stats, key=lambda s: s["pnl"])
        biggest_gain = max(range_stats, key=lambda s: s["pnl"])
        print(f"\n  Biggest $ loss:     {biggest_loss['label']} "
              f"(PnL=${biggest_loss['pnl']:+,.2f}, {biggest_loss['bets']} bets)")
        print(f"  Biggest $ gain:     {biggest_gain['label']} "
              f"(PnL=${biggest_gain['pnl']:+,.2f}, {biggest_gain['bets']} bets)")

    # CSV output
    if args.csv and csv_rows:
        with open(args.csv, "w", newline="") as f:
            fieldnames = ["range", "tool", "bets", "wins", "losses", "accuracy",
                          "total_invested", "pnl", "roi", "avg_share_price", "avg_bet_size"]
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in csv_rows:
                writer.writerow({k: round(v, 4) if isinstance(v, float) else v
                                 for k, v in row.items()})
        print(f"\nCSV written to {args.csv}")

    print("\nDone.")


if __name__ == "__main__":
    main()
