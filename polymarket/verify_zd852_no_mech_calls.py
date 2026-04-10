"""
Verify on-chain activity for ZD#852 PolyStrat agent that "isn't trading".

User reports their bot is staking but never opens any positions. Pearl logs
(trader v0.33.0-rc3) show every period exits via check_stop_trading_round
with stop_trading=True before reaching the bet/Kelly stage.

Hypothesis to verify on-chain:
  - Did this Safe ever submit mech requests on Polygon?
  - Did it receive deliveries?
  - Did it ever place a Polymarket bet?
  - What does the trader_agent aggregate row look like?

Safe:        0x7537E909eFccBfA40dc3F274AB3333C1C335aDD9
Service ID:  177 (staking program: polygon_beta_1)
Service cfg: sc-e3092854-5f47-4e67-a626-c41240ad3286
Deployed:    ~2026-03-25
Logs end:    2026-03-27

Usage:
    poetry run python polymarket/verify_zd852_no_mech_calls.py
    poetry run python polymarket/verify_zd852_no_mech_calls.py --safe 0x...
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import requests

POLYMARKET_BETS_SUBGRAPH_URL = (
    "https://predict-polymarket-agents.subgraph.autonolas.tech/"
)
OLAS_MECH_POLYGON_SUBGRAPH_URL = (
    "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"
)

DEFAULT_SAFE = "0x7537E909eFccBfA40dc3F274AB3333C1C335aDD9"

REQUEST_TIMEOUT = 60
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3


def _post_with_retry(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            print(f"  [retry {attempt}/{MAX_RETRIES - 1}] {exc}, retrying in {wait}s")
            time.sleep(wait)
    raise last_exc


def call_subgraph(url: str, query: str, variables: dict | None = None) -> dict:
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = _post_with_retry(
        url, json=payload, headers={"Content-Type": "application/json"}
    )
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Subgraph error: {data['errors']}")
    return data


def fmt_ts(ts: int | str | None) -> str:
    if ts in (None, "", 0, "0"):
        return "-"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Mech (Polygon marketplace) — sender = the Safe address
# ---------------------------------------------------------------------------


def fetch_mech_sender(safe_address: str) -> dict | None:
    """Pull the sender row + a window of requests/deliveries.

    The polygon mech-marketplace subgraph indexes requests by `sender`
    (lowercased Safe address). If the sender entity does not exist, the
    Safe has never submitted a mech request.
    """
    query = """
query Sender($id: ID!) {
  sender(id: $id) {
    id
    totalMarketplaceRequests
    requests(first: 1000, orderBy: blockTimestamp, orderDirection: asc) {
      id
      blockTimestamp
      blockNumber
      transactionHash
      isDelivered
      feeUSD
      parsedRequest {
        tool
        questionTitle
        prompt
        hash
      }
      deliveries {
        id
        blockTimestamp
        transactionHash
        model
        toolResponse
      }
    }
  }
}
"""
    response = call_subgraph(
        OLAS_MECH_POLYGON_SUBGRAPH_URL, query, {"id": safe_address.lower()}
    )
    return (response.get("data") or {}).get("sender")


def fetch_request_with_deliver(request_id: str) -> dict | None:
    """Fetch a single request with its full deliver content."""
    query = """
query Req($id: ID!) {
  request(id: $id) {
    id
    blockTimestamp
    transactionHash
    isDelivered
    parsedRequest { tool questionTitle prompt hash }
    deliveries(first: 5) {
      id
      blockTimestamp
      transactionHash
      model
      toolResponse { id }
    }
  }
}
"""
    response = call_subgraph(
        OLAS_MECH_POLYGON_SUBGRAPH_URL, query, {"id": request_id}
    )
    return (response.get("data") or {}).get("request")


# ---------------------------------------------------------------------------
# Polymarket bets subgraph — traderAgent row + any bets
# ---------------------------------------------------------------------------


def fetch_trader_agent(safe_address: str) -> dict | None:
    query = """
query Trader($id: ID!) {
  traderAgent(id: $id) {
    id
    serviceId
    totalBets
    totalPayout
    totalTraded
    totalTradedSettled
  }
}
"""
    response = call_subgraph(
        POLYMARKET_BETS_SUBGRAPH_URL, query, {"id": safe_address.lower()}
    )
    return (response.get("data") or {}).get("traderAgent")


def fetch_bets(safe_address: str) -> list[dict]:
    query = """
query Bets($id: ID!) {
  marketParticipants(where: {traderAgent_: {id: $id}}, first: 1000) {
    bets(first: 1000, orderBy: blockTimestamp, orderDirection: asc) {
      id
      outcomeIndex
      amount
      shares
      blockTimestamp
      transactionHash
      question { id metadata { title } }
    }
  }
}
"""
    response = call_subgraph(
        POLYMARKET_BETS_SUBGRAPH_URL, query, {"id": safe_address.lower()}
    )
    parts = (response.get("data") or {}).get("marketParticipants") or []
    bets: list[dict] = []
    for p in parts:
        bets.extend(p.get("bets") or [])
    return bets


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def report(safe_address: str) -> int:
    print("=" * 78)
    print(f"ZD#852 on-chain verification for Safe {safe_address}")
    print("=" * 78)

    # 1. Mech requests on Polygon
    print("\n[1] Mech marketplace (Polygon) — sender row")
    print("-" * 78)
    sender = fetch_mech_sender(safe_address)
    if sender is None:
        print("  sender entity: NOT FOUND")
        print("  → This Safe has never submitted a mech request on Polygon.")
        mech_requests: list[dict] = []
        total_marketplace = 0
    else:
        total_marketplace = int(sender.get("totalMarketplaceRequests") or 0)
        mech_requests = sender.get("requests") or []
        print(f"  sender id: {sender.get('id')}")
        print(f"  totalMarketplaceRequests (counter): {total_marketplace}")
        print(f"  requests pulled (this window):     {len(mech_requests)}")
        if mech_requests:
            first = mech_requests[0]
            last = mech_requests[-1]
            print(
                f"  first request: {fmt_ts(first.get('blockTimestamp'))}  "
                f"tx={first.get('transactionHash')}"
            )
            print(
                f"  last request:  {fmt_ts(last.get('blockTimestamp'))}  "
                f"tx={last.get('transactionHash')}"
            )
            tools = {}
            for r in mech_requests:
                t = ((r.get("parsedRequest") or {}).get("tool")) or "<none>"
                tools[t] = tools.get(t, 0) + 1
            print("  tools used:")
            for t, n in sorted(tools.items(), key=lambda kv: -kv[1]):
                print(f"    {n:>4}  {t}")

    # 2. Mech deliveries for those requests
    print("\n[2] Mech deliveries for the requests above")
    print("-" * 78)
    if not mech_requests:
        print("  (skipped — no requests to look up)")
    else:
        delivered = 0
        for r in mech_requests:
            if r.get("isDelivered") or r.get("deliveries"):
                delivered += 1
        print(
            f"  delivered: {delivered}/{len(mech_requests)} "
            f"(via Request.isDelivered / .deliveries)"
        )
        print()
        print("  Per-request detail:")
        for i, r in enumerate(mech_requests, 1):
            pr = r.get("parsedRequest") or {}
            delivs = r.get("deliveries") or []
            qt = (pr.get("questionTitle") or "").strip().split("\u241f")[0]
            print(
                f"   {i:>2}. {fmt_ts(r.get('blockTimestamp'))} "
                f"tool={pr.get('tool') or '-'} "
                f"isDelivered={r.get('isDelivered')} "
                f"deliveries={len(delivs)}"
            )
            print(f"        Q: {qt[:90]}")
            for d in delivs[:1]:
                tr = d.get("toolResponse")
                tr_short = (tr[:120] + "...") if isinstance(tr, str) and len(tr) > 120 else tr
                print(
                    f"        deliver tx={d.get('transactionHash')} "
                    f"model={d.get('model')}"
                )
                print(f"        response: {tr_short}")

    # 3. Polymarket bets subgraph — trader_agent aggregate row
    print("\n[3] Polymarket bets subgraph — traderAgent aggregate row")
    print("-" * 78)
    trader = fetch_trader_agent(safe_address)
    if trader is None:
        print("  traderAgent: NOT FOUND")
        print("  → Subgraph has never seen a bet from this Safe.")
    else:
        print(f"  id:                          {trader.get('id')}")
        print(f"  serviceId:                   {trader.get('serviceId')}")
        print(f"  totalBets:                   {trader.get('totalBets')}")
        print(
            f"  totalTraded (USDC, raw):     {trader.get('totalTraded')}"
        )
        print(
            f"  totalTradedSettled (raw):    {trader.get('totalTradedSettled')}"
        )
        print(f"  totalPayout (raw):           {trader.get('totalPayout')}")

    # 4. Individual bets
    print("\n[4] Individual bets from this Safe")
    print("-" * 78)
    bets = fetch_bets(safe_address)
    print(f"  bets returned: {len(bets)}")
    for b in bets[:10]:
        title = ((b.get("question") or {}).get("metadata") or {}).get("title") or "?"
        print(
            f"  - {fmt_ts(b.get('blockTimestamp'))}  amount={b.get('amount')}  "
            f"outcome={b.get('outcomeIndex')}  tx={b.get('transactionHash')}\n"
            f"      {title[:90]}"
        )
    if len(bets) > 10:
        print(f"  ... ({len(bets) - 10} more)")

    # 5. Verdict
    print("\n[5] Verdict")
    print("-" * 78)
    has_mech = total_marketplace > 0 or bool(mech_requests)
    has_bets = bool(bets) or (trader and int(trader.get("totalBets") or 0) > 0)
    print(f"  any mech requests on-chain? {'YES' if has_mech else 'NO'}")
    print(f"  any Polymarket bets?        {'YES' if has_bets else 'NO'}")
    print()
    if not has_mech and not has_bets:
        print("  CONFIRMED: this Safe has produced ZERO mech requests and ZERO bets")
        print("  on-chain. Whatever the trader is doing in-process, it has never")
        print("  reached the point of submitting a request to the mech marketplace.")
        print()
        print("  This rules out the 'Kelly rejected every bet' hypothesis: Kelly")
        print("  runs AFTER the mech round, but the agent never produced any mech")
        print("  request to begin with. The short-circuit is upstream of Kelly,")
        print("  consistent with the Pearl logs showing check_stop_trading_round")
        print("  emitting SKIP_TRADING on every period.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--safe", default=DEFAULT_SAFE, help="Safe address")
    parser.add_argument(
        "--json", action="store_true", help="Dump raw fetched data as JSON"
    )
    args = parser.parse_args()

    if args.json:
        out = {
            "safe": args.safe,
            "sender": fetch_mech_sender(args.safe),
            "trader_agent": fetch_trader_agent(args.safe),
            "bets": fetch_bets(args.safe),
        }
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    return report(args.safe)


if __name__ == "__main__":
    raise SystemExit(main())
