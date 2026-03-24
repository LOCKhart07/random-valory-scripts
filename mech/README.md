# Mech Marketplace Monitoring

Scripts for querying mech request/deliver events on Gnosis and Base chains.

## Scripts

| Script | Description |
|--------|-------------|
| `analyze_mech_delivers.py` | Analyze recent delivers for a mech, enrich with IPFS data |
| `analyze_base_mech_delivers.py` | Same analysis for Base chain mechs |
| `check_all_mechs.py` | Check all active mechs for broken delivers in last hour |
| `check_mech_requests_ipfs.py` | Probe IPFS availability for mech requests |
| `diff_base_delivers.py` | Diff delivers between two Base mechs |
| `fetch_mech_delivers.py` | Fetch last N delivers from mech-marketplace subgraph |
| `find_all_tools_requested_from_a_mech.py` | List all tool types requested from a mech |
| `find_deliver_events_for_a_request_id.py` | Query Gnosis RPC for Deliver events by request ID |
| `find_mech_delivers_for_al_tool.py` | Find delivers for a specific tool on a mech |
| `find_mech_requests_for_a_tool.py` | Find all requests for a specific tool |
| `find_requests_for_a_mech.py` | Fetch request events from Base contract with date filters |
| `mech_deliver_timeline.py` | Plot per-mech deliver trends over 30-90 days |
| `mech_usage_timeline.py` | Plot per-mech request trends over time |
| `test_tools_with_resource_util.py` | Test tools with resource utilization metrics |

## Data Sources

- **Gnosis mech marketplace**: `api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis`
- **Base mech marketplace**: `api.subgraph.autonolas.tech/api/proxy/marketplace-base`
- **IPFS**: `gateway.autonolas.tech` for request/delivery metadata
- **RPC**: Direct event log queries for Deliver events
