"""
Compare PolyStrat fleet metrics between two time periods to identify what changed.

Splits bets into "before" and "after" periods around a deploy/change date and compares:
  - Overall metrics (bets, accuracy, PnL, bet sizing)
  - Tool accuracy & usage share shifts
  - Bet sizing distribution changes
  - Agent participation & accuracy shifts
  - Daily activity timeline

Usage:
    python polymarket/analyze_poly_week_compare.py
    python polymarket/analyze_poly_week_compare.py --days 14 --split-date 2026-03-26
"""

import argparse
import statistics
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

POLYMARKET_BETS_URL = "https://predict-polymarket-agents.subgraph.autonolas.tech/"
POLYGON_MECH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"

USDC_DIV = 1_000_000


def post(url, query, variables=None, retries=4):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    for attempt in range(retries):
        try:
            r = requests.post(
                url, json=payload,
                headers={"Content-Type": "application/json"}, timeout=90,
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


def fetch_all_agents():
    """Get all trader agents from the Polymarket bets subgraph."""
    data = post(POLYMARKET_BETS_URL, """
    {
      traderAgents(first: 1000, orderBy: totalBets, orderDirection: desc) {
        id
        serviceId
        totalBets
      }
    }
    """)
    return data.get("traderAgents", [])


def fetch_agent_bets(agent_id, since_ts):
    """Fetch all bets for an agent via marketParticipants, filtered by timestamp."""
    all_bets = []
    data = post(POLYMARKET_BETS_URL, """
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
    """, {"id": agent_id})

    for p in (data or {}).get("marketParticipants", []):
        for bet in p.get("bets", []):
            ts = int(bet.get("blockTimestamp", 0))
            if ts >= since_ts:
                all_bets.append({**bet, "_agent": agent_id})

    return agent_id, all_bets


def fetch_mech_for_agent(agent, since_ts):
    all_reqs = []
    skip = 0
    while True:
        data = post(POLYGON_MECH_URL, """
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
    return agent, all_reqs


def fetch_concurrent(fn, items, since_ts, max_workers=15, label="items"):
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn, item, since_ts): item for item in items}
        done = 0
        for future in as_completed(futures):
            done += 1
            key, data = future.result()
            results[key] = data
            if done % 20 == 0 or done == len(items):
                print(f"  [{done}/{len(items)}] {label}", flush=True)
    return results


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


def process_bets(raw_bets):
    """Include all bets placed — resolved or not. win/pnl are None if still pending."""
    results = []
    for bet in raw_bets:
        q = bet.get("question") or {}
        resolution = q.get("resolution")
        title = ((q.get("metadata") or {}).get("title") or "").strip()
        agent = bet.get("_agent", "")
        ts = int(bet.get("blockTimestamp", 0))
        amount = int(bet.get("amount", 0)) / USDC_DIV
        shares = int(bet.get("shares", 0)) / USDC_DIV
        outcome_idx = int(bet.get("outcomeIndex", 0))
        share_price = (amount / shares) if shares > 0 else 0

        is_win = None
        pnl = None
        wi = (resolution or {}).get("winningIndex")
        if wi is not None and int(wi) >= 0:
            is_win = outcome_idx == int(wi)
            pnl = (shares - amount) if is_win else -amount

        results.append({
            "bet_id": bet.get("id", ""),
            "agent": agent,
            "title": title,
            "ts": ts,
            "amount": amount,
            "shares": shares,
            "share_price": share_price,
            "win": is_win,
            "pnl": pnl,
            "market_id": q.get("id", ""),
            "outcome_idx": outcome_idx,
        })
    return results


def match_tool(bet, mech_reqs):
    bt = bet["title"]
    if not bt:
        return "unknown"
    candidates = []
    for r in mech_reqs:
        pr = r.get("parsedRequest") or {}
        mt = (pr.get("questionTitle") or "").strip()
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
# Stats
# ---------------------------------------------------------------------------


def compute_stats(bets):
    if not bets:
        return {"n": 0}
    n = len(bets)
    amounts = [b["amount"] for b in bets]
    share_prices = [b["share_price"] for b in bets if b["share_price"] > 0]
    resolved = [b for b in bets if b["win"] is not None]
    wins = sum(1 for b in resolved if b["win"])
    accuracy = wins / len(resolved) * 100 if resolved else None
    pnl = sum(b["pnl"] for b in resolved) if resolved else None
    return {
        "n": n,
        "resolved": len(resolved),
        "wins": wins,
        "accuracy": accuracy,
        "total_invested": sum(amounts),
        "pnl": pnl,
        "avg_bet": statistics.mean(amounts),
        "median_bet": statistics.median(amounts),
        "avg_share_price": statistics.mean(share_prices) if share_prices else 0,
        "median_share_price": statistics.median(share_prices) if share_prices else 0,
        "agents": len(set(b["agent"] for b in bets)),
        "markets": len(set(b["market_id"] for b in bets)),
    }


def tool_breakdown(bets):
    by_tool = defaultdict(list)
    for b in bets:
        by_tool[b["tool"]].append(b)
    results = {}
    total = len(bets)
    for tool, tb in sorted(by_tool.items(), key=lambda x: len(x[1]), reverse=True):
        n = len(tb)
        resolved = [b for b in tb if b["win"] is not None]
        wins = sum(1 for b in resolved if b["win"])
        accuracy = wins / len(resolved) * 100 if resolved else None
        results[tool] = {
            "n": n,
            "share": n / total * 100 if total else 0,
            "accuracy": accuracy,
            "avg_bet": statistics.mean(b["amount"] for b in tb),
            "avg_sp": statistics.mean(b["share_price"] for b in tb if b["share_price"] > 0) if any(b["share_price"] > 0 for b in tb) else 0,
            "pnl": sum(b["pnl"] for b in resolved) if resolved else None,
        }
    return results


def sizing_breakdown(bets):
    buckets = [
        (0, 0.5, "< $0.50"),
        (0.5, 1.0, "$0.50-1"),
        (1.0, 2.0, "$1-2"),
        (2.0, 5.0, "$2-5"),
        (5.0, 10.0, "$5-10"),
        (10.0, 25.0, "$10-25"),
        (25.0, float("inf"), "> $25"),
    ]
    results = {}
    for lo, hi, label in buckets:
        bucket = [b for b in bets if lo <= b["amount"] < hi]
        if bucket:
            n = len(bucket)
            resolved = [b for b in bucket if b["win"] is not None]
            wins = sum(1 for b in resolved if b["win"])
            results[label] = {
                "n": n,
                "accuracy": wins / len(resolved) * 100 if resolved else None,
                "pnl": sum(b["pnl"] for b in resolved) if resolved else None,
                "total": sum(b["amount"] for b in bucket),
            }
    return results


def daily_breakdown(bets):
    by_day = defaultdict(list)
    for b in bets:
        day = datetime.fromtimestamp(b["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day].append(b)
    return by_day


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_comparison(label_a, stats_a, label_b, stats_b):
    w = 105
    print(f"\n{'=' * w}")
    print(f"{'METRIC':<30} {label_a:>30} {label_b:>30}   {'Delta':>10}")
    print(f"{'=' * w}")

    def row(name, va, vb, fmt=".1f", suffix="", higher_is_better=True):
        if va is None and vb is None:
            return
        if va is None or vb is None:
            val_s = f"{vb:{fmt}}{suffix}" if va is None else f"{va:{fmt}}{suffix}"
            label = label_b if va is None else label_a
            print(f"  {name:<28} {'n/a':>29} {val_s:>29}   n/a" if va is None
                  else f"  {name:<28} {val_s:>29} {'n/a':>29}   n/a")
            return
        delta = vb - va
        arrow = "+" if delta > 0 else ""
        indicator = ""
        if abs(delta) > 0.1:
            if higher_is_better:
                indicator = " !" if delta < 0 else ""
            else:
                indicator = " !" if delta > 0 else ""
        print(f"  {name:<28} {va:>28{fmt}}{suffix} {vb:>28{fmt}}{suffix}   {arrow}{delta:{fmt}}{indicator}")

    row("Bets placed", stats_a["n"], stats_b["n"], "d", higher_is_better=True)
    row("  of which resolved", stats_a.get("resolved", 0), stats_b.get("resolved", 0), "d")
    row("Accuracy % (resolved)", stats_a.get("accuracy"), stats_b.get("accuracy"), ".1f", "%")
    row("PnL (USDC, resolved)", stats_a.get("pnl"), stats_b.get("pnl"), ".2f", "")
    row("Avg bet size ($)", stats_a.get("avg_bet"), stats_b.get("avg_bet"), ".2f", "")
    row("Median bet size ($)", stats_a.get("median_bet"), stats_b.get("median_bet"), ".2f", "")
    row("Avg share price", stats_a.get("avg_share_price"), stats_b.get("avg_share_price"), ".4f", "")
    row("Median share price", stats_a.get("median_share_price"), stats_b.get("median_share_price"), ".4f", "")
    row("Active agents", stats_a.get("agents"), stats_b.get("agents"), "d")
    row("Unique markets", stats_a.get("markets"), stats_b.get("markets"), "d")

    # Bets per agent per day
    if stats_a.get("agents") and stats_b.get("agents"):
        bpd_a = stats_a["n"] / max(stats_a["agents"], 1)
        bpd_b = stats_b["n"] / max(stats_b["agents"], 1)
        row("Bets per agent (period)", bpd_a, bpd_b, ".1f")


def print_tool_comparison(label_a, tools_a, label_b, tools_b):
    w = 110
    all_tools = sorted(
        set(list(tools_a.keys()) + list(tools_b.keys())),
        key=lambda t: tools_a.get(t, {}).get("n", 0) + tools_b.get(t, {}).get("n", 0),
        reverse=True,
    )

    print(f"\n{'=' * w}")
    print(f"TOOL COMPARISON: {label_a} vs {label_b}")
    print(f"{'=' * w}")
    print(f"\n  {'Tool':<36} {'N':>5}|{'N':>5} {'Share%':>7}|{'Share%':>7} "
          f"{'Acc%':>6}|{'Acc%':>6} {'AvgBet':>7}|{'AvgBet':>7}  {'AccDelta':>9}")
    print("  " + "-" * 105)

    for tool in all_tools:
        if tool == "unknown":
            continue
        a = tools_a.get(tool, {"n": 0, "share": 0, "accuracy": 0, "avg_bet": 0, "pnl": 0, "avg_sp": 0})
        b = tools_b.get(tool, {"n": 0, "share": 0, "accuracy": 0, "avg_bet": 0, "pnl": 0, "avg_sp": 0})
        if a["n"] + b["n"] < 5:
            continue
        acc_a_s = f"{a['accuracy']:5.1f}%" if a["accuracy"] is not None else "  n/a "
        acc_b_s = f"{b['accuracy']:5.1f}%" if b["accuracy"] is not None else "  n/a "
        if a["accuracy"] is not None and b["accuracy"] is not None:
            acc_delta = b["accuracy"] - a["accuracy"]
            flag = " !!" if acc_delta < -10 else " !" if acc_delta < -5 else ""
            delta_s = f"{acc_delta:>+8.1f}pp{flag}"
        else:
            delta_s = "     n/a"
        print(f"  {tool:<36} {a['n']:>5}|{b['n']:>5} "
              f"{a['share']:>6.1f}%|{b['share']:>6.1f}% "
              f"{acc_a_s}|{acc_b_s} "
              f"${a['avg_bet']:>5.2f}|${b['avg_bet']:>5.2f}  {delta_s}")

    # Show unknown count
    unk_a = tools_a.get("unknown", {"n": 0})
    unk_b = tools_b.get("unknown", {"n": 0})
    if unk_a["n"] + unk_b["n"] > 0:
        print(f"\n  (unmatched to tool: {unk_a['n']} before, {unk_b['n']} after)")


def print_sizing_comparison(label_a, sizing_a, label_b, sizing_b):
    all_buckets = list(dict.fromkeys(list(sizing_a.keys()) + list(sizing_b.keys())))

    print(f"\n{'=' * 85}")
    print(f"BET SIZE COMPARISON: {label_a} vs {label_b}")
    print(f"{'=' * 85}")
    print(f"\n  {'Bucket':<14} {'N':>6}|{'N':>6} {'Total$':>9}|{'Total$':>9} {'Acc%':>6}|{'Acc%':>6} {'AccDelta':>9}")
    print("  " + "-" * 80)

    for bucket in all_buckets:
        a = sizing_a.get(bucket, {"n": 0, "accuracy": 0, "total": 0})
        b = sizing_b.get(bucket, {"n": 0, "accuracy": 0, "total": 0})
        if a["n"] + b["n"] < 3:
            continue
        acc_a_s = f"{a['accuracy']:5.1f}%" if a.get("accuracy") is not None else "  n/a "
        acc_b_s = f"{b['accuracy']:5.1f}%" if b.get("accuracy") is not None else "  n/a "
        if a.get("accuracy") is not None and b.get("accuracy") is not None:
            delta_s = f"{b['accuracy'] - a['accuracy']:>+8.1f}pp"
        else:
            delta_s = "     n/a"
        print(f"  {bucket:<14} {a['n']:>6}|{b['n']:>6} "
              f"${a.get('total', 0):>7.2f}|${b.get('total', 0):>7.2f} "
              f"{acc_a_s}|{acc_b_s} {delta_s}")


def print_agent_movers(bets_before, bets_after):
    agents_before = defaultdict(list)
    agents_after = defaultdict(list)
    for b in bets_before:
        agents_before[b["agent"]].append(b)
    for b in bets_after:
        agents_after[b["agent"]].append(b)

    movers = []
    for agent in set(list(agents_before.keys()) + list(agents_after.keys())):
        ab = agents_before.get(agent, [])
        aa = agents_after.get(agent, [])
        nb = len(ab)
        na = len(aa)
        if nb < 3 and na < 3:
            continue
        res_b = [b for b in ab if b["win"] is not None]
        res_a = [b for b in aa if b["win"] is not None]
        acc_b = sum(1 for b in res_b if b["win"]) / len(res_b) * 100 if res_b else 0
        acc_a = sum(1 for b in res_a if b["win"]) / len(res_a) * 100 if res_a else 0
        pnl_b = sum(b["pnl"] for b in res_b)
        pnl_a = sum(b["pnl"] for b in res_a)
        avg_bet_b = statistics.mean(b["amount"] for b in ab) if ab else 0
        avg_bet_a = statistics.mean(b["amount"] for b in aa) if aa else 0
        movers.append({
            "agent": agent,
            "bets_before": nb, "acc_before": acc_b, "pnl_before": pnl_b, "avg_bet_before": avg_bet_b,
            "bets_after": na, "acc_after": acc_a, "pnl_after": pnl_a, "avg_bet_after": avg_bet_a,
            "acc_delta": acc_a - acc_b,
            "bet_delta": avg_bet_a - avg_bet_b,
        })

    # Sort by PnL change
    movers.sort(key=lambda m: m["pnl_after"] - m["pnl_before"])

    print(f"\n{'=' * 120}")
    print("AGENT COMPARISON (sorted by PnL change)")
    print(f"{'=' * 120}")
    print(f"\n  {'Agent':<14} {'BetsB':>6} {'BetsA':>6} {'AccB%':>6} {'AccA%':>6} "
          f"{'AccD':>6} {'AvgBetB':>8} {'AvgBetA':>8} {'PnLB':>10} {'PnLA':>10}")
    print("  " + "-" * 115)

    for m in movers[:25]:
        addr = m["agent"][:6] + ".." + m["agent"][-4:]
        print(f"  {addr:<14} {m['bets_before']:>6} {m['bets_after']:>6} "
              f"{m['acc_before']:>5.1f}% {m['acc_after']:>5.1f}% "
              f"{m['acc_delta']:>+5.1f}p "
              f"${m['avg_bet_before']:>6.2f} ${m['avg_bet_after']:>6.2f} "
              f"${m['pnl_before']:>+8.2f} ${m['pnl_after']:>+8.2f}")


def print_daily_timeline(bets, split_ts):
    by_day = daily_breakdown(bets)
    if not by_day:
        return

    split_date = datetime.fromtimestamp(split_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    print(f"\n{'=' * 90}")
    print("DAILY ACTIVITY TIMELINE")
    print(f"{'=' * 90}")
    print(f"\n  {'Date':<12} {'Bets':>6} {'Acc%':>7} {'AvgBet$':>9} "
          f"{'TotalInv$':>11} {'PnL$':>10} {'Agents':>7}")
    print("  " + "-" * 85)

    for day in sorted(by_day.keys()):
        db = by_day[day]
        n = len(db)
        resolved = [b for b in db if b["win"] is not None]
        wins = sum(1 for b in resolved if b["win"])
        acc_s = f"{wins / len(resolved) * 100:5.1f}%" if resolved else "  n/a "
        avg_bet = statistics.mean(b["amount"] for b in db)
        total_inv = sum(b["amount"] for b in db)
        pnl_s = f"${sum(b['pnl'] for b in resolved):>+8.2f}" if resolved else "     n/a"
        agents = len(set(b["agent"] for b in db))
        marker = " <-- deploy" if day == split_date else ""
        print(f"  {day:<12} {n:>6} {acc_s} ${avg_bet:>7.2f} "
              f"${total_inv:>9.2f} {pnl_s} {agents:>7}{marker}")


def print_market_analysis(bets_after):
    market_bets = defaultdict(list)
    for b in bets_after:
        market_bets[b["market_id"]].append(b)

    consensus_wrong = []
    for mid, mb in market_bets.items():
        if len(mb) < 3:
            continue
        n = len(mb)
        resolved = [b for b in mb if b["win"] is not None]
        wins = sum(1 for b in resolved if b["win"])
        acc = wins / len(resolved) * 100 if resolved else 0
        outcome_counts = defaultdict(int)
        for b in mb:
            outcome_counts[b["outcome_idx"]] += 1
        dominant = max(outcome_counts.values())
        consensus_pct = dominant / n * 100

        consensus_wrong.append({
            "title": mb[0]["title"][:70],
            "n": n,
            "accuracy": acc,
            "consensus": consensus_pct,
            "pnl": sum(b["pnl"] for b in resolved) if resolved else 0,
            "agents": len(set(b["agent"] for b in mb)),
        })

    consensus_wrong.sort(key=lambda m: m["pnl"])

    print(f"\n{'=' * 105}")
    print("WORST MARKETS (fleet consensus failures)")
    print(f"{'=' * 105}")
    print(f"\n  {'Bets':>5} {'Ag':>3} {'Acc%':>6} {'Cons%':>6} {'PnL':>10}  Question")
    print("  " + "-" * 100)

    for m in consensus_wrong[:15]:
        print(f"  {m['n']:>5} {m['agents']:>3} {m['accuracy']:>5.1f}% {m['consensus']:>5.1f}% "
              f"${m['pnl']:>+8.2f}  {m['title']}")

    total_markets = len([m for m in consensus_wrong if m["n"] >= 3])
    wrong_markets = len([m for m in consensus_wrong if m["accuracy"] < 50])
    if total_markets > 0:
        print(f"\n  Markets with <50% accuracy: {wrong_markets}/{total_markets} "
              f"({wrong_markets / total_markets * 100:.0f}%)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Compare PolyStrat fleet between two periods (e.g. pre/post deploy)")
    parser.add_argument("--days", type=int, default=14,
                        help="Total lookback days (default: 14)")
    parser.add_argument("--split-date", type=str, default="2026-03-26",
                        help="Date to split periods (YYYY-MM-DD, this date starts the 'after' period)")
    args = parser.parse_args()

    since_ts = int(time.time()) - args.days * 86400
    split_dt = datetime.strptime(args.split_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    split_ts = int(split_dt.timestamp())
    mech_since_ts = since_ts - 7 * 86400

    since_date = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    print(f"Comparing periods: [{since_date} to {args.split_date}) vs [{args.split_date} to now]")
    print()

    t0 = time.time()

    # 1. Fetch all agents
    print("[1/3] Fetching all PolyStrat agents...", end=" ", flush=True)
    agents = fetch_all_agents()
    agent_ids = [a["id"] for a in agents]
    print(f"{len(agent_ids)} agents")

    # 2. Fetch bets for all agents concurrently
    print(f"[2/3] Fetching bets for {len(agent_ids)} agents...")
    bets_by_agent = fetch_concurrent(fetch_agent_bets, agent_ids, since_ts, label="agents (bets)")

    all_raw_bets = []
    for agent_id, agent_bets in bets_by_agent.items():
        all_raw_bets.extend(agent_bets)
    print(f"  Total raw bets: {len(all_raw_bets)}")

    bets = process_bets(all_raw_bets)
    if not bets:
        print("No resolved bets in this period!")
        return
    print(f"  Resolved bets: {len(bets)}")

    # 3. Fetch mech requests for tool matching
    agents_with_bets = list(set(b["agent"] for b in bets))
    print(f"[3/3] Fetching mech requests for {len(agents_with_bets)} agents...")
    mech_index = fetch_concurrent(
        fetch_mech_for_agent, agents_with_bets, mech_since_ts,
        label="agents (mech)",
    )

    for b in bets:
        b["tool"] = match_tool(b, mech_index.get(b["agent"], []))

    fetch_time = time.time() - t0
    print(f"\nData ready in {fetch_time:.1f}s.\n")

    # Split
    before = [b for b in bets if b["ts"] < split_ts]
    after = [b for b in bets if b["ts"] >= split_ts]

    label_a = f"Before ({since_date} to {args.split_date})"
    label_b = f"After ({args.split_date} to now)"

    if not before:
        print(f"No resolved bets before {args.split_date}!")
        return
    if not after:
        print(f"No resolved bets after {args.split_date} yet (bets may still be pending).")
        print("Showing before-period stats only:\n")

    # 1. Overall comparison
    stats_a = compute_stats(before)
    stats_b = compute_stats(after)
    print_comparison(label_a, stats_a, label_b, stats_b)

    # 2. Tool comparison
    tools_a = tool_breakdown(before)
    tools_b = tool_breakdown(after)
    print_tool_comparison(label_a, tools_a, label_b, tools_b)

    # 3. Bet sizing comparison
    sizing_a = sizing_breakdown(before)
    sizing_b = sizing_breakdown(after)
    print_sizing_comparison(label_a, sizing_a, label_b, sizing_b)

    # 4. Agent movers
    print_agent_movers(before, after)

    # 5. Daily timeline
    print_daily_timeline(bets, split_ts)

    # 6. Worst markets in after period
    if after:
        print_market_analysis(after)
    else:
        print_market_analysis(before)

    print(f"\nDone in {time.time() - t0:.1f}s.")


if __name__ == "__main__":
    main()
