# -*- coding: utf-8 -*-
"""P1-B 知识自生长闭环（对标 Data Neo「反馈→规则」+ FineBI 经营记忆口径版本）测试。

覆盖：
- 负反馈 → 提炼口径候选（source=feedback_negative，半自动，人工审核闸门）
- 正反馈 → 提炼口径候选（source=feedback_positive）
- 空 SQL 负反馈 → 不提炼
- 指标版本：同名历史（含已停用）→ version+1
- 口径变更 → 审计留痕（旧→新）
"""
from __future__ import annotations

import sys
import os
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}  {detail}")


print("═══ A. 负反馈 → 提炼口径候选（半自动） ═══")
import main
from main import FeedbackRequest

# 构造负反馈请求，打桩外部依赖，只验证 suggest_from_query 被正确调用
with mock.patch("main.audit"), \
     mock.patch("agent.metric_miner.suggest_from_query", return_value=1) as m_sug, \
     mock.patch("agent.llm_service._semantic_remove", return_value=0), \
     mock.patch("cache_store.cache_get", return_value=None):
    req = FeedbackRequest(query="各仓库安全库存", sql="SELECT wh_code, SUM(safety_stock) FROM inv_inventory_snapshot GROUP BY wh_code",
                          correct=False, table_name="inv_inventory_snapshot")
    r = main.feedback_api(req)
check("A1 负反馈返回成功", r.get("success") is True, str(r))
check("A2 调用 suggest_from_query", m_sug.called, "")
check("A3 source=feedback_negative", m_sug.call_args and m_sug.call_args.kwargs.get("source") == "feedback_negative",
      str(m_sug.call_args))
check("A4 query 透传", m_sug.call_args and m_sug.call_args.args[0] == "各仓库安全库存", str(m_sug.call_args))

print("═══ B. 负反馈空 SQL → 不提炼 ═══")
with mock.patch("main.audit"), \
     mock.patch("agent.metric_miner.suggest_from_query", return_value=0) as m_sug2, \
     mock.patch("agent.llm_service._semantic_remove", return_value=0), \
     mock.patch("cache_store.cache_get", return_value=None):
    req2 = FeedbackRequest(query="查不了", sql="", correct=False)
    main.feedback_api(req2)
check("B1 空 SQL 不调 suggest", not m_sug2.called, str(m_sug2.call_count))

print("═══ C. 正反馈 → 提炼口径候选 ═══")
with mock.patch("agent.memory.get_memory") as m_mem, \
     mock.patch("agent.metric_miner.suggest_from_query", return_value=1) as m_sug3, \
     mock.patch("agent.llm_service._semantic_store"), \
     mock.patch("security.enforcer.acl_fingerprint", return_value="fp"):
    m_mem.return_value.add_sql.return_value = 1
    req3 = FeedbackRequest(query="各仓库安全库存", sql="SELECT wh_code, SUM(safety_stock) AS x FROM inv_inventory_snapshot GROUP BY wh_code",
                           correct=True, table_name="inv_inventory_snapshot")
    r3 = main.feedback_api(req3)
check("C1 正反馈返回成功", r3.get("success") is True, str(r3))
check("C2 调用 suggest_from_query", m_sug3.called, "")
check("C3 source=feedback_positive", m_sug3.call_args.kwargs.get("source") == "feedback_positive", str(m_sug3.call_args))

print("═══ D. 指标版本：同名历史 → version+1 ═══")
import agent.metric_registry as MR

old_history = [
    {"name": "产量", "version": 2, "sql_expression": "SUM(old_qty)", "valid_to": "2026-01-01"},
    {"name": "产量", "version": 1, "sql_expression": "SUM(orig_qty)", "valid_to": "2025-06-01"},
    {"name": "良率", "sql_expression": "SUM(g)", "valid_to": None},  # 无 version → 默认 1
]
with mock.patch.object(MR, "get_all_metrics", return_value=old_history), \
     mock.patch.object(MR, "_load_user_metrics", return_value=[]), \
     mock.patch.object(MR, "save_user_metrics", return_value=None), \
     mock.patch("agent.metric_memory.get_metric_memory"), \
     mock.patch("auth.audit") as m_audit:
    m = MR.create_user_metric({"name": "产量", "sql_expression": "SUM(new_qty)"})
    new_metric = m["metric"]
check("D1 version = 历史 max+1 = 3", new_metric.get("version") == 3, str(new_metric.get("version")))
check("D2 口径变更触发审计", m_audit.called, "")
check("D3 审计含新旧表达式", m_audit.call_args and "new_qty" in str(m_audit.call_args),
      str(m_audit.call_args))

print("═══ E. 首次注册 → version=1、无审计 ═══")
with mock.patch.object(MR, "get_all_metrics", return_value=[{"name": "别的指标"}] ), \
     mock.patch.object(MR, "_load_user_metrics", return_value=[]), \
     mock.patch.object(MR, "save_user_metrics", return_value=None), \
     mock.patch("agent.metric_memory.get_metric_memory"), \
     mock.patch("auth.audit") as m_audit2:
    m2 = MR.create_user_metric({"name": "新指标", "sql_expression": "SUM(x)"})
check("E1 首次 version=1", m2["metric"].get("version") == 1, str(m2["metric"].get("version")))
check("E2 无历史不审计", not m_audit2.called, str(m_audit2.call_count))

print("═══ F. 同名同义仍拒绝 ═══")
with mock.patch.object(MR, "get_all_metrics", return_value=[
        {"name": "产量", "version": 1, "sql_expression": "SUM(good_qty)", "valid_to": None}]):
    try:
        MR.create_user_metric({"name": "产量", "sql_expression": "SUM(good_qty)"})
        check("F1 同名同义拒绝", False, "未抛异常")
    except ValueError as e:
        check("F1 同名同义拒绝", "口径一致" in str(e), str(e)[:60])

print()
print("=" * 40)
print(f"通过 {_passed} 项，失败 {_failed} 项")
print("=" * 40)
