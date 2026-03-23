"""
High-granularity tool usage analysis for the PolyStrat fleet.

Supports hour-level and minute-level breakdowns, relative time windows
(--last 6h, --last 30m), per-request event logs sorted by timestamp,
and burst detection.

Usage:
    # Last 6 hours, hourly breakdown
    python polymarket/analyze_tool_usage_granular.py --last 6h

    # Last 30 minutes, minute-level
    python polymarket/analyze_tool_usage_granular.py --last 30m --bucket minute

    # Last 2 hours, 15-minute buckets
    python polymarket/analyze_tool_usage_granular.py --last 2h --bucket 15m

    # Exact time range (ISO timestamps)
    python polymarket/analyze_tool_usage_granular.py --from "2026-03-20T14:00" --to "2026-03-20T20:00"

    # Show every individual request
    python polymarket/analyze_tool_usage_granular.py --last 1h --events

    # JSON output
    python polymarket/analyze_tool_usage_granular.py --last 6h --json

    # CSV output
    python polymarket/analyze_tool_usage_granular.py --last 6h --csv out.csv
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLYMARKET_BETS_SUBGRAPH_URL = (
    "https://predict-polymarket-agents.subgraph.autonolas.tech/"
)
THE_GRAPH_API_KEY = os.getenv("THE_GRAPH_API_KEY")
POLYGON_REGISTRY_SUBGRAPH_URL = (
    f"https://gateway.thegraph.com/api/{THE_GRAPH_API_KEY}/subgraphs/id/"
    f"HHRBjVWFT2bV7eNSRqbCNDtUVnLPt911hcp8mSe4z6KG"
    if THE_GRAPH_API_KEY
    else None
)
OLAS_MECH_SUBGRAPH_URL = (
    "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"
)

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post_with_retry(url, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            time.sleep(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
    raise last_exc


def call_subgraph(url, query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = _post_with_retry(
        url, json=payload, headers={"Content-Type": "application/json"}
    )
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Subgraph error: {data['errors']}")
    return data


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_duration(s):
    """Parse '6h', '30m', '2d' into a timedelta."""
    m = re.match(r"^(\d+)\s*(m|min|h|hr|d|day)s?$", s.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(
            f"Invalid duration '{s}'. Use e.g. 6h, 30m, 2d"
        )
    val = int(m.group(1))
    unit = m.group(2)
    if unit in ("m", "min"):
        return timedelta(minutes=val)
    if unit in ("h", "hr"):
        return timedelta(hours=val)
    return timedelta(days=val)


def parse_iso(s):
    """Parse ISO-ish datetime: YYYY-MM-DD or YYYY-MM-DDTHH:MM."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Cannot parse datetime '{s}'")


def parse_bucket_size(s):
    """Parse bucket size: 'minute', 'hour', '15m', '5m', '30m'."""
    s = s.strip().lower()
    if s in ("minute", "1m", "min"):
        return timedelta(minutes=1), "minute"
    if s in ("hour", "1h", "hr"):
        return timedelta(hours=1), "hour"
    if s in ("day", "1d"):
        return timedelta(days=1), "day"
    m = re.match(r"^(\d+)\s*(m|min|h|hr)$", s)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if unit in ("m", "min"):
            return timedelta(minutes=val), f"{val}m"
        return timedelta(hours=val), f"{val}h"
    raise argparse.ArgumentTypeError(
        f"Invalid bucket '{s}'. Use minute, hour, 5m, 15m, 30m, etc."
    )


def ts_to_datetime(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def bucket_key(dt, bucket_delta, bucket_label):
    """Snap a datetime to the start of its bucket."""
    if bucket_label == "hour":
        return dt.strftime("%Y-%m-%d %H:00")
    if bucket_label == "minute":
        return dt.strftime("%Y-%m-%d %H:%M")
    if bucket_label == "day":
        return dt.strftime("%Y-%m-%d")
    # Custom bucket: snap to bucket boundaries from epoch
    epoch = datetime(2020, 1, 1, tzinfo=timezone.utc)
    secs = (dt - epoch).total_seconds()
    bucket_secs = bucket_delta.total_seconds()
    snapped = epoch + timedelta(
        seconds=int(secs // bucket_secs) * bucket_secs
    )
    return snapped.strftime("%Y-%m-%d %H:%M")


def generate_all_buckets(start_dt, end_dt, bucket_delta, bucket_label):
    """Generate all bucket keys between start and end, even if empty."""
    keys = []
    current = start_dt
    while current <= end_dt:
        keys.append(bucket_key(current, bucket_delta, bucket_label))
        current += bucket_delta
    return keys


# ---------------------------------------------------------------------------
# Data fetching (reused from analyze_tool_usage.py)
# ---------------------------------------------------------------------------


def get_all_polystrat_agents():
    if POLYGON_REGISTRY_SUBGRAPH_URL:
        try:
            query = '{ services(where: { agentIds_contains: [86] }, first: 1000) { id multisig } }'
            response = call_subgraph(POLYGON_REGISTRY_SUBGRAPH_URL, query)
            return [s["multisig"].lower() for s in response["data"]["services"]]
        except Exception:
            pass
    query = '{ traderAgents(first: 1000, orderBy: totalBets, orderDirection: desc) { id } }'
    response = call_subgraph(POLYMARKET_BETS_SUBGRAPH_URL, query)
    return [a["id"].lower() for a in response.get("data", {}).get("traderAgents", [])]


GET_MECH_SENDER_QUERY = """
query MechSender($id: ID!, $timestamp_gt: Int!, $skip: Int, $first: Int) {
    sender(id: $id) {
        requests(first: $first, skip: $skip,
                 where: { blockTimestamp_gt: $timestamp_gt }) {
            blockTimestamp
            parsedRequest { tool }
        }
    }
}
"""


def fetch_mech_requests(agent_address, timestamp_gt, batch_size=1000):
    all_requests = []
    skip = 0
    while True:
        variables = {
            "id": agent_address,
            "timestamp_gt": timestamp_gt,
            "skip": skip,
            "first": batch_size,
        }
        try:
            response = _post_with_retry(
                OLAS_MECH_SUBGRAPH_URL,
                json={"query": GET_MECH_SENDER_QUERY, "variables": variables},
                headers={"Content-Type": "application/json"},
            )
            data = response.json()
            result = (data.get("data") or {}).get("sender") or {}
            batch = result.get("requests", [])
        except Exception:
            break
        if not batch:
            break
        all_requests.extend(batch)
        if len(batch) < batch_size:
            break
        skip += batch_size
    return all_requests


def collect_all_requests(agents, from_ts, to_ts, sample_size=None):
    if sample_size:
        agents = agents[:sample_size]

    all_records = []
    total = len(agents)
    agents_with_requests = 0

    for i, addr in enumerate(agents):
        print(f"\r  [{i + 1}/{total}] {addr[:10]}...", end="", flush=True)
        try:
            reqs = fetch_mech_requests(addr, timestamp_gt=from_ts)
        except Exception as e:
            print(f" ERROR: {e}")
            continue

        if not reqs:
            continue

        agents_with_requests += 1
        for req in reqs:
            ts = int(req.get("blockTimestamp", 0))
            if to_ts and ts > to_ts:
                continue
            tool = (req.get("parsedRequest") or {}).get("tool") or "unknown"
            all_records.append({
                "agent": addr,
                "tool": tool,
                "timestamp": ts,
            })

    print(f"\n  Agents with requests: {agents_with_requests}/{total}")
    print(f"  Total mech requests: {len(all_records)}")
    return all_records


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze_overall(records):
    counts = defaultdict(int)
    for r in records:
        counts[r["tool"]] += 1
    total = len(records)
    return [
        {"tool": tool, "requests": count,
         "pct": round(count / total * 100, 2) if total else 0}
        for tool, count in sorted(counts.items(), key=lambda x: -x[1])
    ]


def analyze_by_bucket(records, bucket_delta, bucket_label, start_dt, end_dt):
    """Tool usage grouped by time bucket with all buckets filled."""
    buckets = defaultdict(lambda: defaultdict(int))
    agents_by_bucket = defaultdict(set)
    for r in records:
        dt = ts_to_datetime(r["timestamp"])
        key = bucket_key(dt, bucket_delta, bucket_label)
        buckets[key][r["tool"]] += 1
        agents_by_bucket[key].add(r["agent"])

    all_keys = generate_all_buckets(start_dt, end_dt, bucket_delta, bucket_label)
    result = {}
    for key in all_keys:
        tool_counts = buckets.get(key, {})
        total = sum(tool_counts.values())
        result[key] = {
            "total": total,
            "agents": len(agents_by_bucket.get(key, set())),
            "tools": dict(sorted(tool_counts.items(), key=lambda x: -x[1])),
        }
    return result


def detect_bursts(records, window_secs=300, threshold=10):
    """Find time windows with unusually high request density."""
    if not records:
        return []
    sorted_recs = sorted(records, key=lambda r: r["timestamp"])
    bursts = []
    i = 0
    while i < len(sorted_recs):
        window_end = sorted_recs[i]["timestamp"] + window_secs
        j = i
        while j < len(sorted_recs) and sorted_recs[j]["timestamp"] <= window_end:
            j += 1
        count = j - i
        if count >= threshold:
            start_ts = sorted_recs[i]["timestamp"]
            end_ts = sorted_recs[j - 1]["timestamp"]
            tools = defaultdict(int)
            agents = set()
            for k in range(i, j):
                tools[sorted_recs[k]["tool"]] += 1
                agents.add(sorted_recs[k]["agent"])
            bursts.append({
                "start": ts_to_datetime(start_ts).strftime("%H:%M:%S"),
                "end": ts_to_datetime(end_ts).strftime("%H:%M:%S"),
                "count": count,
                "agents": len(agents),
                "duration_s": end_ts - start_ts,
                "tools": dict(sorted(tools.items(), key=lambda x: -x[1])),
            })
            i = j
        else:
            i += 1
    return bursts


def build_event_log(records):
    """Sorted list of individual requests for the event log view."""
    sorted_recs = sorted(records, key=lambda r: r["timestamp"])
    events = []
    for r in sorted_recs:
        dt = ts_to_datetime(r["timestamp"])
        events.append({
            "time": dt.strftime("%H:%M:%S"),
            "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "agent": r["agent"],
            "tool": r["tool"],
        })
    return events


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_report(overall, bucketed, bucket_label, events, bursts,
                 start_dt, end_dt, show_events):
    w = 90
    duration = end_dt - start_dt
    hours = duration.total_seconds() / 3600

    print("\n" + "=" * w)
    print("  POLYSTRAT FLEET — GRANULAR TOOL USAGE")
    print(f"  {start_dt.strftime('%Y-%m-%d %H:%M')} → "
          f"{end_dt.strftime('%Y-%m-%d %H:%M')} UTC "
          f"({hours:.1f}h, bucket={bucket_label})")
    print("=" * w)

    # --- Overall ---
    print("\n--- OVERALL ---")
    col_t = max((len(t["tool"]) for t in overall), default=10)
    col_t = min(col_t, 45)
    print(f"  {'Tool':<{col_t}}  {'Reqs':>6}  {'Share':>7}")
    print(f"  {'-' * col_t}  {'-' * 6}  {'-' * 7}")
    for t in overall:
        bar = "#" * max(1, int(t["pct"] / 2))
        print(f"  {t['tool']:<{col_t}}  {t['requests']:>6}  "
              f"{t['pct']:>5.1f}%  {bar}")

    # --- Bucketed timeline ---
    top_tools = [t["tool"] for t in overall[:5]]
    print(f"\n--- TIMELINE ({bucket_label} buckets) ---")
    # Header
    tool_hdrs = "  ".join(f"{t[:16]:>16}" for t in top_tools)
    print(f"  {'Bucket':<18}  {'Total':>5}  {'Agents':>6}  {tool_hdrs}")
    sep = "  ".join("-" * 16 for _ in top_tools)
    print(f"  {'-' * 18}  {'-' * 5}  {'-' * 6}  {sep}")

    for key, data in bucketed.items():
        cols = []
        for t in top_tools:
            c = data["tools"].get(t, 0)
            if c:
                pct = c / data["total"] * 100 if data["total"] else 0
                cols.append(f"{c:>4} ({pct:4.0f}%)")
            else:
                cols.append(f"{'·':>16}")
        print(f"  {key:<18}  {data['total']:>5}  {data['agents']:>6}  "
              f"{'  '.join(cols)}")

    # --- Bursts ---
    if bursts:
        print(f"\n--- BURSTS (>= threshold in 5min window) ---")
        print(f"  {'Window':<20}  {'Reqs':>5}  {'Agents':>6}  "
              f"{'Duration':>8}  Top tools")
        print(f"  {'-' * 20}  {'-' * 5}  {'-' * 6}  {'-' * 8}  {'-' * 30}")
        for b in bursts:
            top = ", ".join(
                f"{t}({c})" for t, c in list(b["tools"].items())[:3]
            )
            print(f"  {b['start']}-{b['end']:<11}  {b['count']:>5}  "
                  f"{b['agents']:>6}  {b['duration_s']:>6}s  {top}")

    # --- Event log ---
    if show_events and events:
        print(f"\n--- EVENT LOG ({len(events)} requests) ---")
        print(f"  {'Time':<10}  {'Agent':<44}  Tool")
        print(f"  {'-' * 10}  {'-' * 44}  {'-' * 35}")
        for e in events:
            print(f"  {e['time']:<10}  {e['agent']:<44}  {e['tool']}")

    # --- Summary ---
    total_reqs = sum(t["requests"] for t in overall)
    total_tools = len(overall)
    nonempty = [d for d in bucketed.values() if d["total"] > 0]
    all_agents = set()
    for e in events:
        all_agents.add(e["agent"])

    print(f"\n--- SUMMARY ---")
    print(f"  Time range:         {start_dt.strftime('%H:%M')} → "
          f"{end_dt.strftime('%H:%M')} UTC ({hours:.1f}h)")
    print(f"  Total requests:     {total_reqs:,}")
    print(f"  Unique tools:       {total_tools}")
    print(f"  Active agents:      {len(all_agents)}")
    if nonempty:
        avg_per_bucket = total_reqs / len(nonempty)
        peak = max(nonempty, key=lambda d: d["total"])
        peak_key = [k for k, v in bucketed.items() if v is peak][0]
        print(f"  Avg reqs/bucket:    {avg_per_bucket:.1f}")
        print(f"  Peak bucket:        {peak_key} ({peak['total']} reqs, "
              f"{peak['agents']} agents)")
    if bursts:
        print(f"  Bursts detected:    {len(bursts)}")
    print("\n" + "=" * w)


def write_csv(overall, bucketed, events, csv_path):
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)

        writer.writerow(["# Overall"])
        writer.writerow(["tool", "requests", "share_pct"])
        for t in overall:
            writer.writerow([t["tool"], t["requests"], t["pct"]])

        writer.writerow([])
        all_tools = sorted({
            tool for data in bucketed.values() for tool in data["tools"]
        })
        writer.writerow(["# Timeline"])
        writer.writerow(["bucket", "total", "agents"] + all_tools)
        for key, data in bucketed.items():
            row = [key, data["total"], data["agents"]]
            for tool in all_tools:
                row.append(data["tools"].get(tool, 0))
            writer.writerow(row)

        writer.writerow([])
        writer.writerow(["# Events"])
        writer.writerow(["datetime", "agent", "tool"])
        for e in events:
            writer.writerow([e["datetime"], e["agent"], e["tool"]])

    print(f"CSV written to: {csv_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="High-granularity tool usage analysis for the PolyStrat fleet."
    )
    parser.add_argument(
        "--last", default=None,
        help="Relative time window: 6h, 30m, 2d (default: 6h)",
    )
    parser.add_argument(
        "--from", dest="from_dt", default=None,
        help="Start datetime (YYYY-MM-DD or YYYY-MM-DDTHH:MM)",
    )
    parser.add_argument(
        "--to", dest="to_dt", default=None,
        help="End datetime (default: now)",
    )
    parser.add_argument(
        "--bucket", default=None,
        help="Bucket size: minute, hour, 5m, 15m, 30m (default: auto)",
    )
    parser.add_argument(
        "--events", action="store_true",
        help="Show per-request event log",
    )
    parser.add_argument(
        "--burst-window", type=int, default=300,
        help="Burst detection window in seconds (default: 300)",
    )
    parser.add_argument(
        "--burst-threshold", type=int, default=10,
        help="Min requests in window to flag as burst (default: 10)",
    )
    parser.add_argument(
        "--sample", type=int, default=None,
        help="Limit number of agents (for speed)",
    )
    parser.add_argument(
        "--exclude-valory", action="store_true",
        help="Exclude Valory team-owned agents",
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--csv", dest="csv_path", default=None,
        help="Write CSV to file",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    # Resolve time window
    if args.from_dt:
        start_dt = parse_iso(args.from_dt)
    elif args.last:
        start_dt = now - parse_duration(args.last)
    else:
        start_dt = now - timedelta(hours=6)

    end_dt = parse_iso(args.to_dt) if args.to_dt else now

    from_ts = int(start_dt.timestamp())
    to_ts = int(end_dt.timestamp())

    # Auto-select bucket size if not specified
    if args.bucket:
        bucket_delta, bucket_label = parse_bucket_size(args.bucket)
    else:
        span = (end_dt - start_dt).total_seconds()
        if span <= 3600:  # <= 1h → minute buckets
            bucket_delta, bucket_label = timedelta(minutes=1), "minute"
        elif span <= 7200:  # <= 2h → 5m buckets
            bucket_delta, bucket_label = timedelta(minutes=5), "5m"
        elif span <= 21600:  # <= 6h → 15m buckets
            bucket_delta, bucket_label = timedelta(minutes=15), "15m"
        elif span <= 86400:  # <= 24h → hour buckets
            bucket_delta, bucket_label = timedelta(hours=1), "hour"
        else:
            bucket_delta, bucket_label = timedelta(days=1), "day"

    print(f"Time: {start_dt.strftime('%Y-%m-%d %H:%M')} → "
          f"{end_dt.strftime('%Y-%m-%d %H:%M')} UTC  "
          f"bucket={bucket_label}")

    print("Fetching PolyStrat agent list...")
    agents = get_all_polystrat_agents()
    print(f"Found {len(agents)} agents")

    if args.exclude_valory:
        team_file = os.path.join(
            os.path.dirname(__file__), "data", "valory_team_agents.json"
        )
        with open(team_file) as f:
            team_data = json.load(f)
        exclude_set = {a["address"].lower() for a in team_data["agents"]}
        before = len(agents)
        agents = [a for a in agents if a not in exclude_set]
        print(f"Excluded {before - len(agents)} agents, {len(agents)} remaining")

    print("Fetching mech requests...")
    records = collect_all_requests(
        agents, from_ts=from_ts, to_ts=to_ts, sample_size=args.sample
    )

    if not records:
        print("No mech requests found in this window.", file=sys.stderr)
        sys.exit(1)

    overall = analyze_overall(records)
    bucketed = analyze_by_bucket(
        records, bucket_delta, bucket_label, start_dt, end_dt
    )
    events = build_event_log(records)
    bursts = detect_bursts(
        records, window_secs=args.burst_window,
        threshold=args.burst_threshold
    )

    if args.json_output:
        output = {
            "window": {
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "bucket": bucket_label,
            },
            "overall": overall,
            "timeline": bucketed,
            "bursts": bursts,
            "events": events,
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print_report(
            overall, bucketed, bucket_label, events, bursts,
            start_dt, end_dt, show_events=args.events,
        )

    if args.csv_path:
        write_csv(overall, bucketed, events, args.csv_path)


if __name__ == "__main__":
    main()
