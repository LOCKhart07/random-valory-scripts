"""
Mech deliver timeline script.

Queries all mech delivers (not requests) in a user-specified time window from
the mech-marketplace subgraph, bins by time, and plots per-mech deliver-count
trend lines over time.

Usage:
    python mech_deliver_timeline.py                        # last 30 days
    python mech_deliver_timeline.py --period 7d            # last 7 days
    python mech_deliver_timeline.py --period 90d           # last 90 days
    python mech_deliver_timeline.py --start 2025-12-01 --end 2026-01-01
    python mech_deliver_timeline.py --start 2025-12-01     # from date to now

Cache:
    .mech_deliver_timeline_cache.json (next to this script, TTL 12 hours)
    Cache entries store 'fetched_from' so a wider historical window correctly
    triggers a re-fetch rather than using stale partial data.
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
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    _HAS_MATPLOTLIB = True
except ImportError:
    _HAS_MATPLOTLIB = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OLAS_MECH_SUBGRAPH_URL = (
    "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"
)

DEPLOYMENTS_DIR = Path("/home/lockhart/work/valory/mech-deployments/deployments")

CACHE_TTL_SECONDS = 12 * 60 * 60  # 12 hours
_CACHE_FILE = Path(__file__).parent / ".mech_deliver_timeline_cache.json"

MIN_DELIVERS_FOR_LINE = 2  # minimum data points before a mech line is drawn

REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3


# ---------------------------------------------------------------------------
# Deployment name map
# ---------------------------------------------------------------------------


def load_mech_name_map() -> dict[str, str]:
    """
    Reads all .env files in DEPLOYMENTS_DIR and builds a mapping:
        lowercase_address -> deployment_name  (filename without .env)

    Handles both JSON-object and JSON-array forms of MECH_TO_CONFIG:
        object: {"0xADDR": {...}, ...}
        array:  [["0xADDR", [...]], ...]
    """
    mapping: dict[str, str] = {}
    if not DEPLOYMENTS_DIR.is_dir():
        return mapping

    for env_file in sorted(DEPLOYMENTS_DIR.glob("*.env")):
        name = env_file.stem  # filename without .env
        try:
            text = env_file.read_text()
        except OSError:
            continue

        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("MECH_TO_CONFIG="):
                continue
            # Strip variable name and surrounding quotes
            raw = line[len("MECH_TO_CONFIG=") :].strip("'\"")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if isinstance(parsed, dict):
                addresses = list(parsed.keys())
            elif isinstance(parsed, list):
                # [["0xADDR", [...]], ...]
                addresses = [entry[0] for entry in parsed if entry]
            else:
                continue

            for addr in addresses:
                mapping[addr.lower()] = name
            break  # only one MECH_TO_CONFIG per file

    return mapping


# Loaded once at startup; used for chart labels.
_MECH_NAMES: dict[str, str] = load_mech_name_map()


# ---------------------------------------------------------------------------
# Disk cache helpers
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
            # Only retry on server-side errors (5xx); re-raise client errors immediately
            if exc.response is not None and exc.response.status_code < 500:
                raise
            last_exc = exc
        if attempt == MAX_RETRIES:
            break
        wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
        print(
            f"    [retry {attempt}/{MAX_RETRIES - 1}] Error, retrying in {wait}s: {last_exc}"
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

GET_MECH_DELIVERS_QUERY = """
query MechDelivers($timestamp_gt: Int!, $timestamp_lte: Int!, $skip: Int, $first: Int) {
    delivers(
        first: $first
        skip: $skip
        orderBy: blockTimestamp
        orderDirection: asc
        where: { blockTimestamp_gt: $timestamp_gt, blockTimestamp_lte: $timestamp_lte }
    ) {
        blockTimestamp
        mech
    }
}
"""


# ---------------------------------------------------------------------------
# Mech deliver fetching (direct, cached)
# ---------------------------------------------------------------------------


# How often (in number of batches) to checkpoint partial results to disk
_CHECKPOINT_EVERY = 10

# Lightweight query used only for counting (no fields fetched beyond id)
_COUNT_PROBE_QUERY = """
query CountProbe($timestamp_gt: Int!, $timestamp_lte: Int!, $skip: Int) {
    delivers(
        first: 1
        skip: $skip
        where: { blockTimestamp_gt: $timestamp_gt, blockTimestamp_lte: $timestamp_lte }
    ) {
        id
    }
}
"""


def _estimate_total_delivers(start_ts: int, end_ts: int) -> int | None:
    """
    Estimate the total number of delivers in the time range using binary search.
    Returns an approximate count (accurate to within ~1000).
    """
    def _has_results_at(skip: int) -> bool:
        variables = {"timestamp_gt": start_ts, "timestamp_lte": end_ts, "skip": skip}
        resp = _post_with_retry(
            OLAS_MECH_SUBGRAPH_URL,
            json={"query": _COUNT_PROBE_QUERY, "variables": variables},
            headers={"Content-Type": "application/json"},
        )
        data = resp.json()
        batch = (data.get("data") or {}).get("delivers") or []
        return len(batch) > 0

    try:
        # Exponential probe to find upper bound (start big, grow fast)
        low, high = 0, 10_000
        while _has_results_at(high):
            low = high
            high *= 4
            if high > 10_000_000:  # safety cap
                return None

        # Coarse binary search between low and high (~3-4 iterations)
        while high - low > 10_000:
            mid = (low + high) // 2
            if _has_results_at(mid):
                low = mid
            else:
                high = mid

        return high
    except Exception:
        return None


def _fetch_mech_delivers_from_api(
    start_ts: int, end_ts: int, cache_key: str, batch_size: int = 1000,
    estimated_total: int | None = None,
) -> list[dict]:
    """
    Fetch all mech delivers in [start_ts, end_ts] directly from the subgraph.

    Uses cursor-based pagination (advancing timestamp_gt to the last seen
    timestamp) instead of skip-based pagination.  This avoids the subgraph
    slowdown that occurs with large skip values.

    When multiple delivers share the same blockTimestamp at a page boundary,
    we use skip to page through them so none are lost.

    Checkpoints partial results to the cache every _CHECKPOINT_EVERY batches.
    """
    total_str = f"/{estimated_total}" if estimated_total else ""
    fetch_start = time.time()

    # Resume from a previous partial fetch if available
    entry = _cache.get(cache_key)
    if entry and not entry.get("complete", True):
        all_delivers: list[dict] = entry["delivers"]
        cursor_ts: int = entry.get("cursor_ts", start_ts)
        cursor_skip: int = entry.get("cursor_skip", 0)
        print(
            f"    Resuming from checkpoint: {len(all_delivers)}{total_str} delivers already fetched..."
        )
    else:
        all_delivers = []
        cursor_ts = start_ts
        cursor_skip = 0

    batch_num = 0
    while True:
        variables = {
            "timestamp_gt": cursor_ts,
            "timestamp_lte": end_ts,
            "skip": cursor_skip,
            "first": batch_size,
        }
        response = _post_with_retry(
            OLAS_MECH_SUBGRAPH_URL,
            json={"query": GET_MECH_DELIVERS_QUERY, "variables": variables},
            headers={"Content-Type": "application/json"},
        )
        data = response.json()
        if "data" not in data:
            raise RuntimeError(f"Subgraph error (mech delivers): {data}")
        batch = (data.get("data") or {}).get("delivers") or []
        if not batch:
            break
        all_delivers.extend(batch)
        batch_num += 1

        # Advance cursor: move timestamp_gt to last item's timestamp.
        # If the entire batch has the same timestamp, increment skip instead
        # to avoid an infinite loop / missing records.
        last_ts = int(batch[-1]["blockTimestamp"])
        if last_ts > cursor_ts:
            cursor_ts = last_ts
            cursor_skip = 0
            # Count how many trailing items share this timestamp — we may
            # need to skip them on the next query since timestamp_gt is strict.
            trailing = sum(1 for d in reversed(batch) if int(d["blockTimestamp"]) == last_ts)
            cursor_skip = trailing
            # Actually use timestamp_gt = last_ts - 1 so the _gt filter is
            # "greater than last_ts - 1" which means ">= last_ts", then skip
            # the ones we already have.
            cursor_ts = last_ts - 1
        else:
            # All items in batch have the same timestamp; advance skip
            cursor_skip += batch_size

        if batch_num % _CHECKPOINT_EVERY == 0:
            pct = ""
            eta = ""
            if estimated_total and estimated_total > 0:
                pct_val = len(all_delivers) / estimated_total * 100
                pct = f" ({pct_val:.0f}%)"
                elapsed = time.time() - fetch_start
                if len(all_delivers) > 0:
                    rate = len(all_delivers) / elapsed
                    remaining = (estimated_total - len(all_delivers)) / rate
                    if remaining > 60:
                        eta = f" ~{remaining / 60:.0f}m remaining"
                    else:
                        eta = f" ~{remaining:.0f}s remaining"
            print(f"    Fetched {len(all_delivers)}{total_str}{pct} delivers so far{eta} (checkpointing)...")
            _cache[cache_key] = {
                "fetched_at": int(time.time()),
                "delivers": all_delivers,
                "cursor_ts": cursor_ts,
                "cursor_skip": cursor_skip,
                "complete": False,
            }
            _save_cache(_cache)

        if len(batch) < batch_size:
            break

    return all_delivers


def fetch_mech_delivers_in_range(start_ts: int, end_ts: int) -> list[dict]:
    """
    Returns all mech delivers in [start_ts, end_ts], using a disk-backed cache.
    Re-fetches if the cache entry is missing, stale, or incomplete.
    Resumes a previous incomplete fetch if one exists.
    """
    cache_key = f"delivers:{start_ts}:{end_ts}"
    entry = _cache.get(cache_key)

    # Use cache only if present, complete, and fresh
    if (
        entry is not None
        and entry.get("complete", True)
        and (time.time() - entry["fetched_at"]) <= CACHE_TTL_SECONDS
    ):
        print(
            f"  Using cached mech delivers "
            f"(fetched {_seconds_ago(entry['fetched_at'])} ago)."
        )
        return entry["delivers"]

    print(f"  Estimating total delivers...")
    estimated_total = _estimate_total_delivers(start_ts, end_ts)
    if estimated_total:
        print(f"  Estimated ~{estimated_total:,} delivers to fetch.")

    if entry and not entry.get("complete", True):
        print(
            f"  Resuming incomplete fetch from subgraph "
            f"({_ts_to_date(start_ts)} → {_ts_to_date(end_ts)})..."
        )
    else:
        print(
            f"  Fetching mech delivers from subgraph "
            f"({_ts_to_date(start_ts)} → {_ts_to_date(end_ts)})..."
        )

    delivers = _fetch_mech_delivers_from_api(start_ts, end_ts, cache_key, estimated_total=estimated_total)
    _cache[cache_key] = {
        "fetched_at": int(time.time()),
        "delivers": delivers,
        "complete": True,
    }
    _save_cache(_cache)
    return delivers


# ---------------------------------------------------------------------------
# Binning
# ---------------------------------------------------------------------------


def _bin_edges(start_ts: int, end_ts: int) -> list[int]:
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


def bin_delivers_by_mech(
    delivers: list[dict], start_ts: int, end_ts: int
) -> tuple[list[datetime], dict[str, list[int]]]:
    """
    Groups mech delivers into time bins and counts per mech per bin.

    Returns:
        bin_labels  – list of datetime objects (one per bin)
        mech_series – dict mapping mech address -> list of deliver counts per bin
    """
    edges = _bin_edges(start_ts, end_ts)
    n_bins = len(edges) - 1
    bin_labels = [
        datetime.fromtimestamp(edges[i], tz=timezone.utc) for i in range(n_bins)
    ]

    bin_counts: list[dict[str, int]] = [defaultdict(int) for _ in range(n_bins)]

    for deliver in delivers:
        ts = int(deliver.get("blockTimestamp") or 0)
        mech = (deliver.get("mech") or "unknown").lower()
        bin_idx = None
        for i in range(n_bins):
            if edges[i] <= ts < edges[i + 1]:
                bin_idx = i
                break
        if bin_idx is None:
            continue
        bin_counts[bin_idx][mech] += 1

    all_mechs: set[str] = set()
    for bc in bin_counts:
        all_mechs.update(bc.keys())

    mech_series: dict[str, list[int]] = {}
    for mech in sorted(all_mechs):
        mech_series[mech] = [bin_counts[i].get(mech, 0) for i in range(n_bins)]

    return bin_labels, mech_series


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------


def _shorten_address(addr: str) -> str:
    """
    Return the deployment name for a known mech address (from DEPLOYMENTS_DIR),
    or a 0x1234…abcd abbreviated label, or the value unchanged.
    """
    name = _MECH_NAMES.get(addr.lower())
    if name:
        return name
    if addr.startswith("0x") and len(addr) >= 10:
        return f"{addr[:6]}…{addr[-4:]}"
    return addr


def plot_mech_deliver_timeline(
    bin_labels: list[datetime],
    mech_series: dict[str, list[int]],
    start_ts: int,
    end_ts: int,
) -> None:
    if not _HAS_MATPLOTLIB:
        print("\nmatplotlib is not installed. Install with: pip install matplotlib")
        print("Skipping chart — see printed summary above.")
        return

    fig, ax = plt.subplots(figsize=(14, 7))

    plotted_any = False
    for mech, counts in mech_series.items():
        non_zero = [(bin_labels[i], v) for i, v in enumerate(counts) if v > 0]
        if len(non_zero) < MIN_DELIVERS_FOR_LINE:
            continue
        xs, ys = zip(*non_zero)
        label = _shorten_address(mech)
        ax.plot(xs, ys, marker="o", markersize=4, linewidth=1.8, label=label)
        plotted_any = True

    # Total line (all mechs)
    total_counts = [
        sum(mech_series[m][i] for m in mech_series) for i in range(len(bin_labels))
    ]
    total_points = [(bin_labels[i], v) for i, v in enumerate(total_counts) if v > 0]
    if total_points:
        xs, ys = zip(*total_points)
        ax.plot(
            xs,
            ys,
            color="black",
            linewidth=2.5,
            linestyle="--",
            marker="s",
            markersize=5,
            label="Total",
            zorder=10,
        )
        plotted_any = True

    if not plotted_any:
        print("Not enough data to plot a chart for the selected time range.")
        plt.close(fig)
        return

    span_days = (end_ts - start_ts) / 86400
    if span_days <= 30:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.xaxis.set_major_locator(
            mdates.DayLocator(interval=max(1, int(span_days // 10)))
        )
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))

    plt.setp(ax.xaxis.get_majorticklabels(), rotation=40, ha="right")
    ax.set_ylabel("Number of delivers")
    ax.set_xlabel("Date")
    ax.set_title(
        f"Mech Delivers Over Time  |  " f"{_ts_to_date(start_ts)} → {_ts_to_date(end_ts)}"
    )
    ax.legend(loc="upper left", fontsize=8, framealpha=0.7)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------


def compute_mech_stats(delivers: list[dict]) -> list[dict]:
    """Per-mech deliver counts across the entire period."""
    totals: dict[str, int] = defaultdict(int)

    for deliver in delivers:
        mech = (deliver.get("mech") or "unknown").lower()
        totals[mech] += 1

    stats = []
    for mech, total in totals.items():
        stats.append({"mech": mech, "total": total})
    return sorted(stats, key=lambda x: x["total"], reverse=True)


def print_summary(stats: list[dict], start_ts: int, end_ts: int) -> None:
    name_w = max((len(_shorten_address(s["mech"])) for s in stats), default=4)
    name_w = max(name_w, 4)
    header = f"{'Mech':<{name_w}} | {'Total':>7}"
    sep = "-" * (len(header) + 10)

    print(f"\nMech deliver summary  ({_ts_to_date(start_ts)} → {_ts_to_date(end_ts)})")
    print(sep)
    print(header)
    print(sep)
    for s in stats:
        print(f"{_shorten_address(s['mech']):<{name_w}} | {s['total']:>7}")
    print(sep)

    total_all = sum(s["total"] for s in stats)
    print(f"\nTotal delivers: {total_all}")


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
        description="Plot per-mech deliver counts over a time period."
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
        f"\nMech Deliver Timeline: {_ts_to_date(start_ts)} → {_ts_to_date(end_ts)}"
        f" ({(end_ts - start_ts) // 86400} days)"
    )
    print("=" * 60)

    print("\n[1/3] Fetching mech delivers...")
    delivers_data = fetch_mech_delivers_in_range(start_ts, end_ts)
    if not delivers_data:
        print("No mech delivers found for the selected time range. Exiting.")
        sys.exit(0)
    unique_mechs = len(
        {d.get("mech") for d in delivers_data if d.get("mech")}
    )
    print(f"  {len(delivers_data)} delivers across {unique_mechs} unique mechs.")

    print("\n[2/3] Computing statistics...")
    stats = compute_mech_stats(delivers_data)
    print_summary(stats, start_ts, end_ts)

    bin_labels, mech_series = bin_delivers_by_mech(delivers_data, start_ts, end_ts)
    span_days = (end_ts - start_ts) / 86400
    granularity = "daily" if span_days <= 30 else "weekly"
    print(f"\n  Binned into {len(bin_labels)} {granularity} bins.")

    print("\n[3/3] Plotting chart...")
    plot_mech_deliver_timeline(bin_labels, mech_series, start_ts, end_ts)


if __name__ == "__main__":
    main()
