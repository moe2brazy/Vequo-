# -*- coding: utf-8 -*-
"""_unit_conversion_reason 边界自测：拦真错 + 放合法，重点测误伤。

误伤是这类守卫最大的风险：「每个产品的产量」这类分组列举若被拦，
会把正确 SQL逼成错的。故反向用例比正向更多。
"""
import os, sys
BACKEND = r"D:\vequo\vequo-viqueo\backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.stdout.reconfigure(encoding="utf-8")
from agent.llm_service import _unit_conversion_reason as g

# (问句, SQL, 期望被拦, 说明)
CASES = [
    # ── 必须拦：82 题实测的真实错答 ──
    ("平均每张工单投入多少件",
     'SELECT AVG(input_qty) AS "平均工单投入量" FROM mes_process_output', True, "#31 真实错答"),
    ("每件产出消耗多少投入",
     'SELECT SUM(input_qty) AS "消耗", SUM(good_qty) AS "产出" FROM mes_process_output', True, "#32 真实错答（没除）"),
    ("每条产线每小时投入多少件",
     'SELECT line_id, SUM(input_qty) FROM mes_process_output GROUP BY line_id', True, "#35 真实错答（没除时间）"),
    ("每台设备日均产出是多少件",
     'SELECT SUM(good_qty) FROM mes_process_output', True, "日均+每台，双换算"),
    ("人均产出是多少",
     'SELECT SUM(good_qty) FROM mes_process_output', True, "人均（虽缺人数字段，也该拦下说明）"),
    ("每天的投入量",
     'SELECT SUM(input_qty) FROM mes_process_output', True, "每天没除天数"),
    ("投入产出比换算成每千件的投入",
     'SELECT SUM(input_qty) FROM mes_process_output', True, "#33 该给全厂单值"),
    # ── 必须放行：已做换算 ──
    ("平均每张工单投入多少件",
     'SELECT SUM(input_qty)/COUNT(DISTINCT work_order_id) FROM mes_process_output', False, "已÷工单数"),
    ("每件产出消耗多少投入",
     'SELECT SUM(input_qty)::numeric/NULLIF(SUM(good_qty),0) FROM mes_process_output', False, "已÷产出"),
    ("每台设备日均产出",
     'SELECT SUM(good_qty)/COUNT(DISTINCT equipment_id) FROM mes_process_output', False, "已÷设备数"),
    # ── 必须放行：分组列举（最大误伤风险区）──
    ("每张工单的投入",
     'SELECT work_order_id, SUM(input_qty) FROM mes_process_output GROUP BY work_order_id', False, "分组明细"),
    ("列出每张工单的投入",
     'SELECT work_order_id, SUM(input_qty) FROM mes_work_order GROUP BY work_order_id', False, "明细列举"),
    ("每个仓库的库存量",
     'SELECT warehouse_code, SUM(available_qty) FROM inv_inventory_snapshot GROUP BY warehouse_code', False, "分组"),
    ("每个产品类别的产量",
     'SELECT category, SUM(good_qty) FROM dim_product GROUP BY category', False, "分组"),
    ("每种缺陷类型各有多少件",
     'SELECT defect_type, COUNT(*) FROM qms_defect_detail GROUP BY defect_type', False, "★分组列举，不拦"),
    ("各产线的投入产出比",
     'SELECT line_id, SUM(good_qty)*1.0/NULLIF(SUM(input_qty),0) FROM mes_process_output GROUP BY line_id', False, "比值走 ②.5"),
    ("不良率是多少",
     'SELECT SUM(defect_qty)*1.0/NULLIF(SUM(input_qty),0) FROM mes_process_output', False, "率类不归本守卫"),
    # ── 边界：无单位词 / 空 SQL ──
    ("各产线的产量", 'SELECT line_id, SUM(good_qty) FROM mes_process_output GROUP BY line_id', False, "无单位词"),
    ("每台设备的台数", 'SELECT COUNT(*) FROM dim_equipment', False, "计数类天然无需除法"),
    ("每小时投入多少件", "", False, "空 SQL"),
    ("每小时投入多少件", None, False, "None SQL"),
    ("", 'SELECT SUM(x) FROM t', False, "空问句"),
]

fail = 0
for q, sql, exp_block, why in CASES:
    try:
        r = g(q, sql)
    except Exception as e:
        r = "EXC:%s" % type(e).__name__
        why += " ←抛异常"
    blocked = bool(r)
    ok = blocked == exp_block
    if not ok:
        fail += 1
    print("%s | %-30s exp=%-6s got=%-6s | %s"
          % ("OK  " if ok else "FAIL", q[:30], "拦" if exp_block else "放",
             "拦" if blocked else "放", why))
    if not ok and blocked:
        print("       原因: %s" % r[:150])

print("\n结果: %s（拦 %d / 放 %d）"
      % ("ALL PASS" if not fail else "FAIL=%d" % fail,
         sum(1 for c in CASES if c[2]), sum(1 for c in CASES if not c[2])))
sys.exit(1 if fail else 0)