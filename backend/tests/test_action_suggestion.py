# -*- coding: utf-8 -*-
"""行动闭环（P2-2 激活，对标 Fabric operations agents）：洞察 → 动作推荐 测试。

覆盖 suggest_action_for_insight 的确定性规则：库存/订单异常 → 对应动作，
其他表不推荐；attach_action_suggestions 批量附加不污染原对象。
"""
from __future__ import annotations

import sys
import os

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


print("═══ A. 洞察 → 动作推荐（确定性规则） ═══")
from agent.insight_scan import suggest_action_for_insight

r = suggest_action_for_insight({"table": "inv_inventory_snapshot", "message": "P010 可用库存 800 vs 均值 280"})
check("A1 库存异常 → update_safety_stock", r and r["action"] == "update_safety_stock", str(r))
check("A2 带理由", r and bool(r.get("reason")), "")
check("A3 带目标表", r and r["target_table"] == "inv_inventory_snapshot", "")

r2 = suggest_action_for_insight({"table": "test_orders", "message": "订单状态异常"})
check("B1 订单异常 → update_order_status", r2 and r2["action"] == "update_order_status", str(r2))

r3 = suggest_action_for_insight({"table": "mes_process_output", "message": "L01 产量异常"})
check("C1 产量异常 → 无建议", r3 is None, str(r3))

r4 = suggest_action_for_insight(None)
check("C2 非 dict → None", r4 is None, "")
r5 = suggest_action_for_insight({"table": "", "message": ""})
check("C3 空输入 → None", r5 is None, "")

print("═══ D. 批量附加（不污染原对象） ═══")
from agent.insight_scan import attach_action_suggestions

ins = [{"table": "inv_inventory_snapshot", "message": "库存异常"},
       {"table": "mes_process_output", "message": "产量异常"}]
out = attach_action_suggestions(ins)
check("D1 库存洞察带建议", out[0].get("action_suggestion") is not None, str(out[0].keys()))
check("D2 产量洞察无建议", out[1].get("action_suggestion") is None, "")
check("D3 原对象未被修改", ins[0].get("action_suggestion") is None, str(ins[0].keys()))
check("D4 返回新列表", out is not ins, "")

print("═══ E. 动作清单含参数 schema（供前端表单渲染） ═══")
from agent.action_agent import list_actions
acts = list_actions()
check("E1 有动作", len(acts) >= 2, str(len(acts)))
check("E2 参数带 label/type", all("label" in p and "type" in p for a in acts for p in a["params"].values()),
      str(acts[0]["params"]))

print()
print("=" * 40)
print(f"通过 {_passed} 项，失败 {_failed} 项")
print("=" * 40)
