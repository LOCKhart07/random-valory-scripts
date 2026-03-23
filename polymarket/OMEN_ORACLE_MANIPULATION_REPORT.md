# Omen Oracle Manipulation Report — March 2026

## Summary

Address `0xc5fd24b2974743896e1e94c47e99d3960c7d4c96` has been submitting incorrect
market resolutions on Reality.io since March 15, 2026, while simultaneously betting on
the outcomes it resolves. This caused a fleet-wide accuracy collapse in week 11
(March 16–22), wiping ~324 xDAI in fleet PnL.

## Timeline

- **March 11**: Address created, first wxDAI wrap (1.1 xDAI). No prior on-chain history.
- **March 15 02:45 UTC**: First Reality.io answer submission.
- **March 15 onward**: Daily submissions at 00:00 UTC with 0.001 xDAI minimum bond.
- **March 17**: First conditional token redemptions (winning positions claimed).

## The Scheme

1. **Bet** 3–25 xDAI on the unlikely "Yes" side of markets (cheap tokens since fleet
   consensus is "No").
2. **Resolve** the market on Reality.io at 00:00 UTC (market expiry) with "Yes",
   posting only 0.001 xDAI bond.
3. **Redeem** conditional tokens minutes later at 00:05 UTC for ~2–3x the invested amount.

The address also places normal bets on markets it doesn't resolve (149 of 200 markets),
which mostly lose. The profit comes from the ~51 markets where it both bets and resolves.

## Evidence

### Reality.io Activity
- **54 answer submissions**, 39 answered "Yes", 15 answered "No".
- **51 became final** (94.4% win rate), 3 were challenged and overturned.
- **Sole responder** on 45/48 unique markets — no one contested.
- Total bond posted: **0.066 xDAI** (0.001 per answer).
- All submissions at **00:00–00:02 UTC**.

### On-Chain Betting
- **1,513 transactions** from a brand-new address.
- **473 wxDAI transfers out** to FPMM contracts (1,280 xDAI total).
- **480 wxDAI transfers in** (1,105 xDAI — includes 385 xDAI from Conditional Tokens).
- Bets placed directly on FPMM contracts, **bypassing the Olas subgraph** entirely
  (zero activity in predict-omen).
- Current balance: **204 xDAI**.

### Cross-Reference: Bets ↔ Resolutions
- **51 out of 54** markets they resolved, they also bet on.
- They invest 3–25 xDAI per market on the side they plan to resolve.
- Conditional token redemptions (6–12 xDAI each) happen minutes after resolution.

### Verifiably Wrong Resolutions
Several "Yes" resolutions are factually incorrect based on web searches:

| Market | Resolved | Reality |
|---|---|---|
| OpenAI settlement with Britannica/Merriam-Webster | Yes | Lawsuit was *filed* Mar 16, no settlement |
| Amazon/Google/Meta/Microsoft reducing carbon credits | Yes | They *increased* purchases (CNBC Mar 16) |
| TSA barring Chinese nationals from flight schools | Yes | Senator Banks *requested* it; TSA made no announcement |
| US telecom AI deepfake voice call blocking | Yes | No carrier announced a deployed system |
| Kalshi/Rotten Tomatoes film festival expansion | Yes | Partnership exists for Oscars only, no festival expansion |
| Ecolab removing energy surcharge | Yes | No evidence of announcement |

### Fleet Impact
- **W08–W10** (before manipulation): 55–57% accuracy, +387 xDAI PnL.
- **W11** (manipulation active): 41.6% accuracy, -324 xDAI PnL.
- Every tool's accuracy dropped simultaneously (superforcaster 56%→38%,
  prediction-request-reasoning 58%→42%), except `prediction-offline` which held steady.
- The fleet's "No" predictions were largely correct — the resolutions were wrong.
- "No" side accuracy collapsed from 90.2% to 58.1% due to fraudulent "Yes" resolutions.

## Manipulator Address Details

- **Address**: `0xc5fd24b2974743896e1e94c47e99d3960c7d4c96`
- **First activity**: March 11, 2026
- **Funded with**: Native xDAI (no wxDAI transfers in), wrapped 774 xDAI total.
- **Not an Olas agent** — no traderAgent, no serviceId, no mech requests.
- **Interacts directly** with FPMM contracts and Reality.io.

## Scripts

Analysis scripts used for this investigation are in `polymarket/`:

- `analyze_omen_agent.py` — Single-agent Omen analysis (tool usage, bet sizing, PnL)
- `analyze_omen_fleet_fast.py` — Fleet-wide analysis with concurrent mech fetching
- `analyze_omen_week_compare.py` — Before/after comparison across two time periods
- `analyze_omen_large_bets.py` — Large vs small bet accuracy divergence analysis
- `analyze_resolver.py` — Deep analysis of suspected oracle manipulator (funding, bets,
  resolutions, cross-reference, conditional token redemptions)
