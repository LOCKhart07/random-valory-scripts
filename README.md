# Olas Ecosystem Analysis Scripts

Utility scripts for analyzing the [Autonolas/Olas](https://olas.network/) ecosystem — mech marketplace monitoring, tool accuracy statistics, Polymarket agent performance, Omen fleet analysis, and market creation tracking.

## Setup

```bash
poetry install
```

Create a `.env` file (see `.env.example` fields below):

| Variable | Used by |
|----------|---------|
| `GNOSIS_RPC` | mech, omen, market-creator scripts |
| `BASE_RPC` | mech scripts (Base chain) |
| `POLYGON_RPC` | polymarket scripts |
| `ETHERSCAN_API_KEY` | gas price lookups |
| `THE_GRAPH_API_KEY` | PolyStrat agent registry queries |
| `SUBGRAPH_API_KEY` | Omen market queries (The Graph) |
| `GENAI_API_KEY` | Google GenAI API (mech analysis) |

## Running Scripts

All scripts are standalone: `poetry run python <path/to/script>.py`. Most accept CLI args via argparse — run with `--help` for options.

## Directory Structure

| Directory | Description |
|-----------|-------------|
| [`mech/`](mech/) | Mech marketplace monitoring — request/deliver events, IPFS checks, usage timelines |
| [`polymarket/`](polymarket/) | PolyStrat agent analysis — diagnostics, fleet profitability, divergence, persistence |
| [`omen/`](omen/) | Omen fleet analysis — agent diagnostics, fleet trends, oracle manipulation investigation |
| [`tool-accuracy/`](tool-accuracy/) | Tool accuracy statistics, bar charts, timelines, CSV generation |
| [`market-creator/`](market-creator/) | Reality.io market event watcher on Gnosis Chain |
| [`staking/`](staking/) | Olas staking subgraph lookups — find staking contract by service multisig |
| `chatui/` | HTTP latency tester for configure_strategies endpoint |

See each directory's README for detailed script descriptions.

## Reports

| Report | Description |
|--------|-------------|
| [`tool-accuracy/FULL_TOOL_ANALYSIS_REPORT.md`](tool-accuracy/FULL_TOOL_ANALYSIS_REPORT.md) | Full tool accuracy analysis across OmenStrat & PolyStrat — CIs, cross-platform comparison, anti-predictive tools, recommendations |
| [`tool-accuracy/SUPERFORCASTER_ACCURACY_REPORT.md`](tool-accuracy/SUPERFORCASTER_ACCURACY_REPORT.md) | Superforcaster accuracy trend investigation — is it degrading or converging? (statistical significance tests) |
| [`polymarket/POLYSTRAT_DIVERGENCE_REPORT.md`](polymarket/POLYSTRAT_DIVERGENCE_REPORT.md) | PolyStrat fleet divergence analysis — why agents perform differently |
| [`omen/OMEN_ORACLE_MANIPULATION_REPORT.md`](omen/OMEN_ORACLE_MANIPULATION_REPORT.md) | Omen oracle manipulation investigation — market creator vulnerability |
| [`omen/ZD919_INVALID_MARKETS_REPORT.md`](omen/ZD919_INVALID_MARKETS_REPORT.md) | ZD#919 — on-chain proof that 7 "invalid" Omen markets are pending Reality.eth finalization, not invalid |
| [`mech/MECH_IPFS_ABUSE_REPORT.md`](mech/MECH_IPFS_ABUSE_REPORT.md) | Mech IPFS abuse report — deliver gateway issues |
| [`mech/source_content_impact_report.md`](mech/source_content_impact_report.md) | source_content IPFS overhead — daily request volumes and storage projections across Gnosis + Polygon |

## Data Sources

- **Subgraphs**: Autonolas subgraph proxies (`api.subgraph.autonolas.tech`) for mech-marketplace (Gnosis + Polygon) and predict-omen/predict-polymarket
- **The Graph**: Omen markets (`9fUVQpFwz...`), Reality.io (`E7ymrCnNc...`), Polygon service registry
- **IPFS**: `gateway.autonolas.tech` for mech request/delivery metadata
- **RPC**: Direct chain queries via Web3.py for event logs
