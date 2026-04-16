"""
Onchain forensics for Polygon service 227 (polygon_beta_1) — Safe
0xdead1D0F135683EC517c13C1E4120B56cF322815.

Customer ticket: agent never placed a bet. totalBets=0 reported by the
Polymarket subgraph. Independent on-chain verification + diagnosis.

Pulls:
  - Polygonscan (Etherscan V2, chainid=137) normal/internal/token tx lists
  - Olas mech-marketplace Polygon subgraph (sender row)
  - Polymarket bets subgraph (traderAgent row)
  - On-chain reads via Polygon RPC: staking KPI parameters, mech-activity
    multisig nonces, token balances, ERC20 allowances to both CTF exchanges.

Usage:
    poetry run python polymarket/investigate_zd_service227.py
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

SAFE = Web3.to_checksum_address("0xdead1D0F135683EC517c13C1E4120B56cF322815")
SAFE_LC = SAFE.lower()
SERVICE_ID = 227

STAKING = Web3.to_checksum_address("0x9F1936f6afB5EAaA2220032Cf5e265F2Cc9511Cc")
MECH_ACTIVITY = Web3.to_checksum_address("0x1f84F8F70dE0651C2d51Bf8850FE9D0289Ba3B3A")
MECH_MP = Web3.to_checksum_address("0x343F2B005cF6D70bA610CD9F1F1927049414B582")
PRIORITY_MECH = Web3.to_checksum_address("0x45F25db135E83d7a010b05FFc1202F8473E3ae7D")
CTF_EXCHANGE = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
NEG_RISK_CTF = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
USDC = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

KNOWN = {
    STAKING.lower(): "STAKING(polygon_beta_1)",
    MECH_ACTIVITY.lower(): "MECH_ACTIVITY",
    MECH_MP.lower(): "MECH_MARKETPLACE",
    PRIORITY_MECH.lower(): "PRIORITY_MECH",
    CTF_EXCHANGE.lower(): "POLY_CTF_EXCHANGE",
    NEG_RISK_CTF.lower(): "NEG_RISK_CTF",
    USDC.lower(): "USDC(native)",
    USDC_E.lower(): "USDC.e",
    SAFE_LC: "SELF(Safe)",
}

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
POLYMARKET_SUBGRAPH = "https://predict-polymarket-agents.subgraph.autonolas.tech/"
OLAS_MECH_POLYGON = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"

ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY")
POLYGON_RPC = os.getenv("POLYGON_RPC") or "https://polygon-bor-rpc.publicnode.com"

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC, request_kwargs={"timeout": 60}))
print(f"RPC connected: chainId={w3.eth.chain_id}  latest={w3.eth.block_number}",
      file=sys.stderr)


# ---------------------------------------------------------------------------
# Etherscan V2 paging
# ---------------------------------------------------------------------------

def es_paged(action, startblock=0):
    out = []
    page = 1
    while True:
        params = {
            "chainid": 137,
            "module": "account",
            "action": action,
            "address": SAFE,
            "startblock": startblock,
            "endblock": 99999999,
            "page": page,
            "offset": 10000,
            "sort": "asc",
            "apikey": ETHERSCAN_KEY,
        }
        r = requests.get(ETHERSCAN_V2, params=params, timeout=60)
        r.raise_for_status()
        d = r.json()
        if d.get("status") != "1":
            # status=0 with empty result is "No transactions found"
            break
        rows = d.get("result", [])
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 10000:
            break
        page += 1
        time.sleep(0.3)
    return out


# ---------------------------------------------------------------------------
# Subgraph helpers
# ---------------------------------------------------------------------------

def gql(url, query, variables=None):
    r = requests.post(url, json={"query": query, "variables": variables or {}},
                      timeout=60)
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(f"subgraph error {url}: {body['errors']}")
    return body["data"]


def fetch_mech_sender():
    q = """
    query Sender($id: ID!) {
      sender(id: $id) {
        id totalMarketplaceRequests
        requests(first: 1000, orderBy: blockTimestamp, orderDirection: asc) {
          id blockTimestamp transactionHash isDelivered
          parsedRequest { tool }
        }
      }
    }"""
    return gql(OLAS_MECH_POLYGON, q, {"id": SAFE_LC}).get("sender")


def fetch_trader_agent():
    q = """
    query Trader($id: ID!) {
      traderAgent(id: $id) {
        id serviceId totalBets totalPayout totalTraded totalTradedSettled
      }
    }"""
    return gql(POLYMARKET_SUBGRAPH, q, {"id": SAFE_LC}).get("traderAgent")


# ---------------------------------------------------------------------------
# On-chain reads
# ---------------------------------------------------------------------------

STAKING_ABI = [
    {"name": "livenessPeriod", "inputs": [], "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"name": "livenessRatio", "inputs": [], "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"name": "tsCheckpoint", "inputs": [], "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"name": "activityChecker", "inputs": [], "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"name": "getServiceInfo", "inputs": [{"name": "serviceId", "type": "uint256"}],
     "outputs": [{"components": [
         {"name": "multisig", "type": "address"},
         {"name": "owner", "type": "address"},
         {"name": "nonces", "type": "uint256[]"},
         {"name": "tsStart", "type": "uint256"},
         {"name": "inactivity", "type": "uint256[]"},
     ], "name": "sInfo", "type": "tuple"}],
     "stateMutability": "view", "type": "function"},
    {"name": "mapServiceInfo", "inputs": [{"name": "", "type": "uint256"}],
     "outputs": [
         {"name": "multisig", "type": "address"},
         {"name": "owner", "type": "address"},
         {"name": "reward", "type": "uint256"},
         {"name": "tsStart", "type": "uint256"},
     ], "stateMutability": "view", "type": "function"},
]

ACTIVITY_ABI = [
    {"name": "livenessRatio", "inputs": [], "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"name": "getMultisigNonces", "inputs": [{"name": "multisig", "type": "address"}],
     "outputs": [{"type": "uint256[]"}], "stateMutability": "view", "type": "function"},
]

ERC20_ABI = [
    {"name": "balanceOf", "inputs": [{"type": "address"}],
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"name": "allowance", "inputs": [{"type": "address"}, {"type": "address"}],
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"name": "decimals", "inputs": [],
     "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"name": "symbol", "inputs": [],
     "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
]


def try_call(contract, fn_name, *args, default=None):
    try:
        return getattr(contract.functions, fn_name)(*args).call()
    except Exception as exc:
        print(f"    call {fn_name}({args}) failed: {exc}", file=sys.stderr)
        return default


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(dst):
    if not dst:
        return "none"
    dst = dst.lower()
    if dst in KNOWN:
        return KNOWN[dst]
    return dst


def main():
    print(f"=== Polygon service 227 — Safe {SAFE} ===")
    print(f"RPC: {POLYGON_RPC}  chainId={w3.eth.chain_id}")
    print(f"Now: {datetime.now(tz=timezone.utc).isoformat()}")

    # 1. Enumerate txs from Polygonscan
    print("\n[1] Polygonscan enumeration")
    print("-" * 78)
    normal = es_paged("txlist")
    internal = es_paged("txlistinternal")
    tokens = es_paged("tokentx")
    print(f"  normal txs:     {len(normal)}")
    print(f"  internal txs:   {len(internal)}")
    print(f"  token xfers:    {len(tokens)}")

    # Classify outbound normal txs
    # For a Safe, EOA-signed execTransaction lands as the outer `to=Safe`.
    # The effective destination is the inner call (`internal` list with from=Safe).
    # We classify by internal-call `to` (from=Safe) for the operational target.
    out_internal = [t for t in internal if t.get("from", "").lower() == SAFE_LC
                    and t.get("isError", "0") == "0"]
    print(f"  outbound internal (from Safe, success): {len(out_internal)}")

    cat_counts = Counter()
    cat_first = {}
    cat_last = {}
    for t in out_internal:
        dst = (t.get("to") or "").lower()
        cat = KNOWN.get(dst, "other")
        cat_counts[cat] += 1
        ts = int(t.get("timeStamp", "0") or "0")
        cat_first.setdefault(cat, ts)
        cat_last[cat] = ts

    # Also classify token transfers from the Safe
    token_out = defaultdict(lambda: defaultdict(int))
    token_meta = {}
    for t in tokens:
        if t.get("from", "").lower() != SAFE_LC:
            continue
        tok = (t.get("contractAddress") or "").lower()
        token_meta[tok] = (t.get("tokenSymbol", "?"),
                           int(t.get("tokenDecimal", "18") or "18"))
        v = int(t.get("value", "0") or "0")
        dst = (t.get("to") or "").lower()
        token_out[tok][dst] += v
    token_in = defaultdict(lambda: defaultdict(int))
    for t in tokens:
        if t.get("to", "").lower() != SAFE_LC:
            continue
        tok = (t.get("contractAddress") or "").lower()
        token_meta.setdefault(
            tok, (t.get("tokenSymbol", "?"),
                  int(t.get("tokenDecimal", "18") or "18")))
        v = int(t.get("value", "0") or "0")
        src = (t.get("from") or "").lower()
        token_in[tok][src] += v

    # Also consider normal txs where the outer signer touched exchanges (direct
    # EOA calls wouldn't apply for a Safe, but include for completeness)
    approval_events = []  # from logs via tokentx isn't enough; we'll infer from
    # calls classified below

    print("\n[2] Outbound internal-call classification (from Safe)")
    print("-" * 78)
    print(f"  {'category':<28}  count    first                       last")
    for cat, cnt in cat_counts.most_common():
        f = datetime.fromtimestamp(cat_first[cat], tz=timezone.utc
                                   ).strftime("%Y-%m-%d %H:%M:%S") if cat_first.get(cat) else "-"
        l = datetime.fromtimestamp(cat_last[cat], tz=timezone.utc
                                   ).strftime("%Y-%m-%d %H:%M:%S") if cat_last.get(cat) else "-"
        print(f"  {cat:<28}  {cnt:>5}    {f}   {l}")

    # 3. Normal txs: classify by outer `to` (only for completeness / direct EOA)
    outer_to = Counter()
    for t in normal:
        if t.get("from", "").lower() == SAFE_LC:
            outer_to[(t.get("to") or "").lower()] += 1
    print("\n[3] Normal txs with Safe as signer (outer from=Safe) — direct EOA calls")
    print("-" * 78)
    if not outer_to:
        print("  (none — Safe cannot sign EOA txs; confirms all outbound goes via owners' execTransaction)")
    else:
        for dst, cnt in outer_to.most_common():
            print(f"  {KNOWN.get(dst, dst)}  {cnt}")

    # Normal txs where `to` is the Safe (execTransaction calls)
    exec_txs = [t for t in normal if t.get("to", "").lower() == SAFE_LC]
    print(f"\n  execTransaction calls hitting the Safe: {len(exec_txs)}")
    print(f"  outbound-internal successes:            {len(out_internal)}")

    # 4. ERC20 outflow from the Safe
    print("\n[4] ERC20 outflows from Safe (any token)")
    print("-" * 78)
    if not token_out:
        print("  (none)")
    for tok, dsts in token_out.items():
        sym, dec = token_meta[tok]
        total = sum(dsts.values())
        print(f"  {sym} ({tok}): total out {total / 10 ** dec:,.6f}")
        for dst, v in sorted(dsts.items(), key=lambda kv: -kv[1])[:10]:
            print(f"      -> {KNOWN.get(dst, dst)}: {v / 10 ** dec:,.6f}")

    print("\n[5] ERC20 inflows to Safe (funding)")
    print("-" * 78)
    if not token_in:
        print("  (none)")
    for tok, srcs in token_in.items():
        sym, dec = token_meta[tok]
        total = sum(srcs.values())
        print(f"  {sym} ({tok}): total in {total / 10 ** dec:,.6f}")
        for src, v in sorted(srcs.items(), key=lambda kv: -kv[1])[:5]:
            print(f"      <- {KNOWN.get(src, src)}: {v / 10 ** dec:,.6f}")

    # 6. Direct check: any internal call with to=CTF_EXCHANGE or NEG_RISK_CTF?
    print("\n[6] Polymarket CTF / Neg-Risk exchange interactions")
    print("-" * 78)
    ctf_hits = [t for t in out_internal if (t.get("to") or "").lower()
                in (CTF_EXCHANGE.lower(), NEG_RISK_CTF.lower())]
    # Also check any tx (normal or internal, any direction) for these addrs in case
    # matchOrders was called with Safe as maker and exchange is caller
    all_ctf_touch = [t for t in normal if
                     (t.get("to") or "").lower() in (CTF_EXCHANGE.lower(),
                                                     NEG_RISK_CTF.lower())
                     or (t.get("from") or "").lower() in (CTF_EXCHANGE.lower(),
                                                          NEG_RISK_CTF.lower())]
    print(f"  outbound internal calls Safe -> CTF/NegRisk: {len(ctf_hits)}")
    print(f"  any normal tx touching CTF/NegRisk involving Safe (either dir): "
          f"{len(all_ctf_touch)}")
    if ctf_hits:
        for t in ctf_hits[:10]:
            print(f"    {t.get('hash')}  {t.get('timeStamp')}  to={t.get('to')}")

    # 7. Mech request count (internal calls Safe -> MECH_MP or PRIORITY_MECH)
    print("\n[7] Mech requests via marketplace / priority mech")
    print("-" * 78)
    mech_mp_calls = [t for t in out_internal
                     if (t.get("to") or "").lower() == MECH_MP.lower()]
    prio_calls = [t for t in out_internal
                  if (t.get("to") or "").lower() == PRIORITY_MECH.lower()]
    print(f"  internal calls Safe -> MECH_MARKETPLACE: {len(mech_mp_calls)}")
    print(f"  internal calls Safe -> PRIORITY_MECH:    {len(prio_calls)}")
    if mech_mp_calls:
        first_ts = int(mech_mp_calls[0]["timeStamp"])
        last_ts = int(mech_mp_calls[-1]["timeStamp"])
        print(f"  first mech mp call: {datetime.fromtimestamp(first_ts, tz=timezone.utc)}")
        print(f"  last mech mp call:  {datetime.fromtimestamp(last_ts, tz=timezone.utc)}")

    # Cross-check with Olas subgraph
    print("\n  Olas mech-marketplace Polygon subgraph:")
    sender = fetch_mech_sender()
    if sender is None:
        print("    sender entity NOT FOUND")
        sg_total = 0
    else:
        sg_total = int(sender.get("totalMarketplaceRequests") or 0)
        reqs = sender.get("requests") or []
        print(f"    totalMarketplaceRequests: {sg_total}")
        print(f"    requests window:          {len(reqs)}")
        if reqs:
            print(f"    first: {datetime.fromtimestamp(int(reqs[0]['blockTimestamp']), tz=timezone.utc)}  {reqs[0]['transactionHash']}")
            print(f"    last:  {datetime.fromtimestamp(int(reqs[-1]['blockTimestamp']), tz=timezone.utc)}  {reqs[-1]['transactionHash']}")
            tools = Counter((r.get("parsedRequest") or {}).get("tool") or "<none>"
                            for r in reqs)
            for t, n in tools.most_common():
                print(f"      tool {t}: {n}")

    # 8. Polymarket bets subgraph
    print("\n[8] Polymarket bets subgraph — traderAgent row")
    print("-" * 78)
    trader = fetch_trader_agent()
    if trader is None:
        print("  traderAgent entity NOT FOUND (subgraph has never seen a bet)")
    else:
        for k, v in trader.items():
            print(f"  {k}: {v}")

    # 9. Staking KPI
    print("\n[9] Staking KPI read (polygon_beta_1 staking)")
    print("-" * 78)
    staking = w3.eth.contract(address=STAKING, abi=STAKING_ABI)
    activity = w3.eth.contract(address=MECH_ACTIVITY, abi=ACTIVITY_ABI)

    liveness_period = try_call(staking, "livenessPeriod")
    liveness_ratio = try_call(staking, "livenessRatio")
    if liveness_ratio is None:
        liveness_ratio = try_call(activity, "livenessRatio")
    ts_checkpoint = try_call(staking, "tsCheckpoint")
    ac_from_staking = try_call(staking, "activityChecker")

    print(f"  activityChecker (from staking): {ac_from_staking}")
    print(f"  livenessPeriod:  {liveness_period}  s  "
          f"({(liveness_period / 86400) if liveness_period else '?':.4f} d)")
    print(f"  livenessRatio:   {liveness_ratio}  (requests/sec * 1e18)")
    print(f"  tsCheckpoint:    {ts_checkpoint}  "
          f"({datetime.fromtimestamp(ts_checkpoint, tz=timezone.utc) if ts_checkpoint else '?'})")

    # Get service info
    sinfo = try_call(staking, "getServiceInfo", SERVICE_ID)
    print(f"  getServiceInfo({SERVICE_ID}): {sinfo}")
    prev_nonces = None
    ts_start = None
    if sinfo is not None:
        # tuple: (multisig, owner, nonces[], tsStart, inactivity[])
        try:
            multisig, owner, nonces, ts_start, inactivity = sinfo
            prev_nonces = list(nonces)
            print(f"    multisig: {multisig}")
            print(f"    owner:    {owner}")
            print(f"    nonces at last checkpoint: {prev_nonces}")
            print(f"    tsStart:  {ts_start} "
                  f"({datetime.fromtimestamp(ts_start, tz=timezone.utc) if ts_start else '?'})")
            print(f"    inactivity: {list(inactivity)}")
        except Exception as e:
            print(f"    (failed to unpack getServiceInfo: {e})")

    # Current multisig nonces per activity checker
    cur_nonces = try_call(activity, "getMultisigNonces", SAFE)
    print(f"  getMultisigNonces(Safe) now: {cur_nonces}")

    if (liveness_ratio and ts_checkpoint and cur_nonces
            and prev_nonces is not None):
        now_ts = int(time.time())
        elapsed = now_ts - int(ts_checkpoint)
        required = liveness_ratio * elapsed // (10 ** 18)
        delta = int(cur_nonces[0]) - int(prev_nonces[0]) if prev_nonces else int(cur_nonces[0])
        print(f"\n  Epoch analysis:")
        print(f"    now - tsCheckpoint:        {elapsed} s  ({elapsed / 3600:.2f} h)")
        print(f"    required requests (ratio): {required}")
        print(f"    nonce delta since ckpt:    {delta}   (cur {cur_nonces[0]} vs prev {prev_nonces[0] if prev_nonces else '?'})")
        print(f"    KPI met THIS EPOCH?        {'YES' if delta >= required else 'NO'}")
        # Also min-window check (typical staking contracts require delta over
        # livenessPeriod, not elapsed — report both interpretations)
        if liveness_period:
            required_period = liveness_ratio * liveness_period // (10 ** 18)
            print(f"    required per livenessPeriod: {required_period}")
            print(f"    KPI (livenessPeriod basis)?  "
                  f"{'YES' if delta >= required_period else 'NO'}")

    # 10. Balances and allowances
    print("\n[10] Balances & allowances")
    print("-" * 78)
    pol_bal = w3.eth.get_balance(SAFE)
    print(f"  POL (native):   {pol_bal / 10 ** 18:,.6f}")
    for tok_addr, label in [(USDC, "USDC (native)"), (USDC_E, "USDC.e (bridged)")]:
        c = w3.eth.contract(address=tok_addr, abi=ERC20_ABI)
        bal = try_call(c, "balanceOf", SAFE, default=0)
        dec = try_call(c, "decimals", default=6)
        sym = try_call(c, "symbol", default="?")
        print(f"  {label}: balance={bal / 10 ** dec:,.6f} {sym}  "
              f"(raw={bal}, decimals={dec})")
        for spender, slabel in [(CTF_EXCHANGE, "CTF_EXCHANGE"),
                                (NEG_RISK_CTF, "NEG_RISK_CTF")]:
            allowance = try_call(c, "allowance", SAFE, spender, default=0)
            print(f"    allowance -> {slabel}: "
                  f"{allowance / 10 ** dec if allowance < 10 ** 60 else 'MAX-ish'}  "
                  f"(raw={allowance})")

    # 11. Verdict
    print("\n[11] VERDICT")
    print("=" * 78)
    mech_mp_count = len(mech_mp_calls) + len(prio_calls)
    print(f"  Outbound internal success txs total:        {len(out_internal)}")
    print(f"  Mech calls (marketplace + priority mech):   {mech_mp_count}")
    print(f"  Olas subgraph totalMarketplaceRequests:     {sg_total}")
    print(f"  CTF / NegRisk exchange calls:               {len(ctf_hits)}  "
          f"(any direction: {len(all_ctf_touch)})")
    print(f"  traderAgent.totalBets (Polymarket subgraph): "
          f"{trader.get('totalBets') if trader else 'ENTITY MISSING'}")
    print()
    print("  -> Any Polymarket bet ever placed?  "
          f"{'YES' if ctf_hits or (trader and int(trader.get('totalBets') or 0) > 0) else 'NO'}")


if __name__ == "__main__":
    main()
