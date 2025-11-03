from web3 import Web3
from dotenv import load_dotenv
import os

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

LOG_FILE = "deliver_events.log"


def find_tx_by_request_id(request_id_to_find: int, days=7, contract_address=None):
    if contract_address is None:
        raise ValueError("Please provide a contract address.")
    contract_address = Web3.to_checksum_address(contract_address)
    w3 = Web3(Web3.HTTPProvider(GNOSIS_RPC))
    if not w3.is_connected():
        print("❌ Could not connect to Gnosis RPC.")
        return

    latest_block = w3.eth.block_number
    blocks_per_day = int((24 * 60 * 60) / BLOCK_TIME_SECONDS)
    from_block = max(latest_block - (blocks_per_day * days), 0)

    print(f"🔎 Searching Deliver events for requestId={request_id_to_find}")
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
            # Decode data: bytes32 requestId, uint256 deliveryRate, bytes data
            data_bytes = bytes(log["data"])  # <--- FIX

            request_id = data_bytes[:32].hex()

            request_data = data_bytes[64:].hex()  # raw bytes data as hex
            with open(LOG_FILE, "a") as f:
                f.write(
                    f"{request_id} : http://gateway.autonolas.tech/ipfs/f01701220{request_data.replace('00000000000000000000000000000000000000000000000000000000000000600000000000000000000000000000000000000000000000000000000000000020','')}/{int(request_id, 16)}\n"
                )

            request_id_to_check = int(request_id, 16)

            print(f"Checking {request_id_to_check=} against {request_id_to_find=}")

            if request_id_to_check == request_id_to_find:
                print("\n✅ Found matching Deliver log:")
                print(f"Tx hash: {log['transactionHash'].hex()}")
                print(f"Block: {log['blockNumber']}")
                print(f"requestId: 0x{request_id}")
                return

    print("\n❌ No Deliver event found for that requestId in the last week.")


if __name__ == "__main__":
    # Clean up log file
    with open(LOG_FILE, "w") as f:
        f.write("")
    request_id = (
        115051242748997020425914349411256490133983050893051385058019894386414237620092
    )
    contract_address = "0x601024E27f1C67B28209E24272CED8A31fc8151F"
    days = 7
    find_tx_by_request_id(request_id, days, contract_address)
