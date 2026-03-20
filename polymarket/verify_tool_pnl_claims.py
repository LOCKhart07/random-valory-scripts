"""
Independent verification of POLYSTRAT_DIVERGENCE_REPORT claims.

Checks:
1. SF (superforcaster) is used more by high-PnL agents
2. PRR (prediction-request-reasoning) is used more by low-PnL agents
3. Fleet-wide PnL per tool (SF: +$94, PRR: -$930)
4. Fleet-wide accuracy per tool (SF: 73.4%, PRR: 63.1%)

Uses the same subgraph data sources but independent code.
"""

import os
import statistics
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
LOOKBACK = 70 * 24 * 60 * 60  # 70 days


def post(url, query, variables=None, retries=3):
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
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


# ---------------------------------------------------------------------------
# Step 1: Get all agents with bets
# ---------------------------------------------------------------------------

def get_agents():
    """Fetch all trader agents from the Polymarket bets subgraph."""
    data = post(BETS_URL, """
    {
      traderAgents(first: 1000, orderBy: totalBets, orderDirection: desc) {
        id
        totalBets
      }
    }
    """)
    return [a["id"] for a in data["traderAgents"] if int(a["totalBets"]) > 0]


# ---------------------------------------------------------------------------
# Step 2: Fetch bets for an agent
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Step 3: Fetch mech requests (tool usage) for an agent
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Step 4: Match bets to tools
# ---------------------------------------------------------------------------

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
# Step 5: Compute per-agent stats
# ---------------------------------------------------------------------------

def compute_agent_stats(bets, mech_reqs):
    resolved = [b for b in bets if b["resolved"]]
    if not resolved:
        return None

    # Match tools
    for b in resolved:
        b["tool"] = match_tool(b, mech_reqs)

    total_pnl = 0.0
    tool_counts = defaultdict(int)
    tool_wins = defaultdict(int)
    tool_total = defaultdict(int)
    tool_pnl = defaultdict(float)

    for b in resolved:
        t = b["tool"]
        tool_total[t] += 1
        if b["win"]:
            pnl = b["shares"] - b["amount"]
            tool_wins[t] += 1
        else:
            pnl = -b["amount"]
        total_pnl += pnl
        tool_pnl[t] += pnl
        tool_counts[t] += 1

    n = len(resolved)
    sf_pct = tool_counts.get("superforcaster", 0) / n * 100 if n else 0
    prr_pct = tool_counts.get("prediction-request-reasoning", 0) / n * 100 if n else 0

    prr_acc = None
    if tool_total.get("prediction-request-reasoning", 0) > 0:
        prr_acc = (
            tool_wins["prediction-request-reasoning"]
            / tool_total["prediction-request-reasoning"]
            * 100
        )

    return {
        "n_resolved": n,
        "pnl": total_pnl,
        "sf_pct": sf_pct,
        "prr_pct": prr_pct,
        "prr_acc": prr_acc,
        "tool_counts": dict(tool_counts),
        "tool_wins": dict(tool_wins),
        "tool_pnl": dict(tool_pnl),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Fetching agents...")
    agents = get_agents()
    print(f"Found {len(agents)} agents with bets")

    results = []
    fleet_tool_bets = defaultdict(int)
    fleet_tool_wins = defaultdict(int)
    fleet_tool_pnl = defaultdict(float)

    for i, agent in enumerate(agents):
        print(f"  [{i+1}/{len(agents)}] {agent[:10]}...", end=" ", flush=True)
        bets = fetch_bets(agent)
        resolved = [b for b in bets if b["resolved"]]
        if len(resolved) < 10:
            print(f"skip ({len(resolved)} resolved)")
            continue

        mech = fetch_mech(agent)
        stats = compute_agent_stats(bets, mech)
        if not stats:
            print("skip (no stats)")
            continue

        stats["agent"] = agent
        results.append(stats)

        # Accumulate fleet-wide tool stats
        for t, c in stats["tool_counts"].items():
            fleet_tool_bets[t] += c
        for t, w in stats["tool_wins"].items():
            fleet_tool_wins[t] += w
        for t, p in stats["tool_pnl"].items():
            fleet_tool_pnl[t] += p

        print(
            f"resolved={stats['n_resolved']} pnl=${stats['pnl']:+.2f} "
            f"sf={stats['sf_pct']:.1f}% prr={stats['prr_pct']:.1f}%"
        )

    if not results:
        print("No results!")
        return

    # Sort by PnL
    results.sort(key=lambda x: x["pnl"])

    print("\n" + "=" * 70)
    print("FLEET-WIDE TOOL STATS")
    print("=" * 70)
    for tool in sorted(fleet_tool_bets, key=lambda t: fleet_tool_bets[t], reverse=True):
        total = fleet_tool_bets[tool]
        wins = fleet_tool_wins.get(tool, 0)
        acc = wins / total * 100 if total else 0
        pnl = fleet_tool_pnl[tool]
        print(f"  {tool:40s} bets={total:5d}  acc={acc:5.1f}%  pnl=${pnl:+8.2f}")

    # Report claim: SF 406 bets, 73.4% acc, +$94 PnL
    # Report claim: PRR 6155 bets, 63.1% acc, -$930 PnL
    sf_bets = fleet_tool_bets.get("superforcaster", 0)
    sf_wins = fleet_tool_wins.get("superforcaster", 0)
    sf_acc = sf_wins / sf_bets * 100 if sf_bets else 0
    sf_pnl = fleet_tool_pnl.get("superforcaster", 0)

    prr_bets = fleet_tool_bets.get("prediction-request-reasoning", 0)
    prr_wins = fleet_tool_wins.get("prediction-request-reasoning", 0)
    prr_acc = prr_wins / prr_bets * 100 if prr_bets else 0
    prr_pnl = fleet_tool_pnl.get("prediction-request-reasoning", 0)

    print(f"\n{'CLAIM CHECK':^70}")
    print("-" * 70)
    print(f"{'Metric':<30} {'Report':>18} {'Verified':>18}")
    print("-" * 70)
    print(f"{'SF bets':<30} {'406':>18} {sf_bets:>18}")
    print(f"{'SF accuracy':<30} {'73.4%':>18} {sf_acc:>17.1f}%")
    print(f"{'SF PnL':<30} {'$+94':>18} ${sf_pnl:>+16.2f}")
    print(f"{'PRR bets':<30} {'6,155':>18} {prr_bets:>18,}")
    print(f"{'PRR accuracy':<30} {'63.1%':>18} {prr_acc:>17.1f}%")
    print(f"{'PRR PnL':<30} {'$-930':>18} ${prr_pnl:>+16.2f}")
    print("-" * 70)

    # Top 10 vs Bottom 10
    n = len(results)
    bottom10 = results[:10]
    top10 = results[-10:]

    def group_stats(group, label):
        avg_pnl = statistics.mean(g["pnl"] for g in group)
        avg_sf = statistics.mean(g["sf_pct"] for g in group)
        avg_prr = statistics.mean(g["prr_pct"] for g in group)
        prr_accs = [g["prr_acc"] for g in group if g["prr_acc"] is not None]
        avg_prr_acc = statistics.mean(prr_accs) if prr_accs else None

        # Longshot exposure
        longshot_pcts = []
        for g in group:
            # we don't have raw bets here, use tool_counts
            pass

        print(f"\n  {label} ({len(group)} agents):")
        print(f"    Avg PnL:           ${avg_pnl:+.2f}")
        print(f"    Avg SF usage:      {avg_sf:.1f}%")
        print(f"    Avg PRR usage:     {avg_prr:.1f}%")
        if avg_prr_acc is not None:
            print(f"    Avg PRR accuracy:  {avg_prr_acc:.1f}%")
        print(f"    PnL range:         ${group[0]['pnl']:+.2f} to ${group[-1]['pnl']:+.2f}")
        return avg_pnl, avg_sf, avg_prr_acc

    print(f"\n{'TOP 10 vs BOTTOM 10 BY PNL':^70}")
    print("=" * 70)

    _, b_sf, b_prr_acc = group_stats(bottom10, "Bottom 10 (worst PnL)")
    _, t_sf, t_prr_acc = group_stats(top10, "Top 10 (best PnL)")

    # Report claims:
    # Bottom 10: avg PnL -$58.71, SF usage 4.0%, PRR acc 60.4%
    # Top 10:    avg PnL +$16.85, SF usage 13.7%, PRR acc 67.5%
    print(f"\n{'CLAIM CHECK: TOP vs BOTTOM':^70}")
    print("-" * 70)
    print(f"{'Metric':<30} {'Report':>18} {'Verified':>18}")
    print("-" * 70)
    print(f"{'Bottom10 SF usage':<30} {'4.0%':>18} {b_sf:>17.1f}%")
    print(f"{'Top10 SF usage':<30} {'13.7%':>18} {t_sf:>17.1f}%")
    if b_prr_acc is not None:
        print(f"{'Bottom10 PRR accuracy':<30} {'60.4%':>18} {b_prr_acc:>17.1f}%")
    if t_prr_acc is not None:
        print(f"{'Top10 PRR accuracy':<30} {'67.5%':>18} {t_prr_acc:>17.1f}%")
    print("-" * 70)

    # Spearman rank correlation: SF% vs PnL
    try:
        from scipy.stats import spearmanr
        sf_vals = [r["sf_pct"] for r in results]
        pnl_vals = [r["pnl"] for r in results]
        rho, p = spearmanr(sf_vals, pnl_vals)
        print(f"\n  Spearman(SF% vs PnL): rho={rho:.3f}, p={p:.4f}")
        print(f"  Report claims: rho=-0.028")
    except ImportError:
        # Manual rank correlation
        pass

    # SF usage buckets
    print(f"\n{'SF USAGE BUCKETS':^70}")
    print("-" * 70)
    high_sf = [r for r in results if r["sf_pct"] >= 10]
    mid_sf = [r for r in results if 3 <= r["sf_pct"] < 10]
    low_sf = [r for r in results if r["sf_pct"] < 3]

    for label, group in [("High (>=10%)", high_sf), ("Mid (3-10%)", mid_sf), ("Low (<3%)", low_sf)]:
        if not group:
            print(f"  {label}: no agents")
            continue
        avg_pnl = statistics.mean(g["pnl"] for g in group)
        profitable = sum(1 for g in group if g["pnl"] > 0)
        print(
            f"  {label:15s}: {len(group):3d} agents, "
            f"avg PnL=${avg_pnl:+.2f}, "
            f"profitable={profitable}/{len(group)} ({profitable/len(group)*100:.0f}%)"
        )
    # Report claims:
    # High (>=10%): 7 agents, avg PnL -$8.89, 3/7 (43%)
    # Mid (3-10%):  20 agents, avg PnL -$15.77, 3/20 (15%)
    # Low (<3%):    45 agents, avg PnL -$15.07, 9/45 (20%)

    print("\nDone.")


if __name__ == "__main__":
    main()
