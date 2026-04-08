"""
Yes vs No ratio analysis for Omen markets.

Shows:
- Market resolution ratio (how many resolved Yes vs No)
- Bet placement ratio (how many bets placed on Yes vs No)
- Volume ratio (xDAI wagered on Yes vs No)

Usage:
    python omen/omen_yes_no_ratio.py
    python omen/omen_yes_no_ratio.py --days 90
"""

import argparse
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


def post(url, query, retries=4):
    payload = {"query": query}
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
            where: {{ timestamp_gte: {since_ts}, fixedProductMarketMaker_: {{ currentAnswer_not: null }} }}
          ) {{
            id timestamp amount feeAmount outcomeIndex
            bettor {{ id }}
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
        if skip % 5000 == 0:
            print(f"  [{skip} bets fetched]", flush=True)
    return all_bets


def main():
    parser = argparse.ArgumentParser(description="Omen Yes/No ratio analysis")
    parser.add_argument("--days", type=int, default=31, help="Lookback days (default: 31)")
    args = parser.parse_args()

    now = int(datetime.now(timezone.utc).timestamp())
    since = now - args.days * 86400
    since_dt = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%d")

    print(f"Fetching resolved bets since {since_dt} ({args.days} days)...")
    bets = fetch_all_bets(since)
    print(f"Total resolved bets: {len(bets)}")

    # --- Market resolution ratio ---
    markets = {}  # fpmm_id -> currentAnswer
    for b in bets:
        fpmm = b["fixedProductMarketMaker"]
        fpmm_id = fpmm["id"]
        if fpmm_id not in markets:
            ca = fpmm.get("currentAnswer")
            if ca and ca != INVALID_ANSWER:
                markets[fpmm_id] = int(ca, 16)

    resolved_yes = sum(1 for v in markets.values() if v == 0)
    resolved_no = sum(1 for v in markets.values() if v == 1)
    resolved_other = sum(1 for v in markets.values() if v not in (0, 1))
    total_markets = len(markets)

    print(f"\n{'='*50}")
    print(f"MARKET RESOLUTION RATIO (last {args.days} days)")
    print(f"{'='*50}")
    print(f"Total resolved markets: {total_markets}")
    print(f"  Resolved YES: {resolved_yes} ({100*resolved_yes/total_markets:.1f}%)" if total_markets else "  No markets")
    print(f"  Resolved NO:  {resolved_no} ({100*resolved_no/total_markets:.1f}%)" if total_markets else "")
    if resolved_other:
        print(f"  Other:        {resolved_other} ({100*resolved_other/total_markets:.1f}%)")
    if resolved_yes:
        print(f"  YES:NO ratio: {resolved_yes/resolved_no:.2f}:1" if resolved_no else "  YES:NO ratio: all YES")
    elif resolved_no:
        print(f"  YES:NO ratio: 0:{resolved_no} (all NO)")

    # --- Bet placement ratio ---
    yes_bets = [b for b in bets if int(b.get("outcomeIndex", 0)) == 0]
    no_bets = [b for b in bets if int(b.get("outcomeIndex", 0)) == 1]

    yes_volume = sum(int(b["amount"]) for b in yes_bets) / WEI_DIV
    no_volume = sum(int(b["amount"]) for b in no_bets) / WEI_DIV

    print(f"\n{'='*50}")
    print(f"BET PLACEMENT RATIO (last {args.days} days)")
    print(f"{'='*50}")
    print(f"Total bets: {len(bets)}")
    print(f"  YES bets: {len(yes_bets)} ({100*len(yes_bets)/len(bets):.1f}%)" if bets else "  No bets")
    print(f"  NO bets:  {len(no_bets)} ({100*len(no_bets)/len(bets):.1f}%)" if bets else "")
    if yes_bets and no_bets:
        print(f"  YES:NO ratio: {len(yes_bets)/len(no_bets):.2f}:1")

    print(f"\n{'='*50}")
    print(f"VOLUME RATIO (last {args.days} days)")
    print(f"{'='*50}")
    total_vol = yes_volume + no_volume
    print(f"Total volume: {total_vol:.2f} xDAI")
    print(f"  YES volume: {yes_volume:.2f} xDAI ({100*yes_volume/total_vol:.1f}%)" if total_vol else "  No volume")
    print(f"  NO volume:  {no_volume:.2f} xDAI ({100*no_volume/total_vol:.1f}%)" if total_vol else "")
    if yes_volume and no_volume:
        print(f"  YES:NO ratio: {yes_volume/no_volume:.2f}:1")


if __name__ == "__main__":
    main()
