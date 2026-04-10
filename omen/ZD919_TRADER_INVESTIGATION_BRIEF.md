# Brief for follow-up agent: investigate "invalid market" mislabel in the Pearl trader codebase — ZD#919

## Repo and scope

Work in the **`valory-xyz/trader`** repository (the Pearl prediction-market trader agent — open-autonomy FSM). Do **not** work in `random-valory-scripts`. Versions affected: Pearl v1.4.21-rc3, trader v0.33.2-rc1 (kelly_criterion strategy). Also cross-reference `valory-xyz/trader-quickstart` and the Pearl app repo if the "status: invalid" label is rendered by the quickstart UI rather than the agent itself.

Do not modify code. This is a **research / diagnostic** task — produce a written report, not a PR. If you find a clear one-line bug, flag it, but don't fix it.

## Context — what we already know from on-chain

A user's Pearl trader agent shows 7 Omen markets in its UI as `status: invalid, payout: 0`, summing to ~8.93 XDAI of "lost" stake. The user believes this is unredeemable. We ran an independent on-chain verification (script: `omen/verify_invalid_markets_zd919.py` in `random-valory-scripts`, report: `omen/ZD919_INVALID_MARKETS_REPORT.md`) and found that **the on-chain state contradicts the agent's label**:

- **0 of 7 markets are actually invalid.** None have Reality.eth `bestAnswer = 0xff..ff`.
- **All 7 markets are still inside the Reality.eth 24h dispute window.** `isFinalized = false`, finalize timestamps 10–22h in the future at the time of the check.
- **6 of 7 have a concrete "No" answer proposed with 10–20 xDAI bond**, 1 of 7 has a concrete "Yes" answer.
- **None have been reported to Conditional Tokens yet** (`payoutDenominator = 0`), so `redeemPositions()` returns 0 right now for any caller — not just the trader.
- **The Safe still holds all 14 outcome positions** (`balanceOf != 0`). Nothing has been burned/redeemed.
- Once Reality finalizes and the oracle calls `reportPayouts`, the Safe will be able to redeem ~**8.395 XDAI** (5 winning positions). Only ~0.53 XDAI is a real trading loss from 2 genuinely wrong bets.

**In short: the user's money is mostly fine, it's just that the agent UI is labelling "pending finalization" as "invalid / payout 0", and the user interpreted that as "the fix-invalid-markets code path is broken and my funds are stuck".**

The leading hypothesis — to investigate, not assume — is that somewhere in the trader FSM or its bets-store / subgraph layer, **"Omen subgraph returned `currentAnswer == null`"** is being conflated with **"Reality answer is `0xff..ff` (invalid)"**, and both are bucketed under `status: INVALID` with `payout: 0`. I saw this exact pattern in an unrelated script in `random-valory-scripts/omen/analyze_omen_agent.py:127` (`if ca is None or ca == INVALID_ANSWER: ...`). The Pearl trader may have the same conflation.

## What I specifically need you to determine

### 1. Where does the `status` field on a bet come from?

Trace the code path that decides a bet's `status` / market status in the Pearl trader. Start from wherever the UI or bets store persists it. Common places to check:

- `packages/valory/skills/decision_maker_abci/` — look for anything like `BetStatus`, `MarketStatus`, `Bet.status`, `is_invalid`, `set_status`.
- `packages/valory/skills/decision_maker_abci/states/bet_placement.py` or similar FSM state files.
- `packages/valory/skills/decision_maker_abci/models.py` — where the bet dataclass lives.
- `packages/valory/skills/decision_maker_abci/bets.py` / `bets_store.py` — persistence + filtering.
- The Omen subgraph query file (look for `omen-xdai` or `predict-omen` or `subgraph`). Check what fields it requests from `fixedProductMarketMaker` — does it pull `currentAnswer`, `isPendingArbitration`, `answerFinalizedTimestamp`, `question { currentAnswer }`, `condition { payouts, payoutDenominator }`?

Give me file paths and line numbers.

### 2. How is `INVALID` defined?

Find the definition of the "invalid" status/constant/enum and every site that assigns it. I want to see whether any of these happen:

- `status = INVALID` when Reality answer is literally `0xff..ff` (correct)
- `status = INVALID` when `currentAnswer is None` (**bug** — this is "pending")
- `status = INVALID` when `currentAnswer == "0xff..ff"` OR `currentAnswer is None` (**bug** — same conflation)
- `status = INVALID` when `payoutDenominator == 0` (**bug** — that's "not reported yet")
- `status = INVALID` when `is_pending_arbitration` is true (**bug** — that's "under dispute")

List every match with file:line. If the conflation exists, this is the mislabel bug.

### 3. How does payout get computed for "invalid" markets?

Find the code that sets `payout = 0` for invalid markets and check whether it's **short-circuiting before ever calling `redeemPositions()`**. In particular:

- Is there a branch like `if market.status == INVALID: skip_redeem()` or `if is_invalid: payout = 0; return`?
- Does the trader ever attempt `redeemPositions` on a market it considers invalid? Even for a genuinely invalid market (which SHOULD refund 50% via `redeemPositions(..., indexSets=[1,2])` when `payoutNumerators == [1,1]`), skipping the redeem call leaves funds on the table.
- Does the trader check `payoutDenominator > 0` before attempting redemption? It should — attempting redeem on an unreported condition will revert or no-op.

### 4. Is there a separate bug in the `redeem_round` FSM entry condition?

The user's agent has **zero `redeem*` activity in logs** across a 16-minute capture and is gridlocked in `mech_request_round → ROUND_TIMEOUT → mech_request_round`. Find the FSM transition table (usually `packages/valory/skills/decision_maker_abci/fsm_specification.yaml` or similar) and determine:

- What event transitions the FSM INTO `redeem_round`? Is it reachable from `mech_request_round` when the Safe is too broke to make a mech request (native ~0, WXDAI ~0.007, mech price 0.01)?
- Is there a precondition check that requires "at least one settled market with non-zero payout" before entering `redeem_round`? If so, a user whose only "settled" markets are (mis)labelled invalid-with-payout-0 would **never enter the redeem state**, regardless of whether the redeem code itself is correct.
- Alternatively: does the trader only redeem when Reality is actually finalized AND oracle has reported? If so, it would correctly do nothing for the user's 7 markets (which are still pending) — but the UI label would still be wrong.

### 5. Check the two PRs already referenced in ZD#919

Read the code changes in:
- `valory-xyz/trader#813` ("Handle Invalid Markets in Agent UI")
- `valory-xyz/trader#747` ("Fix/invalid market edge case")

For each: summarize what the PR actually changed, whether it's merged into v0.33.2-rc1, and whether it fixes or introduces the `None == INVALID` conflation. Quote the specific diff lines. These PRs are the most likely source of the current behaviour.

### 6. Where does the bets-store `status` get persisted / rendered?

The user says "the UI shows status: invalid" — find out whether that comes from:
- Pearl Middleware reading the trader's bets-store JSON file directly
- A FastAPI endpoint on the trader exposing bet state
- The trader's own logs being scraped

This matters because the fix location depends on whether the mislabel happens at write-time (in the FSM) or at read-time (in the middleware/UI).

## Concrete ground-truth you can compare against

Here are the 7 markets' actual on-chain state as of 2026-04-09 20:35 UTC (Gnosis block 45591043). Any code path in the trader that labels them `invalid` is wrong in at least one dimension:

| FPMM | conditionId | Reality bestAnswer | Reality isFinalized | CT payoutDenominator | Safe cTokens held | True state |
|---|---|---|---|---|---|---|
| `0x1a8aee366e16f0525a9967468e423b762ee49122` | `0xd69e027084d9e9f9eccec35bab374d9088097bec2bf47db84ffe30abbd66db89` | `0x01` (No) | false | 0 | 2.142 No | pending-finalize |
| `0x93b2c7f7db5911de3752a49440c9fcb1d94f70a7` | `0xbcfbc30a3788b18a0274653637eda8e4939b4a59c34fe052e0bdca858e3a3bdb` | `0x00` (Yes) | false | 0 | 2.069 No | pending-finalize |
| `0x441162d89526f368b4b56a34d9b8f2685d4804d0` | `0xf365b4497083ff1105c3836e8a4787f43f705b1ce961fee73af211e5b4f56330` | `0x01` (No) | false | 0 | 2.484 No | pending-finalize |
| `0xb09065a7763e8c77503fc3c8108e686620841eab` | `0x71af98bd3ba06a9180e0336356201208ea1776d9b9ef779591d8317892edc171` | `0x01` (No) | false | 0 | 0.769 No | pending-finalize |
| `0xd98e374ba1497164a4e5f7aa337308e2178faacb` | `0x59ff8e5553425c5d4025125ac605aafa4774d8ddac6e66d2b5f9e347bcb3d7aa` | `0x01` (No) | false | 0 | 1.562 No | pending-finalize |
| `0xe4dab291e3244e66b64ebba0fb5035a6e559d9ef` | `0xf07d2941bbb2adcd97b3cb500967655837dc43ce3387a4c8cc61579e78637533` | `0x01` (No) | false | 0 | 3.049 Yes | pending-finalize |
| `0x3c9c6f3169b6bf355d17cbe7b8efaa5a1137d982` | `0x9000b9cf7d305e9a9d3075b4d33ba355023cdaefaffaaa05114dd69050a52071` | `0x01` (No) | false | 0 | 1.438 No | pending-finalize |

None is `0xff..ff`. None is finalized. None has been reported on CT. All are in the dispute window until ~2026-04-10 18:30 UTC.

Trader internal bet ids (from the user's agent):
```
0x0ec0f104e084653baa1700e4540d591d5b75be767d4d9493a3e943a786c863d926000000  (FIFA)
0x39861efae627b24f4d519cee32dcbd1b205a62769ff1351dd7028fda56ac67720d000000  (Kittleson)
0x06e2a11d6a677269580e683a1a79a12f2ec071a7771117c9a45a7df6c243ad1f07000000  (HP)
0x87bc7e56c7ce027cab00d1dd1e225e775c651b239f891d7ec933479f888a846b1b000000  (ChromeOS)
0x9d72f75762777a2383984025a616c54f58ab609c6625d238e96f46d368bb7ccf07000000  (US pharma)
0x999f9262b33630cc2a859d1f6168111e71cf17bf705c0025344a1a1544d6f75945000000  (CrystalX)
0x40dd51505e1dfd548fb058cb0d50b0788728475b788ae499db7d51c0d5d4acc717000000  (EPA)
```

If you can find where bet ids of this form (32-byte hex with an `NN000000` suffix) are constructed/looked up in the trader code, that will lead you straight to the bets store.

## Deliverable

A single written report (~800–1500 words + code citations) covering:

1. **Where and how `status = INVALID` is assigned.** File:line citations, with the exact condition. Verdict: does `currentAnswer == null` get mis-bucketed as invalid? (Yes / No / partially.)
2. **Whether the 7 markets in the table above would trip the invalid-labelling code path** given the on-chain state shown. Walk through one example explicitly.
3. **Whether the redeem FSM path would ever execute for markets the agent considers "invalid"** — or whether the INVALID status is absorbing state, preventing any redemption attempt.
4. **The FSM reachability of `redeem_round` from the current `mech_request_round` gridlock.** Can it ever get there if mech calls keep failing?
5. **Summary of what PR #813 and PR #747 changed**, and whether those changes fix or perpetuate the bug.
6. **Recommended minimal fix** (don't implement it) — a 2–5 line diff showing where the conflation should be untangled, or a pointer to an entirely different root cause if my "subgraph-null-is-bucketed-as-invalid" hypothesis turns out wrong.
7. **A clear "is there actually a fund-loss bug"** answer. Either: (a) "no, trader is correctly waiting for finalization and UI label is cosmetic", (b) "yes, trader will never redeem these once finalized because of X", or (c) "yes, trader has an entry-point bug that prevents `redeem_round` from ever firing for this user".

Do not trust my hypothesis. Trust the code. If the conflation hypothesis is wrong, report that clearly and tell me what the actual assignment logic is.
