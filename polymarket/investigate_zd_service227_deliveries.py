"""
Follow-up to investigate_zd_service227.py — enumerate the 17 mech requests
from Safe 0xdead1D0F135683EC517c13C1E4120B56cF322815 and verify delivery
status for each, plus fetch one delivered IPFS payload to check well-formedness.
"""
import json
import statistics
import sys
import time
from datetime import datetime, timezone

import requests

SAFE = "0xdead1D0F135683EC517c13C1E4120B56cF322815".lower()
OLAS_MECH_POLYGON = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs"


def gql(q, variables=None):
    r = requests.post(OLAS_MECH_POLYGON,
                      json={"query": q, "variables": variables or {}},
                      timeout=60)
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(body["errors"])
    return body["data"]


def fetch_requests_with_deliveries():
    q = """
    query Sender($id: ID!) {
      sender(id: $id) {
        id totalMarketplaceRequests
        requests(first: 1000, orderBy: blockTimestamp, orderDirection: asc) {
          id
          blockTimestamp
          blockNumber
          transactionHash
          isDelivered
          feeUSD
          priorityMech { id }
          parsedRequest { tool questionTitle prompt hash }
          deliveries {
            id
            blockTimestamp
            transactionHash
            deliveryMech { id }
            ipfsHash
            toolResponse
          }
        }
      }
    }
    """
    return gql(q, {"id": SAFE}).get("sender")


def fetch_request_fallback(req_id):
    """Alternate query schema in case fields differ."""
    q = """
    query R($id: ID!) {
      request(id: $id) {
        id
        blockTimestamp
        transactionHash
        isDelivered
        priorityMech
        parsedRequest { tool questionTitle hash }
        deliveries {
          id
          blockTimestamp
          transactionHash
          mech
          deliveryMech
          ipfsHash
          toolResponse
        }
      }
    }
    """
    try:
        return gql(q, {"id": req_id}).get("request")
    except Exception as e:
        return {"_err": str(e)}


def fmt_ts(ts):
    if ts in (None, 0, "0", ""):
        return "-"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fetch_ipfs(ipfs_hash):
    if not ipfs_hash:
        return None
    # Try raw hash, then f01701220-wrapped
    for cid in (ipfs_hash,
                f"f01701220{ipfs_hash[2:] if ipfs_hash.startswith('0x') else ipfs_hash}"):
        url = f"{IPFS_GATEWAY}/{cid}"
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                try:
                    return url, r.json()
                except Exception:
                    return url, r.text[:500]
        except Exception:
            pass
    return None


def main():
    print("=== Deliveries for service 227 Safe ===")
    print(f"Safe: 0xdead1D0F135683EC517c13C1E4120B56cF322815")
    print()

    try:
        sender = fetch_requests_with_deliveries()
    except Exception as e:
        print(f"Primary query failed: {e}")
        # Fallback: query just requests without deliveryMech
        q = """
        query Sender($id: ID!) {
          sender(id: $id) {
            id totalMarketplaceRequests
            requests(first: 1000, orderBy: blockTimestamp, orderDirection: asc) {
              id blockTimestamp transactionHash isDelivered
              parsedRequest { tool hash }
              deliveries {
                id blockTimestamp transactionHash toolResponse
              }
            }
          }
        }
        """
        sender = gql(q, {"id": SAFE}).get("sender")

    if not sender:
        print("sender entity not found")
        return 1

    print(f"totalMarketplaceRequests: {sender['totalMarketplaceRequests']}")
    reqs = sender.get("requests") or []
    print(f"requests fetched:         {len(reqs)}")
    print()

    rows = []
    for r in reqs:
        pr = r.get("parsedRequest") or {}
        delivs = r.get("deliveries") or []
        first_d = delivs[0] if delivs else None
        req_ts = int(r["blockTimestamp"])
        del_ts = int(first_d["blockTimestamp"]) if first_d else None
        latency = (del_ts - req_ts) if del_ts else None
        # Mech that served
        deliv_mech = None
        if first_d:
            deliv_mech = ((first_d.get("deliveryMech") or {}).get("id")
                          if isinstance(first_d.get("deliveryMech"), dict)
                          else first_d.get("deliveryMech") or first_d.get("mech"))
        priority_mech = None
        pm = r.get("priorityMech")
        if isinstance(pm, dict):
            priority_mech = pm.get("id")
        elif isinstance(pm, str):
            priority_mech = pm
        rows.append({
            "req_id": r["id"],
            "req_tx": r["transactionHash"],
            "req_ts": req_ts,
            "tool": pr.get("tool"),
            "ipfs_hash": pr.get("hash"),
            "isDelivered": r.get("isDelivered"),
            "priority_mech": priority_mech,
            "deliv_count": len(delivs),
            "deliv_tx": first_d["transactionHash"] if first_d else None,
            "deliv_ts": del_ts,
            "deliv_mech": deliv_mech,
            "deliv_ipfs": (first_d or {}).get("ipfsHash") if first_d else None,
            "tool_response": (first_d or {}).get("toolResponse") if first_d else None,
            "latency_s": latency,
        })

    print(f"{'#':>3} {'req_ts':<20} {'tool':<14} {'delivered':<5} {'lat_s':>7} "
          f"{'deliv_mech':<44} req_tx")
    for i, x in enumerate(rows, 1):
        print(f"{i:>3} {fmt_ts(x['req_ts']):<20} {str(x['tool'])[:14]:<14} "
              f"{'Y' if x['deliv_ts'] else 'N':<5} "
              f"{(str(x['latency_s']) if x['latency_s'] is not None else '-'):>7} "
              f"{str(x['deliv_mech'] or '-')[:44]:<44} {x['req_tx']}")

    # Per-row detail
    print("\n--- per-request detail ---")
    for i, x in enumerate(rows, 1):
        print(f"\n[{i}] req_ts={fmt_ts(x['req_ts'])}  tool={x['tool']}")
        print(f"    req_tx  : {x['req_tx']}")
        print(f"    req_ipfs: {x['ipfs_hash']}")
        print(f"    priority_mech: {x['priority_mech']}")
        print(f"    isDelivered: {x['isDelivered']}  deliv_count: {x['deliv_count']}")
        if x['deliv_ts']:
            print(f"    deliv_ts: {fmt_ts(x['deliv_ts'])}  lat={x['latency_s']}s")
            print(f"    deliv_tx: {x['deliv_tx']}")
            print(f"    deliv_mech: {x['deliv_mech']}")
            print(f"    deliv_ipfs: {x['deliv_ipfs']}")
            tr = x['tool_response']
            if tr:
                if isinstance(tr, str):
                    print(f"    toolResponse (first 200): {tr[:200]}")
                else:
                    print(f"    toolResponse (type={type(tr).__name__}): {str(tr)[:200]}")

    # Stats
    delivered = [x for x in rows if x["deliv_ts"]]
    undelivered = [x for x in rows if not x["deliv_ts"]]
    print("\n--- summary ---")
    print(f"total requests:   {len(rows)}")
    print(f"delivered:        {len(delivered)}")
    print(f"undelivered:      {len(undelivered)}")
    if delivered:
        lats = [x["latency_s"] for x in delivered]
        print(f"latency median:   {statistics.median(lats)} s")
        print(f"latency mean:     {statistics.mean(lats):.1f} s")
        print(f"latency min/max:  {min(lats)}/{max(lats)} s")

    now_ts = int(time.time())
    print(f"\nnow: {fmt_ts(now_ts)} ({now_ts})")
    if undelivered:
        print("\nundelivered age vs 300s trader timeout:")
        for x in undelivered:
            age = now_ts - x["req_ts"]
            print(f"  req_ts={fmt_ts(x['req_ts'])}  age={age}s  "
                  f"{'within 300s' if age <= 300 else 'LONG PAST 300s'}  "
                  f"tx={x['req_tx']}")

    # Fetch one IPFS payload
    print("\n--- IPFS payload of first delivered response ---")
    target = None
    for x in delivered:
        if x.get("deliv_ipfs"):
            target = x
            break
    if target:
        got = fetch_ipfs(target["deliv_ipfs"])
        if got:
            url, payload = got
            print(f"URL: {url}")
            if isinstance(payload, dict):
                print(json.dumps(payload, indent=2)[:2000])
            else:
                print(str(payload)[:2000])
        else:
            print(f"Could not fetch ipfs hash {target['deliv_ipfs']}")
    else:
        # Try fetching via deliv.toolResponse content if it's an IPFS id,
        # otherwise try the request's parsedRequest.hash which can point to
        # the deliver metadata depending on subgraph version.
        print("No deliv_ipfs field on any delivery row — trying raw toolResponse strings")
        for x in delivered[:1]:
            tr = x.get("tool_response")
            print(f"  tr = {str(tr)[:500]}")


if __name__ == "__main__":
    sys.exit(main() or 0)
