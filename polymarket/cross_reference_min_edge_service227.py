"""
Cross-reference mech p_yes vs Polymarket best-ask at delivery time for the
17 mech requests made by service 227 Safe 0xdead1D0F135683EC517c13C1E4120B56cF322815
on Polygon over the last ~48h. Goal: confirm or rule out the min_edge=0.03
hypothesis in trader decision_maker Gate D.

For each of 17 requests:
  - Fetch IPFS request payload (prompt + market id)
  - Extract delivered JSON (p_yes, p_no, confidence, info_utility, tool)
  - Resolve the Polymarket question / condition id / YES+NO token ids
  - Pull Polymarket CLOB price history around delivery timestamp
  - Compute best available edge vs p_yes and vs p_no
  - Report whether either side would have crossed 0.03
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv

load_dotenv("/home/lockhart/work/valory/repos/random-valory-scripts/.env")

SAFE = "0xdead1d0f135683ec517c13c1e4120b56cf322815"
OLAS_MECH_POLYGON = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs"
CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"


def gql(q, variables=None):
    r = requests.post(OLAS_MECH_POLYGON, json={"query": q, "variables": variables or {}},
                      timeout=60)
    r.raise_for_status()
    b = r.json()
    if "errors" in b: raise RuntimeError(b["errors"])
    return b["data"]


def fetch_requests():
    q = """
    query Sender($id: ID!) {
      sender(id: $id) {
        requests(first: 1000, orderBy: blockTimestamp, orderDirection: asc) {
          id blockTimestamp transactionHash isDelivered
          parsedRequest { tool hash prompt questionTitle }  # keep prompt
          deliveries { blockTimestamp transactionHash toolResponse }
        }
      }
    }"""
    return gql(q, {"id": SAFE}).get("sender", {}).get("requests", [])


def fetch_ipfs(multihash):
    if not multihash:
        return None
    # request multihash in subgraph is prefixed `f01701220...`
    cid = multihash
    if multihash.startswith("0x"):
        cid = f"f01701220{multihash[2:]}"
    url = f"{IPFS_GATEWAY}/{cid}"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:
            return r.text
    except Exception:
        return None


def parse_prediction(tool_response):
    if not tool_response:
        return None
    s = tool_response if isinstance(tool_response, str) else json.dumps(tool_response)
    # try the whole thing
    try:
        p = json.loads(s)
        if isinstance(p, dict) and "p_yes" in p:
            return p
    except Exception:
        pass
    import re
    for m in re.finditer(r"\{[^{}]*\}", s, re.DOTALL):
        try:
            p = json.loads(m.group(0))
            if isinstance(p, dict) and "p_yes" in p:
                return p
        except Exception:
            continue
    return None


def polymarket_market_by_condition(cond_id):
    # Try gamma API first (supports condition_ids=0x...)
    try:
        url = f"{GAMMA}/markets"
        r = requests.get(url, params={"condition_ids": cond_id}, timeout=30)
        if r.status_code == 200:
            j = r.json()
            if isinstance(j, list) and j:
                return j[0]
    except Exception:
        pass
    # CLOB fallback by condition_id
    try:
        r = requests.get(f"{CLOB}/markets/{cond_id}", timeout=30)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def polymarket_market_by_token(token_id):
    try:
        r = requests.get(f"{CLOB}/markets/{token_id}", timeout=30)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def polymarket_prices_history(token_id, start_ts, end_ts):
    # CLOB prices-history — returns midpoint history for a token id
    for params in (
        {"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": 1},
        {"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": 60},
        {"market": token_id, "interval": "1d", "fidelity": 1},
    ):
        try:
            r = requests.get(f"{CLOB}/prices-history", params=params, timeout=30)
            if r.status_code == 200:
                j = r.json()
                hist = j.get("history") if isinstance(j, dict) else None
                if hist:
                    return hist
        except Exception:
            pass
    return None


def polymarket_book(token_id):
    """Current orderbook — best bid/ask NOW (historical book not available)."""
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def closest_price(history, ts):
    if not history: return None
    best = min(history, key=lambda p: abs(int(p.get("t",0)) - int(ts)))
    return best


def fmt_ts(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def main():
    print("Fetching 17 mech requests + deliveries …")
    reqs = fetch_requests()
    print(f"  got {len(reqs)} requests")

    # Pull IPFS for each request in parallel
    print("Fetching IPFS request payloads …")
    ipfs_payloads = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        fut_map = {ex.submit(fetch_ipfs, (r.get("parsedRequest") or {}).get("hash")): r["id"]
                   for r in reqs}
        for f in as_completed(fut_map):
            ipfs_payloads[fut_map[f]] = f.result()

    rows = []
    for r in reqs:
        pr = r.get("parsedRequest") or {}
        delivs = r.get("deliveries") or []
        first_d = delivs[0] if delivs else {}
        pred = parse_prediction(first_d.get("toolResponse"))
        ipfs = ipfs_payloads.get(r["id"]) or {}
        if isinstance(ipfs, dict):
            prompt = ipfs.get("prompt") or ipfs.get("tool_args", {}).get("prompt") if isinstance(ipfs.get("tool_args"), dict) else ipfs.get("prompt")
            tool = ipfs.get("tool") or pr.get("tool")
            # Polymarket request hash layout often: prompt contains the market question;
            # sometimes fields `market_id`, `condition_id`, or `token_id`.
            cond_id = (ipfs.get("condition_id") or ipfs.get("conditionId")
                       or ipfs.get("market_id") or ipfs.get("marketId"))
            token_yes = (ipfs.get("token_id") or ipfs.get("tokenId")
                         or ipfs.get("yes_token") or ipfs.get("yesTokenId"))
        else:
            prompt = None; tool = pr.get("tool"); cond_id = None; token_yes = None
        rows.append({
            "req_id": r["id"],
            "req_tx": r["transactionHash"],
            "req_ts": int(r["blockTimestamp"]),
            "tool": tool,
            "question": pr.get("questionTitle") or (prompt[:200] if isinstance(prompt, str) else None),
            "prompt_snippet": (prompt[:300] if isinstance(prompt, str) else None),
            "ipfs_keys": list(ipfs.keys()) if isinstance(ipfs, dict) else None,
            "ipfs_full": ipfs if isinstance(ipfs, dict) else None,
            "deliv_ts": int(first_d["blockTimestamp"]) if first_d else None,
            "deliv_tx": first_d.get("transactionHash"),
            "prediction": pred,
            "cond_id": cond_id,
            "token_yes": token_yes,
        })

    # Show raw IPFS structure of the first request to understand field names
    print("\n=== IPFS payload keys (first few) ===")
    for i, x in enumerate(rows[:3], 1):
        print(f"[{i}] tool={x['tool']}  req_ts={fmt_ts(x['req_ts'])}")
        print(f"    ipfs_keys: {x['ipfs_keys']}")
        print(f"    ipfs_full: {json.dumps(x['ipfs_full'], indent=2)[:1500] if x['ipfs_full'] else 'None'}")

    # For rows that have condition/token ids or a question, try to resolve
    # Polymarket market and token ids, then fetch history around delivery ts.
    # If we only have a question string, use gamma search to find matching market.
    def resolve_market_from_row(x):
        q = x["question"]
        if not q:
            return None
        qclean = q.strip().split("\u241f")[0][:200]
        try:
            r = requests.get(f"{GAMMA}/public-search",
                             params={"q": qclean}, timeout=30)
            if r.status_code != 200:
                return None
            events = (r.json() or {}).get("events") or []
        except Exception:
            return None
        best = None
        for ev in events:
            for m in (ev.get("markets") or []):
                mq = (m.get("question") or "").strip()
                if mq == qclean:
                    return m
                if not best and qclean.lower() in mq.lower():
                    best = m
        return best

    print("\n=== Resolving markets + fetching history ===")
    for x in rows:
        m = resolve_market_from_row(x)
        x["market"] = m
        # Extract token ids
        yes_tok = None; no_tok = None
        if m:
            clob_toks = m.get("clobTokenIds") or m.get("clob_token_ids") or m.get("tokens")
            if isinstance(clob_toks, str):
                try: clob_toks = json.loads(clob_toks)
                except Exception: pass
            if isinstance(clob_toks, list) and len(clob_toks) >= 2:
                # gamma tokens can be list of strings or list of objects {token_id, outcome}
                if isinstance(clob_toks[0], str):
                    yes_tok, no_tok = clob_toks[0], clob_toks[1]
                elif isinstance(clob_toks[0], dict):
                    for tok in clob_toks:
                        out = (tok.get("outcome") or "").lower()
                        tid = tok.get("token_id") or tok.get("tokenID")
                        if out.startswith("yes"): yes_tok = tid
                        elif out.startswith("no"): no_tok = tid
        x["yes_tok"] = yes_tok
        x["no_tok"] = no_tok

        # Fetch historical price around delivery time
        x["yes_price_at_deliv"] = None
        x["no_price_at_deliv"] = None
        x["yes_book_now"] = None
        if yes_tok and x["deliv_ts"]:
            start = x["deliv_ts"] - 3600
            end = x["deliv_ts"] + 3600
            hist = polymarket_prices_history(yes_tok, start, end)
            if hist:
                cp = closest_price(hist, x["deliv_ts"])
                if cp: x["yes_price_at_deliv"] = cp.get("p")
            # Book now
            book = polymarket_book(yes_tok)
            if book:
                asks = book.get("asks") or []
                bids = book.get("bids") or []
                x["yes_book_now"] = {
                    "best_ask": (asks[0]["price"] if asks else None),
                    "best_bid": (bids[-1]["price"] if bids else None),
                }
        if no_tok and x["deliv_ts"]:
            start = x["deliv_ts"] - 3600
            end = x["deliv_ts"] + 3600
            hist = polymarket_prices_history(no_tok, start, end)
            if hist:
                cp = closest_price(hist, x["deliv_ts"])
                if cp: x["no_price_at_deliv"] = cp.get("p")

    # --- Table ---
    print("\n\n=== PER-REQUEST TABLE ===")
    print(f"{'#':>2} {'deliv_ts':<20} {'tool':<14} {'p_yes':>6} {'conf':>5} "
          f"{'yes@deliv':>10} {'no@deliv':>10} {'edge_yes':>9} {'edge_no':>9}  question")
    for i, x in enumerate(rows, 1):
        p = x["prediction"] or {}
        pyes = p.get("p_yes")
        pno = p.get("p_no")
        conf = p.get("confidence")
        ydel = x["yes_price_at_deliv"]
        ndel = x["no_price_at_deliv"]
        edge_yes = (pyes - float(ydel)) if (pyes is not None and ydel is not None) else None
        edge_no = (pno - float(ndel)) if (pno is not None and ndel is not None) else None
        q = (x["question"] or "")[:60]
        def f(v, w=6, d=3):
            return f"{v:>{w}.{d}f}" if isinstance(v,(int,float)) else str(v)[:w].rjust(w)
        # safe fmt for strings
        y_s = f"{float(ydel):.3f}" if ydel is not None else "-"
        n_s = f"{float(ndel):.3f}" if ndel is not None else "-"
        ey_s = f"{edge_yes:+.3f}" if edge_yes is not None else "-"
        en_s = f"{edge_no:+.3f}" if edge_no is not None else "-"
        pyes_s = f"{pyes:.3f}" if isinstance(pyes,(int,float)) else "-"
        conf_s = f"{conf:.2f}" if isinstance(conf,(int,float)) else "-"
        print(f"{i:>2} {fmt_ts(x['deliv_ts']) if x['deliv_ts'] else '-':<20} "
              f"{str(x['tool'])[:14]:<14} {pyes_s:>6} {conf_s:>5} "
              f"{y_s:>10} {n_s:>10} {ey_s:>9} {en_s:>9}  {q}")

    # --- Per-row detail for first 5 ---
    print("\n\n=== DETAILS (first 5 rows) ===")
    for i, x in enumerate(rows[:5], 1):
        print(f"\n[{i}]")
        print(f"  req_ts    : {fmt_ts(x['req_ts'])}")
        print(f"  deliv_ts  : {fmt_ts(x['deliv_ts']) if x['deliv_ts'] else '-'}")
        print(f"  tool      : {x['tool']}")
        print(f"  prediction: {x['prediction']}")
        print(f"  question  : {x['question']}")
        print(f"  ipfs keys : {x['ipfs_keys']}")
        if x["ipfs_full"]:
            print(f"  ipfs dump : {json.dumps(x['ipfs_full'], indent=2)[:1500]}")
        print(f"  resolved market: {x['market'].get('question') if x['market'] else None}")
        print(f"  yes_tok   : {x['yes_tok']}")
        print(f"  no_tok    : {x['no_tok']}")
        print(f"  yes@deliv : {x['yes_price_at_deliv']}")
        print(f"  no@deliv  : {x['no_price_at_deliv']}")
        print(f"  yes_book_now: {x['yes_book_now']}")

    # --- Summary ---
    print("\n\n=== SUMMARY ===")
    edges = []
    for x in rows:
        p = x["prediction"] or {}
        pyes = p.get("p_yes"); pno = p.get("p_no")
        ydel = x["yes_price_at_deliv"]
        ndel = x["no_price_at_deliv"]
        ey = (pyes - float(ydel)) if (pyes is not None and ydel is not None) else None
        en = (pno - float(ndel)) if (pno is not None and ndel is not None) else None
        edges.append({"i": rows.index(x)+1, "ey": ey, "en": en,
                      "max": max([e for e in (ey, en) if e is not None], default=None)})
    over = [e for e in edges if e["max"] is not None and e["max"] >= 0.03]
    under = [e for e in edges if e["max"] is not None and e["max"] < 0.03]
    missing = [e for e in edges if e["max"] is None]
    print(f"  rows with resolvable Polymarket price: {len(over)+len(under)}")
    print(f"  rows with max(edge_yes, edge_no) >= 0.03: {len(over)}")
    print(f"  rows with max(edge_yes, edge_no) <  0.03: {len(under)}")
    print(f"  rows where price could not be resolved: {len(missing)}")
    if over:
        print(f"  passing rows: {[e['i'] for e in over]}")
    if under:
        print(f"  failing rows: {[(e['i'], round(e['max'],3)) for e in under]}")

    # dump json for inspection
    out_path = "/tmp/service227_cross_ref.json"
    with open(out_path, "w") as f:
        def default(o):
            if isinstance(o, bytes): return o.hex()
            return str(o)
        json.dump(rows, f, indent=2, default=default)
    print(f"\nFull dump: {out_path}")


if __name__ == "__main__":
    main()
