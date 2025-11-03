import os
from datetime import datetime

import dotenv
import requests

dotenv.load_dotenv()

ETHERSCAN_API_KEY = os.environ["ETHERSCAN_API_KEY"]
ADDRESS = "0x12a9a43b97985f160b1ca4f28b4bb8fe359aa21b"
CHAIN_ID = "100"  # Gnosis
# CHAIN_ID = "10"  # Optimism
LIMIT = 200


def eth_to_usd(eth_amount):
    # Fetch ETH price in USD
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": "ethereum", "vs_currencies": "usd"}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    price_usd = resp.json()["ethereum"]["usd"]

    # Convert
    usd_value = eth_amount * price_usd
    return usd_value


def get_transactions(address, chain_id, limit=200):
    url = "https://api.etherscan.io/v2/api"
    params = {
        "apikey": ETHERSCAN_API_KEY,
        "chainid": chain_id,
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        # "offset": limit,
        "sort": "desc",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    return data["result"]


if __name__ == "__main__":
    txs = get_transactions(ADDRESS, CHAIN_ID, LIMIT)
    fees = []
    for i, tx in enumerate(txs, start=1):

        value_eth = int(tx["value"]) / 1e18
        gas_price = int(tx["gasPrice"])
        gas_used = int(tx["gasUsed"])
        fee_eth = (gas_price * gas_used) / 1e18
        timestamp = datetime.utcfromtimestamp(int(tx["timeStamp"]))
        # print(
        #     f"{i}. {timestamp} | Hash: {tx['hash']} | From: {tx['from']} | To: {tx['to']} | Value: {value_eth:.6f} ETH | Fee: {fee_eth:.8f} ETH"
        # )
        fees.append(fee_eth)
        print(f"{i}. Fee: {fee_eth:.8f} ETH at {timestamp} | Hash: {tx['hash']}")

    average_fees = sum(fees) / len(fees)
    average_fees_usd = eth_to_usd(average_fees) if CHAIN_ID != "100" else average_fees

    print(
        f"Average Gas Fee for last {len(fees)} transactions: {sum(fees) / len(fees):.8f} ETH       {average_fees_usd:.8f} USD"
    )
