# ZD#919 — On-chain verification of 7 "invalid" Omen markets

**Headline finding: the ticket's premise is wrong. These 7 markets are NOT invalid. They are still in the Reality.eth dispute window and will resolve with concrete Yes/No answers within the next ~10–22 hours. ~8.39 XDAI of the user's ~8.93 XDAI stake IS recoverable — just not yet.**

- Script: [`omen/verify_invalid_markets_zd919.py`](verify_invalid_markets_zd919.py)
- Run at: Gnosis block 45591043, chain time `2026-04-09T20:35:55Z`
- Trader Safe: `0x85378392A666759e1170a53a34a1Ae98e54F7fD0`
- Service id: 2403 (Pearl v1.4.21-rc3 / trader v0.33.2-rc1, kelly_criterion)

## Key correction to the ticket

| Claim in ticket | Reality on-chain |
|---|---|
| "All 7 markets settled as `invalid`" | **FALSE.** 0/7 have Reality answer `0xff..ff`. 6/7 have `bestAnswer = 0x01` (No), 1/7 has `0x00` (Yes). Bonds of 10–20 xDAI have been posted. |
| "Markets are resolved" | **FALSE.** 7/7 have `isFinalized = false`. Reality.eth `openingTS = 2026-04-09 00:00 UTC`, 24h timeout. Finalize timestamps are 10.1h–22.1h in the future. |
| "CT should return 50% via `redeemPositions()`" | **N/A.** The condition has NOT been reported to Conditional Tokens yet (`payoutDenominator = 0`). Nothing is redeemable right now — no one, not just the Pearl trader. |
| "~8.93 XDAI is gone" | **FALSE.** ~**8.395 XDAI** will be redeemable once markets finalize. Only ~0.53 XDAI is actually lost, from 2 genuinely wrong bets. |

## I used the wrong Reality.eth address at first

The ticket lists `0xE78996A233895bE74a66F451f1019cA9734205cc` (Reality v2.1). That is **not** the contract Omen currently uses on Gnosis. I verified on-chain via `oracle.realitio()` on Omen's oracle proxy (`0xAB16D643bA051C11962DA645f74632d3130c81E2`) that the active Reality.eth is **v3** at `0x79e32aE03fb27B07C89c0c568F80287C01ca2E57`. Calling the v2.1 address with these question IDs returns all-zeros and would have masked the real state.

## Per-market table

| # | FPMM | Stake | Safe Yes | Safe No | Reality answer | Finalize in | Projected redeem |
|---|---|---:|---:|---:|---|---|---:|
| 1 | `0x1a8aee36…` FIFA | 1.679 | 0.000 | **2.142** | **No** (0x01) | 22.0 h | **2.142 XDAI ✅** |
| 2 | `0x93b2c7f7…` Kittleson | 1.596 | 0.000 | 2.069 | Yes (0x00) | 21.2 h | **0.000 XDAI ❌** (wrong side) |
| 3 | `0x441162d8…` HP EliteBook | 1.893 | 0.000 | **2.484** | **No** (0x01) | 10.1 h | **2.484 XDAI ✅** |
| 4 | `0xb09065a7…` ChromeOS Flex | 0.567 | 0.000 | **0.769** | **No** (0x01) | 22.1 h | **0.769 XDAI ✅** |
| 5 | `0xd98e374b…` US pharma | 1.078 | 0.000 | **1.562** | **No** (0x01) | 10.4 h | **1.562 XDAI ✅** |
| 6 | `0xe4dab291…` CrystalX RAT | 1.090 | 3.049 | 0.000 | No (0x01) | 10.5 h | **0.000 XDAI ❌** (wrong side) |
| 7 | `0x3c9c6f31…` EPA microplastics | 1.022 | 0.000 | **1.438** | **No** (0x01) | 16.0 h | **1.438 XDAI ✅** |
|   | **TOTAL** | **8.925** | **3.049** | **10.464** |   |   | **8.395 XDAI** |

- All 7 use oracle `0xAB16D643bA051C11962DA645f74632d3130c81E2` → Reality v3.
- All 7 use Reality arbitrator `0x5562Ac605764DC4039fb6aB56a74f7321396Cdf2` with a **24h timeout**.
- All 7 are unreported on CT (`payoutNumerators=[?,?]`, `payoutDenominator=0`).
- All 7 have all of the Safe's outcome tokens still sitting in its CT balance — 0 redemptions have happened yet.
- The "No balance > Yes balance" pattern on 6 of 7 is Omen AMM mechanics: the Pearl trader bought the minority outcome, got more tokens per xDAI, and in 5 of those 6 cases the minority turned out right.

### Raw condition IDs and question IDs

| # | FPMM | conditionId | questionId |
|---|---|---|---|
| 1 | `0x1a8aee36…` | `0xd69e027084d9e9f9eccec35bab374d9088097bec2bf47db84ffe30abbd66db89` | `0x2e2bacc5f6cf9a729670446f6ab0a1e8cd4e8229d5c0851f3d9b33148d6a6b57` |
| 2 | `0x93b2c7f7…` | `0xbcfbc30a3788b18a0274653637eda8e4939b4a59c34fe052e0bdca858e3a3bdb` | `0xf3b50ad554e73f1a4db57a03f227a35243ea3175f2ee616e8f13129af3311803` |
| 3 | `0x441162d8…` | `0xf365b4497083ff1105c3836e8a4787f43f705b1ce961fee73af211e5b4f56330` | `0x74c6193658e4fd432cfa6b871c721335acfb3408b4641c7a284ce6bf6ef68573` |
| 4 | `0xb09065a7…` | `0x71af98bd3ba06a9180e0336356201208ea1776d9b9ef779591d8317892edc171` | `0x98c1186797002bce6e57832742773ef0334a27f6aaebe305e8cdc8f8f5600247` |
| 5 | `0xd98e374b…` | `0x59ff8e5553425c5d4025125ac605aafa4774d8ddac6e66d2b5f9e347bcb3d7aa` | `0x17933cbe42b21e059ab71130a19b2eb1e48fa6d3c20dbc6ad735878c32b7bb3b` |
| 6 | `0xe4dab291…` | `0xf07d2941bbb2adcd97b3cb500967655837dc43ce3387a4c8cc61579e78637533` | `0x1d483b224cad334dccd806bc553af7fc5a33e52dcab9586d45649c9a30fc58d6` |
| 7 | `0x3c9c6f31…` | `0x9000b9cf7d305e9a9d3075b4d33ba355023cdaefaffaaa05114dd69050a52071` | `0xf290efec5899eaa00d8825e4652c8909ad9b955d0b97ac618cdb7e47da15655d` |

## Answers to the 5 ticket questions

1. **Is each market resolved invalid on Reality.eth?** No. 0 of 7 are resolved at all. 0 of 7 are "invalid" (`0xff..ff`). 6 have a "No" answer, 1 has a "Yes" answer, all still in dispute window.
2. **Does the Safe hold unredeemed CTs?** Yes, 7 of 7. Balances sum to 13.51 CTokens across the 7 conditions (3.05 Yes + 10.46 No).
3. **What does `redeemPositions()` return today?** **0 XDAI** on every market — the condition hasn't been reported to CT yet (`payoutDenominator=0`). After finalization and `reportPayouts`, projected total is **8.395 XDAI** (5 wins: FIFA, HP, ChromeOS, US pharma, EPA; 2 losses: Kittleson, CrystalX).
4. **Subgraph redemptions for this Safe on these markets?** Zero (the `predict-omen` subgraph schema doesn't expose `redemptions`/`fpmmTrades` in the form queried, so this is confirmed only by on-chain: CT `balanceOf` is non-zero for all 14 positions, so no redemption has been executed — if any had run, `balanceOf` would now be zero for that outcome).
5. **Other traders successfully redeemed these conditions?** **No, 0 of 7.** This is NOT evidence of a redeem-path bug — it is because the conditions are not yet reported. No one on chain could have redeemed any of these yet. The protocol path cannot even be exercised for another 10+ hours.

## What this means for the user's agent

- **There is no redemption bug to debug right now for these 7 markets.** There is nothing to redeem yet. Revisit after ~2026-04-10 18:30 UTC (when the latest finalize-ts, 1775846490, elapses) and the Omen oracle has called `resolve()`/`reportPayouts()`.
- **The "status: invalid, payout: 0" the user is seeing in the Pearl UI is a UI-layer mislabel** of "condition has no `currentAnswer` yet / not finalized". The same conflation exists in `omen/analyze_omen_agent.py:127` (`ca is None or ca == INVALID_ANSWER` → bucketed together). Pearl is almost certainly doing the same thing.
- **Real recoverable value: ~8.395 XDAI** (not ~4.5 XDAI as a naive "50% of stake on invalid" estimate would give). The user's money is mostly fine; they just can't touch it for another day.
- **Real loss: ~0.53 XDAI** from 2 legitimately wrong bets (Kittleson bet No, answer will be Yes; CrystalX bet Yes, answer will be No). This is normal trading loss, not a refund gap.
- **The gridlock still needs attention.** The Safe has 0 native xDAI / 0.007 WXDAI. When markets finalize in 10–22h, the trader needs gas to call `redeemPositions()`. If it's still looping in `mech_request_round` waiting for 0.01 WXDAI to make a mech call, it will never reach `redeem_round`. **The fix is to top up the Safe BEFORE ~18:30 UTC 2026-04-10 so the FSM can advance past `mech_request_round` and actually redeem.** Topping up even ~0.1 xDAI is enough to break the loop.
- **Caveat on bonds.** Bonds of 10–20 xDAI on markets with ~2 xDAI total stake are unusually high. That suggests Kleros arbitrators or dedicated Reality.eth answerers are posting. The answers could theoretically still be challenged in the remaining dispute window, but it's unlikely at those bond sizes — you'd need to double them (20–40 xDAI) to flip.

## Script caveats worth noting

- The `predict-omen` subgraph does not expose `fpmmTrades` or `redemptions` at the query paths tried — schema miss. Not pursued further (on-chain `balanceOf` is authoritative and showed all 14 positions still held).
- `find_other_redemptions()` ran but returned 0 for all 7 conditions because nothing is redeemable yet. Re-run after finalization to confirm the protocol path works end-to-end.
- Re-run the script in ~24h to watch `isFinalized` flip to `True`, `payoutDenominator` become `2`, and `redeemable_wei` become non-zero. If at that point the Pearl Safe still has non-zero CT balances, **then** you have the actual "redeem never called" bug to debug.

## Follow-up actions

1. **Now:** Top up `0x85378392A666759e1170a53a34a1Ae98e54F7fD0` with ≥0.1 xDAI native so the FSM can break out of `mech_request_round → ROUND_TIMEOUT` loop and reach `redeem_round` once markets finalize.
2. **After ~2026-04-10 18:30 UTC:** Re-run `poetry run python omen/verify_invalid_markets_zd919.py`. Expected state:
   - `isFinalized: True` on all 7 Reality questions
   - `payoutNumerators: [0,1]` or `[1,0]` and `payoutDenominator: 2` on CT
   - `Redeemable now: 2.142 / 0 / 2.484 / 0.769 / 1.562 / 0 / 1.438` → total **8.395 XDAI**
   - After Pearl's `redeem_round` fires, all `balanceOf` should go to 0 and a `PayoutRedemption` event should be emitted for the Safe.
3. **Separately:** Audit the Pearl agent UI / subgraph layer that labels these markets "status: invalid, payout: 0" while Reality has a real pending answer — that mislabel is what caused the original ZD report.
