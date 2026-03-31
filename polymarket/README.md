# PolyStrat Agent Analysis

Analysis scripts for Polymarket PolyStrat prediction agents (agent ID 86) on Polygon chain.

## Scripts

| Script | Description |
|--------|-------------|
| `analyze_agent.py` | Single-agent diagnostic — accuracy, tools, bet sizing, temporal trends |
| `analyze_agent_deep.py` | Deep single-agent vs fleet comparison |
| `analyze_divergence.py` | Fleet divergence analysis — hypotheses about performance differences |
| `analyze_fleet.py` | Fleet-wide profitability across all registered agents |
| `analyze_persistence.py` | Path persistence testing (quartile stickiness, PnL streaks) |
| `analyze_persistence_deep.py` | Deep persistence with 6 hypothesis tests |
| `analyze_price_ranges.py` | Accuracy and profitability bucketed by share price ranges |
| `analyze_tool_profitability.py` | Per-tool PnL and ROI analysis |
| `analyze_tool_usage.py` | Tool usage patterns across fleet (popularity, trends, adoption) |
| `analyze_tool_usage_granular.py` | Fine-grained tool usage with time-binned trends |
| `generate_accuracy_csv.py` | Generate tools accuracy CSV from on-chain Polymarket data |
| `get_polymarket_agents_accuracy_and_roi.py` | Fetch all agents, generate profitability report |
| `verify_lockin.py` | Verify accuracy store lock-in behavior |
| `verify_tool_pnl_claims.py` | Verify tool PnL claims against on-chain data |
| `analyze_daily_activity.py` | Per-day betting activity metrics — bet counts, sizing, active agents |
| `analyze_poly_week_compare.py` | Before/after comparison of fleet metrics around a deploy or change date |
| `server.py` | FastAPI server for agent analysis (HMAC-authenticated) |

The directory also contains Jupyter notebooks for Safe management, Polymarket operations, and redemption workflows.

## Usage

```bash
# Single agent analysis
poetry run python polymarket/analyze_agent.py 0x33d20338f1700eda034ea2543933f94a2177ae4c

# Fleet analysis
poetry run python polymarket/analyze_fleet.py

# Tool usage
poetry run python polymarket/analyze_tool_usage.py --days 30
```

## Data Sources

- **predict-polymarket subgraph**: `predict-polymarket-agents.subgraph.autonolas.tech` — bets, market participants
- **Polygon mech marketplace**: `api.subgraph.autonolas.tech/api/proxy/marketplace-polygon` — mech requests, tool matching
- **The Graph**: Polygon service registry (agent ID 86 = PolyStrat)
