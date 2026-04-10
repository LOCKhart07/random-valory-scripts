"""
ZD#899 — Verify on-chain status of 6 "unredeemed but Traded" Omen markets
for a Pearl trader Safe whose UI shows them as still 'Traded' two weeks
after the markets supposedly closed.

This is the FOLLOW-UP to ZD#919 (verify_invalid_markets_zd919.py). Different
Safe, different markets, different symptoms:

  - ZD#919 markets were pending (24h dispute window not elapsed) and the UI
    mislabelled them as 'invalid'.
  - ZD#899 markets closed ~Mar 23-24 2026 (2+ weeks ago, today is 2026-04-10)
    and the UI labels them as 'Traded' (not Won/Lost). Agent IS reaching
    redeem_round per logs.

The question this script answers: are these markets actually invalid
(Reality answer == 0xff..ff), or are they normally finalized markets that
should have been redeemed by now? And is the Safe still holding the CTs?

For each FPMM:
  1. Read FPMM.conditionalTokens, FPMM.collateralToken, FPMM.conditionIds(0)
  2. Recover the Reality.eth questionId via the ConditionPreparation event
  3. Read Reality.eth: bestAnswer, finalAnswer, isFinalized, finalizeTS, bond
  4. Read CT: payoutNumerators[0..1], payoutDenominator
  5. Read Safe's CT balanceOf for both Yes and No outcomes
  6. Compute redeemable XDAI right now via the standard CT formula
  7. Scan PayoutRedemption events on the CT to see whether the Safe (or
     anyone else) has already redeemed this condition.

Usage:
    poetry run python omen/verify_unredeemed_markets_zd899.py
"""

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import requests
from dotenv import load_dotenv
from web3 import Web3
from web3._utils.events import get_event_data

load_dotenv()

GNOSIS_RPC = os.getenv("GNOSIS_RPC")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAFE = Web3.to_checksum_address("0xC45CCA9d465Efc6883D07EC4B5a8eE25e1519570")
AGENT_EOA = Web3.to_checksum_address("0x884dC58a032b6d60C1cD48D120aD668fe7e2F4D0")

CONDITIONAL_TOKENS = Web3.to_checksum_address("0xCeAfDD6bc0bEF976fdCd1112955828E00543c0Ce")
# Reality.eth v3 — what Omen's oracle proxy (0xAB16D643...) currently points at
# on Gnosis. Verified in the ZD#919 investigation via oracle.realitio().
REALITIO = Web3.to_checksum_address("0x79e32aE03fb27B07C89c0c568F80287C01ca2E57")
WXDAI = Web3.to_checksum_address("0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d")

OMEN_SUBGRAPH = "https://api.subgraph.staging.autonolas.tech/api/proxy/predict-omen"

INVALID_ANSWER = "0x" + "ff" * 32
ZERO_BYTES32 = b"\x00" * 32
WEI = 10 ** 18

# NOTE on inputs: the addresses pulled verbatim from the Zendesk ticket #899
# ("unredeemed bet addresses observed in logs") turned out to be the agent's
# CURRENT working set (open positions on April-close markets) — NOT the markets
# the user is actually complaining about. The user's screenshot (OCR) shows
# three "Traded" entries with March 23-24 close dates in the titles. By
# pulling all bets for the user's Safe from the predict-omen subgraph
# (orderBy timestamp desc, bettor=safe) and grepping for the OCR titles, the
# real stuck markets are:
#
#   1. Cuban government water rationing — close March 24 2026 — No 0.025 xDAI
#      FPMM 0xeaeb8b57df00d0f7e39c38ffa0147e84622a0014
#   2. Nvidia commercial space partners — close March 23 2026 — No 2.000 xDAI
#      FPMM 0x7c4179e1f30f8c638168a711d3401b9d2e3c2035
#   3. Cloud provider API credentials — close April 7 2026 — No 1.335 xDAI
#      FPMM 0x536d848930e734f9a2442f1e8e8b2685ffdd2f08
#
# All three have Reality currentAnswer == 0x01 (No), matching the user's bet
# direction in the screenshot — so they should all have WON.
#
# Markets 1 & 2 closed ~17-18 days ago and ARE the actual financial loss
# the user is reporting. Market 3 closed ~3 days ago and is the same code
# path triggered more recently.
MARKETS = [
    "0xeaeb8b57df00d0f7e39c38ffa0147e84622a0014",  # Cuban water March 24
    "0x7c4179e1f30f8c638168a711d3401b9d2e3c2035",  # Nvidia space March 23
    "0x536d848930e734f9a2442f1e8e8b2685ffdd2f08",  # Cloud API April 7
]

# ---------------------------------------------------------------------------
# ABIs (minimal)
# ---------------------------------------------------------------------------

FPMM_ABI = [
    {"inputs": [], "name": "conditionalTokens", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "collateralToken", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}], "name": "conditionIds",
     "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
]

CT_ABI = [
    {"inputs": [{"type": "bytes32"}, {"type": "bytes32"}, {"type": "uint256"}],
     "name": "getCollectionId", "outputs": [{"type": "bytes32"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}, {"type": "bytes32"}], "name": "getPositionId",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}, {"type": "uint256"}], "name": "balanceOf",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}, {"type": "uint256"}], "name": "payoutNumerators",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "payoutDenominator",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"anonymous": False, "inputs": [
        {"indexed": True, "name": "conditionId", "type": "bytes32"},
        {"indexed": True, "name": "oracle", "type": "address"},
        {"indexed": True, "name": "questionId", "type": "bytes32"},
        {"indexed": False, "name": "outcomeSlotCount", "type": "uint256"},
    ], "name": "ConditionPreparation", "type": "event"},
    {"anonymous": False, "inputs": [
        {"indexed": True, "name": "conditionId", "type": "bytes32"},
        {"indexed": True, "name": "oracle", "type": "address"},
        {"indexed": True, "name": "questionId", "type": "bytes32"},
        {"indexed": False, "name": "outcomeSlotCount", "type": "uint256"},
        {"indexed": False, "name": "payoutNumerators", "type": "uint256[]"},
    ], "name": "ConditionResolution", "type": "event"},
    {"anonymous": False, "inputs": [
        {"indexed": True, "name": "redeemer", "type": "address"},
        {"indexed": True, "name": "collateralToken", "type": "address"},
        {"indexed": True, "name": "parentCollectionId", "type": "bytes32"},
        {"indexed": False, "name": "conditionId", "type": "bytes32"},
        {"indexed": False, "name": "indexSets", "type": "uint256[]"},
        {"indexed": False, "name": "payout", "type": "uint256"},
    ], "name": "PayoutRedemption", "type": "event"},
]

REALITIO_ABI = [
    {"inputs": [{"type": "bytes32"}], "name": "getFinalAnswer",
     "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getBestAnswer",
     "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "isFinalized",
     "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getFinalizeTS",
     "outputs": [{"type": "uint32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getBond",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getOpeningTS",
     "outputs": [{"type": "uint32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getTimeout",
     "outputs": [{"type": "uint32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getArbitrator",
     "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def post_subgraph(url: str, query: str, variables: Optional[dict] = None) -> Optional[dict]:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    for attempt in range(4):
        try:
            r = requests.post(url, json=payload, timeout=60,
                              headers={"Content-Type": "application/json"})
            r.raise_for_status()
            d = r.json()
            if "errors" in d:
                print(f"  SUBGRAPH ERROR: {d['errors']}")
                return None
            return d.get("data")
        except Exception as e:
            if attempt == 3:
                print(f"  subgraph failed: {e}")
                return None
            time.sleep(3 * 2 ** attempt)


def chunked_get_logs(w3: Web3, params: dict, chunk: int = 400_000) -> list:
    """Paginate eth_getLogs across large block ranges."""
    start = params["fromBlock"]
    end = params["toBlock"]
    out = []
    cur = start
    while cur <= end:
        nxt = min(cur + chunk - 1, end)
        local = dict(params)
        local["fromBlock"] = cur
        local["toBlock"] = nxt
        try:
            out.extend(w3.eth.get_logs(local))
        except Exception:
            if chunk > 10_000:
                sub_chunk = chunk // 4
                sub_cur = cur
                while sub_cur <= nxt:
                    sub_nxt = min(sub_cur + sub_chunk - 1, nxt)
                    sp = dict(params)
                    sp["fromBlock"] = sub_cur
                    sp["toBlock"] = sub_nxt
                    out.extend(w3.eth.get_logs(sp))
                    sub_cur = sub_nxt + 1
            else:
                raise
        cur = nxt + 1
    return out


# ---------------------------------------------------------------------------
# Per-market analysis
# ---------------------------------------------------------------------------

@dataclass
class MarketReport:
    address: str
    title: str = ""
    condition_id: str = ""
    collateral: str = ""
    question_id: str = ""
    oracle: str = ""
    reality_best_answer: str = ""
    reality_final_answer: str = ""
    reality_finalized: bool = False
    reality_finalize_ts: int = 0
    reality_bond: float = 0.0
    reality_opening_ts: int = 0
    reality_timeout: int = 0
    reality_arbitrator: str = ""
    payout_numerators: List[int] = field(default_factory=list)
    payout_denominator: int = 0
    safe_yes_balance: int = 0
    safe_no_balance: int = 0
    redeemable_wei: int = 0
    safe_already_redeemed: bool = False
    safe_redemption_records: List[dict] = field(default_factory=list)
    other_redemptions: List[dict] = field(default_factory=list)
    prep_block: int = 0
    resolution_block: int = 0
    errors: List[str] = field(default_factory=list)


def recover_question_and_oracle(w3: Web3, condition_id_bytes: bytes,
                                latest_block: int) -> tuple:
    """Fetch ConditionPreparation event for the given conditionId, returning
    (question_id_hex, oracle, prep_block)."""
    event_abi = next(a for a in CT_ABI if a.get("name") == "ConditionPreparation" and a.get("type") == "event")
    sig = w3.keccak(text="ConditionPreparation(bytes32,address,bytes32,uint256)")
    sig_hex = "0x" + sig.hex().lstrip("0x")
    topic1 = "0x" + condition_id_bytes.hex()

    step = 500_000
    cur_end = latest_block
    min_block = max(latest_block - 400 * 17280, 1)
    while cur_end >= min_block:
        cur_start = max(cur_end - step + 1, min_block)
        try:
            logs = w3.eth.get_logs({
                "fromBlock": cur_start,
                "toBlock": cur_end,
                "address": CONDITIONAL_TOKENS,
                "topics": [sig_hex, topic1],
            })
        except Exception:
            sub = step // 5
            logs = []
            c = cur_start
            while c <= cur_end:
                cn = min(c + sub - 1, cur_end)
                logs.extend(w3.eth.get_logs({
                    "fromBlock": c, "toBlock": cn,
                    "address": CONDITIONAL_TOKENS,
                    "topics": [sig_hex, topic1],
                }))
                c = cn + 1
        if logs:
            ev = get_event_data(w3.codec, event_abi, logs[0])
            return (
                "0x" + ev["args"]["questionId"].hex(),
                Web3.to_checksum_address(ev["args"]["oracle"]),
                ev["blockNumber"],
            )
        cur_end = cur_start - 1
    return ("", "", 0)


def find_resolution_block(w3: Web3, condition_id_bytes: bytes, latest_block: int) -> int:
    """Find the ConditionResolution event for a condition. Returns block or 0."""
    sig = w3.keccak(text="ConditionResolution(bytes32,address,bytes32,uint256,uint256[])")
    sig_hex = "0x" + sig.hex().lstrip("0x")
    topic1 = "0x" + condition_id_bytes.hex()
    step = 500_000
    cur_end = latest_block
    min_block = max(latest_block - 400 * 17280, 1)
    while cur_end >= min_block:
        cur_start = max(cur_end - step + 1, min_block)
        try:
            logs = w3.eth.get_logs({
                "fromBlock": cur_start,
                "toBlock": cur_end,
                "address": CONDITIONAL_TOKENS,
                "topics": [sig_hex, topic1],
            })
        except Exception:
            sub = step // 5
            logs = []
            c = cur_start
            while c <= cur_end:
                cn = min(c + sub - 1, cur_end)
                logs.extend(w3.eth.get_logs({
                    "fromBlock": c, "toBlock": cn,
                    "address": CONDITIONAL_TOKENS,
                    "topics": [sig_hex, topic1],
                }))
                c = cn + 1
        if logs:
            return logs[0]["blockNumber"]
        cur_end = cur_start - 1
    return 0


def find_redemptions_for_condition(w3: Web3, condition_id_bytes: bytes,
                                   start_block: int, latest_block: int) -> List[dict]:
    """Scan PayoutRedemption events for this condition. Filter by collateral=WXDAI."""
    event_abi = next(a for a in CT_ABI if a.get("name") == "PayoutRedemption" and a.get("type") == "event")
    sig = w3.keccak(text="PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)")
    sig_hex = "0x" + sig.hex().lstrip("0x")
    ct_topic = "0x" + WXDAI.lower()[2:].zfill(64)
    out = []
    if start_block == 0:
        start_block = max(latest_block - 60 * 17280, 1)
    logs = chunked_get_logs(w3, {
        "fromBlock": start_block,
        "toBlock": latest_block,
        "address": CONDITIONAL_TOKENS,
        "topics": [sig_hex, None, ct_topic, None],
    }, chunk=200_000)
    for lg in logs:
        ev = get_event_data(w3.codec, event_abi, lg)
        if ev["args"]["conditionId"] == condition_id_bytes:
            out.append({
                "redeemer": Web3.to_checksum_address(ev["args"]["redeemer"]),
                "payout": int(ev["args"]["payout"]),
                "block": lg["blockNumber"],
                "tx": lg["transactionHash"].hex(),
                "indexSets": list(ev["args"]["indexSets"]),
            })
    return out


def get_market_title_from_subgraph(fpmm: str) -> str:
    """Title fetch is not supported by the predict-omen subgraph schema —
    it does not expose fixedProductMarketMaker(id:). Returning empty string;
    on-chain data is sufficient for the verdict and titles are cosmetic."""
    return ""


def analyze_market(w3: Web3, fpmm_addr: str, latest_block: int,
                   realitio, ct) -> MarketReport:
    addr = Web3.to_checksum_address(fpmm_addr)
    r = MarketReport(address=addr)

    fpmm = w3.eth.contract(address=addr, abi=FPMM_ABI)
    try:
        ct_addr = fpmm.functions.conditionalTokens().call()
        r.collateral = fpmm.functions.collateralToken().call()
        cid = fpmm.functions.conditionIds(0).call()
        r.condition_id = "0x" + cid.hex()
    except Exception as e:
        r.errors.append(f"FPMM read failed (is this really an FPMM?): {e}")
        return r

    if Web3.to_checksum_address(ct_addr) != CONDITIONAL_TOKENS:
        r.errors.append(f"Unexpected CT address: {ct_addr}")

    # Optional title from subgraph
    try:
        r.title = get_market_title_from_subgraph(addr)
    except Exception:
        pass

    # Payouts on CT
    try:
        num0 = ct.functions.payoutNumerators(cid, 0).call()
        num1 = ct.functions.payoutNumerators(cid, 1).call()
        r.payout_numerators = [num0, num1]
        r.payout_denominator = ct.functions.payoutDenominator(cid).call()
    except Exception as e:
        r.errors.append(f"payouts read failed: {e}")

    # Safe balances
    try:
        coll_id_yes = ct.functions.getCollectionId(ZERO_BYTES32, cid, 1).call()
        coll_id_no = ct.functions.getCollectionId(ZERO_BYTES32, cid, 2).call()
        pos_id_yes = ct.functions.getPositionId(Web3.to_checksum_address(r.collateral), coll_id_yes).call()
        pos_id_no = ct.functions.getPositionId(Web3.to_checksum_address(r.collateral), coll_id_no).call()
        r.safe_yes_balance = ct.functions.balanceOf(SAFE, pos_id_yes).call()
        r.safe_no_balance = ct.functions.balanceOf(SAFE, pos_id_no).call()
    except Exception as e:
        r.errors.append(f"balanceOf failed: {e}")

    if r.payout_denominator > 0:
        r.redeemable_wei = (r.safe_yes_balance * r.payout_numerators[0] +
                            r.safe_no_balance * r.payout_numerators[1]) // r.payout_denominator

    # ConditionPreparation -> questionId, oracle
    try:
        qid, oracle, prep_block = recover_question_and_oracle(w3, cid, latest_block)
        r.question_id = qid
        r.oracle = oracle
        r.prep_block = prep_block
    except Exception as e:
        r.errors.append(f"ConditionPreparation lookup failed: {e}")

    # Reality.eth state
    if r.question_id:
        try:
            qid_bytes = bytes.fromhex(r.question_id[2:])
            r.reality_best_answer = "0x" + realitio.functions.getBestAnswer(qid_bytes).call().hex()
            try:
                r.reality_final_answer = "0x" + realitio.functions.getFinalAnswer(qid_bytes).call().hex()
            except Exception:
                r.reality_final_answer = "(reverted — not finalized)"
            r.reality_finalized = realitio.functions.isFinalized(qid_bytes).call()
            r.reality_finalize_ts = realitio.functions.getFinalizeTS(qid_bytes).call()
            r.reality_bond = realitio.functions.getBond(qid_bytes).call() / WEI
            r.reality_opening_ts = realitio.functions.getOpeningTS(qid_bytes).call()
            r.reality_timeout = realitio.functions.getTimeout(qid_bytes).call()
            r.reality_arbitrator = realitio.functions.getArbitrator(qid_bytes).call()
        except Exception as e:
            r.errors.append(f"Reality.eth read failed: {e}")

    # Resolution block
    try:
        r.resolution_block = find_resolution_block(w3, cid, latest_block)
    except Exception as e:
        r.errors.append(f"ConditionResolution lookup failed: {e}")

    # Redemption events on this condition (any redeemer)
    try:
        all_reds = find_redemptions_for_condition(
            w3, cid, r.resolution_block, latest_block)
        for rec in all_reds:
            if rec["redeemer"] == SAFE:
                r.safe_redemption_records.append(rec)
            else:
                r.other_redemptions.append(rec)
        r.safe_already_redeemed = len(r.safe_redemption_records) > 0
    except Exception as e:
        r.errors.append(f"PayoutRedemption scan failed: {e}")

    return r


def fmt_payouts(nums: List[int], denom: int) -> str:
    if not nums or denom == 0:
        return "unreported"
    return f"[{','.join(str(n) for n in nums)}] / {denom}"


def is_invalid_resolution(nums: List[int], denom: int) -> bool:
    return (len(nums) == 2 and denom > 0 and nums[0] == nums[1] and nums[0] > 0)


def interpret_reality_answer(best: str) -> str:
    if not best or best == "0x":
        return "unknown"
    if best.lower() == INVALID_ANSWER:
        return "Invalid (0xff..ff)"
    val = int(best, 16)
    if val == 0:
        return "Yes (outcome 0)"
    if val == 1:
        return "No (outcome 1)"
    return f"raw={best}"


def main():
    assert GNOSIS_RPC, "GNOSIS_RPC env var missing"
    w3 = Web3(Web3.HTTPProvider(GNOSIS_RPC))
    assert w3.is_connected(), "RPC not connected"
    latest = w3.eth.block_number
    now_ts = w3.eth.get_block(latest)["timestamp"]
    now_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()
    print(f"ZD#899 — Verify unredeemed Omen markets for Pearl trader Safe")
    print(f"Connected to Gnosis. Latest block: {latest}  ({now_iso})")
    print(f"Safe:      {SAFE}")
    print(f"Agent EOA: {AGENT_EOA}")
    print(f"CT:        {CONDITIONAL_TOKENS}")
    print(f"RealitIO:  {REALITIO}")
    print()

    ct = w3.eth.contract(address=CONDITIONAL_TOKENS, abi=CT_ABI)
    realitio = w3.eth.contract(address=REALITIO, abi=REALITIO_ABI)

    reports: List[MarketReport] = []
    for addr in MARKETS:
        print(f"--- Analyzing {addr} ---")
        try:
            r = analyze_market(w3, addr, latest, realitio, ct)
        except Exception as e:
            r = MarketReport(address=addr, errors=[str(e)])
        reports.append(r)
        print(f"  title={r.title[:80] if r.title else '(no title)'}")
        print(f"  condId={r.condition_id}")
        print(f"  payouts={fmt_payouts(r.payout_numerators, r.payout_denominator)}")
        print(f"  reality bestAnswer={r.reality_best_answer}  finalized={r.reality_finalized}")
        print(f"  safe Yes={r.safe_yes_balance/WEI:.4f}  No={r.safe_no_balance/WEI:.4f}")
        print(f"  redeemable={r.redeemable_wei/WEI:.4f} XDAI")
        print(f"  safe already redeemed: {r.safe_already_redeemed}  ({len(r.safe_redemption_records)} record(s))")
        print(f"  other redemptions seen: {len(r.other_redemptions)}")
        if r.errors:
            for err in r.errors:
                print(f"  ERROR: {err}")
        print()

    # Detailed report
    print("=" * 80)
    print("DETAILED REPORT")
    print("=" * 80)

    for r in reports:
        print()
        print(f"FPMM {r.address}")
        if r.title:
            print(f"  Title:                {r.title[:100]}")
        print(f"  conditionId:          {r.condition_id}")
        print(f"  questionId:           {r.question_id or '(not recovered)'}")
        print(f"  oracle:               {r.oracle}")
        if r.reality_opening_ts:
            opening_iso = datetime.fromtimestamp(r.reality_opening_ts, tz=timezone.utc).isoformat()
            print(f"  Reality openingTS:    {opening_iso}")
        if r.reality_finalize_ts:
            fin_iso = datetime.fromtimestamp(r.reality_finalize_ts, tz=timezone.utc).isoformat()
            delta_h = (now_ts - r.reality_finalize_ts) / 3600
            print(f"  Reality finalizeTS:   {fin_iso}  ({delta_h:+.1f}h relative to now)")
        print(f"  Reality bestAnswer:   {r.reality_best_answer}  ({interpret_reality_answer(r.reality_best_answer)})")
        print(f"  Reality finalAnswer:  {r.reality_final_answer}")
        print(f"  Reality finalized:    {r.reality_finalized}")
        print(f"  Reality bond:         {r.reality_bond:.4f} xDAI")
        print(f"  Reality timeout:      {r.reality_timeout} s ({r.reality_timeout/3600:.1f} h)")
        print(f"  Reality arbitrator:   {r.reality_arbitrator}")
        is_invalid_reality = (r.reality_best_answer == INVALID_ANSWER)
        is_invalid_resol = is_invalid_resolution(r.payout_numerators, r.payout_denominator)
        print(f"  => Reality INVALID:   {is_invalid_reality}")
        print(f"  payoutNumerators:     {fmt_payouts(r.payout_numerators, r.payout_denominator)}")
        print(f"  => CT invalid (50/50):{is_invalid_resol}")
        print(f"  Safe Yes balance:     {r.safe_yes_balance/WEI:.6f} cTokens")
        print(f"  Safe No balance:      {r.safe_no_balance/WEI:.6f} cTokens")
        print(f"  Redeemable now:       {r.redeemable_wei/WEI:.6f} XDAI")
        print(f"  Safe already redeemed:{r.safe_already_redeemed}")
        if r.safe_redemption_records:
            for rec in r.safe_redemption_records:
                print(f"    -> Safe redeemed {rec['payout']/WEI:.4f} XDAI at block {rec['block']} tx={rec['tx']}")
        if r.other_redemptions:
            others_total = sum(x["payout"] for x in r.other_redemptions) / WEI
            distinct = {x["redeemer"].lower() for x in r.other_redemptions}
            print(f"  Other redeemers:      {len(distinct)} addresses, total {others_total:.4f} XDAI")
            for x in r.other_redemptions[:3]:
                print(f"    - {x['redeemer']} payout={x['payout']/WEI:.4f} block={x['block']}")
        if r.errors:
            for err in r.errors:
                print(f"  !! {err}")

    # Summary table
    print()
    print("=" * 80)
    print("SUMMARY TABLE")
    print("=" * 80)
    print(f"{'FPMM':<14}{'Final?':<8}{'Answer':<22}{'Payouts':<14}{'Yes':>10}{'No':>10}{'Redeem':>10}  Status")
    for r in reports:
        ans = interpret_reality_answer(r.reality_best_answer)[:20]
        payouts = fmt_payouts(r.payout_numerators, r.payout_denominator)[:12]
        if r.safe_already_redeemed:
            status = "REDEEMED"
        elif r.safe_yes_balance == 0 and r.safe_no_balance == 0:
            status = "no CTs held"
        elif r.payout_denominator == 0:
            status = "CT NOT REPORTED"
        elif r.redeemable_wei > 0:
            status = "REDEEMABLE NOW"
        else:
            status = "lost (zero payout)"
        print(f"{r.address[:12]:<14}"
              f"{str(r.reality_finalized):<8}"
              f"{ans:<22}"
              f"{payouts:<14}"
              f"{r.safe_yes_balance/WEI:>10.3f}"
              f"{r.safe_no_balance/WEI:>10.3f}"
              f"{r.redeemable_wei/WEI:>10.4f}  {status}")

    # Verdicts
    n = len(reports)
    n_invalid_reality = sum(1 for r in reports if r.reality_best_answer == INVALID_ANSWER)
    n_invalid_ct = sum(1 for r in reports if is_invalid_resolution(r.payout_numerators, r.payout_denominator))
    n_finalized = sum(1 for r in reports if r.reality_finalized)
    n_ct_reported = sum(1 for r in reports if r.payout_denominator > 0)
    n_safe_redeemed = sum(1 for r in reports if r.safe_already_redeemed)
    n_safe_holds_cts = sum(1 for r in reports if r.safe_yes_balance or r.safe_no_balance)
    total_redeemable = sum(r.redeemable_wei for r in reports) / WEI
    total_yes = sum(r.safe_yes_balance for r in reports) / WEI
    total_no = sum(r.safe_no_balance for r in reports) / WEI

    print()
    print("=" * 80)
    print("VERDICTS")
    print("=" * 80)
    print(f"Markets analyzed:                          {n}")
    print(f"Reality.eth answer == INVALID (0xff..ff):  {n_invalid_reality}/{n}")
    print(f"Reality.eth isFinalized:                   {n_finalized}/{n}")
    print(f"CT condition reported (denominator > 0):   {n_ct_reported}/{n}")
    print(f"CT payouts == invalid 50/50 split:         {n_invalid_ct}/{n}")
    print(f"Safe still HOLDS cTokens (unredeemed):     {n_safe_holds_cts}/{n}")
    print(f"Safe HAS already redeemed (event seen):    {n_safe_redeemed}/{n}")
    print(f"Total Yes cTokens held:                    {total_yes:.4f}")
    print(f"Total No cTokens held:                     {total_no:.4f}")
    print(f"Total XDAI redeemable RIGHT NOW:           {total_redeemable:.4f}")


if __name__ == "__main__":
    main()
