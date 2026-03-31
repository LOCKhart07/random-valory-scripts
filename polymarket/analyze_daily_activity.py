"""
Daily betting activity for the PolyStrat fleet.

Shows per-day: bets placed, avg/median bet size, avg share price, active agents,
and unique markets. Useful for spotting changes after a deploy.

Usage:
    python polymarket/analyze_daily_activity.py
    python polymarket/analyze_daily_activity.py --days 14
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


def fetch_all_agents():
    data = post(POLYMARKET_BETS_URL, """
    {
      traderAgents(first: 1000, orderBy: totalBets, orderDirection: desc) {
        id
      }
    }
    """)
    return [a["id"] for a in data.get("traderAgents", [])]


def fetch_agent_bets(agent_id, since_ts):
    data = post(POLYMARKET_BETS_URL, """
    query($id: ID!) {
      marketParticipants(
        where: {traderAgent_: {id: $id}}
        first: 1000
        orderBy: blockTimestamp
        orderDirection: desc
      ) {
        bets {
          outcomeIndex
          amount
          shares
          blockTimestamp
          question { id }
        }
      }
    }
    """, {"id": agent_id})

    bets = []
    for p in (data or {}).get("marketParticipants", []):
        for bet in p.get("bets", []):
            if int(bet.get("blockTimestamp", 0)) >= since_ts:
                bets.append({**bet, "_agent": agent_id})
    return agent_id, bets


def main():
    parser = argparse.ArgumentParser(description="Daily PolyStrat betting activity")
    parser.add_argument("--days", type=int, default=14, help="Lookback days (default: 14)")
    args = parser.parse_args()

    since_ts = int(time.time()) - args.days * 86400
    since_date = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    print(f"Fetching bets since {since_date} ({args.days} days)...\n")

    t0 = time.time()

    agent_ids = fetch_all_agents()
    print(f"Found {len(agent_ids)} agents. Fetching bets...")

    all_bets = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futures = {pool.submit(fetch_agent_bets, aid, since_ts): aid for aid in agent_ids}
        done = 0
        for future in as_completed(futures):
            done += 1
            _, bets = future.result()
            all_bets.extend(bets)
            if done % 20 == 0 or done == len(agent_ids):
                print(f"  [{done}/{len(agent_ids)}]", flush=True)

    print(f"\n{len(all_bets)} bets fetched in {time.time() - t0:.1f}s.\n")

    # Group by day
    by_day = defaultdict(list)
    for bet in all_bets:
        ts = int(bet.get("blockTimestamp", 0))
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day].append(bet)

    # Print daily table
    print(f"  {'Date':<12} {'Bets':>6} {'Agents':>7} {'Markets':>8} "
          f"{'AvgBet$':>9} {'MedBet$':>9} {'TotalInv$':>11} {'AvgShareP':>10}")
    print("  " + "-" * 85)

    total_bets = 0
    for day in sorted(by_day.keys()):
        db = by_day[day]
        amounts = [int(b.get("amount", 0)) / USDC_DIV for b in db]
        shares_list = [int(b.get("shares", 0)) / USDC_DIV for b in db]
        share_prices = [
            a / s for a, s in zip(amounts, shares_list) if s > 0
        ]
        agents = len(set(b["_agent"] for b in db))
        markets = len(set((b.get("question") or {}).get("id", "") for b in db))
        avg_bet = statistics.mean(amounts) if amounts else 0
        med_bet = statistics.median(amounts) if amounts else 0
        total_inv = sum(amounts)
        avg_sp = statistics.mean(share_prices) if share_prices else 0
        total_bets += len(db)

        print(f"  {day:<12} {len(db):>6} {agents:>7} {markets:>8} "
              f"${avg_bet:>7.2f} ${med_bet:>7.2f} ${total_inv:>9.2f} {avg_sp:>10.4f}")

    print("  " + "-" * 85)
    print(f"  {'TOTAL':<12} {total_bets:>6}")
    print(f"\nDone in {time.time() - t0:.1f}s.")


if __name__ == "__main__":
    main()
