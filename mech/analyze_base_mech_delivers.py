"""
Analyze recent mech delivers on Base chain for a specific mech address.

Fetches delivers from the mech-marketplace-base subgraph, enriches with IPFS data,
and produces a summary analysis (tool breakdown, error rates, response patterns).

Usage:
    python analyze_base_mech_delivers.py 0xe535D7AcDEeD905dddcb5443f41980436833cA2B --period 7d
    python analyze_base_mech_delivers.py 0xe535D7AcDEeD905dddcb5443f41980436833cA2B --period 7d --json
"""

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from requests.exceptions import ConnectionError, Timeout

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUBGRAPH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-base"
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs"

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3

QUERY = """
query MechDelivers($mech: Bytes!, $first: Int!, $skip: Int!, $where: Deliver_filter!) {
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
            parsedRequest {
                tool
                prompt
                questionTitle
            }
        }
    }
}
"""

PAGE_SIZE = 100

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
# Fetch (with pagination)
# ---------------------------------------------------------------------------


def fetch_all_delivers(
    mech: str, start_ts: int | None, end_ts: int | None
) -> list[dict]:
    """Fetch all delivers in the time range, paginating through results."""
    where: dict = {"mech": mech.lower()}
    if start_ts is not None:
        where["blockTimestamp_gt"] = str(start_ts)
    if end_ts is not None:
        where["blockTimestamp_lte"] = str(end_ts)

    all_delivers = []
    skip = 0
    while True:
        variables = {
            "mech": mech.lower(),
            "first": PAGE_SIZE,
            "skip": skip,
            "where": where,
        }
        resp = _post_with_retry(
            SUBGRAPH_URL,
            json={"query": QUERY, "variables": variables},
            headers={"Content-Type": "application/json"},
        )
        data = resp.json()
        if "data" not in data:
            print(f"Subgraph error: {data}", file=sys.stderr)
            sys.exit(1)
        batch = data["data"]["delivers"]
        all_delivers.extend(batch)
        print(f"  Fetched {len(all_delivers)} delivers so far...")
        if len(batch) < PAGE_SIZE:
            break
        skip += PAGE_SIZE

    return all_delivers


# ---------------------------------------------------------------------------
# IPFS enrichment
# ---------------------------------------------------------------------------


def _get_ipfs_hash(deliver: dict) -> str | None:
    mech_del = deliver.get("mechDelivery")
    if mech_del and mech_del.get("ipfsHash"):
        return mech_del["ipfsHash"]
    mkt_del = deliver.get("marketplaceDelivery")
    if mkt_del and mkt_del.get("ipfsHashBytes"):
        return mkt_del["ipfsHashBytes"].replace("0x", "")
    return None


def _fetch_ipfs_delivery(ipfs_hash: str, request_id: str) -> dict | None:
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
# Analysis
# ---------------------------------------------------------------------------


def _ts_to_str(ts: str) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def _classify_response(deliver: dict) -> str:
    """Classify a deliver response as valid, invalid, error, or empty."""
    ipfs = deliver.get("ipfsData") or {}
    result = ipfs.get("result", "")

    if not result:
        raw = deliver.get("toolResponse") or ""
        if not raw:
            return "empty"
        try:
            parsed = json.loads(raw)
            result = parsed.get("result", "")
        except (json.JSONDecodeError, TypeError, AttributeError):
            result = raw

    if not result:
        return "empty"

    result_lower = result.lower() if isinstance(result, str) else ""
    if "invalid response" in result_lower:
        return "invalid"
    if "error" in result_lower:
        return "error"
    return "valid"


def analyze(delivers: list[dict]) -> dict:
    """Produce analysis summary from delivers."""
    total = len(delivers)
    if total == 0:
        return {"total": 0}

    timestamps = [int(d["blockTimestamp"]) for d in delivers]
    earliest = min(timestamps)
    latest = max(timestamps)

    # Tool breakdown
    tool_counter: Counter = Counter()
    # Response classification per tool
    tool_classifications: dict[str, Counter] = {}
    # Model breakdown
    model_counter: Counter = Counter()
    # Senders
    sender_counter: Counter = Counter()
    # Invalid responses detail
    invalid_details: list[dict] = []

    for d in delivers:
        parsed = (d.get("request") or {}).get("parsedRequest") or {}
        tool = parsed.get("tool", "unknown")
        model = d.get("model") or "unknown"
        sender = d.get("sender", "unknown")

        tool_counter[tool] += 1
        model_counter[model] += 1
        sender_counter[sender] += 1

        classification = _classify_response(d)
        if tool not in tool_classifications:
            tool_classifications[tool] = Counter()
        tool_classifications[tool][classification] += 1

        if classification in ("invalid", "error"):
            ipfs = d.get("ipfsData") or {}
            question = parsed.get("questionTitle", "—")
            result = ipfs.get("result", d.get("toolResponse", ""))
            if isinstance(result, str) and len(result) > 200:
                result = result[:200] + "..."
            invalid_details.append({
                "time": _ts_to_str(d["blockTimestamp"]),
                "tool": tool,
                "question": question,
                "result": result,
                "requestId": d["requestId"],
                "tx": d.get("transactionHash", "—"),
            })

    return {
        "total": total,
        "time_range": {
            "earliest": _ts_to_str(str(earliest)),
            "latest": _ts_to_str(str(latest)),
            "span_minutes": round((latest - earliest) / 60, 1),
        },
        "tools": dict(tool_counter.most_common()),
        "tool_classifications": {
            tool: dict(cls) for tool, cls in sorted(tool_classifications.items())
        },
        "models": dict(model_counter.most_common()),
        "unique_senders": len(sender_counter),
        "top_senders": dict(sender_counter.most_common(10)),
        "invalid_or_error_count": len(invalid_details),
        "invalid_details": invalid_details,
    }


def print_analysis(analysis: dict) -> None:
    if analysis["total"] == 0:
        print("No delivers found in the specified period.")
        return

    tr = analysis["time_range"]
    print(f"=== Base Mech Deliver Analysis ===\n")
    print(f"Total delivers: {analysis['total']}")
    print(f"Time range:     {tr['earliest']} → {tr['latest']} ({tr['span_minutes']} min)")
    print(f"Unique senders: {analysis['unique_senders']}")

    print(f"\n--- Tool Breakdown ---")
    for tool, count in analysis["tools"].items():
        cls = analysis["tool_classifications"].get(tool, {})
        valid = cls.get("valid", 0)
        invalid = cls.get("invalid", 0)
        error = cls.get("error", 0)
        empty = cls.get("empty", 0)
        error_rate = ((invalid + error) / count * 100) if count else 0
        print(f"  {tool}: {count} total | {valid} valid | {invalid} invalid | {error} error | {empty} empty | {error_rate:.1f}% error rate")

    print(f"\n--- Model Breakdown ---")
    for model, count in analysis["models"].items():
        print(f"  {model}: {count}")

    if analysis["invalid_details"]:
        print(f"\n--- Invalid/Error Responses ({analysis['invalid_or_error_count']}) ---")
        for item in analysis["invalid_details"][:20]:
            print(f"\n  Time:     {item['time']}")
            print(f"  Tool:     {item['tool']}")
            print(f"  Question: {item['question']}")
            print(f"  Result:   {item['result']}")
            print(f"  Tx:       {item['tx']}")
        if len(analysis["invalid_details"]) > 20:
            print(f"\n  ... and {len(analysis['invalid_details']) - 20} more")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze recent mech delivers on Base chain."
    )
    parser.add_argument(
        "mech",
        nargs="?",
        default="0xe535D7AcDEeD905dddcb5443f41980436833cA2B",
        help="Mech contract address (default: 0xe535D7AcDEeD905dddcb5443f41980436833cA2B)",
    )
    parser.add_argument(
        "--period",
        default="7d",
        help="Look-back period, e.g. 3h, 7d (default: 7d)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON analysis",
    )
    args = parser.parse_args()

    suffix = args.period[-1]
    if suffix not in ("d", "h"):
        parser.error("--period must end with 'd' or 'h', e.g. 7d, 3h.")
    try:
        value = int(args.period[:-1])
    except ValueError:
        parser.error(f"Invalid --period value: {args.period!r}.")
    multiplier = 86400 if suffix == "d" else 3600
    start_ts = int(time.time()) - value * multiplier

    return args.mech, start_ts, args.json_output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    mech, start_ts, json_output = parse_args()

    print(f"Fetching Base chain delivers since {_ts_to_str(str(start_ts))}...")
    delivers = fetch_all_delivers(mech, start_ts, None)

    if not delivers:
        print("No delivers found.")
        sys.exit(0)

    print(f"Enriching {len(delivers)} delivers with IPFS data...")
    enrich_delivers_with_ipfs(delivers)

    analysis = analyze(delivers)

    if json_output:
        print(json.dumps(analysis, indent=2))
    else:
        print()
        print_analysis(analysis)


if __name__ == "__main__":
    main()
