# Omen Fleet Analysis & Oracle Manipulation Investigation

Scripts for analyzing predict-omen agents on Gnosis Chain and investigating the March 2026 oracle manipulation attack.

See [OMEN_ORACLE_MANIPULATION_REPORT.md](OMEN_ORACLE_MANIPULATION_REPORT.md) for the full investigation report.

## Scripts

| Script | Description |
|--------|-------------|
| `analyze_omen_agent.py` | Single-agent diagnostics — tool usage, bet sizing, PnL, temporal trends |
| `analyze_omen_fleet_fast.py` | Fleet-wide analysis with concurrent mech fetching — tool profitability, agent leaderboard, weekly trends |
| `analyze_omen_profitability.py` | Tool & price range profitability across all agents |
| `analyze_omen_week_compare.py` | Before/after period comparison — identifies what changed (tools, sizing, outcome side) |
| `analyze_omen_large_bets.py` | Large vs small bet accuracy divergence analysis |
| `analyze_resolver.py` | Deep analysis of suspected oracle manipulator — funding, on-chain bets, Reality.io submissions, cross-reference |

## Usage

```bash
# Single agent
poetry run python omen/analyze_omen_agent.py 0x2aD146E33B27933241dd68eEb18E77d860ba361D --days 30

# Fleet-wide
poetry run python omen/analyze_omen_fleet_fast.py --days 30 --min-bets 5

# Week comparison (split at a date)
poetry run python omen/analyze_omen_week_compare.py --days 30 --split-date 2026-03-16

# Investigate a resolver address
poetry run python omen/analyze_resolver.py 0xc5fd24b2974743896e1e94c47e99d3960c7d4c96 --days 30
```

## Data Sources

- **predict-omen subgraph**: `api.subgraph.staging.autonolas.tech/api/proxy/predict-omen` — bets, market participants, trader agents
- **Gnosis mech marketplace**: `api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis` — mech requests, tool matching
- **Reality.io subgraph** (The Graph, ID `E7ymrCnNcQdAAgLbdFWzGE5mvr5Mb5T9VfT43FqA7bNh`): answer submissions, question responses
- **Gnosis RPC**: on-chain wxDAI transfers, conditional token redemptions
