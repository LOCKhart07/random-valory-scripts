"""Compare data-field encoding in mech-marketplace Request events.

Goal: confirm whether recent events use raw 34-byte multihash bytes
(0x12 0x20 || sha2-256) vs older events that used UTF-8 bytes of a CID string.
"""
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

GNOSIS_RPC = os.getenv("GNOSIS_RPC")
MARKETPLACE = Web3.to_checksum_address("0xb3c6319962484602b00d5587e965946890b82101")

FAILING_TX = "0x6cad0e1a1ab25010d351727c86fd30b9e8ac3f862c3d856e1b1b921fc1e9f139"
FAILING_BLOCK = 45653597
FAILING_REQ_ID = "0xb7dc053bb9e5a07dd8549a46a707f3bedcad6659513e0b044ee1d8d38ec6d130"
FAILING_REQUESTER = "0xF6631D7E76DfD3fe62971FE61F8aa985fe162aCA"

BLOCK_TIME = 5
MAX_SPAN = 20_000


def describe_data(raw: bytes):
    print(f"    length: {len(raw)} bytes")
    print(f"    hex:    0x{raw.hex()}")
    print(f"    first2: 0x{raw[:2].hex()}  "
          f"(sha2-256 multihash header = 0x1220)")
    try:
        s = raw.decode("utf-8")
        print(f"    utf-8:  {s!r}")
    except UnicodeDecodeError:
        print(f"    utf-8:  <not valid UTF-8>")


def dump_log(w3, log, label):
    tx = log["transactionHash"].hex() if hasattr(log["transactionHash"], "hex") else log["transactionHash"]
    blk = w3.eth.get_block(log["blockNumber"])
    ts = datetime.fromtimestamp(blk["timestamp"], tz=timezone.utc)
    print(f"\n=== {label} ===")
    print(f"  block:     {log['blockNumber']}  ({ts.isoformat()})")
    print(f"  tx:        {tx}")
    print(f"  topics:    {[t.hex() if hasattr(t,'hex') else t for t in log['topics']]}")
    data_bytes = log["data"] if isinstance(log["data"], bytes) else bytes.fromhex(log["data"][2:])
    print(f"  raw log.data length: {len(data_bytes)}")

    # Try both decodings: (bytes32,bytes) [olas_mech] and (uint256,uint256,bytes) etc.
    # We don't know marketplace ABI for sure — just show the tail bytes heuristically.
    # Last dynamic `bytes` arg: look for a 32-byte length near end.
    from eth_abi import decode
    for sig in [
        ["bytes32", "bytes"],
        ["address", "uint256", "bytes"],
        ["address", "bytes32", "bytes"],
        ["uint256", "bytes"],
        ["bytes"],
    ]:
        try:
            decoded = decode(sig, data_bytes)
            data_arg = decoded[-1]
            if isinstance(data_arg, bytes):
                print(f"  decoded with {sig}: data arg ({len(data_arg)} bytes)")
                describe_data(data_arg)
                return
        except Exception:
            pass
    print("  ❌ could not decode with any guessed ABI")


def main():
    w3 = Web3(Web3.HTTPProvider(GNOSIS_RPC))
    assert w3.is_connected(), "RPC down"

    # Signature candidates for marketplace
    event_sigs = [
        "Request(address,bytes32,bytes)",
        "MarketplaceRequest(address,address,uint256,bytes32,bytes)",
        "Request(address,address,bytes32,bytes)",
        "Request(address,uint256,bytes32,bytes)",
    ]

    # ---- 1. Inspect the failing tx directly ----
    print("### FAILING TX ###")
    receipt = w3.eth.get_transaction_receipt(FAILING_TX)
    print(f"  logs in tx: {len(receipt['logs'])}")
    for i, log in enumerate(receipt["logs"]):
        if log["address"].lower() != MARKETPLACE.lower():
            continue
        print(f"\n  -- log[{i}] from marketplace --")
        topics_hex = [t.hex() if hasattr(t, "hex") else t for t in log["topics"]]
        print(f"  topic0: {topics_hex[0]}")
        for sig in event_sigs:
            th = "0x" + Web3.keccak(text=sig).hex().lstrip("0x")
            if topics_hex[0].lower() == th.lower():
                print(f"  ✓ matches {sig}")
                break
        dump_log(w3, log, f"failing log[{i}]")

    # ---- 2. Older successful events ----
    latest = w3.eth.block_number
    # 45653597 is failing block. ~30 days ago on gnosis = 45653597 - 518400
    # ~90 days ago = 45653597 - 1555200
    probe_ranges = [
        ("~30d before failure", FAILING_BLOCK - 520_000, FAILING_BLOCK - 500_000),
        ("~60d before failure", FAILING_BLOCK - 1_040_000, FAILING_BLOCK - 1_020_000),
        ("~90d before failure", FAILING_BLOCK - 1_560_000, FAILING_BLOCK - 1_540_000),
    ]

    # Figure out which topic0 is actually used on the marketplace by sampling the failing tx.
    failing_topic0 = None
    for log in receipt["logs"]:
        if log["address"].lower() == MARKETPLACE.lower():
            t0 = log["topics"][0]
            failing_topic0 = t0.hex() if hasattr(t0, "hex") else t0
            # prefer a topic that also appears in historical blocks — take first for now
            break
    print(f"\nUsing topic0 from failing tx: {failing_topic0}")

    for label, start, end in probe_ranges:
        print(f"\n### {label}: blocks {start}→{end} ###")
        found = 0
        for s in range(start, end, MAX_SPAN):
            e = min(s + MAX_SPAN - 1, end)
            logs = w3.eth.get_logs({
                "fromBlock": s,
                "toBlock": e,
                "address": MARKETPLACE,
                "topics": [failing_topic0],
            })
            for log in logs:
                dump_log(w3, log, f"{label} log")
                found += 1
                if found >= 2:
                    break
            if found >= 2:
                break
        if found == 0:
            print(f"  no {failing_topic0} logs found in {label}")


if __name__ == "__main__":
    main()
