# -*- coding: utf-8 -*-
"""遗留 2 的守卫：问句限定（含时间/维度）必须体现在 SQL 里 —— 先做时间这一维。
设计依据（本库实查，不是猜）：
  qms_defect_detail  ★唯一无日期字段的事实表
      → 时间条件必须经 inspection_id → qms_inspection.inspection_date 落地
  其余事实表都有日期列：mes_process_output.stat_date /
      qms_inspection.inspection_date / eqp_downtime_record.start_time /
      mes_work_order.start_date / inv_inventory_snapshot.snapshot_date
且实测「今天/最近/本月」类问句在有日期列的表上**都能正确落地**（6 条里5 条OK），
唯一漏的就是 qms_defect_detail 这条。
"""
import os, re, sys
BACKEND = r"D:\vequo\vequo-viqueo\backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.stdout.reconfigure(encoding="utf-8")
import agent.llm_service as M

# 库里所有日期列（含时间列），出现任一即认为"时间条件已落地"
_DATE_COLS = ("stat_date", "inspection_date", "start_time", "end_time",
              "start_date", "end_date", "snapshot_date", "downtime_date",
              "record_date", "event_date", "check_date")
# 无日期字段、必须经关联表取日期的表 → (表, 关联到日期的路径说明)
_NO_DATE_TABLES = {
    "qms_defect_detail": "该表无日期字段，须经 inspection_id 关联 qms_inspection.inspection_date",
}

# 判据：问句含时间限定 → SQL 必须有日期列的过滤
BLOCK = [
    ("今天的缺陷数", 'SELECT COUNT(*) AS "缺陷数" FROM qms_defect_detail f LIMIT 1',
     True, "★#5真实错答：报 2115（全量）"),
    ("今天的缺陷数量是多少", 'SELECT COUNT(*) FROM qms_defect_detail', True, "同形态"),
    ("今天有多少缺陷", 'SELECT defect_type, COUNT(*) FROM qms_defect_detail GROUP BY defect_type', True, "分组也算漏"),
    ("本月缺陷数", 'SELECT COUNT(*) FROM qms_defect_detail', True, "本月同漏"),
    # 有日期列的表，正确落地 → 必须放行
    ("今天的良率", 'SELECT SUM(good_qty)*1.0/NULLIF(SUM(input_qty),0) FROM mes_process_output f WHERE stat_date >= CURRENT_DATE', False, "已落地"),
    ("今天的产量", 'SELECT SUM(good_qty) FROM mes_process_output f WHERE stat_date >= CURRENT_DATE', False, "已落地"),
    ("最近七天的投入量", 'SELECT SUM(input_qty) FROM mes_process_output f WHERE stat_date >= CURRENT_DATE - INTERVAL \'6 days\'', False, "已落地"),
    # 绝对月份
    ("九月的缺陷数", 'SELECT COUNT(*) FROM qms_defect_detail d JOIN qms_inspection i ON d.inspection_id=i.inspection_id WHERE i.inspection_date >= \'2026-09-01\'', False, "走关联表取日期，正确"),
    # 无时间限定 → 不适用
    ("缺陷总数", 'SELECT COUNT(*) FROM qms_defect_detail', False, "无时间限定"),
    ("缺陷类型有哪些", 'SELECT DISTINCT defect_type FROM qms_defect_detail', False, "无时间限定"),
]

fail = 0
for q, sql, exp, why in BLOCK:
    r = M._time_condition_missing_reason(q, sql) if hasattr(M, "_time_condition_missing_reason") else "NO_FUNC"
    blocked = bool(r) if r != "NO_FUNC" else None
    if blocked is None:
        print("!! 守卫函数尚未实现")
        sys.exit(1)
    ok = blocked == exp
    if not ok:
        fail += 1
    print("%s | %-22s exp=%-4s got=%-4s | %s"
          % ("OK  " if ok else "FAIL", q, "拦" if exp else "放",
             "拦" if blocked else "放", why))
    if r and exp:
        print("     原因: %s" % r[:140])

print("\n结果: %s" % ("ALL PASS" if not fail else "FAIL=%d" % fail))
sys.exit(1 if fail else 0)