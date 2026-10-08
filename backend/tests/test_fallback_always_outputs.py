# -*- coding: utf-8 -*-
"""第十一轮回归：AI 兜底「必定出结果」三条硬约束

1) 闸门不再源头拒绝（命中后继续生成）
2) 未注册口径的提示词硬约束必被注入
3) 退化输出守卫：`表名.*` 必须拦下，但明细意图问句必须豁免
4) 指标形态问法必须判为需聚合（否则裸明细守卫整条跳过）
5) 【执行说明】拼接幂等

运行：cd backend && python tests/test_fallback_always_outputs.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.llm_service as M  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("  [%s] %s%s" % (tag, name, ("  <- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILED.append(name)


# ── 1) 闸门不再源头拒绝 ────────────────────────────────────────────────
print("=== 1. 未注册口径：判定命中但不阻断 ===")
# 这些口径库里确实没有源字段（人均无人数表、周转无出入库流水、成本无成本列）
UNREG = ["人均产量是多少", "库存周转率是多少", "单位产品成本是多少",
         "OEE 是多少", "设备利用率是多少"]
for q in UNREG:
    r = M._underivable_metric_reason(q)
    check("命中闸门: %s" % q, r is not None, "返回 None（未命中）")
    h = M._build_agg_hint(q) or ""
    check("注入硬约束: %s" % q, "【口径未注册·必须声明】" in h)

# 已注册/可推导的口径不应命中闸门
for q in ("综合设备停机率", "稼动率", "各产线的产量"):
    check("不误伤已注册口径: %s" % q, M._underivable_metric_reason(q) is None)

# ── 2) 指标形态问法必须判为需聚合 ───────────────────────────────────────
print("=== 2. _needs_aggregation：指标形态词 ===")
MUST_AGG = ["单位产品成本是多少", "设备利用率是多少", "产能利用率是多少", "OEE 是多少",
            "库存周转率是多少", "人均产量是多少", "良率是多少", "平均单价是多少",
            "单件成本", "设备综合效率", "返工造成的产出损失是多少"]
for q in MUST_AGG:
    check("判为需聚合: %s" % q, M._needs_aggregation(q) is True)

print("=== 2b. _needs_aggregation：明细问法不得误伤 ===")
MUST_NOT = ["最近5条设备维护记录", "库存最低的前5个产品", "前10名员工",
            "列出所有产品", "查询dim_product表", "今天天气怎么样"]
for q in MUST_NOT:
    check("不判为聚合: %s" % q, M._needs_aggregation(q) is False)

# ── 3) 退化输出守卫 ────────────────────────────────────────────────────
print("=== 3. SELECT * / 表名.* 守卫 ===")
BARE = [
    ("单位产品成本是多少", "SELECT dim_product.* FROM dim_product LIMIT 20"),
    ("单位产品成本是多少", "SELECT * FROM dim_product LIMIT 20"),
    ("设备利用率是多少",
     "SELECT eqp_downtime_record.* FROM eqp_downtime_record LIMIT 20"),
    ("产量是多少", "SELECT mes_process_output.* FROM mes_process_output LIMIT 20"),
    ('查一下停机', 'SELECT "e".* FROM eqp_downtime_record "e" LIMIT 20'),
]
for q, sql in BARE:
    check("拦下退化明细: %s" % sql[:44], bool(M._output_quality_reason(q, sql)))

print("=== 3b. 明细意图问句必须豁免 SELECT * ===")
DETAIL_OK = [
    ("最近5条设备维护记录", "SELECT * FROM eqp_downtime_record ORDER BY start_time DESC LIMIT 5"),
    ("列出所有产品", "SELECT * FROM dim_product"),
    ("最近10条停机记录", "SELECT * FROM eqp_downtime_record ORDER BY start_time DESC LIMIT 10"),
    ("查看设备停机明细", "SELECT * FROM eqp_downtime_record"),
    ("所有产品的库存情况", "SELECT * FROM inv_inventory_snapshot"),
    ("逐条列出工单", "SELECT * FROM mes_work_order"),
]
for q, sql in DETAIL_OK:
    check("豁免明细: %s" % q, M._output_quality_reason(q, sql) == "",
          M._output_quality_reason(q, sql)[:60])

print("=== 3c. 合法聚合 SQL 不得误伤 ===")
GOOD = [
    ("各产线的产量",
     "SELECT d.line_name, SUM(f.good_qty) FROM mes_process_output f "
     "JOIN dim_production_line d ON d.line_id=f.line_id GROUP BY d.line_name"),
    ("综合设备停机率",
     'SELECT ROUND(SUM(d.downtime_minutes)*100.0/NULLIF((SELECT COUNT(*) FROM dim_equipment)*1440,0),2) '
     'AS "停机率" FROM eqp_downtime_record d'),
    ("库存最低的前5个产品",
     "SELECT product_id, available_qty FROM inv_inventory_snapshot ORDER BY available_qty ASC LIMIT 5"),
    ("缺陷数排行", "SELECT process_name, COUNT(*) AS c FROM qms_defect_detail "
                   "GROUP BY process_name ORDER BY c DESC"),
]
for q, sql in GOOD:
    check("不误伤合法 SQL: %s" % q, M._output_quality_reason(q, sql) == "",
          M._output_quality_reason(q, sql)[:60])

print("=== 3d. 正则精确性（不得把 count(*) / max_qty 误判） ===")
for s, want in [("SELECT * FROM t", True), ("select   *  from t", True),
                ("SELECT t.* FROM t", True), ("SELECT a.t.* FROM a", True),
                ('SELECT "x".* FROM t', True), ("SELECT count(*) FROM t", False),
                ("SELECT a, b FROM t", False), ("SELECT max_qty FROM t", False)]:
    check("正则 %r -> %s" % (s, want), bool(M._SQL_SELECTSTAR_RE.search(s)) is want)

# ── 4) 【执行说明】拼接幂等 ────────────────────────────────────────────
print("=== 4. _append_fix_notes 幂等与正确性 ===")


class _Fake:
    _fix_notes = ["口径未注册，结果为估算。", "时间锚点已按最新月份重定。"]
    _metric_alt_hint = None


fn = M.LLMService._append_fix_notes
f = _Fake()
t1 = fn(f, "综合设备停机率为 0.50%。")
check("拼出【执行说明】", "【执行说明】" in t1)
check("不产生「。。」", "。。" not in t1)
check("幂等（重复调用不再追加）", fn(f, t1) == t1)
check("保留全部 note", "口径未注册" in t1 and "重新" not in t1 and "锚点已按最新月份重定" in t1)

f2 = _Fake()
f2._fix_notes = []
check("无 note 时不改文本", fn(f2, "正常答案") == "正常答案")

# ── 4b. 「率」类必须真做除法（2026-10-05 P0）─────────────────────────────
# 现象：问「报废率是多少」→ `SUM(defect_qty) AS "报废率"` 返回 91,737（那是缺陷**计数**）。
# 提示词里**早已**写了「率类必须真的做除法」且实测确认注入，但模型没照做；
# 而守卫只查 SQL 形态（有 SUM、有 ORDER BY ⇒ "合规"）⇒ 整条放行。
# ⇒ 提示词是软约束，只有确定性守卫才是硬约束。
print("=== 4b. 率类必须真做除法 ===")
RATIO_BAD = [
    ("报废率是多少", 'SELECT SUM(defect_qty) AS "报废率" FROM mes_process_output'),
    ("废品率是多少", 'SELECT SUM(defect_qty) AS "废品率" FROM mes_process_output'),
    ("准时交付率是多少", "SELECT COUNT(*) AS 准时交付率 FROM mes_work_order"),
    ("OEE 是多少", "SELECT SUM(downtime_minutes) AS OEE FROM eqp_downtime_record"),
]
for q, sql in RATIO_BAD:
    check("拦下未做除法的率类: %s" % q, bool(M._output_quality_reason(q, sql)))

RATIO_GOOD = [
    ("良率是多少", 'SELECT ROUND(SUM(good_qty)*100.0/NULLIF(SUM(input_qty),0),2) AS "良率" '
                   'FROM mes_process_output'),
    ("报废率是多少", 'SELECT ROUND(SUM(scrap_qty)*100.0/NULLIF(SUM(input_qty),0),2) AS "报废率" '
                     'FROM mes_process_output'),
    ("库存占比是多少", 'SELECT product_id, SUM(available_qty)*100.0/'
                       '(SELECT SUM(available_qty) FROM inv_inventory_snapshot) AS "占比" '
                       'FROM inv_inventory_snapshot GROUP BY product_id'),
]
for q, sql in RATIO_GOOD:
    check("放行真做除法的率类: %s" % q, M._output_quality_reason(q, sql) == "")

RATIO_EXEMPT = [
    ("各产线的产量", "SELECT line_id, SUM(input_qty) FROM mes_process_output GROUP BY line_id"),
    ("占比最大的前3个产品", 'SELECT product_id, SUM(available_qty) AS q '
                            'FROM inv_inventory_snapshot GROUP BY product_id ORDER BY q DESC LIMIT 3'),
    ("不良率最高的产线", 'SELECT line_name, SUM(defect_qty) AS c FROM mes_process_output '
                         'GROUP BY line_name ORDER BY c DESC LIMIT 1'),
    ("报废数是多少", 'SELECT SUM(defect_qty) AS "报废数" FROM mes_process_output'),
    ("最近5条设备维护记录", "SELECT * FROM eqp_downtime_record ORDER BY start_time DESC LIMIT 5"),
]
for q, sql in RATIO_EXEMPT:
    check("率类守卫不误伤: %s" % q, M._output_quality_reason(q, sql) == "",
          M._output_quality_reason(q, sql)[:60])

# ── 5) 停机率/稼动率/产能利用率已注册为确定性口径 ────────────────────────
print("=== 5. 率类指标走确定性路径（不依赖 LLM） ===")
try:
    from agent.metric_registry import render_exec_sql_metric
    for q, want in (("综合设备停机率", "停机率"), ("稼动率", "稼动率"),
                    ("产能利用率是多少", "产能利用率"),
                    ("各产线的产能利用率", "产能利用率"),
                    ("各设备类型的停机率", "设备类型")):
        r = render_exec_sql_metric(q)
        check("render_exec_sql_metric 命中: %s" % q, bool(r))
        if r:
            check("  列含「%s」" % want, want in (r.get("sql") or ""))
            # 确定性口径自己就必须是真除法
            check("  口径内含除法: %s" % q, "/" in (r.get("sql") or ""))
except Exception as e:  # pragma: no cover
    check("render_exec_sql_metric 可用", False, repr(e))

# ── 6) 枚举值大小写归一（2026-10-05）────────────────────────────────────
# 现象：LLM 生成 `WHERE order_status IN ('COMPLETED','IN_PROGRESS')`，
# 而库里存的是小写 completed / in_progress → 0 行 → 洞察只能说「数据缺失」。
# 这既不是拒绝也不是正确结果，是**静默算不出数**（最难查的一类）。
print("=== 6. 枚举值大小写归一（IN 列表） ===")
_ci = M._bird_case_insensitive
S1 = "SELECT COUNT(*) FROM mes_work_order WHERE order_status IN ('COMPLETED','IN_PROGRESS')"
O1 = _ci(S1)
check("IN 列表被改为 lower(col) IN (小写值)", "lower(order_status)" in O1, O1)
check("字面量已小写化", "'completed'" in O1 and "'in_progress'" in O1, O1)

# 语法健全性：lower( 内不得是关键字；不得出现 "col lower(" 的插入式错误
for sql_in, must_change in [
        ("SELECT * FROM t WHERE status IN ('A','B')", True),
        ("SELECT * FROM t WHERE x = 'A' AND y IN ('P','Q')", True),
        ("SELECT * FROM t WHERE a NOT IN ('A','B')", False),      # 取反语义，不改
        ("SELECT * FROM t WHERE s IN (SELECT k FROM u)", False),  # 子查询不改
]:
    out = _ci(sql_in)
    check("IN 改写符合预期: %s" % sql_in[:40], (out != sql_in) == must_change,
          "-> %s" % out)
    for mm in re.finditer(r"lower\(\s*([^)]*)\)", out):
        tok = mm.group(1).strip().strip('"`[]')
        check("  lower() 内不是关键字: %s" % tok[:20], tok.lower() not in M._S_RESERVED)
    for mm in re.finditer(r"(\S+)\s+lower\s*\(", out):
        prev = mm.group(1).strip().strip('"`[],()')
        check("  无插入式 lower 错误: %s" % sql_in[:30],
              (not prev) or prev.lower() in M._S_RESERVED, "prev=%s" % prev)

# 不得误伤非字符串比较
for s in ("SELECT * FROM t WHERE id = 123",
          "SELECT * FROM t WHERE a > 'x'",
          "SELECT * FROM t WHERE a <= 'x'",
          "SELECT * FROM t WHERE a != 'x'"):
    check("不改写非等值比较: %s" % s[-14:], _ci(s) == s)

print("")
print("=" * 60)
if FAILED:
    print("失败 %d 项：" % len(FAILED))
    for x in FAILED:
        print("  - " + x)
    sys.exit(1)
print("全部通过")
sys.exit(0)
