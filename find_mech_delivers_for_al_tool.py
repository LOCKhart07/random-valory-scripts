import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from eth_abi import decode
from web3 import Web3

# -------------------------------
# CONFIG
# -------------------------------
load_dotenv()

GNOSIS_RPC = os.getenv("GNOSIS_RPC")


# Deliver(address mech, address mechServiceMultisig, bytes32 requestId, uint256 deliveryRate, bytes data)
EVENT_SIGNATURE = "Deliver(address,address,bytes32,uint256,bytes)"
EVENT_TOPIC = Web3.keccak(text=EVENT_SIGNATURE).hex()


# Average block time on Gnosis (≈5s)
BLOCK_TIME_SECONDS = 5
MAX_BLOCK_SPAN = 20_000  # Gnosis RPC max per getLogs call

LOG_FILE = "request_events.log"


def find_tx_by_request_id(
    days=7, contract_address=None, from_block=None, tool_to_find=None
):
    if contract_address is None:
        raise ValueError("Please provide a contract address.")
    contract_address = Web3.to_checksum_address(contract_address)
    w3 = Web3(Web3.HTTPProvider(GNOSIS_RPC))
    if not w3.is_connected():
        print("❌ Could not connect to Gnosis RPC.")
        return

    latest_block = w3.eth.block_number
    blocks_per_day = int((24 * 60 * 60) / BLOCK_TIME_SECONDS)
    if from_block is None:
        from_block = max(latest_block - (blocks_per_day * days), 0)

    print(f"🔎 Searching Requests events")
    print(f"⏱  From block {from_block} → {latest_block} (≈ last {days} days)\n")

    # Search in chunks of 20k blocks
    for start in range(from_block, latest_block, MAX_BLOCK_SPAN):
        end = min(start + MAX_BLOCK_SPAN - 1, latest_block)
        print(f"📦 Checking blocks {start} → {end} ...", end="\r")

        logs = w3.eth.get_logs(
            {
                "fromBlock": start,
                "toBlock": end,
                "address": contract_address,
                "topics": [EVENT_TOPIC],
            }
        )

        for log in logs:
            decoded = decode(["bytes32", "uint256", "bytes"], log["data"])
            request_id, delivery_rate, request_data = decoded
            ipfs_link = f"http://gateway.autonolas.tech/ipfs/f01701220{request_data.hex()}/{int(request_id.hex(), 16)}"
            result = requests.get(ipfs_link)
            try:
                result_json = result.json()
            except Exception as e:
                print(f"❌ Could not decode JSON from IPFS link: {ipfs_link}")
                # if 'no link named "metadata.json"' in result.text:
                # print(f"CHECK THIS LINK MANUALLY {ipfs_link}")

                continue
            # print(result["tool"])
            if result_json.get("tool") != tool_to_find:
                continue
            blk = w3.eth.get_block(log["blockNumber"])
            ts = blk["timestamp"]
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            print(
                f"⏱  Tx {log['transactionHash'].hex()} - Block {log['blockNumber']} - {dt.isoformat()}"
            )


if __name__ == "__main__":
    # Clean up log file
    with open(LOG_FILE, "w") as f:
        f.write("")

    contract_address = "0xdb78159e9246EC738F51c2c9cb1169b5C0e45fee"
    days = 1
    tool_to_find = "resolve-market-reasoning-gpt-4.1"
    find_tx_by_request_id(
        days, contract_address, from_block=None, tool_to_find=tool_to_find
    )
