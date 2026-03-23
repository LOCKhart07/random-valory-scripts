"""
Single-agent analysis for Omen (predict-omen) agents on Gnosis Chain.

Fetches all resolved bets for a given agent address over a lookback period,
matches tools via Gnosis mech marketplace, and produces:
  1. Overall summary (accuracy, PnL, ROI)
  2. Per-tool usage and profitability
  3. Bet sizing patterns
  4. Price range profitability with breakeven analysis
  5. Temporal trends (weekly accuracy, daily activity, cumulative PnL)

Usage:
    python polymarket/analyze_omen_agent.py 0x2aD146E33B27933241dd68eEb18E77d860ba361D
    python polymarket/analyze_omen_agent.py 0x2aD146E33B27933241dd68eEb18E77d860ba361D --days 60
"""

import argparse
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Endpoints & constants
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

def fetch_agent_participants(agent, since_ts):
    """Fetch market participant data with per-bet detail."""
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

    resolved = []
    pending = []

    for p in all_participants:
        fpmm = p.get("fixedProductMarketMaker") or {}
        ca = fpmm.get("currentAnswer")

        bets_in_window = [
            b for b in (p.get("bets") or [])
            if int(b.get("timestamp", 0)) >= since_ts
        ]
        if not bets_in_window:
            continue

        if ca is None or ca == INVALID_ANSWER:
            # Unresolved or invalid
            for bet in bets_in_window:
                amount = float(bet.get("amount", 0)) / WEI_DIV
                question = (fpmm.get("question") or "").split(SEP)[0].strip()
                pending.append({
                    "bet_id": bet.get("id", ""),
                    "title": question,
                    "ts": int(bet.get("timestamp", 0)),
                    "amount": amount,
                    "market_id": fpmm.get("id", ""),
                })
            continue

        correct_outcome = int(ca, 16)
        total_payout = float(p.get("totalPayout", 0)) / WEI_DIV
        total_traded = float(p.get("totalTraded", 0)) / WEI_DIV

        for bet in bets_in_window:
            outcome_idx = int(bet.get("outcomeIndex", 0))
            amount = float(bet.get("amount", 0)) / WEI_DIV
            is_win = outcome_idx == correct_outcome

            share_price = None
            if total_payout > 0 and is_win:
                share_price = total_traded / total_payout

            if is_win:
                if total_traded > 0:
                    pnl = (amount / total_traded) * (total_payout - total_traded)
                else:
                    pnl = 0
            else:
                pnl = -amount

            question = (fpmm.get("question") or "").split(SEP)[0].strip()
            resolved.append({
                "bet_id": bet.get("id", ""),
                "title": question,
                "ts": int(bet.get("timestamp", 0)),
                "amount": amount,
                "share_price": share_price,
                "win": is_win,
                "pnl": pnl,
                "market_id": fpmm.get("id", ""),
            })

    return resolved, pending


def fetch_mech(agent, since_ts):
    """Fetch mech requests from Gnosis marketplace."""
    all_reqs = []
    skip = 0
    while True:
        data = post(GNOSIS_MECH_URL, """
        query($id: ID!, $ts: Int!, $skip: Int, $first: Int) {
          sender(id: $id) {
            totalMarketplaceRequests
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
# Analysis
# ---------------------------------------------------------------------------

def analyze_tools(bets):
    tool_bets = defaultdict(list)
    for b in bets:
        tool_bets[b["tool"]].append(b)

    results = []
    for tool, tb in sorted(tool_bets.items(), key=lambda x: len(x[1]), reverse=True):
        n = len(tb)
        wins = sum(1 for b in tb if b["win"])
        accuracy = wins / n * 100 if n else 0
        total_invested = sum(b["amount"] for b in tb)
        pnl = sum(b["pnl"] for b in tb)
        roi = (pnl / total_invested * 100) if total_invested > 0 else 0
        avg_bet = statistics.mean(b["amount"] for b in tb)
        results.append({
            "tool": tool, "bets": n, "wins": wins, "losses": n - wins,
            "accuracy": accuracy, "total_invested": total_invested,
            "pnl": pnl, "roi": roi, "avg_bet": avg_bet,
        })
    return results


def analyze_bet_sizing(resolved, pending):
    all_bets = resolved + [{"amount": p["amount"]} for p in pending]
    amounts = [b["amount"] for b in all_bets]

    size_buckets = [
        (0, 0.005, "< 0.005"),
        (0.005, 0.01, "0.005-0.01"),
        (0.01, 0.05, "0.01-0.05"),
        (0.05, 0.1, "0.05-0.10"),
        (0.1, 0.5, "0.10-0.50"),
        (0.5, 1.0, "0.50-1.00"),
        (1.0, 5.0, "1.00-5.00"),
        (5.0, float("inf"), "> 5.00"),
    ]

    dist = {}
    for lo, hi, label in size_buckets:
        count = sum(1 for a in amounts if lo <= a < hi)
        if count > 0:
            dist[label] = count

    win_amounts = [b["amount"] for b in resolved if b["win"]]
    loss_amounts = [b["amount"] for b in resolved if not b["win"]]

    def _stats(vals):
        if not vals:
            return None
        return {
            "mean": statistics.mean(vals),
            "median": statistics.median(vals),
            "min": min(vals),
            "max": max(vals),
            "stdev": statistics.stdev(vals) if len(vals) > 1 else 0,
        }

    return {
        "all": _stats(amounts),
        "win": _stats(win_amounts),
        "loss": _stats(loss_amounts),
        "distribution": dist,
    }


def analyze_price_ranges(bets, n_buckets=10):
    """Estimate share prices and bucket by price range."""
    # Estimate losing-side share prices via complement
    market_win_sp = defaultdict(list)
    for b in bets:
        if b["win"] and b["share_price"] is not None and 0 < b["share_price"] <= 1:
            market_win_sp[b["market_id"]].append(b["share_price"])

    market_avg_win_sp = {mid: statistics.mean(sps) for mid, sps in market_win_sp.items()}

    estimated = 0
    for b in bets:
        if b["share_price"] is None and not b["win"]:
            mid = b["market_id"]
            if mid in market_avg_win_sp:
                b["share_price"] = 1.0 - market_avg_win_sp[mid]
                estimated += 1

    priced = [b for b in bets if b["share_price"] is not None and 0 < b["share_price"] <= 1]
    width = 1.0 / n_buckets
    buckets = defaultdict(list)

    for b in priced:
        idx = min(int(b["share_price"] / width), n_buckets - 1)
        buckets[idx].append(b)

    return priced, estimated, buckets, width, n_buckets


def analyze_temporal(resolved, pending):
    all_items = [(b["ts"], b) for b in resolved] + [(p["ts"], p) for p in pending]
    all_items.sort(key=lambda x: x[0])

    daily = defaultdict(int)
    weekly_stats = defaultdict(lambda: {"total": 0, "wins": 0})

    for ts, item in all_items:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        daily[dt.strftime("%Y-%m-%d")] += 1

    resolved_sorted = sorted(resolved, key=lambda b: b["ts"])
    for b in resolved_sorted:
        dt = datetime.fromtimestamp(b["ts"], tz=timezone.utc)
        week = dt.strftime("%Y-W%W")
        weekly_stats[week]["total"] += 1
        if b["win"]:
            weekly_stats[week]["wins"] += 1

    # Cumulative PnL
    cum_pnl = []
    running = 0.0
    for b in resolved_sorted:
        running += b["pnl"]
        dt = datetime.fromtimestamp(b["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        cum_pnl.append((dt, round(running, 6)))

    # Streaks
    max_win = max_loss = cur_win = cur_loss = 0
    for b in resolved_sorted:
        if b["win"]:
            cur_win += 1
            cur_loss = 0
            max_win = max(max_win, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            max_loss = max(max_loss, cur_loss)

    first_ts = all_items[0][0] if all_items else None
    last_ts = all_items[-1][0] if all_items else None

    return {
        "daily": dict(sorted(daily.items())),
        "weekly": dict(sorted(weekly_stats.items())),
        "cumulative_pnl": cum_pnl,
        "max_win_streak": max_win,
        "max_loss_streak": max_loss,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "active_days": len(daily),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_report(address, resolved, pending, tool_stats, sizing, price_data, temporal):
    w = 90
    total_bets = len(resolved) + len(pending)
    wins = sum(1 for b in resolved if b["win"])
    losses = len(resolved) - wins
    accuracy = wins / len(resolved) * 100 if resolved else 0
    total_invested = sum(b["amount"] for b in resolved)
    total_pnl = sum(b["pnl"] for b in resolved)
    roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    print(f"\n{'=' * w}")
    print(f"  Omen Agent Analysis: {address}")
    print(f"{'=' * w}")

    # --- Overall Summary ---
    print(f"\n--- OVERALL SUMMARY ---")
    print(f"  Total bets:        {total_bets}")
    print(f"  Resolved:          {len(resolved)}")
    print(f"  Pending:           {len(pending)}")
    print(f"  Wins / Losses:     {wins} / {losses}")
    print(f"  Accuracy:          {accuracy:.1f}%")
    print(f"  Total invested:    {total_invested:.4f} xDAI")
    print(f"  Net PnL:           {total_pnl:+.4f} xDAI")
    print(f"  ROI:               {roi:+.1f}%")

    pending_value = sum(p["amount"] for p in pending)
    if pending:
        print(f"  Pending exposure:  {pending_value:.4f} xDAI ({len(pending)} bets)")

    # --- Tool Usage ---
    if tool_stats:
        print(f"\n--- TOOL USAGE & PROFITABILITY ---")
        col_t = max(len(t["tool"]) for t in tool_stats)
        col_t = max(col_t, 4)
        hdr = (f"  {'Tool':<{col_t}} | {'Bets':>5} | {'W/L':>7} | {'Acc%':>6} | "
               f"{'Avg Bet':>10} | {'Invested':>10} | {'PnL':>12} | {'ROI%':>7}")
        sep = "  " + "-" * (len(hdr) - 2)
        print(sep)
        print(hdr)
        print(sep)
        for t in tool_stats:
            print(f"  {t['tool']:<{col_t}} | {t['bets']:>5} | "
                  f"{t['wins']:>3}/{t['losses']:<3} | {t['accuracy']:>5.1f}% | "
                  f"{t['avg_bet']:>9.4f} | {t['total_invested']:>9.4f} | "
                  f"{t['pnl']:>+11.4f} | {t['roi']:>+6.1f}%")
        print(sep)

    # --- Bet Sizing ---
    print(f"\n--- BET SIZING ---")
    if sizing["all"]:
        s = sizing["all"]
        print(f"  Amount (xDAI):  mean={s['mean']:.4f}  median={s['median']:.4f}  "
              f"min={s['min']:.4f}  max={s['max']:.4f}  stdev={s['stdev']:.4f}")

    if sizing["distribution"]:
        print(f"\n  Bet size distribution:")
        for label, count in sizing["distribution"].items():
            bar = "#" * min(count, 60)
            print(f"    {label:>10} xDAI: {count:>4}  {bar}")

    if sizing["win"] and sizing["loss"]:
        print(f"\n  Avg bet on wins:   {sizing['win']['mean']:.4f} xDAI")
        print(f"  Avg bet on losses: {sizing['loss']['mean']:.4f} xDAI")

    # --- Price Range ---
    priced, estimated, buckets, width, n_buckets = price_data

    if priced:
        print(f"\n--- PRICE RANGE PROFITABILITY ---")
        print(f"  Share prices estimated for {len(priced)}/{len(resolved)} resolved bets "
              f"({estimated} via complement).\n")

        print(f"  {'Price Range':<14} {'Bets':>6} {'Wins':>6} {'Acc%':>6} "
              f"{'Invested':>12} {'PnL':>12} {'ROI%':>7}")
        print("  " + "-" * 72)

        for idx in range(n_buckets):
            bet_list = buckets.get(idx, [])
            if not bet_list:
                continue
            lo = idx * width
            hi = lo + width
            label = f"{lo:.2f}-{hi:.2f}"
            n = len(bet_list)
            bwins = sum(1 for b in bet_list if b["win"])
            acc = bwins / n * 100
            inv = sum(b["amount"] for b in bet_list)
            pnl = sum(b["pnl"] for b in bet_list)
            r = (pnl / inv * 100) if inv > 0 else 0
            print(f"  {label:<14} {n:>6} {bwins:>6} {acc:>5.1f}% "
                  f"{inv:>11.4f} {pnl:>+11.4f} {r:>+6.1f}%")

        # Breakeven
        print(f"\n  {'BREAKEVEN ANALYSIS':^72}")
        print(f"  {'Price Range':<14} {'AvgSP':>6} {'Breakeven':>10} {'Actual':>8} {'Edge':>8} {'Verdict':>10}")
        print("  " + "-" * 65)
        for idx in range(n_buckets):
            bet_list = buckets.get(idx, [])
            if not bet_list or len(bet_list) < 3:
                continue
            lo = idx * width
            hi = lo + width
            label = f"{lo:.2f}-{hi:.2f}"
            n = len(bet_list)
            bwins = sum(1 for b in bet_list if b["win"])
            actual = bwins / n * 100
            avg_sp = statistics.mean(b["share_price"] for b in bet_list)
            breakeven = avg_sp * 100
            edge = actual - breakeven
            verdict = "PROFIT" if edge > 0 else "LOSS"
            print(f"  {label:<14} {avg_sp:>5.3f} {breakeven:>9.1f}% {actual:>7.1f}% "
                  f"{edge:>+7.1f}pp  {verdict:>8}")

    # --- Temporal ---
    print(f"\n--- TEMPORAL ANALYSIS ---")
    if temporal["first_ts"]:
        first = datetime.fromtimestamp(temporal["first_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        last = datetime.fromtimestamp(temporal["last_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        span = (temporal["last_ts"] - temporal["first_ts"]) // 86400 + 1
        print(f"  First bet:     {first}")
        print(f"  Last bet:      {last}")
        print(f"  Active days:   {temporal['active_days']} / {span} days")
        print(f"  Avg bets/day:  {total_bets / temporal['active_days']:.1f}" if temporal['active_days'] else "")

    print(f"\n  Streaks:  longest win={temporal['max_win_streak']}  "
          f"longest loss={temporal['max_loss_streak']}")

    if temporal["weekly"]:
        print(f"\n  Weekly accuracy:")
        for week, stats in temporal["weekly"].items():
            acc = stats["wins"] / stats["total"] * 100 if stats["total"] else 0
            bar = "#" * int(acc / 5)
            print(f"    {week}: {acc:>5.1f}% ({stats['wins']}/{stats['total']}) {bar}")

    if temporal["cumulative_pnl"]:
        print(f"\n  Cumulative PnL timeline:")
        points = temporal["cumulative_pnl"]
        step = max(1, len(points) // 15)
        for i in range(0, len(points), step):
            dt, val = points[i]
            print(f"    {dt}: {val:+.4f} xDAI")
        if len(points) % step != 1:
            dt, val = points[-1]
            print(f"    {dt}: {val:+.4f} xDAI")

    # --- Recent bets sample ---
    print(f"\n--- RECENT BETS (last 10) ---")
    recent = sorted(resolved, key=lambda b: b["ts"], reverse=True)[:10]
    for b in recent:
        dt = datetime.fromtimestamp(b["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        result = "WIN" if b["win"] else "LOSS"
        title = b["title"][:50] + "..." if len(b["title"]) > 50 else b["title"]
        print(f"  {dt}  {result:<4}  {b['amount']:.4f} xDAI  PnL={b['pnl']:+.4f}  "
              f"tool={b.get('tool', '?')}")
        print(f"    {title}")

    print(f"\n{'=' * w}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Omen single-agent analysis")
    parser.add_argument("address", help="Agent address (0x...)")
    parser.add_argument("--days", type=int, default=30, help="Lookback days (default: 30)")
    parser.add_argument("--buckets", type=int, default=10, help="Price range buckets (default: 10)")
    args = parser.parse_args()

    address = args.address.lower()
    since_ts = int(time.time()) - args.days * 86400
    since_date = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    print(f"Analyzing Omen agent: {address}")
    print(f"Lookback: {args.days} days (since {since_date})\n")

    # Fetch data
    print("[1/2] Fetching bets from predict-omen subgraph...")
    resolved, pending = fetch_agent_participants(address, since_ts)
    print(f"  Found {len(resolved)} resolved + {len(pending)} pending bets.")

    if not resolved and not pending:
        print("\nNo bets found for this address in the time window. Exiting.")
        sys.exit(0)

    print("[2/2] Fetching mech requests from Gnosis marketplace...")
    mech_reqs = fetch_mech(address, since_ts - 7 * 86400)
    print(f"  Found {len(mech_reqs)} mech requests.")

    # Match tools
    matched = 0
    for b in resolved:
        b["tool"] = match_tool(b, mech_reqs)
        if b["tool"] != "unknown":
            matched += 1
    print(f"  Matched tools: {matched}/{len(resolved)} bets.\n")

    # Analyze
    tool_stats = analyze_tools(resolved)
    sizing = analyze_bet_sizing(resolved, pending)
    price_data = analyze_price_ranges(resolved, n_buckets=args.buckets)
    temporal = analyze_temporal(resolved, pending)

    print_report(address, resolved, pending, tool_stats, sizing, price_data, temporal)


if __name__ == "__main__":
    main()
