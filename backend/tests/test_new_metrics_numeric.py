# -*- coding: utf-8 -*-
"""三条新增口径的**数值**回归测试（2026-10-06 第14轮）。

只测"能不能命中"是不够的：第 14 轮出现过口径已写入、find_metrics 也命中，
但 (a) exec_sql 没配→ 回退 LLM 编成 `GROUP BY defect_id`（Q2 答非所问）；
    (b) sql_expression 存在 PostgreSQL 整数除法陷阱 → 检验良率全0（Q7 四个 0）；
    (c) 维度词表缺「工序」→ 选不出模板 → exec_sql 完全不生效。
三个坑都只在**真库跑数字**时才暴露，故本测试必须连库。

期望值来源：库内直接 GROUP BY + 三套独立算法互检（偏差 0.0000）。
运行：python tests/test_new_metrics_numeric.py
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.stdout.reconfigure(encoding="utf-8")

FAIL = 0

try:
    import psycopg2
except Exception as e:
    print("SKIP: 无 psycopg2 (%s)" % e)
    sys.exit(0)

try:
    from agent.metric_registry import render_exec_sql_metric
except Exception as e:
    print("IMPORT FAIL:", e)
    sys.exit(1)

DSN = "postgresql://postgres:123456@localhost:5432/yans"

# ── 真值（库内直接 GROUP BY，2026-10-06 实测）──
GOLD = {
    "责任工序缺陷数": {
        # 双口径并列：缺陷记录数 COUNT(*) 与 缺陷件数 SUM(defect_qty) 同时输出
        "cols": ["责任工序", "缺陷记录数", "记录数占比(%)", "缺陷件数", "件数占比(%)"],
        # 每行 = (工序, 记录数, 记录占比, 件数, 件数占比)
        "rows": [("功能测试", "695", "32.86", "2340", "44.38"),
                 ("老化测试", "701", "33.14", "2084", "39.52"),
                 ("AOI检测", "369", "17.45", "442", "8.38"),
                 ("包装入库", "350", "16.55", "407", "7.72")],
    },
    "责任工序缺陷量": {
        "cols": ["责任工序", "缺陷数量"],
        "rows": [("功能测试", 2340), ("老化测试", 2084),
                 ("AOI检测", 442), ("包装入库", 407)],
    },
    "检验良率": {
        "cols": ["工序名称", "检验良率(%)", "标准良率(%)", "差距(百分点)"],
        # 每行 = (工序名, 检验良率, 标准良率, 差距)
        "rows": [("功能测试", "94.78", "96.50", "-1.72"),   # 差距最大排最前
                 ("老化测试", "95.29", "96.80", "-1.51"),
                 ("包装入库", "99.08", "99.20", "-0.12"),
                 ("AOI检测", "99.02", "99.00", "0.02")],
    },
}
QUESTIONS = {
    "责任工序缺陷数": "按责任工序统计缺陷数量并降序排列",
    "责任工序缺陷量": "责任工序缺陷量是多少",
    "检验良率": "各工序的检验良率是多少，跟标准良率比哪个差得最多",
}

try:
    con = psycopg2.connect(DSN)
except Exception as e:
    print("SKIP: 连不上库 (%s)" % e)
    sys.exit(0)
cu = con.cursor()

for metric, q in QUESTIONS.items():
    print("\n" + "=" * 70)
    print("##", metric, "|", q)
    r = render_exec_sql_metric(q)
    if not r:
        print("FAIL: render_exec_sql_metric 返回 None（exec_sql 未生效）")
        FAIL += 1
        continue
    if r.get("metric") != metric:
        print("FAIL: 命中口径是 %s，期望 %s" % (r.get("metric"), metric))
        FAIL += 1
        continue
    try:
        cu.execute(r["sql"])
    except Exception as e:
        print("FAIL: SQL 执行失败 —— %s" % e)
        FAIL += 1
        continue
    cols = [c[0] for c in cu.description]
    rows = cu.fetchall()
    print("   列:", cols)
    for row in rows[:6]:
        print("   ", row)

    gold = GOLD[metric]
    if cols != gold["cols"]:
        print("FAIL: 列名不符\n期望 %s\n实得 %s" % (gold["cols"], cols))
        FAIL += 1
        continue
    got = {r[0]: r[1:] for r in rows}
    for expect in gold["rows"]:
        name = expect[0]
        wanted = expect[1:]
        if name not in got:
            print("FAIL: 缺行 %s（实得行 %s）" % (name, list(got)))
            FAIL += 1
            continue
        actual = got[name]
        for i, exp in enumerate(wanted):
            a = actual[i]
            # 统一按数值比（Decimal/float/int 混用），非数值才退回字符串
            try:
                same = abs(float(a) - float(exp)) < 0.005
            except (TypeError, ValueError):
                same = str(a) == str(exp)
            if not same:
                print("FAIL: %s 第%d列 期望 %s 实得 %s"
                      % (name, i + 1, exp, a))
                FAIL += 1

# ── 整数除法回归：口径表达式必须含 ::numeric ──
print("\n" + "=" * 70)
import json
reg = json.load(open(os.path.join(BACKEND, "metrics_registry.json"),
                     encoding="utf-8"))
for m in reg:
    if str(m.get("name")) == "检验良率":
        expr = str(m.get("sql_expression") or "")
        ok = "::numeric" in expr
        print("%s 检验良率表达式含 ::numeric | %s" % ("OK  " if ok else "FAIL", expr[:90]))
        if not ok:
            FAIL += 1

cu.close(); con.close()
print("\n结果: %s" % ("ALL PASS" if not FAIL else "FAIL=%d" % FAIL))
sys.exit(1 if FAIL else 0)