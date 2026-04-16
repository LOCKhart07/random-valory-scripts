"""
Fetch delivers for a mech over a past-N-hours window and summarize by tool.

Useful right after a mech/tool deployment to verify the new tool is being
invoked and to spot any deliver-time failures.

Usage:
    python fetch_recent_mech_delivers.py <mech_address> --hours 5
    python fetch_recent_mech_delivers.py <mech_address> --hours 5 --new-tool <tool>
    python fetch_recent_mech_delivers.py <mech_address> --hours 5 --json
"""

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import requests
from requests.exceptions import ConnectionError, Timeout

SUBGRAPH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3
PAGE_SIZE = 1000

QUERY = """
query RecentDelivers($where: Deliver_filter!, $first: Int!, $skip: Int!) {
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
        blockTimestamp
        transactionHash
        model
        toolResponse
        request {
            parsedRequest {
                tool
                questionTitle
            }
        }
    }
}
"""


def _post_with_retry(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (Timeout, ConnectionError) as exc:
            last_exc = exc
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code < 500:
                raise
            last_exc = exc
        if attempt == MAX_RETRIES:
            break
        wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
        print(f"  [retry {attempt}] {last_exc}; sleeping {wait}s", file=sys.stderr)
        time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def fetch_delivers(mech: str, start_ts: int) -> list[dict]:
    where = {"mech": mech.lower(), "blockTimestamp_gte": str(start_ts)}
    results: list[dict] = []
    skip = 0
    while True:
        resp = _post_with_retry(
            SUBGRAPH_URL,
            json={
                "query": QUERY,
                "variables": {"where": where, "first": PAGE_SIZE, "skip": skip},
            },
            headers={"Content-Type": "application/json"},
        )
        data = resp.json()
        if "data" not in data:
            print(f"Subgraph error: {data}", file=sys.stderr)
            sys.exit(1)
        page = data["data"]["delivers"]
        results.extend(page)
        if len(page) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    return results


def _ts(ts: str) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def summarize(delivers: list[dict], new_tool: str | None) -> None:
    if not delivers:
        print("No delivers in window.")
        return

    tool_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    empty_response = 0
    error_response = 0
    for d in delivers:
        parsed = (d.get("request") or {}).get("parsedRequest") or {}
        tool = parsed.get("tool") or "—"
        tool_counts[tool] += 1
        model_counts[d.get("model") or "—"] += 1
        resp = d.get("toolResponse") or ""
        if not resp:
            empty_response += 1
            continue
        try:
            obj = json.loads(resp)
            if isinstance(obj, dict) and (
                obj.get("error") or obj.get("p_yes") is None and obj.get("prediction") is None
            ):
                # Heuristic: predict tools should produce p_yes/prediction
                if "error" in obj:
                    error_response += 1
        except (json.JSONDecodeError, TypeError):
            pass

    first_ts = int(min(d["blockTimestamp"] for d in delivers))
    last_ts = int(max(d["blockTimestamp"] for d in delivers))

    print(f"Delivers: {len(delivers)}")
    print(f"Window:   {_ts(str(first_ts))} → {_ts(str(last_ts))}")
    print(f"Empty toolResponse:  {empty_response}")
    print(f"Explicit error body: {error_response}")
    print()

    print("Tool breakdown:")
    for tool, count in tool_counts.most_common():
        marker = "  ← NEW" if new_tool and tool == new_tool else ""
        print(f"  {count:4d}  {tool}{marker}")
    print()

    print("Model breakdown:")
    for model, count in model_counts.most_common():
        print(f"  {count:4d}  {model}")
    print()

    if new_tool:
        matches = [
            d for d in delivers
            if ((d.get("request") or {}).get("parsedRequest") or {}).get("tool") == new_tool
        ]
        print(f"New-tool '{new_tool}' delivers: {len(matches)}")
        for d in matches[:5]:
            parsed = (d.get("request") or {}).get("parsedRequest") or {}
            print(f"  {_ts(d['blockTimestamp'])} | tx={d['transactionHash']}")
            print(f"    question: {parsed.get('questionTitle', '—')}")
            raw = d.get("toolResponse") or ""
            preview = raw[:240].replace("\n", " ")
            print(f"    response[:240]: {preview}")


def main():
    parser = argparse.ArgumentParser(
        description="Summarize recent mech delivers over a past-N-hours window."
    )
    parser.add_argument("mech", help="Mech contract address (0x...)")
    parser.add_argument(
        "--hours", type=float, default=5.0, help="Look-back window in hours (default: 5)"
    )
    parser.add_argument(
        "--new-tool",
        metavar="TOOL",
        help="Highlight this specific tool name (e.g. a newly deployed tool)",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()

    start_ts = int(time.time() - args.hours * 3600)
    delivers = fetch_delivers(args.mech, start_ts)

    if args.json_output:
        print(json.dumps(delivers, indent=2))
        return

    print(f"Mech:   {args.mech}")
    print(f"Window: past {args.hours}h (since {_ts(str(start_ts))})")
    print()
    summarize(delivers, args.new_tool)


if __name__ == "__main__":
    main()
