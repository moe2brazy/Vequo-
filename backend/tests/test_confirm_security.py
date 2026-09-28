# -*- coding: utf-8 -*-
"""第二轮扩展回归：RLS 权限改写 / 语义缓存优先于弹窗 / 占位符性能优化"""
import sys, json
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

import database
from security.enforcer import AclContext, rewrite_sql
from db.executor import execute_sql, fill_param_placeholders

# ═══ E. RLS 行级/列级权限改写（SE-1 修复验证）═══
print("═══ E. rewrite_sql 权限改写 ═══")

# E1 行级过滤：row_filters 注入 AND 条件
acl = AclContext(
    username="u1",
    allowed_tables={"mes_process_output"},
    row_filters={"mes_process_output": "line_id = 'L01'"},
)
sql = "SELECT line_id, SUM(good_qty) AS qty FROM mes_process_output GROUP BY line_id ORDER BY qty DESC LIMIT 10"
eff, err, applied = rewrite_sql(sql, acl)
check("E1 行级过滤注入", err == "" and "line_id = 'l01'" in eff.lower(), f"eff={eff[:80]}")

# E2 列级拒绝：SELECT 投影含无权列 → fail-close 拒绝（安全优先，不做剔除）
acl2 = AclContext(
    username="u2",
    allowed_tables={"test_orders"},
    column_denies={"test_orders": {"customer_name"}},
)
sql2 = "SELECT customer_name, status, SUM(amount) AS amt FROM test_orders GROUP BY customer_name, status"
eff2, err2, _ = rewrite_sql(sql2, acl2)
check("E2 列级拒绝 fail-close", err2 != "" and "无权访问字段" in err2, f"err2={err2[:40]}")

# E2b 列级拒绝不误伤有权列：仅含有权列的 SQL 正常
sql2b = "SELECT status, SUM(amount) AS amt FROM test_orders GROUP BY status"
eff2b, err2b, _ = rewrite_sql(sql2b, acl2)
check("E2b 有权列不受影响", err2b == "" and "status" in eff2b)

# E3 superuser 短路（零改写）
acl3 = AclContext(username="admin", superuser=True, row_filters={"mes_process_output": "line_id='L01'"})
eff3, err3, _ = rewrite_sql(sql, acl3)
check("E3 superuser 短路", err3 == "" and eff3.strip() == sql.strip())

# E4 无策略短路（零开销）
acl4 = AclContext(username="u4", allowed_tables=None)
eff4, err4, _ = rewrite_sql(sql, acl4)
check("E4 无策略短路", eff4 == sql and err4 == "")

# E5 改写后 SQL 真实可执行（行级条件）
res = execute_sql(eff)
check("E5 行级改写后执行", res.get("success"), f"rows={res.get('row_count')}")

# E6 行条件列有效性：rewrite 只做文本注入，列存在性由执行层校验（执行失败而非改写失败）
acl5 = AclContext(username="u5", allowed_tables={"mes_process_output"},
                  row_filters={"mes_process_output": "unknown_col = 'x'"})
eff5, err5, _ = rewrite_sql("SELECT * FROM mes_process_output LIMIT 1", acl5)
check("E6 无效行条件：改写成功(文本注入)", err5 == "" and "unknown_col = 'x'" in eff5)
res5 = execute_sql(eff5)
check("E6b 无效列执行失败(执行层拦截)", not res5.get("success"))

# ═══ F. 语义缓存优先于弹窗（LE-4 修复验证：代码级事件流）═══
print("═══ F. run() 语义缓存优先于 no_hit 弹窗 ═══")
import agent.llm_service as m

_TBL = {"table_name": "mes_process_output", "table_alias": "mes_process_output"}

# 构造：no_hit 问题 + 语义缓存命中 → 应走缓存执行，不产生 analysis_confirm 事件
class FakeService(m.LLMService):
    def _semantic_cache_hit(self):
        return {"sql": "SELECT line_id, SUM(good_qty) qty FROM mes_process_output GROUP BY line_id LIMIT 5",
                "matched_tables": [_TBL],
                "schema_context": "SCHEMA", "chart_type": "bar", "similarity": 0.95}

svc = FakeService("库存周转天数是多少", [], acl=AclContext(username="t"))
events = [e["type"] for e in svc.run()]
check("F1 缓存命中不弹窗", "analysis_confirm" not in events, f"events={events[:6]}")
check("F2 缓存命中出 SQL", "sql" in events)

# 未命中 + no_hit（mock 推断成功；用未注册指标问题保证 no_hit）
class FakeService2(m.LLMService):
    def _semantic_cache_hit(self):
        return None
    def _build_analysis_confirm(self):
        return {"understanding": "u", "metrics": [], "dimensions": [], "filters": [],
                "time_range": "", "sql_draft": "SELECT 1 LIMIT 1", "confidence": 0.5, "risks": []}

svc2 = FakeService2("车间温度异常率是多少", [], acl=AclContext(username="t"))
ev2 = [e for e in svc2.run() if e["type"] == "done"]
resp = ev2[0].get("response", {}) if ev2 else {}
# 2026-09-07 产品决策（llm_service.py no_hit 分支注释）：未命中注册口径不再弹窗询问，
# 直接走 LLM 生成 SQL 出结果，结果标注「AI 生成，请核对」；口径登记后重问即命中确定性编译。
# 本断言由「弹窗」同步更新为「不弹窗、直接查询」。
check("F3 未命中+no_hit → 不弹窗直接查询（2026-09-07 产品决策）",
      "analysis_confirm" not in [e["type"] for e in svc2.run()] and resp.get("type") == "data_query",
      f"type={resp.get('type')}")

# ═══ G. 占位符性能优化回归（合并查询）═══
print("═══ G. fill_param_placeholders 回归 ═══")
sql_p = "SELECT * FROM test_orders WHERE order_date BETWEEN %s AND %s LIMIT 5"
filled = fill_param_placeholders(sql_p)
res_p = execute_sql(filled)
check("G1 填充后执行", res_p.get("success") and "%s" not in filled)
# 无占位符原样
check("G2 无占位符原样", fill_param_placeholders("SELECT 1") == "SELECT 1")

print(f"\n═══ 结果: PASS={PASS} FAIL={FAIL} ═══")
sys.exit(1 if FAIL else 0)
