"""
Compare Omen fleet metrics between two time periods to identify what changed.

Reuses bulk-fetch approach from analyze_omen_fleet_fast.py.
Splits bets into "before" (W08-W10) and "after" (W11) periods and compares:
  - Tool accuracy & usage share shifts
  - Bet sizing changes
  - Agent participation & accuracy shifts
  - Market question characteristics

Usage:
    python polymarket/analyze_omen_week_compare.py
    python polymarket/analyze_omen_week_compare.py --days 30 --split-date 2026-03-16
"""

import argparse
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

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
            r = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=90)
            r.raise_for_status()
            d = r.json()
            if "errors" in d:
                raise RuntimeError(d["errors"])
            return d["data"]
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(3 * (2 ** attempt))


def fetch_all_bets(since_ts):
    all_bets = []
    skip = 0
    while True:
        data = post(OMEN_BETS_URL, f"""
        {{
          bets(
            first: 1000, skip: {skip}
            orderBy: timestamp, orderDirection: desc
            where: {{ timestamp_gte: {since_ts}, fixedProductMarketMaker_: {{currentAnswer_not: null}} }}
          ) {{
            id timestamp amount feeAmount outcomeIndex
            bettor {{ id serviceId }}
            fixedProductMarketMaker {{ id currentAnswer question outcomes }}
          }}
        }}
        """)
        batch = data.get("bets", [])
        if not batch:
            break
        all_bets.extend(batch)
        if len(batch) < 1000:
            break
        skip += 1000
    return all_bets


def fetch_mech_for_agent(agent, since_ts):
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
    return agent, all_reqs


def fetch_mech_concurrent(agents, since_ts, max_workers=15):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    mech_index = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_mech_for_agent, a, since_ts): a for a in agents}
        done = 0
        for future in as_completed(futures):
            done += 1
            agent, reqs = future.result()
            mech_index[agent] = reqs
            if done % 20 == 0 or done == len(agents):
                print(f"  [{done}/{len(agents)}]", flush=True)
    return mech_index


def process_bets(raw_bets):
    results = []
    for bet in raw_bets:
        fpmm = bet.get("fixedProductMarketMaker") or {}
        ca = fpmm.get("currentAnswer")
        if ca is None or ca == INVALID_ANSWER:
            continue
        correct_outcome = int(ca, 16)
        outcome_idx = int(bet.get("outcomeIndex", 0))
        amount = float(bet.get("amount", 0)) / WEI_DIV
        is_win = outcome_idx == correct_outcome
        question = (fpmm.get("question") or "").split(SEP)[0].strip()
        agent = bet.get("bettor", {}).get("id", "")
        results.append({
            "bet_id": bet.get("id", ""),
            "agent": agent,
            "title": question,
            "ts": int(bet.get("timestamp", 0)),
            "amount": amount,
            "win": is_win,
            "pnl": amount if is_win else -amount,
            "market_id": fpmm.get("id", ""),
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


def compute_stats(bets):
    """Compute summary stats for a set of bets."""
    if not bets:
        return {"n": 0}
    n = len(bets)
    wins = sum(1 for b in bets if b["win"])
    amounts = [b["amount"] for b in bets]
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "accuracy": wins / n * 100,
        "total_invested": sum(amounts),
        "pnl": sum(b["pnl"] for b in bets),
        "avg_bet": statistics.mean(amounts),
        "median_bet": statistics.median(amounts),
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
        wins = sum(1 for b in tb if b["win"])
        results[tool] = {
            "n": n,
            "share": n / total * 100 if total else 0,
            "accuracy": wins / n * 100 if n else 0,
            "avg_bet": statistics.mean(b["amount"] for b in tb),
            "pnl": sum(b["pnl"] for b in tb),
        }
    return results


def sizing_breakdown(bets):
    buckets = [
        (0, 0.005, "< 0.005"),
        (0.005, 0.01, "0.005-0.01"),
        (0.01, 0.05, "0.01-0.05"),
        (0.05, 0.5, "0.05-0.50"),
        (0.5, 2.0, "0.50-2.00"),
        (2.0, float("inf"), "> 2.00"),
    ]
    results = {}
    for lo, hi, label in buckets:
        bucket = [b for b in bets if lo <= b["amount"] < hi]
        if bucket:
            n = len(bucket)
            wins = sum(1 for b in bucket if b["win"])
            results[label] = {
                "n": n,
                "accuracy": wins / n * 100,
                "pnl": sum(b["pnl"] for b in bucket),
            }
    return results


def outcome_breakdown(bets):
    by_idx = defaultdict(list)
    for b in bets:
        by_idx[b["outcome_idx"]].append(b)
    results = {}
    for idx in sorted(by_idx.keys()):
        tb = by_idx[idx]
        n = len(tb)
        wins = sum(1 for b in tb if b["win"])
        label = "Yes (0)" if idx == 0 else "No (1)" if idx == 1 else f"Outcome {idx}"
        results[label] = {"n": n, "share": n / len(bets) * 100, "accuracy": wins / n * 100}
    return results


def print_comparison(label_a, stats_a, label_b, stats_b):
    w = 100
    print(f"\n{'=' * w}")
    print(f"{'METRIC':<30} {label_a:>30} {label_b:>30}   {'Delta':>8}")
    print(f"{'=' * w}")

    def row(name, va, vb, fmt=".1f", suffix="", higher_is_better=True):
        if va is None or vb is None:
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

    row("Bets", stats_a["n"], stats_b["n"], "d", higher_is_better=True)
    row("Accuracy %", stats_a.get("accuracy"), stats_b.get("accuracy"), ".1f", "%")
    row("PnL", stats_a.get("pnl"), stats_b.get("pnl"), ".2f", " xDAI")
    row("Avg bet size", stats_a.get("avg_bet"), stats_b.get("avg_bet"), ".4f", " xDAI", higher_is_better=False)
    row("Median bet size", stats_a.get("median_bet"), stats_b.get("median_bet"), ".4f", " xDAI", higher_is_better=False)
    row("Active agents", stats_a.get("agents"), stats_b.get("agents"), "d")
    row("Unique markets", stats_a.get("markets"), stats_b.get("markets"), "d")


def print_tool_comparison(label_a, tools_a, label_b, tools_b):
    w = 100
    all_tools = sorted(set(list(tools_a.keys()) + list(tools_b.keys())),
                       key=lambda t: tools_a.get(t, {}).get("n", 0) + tools_b.get(t, {}).get("n", 0),
                       reverse=True)

    print(f"\n{'=' * w}")
    print(f"TOOL COMPARISON: {label_a} vs {label_b}")
    print(f"{'=' * w}")
    print(f"\n  {'Tool':<36} {'Share%':>7}|{'Share%':>7} {'Acc%':>6}|{'Acc%':>6} "
          f"{'AvgBet':>8}|{'AvgBet':>8}  {'AccDelta':>9}")
    print(f"  {'':<36} {label_a[:7]:>7}|{label_b[:7]:>7} {label_a[:6]:>6}|{label_b[:6]:>6} "
          f"{label_a[:8]:>8}|{label_b[:8]:>8}")
    print("  " + "-" * 95)

    for tool in all_tools:
        if tool == "unknown":
            continue
        a = tools_a.get(tool, {"n": 0, "share": 0, "accuracy": 0, "avg_bet": 0, "pnl": 0})
        b = tools_b.get(tool, {"n": 0, "share": 0, "accuracy": 0, "avg_bet": 0, "pnl": 0})
        if a["n"] + b["n"] < 10:
            continue
        acc_delta = b["accuracy"] - a["accuracy"]
        flag = " !!" if acc_delta < -10 else " !" if acc_delta < -5 else ""
        print(f"  {tool:<36} {a['share']:>6.1f}%|{b['share']:>6.1f}% "
              f"{a['accuracy']:>5.1f}%|{b['accuracy']:>5.1f}% "
              f"{a['avg_bet']:>7.4f}|{b['avg_bet']:>7.4f}  {acc_delta:>+8.1f}pp{flag}")


def print_sizing_comparison(label_a, sizing_a, label_b, sizing_b):
    all_buckets = list(dict.fromkeys(list(sizing_a.keys()) + list(sizing_b.keys())))

    print(f"\n{'=' * 80}")
    print(f"BET SIZE COMPARISON: {label_a} vs {label_b}")
    print(f"{'=' * 80}")
    print(f"\n  {'Bucket':<14} {'N':>6}|{'N':>6} {'Acc%':>6}|{'Acc%':>6} {'AccDelta':>9}")
    print("  " + "-" * 60)

    for bucket in all_buckets:
        a = sizing_a.get(bucket, {"n": 0, "accuracy": 0})
        b = sizing_b.get(bucket, {"n": 0, "accuracy": 0})
        if a["n"] + b["n"] < 5:
            continue
        delta = b["accuracy"] - a["accuracy"] if a["n"] > 0 and b["n"] > 0 else 0
        print(f"  {bucket:<14} {a['n']:>6}|{b['n']:>6} "
              f"{a['accuracy']:>5.1f}%|{b['accuracy']:>5.1f}% {delta:>+8.1f}pp")


def print_outcome_comparison(label_a, out_a, label_b, out_b):
    print(f"\n{'=' * 80}")
    print(f"OUTCOME SIDE COMPARISON: {label_a} vs {label_b}")
    print(f"{'=' * 80}")
    print(f"\n  {'Side':<14} {'Share%':>7}|{'Share%':>7} {'Acc%':>6}|{'Acc%':>6} {'AccDelta':>9}")
    print("  " + "-" * 60)

    for side in sorted(set(list(out_a.keys()) + list(out_b.keys()))):
        a = out_a.get(side, {"n": 0, "share": 0, "accuracy": 0})
        b = out_b.get(side, {"n": 0, "share": 0, "accuracy": 0})
        delta = b["accuracy"] - a["accuracy"]
        print(f"  {side:<14} {a['share']:>6.1f}%|{b['share']:>6.1f}% "
              f"{a['accuracy']:>5.1f}%|{b['accuracy']:>5.1f}% {delta:>+8.1f}pp")


def print_agent_movers(bets_before, bets_after, direction="losers"):
    """Show agents with biggest accuracy drops or gains."""
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
        if len(ab) < 5 or len(aa) < 5:
            continue
        acc_b = sum(1 for b in ab if b["win"]) / len(ab) * 100
        acc_a = sum(1 for b in aa if b["win"]) / len(aa) * 100
        movers.append({
            "agent": agent,
            "bets_before": len(ab), "acc_before": acc_b,
            "bets_after": len(aa), "acc_after": acc_a,
            "delta": acc_a - acc_b,
        })

    if direction == "losers":
        movers.sort(key=lambda m: m["delta"])
        title = "BIGGEST ACCURACY DROPS (agents with >= 5 bets each period)"
    else:
        movers.sort(key=lambda m: m["delta"], reverse=True)
        title = "BIGGEST ACCURACY GAINS"

    print(f"\n{'=' * 80}")
    print(title)
    print(f"{'=' * 80}")
    print(f"\n  {'Agent':<14} {'Before':>8} {'AccB%':>6} {'After':>8} {'AccA%':>6} {'Delta':>8}")
    print("  " + "-" * 60)

    for m in movers[:20]:
        addr = m["agent"][:6] + ".." + m["agent"][-4:]
        print(f"  {addr:<14} {m['bets_before']:>8} {m['acc_before']:>5.1f}% "
              f"{m['bets_after']:>8} {m['acc_after']:>5.1f}% {m['delta']:>+7.1f}pp")


def print_market_analysis(bets_after):
    """Analyze what kinds of markets were bet on in the bad period."""
    market_bets = defaultdict(list)
    for b in bets_after:
        market_bets[b["market_id"]].append(b)

    # Fleet consensus: markets where most agents agreed on the same side
    consensus_wrong = []
    for mid, mb in market_bets.items():
        if len(mb) < 5:
            continue
        n = len(mb)
        wins = sum(1 for b in mb if b["win"])
        acc = wins / n * 100
        # Check if most bet the same outcome
        outcome_counts = defaultdict(int)
        for b in mb:
            outcome_counts[b["outcome_idx"]] += 1
        dominant = max(outcome_counts.values())
        consensus_pct = dominant / n * 100

        consensus_wrong.append({
            "title": mb[0]["title"][:65],
            "n": n,
            "accuracy": acc,
            "consensus": consensus_pct,
            "pnl": sum(b["pnl"] for b in mb),
            "agents": len(set(b["agent"] for b in mb)),
        })

    consensus_wrong.sort(key=lambda m: m["pnl"])

    print(f"\n{'=' * 100}")
    print(f"WORST MARKETS IN BAD PERIOD (fleet consensus failures)")
    print(f"{'=' * 100}")
    print(f"\n  {'Bets':>5} {'Ag':>3} {'Acc%':>6} {'Cons%':>6} {'PnL':>10}  Question")
    print("  " + "-" * 95)

    for m in consensus_wrong[:15]:
        print(f"  {m['n']:>5} {m['agents']:>3} {m['accuracy']:>5.1f}% {m['consensus']:>5.1f}% "
              f"{m['pnl']:>+9.4f}  {m['title']}")

    # Summary: what fraction of markets did the fleet get wrong?
    total_markets = len([m for m in consensus_wrong if m["n"] >= 5])
    wrong_markets = len([m for m in consensus_wrong if m["accuracy"] < 50])
    if total_markets > 0:
        print(f"\n  Markets with <50% accuracy: {wrong_markets}/{total_markets} "
              f"({wrong_markets/total_markets*100:.0f}%)")


def main():
    parser = argparse.ArgumentParser(description="Compare Omen fleet between two periods")
    parser.add_argument("--days", type=int, default=30, help="Total lookback days")
    parser.add_argument("--split-date", type=str, default="2026-03-16",
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

    print("[1/2] Fetching all resolved bets...", end=" ", flush=True)
    raw_bets = fetch_all_bets(since_ts)
    print(f"{len(raw_bets)} bets")

    bets = process_bets(raw_bets)
    if not bets:
        print("No bets!")
        return

    # Find agents with enough bets to matter
    agent_counts = defaultdict(int)
    for b in bets:
        agent_counts[b["agent"]] += 1
    agents_to_fetch = [a for a, c in agent_counts.items() if c >= 3]

    print(f"[2/2] Fetching mech for {len(agents_to_fetch)} agents...")
    mech_index = fetch_mech_concurrent(agents_to_fetch, mech_since_ts)

    # Match tools
    for b in bets:
        b["tool"] = match_tool(b, mech_index.get(b["agent"], []))

    fetch_time = time.time() - t0
    print(f"\nData ready in {fetch_time:.1f}s.\n")

    # Split
    before = [b for b in bets if b["ts"] < split_ts]
    after = [b for b in bets if b["ts"] >= split_ts]

    label_a = f"Before ({since_date} to {args.split_date})"
    label_b = f"After ({args.split_date} to now)"

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

    # 4. Outcome side comparison
    out_a = outcome_breakdown(before)
    out_b = outcome_breakdown(after)
    print_outcome_comparison(label_a, out_a, label_b, out_b)

    # 5. Agent movers
    print_agent_movers(before, after, direction="losers")

    # 6. Market analysis of bad period
    print_market_analysis(after)

    print(f"\nDone in {time.time() - t0:.1f}s.")


if __name__ == "__main__":
    main()
