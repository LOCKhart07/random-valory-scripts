import time

import requests

# ==== CONFIG ====
URL = "http://localhost:8716/configure_strategies"  # <-- change this
HEADERS = {"Content-Type": "application/json"}

# Only the "prompt" value changes
PROMPTS = [
    "change mech tool to superforcaster",
    "change mech tool to auto",
    "use risky strategy",
    "use safe strategy",
    "show me all mech tools",
    "what can you do",
    "hi",
    "place more bets",  # this one takes a while on gemini for some reason
    "what is your current strategy",
    "set strategy to balanced",
    "what is the optimal mech tool",
    "which mech tool should i use",
    "switch to the optimal strategy",
    "switch to the safest strategy",
    "list available strategies",
    "list available mech tools",
    "optimize for maximum profit",
]

# ==== MAIN LOOP ====
results = []

for i, prompt in enumerate(PROMPTS, start=1):
    body = {"prompt": prompt}
    print(f"\n➡️  Sending request {i}/{len(PROMPTS)}: {body}")
    start = time.time()
    try:
        response = requests.post(URL, headers=HEADERS, json=body)
        duration = round(time.time() - start, 3)  # seconds
        print(
            f"   {'✅' if response.status_code==200 else ''} Status: {response.status_code} | Time: {duration} s"
        )
        results.append(
            {
                "prompt": prompt,
                "status": response.status_code,
                "time_s": duration,
                "ok": response.ok,
            }
        )
    except Exception as e:
        duration = round((time.time() - start) * 1000, 2)
        print(f"   ❌ Error after {duration} s: {e}")
        results.append(
            {"prompt": prompt, "status": None, "time_s": duration, "error": str(e)}
        )

# ==== SUMMARY ====
print("\n=== SUMMARY ===")
avg_time = sum(r["time_s"] for r in results) / len(results)
print(f"Total: {len(results)} requests")
print(f"Average time: {avg_time:.2f} s")
