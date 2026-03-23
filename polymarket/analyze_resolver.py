"""
Deep analysis of a suspected oracle manipulator on Omen/Reality.io.

Traces:
  1. Funding source — where did the initial xDAI come from
  2. All FPMM interactions — which markets, which side, how much
  3. Cross-reference with Reality.io resolutions — did they resolve markets they bet on
  4. Accuracy — on resolved markets, what is their win rate
  5. Profit — net P&L from conditional token redemptions vs bets placed

Usage:
    python polymarket/analyze_resolver.py 0xc5fd24b2974743896e1e94c47e99d3960c7d4c96
    python polymarket/analyze_resolver.py 0xc5fd24b2974743896e1e94c47e99d3960c7d4c96 --days 30
"""

import argparse
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

GNOSIS_RPC = os.getenv("GNOSIS_RPC")
SUBGRAPH_API_KEY = os.getenv("SUBGRAPH_API_KEY", "")

REALITIO_SUBGRAPH_ID = "E7ymrCnNcQdAAgLbdFWzGE5mvr5Mb5T9VfT43FqA7bNh"
REALITIO_URL = f"https://gateway.thegraph.com/api/{SUBGRAPH_API_KEY}/subgraphs/id/{REALITIO_SUBGRAPH_ID}"

OMEN_URL = "https://api.subgraph.staging.autonolas.tech/api/proxy/predict-omen"

# Public Gnosis Chain contract addresses
WXDAI = Web3.to_checksum_address("0xe91d153e0b41518a2ce8dd3d7944fa863463a97d")
CONDITIONAL_TOKENS = Web3.to_checksum_address("0xceafdd6bc0bef976fdcd1112955828e00543c0ce")

WEI = 10 ** 18
SEP = "\u241f"
INVALID = "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def post_subgraph(url, q, v=None):
    p = {"query": q}
    if v:
        p["variables"] = v
    for attempt in range(4):
        try:
            r = requests.post(url, json=p, headers={"Content-Type": "application/json"}, timeout=90)
            r.raise_for_status()
            d = r.json()
            if "errors" in d:
                print(f"  SUBGRAPH ERROR: {d['errors']}")
                return None
            return d["data"]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * 2 ** attempt)


# ---------------------------------------------------------------------------
# 1. Funding source
# ---------------------------------------------------------------------------

def analyze_funding(w3, addr, from_block):
    print("=" * 80)
    print("1. FUNDING SOURCE")
    print("=" * 80)

    # Check native xDAI internal transactions by scanning earliest blocks
    # First find the first transaction nonce=0
    balance = w3.eth.get_balance(Web3.to_checksum_address(addr))
    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(addr))
    print(f"\n  Current balance: {balance / WEI:.4f} xDAI")
    print(f"  Total txs sent: {nonce}")

    # Find all native xDAI received via block traces is expensive.
    # Instead, check wxDAI deposits (wrap events) and transfers TO
    TRANSFER_SIG = w3.keccak(text="Transfer(address,address,uint256)").hex()
    addr_topic = "0x" + addr[2:].lower().zfill(64)

    # wxDAI transfers TO this address (from the very beginning)
    # Use a wide range to catch initial funding
    earliest_block = from_block - 200_000  # go further back
    logs_to = w3.eth.get_logs({
        "fromBlock": max(earliest_block, 1),
        "toBlock": from_block + 50_000,  # first few days
        "address": Web3.to_checksum_address(WXDAI),
        "topics": [TRANSFER_SIG, None, addr_topic],
    })

    if logs_to:
        print(f"\n  Early wxDAI transfers TO address ({len(logs_to)} found):")
        for l in logs_to[:15]:
            src = "0x" + l["topics"][1].hex()[-40:]
            amt = int(l["data"].hex(), 16) / WEI
            block = w3.eth.get_block(l["blockNumber"])
            dt = datetime.fromtimestamp(block["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"    {dt}  from={src}  amount={amt:.4f} xDAI  block={l['blockNumber']}")
    else:
        print("\n  No early wxDAI transfers found — likely funded with native xDAI")

    # Check wxDAI Deposit events (wrapping native xDAI)
    DEPOSIT_SIG = w3.keccak(text="Deposit(address,uint256)").hex()
    deposit_logs = w3.eth.get_logs({
        "fromBlock": max(earliest_block, 1),
        "toBlock": w3.eth.block_number,
        "address": Web3.to_checksum_address(WXDAI),
        "topics": [DEPOSIT_SIG, addr_topic],
    })

    if deposit_logs:
        total_wrapped = sum(int(l["data"].hex(), 16) / WEI for l in deposit_logs)
        first_block = w3.eth.get_block(deposit_logs[0]["blockNumber"])
        first_dt = datetime.fromtimestamp(first_block["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"\n  wxDAI Deposit (wrap) events: {len(deposit_logs)}")
        print(f"  Total wrapped: {total_wrapped:.4f} xDAI")
        print(f"  First wrap: {first_dt}")
        for l in deposit_logs[:10]:
            amt = int(l["data"].hex(), 16) / WEI
            block = w3.eth.get_block(l["blockNumber"])
            dt = datetime.fromtimestamp(block["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"    {dt}  wrapped {amt:.4f} xDAI  block={l['blockNumber']}")
    else:
        print("\n  No wxDAI wrap events found")

    # Check first ever transaction
    # Binary search for first tx block
    print("\n  Searching for first transaction...")
    # Check some early blocks manually using transaction receipts
    # We know nonce=0 was the first tx. Let's find it.
    # Try scanning from earliest_block
    first_tx_block = None
    for b in range(max(earliest_block, from_block - 200_000), from_block + 10_000, 5000):
        try:
            count = w3.eth.get_transaction_count(Web3.to_checksum_address(addr), block_identifier=b)
            if count > 0 and first_tx_block is None:
                # Binary search back
                lo, hi = max(b - 5000, 1), b
                while lo < hi:
                    mid = (lo + hi) // 2
                    c = w3.eth.get_transaction_count(Web3.to_checksum_address(addr), block_identifier=mid)
                    if c > 0:
                        hi = mid
                    else:
                        lo = mid + 1
                first_tx_block = lo
                break
        except Exception:
            continue

    if first_tx_block:
        block = w3.eth.get_block(first_tx_block, full_transactions=True)
        dt = datetime.fromtimestamp(block["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"  First outgoing tx at block {first_tx_block} ({dt})")

        # Check who sent xDAI to this address before this block
        # Look at native xDAI transactions in block
        for tx in block["transactions"]:
            if hasattr(tx, "to") and tx.get("to") and tx["to"].lower() == addr.lower():
                print(f"  Funded by: {tx['from']}  amount: {tx['value'] / WEI:.4f} xDAI")


# ---------------------------------------------------------------------------
# 2. FPMM betting activity
# ---------------------------------------------------------------------------

def analyze_betting(w3, addr, from_block):
    print("\n" + "=" * 80)
    print("2. FPMM BETTING ACTIVITY")
    print("=" * 80)

    TRANSFER_SIG = w3.keccak(text="Transfer(address,address,uint256)").hex()
    addr_topic = "0x" + addr[2:].lower().zfill(64)

    latest = w3.eth.block_number

    # wxDAI transfers FROM (bets placed into FPMMs)
    logs_out = w3.eth.get_logs({
        "fromBlock": from_block,
        "toBlock": latest,
        "address": Web3.to_checksum_address(WXDAI),
        "topics": [TRANSFER_SIG, addr_topic, None],
    })

    # wxDAI transfers TO (redemptions from FPMMs / conditional tokens)
    logs_in = w3.eth.get_logs({
        "fromBlock": from_block,
        "toBlock": latest,
        "address": Web3.to_checksum_address(WXDAI),
        "topics": [TRANSFER_SIG, None, addr_topic],
    })

    # Outgoing by destination
    out_by_dest = defaultdict(lambda: {"amount": 0, "count": 0, "blocks": []})
    for l in logs_out:
        dst = "0x" + l["topics"][2].hex()[-40:]
        amt = int(l["data"].hex(), 16) / WEI
        out_by_dest[dst]["amount"] += amt
        out_by_dest[dst]["count"] += 1
        out_by_dest[dst]["blocks"].append(l["blockNumber"])

    # Incoming by source
    in_by_src = defaultdict(lambda: {"amount": 0, "count": 0})
    for l in logs_in:
        src = "0x" + l["topics"][1].hex()[-40:]
        amt = int(l["data"].hex(), 16) / WEI
        in_by_src[src]["amount"] += amt
        in_by_src[src]["count"] += 1

    total_out = sum(d["amount"] for d in out_by_dest.values())
    total_in = sum(d["amount"] for d in in_by_src.values())

    print(f"\n  wxDAI out (bets): {len(logs_out)} transfers, {total_out:.4f} xDAI")
    print(f"  wxDAI in (returns): {len(logs_in)} transfers, {total_in:.4f} xDAI")
    print(f"  Net wxDAI flow: {total_in - total_out:+.4f} xDAI")

    # Conditional tokens redemptions
    ct_in = in_by_src.get(CONDITIONAL_TOKENS.lower(), {"amount": 0, "count": 0})
    print(f"\n  From Conditional Tokens (winning redemptions): {ct_in['count']} transfers, {ct_in['amount']:.4f} xDAI")

    # FPMM returns (selling positions or partial redemptions)
    fpmm_in = {k: v for k, v in in_by_src.items() if k != CONDITIONAL_TOKENS.lower()}
    fpmm_in_total = sum(v["amount"] for v in fpmm_in.values())
    print(f"  From FPMMs directly: {sum(v['count'] for v in fpmm_in.values())} transfers, {fpmm_in_total:.4f} xDAI")

    # Markets traded (unique FPMM destinations)
    fpmm_contracts = set(out_by_dest.keys())
    print(f"\n  Unique markets traded: {len(fpmm_contracts)}")

    # Per-market P&L
    print(f"\n  Per-market breakdown (top 15 by invested):")
    print(f"  {'Market':<14} {'Invested':>10} {'Returned':>10} {'Net':>10} {'Txs':>5}")
    print("  " + "-" * 55)

    market_pnl = []
    for mkt in sorted(fpmm_contracts, key=lambda m: out_by_dest[m]["amount"], reverse=True):
        invested = out_by_dest[mkt]["amount"]
        returned = in_by_src.get(mkt, {}).get("amount", 0)
        net = returned - invested
        txs = out_by_dest[mkt]["count"]
        market_pnl.append({"market": mkt, "invested": invested, "returned": returned, "net": net, "txs": txs})

    for m in market_pnl[:15]:
        print(f"  {m['market'][:14]} {m['invested']:>9.4f} {m['returned']:>9.4f} {m['net']:>+9.4f} {m['txs']:>5}")

    profitable = [m for m in market_pnl if m["net"] > 0]
    losing = [m for m in market_pnl if m["net"] < 0]
    print(f"\n  Profitable markets: {len(profitable)}, total gain: {sum(m['net'] for m in profitable):+.4f} xDAI")
    print(f"  Losing markets: {len(losing)}, total loss: {sum(m['net'] for m in losing):+.4f} xDAI")

    # Temporal pattern — when do they bet
    print(f"\n  Betting time distribution (UTC hour):")
    hour_counts = defaultdict(int)
    for l in logs_out:
        block = w3.eth.get_block(l["blockNumber"])
        hour = datetime.fromtimestamp(block["timestamp"], tz=timezone.utc).hour
        hour_counts[hour] += 1
    for h in sorted(hour_counts.keys()):
        bar = "#" * hour_counts[h]
        print(f"    {h:02d}:00  {hour_counts[h]:>4}  {bar}")

    return fpmm_contracts, out_by_dest


# ---------------------------------------------------------------------------
# 3. Reality.io cross-reference
# ---------------------------------------------------------------------------

def analyze_resolutions(addr):
    print("\n" + "=" * 80)
    print("3. REALITY.IO RESOLUTIONS")
    print("=" * 80)

    data = post_subgraph(REALITIO_URL, '''
    query($user: String!) {
      responses(where: { user: $user }, first: 1000, orderBy: timestamp, orderDirection: desc) {
        timestamp answer bond
        question {
          questionId currentAnswer currentAnswerBond data
          responses(orderBy: timestamp, orderDirection: asc) {
            answer bond user timestamp
          }
        }
      }
    }
    ''', {"user": addr})

    if not data:
        print("  Failed to fetch")
        return {}

    responses = data.get("responses", [])
    print(f"\n  Total answer submissions: {len(responses)}")

    resolutions = {}
    total_bond = 0
    wins = 0
    yes_answers = 0
    no_answers = 0

    for r in responses:
        bond = int(r["bond"]) / WEI
        total_bond += bond
        answer_raw = r["answer"]
        answer_idx = int(answer_raw, 16)
        if answer_idx == 0:
            yes_answers += 1
        else:
            no_answers += 1

        q = r.get("question") or {}
        current = q.get("currentAnswer", "")
        is_final = current and current.lower() == answer_raw.lower()
        if is_final:
            wins += 1

        title = (q.get("data", "") or "").split(SEP)[0].strip()
        resolutions[title] = {
            "side": "Yes" if answer_idx == 0 else "No",
            "is_final": is_final,
            "bond": bond,
            "ts": int(r["timestamp"]),
            "n_responses": len(q.get("responses", [])),
            "qid": q.get("questionId", ""),
        }

    print(f"  Total bond posted: {total_bond:.4f} xDAI")
    print(f"  Answer became final: {wins}/{len(responses)} ({wins/len(responses)*100:.1f}%)")
    print(f"  Answered Yes: {yes_answers}  No: {no_answers}")

    sole_responder = sum(1 for v in resolutions.values() if v["n_responses"] == 1)
    print(f"  Sole responder (no challenger): {sole_responder}/{len(resolutions)}")

    # Timing
    hours = defaultdict(int)
    for v in resolutions.values():
        h = datetime.fromtimestamp(v["ts"], tz=timezone.utc).hour
        hours[h] += 1
    print(f"\n  Resolution time distribution:")
    for h in sorted(hours.keys()):
        print(f"    {h:02d}:00 UTC  {hours[h]:>4}")

    return resolutions


# ---------------------------------------------------------------------------
# 4. Cross-reference bets with resolutions
# ---------------------------------------------------------------------------

def cross_reference(w3, addr, fpmm_contracts, out_by_dest, resolutions, from_block):
    print("\n" + "=" * 80)
    print("4. CROSS-REFERENCE: BETS vs RESOLUTIONS")
    print("=" * 80)

    # For each FPMM they bet on, try to find the market question in Omen
    # Query Omen for these FPMM addresses
    print(f"\n  Querying Omen for {len(fpmm_contracts)} FPMM contracts...")

    fpmm_list = list(fpmm_contracts)
    market_info = {}

    for i in range(0, len(fpmm_list), 100):
        batch = fpmm_list[i:i + 100]
        ids_str = ",".join(f'"{fid}"' for fid in batch)
        data = post_subgraph(OMEN_URL, f"""
        {{ fixedProductMarketMakerCreations(
            first: 100,
            where: {{ id_in: [{ids_str}] }}
          ) {{
            id question outcomes currentAnswer
          }}
        }}
        """)
        if data:
            for m in data.get("fixedProductMarketMakerCreations", []):
                title = (m.get("question", "") or "").split(SEP)[0].strip()
                market_info[m["id"]] = {
                    "title": title,
                    "currentAnswer": m.get("currentAnswer"),
                    "outcomes": m.get("outcomes"),
                }

    print(f"  Matched {len(market_info)} FPMMs to Omen markets")

    # Now cross-reference
    both_bet_and_resolved = 0
    bet_aligned_with_resolution = 0
    bet_opposed_to_resolution = 0

    print(f"\n  Markets where they BOTH bet AND resolved:")
    print(f"  {'Invested':>10} {'Resolved':>8} {'Q':>60}")
    print("  " + "-" * 85)

    for fpmm_addr in fpmm_contracts:
        info = market_info.get(fpmm_addr)
        if not info:
            continue
        title = info["title"]
        if title in resolutions:
            both_bet_and_resolved += 1
            invested = out_by_dest[fpmm_addr]["amount"]
            res_side = resolutions[title]["side"]
            print(f"  {invested:>9.4f} {res_side:>8}  {title[:60]}")

    print(f"\n  Markets where they bet AND resolved: {both_bet_and_resolved}")
    print(f"  Markets where they only bet: {len(fpmm_contracts) - both_bet_and_resolved}")
    print(f"  Markets where they only resolved: {len(resolutions) - both_bet_and_resolved}")

    # Accuracy on markets they bet on (using Omen resolution)
    print(f"\n  Betting accuracy (based on Omen resolution):")
    resolved_bets = 0
    won_bets = 0
    for fpmm_addr in fpmm_contracts:
        info = market_info.get(fpmm_addr)
        if not info or not info["currentAnswer"] or info["currentAnswer"] == INVALID:
            continue
        resolved_bets += 1
        # We can't easily determine which outcome they bought from transfer logs alone
        # but we know their resolution side matches the final answer when they resolved it

    print(f"  (Outcome side detection requires deeper ERC1155 analysis)")


# ---------------------------------------------------------------------------
# 5. Conditional token analysis
# ---------------------------------------------------------------------------

def analyze_conditional_tokens(w3, addr, from_block):
    print("\n" + "=" * 80)
    print("5. CONDITIONAL TOKEN ACTIVITY")
    print("=" * 80)

    # Check PayoutRedemption events — these show actual profit claims
    # PayoutRedemption(address indexed redeemer, bytes32 indexed collateralToken,
    #                  bytes32 indexed parentCollectionId, bytes32 conditionId,
    #                  uint256[] indexSets, uint256 payout)
    PAYOUT_SIG = w3.keccak(
        text="PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)"
    ).hex()
    addr_topic = "0x" + addr[2:].lower().zfill(64)

    latest = w3.eth.block_number
    logs = w3.eth.get_logs({
        "fromBlock": from_block,
        "toBlock": latest,
        "address": Web3.to_checksum_address(CONDITIONAL_TOKENS),
        "topics": [PAYOUT_SIG, addr_topic],
    })

    print(f"\n  PayoutRedemption events: {len(logs)}")

    total_redeemed = 0
    for l in logs:
        data = l["data"].hex() if isinstance(l["data"], bytes) else l["data"]
        if data.startswith("0x"):
            data = data[2:]
        # Payout is the last uint256 in the data
        # Layout: conditionId(32) + offset(32) + length(32) + indexSets... + payout
        # Actually the event data layout for dynamic types is complex
        # Just count the events and use the wxDAI transfer data for amounts
        pass

    # Use the wxDAI transfer data we already have
    TRANSFER_SIG = w3.keccak(text="Transfer(address,address,uint256)").hex()
    ct_topic = "0x" + CONDITIONAL_TOKENS[2:].lower().zfill(64)

    logs_from_ct = w3.eth.get_logs({
        "fromBlock": from_block,
        "toBlock": latest,
        "address": Web3.to_checksum_address(WXDAI),
        "topics": [TRANSFER_SIG, ct_topic, addr_topic],
    })

    total = 0
    redemptions = []
    for l in logs_from_ct:
        amt = int(l["data"].hex(), 16) / WEI
        total += amt
        block = w3.eth.get_block(l["blockNumber"])
        dt = datetime.fromtimestamp(block["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        redemptions.append({"dt": dt, "amount": amt, "block": l["blockNumber"]})

    print(f"  wxDAI from Conditional Tokens: {len(redemptions)} transfers, {total:.4f} xDAI")
    print(f"\n  Redemption timeline:")
    for r in redemptions:
        print(f"    {r['dt']}  {r['amount']:>10.4f} xDAI  block={r['block']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Analyze suspected oracle manipulator")
    parser.add_argument("address", help="Address to analyze (0x...)")
    parser.add_argument("--days", type=int, default=30, help="Lookback days")
    args = parser.parse_args()

    addr = args.address.lower()
    w3 = Web3(Web3.HTTPProvider(GNOSIS_RPC))

    if not w3.is_connected():
        print("Failed to connect to Gnosis RPC")
        return

    latest = w3.eth.block_number
    blocks_per_day = 17280  # ~5s per block
    from_block = latest - args.days * blocks_per_day

    print(f"Analyzing address: {addr}")
    print(f"Lookback: {args.days} days (from block {from_block})")
    print(f"Current block: {latest}")
    print(f"Connected: {w3.is_connected()}\n")

    # Run all analyses
    analyze_funding(w3, addr, from_block)
    fpmm_contracts, out_by_dest = analyze_betting(w3, addr, from_block)
    resolutions = analyze_resolutions(addr)
    cross_reference(w3, addr, fpmm_contracts, out_by_dest, resolutions, from_block)
    analyze_conditional_tokens(w3, addr, from_block)

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
