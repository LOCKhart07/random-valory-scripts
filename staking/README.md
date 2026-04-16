# Olas Staking Lookups

Scripts that query the Olas staking subgraph on Gnosis Chain.

## Scripts

| Script | Description |
|--------|-------------|
| `find_staking_contract.py` | Given a Safe / service multisig address, find its service ID, the staking contract it's currently staked on, and resolve the contract's IPFS metadata to a human name |
| `investigate_predict_860.py` | PREDICT-860 — onchain investigation for a Pearl Trader Safe burning xDAI/wxDAI without meeting staking KPI: classifies outflows (bets vs mech fees vs other) and counts mech requests per epoch against the activity-checker liveness ratio |

## Usage

```bash
poetry run python staking/find_staking_contract.py 0x225dda312935a005A414A45D80470706a9873658
```

Output includes service ID, current OLAS staked, rewards earned/claimed, epochs participated, the staking contract address & implementation, slot count, min stake, rewards/sec, and the IPFS-resolved staking program name (e.g. "Quickstart Beta Mech MarketPlace - Expert 5").

## Data Sources

- **Olas staking subgraph (Gnosis)**: `https://staking-gnosis.subgraph.autonolas.tech` — `serviceStakeds`, `service`, `stakingContract` entities
- **IPFS gateway**: `https://gateway.autonolas.tech/ipfs/f01701220<metadataHash>` — staking contract name/description JSON

## Lookup pattern

Three subgraph queries chain together to go from a multisig to the staking program name:

1. `serviceStakeds(where: {multisig: <safe>}, orderBy: blockTimestamp, orderDirection: desc, first: 1)` → `serviceId`
2. `service(id: <serviceId>)` → `latestStakingContract`
3. `stakingContract(id: <addr>)` → `metadataHash`, then resolve via IPFS gateway with the `f01701220` CID prefix
