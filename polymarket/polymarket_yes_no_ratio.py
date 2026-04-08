"""
Yes vs No ratio analysis for Polymarket (PolyStrat) agents.

Shows:
- Market resolution ratio (how many resolved Yes vs No)
- Bet placement ratio (how many bets placed on Yes vs No)
- Volume ratio (USDC wagered on Yes vs No)

Usage:
    python polymarket/polymarket_yes_no_ratio.py
    python polymarket/polymarket_yes_no_ratio.py --days 90
"""

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

POLYMARKET_BETS_SUBGRAPH_URL = (
    "https://predict-polymarket-agents.subgraph.autonolas.tech/"
)
THE_GRAPH_API_KEY = os.getenv("THE_GRAPH_API_KEY")
POLYGON_REGISTRY_SUBGRAPH_URL = (
    f"https://gateway.thegraph.com/api/{THE_GRAPH_API_KEY}/subgraphs/id/HHRBjVWFT2bV7eNSRqbCNDtUVnLPt911hcp8mSe4z6KG"
    if THE_GRAPH_API_KEY else None
)

USDC_DIV = 1_000_000


def _post_with_retry(url, **kwargs):
    kwargs.setdefault("timeout", 90)
    for attempt in range(4):
        try:
            resp = requests.post(url, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * (2 ** attempt))


def call_subgraph(url, query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = _post_with_retry(url, json=payload, headers={"Content-Type": "application/json"})
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Subgraph error: {data['errors']}")
    return data


def get_all_agents():
    if POLYGON_REGISTRY_SUBGRAPH_URL:
        try:
            query = '{ services(where: { agentIds_contains: [86] }, first: 1000) { id multisig } }'
            response = call_subgraph(POLYGON_REGISTRY_SUBGRAPH_URL, query)
            return [s["multisig"] for s in response["data"]["services"]]
        except Exception as exc:
            print(f"  Registry fetch failed ({exc}), falling back to subgraph...")

    query = '{ traderAgents(first: 1000, orderBy: totalBets, orderDirection: desc) { id totalBets } }'
    response = call_subgraph(POLYMARKET_BETS_SUBGRAPH_URL, query)
    agents = response.get("data", {}).get("traderAgents", [])
    return [a["id"] for a in agents]


def fetch_agent_bets(safe_address, since_ts=None):
    query = """
query GetBets($id: ID!) {
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
"""
    response = call_subgraph(POLYMARKET_BETS_SUBGRAPH_URL, query, {"id": safe_address})
    participants = response.get("data", {}).get("marketParticipants", [])
    all_bets = []
    for p in participants:
        for bet in p.get("bets", []):
            if since_ts and int(bet.get("blockTimestamp", 0)) < since_ts:
                continue
            all_bets.append(bet)
    return all_bets


def main():
    parser = argparse.ArgumentParser(description="Polymarket Yes/No ratio analysis")
    parser.add_argument("--days", type=int, default=31, help="Lookback days (default: 31)")
    args = parser.parse_args()

    now = int(datetime.now(timezone.utc).timestamp())
    since = now - args.days * 86400
    since_dt = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%d")

    print(f"Fetching agents...")
    agents = get_all_agents()
    print(f"Found {len(agents)} agents")

    print(f"Fetching bets since {since_dt} ({args.days} days)...")
    all_bets = []

    def _fetch(addr):
        return fetch_agent_bets(addr, since_ts=since)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch, a): a for a in agents}
        done = 0
        for f in as_completed(futures):
            done += 1
            if done % 50 == 0:
                print(f"  [{done}/{len(agents)} agents queried]", flush=True)
            try:
                all_bets.extend(f.result())
            except Exception as exc:
                pass  # skip failed agents

    # Filter to resolved bets only
    resolved = []
    for b in all_bets:
        q = b.get("question") or {}
        res = q.get("resolution")
        if res and res.get("winningIndex") is not None and int(res["winningIndex"]) >= 0:
            resolved.append(b)

    print(f"Total bets: {len(all_bets)} ({len(resolved)} resolved)")

    # --- Market resolution ratio ---
    markets = {}  # question_id -> winningIndex
    for b in resolved:
        qid = b["question"]["id"]
        if qid not in markets:
            markets[qid] = int(b["question"]["resolution"]["winningIndex"])

    resolved_yes = sum(1 for v in markets.values() if v == 0)
    resolved_no = sum(1 for v in markets.values() if v == 1)
    resolved_other = sum(1 for v in markets.values() if v not in (0, 1))
    total_markets = len(markets)

    print(f"\n{'='*50}")
    print(f"MARKET RESOLUTION RATIO (last {args.days} days)")
    print(f"{'='*50}")
    print(f"Total resolved markets: {total_markets}")
    if total_markets:
        print(f"  Resolved YES: {resolved_yes} ({100*resolved_yes/total_markets:.1f}%)")
        print(f"  Resolved NO:  {resolved_no} ({100*resolved_no/total_markets:.1f}%)")
        if resolved_other:
            print(f"  Other:        {resolved_other} ({100*resolved_other/total_markets:.1f}%)")
        if resolved_yes and resolved_no:
            print(f"  YES:NO ratio: {resolved_yes/resolved_no:.2f}:1")
        elif resolved_yes:
            print(f"  YES:NO ratio: all YES")
        elif resolved_no:
            print(f"  YES:NO ratio: 0:{resolved_no} (all NO)")

    # --- Bet placement ratio ---
    yes_bets = [b for b in resolved if int(b.get("outcomeIndex", 0)) == 0]
    no_bets = [b for b in resolved if int(b.get("outcomeIndex", 0)) == 1]

    yes_volume = sum(int(b["amount"]) for b in yes_bets) / USDC_DIV
    no_volume = sum(int(b["amount"]) for b in no_bets) / USDC_DIV

    print(f"\n{'='*50}")
    print(f"BET PLACEMENT RATIO (last {args.days} days)")
    print(f"{'='*50}")
    print(f"Total resolved bets: {len(resolved)}")
    if resolved:
        print(f"  YES bets: {len(yes_bets)} ({100*len(yes_bets)/len(resolved):.1f}%)")
        print(f"  NO bets:  {len(no_bets)} ({100*len(no_bets)/len(resolved):.1f}%)")
        if yes_bets and no_bets:
            print(f"  YES:NO ratio: {len(yes_bets)/len(no_bets):.2f}:1")

    print(f"\n{'='*50}")
    print(f"VOLUME RATIO (last {args.days} days)")
    print(f"{'='*50}")
    total_vol = yes_volume + no_volume
    if total_vol:
        print(f"Total volume: {total_vol:,.2f} USDC")
        print(f"  YES volume: {yes_volume:,.2f} USDC ({100*yes_volume/total_vol:.1f}%)")
        print(f"  NO volume:  {no_volume:,.2f} USDC ({100*no_volume/total_vol:.1f}%)")
        if yes_volume and no_volume:
            print(f"  YES:NO ratio: {yes_volume/no_volume:.2f}:1")
    else:
        print("No volume")


if __name__ == "__main__":
    main()
