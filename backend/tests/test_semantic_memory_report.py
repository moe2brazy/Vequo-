# -*- coding: utf-8 -*-
"""P0-1 语义构建 / P1-2 经营记忆 / P1-4 报告资产 冒烟测试"""
import sys
sys.path.insert(0, '.')

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

print("═══ P0-1 语义层自动构建 Agent ═══")
from agent.semantic_builder import scan_schema_candidates
r = scan_schema_candidates(limit=10, use_llm=False)
check("扫描成功", r.get("success"), str(r.get("error"))[:120])
check("产出候选", len(r.get("candidates") or []) > 0, f"total={r.get('total')}")
if r.get("candidates"):
    c = r["candidates"][0]
    check("候选含表达式/表/来源", c.get("expr") and c.get("tables") and c.get("source") == "schema_scan", str(c)[:120])

# 候选入池 → 人工采纳链路（adopt 后清理，避免污染注册表）
from agent.metric_miner import list_candidates, adopt_candidate, ignore_candidate, add_schema_candidates
sc = [c for c in list_candidates() if c.get("source") == "schema_scan"]
print(f"  候选池 schema_scan 数: {len(sc)}")

print("═══ P1-2 经营记忆中心 ═══")
from agent.memory_center import record_preference, get_preferences, add_note, list_notes, delete_note, memory_hint
record_preference("良率", ["工序"])
record_preference("良率", ["工序"])
record_preference("良率", ["工序"])
prefs = get_preferences()
check("偏好累计", prefs and prefs[0].get("metric") == "良率" and prefs[0].get("count") >= 3, str(prefs[:1]))
n = add_note("良率", "测试备注：良率按合格/投入口径", "tester", "admin")
notes = list_notes("良率", "admin", "tester")
check("备注录入+列表", any(x.get("id") == n["id"] for x in notes))
hint = memory_hint("各工序的良率是多少", "admin", "tester")
check("memory_hint 命中备注", "测试备注" in hint, hint[:80])
check("备注删除", delete_note(n["id"]))

print("═══ P1-4 报告资产 ═══")
from agent.report_assets import save_report, list_reports, get_report, update_report, delete_report, split_sections
secs = split_sections("---执行摘要---\n产量1,234件\n---总体结论---\n整体良好")
check("章节切分", len(secs) == 2 and secs[0]["title"] == "执行摘要", str(secs))
a = save_report("测试报告", "---执行摘要---\n产量1,234件\n---总体结论---\n整体良好", ["test_orders"], "tester", "admin")
check("报告保存", bool(a.get("id")))
lst = list_reports("admin", "tester")
check("报告列表可见", any(x.get("id") == a["id"] for x in lst))
lst2 = list_reports("guest", "other")
check("角色隔离（guest 看不到他人报告）", not any(x.get("id") == a["id"] for x in lst2))
d = get_report(a["id"])
check("报告详情含正文", d and "执行摘要" in d.get("markdown", ""))
u = update_report(a["id"], {"markdown": "---执行摘要---\n更新版"})
check("报告再生成覆盖", u and "更新版" in u.get("markdown", ""))
check("报告删除", delete_report(a["id"]))

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
