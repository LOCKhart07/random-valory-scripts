"""
Verify on-chain status of the SMR market reported by Kim Breitholtz.

User report (kim.breitholtz, 2026-04-10):
  Pearl Omenstrat UI shows status "0m" / "Traded" for an old trade that
  settled yesterday — should show win/loss instead.

Market: "Will any EU country publicly announce, on or before April 9, 2026,
the allocation of new government funding specifically earmarked for the
construction or development of small modular reactors (SMRs) ..."

FPMM: 0x31a3b07935d7fc77d4050c8143d92c362c59826e
Safe: 0xD7F52526ef848F113b9043c98E6124206a8a67aF
Bet : 1.030 No   |   Reported payout if win: 1.871

The agent log itself reports `status: "pending"` and `remaining_seconds: 0`
for this position, which is the same pattern flagged in ZD#919: the market's
opening_ts has passed but Reality.eth has not finalized yet, so the trader
has no win/loss to display. This script verifies that on-chain.

Usage:
    poetry run python omen/verify_smr_market_kim.py
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from web3 import Web3
from web3._utils.events import get_event_data

load_dotenv()

GNOSIS_RPC = os.getenv("GNOSIS_RPC")
assert GNOSIS_RPC, "GNOSIS_RPC missing in .env"

FPMM = Web3.to_checksum_address("0x31a3b07935d7fc77d4050c8143d92c362c59826e")
SAFE = Web3.to_checksum_address("0xD7F52526ef848F113b9043c98E6124206a8a67aF")
CONDITIONAL_TOKENS = Web3.to_checksum_address("0xCeAfDD6bc0bEF976fdCd1112955828E00543c0Ce")
REALITIO = Web3.to_checksum_address("0x79e32aE03fb27B07C89c0c568F80287C01ca2E57")
WXDAI = Web3.to_checksum_address("0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d")

INVALID_ANSWER = "0x" + "ff" * 32
ZERO_BYTES32 = "0x" + "00" * 32

FPMM_ABI = [
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
]

REALITIO_ABI = [
    {"inputs": [{"type": "bytes32"}], "name": "getBestAnswer",
     "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getFinalAnswer",
     "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "isFinalized",
     "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getFinalizeTS",
     "outputs": [{"type": "uint32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getOpeningTS",
     "outputs": [{"type": "uint32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getTimeout",
     "outputs": [{"type": "uint32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "getBond",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]


def fmt_ts(ts: int) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def find_question_id(w3: Web3, condition_id_hex: str, latest: int):
    """Recover questionId for a condition by scanning ConditionPreparation events."""
    event_abi = next(a for a in CT_ABI if a.get("name") == "ConditionPreparation")
    sig = "0x" + w3.keccak(text="ConditionPreparation(bytes32,address,bytes32,uint256)").hex().lstrip("0x")
    topic1 = condition_id_hex if condition_id_hex.startswith("0x") else "0x" + condition_id_hex
    step = 500_000
    end = latest
    floor = max(latest - 400 * 17280, 1)
    while end >= floor:
        start = max(end - step + 1, floor)
        try:
            logs = w3.eth.get_logs({
                "fromBlock": start, "toBlock": end,
                "address": CONDITIONAL_TOKENS,
                "topics": [sig, topic1],
            })
        except Exception:
            sub = step // 5
            logs = []
            c = start
            while c <= end:
                cn = min(c + sub - 1, end)
                logs.extend(w3.eth.get_logs({
                    "fromBlock": c, "toBlock": cn,
                    "address": CONDITIONAL_TOKENS,
                    "topics": [sig, topic1],
                }))
                c = cn + 1
        if logs:
            ev = get_event_data(w3.codec, event_abi, logs[0])
            return (
                "0x" + ev["args"]["questionId"].hex(),
                Web3.to_checksum_address(ev["args"]["oracle"]),
                ev["blockNumber"],
            )
        end = start - 1
    return ("", "", 0)


def main():
    w3 = Web3(Web3.HTTPProvider(GNOSIS_RPC))
    assert w3.is_connected(), "RPC not reachable"
    latest = w3.eth.block_number
    now = int(datetime.now(timezone.utc).timestamp())
    print(f"Gnosis head: {latest}    now: {fmt_ts(now)}")
    print(f"FPMM: {FPMM}    Safe: {SAFE}\n")

    fpmm = w3.eth.contract(address=FPMM, abi=FPMM_ABI)
    ct = w3.eth.contract(address=CONDITIONAL_TOKENS, abi=CT_ABI)
    rl = w3.eth.contract(address=REALITIO, abi=REALITIO_ABI)

    # 1. condition id from FPMM
    cid_bytes = fpmm.functions.conditionIds(0).call()
    cid_hex = "0x" + cid_bytes.hex()
    print(f"conditionId: {cid_hex}")

    # 2. recover questionId via ConditionPreparation event
    qid, oracle, prep_block = find_question_id(w3, cid_hex, latest)
    print(f"questionId : {qid}")
    print(f"oracle     : {oracle}")
    print(f"prep block : {prep_block}\n")

    if not qid:
        print("ERROR: could not recover questionId")
        return

    # 3. Reality.eth state
    print("--- Reality.eth ---")
    best   = "0x" + rl.functions.getBestAnswer(qid).call().hex()
    try:
        final  = "0x" + rl.functions.getFinalAnswer(qid).call().hex()
    except Exception as e:
        final  = f"<revert: {str(e)[:80]}>"
    is_fin = rl.functions.isFinalized(qid).call()
    fts    = rl.functions.getFinalizeTS(qid).call()
    ots    = rl.functions.getOpeningTS(qid).call()
    timeout = rl.functions.getTimeout(qid).call()
    bond   = rl.functions.getBond(qid).call() / 1e18
    print(f"  isFinalized   : {is_fin}")
    print(f"  bestAnswer    : {best}  (== INVALID? {best.lower() == INVALID_ANSWER})")
    print(f"  finalAnswer   : {final}")
    print(f"  openingTS     : {ots}  ({fmt_ts(ots)})")
    print(f"  finalizeTS    : {fts}  ({fmt_ts(fts)})")
    print(f"  timeout       : {timeout} s")
    print(f"  bond          : {bond} xDAI")
    if not is_fin and fts:
        delta = fts - now
        sign = "in" if delta > 0 else "ago"
        print(f"  -> finalizes  : {abs(delta)//3600}h {(abs(delta)%3600)//60}m {sign}")

    # 4. CT resolution + Safe holdings
    print("\n--- ConditionalTokens ---")
    pd = ct.functions.payoutDenominator(cid_bytes).call()
    pn0 = ct.functions.payoutNumerators(cid_bytes, 0).call()  # Yes
    pn1 = ct.functions.payoutNumerators(cid_bytes, 1).call()  # No
    print(f"  payoutDenominator       : {pd}")
    print(f"  payoutNumerators[Yes,No]: [{pn0}, {pn1}]")
    print(f"  -> condition reported?  : {pd != 0}")

    parent = bytes.fromhex(ZERO_BYTES32[2:])
    yes_collection = ct.functions.getCollectionId(parent, cid_bytes, 1).call()  # outcome 0 = index set 0b01
    no_collection  = ct.functions.getCollectionId(parent, cid_bytes, 2).call()  # outcome 1 = index set 0b10
    yes_pos = ct.functions.getPositionId(WXDAI, yes_collection).call()
    no_pos  = ct.functions.getPositionId(WXDAI, no_collection).call()
    yes_bal = ct.functions.balanceOf(SAFE, yes_pos).call()
    no_bal  = ct.functions.balanceOf(SAFE, no_pos).call()
    print(f"  Safe Yes balance        : {yes_bal / 1e18} xDAI-equiv")
    print(f"  Safe No  balance        : {no_bal  / 1e18} xDAI-equiv")

    if pd != 0:
        redeem = (yes_bal * pn0 + no_bal * pn1) // pd
        print(f"  -> redeemPositions()    : {redeem / 1e18} xDAI")
    else:
        print(f"  -> redeemPositions()    : 0 (condition not reported yet)")

    # 5. Final verdict
    print("\n--- Verdict ---")
    if is_fin and pd != 0:
        winner = "Yes" if pn0 > pn1 else "No" if pn1 > pn0 else "Invalid/split"
        print(f"  Resolved. Winning outcome: {winner}.")
        print(f"  This trade should show as Won/Lost in the UI — if it doesn't,")
        print(f"  the bug is in Pearl's UI rendering, not the on-chain state.")
    elif (not is_fin) and best.lower() != ZERO_BYTES32 and best.lower() != INVALID_ANSWER:
        winning_bit = int.from_bytes(bytes.fromhex(best[2:]), "big")
        likely = "Yes" if winning_bit == 1 else "No" if winning_bit == 0 else f"raw={winning_bit}"
        print(f"  PENDING — same pattern as ZD#919.")
        print(f"  Reality.eth has a provisional answer ({likely}) but the")
        print(f"  dispute window does not close until {fmt_ts(fts)}.")
        print(f"  CT condition is NOT yet reported (payoutDenominator=0), so the")
        print(f"  trader has no win/loss to display. The Pearl UI should show")
        print(f"  'awaiting resolution' instead of 'Traded / 0m'.")
    elif (not is_fin) and best.lower() == ZERO_BYTES32:
        print(f"  PENDING — no answer posted to Reality.eth yet at all.")
    else:
        print(f"  Unexpected state — investigate manually.")


if __name__ == "__main__":
    main()
