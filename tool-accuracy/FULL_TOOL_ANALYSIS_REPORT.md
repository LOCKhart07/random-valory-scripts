# Full Tool Analysis Report — OmenStrat & PolyStrat

**Generated from production accuracy endpoint.**

## 1. Overall Tool Accuracy

### OmenStrat

| Tool | Bets | Correct | Accuracy | 95% CI | vs 50% |
|------|------|---------|----------|--------|--------|
| superforcaster | 4445 | 2240 | 50.4% | [48.9%, 51.9%] | Includes 50% |
| prediction-request-reasoning | 2984 | 1792 | 60.1% | [58.3%, 61.8%] | Above |
| prediction-offline | 797 | 546 | 68.5% | [65.2%, 71.6%] | Above |
| prediction-online | 490 | 296 | 60.4% | [56.0%, 64.6%] | Above |
| prediction-request-rag | 443 | 184 | 41.5% | [37.0%, 46.2%] | **Below** |
| claude-prediction-offline | 401 | 255 | 63.6% | [58.8%, 68.2%] | Above |
| prediction-request-reasoning-claude | 364 | 215 | 59.1% | [53.9%, 64.0%] | Above |
| prediction-request-rag-claude | 5 | 1 | 20.0% | [3.6%, 62.4%] | Includes 50% |
| prediction-online-sme | 4 | 2 | 50.0% | [15.0%, 85.0%] | Includes 50% |
| claude-prediction-online | 4 | 2 | 50.0% | [15.0%, 85.0%] | Includes 50% |

### PolyStrat

| Tool | Bets | Correct | Accuracy | 95% CI | vs 50% |
|------|------|---------|----------|--------|--------|
| prediction-request-reasoning | 5536 | 3380 | 61.1% | [59.8%, 62.3%] | Above |
| superforcaster | 1176 | 749 | 63.7% | [60.9%, 66.4%] | Above |
| prediction-request-reasoning-claude | 240 | 159 | 66.3% | [60.1%, 71.9%] | Above |
| claude-prediction-offline | 204 | 138 | 67.6% | [61.0%, 73.7%] | Above |
| prediction-offline | 201 | 117 | 58.2% | [51.3%, 64.8%] | Above |
| prediction-online | 189 | 117 | 61.9% | [54.8%, 68.5%] | Above |
| prediction-request-rag | 177 | 102 | 57.6% | [50.3%, 64.7%] | Above |
| claude-prediction-online | 170 | 103 | 60.6% | [53.1%, 67.6%] | Above |
| prediction-request-rag-claude | 165 | 102 | 61.8% | [54.2%, 68.9%] | Above |
| prediction-online-sme | 87 | 46 | 52.9% | [42.5%, 63.0%] | Includes 50% |

## 2. Cross-Strategy Comparison (Same Tool, Omen vs Polymarket)

| Tool | Omen Acc | Omen n | Poly Acc | Poly n | Delta | Fisher p |
|------|----------|--------|----------|--------|-------|----------|
| claude-prediction-offline | 63.6% | 401 | 67.6% | 204 | +4.0pp | 0.3675 ns |
| claude-prediction-online | - | - | 60.6% | 170 | - | - |
| prediction-offline | 68.5% | 797 | 58.2% | 201 | -10.3pp | 0.0074 ** |
| prediction-online | 60.4% | 490 | 61.9% | 189 | +1.5pp | 0.7925 ns |
| prediction-online-sme | - | - | 52.9% | 87 | - | - |
| prediction-request-rag | 41.5% | 443 | 57.6% | 177 | +16.1pp | 0.0003 *** |
| prediction-request-rag-claude | - | - | 61.8% | 165 | - | - |
| prediction-request-reasoning | 60.1% | 2984 | 61.1% | 5536 | +1.0pp | 0.3770 ns |
| prediction-request-reasoning-claude | 59.1% | 364 | 66.3% | 240 | +7.2pp | 0.0868 . |
| superforcaster | 50.4% | 4445 | 63.7% | 1176 | +13.3pp | 0.0000 *** |

## 3. Anti-Predictive Tools

Tools whose 95% confidence interval falls entirely below 50%:

- **prediction-request-rag** on OmenStrat: 41.5% on 443 bets (CI [37.0%, 46.2%]). Inverse-betting would yield 58.5% (CI [53.8%, 63.0%]).

## 4. Tool Tiers

### OmenStrat

**Statistically above 50%:**

- prediction-offline: 68.5% [65.2%-71.6%] (n=797)
- claude-prediction-offline: 63.6% [58.8%-68.2%] (n=401)
- prediction-online: 60.4% [56.0%-64.6%] (n=490)
- prediction-request-reasoning: 60.1% [58.3%-61.8%] (n=2984)
- prediction-request-reasoning-claude: 59.1% [53.9%-64.0%] (n=364)

**Insufficient evidence (CI includes 50%):**

- superforcaster: 50.4% [48.9%-51.9%] (n=4445)
- prediction-online-sme: 50.0% [15.0%-85.0%] (n=4)
- claude-prediction-online: 50.0% [15.0%-85.0%] (n=4)
- prediction-request-rag-claude: 20.0% [3.6%-62.4%] (n=5)

**Statistically below 50% (anti-predictive):**

- prediction-request-rag: 41.5% [37.0%-46.2%] (n=443)

### PolyStrat

**Statistically above 50%:**

- claude-prediction-offline: 67.6% [61.0%-73.7%] (n=204)
- prediction-request-reasoning-claude: 66.3% [60.1%-71.9%] (n=240)
- superforcaster: 63.7% [60.9%-66.4%] (n=1176)
- prediction-online: 61.9% [54.8%-68.5%] (n=189)
- prediction-request-rag-claude: 61.8% [54.2%-68.9%] (n=165)
- prediction-request-reasoning: 61.1% [59.8%-62.3%] (n=5536)
- claude-prediction-online: 60.6% [53.1%-67.6%] (n=170)
- prediction-offline: 58.2% [51.3%-64.8%] (n=201)
- prediction-request-rag: 57.6% [50.3%-64.7%] (n=177)

**Insufficient evidence (CI includes 50%):**

- prediction-online-sme: 52.9% [42.5%-63.0%] (n=87)

## 5. Key Findings

### Superforcaster performs very differently across platforms

- **Omen: 50.4%** on 4,445 bets — statistically indistinguishable from coin flip
- **Polymarket: 63.7%** on 1,176 bets — significantly above coin flip
- Fisher p < 0.0001 — the difference is real, not noise

SF uses search snippets only (no page scraping) and a structured debiasing prompt. This architecture works well on Polymarket questions but adds no value on Omen questions. The prompt engineering that helps on one platform doesn't transfer.

### prediction-request-rag is anti-predictive on Omen

- **Omen: 41.5%** on 443 bets — CI entirely below 50%
- **Polymarket: 57.6%** on 177 bets — above 50%

RAG has no calibration guidance and no reasoning step. On Omen, it's actively worse than random. Inverse-betting RAG on Omen would yield 58.5%, matching PRR. The same tool works fine on Polymarket, suggesting Omen's question format exposes RAG's lack of structured reasoning more than Polymarket's does.

### prediction-offline is the opposite — better on Omen

- **Omen: 68.5%** on 797 bets — the highest accuracy of any tool on either platform
- **Polymarket: 58.2%** on 201 bets

This tool doesn't use web search at all — it relies entirely on the LLM's training data. The fact that it's the best tool on Omen suggests that for Omen-style questions, the LLM's prior knowledge is more predictive than web search results.

### PRR is the most consistent tool across platforms

- **Omen: 60.1%** on 2,984 bets
- **Polymarket: 61.1%** on 5,536 bets

Nearly identical performance. PRR's two-step architecture (search → reason → predict) with FAISS retrieval delivers stable results regardless of question type.

### Claude variants generally match or beat their base versions

| Base tool | Base (Omen/Poly) | Claude variant (Omen/Poly) |
|-----------|------------------|---------------------------|
| prediction-request-reasoning | 60% / 61% | 59% / 66% |
| prediction-offline | 68% / 58% | 64% / 68% |
| prediction-online | 60% / 62% | - / 61% |
| prediction-request-rag | 42% / 58% | - / 62% |

## 6. Recommendations

1. **Retire or inverse-bet prediction-request-rag on Omen.** 41.5% on 443 bets is statistically anti-predictive. Either remove it from OmenStrat's tool pool or investigate inverse-betting as a strategy.

2. **Investigate superforcaster on Omen.** 50.4% on 4,445 bets means it's consuming resources (LLM calls, search API) for zero predictive value on Omen. Consider restricting SF to PolyStrat where it runs at 63.7%.

3. **prediction-offline deserves more volume on Omen.** At 68.5% on 797 bets, it's the best-performing tool on either platform. It's also the cheapest (no search API calls). Give it more allocation on OmenStrat.

4. **All tools need more Polymarket bets.** Only PRR (5,536) and SF (1,176) have strong sample sizes on PolyStrat. Everything else is under 250 bets — too few for confident conclusions.

5. **Monitor factual_research when it ships.** Its architecture (sub-question decomposition + information barrier + base-rate anchoring) addresses SF's Omen weakness (snippet-only evidence) while preserving calibration discipline. It may perform well where SF fails.

---

*Significance: \*\*\* p<0.001, \*\* p<0.01, \* p<0.05, . p<0.1, ns not significant*

*All confidence intervals are 95% Wilson score intervals.*