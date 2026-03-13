"""
Tool accuracy bar chart script.

Fetches resolved bets in a user-specified time window, enriches each with the
mech tool that was used, bins results by time, and plots a grouped bar chart
showing per-tool accuracy for each individual time bin.

Usage:
    python tool_accuracy_bars.py                        # last 30 days
    python tool_accuracy_bars.py --period 7d            # last 7 days
    python tool_accuracy_bars.py --period 90d           # last 90 days
    python tool_accuracy_bars.py --start 2025-12-01 --end 2026-01-01
    python tool_accuracy_bars.py --start 2025-12-01     # from date to now

Cache:
    Shares .tool_accuracy_timeline_cache.json with tool_accuracy_timeline.py
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.exceptions import ConnectionError, Timeout

try:
    import matplotlib.pyplot as plt
    import numpy as np

    _HAS_MATPLOTLIB = True
except ImportError:
    _HAS_MATPLOTLIB = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OLAS_MECH_SUBGRAPH_URL = (
    "https://api.subgraph.staging.autonolas.tech/api/proxy/mech-marketplace-gnosis"
)
PREDICT_OMEN_URL = "https://api.subgraph.staging.autonolas.tech/api/proxy/predict-omen"
QUESTION_DATA_SEPARATOR = "\u241f"

CACHE_TTL_SECONDS = 12 * 60 * 60  # 12 hours
_CACHE_FILE = Path(__file__).parent / ".tool_accuracy_timeline_cache.json"

MIN_BETS_FOR_BAR = 2  # minimum bets in a bin to show a bar for that tool

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3


# ---------------------------------------------------------------------------
# Cache helpers
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
            if attempt == MAX_RETRIES:
                break
            wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            print(
                f"    [retry {attempt}/{MAX_RETRIES - 1}] Network error, retrying in {wait}s: {exc}"
            )
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def _load_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE_FILE.write_text(json.dumps(cache))


_cache: dict = _load_cache()


# ---------------------------------------------------------------------------
# GraphQL query
# ---------------------------------------------------------------------------

GET_MECH_SENDER_QUERY = """
query MechSender($id: ID!, $timestamp_gt: Int!, $skip: Int, $first: Int) {
    sender(id: $id) {
        totalMarketplaceRequests
        requests(first: $first, skip: $skip, where: { blockTimestamp_gt: $timestamp_gt }) {
            blockTimestamp
            parsedRequest {
                questionTitle
                tool
                prompt
            }
        }
    }
}
"""


# ---------------------------------------------------------------------------
# Bet fetching
# ---------------------------------------------------------------------------


def _fetch_bets_from_api(
    start_ts: int, end_ts: int, batch_size: int = 1000
) -> list[dict]:
    headers = {"Content-Type": "application/json"}
    all_raw_bets: list[dict] = []
    skip = 0

    while True:
        query = f"""
    {{
      bets(
        first: {batch_size}
        skip: {skip}
        orderBy: timestamp
        orderDirection: asc
        where: {{
          timestamp_gte: {start_ts}
          timestamp_lte: {end_ts}
          fixedProductMarketMaker_: {{currentAnswer_not: null}}
        }}
      ) {{
        id
        timestamp
        bettor {{
          id
          serviceId
        }}
        outcomeIndex
        fixedProductMarketMaker {{
          currentAnswer
          question
        }}
      }}
    }}
    """
        response = _post_with_retry(
            PREDICT_OMEN_URL, headers=headers, json={"query": query}
        )
        data = response.json()
        if "data" not in data:
            raise RuntimeError(f"Subgraph error (bets): {data.get('errors', data)}")
        batch = data["data"]["bets"]
        if not batch:
            break
        all_raw_bets.extend(batch)
        if len(batch) < batch_size:
            break
        skip += batch_size

    formatted = []
    for bet in all_raw_bets:
        chosen = int(bet["outcomeIndex"])
        correct = int(bet["fixedProductMarketMaker"]["currentAnswer"], 16)
        formatted.append(
            {
                "bet_id": bet["id"],
                "timestamp": int(bet["timestamp"]),
                "bettor": bet["bettor"]["id"],
                "service_id": bet["bettor"]["serviceId"],
                "chosen_outcome": chosen,
                "correct_outcome": correct,
                "is_correct": chosen == correct,
                "question": bet["fixedProductMarketMaker"]["question"],
            }
        )
    return formatted


def fetch_bets_in_range(start_ts: int, end_ts: int) -> list[dict]:
    key = f"bets:{start_ts}:{end_ts}"
    entry = _cache.get(key)
    if entry is None or (time.time() - entry["fetched_at"]) > CACHE_TTL_SECONDS:
        print(
            f"  Fetching bets from subgraph ({_ts_to_date(start_ts)} → {_ts_to_date(end_ts)})..."
        )
        bets = _fetch_bets_from_api(start_ts, end_ts)
        _cache[key] = {"fetched_at": int(time.time()), "bets": bets}
        _save_cache(_cache)
    else:
        print(
            f"  Using cached bets (fetched {_seconds_ago(_cache[key]['fetched_at'])} ago)."
        )
    return _cache[key]["bets"]


# ---------------------------------------------------------------------------
# Mech request fetching
# ---------------------------------------------------------------------------


def _fetch_all_mech_requests_from_api(
    agent_address: str, timestamp_gt: int, batch_size: int = 1000
) -> list[dict]:
    all_requests = []
    skip = 0
    while True:
        variables = {
            "id": agent_address,
            "timestamp_gt": timestamp_gt,
            "skip": skip,
            "first": batch_size,
        }
        response = _post_with_retry(
            OLAS_MECH_SUBGRAPH_URL,
            json={"query": GET_MECH_SENDER_QUERY, "variables": variables},
            headers={"Content-Type": "application/json"},
        )
        data = response.json()
        if "data" not in data:
            raise RuntimeError(f"Subgraph error (mech): {data}")
        result = (data.get("data") or {}).get("sender") or {}
        batch = result.get("requests", [])
        if not batch:
            break
        all_requests.extend(batch)
        if len(batch) < batch_size:
            break
        skip += batch_size
    return all_requests


def get_mech_requests_cached(agent_address: str, start_ts: int) -> list[dict]:
    key = f"mech:{agent_address}"
    entry = _cache.get(key)
    needs_refresh = (
        entry is None
        or start_ts < entry.get("fetched_from", start_ts + 1)
        or (time.time() - entry["fetched_at"]) > CACHE_TTL_SECONDS
    )
    if needs_refresh:
        reqs = _fetch_all_mech_requests_from_api(agent_address, timestamp_gt=start_ts)
        _cache[key] = {
            "fetched_at": int(time.time()),
            "fetched_from": start_ts,
            "requests": reqs,
        }
        _save_cache(_cache)
    return _cache[key]["requests"]


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------


def extract_question_title(question: str) -> str:
    if not question:
        return ""
    return question.split(QUESTION_DATA_SEPARATOR)[0]


def match_bet_to_mech_request(bet: dict, mech_requests: list[dict]) -> list[dict]:
    bet_title = extract_question_title(bet.get("question", "")).strip()
    if not bet_title:
        return []
    matched = []
    for req in mech_requests:
        mech_title = extract_question_title(
            (req.get("parsedRequest") or {}).get("questionTitle", "")
        ).strip()
        if not mech_title:
            continue
        if bet_title.startswith(mech_title) or mech_title.startswith(bet_title):
            matched.append(req)
    return matched


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def enrich_bets_with_tool(bets: list[dict], start_ts: int) -> list[dict]:
    agents = {bet["bettor"] for bet in bets}
    print(f"  Fetching mech tool data for {len(agents)} unique agents...")

    agent_requests: dict[str, list[dict]] = {}
    failed_agents: set[str] = set()
    for i, agent in enumerate(agents, 1):
        if i % 20 == 0 or i == len(agents):
            print(f"    Agent {i}/{len(agents)}...")
        try:
            agent_requests[agent] = get_mech_requests_cached(agent, start_ts)
        except Exception as exc:
            print(
                f"    [warn] Failed to fetch mech requests for {agent}: {exc}. Skipping."
            )
            failed_agents.add(agent)
            agent_requests[agent] = []
    if failed_agents:
        print(
            f"  Warning: {len(failed_agents)} agent(s) skipped — their bets will be 'unknown'."
        )

    enriched = []
    for bet in bets:
        mech_requests = agent_requests[bet["bettor"]]
        matches = match_bet_to_mech_request(bet, mech_requests)

        if matches:
            bet_ts = bet["timestamp"]
            before_bet = [
                r for r in matches if int(r.get("blockTimestamp") or 0) <= bet_ts
            ]
            chosen = (
                max(before_bet, key=lambda r: int(r.get("blockTimestamp") or 0))
                if before_bet
                else matches[0]
            )
            tool = (chosen.get("parsedRequest") or {}).get("tool") or "unknown"
        else:
            tool = "unknown"

        enriched.append({**bet, "tool": tool})
    return enriched


# ---------------------------------------------------------------------------
# Binning
# ---------------------------------------------------------------------------


def _bin_edges(start_ts: int, end_ts: int) -> list[int]:
    """Auto-selects daily bins for ≤30 days, weekly otherwise."""
    span_days = (end_ts - start_ts) / 86400
    step_days = 1 if span_days <= 30 else 7

    edges: list[int] = []
    cursor = datetime.fromtimestamp(start_ts, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    while cursor <= end_dt:
        edges.append(int(cursor.timestamp()))
        cursor += timedelta(days=step_days)
    if not edges or edges[-1] < end_ts:
        edges.append(end_ts + 1)
    return edges


def bin_bets(
    enriched_bets: list[dict], start_ts: int, end_ts: int
) -> tuple[list[str], dict[str, list[float | None]], list[float | None], list[int]]:
    """
    Groups enriched bets into time bins and computes accuracy per tool per bin.
    Each bin's value reflects only the bets within that bin — no cumulation.

    Returns:
        bin_labels   – list of date strings (one per bin)
        tool_series  – dict mapping tool -> list of accuracy% per bin (None = no data)
        overall_series – overall accuracy% per bin (excluding 'unknown')
        bin_counts   – total known-tool bets per bin
    """
    edges = _bin_edges(start_ts, end_ts)
    n_bins = len(edges) - 1

    span_days = (end_ts - start_ts) / 86400
    if span_days <= 30:
        fmt = "%b %d"
    else:
        fmt = "%b %d"

    bin_labels = [
        datetime.fromtimestamp(edges[i], tz=timezone.utc).strftime(fmt)
        for i in range(n_bins)
    ]

    bin_totals: list[dict[str, int]] = [defaultdict(int) for _ in range(n_bins)]
    bin_corrects: list[dict[str, int]] = [defaultdict(int) for _ in range(n_bins)]

    for bet in enriched_bets:
        ts = bet["timestamp"]
        bin_idx = None
        for i in range(n_bins):
            if edges[i] <= ts < edges[i + 1]:
                bin_idx = i
                break
        if bin_idx is None:
            continue
        tool = bet["tool"]
        bin_totals[bin_idx][tool] += 1
        if bet["is_correct"]:
            bin_corrects[bin_idx][tool] += 1

    all_tools: set[str] = set()
    for bt in bin_totals:
        all_tools.update(bt.keys())

    tool_series: dict[str, list[float | None]] = {}
    for tool in sorted(all_tools):
        series: list[float | None] = []
        for i in range(n_bins):
            total = bin_totals[i].get(tool, 0)
            correct = bin_corrects[i].get(tool, 0)
            series.append(
                round(correct / total * 100, 1) if total >= MIN_BETS_FOR_BAR else None
            )
        tool_series[tool] = series

    overall_series: list[float | None] = []
    bin_counts: list[int] = []
    for i in range(n_bins):
        total = sum(v for k, v in bin_totals[i].items() if k != "unknown")
        correct = sum(v for k, v in bin_corrects[i].items() if k != "unknown")
        overall_series.append(round(correct / total * 100, 1) if total > 0 else None)
        bin_counts.append(total)

    return bin_labels, tool_series, overall_series, bin_counts


# ---------------------------------------------------------------------------
# Bar chart
# ---------------------------------------------------------------------------


def plot_accuracy_bars(
    bin_labels: list[str],
    tool_series: dict[str, list[float | None]],
    overall_series: list[float | None],
    bin_counts: list[int],
    start_ts: int,
    end_ts: int,
) -> None:
    """
    Draws a grouped bar chart of per-tool accuracy per time bin.
    Each bar shows the accuracy for that bin only (no cumulation).
    """
    if not _HAS_MATPLOTLIB:
        print("\nmatplotlib is not installed. Install with: pip install matplotlib")
        print("Skipping chart — see printed summary above.")
        return

    # Filter to tools that have at least one non-None value (excluding 'unknown')
    plot_tools = [
        tool
        for tool, series in tool_series.items()
        if tool != "unknown" and any(v is not None for v in series)
    ]

    if not plot_tools and not any(v is not None for v in overall_series):
        print("Not enough data to plot a chart for the selected time range.")
        return

    n_bins = len(bin_labels)
    n_tools = len(plot_tools)
    x = np.arange(n_bins)

    # Bar width: split the [0,1) slot among tools + an overall bar
    n_groups = n_tools + 1  # tools + overall
    bar_width = min(0.8 / max(n_groups, 1), 0.35)
    total_width = bar_width * n_groups
    offsets = np.linspace(
        -total_width / 2 + bar_width / 2, total_width / 2 - bar_width / 2, n_groups
    )

    fig, ax = plt.subplots(figsize=(max(14, n_bins * 0.9), 7))

    cmap = plt.get_cmap("tab10")

    for idx, tool in enumerate(plot_tools):
        series = tool_series[tool]
        heights = [v if v is not None else 0 for v in series]
        alphas = [1.0 if v is not None else 0.15 for v in series]
        bars = ax.bar(
            x + offsets[idx],
            heights,
            width=bar_width,
            label=tool,
            color=cmap(idx % 10),
            alpha=0.85,
        )
        # Dim bars where there was no data
        for bar, alpha in zip(bars, alphas):
            bar.set_alpha(alpha)
        # Value labels on top
        for bar, val in zip(bars, series):
            if val is not None:
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + 1.0,
                    f"{val:.0f}%",
                    ha="center",
                    va="bottom",
                    fontsize=7,
                    rotation=90 if n_tools > 4 else 0,
                )

    # Overall bar (last slot, black/grey)
    overall_heights = [v if v is not None else 0 for v in overall_series]
    overall_bars = ax.bar(
        x + offsets[-1],
        overall_heights,
        width=bar_width,
        label="Overall",
        color="dimgray",
        alpha=0.85,
    )
    for bar, val in zip(overall_bars, overall_series):
        if val is not None:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 1.0,
                f"{val:.0f}%",
                ha="center",
                va="bottom",
                fontsize=7,
                fontweight="bold",
                rotation=90 if n_tools > 4 else 0,
            )

    # 50% reference line
    ax.axhline(
        50, color="red", linestyle=":", linewidth=1.2, alpha=0.7, label="50% (random)"
    )

    ax.set_xticks(x)
    ax.set_xticklabels(bin_labels, rotation=40, ha="right", fontsize=9)
    ax.set_ylim(0, 115)
    ax.set_ylabel("Accuracy (%)")
    ax.set_xlabel("Time bin")

    span_days = (end_ts - start_ts) / 86400
    granularity = "daily" if span_days <= 30 else "weekly"
    ax.set_title(
        f"Tool Accuracy per {granularity.capitalize()} Bin  |  "
        f"{_ts_to_date(start_ts)} → {_ts_to_date(end_ts)}"
    )

    # Secondary x-axis annotation: bet counts
    ax2 = ax.twiny()
    ax2.set_xlim(ax.get_xlim())
    ax2.set_xticks(x)
    ax2.set_xticklabels([f"n={c}" for c in bin_counts], fontsize=7, color="grey")
    ax2.set_xlabel("Bets per bin (known tools)", fontsize=8, color="grey")

    ax.legend(loc="lower left", fontsize=8, framealpha=0.75)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------


def compute_overall_stats(enriched_bets: list[dict]) -> list[dict]:
    totals: dict[str, int] = defaultdict(int)
    corrects: dict[str, int] = defaultdict(int)
    for bet in enriched_bets:
        tool = bet["tool"]
        totals[tool] += 1
        if bet["is_correct"]:
            corrects[tool] += 1
    stats = []
    for tool, total in totals.items():
        correct = corrects[tool]
        stats.append(
            {
                "tool": tool,
                "total": total,
                "correct": correct,
                "accuracy": round(correct / total * 100, 1) if total > 0 else 0.0,
            }
        )
    return sorted(stats, key=lambda x: x["total"], reverse=True)


def print_summary(stats: list[dict], start_ts: int, end_ts: int) -> None:
    col_w = max((len(s["tool"]) for s in stats), default=4)
    col_w = max(col_w, 4)
    header = f"{'Tool':<{col_w}} | {'Total':>7} | {'Correct':>7} | {'Accuracy':>8}"
    sep = "-" * len(header)

    print(f"\nTool accuracy summary  ({_ts_to_date(start_ts)} → {_ts_to_date(end_ts)})")
    print(sep)
    print(header)
    print(sep)
    for s in stats:
        print(
            f"{s['tool']:<{col_w}} | {s['total']:>7} | {s['correct']:>7} | {s['accuracy']:>7.1f}%"
        )
    print(sep)

    known = [s for s in stats if s["tool"] != "unknown"]
    if known:
        total_k = sum(s["total"] for s in known)
        correct_k = sum(s["correct"] for s in known)
        pct = round(correct_k / total_k * 100, 1) if total_k else 0.0
        print(f"\nOverall (known tools): {correct_k}/{total_k} correct ({pct}%)")

    unk = next((s for s in stats if s["tool"] == "unknown"), None)
    if unk:
        print(f"Unmatched bets (no mech request found): {unk['total']}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts_to_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _seconds_ago(ts: int) -> str:
    secs = int(time.time()) - ts
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


def _parse_args() -> tuple[int, int]:
    parser = argparse.ArgumentParser(
        description="Plot per-tool mech accuracy as grouped bars per time bin."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--period",
        metavar="Nd",
        default=None,
        help="Look-back period, e.g. 7d, 30d, 90d (default: 30d)",
    )
    group.add_argument(
        "--start",
        metavar="YYYY-MM-DD",
        default=None,
        help="Start date (UTC). Can be combined with --end.",
    )
    parser.add_argument(
        "--end",
        metavar="YYYY-MM-DD",
        default=None,
        help="End date (UTC, inclusive). Defaults to today. Only valid with --start.",
    )

    args = parser.parse_args()
    now = int(time.time())
    today_end = int(
        datetime.now(tz=timezone.utc)
        .replace(hour=23, minute=59, second=59, microsecond=0)
        .timestamp()
    )

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
                end_dt = datetime(
                    *[int(p) for p in args.end.split("-")],
                    23,
                    59,
                    59,
                    tzinfo=timezone.utc,
                )
            except ValueError:
                parser.error(f"Invalid --end date: {args.end!r}. Expected YYYY-MM-DD.")
            end_ts = int(end_dt.timestamp())
        else:
            end_ts = today_end
    else:
        if args.end:
            parser.error("--end requires --start.")
        period_str = args.period or "30d"
        if not period_str.endswith("d"):
            parser.error("--period must end with 'd', e.g. 7d, 30d, 90d.")
        try:
            days = int(period_str[:-1])
        except ValueError:
            parser.error(f"Invalid --period value: {period_str!r}.")
        start_ts = now - days * 86400
        end_ts = today_end

    if end_ts <= start_ts:
        parser.error("End date must be after start date.")

    return start_ts, end_ts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    start_ts, end_ts = _parse_args()

    print(
        f"\nTool Accuracy Bar Chart: {_ts_to_date(start_ts)} → {_ts_to_date(end_ts)}"
        f" ({(end_ts - start_ts) // 86400} days)"
    )
    print("=" * 60)

    print("\n[1/4] Fetching resolved bets...")
    bets = fetch_bets_in_range(start_ts, end_ts)
    if not bets:
        print("No resolved bets found for the selected time range. Exiting.")
        sys.exit(0)
    unique_agents = len({b["bettor"] for b in bets})
    print(f"  {len(bets)} bets from {unique_agents} unique agents.")

    print("\n[2/4] Enriching bets with mech tool data...")
    enriched = enrich_bets_with_tool(bets, start_ts)
    print(
        f"  Done. {sum(1 for b in enriched if b['tool'] != 'unknown')} bets matched to a tool."
    )

    print("\n[3/4] Computing statistics...")
    stats = compute_overall_stats(enriched)
    print_summary(stats, start_ts, end_ts)

    bin_labels, tool_series, overall_series, bin_counts = bin_bets(
        enriched, start_ts, end_ts
    )
    span_days = (end_ts - start_ts) / 86400
    granularity = "daily" if span_days <= 30 else "weekly"
    print(f"\n  Binned into {len(bin_labels)} {granularity} bins.")

    print("\n[4/4] Plotting chart...")
    plot_accuracy_bars(
        bin_labels, tool_series, overall_series, bin_counts, start_ts, end_ts
    )


if __name__ == "__main__":
    main()
