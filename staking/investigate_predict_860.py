"""
On-chain investigation for PREDICT-860: Pearl Trader Agent Safe burns xDAI/wxDAI
without meeting staking KPI.

Safe: 0x37c241945001F6c26C886C8d551cc2e6cf34C214 (service 697, pearl_beta_mech_marketplace_1)

Pulls:
  - All internal txs from the Safe (native xDAI outflow)
  - All ERC20 token transfers from the Safe (wxDAI / OLAS outflow)
  - Classifies destinations: FPMM/conditional tokens (bets) vs mech marketplace
    (request fees) vs other
  - Counts mech requests per staking epoch and compares to the activity-checker
    liveness ratio KPI
"""
import json
import os
import sys
import time
from collections import defaultdict, Counter
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

SAFE = "0x37c241945001F6c26C886C8d551cc2e6cf34C214"
SAFE_LC = SAFE.lower()
SERVICE_ID = 697
STAKING_CONTRACT = "0xab10188207ea030555f53c8a84339a92f473aa5e"
ACTIVITY_CHECKER = "0x95b37c45badaf4668c18d00501948196761736b1"
WXDAI = "0xe91d153e0b41518a2ce8dd3d7944fa863463a97d"
OLAS = "0xce11e14225575945b8e6dc0d4f2dd4c570f79d9f"

# Gnosis mech marketplace (pearl_beta uses this)
MECH_MARKETPLACE = "0x4554fe75c1f5576c1d7f765b2a036c199adae329"  # Gnosis

BS = "https://gnosis.blockscout.com/api"

GNOSIS_RPC = os.getenv("GNOSIS_RPC")
w3 = Web3(Web3.HTTPProvider(GNOSIS_RPC, request_kwargs={"timeout": 60}))


def bs_paged(module, action, extra=""):
    """Fetch all pages from blockscout."""
    out = []
    page = 1
    while True:
        url = f"{BS}?module={module}&action={action}&address={SAFE}&page={page}&offset=1000&sort=asc{extra}"
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        d = r.json()
        if d.get("status") != "1":
            break
        rows = d.get("result", [])
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
        time.sleep(0.3)
    return out


def main():
    print(f"Fetching on-chain data for Safe {SAFE}...", file=sys.stderr)
    normal = bs_paged("account", "txlist")
    internal = bs_paged("account", "txlistinternal")
    tokens = bs_paged("account", "tokentx")
    print(f"  normal txs: {len(normal)}", file=sys.stderr)
    print(f"  internal txs: {len(internal)}", file=sys.stderr)
    print(f"  token transfers: {len(tokens)}", file=sys.stderr)

    # 1. Native xDAI outflow (from Safe)
    xdai_out = defaultdict(int)  # to_addr -> wei
    xdai_total = 0
    for t in internal:
        if t.get("from", "").lower() == SAFE_LC and t.get("isError", "0") == "0":
            v = int(t.get("value", "0") or "0")
            if v == 0:
                continue
            dst = t.get("to", "").lower()
            xdai_out[dst] += v
            xdai_total += v

    # 2. ERC20 outflow (wxDAI, OLAS, etc) from Safe
    token_out = defaultdict(lambda: defaultdict(int))  # token -> to_addr -> amount
    token_symbol = {}
    token_decimals = {}
    for t in tokens:
        if t.get("from", "").lower() != SAFE_LC:
            continue
        tok = t.get("contractAddress", "").lower()
        token_symbol[tok] = t.get("tokenSymbol", "?")
        token_decimals[tok] = int(t.get("tokenDecimal", "18") or "18")
        v = int(t.get("value", "0") or "0")
        dst = t.get("to", "").lower()
        token_out[tok][dst] += v

    # 3. Mech request count: find calls to MECH_MARKETPLACE initiated by Safe
    #    (execTransaction inner call) - look at internal txs from Safe to marketplace
    mech_requests = []  # timestamps
    mech_fee_total = 0
    for t in internal:
        if t.get("from", "").lower() == SAFE_LC and t.get("to", "").lower() == MECH_MARKETPLACE:
            mech_requests.append(int(t.get("timeStamp", "0")))
            mech_fee_total += int(t.get("value", "0") or "0")

    # Also check normal txs ending at marketplace from Safe owners (Safe's execTransaction target)
    mech_marketplace_calls = 0
    for t in normal:
        # the outer tx's `to` is the Safe itself (execTransaction). The inner target
        # appears in internal txs. So we count internals above.
        pass

    # 4. Classify wxDAI destinations
    wxdai_dests = token_out.get(WXDAI, {})
    # Fetch code type hints for top destinations
    top_dests = sorted(wxdai_dests.items(), key=lambda x: -x[1])[:20]

    # 5. Native xDAI top destinations
    top_xdai = sorted(xdai_out.items(), key=lambda x: -x[1])[:20]

    # Print report
    print("\n=== SUMMARY ===")
    print(f"Safe: {SAFE}  (service {SERVICE_ID})")
    print(f"Native xDAI outflow total: {xdai_total/1e18:,.4f} xDAI  over {len([1 for t in internal if t.get('from','').lower()==SAFE_LC and int(t.get('value','0') or '0')>0])} internal txs")
    for tok, dests in token_out.items():
        total = sum(dests.values())
        dec = token_decimals[tok]
        print(f"ERC20 {token_symbol[tok]} ({tok}) outflow: {total/10**dec:,.4f}")

    print("\n=== TOP NATIVE xDAI DESTINATIONS ===")
    for dst, v in top_xdai:
        print(f"  {dst}  {v/1e18:,.4f} xDAI")

    print("\n=== TOP wxDAI DESTINATIONS ===")
    for dst, v in top_dests:
        print(f"  {dst}  {v/1e18:,.4f} wxDAI")

    print(f"\n=== MECH MARKETPLACE INTERACTIONS ===")
    print(f"Internal calls Safe -> {MECH_MARKETPLACE}: {len(mech_requests)}")
    print(f"Total value sent to marketplace: {mech_fee_total/1e18:,.4f} xDAI")
    if mech_requests:
        mech_requests.sort()
        first = datetime.fromtimestamp(mech_requests[0], tz=timezone.utc)
        last = datetime.fromtimestamp(mech_requests[-1], tz=timezone.utc)
        print(f"First: {first}")
        print(f"Last:  {last}")
        # bin by day
        by_day = Counter()
        for ts in mech_requests:
            d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            by_day[d] += 1
        print("Requests per UTC day:")
        for d in sorted(by_day):
            print(f"  {d}: {by_day[d]}")

    # 6. KPI from activity checker
    print("\n=== STAKING KPI ===")
    try:
        ac_abi = [
            {"name":"livenessRatio","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
        ]
        ac = w3.eth.contract(address=Web3.to_checksum_address(ACTIVITY_CHECKER), abi=ac_abi)
        lr = ac.functions.livenessRatio().call()
        print(f"livenessRatio: {lr}  (requests per second * 1e18)")
        # Required requests per 24h liveness period:
        req_per_day = lr * 86400 / 1e18
        print(f"=> {req_per_day:.4f} mech requests per 24h liveness period required to earn rewards")
    except Exception as e:
        print(f"activity checker read failed: {e}")

    # 7. Bet sizing: look at wxDAI transfers to FPMMs (buy) and see median/mean/max
    # FPMM contracts are contracts with FPMMBuy signature. We'll just look at large wxDAI destinations.
    bet_values = []
    fpmm_candidates = set()
    for t in tokens:
        if t.get("from", "").lower() != SAFE_LC:
            continue
        if t.get("contractAddress", "").lower() != WXDAI:
            continue
        dst = t.get("to", "").lower()
        # skip exchange/router; tentatively treat all non-EOA destinations as FPMMs
        # (omen FPMM addresses are per-market)
        v = int(t.get("value", "0") or "0") / 1e18
        bet_values.append((v, dst, t.get("hash"), int(t.get("timeStamp","0"))))

    print("\n=== wxDAI TRANSFERS FROM SAFE (bet sizing) ===")
    print(f"Total wxDAI transfers: {len(bet_values)}")
    if bet_values:
        vs = [v for v,_,_,_ in bet_values]
        print(f"  Total: {sum(vs):,.4f} wxDAI")
        print(f"  Mean:  {sum(vs)/len(vs):.4f}  Median: {sorted(vs)[len(vs)//2]:.4f}")
        print(f"  Max:   {max(vs):.4f}  Min: {min(vs):.6f}")
        # count large bets (>= 1 xDAI)
        big = [v for v in vs if v >= 1]
        print(f"  Bets >= 1 xDAI: {len(big)}  sum={sum(big):,.4f}")
        big2 = [v for v in vs if v >= 1.5]
        print(f"  Bets >= 1.5 xDAI: {len(big2)}  sum={sum(big2):,.4f}")

    # Output raw summary JSON
    out = {
        "safe": SAFE,
        "xdai_out_total": xdai_total / 1e18,
        "xdai_top": [(d, v/1e18) for d, v in top_xdai],
        "wxdai_top": [(d, v/1e18) for d, v in top_dests],
        "mech_marketplace_calls": len(mech_requests),
        "mech_fee_total_xdai": mech_fee_total/1e18,
        "bet_count_wxdai_transfers": len(bet_values),
        "bet_total_wxdai": sum(v for v,_,_,_ in bet_values) if bet_values else 0,
    }
    with open("/tmp/predict-860/summary.json", "w") as f:
        json.dump(out, f, indent=2, default=str)


if __name__ == "__main__":
    main()
