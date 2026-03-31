# source_content IPFS Overhead — Impact Report

**Date:** 2026-04-01
**Window:** Last 7 days (2026-03-24 to 2026-03-31)
**Chains:** Gnosis + Polygon

## Summary

Enabling `return_source_content=true` on prediction tools would increase daily IPFS storage from ~0.6 MB to ~9.6 GB — a ~16,000x increase. The overhead comes almost entirely from raw HTML stored in `source_content.pages`.

## Current Daily Request Volume

Total mech requests across both chains: **~16,241/day**

| Chain   | Requests/day |
|---------|-------------|
| Gnosis  | ~15,612     |
| Polygon | ~630        |

## Per-Tool Overhead (from Jenslee's measurements)

| Tool                         | Without source_content | With source_content | Overhead | Pages | Avg page size |
|------------------------------|----------------------|-------------------|----------|-------|---------------|
| prediction_request           | 102 B                | 1.9 MB            | 1.9 MB   | 5     | 370 KB        |
| prediction_request_sme       | 84 B                 | 3.1 MB            | 3.1 MB   | 6     | 355 KB        |
| prediction_request_rag       | 102 B                | 2.5 MB            | 2.5 MB   | 9     | 258 KB        |
| prediction_request_reasoning | 102 B                | 1.5 MB            | 1.5 MB   | 5     | 293 KB        |
| superforcaster               | 56 B                 | 3.6 KB            | 3.6 KB   | 0     | n/a           |

Average overhead per request: ~1.5 MB (superforcaster excluded — uses Serper snippets only, no full pages).

## Affected Tools (by package)

Source: `TOOLS_TO_PACKAGE_HASH` env config.

| Package hash (prefix) | Tools                                                              | Combined avg/day |
|------------------------|--------------------------------------------------------------------|-----------------|
| `bafybeihatfa...`      | prediction-offline, prediction-online, claude-prediction-online, claude-prediction-offline | ~3,716 |
| `bafybeibaji2...`      | prediction-request-reasoning, prediction-request-reasoning-claude  | ~2,302          |
| `bafybeihqpsw...`      | prediction-request-rag, prediction-request-rag-claude              | ~260            |
| `bafybeic4zhp...`      | superforcaster                                                     | ~2,750          |
| `bafybeicvhed...`      | prediction-online-sme, prediction-offline-sme                      | ~20             |
| `bafybeiddqmd...`      | resolve-market-reasoning-gpt-4.1                                   | ~66             |
| `bafybeigezfb...`      | resolve-market-jury-v1                                             | ~8              |

## Storage Impact

Excluding superforcaster (3.6 KB overhead, negligible):

| Metric                    | Without source_content | With source_content |
|---------------------------|----------------------|-------------------|
| Affected requests/day     | ~6,400               | ~6,400            |
| Avg payload size          | ~100 B               | ~1.5 MB           |
| **Total storage/day**     | **~0.6 MB**          | **~9.6 GB**       |
| Total storage/week        | ~4.2 MB              | ~67.2 GB          |
| Total storage/month (30d) | ~18 MB               | ~288 GB           |

Including superforcaster (~2,750/day × 3.6 KB = ~10 MB/day) adds negligible overhead.

## Full Tool Activity (last 7 days)

| Tool                                  | Gnosis | Polygon | Total   | Avg/day |
|---------------------------------------|--------|---------|---------|---------|
| unknown                               | 24,752 | 0       | 24,752  | 3,536.0 |
| superforcaster                        | 16,042 | 3,206   | 19,248  | 2,749.7 |
| coingecko_api                         | 17,693 | 0       | 17,693  | 2,527.6 |
| prediction-request-reasoning          | 13,548 | 443     | 13,991  | 1,998.7 |
| grid_pair_screener                    | 10,530 | 0       | 10,530  | 1,504.3 |
| prediction-offline                    | 7,912  | 63      | 7,975   | 1,139.3 |
| grid_analyser                         | 2,870  | 0       | 2,870   | 410.0   |
| synth_data                            | 2,556  | 0       | 2,556   | 365.1   |
| coinbase_commerce_request             | 2,529  | 0       | 2,529   | 361.3   |
| prediction-request-reasoning-claude   | 1,958  | 155     | 2,113   | 301.9   |
| prediction-online                     | 1,948  | 86      | 2,034   | 290.6   |
| claude-prediction-offline             | 1,748  | 126     | 1,874   | 267.7   |
| prediction-request-rag                | 1,592  | 79      | 1,671   | 238.7   |
| prediction_request_reasoning-claude   | 764    | 0       | 764     | 109.1   |
| prediction_request_reasoning          | 742    | 0       | 742     | 106.0   |
| prediction_request_reasoning-5.2.mini | 708    | 0       | 708     | 101.1   |
| resolve-market-reasoning-gpt-4.1      | 461    | 0       | 461     | 65.9    |
| grid_analyzer                         | 338    | 0       | 338     | 48.3    |
| coinbase_commerce_api                 | 317    | 0       | 317     | 45.3    |
| prediction-request-rag-claude         | 61     | 85      | 146     | 20.9    |
| prediction-online-sme                 | 73     | 59      | 132     | 18.9    |
| claude-prediction-online              | 19     | 107     | 126     | 18.0    |
| openai-gpt-4o-2024-08-06             | 58     | 0       | 58      | 8.3     |
| resolve-market-jury-v1               | 57     | 0       | 57      | 8.1     |
| prediction-offline-sme               | 4      | 0       | 4       | 0.6     |
| echo                                  | 1      | 0       | 1       | 0.1     |

## Notes

- The `unknown` tool category (3,536/day) represents requests where `parsedRequest.tool` is empty in the subgraph. These could include affected tools.
- Older underscore-named tools (`prediction_request_*`) are largely inactive (~100-240/day). The active fleet uses hyphenated names (`prediction-request-*`, `prediction-offline/online`).
- `prediction_url_cot` was not observed in the 7-day window (Jenslee also noted it returned empty without `embedding_provider`).

## Data Source

Queried from mech-marketplace subgraphs:
- Gnosis: `api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis`
- Polygon: `api.subgraph.autonolas.tech/api/proxy/marketplace-polygon`

Script: `mech/count_daily_requests_by_tool.py`
