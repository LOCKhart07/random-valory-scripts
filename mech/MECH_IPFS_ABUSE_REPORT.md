# Mech 0xC05e — Invalid IPFS Requests & OLAS Mining Abuse Report

**Date:** 2026-03-17
**Mech:** `0xC05e7412439bD7e91730a6880E18d5D5873F632C` (Gnosis Chain)

## Executive Summary

27 agent services are sending mech requests with invalid IPFS data to mech `0xC05e...632C`, accounting for **88.5% of the mech's traffic** over the past 7 days. These services have **never placed a single trade** on Omen. The evidence strongly suggests they are modified OmenStrat agents designed to generate mech request activity solely to qualify for OLAS staking rewards — an OLAS mining abuse pattern.

## Scale of the Problem

| Metric | Last 7 days | Last 14 days |
|---|---|---|
| Total requests to mech | 11,315 | 21,585 |
| Requests with valid IPFS data (parsed) | 1,296 (11.5%) | 2,420 (11.2%) |
| Requests with invalid IPFS data (unparsed) | 10,019 (88.5%) | 19,165 (88.8%) |
| Unique broken senders | 27 | 27 |
| Unique healthy senders | 32 | 124 |

### Bet Verification (On-Chain, 14-Day Window)

Verified directly on-chain via `TransferSingle` events on the ConditionalTokens contract (`0xCeAfDD6bc0bEF976fdCd1112955828E00543c0Ce`):

| Group | Addresses | Conditional Token Transfers (14d) |
|---|---|---|
| Broken senders | 27 | **0 across all 27** |
| Healthy senders (sample) | 4 | 50–268 each |

**None of the 27 broken senders have received a single conditional token transfer in 14 days**, confirming they are not placing any bets on Omen. All 4 healthy senders sampled have active betting activity (50–268 transfers each).

## Root Cause

The broken senders emit valid `Request(address,bytes32,bytes)` events on-chain, but the 32-byte `requestData` field does **not** correspond to pinned IPFS content. When the IPFS gateway attempts to resolve the hash, it returns:

```
failed to resolve /ipfs/f01701220...: protobuf: (PBNode) invalid wireType, expected 2, got 3
```

Healthy agents pin their request payload (prompt, tool name, nonce) to IPFS first, then submit the content hash on-chain. The broken agents skip the IPFS pinning step entirely and submit garbage hashes. The mech still delivers responses (it can't validate the IPFS hash before responding), but the subgraph cannot parse the request metadata.

### On-Chain Log Comparison

Both broken and healthy transactions produce identical log structures (6 logs each, same event signatures, same gas usage ~336k). The only difference is the `requestData` bytes — broken ones are not resolvable on IPFS, healthy ones resolve to a directory containing `metadata.json` with prompt, tool, and nonce.

## Service & Staking Analysis

All 27 broken senders are Safe multisig contracts (agent services). They all use **agent ID 25** and cluster into 3 config hashes, indicating 3 slightly different versions of the same modified agent.

### Config Hash & Agent Code Analysis

All services (broken and healthy) use `service/valory/trader_pearl/0.1.0` with the same image hash. Each config hash contains a `code_uri` pointing to an agent package with a `valory/trader` agent hash in its `service.yaml`.

**Important caveat:** The on-chain config hash and agent hash only reflect what was registered at service creation (or last on-chain update). They do **not** indicate what code the operator is actually running locally. The operator can run arbitrary modified code regardless of the on-chain hash. Therefore, the agent hashes cannot be used to determine whether the running code has been modified.

The 3 broken config hashes and 3 healthy config hashes all contain legitimate `valory/trader` agent hashes that exist in the official `trader` repo git history. One broken agent hash is even identical to a healthy service's hash. This tells us nothing about the actual running code — only roughly when the service was registered on-chain.

### Config Hash Details

| Config Hash | Services | Total Lifetime Requests | IPFS Link |
|---|---|---|---|
| `0x2c8140de...` | 12 svcs (1994–2016) | ~270,000 | [link](https://gateway.autonolas.tech/ipfs/f017012202c8140dec99e9768d7592c6d4e40aa9620efc0a7288db810ead98307973f8697) |
| `0x003bc66d...` | 4 svcs (2332–2336) | ~42,500 | [link](https://gateway.autonolas.tech/ipfs/f01701220003bc66dba8cb58bd462f3adff930adea32097c3ebb1ec3dd5eebddac94e1d48) |
| `0x108e9079...` | 11 svcs (2613–2929) | ~25,400 | [link](https://gateway.autonolas.tech/ipfs/f01701220108e90795119d6015274ef03af1a669c6d13ab6acc9e2b2978be01ee9ea2ec93) |

Healthy agents for comparison:
- `0xc06da35f...` — [link](https://gateway.autonolas.tech/ipfs/f01701220c06da35f3cf0b90023247fb1690af6cfcca46c0accf01207d2704f598474efb8) (services 1785, 1791)
- `0xaacd37b3...` — [link](https://gateway.autonolas.tech/ipfs/f01701220aacd37b3ef661700065977eeb1391e29886c069794c68033eef13f0995b49b4d) (service 1800)
- `0x78af2848...` — [link](https://gateway.autonolas.tech/ipfs/f0170122078af2848c970308d13e7005f8183b3d1ebb30770c0c7a8ed3955c1b1b24e85d9) (service 2020)

### Staking Contracts

The 27 services are staked across **5 different staking contracts**, all with significant OLAS reward pools:

| Staking Contract | # Broken Services | Available OLAS Rewards | Max Services |
|---|---|---|---|
| `0xdDa9cD21...65C8` | 11 | 79,081 OLAS | 50 |
| `0x53a38655...7156` | 10 | 68,640 OLAS | 50 |
| `0xaaEcdf4d...a5f3` | 3 | 56,262 OLAS | 50 |
| `0x22D6cd3d...4A85` (QS Beta MMM Expert 6) | 2 | 41,772 OLAS | 50 |
| `0xcdC603e0...f7d` | 1 | 30,826 OLAS | 50 |
| **Total** | **27** | **~276,581 OLAS** | |

### Behavioral Pattern

All broken senders follow a highly uniform pattern:
- **Exactly 63 requests per day** per service (441/week)
- Operate in **time-windowed cohorts** (groups of 2–12 agents active simultaneously)
- Request frequency within a window: 0.7–3.4 minutes between requests
- Each multisig has **one dedicated EOA operator** (no shared operators)
- xDAI balances: 2.5–9.9 xDAI per multisig (enough to sustain mech request fees)

## Complete Service Inventory

### Broken Services (27)

| Service ID | Multisig | Staking Contract | Config Hash | Lifetime Requests |
|---|---|---|---|---|
| 1994 | `0x2d94...cc43` | `0xdDa9...65C8` | `0x2c81...8697` | 23,045 |
| 1995 | `0xb269...2943` | `0xdDa9...65C8` | `0x2c81...8697` | 22,999 |
| 2005 | `0x7d3f...25b2` | `0x53a3...7156` | `0x2c81...8697` | 22,680 |
| 2006 | `0x7175...6273` | `0x53a3...7156` | `0x2c81...8697` | 22,685 |
| 2007 | `0x8ed5...59a8` | `0x53a3...7156` | `0x2c81...8697` | 22,653 |
| 2008 | `0xb335...1265` | `0x53a3...7156` | `0x2c81...8697` | 22,682 |
| 2011 | `0x5a7a...4302` | `0x53a3...7156` | `0x2c81...8697` | 22,624 |
| 2012 | `0x31cf...700f` | `0xdDa9...65C8` | `0x2c81...8697` | 22,625 |
| 2013 | `0x57ed...c6fb` | `0x53a3...7156` | `0x2c81...8697` | 22,667 |
| 2014 | `0x0931...6227` | `0xdDa9...65C8` | `0x2c81...8697` | 22,629 |
| 2015 | `0x9a3c...a898` | `0xdDa9...65C8` | `0x2c81...8697` | 22,620 |
| 2016 | `0x53b0...b1ae9` | `0xdDa9...65C8` | `0x2c81...8697` | 22,608 |
| 2332 | `0x127e...63a` | `0xdDa9...65C8` | `0x003b...1d48` | 10,677 |
| 2334 | `0xdf9e...5285` | `0xaaEc...a5f3` | `0x003b...1d48` | 10,612 |
| 2335 | `0x960f...721c` | `0xaaEc...a5f3` | `0x003b...1d48` | 10,617 |
| 2336 | `0x4cca...2f6e` | `0xaaEc...a5f3` | `0x003b...1d48` | 10,620 |
| 2613 | `0xb9cb...fad4` | `0x53a3...7156` | `0x108e...ec93` | 3,858 |
| 2625 | `0x72b4...78b` | `0xdDa9...65C8` | `0x108e...ec93` | 3,748 |
| 2626 | `0x8f64...359d` | `0xcdC6...f7d` | `0x108e...ec93` | 3,750 |
| 2628 | `0xdf51...f7ce` | `0x22D6...4A85` | `0x108e...ec93` | 3,804 |
| 2642 | `0x8169...c502` | `0x53a3...7156` | `0x108e...ec93` | 2,778 |
| 2643 | `0x4064...c850` | `0x53a3...7156` | `0x108e...ec93` | 2,781 |
| 2666 | `0xb950...48cd` | `0x22D6...4A85` | `0x108e...ec93` | 2,086 |
| 2669 | `0x3467...611d` | `0x53a3...7156` | `0x108e...ec93` | 1,960 |
| 2912 | `0x0e36...9b66` | `0xdDa9...65C8` | `0x108e...ec93` | 630 |
| 2928 | `0xf172...d3bc` | `0xdDa9...65C8` | `0x108e...ec93` | 63 |
| 2929 | `0xdc0d...2e68` | `0xdDa9...65C8` | `0x108e...ec93` | 63 |

### Healthy Services (for comparison)

| Service ID | Multisig | Config Hash | 7d Requests | Trades |
|---|---|---|---|---|
| 1785 | `0xd4a5...0d8` | `0xc06d...efb8` | 427 | Yes |
| 1791 | `0x5afc...0489` | `0xc06d...efb8` | 425 | Yes |
| 1800 | `0xefe1...46c` | `0xaacd...b4d` | 42 | Yes |
| 2020 | `0x8ad8...f31e` | `0x78af...85d9` | 31 | Yes |

## EOA Operators

Each broken multisig has a unique EOA operator (27 total). Sample mapping:

| EOA | Multisig (Service) |
|---|---|
| `0x24588c1fa596...` | `0xb950...` (Svc 2666) |
| `0x63405cdd2ee0...` | `0xdf51...` (Svc 2628) |
| `0x296f30eae0a8...` | `0x9a3c...` (Svc 2015) |
| ... | (24 more, all unique) |

## Conclusions

1. **OLAS mining abuse confirmed**: 27 services generate fake mech request activity without placing any bets, purely to earn staking rewards.

2. **On-chain config hashes are not evidence of code modification**: The on-chain agent hash only reflects what was registered at service creation, not what's actually running. The operators can run arbitrary code locally. We cannot determine from on-chain data alone whether the agent code was modified — only that the behavior (no IPFS pinning, no bets, garbage request data) is clearly abnormal.

3. **Systematic operation**: The uniform 63 requests/day cadence, time-windowed cohorts, and spread across 5 staking contracts indicate a deliberate, organized operation.

4. **Impact**: These services consume 88.5% of the mech's request capacity, generate ~276k OLAS in potential staking rewards, and pollute the subgraph data with unparseable requests.

---

*Generated by analysis scripts in `mech/check_mech_requests_ipfs.py`*
