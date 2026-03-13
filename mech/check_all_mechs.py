"""Check all active mechs in the last hour for broken delivers."""

import json
import time
from collections import defaultdict

import requests

SUBGRAPH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"

QUERY = """
query RecentDelivers($first: Int!, $where: Deliver_filter!) {
    delivers(first: $first, orderBy: blockTimestamp, orderDirection: desc, where: $where) {
        mech
        toolResponse
        request {
            parsedRequest {
                tool
            }
        }
    }
}
"""


def classify(raw):
    if not raw:
        return "empty"
    try:
        resp = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "unparseable"
    if isinstance(resp, dict):
        if "p_yes" in resp or "p_no" in resp:
            return "valid"
        result = resp.get("result", "")
        if isinstance(result, str) and "invalid response" in result.lower():
            return "invalid"
    return "valid"


def main():
    now = int(time.time())
    start = now - 3600

    variables = {"first": 1000, "where": {"blockTimestamp_gt": str(start)}}
    resp = requests.post(
        SUBGRAPH_URL,
        json={"query": QUERY, "variables": variables},
        headers={"Content-Type": "application/json"},
        timeout=90,
    )
    data = resp.json()
    delivers = data["data"]["delivers"]

    mech_stats = defaultdict(
        lambda: {
            "valid": 0,
            "invalid": 0,
            "tools": defaultdict(lambda: {"valid": 0, "invalid": 0}),
        }
    )

    for d in delivers:
        mech = d["mech"]
        parsed = (d.get("request") or {}).get("parsedRequest") or {}
        tool = parsed.get("tool", "unknown")
        raw = d.get("toolResponse") or ""
        c = classify(raw)
        is_valid = c == "valid"

        if is_valid:
            mech_stats[mech]["valid"] += 1
            mech_stats[mech]["tools"][tool]["valid"] += 1
        else:
            mech_stats[mech]["invalid"] += 1
            mech_stats[mech]["tools"][tool]["invalid"] += 1

    print(f"Active mechs in last hour: {len(mech_stats)}")
    print(f"Total delivers: {len(delivers)}\n")

    for mech in sorted(
        mech_stats.keys(),
        key=lambda m: mech_stats[m]["valid"] + mech_stats[m]["invalid"],
        reverse=True,
    ):
        s = mech_stats[mech]
        total = s["valid"] + s["invalid"]
        fail_rate = s["invalid"] / total * 100 if total else 0
        if s["invalid"] > 0 and s["valid"] == 0:
            status = "BROKEN"
        elif s["invalid"] > 0:
            status = "PARTIAL"
        else:
            status = "OK"
        print(f"Mech: {mech}")
        print(
            f"  Total: {total} | Valid: {s['valid']} | Invalid: {s['invalid']} | {fail_rate:.0f}% fail | {status}"
        )
        for tool in sorted(s["tools"].keys()):
            t = s["tools"][tool]
            tt = t["valid"] + t["invalid"]
            print(f"    {tool}: {tt} ({t['valid']} valid, {t['invalid']} invalid)")
        print()


if __name__ == "__main__":
    main()
