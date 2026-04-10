"""
Compare mech tool prediction outputs (p_yes / p_no / confidence) before and
after a split date for the PolyStrat (Polygon) and OmenStrat (Gnosis) mech
marketplaces.

The toolResponse field on Deliver entities is a JSON string with shape:
    {"p_yes": 0.74, "p_no": 0.26, "confidence": 0.82, "info_utility": 0.85}
so we can pull the values directly from the subgraph — no IPFS fetch.

We fetch all delivers in the window globally (no sender filter — sender on a
Deliver is the mech operator, not the requester), parse each toolResponse, and
group by date / tool / mech.

Output per chain:
  - Overall before/after p_yes/confidence split
  - Per-tool before/after comparison
  - Per-mech before/after comparison (lets you spot which mech moved)
  - Daily timeline

Usage:
    python mech/analyze_pyes_trends.py
    python mech/analyze_pyes_trends.py --days 14 --split-date 2026-04-08
    python mech/analyze_pyes_trends.py --chain polygon
"""

import argparse
import json
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

POLYGON_MECH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"
GNOSIS_MECH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"

PAGE_SIZE = 1000
RETRIES = 4


def post(url, query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    for attempt in range(RETRIES):
        try:
            r = requests.post(
                url, json=payload,
                headers={"Content-Type": "application/json"}, timeout=120,
            )
            r.raise_for_status()
            d = r.json()
            if "errors" in d:
                raise RuntimeError(d["errors"])
            return d["data"]
        except Exception:
            if attempt == RETRIES - 1:
                raise
            time.sleep(3 * (2 ** attempt))


# ---------------------------------------------------------------------------
# Mech delivers — fetch all in window, page on blockTimestamp
# ---------------------------------------------------------------------------


def fetch_delivers(url, since_ts):
    """
    Fetch all delivers since `since_ts`, paging on blockTimestamp ascending.
    No sender filter — we group by mech post-hoc.
    """
    all_delivers = []
    cursor = since_ts
    seen_ids = set()
    last_log = time.time()

    while True:
        data = post(url, """
        query($ts: Int!, $first: Int!) {
          delivers(
            first: $first
            orderBy: blockTimestamp
            orderDirection: asc
            where: { blockTimestamp_gte: $ts }
          ) {
            id
            mech
            blockTimestamp
            toolResponse
            request { parsedRequest { tool } }
          }
        }
        """, {"ts": cursor, "first": PAGE_SIZE})

        batch = data.get("delivers", [])
        if not batch:
            break

        new_items = [d for d in batch if d["id"] not in seen_ids]
        for d in new_items:
            seen_ids.add(d["id"])
        all_delivers.extend(new_items)

        max_ts = max(int(d["blockTimestamp"]) for d in batch)
        if max_ts == cursor and len(new_items) == 0:
            # All items share the same timestamp as cursor and we've already seen them — done
            break
        # Always advance cursor to max_ts; gte filter lets us re-fetch the boundary,
        # then dedup drops what we already have
        cursor = max_ts

        if time.time() - last_log > 2:
            print(f"  [{len(all_delivers)} delivers, "
                  f"cursor {datetime.fromtimestamp(cursor, tz=timezone.utc):%Y-%m-%d %H:%M}]",
                  flush=True)
            last_log = time.time()

        if len(batch) < PAGE_SIZE:
            break

    return all_delivers


# ---------------------------------------------------------------------------
# Parsing & stats
# ---------------------------------------------------------------------------


def parse_response(raw):
    """Return (p_yes, p_no, confidence) or None if not parseable."""
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    p_yes = d.get("p_yes")
    p_no = d.get("p_no")
    conf = d.get("confidence")
    if p_yes is None or p_no is None:
        return None
    try:
        return float(p_yes), float(p_no), float(conf) if conf is not None else None
    except (TypeError, ValueError):
        return None


def process_delivers(delivers):
    out = []
    for d in delivers:
        parsed = parse_response(d.get("toolResponse"))
        if parsed is None:
            continue
        p_yes, p_no, conf = parsed
        tool = ((d.get("request") or {}).get("parsedRequest") or {}).get("tool") or "unknown"
        out.append({
            "ts": int(d["blockTimestamp"]),
            "mech": (d.get("mech") or "").lower(),
            "tool": tool,
            "p_yes": p_yes,
            "p_no": p_no,
            "confidence": conf,
        })
    return out


def stats_block(rows):
    if not rows:
        return None
    p_yes = [r["p_yes"] for r in rows]
    confs = [r["confidence"] for r in rows if r["confidence"] is not None]
    n = len(rows)
    return {
        "n": n,
        "mean_pyes": statistics.mean(p_yes),
        "median_pyes": statistics.median(p_yes),
        "stdev_pyes": statistics.pstdev(p_yes) if n > 1 else 0,
        "frac_pyes_gt_05": sum(1 for v in p_yes if v > 0.5) / n,
        "frac_pyes_extreme": sum(1 for v in p_yes if v < 0.1 or v > 0.9) / n,
        "frac_pyes_uncertain": sum(1 for v in p_yes if 0.4 <= v <= 0.6) / n,
        "mean_conf": statistics.mean(confs) if confs else None,
        "median_conf": statistics.median(confs) if confs else None,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_daily(rows, split_ts):
    by_day = defaultdict(list)
    for r in rows:
        day = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day].append(r)

    split_date = datetime.fromtimestamp(split_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    print(f"\n  {'Date':<12} {'N':>6} {'mean_pyes':>10} {'med_pyes':>10} "
          f"{'stdev':>8} {'%>0.5':>7} {'%extr':>7} {'%uncrt':>7} "
          f"{'mean_cnf':>9}")
    print("  " + "-" * 90)
    for day in sorted(by_day.keys()):
        s = stats_block(by_day[day])
        if s is None:
            continue
        marker = " <-- split" if day == split_date else ""
        mc = f"{s['mean_conf']:>8.3f}" if s['mean_conf'] is not None else "     n/a"
        print(f"  {day:<12} {s['n']:>6} {s['mean_pyes']:>10.4f} {s['median_pyes']:>10.4f} "
              f"{s['stdev_pyes']:>8.4f} {s['frac_pyes_gt_05']*100:>6.1f}% "
              f"{s['frac_pyes_extreme']*100:>6.1f}% {s['frac_pyes_uncertain']*100:>6.1f}% "
              f"{mc}{marker}")


def print_split(label, rows_b, rows_a):
    print(f"\n{'=' * 100}")
    print(f"{label}")
    print(f"{'=' * 100}")

    sb = stats_block(rows_b)
    sa = stats_block(rows_a)
    if sb is None or sa is None:
        print(f"  insufficient data: before={len(rows_b)} after={len(rows_a)}")
        return

    def row(name, vb, va, fmt=".4f", suffix=""):
        delta = va - vb
        arrow = "+" if delta > 0 else ""
        print(f"  {name:<26} {vb:>12{fmt}}{suffix}  {va:>12{fmt}}{suffix}   {arrow}{delta:{fmt}}")

    print(f"\n  {'METRIC':<26} {'before':>14} {'after':>14}   {'delta':>10}")
    print("  " + "-" * 70)
    print(f"  {'n delivers':<26} {sb['n']:>14d} {sa['n']:>14d}   {sa['n'] - sb['n']:+d}")
    row("mean p_yes",        sb["mean_pyes"], sa["mean_pyes"])
    row("median p_yes",      sb["median_pyes"], sa["median_pyes"])
    row("stdev p_yes",       sb["stdev_pyes"], sa["stdev_pyes"])
    row("frac p_yes > 0.5",  sb["frac_pyes_gt_05"], sa["frac_pyes_gt_05"], ".4f")
    row("frac extreme (<.1 or >.9)", sb["frac_pyes_extreme"], sa["frac_pyes_extreme"])
    row("frac uncertain (.4-.6)",    sb["frac_pyes_uncertain"], sa["frac_pyes_uncertain"])
    if sb["mean_conf"] is not None and sa["mean_conf"] is not None:
        row("mean confidence", sb["mean_conf"], sa["mean_conf"])
        row("median confidence", sb["median_conf"], sa["median_conf"])


def print_per_mech(rows_b, rows_a):
    by_mech_b = defaultdict(list)
    by_mech_a = defaultdict(list)
    for r in rows_b:
        by_mech_b[r["mech"]].append(r)
    for r in rows_a:
        by_mech_a[r["mech"]].append(r)

    mechs = sorted(
        set(list(by_mech_b.keys()) + list(by_mech_a.keys())),
        key=lambda m: len(by_mech_b.get(m, [])) + len(by_mech_a.get(m, [])),
        reverse=True,
    )

    print(f"\n{'=' * 110}")
    print("PER-MECH P_YES / CONFIDENCE COMPARISON")
    print(f"{'=' * 110}")
    print(f"\n  {'mech':<44} {'Nb':>6} {'Na':>6}  "
          f"{'pyesB':>7} {'pyesA':>7} {'Δpyes':>7}  "
          f"{'%>0.5B':>7} {'%>0.5A':>7}  {'cnfB':>6} {'cnfA':>6} {'Δcnf':>7}")
    print("  " + "-" * 105)
    for mech in mechs:
        sb = stats_block(by_mech_b.get(mech, []))
        sa = stats_block(by_mech_a.get(mech, []))
        nb = sb["n"] if sb else 0
        na = sa["n"] if sa else 0
        if nb + na < 50:
            continue
        if sb is None or sa is None:
            continue
        d_pyes = sa["mean_pyes"] - sb["mean_pyes"]
        d_conf = (sa["mean_conf"] - sb["mean_conf"]) if (sb["mean_conf"] is not None and sa["mean_conf"] is not None) else None
        cnfB = f"{sb['mean_conf']:>5.3f}" if sb["mean_conf"] is not None else "  n/a"
        cnfA = f"{sa['mean_conf']:>5.3f}" if sa["mean_conf"] is not None else "  n/a"
        d_conf_s = f"{d_conf:+7.4f}" if d_conf is not None else "    n/a"
        print(f"  {mech:<44} {nb:>6} {na:>6}  "
              f"{sb['mean_pyes']:>7.4f} {sa['mean_pyes']:>7.4f} {d_pyes:+7.4f}  "
              f"{sb['frac_pyes_gt_05']*100:>6.1f}% {sa['frac_pyes_gt_05']*100:>6.1f}%  "
              f"{cnfB} {cnfA} {d_conf_s}")


def print_per_tool(rows_b, rows_a):
    by_tool_b = defaultdict(list)
    by_tool_a = defaultdict(list)
    for r in rows_b:
        by_tool_b[r["tool"]].append(r)
    for r in rows_a:
        by_tool_a[r["tool"]].append(r)

    tools = sorted(
        set(list(by_tool_b.keys()) + list(by_tool_a.keys())),
        key=lambda t: len(by_tool_b.get(t, [])) + len(by_tool_a.get(t, [])),
        reverse=True,
    )

    print(f"\n{'=' * 110}")
    print("PER-TOOL P_YES / CONFIDENCE COMPARISON")
    print(f"{'=' * 110}")
    print(f"\n  {'tool':<36} {'Nb':>6} {'Na':>6}  "
          f"{'pyesB':>7} {'pyesA':>7} {'Δpyes':>7}  "
          f"{'%>0.5B':>7} {'%>0.5A':>7}  {'cnfB':>6} {'cnfA':>6} {'Δcnf':>7}")
    print("  " + "-" * 105)
    for tool in tools:
        if tool == "unknown":
            continue
        sb = stats_block(by_tool_b.get(tool, []))
        sa = stats_block(by_tool_a.get(tool, []))
        nb = sb["n"] if sb else 0
        na = sa["n"] if sa else 0
        if nb + na < 20:
            continue
        if sb is None or sa is None:
            continue
        d_pyes = sa["mean_pyes"] - sb["mean_pyes"]
        d_conf = (sa["mean_conf"] - sb["mean_conf"]) if (sb["mean_conf"] is not None and sa["mean_conf"] is not None) else None
        flag = " !!" if abs(d_pyes) > 0.05 else ""
        cnfB = f"{sb['mean_conf']:>5.3f}" if sb["mean_conf"] is not None else "  n/a"
        cnfA = f"{sa['mean_conf']:>5.3f}" if sa["mean_conf"] is not None else "  n/a"
        d_conf_s = f"{d_conf:+7.4f}" if d_conf is not None else "    n/a"
        print(f"  {tool:<36} {nb:>6} {na:>6}  "
              f"{sb['mean_pyes']:>7.4f} {sa['mean_pyes']:>7.4f} {d_pyes:+7.4f}  "
              f"{sb['frac_pyes_gt_05']*100:>6.1f}% {sa['frac_pyes_gt_05']*100:>6.1f}%  "
              f"{cnfB} {cnfA} {d_conf_s}{flag}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_chain(label, mech_url, since_ts, split_ts):
    print(f"\n{'#' * 100}")
    print(f"# {label}")
    print(f"{'#' * 100}")

    t0 = time.time()
    print(f"[1/2] Fetching mech delivers since "
          f"{datetime.fromtimestamp(since_ts, tz=timezone.utc):%Y-%m-%d}...",
          flush=True)
    delivers = fetch_delivers(mech_url, since_ts)
    print(f"  {len(delivers)} delivers fetched in {time.time() - t0:.1f}s")

    print(f"[2/2] Parsing toolResponse JSON...", end=" ", flush=True)
    rows = process_delivers(delivers)
    parse_rate = len(rows) / len(delivers) * 100 if delivers else 0
    print(f"{len(rows)} parsed as p_yes/p_no JSON ({parse_rate:.1f}% of delivers)")

    rows_b = [r for r in rows if r["ts"] < split_ts]
    rows_a = [r for r in rows if r["ts"] >= split_ts]

    print_split(f"{label} — overall before/after split", rows_b, rows_a)
    print_per_mech(rows_b, rows_a)
    print_per_tool(rows_b, rows_a)

    print(f"\n{label} — DAILY TIMELINE")
    print_daily(rows, split_ts)


def main():
    parser = argparse.ArgumentParser(description="Compare mech tool p_yes/p_no/confidence trends across a split date")
    parser.add_argument("--days", type=int, default=14, help="Total lookback days (default: 14)")
    parser.add_argument("--split-date", type=str, default="2026-04-08",
                        help="Split date YYYY-MM-DD (default: 2026-04-08)")
    parser.add_argument("--chain", choices=["polygon", "gnosis", "both"], default="both",
                        help="Which marketplace to analyze (default: both)")
    args = parser.parse_args()

    since_ts = int(time.time()) - args.days * 86400
    split_ts = int(datetime.strptime(args.split_date, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())

    print(f"Window: {datetime.fromtimestamp(since_ts, tz=timezone.utc):%Y-%m-%d} → now")
    print(f"Split:  {args.split_date}")

    if args.chain in ("polygon", "both"):
        run_chain("PolyStrat marketplace (Polygon)", POLYGON_MECH_URL, since_ts, split_ts)

    if args.chain in ("gnosis", "both"):
        run_chain("OmenStrat marketplace (Gnosis)", GNOSIS_MECH_URL, since_ts, split_ts)


if __name__ == "__main__":
    main()
