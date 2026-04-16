# factual_research (Gnosis) — post-launch analysis

Analysis window: **2026-03-17 → 2026-04-16** (30 days)
Mech: `0x601024e27f1c67b28209e24272ced8a31fc8151f`
Tool: `factual_research`
Data source: `api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis` — `Deliver.toolResponse`

The tool went live on **2026-04-15**; all 209 delivers in the 30-day window fall in the last 2 days.

---

## TL;DR

1. **~20% of deliveries are broken.** 41 of 209 delivers return a raw error string, not JSON. **All 41** are the same failure: `Could not parse response content as the length limit was reached — CompletionUsage(completion_tokens=1500, …)`. The LLM hits its 1500-token output cap mid-JSON and the tool returns the framework's error string instead of a structured prediction. **Fix is mechanical: raise `max_completion_tokens` and/or switch to structured-output / JSON mode.**
2. **Failure rate is climbing.** 10.0% on day 1, 24.5% on day 2 — the problem is getting worse as traffic grows, not self-correcting.
3. **Outputs look overconfident.** 85.7% of structured answers are at the tails (`p_yes<0.1` or `>0.9`), only 2.4% express genuine uncertainty (`0.3≤p_yes≤0.5`), and mean `confidence` in the `p_yes>0.9` bucket is **0.984**. No ground-truth calibration yet (markets haven't resolved), but the distribution alone is out of line with sibling tools on this marketplace.

---

## Scope

| metric | value |
|---|---|
| Delivers in window | 209 |
| First deliver | 2026-04-15 00:07 UTC |
| Latest deliver | 2026-04-16 19:00 UTC |
| Structured (valid JSON with p_yes/p_no) | 168 (80.4%) |
| non-json / error strings | 41 (19.6%) |
| empty / facts-leak / json-other | 0 |
| Days active | 2 |

Day-by-day:

| day | total | structured | non-json | bad% |
|---|---|---|---|---|
| 2026-04-15 | 70 | 63 | 7 | **10.0%** |
| 2026-04-16 | 139 | 105 | 34 | **24.5%** |

---

## Problem 1 — token-limit truncation (ship-blocker)

### Evidence

All 41 non-JSON responses contain the **exact same signature**:

```
Could not parse response content as the length limit was reached -
  CompletionUsage(
    completion_tokens=1500,
    prompt_tokens=<3600–4000>,
    total_tokens=<5100–5500>,
    …
  )
```

`completion_tokens=1500` is the ceiling in every failed call. The model is being cut off mid-response.

Sample prompt/completion token counts across 5 failures: prompt 3680, 3825, 3836, 3851, 3977 — completion always exactly 1500.

### Sample failing txs (Gnosis)

| time (UTC) | prompt tok | tx |
|---|---|---|
| 2026-04-16 19:01 | 3836 | `0x77c72e1ae431b2fa7d4fe268a79b45a6d19b94fcd4a05baf799bf9552b58ec18` |
| 2026-04-16 18:36 | 3851 | `0xa7b76685310f2b89c817968ff5f741781edb2bb449980e6bf7737834ae835d1a` |
| 2026-04-16 18:24 | 3977 | `0x2b70eefd9887ccccf6016546b4fec4de07ed9fab8d72e08fe2e618f96ff99319` |
| 2026-04-16 17:33 | 3825 | `0x0b61bdb271ec46fab6eeff1547e99e0f94dd1070a755f34622d5871179b798a5` |
| 2026-04-16 17:22 | 3680 | `0xa95738d6528e76d19940267ca4227fa8c2c22220effdadbb4f85f11d59ca258c` |

### Likely causes

- `max_completion_tokens` (or `max_tokens`) is set to 1500 somewhere in the `factual_research` tool implementation. A JSON response that includes `p_yes`, `p_no`, `confidence`, `info_utility`, and any explanation/citation fields doesn't fit in 1500 tokens when the reasoning is verbose.
- The model is being allowed to emit long free-form reasoning before the JSON block, rather than being constrained to structured output.

### Suggested fixes

1. **Raise `max_completion_tokens` to ≥ 4096** (or match the model's max). Quick win — unblocks the 20% of delivers currently failing.
2. **Use structured-output / JSON-mode** (OpenAI `response_format={"type": "json_object"}` or function-calling with a schema). Forces the model to emit the JSON and nothing else, so truncation can't corrupt the response.
3. **Defensive parse on the tool side**: if the model output is truncated, return a well-formed error-shaped JSON (e.g. `{"p_yes": null, "error": "length_limit"}`) so downstream consumers don't get raw framework strings.
4. Add a **per-deliver monitor/alert** on non-JSON `toolResponse` rate so regressions are caught within minutes, not days.

---

## Problem 2 — possible overconfidence (needs calibration once markets resolve)

### The distribution

Across the 168 structured deliveries:

| field | n | mean | median | stdev | min | max |
|---|---|---|---|---|---|---|
| p_yes | 168 | 0.1104 | 0.0300 | 0.2333 | 0.001 | 0.995 |
| p_no | 168 | 0.8896 | 0.9700 | 0.2333 | 0.005 | 0.999 |
| confidence | 168 | 0.7840 | 0.8500 | 0.1557 | 0.320 | 1.000 |

`p_yes` bucket histogram + **mean confidence within each bucket**:

| p_yes bucket | count | % | mean confidence |
|---|---|---|---|
| `<0.1` | 136 | 81.0% | **0.8124** |
| `0.1–0.3` | 16 | 9.5% | 0.5887 |
| `0.3–0.5` | 4 | 2.4% | 0.4500 |
| `0.5–0.7` | 2 | 1.2% | 0.4750 |
| `0.7–0.9` | 2 | 1.2% | 0.6000 |
| `>0.9` | 8 | 4.8% | **0.9838** |

### What this shows

- **85.7%** of predictions sit at the tails (`<0.1` or `>0.9`). Only **2.4%** land in a genuinely uncertain middle band (`0.3–0.5`).
- The confidence signal is smile-shaped — highest at the extremes, lowest in the middle — which is structurally correct *if* the extremes are justified.
- **Extremes are extremely extreme.** The 5 lowest `p_yes` samples:

  | p_yes | p_no | confidence | tx |
  |---|---|---|---|
  | 0.0010 | 0.9990 | 0.980 | `0xb27fdf13b05e7055ff1b74ac80a0aa82c14fc9e354d9d9be140fc8841544bb31` |
  | 0.0030 | 0.9970 | 0.900 | `0x9c4d755ec5bc353eccb5ad7aad86b6a741a92f835d8485340de71b450e5f50f5` |
  | 0.0050 | 0.9950 | 0.950 | `0xa5e8747349169e8872c9142211577797292140cb323d4f9c03a736cf92123436` |
  | 0.0050 | 0.9950 | 0.900 | `0xd20cd91c8198d10ebdd4887fa0ebe0f9b772faeb6b4d59d06b942e0163176bcd` |
  | 0.0050 | 0.9950 | 0.900 | `0xc8830d263f5d217ee156e5f525cec8d0688229cd24998d2f95fef3befaf48c1d` |

  The 5 highest `p_yes` samples:

  | p_yes | p_no | confidence | tx |
  |---|---|---|---|
  | 0.9950 | 0.0050 | 0.990 | `0x40a0e15c785c5a20d862e04cd4a319ad602190da93119ccc44b26de81cb50c72` |
  | 0.9950 | 0.0050 | 0.980 | `0xe3c71c4cc86db0b4dbce9a3a4d3634efde4865caa430eb1f9cf98ad496c22b96` |
  | 0.9950 | 0.0050 | 1.000 | `0x5dfc1256c2b54d71c4ec86c23baa1029b59006398767a6ea1eb7ee38c747205c` |
  | 0.9950 | 0.0050 | 0.980 | `0xc0ba8c2f4de9b74b921313c2be93a308604f3c376c947636a2de53ed79962096` |
  | 0.9950 | 0.0050 | 0.980 | `0xd2a7355e21920393a86ccfa2df3902d01f2da622c34b4c8c3169d946ded4b5a6` |

  These say effectively "99% sure" with 98%+ stated confidence on prediction-market questions — a claim that is almost never defensible for open-world forecasting.

### Why this is suspicious without calibration

- For a well-calibrated forecaster, the fraction of "very confident NO" (`p_yes<0.1`, cf>0.8) calls that resolve YES should be ~5% or less. 81% of delivers fall in this bucket — if even a small fraction resolves against the call, the tool's log-loss will be catastrophic.
- A factual-research tool is *supposed* to land near 0.5 on questions where public evidence is genuinely ambiguous. Only 4 of 168 (2.4%) answers land there.
- Sibling tools on this marketplace (`superforcaster`, OmenStrat/PolyStrat predictors — see `mech/analyze_pyes_trends.py`) produce materially wider distributions.

### What we can't yet say

No market has resolved in the 2 days since launch, so a calibration curve (frequency of YES within each `p_yes` bucket vs. the bucket midpoint) is not computable yet. The overconfidence claim is **distribution-based, not outcome-based**.

### Suggested follow-ups

1. **Re-run this analysis in ~14–21 days**, joining predictions against Reality.eth `currentAnswer` (recipe: `omen/` scripts + `reference_omen_gnosis_contracts.md`). Produce a calibration curve and Brier score.
2. **Inspect the prompt.** Overconfidence at this scale usually traces to one of:
   - A prompt that asks the model to "decide" rather than estimate a probability.
   - A prompt that rewards the model for resolving ambiguity rather than preserving it.
   - Few-shot examples that are themselves overconfident.
3. **Soften the output format.** If the JSON schema allows / asks for extreme probabilities, move to explicit anchors like `{0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95}` and require the model to pick one — this alone caps the damage.
4. **Mix with `superforcaster` or OmenStrat ensembles**, rather than using `factual_research` alone, until calibration is verified.

---

## Invariant checks (all clean)

| check | violations |
|---|---|
| `p_yes + p_no ≠ 1` (±0.01) | 0 |
| any value outside `[0, 1]` | 0 |
| degenerate `p_yes=p_no=0.5` with no confidence | 0 |

So when the tool returns structured output, the values are internally consistent — the problem is their *distribution*, not the arithmetic.

---

## Reproduction

All numbers above come from one script:

```bash
poetry run python mech/tool_response_trend.py \
    0x601024e27f1c67b28209e24272ced8a31fc8151f \
    --tool factual_research \
    --days 30 \
    --bucket day
```

Flags:
- `--dump-values` — prints every `(ts, p_yes, p_no, confidence, tx)` triple for offline analysis.
- `--bucket hour` — hourly rather than daily breakdown (useful once volume grows).
- `--tool <name>` — re-run for `superforcaster`, `prediction-offline-sme`, etc. for sibling comparison.

Raw failure inspection (IPFS payloads for any of the txs above):

```bash
poetry run python mech/list_factual_research_ipfs_links.py --hours 48 -n 50
```

---

## Action items (ordered)

1. **Raise `max_completion_tokens` to ≥ 4096** in the factual_research tool config. Expected to drop non-JSON rate from ~20% to near 0%.
2. **Switch to structured/JSON output mode** so a truncation cannot corrupt the response.
3. **Return error-shaped JSON** (`{"p_yes": null, "error": "..."}`) on tool-side failures rather than raw framework strings.
4. **Alert on `non-JSON toolResponse rate > 2%`** over a rolling 1h window.
5. **Audit the prompt** for anything that pushes the model to commit to extremes instead of preserving uncertainty.
6. **Re-run this report in 2–3 weeks** with market outcomes joined in — produce a calibration curve and Brier score before declaring the overconfidence confirmed or resolved.
