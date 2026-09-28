# -*- coding: utf-8 -*-
"""收集轮换候选问题（走真实 HTTP:8010，多 seed 并集）"""
import json, urllib.request

def fetch(seed):
    url = f"http://localhost:8010/api/suggest/questions?limit=8&seed={seed}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))

seen: dict[str, str] = {}
for i in range(50):
    try:
        d = fetch(i * 7919 + 13)
    except Exception as e:
        print("ERR", i, e)
        continue
    items = d.get("questions") or d.get("items") or []
    if items and isinstance(items[0], str):
        for q in items:
            seen.setdefault(str(q).strip(), "")
    else:
        for it in items:
            if not isinstance(it, dict):
                continue
            q = it.get("question") or it.get("q") or ""
            src = it.get("category") or it.get("source") or ""
            if q:
                seen.setdefault(str(q).strip(), src)
    if i % 25 == 0:
        print("progress", i, "unique", len(seen), flush=True)
    if len(seen) >= 40:
        break

qs = [{"question": q, "src": s} for q, s in seen.items()]
with open("backend/tests/suggest_questions_pool.json", "w", encoding="utf-8") as f:
    json.dump(qs, f, ensure_ascii=False, indent=1)
print("候选问题数:", len(qs), flush=True)
for q in qs:
    print(" -", q["question"], "|", q["src"], flush=True)
