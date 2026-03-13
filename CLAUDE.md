# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Collection of utility scripts for analyzing the Autonolas/Olas ecosystem: tool accuracy statistics, mech marketplace monitoring, Polymarket agent analysis, and market creation tracking on Gnosis Chain.

## Setup

```bash
poetry install
```

Scripts require a `.env` file with RPC endpoints and API keys (`GNOSIS_RPC`, `ETHERSCAN_API_KEY`, `BASE_RPC`, `GENAI_API_KEY`).

## Running Scripts

All scripts are standalone and run directly with `python <script>.py`. Most accept CLI args via argparse (e.g., `python tool_accuracy.py 200` for 200 bets). No test suite or CI exists.

## Linting

Flake8 is configured (`.flake8`) with E501 (line length) ignored.

## Architecture

- **Data fetching**: GraphQL queries against Autonolas subgraphs (mech-marketplace, predict-omen) with pagination and retry logic
- **Blockchain interaction**: Web3.py for RPC calls, event log querying with block-range pagination, IPFS gateway access for request metadata
- **Caching**: JSON-based disk cache files (`.mech_cache.json`, etc.) with TTL enforcement (1–12 hours)
- **Concurrency**: ThreadPoolExecutor for parallel IPFS/API requests
- **Visualization**: Optional matplotlib with graceful fallback when unavailable

## Key Directories

- `mech/` — scripts for querying mech request/deliver events, usage timelines, and deliver analysis
- `tool-accuracy/` — tool accuracy statistics, bar charts, timelines, and Polymarket accuracy analysis
- `polymarket/` — Polymarket agent accuracy, ROI analysis, and a small HTTP server
- `market-creator/` — Reality.io market event watcher on Gnosis Chain
- `chatui/` — chat UI components
