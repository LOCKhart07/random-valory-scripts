# Script to fetch specific events from a contract on Base, with date filters
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

# --- CONFIG ---
BASE_RPC = os.environ.get(
    "BASE_RPC", "https://mainnet.base.org"
)  # Set your Base RPC endpoint

print(f"Using Base RPC: {BASE_RPC}")
CONTRACT_ADDRESS = "0xe535D7AcDEeD905dddcb5443f41980436833cA2B"
EVENT_SIGNATURE_HASH = (
    "0x1ebd17f97038d3a14148566de635eab9901371bf904262f5498331b0c62921ce"
)


def get_block_by_timestamp(w3, target_ts, start_block=0, end_block=None):
    """
    Binary search to find the closest block to the given timestamp.
    """
    if end_block is None:
        end_block = w3.eth.get_block("latest").number
    while start_block < end_block:
        mid = (start_block + end_block) // 2
        block = w3.eth.get_block(mid)
        if block.timestamp < target_ts:
            start_block = mid + 1
        else:
            end_block = mid
    return start_block


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch contract events for a given day."
    )
    parser.add_argument(
        "--days", type=int, default=1, help="Number of days to look back (default: 1)"
    )
    parser.add_argument("--from-date", type=str, help="Start date (YYYY-MM-DD) UTC")
    parser.add_argument("--to-date", type=str, help="End date (YYYY-MM-DD) UTC")
    args = parser.parse_args()

    w3 = Web3(Web3.HTTPProvider(BASE_RPC))

    if not w3.is_connected(show_traceback=True):
        print("Failed to connect to Base RPC.")
        return

    now = datetime.now(timezone.utc)
    if args.from_date:
        from_dt = datetime.strptime(args.from_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    else:
        from_dt = now - timedelta(days=args.days)
    if args.to_date:
        to_dt = datetime.strptime(args.to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        to_dt = now

    from_ts = int(from_dt.timestamp())
    to_ts = int(to_dt.timestamp())

    print(f"Finding blocks for {from_dt} to {to_dt} (UTC)...")
    start_block = get_block_by_timestamp(w3, from_ts)
    end_block = get_block_by_timestamp(w3, to_ts)
    print(f"Block range: {start_block} to {end_block}")

    # Paginate logs in 5000-block chunks
    logs = []
    chunk_size = 5000
    current_block = start_block
    while current_block < end_block:
        chunk_end = min(current_block + chunk_size - 1, end_block)
        try:
            chunk_logs = w3.eth.get_logs(
                {
                    "address": Web3.to_checksum_address(CONTRACT_ADDRESS),
                    "topics": [EVENT_SIGNATURE_HASH],
                    "fromBlock": current_block,
                    "toBlock": chunk_end,
                }
            )
            logs.extend(chunk_logs)
        except Exception as e:
            print(f"Error fetching logs for blocks {current_block}-{chunk_end}: {e}")
        current_block = chunk_end + 1

    print(f"Found {len(logs)} events.")
    for log in logs:
        block = w3.eth.get_block(log["blockNumber"])
        block_time = datetime.fromtimestamp(block.timestamp, timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        print(f"Block: {log['blockNumber']} | Time: {block_time} UTC")
        print(f"Tx: {log['transactionHash'].hex()}")
        # Decode requestId and data from log['data']
        data_bytes = bytes(log["data"])
        request_id_bytes, raw_data_bytes = w3.codec.decode(["bytes32", "bytes"], data_bytes)
        requestId = request_id_bytes.hex().upper()
        data_field = raw_data_bytes.hex().upper()
        print(f"requestId (bytes32): {requestId}")
        print(f"data (bytes): {data_field}")
        print(f"link: https://gateway.autonolas.tech/ipfs/f01701220{str(data_field)}")
        # Decode mech address from topics[1]
        if len(log["topics"]) > 1:
            mech_address = Web3.to_checksum_address("0x" + log["topics"][1].hex()[-40:])
            print(f"mech (address): {mech_address}")
        print(f"Topics: {log['topics']}")
        print("-" * 40)


if __name__ == "__main__":
    main()
