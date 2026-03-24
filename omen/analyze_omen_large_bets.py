"""
Drill into large vs small bet accuracy divergence.

Compares micro-bets (<0.05 xDAI) to larger bets (>=0.05 xDAI) in both periods,
breaking down by: tool, agent overlap, market overlap, outcome side.

Usage:
    python polymarket/analyze_omen_large_bets.py
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
            "service_id": bet.get("bettor", {}).get("serviceId"),
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


def acc(bets):
    if not bets:
        return 0
    return sum(1 for b in bets if b["win"]) / len(bets) * 100


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--split-date", type=str, default="2026-03-16")
    parser.add_argument("--threshold", type=float, default=0.05,
                        help="xDAI threshold for small vs large (default: 0.05)")
    args = parser.parse_args()

    since_ts = int(time.time()) - args.days * 86400
    split_dt = datetime.strptime(args.split_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    split_ts = int(split_dt.timestamp())
    mech_since_ts = since_ts - 7 * 86400
    thr = args.threshold

    print(f"Analyzing large (>={thr}) vs small (<{thr}) bet divergence")
    print(f"Split date: {args.split_date}\n")

    t0 = time.time()
    print("[1/2] Fetching bets...", end=" ", flush=True)
    raw_bets = fetch_all_bets(since_ts)
    print(f"{len(raw_bets)}")

    bets = process_bets(raw_bets)
    agent_counts = defaultdict(int)
    for b in bets:
        agent_counts[b["agent"]] += 1

    print(f"[2/2] Fetching mech for agents with large bets...")
    # Only fetch mech for agents that placed large bets
    large_bet_agents = set(b["agent"] for b in bets if b["amount"] >= thr)
    # Also include agents with >= 3 bets total for context
    agents_to_fetch = list(large_bet_agents | {a for a, c in agent_counts.items() if c >= 3})
    mech_index = fetch_mech_concurrent(agents_to_fetch, mech_since_ts)

    for b in bets:
        b["tool"] = match_tool(b, mech_index.get(b["agent"], []))

    print(f"\nReady in {time.time() - t0:.1f}s.\n")

    # Split by period and size
    before = [b for b in bets if b["ts"] < split_ts]
    after = [b for b in bets if b["ts"] >= split_ts]

    before_small = [b for b in before if b["amount"] < thr]
    before_large = [b for b in before if b["amount"] >= thr]
    after_small = [b for b in after if b["amount"] < thr]
    after_large = [b for b in after if b["amount"] >= thr]

    w = 100

    # =========================================================================
    # 1. OVERVIEW
    # =========================================================================
    print(f"{'=' * w}")
    print(f"OVERVIEW: SMALL (<{thr}) vs LARGE (>={thr}) BETS")
    print(f"{'=' * w}")
    print(f"\n  {'Category':<30} {'N':>7} {'Acc%':>7} {'AvgBet':>10} {'PnL':>12}")
    print("  " + "-" * 70)
    for label, subset in [
        ("Before / Small", before_small),
        ("Before / Large", before_large),
        ("After / Small", after_small),
        ("After / Large", after_large),
    ]:
        n = len(subset)
        a = acc(subset)
        avg = statistics.mean(b["amount"] for b in subset) if subset else 0
        pnl = sum(b["pnl"] for b in subset)
        print(f"  {label:<30} {n:>7} {a:>6.1f}% {avg:>9.4f} {pnl:>+11.4f}")

    print(f"\n  Accuracy deltas:")
    print(f"    Small bets:  {acc(before_small):.1f}% -> {acc(after_small):.1f}%  ({acc(after_small)-acc(before_small):+.1f}pp)")
    print(f"    Large bets:  {acc(before_large):.1f}% -> {acc(after_large):.1f}%  ({acc(after_large)-acc(before_large):+.1f}pp)")

    # =========================================================================
    # 2. WHO PLACES LARGE BETS?
    # =========================================================================
    print(f"\n{'=' * w}")
    print(f"WHO PLACES LARGE BETS (>= {thr} xDAI)?")
    print(f"{'=' * w}")

    # Agents placing large bets in after period
    large_after_agents = defaultdict(list)
    for b in after_large:
        large_after_agents[b["agent"]].append(b)

    small_after_agents = defaultdict(list)
    for b in after_small:
        small_after_agents[b["agent"]].append(b)

    print(f"\n  Agents with large bets (after period): {len(large_after_agents)}")
    print(f"  Agents with only small bets (after):   {len(set(small_after_agents.keys()) - set(large_after_agents.keys()))}")

    # Do large-bet agents also place small bets? Compare their accuracy on each.
    print(f"\n  For agents who place BOTH large and small bets in the after period:")
    both_agents = set(large_after_agents.keys()) & set(small_after_agents.keys())
    if both_agents:
        both_large = [b for b in after_large if b["agent"] in both_agents]
        both_small = [b for b in after_small if b["agent"] in both_agents]
        print(f"    {len(both_agents)} agents place both")
        print(f"    Their small bet accuracy:  {acc(both_small):.1f}% (n={len(both_small)})")
        print(f"    Their large bet accuracy:  {acc(both_large):.1f}% (n={len(both_large)})")

    # Are large bets from a few agents or spread out?
    print(f"\n  Top large-bet agents (after period):")
    print(f"  {'Agent':<14} {'Svc':>5} {'LargeBets':>10} {'LargeAcc%':>10} {'SmallBets':>10} {'SmallAcc%':>10} {'AvgLarge':>10}")
    print("  " + "-" * 80)

    sorted_large = sorted(large_after_agents.items(), key=lambda x: len(x[1]), reverse=True)
    for agent, lb in sorted_large[:20]:
        sb = small_after_agents.get(agent, [])
        addr = agent[:6] + ".." + agent[-4:]
        svc = lb[0].get("service_id", "?")
        avg_l = statistics.mean(b["amount"] for b in lb)
        print(f"  {addr:<14} {svc:>5} {len(lb):>10} {acc(lb):>9.1f}% "
              f"{len(sb):>10} {acc(sb):>9.1f}% {avg_l:>9.4f}")

    # =========================================================================
    # 3. TOOLS ON LARGE BETS
    # =========================================================================
    print(f"\n{'=' * w}")
    print(f"TOOL USAGE: LARGE BETS ONLY")
    print(f"{'=' * w}")

    def tool_table(label, subset):
        by_tool = defaultdict(list)
        for b in subset:
            by_tool[b["tool"]].append(b)
        print(f"\n  {label}:")
        print(f"  {'Tool':<40} {'N':>5} {'Acc%':>6} {'AvgBet':>10} {'PnL':>12}")
        print("  " + "-" * 80)
        for tool, tb in sorted(by_tool.items(), key=lambda x: len(x[1]), reverse=True):
            if len(tb) < 3:
                continue
            avg = statistics.mean(b["amount"] for b in tb)
            pnl = sum(b["pnl"] for b in tb)
            print(f"  {tool:<40} {len(tb):>5} {acc(tb):>5.1f}% {avg:>9.4f} {pnl:>+11.4f}")

    tool_table("Before period — large bets", before_large)
    tool_table("After period — large bets", after_large)

    # Compare same tool, large vs small, after period
    print(f"\n  Same tool, large vs small (after period):")
    after_by_tool_large = defaultdict(list)
    after_by_tool_small = defaultdict(list)
    for b in after_large:
        after_by_tool_large[b["tool"]].append(b)
    for b in after_small:
        after_by_tool_small[b["tool"]].append(b)

    print(f"  {'Tool':<40} {'SmallN':>7} {'SmallAcc':>9} {'LargeN':>7} {'LargeAcc':>9} {'Gap':>8}")
    print("  " + "-" * 85)
    all_tools = sorted(set(list(after_by_tool_large.keys()) + list(after_by_tool_small.keys())),
                       key=lambda t: len(after_by_tool_large.get(t, [])), reverse=True)
    for tool in all_tools:
        sl = after_by_tool_small.get(tool, [])
        ll = after_by_tool_large.get(tool, [])
        if len(sl) < 5 or len(ll) < 3:
            continue
        gap = acc(ll) - acc(sl)
        print(f"  {tool:<40} {len(sl):>7} {acc(sl):>8.1f}% {len(ll):>7} {acc(ll):>8.1f}% {gap:>+7.1f}pp")

    # =========================================================================
    # 4. MARKET OVERLAP — same market, different bet sizes
    # =========================================================================
    print(f"\n{'=' * w}")
    print(f"SAME MARKET, DIFFERENT BET SIZES (after period)")
    print(f"{'=' * w}")

    after_markets_large = defaultdict(list)
    after_markets_small = defaultdict(list)
    for b in after_large:
        after_markets_large[b["market_id"]].append(b)
    for b in after_small:
        after_markets_small[b["market_id"]].append(b)

    overlap_markets = set(after_markets_large.keys()) & set(after_markets_small.keys())
    print(f"\n  Markets with both large and small bets: {len(overlap_markets)}")

    if overlap_markets:
        overlap_large = [b for b in after_large if b["market_id"] in overlap_markets]
        overlap_small = [b for b in after_small if b["market_id"] in overlap_markets]
        print(f"  On those markets:")
        print(f"    Small bets accuracy: {acc(overlap_small):.1f}% (n={len(overlap_small)})")
        print(f"    Large bets accuracy: {acc(overlap_large):.1f}% (n={len(overlap_large)})")

    # Markets with ONLY large bets
    only_large_markets = set(after_markets_large.keys()) - set(after_markets_small.keys())
    only_large_bets = [b for b in after_large if b["market_id"] in only_large_markets]
    if only_large_bets:
        print(f"\n  Markets with ONLY large bets: {len(only_large_markets)}")
        print(f"    Accuracy: {acc(only_large_bets):.1f}% (n={len(only_large_bets)})")

    # =========================================================================
    # 5. OUTCOME SIDE BY BET SIZE
    # =========================================================================
    print(f"\n{'=' * w}")
    print(f"OUTCOME SIDE BY BET SIZE (after period)")
    print(f"{'=' * w}")

    for label, subset in [("Small (<0.05)", after_small), ("Large (>=0.05)", after_large)]:
        yes = [b for b in subset if b["outcome_idx"] == 0]
        no = [b for b in subset if b["outcome_idx"] == 1]
        print(f"\n  {label}:")
        print(f"    Yes bets: {len(yes):>5} ({len(yes)/len(subset)*100:.1f}%)  accuracy={acc(yes):.1f}%")
        print(f"    No bets:  {len(no):>5} ({len(no)/len(subset)*100:.1f}%)  accuracy={acc(no):.1f}%")

    # =========================================================================
    # 6. FINER BET SIZE BUCKETS IN AFTER PERIOD
    # =========================================================================
    print(f"\n{'=' * w}")
    print(f"FINE-GRAINED BET SIZE ACCURACY (after period only)")
    print(f"{'=' * w}")

    fine_buckets = [
        (0, 0.025, "< 0.025"),
        (0.025, 0.026, "= 0.025 (min)"),
        (0.026, 0.05, "0.026-0.05"),
        (0.05, 0.10, "0.05-0.10"),
        (0.10, 0.25, "0.10-0.25"),
        (0.25, 0.50, "0.25-0.50"),
        (0.50, 1.00, "0.50-1.00"),
        (1.00, 1.50, "1.00-1.50"),
        (1.50, 2.01, "1.50-2.00"),
        (2.01, 5.00, "2.01-5.00"),
        (5.0, float("inf"), "> 5.00"),
    ]

    print(f"\n  {'Bucket':<16} {'N':>6} {'Wins':>6} {'Acc%':>6} {'PnL':>12} {'Agents':>7} {'NoSide%':>8}")
    print("  " + "-" * 70)

    for lo, hi, label in fine_buckets:
        bucket = [b for b in after if lo <= b["amount"] < hi]
        if not bucket:
            continue
        n = len(bucket)
        wins = sum(1 for b in bucket if b["win"])
        pnl = sum(b["pnl"] for b in bucket)
        agents = len(set(b["agent"] for b in bucket))
        no_side = sum(1 for b in bucket if b["outcome_idx"] == 1) / n * 100
        print(f"  {label:<16} {n:>6} {wins:>6} {wins/n*100:>5.1f}% "
              f"{pnl:>+11.4f} {agents:>7} {no_side:>7.1f}%")

    # =========================================================================
    # 7. LARGE BET SAMPLE — worst losses
    # =========================================================================
    print(f"\n{'=' * w}")
    print(f"WORST LARGE BET LOSSES (after period, top 15)")
    print(f"{'=' * w}")
    print(f"\n  {'Date':<12} {'Amount':>8} {'Side':>4} {'Tool':<35} Question")
    print("  " + "-" * 95)

    worst = sorted([b for b in after_large if not b["win"]], key=lambda b: b["amount"], reverse=True)
    for b in worst[:15]:
        dt = datetime.fromtimestamp(b["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        side = "Yes" if b["outcome_idx"] == 0 else "No"
        title = b["title"][:40] + "..." if len(b["title"]) > 40 else b["title"]
        print(f"  {dt:<12} {b['amount']:>7.4f} {side:>4} {b['tool']:<35} {title}")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
