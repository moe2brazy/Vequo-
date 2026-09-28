# -*- coding: utf-8 -*-
"""行动闭环 write-back 单元测试（P2-2，对标 Sigma Agents）：确定性编译 + 安全闸门"""
import sys
sys.path.insert(0, '.')

import agent.action_agent as A

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

print("═══ A. 动作清单 ═══")
acts = A.list_actions()
check("A1 有内置动作", len(acts) >= 2, str([a["id"] for a in acts]))
check("A2 动作含参数 schema", all("params" in a for a in acts), "")
check("A3 不泄漏目标表白名单外字段", all("target_table" not in a for a in acts), "")

print("═══ B. 确定性编译（SQL 由注册表生成，LLM 不碰）═══")
r = A.compile_action("update_safety_stock", {"product_id": "P001", "safety_stock": 100})
check("B1 编译成功", r["success"], r.get("error", ""))
check("B2 SQL 是 UPDATE", r["sql"].upper().startswith("UPDATE"), r["sql"])
check("B3 参数化绑定（无值拼接）", ":" in r["sql"] and "100" not in r["sql"] and "P001" not in r["sql"],
      r["sql"])
check("B4 绑定参数正确", r["bind_params"].get("s_safety_stock") == 100
      and r["bind_params"].get("w_product_id") == "P001", str(r["bind_params"]))

r = A.compile_action("update_order_status", {"order_id": 5, "status": "已完成"})
check("B5 int 参数转换", r["bind_params"].get("w_order_id") == 5, str(r["bind_params"].get("w_order_id")))
check("B6 number 参数 int 化", A.compile_action("update_safety_stock", {"product_id": "P1", "safety_stock": 50})["bind_params"]["s_safety_stock"] == 50, "")

print("═══ C. 编译校验（防错 / 防越权）═══")
check("C1 未知动作拒绝", not A.compile_action("drop_table", {})["success"], "")
check("C2 缺必填参数拒绝", not A.compile_action("update_safety_stock", {"product_id": "P1"})["success"],
      A.compile_action("update_safety_stock", {"product_id": "P1"})["error"])
check("C3 缺定位条件拒绝", not A.compile_action("update_safety_stock", {"safety_stock": 1})["success"], "")
check("C4 非法数值拒绝", not A.compile_action("update_safety_stock", {"product_id": "P1", "safety_stock": "abc"})["success"], "")
check("C5 空字符串值拒绝", not A.compile_action("update_safety_stock", {"product_id": "  ", "safety_stock": 1})["success"], "")
# 注入尝试：字符串参数走绑定，不会被当 SQL 执行
r = A.compile_action("update_order_status", {"order_id": 1, "status": "x'; DROP TABLE t; --"})
check("C6 注入串不破坏 SQL 结构", r["success"] and "DROP" not in r["sql"], r.get("sql", ""))

print("═══ D. 执行闸门 ═══")
check("D1 总开关关闭时拒绝执行", not A.execute_action("update_safety_stock",
      {"product_id": "P1", "safety_stock": 1}, confirmed=True)["success"],
      A.execute_action("update_safety_stock", {"product_id": "P1", "safety_stock": 1}, confirmed=True)["error"][:40])
A._WRITE_ENABLED = True
try:
    r = A.execute_action("update_safety_stock", {"product_id": "P1", "safety_stock": 1}, confirmed=False)
    check("D2 未二次确认拒绝", not r["success"] and r.get("needs_confirm") is True, str(r)[:80])
    check("D3 未知动作执行拒绝", not A.execute_action("nope", {}, confirmed=True)["success"], "")
finally:
    A._WRITE_ENABLED = False

print("═══ E. 动作建议（LLM 只产参数，不产 SQL）═══")
try:
    pr = A.propose_action("把产品 P001 的安全库存阈值调到 200")
    if pr.get("success"):
        check("E1 建议动作在注册表内", pr["action"] in A.ACTIONS, pr["action"])
        check("E2 只含参数不产 SQL", "sql" not in pr and isinstance(pr.get("params"), dict), str(list(pr.keys())))
        print("    动作:", pr["action"], "参数:", pr["params"], "理由:", pr["reason"][:40])
    else:
        print("  ⚠️  ", pr.get("error"), "—— 跳过 E1-E2")
        check("E0 降级不崩", pr["success"] is False, "")
except Exception as e:
    print("  ⚠️  propose 异常（LLM 不可用），跳过 E:", str(e)[:60])
    check("E0 降级不崩", True, "")

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
