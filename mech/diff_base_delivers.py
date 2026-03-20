"""Compare successful vs failed google_image_gen delivers on Base mech."""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.exceptions import ConnectionError, Timeout

SUBGRAPH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-base"
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs"
MECH = "0xe535D7AcDEeD905dddcb5443f41980436833cA2B".lower()

QUERY = """
query MechDelivers($first: Int!, $skip: Int!, $where: Deliver_filter!) {
    delivers(
        first: $first
        skip: $skip
        orderBy: blockTimestamp
        orderDirection: desc
        where: $where
    ) {
        id
        requestId
        sender
        mech
        blockTimestamp
        blockNumber
        transactionHash
        model
        toolResponse
        mechDelivery {
            ipfsHash
        }
        marketplaceDelivery {
            ipfsHashBytes
        }
        request {
            id
            blockTimestamp
            sender
            mechRequest {
                ipfsHash
            }
            parsedRequest {
                tool
                prompt
                nonce
                questionTitle
            }
        }
    }
}
"""


def _post_with_retry(url, **kwargs):
    kwargs.setdefault("timeout", 90)
    for attempt in range(1, 5):
        try:
            resp = requests.post(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (Timeout, ConnectionError, requests.exceptions.HTTPError) as exc:
            if attempt == 4:
                raise
            time.sleep(3 * (2 ** (attempt - 1)))


def fetch_delivers(start_ts):
    where = {"mech": MECH, "blockTimestamp_gt": str(start_ts)}
    all_delivers = []
    skip = 0
    while True:
        variables = {"first": 100, "skip": skip, "where": where}
        resp = _post_with_retry(
            SUBGRAPH_URL,
            json={"query": QUERY, "variables": variables},
            headers={"Content-Type": "application/json"},
        )
        body = resp.json()
        if "data" not in body:
            print(f"Subgraph error: {json.dumps(body)[:500]}")
            # Retry with simpler query
            break
        batch = body["data"]["delivers"]
        all_delivers.extend(batch)
        if len(batch) < 100:
            break
        skip += 100
    return all_delivers


def get_ipfs_hash(d):
    md = d.get("mechDelivery")
    if md and md.get("ipfsHash"):
        return md["ipfsHash"]
    mk = d.get("marketplaceDelivery")
    if mk and mk.get("ipfsHashBytes"):
        return mk["ipfsHashBytes"].replace("0x", "")
    return None


def fetch_ipfs(ipfs_hash, request_id):
    h = ipfs_hash.upper()
    rid = int(request_id, 16)
    url = f"{IPFS_GATEWAY}/f01701220{h}/{rid}"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def fetch_request_ipfs(req):
    """Fetch the original request data from IPFS."""
    mech_req = req.get("mechRequest") or {}
    ipfs_hash = mech_req.get("ipfsHash")
    if not ipfs_hash:
        return None
    h = ipfs_hash.upper()
    url = f"{IPFS_GATEWAY}/f01701220{h}/metadata.json"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        # Try without metadata.json
        url2 = f"{IPFS_GATEWAY}/f01701220{h}"
        try:
            resp = requests.get(url2, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None


def main():
    start_ts = int(time.time()) - 7 * 86400
    print("Fetching delivers...")
    delivers = fetch_delivers(start_ts)

    # Filter to google_image_gen only
    img_delivers = []
    for d in delivers:
        parsed = (d.get("request") or {}).get("parsedRequest") or {}
        if parsed.get("tool") == "google_image_gen":
            img_delivers.append(d)

    print(f"Found {len(img_delivers)} google_image_gen delivers")

    # Classify
    valid = []
    invalid = []
    for d in img_delivers:
        tr = d.get("toolResponse") or ""
        if "invalid response" in tr.lower():
            invalid.append(d)
        elif tr:
            valid.append(d)
        else:
            invalid.append(d)

    print(f"Valid: {len(valid)}, Invalid: {len(invalid)}")

    # Enrich with IPFS data
    print("\nEnriching with IPFS data...")
    all_to_enrich = valid + invalid[:5]  # all valid + sample of invalid
    tasks = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        for d in all_to_enrich:
            ipfs_hash = get_ipfs_hash(d)
            if ipfs_hash:
                f = executor.submit(fetch_ipfs, ipfs_hash, d["requestId"])
                tasks[f] = ("deliver_ipfs", d)
            req = d.get("request") or {}
            mech_req = req.get("mechRequest") or {}
            if mech_req.get("ipfsHash"):
                f2 = executor.submit(fetch_request_ipfs, req)
                tasks[f2] = ("request_ipfs", d)

        for future in as_completed(tasks):
            tag, deliver = tasks[future]
            result = future.result()
            if result:
                if tag == "deliver_ipfs":
                    deliver["ipfsData"] = result
                else:
                    deliver["requestIpfsData"] = result

    # Print comparison
    print("\n" + "=" * 70)
    print("VALID google_image_gen delivers:")
    print("=" * 70)
    for d in valid:
        parsed = (d.get("request") or {}).get("parsedRequest") or {}
        req = d.get("request") or {}
        print(f"\n  Tx:         {d.get('transactionHash')}")
        print(f"  Time:       {d['blockTimestamp']}")
        print(f"  Model:      {d.get('model')}")
        print(f"  Sender:     {d.get('sender')}")
        print(f"  RequestId:  {d.get('requestId')}")
        print(f"  Tool:       {parsed.get('tool')}")
        print(f"  Nonce:      {parsed.get('nonce')}")
        print(f"  Prompt:     {(parsed.get('prompt') or '')[:300]}")
        print(f"  Question:   {parsed.get('questionTitle')}")
        print(f"  Req IPFS:   {(req.get('mechRequest') or {}).get('ipfsHash')}")
        print(f"  Del IPFS:   {get_ipfs_hash(d)}")
        tr = d.get("toolResponse") or ""
        print(f"  toolResp:   {tr[:300]}")
        if d.get("ipfsData"):
            print(f"  IPFS data:  {json.dumps(d['ipfsData'], indent=4)[:500]}")
        if d.get("requestIpfsData"):
            print(f"  Req IPFS data: {json.dumps(d['requestIpfsData'], indent=4)[:500]}")

    print("\n" + "=" * 70)
    print("INVALID google_image_gen delivers (first 5):")
    print("=" * 70)
    for d in invalid[:5]:
        parsed = (d.get("request") or {}).get("parsedRequest") or {}
        req = d.get("request") or {}
        print(f"\n  Tx:         {d.get('transactionHash')}")
        print(f"  Time:       {d['blockTimestamp']}")
        print(f"  Model:      {d.get('model')}")
        print(f"  Sender:     {d.get('sender')}")
        print(f"  RequestId:  {d.get('requestId')}")
        print(f"  Tool:       {parsed.get('tool')}")
        print(f"  Nonce:      {parsed.get('nonce')}")
        print(f"  Prompt:     {(parsed.get('prompt') or '')[:300]}")
        print(f"  Question:   {parsed.get('questionTitle')}")
        print(f"  Req IPFS:   {(req.get('mechRequest') or {}).get('ipfsHash')}")
        print(f"  Del IPFS:   {get_ipfs_hash(d)}")
        tr = d.get("toolResponse") or ""
        print(f"  toolResp:   {tr[:300]}")
        if d.get("ipfsData"):
            print(f"  IPFS data:  {json.dumps(d['ipfsData'], indent=4)[:500]}")
        if d.get("requestIpfsData"):
            print(f"  Req IPFS data: {json.dumps(d['requestIpfsData'], indent=4)[:500]}")


if __name__ == "__main__":
    main()
