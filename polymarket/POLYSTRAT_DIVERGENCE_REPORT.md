# PolyStrat Fleet Divergence Analysis Report

**Date:** 2026-03-17
**Subject:** Why identical PolyStrat agents show persistent performance divergence
**Focus agent:** Thomas (`0x33d20338f1700eda034ea2543933f94a2177ae4c`)
**Fleet size:** 98 registered agents, 91 with bets, 73 with 10+ resolved bets

---

## Executive Summary

All PolyStrat agents run identical code, see the same markets, and share the same initial configuration. Despite this, some agents persistently outperform while others persistently underperform. Thomas' agent is -$81 (2nd worst in fleet) and has never had a single profitable week across 7 weeks.

**Root cause:** The epsilon-greedy accuracy store locks agents into different mech tools within ~36 bets based on early random tool selections. The two dominant tools have dramatically different economics:
1
| Tool | Fleet Bets | Accuracy | Fleet PnL |
|---|---|---|---|
| `superforcaster` | 406 | 73.4% | **+$94** |
| `prediction-request-reasoning` | 6,155 | 63.1% | **-$930** |

63% accuracy is below breakeven at 71% of the price ranges PRR encounters. Agents locked into PRR bleed money. Agents locked into SF are more likely to stay profitable — though SF is not a silver bullet (4 out of 7 high-SF agents still lose money). The store never self-corrects because PRR's 63% isn't bad enough to get displaced, just bad enough to lose at the prices being paid.

The underlying issue is that PRR accuracy varies widely across agents (48% to 88%). Some agents with high PRR accuracy are profitable without SF. The tool lock-in explains the persistence mechanism, but PRR being too inaccurate at the prices being paid across most of the fleet is the fundamental problem.

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

**H1: Accuracy Store Feedback Loop — CONFIRMED**
- Store lock-in round: mean=64, median=36
- Early luck (first 10 bets accuracy) vs final PnL: rho=0.282
- Only 2/75 agents end up with PRR as best tool in their store
- Top agents had 80-90% early accuracy, bottom agents had 50%

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

---

## The Persistence Mechanism (Full Chain)

1. **All agents start identical** — same code, same IPFS accuracy store, same markets

2. **Seed divergence (bet #1-10):** Each agent processes at slightly different times → different drand beacon values → different random tool picks during the 25% exploration phase. ONE different pick is enough.

3. **Tool accuracy diverges predictions:** SF (73% accuracy, 4.9% longshot rate) gives different predictions than PRR (63%, 8.5% longshots) on the same market. Different tools literally recommend different sides.

4. **Accuracy store locks in (by bet #36):** Whichever tool wins early gets a higher weighted_accuracy → exploitation (75% of rounds) picks it more → more data reinforces it → store locks.

5. **PRR lock-in = persistent losses:** PRR is below breakeven at 71% of price ranges. The 63% accuracy looks decent but is insufficient at the prices being paid. The store never self-corrects because PRR's accuracy isn't bad enough to trigger displacement — just bad enough to bleed money.

6. **SF lock-in ≠ guaranteed profits:** SF is above breakeven at most price ranges and is the only net-profitable tool fleet-wide (+$94). However, 4 out of 7 high-SF agents (>=10% usage) are still losing money. SF helps but does not guarantee profitability — agents can still lose on their PRR bets (which still make up the majority even for high-SF agents).

7. **PRR accuracy varies wildly across agents (48% to 88%).** Some agents with high PRR accuracy are profitable without SF (e.g., `0x433a5adb` has 88% PRR accuracy and is the most profitable agent at +$44 with only 4.5% SF usage). The tool lock-in explains the persistence mechanism, but the non-determinism of PRR predictions across agents is the deeper source of divergence.

8. **Volume regularization reinforces the lock:** The `0.1 * requests/n_requests` bonus in weighted_accuracy gives the most-used tool a small extra advantage, making it even harder for less-used tools to overtake.

---

## Recommended Fixes (by estimated impact)

### 1. Tighten price filter from 0.80 to 0.70 (~$665 savings)
Both tails (below 0.30 and above 0.70) are deeply negative EV. The filter already works symmetrically — setting it to 0.70 cuts bets where one side is above 0.70 OR below 0.30. This eliminates the worst-performing price ranges with zero code complexity.

### 2. Increase superforcaster usage (with caveats)
SF is the only net-profitable tool fleet-wide (+$94 vs PRR's -$930), but it's not a silver bullet — 4/7 high-SF agents still lose money. Still worth increasing exposure:
- Increase exploration rate (epsilon) temporarily to give all agents more SF exposure
- Weight initial accuracy store to favor SF
- Note: the real fix may be improving PRR's accuracy or calibration, since PRR dominates 78% of all bets regardless of store state

### 3. Add minimum edge requirement
Currently any expected profit > 0 triggers a bet. Adding a requirement like `predicted_probability > share_price + 0.05` would filter out low-conviction bets. Note: the 0.45-0.55 zone is actually profitable, so the edge threshold should be tuned carefully.

### 4. Cap Kelly sizing more aggressively on extreme prices
The $2.50 max bet hits on longshot bets where the model is wrong 90% of the time. Scaling the max bet down for prices outside 0.40-0.60 would reduce the damage from miscalibrated predictions.

---

## Scripts Created

All scripts are in `polymarket/` and run standalone:

```bash
# Fleet-wide divergence analysis
python polymarket/analyze_divergence.py --focus 0x33d20338f1700eda034ea2543933f94a2177ae4c

# Deep single-agent analysis
python polymarket/analyze_agent_deep.py 0x33d20338f1700eda034ea2543933f94a2177ae4c

# Path persistence tests
python polymarket/analyze_persistence.py

# Deep mechanism analysis (accuracy store simulation, Kelly amplification, etc.)
python polymarket/analyze_persistence_deep.py

# All support --json, --no-charts, --no-tools, --min-bets flags
```

---

## Data Sources

- Polymarket bets subgraph (`predict-polymarket-agents.subgraph.autonolas.tech`)
- Polygon registry subgraph (The Graph, agentId=86)
- Polygon marketplace subgraph (`marketplace-polygon`) for mech tool matching
- Trader codebase (`trader/packages/valory/skills/decision_maker_abci/policy.py`) for accuracy store logic
- Trader service config (`trader/packages/valory/services/polymarket_trader/service.yaml`) for parameters
- Mech-predict codebase (`mech-predict/packages/`) for tool implementation details
