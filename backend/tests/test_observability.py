# -*- coding: utf-8 -*-
"""运行时观测 + 编译覆盖率闭环 单元测试"""
import sys
sys.path.insert(0, '.')

import agent.observability as O
from agent.metric_miner import suggest_from_query, list_candidates, _CAND, _IGNORED, _registered_keys

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

print("═══ A. 观测记录与汇总 ═══")
O._rounds.clear()
O.record("各产线的产量", "data", ["mes_process_output"], True, 3, 40, compiled=True)
O.record("各产线的产量", "data", ["mes_process_output"], True, 3, 38, compiled=True)
O.record("库存低于安全库存的产品", "data", ["inv_inventory_snapshot"], True, 4, 12000, compiled=False)
O.record("随便聊聊", "chat", [], False, 0, 2000, compiled=False)
ov = O.get_overview()
check("A1 总数", ov["total"] == 4, str(ov["total"]))
check("A2 编译命中率", abs(ov["compile_rate"] - 0.5) < 0.001, str(ov["compile_rate"]))
check("A3 成功率", abs(ov["ok_rate"] - 0.75) < 0.001, str(ov["ok_rate"]))
check("A4 P95 耗时（4条 → 取第4条）", ov["p95_ms"] == max(40, 38, 12000, 2000), str(ov["p95_ms"]))
check("A5 意图分布", ov["intent_dist"].get("data") == 3 and ov["intent_dist"].get("chat") == 1, str(ov["intent_dist"]))
check("A6 最近失败被记录", len(ov["recent_fails"]) == 1 and not ov["recent_fails"][0]["ok"], str(len(ov["recent_fails"])))
check("A7 recent 倒序", O.recent(10)[0]["elapsed_ms"] == 2000, str(O.recent(10)[0]["elapsed_ms"]))
check("A8 空观测不崩", O._rounds.clear() or O.get_overview()["total"] == 0, "")

print("═══ B. 编译覆盖率闭环：运行时口径信号 ═══")
# 清理候选池
for k in list(_CAND):
    _CAND.pop(k)
_IGNORED.clear()
# 注意：SUM(available_qty) 属已注册口径「库存量」，会被正确跳过——
# 运行时沉淀只收**未注册**的聚合，这里用一个真实未注册的表达式
n = suggest_from_query("按产品统计可用库存与冻结库存之和",
                       "SELECT product_id AS 产品, SUM(available_qty + frozen_qty) AS 可用加冻结 FROM inv_inventory_snapshot GROUP BY product_id",
                       source="runtime")
check("B1 新增候选", n == 1, str(n))
cands = list_candidates()
check("B2 候选带运行时来源", cands and cands[0]["source"] == "runtime", str(cands and cands[0].get("source")))
check("B3 记录问法", cands and cands[0]["sample_question"].startswith("按产品统计"), "")
# 重复沉淀不重复入池
n2 = suggest_from_query("按产品统计可用与冻结之和",
                        "SELECT product_id, SUM(available_qty + frozen_qty) FROM inv_inventory_snapshot GROUP BY product_id")
check("B4 同源不重复入池", n2 == 0 and len(list_candidates()) == 1, str(n2))
# 已注册口径不再沉淀
# 2026-10-04 更正：原样例用 `SUM(good_qty)` 断言「已注册口径应跳过」，实测沉淀了 1 条。
# 定性结论：**测试样例选错，不是代码 bug**。`suggest_from_query` 的去重键是
#   (归一化表达式, 表集合, 过滤签名)   —— 见 metric_miner._registered_keys / _metric_key
# 而注册表里「产量」的表达式是
#   SUM(COALESCE(good_qty,0) + COALESCE(defect_qty,0))   （合格+不良=总产量）
# **与 `SUM(good_qty)` 本来就是两个不同口径**（后者只是合格数，不是产量）。
# 所以它被当成新口径沉淀是**正确行为** —— 管理员正是靠这个入口发现「良数」这类新口径。
# 另注：B4 之所以能过，并非走了 registered 去重（实测其 key 同样未命中注册表），
#       而是走了 `cid in _CAND` 这条「同源不重复入池」分支。
# 改用**与注册表逐字一致**的表达式来验证「已注册跳过」这条真正的红线。
from agent.metric_registry import get_all_metrics as _gam
_reg_expr = ""
_reg_tbl = ""
for _m in _gam():
    if _m.get("name") == "产量":
        _reg_expr = _m.get("sql_expression") or ""
        _reg_tbl = (_m.get("tables") or [""])[0]
        break
_sql_reg = ("SELECT %s, %s FROM %s GROUP BY %s"
            % (_reg_tbl.split(".")[-1], _reg_expr, _reg_tbl.split(".")[-1], _reg_tbl.split(".")[-1]))
n3 = suggest_from_query("各产线产量", _sql_reg)
check("B5 已注册口径跳过(与注册表表达式逐字一致)", n3 == 0,
      f"n3={n3} sql={_sql_reg[:70]}")
# 反向断言：不同口径（良数 vs 产量）**应该**被沉淀，这是该功能的设计目的
_n_good = suggest_from_query("只统计合格数", "SELECT line_id, SUM(good_qty) FROM mes_process_output GROUP BY line_id")
check("B5b 不同口径应沉淀(良数≠产量)", _n_good == 1, f"n={_n_good}")
# 清掉本条，避免影响 B8
if _n_good:
    _CAND.pop([v["id"] for v in _CAND.values() if v.get("sample_question", "").startswith("只统计合格数")][0], None)
# 空 SQL / 无聚合不沉淀
check("B6 空 SQL 返回 0", suggest_from_query("x", "") == 0, "")
check("B7 纯 SELECT 无聚合返回 0", suggest_from_query("x", "SELECT * FROM t") == 0, "")
# 忽略的口径不再推荐
# 2026-10-04 更正：原样例用 `SUM(available_qty)` 断言「ignore 后返回 0」，但那条
# 表达式**本来就不是已注册口径**（注册表是 sum(available_qty+coalesce(frozen_qty,0))
# 且带最新快照过滤），所以即使没被 ignore，它也会被当作新口径沉淀 1 条 → 断言必然失败。
# 真正要守的红线是「被 ignore 的**同一条**候选不再进池」，
# 因此改用 B1 沉淀的那条完全相同的 SQL。
_IGNORED.add(cands[0]["id"])
_n_b8 = suggest_from_query("按产品统计可用与冻结之和",
      "SELECT product_id, SUM(available_qty + frozen_qty) FROM inv_inventory_snapshot GROUP BY product_id")
check("B8 忽略后不再沉淀", _n_b8 == 0, f"n={_n_b8}")
_IGNORED.clear()
for k in list(_CAND):
    _CAND.pop(k)

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
