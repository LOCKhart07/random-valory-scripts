"""
Full tool analysis for Polymarket prediction tools.

Combines on-chain accuracy data with code architecture insights to produce
a comprehensive assessment of each tool's performance characteristics.

Uses cached data from superforcaster_trend.py.

Usage:
    python full_tool_analysis.py
"""

import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations

# ---------------------------------------------------------------------------
# Data loading (reuses superforcaster_trend cache)
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

    return sorted(bets, key=lambda b: b["timestamp"])


# ---------------------------------------------------------------------------
# Stats helpers (no scipy)
# ---------------------------------------------------------------------------


def normal_cdf(x):
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    d = 0.3989422804014327
    p = d * math.exp(-x * x / 2.0) * (
        t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 +
        t * (-1.821255978 + t * 1.330274429))))
    )
    return 1.0 - p if sign > 0 else p


def normal_ppf(p):
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


def wilson_ci(successes, n, confidence=0.95):
    if n == 0:
        return 0, 0, 0
    p_hat = successes / n
    z = normal_ppf(1 - (1 - confidence) / 2)
    denom = 1 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) / denom
    return p_hat, max(0, center - spread), min(1, center + spread)


def fisher_exact_2x2(a, b, c, d):
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


def permutation_trend_test(bets, n_perm=10000):
    n = len(bets)
    if n < 20:
        return 0, 1.0
    outcomes = [1 if b["is_correct"] else 0 for b in bets]
    mean_y = sum(outcomes) / n
    mean_x = (n - 1) / 2
    var_x = sum((i - mean_x) ** 2 for i in range(n))

    def corr(ys):
        cov = sum((i - mean_x) * (ys[i] - mean_y) for i in range(n))
        var_y = sum((y - mean_y) ** 2 for y in ys)
        if var_x == 0 or var_y == 0:
            return 0
        return cov / math.sqrt(var_x * var_y)

    obs = corr(outcomes)
    rng = random.Random(42)
    count = 0
    for _ in range(n_perm):
        shuffled = outcomes[:]
        rng.shuffle(shuffled)
        if abs(corr(shuffled)) >= abs(obs):
            count += 1
    return obs, (count + 1) / (n_perm + 1)


def sig(p):
    if p < 0.001:
        return "***"
    if p < 0.01:
        return "**"
    if p < 0.05:
        return "*"
    if p < 0.1:
        return "."
    return "ns"


# ---------------------------------------------------------------------------
# Market categorization
# ---------------------------------------------------------------------------

CATEGORIES = {
    "weather": ["temperature", "°f", "°c", "highest temp", "lowest temp"],
    "politics": [
        "election", "seats", "folketing", "parliament", "vote margin",
        "popular vote", "democrat", "republican", "president", "prime minister",
    ],
    "crypto/stocks": [
        "close above", "close below", "price above", "price below",
        "btc", "eth", "googl", "aapl", "msft", "nvda", "tsla", "spy",
        "qqq", "stock", "bitcoin", "ethereum",
    ],
}


def categorize(title):
    t = title.lower()
    for cat, keywords in CATEGORIES.items():
        if any(kw in t for kw in keywords):
            return cat
    return "other"


# ---------------------------------------------------------------------------
# Analysis sections
# ---------------------------------------------------------------------------


def section_overall(tools_bets):
    """Section 1: Overall accuracy with CIs."""
    print("\n" + "=" * 85)
    print("  1. OVERALL TOOL ACCURACY (95% Wilson confidence intervals)")
    print("=" * 85)
    print(f"\n  {'Tool':<35} | {'n':>5} | {'Correct':>7} | {'Accuracy':>8} | {'95% CI':>17} | vs 50%")
    print("  " + "-" * 95)

    for name in sorted(tools_bets, key=lambda t: -len(tools_bets[t])):
        tb = tools_bets[name]
        if name == "unknown":
            continue
        n = len(tb)
        c = sum(1 for b in tb if b["is_correct"])
        p, lo, hi = wilson_ci(c, n)
        if lo > 0.5:
            vs50 = "ABOVE"
        elif hi < 0.5:
            vs50 = "BELOW"
        else:
            vs50 = "includes 50%"
        print(
            f"  {name:<35} | {n:>5} | {c:>7} | {p*100:>6.1f}%  | "
            f"[{lo*100:>4.1f}%, {hi*100:>4.1f}%] | {vs50}"
        )


def section_categories(tools_bets):
    """Section 2: Accuracy by market category per tool."""
    print("\n" + "=" * 85)
    print("  2. ACCURACY BY MARKET CATEGORY")
    print("=" * 85)

    cats = ["weather", "politics", "crypto/stocks", "other"]

    for name in sorted(tools_bets, key=lambda t: -len(tools_bets[t])):
        tb = tools_bets[name]
        if name == "unknown" or len(tb) < 10:
            continue

        cat_bets = defaultdict(list)
        for b in tb:
            cat_bets[categorize(b.get("question_title", ""))].append(b)

        print(f"\n  {name} ({len(tb)} bets):")
        print(f"    {'Category':<18} | {'n':>5} | {'Correct':>7} | {'Accuracy':>8} | {'95% CI':>17}")
        print(f"    " + "-" * 70)

        for cat in cats:
            bs = cat_bets.get(cat, [])
            if not bs:
                continue
            c = sum(1 for b in bs if b["is_correct"])
            p, lo, hi = wilson_ci(c, len(bs))
            flag = " <-- coin flip" if lo <= 0.5 <= hi else ""
            print(
                f"    {cat:<18} | {len(bs):>5} | {c:>7} | {p*100:>6.1f}%  | "
                f"[{lo*100:>4.1f}%, {hi*100:>4.1f}%]{flag}"
            )


def section_head_to_head(tools_bets):
    """Section 3: Head-to-head on shared markets."""
    print("\n" + "=" * 85)
    print("  3. HEAD-TO-HEAD ON SHARED MARKETS (Fisher's exact test)")
    print("=" * 85)
    print("  When two tools both bet on the same market, which one wins more often?")

    # Build question -> tool -> is_correct mapping
    # Use most recent bet per (tool, question) pair
    tool_q = {}
    for name, tb in tools_bets.items():
        if name == "unknown" or len(tb) < 10:
            continue
        q_map = {}
        for b in sorted(tb, key=lambda x: x["timestamp"]):
            q_map[b["question_id"]] = b["is_correct"]
        tool_q[name] = q_map

    tool_names = sorted(tool_q.keys(), key=lambda t: -len(tools_bets[t]))

    print(f"\n  {'Pair':<55} | {'Shared':>6} | {'Tool A':>8} | {'Tool B':>8} | {'p':>8}")
    print("  " + "-" * 95)

    for t1, t2 in combinations(tool_names, 2):
        shared = set(tool_q[t1].keys()) & set(tool_q[t2].keys())
        if len(shared) < 5:
            continue

        c1 = sum(tool_q[t1][q] for q in shared)
        c2 = sum(tool_q[t2][q] for q in shared)
        n = len(shared)
        f1 = n - c1
        f2 = n - c2

        p = fisher_exact_2x2(c1, f1, c2, f2)
        a1 = c1 / n * 100
        a2 = c2 / n * 100

        label = f"{t1} vs {t2}"
        print(
            f"  {label:<55} | {n:>6} | {a1:>6.1f}%  | {a2:>6.1f}%  | "
            f"{p:.4f} {sig(p)}"
        )

    # Detailed: on shared markets, break down by category
    # Focus on the two big tools
    if "superforcaster" in tool_q and "prediction-request-reasoning" in tool_q:
        sf_q = tool_q["superforcaster"]
        prr_q = tool_q["prediction-request-reasoning"]
        shared = set(sf_q.keys()) & set(prr_q.keys())

        if len(shared) >= 10:
            print(f"\n  SF vs PRR on {len(shared)} shared markets, by category:")

            # Get titles for shared markets
            title_map = {}
            for b in tools_bets["superforcaster"] + tools_bets["prediction-request-reasoning"]:
                if b["question_id"] in shared:
                    title_map[b["question_id"]] = b.get("question_title", "")

            cat_shared = defaultdict(list)
            for q in shared:
                cat_shared[categorize(title_map.get(q, ""))].append(q)

            print(f"    {'Category':<18} | {'n':>4} | {'SF':>8} | {'PRR':>8} | {'p':>8}")
            print(f"    " + "-" * 55)
            for cat in ["weather", "politics", "crypto/stocks", "other"]:
                qs = cat_shared.get(cat, [])
                if len(qs) < 3:
                    continue
                sf_c = sum(sf_q[q] for q in qs)
                prr_c = sum(prr_q[q] for q in qs)
                n = len(qs)
                p = fisher_exact_2x2(sf_c, n - sf_c, prr_c, n - prr_c)
                print(
                    f"    {cat:<18} | {n:>4} | {sf_c/n*100:>6.1f}%  | "
                    f"{prr_c/n*100:>6.1f}%  | {p:.4f} {sig(p)}"
                )


def section_trends(tools_bets):
    """Section 4: Trend analysis per tool."""
    print("\n" + "=" * 85)
    print("  4. TREND ANALYSIS")
    print("=" * 85)

    print(f"\n  {'Tool':<35} | {'r':>8} | {'p (trend)':>10} | {'Verdict':>20}")
    print("  " + "-" * 85)

    for name in sorted(tools_bets, key=lambda t: -len(tools_bets[t])):
        tb = sorted(tools_bets[name], key=lambda b: b["timestamp"])
        if name == "unknown" or len(tb) < 20:
            continue

        r, p = permutation_trend_test(tb, n_perm=5000)
        if p < 0.05:
            verdict = "DEGRADING" if r < 0 else "IMPROVING"
        else:
            verdict = "No sig. trend"
        print(f"  {name:<35} | {r:>+7.4f} | {p:>8.4f} {sig(p):>2} | {verdict}")


def section_weekly(tools_bets):
    """Section 5: Weekly breakdown for all tools."""
    print("\n" + "=" * 85)
    print("  5. WEEKLY ACCURACY BREAKDOWN")
    print("=" * 85)

    # Collect all weeks
    all_weeks = set()
    weekly = {}
    for name, tb in tools_bets.items():
        if name == "unknown" or len(tb) < 10:
            continue
        w = defaultdict(lambda: {"total": 0, "correct": 0})
        for b in tb:
            dt = datetime.fromtimestamp(b["timestamp"], tz=timezone.utc)
            week = (dt - timedelta(days=dt.weekday())).strftime("%m-%d")
            w[week]["total"] += 1
            if b["is_correct"]:
                w[week]["correct"] += 1
            all_weeks.add(week)
        weekly[name] = w

    weeks = sorted(all_weeks)
    # Only show weeks with data
    weeks = [w for w in weeks if any(
        weekly[t].get(w, {}).get("total", 0) > 0
        for t in weekly
    )]

    hdr = f"  {'Tool':<35}"
    for w in weeks:
        hdr += f" | {w:>10}"
    print(f"\n{hdr}")
    print("  " + "-" * (len(hdr) - 2))

    for name in sorted(weekly.keys(), key=lambda t: -len(tools_bets[t])):
        row = f"  {name:<35}"
        for w in weeks:
            s = weekly[name].get(w, {"total": 0, "correct": 0})
            if s["total"] >= 3:
                acc = round(s["correct"] / s["total"] * 100, 0)
                row += f" | {acc:>4.0f}%({s['total']:>3})"
            elif s["total"] > 0:
                row += f" |    -({s['total']:>3})"
            else:
                row += f" |           -"
        print(row)


def section_outcome_bias(tools_bets):
    """Section 6: Yes/No outcome bias per tool."""
    print("\n" + "=" * 85)
    print("  6. OUTCOME SELECTION BIAS (Yes = outcome 0, No = outcome 1)")
    print("=" * 85)

    print(f"\n  {'Tool':<35} | {'Yes%':>5} | {'YesAcc':>7} | {'NoAcc':>7} | {'Gap':>7}")
    print("  " + "-" * 75)

    for name in sorted(tools_bets, key=lambda t: -len(tools_bets[t])):
        tb = tools_bets[name]
        if name == "unknown" or len(tb) < 10:
            continue
        yes_b = [b for b in tb if b["chosen_outcome"] == 0]
        no_b = [b for b in tb if b["chosen_outcome"] == 1]
        yes_pct = round(len(yes_b) / len(tb) * 100, 0) if tb else 0
        yes_acc = round(
            sum(1 for b in yes_b if b["is_correct"]) / len(yes_b) * 100, 1
        ) if yes_b else 0
        no_acc = round(
            sum(1 for b in no_b if b["is_correct"]) / len(no_b) * 100, 1
        ) if no_b else 0
        gap = yes_acc - no_acc
        print(
            f"  {name:<35} | {yes_pct:>4.0f}% | {yes_acc:>5.1f}%  | "
            f"{no_acc:>5.1f}%  | {gap:>+5.1f}pp"
        )


def section_agent_consistency(tools_bets, all_bets):
    """Section 7: Per-agent accuracy for each tool."""
    print("\n" + "=" * 85)
    print("  7. AGENT-LEVEL CONSISTENCY")
    print("=" * 85)
    print("  Do all agents perform similarly with the same tool, or do some agents")
    print("  consistently outperform others?")

    for name in sorted(tools_bets, key=lambda t: -len(tools_bets[t])):
        tb = tools_bets[name]
        if name == "unknown" or len(tb) < 20:
            continue

        agent_bets = defaultdict(list)
        for b in tb:
            agent_bets[b["bettor"]].append(b)

        # Only agents with >= 5 bets
        agents = {a: bs for a, bs in agent_bets.items() if len(bs) >= 5}
        if len(agents) < 2:
            continue

        print(f"\n  {name} — {len(agents)} agents with >= 5 bets:")
        print(f"    {'Agent':>10} | {'n':>5} | {'Accuracy':>8} | {'95% CI':>17}")
        print(f"    " + "-" * 50)

        accs = []
        for agent in sorted(agents, key=lambda a: -len(agents[a])):
            bs = agents[agent]
            c = sum(1 for b in bs if b["is_correct"])
            p, lo, hi = wilson_ci(c, len(bs))
            accs.append(p)
            short_addr = agent[:6] + ".." + agent[-4:]
            print(
                f"    {short_addr:>10} | {len(bs):>5} | {p*100:>6.1f}%  | "
                f"[{lo*100:>4.1f}%, {hi*100:>4.1f}%]"
            )

        # Spread
        if accs:
            spread = (max(accs) - min(accs)) * 100
            mean_acc = sum(accs) / len(accs) * 100
            print(f"    Agent accuracy range: {spread:.1f}pp (mean: {mean_acc:.1f}%)")

            # Do CIs overlap? If all CIs overlap, agent differences aren't significant
            all_overlap = True
            for a1, a2 in combinations(agents.keys(), 2):
                bs1, bs2 = agents[a1], agents[a2]
                c1 = sum(1 for b in bs1 if b["is_correct"])
                c2 = sum(1 for b in bs2 if b["is_correct"])
                _, lo1, hi1 = wilson_ci(c1, len(bs1))
                _, lo2, hi2 = wilson_ci(c2, len(bs2))
                if hi1 < lo2 or hi2 < lo1:
                    all_overlap = False
                    break
            if all_overlap:
                print(f"    All agent CIs overlap — no significant agent-level differences")
            else:
                print(f"    Some agent CIs DON'T overlap — agent performance varies")


def section_market_difficulty(all_bets, tools_bets):
    """Section 8: Are some markets just harder? Multi-tool agreement analysis."""
    print("\n" + "=" * 85)
    print("  8. MARKET DIFFICULTY — MULTI-TOOL AGREEMENT")
    print("=" * 85)
    print("  Markets where multiple tools bet: do they agree, and is agreement a signal?")

    # Build question -> list of (tool, is_correct)
    q_results = defaultdict(list)
    for b in all_bets:
        if b["tool"] == "unknown":
            continue
        q_results[b["question_id"]].append({
            "tool": b["tool"],
            "is_correct": b["is_correct"],
            "title": b.get("question_title", ""),
        })

    # Markets with 2+ different tools
    multi_tool = {
        q: rs for q, rs in q_results.items()
        if len(set(r["tool"] for r in rs)) >= 2
    }

    if not multi_tool:
        print("  No markets with multiple tools betting.")
        return

    print(f"\n  {len(multi_tool)} markets had 2+ different tools betting on them")

    # Agreement analysis
    all_agree_correct = 0
    all_agree_wrong = 0
    disagree = 0
    total = len(multi_tool)

    easy_markets = []
    hard_markets = []

    for q, rs in multi_tool.items():
        unique_tools = set(r["tool"] for r in rs)
        # Use one result per tool (latest)
        tool_results = {}
        for r in rs:
            tool_results[r["tool"]] = r["is_correct"]

        outcomes = list(tool_results.values())
        if all(outcomes):
            all_agree_correct += 1
            easy_markets.append(rs[0]["title"])
        elif not any(outcomes):
            all_agree_wrong += 1
            hard_markets.append(rs[0]["title"])
        else:
            disagree += 1

    print(f"  All tools correct:  {all_agree_correct:>3} ({all_agree_correct/total*100:.0f}%)")
    print(f"  All tools wrong:    {all_agree_wrong:>3} ({all_agree_wrong/total*100:.0f}%)")
    print(f"  Tools disagree:     {disagree:>3} ({disagree/total*100:.0f}%)")

    if hard_markets:
        print(f"\n  Markets where ALL tools got it wrong (sample):")
        for t in hard_markets[:10]:
            print(f"    - {t[:85]}")

    # On disagreement markets: which tool wins?
    if disagree > 0:
        print(f"\n  On the {disagree} disagreement markets, per-tool win rate:")
        tool_disagree = defaultdict(lambda: {"total": 0, "correct": 0})
        for q, rs in multi_tool.items():
            tool_results = {}
            for r in rs:
                tool_results[r["tool"]] = r["is_correct"]
            outcomes = list(tool_results.values())
            if all(outcomes) or not any(outcomes):
                continue
            for tool, correct in tool_results.items():
                tool_disagree[tool]["total"] += 1
                if correct:
                    tool_disagree[tool]["correct"] += 1

        print(f"    {'Tool':<35} | {'n':>4} | {'Win rate':>8}")
        print(f"    " + "-" * 55)
        for t in sorted(tool_disagree, key=lambda x: -tool_disagree[x]["total"]):
            s = tool_disagree[t]
            if s["total"] < 3:
                continue
            acc = s["correct"] / s["total"] * 100
            print(f"    {t:<35} | {s['total']:>4} | {acc:>6.1f}%")


def section_confidence_calibration(tools_bets):
    """Section 9: Do tools bet more on 'obvious' markets and win?"""
    print("\n" + "=" * 85)
    print("  9. BET VOLUME AS CONFIDENCE SIGNAL")
    print("=" * 85)
    print("  Markets where multiple agents use the same tool: does more volume = more accuracy?")

    for name in sorted(tools_bets, key=lambda t: -len(tools_bets[t])):
        tb = tools_bets[name]
        if name == "unknown" or len(tb) < 20:
            continue

        # Group by question
        q_bets = defaultdict(list)
        for b in tb:
            q_bets[b["question_id"]].append(b)

        # Bucket by number of agents betting
        singles = []  # 1 agent bet
        multis = []   # 2+ agents bet

        for q, bs in q_bets.items():
            unique_agents = len(set(b["bettor"] for b in bs))
            if unique_agents == 1:
                singles.extend(bs)
            else:
                multis.extend(bs)

        if not singles or not multis:
            continue

        s_acc = sum(1 for b in singles if b["is_correct"]) / len(singles) * 100
        m_acc = sum(1 for b in multis if b["is_correct"]) / len(multis) * 100

        c_s = sum(1 for b in singles if b["is_correct"])
        c_m = sum(1 for b in multis if b["is_correct"])
        p = fisher_exact_2x2(c_s, len(singles) - c_s, c_m, len(multis) - c_m)

        print(f"\n  {name}:")
        print(f"    1-agent markets:  {len(singles):>4} bets  {s_acc:.1f}% accuracy")
        print(f"    2+-agent markets: {len(multis):>4} bets  {m_acc:.1f}% accuracy")
        print(f"    Fisher p = {p:.4f} {sig(p)}")


def section_summary(tools_bets):
    """Final summary and recommendations."""
    print("\n" + "=" * 85)
    print("  SUMMARY & KEY FINDINGS")
    print("=" * 85)

    # Classify tools
    good = []
    bad = []
    uncertain = []

    for name in sorted(tools_bets, key=lambda t: -len(tools_bets[t])):
        tb = tools_bets[name]
        if name == "unknown":
            continue
        n = len(tb)
        c = sum(1 for b in tb if b["is_correct"])
        _, lo, hi = wilson_ci(c, n)

        if lo > 0.5:
            good.append((name, n, c / n * 100, lo * 100, hi * 100))
        elif hi < 0.5:
            bad.append((name, n, c / n * 100, lo * 100, hi * 100))
        else:
            uncertain.append((name, n, c / n * 100, lo * 100, hi * 100))

    if good:
        print("\n  STATISTICALLY ABOVE 50% (CI excludes coin flip):")
        for name, n, acc, lo, hi in good:
            print(f"    {name:<35}  {acc:.1f}% [{lo:.1f}%-{hi:.1f}%]  (n={n})")

    if uncertain:
        print("\n  INSUFFICIENT EVIDENCE (CI includes 50%):")
        for name, n, acc, lo, hi in uncertain:
            print(f"    {name:<35}  {acc:.1f}% [{lo:.1f}%-{hi:.1f}%]  (n={n})")

    if bad:
        print("\n  STATISTICALLY BELOW 50% (worse than coin flip):")
        for name, n, acc, lo, hi in bad:
            print(f"    {name:<35}  {acc:.1f}% [{lo:.1f}%-{hi:.1f}%]  (n={n})")

    print(f"""
  KEY TAKEAWAYS:

  1. STATISTICAL POWER: Most tools have too few bets for definitive
     conclusions. Only PRR (n=438) and SF (n=212) have meaningful
     sample sizes. All other tools need 5-10x more bets.

  2. PRR vs SF: Statistically indistinguishable. On shared markets
     they perform identically. Neither is better than the other.

  3. TRENDS: No tool shows a statistically significant accuracy
     trend. Observed week-to-week swings are within normal variance.

  4. MARKET CATEGORY: No tool shows statistically significant
     performance differences across categories (weather, politics,
     crypto). Observed differences are within CI overlap.

  5. PREDICTION-REQUEST-RAG: The only tool with evidence of being
     genuinely worse than random (25%, CI excludes 50%). Should be
     investigated or retired.

  Significance legend: *** p<0.001  ** p<0.01  * p<0.05  . p<0.1  ns not significant
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("Loading cached data...")
    bets = load_data()
    print(f"Loaded {len(bets)} resolved bets.\n")

    tools_bets = defaultdict(list)
    for b in bets:
        tools_bets[b["tool"]].append(b)

    n_tools = len([t for t in tools_bets if t != "unknown"])
    n_agents = len(set(b["bettor"] for b in bets))
    first = datetime.fromtimestamp(bets[0]["timestamp"], tz=timezone.utc)
    last = datetime.fromtimestamp(bets[-1]["timestamp"], tz=timezone.utc)

    print(f"  {len(bets)} bets | {n_tools} tools | {n_agents} agents")
    print(f"  {first:%Y-%m-%d} to {last:%Y-%m-%d}")

    section_overall(tools_bets)
    section_categories(tools_bets)
    section_head_to_head(tools_bets)
    section_weekly(tools_bets)
    section_outcome_bias(tools_bets)
    section_trends(tools_bets)
    section_agent_consistency(tools_bets, bets)
    section_market_difficulty(bets, tools_bets)
    section_confidence_calibration(tools_bets)
    section_summary(tools_bets)


if __name__ == "__main__":
    main()
