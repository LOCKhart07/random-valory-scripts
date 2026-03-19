# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Collection of utility scripts for analyzing the Autonolas/Olas ecosystem: mech marketplace monitoring, tool accuracy statistics, Polymarket PolyStrat agent analysis, and market creation tracking across Gnosis, Polygon, and Base chains.

## Setup

```bash
poetry install
```

Scripts require a `.env` file with RPC endpoints and API keys:
- `GNOSIS_RPC` — Gnosis chain RPC endpoint
- `BASE_RPC` — Base chain RPC endpoint
- `POLYGON_RPC` — Polygon chain RPC endpoint
- `ETHERSCAN_API_KEY` — Etherscan API for gas price lookups
- `THE_GRAPH_API_KEY` — The Graph API for PolyStrat agent registry queries
- `GENAI_API_KEY` — Google GenAI API key

## Running Scripts

All scripts are standalone and run with `poetry run python <script>.py`. Most accept CLI args via argparse. No test suite or CI exists.

## Linting

Flake8 is configured (`.flake8`) with E501 (line length) ignored.

## Architecture

- **Data fetching**: GraphQL queries against Autonolas subgraph proxies (`api.subgraph.autonolas.tech/api/proxy/{chain}`) for mech-marketplace and predict-omen/predict-polymarket, plus The Graph for Polygon service registry
- **Blockchain interaction**: Web3.py for RPC calls, event log querying with block-range pagination, IPFS gateway access (`gateway.autonolas.tech/ipfs`) for request metadata
- **Caching**: JSON-based disk cache files (`.mech_cache.json`, etc.) with TTL enforcement (1–12 hours)
- **Concurrency**: ThreadPoolExecutor for parallel IPFS/API requests
- **Visualization**: Optional matplotlib with graceful fallback when unavailable

## Key Directories

- `mech/` — mech request/deliver event queries, IPFS checks, usage/deliver timelines (Gnosis + Base chains)
- `tool-accuracy/` — tool accuracy statistics, bar charts, timelines, and CSV generation for both Omen and Polymarket
- `polymarket/` — PolyStrat agent analysis: single-agent diagnostics, fleet profitability, divergence analysis, tool usage trends, persistence testing, accuracy CSV generation, and a FastAPI server. Also contains Jupyter notebooks for Safe management and Polymarket operations
- `market-creator/` — Reality.io market event watcher on Gnosis Chain
- `chatui/` — HTTP latency tester for configure_strategies endpoint

## Key Subgraph Endpoints

- **Polymarket bets**: `predict-polymarket-agents.subgraph.autonolas.tech`
- **Mech marketplace (Polygon)**: `api.subgraph.autonolas.tech/api/proxy/marketplace-polygon`
- **Mech marketplace (Gnosis)**: `api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis`
- **Predict Omen**: `api.subgraph.staging.autonolas.tech/api/proxy/predict-omen`
- **Polygon registry**: via The Graph gateway (agent ID 86 = PolyStrat)

## Conventions

- New analysis should go in a new script, not modify existing ones
- Generated output files (CSVs, caches, logs) should not be committed
- Scripts use `_post_with_retry()` with exponential backoff (4 retries, 3s base) for subgraph queries
- Subgraph pagination is skip-based (first: 1000, skip: N)
- Tool names come from `parsedRequest.tool` in the mech marketplace subgraph
- Bet-to-tool matching uses question title prefix matching with closest-before-bet timestamp selection
