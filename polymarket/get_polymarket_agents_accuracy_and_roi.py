import json
import os
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

THE_GRAPH_API_KEY = os.getenv("THE_GRAPH_API_KEY")
POLYGON_REGISTRY_SUBGRAPH_URL = f"https://gateway.thegraph.com/api/{THE_GRAPH_API_KEY}/subgraphs/id/HHRBjVWFT2bV7eNSRqbCNDtUVnLPt911hcp8mSe4z6KG"
POLYMARKET_BETS_SUBGRAPH_URL = (
    "https://predict-polymarket-agents.subgraph.autonolas.tech/"
)

PERCENTAGE_FACTOR = 100.0
USDC_DECIMALS_DIVISOR = 1_000_000
WEI_IN_ETH = 1_000_000_000_000_000_000
DEFAULT_MECH_FEE = 10_000_000_000_000_000  # 0.01 ETH (or POL) in wei


def call_subgraph(subgraph_url, query, variables):
    response = requests.post(
        subgraph_url,
        json={"query": query, "variables": variables},
        headers={
            "Content-Type": "application/json",
        },
    )
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Query failed with status code {response.status_code}")
        return None


def get_all_polystrat_agents():
    query = """
{
  services(where: {
    agentIds_contains: [86]
  },first: 1000) {
    id
    multisig
    agentIds
  }
}
"""
    response = call_subgraph(POLYGON_REGISTRY_SUBGRAPH_URL, query, {})
    agents_safe_addresses = [
        service["multisig"] for service in response["data"]["services"]
    ]
    return agents_safe_addresses


def fetch_agent_bets(safe_address: str) -> list:
    """Fetch agent bets from Polymarket subgraph."""
    query = """
query GetPolymarketTraderAgentBets($id: ID!) {
  marketParticipants(
    where: {traderAgent_: {id: $id}}
    first: 1000
    orderBy: blockTimestamp
    orderDirection: desc
  ) {
    bets {
      id
      outcomeIndex
      question {
        resolution {
          winningIndex
        }
      }
      amount
    }
  }
}
"""
    response = call_subgraph(POLYMARKET_BETS_SUBGRAPH_URL, query, {"id": safe_address})
    if (
        not response
        or "data" not in response
        or "marketParticipants" not in response["data"]
    ):
        return []
    # Flatten all bets from all marketParticipants
    all_bets = []
    for participant in response["data"]["marketParticipants"]:
        bets = participant.get("bets", [])
        for bet in bets:
            all_bets.append(bet)

    return all_bets


def fetch_trader_agent(safe_address: str) -> Optional[dict]:
    query = """
query GetPolymarketTraderAgentPerformance($id: ID!) {
  traderAgent(id: $id) {
    serviceId
    totalBets
    totalPayout
    totalTraded
    totalTradedSettled
  }
}
"""
    response = call_subgraph(POLYMARKET_BETS_SUBGRAPH_URL, query, {"id": safe_address})

    # print(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! {response=}")
    return response["data"].get("traderAgent")


def get_resolved_bets(bets: list) -> list:
    """Filter bets to only include those on resolved markets."""
    resolved_bets = [
        bet for bet in bets if bet.get("question", {}).get("resolution") is not None
    ]
    return resolved_bets


def calculate_polymarket_accuracy(bets: list) -> float:
    """Calculate prediction accuracy for Polymarket markets."""
    bets_on_resolved_markets = get_resolved_bets(bets)
    if len(bets_on_resolved_markets) == 0:
        return None
    won_bets = 0
    total_bets = 0
    for bet in bets_on_resolved_markets:
        resolution = bet.get("question", {}).get("resolution", {})
        winning_index = resolution.get("winningIndex")
        outcome_index = bet.get("outcomeIndex")
        if winning_index is None or outcome_index is None:
            continue
        if int(winning_index) < 0:
            continue
        total_bets += 1
        if int(outcome_index) == int(winning_index):
            won_bets += 1
    if total_bets == 0:
        return None
    win_rate = (won_bets / total_bets) * PERCENTAGE_FACTOR
    return win_rate


def calculate_partial_roi(trader_agent: dict) -> Optional[float]:
    if not trader_agent:
        return None
    total_traded_settled_raw = int(trader_agent.get("totalTradedSettled", 0))
    total_fees_settled_raw = int(trader_agent.get("totalFeesSettled", 0))
    total_market_payout_raw = int(trader_agent.get("totalPayout", 0))

    # Convert to USD
    total_traded_settled_usd = total_traded_settled_raw / USDC_DECIMALS_DIVISOR
    total_fees_settled_usd = total_fees_settled_raw / USDC_DECIMALS_DIVISOR
    total_market_payout_usd = total_market_payout_raw / USDC_DECIMALS_DIVISOR

    # Ignore mech costs
    total_costs_usd = total_traded_settled_usd + total_fees_settled_usd
    if total_costs_usd == 0:
        return None
    partial_roi = (
        (total_market_payout_usd - total_costs_usd) * PERCENTAGE_FACTOR
    ) / total_costs_usd
    return partial_roi


def get_accuracy_and_roi_for_agent(agent_safe_address):
    bets = fetch_agent_bets(agent_safe_address)
    accuracy = calculate_polymarket_accuracy(bets)
    resolved_bets = get_resolved_bets(bets)
    avg_bet_amount = (
        (sum(int(bet.get("amount", 0)) for bet in bets) / len(bets)) if bets else 0
    )
    trader_agent = fetch_trader_agent(agent_safe_address)
    roi = calculate_partial_roi(trader_agent)
    if accuracy is None or roi is None:
        print(
            f"Agent {agent_safe_address} has no resolved bets to calculate accuracy. {roi=}"
        )
    else:
        # print(
        #     f"Agent {agent_safe_address} has an accuracy of {accuracy:.2f}% with {len(bets)} bets"
        # )
        print(
            f"Agent {agent_safe_address} has an accuracy of {accuracy:.2f}% with {len(resolved_bets)}/{len(bets)} resolved bets and a partial ROI of {roi:.2f}%. AVG bet amount: {(avg_bet_amount/USDC_DECIMALS_DIVISOR):2f} USDC"
        )
    return accuracy, roi


def main():
    agent_safe_addresses = get_all_polystrat_agents()
    print(f"Found {len(agent_safe_addresses)} PolyStrat agents.")

    total_accuracy = 0
    total_roi = 0
    count_with_accuracy = 0
    c = 0
    for safe_address in agent_safe_addresses:
        # print(f"\n{c=}   {safe_address=}")
        # c += 1
        accuracy, roi = get_accuracy_and_roi_for_agent(safe_address)
        if accuracy is None or roi is None:
            continue

        total_accuracy += accuracy
        total_roi += roi
        count_with_accuracy += 1

    if count_with_accuracy > 0:
        avg_accuracy = total_accuracy / count_with_accuracy
        avg_roi = total_roi / count_with_accuracy
        print(
            f"Average accuracy across {count_with_accuracy} agents: {avg_accuracy:.2f}%"
        )
        print(
            f"Average partial ROI across {count_with_accuracy} agents: {avg_roi:.2f}%"
        )


main()
