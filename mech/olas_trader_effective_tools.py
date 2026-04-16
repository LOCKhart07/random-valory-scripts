#!/usr/bin/env python3
"""
Compute the effective set of mech tools each Olas Trader service template can
actually use = (tools advertised by active mechs on the trader's home chain's
mech marketplace) MINUS (tools listed in the template's IRRELEVANT_TOOLS env var).

Two trader templates are handled:
  - PREDICT_SERVICE_TEMPLATE              -> Omen trader, home_chain = Gnosis
  - PREDICT_POLYMARKET_SERVICE_TEMPLATE   -> Polymarket trader, home_chain = Polygon

Both templates live in the olas-operate-app repo at:
    frontend/constants/serviceTemplates/service/trader.ts

The IRRELEVANT_TOOLS values are JSON-encoded string literals inside each
template's env_variables block. This script parses them directly from that file
so it always reflects the current ground truth rather than a hardcoded snapshot.

Data sources
------------
1. Mech marketplace subgraphs (via autonolas proxy):
     Gnosis  : https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis
     Polygon : https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon
   We query `meches` filtered to services with totalDeliveries_gt: 0 (i.e. active
   mechs), paginated by id. For each mech we pull the metadata CID.
2. IPFS (autonolas gateway): each mech's metadata manifest is fetched from
   `https://gateway.autonolas.tech/ipfs/f01701220<cid>` and its `tools` array is
   used to build the per-chain tool catalogue.
3. trader.ts: IRRELEVANT_TOOLS values are extracted via a regex that targets the
   `value: '...'` single-line JSON string literal inside each template's
   IRRELEVANT_TOOLS env variable.

This is the same discovery method `mech-interact` uses to learn tool manifests.

How to re-run
-------------
    poetry run python mech/olas_trader_effective_tools.py

Optional flags:
    --trader-ts <path>   Override the default path to trader.ts.
    --json-out <path>    Write the full structured result (catalogues + effective
                         sets + diffs) as JSON for downstream tooling.

Expected environment:
    - A working internet connection (subgraph + IPFS).
    - The olas-operate-app repo checked out alongside this one at
      ~/work/valory/repos/olas-operate-app (override with --trader-ts otherwise).

No API keys or RPC credentials are required; all queries are public.

Output
------
Human-readable summary to stdout:
  * per-chain available tool catalogues
  * per-template IRRELEVANT_TOOLS counts (including how many are stale — i.e.
    blocked names that don't exist on the relevant chain)
  * per-template effective tool set
  * cross-template diff restricted to tools that exist on the relevant chain
    (i.e. real config asymmetries, not noise from chain-specific deployments)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

import requests

SUBGRAPH_BASE = "https://api.subgraph.autonolas.tech/api/proxy"
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs/"
CID_PREFIX = "f01701220"

CHAIN_SUBGRAPHS = {
    "gnosis": f"{SUBGRAPH_BASE}/marketplace-gnosis",
    "polygon": f"{SUBGRAPH_BASE}/marketplace-polygon",
}

# Map each trader template to the chain whose marketplace it draws from.
TEMPLATE_CHAINS = {
    "PREDICT_SERVICE_TEMPLATE": "gnosis",
    "PREDICT_POLYMARKET_SERVICE_TEMPLATE": "polygon",
}

DEFAULT_TRADER_TS = Path(
    os.path.expanduser(
        "~/work/valory/repos/olas-operate-app/frontend/constants/serviceTemplates/service/trader.ts"
    )
)

MECHS_QUERY = """
query GetAllMechs($first: Int!, $mechs_id_gt: String!) {
    meches(
        first: $first,
        orderBy: id,
        orderDirection: asc,
        where: {
            id_gt: $mechs_id_gt,
            service_: {totalDeliveries_gt: 0}
        }
    ) {
        id
        address
        service {
            metadata {
                metadata
            }
        }
    }
}
"""

QUERY_BATCH_SIZE = 1000
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30


def fetch_mechs_from_subgraph(subgraph_url: str) -> List[Dict]:
    """Paginate `meches` for a single marketplace subgraph."""
    all_mechs: List[Dict] = []
    mechs_id_gt = ""
    while True:
        variables = {"first": QUERY_BATCH_SIZE, "mechs_id_gt": mechs_id_gt}
        response = requests.post(
            subgraph_url,
            json={"query": MECHS_QUERY, "variables": variables},
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL error from {subgraph_url}: {data['errors']}")
        mechs = data.get("data", {}).get("meches", [])
        if not mechs:
            break
        valid = [
            m for m in mechs
            if m.get("service", {}).get("metadata")
            and m["service"]["metadata"][0].get("metadata")
        ]
        all_mechs.extend(valid)
        if len(mechs) < QUERY_BATCH_SIZE:
            break
        mechs_id_gt = mechs[-1]["id"]
    return all_mechs


def fetch_tools_from_ipfs(mech_address: str, metadata_cid: str) -> Optional[List[str]]:
    """Fetch a mech's tools manifest from IPFS; returns None on failure."""
    ipfs_url = urljoin(IPFS_GATEWAY, CID_PREFIX + metadata_cid)
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(ipfs_url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and "tools" in data:
                tools = data["tools"]
                return tools if isinstance(tools, list) else None
            if isinstance(data, list):
                return data
            return None
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                print(
                    f"[warn] failed to fetch tools for {mech_address} at {ipfs_url}: {e}",
                    file=sys.stderr,
                )
                return None
    return None


def process_mech(mech: Dict) -> Tuple[str, Optional[List[str]]]:
    mech_address = mech["address"]
    metadata_list = mech.get("service", {}).get("metadata", [])
    if not metadata_list:
        return mech_address, None
    metadata_hex = metadata_list[0].get("metadata")
    if not metadata_hex:
        return mech_address, None
    metadata_cid = metadata_hex[2:] if metadata_hex.startswith("0x") else metadata_hex
    tools = fetch_tools_from_ipfs(mech_address, metadata_cid)
    return mech_address, tools


def collect_chain_tools(chain: str) -> Dict:
    """For one chain, return {'tools': set, 'tool_to_mechs': dict, 'mech_count': int}."""
    subgraph = CHAIN_SUBGRAPHS[chain]
    print(f"[{chain}] fetching mechs from {subgraph} ...", file=sys.stderr)
    mechs = fetch_mechs_from_subgraph(subgraph)
    print(f"[{chain}] found {len(mechs)} active mechs with metadata", file=sys.stderr)

    tool_to_mechs: Dict[str, Set[str]] = defaultdict(set)
    mech_to_tools: Dict[str, Optional[List[str]]] = {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(process_mech, m) for m in mechs]
        for fut in as_completed(futures):
            addr, tools = fut.result()
            mech_to_tools[addr] = tools
            if tools:
                for tool in tools:
                    tool_to_mechs[tool].add(addr)

    return {
        "chain": chain,
        "mech_count": len(mechs),
        "mechs_with_tools": sum(1 for t in mech_to_tools.values() if t),
        "tools": set(tool_to_mechs.keys()),
        "tool_to_mechs": {t: sorted(m) for t, m in tool_to_mechs.items()},
        "mech_to_tools": {m: sorted(t) if t else [] for m, t in mech_to_tools.items()},
    }


def parse_irrelevant_tools(trader_ts_path: Path) -> Dict[str, List[str]]:
    """Parse IRRELEVANT_TOOLS JSON arrays from both templates in trader.ts.

    Returns a dict keyed by template name (e.g. 'PREDICT_SERVICE_TEMPLATE') with
    the parsed list of tool-name strings as values.
    """
    if not trader_ts_path.exists():
        raise FileNotFoundError(
            f"trader.ts not found at {trader_ts_path}. Use --trader-ts to override."
        )
    content = trader_ts_path.read_text()

    result: Dict[str, List[str]] = {}
    for template in TEMPLATE_CHAINS:
        # Limit the search window to the block starting at this template's
        # `export const` so we don't cross-contaminate between templates.
        start = content.find(f"export const {template}")
        if start == -1:
            raise RuntimeError(f"could not find template '{template}' in {trader_ts_path}")
        next_export = content.find("export const", start + 1)
        block = content[start : next_export if next_export != -1 else len(content)]

        # Matches the `IRRELEVANT_TOOLS: { ... value: '<json>' ... }` pattern.
        # The JSON string uses no single quotes internally (all tool names use
        # hyphens/underscores/dots), so a non-greedy match on the single-quoted
        # literal is safe.
        match = re.search(
            r"IRRELEVANT_TOOLS:\s*\{[^}]*?value:\s*'([^']*)'",
            block,
            re.DOTALL,
        )
        if not match:
            raise RuntimeError(f"could not find IRRELEVANT_TOOLS value in {template}")
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"IRRELEVANT_TOOLS for {template} is not valid JSON: {e}")
        if not isinstance(parsed, list):
            raise RuntimeError(f"IRRELEVANT_TOOLS for {template} did not parse to a list")
        result[template] = parsed
    return result


def print_section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trader-ts",
        type=Path,
        default=DEFAULT_TRADER_TS,
        help=f"path to olas-operate-app trader.ts (default: {DEFAULT_TRADER_TS})",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="optional path to write full structured results as JSON",
    )
    args = parser.parse_args()

    irrelevant = parse_irrelevant_tools(args.trader_ts)
    print(f"Parsed IRRELEVANT_TOOLS from {args.trader_ts}", file=sys.stderr)
    for template, tools in irrelevant.items():
        print(f"  {template}: {len(tools)} entries", file=sys.stderr)

    # Fetch both chains (sequentially; each internally parallelises the IPFS fan-out).
    chain_catalogues = {chain: collect_chain_tools(chain) for chain in CHAIN_SUBGRAPHS}

    # Report per-chain catalogues.
    for chain, cat in chain_catalogues.items():
        print_section(f"{chain.upper()} MARKETPLACE — {len(cat['tools'])} unique tools across {cat['mech_count']} active mechs")
        for tool in sorted(cat["tools"]):
            print(f"  {tool}")

    # Effective sets per template.
    effective: Dict[str, Set[str]] = {}
    for template, chain in TEMPLATE_CHAINS.items():
        available = chain_catalogues[chain]["tools"]
        irr = set(irrelevant[template])
        eff = available - irr
        effective[template] = eff
        dead = irr - available  # blocked names not on this chain
        print_section(f"{template} (chain={chain})")
        print(f"  available on chain      : {len(available)}")
        print(f"  IRRELEVANT_TOOLS entries: {len(irr)}")
        print(f"    of which not on chain : {len(dead)}  (dead/no-op exclusions)")
        print(f"  EFFECTIVE usable tools  : {len(eff)}")
        for tool in sorted(eff):
            print(f"    + {tool}")

    # Cross-template diff, restricted to tools that exist on the relevant chain,
    # so we surface real config asymmetries rather than chain-specific deployments.
    print_section("CROSS-TEMPLATE DIFF (meaningful asymmetries)")

    t1, t2 = "PREDICT_SERVICE_TEMPLATE", "PREDICT_POLYMARKET_SERVICE_TEMPLATE"
    chain1, chain2 = TEMPLATE_CHAINS[t1], TEMPLATE_CHAINS[t2]
    avail1 = chain_catalogues[chain1]["tools"]
    avail2 = chain_catalogues[chain2]["tools"]

    # T1 can use, T2 cannot — split by whether the tool also exists on T2's chain.
    t1_only_config_block = sorted((effective[t1] & avail2) - effective[t2])
    t1_only_chain_specific = sorted((effective[t1] - avail2))
    t2_only_config_block = sorted((effective[t2] & avail1) - effective[t1])
    t2_only_chain_specific = sorted((effective[t2] - avail1))
    shared = sorted(effective[t1] & effective[t2])

    print(f"  Both can use                : {shared}")
    print(f"  {t1} uses; {t2} blocks (but tool IS on Polygon):")
    for tool in t1_only_config_block:
        print(f"    - {tool}")
    print(f"  {t1} uses; tool not on Polygon at all:")
    for tool in t1_only_chain_specific:
        print(f"    - {tool}")
    print(f"  {t2} uses; {t1} blocks (but tool IS on Gnosis):")
    for tool in t2_only_config_block:
        print(f"    - {tool}")
    print(f"  {t2} uses; tool not on Gnosis at all:")
    for tool in t2_only_chain_specific:
        print(f"    - {tool}")

    if args.json_out:
        out = {
            "source_trader_ts": str(args.trader_ts),
            "chains": {
                chain: {
                    "mech_count": cat["mech_count"],
                    "mechs_with_tools": cat["mechs_with_tools"],
                    "tools": sorted(cat["tools"]),
                    "tool_to_mechs": cat["tool_to_mechs"],
                    "mech_to_tools": cat["mech_to_tools"],
                }
                for chain, cat in chain_catalogues.items()
            },
            "templates": {
                template: {
                    "chain": TEMPLATE_CHAINS[template],
                    "irrelevant_tools": sorted(irrelevant[template]),
                    "effective_tools": sorted(effective[template]),
                    "dead_irrelevant_entries": sorted(
                        set(irrelevant[template]) - chain_catalogues[TEMPLATE_CHAINS[template]]["tools"]
                    ),
                }
                for template in TEMPLATE_CHAINS
            },
            "diff": {
                "shared": shared,
                f"{t1}_only_config_block": t1_only_config_block,
                f"{t1}_only_chain_specific": t1_only_chain_specific,
                f"{t2}_only_config_block": t2_only_config_block,
                f"{t2}_only_chain_specific": t2_only_chain_specific,
            },
        }
        args.json_out.write_text(json.dumps(out, indent=2))
        print(f"\nWrote structured output to {args.json_out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
