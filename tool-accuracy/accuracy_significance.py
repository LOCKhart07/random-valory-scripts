"""
Statistical significance analysis for tool accuracy trends on Polymarket.

Tests whether observed accuracy differences are real signals or random noise using:
  1. Binomial confidence intervals (per-tool overall accuracy)
  2. Fisher's exact test (first-half vs second-half accuracy)
  3. Chi-squared test (accuracy differences across market categories)
  4. Mann-Kendall trend test (monotonic trend in weekly accuracy)
  5. Permutation test (is SF's degradation different from PRR's?)
  6. Runs test (is the win/loss sequence non-random?)
  7. Changepoint detection (did accuracy shift at a specific point?)

Uses cached data from superforcaster_trend.py.
"""

import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations

# ---------------------------------------------------------------------------
# Load cached data
# ---------------------------------------------------------------------------

SEP = "\u241f"


def extract_title(q):
    return q.split(SEP)[0].strip() if q else ""


def match_tool(bet, reqs):
    bt = extract_title(bet.get("question_title", ""))
    if not bt:
        return "unknown"
    matched = []
    for r in reqs:
        mt = extract_title(
            (r.get("parsedRequest") or {}).get("questionTitle", "")
        )
        if not mt:
            continue
        if bt.startswith(mt) or mt.startswith(bt):
            matched.append(r)
    if not matched:
        return "unknown"
    bet_ts = bet["timestamp"]
    before = [r for r in matched if int(r.get("blockTimestamp") or 0) <= bet_ts]
    chosen = (
        max(before, key=lambda r: int(r.get("blockTimestamp") or 0))
        if before
        else matched[0]
    )
    return (chosen.get("parsedRequest") or {}).get("tool") or "unknown"


def load_data():
    cache = json.loads(
        open("tool-accuracy/.superforcaster_trend_cache.json").read()
    )
    bets = None
    for k, v in cache.items():
        if k.startswith("bets:"):
            if bets is None or len(v["bets"]) > len(bets):
                bets = v["bets"]

    agent_reqs = {}
    for k, v in cache.items():
        if k.startswith("mech:"):
            agent_reqs[k[5:]] = v["requests"]

    for bet in bets:
        bet["tool"] = match_tool(bet, agent_reqs.get(bet["bettor"], []))

    return bets


# ---------------------------------------------------------------------------
# Statistical helpers (no scipy dependency)
# ---------------------------------------------------------------------------


def normal_cdf(x):
    """Standard normal CDF using Abramowitz & Stegun approximation."""
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    d = 0.3989422804014327  # 1/sqrt(2*pi)
    p = d * math.exp(-x * x / 2.0) * (
        t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 +
        t * (-1.821255978 + t * 1.330274429))))
    )
    return 1.0 - p if sign > 0 else p


def normal_ppf(p):
    """Inverse normal CDF (rational approximation, Abramowitz & Stegun 26.2.23)."""
    if p <= 0:
        return -10
    if p >= 1:
        return 10
    if p < 0.5:
        return -normal_ppf(1 - p)
    t = math.sqrt(-2 * math.log(1 - p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return t - (c0 + c1 * t + c2 * t * t) / (1 + d1 * t + d2 * t * t + d3 * t * t * t)


def binomial_ci(successes, n, confidence=0.95):
    """Wilson score interval for binomial proportion."""
    if n == 0:
        return 0, 0, 0
    p_hat = successes / n
    z = normal_ppf(1 - (1 - confidence) / 2)
    denom = 1 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) / denom
    return p_hat, max(0, center - spread), min(1, center + spread)


def fisher_exact_2x2(a, b, c, d):
    """
    Fisher's exact test for 2x2 contingency table.
    [[a, b], [c, d]]  =>  p-value (two-sided)
    Uses logarithms for factorial computation.
    """
    n = a + b + c + d

    def log_factorial(x):
        return sum(math.log(i) for i in range(1, x + 1))

    def log_hyper(aa, bb, cc, dd):
        return (
            log_factorial(aa + bb) + log_factorial(cc + dd) +
            log_factorial(aa + cc) + log_factorial(bb + dd) -
            log_factorial(n) - log_factorial(aa) - log_factorial(bb) -
            log_factorial(cc) - log_factorial(dd)
        )

    log_p_obs = log_hyper(a, b, c, d)
    p_value = 0.0

    row1 = a + b
    row2 = c + d
    col1 = a + c

    for aa in range(0, min(row1, col1) + 1):
        bb = row1 - aa
        cc = col1 - aa
        dd = row2 - cc
        if bb < 0 or cc < 0 or dd < 0:
            continue
        log_p = log_hyper(aa, bb, cc, dd)
        if log_p <= log_p_obs + 1e-10:
            p_value += math.exp(log_p)

    return min(p_value, 1.0)


def chi_squared_test(observed_pairs):
    """
    Chi-squared test for multiple categories.
    observed_pairs: list of (successes, total) per category.
    Returns chi2 statistic, degrees of freedom, p-value.
    """
    total_success = sum(s for s, _ in observed_pairs)
    total_n = sum(n for _, n in observed_pairs)
    if total_n == 0:
        return 0, 0, 1.0

    expected_rate = total_success / total_n
    chi2 = 0
    for s, n in observed_pairs:
        if n == 0:
            continue
        e_s = expected_rate * n
        e_f = (1 - expected_rate) * n
        if e_s > 0:
            chi2 += (s - e_s) ** 2 / e_s
        if e_f > 0:
            chi2 += ((n - s) - e_f) ** 2 / e_f

    df = len([n for _, n in observed_pairs if n > 0]) - 1
    if df <= 0:
        return chi2, 0, 1.0

    # Chi-squared CDF approximation (Wilson-Hilferty)
    if df > 0:
        x = chi2
        k = df
        z = (((x / k) ** (1 / 3)) - (1 - 2 / (9 * k))) / math.sqrt(2 / (9 * k))
        p_value = 1 - normal_cdf(z)
    else:
        p_value = 1.0

    return chi2, df, p_value


def mann_kendall(series):
    """
    Mann-Kendall trend test.
    Returns: tau, z_score, p_value (two-sided)
    """
    n = len(series)
    if n < 3:
        return 0, 0, 1.0

    s = 0
    for i in range(n):
        for j in range(i + 1, n):
            diff = series[j] - series[i]
            if diff > 0:
                s += 1
            elif diff < 0:
                s -= 1

    tau = 2 * s / (n * (n - 1))

    # Variance of S
    var_s = n * (n - 1) * (2 * n + 5) / 18

    # Tie correction
    unique_vals = defaultdict(int)
    for v in series:
        unique_vals[v] += 1
    for t in unique_vals.values():
        if t > 1:
            var_s -= t * (t - 1) * (2 * t + 5) / 18

    if var_s <= 0:
        return tau, 0, 1.0

    if s > 0:
        z = (s - 1) / math.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s)
    else:
        z = 0

    p_value = 2 * (1 - normal_cdf(abs(z)))
    return tau, z, p_value


def runs_test(sequence):
    """
    Wald-Wolfowitz runs test for randomness.
    sequence: list of booleans (True=correct, False=incorrect)
    Returns: z_score, p_value
    """
    n = len(sequence)
    if n < 10:
        return 0, 1.0

    n1 = sum(sequence)
    n0 = n - n1
    if n1 == 0 or n0 == 0:
        return 0, 1.0

    # Count runs
    runs = 1
    for i in range(1, n):
        if sequence[i] != sequence[i - 1]:
            runs += 1

    # Expected runs and variance
    expected = 1 + 2 * n1 * n0 / n
    var = 2 * n1 * n0 * (2 * n1 * n0 - n) / (n * n * (n - 1))
    if var <= 0:
        return 0, 1.0

    z = (runs - expected) / math.sqrt(var)
    p_value = 2 * (1 - normal_cdf(abs(z)))
    return z, p_value


def permutation_test_trend(bets, n_permutations=10000):
    """
    Permutation test: is the observed accuracy trend (correlation between
    bet index and correctness) stronger than random?
    Returns observed correlation, p_value.
    """
    n = len(bets)
    if n < 10:
        return 0, 1.0

    outcomes = [1 if b["is_correct"] else 0 for b in bets]
    indices = list(range(n))

    # Observed correlation (point-biserial = Pearson on 0/1)
    mean_y = sum(outcomes) / n
    mean_x = (n - 1) / 2
    var_x = sum((i - mean_x) ** 2 for i in range(n))

    def correlation(ys):
        cov = sum((i - mean_x) * (ys[i] - mean_y) for i in range(n))
        var_y = sum((y - mean_y) ** 2 for y in ys)
        if var_x == 0 or var_y == 0:
            return 0
        return cov / math.sqrt(var_x * var_y)

    obs_corr = correlation(outcomes)

    # Permutation distribution
    rng = random.Random(42)
    count_extreme = 0
    for _ in range(n_permutations):
        shuffled = outcomes[:]
        rng.shuffle(shuffled)
        perm_corr = correlation(shuffled)
        if abs(perm_corr) >= abs(obs_corr):
            count_extreme += 1

    p_value = (count_extreme + 1) / (n_permutations + 1)
    return obs_corr, p_value


def cusum_changepoint(bets):
    """
    CUSUM changepoint detection.
    Finds the bet index where accuracy most sharply shifts.
    Returns: changepoint_index, before_acc, after_acc, max_cusum
    """
    n = len(bets)
    if n < 10:
        return None, 0, 0, 0

    outcomes = [1 if b["is_correct"] else 0 for b in bets]
    mean_acc = sum(outcomes) / n

    # Cumulative sum of deviations from mean
    cusum = [0.0]
    for y in outcomes:
        cusum.append(cusum[-1] + (y - mean_acc))

    # Find max absolute deviation
    max_abs = 0
    cp = 0
    for i in range(1, n):
        if abs(cusum[i]) > max_abs:
            max_abs = abs(cusum[i])
            cp = i

    if cp == 0 or cp == n:
        return None, mean_acc, mean_acc, 0

    before = outcomes[:cp]
    after = outcomes[cp:]
    before_acc = sum(before) / len(before) * 100
    after_acc = sum(after) / len(after) * 100

    return cp, before_acc, after_acc, max_abs


def bootstrap_accuracy_diff(bets1, bets2, n_boot=10000):
    """
    Bootstrap CI for accuracy difference (bets1 - bets2).
    Returns: observed_diff, ci_lower, ci_upper
    """
    rng = random.Random(42)
    o1 = [1 if b["is_correct"] else 0 for b in bets1]
    o2 = [1 if b["is_correct"] else 0 for b in bets2]
    obs_diff = sum(o1) / len(o1) - sum(o2) / len(o2)

    diffs = []
    for _ in range(n_boot):
        s1 = [rng.choice(o1) for _ in range(len(o1))]
        s2 = [rng.choice(o2) for _ in range(len(o2))]
        diffs.append(sum(s1) / len(s1) - sum(s2) / len(s2))

    diffs.sort()
    ci_lo = diffs[int(0.025 * n_boot)]
    ci_hi = diffs[int(0.975 * n_boot)]
    return obs_diff, ci_lo, ci_hi


# ---------------------------------------------------------------------------
# Market categorization
# ---------------------------------------------------------------------------


def categorize(title):
    t = title.lower()
    if any(kw in t for kw in ["temperature", "°f", "°c", "highest temp"]):
        return "weather"
    if any(kw in t for kw in [
        "election", "seats", "folketing", "parliament", "vote margin",
        "popular vote", "democrat", "republican",
    ]):
        return "politics"
    if any(kw in t for kw in [
        "close above", "close below", "price", "btc", "eth", "googl",
        "aapl", "msft", "nvda", "tsla", "spy", "qqq", "stock",
    ]):
        return "crypto/stocks"
    return "other"


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


def sig_label(p):
    if p < 0.001:
        return "***"
    if p < 0.01:
        return "**"
    if p < 0.05:
        return "*"
    if p < 0.1:
        return "."
    return "ns"


def analyze_tool(name, bets, all_bets):
    bets = sorted(bets, key=lambda b: b["timestamp"])
    n = len(bets)
    correct = sum(1 for b in bets if b["is_correct"])

    print(f"\n{'=' * 70}")
    print(f"  {name}  (n={n})")
    print(f"{'=' * 70}")

    # --- 1. Binomial CI ---
    p_hat, ci_lo, ci_hi = binomial_ci(correct, n)
    print(f"\n  1. OVERALL ACCURACY with 95% Wilson CI")
    print(f"     {correct}/{n} = {p_hat*100:.1f}%  [{ci_lo*100:.1f}%, {ci_hi*100:.1f}%]")
    if ci_lo > 0.5:
        print(f"     => Significantly better than coin flip")
    elif ci_hi < 0.5:
        print(f"     => Significantly worse than coin flip")
    else:
        print(f"     => CI includes 50% — NOT significantly different from coin flip")

    # --- 2. First half vs second half (Fisher's exact) ---
    if n >= 10:
        mid = n // 2
        h1, h2 = bets[:mid], bets[mid:]
        c1 = sum(1 for b in h1 if b["is_correct"])
        c2 = sum(1 for b in h2 if b["is_correct"])
        f1, f2 = len(h1) - c1, len(h2) - c2
        p_fisher = fisher_exact_2x2(c1, f1, c2, f2)
        a1 = c1 / len(h1) * 100
        a2 = c2 / len(h2) * 100
        print(f"\n  2. FIRST HALF vs SECOND HALF (Fisher's exact test)")
        print(f"     1st: {c1}/{len(h1)} = {a1:.1f}%")
        print(f"     2nd: {c2}/{len(h2)} = {a2:.1f}%")
        print(f"     Delta: {a2-a1:+.1f}pp")
        print(f"     p = {p_fisher:.4f}  {sig_label(p_fisher)}")
        if p_fisher < 0.05:
            print(f"     => Statistically significant difference")
        else:
            print(f"     => NOT significant — could be noise")

        # Bootstrap CI on the difference
        obs_d, ci_lo_d, ci_hi_d = bootstrap_accuracy_diff(h1, h2)
        print(f"     Bootstrap 95% CI for diff: [{ci_lo_d*100:+.1f}pp, {ci_hi_d*100:+.1f}pp]")
        if ci_lo_d > 0:
            print(f"     => First half significantly better (CI excludes 0)")
        elif ci_hi_d < 0:
            print(f"     => Second half significantly better (CI excludes 0)")
        else:
            print(f"     => CI includes 0 — difference is NOT robust")

    # --- 3. Thirds ---
    if n >= 30:
        t = n // 3
        thirds = [bets[:t], bets[t:2*t], bets[2*t:]]
        accs = []
        for s in thirds:
            c = sum(1 for b in s if b["is_correct"])
            accs.append(c / len(s) * 100)

        pairs = [(sum(1 for b in s if b["is_correct"]), len(s)) for s in thirds]
        chi2, df, p_chi = chi_squared_test(pairs)
        print(f"\n  3. THIRDS (Chi-squared homogeneity test)")
        print(f"     1st: {accs[0]:.1f}%  |  2nd: {accs[1]:.1f}%  |  3rd: {accs[2]:.1f}%")
        print(f"     chi2 = {chi2:.2f}, df = {df}, p = {p_chi:.4f}  {sig_label(p_chi)}")
        if p_chi < 0.05:
            print(f"     => Accuracy differs significantly across thirds")
        else:
            print(f"     => Thirds are NOT significantly different")

    # --- 4. Mann-Kendall trend on weekly bins ---
    weekly = defaultdict(lambda: {"total": 0, "correct": 0})
    for b in bets:
        dt = datetime.fromtimestamp(b["timestamp"], tz=timezone.utc)
        week = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
        weekly[week]["total"] += 1
        if b["is_correct"]:
            weekly[week]["correct"] += 1

    # Only weeks with >= 3 bets
    week_accs = []
    week_labels = []
    for w in sorted(weekly.keys()):
        s = weekly[w]
        if s["total"] >= 3:
            week_accs.append(s["correct"] / s["total"] * 100)
            week_labels.append(w)

    if len(week_accs) >= 3:
        tau, z_mk, p_mk = mann_kendall(week_accs)
        print(f"\n  4. MANN-KENDALL TREND TEST (weekly bins, >= 3 bets/week)")
        print(f"     Weeks: {' -> '.join(f'{a:.0f}%' for a in week_accs)}")
        print(f"     tau = {tau:.3f}, z = {z_mk:.2f}, p = {p_mk:.4f}  {sig_label(p_mk)}")
        if p_mk < 0.05:
            direction = "downward" if tau < 0 else "upward"
            print(f"     => Significant monotonic {direction} trend")
        else:
            print(f"     => No significant trend detected")

    # --- 5. Permutation test for trend ---
    if n >= 20:
        corr, p_perm = permutation_test_trend(bets)
        print(f"\n  5. PERMUTATION TEST (bet-index vs outcome correlation)")
        print(f"     r = {corr:.4f}, p = {p_perm:.4f}  {sig_label(p_perm)}")
        if p_perm < 0.05:
            direction = "degrading" if corr < 0 else "improving"
            print(f"     => Significant {direction} trend (not explained by chance)")
        else:
            print(f"     => Trend is NOT significant — consistent with random noise")

    # --- 6. Runs test ---
    if n >= 10:
        seq = [b["is_correct"] for b in bets]
        z_runs, p_runs = runs_test(seq)
        print(f"\n  6. RUNS TEST (is win/loss sequence random?)")
        print(f"     z = {z_runs:.2f}, p = {p_runs:.4f}  {sig_label(p_runs)}")
        if p_runs < 0.05:
            if z_runs < 0:
                print(f"     => Fewer runs than expected — streaky (wins/losses cluster)")
            else:
                print(f"     => More runs than expected — alternating pattern")
        else:
            print(f"     => Sequence is consistent with random coin flips")

    # --- 7. CUSUM changepoint ---
    if n >= 20:
        cp, before_acc, after_acc, max_cs = cusum_changepoint(bets)
        if cp is not None:
            cp_dt = datetime.fromtimestamp(
                bets[cp]["timestamp"], tz=timezone.utc
            ).strftime("%Y-%m-%d")
            print(f"\n  7. CUSUM CHANGEPOINT DETECTION")
            print(f"     Changepoint at bet #{cp} ({cp_dt})")
            print(f"     Before: {before_acc:.1f}%  |  After: {after_acc:.1f}%")
            shift = after_acc - before_acc

            # Significance via Fisher's exact on before/after split
            c_before = sum(1 for b in bets[:cp] if b["is_correct"])
            c_after = sum(1 for b in bets[cp:] if b["is_correct"])
            f_before = cp - c_before
            f_after = (n - cp) - c_after
            p_cp = fisher_exact_2x2(c_before, f_before, c_after, f_after)
            print(f"     Shift: {shift:+.1f}pp, Fisher p = {p_cp:.4f}  {sig_label(p_cp)}")
            if p_cp < 0.05:
                print(f"     => Significant changepoint")
            else:
                print(f"     => Changepoint is NOT statistically significant")

    # --- 8. Category breakdown with CIs ---
    cat_bets = defaultdict(list)
    for b in bets:
        cat_bets[categorize(b.get("question_title", ""))].append(b)

    cats_with_data = {c: bs for c, bs in cat_bets.items() if len(bs) >= 5}
    if cats_with_data:
        print(f"\n  8. ACCURACY BY MARKET CATEGORY (95% Wilson CI)")
        pairs = []
        for cat in ["weather", "politics", "crypto/stocks", "other"]:
            if cat not in cats_with_data:
                continue
            bs = cats_with_data[cat]
            c = sum(1 for b in bs if b["is_correct"])
            p_hat, ci_lo, ci_hi = binomial_ci(c, len(bs))
            pairs.append((c, len(bs)))
            coin = " <-- includes 50%" if ci_lo <= 0.5 <= ci_hi else ""
            print(
                f"     {cat:<18} {c:>3}/{len(bs):<3} = {p_hat*100:>5.1f}%  "
                f"[{ci_lo*100:.1f}%, {ci_hi*100:.1f}%]{coin}"
            )

        if len(pairs) >= 2:
            chi2, df, p_chi = chi_squared_test(pairs)
            print(f"     Chi-squared: {chi2:.2f}, df={df}, p={p_chi:.4f}  {sig_label(p_chi)}")
            if p_chi < 0.05:
                print(f"     => Accuracy significantly varies by category")
            else:
                print(f"     => Category differences are NOT significant")

    # --- 9. Pairwise category comparisons (Fisher's exact) ---
    if len(cats_with_data) >= 2:
        print(f"\n  9. PAIRWISE CATEGORY COMPARISONS (Fisher's exact)")
        cat_names = [c for c in ["weather", "politics", "crypto/stocks", "other"]
                     if c in cats_with_data]
        for c1, c2 in combinations(cat_names, 2):
            b1, b2 = cats_with_data[c1], cats_with_data[c2]
            s1 = sum(1 for b in b1 if b["is_correct"])
            s2 = sum(1 for b in b2 if b["is_correct"])
            f1 = len(b1) - s1
            f2 = len(b2) - s2
            p = fisher_exact_2x2(s1, f1, s2, f2)
            a1 = s1 / len(b1) * 100
            a2 = s2 / len(b2) * 100
            print(
                f"     {c1} ({a1:.0f}%) vs {c2} ({a2:.0f}%): "
                f"p = {p:.4f}  {sig_label(p)}"
            )


def compare_tools_degradation(tools_bets):
    """
    Permutation test: is SF's degradation significantly different from PRR's?
    """
    sf = sorted(tools_bets.get("superforcaster", []), key=lambda b: b["timestamp"])
    prr = sorted(tools_bets.get("prediction-request-reasoning", []),
                 key=lambda b: b["timestamp"])

    if len(sf) < 20 or len(prr) < 20:
        return

    print(f"\n{'=' * 70}")
    print(f"  CROSS-TOOL COMPARISON: SF vs PRR degradation")
    print(f"{'=' * 70}")

    # Compute slope (correlation) for each
    def slope(bets):
        n = len(bets)
        outcomes = [1 if b["is_correct"] else 0 for b in bets]
        mean_y = sum(outcomes) / n
        mean_x = (n - 1) / 2
        cov = sum((i - mean_x) * (outcomes[i] - mean_y) for i in range(n))
        var_x = sum((i - mean_x) ** 2 for i in range(n))
        return cov / var_x if var_x else 0

    sf_slope = slope(sf)
    prr_slope = slope(prr)

    print(f"\n  Trend slope (outcome vs bet-index):")
    print(f"    SF:  {sf_slope:.6f} ({'declining' if sf_slope < 0 else 'improving'})")
    print(f"    PRR: {prr_slope:.6f} ({'declining' if prr_slope < 0 else 'improving'})")
    print(f"    Difference: {sf_slope - prr_slope:.6f}")

    # Permutation test: is the slope difference significant?
    # Pool all bets, randomly assign to "SF-sized" and "PRR-sized" groups,
    # compute slope difference
    pooled = sf + prr
    obs_diff = sf_slope - prr_slope
    n_sf = len(sf)
    rng = random.Random(42)
    n_perm = 10000
    count = 0
    for _ in range(n_perm):
        rng.shuffle(pooled)
        perm_sf = sorted(pooled[:n_sf], key=lambda b: b["timestamp"])
        perm_prr = sorted(pooled[n_sf:], key=lambda b: b["timestamp"])
        perm_diff = slope(perm_sf) - slope(perm_prr)
        if abs(perm_diff) >= abs(obs_diff):
            count += 1

    p = (count + 1) / (n_perm + 1)
    print(f"\n  Permutation test (is SF degrading MORE than PRR?):")
    print(f"    p = {p:.4f}  {sig_label(p)}")
    if p < 0.05:
        print(f"    => SF's trend is significantly different from PRR's")
    else:
        print(f"    => SF and PRR trends are NOT significantly different")
        print(f"       (both may be responding to the same market conditions)")

    # On shared markets only
    sf_questions = {b["question_id"]: b["is_correct"] for b in sf}
    prr_questions = {b["question_id"]: b["is_correct"] for b in prr}
    shared = set(sf_questions.keys()) & set(prr_questions.keys())

    if len(shared) >= 10:
        sf_shared = sum(sf_questions[q] for q in shared)
        prr_shared = sum(prr_questions[q] for q in shared)
        n_shared = len(shared)
        p_shared = fisher_exact_2x2(
            sf_shared, n_shared - sf_shared,
            prr_shared, n_shared - prr_shared
        )
        print(f"\n  On {n_shared} SHARED markets:")
        print(f"    SF:  {sf_shared}/{n_shared} = {sf_shared/n_shared*100:.1f}%")
        print(f"    PRR: {prr_shared}/{n_shared} = {prr_shared/n_shared*100:.1f}%")
        print(f"    Fisher p = {p_shared:.4f}  {sig_label(p_shared)}")
        if p_shared >= 0.05:
            print(f"    => No significant difference on the same markets")


def effect_size_summary(tools_bets):
    """Summary of effect sizes across all tools."""
    print(f"\n{'=' * 70}")
    print(f"  EFFECT SIZE SUMMARY (all tools with >= 10 bets)")
    print(f"{'=' * 70}")
    print(f"\n  {'Tool':<38} | {'n':>4} | {'Acc':>6} | {'95% CI':>15} | {'Half Δ':>8} | {'Fisher p':>8} | {'Trend p':>8}")
    print(f"  {'-' * 100}")

    for name in sorted(tools_bets.keys(), key=lambda t: -len(tools_bets[t])):
        tb = sorted(tools_bets[name], key=lambda b: b["timestamp"])
        n = len(tb)
        if n < 10 or name == "unknown":
            continue
        c = sum(1 for b in tb if b["is_correct"])
        p_hat, ci_lo, ci_hi = binomial_ci(c, n)

        mid = n // 2
        h1, h2 = tb[:mid], tb[mid:]
        c1 = sum(1 for b in h1 if b["is_correct"])
        c2 = sum(1 for b in h2 if b["is_correct"])
        delta = c2 / len(h2) * 100 - c1 / len(h1) * 100
        p_f = fisher_exact_2x2(c1, len(h1) - c1, c2, len(h2) - c2)

        if n >= 20:
            _, p_perm = permutation_test_trend(tb, n_permutations=5000)
            trend_str = f"{p_perm:.4f} {sig_label(p_perm)}"
        else:
            trend_str = "n/a"

        print(
            f"  {name:<38} | {n:>4} | {p_hat*100:>5.1f}% | "
            f"[{ci_lo*100:>4.1f}%, {ci_hi*100:>4.1f}%] | "
            f"{delta:>+6.1f}pp | "
            f"{p_f:.4f} {sig_label(p_f):>2} | "
            f"{trend_str}"
        )


def main():
    print("Loading cached data...")
    bets = load_data()
    print(f"Loaded {len(bets)} bets.\n")

    tools_bets = defaultdict(list)
    for b in bets:
        tools_bets[b["tool"]].append(b)

    # Significance legend
    print("Significance: *** p<0.001  ** p<0.01  * p<0.05  . p<0.1  ns not significant")

    # Effect size summary first
    effect_size_summary(tools_bets)

    # Detailed per-tool analysis for tools with >= 10 bets
    for name in sorted(tools_bets.keys(), key=lambda t: -len(tools_bets[t])):
        if len(tools_bets[name]) < 10 or name == "unknown":
            continue
        analyze_tool(name, tools_bets[name], bets)

    # Cross-tool comparison
    compare_tools_degradation(tools_bets)


if __name__ == "__main__":
    main()
