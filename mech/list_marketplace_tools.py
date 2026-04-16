#!/usr/bin/env python3
"""
List all tools advertised by mechs in the Olas Mech Marketplace on Gnosis Chain.

Discovery mechanism:
1. Query the Olas Mech Marketplace subgraph to get all active mechs with their metadata
2. For each mech, extract the metadata CID from the subgraph response
3. Construct IPFS URL: ipfs_gateway + "f01701220" + metadata_cid
4. Fetch the tools manifest from IPFS (autonolas gateway)
5. Extract tools from manifest["tools"] array
6. Deduplicate and aggregate: tool_name -> [mech_addresses]

This follows the same discovery method used by mech-interact.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

GNOSIS_RPC = os.getenv("GNOSIS_RPC", "https://rpc-gate.autonolas.tech/gnosis-rpc/")

# Subgraph URL for Olas Mech Marketplace on Gnosis
SUBGRAPH_URL = "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"

# IPFS gateway - use the autonolas one that works in this repo
IPFS_GATEWAY = "https://gateway.autonolas.tech/ipfs/"

# CID prefix used by mech-interact to construct IPFS URLs
CID_PREFIX = "f01701220"

# Subgraph query to fetch all mechs with their metadata
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


def fetch_mechs_from_subgraph() -> List[Dict[str, str]]:
    """Fetch all mechs from the subgraph with pagination."""
    all_mechs = []
    mechs_id_gt = ""
    
    while True:
        variables = {
            "first": QUERY_BATCH_SIZE,
            "mechs_id_gt": mechs_id_gt,
        }
        
        response = requests.post(
            SUBGRAPH_URL,
            json={"query": MECHS_QUERY, "variables": variables},
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        
        data = response.json()
        if "errors" in data:
            raise ValueError(f"GraphQL error: {data['errors']}")
        
        mechs = data.get("data", {}).get("meches", [])
        
        if not mechs:
            break
        
        # Filter out mechs without metadata
        valid_mechs = [
            m for m in mechs
            if m.get("service", {}).get("metadata")
            and m["service"]["metadata"][0].get("metadata")
        ]
        
        all_mechs.extend(valid_mechs)
        
        if len(mechs) < QUERY_BATCH_SIZE:
            break
        
        # Use the last mech's ID for pagination
        mechs_id_gt = mechs[-1]["id"]
    
    return all_mechs


def fetch_tools_from_ipfs(mech_address: str, metadata_cid: str) -> Optional[List[str]]:
    """Fetch tools manifest from IPFS for a given mech.
    
    Args:
        mech_address: The mech's address
        metadata_cid: The CID of the metadata (without prefix)
    
    Returns:
        List of tool names, or None if fetch fails
    """
    # Construct the full IPFS URL following mech-interact's pattern
    ipfs_url = urljoin(IPFS_GATEWAY, CID_PREFIX + metadata_cid)
    
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(ipfs_url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            
            # Parse the response as JSON
            data = response.json()
            
            # Tools are in the "tools" key
            if isinstance(data, dict) and "tools" in data:
                tools = data["tools"]
                if isinstance(tools, list):
                    return tools
                else:
                    print(f"Warning: Tools for {mech_address} is not a list: {type(tools)}")
                    return None
            elif isinstance(data, list):
                # Legacy format: tools are directly the list
                return data
            else:
                print(f"Warning: Tools manifest for {mech_address} has unexpected format: {type(data)}")
                return None
                
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                print(f"Error fetching tools for {mech_address} at {ipfs_url}: {e}")
                return None
            # Retry on transient errors


def process_mech(mech: Dict[str, str]) -> tuple:
    """Process a single mech and fetch its tools.
    
    Returns:
        (mech_address, tools_list) or (mech_address, None) on failure
    """
    mech_address = mech["address"]
    
    # Extract metadata CID from the nested structure
    try:
        metadata_list = mech.get("service", {}).get("metadata", [])
        if not metadata_list:
            return (mech_address, None)
        
        metadata_hex = metadata_list[0].get("metadata")
        if not metadata_hex:
            return (mech_address, None)
        
        # Remove the "0x" prefix to get the CID
        metadata_cid = metadata_hex[2:] if metadata_hex.startswith("0x") else metadata_hex
        
        # Fetch tools from IPFS
        tools = fetch_tools_from_ipfs(mech_address, metadata_cid)
        return (mech_address, tools)
        
    except Exception as e:
        print(f"Error processing mech {mech_address}: {e}")
        return (mech_address, None)


def main():
    """Main function to list all marketplace tools."""
    print("Fetching mechs from Olas Mech Marketplace subgraph...")
    
    try:
        mechs = fetch_mechs_from_subgraph()
        print(f"Found {len(mechs)} mechs with metadata\n")
    except Exception as e:
        print(f"Error fetching mechs: {e}")
        return 1
    
    if not mechs:
        print("No mechs found")
        return 0
    
    # Fetch tools from IPFS in parallel
    print(f"Fetching tools manifests from IPFS (using {IPFS_GATEWAY})...\n")
    
    mech_to_tools: Dict[str, Optional[List[str]]] = {}
    tool_to_mechs: Dict[str, Set[str]] = defaultdict(set)
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(process_mech, mech): mech for mech in mechs}
        
        completed = 0
        for future in as_completed(futures):
            mech_address, tools = future.result()
            mech_to_tools[mech_address] = tools
            
            if tools:
                for tool in tools:
                    tool_to_mechs[tool].add(mech_address)
            
            completed += 1
            if completed % 5 == 0:
                print(f"Progress: {completed}/{len(mechs)} mechs processed")
    
    # Calculate statistics
    mechs_with_tools = sum(1 for tools in mech_to_tools.values() if tools)
    mechs_without_tools = len(mech_to_tools) - mechs_with_tools
    total_unique_tools = len(tool_to_mechs)
    
    print(f"\n{'='*80}")
    print("SUMMARY STATISTICS")
    print(f"{'='*80}")
    print(f"Total mechs: {len(mechs)}")
    print(f"Mechs with tools: {mechs_with_tools}")
    print(f"Mechs without tools (failed fetch): {mechs_without_tools}")
    print(f"Total unique tools: {total_unique_tools}")
    print(f"{'='*80}\n")
    
    # Print tool -> mechs mapping (sorted by number of mechs, then alphabetically)
    print(f"{'='*80}")
    print("TOOLS TO MECHS MAPPING (deduplicated)")
    print(f"{'='*80}\n")
    
    sorted_tools = sorted(
        tool_to_mechs.items(),
        key=lambda x: (-len(x[1]), x[0])  # Sort by count (desc), then name (asc)
    )
    
    for tool_name, mech_addresses in sorted_tools:
        print(f"{tool_name}:")
        for mech_address in sorted(mech_addresses):
            print(f"  {mech_address}")
        print()
    
    # Print per-mech tools mapping
    print(f"\n{'='*80}")
    print("TOOLS BY MECH (address -> tools)")
    print(f"{'='*80}\n")
    
    for mech_address in sorted(mech_to_tools.keys()):
        tools = mech_to_tools[mech_address]
        if tools:
            print(f"{mech_address}:")
            for tool in sorted(tools):
                print(f"  - {tool}")
            print()
    
    print(f"\n{'='*80}")
    print("Export options:")
    print(f"{'='*80}")
    
    # Export as JSON
    export_data = {
        "summary": {
            "total_mechs": len(mechs),
            "mechs_with_tools": mechs_with_tools,
            "unique_tools": total_unique_tools,
        },
        "tools_to_mechs": {
            tool: sorted(list(mechs)) for tool, mechs in tool_to_mechs.items()
        },
        "mechs_to_tools": {
            mech: sorted(tools) if tools else []
            for mech, tools in mech_to_tools.items()
        }
    }
    
    json_file = "/tmp/marketplace_tools.json"
    with open(json_file, "w") as f:
        json.dump(export_data, f, indent=2)
    
    print(f"Full data exported to: {json_file}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
