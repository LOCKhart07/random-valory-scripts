"""
Interactive market resolution script for Omen/Reality.io.

Fetches all unfinalized markets created by Pearl and Quickstart, calls
the mech tool (offchain) to get the resolution answer, compares with any
existing on-chain answer, and lets the operator submit the correct answer
via Ledger hardware wallet with a configurable bond.

Two signing keys:
  - Mech requests: regular private key file (offchain, no gas)
  - Reality.io bonds: Ledger hardware wallet (physical approval per tx)

Env vars (in .env):
    MECH_PRIVATE_KEY — hex private key for mech requests (offchain only)
    SUBGRAPH_API_KEY — The Graph API key
    GNOSIS_RPC — Gnosis Chain RPC endpoint

Usage:
    python market-creator/resolve_markets.py --dry-run
    python market-creator/resolve_markets.py --bond 10
    python market-creator/resolve_markets.py --bond 0.01 --dry-run
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests as http_requests
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PEARL_CREATOR = "0xFfc8029154ECD55ABED15BD428bA596E7D23f557".lower()
QS_CREATOR = "0x89c5cc945dd550BcFfb72Fe42BfF002429F46Fec".lower()
CREATORS = [PEARL_CREATOR, QS_CREATOR]
CREATOR_LABELS = {PEARL_CREATOR: "Pearl", QS_CREATOR: "QS"}

REALITIO_ADDRESS = "0x79e32aE03fb27B07C89c0c568F80287C01ca2E57"
MECH_ADDRESS = "0xC05e7412439bD7e91730a6880E18d5D5873F632C"
MECH_TOOL = "resolve-market-reasoning-gpt-4.1"
CHAIN_ID = 100
SEP = "\u241f"

ANSWER_YES = bytes(32)  # 0x00..00
ANSWER_NO = (1).to_bytes(32, "big")  # 0x00..01
ANSWER_INVALID = b"\xff" * 32

ANSWER_LABELS = {
    ANSWER_YES.hex(): "Yes",
    ANSWER_NO.hex(): "No",
    ANSWER_INVALID.hex(): "Invalid",
}

OMEN_SUBGRAPH_ID = "9fUVQpFwzpdWS9bq5WkAnmKbNNcoBwatMR4yZq81pbbz"

REALITIO_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "question_id", "type": "bytes32"},
            {"name": "answer", "type": "bytes32"},
            {"name": "max_previous", "type": "uint256"},
        ],
        "name": "submitAnswer",
        "outputs": [],
        "payable": True,
        "stateMutability": "payable",
        "type": "function",
    }
]


# ---------------------------------------------------------------------------
# Subgraph
# ---------------------------------------------------------------------------


def fetch_unfinalized_markets(api_key):
    """Fetch all unfinalized markets from Pearl and QS creators."""
    url = f"https://gateway.thegraph.com/api/{api_key}/subgraphs/id/{OMEN_SUBGRAPH_ID}"
    now = int(time.time())
    all_markets = []

    for creator in CREATORS:
        label = CREATOR_LABELS[creator]
        skip = 0
        while True:
            query = f"""
            {{
              fixedProductMarketMakers(
                where: {{
                  creator: "{creator}"
                  openingTimestamp_lt: {now}
                  answerFinalizedTimestamp: null
                }}
                first: 1000
                skip: {skip}
                orderBy: openingTimestamp
                orderDirection: asc
              ) {{
                id
                question {{
                  id
                  title
                  outcomes
                  currentAnswer
                  currentAnswerBond
                }}
                openingTimestamp
                currentAnswer
                currentAnswerBond
                timeout
                collateralVolume
              }}
            }}
            """
            r = http_requests.post(
                url,
                json={"query": query},
                headers={"Content-Type": "application/json"},
                timeout=90,
            )
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                print(f"  Subgraph error: {data['errors']}")
                break
            markets = data.get("data", {}).get("fixedProductMarketMakers", [])
            for m in markets:
                m["_creator_label"] = label
            all_markets.extend(markets)
            if len(markets) < 1000:
                break
            skip += 1000

        print(
            f"  {label}: {len([m for m in all_markets if m['_creator_label'] == label])} unfinalized markets"
        )

    return all_markets


def decode_answer(answer_hex):
    """Convert hex answer to human-readable label."""
    if answer_hex is None:
        return None
    normalized = answer_hex.lower().replace("0x", "").zfill(64)
    return ANSWER_LABELS.get(normalized, f"Unknown(0x{normalized[:8]}...)")


def parse_current_bond(market):
    """Get the current bond in xDAI from a market."""
    bond = market.get("currentAnswerBond") or (market.get("question") or {}).get(
        "currentAnswerBond"
    )
    if bond is None:
        return 0
    return int(bond)


# ---------------------------------------------------------------------------
# Mech
# ---------------------------------------------------------------------------


async def get_mech_answer(service, title):
    """Call the mech tool offchain to get the resolution answer."""
    try:
        result = await service.send_request(
            prompts=(title,),
            tools=(MECH_TOOL,),
            priority_mech=MECH_ADDRESS,
            use_offchain=False,
        )
        return result
    except Exception as e:
        print(f"    Mech error: {e}")
        return None


def parse_mech_response(result):
    """Parse mech response into an answer bytes32.

    Returns (answer_bytes, label) or (None, reason).
    Same logic as market creator's answer_questions.py lines 72-95.
    """
    if result is None:
        return None, "mech_error"

    # mech-client returns {delivery_results: {request_id: ipfs_url}, request_ids: [...]}
    # Actual response is at {ipfs_url}/{request_id_as_integer}
    response_data = None
    if isinstance(result, dict):
        delivery = result.get("delivery_results", {})
        request_ids = result.get("request_ids", [])
        if delivery and request_ids:
            request_id_hex = request_ids[0]
            request_id_int = str(int(request_id_hex, 16))
            ipfs_url = delivery.get(request_id_hex, "")
            if ipfs_url:
                fetch_url = ipfs_url.rstrip("/") + "/" + request_id_int
                try:
                    r = http_requests.get(fetch_url, timeout=30)
                    r.raise_for_status()
                    envelope = r.json()
                    # Response is double-encoded: {"result": "{\"is_determinable\": false}"}
                    result_str = envelope.get("result", "")
                    response_data = json.loads(result_str) if isinstance(result_str, str) else result_str
                except Exception as e:
                    print(f"    IPFS fetch error: {e}")
                    return None, "ipfs_error"

    if response_data is None:
        return None, "no_response"

    try:
        if isinstance(response_data, str):
            data = json.loads(response_data)
        elif isinstance(response_data, dict):
            data = response_data
        else:
            return None, "unparseable"
    except json.JSONDecodeError:
        return None, "json_error"

    is_valid = data.get("is_valid", True)
    is_determinable = data.get("is_determinable", True)
    has_occurred = data.get("has_occurred", None)

    if not is_valid:
        return ANSWER_INVALID, "Invalid"
    if not is_determinable:
        return None, "undeterminable"
    if has_occurred is True:
        return ANSWER_YES, "Yes"
    if has_occurred is False:
        return ANSWER_NO, "No"

    return None, "inconclusive"


# ---------------------------------------------------------------------------
# Reality.io submission
# ---------------------------------------------------------------------------


def submit_answer(
    w3, bond_crypto, question_id_hex, answer_bytes, max_previous, bond_wei
):
    """Build and sign submitAnswer tx via Ledger."""
    realitio = w3.eth.contract(
        address=Web3.to_checksum_address(REALITIO_ADDRESS),
        abi=REALITIO_ABI,
    )

    question_id_bytes = bytes.fromhex(question_id_hex.replace("0x", ""))

    tx_data = realitio.encode_abi(
        "submitAnswer",
        args=[question_id_bytes, answer_bytes, max_previous],
    )

    tx = {
        "to": Web3.to_checksum_address(REALITIO_ADDRESS),
        "value": bond_wei,
        "data": tx_data,
        "gas": 200_000,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(
            Web3.to_checksum_address(bond_crypto.address)
        ),
        "chainId": CHAIN_ID,
    }

    print(f"    Signing on Ledger (approve on device)...")
    signed = bond_crypto.sign_transaction(tx)
    raw_tx = signed.get("raw_transaction") or signed.get("rawTransaction")
    if raw_tx is None:
        print(f"    Error: unexpected signed tx format: {list(signed.keys())}")
        return None

    tx_hash = w3.eth.send_raw_transaction(raw_tx)
    return tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash


def _submit_and_confirm(w3, bond_crypto, question_id, answer_bytes, current_bond, bond_wei, submitted, errors):
    """Submit answer and wait for confirmation. Returns updated (submitted, errors) counts."""
    try:
        tx_hash = submit_answer(
            w3, bond_crypto, question_id, answer_bytes,
            max_previous=current_bond, bond_wei=bond_wei,
        )
        if tx_hash:
            print(f"  TX: {tx_hash}")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt["status"] == 1:
                print(f"  Confirmed (block {receipt['blockNumber']})")
                submitted += 1
            else:
                print(f"  REVERTED")
                errors += 1
        else:
            errors += 1
    except Exception as e:
        print(f"  Error: {e}")
        errors += 1
    return submitted, errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def async_main(args):
    # Load env
    api_key = os.getenv("SUBGRAPH_API_KEY", "")
    rpc_url = os.getenv("GNOSIS_RPC", "")
    if not api_key:
        print("Error: SUBGRAPH_API_KEY not set in .env")
        sys.exit(1)
    if not rpc_url:
        print("Error: GNOSIS_RPC not set in .env")
        sys.exit(1)

    bond_xdai = args.bond
    bond_wei = int(bond_xdai * 10**18)

    # Connect web3
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    print(f"Connected to Gnosis Chain (block {w3.eth.block_number})")

    # Connect mech signing key
    mech_key = os.getenv("MECH_PRIVATE_KEY", "")
    if not mech_key:
        print("Error: MECH_PRIVATE_KEY not set in .env")
        sys.exit(1)
    import tempfile

    from aea_ledger_ethereum import EthereumCrypto

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(mech_key)
        mech_key_path = f.name
    try:
        mech_crypto = EthereumCrypto(mech_key_path)
    finally:
        os.unlink(mech_key_path)
    print(f"Mech key: {mech_crypto.address}")

    # Connect Ledger (skip in dry-run)
    bond_crypto = None
    if not args.dry_run:
        from aea_ledger_ethereum_hwi import EthereumHWICrypto

        bond_crypto = EthereumHWICrypto(
            default_device_index=args.device,
            default_account_index=args.account,
            default_keypair_index=0,
        )
        balance = w3.eth.get_balance(Web3.to_checksum_address(bond_crypto.address))
        print(f"Ledger address: {bond_crypto.address}")
        print(f"Ledger balance: {balance / 10**18:.4f} xDAI")
    else:
        print("DRY RUN — no Ledger needed, no transactions will be sent")

    # Fetch markets
    print(f"\nFetching unfinalized markets...")
    markets = fetch_unfinalized_markets(api_key)
    print(f"Total: {len(markets)} unfinalized markets\n")

    if not markets:
        print("No markets to process.")
        return

    # Categorize
    unanswered = [m for m in markets if parse_current_bond(m) == 0]
    answered = [m for m in markets if parse_current_bond(m) > 0]
    print(f"  Unanswered: {len(unanswered)}")
    print(f"  Answered (not finalized): {len(answered)}")

    # Init mech-client
    from mech_client import MarketplaceService

    mech_service = MarketplaceService(
        chain_config="gnosis",
        agent_mode=False,
        crypto=mech_crypto,
    )

    # Process markets
    submitted = 0
    skipped = 0
    disagreements = 0
    errors = 0
    inconclusive = []  # markets where mech couldn't determine answer

    all_markets = answered + unanswered  # prioritize already-answered (more urgent)

    for i, market in enumerate(all_markets):
        q = market.get("question") or {}
        title = q.get("title", "").split(SEP)[0].strip()
        question_id = q.get("id", "")
        current_answer_hex = market.get("currentAnswer")
        current_bond = parse_current_bond(market)
        label = market.get("_creator_label", "?")
        is_answered = current_bond > 0

        print(f"\n[{i+1}/{len(all_markets)}] ({label}) {title[:80]}")

        if is_answered:
            current_label = decode_answer(current_answer_hex)
            print(
                f"  On-chain: {current_label} (bond: {current_bond / 10**18:.4f} xDAI)"
            )

            # Check if bond is already >= our bond (someone else defended)
            if current_bond >= bond_wei:
                print(f"  Bond already >= {bond_xdai} xDAI — skipping")
                skipped += 1
                continue
        else:
            print(f"  On-chain: NO ANSWER")

        # Get mech answer
        print(f"  Calling mech ({MECH_TOOL})...")
        result = await get_mech_answer(mech_service, title)
        mech_answer, mech_label = parse_mech_response(result)

        if mech_answer is None:
            print(f"  Mech: {mech_label} — flagged for manual review")
            inconclusive.append({
                "title": title,
                "question_id": question_id,
                "current_answer": decode_answer(current_answer_hex) if is_answered else None,
                "current_bond": current_bond,
                "label": label,
                "reason": mech_label,
            })
            continue

        print(f"  Mech: {mech_label}")

        # Compare
        if is_answered:
            current_normalized = (
                (current_answer_hex or "").lower().replace("0x", "").zfill(64)
            )
            mech_normalized = mech_answer.hex()
            if current_normalized == mech_normalized:
                print(f"  AGREE — skipping")
                skipped += 1
                continue
            else:
                print(
                    f"  DISAGREE — on-chain={decode_answer(current_answer_hex)}, mech={mech_label}"
                )
                disagreements += 1

        # Prompt
        if args.dry_run:
            print(f"  [DRY RUN] Would submit: {mech_label} with {bond_xdai} xDAI bond")
            continue

        if not args.auto:
            choice = (
                input(
                    f"  Submit {mech_label} with {bond_xdai} xDAI bond? [y/n/q(uit)] "
                )
                .strip()
                .lower()
            )
            if choice == "q":
                print("Quitting.")
                break
            if choice != "y":
                skipped += 1
                continue

        # Submit
        submitted, errors = _submit_and_confirm(
            w3, bond_crypto, question_id, mech_answer, current_bond,
            bond_wei, submitted, errors,
        )

    # ---- Manual review for inconclusive markets ----
    if inconclusive:
        print(f"\n{'=' * 60}")
        print(f"MANUAL REVIEW — {len(inconclusive)} inconclusive markets")
        print(f"{'=' * 60}")
        print(f"Mech couldn't determine the answer. Review each and decide.")

        for j, m in enumerate(inconclusive):
            on_chain = f"{m['current_answer']} (bond: {m['current_bond']/10**18:.4f})" if m["current_answer"] else "NO ANSWER"
            print(f"\n  [{j+1}/{len(inconclusive)}] ({m['label']}) {m['title'][:80]}")
            print(f"    On-chain: {on_chain}")
            print(f"    Mech reason: {m['reason']}")

            if args.dry_run:
                print(f"    [DRY RUN] Skipping")
                continue

            choice = (
                input(f"    Submit as: [y]es / [n]o / [i]nvalid / [s]kip / [q]uit? ")
                .strip()
                .lower()
            )
            if choice == "q":
                break
            if choice == "s" or choice == "":
                skipped += 1
                continue

            answer_map = {"y": ANSWER_YES, "n": ANSWER_NO, "i": ANSWER_INVALID}
            answer_bytes = answer_map.get(choice)
            if answer_bytes is None:
                print(f"    Unknown choice '{choice}' — skipping")
                skipped += 1
                continue

            label_map = {"y": "Yes", "n": "No", "i": "Invalid"}
            print(f"    Submitting: {label_map[choice]} with {bond_xdai} xDAI bond")
            submitted, errors = _submit_and_confirm(
                w3, bond_crypto, m["question_id"], answer_bytes, m["current_bond"],
                bond_wei, submitted, errors,
            )

    # Summary
    print(f"\n{'=' * 60}")
    print(f"SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Markets processed: {len(all_markets)}")
    print(f"  Submitted: {submitted}")
    print(f"  Skipped: {skipped}")
    print(f"  Inconclusive (manual review): {len(inconclusive)}")
    print(f"  Disagreements found: {disagreements}")
    print(f"  Errors: {errors}")


def main():
    parser = argparse.ArgumentParser(description="Interactive Omen market resolution")
    parser.add_argument(
        "--bond", type=float, default=10.0, help="Bond amount in xDAI (default: 10)"
    )
    parser.add_argument(
        "--device", type=int, default=0, help="Ledger device index (default: 0)"
    )
    parser.add_argument(
        "--account", type=int, default=0, help="Ledger BIP44 account index (default: 0)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show comparisons without submitting"
    )
    parser.add_argument(
        "--auto", action="store_true", help="Auto-submit all disagreements"
    )
    args = parser.parse_args()

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
