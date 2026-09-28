# -*- coding: utf-8 -*-
"""弹窗链路后端逻辑系统性测试：infer_query_analysis / fill_param_placeholders / resolve_metric_intent"""
import sys, json, re
sys.path.insert(0, '.')

# 固定测试基准库（postgres 演示库），不依赖 .env 当前连接
import database
database.switch_database({"db_type": "postgresql", "host": "localhost", "port": 5432,
                          "name": "postgres", "user": "postgres", "password": "123456"})

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

import agent.llm_service as m
from langchain_core.messages import AIMessageChunk
import agent.metric_registry as reg

# 测试固定走 stream 快路径：本文件的 mock LLM 只实现 stream（真实部署切 zhipu/glm 后走
# invoke 慢路径由另一组 GLM 专项验证覆盖；此处不改则 LLM_CONFIG 指向 glm-4.7 会触发
# invoke → mock 无 invoke → 异常被吞全量失败）
m.LLM_CONFIG["model"] = "mock-fast-llm"
m.LLM_CONFIG["base_url"] = "https://mock.local/v1"  # 脱离 zhipu host 匹配，强制 stream 快路径

# ═══ A. infer_query_analysis 边界 ═══
print("═══ A. infer_query_analysis ═══")

# A1 LLM 异常 → None
m._make_llm = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("LLM down"))
check("A1 LLM 异常降级", m.infer_query_analysis("q", "s", [{"table_name": "t"}]) is None)

# A2 流式返回空 → None
def empty_llm(*a, **k):
    class L:
        def stream(self, msgs): return iter([])
    return L()
m._make_llm = empty_llm
check("A2 空响应 → None", m.infer_query_analysis("q", "s", [{"table_name": "t"}]) is None)

# A3 非法 JSON → None
def bad_json(*a, **k):
    class L:
        def stream(self, msgs):
            yield AIMessageChunk(content="{not json")
    return L()
m._make_llm = bad_json
check("A3 非法 JSON → None", m.infer_query_analysis("q", "s", []) is None)

# A4 json 数组（非对象）→ None
def list_json(*a, **k):
    class L:
        def stream(self, msgs):
            yield AIMessageChunk(content="[1,2,3]")
    return L()
m._make_llm = list_json
check("A4 JSON 数组 → None", m.infer_query_analysis("q", "s", []) is None)

# ── A5~A9 统一改用 MQL 契约 mock ────────────────────────────
# 2026-09-02 红线收敛后 infer_query_analysis 不再接受 sql_draft：LLM 只产出结构化口径
# （metrics/dimensions/filters/time_range），SQL 由 _compile_sql_draft 编译。
# 旧用例注入 sql_draft 会因「编译不出聚合」而返回 None，导致 A5 假绿 / A6b 崩溃，
# 故统一改为 MQL 输入，并把防御点下沉到编译器层验证。
_MQL_TBL = [{"table_name": "mes_process_output"}, {"table_name": "dim_process"}]

def _mk_llm(payload):
    def f(*a, **k):
        class L:
            def stream(self, msgs):
                yield AIMessageChunk(content=json.dumps(payload))
        return L()
    return f

def mk_mql(**over):
    p = {"understanding": "u",
         "metrics": [{"name": "产量", "agg": "SUM", "column": "good_qty"}],
         "dimensions": ["process_name"], "filters": [], "time_range": "",
         "confidence": 0.5, "risks": []}
    p.update(over)
    return _mk_llm(p)

# A5 注入列名必须被列白名单剔除（防注入已下沉到编译层：编译器只拼白名单列）
# v2 行为：指标列被剔除后可能降级明细模式，故断言「注入片段绝不出现在 SQL 里」+「无可用列→None」
m._make_llm = mk_mql(metrics=[{"name": "x", "agg": "SUM",
                               "column": "good_qty); DELETE FROM mes_process_output; --"}],
                      dimensions=[])
_r_inj = m.infer_query_analysis("q", "s", _MQL_TBL)
_inj_sql = ((_r_inj or {}).get("sql_draft") or "")
check("A5a 注入列名被剔除（SQL 无注入片段）",
      "DELETE" not in _inj_sql.upper() and "--" not in _inj_sql,
      f"sql={_inj_sql[:60]}")
check("A5b 注入列+无维度 → 无可用列降级 None", _r_inj is None)

# A6 置信度 0 / 越界 5 / 负数 → 被夹到 [0,1]
for _c, _want, _name in ((0, 0.0, "A6a 置信度 0 保留"),
                         (5, 1.0, "A6b 置信度 5 → 1.0"),
                         (-1, 0.0, "A6c 置信度 -1 → 0.0")):
    m._make_llm = mk_mql(confidence=_c)
    _r = m.infer_query_analysis("q", "s", _MQL_TBL)
    check(_name, _r is not None and _r["confidence"] == _want)

# A7a metrics 与 dimensions 全空 → 编译不出任何 SQL → None（宁可降级，不硬凑）
# v2 行为：有 dimensions 时会编明细 SQL（这是特性），两者全空才应降级
m._make_llm = mk_mql(metrics=[], dimensions=[])
check("A7a 无 metrics 且无 dimensions → None（不硬凑 SQL）",
      m.infer_query_analysis("q", "s", _MQL_TBL) is None)

# A7c v2 明细模式：无 metrics 但有 dimensions → 编出明细 SELECT（非聚合查询）
m._make_llm = mk_mql(metrics=[], dimensions=["process_name"],
                     filters=[{"field": "department", "op": "=", "value": "品保部"}])
_r_detail = m.infer_query_analysis("q", "s", _MQL_TBL)
_dsql = ((_r_detail or {}).get("sql_draft") or "")
check("A7c 明细模式编译", _r_detail is not None and _dsql.upper().startswith("SELECT")
      and "department" in _dsql and "GROUP BY" not in _dsql.upper(),
      f"sql={_dsql[:80]}")

# A7d v2 distinct 计数 + time_range 落 WHERE（2026-09-06 契约修订：聚合列与时间列同主表，
# 不再构造「聚合列在维表 + 时间列在事实表」的跨表形态——该形态被事实表-维表角色规则挡下）
m._make_llm = mk_mql(metrics=[{"name": "产出工序数", "agg": "COUNT", "column": "process_id", "distinct": True}],
                      dimensions=[],
                      time_range={"column": "stat_date", "start": "2026-01-01", "end": "2026-08-31"})
_r_v2 = m.infer_query_analysis("q", "s", _MQL_TBL)
_vsql = ((_r_v2 or {}).get("sql_draft") or "")
check("A7d distinct + time_range 编译",
      "COUNT(DISTINCT mes_process_output.process_id)" in _vsql
      and "stat_date >= '2026-01-01'" in _vsql,
      f"sql={_vsql[:110]}")

# A7e v2 IN 筛选（多可选值；2026-09-06 契约：筛选列与维度列同表，避免跨表筛选被角色规则剔除）
m._make_llm = mk_mql(metrics=[], dimensions=["process_name"],
                      filters=[{"field": "process_name", "op": "IN", "value": ["车削", "装配"]}])
_r_in = m.infer_query_analysis("q", "s", _MQL_TBL)
_isql = ((_r_in or {}).get("sql_draft") or "")
check("A7e IN 筛选编译", "process_name IN ('车削', '装配')" in _isql, f"sql={_isql[:100]}")

# A7f JOIN 放大回归（2026-09-06 用户实测 bug：mes_work_order 统计被 mes_process_output
# 1:N JOIN 放大 8 倍）——主表聚合引用事实次表列时必须剔除该列且【禁止事实表间 JOIN】
_MQL_FACT = [{"table_name": "mes_work_order"}, {"table_name": "mes_process_output"}]
m._make_llm = mk_mql(metrics=[{"name": "计划数量", "agg": "SUM", "column": "plan_qty"}],
                      dimensions=["shift_code"])  # shift_code 只在事实次表 → 应被剔除
_r_amp = m.infer_query_analysis("q", "s", _MQL_FACT)
_asql = ((_r_amp or {}).get("sql_draft") or "")
check("A7f 事实表间引用剔除（防 JOIN 放大）",
      "FROM mes_work_order" in _asql and "JOIN mes_process_output" not in _asql,
      f"sql={_asql[:100]}")
# 维表维度则允许 JOIN（多对一安全）：mes_process_output + dim_process（同 _MQL_TBL）
m._make_llm = mk_mql(metrics=[{"name": "产出量", "agg": "SUM", "column": "good_qty"}],
                      dimensions=["process_name"])
_r_dim = m.infer_query_analysis("q", "s", _MQL_TBL)
_dsql2 = ((_r_dim or {}).get("sql_draft") or "")
check("A7f 维表维度 JOIN 保留",
      "JOIN dim_process" in _dsql2 and "GROUP BY" in _dsql2.upper(),
      f"sql={_dsql2[:100]}")

# A7b 稀疏 JSON（无 filters/time_range 键）→ 兜底为空，不崩
m._make_llm = _mk_llm({"understanding": "u",
                       "metrics": [{"name": "产量", "agg": "SUM", "column": "good_qty"}]})
r = m.infer_query_analysis("q", "s", _MQL_TBL)
check("A7b 稀疏 JSON 兜底", r is not None and r["filters"] == [] and r["time_range"] == "")

# A8 开关关闭
m._ANALYSIS_CONFIRM_ENABLED = False
check("A8 总开关关闭 → None", m.infer_query_analysis("q", "s", []) is None)
m._ANALYSIS_CONFIRM_ENABLED = True

# A9 无候选表 → 编译不出 SQL → None（不崩即正确）
m._make_llm = mk_mql()
check("A9 无候选表 → None（不崩）", m.infer_query_analysis("q", "", []) is None)

# ═══ B. fill_param_placeholders ═══
print("═══ B. fill_param_placeholders ═══")
from db.executor import fill_param_placeholders
import database

# B1 无占位符原样
check("B1 无 %s 原样", fill_param_placeholders("SELECT 1") == "SELECT 1")

# B2 单 BETWEEN 填充（真实库）
r = fill_param_placeholders("SELECT * FROM mes_process_output WHERE stat_date BETWEEN %s AND %s LIMIT 5")
check("B2 单 BETWEEN 填充", "%s" not in r and "'" in r)

# B3 多 BETWEEN 交替填充
r = fill_param_placeholders("SELECT * FROM test_orders WHERE order_date BETWEEN %s AND %s AND id BETWEEN %s AND %s LIMIT 5")
check("B3 多占位符交替", r.count("'") >= 4 and "%s" not in r)

# B4 引用不存在表 → 原样返回（不崩）
r = fill_param_placeholders("SELECT * FROM not_exist_tbl WHERE d BETWEEN %s AND %s")
check("B4 表不存在不崩", isinstance(r, str))

# B5 填充后 SQL 可真实执行（复杂 JOIN 场景）
complex_sql = """WITH inv_agg AS (SELECT p.product_code, AVG(inv.available_qty + COALESCE(inv.frozen_qty,0)) AS avg_qty FROM inv_inventory_snapshot inv JOIN dim_product p ON inv.product_id = p.product_id WHERE inv.snapshot_date BETWEEN %s AND %s GROUP BY p.product_code), sales_agg AS (SELECT LOWER(TRIM(product_name)) AS jn, SUM(quantity) AS sq FROM test_orders WHERE status='completed' AND order_date BETWEEN %s AND %s GROUP BY LOWER(TRIM(product_name))) SELECT i.product_code, ROUND(i.avg_qty * 30.0 / NULLIF(s.sq,0), 2) AS turnover_days FROM inv_agg i LEFT JOIN sales_agg s ON LOWER(i.product_code) = s.jn ORDER BY turnover_days DESC NULLS LAST LIMIT 10"""
filled = fill_param_placeholders(complex_sql)
from db.executor import execute_sql
res = execute_sql(filled)
check("B5 复杂 JOIN 填充后可执行", res.get("success"), f"rows={res.get('row_count')}")

# ═══ C. resolve_metric_intent 模糊/边界 ═══
print("═══ C. resolve_metric_intent 模糊匹配 ═══")

# C1 空/空白/None
check("C1a 空串 → skip", reg.resolve_metric_intent("")["status"] == "skip")
check("C1b 空白串 → skip", reg.resolve_metric_intent("   ")["status"] == "skip")
check("C1c None → skip", reg.resolve_metric_intent(None)["status"] == "skip")

# C2 OEE：2026-09-04 口径重构已将其下线（与良率同公式且缺字段，属误导性口径）
# → 现期望为不命中。此处作回归保护，防止被误加回注册表。
r = reg.resolve_metric_intent("设备综合效率oee是多少")
check("C2a OEE 已下线 → no_hit", r["status"] == "no_hit", f"status={r['status']}")
r = reg.resolve_metric_intent("OEE")
check("C2b 大写 OEE 不误命中", r["status"] in ("no_hit", "skip"), f"status={r['status']}")

# C3 特殊字符
r = reg.resolve_metric_intent("【产量】是多少？？？")
check("C3 特殊字符不崩", r["status"] in ("hit", "no_hit", "skip", "ambiguous"), f"status={r['status']}")

# C4 部分关键字（"不良"）
r = reg.resolve_metric_intent("看看不良情况")
check("C4 部分关键字不崩", r["status"] in ("hit", "no_hit", "skip"), f"status={r['status']}")

# C5 拼音首字母（不命中不崩）
r = reg.resolve_metric_intent("cl zl")
check("C5 拼音首字母不崩", r["status"] in ("hit", "no_hit", "skip"), f"status={r['status']}")

# C6 极长输入
r = reg.resolve_metric_intent("产量" * 200)
check("C6 超长输入不崩", r["status"] in ("hit", "no_hit", "skip", "ambiguous"))

# ═══ D. execute_confirm 权限/表存在性 ═══
print("═══ D. execute_confirm 权限校验 ═══")
from agent.llm_service import _extract_sql_tables
check("D1 sqlglot 解析 CTE 表", "mes_process_output" in _extract_sql_tables("WITH x AS (SELECT 1) SELECT * FROM mes_process_output LIMIT 1"))
check("D2 解析多表", {"inv_inventory_snapshot", "dim_product"}.issubset(_extract_sql_tables("SELECT * FROM inv_inventory_snapshot a JOIN dim_product b ON a.product_id=b.product_id LIMIT 1")))
check("D3 空 SQL → 空集", _extract_sql_tables("") == set())
check("D4 无表 SQL → 空集", _extract_sql_tables("SELECT 1") == set())

print(f"\n═══ 结果: PASS={PASS} FAIL={FAIL} ═══")
sys.exit(1 if FAIL else 0)
