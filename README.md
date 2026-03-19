# Olas Ecosystem Analysis Scripts

Utility scripts for analyzing the [Autonolas/Olas](https://olas.network/) ecosystem — mech marketplace monitoring, tool accuracy statistics, Polymarket agent performance, and market creation tracking.

## Setup

```bash
poetry install
```

Create a `.env` file (see `.env.example` fields below):

| Variable | Used by |
|----------|---------|
| `GNOSIS_RPC` | mech scripts (Gnosis chain RPC) |
| `BASE_RPC` | mech scripts (Base chain RPC) |
| `POLYGON_RPC` | polymarket scripts (Polygon chain RPC) |
| `ETHERSCAN_API_KEY` | gas price lookups |
| `THE_GRAPH_API_KEY` | PolyStrat agent registry queries |
| `GENAI_API_KEY` | Google GenAI API (mech analysis) |

## Running Scripts

All scripts are standalone: `poetry run python <path/to/script>.py`. Most accept CLI args via argparse — run with `--help` for options.

## Directory Structure

### `mech/` — Mech Marketplace Monitoring

Scripts for querying mech request/deliver events on Gnosis and Base chains.

| Script | Description |
|--------|-------------|
| `analyze_mech_delivers.py` | Analyze recent delivers for a mech, enrich with IPFS data |
| `check_all_mechs.py` | Check all active mechs for broken delivers in last hour |
| `check_mech_requests_ipfs.py` | Probe IPFS availability for mech requests |
| `fetch_mech_delivers.py` | Fetch last N delivers from mech-marketplace subgraph |
| `find_all_tools_requested_from_a_mech.py` | List all tool types requested from a mech |
| `find_deliver_events_for_a_request_id.py` | Query Gnosis RPC for Deliver events by request ID |
| `find_mech_delivers_for_al_tool.py` | Find delivers for a specific tool on a mech |
| `find_mech_requests_for_a_tool.py` | Find all requests for a specific tool |
| `find_requests_for_a_mech.py` | Fetch request events from Base contract with date filters |
| `mech_deliver_timeline.py` | Plot per-mech deliver trends over 30-90 days |
| `mech_usage_timeline.py` | Plot per-mech request trends over time |

### `polymarket/` — PolyStrat Agent Analysis

Analysis scripts for Polymarket PolyStrat prediction agents (agent ID 86).

| Script | Description |
|--------|-------------|
| `analyze_agent.py` | Single-agent diagnostic (accuracy, tools, bet sizing, temporal trends) |
| `analyze_agent_deep.py` | Deep single-agent vs fleet comparison |
| `analyze_divergence.py` | Fleet divergence analysis — hypotheses about performance differences |
| `analyze_fleet.py` | Fleet-wide profitability across all registered agents |
| `analyze_persistence.py` | Path persistence testing (quartile stickiness, PnL streaks) |
| `analyze_persistence_deep.py` | Deep persistence with 6 hypothesis tests |
| `analyze_tool_usage.py` | Tool usage patterns across fleet (popularity, trends, adoption) |
| `generate_accuracy_csv.py` | Generate tools accuracy CSV from on-chain Polymarket data |
| `get_polymarket_agents_accuracy_and_roi.py` | Fetch all agents, generate profitability report |
| `verify_lockin.py` | Verify accuracy store lock-in behavior |
| `server.py` | FastAPI server for agent analysis (HMAC-authenticated) |

The directory also contains Jupyter notebooks for Safe management, Polymarket operations, and redemption workflows.

### `tool-accuracy/` — Tool Accuracy Statistics (Omen)

| Script | Description |
|--------|-------------|
| `tool_accuracy.py` | Per-tool accuracy stats from predict-omen subgraph |
| `tool_accuracy_bars.py` | Grouped bar chart of accuracy by time bin |
| `tool_accuracy_timeline.py` | Per-tool accuracy trend lines |
| `tool_accuracy_polymarket.py` | Tool accuracy stats using Polymarket subgraphs |
| `generate_accuracy_csv.py` | Generate accuracy CSV from on-chain Omen data |

### `market-creator/` — Market Event Watcher

| Script | Description |
|--------|-------------|
| `market-watcher.py` | Reality.io market event watcher on Gnosis Chain |

### Other

| Path | Description |
|------|-------------|
| `analyse_mech_requests.py` | Root-level: analyze mech requests/trades for PolyStrat agents |
| `get_gas_price.py` | Root-level: fetch gas prices from Etherscan (Gnosis/Optimism/Base) |
| `chatui/latency_tester.py` | HTTP latency tester for `/configure_strategies` endpoint |

## Data Sources

- **Subgraphs**: Autonolas subgraph proxies (`api.subgraph.autonolas.tech`) for mech-marketplace (Gnosis + Polygon) and predict-omen/predict-polymarket
- **The Graph**: Polygon service registry for PolyStrat agent discovery
- **IPFS**: `gateway.autonolas.tech` for mech request/delivery metadata
- **RPC**: Direct chain queries via Web3.py for event logs

## Caching

Scripts use JSON-based disk cache files (`.mech_cache.json`, etc.) with TTL enforcement (1-12 hours). These are gitignored.
