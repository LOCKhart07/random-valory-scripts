"""
Classify deliver responses over time for a given mech + tool.

Buckets each response as:
  - structured: JSON dict with p_yes/p_no keys (expected shape)
  - facts-leak: non-JSON text containing <facts> (reasoning leaked as response)
  - json-other: JSON but missing p_yes (unexpected shape)
  - non-json:   non-JSON, non-<facts> text
  - empty

Groups counts per hour (or per day for longer windows) so you can see whether
the malformed-response rate changed around a deployment time.

For the `structured` bucket we also summarise the p_yes / p_no / confidence
distributions (mean, median, stdev, extremes, uncertain band, invariant
violations) and can dump the raw triples with --dump-values.

Usage:
    python tool_response_trend.py <mech> --tool superforcaster --hours 24
    python tool_response_trend.py <mech> --tool factual_research --days 30 --bucket day
    python tool_response_trend.py <mech> --tool factual_research --days 30 --dump-values
"""

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests
from requests.exceptions import ConnectionError, Timeout

SUBGRAPH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"
REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 3
PAGE_SIZE = 1000

QUERY = """
query Delivers($where: Deliver_filter!, $first: Int!, $skip: Int!) {
    delivers(
        first: $first
        skip: $skip
        orderBy: blockTimestamp
        orderDirection: desc
        where: $where
    ) {
        blockTimestamp
        transactionHash
        toolResponse
        request { parsedRequest { tool } }
    }
}
"""


def _post_with_retry(url, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, **kwargs)
            r.raise_for_status()
            return r
        except (Timeout, ConnectionError) as e:
            last = e
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code < 500:
                raise
            last = e
        if attempt == MAX_RETRIES:
            break
        time.sleep(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
    raise last


def fetch(mech, start_ts):
    where = {"mech": mech.lower(), "blockTimestamp_gte": str(start_ts)}
    out = []
    skip = 0
    while True:
        r = _post_with_retry(
            SUBGRAPH_URL,
            json={"query": QUERY, "variables": {"where": where, "first": PAGE_SIZE, "skip": skip}},
            headers={"Content-Type": "application/json"},
        )
        data = r.json()
        if "data" not in data:
            print(f"Subgraph error: {data}", file=sys.stderr)
            sys.exit(1)
        page = data["data"]["delivers"]
        out.extend(page)
        if len(page) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    return out


def classify(resp: str) -> str:
    if not resp:
        return "empty"
    stripped = resp.lstrip()
    try:
        obj = json.loads(resp)
        if isinstance(obj, dict) and "p_yes" in obj:
            return "structured"
        return "json-other"
    except (json.JSONDecodeError, TypeError):
        pass
    if "<facts>" in resp or stripped.startswith("<facts>"):
        return "facts-leak"
    return "non-json"


def parse_triple(resp: str):
    """Return dict with p_yes/p_no/confidence (floats or None) or None if not structured."""
    try:
        obj = json.loads(resp)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict) or "p_yes" not in obj:
        return None

    def f(k):
        v = obj.get(k)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {"p_yes": f("p_yes"), "p_no": f("p_no"), "confidence": f("confidence")}


def value_summary(triples: list[dict]) -> None:
    """Print distribution stats for p_yes / p_no / confidence."""
    if not triples:
        return

    def stats(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        return {
            "n": len(vals),
            "mean": statistics.mean(vals),
            "median": statistics.median(vals),
            "stdev": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            "min": min(vals),
            "max": max(vals),
        }

    rows = [
        ("p_yes",       [t["p_yes"] for t in triples]),
        ("p_no",        [t["p_no"] for t in triples]),
        ("confidence",  [t["confidence"] for t in triples]),
    ]

    print()
    print("Structured-response values:")
    print(f"  {'field':<12} {'n':>5} {'mean':>8} {'median':>8} {'stdev':>8} {'min':>6} {'max':>6}")
    for name, vals in rows:
        s = stats(vals)
        if s is None:
            print(f"  {name:<12} {'n/a':>5}")
            continue
        print(f"  {name:<12} {s['n']:>5d} {s['mean']:>8.4f} {s['median']:>8.4f} "
              f"{s['stdev']:>8.4f} {s['min']:>6.3f} {s['max']:>6.3f}")

    # p_yes distribution buckets + conditional confidence + invariants
    pys_full = [(t["p_yes"], t["confidence"]) for t in triples if t["p_yes"] is not None]
    n = len(pys_full)
    if n:
        edges = [0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.000001]
        labels = ["<0.1", "0.1-0.3", "0.3-0.5", "0.5-0.7", "0.7-0.9", ">0.9"]
        counts = [0] * len(labels)
        conf_sums = [0.0] * len(labels)
        conf_ns = [0] * len(labels)
        for v, c in pys_full:
            for i in range(len(labels)):
                if edges[i] <= v < edges[i + 1]:
                    counts[i] += 1
                    if c is not None:
                        conf_sums[i] += c
                        conf_ns[i] += 1
                    break
        print()
        print(f"p_yes distribution (mean confidence in bucket):")
        print(f"  {'bucket':<8} {'count':>6} {'%':>6}  {'mean cf':>8}")
        for lab, ct, cs, cn in zip(labels, counts, conf_sums, conf_ns):
            mean_cf = f"{cs / cn:.4f}" if cn > 0 else "   n/a"
            print(f"  {lab:<8} {ct:>6d} {100 * ct / n:>5.1f}%  {mean_cf:>8}")

    # Invariant checks on the structured rows
    sum_issues = []
    range_issues = []
    degenerate = 0
    for t in triples:
        py, pn = t["p_yes"], t["p_no"]
        c = t["confidence"]
        if py is not None and pn is not None:
            if abs(py + pn - 1.0) > 0.01:
                sum_issues.append((py, pn))
        for name, v in (("p_yes", py), ("p_no", pn), ("confidence", c)):
            if v is not None and not (0.0 <= v <= 1.0):
                range_issues.append((name, v))
        if py == 0.5 and pn == 0.5 and (c is None or c == 0):
            degenerate += 1

    print()
    print("Invariant checks:")
    print(f"  p_yes + p_no != 1 (±0.01)  : {len(sum_issues)}")
    print(f"  value outside [0, 1]       : {len(range_issues)}")
    print(f"  degenerate (0.5/0.5, no cf): {degenerate}")
    if sum_issues[:3]:
        print("  sample sum issues:", sum_issues[:3])
    if range_issues[:3]:
        print("  sample range issues:", range_issues[:3])


def bucket_key(ts: int, bucket: str) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if bucket == "hour":
        return dt.strftime("%Y-%m-%d %H:00")
    return dt.strftime("%Y-%m-%d")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mech")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--hours", type=float)
    g.add_argument("--days", type=float)
    p.add_argument("--bucket", choices=["hour", "day"], default="hour")
    p.add_argument("--tool", required=True, help="tool name in parsedRequest.tool (e.g. factual_research)")
    p.add_argument("--dump-values", action="store_true",
                   help="print every (p_yes, p_no, confidence) triple with timestamp + tx")
    args = p.parse_args()

    window_s = args.hours * 3600 if args.hours else (args.days or 1) * 86400
    start_ts = int(time.time() - window_s)

    print(f"Mech:  {args.mech}")
    print(f"Tool:  {args.tool}")
    print(f"Since: {datetime.fromtimestamp(start_ts, tz=timezone.utc):%Y-%m-%d %H:%M UTC}")
    print()

    delivers = fetch(args.mech, start_ts)
    rows = [
        d for d in delivers
        if ((d.get("request") or {}).get("parsedRequest") or {}).get("tool") == args.tool
    ]
    print(f"{args.tool} delivers in window: {len(rows)}")
    if not rows:
        return

    per_bucket: dict[str, Counter] = defaultdict(Counter)
    overall: Counter = Counter()
    facts_examples: list[tuple[int, str, str]] = []
    bad_records: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    triples: list[dict] = []
    triple_rows: list[tuple[int, str, dict]] = []
    for d in rows:
        ts = int(d["blockTimestamp"])
        raw = d.get("toolResponse") or ""
        cls = classify(raw)
        per_bucket[bucket_key(ts, args.bucket)][cls] += 1
        overall[cls] += 1
        if cls == "structured":
            t = parse_triple(raw)
            if t is not None:
                triples.append(t)
                triple_rows.append((ts, d["transactionHash"], t))
        if cls == "facts-leak" and len(facts_examples) < 3:
            facts_examples.append(
                (ts, d["transactionHash"], raw[:200].replace("\n", " "))
            )
        if cls in ("non-json", "json-other", "empty"):
            bad_records[cls].append(
                (ts, d["transactionHash"], raw[:300].replace("\n", " "))
            )

    print()
    print("Overall:")
    for k, v in overall.most_common():
        pct = 100 * v / len(rows)
        print(f"  {k:12s} {v:4d} ({pct:5.1f}%)")

    print()
    print(f"Per {args.bucket}:")
    header = f"{'bucket':<18s} {'total':>6} {'struct':>7} {'facts':>6} {'other':>6} {'nonjson':>8} {'empty':>6} {'bad%':>6}"
    print(header)
    for key in sorted(per_bucket.keys()):
        c = per_bucket[key]
        total = sum(c.values())
        bad = total - c["structured"]
        pct = 100 * bad / total if total else 0
        print(
            f"{key:<18s} {total:>6d} {c['structured']:>7d} {c['facts-leak']:>6d} "
            f"{c['json-other']:>6d} {c['non-json']:>8d} {c['empty']:>6d} {pct:>5.1f}%"
        )

    if facts_examples:
        print()
        print("facts-leak samples:")
        for ts, tx, preview in facts_examples:
            t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            print(f"  {t}  tx={tx}")
            print(f"    {preview}")

    for cls, samples in bad_records.items():
        if not samples:
            continue
        signatures = Counter()
        for _, _, preview in samples:
            if "length limit was reached" in preview:
                signatures["length-limit-reached"] += 1
            elif "Could not parse response" in preview:
                signatures["parse-failure-other"] += 1
            else:
                signatures["other"] += 1
        print()
        print(f"{cls} pattern breakdown ({len(samples)} total):")
        for sig, n in signatures.most_common():
            print(f"  {sig:<28} {n}")
        print(f"{cls} samples (first 5):")
        for ts, tx, preview in samples[:5]:
            t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            print(f"  {t}  tx={tx}")
            print(f"    {preview}")

    value_summary(triples)

    if triple_rows:
        extreme_low = sorted(
            [(ts, tx, t) for ts, tx, t in triple_rows if t["p_yes"] is not None and t["p_yes"] < 0.05],
            key=lambda x: x[2]["p_yes"],
        )[:5]
        extreme_high = sorted(
            [(ts, tx, t) for ts, tx, t in triple_rows if t["p_yes"] is not None and t["p_yes"] > 0.9],
            key=lambda x: -x[2]["p_yes"],
        )[:5]
        if extreme_low:
            print()
            print(f"Extreme-low p_yes (<0.05) samples, sorted ascending:")
            for ts, tx, t in extreme_low:
                tstr = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                print(f"  {tstr}  py={t['p_yes']:.4f} pn={t['p_no']:.4f} cf={t['confidence']:.4f}  tx={tx}")
        if extreme_high:
            print()
            print(f"Extreme-high p_yes (>0.9) samples, sorted descending:")
            for ts, tx, t in extreme_high:
                tstr = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                print(f"  {tstr}  py={t['p_yes']:.4f} pn={t['p_no']:.4f} cf={t['confidence']:.4f}  tx={tx}")

    if args.dump_values and triple_rows:
        print()
        print(f"All ({len(triple_rows)}) structured values (ts, p_yes, p_no, confidence, tx):")
        for ts, tx, t in sorted(triple_rows):
            tstr = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            py = f"{t['p_yes']:.4f}" if t['p_yes'] is not None else "  n/a"
            pn = f"{t['p_no']:.4f}" if t['p_no'] is not None else "  n/a"
            cf = f"{t['confidence']:.4f}" if t['confidence'] is not None else "  n/a"
            print(f"  {tstr}  py={py}  pn={pn}  cf={cf}  tx={tx}")


if __name__ == "__main__":
    main()
