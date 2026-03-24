"""
Fast fleet-wide Omen agent analysis.

Fetches all data in bulk (2 paginated queries total instead of per-agent),
then matches and analyzes in-memory.

Queries:
  1. All resolved bets from predict-omen (has bettor, amount, outcome, market answer)
  2. All mech requests from Gnosis marketplace (has sender, tool, question title)

PnL note: losses are exact (-amount). Win payouts require per-agent participant
data which is slow to fetch. Instead, wins are estimated as +amount*(1/est_share_price - 1)
using the market's outcome token pool ratios when available, otherwise omitted.

Usage:
    python polymarket/analyze_omen_fleet_fast.py
    python polymarket/analyze_omen_fleet_fast.py --days 30 --min-bets 5
"""

import argparse
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# Bulk data fetching
# ---------------------------------------------------------------------------

def fetch_all_bets(since_ts):
    """Single paginated query for all resolved bets."""
    all_bets = []
    skip = 0
    while True:
        data = post(OMEN_BETS_URL, f"""
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
        """)
        batch = data.get("bets", [])
        if not batch:
            break
        all_bets.extend(batch)
        if len(batch) < 1000:
            break
        skip += 1000
    return all_bets


def fetch_all_pending_bets(since_ts):
    """Fetch unresolved bets for pending exposure count."""
    all_bets = []
    skip = 0
    while True:
        data = post(OMEN_BETS_URL, f"""
        {{
          bets(
            first: 1000
            skip: {skip}
            orderBy: timestamp
            orderDirection: desc
            where: {{
              timestamp_gte: {since_ts}
              fixedProductMarketMaker_: {{currentAnswer: null}}
            }}
          ) {{
            id
            timestamp
            amount
            bettor {{ id }}
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
    """Fetch mech requests for a single agent."""
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


def fetch_mech_concurrent(agents, since_ts, max_workers=10):
    """Fetch mech requests for multiple agents concurrently."""
    mech_index = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_mech_for_agent, agent, since_ts): agent
            for agent in agents
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            agent, reqs = future.result()
            mech_index[agent] = reqs
            if done % 20 == 0 or done == len(agents):
                print(f"  [{done}/{len(agents)}] mech fetches done", flush=True)
    return mech_index


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def process_bets(raw_bets):
    """Convert raw subgraph bets into analysis-ready dicts."""
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

        # PnL estimation: losses are exact, wins are approximate.
        # Without per-agent participant data we don't know exact payout.
        # Conservative estimate: win profit = amount (i.e. ~2x payout assumption).
        share_price = None
        if is_win:
            pnl = amount  # approximate: assume ~0.50 share price on average
        else:
            pnl = -amount

        question = (fpmm.get("question") or "").split(SEP)[0].strip()
        agent = bet.get("bettor", {}).get("id", "")
        service_id = bet.get("bettor", {}).get("serviceId")

        results.append({
            "bet_id": bet.get("id", ""),
            "agent": agent,
            "service_id": service_id,
            "title": question,
            "ts": int(bet.get("timestamp", 0)),
            "amount": amount,
            "share_price": share_price,
            "win": is_win,
            "pnl": pnl,
            "market_id": fpmm.get("id", ""),
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


# ---------------------------------------------------------------------------
# Analysis & output
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fast fleet-wide Omen analysis")
    parser.add_argument("--days", type=int, default=30, help="Lookback days (default: 30)")
    parser.add_argument("--min-bets", type=int, default=5, help="Min bets per tool/agent to display (default: 5)")
    parser.add_argument("--buckets", type=int, default=10, help="Price range buckets (default: 10)")
    args = parser.parse_args()

    since_ts = int(time.time()) - args.days * 86400
    mech_since_ts = since_ts - 7 * 86400  # wider window for mech matching
    since_date = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    print(f"Fast fleet-wide Omen analysis — last {args.days} days (since {since_date})")
    print(f"Fetching all data in bulk...\n")

    # ---- Bulk fetches (just 3 queries, each paginated) ----
    t0 = time.time()

    print("[1/3] Fetching all resolved bets...", end=" ", flush=True)
    raw_bets = fetch_all_bets(since_ts)
    print(f"{len(raw_bets)} bets")

    print("[2/3] Fetching pending bets...", end=" ", flush=True)
    raw_pending = fetch_all_pending_bets(since_ts)
    print(f"{len(raw_pending)} pending")

    # ---- Process bets first to find agents worth fetching mech for ----
    bets = process_bets(raw_bets)
    if not bets:
        print("No resolved bets found!")
        return

    # Count bets per agent, only fetch mech for agents with >= 3 bets
    agent_bet_counts = defaultdict(int)
    for b in bets:
        agent_bet_counts[b["agent"]] += 1
    agents_to_fetch = [a for a, c in agent_bet_counts.items() if c >= 3]

    print(f"[3/3] Fetching mech requests for {len(agents_to_fetch)} agents (>= 3 bets) concurrently...")
    mech_index = fetch_mech_concurrent(agents_to_fetch, mech_since_ts, max_workers=15)

    fetch_time = time.time() - t0
    print(f"\nData fetched in {fetch_time:.1f}s. Processing...\n")

    # Match tools
    matched_count = 0
    for b in bets:
        agent_mech = mech_index.get(b["agent"], [])
        b["tool"] = match_tool(b, agent_mech)
        if b["tool"] != "unknown":
            matched_count += 1

    agents = set(b["agent"] for b in bets)
    total_bets = len(bets)
    total_invested = sum(b["amount"] for b in bets)
    total_pnl = sum(b["pnl"] for b in bets)
    total_wins = sum(1 for b in bets if b["win"])
    fleet_acc = total_wins / total_bets * 100
    fleet_roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    pending_exposure = sum(float(b["amount"]) / WEI_DIV for b in raw_pending)

    w = 100

    # =========================================================================
    # FLEET SUMMARY
    # =========================================================================
    print(f"{'=' * w}")
    print(f"OMEN FLEET SUMMARY — LAST {args.days} DAYS")
    print(f"{'=' * w}")
    print(f"\n  Agents:            {len(agents)}")
    print(f"  Resolved bets:     {total_bets}")
    print(f"  Tool match rate:   {matched_count}/{total_bets} ({matched_count/total_bets*100:.0f}%)")
    print(f"  Fleet accuracy:    {fleet_acc:.1f}%")
    print(f"  Total invested:    {total_invested:,.4f} xDAI")
    print(f"  Fleet PnL:         {total_pnl:+,.4f} xDAI  (win PnL estimated from pool ratios)")
    print(f"  Fleet ROI:         {fleet_roi:+.1f}%")
    print(f"  Pending:           {len(raw_pending)} bets, {pending_exposure:,.4f} xDAI exposure")

    # =========================================================================
    # TOOL PROFITABILITY
    # =========================================================================
    tool_bets = defaultdict(list)
    for b in bets:
        tool_bets[b["tool"]].append(b)

    tool_stats = []
    for tool, tb in tool_bets.items():
        n = len(tb)
        wins = sum(1 for b in tb if b["win"])
        inv = sum(b["amount"] for b in tb)
        pnl = sum(b["pnl"] for b in tb)
        roi = (pnl / inv * 100) if inv > 0 else 0
        avg_bet = statistics.mean(b["amount"] for b in tb)
        n_agents = len(set(b["agent"] for b in tb))
        tool_stats.append({
            "tool": tool, "bets": n, "wins": wins, "losses": n - wins,
            "accuracy": wins / n * 100 if n else 0,
            "total_invested": inv, "pnl": pnl, "roi": roi,
            "avg_bet": avg_bet, "n_agents": n_agents,
        })

    tool_stats.sort(key=lambda t: t["bets"], reverse=True)

    print(f"\n{'=' * w}")
    print(f"TOOL PROFITABILITY (min {args.min_bets} bets)")
    print(f"{'=' * w}")
    print(f"\n  {'Tool':<40} {'Bets':>6} {'Acc%':>6} {'PnL (xDAI)':>12} {'ROI%':>7} {'AvgBet':>8} {'Agents':>7}")
    print("  " + "-" * 92)

    for t in tool_stats:
        if t["bets"] < args.min_bets:
            continue
        print(f"  {t['tool']:<40} {t['bets']:>6} {t['accuracy']:>5.1f}% "
              f"{t['pnl']:>+11.4f} {t['roi']:>+6.1f}% "
              f"{t['avg_bet']:>7.4f} {t['n_agents']:>7}")

    # =========================================================================
    # AGENT LEADERBOARD
    # =========================================================================
    agent_bets = defaultdict(list)
    for b in bets:
        agent_bets[b["agent"]].append(b)

    agent_stats = []
    for agent, ab in agent_bets.items():
        n = len(ab)
        wins = sum(1 for b in ab if b["win"])
        inv = sum(b["amount"] for b in ab)
        pnl = sum(b["pnl"] for b in ab)
        roi = (pnl / inv * 100) if inv > 0 else 0
        # Primary tool
        tool_counts = defaultdict(int)
        for b in ab:
            tool_counts[b["tool"]] += 1
        primary_tool = max(tool_counts, key=tool_counts.get)
        svc = ab[0].get("service_id") or "?"
        agent_stats.append({
            "agent": agent, "service_id": svc, "bets": n, "wins": wins,
            "accuracy": wins / n * 100 if n else 0,
            "invested": inv, "pnl": pnl, "roi": roi,
            "primary_tool": primary_tool,
        })

    agent_stats.sort(key=lambda a: a["pnl"], reverse=True)

    print(f"\n{'=' * w}")
    print(f"AGENT LEADERBOARD (by PnL)")
    print(f"{'=' * w}")
    print(f"\n  {'Agent':<14} {'Svc':>5} {'Bets':>5} {'Acc%':>6} {'Invested':>10} "
          f"{'PnL':>12} {'ROI%':>7}  {'Primary Tool'}")
    print("  " + "-" * 92)

    for a in agent_stats:
        if a["bets"] < 3:
            continue
        addr = a["agent"][:6] + ".." + a["agent"][-4:]
        print(f"  {addr:<14} {a['service_id']:>5} {a['bets']:>5} {a['accuracy']:>5.1f}% "
              f"{a['invested']:>9.4f} {a['pnl']:>+11.4f} {a['roi']:>+6.1f}%  "
              f"{a['primary_tool']}")

    # =========================================================================
    # BET SIZE PROFITABILITY
    # =========================================================================
    print(f"\n{'=' * w}")
    print(f"BET SIZE PROFITABILITY")
    print(f"{'=' * w}")

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

    print(f"\n  {'Bet Size':<16} {'Bets':>6} {'Wins':>6} {'Acc%':>6} "
          f"{'Invested':>12} {'PnL':>12} {'ROI%':>7}")
    print("  " + "-" * 75)

    for lo, hi, label in size_buckets:
        bucket = [b for b in bets if lo <= b["amount"] < hi]
        if not bucket:
            continue
        n = len(bucket)
        wins = sum(1 for b in bucket if b["win"])
        acc = wins / n * 100
        inv = sum(b["amount"] for b in bucket)
        pnl = sum(b["pnl"] for b in bucket)
        roi = (pnl / inv * 100) if inv > 0 else 0
        print(f"  {label:<16} {n:>6} {wins:>6} {acc:>5.1f}% "
              f"{inv:>11.4f} {pnl:>+11.4f} {roi:>+6.1f}%")

    # =========================================================================
    # WEEKLY TRENDS
    # =========================================================================
    weekly = defaultdict(lambda: {"bets": 0, "wins": 0, "invested": 0, "pnl": 0, "agents": set()})
    for b in bets:
        dt = datetime.fromtimestamp(b["ts"], tz=timezone.utc)
        week = dt.strftime("%Y-W%W")
        weekly[week]["bets"] += 1
        weekly[week]["invested"] += b["amount"]
        weekly[week]["pnl"] += b["pnl"]
        weekly[week]["agents"].add(b["agent"])
        if b["win"]:
            weekly[week]["wins"] += 1

    print(f"\n{'=' * w}")
    print(f"WEEKLY TRENDS")
    print(f"{'=' * w}")
    print(f"\n  {'Week':<10} {'Bets':>6} {'Acc%':>6} {'Invested':>12} {'PnL':>12} {'ROI%':>7} {'Agents':>7}")
    print("  " + "-" * 70)

    for week in sorted(weekly.keys()):
        s = weekly[week]
        acc = s["wins"] / s["bets"] * 100 if s["bets"] else 0
        roi = (s["pnl"] / s["invested"] * 100) if s["invested"] > 0 else 0
        print(f"  {week:<10} {s['bets']:>6} {acc:>5.1f}% "
              f"{s['invested']:>11.4f} {s['pnl']:>+11.4f} {roi:>+6.1f}% "
              f"{len(s['agents']):>7}")

    # =========================================================================
    # MARKET ANALYSIS — most bet-on questions
    # =========================================================================
    market_bets = defaultdict(list)
    for b in bets:
        market_bets[b["market_id"]].append(b)

    market_stats = []
    for mid, mb in market_bets.items():
        n = len(mb)
        wins = sum(1 for b in mb if b["win"])
        pnl = sum(b["pnl"] for b in mb)
        n_agents = len(set(b["agent"] for b in mb))
        title = mb[0]["title"][:60]
        market_stats.append({
            "market_id": mid, "title": title, "bets": n,
            "wins": wins, "accuracy": wins / n * 100, "pnl": pnl,
            "n_agents": n_agents,
        })

    market_stats.sort(key=lambda m: m["bets"], reverse=True)

    print(f"\n{'=' * w}")
    print(f"TOP MARKETS BY BET COUNT (top 20)")
    print(f"{'=' * w}")
    print(f"\n  {'Bets':>5} {'Ag':>3} {'Acc%':>6} {'PnL':>10}  Question")
    print("  " + "-" * 90)

    for m in market_stats[:20]:
        print(f"  {m['bets']:>5} {m['n_agents']:>3} {m['accuracy']:>5.1f}% "
              f"{m['pnl']:>+9.4f}  {m['title']}")

    # Worst markets by PnL
    worst_markets = sorted(market_stats, key=lambda m: m["pnl"])[:10]
    print(f"\n{'=' * w}")
    print(f"WORST MARKETS BY PnL (top 10 losers)")
    print(f"{'=' * w}")
    print(f"\n  {'Bets':>5} {'Ag':>3} {'Acc%':>6} {'PnL':>10}  Question")
    print("  " + "-" * 90)

    for m in worst_markets:
        print(f"  {m['bets']:>5} {m['n_agents']:>3} {m['accuracy']:>5.1f}% "
              f"{m['pnl']:>+9.4f}  {m['title']}")

    # =========================================================================
    # KEY INSIGHTS
    # =========================================================================
    significant_tools = [t for t in tool_stats if t["bets"] >= args.min_bets]

    print(f"\n{'=' * w}")
    print(f"KEY INSIGHTS")
    print(f"{'=' * w}")

    if significant_tools:
        best_tool = max(significant_tools, key=lambda t: t["roi"])
        worst_tool = min(significant_tools, key=lambda t: t["roi"])
        most_used = max(significant_tools, key=lambda t: t["bets"])
        print(f"\n  Best ROI tool:   {best_tool['tool']} "
              f"(ROI={best_tool['roi']:+.1f}%, acc={best_tool['accuracy']:.1f}%, n={best_tool['bets']})")
        print(f"  Worst ROI tool:  {worst_tool['tool']} "
              f"(ROI={worst_tool['roi']:+.1f}%, acc={worst_tool['accuracy']:.1f}%, n={worst_tool['bets']})")
        print(f"  Most used tool:  {most_used['tool']} ({most_used['bets']} bets)")

    if agent_stats:
        best_agent = agent_stats[0]
        worst_agent = agent_stats[-1]
        print(f"\n  Best agent:      {best_agent['agent'][:10]}... "
              f"(PnL={best_agent['pnl']:+.4f}, acc={best_agent['accuracy']:.1f}%, n={best_agent['bets']})")
        print(f"  Worst agent:     {worst_agent['agent'][:10]}... "
              f"(PnL={worst_agent['pnl']:+.4f}, acc={worst_agent['accuracy']:.1f}%, n={worst_agent['bets']})")

    print(f"\n  Fleet accuracy:  {fleet_acc:.1f}%")
    print(f"  Fleet ROI:       {fleet_roi:+.1f}%")
    print(f"  Fleet PnL:       {total_pnl:+,.4f} xDAI")

    total_time = time.time() - t0
    print(f"\n  Total time: {total_time:.1f}s")
    print(f"\nDone.")


if __name__ == "__main__":
    main()
