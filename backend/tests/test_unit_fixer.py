# -*- coding: utf-8 -*-
"""验证 _fix_unit_conversion 产出正确：既要对（真值逐格相等），
又不能乱动（分组列举/已有除法/不可修单位必须原样返回）。
"""
import os, sys
BACKEND = r"D:\vequo\vequo-viqueo\backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.stdout.reconfigure(encoding="utf-8")
import psycopg2
# ★直接用后端里的实现，**不要** import _vequo_fb 里的草稿副本。
#   2026-10-06 踩过：测试 import 草稿副本，代码改了测试却仍在测旧实现，
#   表现为「明明改了还 FAIL」，白排查一轮。
from agent.llm_service import _fix_unit_conversion as fx

con = psycopg2.connect("postgresql://postgres:123456@localhost:5432/yans")
cu = con.cursor()

CASES = [
    # (问句, 错SQL, 期望真值, 期望是否该改)
    ("平均每张工单投入多少件",
     "SELECT AVG(input_qty) AS input_qty FROM mes_process_output", 9264.56, True),
    ("每张工单平均投入多少件",
     "SELECT AVG(m.input_qty) AS input_qty FROM mes_process_output m", 9264.56, True),
    ("每件产出消耗多少投入",
     "SELECT SUM(input_qty) AS input_qty FROM mes_process_output", 1.02505, True),
    ("投入产出比换算成每千件的投入",
     "SELECT SUM(input_qty) AS input_qty FROM mes_process_output", 1025.05, True),
    ("每天的投入量",
     "SELECT SUM(input_qty) AS input_qty FROM mes_process_output", 70822.42, True),
    ("每台设备日均产出",
     "SELECT SUM(good_qty) AS good_qty FROM mes_process_output", 1439.41, True),
    # ★第17轮新增：倍率词的分子由「问句要什么量」决定。
    #   首版分母写死 SUM(good_qty)，当投影恰好也是 good_qty 时
    #   产出 SUM(good)/SUM(good)*1000 = 恒等 1000（真值 1025.05，差 2.5% 无征兆）。
    #   每千件产出有多少不良 →分子取 SUM(defect_qty)（真值 91737/3109120*1000=29.5058）
    ("每千件产出有多少不良",
     "SELECT SUM(good_qty) AS good_qty FROM mes_process_output", 29.5058, True),
    # 问句没说清折算哪个量 → 不擅自改写
    ("每千件是多少",
     "SELECT SUM(good_qty) AS good_qty FROM mes_process_output", None, False),
    # 不可修 / 不该动
    ("人均产出是多少",
     "SELECT SUM(good_qty) AS good_qty FROM mes_process_output", None, False),
    ("每批产出多少",
     "SELECT SUM(good_qty) AS good_qty FROM mes_process_output", None, False),
    ("每张工单的投入",
     "SELECT work_order_id, SUM(input_qty) AS input_qty FROM mes_process_output GROUP BY work_order_id",
     None, False),
    ("每天各产线的产量",
     "SELECT stat_date, line_id, SUM(input_qty) AS input_qty FROM mes_process_output GROUP BY stat_date, line_id",
     None, False),
    ("各产线的投入产出比",
     "SELECT line_id, SUM(good_qty)*1.0/NULLIF(SUM(input_qty),0) AS roi FROM mes_process_output GROUP BY line_id",
     None, False),
]

fail = 0
for q, sql, truth, should_change in CASES:
    out = fx(q, sql)
    changed = out != sql
    print("=" * 76)
    print("Q:", q)
    print("  原: %s" % sql[:110])
    print("  新: %s" % out[:150])
    if not should_change:
        ok = not changed
        if not ok:
            fail += 1
        print("  %s 期望不改，实际%s" % ("OK  " if ok else "FAIL",
                                       "改了" if changed else "未改"))
        continue
    if not changed:
        fail += 1
        print("  FAIL 期望改写但未改")
        continue
    # 真库执行核对
    try:
        cu.execute(out)
        rows = cu.fetchall()
        # psycopg2 默认返回 tuple（不是 dict）——首版按 dict 取值，全栽在这
        if not rows:
            print("  FAIL 执行返回空")
            fail += 1
            continue
        got = rows[0][0] if isinstance(rows[0], (tuple, list)) else list(rows[0].values())[0]
        if got is None:
            print("  FAIL 执行返回 NULL")
            fail += 1
            continue
        # ★ 比对**未舍入**的原始值。首版先 round(,2) 再比，把 1.0250517
        #   压成 1.03 再按 0.1% 容差比 1.02505 ⇒ 相对误差 0.48% 判成 FAIL，
        #   而SQL 本身完全正确。舍入会吃掉小量级比值的全部有效数字。
        got_f = float(got)
        close = abs(got_f - truth) / max(abs(truth), 1e-9) < 0.001
        if not close:
            fail += 1
        print("  %s 真值=%s 实得=%s" % ("OK  " if close else "FAIL", truth, got_f))
    except Exception as e:
        fail += 1
        print("  FAIL 执行出错: %s" % e)

print()
print("结果: %s" % ("ALL PASS" if not fail else "FAIL=%d" % fail))
con.close()
sys.exit(1 if fail else 0)