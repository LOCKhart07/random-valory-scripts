# PolyStrat Fleet Divergence Analysis Report

**Date:** 2026-03-17 (updated 2026-03-18)
**Subject:** Why identical PolyStrat agents show persistent performance divergence
**Focus agent:** Thomas (`0x33d20338f1700eda034ea2543933f94a2177ae4c`)
**Fleet size:** 98 registered agents, 91 with bets, 73 with 10+ resolved bets

---

## Executive Summary

All PolyStrat agents run identical code, see the same markets, and share the same initial configuration. Despite this, some agents persistently outperform while others persistently underperform. Thomas' agent is -$81 (2nd worst in fleet) and has never had a single profitable week across 7 weeks.

**Root cause:** The IPFS accuracy store (`QmR8etyW3TPFadNtNrW54vfnFqmh8vBrMARWV76EmxCZyk`) pre-loads `prediction-request-reasoning` as the highest-weighted tool from initialization. Agents are locked into PRR from **bet #1**, not bet #36 as previously reported. The lock-in is not caused by early random exploration — it is baked into the initial configuration.

| Tool | Fleet Bets | Accuracy | Fleet PnL |
|---|---|---|---|
| `superforcaster` | 406 | 73.4% | **+$94** |
| `prediction-request-reasoning` | 6,155 | 63.1% | **-$930** |

**Why PRR wins the initial store:** The IPFS CSV contains Omen-era accuracy data (April–June 2024). PRR has 67.1% accuracy and 17,372 requests (28% of all volume). The volume regularization term (`+0.1 * requests/total`) pushes PRR's weighted accuracy above `prediction-offline` (67.4% accuracy but only 7.2% volume share). `superforcaster` is **not in the CSV at all** — it gets added with 0 accuracy and 0 requests at startup. The 75% exploitation rate then selects PRR on 75% of all bets from the very first round, with SF only reachable via the 25% random exploration phase (~2.5% chance per bet given ~10 tools).

On-chain verification (30-agent sample) confirms this: **83% of agents use PRR on their literal first bet**, and PRR averages 77.6% of tool selections across all agents' first 10 bets — matching the expected 75% exploit + 2.5% explore rate almost exactly.

63% accuracy is below breakeven at 71% of the price ranges PRR encounters. Agents locked into PRR bleed money. The store never self-corrects because PRR's 63% isn't bad enough to get displaced, just bad enough to lose at the prices being paid.

**Secondary cause:** The price filter (0.80 threshold) allows deeply negative EV bets on both tails. Tightening to 0.70 would save ~$665 fleet-wide.

---

## What Was Checked

### 1. Fleet-Wide Statistical Divergence (`analyze_divergence.py`)

**Market overlap:**
- 1,317 unique markets bet on across the fleet
- Average pairwise Jaccard similarity: 0.096 (very low — agents bet on mostly different markets)
- 70.5% of markets have 2+ agents, but any given pair only overlaps ~10%

**Same-market comparison (929 shared markets):**
- Outcome agreement rate: 69.3% (agents agree on which side to bet 69% of the time)
- Average share price spread between agents: 0.205
- Bet amount ratio (max/min): 3.2x

**Tool assignment:**
- `prediction-request-reasoning` dominates: 78% of fleet bets
- `superforcaster` is the best tool (73.9% accuracy) but only 5% of fleet bets
- Tool distribution varies significantly across agents (stdev 14% for PRR)

**Entry timing:**
- Early vs late entry accuracy: 62.9% vs 63.0% — no effect
- Entry timing vs PnL: rho=0.122 — weak, not a factor

**Convergence test (4 time windows):**
- Rank autocorrelation: rho=0.052 — accuracy ranks shuffle (weak persistence)
- BUT PnL persistence is real (see below)

### 2. Deep Single-Agent Analysis (`analyze_agent_deep.py`)

**Thomas' agent (0x33d20338...):**
- 475 bets, 427 resolved, 60.9% accuracy
- PnL: -$80.72, never recovered, 0th percentile in fleet
- Every week negative: -$49, -$3, -$13, -$8, -$3, -$5

**Head-to-head on 350 shared markets:**
- Thomas accuracy: 60.0% vs fleet 62.4% — only 2.4 percentage points worse
- 89.3% outcome agreement with fleet
- Thomas PnL on shared markets: -$59

**Longshot exposure (share price < 0.30):**
- 54 bets at 9.3% accuracy costing -$58.80 (73% of total loss)
- Thomas: 12.6% longshot exposure vs fleet mean 7.9%

**Tool distribution:**
- 81% `prediction-request-reasoning`, only 1.5% `superforcaster`
- SF accuracy when Thomas used it: 85.7% (7/8 wins) — great, but too few uses

**Price bucket breakdown:**

| Price Range | Bets | Accuracy | PnL |
|---|---|---|---|
| Longshot (0-0.30) | 54 | 9.3% | -$58.80 |
| Underdog (0.30-0.50) | 54 | 33.3% | -$13.78 |
| Slight fav (0.50-0.70) | 85 | 60.0% | -$3.87 |
| Favorite (0.70-0.85) | 177 | 74.0% | -$8.01 |
| Heavy fav (0.85-1.00) | 57 | 96.5% | +$3.74 |

### 3. Path Persistence Tests (`analyze_persistence.py`)

**Quartile stickiness (8 time windows):**
- Accuracy quartile retention: 26.9% (random baseline: 25%) — not sticky
- PnL quartile retention: 26.4% — not sticky
- But 10 agents have ZERO profitable weeks including Thomas

**First-half vs second-half correlation:**
- Accuracy rank correlation: -0.014 (zero predictive power)
- PnL rank correlation: 0.124 (weak)
- Bottom quartile retention: 37.5% stayed bottom (vs 25% random)

**Recovery analysis (65 agents with 20+ bets):**
- 64/65 went negative at some point
- 28 (43.8%) never recovered to positive
- Thomas: max drawdown $81.90, negative for all 427 consecutive bets

**Market difficulty exposure:**
- Longshot exposure varies: mean 7.9%, stdev 5.1%, range 0-24%
- Thomas: 12.6% (above average)

### 4. Tool Usage vs Performance Comparison

**Top 10 agents (by PnL) vs Bottom 10:**

| Metric | Bottom 10 | Top 10 |
|---|---|---|
| Avg PnL | -$58.71 | +$16.85 |
| Longshot exposure | 11.5% | 7.2% |
| Max-bet exposure | 50.3% | 65.2% |
| `superforcaster` usage | 4.0% | 13.7% |
| PRR accuracy | 60.4% | 67.5% |

**Superforcaster usage vs profitability:**

| SF Usage | Agents | Avg PnL | Profitable |
|---|---|---|---|
| High (>=10%) | 7 | -$8.89 | 3/7 (43%) |
| Mid (3-10%) | 20 | -$15.77 | 3/20 (15%) |
| Low (<3%) | 45 | -$15.07 | 9/45 (20%) |

SF helps (43% profitable vs 15-20%) but is not a guarantee. Some zero-SF agents are profitable with high PRR accuracy (e.g., 78% PRR accuracy → +$5.38).

**Correlation analysis:**
- `superforcaster %` vs PnL: rho=-0.028 (no direct correlation)
- `longshot %` vs PnL: rho=-0.433 (strong)
- `longshot %` vs accuracy: rho=-0.556 (very strong)
- SF usage does NOT correlate with PRR accuracy (rho=0.037) — independent effects

### 5. Same-Market Side Selection

**Do agents bet on the same markets?**
- 0 markets have all 91 agents
- Only 6 markets have 50%+ of agents
- Median: 3 agents per market

**When they share a market, do they pick the same side?**
- Median disagreement: 0% (near-unanimous on shared markets)
- 69% of shared markets have <10% minority
- Thomas is on the minority side 7.8% of the time (fleet avg 8.1%)

**Minority side vs PnL: rho=0.038 — not a factor**

### 6. Deep Mechanism Analysis (`analyze_persistence_deep.py`)

**H1: Accuracy Store Feedback Loop — CONFIRMED (corrected with real IPFS store initialization)**
- **100% of agents start with PRR as `best_tool`** — determined by the pre-loaded IPFS accuracy store
- SF can overtake PRR once it accumulates a few winning bets (SF's real 72.6% accuracy exceeds PRR's stored 67.1% + volume bonus ≈ 70%), but it only gets ~2.5% selection rate via exploration
- **61% of agents (50/82) eventually see SF overtake PRR** in the store, at median bet #50
- **39% of agents never switch** — they either didn't get enough SF exploration bets, or SF's early bets were losses
- Final best_tool distribution: PRR 55% (45 agents), SF 45% (37 agents)
- The bottom 3 agents by PnL ALL ended up with SF as best_tool — but the losses accumulated during the PRR-dominant early phase couldn't be recovered
- Early luck (first 10 bets accuracy) vs final PnL: rho=0.263

**H2: Kelly Dynamic Fraction Amplification — MINOR**
- Dynamic fraction range: 1.516-1.519 (barely varies)
- Total fleet impact: $11.40 — negligible

**H3: Tool Quarantine Signals — MODERATE**
- 27/73 agents show quarantine-signature gaps
- Early superforcaster usage vs PnL: rho=0.203

**H4: Tool-Specific Longshot Exposure — CONFIRMED**
- PRR longshot rate: 8.5%
- SF longshot rate: 4.9%
- PRR longshot PnL: -$315 vs SF longshot PnL: -$19

**H5: Price Threshold Gap — BIGGEST DOLLAR IMPACT**
- Current threshold 0.80 allows deeply negative EV bets in 0.20-0.40 range
- Counterfactual at 0.70: saves $665 fleet-wide (63% of losses)
- Counterfactual at 0.75: saves $563

**H6: Minimum Edge — NOT THE ISSUE**
- Thin-edge zone (0.45-0.55) is actually profitable (+$151)
- Removing thin-edge bets makes things worse

### 7. PRR Breakeven Analysis — The Key Finding

**PRR is below breakeven at 71% of price ranges it encounters.**

At high share prices, the win/loss asymmetry is brutal:
- At 0.75: win pays $0.33, loss costs $1.00. Need 75% accuracy. PRR gets 74.8%.
- At 0.80: win pays $0.25, loss costs $1.00. Need 80% accuracy. PRR gets 78.3%.
- At 0.60: win pays $0.67, loss costs $1.00. Need 60% accuracy. PRR gets 56.6%.

**SF at the same prices is 15-24% more accurate in the critical 0.30-0.70 range:**

| Price Range | PRR Accuracy | SF Accuracy | PRR PnL | SF PnL |
|---|---|---|---|---|
| 0.30-0.40 | 26.6% | 41.7% | -$221 | +$0.13 |
| 0.40-0.50 | 45.5% | 69.0% | -$18 | +$38 |
| 0.50-0.60 | 52.6% | 70.0% | -$35 | +$28 |
| 0.60-0.70 | 62.9% | 78.5% | -$134 | +$34 |

**PRR decomposition:**
- Bets below breakeven: 4,378 bets → -$1,085 PnL
- Bets above breakeven: 1,777 bets → +$155 PnL
- Net: -$930

**SF decomposition:**
- Bets below breakeven: 191 bets → -$36 PnL
- Bets above breakeven: 214 bets → +$131 PnL
- Net: +$94

### 8. IPFS Accuracy Store Verification (`verify_lockin.py`) — CORRECTS H1

**The original H1 simulation was wrong.** `analyze_persistence_deep.py` initialized the accuracy store with `{requests: 0, accuracy: 0.0}` for each tool — a blank slate. In reality, agents start with a pre-loaded IPFS accuracy CSV.

**Contents of IPFS accuracy store (`QmR8etyW3TPFadNtNrW54vfnFqmh8vBrMARWV76EmxCZyk`):**

| Tool | Accuracy (%) | Requests | Volume Share | Weighted Acc (raw) |
|---|---|---|---|---|
| **prediction-request-reasoning** | 67.11 | **17,372** | **28.2%** | **0.6993 (#1)** |
| prediction-online-sme | 65.67 | 14,642 | 23.7% | 0.6805 (#2) |
| prediction-offline | **67.41** | 4,465 | 7.2% | 0.6813 (#3) |
| prediction-online | 66.01 | 9,490 | 15.4% | 0.6755 (#4) |
| prediction-request-rag-claude | 65.64 | 7,428 | 12.0% | 0.6684 |
| prediction-request-reasoning-claude | 66.72 | 2,470 | 4.0% | 0.6712 |
| prediction-request-rag | 63.58 | 2,691 | 4.4% | 0.6402 |
| prediction-url-cot-claude | 61.90 | 1,596 | 2.6% | 0.6216 |
| claude-prediction-online | 61.14 | 1,055 | 1.7% | 0.6131 |
| claude-prediction-offline | 57.38 | 481 | 0.8% | 0.5746 |
| **superforcaster** | **NOT PRESENT** | **0** | **0%** | **0.0000** |

Note: `prediction-offline-sme` (70.49%, 61 requests) is in the CSV but filtered out by `irrelevant_tools`.

**PRR is not the most accurate tool — `prediction-offline` is (67.41% vs 67.11%). PRR wins `best_tool` because the volume regularization bonus (`+0.1 * 17372/61690 = +0.028`) pushes it above `prediction-offline` (`+0.1 * 4465/61690 = +0.007`).** The IPFS data is from Omen-era trading (April–June 2024), so PRR's volume dominance reflects Omen history, not Polymarket performance.

**On-chain verification (30 agents, `verify_lockin.py`):**

| Metric | Value |
|---|---|
| Agents using PRR on first bet | **83%** (25/30) |
| PRR rate in first 10 bets | **77.6%** (expected: 77.5% if PRR = best_tool) |
| PRR rate in first 20 bets | **78.3%** |
| PRR rate in first 50 bets | **79.2%** |
| SF rate in first 10 bets | **26.7%** (only among 6 agents that used it) |
| SF rate in first 50 bets | **9.4%** |
| Median bet at which PRR >75% cumulative | **10** (not 36) |
| Agents with PRR >= 60% in first 10 bets | **27/30** (90%) |
| Mean unique tools in first 20 bets | **4.2** |

**First bet tool distribution:**
- `prediction-request-reasoning`: 83.3%
- `prediction-online-sme`: 6.7%
- `claude-prediction-online`: 6.7%
- `prediction-request-rag`: 3.3%
- `superforcaster`: 0%

**Conclusion:** Lock-in is not a gradual process. It is instantaneous — determined by the IPFS accuracy store before a single bet is placed. The "~36 bet lock-in" from the earlier simulation was an artifact of starting from an empty store. The real question is not "when does lock-in happen" but "why does the IPFS store favor PRR." Answer: stale Omen-era volume counts in the regularization term.

### 9. Cross-Market Accuracy Comparison (`generate_accuracy_csv.py`)

**Tool rankings are market-specific.** Generating fresh accuracy CSVs from on-chain data for both markets reveals that the same tools perform very differently on Omen vs Polymarket:

| Tool | IPFS Store (Omen Apr-Jun '24) | Omen (Feb '26) | Polymarket (all time) |
|---|---|---|---|
| superforcaster | N/A | 57.84% | **72.58%** |
| prediction-request-reasoning-claude | 66.72% | **63.53%** | 67.53% |
| prediction-offline | **67.41%** | 62.47% | 64.67% |
| prediction-request-reasoning | 67.11% | 56.25% | 62.33% |
| claude-prediction-offline | 57.38% | 52.88% | 66.50% |
| prediction-online | 66.01% | 43.99% | 60.22% |
| prediction-online-sme | 65.67% | 32.37% | 60.91% |
| prediction-request-rag-claude | 65.64% | 29.28% | 62.92% |
| claude-prediction-online | 61.14% | 27.30% | 62.71% |
| prediction-request-rag | 63.58% | 43.15% | 55.14% |

**Key observations:**
- **SF dominates Polymarket (72.6%) but is mediocre on Omen (57.8%).** Tool performance is market-dependent.
- **The old IPFS CSV is stale for both markets.** Most tools dropped 5-35 percentage points on recent Omen data vs the Apr-Jun 2024 snapshot. Half the tools (prediction-online-sme, prediction-request-rag-claude, claude-prediction-online) collapsed to below 33%.
- **A single accuracy CSV for both Omen and Polymarket services would be wrong.** The `tools_accuracy_hash` in service.yaml should be market-specific.
- **Updated CSVs have been generated and pinned to IPFS:**
  - Polymarket: `QmdNF1cidJASsVKSnbvSSmZLLaYfBPixBzpT4Pw3ZvmYTu`
  - Omen: `tool-accuracy/tools_accuracy_omen.csv` (pin with `--pin` flag)

---

## The Persistence Mechanism (Full Chain) — REVISED

1. **All agents start with PRR pre-loaded as `best_tool`** — the IPFS accuracy store CSV contains Omen-era data (April–June 2024) where PRR had the most requests (17,372). The volume regularization bonus makes PRR the highest-weighted tool before any Polymarket betting. Superforcaster is not in the CSV and starts at 0/0.

2. **PRR is selected on 75-80% of bets from bet #1.** This is not gradual lock-in — it's the default state. On-chain data shows 83% of agents use PRR on their literal first bet. The epsilon-greedy policy selects `best_tool` (PRR) during the 75% exploitation phase and picks randomly among ~10 tools during the 25% exploration phase. This means PRR gets ~77.5% of all bets and SF gets ~2.5%.

3. **SF can overtake PRR, but only after ~50 bets.** SF's real accuracy (72.6%) exceeds PRR's stored weighted accuracy (~70%), so even 1-2 SF wins are enough to make SF the store's best_tool. But at ~2.5% exploration rate, it takes a median of 50 bets to accumulate enough SF selections. 61% of agents eventually see SF overtake PRR; 39% never do (bad luck on their few SF exploration picks). By the time SF takes over, agents have already lost money on ~40 PRR bets.

4. **PRR's 63% accuracy bleeds money at fleet prices.** PRR is below breakeven at 71% of the price ranges it encounters. The accuracy looks decent in isolation but is insufficient at the prices being paid. The store never self-corrects because PRR's accuracy isn't bad enough to trigger quarantine — just bad enough to lose at the odds offered.

5. **SF lock-in ≠ guaranteed profits.** SF is above breakeven at most price ranges and is the only net-profitable tool fleet-wide (+$94). However, 4 out of 7 high-SF agents (>=10% usage) still lose money. Even high-SF agents still use PRR for the majority of their bets.

6. **The volume regularization term is the tipping mechanism.** By raw accuracy, `prediction-offline` (67.41%) beats PRR (67.11%). But the `0.1 * requests/total_requests` bonus gives PRR +0.028 vs prediction-offline's +0.007, making PRR the winner. This means the fleet's tool selection is determined by a stale volume count from 2024, not by which tool is actually most accurate.

7. **PRR accuracy varies wildly across agents (48% to 88%).** Some agents with high PRR accuracy are profitable without SF (e.g., `0x433a5adb` has 88% PRR accuracy and is the most profitable agent at +$44 with only 4.5% SF usage). The pre-loaded store explains why PRR dominates, but PRR's non-deterministic accuracy across agents determines which agents survive.

---

## Recommended Fixes (by estimated impact)

### 1. Update the IPFS accuracy store per market (HIGHEST IMPACT — root cause fix)
The IPFS CSV (`QmR8etyW3TPFadNtNrW54vfnFqmh8vBrMARWV76EmxCZyk`) contains stale Omen-era data from April–June 2024. It doesn't include superforcaster at all, and tool rankings have shifted dramatically since then — even on Omen itself.

**Updated CSVs generated from on-chain data:**
- **Polymarket:** `QmdNF1cidJASsVKSnbvSSmZLLaYfBPixBzpT4Pw3ZvmYTu` — SF is #1 at 72.6%
- **Omen:** generate with `python tool-accuracy/generate_accuracy_csv.py --pin` — `prediction-request-reasoning-claude` is #1 at 63.5%, SF is 57.8%

**These must be separate hashes per service.** Tool performance is market-specific (SF is 72.6% on Polymarket vs 57.8% on Omen). Using a single CSV across both services will bias one market's agents toward the wrong tool. The `polymarket_trader/service.yaml` and `trader/service.yaml` should each have their own `tools_accuracy_hash`.

Scripts to regenerate:
```bash
python polymarket/generate_accuracy_csv.py --pin         # Polymarket
python tool-accuracy/generate_accuracy_csv.py --pin      # Omen
```

### 2. Tighten price filter from 0.80 to 0.70 (~$665 savings)
Both tails (below 0.30 and above 0.70) are deeply negative EV. The filter already works symmetrically — setting it to 0.70 cuts bets where one side is above 0.70 OR below 0.30. This eliminates the worst-performing price ranges with zero code complexity.

### 3. Increase exploration rate temporarily
The current epsilon of 0.25 gives each non-best tool ~2.5% selection rate. Temporarily increasing epsilon to 0.5 or higher would give SF more exposure to build a track record in the store. Once SF accumulates enough wins, it can overtake PRR organically. This is a band-aid, not a fix — the real solution is #1.

### 4. Add minimum edge requirement
Currently any expected profit > 0 triggers a bet. Adding a requirement like `predicted_probability > share_price + 0.05` would filter out low-conviction bets. Note: the 0.45-0.55 zone is actually profitable, so the edge threshold should be tuned carefully.

### 5. Cap Kelly sizing more aggressively on extreme prices
The $2.50 max bet hits on longshot bets where the model is wrong 90% of the time. Scaling the max bet down for prices outside 0.40-0.60 would reduce the damage from miscalibrated predictions.

---

## Scripts Created

Analysis scripts are in `polymarket/`, accuracy CSV generators in both `polymarket/` and `tool-accuracy/`:

```bash
# Fleet-wide divergence analysis
python polymarket/analyze_divergence.py --focus 0x33d20338f1700eda034ea2543933f94a2177ae4c

# Deep single-agent analysis
python polymarket/analyze_agent_deep.py 0x33d20338f1700eda034ea2543933f94a2177ae4c

# Path persistence tests
python polymarket/analyze_persistence.py

# Deep mechanism analysis (accuracy store simulation, Kelly amplification, etc.)
python polymarket/analyze_persistence_deep.py

# IPFS accuracy store verification — checks on-chain tool usage vs pre-loaded store
python polymarket/verify_lockin.py --sample 30

# Generate updated accuracy CSV from on-chain data and pin to IPFS
python polymarket/generate_accuracy_csv.py --pin                          # Polymarket
python polymarket/generate_accuracy_csv.py --from 2026-03-01 --pin        # Polymarket, recent
python tool-accuracy/generate_accuracy_csv.py --pin                       # Omen
python tool-accuracy/generate_accuracy_csv.py --from 2026-01-01 --pin     # Omen, recent

# Analysis scripts support --json, --no-charts, --no-tools, --min-bets flags
```

---

## Data Sources

- Polymarket bets subgraph (`predict-polymarket-agents.subgraph.autonolas.tech`)
- Omen bets subgraph (`predict-omen`, staging)
- Polygon registry subgraph (The Graph, agentId=86)
- Polygon marketplace subgraph (`marketplace-polygon`) for Polymarket mech tool matching
- Gnosis marketplace subgraph (`mech-marketplace-gnosis`) for Omen mech tool matching
- **IPFS accuracy store CSV** (`QmR8etyW3TPFadNtNrW54vfnFqmh8vBrMARWV76EmxCZyk`) — the pre-loaded tool accuracy data from `tools_accuracy_hash` in service.yaml
- Trader codebase (`trader/packages/valory/skills/decision_maker_abci/policy.py`) for accuracy store logic
- Trader codebase (`trader/packages/valory/skills/decision_maker_abci/behaviours/storage_manager.py`) for store initialization flow
- Trader service config (`trader/packages/valory/services/polymarket_trader/service.yaml`) for parameters and `irrelevant_tools`
- Mech-predict codebase (`mech-predict/packages/`) for tool implementation details
