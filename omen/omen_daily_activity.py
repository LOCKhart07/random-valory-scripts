"""
Daily betting activity for the Omen (predict-omen) fleet.

Shows per-day: bets placed, avg/median bet size, active agents,
unique markets, accuracy, and PnL.

Usage:
    python omen/omen_daily_activity.py
    python omen/omen_daily_activity.py --days 31
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
            where: {{ timestamp_gte: {since_ts} }}
          ) {{
            id timestamp amount feeAmount outcomeIndex
            bettor {{ id }}
            fixedProductMarketMaker {{ id currentAnswer question }}
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
        if skip % 5000 == 0:
            print(f"  [{skip} fetched]", flush=True)
    return all_bets


def main():
    parser = argparse.ArgumentParser(description="Daily Omen betting activity")
    parser.add_argument("--days", type=int, default=31, help="Lookback days (default: 31)")
    args = parser.parse_args()

    since_ts = int(time.time()) - args.days * 86400
    since_date = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    print(f"Fetching bets since {since_date} ({args.days} days)...\n")

    t0 = time.time()
    raw_bets = fetch_all_bets(since_ts)
    print(f"\n{len(raw_bets)} total bets fetched in {time.time() - t0:.1f}s.\n")

    # Process and group by day
    by_day = defaultdict(list)
    for bet in raw_bets:
        ts = int(bet.get("timestamp", 0))
        fpmm = bet.get("fixedProductMarketMaker") or {}
        ca = fpmm.get("currentAnswer")
        amount = float(bet.get("amount", 0)) / WEI_DIV
        agent = bet.get("bettor", {}).get("id", "")
        market_id = fpmm.get("id", "")
        outcome_idx = int(bet.get("outcomeIndex", 0))

        resolved = ca is not None and ca != INVALID_ANSWER
        is_win = None
        if resolved:
            correct_outcome = int(ca, 16)
            is_win = outcome_idx == correct_outcome

        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day].append({
            "amount": amount,
            "agent": agent,
            "market_id": market_id,
            "win": is_win,
            "resolved": resolved,
        })

    print(f"  {'Date':<12} {'Bets':>6} {'Agents':>7} {'Markets':>8} "
          f"{'AvgBet':>10} {'MedBet':>10} {'TotalInv':>12} {'Resolved':>9} {'Acc%':>6}")
    print("  " + "-" * 95)

    total_bets = 0
    total_resolved = 0
    total_wins = 0
    for day in sorted(by_day.keys()):
        db = by_day[day]
        amounts = [b["amount"] for b in db]
        agents = len(set(b["agent"] for b in db))
        markets = len(set(b["market_id"] for b in db))
        avg_bet = statistics.mean(amounts) if amounts else 0
        med_bet = statistics.median(amounts) if amounts else 0
        total_inv = sum(amounts)
        resolved = [b for b in db if b["resolved"]]
        wins = sum(1 for b in resolved if b["win"])
        acc = (wins / len(resolved) * 100) if resolved else 0
        total_bets += len(db)
        total_resolved += len(resolved)
        total_wins += wins

        print(f"  {day:<12} {len(db):>6} {agents:>7} {markets:>8} "
              f"{avg_bet:>9.4f}x {med_bet:>9.4f}x {total_inv:>10.2f}x "
              f"{len(resolved):>9} {acc:>5.1f}%")

    print("  " + "-" * 95)
    overall_acc = (total_wins / total_resolved * 100) if total_resolved else 0
    print(f"  {'TOTAL':<12} {total_bets:>6} {'':>7} {'':>8} "
          f"{'':>10} {'':>10} {'':>12} {total_resolved:>9} {overall_acc:>5.1f}%")
    print(f"\nDone in {time.time() - t0:.1f}s.")


if __name__ == "__main__":
    main()
