# -*- coding: utf-8 -*-
"""词表同步性测试：守卫侧与修复器侧的词表必须一致。
第 17 轮审计发现的 5 个问题之一就是「守卫有『每万件』、修复器没有」——
守卫拦得住、修不出，等于只拦不修。凡新增单位词，两侧必须同步。
"""
import os, re, sys
BACKEND = r"D:\vequo\vequo-viqueo\backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.stdout.reconfigure(encoding="utf-8")
import agent.llm_service as M

fail = 0

print("=" * 74)
print("① 单位词表同步：守卫侧 ⊇ 修复器侧")
print("=" * 74)
guard = {w for w, _ in M._UNIT_PER}
fix = {w for w, _, _, _ in M._UNIT_FIX_RULES}
stack = set(M._UNIT_STACKABLE)

only_guard = sorted(guard - fix)
only_fix = sorted(fix - guard)
if only_guard:
    print("  ★ 守卫有但修复器无（拦得住、修不出）: %s" % only_guard)
    fail += 1
else:
    print("  OK 守卫词全部有对应修复规则")
if only_fix:
    print("  ★ 修复器有但守卫无（修得动、拦不住）: %s" % only_fix)
    fail += 1
else:
    print("  OK 修复规则全部有守卫对应")
if stack - fix:
    print("  ★ 可叠加表有但主规则表无: %s" % sorted(stack - fix))
    fail += 1
else:
    print("  OK 可叠加表 ⊆ 主规则表")

print()
print("=" * 74)
print("② _RATIO_ASK_RE 也应覆盖全部单位词（比值守卫族的前置条件）")
print("=" * 74)
miss = [w for w in fix if not re.search(re.escape(w), M._RATIO_ASK_RE.pattern)]
if miss:
    print("  ★ _RATIO_ASK_RE 漏了: %s" % miss)
    fail += 1
    print("     ⇒ 这些单位问句会绕过整个比值守卫族"
          "（分子含 defect / 分母含 defect / 必须做除法）")
else:
    print("  OK 全部单位词都在 _RATIO_ASK_RE 里")

print()
print("=" * 74)
print("③ 守卫与修复器对同一问句的判断必须一致（拦得住 ⇒ 修得出）")
print("=" * 74)
PROBES = [
    "每千件的投入是多少", "每百件的投入是多少", "每万件的投入是多少",
    "每件产出消耗多少投入", "平均每张工单投入多少件", "每台设备日均产出",
    "每天的投入量", "每小时的投入量", "每月投入量",
]
for q in PROBES:
    bad_sql = "SELECT SUM(input_qty) AS input_qty FROM mes_process_output"
    blocked = bool(M._unit_conversion_reason(q, bad_sql))
    fixed = M._fix_unit_conversion(q, bad_sql)
    repaired = fixed != bad_sql
    ok = (not blocked) or repaired
    if not ok:
        fail += 1
    print("  %s | %-22s 守卫拦=%-5s 修复器改=%-5s"
          % ("OK  " if ok else "FAIL", q, blocked, repaired))

print()
print("=" * 74)
print("④ 不可修单位必须有声明出口（缺源字段闸门）")
print("=" * 74)
for q in ["人均产出是多少", "每个工人一天能产多少件", "每批产出多少"]:
    blocked = bool(M._unit_conversion_reason(q, "SELECT SUM(good_qty) AS g FROM mes_process_output"))
    changed = M._fix_unit_conversion(q, "SELECT SUM(good_qty) AS g FROM mes_process_output") != \
        "SELECT SUM(good_qty) AS g FROM mes_process_output"
    declared = bool(M._underivable_metric_reason(q))
    ok = (not blocked) or (not changed and declared)
    if not ok:
        fail += 1
    print("  %s | %-22s 拦=%-5s 改写=%-5s 有声明=%s"
          % ("OK  " if ok else "FAIL", q, blocked, changed, declared))

print()
print("=" * 74)
print("⑤ 死代码：今日新增符号必须都被引用")
print("=" * 74)
src = open(os.path.join(BACKEND, "agent", "llm_service.py"), encoding="utf-8").read()
for sym in ("_rows_effectively_empty", "_unit_conversion_reason",
            "_fix_unit_conversion", "_ratio_denominator_has_defect_reason",
            "_time_condition_missing_reason", "_scale_factor",
            "_UNIT_FIX_RULES", "_UNIT_STACKABLE", "_TIME_COND_COLS",
            "_ROLL_RATIO_ASK_RE_NARROW"):
    n = len(re.findall(r"\b%s\b" % re.escape(sym), src))
    ok = n > 1
    if not ok:
        fail += 1
    print("  %s %-36s 引用 %d 次" % ("OK  " if ok else "FAIL", sym, n))

print()
print("结果: %s" % ("ALL PASS" if not fail else "FAIL=%d" % fail))
sys.exit(1 if fail else 0)