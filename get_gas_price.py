import os
from datetime import datetime

import dotenv
import requests

dotenv.load_dotenv()

ETHERSCAN_API_KEY = os.environ["ETHERSCAN_API_KEY"]
ADDRESS = "0x12A9a43b97985F160B1ca4F28B4bb8fe359Aa21b"
# ADDRESS = "0x350817A0aE17FA392d9aBf4AD438407521cB23AD"
CHAIN_ID = "100"  # Gnosis
# CHAIN_ID = "10"  # Optimism
# CHAIN_ID = "8453"  # Base
# CHAIN_ID = "137"  # Polygon
LIMIT = 500


def get_native_token_price_usd(chain_id):
    """Get the native token price in USD for the given chain."""
    # Map chain IDs to CoinGecko token IDs
    token_map = {
        "1": "ethereum",  # Ethereum
        "10": "ethereum",  # Optimism (uses ETH)
        "100": "xdai",  # Gnosis (uses xDAI, which is ~$1)
        "137": "polygon-ecosystem-token",  # Polygon (uses POL, formerly MATIC)
        "8453": "ethereum",  # Base (uses ETH)
    }

    token_id = token_map.get(chain_id, "ethereum")

    # Fetch token price in USD
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": token_id, "vs_currencies": "usd"}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    price_usd = resp.json()[token_id]["usd"]

    return price_usd


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
    token_price_usd = get_native_token_price_usd(CHAIN_ID)
    average_fees_usd = average_fees * token_price_usd

    # Get native token name for display
    token_names = {
        "1": "ETH",
        "10": "ETH",
        "100": "xDAI",
        "137": "POL",
        "8453": "ETH",
    }
    token_name = token_names.get(CHAIN_ID, "ETH")

    print(
        f"Average Gas Fee for last {len(fees)} transactions: {average_fees:.8f} {token_name}       {average_fees_usd:.8f} USD"
    )
