"""Check requests for a specific mech and probe IPFS data availability.

Includes per-sender breakdown showing which addresses are sending requests
with missing IPFS data.
"""

import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

SUBGRAPH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs"

REQUESTS_QUERY = """
query MechRequests($first: Int!, $skip: Int!, $where: Request_filter!) {
    requests(
        first: $first
        skip: $skip
        orderBy: blockTimestamp
        orderDirection: desc
        where: $where
    ) {
        id
        blockTimestamp
        transactionHash
        isDelivered
        sender { id }
        mechRequest { ipfsHash }
        parsedRequest {
            questionTitle
            tool
            prompt
            hash
        }
    }
}
"""


def fetch_requests(mech_address, since_ts, batch_size=1000):
    """Paginate through all requests for a mech since a timestamp."""
    all_requests = []
    skip = 0
    while True:
        variables = {
            "first": batch_size,
            "skip": skip,
            "where": {
                "priorityMech": mech_address.lower(),
                "blockTimestamp_gte": str(since_ts),
            },
        }
        resp = requests.post(
            SUBGRAPH_URL,
            json={"query": REQUESTS_QUERY, "variables": variables},
            headers={"Content-Type": "application/json"},
            timeout=90,
        )
        data = resp.json()
        if "errors" in data:
            print(f"Subgraph errors: {data['errors']}")
            break
        batch = data["data"]["requests"]
        all_requests.extend(batch)
        if len(batch) < batch_size:
            break
        skip += batch_size
    return all_requests


def probe_ipfs(ipfs_hash, timeout=15):
    """Try to fetch IPFS metadata. Returns (status, tool_name)."""
    if not ipfs_hash:
        return "no_hash", None

    if ipfs_hash.startswith("f01701220"):
        base_url = f"{IPFS_GATEWAY}/{ipfs_hash}"
    else:
        base_url = f"{IPFS_GATEWAY}/f01701220{ipfs_hash}"

    for url in [f"{base_url}/metadata.json", base_url]:
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    tool = data.get("tool", "unknown")
                    return "ok", tool
                except Exception:
                    return "not_json", None
            elif resp.status_code == 404:
                continue
            else:
                continue
        except requests.exceptions.Timeout:
            return "timeout", None
        except Exception:
            return "error", None

    return "not_found", None


def main():
    mech = sys.argv[1] if len(sys.argv) > 1 else "0xC05e7412439bD7e91730a6880E18d5D5873F632C"
    hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24

    now = int(time.time())
    since = now - (hours * 3600)

    print(f"Fetching requests for mech {mech} (last {hours}h)...")
    reqs = fetch_requests(mech, since)
    print(f"Found {len(reqs)} requests from subgraph\n")

    if not reqs:
        return

    # --- Overall summary ---
    parsed_count = sum(1 for r in reqs if r.get("parsedRequest"))
    unparsed_count = len(reqs) - parsed_count
    has_mech_req = sum(1 for r in reqs if r.get("mechRequest"))
    delivered = sum(1 for r in reqs if r.get("isDelivered"))

    print(f"Total requests: {len(reqs)}")
    print(f"  Delivered: {delivered}")
    print(f"  Has mechRequest (on-chain hash): {has_mech_req}")
    print(f"  Has parsedRequest (IPFS parsed): {parsed_count}")
    print(f"  Missing parsedRequest: {unparsed_count}")

    # --- Per-sender analysis ---
    sender_stats = defaultdict(lambda: {
        "total": 0, "parsed": 0, "unparsed": 0,
        "delivered": 0, "has_mech_req": 0,
        "tools": defaultdict(int),
        "first_ts": float("inf"), "last_ts": 0,
    })

    for r in reqs:
        sender = r.get("sender", {}).get("id", "unknown")
        s = sender_stats[sender]
        s["total"] += 1
        ts = int(r["blockTimestamp"])
        s["first_ts"] = min(s["first_ts"], ts)
        s["last_ts"] = max(s["last_ts"], ts)
        if r.get("isDelivered"):
            s["delivered"] += 1
        if r.get("mechRequest"):
            s["has_mech_req"] += 1
        if r.get("parsedRequest"):
            s["parsed"] += 1
            tool = r["parsedRequest"].get("tool", "unknown")
            s["tools"][tool] += 1
        else:
            s["unparsed"] += 1

    # Sort senders by total requests descending
    sorted_senders = sorted(sender_stats.items(), key=lambda x: -x[1]["total"])

    print(f"\n{'='*90}")
    print(f"PER-SENDER BREAKDOWN ({len(sorted_senders)} unique senders)")
    print(f"{'='*90}")

    for sender, s in sorted_senders:
        unparsed_pct = s["unparsed"] / s["total"] * 100 if s["total"] else 0
        first = datetime.fromtimestamp(s["first_ts"], timezone.utc).strftime("%H:%M:%S")
        last = datetime.fromtimestamp(s["last_ts"], timezone.utc).strftime("%H:%M:%S")

        # Calculate request frequency
        span_seconds = s["last_ts"] - s["first_ts"]
        if span_seconds > 0 and s["total"] > 1:
            freq_min = span_seconds / (s["total"] - 1) / 60
            freq_str = f"~1 req every {freq_min:.1f}min"
        else:
            freq_str = "single request"

        print(f"\nSender: {sender}")
        print(f"  Requests: {s['total']} | Delivered: {s['delivered']} | "
              f"Parsed: {s['parsed']} | Unparsed: {s['unparsed']} ({unparsed_pct:.0f}%)")
        print(f"  Has mechRequest: {s['has_mech_req']}/{s['total']}")
        print(f"  Active: {first} - {last} UTC | {freq_str}")
        if s["tools"]:
            tools_str = ", ".join(
                f"{t}({c})" for t, c in
                sorted(s["tools"].items(), key=lambda x: -x[1])
            )
            print(f"  Tools (parsed only): {tools_str}")

    # --- Summary table ---
    print(f"\n{'='*90}")
    print(f"SUMMARY TABLE")
    print(f"{'='*90}")
    print(f"{'Sender':<46} {'Total':>6} {'Parsed':>7} {'Unparsed':>9} {'Unp%':>5} {'MechReq':>8}")
    print(f"{'-'*46} {'-'*6} {'-'*7} {'-'*9} {'-'*5} {'-'*8}")
    for sender, s in sorted_senders:
        unparsed_pct = s["unparsed"] / s["total"] * 100 if s["total"] else 0
        print(f"{sender:<46} {s['total']:>6} {s['parsed']:>7} {s['unparsed']:>9} "
              f"{unparsed_pct:>4.0f}% {s['has_mech_req']:>8}")

    # --- Tool breakdown across all parsed ---
    all_tools = defaultdict(int)
    for s in sender_stats.values():
        for tool, count in s["tools"].items():
            all_tools[tool] += count
    if all_tools:
        print(f"\nTool breakdown (all parsed requests):")
        for tool, count in sorted(all_tools.items(), key=lambda x: -x[1]):
            print(f"  {tool}: {count}")


if __name__ == "__main__":
    main()
