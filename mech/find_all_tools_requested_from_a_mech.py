import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from collections import defaultdict

import requests
from dotenv import load_dotenv
from eth_abi import decode
from web3 import Web3

# -------------------------------
# CONFIG
# -------------------------------
load_dotenv()

GNOSIS_RPC = os.getenv("GNOSIS_RPC")
if not GNOSIS_RPC:
    raise ValueError("Please set the GNOSIS_RPC environment variable.")

EVENT_SIGNATURE = "Request(address,bytes32,bytes)"
EVENT_TOPIC = Web3.keccak(text=EVENT_SIGNATURE).hex()

# Average block time on Gnosis (≈5s)
BLOCK_TIME_SECONDS = 5
MAX_BLOCK_SPAN = 20_000  # Max per getLogs call

LOG_FILE = "request_events.log"

# -------------------------------
# HELPER FUNCTIONS
# -------------------------------


def ipfs_request(ipfs_link: str):
    response = requests.get(ipfs_link, timeout=10)
    response.raise_for_status()  # raise if status code is not 200
    data = response.json()
    return data.get("tool")


@lru_cache(maxsize=None)
def fetch_tool_from_ipfs(request_data_hex: str):
    base_ipfs_link = f"http://gateway.autonolas.tech/ipfs/f01701220{request_data_hex}"
    urls_to_try = [f"{base_ipfs_link}/metadata.json", base_ipfs_link]

    for url in urls_to_try:
        try:
            tool = ipfs_request(url)
            if tool:
                return tool
        except Exception as e:
            continue  # silently try next URL

    print(f"❌ Failed to fetch tool from IPFS: {base_ipfs_link}")
    return None


def get_all_tool_ids(days=7, contract_address=None, from_block=None, max_workers=20):
    if contract_address is None:
        raise ValueError("Please provide a contract address.")

    contract_address = Web3.to_checksum_address(contract_address)
    w3 = Web3(Web3.HTTPProvider(GNOSIS_RPC))
    if not w3.is_connected():
        print("❌ Could not connect to Gnosis RPC.")
        return set()

    latest_block = w3.eth.block_number
    blocks_per_day = int((24 * 60 * 60) / BLOCK_TIME_SECONDS)
    if from_block is None:
        from_block = max(latest_block - (blocks_per_day * days), 0)

    print(f"🔎 Searching Request events for {contract_address=}")
    print(f"⏱  From block {from_block} → {latest_block} (≈ last {days} days)\n")

    all_tools_requested_for = defaultdict(int)

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

        if not logs:
            continue

        # Fetch IPFS in parallel
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    fetch_tool_from_ipfs,
                    decode(["bytes32", "bytes"], log["data"])[1].hex(),
                ): log
                for log in logs
            }

            for future in as_completed(futures):
                tool = future.result()
                if tool:
                    all_tools_requested_for[tool] += 1

    return all_tools_requested_for


# -------------------------------
# MAIN
# -------------------------------
if __name__ == "__main__":
    # Clean up log file
    with open(LOG_FILE, "w") as f:
        f.write("")

    contract_address = "0xC05e7412439bD7e91730a6880E18d5D5873F632C"
    days = 3
    requested_tools = get_all_tool_ids(
        days, contract_address, from_block=None, max_workers=20
    )

    print(
        f"\n✅ Found {len(requested_tools)} unique tools requested in the last {days} days:\n"
    )
    for tool, calls in requested_tools.items():
        print(f"- {tool} called {calls} time(s)")