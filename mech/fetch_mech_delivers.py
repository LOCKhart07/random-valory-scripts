"""
Fetch the last N delivers for a specific mech address.

Queries the mech-marketplace subgraph for deliver events including the full
parsed request data (tool, prompt, question, model, response).

Usage:
    python fetch_mech_delivers.py <mech_address>
    python fetch_mech_delivers.py <mech_address> -n 20
    python fetch_mech_delivers.py <mech_address> --start 2026-02-01 --end 2026-03-01
    python fetch_mech_delivers.py <mech_address> --period 7d
    python fetch_mech_delivers.py <mech_address> --json
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from requests.exceptions import ConnectionError, Timeout

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUBGRAPH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs"

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3

QUERY = """
query MechDelivers($mech: Bytes!, $first: Int!, $where: Deliver_filter!) {
    delivers(
        first: $first
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
            parsedRequest {
                tool
                prompt
                questionTitle
            }
        }
    }
}
"""

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


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
        print(
            f"  [retry {attempt}/{MAX_RETRIES - 1}] Error, retrying in {wait}s: {last_exc}"
        )
        time.sleep(wait)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_delivers(
    mech: str, n: int, start_ts: int | None, end_ts: int | None
) -> list[dict]:
    where: dict = {"mech": mech.lower()}
    if start_ts is not None:
        where["blockTimestamp_gt"] = str(start_ts)
    if end_ts is not None:
        where["blockTimestamp_lte"] = str(end_ts)

    variables = {"mech": mech.lower(), "first": n, "where": where}
    resp = _post_with_retry(
        SUBGRAPH_URL,
        json={"query": QUERY, "variables": variables},
        headers={"Content-Type": "application/json"},
    )
    data = resp.json()
    if "data" not in data:
        print(f"Subgraph error: {data}", file=sys.stderr)
        sys.exit(1)
    return data["data"]["delivers"]


# ---------------------------------------------------------------------------
# IPFS delivery data
# ---------------------------------------------------------------------------


def _get_ipfs_hash(deliver: dict) -> str | None:
    """Extract the IPFS hash hex from either mechDelivery or marketplaceDelivery."""
    mech_del = deliver.get("mechDelivery")
    if mech_del and mech_del.get("ipfsHash"):
        return mech_del["ipfsHash"]
    mkt_del = deliver.get("marketplaceDelivery")
    if mkt_del and mkt_del.get("ipfsHashBytes"):
        return mkt_del["ipfsHashBytes"].replace("0x", "")
    return None


def _fetch_ipfs_delivery(ipfs_hash: str, request_id: str) -> dict | None:
    """Fetch the full delivery data from IPFS gateway."""
    hash_upper = ipfs_hash.upper()
    req_id_int = int(request_id, 16)
    url = f"{IPFS_GATEWAY}/f01701220{hash_upper}/{req_id_int}"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def enrich_delivers_with_ipfs(delivers: list[dict]) -> None:
    """Fetch IPFS delivery data in parallel and attach to each deliver."""
    tasks = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        for d in delivers:
            ipfs_hash = _get_ipfs_hash(d)
            if not ipfs_hash:
                continue
            future = executor.submit(
                _fetch_ipfs_delivery, ipfs_hash, d["requestId"]
            )
            tasks[future] = d

        for future in as_completed(tasks):
            deliver = tasks[future]
            result = future.result()
            if result:
                deliver["ipfsData"] = result


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def _ts_to_str(ts: str) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def print_delivers(delivers: list[dict]) -> None:
    for i, d in enumerate(delivers, 1):
        parsed = (d.get("request") or {}).get("parsedRequest") or {}
        tool = parsed.get("tool", "—")
        question = parsed.get("questionTitle", "—")
        model = d.get("model") or "—"
        tx = d.get("transactionHash", "—")
        ts = _ts_to_str(d["blockTimestamp"])

        # Try to pretty-print toolResponse JSON
        raw_response = d.get("toolResponse") or ""
        try:
            response_obj = json.loads(raw_response)
            response_str = json.dumps(response_obj, indent=2)
        except (json.JSONDecodeError, TypeError):
            response_str = raw_response

        ipfs = d.get("ipfsData") or {}
        deliver_prompt = ipfs.get("prompt", "—")

        print(f"── Deliver {i} ──")
        print(f"  Time:     {ts}")
        print(f"  Tool:     {tool}")
        print(f"  Model:    {model}")
        print(f"  Question: {question}")
        print(f"  Tx:       {tx}")
        print(f"  Response: {response_str}")
        print(f"  Prompt:   {deliver_prompt}")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fetch the last N delivers for a specific mech."
    )
    parser.add_argument("mech", help="Mech contract address (0x...)")
    parser.add_argument(
        "-n", type=int, default=10, help="Number of delivers to fetch (default: 10)"
    )

    time_group = parser.add_mutually_exclusive_group()
    time_group.add_argument(
        "--period", metavar="Nd", help="Look-back period, e.g. 7d, 30d"
    )
    time_group.add_argument("--start", metavar="YYYY-MM-DD", help="Start date (UTC)")
    parser.add_argument(
        "--end",
        metavar="YYYY-MM-DD",
        help="End date (UTC, inclusive). Only with --start.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of formatted text",
    )

    args = parser.parse_args()

    start_ts = None
    end_ts = None

    if args.start:
        try:
            start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            parser.error(f"Invalid --start date: {args.start!r}. Expected YYYY-MM-DD.")
        start_ts = int(start_dt.timestamp())

        if args.end:
            try:
                end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )
            except ValueError:
                parser.error(f"Invalid --end date: {args.end!r}. Expected YYYY-MM-DD.")
            end_ts = int(end_dt.timestamp())
    elif args.period:
        if args.end:
            parser.error("--end requires --start.")
        if not args.period.endswith("d"):
            parser.error("--period must end with 'd', e.g. 7d, 30d.")
        try:
            days = int(args.period[:-1])
        except ValueError:
            parser.error(f"Invalid --period value: {args.period!r}.")
        start_ts = int(time.time()) - days * 86400
    elif args.end:
        parser.error("--end requires --start.")

    return args.mech, args.n, start_ts, end_ts, args.json_output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    mech, n, start_ts, end_ts, json_output = parse_args()

    delivers = fetch_delivers(mech, n, start_ts, end_ts)

    if not delivers:
        print("No delivers found.")
        sys.exit(0)

    print(f"Fetching IPFS delivery data for {len(delivers)} delivers...")
    enrich_delivers_with_ipfs(delivers)

    if json_output:
        print(json.dumps(delivers, indent=2))
    else:
        print(f"Found {len(delivers)} delivers for {mech}\n")
        print_delivers(delivers)


if __name__ == "__main__":
    main()
