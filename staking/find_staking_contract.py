"""
Look up which Olas staking contract a Safe (service multisig) is staked on.

Given a Safe address on Gnosis Chain, walks the Olas staking subgraph to find:
  1. The service ID whose multisig is this Safe
  2. The latest staking contract that service is staked on
  3. The staking contract's metadata (name, slots, rewards/sec, min stake)

Resolves the staking contract's metadataHash via the Olas IPFS gateway to
recover the human-readable contract name.

Usage:
    poetry run python staking/find_staking_contract.py 0x225dda312935a005A414A45D80470706a9873658
"""

import argparse
import json
import sys

import requests

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

STAKING_SUBGRAPH = "https://staking-gnosis.subgraph.autonolas.tech"
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs"

TIMEOUT = 30


# ---------------------------------------------------------------------------
# Subgraph helpers
# ---------------------------------------------------------------------------

def gql(query: str) -> dict:
    r = requests.post(STAKING_SUBGRAPH, json={"query": query}, timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(f"subgraph error: {body['errors']}")
    return body["data"]


def find_service_id_by_multisig(multisig: str) -> dict | None:
    """Find the most recent ServiceStaked event for a given multisig."""
    multisig = multisig.lower()
    data = gql(f"""
    {{
      serviceStakeds(
        where: {{multisig: "{multisig}"}}
        orderBy: blockTimestamp
        orderDirection: desc
        first: 1
      ) {{
        serviceId
        multisig
        owner
        epoch
        blockTimestamp
        transactionHash
      }}
    }}
    """)
    rows = data.get("serviceStakeds", [])
    return rows[0] if rows else None


def get_service(service_id: str) -> dict | None:
    data = gql(f"""
    {{
      service(id: "{service_id}") {{
        id
        latestStakingContract
        currentOlasStaked
        olasRewardsEarned
        olasRewardsClaimed
        totalEpochsParticipated
      }}
    }}
    """)
    return data.get("service")


def get_staking_contract(address: str) -> dict | None:
    address = address.lower()
    data = gql(f"""
    {{
      stakingContract(id: "{address}") {{
        id
        instance
        implementation
        metadataHash
        maxNumServices
        rewardsPerSecond
        minStakingDeposit
        minStakingDuration
        livenessPeriod
        numAgentInstances
        threshold
        serviceRegistry
        activityChecker
      }}
    }}
    """)
    return data.get("stakingContract")


# ---------------------------------------------------------------------------
# IPFS metadata
# ---------------------------------------------------------------------------

def resolve_metadata(metadata_hash: str) -> dict | None:
    """Olas stores metadata as f0170122 + 32-byte hash on IPFS."""
    if metadata_hash.startswith("0x"):
        metadata_hash = metadata_hash[2:]
    cid = f"f01701220{metadata_hash}"
    url = f"{IPFS_GATEWAY}/{cid}"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

OLAS = 10 ** 18
SECONDS_PER_DAY = 86_400


def fmt_olas(wei: str | int) -> str:
    return f"{int(wei) / OLAS:,.4f} OLAS"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("multisig", help="Safe / service multisig address on Gnosis")
    args = p.parse_args()

    safe = args.multisig
    print(f"Multisig: {safe}\n")

    staked = find_service_id_by_multisig(safe)
    if not staked:
        print("No ServiceStaked event found for this multisig on the Gnosis staking subgraph.")
        print("(Either it's not an Olas service multisig, or it has never been staked.)")
        return 1

    service_id = staked["serviceId"]
    print(f"Service ID:        {service_id}")
    print(f"Owner:             {staked['owner']}")
    print(f"Last staked tx:    {staked['transactionHash']}")
    print(f"Last staked epoch: {staked['epoch']}")
    print(f"Last staked at:    {staked['blockTimestamp']} (unix)\n")

    service = get_service(service_id)
    if not service or not service.get("latestStakingContract"):
        print("Service has no latestStakingContract — may have been unstaked.")
        return 1

    staking_addr = service["latestStakingContract"]
    print(f"Currently staked:  {fmt_olas(service['currentOlasStaked'])}")
    print(f"Rewards earned:    {fmt_olas(service['olasRewardsEarned'])}")
    print(f"Rewards claimed:   {fmt_olas(service['olasRewardsClaimed'])}")
    print(f"Epochs:            {service['totalEpochsParticipated']}\n")

    contract = get_staking_contract(staking_addr)
    if not contract:
        print(f"StakingContract entity not found for {staking_addr}")
        return 1

    print(f"Staking contract:  {contract['instance']}")
    print(f"Implementation:    {contract['implementation']}")
    print(f"Slots:             {contract['maxNumServices']}")
    print(f"Min stake:         {fmt_olas(contract['minStakingDeposit'])}")
    rps = int(contract["rewardsPerSecond"])
    print(f"Rewards/sec:       {rps} wei  ({rps * SECONDS_PER_DAY / OLAS:,.4f} OLAS/day)")
    print(f"Liveness period:   {contract['livenessPeriod']} s")
    print(f"Activity checker:  {contract['activityChecker']}\n")

    metadata = resolve_metadata(contract["metadataHash"])
    if metadata:
        print(f"Name:        {metadata.get('name', '<no name>')}")
        desc = metadata.get("description", "")
        if desc:
            print(f"Description: {desc}")
    else:
        print(f"(metadata at hash {contract['metadataHash']} could not be resolved)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
