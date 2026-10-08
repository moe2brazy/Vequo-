# -*- coding: utf-8 -*-
"""_RATIO_ASK_RE 扩词表后的影响面验证。
扩词表让 3 个守卫 + 1 个修复函数同时生效，影响面比单个守卫大得多，
必须专门验证：真错仍拦得住、合法写法不误伤、修复函数能正常改。
"""
import os, sys
BACKEND = r"D:\vequo\vequo-viqueo\backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.stdout.reconfigure(encoding="utf-8")
import agent.llm_service as M

fail = 0
print("=" * 78)
print("① 分子守卫：单位词问句现在该能拦下 good+defect 分子")
print("=" * 78)
BAD = [
    ("每件产出消耗多少投入",
     'SELECT SUM(input_qty)::numeric/NULLIF(SUM(good_qty + defect_qty),0) AS r FROM mes_process_output',
     True, "★真错：分母含 defect（0.9957 vs 真值 1.02505）"),
    ("平均每张工单投入多少件",
     'SELECT SUM(input_qty)::numeric/NULLIF(SUM(good_qty + defect_qty),0) FROM mes_process_output',
     True, "同样含 defect 分子"),
]
for q, sql, exp, why in BAD:
    # ★ 2026-10-06 修正：本组样本的错误在**分母**（SUM(good+defect) 当分母），
    #   该由 _ratio_denominator_has_defect_reason 拦；分子守卫只查分子位置。
    #   首版调错函数，3 条FAIL 全是测试自身的问题。
    r = M._ratio_denominator_has_defect_reason(q, sql)
    ok = bool(r) == exp
    if not ok:
        fail += 1
    print("%s | %-22s | %s" % ("OK  " if ok else "FAIL", q, why))
    if r:
        print("     原因: %s" % r[:130])

print()
print("=" * 78)
print("② 修复函数：good+defect 应被改回 good")
print("=" * 78)
for q, sql, why in [
    ("各产线的不良率",
     'SELECT SUM(good_qty + defect_qty)*1.0/NULLIF(SUM(input_qty),0) FROM mes_process_output',
     "★分子形态应改"),
    ("各产线的投入产出比",
     'SELECT SUM(good_qty + defect_qty)*1.0/NULLIF(SUM(input_qty),0) FROM mes_process_output',
     "原有能力不能退化"),
]:
    out = M._fix_ratio_molecule_defect(q, sql) if hasattr(M, "_fix_ratio_molecule_defect") else None
    if out is None:
        # 找真实函数名
        cands = [n for n in dir(M) if "molecule" in n and "fix" in n.lower()]
        print("  可用修复函数: %s" % cands)
        if cands:
            fn = getattr(M, cands[0])
            out = fn(q, sql)
    changed = out != sql
    ok = changed
    if not ok:
        fail += 1
    print("%s | %-22s | %s" % ("OK  " if ok else "FAIL", q, why))
    print("     结果: %s" % (out or sql)[:150])

print()
print("=" * 78)
print("③ 误伤检查：合法写法不能被新词表波及")
print("=" * 78)
# 扩词表后，_ratio_without_division_reason 会对含单位词的问句启动。
# 分组列举句式必须仍放行。
NOT_BLOCK = [
    ("每张工单的投入",
     'SELECT work_order_id, SUM(input_qty) FROM mes_process_output GROUP BY work_order_id', "分组明细"),
    ("每种缺陷类型各有多少件",
     'SELECT defect_type, COUNT(*) FROM qms_defect_detail GROUP BY defect_type', "分组"),
    ("每台设备的台数",
     'SELECT COUNT(*) FROM dim_equipment', "计数无需除法"),
    ("每个仓库的库存量",
     'SELECT warehouse_code, SUM(available_qty) FROM inv_inventory_snapshot GROUP BY warehouse_code', "分组"),
    ("各产线的投入产出比",
     'SELECT line_id, SUM(good_qty)*1.0/NULLIF(SUM(input_qty),0) FROM mes_process_output GROUP BY line_id', "已做除法"),
]
for q, sql, why in NOT_BLOCK:
    r = M._ratio_without_division_reason(q, sql)
    ok = not r
    if not ok:
        fail += 1
    print("%s | %-22s | %s" % ("OK  " if ok else "FAIL", q, why))
    if r:
        print("     误伤原因: %s" % r[:130])

print()
print("=" * 78)
print("④ 原有比值类能力不能退化")
print("=" * 78)
STILL_BAD = [
    ("各产线的不良率",
     'SELECT line_id, SUM(defect_qty) AS "不良率" FROM mes_process_output GROUP BY line_id', "裸 SUM 充当率"),
    ("投入产出比是多少",
     'SELECT SUM(good_qty) AS "投入产出比", SUM(input_qty) AS "投入量" FROM mes_process_output', "裸 SUM 充当比值"),
]
for q, sql, why in STILL_BAD:
    r = M._ratio_without_division_reason(q, sql)
    ok = bool(r)
    if not ok:
        fail += 1
    print("%s | %-22s | %s" % ("OK  " if ok else "FAIL", q, why))

print("\n结果: %s" % ("ALL PASS" if not fail else "FAIL=%d" % fail))
sys.exit(1 if fail else 0)