import os

from dotenv import load_dotenv
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

LOG_FILE = "deliver_events.log"


def normalize_request_id(value) -> bytes:
    """
    Normalize requestId to raw 32-byte value.
    Accepts:
    - int
    - hex str (with or without 0x)
    - bytes
    """
    if isinstance(value, bytes):
        if len(value) != 32:
            raise ValueError("bytes requestId must be 32 bytes")
        return value

    if isinstance(value, int):
        return value.to_bytes(32, byteorder="big")

    if isinstance(value, str):
        hex_str = value.lower().replace("0x", "")
        return bytes.fromhex(hex_str.zfill(64))

    raise TypeError("Unsupported requestId type")


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
            data_bytes = bytes(log["data"])

            request_id = data_bytes[:32].hex()

            request_data = data_bytes[64:].hex()  # raw bytes data as hex
            with open(LOG_FILE, "a") as f:
                f.write(
                    f"{request_id} : http://gateway.autonolas.tech/ipfs/f01701220{request_data.replace('00000000000000000000000000000000000000000000000000000000000000600000000000000000000000000000000000000000000000000000000000000020', '')}/{int(request_id, 16)}\n"
                )

            request_id_to_check = int(request_id, 16)
            request_id_to_find = normalize_request_id(request_id_to_find)

            print(
                f"Checking {request_id_to_check=} {request_id=} against {request_id_to_find=}"
            )

            data_bytes = bytes(log["data"])
            request_id_bytes = data_bytes[:32]

            if request_id_bytes == request_id_to_find:
                print("\n✅ Found matching Deliver log:")
                print(f"Tx hash: 0x{log['transactionHash'].hex()}")
                print(f"Block: {log['blockNumber']}")
                print(f"requestId: 0x{request_id}")
                # print(f"log data: {log['data']}")
                return

    print("\n❌ No Deliver event found for that requestId in the last week.")


if __name__ == "__main__":
    # Clean up log file
    with open(LOG_FILE, "w") as f:
        f.write("")
    request_id = "93F6C63A51A7ACD66007AF3F704F3A1E38205B96A54A9AEDEA7639CA914BFE46"

    contract_address = "0xdb78159e9246EC738F51c2c9cb1169b5C0e45fee"
    days = 3
    find_tx_by_request_id(request_id, days, contract_address)
