"""
Count daily mech requests by tool across Gnosis and Polygon chains.

Queries the mech-marketplace subgraph for both chains, groups requests
by tool name, and prints daily averages plus totals.

Usage:
    python count_daily_requests_by_tool.py                # last 7 days
    python count_daily_requests_by_tool.py --period 30d   # last 30 days
    python count_daily_requests_by_tool.py --start 2026-03-01 --end 2026-04-01
"""

import argparse
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from requests.exceptions import ConnectionError, Timeout

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

SUBGRAPH_URLS = {
    "gnosis": "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis",
    "polygon": "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon",
}

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


def _post_with_retry(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_exc = None
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
        print(f"    [retry {attempt}/{MAX_RETRIES - 1}] {last_exc}")
        time.sleep(wait)
    raise last_exc


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

GET_REQUESTS_QUERY = """
query MechRequests($timestamp_gt: Int!, $timestamp_lte: Int!, $skip: Int, $first: Int) {
    requests(
        first: $first
        skip: $skip
        orderBy: blockTimestamp
        orderDirection: asc
        where: { blockTimestamp_gt: $timestamp_gt, blockTimestamp_lte: $timestamp_lte }
    ) {
        blockTimestamp
        parsedRequest {
            tool
        }
    }
}
"""


def fetch_requests(url: str, start_ts: int, end_ts: int) -> list[dict]:
    """Fetch all requests in the time range with skip-based pagination."""
    all_requests = []
    skip = 0
    batch_size = 1000
    while True:
        variables = {
            "timestamp_gt": start_ts,
            "timestamp_lte": end_ts,
            "first": batch_size,
            "skip": skip,
        }
        resp = _post_with_retry(url, json={"query": GET_REQUESTS_QUERY, "variables": variables})
        data = resp.json()
        batch = data.get("data", {}).get("requests", [])
        if not batch:
            break
        all_requests.extend(batch)
        skip += len(batch)
        if len(batch) < batch_size:
            break
    return all_requests


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

# Tools mentioned in Jenslee's measurement
SOURCE_CONTENT_TOOLS = {
    "prediction_request",
    "prediction_request_sme",
    "prediction_request_rag",
    "prediction_request_reasoning",
    "prediction_url_cot",
    "superforcaster",
}


def analyze(requests_by_chain: dict[str, list[dict]], num_days: float):
    """Print per-tool daily averages and totals."""
    # Aggregate: tool -> {chain -> count}
    tool_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    chain_totals: dict[str, int] = defaultdict(int)

    for chain, reqs in requests_by_chain.items():
        for req in reqs:
            tool = (req.get("parsedRequest") or {}).get("tool") or "unknown"
            tool_counts[tool][chain] += 1
            chain_totals[chain] += 1

    # Sort by total descending
    sorted_tools = sorted(
        tool_counts.items(),
        key=lambda x: sum(x[1].values()),
        reverse=True,
    )

    grand_total = sum(chain_totals.values())

    sep = "=" * 90
    dash = "-" * 40
    col_dash = "-" * 8
    print(f"\n{sep}")
    print(f"Daily mech requests by tool  ({num_days:.0f}-day window)")
    print(f"{sep}")
    print(
        f"{'TOOL':<40} {'GNOSIS':>8} {'POLYGON':>8} {'TOTAL':>8} {'AVG/DAY':>8}"
    )
    print(f"{dash} {col_dash} {col_dash} {col_dash} {col_dash}")

    source_content_total = 0
    for tool, chains in sorted_tools:
        gn = chains.get("gnosis", 0)
        pg = chains.get("polygon", 0)
        total = gn + pg
        avg = total / num_days if num_days > 0 else 0
        print(f"{tool:<40} {gn:>8} {pg:>8} {total:>8} {avg:>8.1f}")
        if tool in SOURCE_CONTENT_TOOLS:
            source_content_total += total

    print(f"{dash} {col_dash} {col_dash} {col_dash} {col_dash}")
    gn_tot = chain_totals.get("gnosis", 0)
    pg_tot = chain_totals.get("polygon", 0)
    avg_total = grand_total / num_days if num_days > 0 else 0
    print(f"{'TOTAL':<40} {gn_tot:>8} {pg_tot:>8} {grand_total:>8} {avg_total:>8.1f}")

    # Source content impact summary
    sc_avg = source_content_total / num_days if num_days > 0 else 0
    print("\n--- Source content impact (prediction tools + superforcaster) ---")
    print(f"Total requests in window:  {source_content_total}")
    print(f"Avg requests/day:          {sc_avg:.1f}")
    overhead_mb = sc_avg * 1.5
    print(f"Est. IPFS overhead/day:    {overhead_mb:.0f} MB  (@ ~1.5 MB avg per request)")
    print(f"Est. IPFS overhead/day:    {overhead_mb / 1024:.2f} GB")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(description="Count daily mech requests by tool")
    parser.add_argument("--period", default="7d", help="Lookback period, e.g. 7d, 30d (default: 7d)")
    parser.add_argument("--start", help="Start date YYYY-MM-DD (overrides --period)")
    parser.add_argument("--end", help="End date YYYY-MM-DD (default: now)")
    parser.add_argument("--chains", nargs="+", default=["gnosis", "polygon"],
                        choices=["gnosis", "polygon"], help="Chains to query")
    return parser.parse_args()


def main():
    args = parse_args()
    now = datetime.now(timezone.utc)

    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        days = int(args.period.rstrip("d"))
        start = now - timedelta(days=days)

    if args.end:
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        end = now

    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    num_days = (end - start).total_seconds() / 86400

    print(f"Querying {', '.join(args.chains)} from {start.date()} to {end.date()} ({num_days:.1f} days)")

    requests_by_chain = {}
    for chain in args.chains:
        url = SUBGRAPH_URLS[chain]
        print(f"  Fetching {chain}...")
        reqs = fetch_requests(url, start_ts, end_ts)
        print(f"    → {len(reqs)} requests")
        requests_by_chain[chain] = reqs

    analyze(requests_by_chain, num_days)


if __name__ == "__main__":
    main()
