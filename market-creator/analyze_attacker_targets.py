"""
Analyze which market creators the attacker targeted and the breakdown of attacks.

Cross-references the attacker's Reality.io answer submissions with Omen FPMM
markets to determine which creator (Pearl/QS) each market belongs to, and
produces a breakdown by creator, answer side, and timeline.

Usage:
    python market-creator/analyze_attacker_targets.py
    python market-creator/analyze_attacker_targets.py --address 0xc5fd24b2974743896e1e94c47e99d3960c7d4c96
"""

import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

SUBGRAPH_API_KEY = os.getenv("SUBGRAPH_API_KEY", "")
REALITIO_SUBGRAPH_ID = "E7ymrCnNcQdAAgLbdFWzGE5mvr5Mb5T9VfT43FqA7bNh"
REALITIO_URL = f"https://gateway.thegraph.com/api/{SUBGRAPH_API_KEY}/subgraphs/id/{REALITIO_SUBGRAPH_ID}"
OMEN_SUBGRAPH_ID = "9fUVQpFwzpdWS9bq5WkAnmKbNNcoBwatMR4yZq81pbbz"
OMEN_URL = f"https://gateway.thegraph.com/api/{SUBGRAPH_API_KEY}/subgraphs/id/{OMEN_SUBGRAPH_ID}"

PEARL_CREATOR = "0xffc8029154ecd55abed15bd428ba596e7d23f557"
QS_CREATOR = "0x89c5cc945dd550bcffb72fe42bff002429f46fec"
CREATOR_LABELS = {PEARL_CREATOR: "Pearl", QS_CREATOR: "QS"}

SEP = "\u241f"
WEI = 10 ** 18


def post(url, q, v=None):
    p = {"query": q}
    if v:
        p["variables"] = v
    for attempt in range(4):
        try:
            r = requests.post(url, json=p, headers={"Content-Type": "application/json"}, timeout=90)
            r.raise_for_status()
            d = r.json()
            if "errors" in d:
                print(f"  Error: {d['errors']}")
                return None
            return d["data"]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * 2 ** attempt)


def main():
    parser = argparse.ArgumentParser(description="Analyze attacker targets by creator")
    parser.add_argument("--address", default="0xc5fd24b2974743896e1e94c47e99d3960c7d4c96",
                        help="Attacker address")
    args = parser.parse_args()
    addr = args.address.lower()

    print(f"Analyzing attacker: {addr}\n")

    # 1. Fetch all Reality.io responses by attacker
    print("[1/3] Fetching Reality.io answer submissions...")
    data = post(REALITIO_URL, '''
    query($user: String!) {
      responses(where: { user: $user }, first: 1000, orderBy: timestamp, orderDirection: asc) {
        timestamp answer bond
        question {
          questionId
          currentAnswer
          data
          responses(orderBy: timestamp, orderDirection: asc) {
            answer bond user timestamp
          }
        }
      }
    }
    ''', {"user": addr})

    responses = data.get("responses", [])
    print(f"  Found {len(responses)} answer submissions")

    # Build question map
    questions = {}
    for r in responses:
        q = r.get("question", {})
        qid = q.get("questionId", "")
        if not qid:
            continue
        answer_idx = int(r["answer"], 16)
        current = q.get("currentAnswer", "")
        is_final = current and current.lower() == r["answer"].lower()
        all_responses = q.get("responses", [])
        title = (q.get("data", "") or "").split(SEP)[0].strip()

        questions[qid] = {
            "qid": qid,
            "title": title,
            "attacker_answer": "Yes" if answer_idx == 0 else "No" if answer_idx == 1 else "Invalid",
            "is_final": is_final,
            "bond": int(r["bond"]) / WEI,
            "timestamp": int(r["timestamp"]),
            "n_responses": len(all_responses),
            "sole_responder": len(all_responses) == 1,
        }

    # 2. Match to Omen markets to find creators
    print("\n[2/3] Matching to Omen markets...")
    qid_list = list(questions.keys())
    market_map = {}  # qid -> {creator, market_id, volume}

    for i in range(0, len(qid_list), 50):
        batch = qid_list[i:i + 50]
        ids_str = ",".join(f'"{qid}"' for qid in batch)
        data = post(OMEN_URL, f"""
        {{
          fixedProductMarketMakers(
            where: {{ question_: {{ id_in: [{ids_str}] }} }}
            first: 1000
          ) {{
            id
            creator
            collateralVolume
            question {{ id }}
          }}
        }}
        """)
        if not data:
            continue
        for m in data.get("fixedProductMarketMakers", []):
            qid = m.get("question", {}).get("id", "")
            market_map[qid] = {
                "creator": m.get("creator", "").lower(),
                "market_id": m.get("id", ""),
                "volume": float(m.get("collateralVolume", 0)) / WEI,
            }

    print(f"  Matched {len(market_map)}/{len(questions)} questions to markets")

    # 3. Analyze
    print("\n[3/3] Analyzing...\n")

    # Enrich questions with creator info
    for qid, q in questions.items():
        m = market_map.get(qid, {})
        q["creator"] = CREATOR_LABELS.get(m.get("creator", ""), "Unknown")
        q["market_id"] = m.get("market_id", "")
        q["volume"] = m.get("volume", 0)

    # ---- By Creator ----
    by_creator = defaultdict(list)
    for q in questions.values():
        by_creator[q["creator"]].append(q)

    w = 80
    print("=" * w)
    print("ATTACKS BY CREATOR")
    print("=" * w)
    print(f"\n  {'Creator':<10} {'Markets':>8} {'Final':>6} {'Sole':>6} {'Yes':>5} {'No':>5} {'Volume':>12}")
    print("  " + "-" * 60)

    for creator in ["Pearl", "QS", "Unknown"]:
        qs = by_creator.get(creator, [])
        if not qs:
            continue
        n = len(qs)
        final = sum(1 for q in qs if q["is_final"])
        sole = sum(1 for q in qs if q["sole_responder"])
        yes = sum(1 for q in qs if q["attacker_answer"] == "Yes")
        no = sum(1 for q in qs if q["attacker_answer"] == "No")
        vol = sum(q["volume"] for q in qs)
        print(f"  {creator:<10} {n:>8} {final:>6} {sole:>6} {yes:>5} {no:>5} {vol:>11.4f}")

    # ---- Timeline by creator ----
    print(f"\n{'=' * w}")
    print("TIMELINE BY CREATOR")
    print("=" * w)

    by_day_creator = defaultdict(lambda: defaultdict(int))
    for q in questions.values():
        day = datetime.fromtimestamp(q["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        by_day_creator[day][q["creator"]] += 1

    print(f"\n  {'Date':<12} {'Pearl':>7} {'QS':>7} {'Unknown':>8} {'Total':>7}")
    print("  " + "-" * 45)
    for day in sorted(by_day_creator.keys()):
        counts = by_day_creator[day]
        total = sum(counts.values())
        print(f"  {day:<12} {counts.get('Pearl',0):>7} {counts.get('QS',0):>7} "
              f"{counts.get('Unknown',0):>8} {total:>7}")

    # ---- Answer side by creator ----
    print(f"\n{'=' * w}")
    print("ANSWER SIDE BY CREATOR")
    print("=" * w)

    for creator in ["Pearl", "QS"]:
        qs = by_creator.get(creator, [])
        if not qs:
            continue
        yes = [q for q in qs if q["attacker_answer"] == "Yes"]
        no = [q for q in qs if q["attacker_answer"] == "No"]
        yes_final = sum(1 for q in yes if q["is_final"])
        no_final = sum(1 for q in no if q["is_final"])
        print(f"\n  {creator}:")
        print(f"    Yes answers: {len(yes)} ({yes_final} became final)")
        print(f"    No answers:  {len(no)} ({no_final} became final)")

    # ---- Full list ----
    print(f"\n{'=' * w}")
    print("ALL ATTACKED MARKETS")
    print("=" * w)
    print(f"\n  {'Date':<12} {'Creator':<7} {'Ans':<4} {'Final':<6} {'Sole':<5} {'Vol':>8}  Title")
    print("  " + "-" * 95)

    for q in sorted(questions.values(), key=lambda x: x["timestamp"]):
        day = datetime.fromtimestamp(q["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        final = "YES" if q["is_final"] else "no"
        sole = "YES" if q["sole_responder"] else "no"
        print(f"  {day:<12} {q['creator']:<7} {q['attacker_answer']:<4} {final:<6} {sole:<5} "
              f"{q['volume']:>7.2f}  {q['title'][:55]}")

    # ---- Summary ----
    total = len(questions)
    total_final = sum(1 for q in questions.values() if q["is_final"])
    total_sole = sum(1 for q in questions.values() if q["sole_responder"])
    total_yes = sum(1 for q in questions.values() if q["attacker_answer"] == "Yes")

    print(f"\n{'=' * w}")
    print("SUMMARY")
    print("=" * w)
    print(f"  Total attacked markets: {total}")
    print(f"  Pearl: {len(by_creator.get('Pearl', []))}  QS: {len(by_creator.get('QS', []))}  Unknown: {len(by_creator.get('Unknown', []))}")
    print(f"  Answer became final: {total_final}/{total} ({total_final/total*100:.0f}%)")
    print(f"  Sole responder: {total_sole}/{total} ({total_sole/total*100:.0f}%)")
    print(f"  Answered Yes: {total_yes}/{total} ({total_yes/total*100:.0f}%)")
    print()


if __name__ == "__main__":
    main()
