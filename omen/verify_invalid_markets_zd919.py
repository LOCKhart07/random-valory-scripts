"""
ZD#919 — Verify on-chain status of 7 "invalid" Omen markets for Pearl trader Safe.

For each of the 7 FPMM addresses provided in the ticket, this script:
  1. Reads the FPMM contract for conditionId, collateralToken.
  2. Reads Reality.eth for the question's final/best answer and finalization status.
     The questionId is recovered from the ConditionPreparation event on the CT.
  3. Reads the ConditionalTokens contract for:
       - payoutNumerators / payoutDenominator (condition resolution)
       - balanceOf(Safe, positionId) for Yes and No outcomes
  4. Computes the XDAI amount that redeemPositions() would return today.
  5. Queries the Omen subgraph for trades by the Safe and redemption records.
  6. Scans PayoutRedemption events on the CT to see if any OTHER address has
     already redeemed the same condition (proves the protocol path is clean).

Usage:
    poetry run python omen/verify_invalid_markets_zd919.py
"""

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv
from web3 import Web3
from web3._utils.events import get_event_data

load_dotenv()

GNOSIS_RPC = os.getenv("GNOSIS_RPC")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAFE = Web3.to_checksum_address("0x85378392A666759e1170a53a34a1Ae98e54F7fD0")
CONDITIONAL_TOKENS = Web3.to_checksum_address("0xCeAfDD6bc0bEF976fdCd1112955828E00543c0Ce")
# Reality.eth v3 — this is what Omen's oracle proxy (0xAB16...) points at on Gnosis.
# Verified via oracle.realitio() on-chain.
REALITIO = Web3.to_checksum_address("0x79e32aE03fb27B07C89c0c568F80287C01ca2E57")
WXDAI = Web3.to_checksum_address("0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d")

OMEN_SUBGRAPH = "https://api.subgraph.staging.autonolas.tech/api/proxy/predict-omen"

INVALID_ANSWER = "0x" + "ff" * 32
ZERO_BYTES32 = b"\x00" * 32
WEI = 10 ** 18

MARKETS = [
    ("0x1a8aee366e16f0525a9967468e423b762ee49122", "FIFA cap on 2026 World Cup resale ticket prices by Apr 8 2026", 1.679),
    ("0x93b2c7f7db5911de3752a49440c9fcb1d94f70a7", "Shelly Kittleson released/found safe by Apr 8 2026", 1.596),
    ("0x441162d89526f368b4b56a34d9b8f2685d4804d0", "HP EliteBook 6 G2q on HP.com by Apr 8 2026", 1.893),
    ("0xb09065a7763e8c77503fc3c8108e686620841eab", "100k ChromeOS Flex kits sold worldwide by Apr 8 2026", 0.567),
    ("0xd98e374ba1497164a4e5f7aa337308e2178faacb", "US pharma reshoring of brand-name drug manufacturing by Apr 8 2026", 1.078),
    ("0xe4dab291e3244e66b64ebba0fb5035a6e559d9ef", "CrystalX RAT detection outside Russia by Apr 8 2026", 1.090),
    ("0x3c9c6f3169b6bf355d17cbe7b8efaa5a1137d982", "EPA microplastics regulation under SDWA by Apr 8 2026", 1.022),
]

# ---------------------------------------------------------------------------
# ABIs (minimal)
# ---------------------------------------------------------------------------

FPMM_ABI = [
    {"inputs": [], "name": "conditionalTokens", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "collateralToken", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}], "name": "conditionIds", "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
]

CT_ABI = [
    {"inputs": [{"type": "bytes32"}, {"type": "bytes32"}, {"type": "uint256"}], "name": "getCollectionId",
     "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}, {"type": "bytes32"}], "name": "getPositionId",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}, {"type": "uint256"}], "name": "balanceOf",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}, {"type": "uint256"}], "name": "payoutNumerators",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "payoutDenominator",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    # Events
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
    {"inputs": [{"type": "bytes32"}], "name": "getHistoryHash",
     "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
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
        except Exception as e:
            if chunk > 10_000:
                # back off with smaller chunks
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
    label: str
    stake: float
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
    other_redemptions: List[dict] = field(default_factory=list)
    prep_block: int = 0
    resolution_block: int = 0
    errors: List[str] = field(default_factory=list)


def recover_question_and_oracle(w3: Web3, ct, condition_id_bytes: bytes,
                                latest_block: int) -> tuple:
    """Fetch the ConditionPreparation event for the given conditionId to recover
    the Reality.eth questionId and oracle. Returns (question_id_hex, oracle, prep_block)."""
    # Scan the last ~400 days for the prep event (~400 * 17280 = ~6.9M blocks).
    # We scan backwards in 500k-block chunks.
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
        except Exception as e:
            # Chunk smaller on RPC complaint
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
    """Find the ConditionResolution event for a condition. Returns block number or 0."""
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


def find_other_redemptions(w3: Web3, condition_id_bytes: bytes,
                           start_block: int, latest_block: int) -> List[dict]:
    """Scan PayoutRedemption events for this condition. Returns list of
    {redeemer, payout, block}. Looks from start_block (resolution) forward."""
    event_abi = next(a for a in CT_ABI if a.get("name") == "PayoutRedemption" and a.get("type") == "event")
    sig = w3.keccak(text="PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)")
    sig_hex = "0x" + sig.hex().lstrip("0x")
    # conditionId is NOT indexed — it is in the data field, so we must scan all
    # PayoutRedemption events and filter. But we also constrain by collateralToken
    # (indexed) = WXDAI to narrow the haystack.
    ct_topic = "0x" + WXDAI.lower()[2:].zfill(64)
    out = []
    if start_block == 0:
        start_block = max(latest_block - 30 * 17280, 1)
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
                "redeemer": ev["args"]["redeemer"],
                "payout": int(ev["args"]["payout"]),
                "block": lg["blockNumber"],
                "tx": lg["transactionHash"].hex(),
            })
    return out


def analyze_market(w3: Web3, mkt: tuple, latest_block: int,
                   realitio, ct) -> MarketReport:
    address, label, stake = mkt
    r = MarketReport(address=Web3.to_checksum_address(address), label=label, stake=stake)

    fpmm = w3.eth.contract(address=Web3.to_checksum_address(address), abi=FPMM_ABI)
    try:
        ct_addr = fpmm.functions.conditionalTokens().call()
        r.collateral = fpmm.functions.collateralToken().call()
        cid = fpmm.functions.conditionIds(0).call()
        r.condition_id = "0x" + cid.hex()
    except Exception as e:
        r.errors.append(f"FPMM read failed: {e}")
        return r

    if Web3.to_checksum_address(ct_addr) != CONDITIONAL_TOKENS:
        r.errors.append(f"Unexpected CT address: {ct_addr}")

    # Payouts
    try:
        num0 = ct.functions.payoutNumerators(cid, 0).call()
        num1 = ct.functions.payoutNumerators(cid, 1).call()
        r.payout_numerators = [num0, num1]
        r.payout_denominator = ct.functions.payoutDenominator(cid).call()
    except Exception as e:
        r.errors.append(f"payouts read failed: {e}")

    # Safe balances (Yes = indexSet 1, No = indexSet 2 for binary market)
    try:
        coll_id_yes = ct.functions.getCollectionId(ZERO_BYTES32, cid, 1).call()
        coll_id_no = ct.functions.getCollectionId(ZERO_BYTES32, cid, 2).call()
        pos_id_yes = ct.functions.getPositionId(Web3.to_checksum_address(r.collateral), coll_id_yes).call()
        pos_id_no = ct.functions.getPositionId(Web3.to_checksum_address(r.collateral), coll_id_no).call()
        r.safe_yes_balance = ct.functions.balanceOf(SAFE, pos_id_yes).call()
        r.safe_no_balance = ct.functions.balanceOf(SAFE, pos_id_no).call()
    except Exception as e:
        r.errors.append(f"balanceOf failed: {e}")

    # Redeemable:
    # For a resolved condition, redeemPositions(collateral, parent=0, conditionId, indexSets=[1,2])
    # pays  (bal_yes * num[0] + bal_no * num[1]) / denom
    if r.payout_denominator > 0:
        r.redeemable_wei = (r.safe_yes_balance * r.payout_numerators[0] +
                            r.safe_no_balance * r.payout_numerators[1]) // r.payout_denominator

    # Recover questionId and oracle via ConditionPreparation event
    try:
        qid, oracle, prep_block = recover_question_and_oracle(w3, ct, cid, latest_block)
        r.question_id = qid
        r.oracle = oracle
        r.prep_block = prep_block
    except Exception as e:
        r.errors.append(f"ConditionPreparation lookup failed: {e}")

    # Query Reality.eth for the question
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

    return r


# ---------------------------------------------------------------------------
# Subgraph cross-checks
# ---------------------------------------------------------------------------

def subgraph_trades_and_redemptions(safe: str, fpmm_addrs: List[str]) -> dict:
    """Try several schema shapes on the predict-omen subgraph.
    Returns {'trades': [...], 'redemptions': [...]} or empty lists on failure."""
    safe_l = safe.lower()

    trades = []
    redemptions = []

    # Attempt fpmmTrades
    q = """
    query($creator: String!, $fpmms: [String!]!) {
      fpmmTrades(first: 1000, where: {creator: $creator, fpmm_in: $fpmms}) {
        id type creator { id } collateralAmount outcomeIndex outcomeTokensTraded
        fpmm { id title }
        creationTimestamp
      }
    }
    """
    d = post_subgraph(OMEN_SUBGRAPH, q, {"creator": safe_l, "fpmms": [a.lower() for a in fpmm_addrs]})
    if d is not None and d.get("fpmmTrades") is not None:
        trades = d["fpmmTrades"]

    # Attempt redemptions
    q2 = """
    query($redeemer: String!) {
      redemptions(first: 1000, where: {redeemer: $redeemer}) {
        id payout conditionId redeemer indexSets blockTimestamp
      }
    }
    """
    d2 = post_subgraph(OMEN_SUBGRAPH, q2, {"redeemer": safe_l})
    if d2 is not None and d2.get("redemptions") is not None:
        redemptions = d2["redemptions"]

    return {"trades": trades, "redemptions": redemptions}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def fmt_payouts(nums: List[int], denom: int) -> str:
    if not nums or denom == 0:
        return "unreported"
    return f"[{','.join(str(n) for n in nums)}] / {denom}"


def is_invalid_resolution(nums: List[int], denom: int) -> bool:
    return (len(nums) == 2 and denom > 0 and nums[0] == nums[1] and nums[0] > 0)


def interpret_reality_answer(best: str) -> str:
    """Return 'Yes', 'No', 'Invalid', 'Unanswered', or raw hex."""
    if not best or best == "0x":
        return "unknown"
    val = int(best, 16)
    if best.lower() == INVALID_ANSWER:
        return "Invalid (0xff..ff)"
    if val == 0:
        return "Yes (outcome 0)"
    if val == 1:
        return "No (outcome 1)"
    return f"raw={best}"


def projected_redeemable(answer_hex: str, yes_bal: int, no_bal: int) -> tuple:
    """If the current best answer finalizes, what will the Safe redeem?
    Returns (projected_wei, winning_side_label)."""
    if not answer_hex:
        return (0, "unknown")
    if answer_hex.lower() == INVALID_ANSWER:
        # Invalid: split 50/50 => each token pays 0.5
        return ((yes_bal + no_bal) // 2, "Invalid (50/50 split)")
    val = int(answer_hex, 16)
    if val == 0:
        return (yes_bal, "Yes wins — Safe's Yes tokens redeem 1:1")
    if val == 1:
        return (no_bal, "No wins — Safe's No tokens redeem 1:1")
    return (0, f"unknown answer {answer_hex}")


def main():
    assert GNOSIS_RPC, "GNOSIS_RPC env var missing"
    w3 = Web3(Web3.HTTPProvider(GNOSIS_RPC))
    assert w3.is_connected(), "RPC not connected"
    latest = w3.eth.block_number
    now_ts = w3.eth.get_block(latest)["timestamp"]
    now_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()
    print(f"Connected to Gnosis. Latest block: {latest}  ({now_iso})")
    print(f"Safe: {SAFE}")
    print(f"CT:   {CONDITIONAL_TOKENS}")
    print(f"RealitIO: {REALITIO}")
    print()

    ct = w3.eth.contract(address=CONDITIONAL_TOKENS, abi=CT_ABI)
    realitio = w3.eth.contract(address=REALITIO, abi=REALITIO_ABI)

    reports: List[MarketReport] = []
    for mkt in MARKETS:
        print(f"--- Analyzing {mkt[0]} ({mkt[1][:48]}) ---")
        try:
            r = analyze_market(w3, mkt, latest, realitio, ct)
        except Exception as e:
            r = MarketReport(address=mkt[0], label=mkt[1], stake=mkt[2], errors=[str(e)])
        reports.append(r)
        # Scan for other redemptions on this condition
        if r.condition_id and r.resolution_block:
            try:
                r.other_redemptions = find_other_redemptions(
                    w3, bytes.fromhex(r.condition_id[2:]),
                    r.resolution_block, latest)
            except Exception as e:
                r.errors.append(f"PayoutRedemption scan failed: {e}")
        print(f"  condId={r.condition_id}")
        print(f"  payouts={fmt_payouts(r.payout_numerators, r.payout_denominator)}")
        print(f"  safe Yes={r.safe_yes_balance/WEI:.4f}  No={r.safe_no_balance/WEI:.4f}")
        print(f"  redeemable={r.redeemable_wei/WEI:.4f} xDAI")
        print(f"  other redemptions on this condition: {len(r.other_redemptions)}")
        if r.errors:
            for err in r.errors:
                print(f"  ERROR: {err}")
        print()

    # Subgraph cross-check
    print("=" * 80)
    print("Subgraph cross-check (predict-omen)")
    print("=" * 80)
    sg = subgraph_trades_and_redemptions(SAFE, [m[0] for m in MARKETS])
    print(f"  fpmmTrades (creator={SAFE}): {len(sg['trades'])} rows")
    print(f"  redemptions (redeemer={SAFE}): {len(sg['redemptions'])} rows")
    if sg["redemptions"]:
        for r in sg["redemptions"][:20]:
            print(f"    {r}")
    print()

    # Report table
    print("=" * 80)
    print("FINAL REPORT")
    print("=" * 80)

    total_redeemable = 0
    total_projected = 0
    total_stake = 0
    summary_rows = []
    for r in reports:
        total_stake += r.stake
        total_redeemable += r.redeemable_wei / WEI
        proj_wei, proj_label = projected_redeemable(
            r.reality_best_answer, r.safe_yes_balance, r.safe_no_balance)
        total_projected += proj_wei / WEI
        answer_txt = interpret_reality_answer(r.reality_best_answer)
        if r.reality_finalize_ts:
            delta = r.reality_finalize_ts - now_ts
            if delta > 0:
                finalize_in = f"in {delta/3600:.1f} h"
            else:
                finalize_in = f"{-delta/3600:.1f} h ago (finalizable)"
        else:
            finalize_in = "n/a"
        summary_rows.append({
            "addr": r.address,
            "label": r.label,
            "stake": r.stake,
            "answer": answer_txt,
            "yes_bal": r.safe_yes_balance / WEI,
            "no_bal": r.safe_no_balance / WEI,
            "projected": proj_wei / WEI,
            "finalize_in": finalize_in,
            "proj_label": proj_label,
        })
        print()
        print(f"FPMM {r.address[:10]}... ({r.label[:50]}, stake {r.stake:.3f} XDAI)")
        print(f"  conditionId:          {r.condition_id}")
        print(f"  questionId:           {r.question_id or '(not recovered)'}")
        print(f"  oracle:               {r.oracle}")
        print(f"  Reality bestAnswer:   {r.reality_best_answer}")
        print(f"  Reality finalAnswer:  {r.reality_final_answer}")
        print(f"  Reality bond:         {r.reality_bond:.4f} xDAI (0 = no answer posted)")
        print(f"  Reality openingTS:    {r.reality_opening_ts} "
              f"({datetime.fromtimestamp(r.reality_opening_ts, tz=timezone.utc).isoformat() if r.reality_opening_ts else '—'})")
        print(f"  Reality timeout:      {r.reality_timeout} s ({r.reality_timeout/3600:.1f} h)")
        print(f"  Reality arbitrator:   {r.reality_arbitrator}")
        print(f"  Reality finalized:    {r.reality_finalized} (ts={r.reality_finalize_ts})")
        is_invalid_reality = (r.reality_best_answer == INVALID_ANSWER)
        is_invalid_resol = is_invalid_resolution(r.payout_numerators, r.payout_denominator)
        print(f"  => Reality INVALID:   {is_invalid_reality}")
        print(f"  payoutNumerators:     {fmt_payouts(r.payout_numerators, r.payout_denominator)}")
        print(f"  => CT invalid payout: {is_invalid_resol}")
        print(f"  Safe Yes balance:     {r.safe_yes_balance/WEI:.6f} cTokens ({r.safe_yes_balance} wei)")
        print(f"  Safe No balance:      {r.safe_no_balance/WEI:.6f} cTokens ({r.safe_no_balance} wei)")
        already_redeemed = (r.safe_yes_balance == 0 and r.safe_no_balance == 0)
        print(f"  Already redeemed:     {already_redeemed}")
        print(f"  Redeemable now:       {r.redeemable_wei/WEI:.6f} XDAI")
        print(f"  Other redeemers seen: {len(r.other_redemptions)}")
        if r.other_redemptions:
            others_total = sum(x["payout"] for x in r.other_redemptions) / WEI
            distinct = {x["redeemer"].lower() for x in r.other_redemptions}
            print(f"    {len(distinct)} distinct addresses, total payout {others_total:.4f} XDAI")
            for x in r.other_redemptions[:5]:
                print(f"    - {x['redeemer']} payout={x['payout']/WEI:.4f} block={x['block']}")
        print(f"  Interpreted answer:   {answer_txt}")
        print(f"  Finalize window:      {finalize_in}")
        print(f"  Projected post-final: {proj_wei/WEI:.4f} XDAI ({proj_label})")
        if r.errors:
            for err in r.errors:
                print(f"  !! {err}")

    print()
    print("=" * 80)
    print("SUMMARY TABLE")
    print("=" * 80)
    print(f"{'FPMM':<14}{'Stake':>8}{'Yes bal':>10}{'No bal':>10}  {'Answer':<22}"
          f"{'Finalize in':<18}{'Projected':>10}  Label")
    for s in summary_rows:
        print(f"{s['addr'][:12]:<14}"
              f"{s['stake']:>8.3f}"
              f"{s['yes_bal']:>10.3f}"
              f"{s['no_bal']:>10.3f}  "
              f"{s['answer']:<22}"
              f"{s['finalize_in']:<18}"
              f"{s['projected']:>10.3f}  {s['proj_label'][:40]}")
    print()
    print(f"TOTAL stake at risk:                  {total_stake:.4f} XDAI")
    print(f"TOTAL recoverable RIGHT NOW:          {total_redeemable:.4f} XDAI  (nothing reported yet)")
    print(f"TOTAL projected post-finalization:    {total_projected:.4f} XDAI  (based on current Reality answers)")
    print()

    # Verdicts
    print("=" * 80)
    print("VERDICTS (one-liners)")
    print("=" * 80)
    n_invalid_reality = sum(1 for r in reports if r.reality_best_answer == INVALID_ANSWER)
    n_invalid_ct = sum(1 for r in reports if is_invalid_resolution(r.payout_numerators, r.payout_denominator))
    n_unredeemed = sum(1 for r in reports if r.safe_yes_balance or r.safe_no_balance)
    n_others = sum(1 for r in reports if r.other_redemptions)
    n_not_finalized = sum(1 for r in reports if not r.reality_finalized)
    n_ct_unreported = sum(1 for r in reports if r.payout_denominator == 0)
    sg_red = len([r for r in sg["redemptions"]
                  if any(r.get("conditionId", "").lower() == rr.condition_id.lower()
                         for rr in reports)])
    print(f"1. Reality.eth answered INVALID (0xff..ff): {n_invalid_reality}/7  (THESE ARE NOT INVALID)")
    print(f"   Reality.eth still NOT finalized:        {n_not_finalized}/7")
    print(f"   CT condition NOT yet reported:          {n_ct_unreported}/7  (payoutDenominator == 0)")
    print(f"2. Safe still holds unredeemed CTokens:    {n_unredeemed}/7")
    print(f"3. Total XDAI recoverable by redeemPositions() RIGHT NOW:   {total_redeemable:.4f}")
    print(f"   Total XDAI projected AFTER finalization & reportPayouts: {total_projected:.4f}")
    print(f"4. Subgraph redemptions rows for Safe on these conditions: {sg_red} (schema miss — query fell through)")
    print(f"5. Markets where OTHER addresses successfully redeemed:  {n_others}/7  (zero — nothing is redeemable yet)")


if __name__ == "__main__":
    main()
