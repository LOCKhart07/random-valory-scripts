"""
List IPFS links for recent `factual_research` delivers on Gnosis.

Queries the mech-marketplace-gnosis subgraph for the newest N delivers where
parsedRequest.tool == "factual_research", then prints a gateway URL for each
so the raw payload can be inspected.

Usage:
    poetry run python mech/list_factual_research_ipfs_links.py
    poetry run python mech/list_factual_research_ipfs_links.py -n 20 --hours 24
"""

import argparse
import sys
import time
from datetime import datetime, timezone

import requests

SUBGRAPH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs"

MECH = "0x601024e27f1c67b28209e24272ced8a31fc8151f"
PAGE_SIZE = 1000

QUERY = """
query Delivers($first: Int!, $skip: Int!, $ts_gte: BigInt!, $mech: Bytes!) {
    delivers(
        first: $first
        skip: $skip
        orderBy: blockTimestamp
        orderDirection: desc
        where: {blockTimestamp_gte: $ts_gte, mech: $mech}
    ) {
        requestId
        blockTimestamp
        transactionHash
        toolResponse
        mechDelivery { ipfsHash }
        marketplaceDelivery { ipfsHashBytes }
        request { parsedRequest { tool questionTitle } }
    }
}
"""


def ipfs_hash(d: dict) -> str | None:
    md = d.get("mechDelivery") or {}
    if md.get("ipfsHash"):
        return md["ipfsHash"]
    mk = d.get("marketplaceDelivery") or {}
    if mk.get("ipfsHashBytes"):
        return mk["ipfsHashBytes"].replace("0x", "")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=10, help="max delivers to list")
    ap.add_argument("--hours", type=float, default=24.0)
    args = ap.parse_args()

    ts_gte = int(time.time() - args.hours * 3600)
    all_delivers: list[dict] = []
    skip = 0
    while True:
        resp = requests.post(
            SUBGRAPH_URL,
            json={
                "query": QUERY,
                "variables": {
                    "first": PAGE_SIZE,
                    "skip": skip,
                    "ts_gte": str(ts_gte),
                    "mech": MECH,
                },
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if "data" not in data:
            print(f"Subgraph error: {data}", file=sys.stderr)
            return 1
        page = data["data"]["delivers"]
        all_delivers.extend(page)
        if len(page) < PAGE_SIZE:
            break
        skip += PAGE_SIZE

    fr = [
        d for d in all_delivers
        if ((d.get("request") or {}).get("parsedRequest") or {}).get("tool")
        == "factual_research"
    ]
    print(f"factual_research delivers in last {args.hours}h: {len(fr)}")
    print(f"(showing up to {args.n})\n")

    for d in fr[: args.n]:
        h = ipfs_hash(d)
        req_id_int = int(d["requestId"], 16)
        ts = datetime.fromtimestamp(int(d["blockTimestamp"]), tz=timezone.utc)
        if h:
            url = f"{IPFS_GATEWAY}/f01701220{h.upper()}/{req_id_int}"
        else:
            url = "(no ipfsHash)"
        q = (d.get("request") or {}).get("parsedRequest", {}).get("questionTitle") or ""
        print(f"{ts:%Y-%m-%d %H:%M:%S} UTC  tx={d['transactionHash']}")
        print(f"  {url}")
        print(f"  q: {q[:140]}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
