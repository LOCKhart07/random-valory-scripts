#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone script to analyse all mech requests and trades for a Polystrat agent.

Groups mech requests into:
  1. PLACED bets — mech requests that resulted in an on-chain trade (with settlement status)
  2. UNPLACED mech requests — mech requests where no bet was placed

Outputs a unified timeline sorted by timestamp (descending), so you can see
placed bets interleaved with unplaced mech requests in chronological order.

Usage:
    python scripts/analyse_mech_requests.py <safe_address> [--platform polymarket|omen] [--json] [--limit N]

Examples:
    python scripts/analyse_mech_requests.py 0xABC123... --platform polymarket
    python scripts/analyse_mech_requests.py 0xABC123... --platform omen --json
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

# ── Subgraph endpoints ──────────────────────────────────────────────────────
POLYMARKET_AGENTS_SUBGRAPH = (
    "https://predict-polymarket-agents.subgraph.autonolas.tech/"
)
POLYGON_MECH_SUBGRAPH = (
    "https://api.subgraph.autonolas.tech/api/proxy/marketplace-polygon"
)

OMEN_AGENTS_SUBGRAPH = (
    "https://api.subgraph.staging.autonolas.tech/api/proxy/predict-omen"
)
GNOSIS_MECH_SUBGRAPH = (
    "https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis"
)

# ── Constants ───────────────────────────────────────────────────────────────
USDC_DECIMALS_DIVISOR = 10**6
WEI_IN_ETH = 10**18
GRAPHQL_BATCH_SIZE = 1000
DEFAULT_MECH_FEE_ETH = 0.01  # 0.01 ETH per mech request
INVALID_ANSWER_HEX = (
    "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
)
ISO_FMT = "%Y-%m-%d %H:%M:%S UTC"
QUESTION_DATA_SEPARATOR = "\u241f"


# ── GraphQL queries ─────────────────────────────────────────────────────────

# Gnosis mech subgraph (has requestId field)
GET_ALL_MECH_REQUESTS_GNOSIS_QUERY = """
query GetAllMechRequests($sender: String!, $skip: Int!) {
  sender(id: $sender) {
    totalMarketplaceRequests
    requests(
      first: 1000
      skip: $skip
      orderBy: requestId
      orderDirection: asc
    ) {
      id
      requestId
      blockTimestamp
      parsedRequest {
        questionTitle
        tool
      }
    }
  }
}
"""

# Polygon mech subgraph (no requestId — use blockTimestamp for ordering)
GET_ALL_MECH_REQUESTS_POLYGON_QUERY = """
query GetAllMechRequests($sender: String!, $skip: Int!) {
  sender(id: $sender) {
    totalMarketplaceRequests
    requests(
      first: 1000
      skip: $skip
      orderBy: blockTimestamp
      orderDirection: asc
    ) {
      id
      blockTimestamp
      parsedRequest {
        questionTitle
        tool
      }
    }
  }
}
"""

# Gnosis mech response query
GET_MECH_RESPONSE_GNOSIS_QUERY = """
query GetMechResponse($sender: String!, $questionTitle: String!) {
  requests(
    where: { sender: $sender, parsedRequest_: { questionTitle: $questionTitle } }
    first: 1
    orderBy: requestId
    orderDirection: desc
  ) {
    parsedRequest {
      questionTitle
    }
    deliveries(first: 1, orderBy: deliveryId, orderDirection: desc) {
      toolResponse
      model
    }
  }
}
"""

# Polygon mech response query (uses blockTimestamp, no deliveryId)
GET_MECH_RESPONSE_POLYGON_QUERY = """
query GetMechResponse($sender: String!, $questionTitle: String!) {
  requests(
    where: { sender: $sender, parsedRequest_: { questionTitle: $questionTitle } }
    first: 1
    orderBy: blockTimestamp
    orderDirection: desc
  ) {
    parsedRequest {
      questionTitle
    }
    deliveries(first: 1, orderBy: id, orderDirection: desc) {
      toolResponse
      model
    }
  }
}
"""

# Polymarket prediction history
GET_POLYMARKET_PREDICTION_HISTORY_QUERY = """
query GetPolymarketPredictionHistory($id: ID!, $first: Int!, $skip: Int!) {
  marketParticipants(
    orderBy: blockTimestamp
    orderDirection: desc
    where: {traderAgent_: {id: $id}}
    first: $first
    skip: $skip
  ) {
    totalPayout
    bets {
      id
      outcomeIndex
      amount
      shares
      blockTimestamp
      transactionHash
      question {
        id
        questionId
        metadata {
          outcomes
          title
        }
        resolution {
          winningIndex
          settledPrice
          blockTimestamp
        }
      }
    }
  }
}
"""

# Omen prediction history
GET_OMEN_PREDICTION_HISTORY_QUERY = """
query GetPredictionHistory($id: ID!, $first: Int!, $skip: Int!) {
  marketParticipants(
    where: { traderAgent_: { id: $id } }
    orderBy: blockTimestamp
    orderDirection: desc
    first: $first
    skip: $skip
  ) {
    id
    totalBets
    totalPayout
    totalTraded
    totalFees
    totalTradedSettled
    totalFeesSettled
    fixedProductMarketMaker {
      id
      question
      outcomes
      currentAnswer
      currentAnswerTimestamp
    }
    bets {
      id
      timestamp
      amount
      feeAmount
      outcomeIndex
    }
  }
}
"""


# ── Data classes ────────────────────────────────────────────────────────────
@dataclass
class MechRequestInfo:
    """Details of a single mech request."""

    request_id: str
    question_title: str
    tool: str
    timestamp: int
    timestamp_utc: str
    mech_response: Optional[Dict[str, Any]] = None  # p_yes, p_no, confidence, etc.


@dataclass
class PlacedBet:
    """A bet that was actually placed on-chain."""

    bet_id: str
    question_title: str
    prediction_side: str
    bet_amount: float
    status: str  # won, lost, pending, invalid
    net_profit: Optional[float]
    total_payout: Optional[float]
    timestamp: int
    timestamp_utc: str
    settled_at: Optional[str]
    transaction_hash: Optional[str]
    mech_requests: List[MechRequestInfo] = field(default_factory=list)


@dataclass
class UnplacedMechRequest:
    """A mech request that did NOT result in a placed bet."""

    question_title: str
    mech_request_count: int
    mech_requests: List[MechRequestInfo] = field(default_factory=list)
    earliest_timestamp: int = 0
    latest_timestamp: int = 0
    latest_timestamp_utc: str = ""
    mech_response: Optional[Dict[str, Any]] = None  # last response


@dataclass
class TimelineEntry:
    """Unified timeline entry for sorting."""

    timestamp: int
    timestamp_utc: str
    entry_type: str  # "placed_bet" or "unplaced_mech_request"
    data: Any


# ── Subgraph helpers ────────────────────────────────────────────────────────
def _post_graphql(url: str, query: str, variables: Dict) -> Optional[Dict]:
    """Send a GraphQL POST and return parsed JSON."""
    try:
        resp = requests.post(
            url,
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"  [WARN] Subgraph returned {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            return None
        return resp.json()
    except Exception as e:
        print(f"  [ERROR] Subgraph request failed: {e}", file=sys.stderr)
        return None


def fetch_all_mech_requests(
    mech_url: str, safe_address: str, is_polygon: bool = False
) -> Tuple[List[Dict], int]:
    """Fetch every mech request for this agent via pagination."""
    query = (
        GET_ALL_MECH_REQUESTS_POLYGON_QUERY
        if is_polygon
        else GET_ALL_MECH_REQUESTS_GNOSIS_QUERY
    )
    all_requests: List[Dict] = []
    total_marketplace = 0
    skip = 0

    while True:
        data = _post_graphql(
            mech_url,
            query,
            {"sender": safe_address.lower(), "skip": skip},
        )
        if not data:
            break

        sender = (data.get("data") or {}).get("sender")
        if not sender:
            break

        if total_marketplace == 0:
            total_marketplace = int(sender.get("totalMarketplaceRequests", 0))

        batch = sender.get("requests") or []
        if not batch:
            break

        all_requests.extend(batch)
        if len(batch) < GRAPHQL_BATCH_SIZE:
            break
        skip += GRAPHQL_BATCH_SIZE

    return all_requests, total_marketplace


def fetch_mech_response(
    mech_url: str, safe_address: str, question_title: str, is_polygon: bool = False
) -> Optional[Dict]:
    """Fetch the mech prediction response for a question."""
    query = (
        GET_MECH_RESPONSE_POLYGON_QUERY
        if is_polygon
        else GET_MECH_RESPONSE_GNOSIS_QUERY
    )
    data = _post_graphql(
        mech_url,
        query,
        {"sender": safe_address.lower(), "questionTitle": question_title},
    )
    if not data:
        return None

    reqs = (data.get("data") or {}).get("requests") or []
    if not reqs:
        return None

    deliveries = reqs[0].get("deliveries") or []
    if not deliveries:
        return None

    raw = deliveries[0].get("toolResponse")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ── Polymarket bet fetching ─────────────────────────────────────────────────
def fetch_polymarket_bets(agents_url: str, safe_address: str) -> List[Dict]:
    """Fetch all Polymarket bets for the agent."""
    all_bets: List[Dict] = []
    skip = 0

    while True:
        data = _post_graphql(
            agents_url,
            GET_POLYMARKET_PREDICTION_HISTORY_QUERY,
            {"id": safe_address.lower(), "first": GRAPHQL_BATCH_SIZE, "skip": skip},
        )
        if not data:
            break

        participants = (data.get("data") or {}).get("marketParticipants") or []
        if not participants:
            break

        for p in participants:
            total_payout = p.get("totalPayout", 0)
            for bet in p.get("bets") or []:
                bet["_totalPayout"] = total_payout
                all_bets.append(bet)

        if len(participants) < GRAPHQL_BATCH_SIZE:
            break
        skip += GRAPHQL_BATCH_SIZE

    return all_bets


def get_polymarket_bet_status(bet: Dict) -> str:
    """Determine bet status for Polymarket."""
    question = bet.get("question") or {}
    resolution = question.get("resolution")

    if not resolution:
        return "pending"

    winning_index = resolution.get("winningIndex")
    if winning_index is not None and int(winning_index) < 0:
        return "invalid"

    outcome_index = bet.get("outcomeIndex")
    if outcome_index is not None and winning_index is not None:
        if int(outcome_index) == int(winning_index):
            total_payout = float(bet.get("_totalPayout", 0))
            if total_payout == 0:
                return "pending"  # won but not redeemed
            return "won"
        return "lost"

    return "unknown"


def format_polymarket_bet(bet: Dict) -> PlacedBet:
    """Convert raw Polymarket bet to PlacedBet."""
    question = bet.get("question") or {}
    metadata = question.get("metadata") or {}
    resolution = question.get("resolution")
    title = metadata.get("title", "")

    amount = float(bet.get("amount", 0)) / USDC_DECIMALS_DIVISOR
    total_payout = float(bet.get("_totalPayout", 0)) / USDC_DECIMALS_DIVISOR
    status = get_polymarket_bet_status(bet)

    outcome_index = int(bet.get("outcomeIndex", 0))
    outcomes = ["Yes", "No"]  # hardcoded per codebase convention
    side = outcomes[outcome_index].lower() if outcome_index < len(outcomes) else "unknown"

    # net profit
    if status == "won":
        net_profit = total_payout - amount
    elif status == "lost":
        net_profit = -amount
    elif status == "invalid":
        net_profit = total_payout - amount
    else:
        net_profit = 0.0

    ts = int(bet.get("blockTimestamp") or 0)
    settled_ts = resolution.get("blockTimestamp") if resolution else None

    return PlacedBet(
        bet_id=bet.get("id", ""),
        question_title=title,
        prediction_side=side,
        bet_amount=round(amount, 4),
        status=status,
        net_profit=round(net_profit, 4) if net_profit is not None else None,
        total_payout=round(total_payout, 4),
        timestamp=ts,
        timestamp_utc=_ts_to_str(ts),
        settled_at=_ts_to_str(int(settled_ts)) if settled_ts else None,
        transaction_hash=bet.get("transactionHash", ""),
    )


# ── Omen bet fetching ──────────────────────────────────────────────────────
def fetch_omen_bets(agents_url: str, safe_address: str) -> List[Dict]:
    """Fetch all Omen bets for the agent."""
    all_bets: List[Dict] = []
    skip = 0

    while True:
        data = _post_graphql(
            agents_url,
            GET_OMEN_PREDICTION_HISTORY_QUERY,
            {"id": safe_address.lower(), "first": GRAPHQL_BATCH_SIZE, "skip": skip},
        )
        if not data:
            break

        participants = (data.get("data") or {}).get("marketParticipants") or []
        if not participants:
            break

        for p in participants:
            fpmm = p.get("fixedProductMarketMaker") or {}
            participant_totals = {
                "totalPayout": float(p.get("totalPayout", 0)),
                "totalTraded": float(p.get("totalTraded", 0)),
                "totalFees": float(p.get("totalFees", 0)),
                "totalBets": p.get("totalBets", 0),
            }
            for bet in p.get("bets") or []:
                bet["_fpmm"] = fpmm
                bet["_participant"] = participant_totals
                all_bets.append(bet)

        if len(participants) < GRAPHQL_BATCH_SIZE:
            break
        skip += GRAPHQL_BATCH_SIZE

    return all_bets


def get_omen_bet_status(bet: Dict) -> str:
    """Determine bet status for Omen."""
    fpmm = bet.get("_fpmm") or {}
    current_answer = fpmm.get("currentAnswer")

    if current_answer is None:
        return "pending"
    if current_answer == INVALID_ANSWER_HEX:
        return "invalid"

    outcome_index = int(bet.get("outcomeIndex", 0))
    correct = int(current_answer, 0)

    if outcome_index == correct:
        participant = bet.get("_participant") or {}
        total_payout = float(participant.get("totalPayout", 0)) / WEI_IN_ETH
        if total_payout == 0:
            return "pending"  # won but not redeemed
        return "won"
    return "lost"


def extract_omen_question_title(question: str) -> str:
    """Extract the question title from an Omen question string (strip separator-suffix)."""
    if not question:
        return ""
    return question.split(QUESTION_DATA_SEPARATOR)[0].strip()


def format_omen_bet(bet: Dict) -> PlacedBet:
    """Convert raw Omen bet to PlacedBet."""
    fpmm = bet.get("_fpmm") or {}
    participant = bet.get("_participant") or {}
    question_raw = fpmm.get("question", "")
    title = extract_omen_question_title(question_raw)
    outcomes = fpmm.get("outcomes") or []

    amount = float(bet.get("amount", 0)) / WEI_IN_ETH
    total_payout = float(participant.get("totalPayout", 0)) / WEI_IN_ETH
    total_traded = float(participant.get("totalTraded", 0)) / WEI_IN_ETH
    status = get_omen_bet_status(bet)

    outcome_index = int(bet.get("outcomeIndex", 0))
    side = outcomes[outcome_index].lower() if outcome_index < len(outcomes) else "unknown"

    # net profit
    if status == "won":
        net_profit = total_payout - total_traded if total_payout > 0 else 0.0
    elif status == "lost":
        net_profit = -amount
    elif status == "invalid":
        net_profit = total_payout - amount if total_payout > 0 else 0.0
    else:
        net_profit = 0.0

    ts = int(bet.get("timestamp") or 0)
    settled_ts = fpmm.get("currentAnswerTimestamp")

    return PlacedBet(
        bet_id=bet.get("id", ""),
        question_title=title,
        prediction_side=side,
        bet_amount=round(amount, 6),
        status=status,
        net_profit=round(net_profit, 6) if net_profit is not None else None,
        total_payout=round(total_payout, 6),
        timestamp=ts,
        timestamp_utc=_ts_to_str(ts),
        settled_at=_ts_to_str(int(settled_ts)) if settled_ts else None,
        transaction_hash=None,
    )


# ── Helpers ─────────────────────────────────────────────────────────────────
def _ts_to_str(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(ISO_FMT)


def build_mech_request_lookup(
    raw_requests: List[Dict],
) -> Tuple[Dict[str, List[MechRequestInfo]], Dict[str, int]]:
    """Build a lookup: question_title -> list of MechRequestInfo, and title -> count."""
    by_title: Dict[str, List[MechRequestInfo]] = defaultdict(list)
    counts: Dict[str, int] = defaultdict(int)

    for req in raw_requests:
        parsed = req.get("parsedRequest") or {}
        title = parsed.get("questionTitle", "")
        if not title:
            continue

        ts = int(req.get("blockTimestamp") or 0)
        info = MechRequestInfo(
            request_id=str(req.get("requestId", "") or req.get("id", "")),
            question_title=title,
            tool=parsed.get("tool", ""),
            timestamp=ts,
            timestamp_utc=_ts_to_str(ts),
        )
        by_title[title].append(info)
        counts[title] += 1

    return dict(by_title), dict(counts)


def classify_mech_requests(
    mech_by_title: Dict[str, List[MechRequestInfo]],
    mech_counts: Dict[str, int],
    placed_titles: Set[str],
    mech_url: str,
    safe_address: str,
    fetch_responses: bool = True,
    is_polygon: bool = False,
) -> Tuple[Dict[str, List[MechRequestInfo]], List[UnplacedMechRequest]]:
    """Split mech requests into placed (title matches a bet) and unplaced."""

    placed_mech: Dict[str, List[MechRequestInfo]] = {}
    unplaced_list: List[UnplacedMechRequest] = []

    for title, reqs in mech_by_title.items():
        if title in placed_titles:
            placed_mech[title] = reqs
        else:
            # This is an unplaced mech request
            sorted_reqs = sorted(reqs, key=lambda r: r.timestamp)
            earliest = sorted_reqs[0].timestamp if sorted_reqs else 0
            latest = sorted_reqs[-1].timestamp if sorted_reqs else 0

            # Optionally fetch the mech response for context
            mech_response = None
            if fetch_responses:
                mech_response = fetch_mech_response(mech_url, safe_address, title, is_polygon=is_polygon)

            entry = UnplacedMechRequest(
                question_title=title,
                mech_request_count=mech_counts.get(title, len(reqs)),
                mech_requests=sorted_reqs,
                earliest_timestamp=earliest,
                latest_timestamp=latest,
                latest_timestamp_utc=_ts_to_str(latest),
                mech_response=mech_response,
            )
            unplaced_list.append(entry)

    return placed_mech, unplaced_list


def build_timeline(
    placed_bets: List[PlacedBet],
    unplaced: List[UnplacedMechRequest],
) -> List[TimelineEntry]:
    """Merge placed bets and unplaced mech requests into a single timeline."""
    entries: List[TimelineEntry] = []

    for bet in placed_bets:
        entries.append(
            TimelineEntry(
                timestamp=bet.timestamp,
                timestamp_utc=bet.timestamp_utc,
                entry_type="placed_bet",
                data=bet,
            )
        )

    for u in unplaced:
        entries.append(
            TimelineEntry(
                timestamp=u.latest_timestamp,
                timestamp_utc=u.latest_timestamp_utc,
                entry_type="unplaced_mech_request",
                data=u,
            )
        )

    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return entries


# ── Pretty printing ─────────────────────────────────────────────────────────
def print_summary(
    timeline: List[TimelineEntry],
    total_mech: int,
    placed_count: int,
    unplaced_count: int,
    platform: str,
):
    """Print a human-readable summary."""
    currency = "USDC" if platform == "polymarket" else "xDAI"

    print("=" * 90)
    print(f"  MECH REQUEST & TRADE ANALYSIS — {platform.upper()}")
    print("=" * 90)
    print(f"  Total mech requests:    {total_mech}")
    print(f"  Placed (resulted in trade): {placed_count}")
    print(f"  Unplaced (no trade):        {unplaced_count}")
    print(f"  Mech fee per request:       {DEFAULT_MECH_FEE_ETH} ETH")
    print(f"  Total mech fees:            {total_mech * DEFAULT_MECH_FEE_ETH:.4f} ETH")
    print(f"  Wasted mech fees (unplaced): {unplaced_count * DEFAULT_MECH_FEE_ETH:.4f} ETH")
    print("=" * 90)
    print()

    # Stats
    bet_entries = [e for e in timeline if e.entry_type == "placed_bet"]
    won = sum(1 for e in bet_entries if e.data.status == "won")
    lost = sum(1 for e in bet_entries if e.data.status == "lost")
    pending = sum(1 for e in bet_entries if e.data.status == "pending")
    invalid = sum(1 for e in bet_entries if e.data.status == "invalid")

    total_bet_amount = sum(e.data.bet_amount for e in bet_entries)
    total_profit = sum(e.data.net_profit or 0 for e in bet_entries)

    print(f"  BET STATS: {len(bet_entries)} bets | Won: {won} | Lost: {lost} | Pending: {pending} | Invalid: {invalid}")
    print(f"  Total amount bet: {total_bet_amount:.4f} {currency}")
    print(f"  Net profit (bets only): {total_profit:.4f} {currency}")
    print(f"  Net profit (incl mech fees): {total_profit - total_mech * DEFAULT_MECH_FEE_ETH:.4f} {currency}/ETH")
    print()
    print("-" * 90)
    print("  TIMELINE (newest first)")
    print("-" * 90)
    print()

    for i, entry in enumerate(timeline, 1):
        if entry.entry_type == "placed_bet":
            bet: PlacedBet = entry.data
            status_icon = {"won": "+", "lost": "-", "pending": "~", "invalid": "!"}
            icon = status_icon.get(bet.status, "?")

            print(f"  [{icon}] PLACED BET #{i}  |  {entry.timestamp_utc}")
            print(f"      Question: {bet.question_title[:80]}")
            print(f"      Side: {bet.prediction_side.upper()} | Amount: {bet.bet_amount} {currency} | Status: {bet.status.upper()}")
            if bet.net_profit is not None:
                print(f"      Net profit: {bet.net_profit:+.4f} {currency} | Payout: {bet.total_payout} {currency}")
            if bet.settled_at:
                print(f"      Settled at: {bet.settled_at}")
            if bet.transaction_hash:
                print(f"      Tx: {bet.transaction_hash}")
            if bet.mech_requests:
                print(f"      Mech requests for this question: {len(bet.mech_requests)}")
                for mr in bet.mech_requests[:3]:
                    print(f"        - ID: {mr.request_id} | Tool: {mr.tool} | {mr.timestamp_utc}")
            print()

        else:
            unreq: UnplacedMechRequest = entry.data
            print(f"  [x] UNPLACED MECH REQUEST #{i}  |  {entry.timestamp_utc}")
            print(f"      Question: {unreq.question_title[:80]}")
            print(f"      Mech calls: {unreq.mech_request_count} | Wasted fee: {unreq.mech_request_count * DEFAULT_MECH_FEE_ETH:.4f} ETH")
            if unreq.mech_response:
                resp = unreq.mech_response
                p_yes = resp.get("p_yes", "?")
                p_no = resp.get("p_no", "?")
                confidence = resp.get("confidence", "?")
                info_util = resp.get("info_utility", "?")
                print(f"      Mech response: p_yes={p_yes}, p_no={p_no}, confidence={confidence}, info_utility={info_util}")
            if unreq.mech_requests:
                for mr in unreq.mech_requests[:3]:
                    print(f"        - ID: {mr.request_id} | Tool: {mr.tool} | {mr.timestamp_utc}")
                if len(unreq.mech_requests) > 3:
                    print(f"        ... and {len(unreq.mech_requests) - 3} more")
            print()

    print("=" * 90)
    print("  END OF ANALYSIS")
    print("=" * 90)


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Analyse mech requests and trades for a Polystrat/Omen trader agent."
    )
    parser.add_argument("safe_address", help="Agent's Safe multisig address")
    parser.add_argument(
        "--platform",
        choices=["polymarket", "omen"],
        default="polymarket",
        help="Trading platform (default: polymarket)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output as JSON instead of human-readable"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit timeline entries (0 = all)"
    )
    parser.add_argument(
        "--no-responses",
        action="store_true",
        help="Skip fetching mech responses for unplaced requests (faster)",
    )

    args = parser.parse_args()
    safe = args.safe_address.lower()
    is_poly = args.platform == "polymarket"

    agents_url = POLYMARKET_AGENTS_SUBGRAPH if is_poly else OMEN_AGENTS_SUBGRAPH
    mech_url = POLYGON_MECH_SUBGRAPH if is_poly else GNOSIS_MECH_SUBGRAPH

    # 1. Fetch all mech requests
    print(f"Fetching all mech requests from {args.platform} mech subgraph...", file=sys.stderr)
    raw_mech_requests, total_marketplace = fetch_all_mech_requests(mech_url, safe, is_polygon=is_poly)
    print(f"  Found {len(raw_mech_requests)} mech requests (total marketplace: {total_marketplace})", file=sys.stderr)

    # 2. Build mech request lookup by question title
    mech_by_title, mech_counts = build_mech_request_lookup(raw_mech_requests)
    print(f"  Unique question titles: {len(mech_by_title)}", file=sys.stderr)

    # 3. Fetch all placed bets
    print(f"Fetching all bets from {args.platform} agents subgraph...", file=sys.stderr)
    if is_poly:
        raw_bets = fetch_polymarket_bets(agents_url, safe)
        placed_bets = [format_polymarket_bet(b) for b in raw_bets]
    else:
        raw_bets = fetch_omen_bets(agents_url, safe)
        placed_bets = [format_omen_bet(b) for b in raw_bets]

    print(f"  Found {len(placed_bets)} placed bets", file=sys.stderr)

    # 4. Get the set of question titles that had bets placed
    placed_titles: Set[str] = {bet.question_title for bet in placed_bets if bet.question_title}

    # 5. Classify mech requests into placed vs unplaced
    print("Classifying mech requests into placed vs unplaced...", file=sys.stderr)
    placed_mech, unplaced_list = classify_mech_requests(
        mech_by_title,
        mech_counts,
        placed_titles,
        mech_url,
        safe,
        fetch_responses=not args.no_responses,
        is_polygon=is_poly,
    )

    # Attach mech request details to placed bets
    for bet in placed_bets:
        if bet.question_title in placed_mech:
            bet.mech_requests = placed_mech[bet.question_title]

    placed_mech_count = sum(mech_counts.get(t, 0) for t in placed_titles)
    unplaced_mech_count = sum(u.mech_request_count for u in unplaced_list)

    print(f"  Placed mech requests (tied to bets): {placed_mech_count}", file=sys.stderr)
    print(f"  Unplaced mech requests (no trade): {unplaced_mech_count}", file=sys.stderr)
    print(f"  Unplaced unique questions: {len(unplaced_list)}", file=sys.stderr)

    # 6. Build unified timeline
    timeline = build_timeline(placed_bets, unplaced_list)

    if args.limit > 0:
        timeline = timeline[: args.limit]

    # 7. Output
    if args.json:
        output = {
            "safe_address": safe,
            "platform": args.platform,
            "summary": {
                "total_mech_requests": total_marketplace or len(raw_mech_requests),
                "placed_mech_requests": placed_mech_count,
                "unplaced_mech_requests": unplaced_mech_count,
                "total_bets": len(placed_bets),
                "mech_fee_eth": DEFAULT_MECH_FEE_ETH,
                "total_mech_fees_eth": (total_marketplace or len(raw_mech_requests)) * DEFAULT_MECH_FEE_ETH,
                "wasted_mech_fees_eth": unplaced_mech_count * DEFAULT_MECH_FEE_ETH,
            },
            "timeline": [
                {
                    "timestamp": e.timestamp,
                    "timestamp_utc": e.timestamp_utc,
                    "type": e.entry_type,
                    "data": asdict(e.data),
                }
                for e in timeline
            ],
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print_summary(
            timeline,
            total_marketplace or len(raw_mech_requests),
            placed_mech_count,
            unplaced_mech_count,
            args.platform,
        )


if __name__ == "__main__":
    main()
