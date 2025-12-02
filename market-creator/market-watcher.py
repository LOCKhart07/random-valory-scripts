import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import dotenv
from web3 import Web3
from web3._utils.events import event_abi_to_log_topic

dotenv.load_dotenv()

RPC_URL = os.getenv("GNOSIS_RPC", "https://rpc.gnosischain.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

FACTORY = Web3.to_checksum_address("0x9083A2B699c0a4AD06F63580BDE2635d26a3eeF0")
REALITIO = Web3.to_checksum_address("0x79e32aE03fb27B07C89c0c568F80287C01ca2E57")

DAYS = 14  # scan last N days

CREATOR_TO_TRACK = "0x89c5cc945dd550BcFfb72Fe42BfF002429F46Fec"  # QS
# CREATOR_TO_TRACK = "0xFfc8029154ECD55ABED15BD428bA596E7D23f557"  # Pearl
# -------------------------------------------------------------------
# ABIs (minimal)
# -------------------------------------------------------------------

FPMM_EVENT_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "creator", "type": "address"},
            {"indexed": False, "name": "fixedProductMarketMaker", "type": "address"},
            {"indexed": False, "name": "conditionalTokens", "type": "address"},
            {"indexed": False, "name": "collateralToken", "type": "address"},
            {"indexed": False, "name": "conditionIds", "type": "bytes32[]"},
            {"indexed": False, "name": "fee", "type": "uint256"},
        ],
        "name": "FixedProductMarketMakerCreation",
        "type": "event",
    }
]
REALITIO_EVENT_ABI = json.loads(open("./market-creator/realitio_abi.json").read())

# -------------------------------------------------------------------
# Event objects & topic0s
# -------------------------------------------------------------------

factory_contract = w3.eth.contract(address=FACTORY, abi=FPMM_EVENT_ABI)
fpmm_event = factory_contract.events.FixedProductMarketMakerCreation()
fpmm_topic = event_abi_to_log_topic(fpmm_event._get_event_abi())

realitio_contract = w3.eth.contract(address=REALITIO, abi=REALITIO_EVENT_ABI)
realitio_event = realitio_contract.events.LogNewQuestion()
realitio_topic = event_abi_to_log_topic(realitio_event._get_event_abi())


# -------------------------------------------------------------------
# Convert timestamp → block using binary search
# -------------------------------------------------------------------


def block_by_timestamp(target_ts):
    low, high = 1, w3.eth.block_number
    while low < high:
        mid = (low + high) // 2
        ts = w3.eth.get_block(mid).timestamp
        if ts < target_ts:
            low = mid + 1
        else:
            high = mid
    return low


now_ts = w3.eth.get_block("latest").timestamp
start_ts = now_ts - DAYS * 86400

FROM_BLOCK = block_by_timestamp(start_ts)
TO_BLOCK = w3.eth.block_number

print(f"Scanning last {DAYS} days → blocks {FROM_BLOCK} → {TO_BLOCK}")


# -------------------------------------------------------------------
# Batch eth_getLogs
# -------------------------------------------------------------------


def batch_get_logs(address, topic0, start, end, batch=20000):
    logs = []
    cur = start
    while cur <= end:
        stop = min(cur + batch - 1, end)
        print(f"  → fetching logs {cur} → {stop}")
        params = {"fromBlock": cur, "toBlock": stop, "topics": [topic0]}
        if address:
            params["address"] = address

        try:
            logs.extend(w3.eth.get_logs(params))
        except Exception as e:
            print("RPC error:", e)

        cur = stop + 1
    return logs


# -------------------------------------------------------------------
# 1) Fetch all Realitio questions in last N days
# -------------------------------------------------------------------

raw_q_logs = batch_get_logs(
    address=[REALITIO],
    topic0=realitio_topic,
    start=FROM_BLOCK,
    end=TO_BLOCK,
)

questions = [realitio_event.process_log(log) for log in raw_q_logs]

# Map question_id → question text + timestamp
question_map = {}
for q in questions:
    qid_hex = q["args"]["question_id"].hex()
    question_map[qid_hex] = {
        "question": q["args"]["question"],
        "created": q["args"]["created"],
    }


print(f"\n🟩 Found {len(question_map)} questions")


# -------------------------------------------------------------------
# 2) Fetch all markets from factory in last N days
# -------------------------------------------------------------------

raw_m_logs = batch_get_logs(
    address=[FACTORY],
    topic0=fpmm_topic,
    start=FROM_BLOCK,
    end=TO_BLOCK,
)

markets = [fpmm_event.process_log(log) for log in raw_m_logs]
print(f"🟦 Found {len(markets)} markets\n")


# -------------------------------------------------------------------
# Helper: Convert timestamp → IST
# -------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def to_ist(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M:%S IST")


def to_utc(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


# -------------------------------------------------------------------
# Print results with mapping
# -------------------------------------------------------------------

print("==============================================")
print("      MARKETS (mapped to questions)")
print("==============================================\n")

no_of_markets = 0
markets_per_day = defaultdict(int)

for m in markets:
    args = m["args"]
    market_addr = args["fixedProductMarketMaker"]
    creator = args["creator"]
    if creator.lower() != CREATOR_TO_TRACK.lower():
        continue
    condition_ids = args["conditionIds"]

    # For deterministic markets, 1 conditionId → 1 questionId
    question_id = condition_ids[0]
    question_id_hex = question_id.hex()
    q = question_map.get(question_id_hex)
    question_text = q["question"] if q else "(No question found)"

    # created_ts = q["created_utc"] if q else None
    # created_ist = to_ist(created_ts) if created_ts else "N/A"

    tx_hash = m["transactionHash"]
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    block = w3.eth.get_block(receipt["blockNumber"])
    created_ts = block.timestamp
    # created_ist = to_ist(created_ts)
    # created_utc = to_utc(created_ts)
    date = to_utc(created_ts).split(" ")[0]

    print("----------------------------------------------")
    print(f"Market:     {market_addr}")
    print(f"Creator:    {creator}")
    print(f"Question:   {question_text}")
    # print(f"Timestamp:  {created_ist}\t|\t{created_utc}")
    print(f"Date:       {date}")
    print()

    no_of_markets += 1
    markets_per_day[date] += 1

print(f"Total markets by {CREATOR_TO_TRACK}: {no_of_markets}")

print("\n==============================================")
print(" Markets created per day")
print("==============================================")
for day, count in sorted(markets_per_day.items()):
    print(f"{day}: {count}")
