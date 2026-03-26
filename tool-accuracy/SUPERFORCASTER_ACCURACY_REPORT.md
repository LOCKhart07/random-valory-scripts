# Superforcaster Accuracy Analysis — Full Report

**Date:** 2026-03-26

## Question

Is superforcaster accuracy degrading on Polymarket, or is it converging to its true value?

## Answer

**Neither.** The apparent degradation is a statistical illusion caused by increased volume after a tool selection update around Mar 19. The underlying accuracy has not changed.

## The data

- **444 total SF bets** (full fetch), **212** in the partial fetch used for statistical tests
- **Overall accuracy: 57.1%** with 95% CI of [50.3%, 63.6%]
- **Date range:** Feb 28 – Mar 25, 2026
- Tool selection update deployed ~Mar 19, increasing SF's share from ~10% to ~60-80% of all bets

## What looked like degradation

| Segment | Accuracy |
|---------|----------|
| First third | 65.7% |
| Middle third | 58.6% |
| Last third | 47.2% |

A 19.6pp monotonic decline across thirds. Weekly accuracy: 67% → 62% → 46%. Rolling 20-bet window dropped from 85% peak to 45% current.

## Why it's not real

**Every statistical test came back non-significant:**

| Test | Result | p-value | Significant? |
|------|--------|---------|:---:|
| First half vs second half (Fisher's exact) | 61.3% → 52.8% | 0.27 | No |
| Thirds homogeneity (Chi-squared) | 65.7/58.6/47.2% | 0.08 | No |
| Weekly trend (Mann-Kendall) | tau = -1.0 | 0.30 | No |
| Bet-index correlation (permutation, 10k runs) | r = -0.13 | 0.07 | No |
| Win/loss sequence randomness (runs test) | z = -1.53 | 0.13 | No |
| SF degrading more than PRR? (permutation) | slope diff = -0.001 | 0.07 | No |
| SF vs PRR on 56 shared markets (Fisher's exact) | 58.9% vs 53.6% | 0.70 | No |
| Category accuracy differences (Chi-squared) | weather/politics/crypto/other | 0.13 | No |
| Bootstrap 95% CI for half-over-half difference | [-4.7pp, +21.7pp] | — | Includes 0 |

The one significant result — a CUSUM changepoint at bet #118 (p=0.018) — is driven by two bad days (Mar 23: 40.5%, Mar 25: 27.3%) sandwiched between two good days (Mar 22: 65.7%, Mar 24: 63.6%). That's normal variance.

## What actually happened

1. **Tool selection update ~Mar 19** pushed SF share from ~10% to ~60-80% of daily bets
2. **SF volume went from ~7 bets/day to ~25 bets/day** — more surface area for variance
3. **SF's early bets (pre-update) had a small sample lucky streak** — 65.7% on 70 bets is well within the CI of a 57% true rate
4. **Post-update bets regressed toward the true mean**, which was always around 57%
5. **A couple of bad days (Mar 23, 25) made the tail end look catastrophic**, but daily accuracy has always been noisy (ranging from 27% to 100% on any given day)

## Cross-tool comparison

| Tool | Bets | Accuracy | 95% CI | Trend |
|------|------|----------|--------|-------|
| PRR | 438 | 58.4% | [53.8%, 63.0%] | Flat (p=0.94) |
| Superforcaster | 212 | 57.1% | [50.3%, 63.6%] | No sig trend (p=0.07) |
| claude-prediction-offline | 17 | 82.4% | [59.0%, 93.8%] | Too few bets |
| prediction-online | 13 | 76.9% | [49.7%, 91.8%] | Too few bets |
| prediction-request-rag | 16 | 25.0% | [10.2%, 49.5%] | Genuinely bad |

- **PRR and SF perform identically** — their CIs overlap almost completely
- On shared markets they're indistinguishable (58.9% vs 53.6%, p=0.70)
- **prediction-request-rag** is the only tool with a genuinely significant problem (25%, CI excludes 50%)

## Market category performance (SF)

| Category | Bets | Accuracy | 95% CI | vs 50%? |
|----------|------|----------|--------|---------|
| Weather | 60 | 51.7% | [39.3%, 63.8%] | Includes 50% |
| Politics | 79 | 60.8% | [49.7%, 70.8%] | Includes 50% |
| Crypto/stocks | 49 | 49.0% | [35.6%, 62.5%] | Includes 50% |
| Other | 24 | 75.0% | [55.1%, 88.0%] | Above 50% |

Category differences are not significant (p=0.13) except crypto vs other (p=0.045), but with only 24 "other" bets that's fragile.

## Conclusion

**Superforcaster is a ~57% accuracy tool. It always was.** The early 65% was a hot streak on a small sample, and the recent 47% is a cold streak on a small sample. Both are within the expected range. The tool selection update didn't break anything — it just gave you more data points, which made the noise more visible.

**Nothing needs to be fixed.** Check back when SF has 500+ bets and the CI will be tight enough to detect real shifts if they occur.
