"""
Analyze tool usage patterns across the PolyStrat fleet over time.

Fetches mech requests for all polystrat agents and produces usage
breakdowns: overall tool popularity, weekly/monthly trends, tool
adoption timelines, and per-agent tool diversity.

Usage:
    # Full fleet analysis (all time)
    python polymarket/analyze_tool_usage.py

    # Last 90 days
    python polymarket/analyze_tool_usage.py --from 2025-12-19

    # Limit agents for speed
    python polymarket/analyze_tool_usage.py --sample 30

    # JSON output
    python polymarket/analyze_tool_usage.py --json

    # Save CSV breakdown
    python polymarket/analyze_tool_usage.py --csv polymarket/tool_usage.csv
"""

import argparse
import csv
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

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
# HTTP / subgraph helpers
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
# Date helpers
# ---------------------------------------------------------------------------


def parse_date(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def ts_to_week(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-W%W")


def ts_to_month(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def ts_to_date(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Data fetching
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


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def collect_all_requests(agents, from_ts, to_ts, sample_size=None):
    """Fetch mech requests for the fleet and return flat list with agent info."""
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


def analyze_overall(records):
    """Overall tool usage counts."""
    counts = defaultdict(int)
    for r in records:
        counts[r["tool"]] += 1
    total = len(records)
    results = []
    for tool, count in sorted(counts.items(), key=lambda x: -x[1]):
        results.append({
            "tool": tool,
            "requests": count,
            "pct": round(count / total * 100, 2) if total else 0,
        })
    return results


def analyze_by_period(records, period_fn):
    """Tool usage grouped by time period (week or month)."""
    # period -> tool -> count
    buckets = defaultdict(lambda: defaultdict(int))
    for r in records:
        period = period_fn(r["timestamp"])
        buckets[period][r["tool"]] += 1

    # Also track totals per period
    result = {}
    for period in sorted(buckets):
        tool_counts = buckets[period]
        total = sum(tool_counts.values())
        result[period] = {
            "total": total,
            "tools": dict(sorted(tool_counts.items(), key=lambda x: -x[1])),
        }
    return result


def analyze_tool_adoption(records):
    """For each tool, when it first appeared and its usage trajectory."""
    tool_first_seen = {}
    tool_last_seen = {}
    tool_counts = defaultdict(int)
    tool_agent_set = defaultdict(set)

    for r in records:
        tool = r["tool"]
        ts = r["timestamp"]
        tool_counts[tool] += 1
        tool_agent_set[tool].add(r["agent"])
        if tool not in tool_first_seen or ts < tool_first_seen[tool]:
            tool_first_seen[tool] = ts
        if tool not in tool_last_seen or ts > tool_last_seen[tool]:
            tool_last_seen[tool] = ts

    results = []
    for tool in sorted(tool_counts, key=lambda t: -tool_counts[t]):
        results.append({
            "tool": tool,
            "total_requests": tool_counts[tool],
            "unique_agents": len(tool_agent_set[tool]),
            "first_seen": ts_to_date(tool_first_seen[tool]),
            "last_seen": ts_to_date(tool_last_seen[tool]),
        })
    return results


def analyze_agent_diversity(records):
    """How many tools each agent uses."""
    agent_tools = defaultdict(set)
    agent_counts = defaultdict(int)
    for r in records:
        agent_tools[r["agent"]].add(r["tool"])
        agent_counts[r["agent"]] += 1

    diversity = []
    for agent in sorted(agent_counts, key=lambda a: -agent_counts[a]):
        diversity.append({
            "agent": agent,
            "total_requests": agent_counts[agent],
            "unique_tools": len(agent_tools[agent]),
            "tools": sorted(agent_tools[agent]),
        })
    return diversity


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def analyze_head_to_head(records, tool_a, tool_b):
    """Per-day head-to-head comparison of two tools."""
    # day -> {tool -> count, tool -> agents}
    daily = defaultdict(lambda: {
        "a_count": 0, "b_count": 0, "other_count": 0,
        "a_agents": set(), "b_agents": set(), "total_agents": set(),
    })
    for r in records:
        day = ts_to_date(r["timestamp"])
        d = daily[day]
        d["total_agents"].add(r["agent"])
        if r["tool"] == tool_a:
            d["a_count"] += 1
            d["a_agents"].add(r["agent"])
        elif r["tool"] == tool_b:
            d["b_count"] += 1
            d["b_agents"].add(r["agent"])
        else:
            d["other_count"] += 1

    result = {}
    for day in sorted(daily):
        d = daily[day]
        total = d["a_count"] + d["b_count"] + d["other_count"]
        result[day] = {
            "total": total,
            "a_count": d["a_count"],
            "b_count": d["b_count"],
            "other_count": d["other_count"],
            "a_pct": round(d["a_count"] / total * 100, 1) if total else 0,
            "b_pct": round(d["b_count"] / total * 100, 1) if total else 0,
            "a_agents": len(d["a_agents"]),
            "b_agents": len(d["b_agents"]),
            "total_agents": len(d["total_agents"]),
            # agents using ONLY tool_b (not tool_a) that day
            "b_exclusive_agents": len(d["b_agents"] - d["a_agents"]),
            "a_exclusive_agents": len(d["a_agents"] - d["b_agents"]),
            "both_agents": len(d["a_agents"] & d["b_agents"]),
        }
    return result


def print_report(overall, monthly, weekly, daily, adoption, diversity,
                 head_to_head=None, tool_a=None, tool_b=None):
    w = 80
    print("\n" + "=" * w)
    print("  POLYSTRAT FLEET — TOOL USAGE ANALYSIS")
    print("=" * w)

    # --- Overall ---
    print("\n--- OVERALL TOOL USAGE ---")
    col_t = max((len(t["tool"]) for t in overall), default=10)
    col_t = min(col_t, 50)
    print(f"  {'Tool':<{col_t}}  {'Requests':>9}  {'Share':>7}")
    print(f"  {'-' * col_t}  {'-' * 9}  {'-' * 7}")
    for t in overall:
        bar = "#" * max(1, int(t["pct"] / 2))
        print(f"  {t['tool']:<{col_t}}  {t['requests']:>9}  {t['pct']:>6.1f}%  {bar}")

    # --- Monthly trends ---
    print("\n--- MONTHLY USAGE ---")
    # Get top tools for column display
    top_tools = [t["tool"] for t in overall[:8]]
    # Header
    tool_cols = "  ".join(f"{t[:18]:>18}" for t in top_tools)
    print(f"  {'Month':<8}  {'Total':>6}  {tool_cols}")
    print(f"  {'-' * 8}  {'-' * 6}  " + "  ".join("-" * 18 for _ in top_tools))
    for period, data in monthly.items():
        cols = []
        for t in top_tools:
            count = data["tools"].get(t, 0)
            pct = count / data["total"] * 100 if data["total"] else 0
            cols.append(f"{count:>5} ({pct:4.0f}%)" if count else f"{'—':>18}")
        print(f"  {period:<8}  {data['total']:>6}  {'  '.join(cols)}")

    # --- Weekly trends (last 12 weeks) ---
    print("\n--- WEEKLY USAGE (recent) ---")
    recent_weeks = dict(list(weekly.items())[-12:])
    top3 = [t["tool"] for t in overall[:3]]
    tool_cols = "  ".join(f"{t[:20]:>20}" for t in top3)
    print(f"  {'Week':<10}  {'Total':>6}  {tool_cols}")
    print(f"  {'-' * 10}  {'-' * 6}  " + "  ".join("-" * 20 for _ in top3))
    for period, data in recent_weeks.items():
        cols = []
        for t in top3:
            count = data["tools"].get(t, 0)
            pct = count / data["total"] * 100 if data["total"] else 0
            cols.append(f"{count:>6} ({pct:4.0f}%)" if count else f"{'—':>20}")
        print(f"  {period:<10}  {data['total']:>6}  {'  '.join(cols)}")

    # --- Tool adoption ---
    print("\n--- TOOL ADOPTION TIMELINE ---")
    col_t = max((len(t["tool"]) for t in adoption), default=10)
    col_t = min(col_t, 45)
    print(f"  {'Tool':<{col_t}}  {'Requests':>9}  {'Agents':>7}  {'First Seen':>12}  {'Last Seen':>12}")
    print(f"  {'-' * col_t}  {'-' * 9}  {'-' * 7}  {'-' * 12}  {'-' * 12}")
    for t in adoption:
        print(
            f"  {t['tool']:<{col_t}}  {t['total_requests']:>9}  "
            f"{t['unique_agents']:>7}  {t['first_seen']:>12}  {t['last_seen']:>12}"
        )

    # --- Agent diversity ---
    print("\n--- AGENT TOOL DIVERSITY (top 20) ---")
    print(f"  {'Agent':<44}  {'Requests':>9}  {'Tools':>6}  Tool List")
    print(f"  {'-' * 44}  {'-' * 9}  {'-' * 6}  {'-' * 30}")
    for a in diversity[:20]:
        tool_list = ", ".join(t[:25] for t in a["tools"][:5])
        if len(a["tools"]) > 5:
            tool_list += f" (+{len(a['tools']) - 5} more)"
        print(f"  {a['agent']:<44}  {a['total_requests']:>9}  {a['unique_tools']:>6}  {tool_list}")

    # --- Daily breakdown ---
    if daily:
        print("\n--- DAILY USAGE ---")
        top3 = [t["tool"] for t in overall[:3]]
        tool_cols = "  ".join(f"{t[:22]:>22}" for t in top3)
        print(f"  {'Date':<12}  {'Total':>6}  {tool_cols}")
        print(f"  {'-' * 12}  {'-' * 6}  " + "  ".join("-" * 22 for _ in top3))
        for period, data in daily.items():
            cols = []
            for t in top3:
                count = data["tools"].get(t, 0)
                pct = count / data["total"] * 100 if data["total"] else 0
                cols.append(f"{count:>7} ({pct:4.0f}%)" if count else f"{'—':>22}")
            print(f"  {period:<12}  {data['total']:>6}  {'  '.join(cols)}")

    # --- Head-to-head ---
    if head_to_head and tool_a and tool_b:
        a_short = tool_a[:25]
        b_short = tool_b[:25]
        print(f"\n--- HEAD-TO-HEAD: {a_short} vs {b_short} ---")
        print()
        print(f"  {'Date':<12}  {'Total':>6}  "
              f"{a_short:>25}  {b_short:>25}  {'Other':>7}")
        print(f"  {'-' * 12}  {'-' * 6}  {'-' * 25}  {'-' * 25}  {'-' * 7}")
        for day, d in head_to_head.items():
            a_str = f"{d['a_count']:>6} ({d['a_pct']:4.1f}%)"
            b_str = f"{d['b_count']:>6} ({d['b_pct']:4.1f}%)"
            print(f"  {day:<12}  {d['total']:>6}  {a_str:>25}  {b_str:>25}  {d['other_count']:>7}")

        print()
        print(f"  {'Date':<12}  {'Active':>7}  "
              f"{a_short + ' only':>25}  {b_short + ' only':>25}  {'Both':>6}")
        print(f"  {'-' * 12}  {'-' * 7}  {'-' * 25}  {'-' * 25}  {'-' * 6}")
        for day, d in head_to_head.items():
            print(f"  {day:<12}  {d['total_agents']:>7}  "
                  f"{d['a_exclusive_agents']:>25}  {d['b_exclusive_agents']:>25}  "
                  f"{d['both_agents']:>6}")

        # Ratio trend
        print()
        print(f"  {'Date':<12}  {a_short}/{b_short} ratio")
        print(f"  {'-' * 12}  {'-' * 30}")
        for day, d in head_to_head.items():
            if d["b_count"] > 0:
                ratio = d["a_count"] / d["b_count"]
                bar_a = "#" * min(50, int(d["a_pct"] / 2))
                bar_b = "*" * min(50, int(d["b_pct"] / 2))
                print(f"  {day:<12}  {ratio:>5.1f}:1  {bar_a}{bar_b}")
            else:
                print(f"  {day:<12}  {'∞':>5}    (no {b_short} requests)")

    # --- Summary stats ---
    print(f"\n--- SUMMARY ---")
    total_requests = sum(t["requests"] for t in overall)
    total_tools = len(overall)
    total_agents = len(diversity)
    avg_tools_per_agent = (
        sum(a["unique_tools"] for a in diversity) / total_agents
        if total_agents else 0
    )
    print(f"  Total mech requests:    {total_requests:,}")
    print(f"  Unique tools:           {total_tools}")
    print(f"  Agents with requests:   {total_agents}")
    print(f"  Avg tools per agent:    {avg_tools_per_agent:.1f}")

    print("\n" + "=" * w)


def write_csv(overall, monthly, adoption, csv_path):
    """Write a multi-sheet CSV with tool usage data."""
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)

        writer.writerow(["# Overall Tool Usage"])
        writer.writerow(["tool", "requests", "share_pct"])
        for t in overall:
            writer.writerow([t["tool"], t["requests"], t["pct"]])

        writer.writerow([])
        writer.writerow(["# Monthly Breakdown"])
        # Collect all tools
        all_tools = sorted({
            tool
            for data in monthly.values()
            for tool in data["tools"]
        })
        writer.writerow(["month", "total"] + all_tools)
        for period, data in monthly.items():
            row = [period, data["total"]]
            for tool in all_tools:
                row.append(data["tools"].get(tool, 0))
            writer.writerow(row)

        writer.writerow([])
        writer.writerow(["# Tool Adoption"])
        writer.writerow(["tool", "total_requests", "unique_agents", "first_seen", "last_seen"])
        for t in adoption:
            writer.writerow([
                t["tool"], t["total_requests"], t["unique_agents"],
                t["first_seen"], t["last_seen"],
            ])

    print(f"CSV written to: {csv_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Analyze tool usage patterns across the PolyStrat fleet."
    )
    parser.add_argument(
        "--sample", type=int, default=None,
        help="Limit number of agents to process (for speed)",
    )
    parser.add_argument(
        "--from", dest="from_date", default=None,
        help="Start date (YYYY-MM-DD, default: all time)",
    )
    parser.add_argument(
        "--to", dest="to_date", default=None,
        help="End date (YYYY-MM-DD, default: now)",
    )
    parser.add_argument(
        "--exclude", nargs="*", default=None,
        help="Agent addresses to exclude (space-separated)",
    )
    parser.add_argument(
        "--exclude-valory", action="store_true",
        help="Exclude Valory team-owned agents (from data/valory_team_agents.json)",
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--csv", dest="csv_path", default=None,
        help="Write CSV breakdown to file",
    )
    args = parser.parse_args()

    from_ts = parse_date(args.from_date) if args.from_date else 0
    to_ts = parse_date(args.to_date) if args.to_date else None

    if args.from_date or args.to_date:
        print(f"Time window: {args.from_date or 'beginning'} to {args.to_date or 'now'}")
    else:
        print("Time window: all time")

    print("Fetching PolyStrat agent list...")
    agents = get_all_polystrat_agents()
    print(f"Found {len(agents)} agents")

    exclude_set = set()
    if args.exclude:
        exclude_set.update(a.lower() for a in args.exclude)
    if args.exclude_valory:
        team_file = os.path.join(os.path.dirname(__file__), "data", "valory_team_agents.json")
        with open(team_file) as f:
            team_data = json.load(f)
        exclude_set.update(a["address"].lower() for a in team_data["agents"])
    if exclude_set:
        before = len(agents)
        agents = [a for a in agents if a not in exclude_set]
        print(f"Excluded {before - len(agents)} agents, {len(agents)} remaining")

    print("Fetching mech requests across fleet...")
    records = collect_all_requests(
        agents, from_ts=from_ts, to_ts=to_ts, sample_size=args.sample
    )

    if not records:
        print("ERROR: No mech requests found", file=sys.stderr)
        sys.exit(1)

    # Run analyses
    overall = analyze_overall(records)
    monthly = analyze_by_period(records, ts_to_month)
    weekly = analyze_by_period(records, ts_to_week)
    daily = analyze_by_period(records, ts_to_date)
    adoption = analyze_tool_adoption(records)
    diversity = analyze_agent_diversity(records)

    # Head-to-head for the two focus tools
    tool_a = "prediction-request-reasoning"
    tool_b = "superforcaster"
    h2h = analyze_head_to_head(records, tool_a, tool_b)

    if args.json_output:
        output = {
            "overall": overall,
            "monthly": {k: {"total": v["total"], "tools": v["tools"]} for k, v in monthly.items()},
            "weekly": {k: {"total": v["total"], "tools": v["tools"]} for k, v in weekly.items()},
            "daily": {k: {"total": v["total"], "tools": v["tools"]} for k, v in daily.items()},
            "head_to_head": h2h,
            "adoption": adoption,
            "agent_diversity": diversity,
        }
        print(json.dumps(output, indent=2))
    else:
        print_report(overall, monthly, weekly, daily, adoption, diversity,
                     head_to_head=h2h, tool_a=tool_a, tool_b=tool_b)

    if args.csv_path:
        write_csv(overall, monthly, adoption, args.csv_path)


if __name__ == "__main__":
    main()
