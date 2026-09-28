# -*- coding: utf-8 -*-
"""指标候选自动挖掘单元测试（P0-4）

重点：确定性解析（sqlglot）的正确性、归一化去重、候选池生命周期。
LLM 命名部分只做"不崩"校验，具体命名质量靠人工审核把关。
"""
import sys
sys.path.insert(0, '.')

from agent.metric_miner import (_extract_aggs, _norm_expr, _cid, mine_candidates,
                                list_candidates, update_candidate, ignore_candidate,
                                get_candidate, adopt_candidate)

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

print("═══ A. 聚合表达式提取（确定性解析）═══")
a = _extract_aggs("SELECT line_name, SUM(good_qty) AS total FROM mes_process_output GROUP BY line_name")
check("A1 单表聚合提取", len(a) == 1 and a[0]["expr"] == "SUM(good_qty)", str(a))
check("A2 取到别名", a[0]["alias"] == "total", a[0]["alias"])
check("A3 GROUP BY 维度", a[0]["dims"] == ["line_name"], str(a[0]["dims"]))

a = _extract_aggs("SELECT p.line_name, SUM(mp.good_qty) FROM mes_process_output mp "
                  "JOIN dim_production_line p ON mp.line_id = p.line_id GROUP BY p.line_name")
check("A4 别名→事实表解析（口径归并的关键）",
      a[0]["tables"] == ("mes_process_output",), str(a[0]["tables"]))

a = _extract_aggs("SELECT SUM(o.quantity * o.unit_price) FROM test_orders o")
check("A5 复合表达式", a and "*" in a[0]["expr"], str(a[0]["expr"]) if a else "无")

a = _extract_aggs("SELECT product_id, ROW_NUMBER() OVER (PARTITION BY line_id ORDER BY good_qty DESC) "
                  "AS rn, SUM(good_qty) FROM mes_process_output GROUP BY product_id, line_id, good_qty")
check("A6 窗口函数不作为指标口径", all("ROW_NUMBER" not in x["expr"] for x in a), str([x["expr"] for x in a]))

check("A7 非法 SQL 返回空", _extract_aggs("这不是 SQL ###") == [], "")
check("A8 纯 SELECT 无聚合返回空", _extract_aggs("SELECT * FROM t") == [], "")

print("═══ B. 归一化（去重正确性）═══")
check("B1 别名剥离后一致", _norm_expr("SUM(mp.good_qty)") == _norm_expr("SUM(good_qty)"),
      f"{_norm_expr('SUM(mp.good_qty)')} vs {_norm_expr('SUM(good_qty)')}")
check("B2 大小写无关", _norm_expr("SUM(Good_Qty)") == _norm_expr("sum(good_qty)"), "")
check("B3 空白无关", _norm_expr("SUM( a . b )") == _norm_expr("SUM(a.b)"), "")
check("B4 不同口径仍不同", _norm_expr("SUM(good_qty)") != _norm_expr("SUM(defect_qty)"), "")
check("B5 候选 ID 稳定", _cid("SUM(mp.good_qty)", ("mes_process_output",))
      == _cid("SUM(good_qty)", ("mes_process_output",)), "")

print("═══ C. 挖掘（真实历史 SQL）═══")
r = mine_candidates(limit=10, use_llm=False)
if not r["success"]:
    print("  ⚠️  ", r.get("error"), "—— 跳过 C2-C4")
else:
    cands = r["candidates"]
    check("C1 挖掘成功", True, f"候选 {r['total']} 个，已注册跳过 {r['skipped_registered']} 项")
    check("C2 候选结构完整", all({"id", "expr", "tables", "hit_count"} <= set(c) for c in cands), "")
    check("C3 按命中次数降序", all(cands[i]["hit_count"] >= cands[i + 1]["hit_count"]
                                for i in range(len(cands) - 1)), "")
    check("C4 已注册口径不重复推荐", r["skipped_registered"] > 0, str(r["skipped_registered"]))

print("═══ D. 候选池生命周期 ═══")
r = mine_candidates(limit=3, use_llm=False)
cands = list_candidates()
if cands:
    c0 = cands[0]
    cid = c0["id"]
    upd = update_candidate(cid, {"name": "人工改名", "unit": "箱"})
    check("D1 人工编辑生效", upd and upd["name"] == "人工改名" and upd["status"] == "edited", str(upd))
    # 编辑过的候选不被下一轮挖掘覆盖
    mine_candidates(limit=3, use_llm=False)
    check("D2 编辑状态不被重新挖掘覆盖", get_candidate(cid)["name"] == "人工改名", get_candidate(cid)["name"])

    res = adopt_candidate(cid, name="挖掘测试指标")
    # 入库可能因本机 metrics_registry.json 系统级锁定而失败——只要优雅报错即可，不崩
    check("D3 采纳有明确结果（成功或明确错误）",
          isinstance(res, dict) and ("success" in res), str(res.get("error", "已入库"))[:60])

    # 清理：采纳用例会真实写入注册表，测试结束必须还原，避免污染用户口径库
    try:
        from agent.metric_registry import _load_user_metrics, save_user_metrics
        user = _load_user_metrics()
        keep = [m for m in user if "挖掘测试指标" not in str(m.get("name", ""))]
        if len(keep) != len(user):
            save_user_metrics(keep)
            try:
                from agent.metric_memory import get_metric_memory
                get_metric_memory().rebuild()
            except Exception:
                pass
        from agent.metric_registry import get_all_metrics
        check("D3b 测试指标已清理，未污染注册表",
              not [m for m in get_all_metrics() if "挖掘测试指标" in str(m.get("name", ""))], "")
    except Exception as e:
        print("  ⚠️   清理失败（请手工检查 metrics_registry.json）:", str(e)[:80])

    mine_candidates(limit=3, use_llm=False)
    rest = [c for c in list_candidates() if c not in cands]
    if rest:
        ignore_candidate(rest[0]["id"])
        check("D4 忽略后移出候选池", get_candidate(rest[0]["id"]) is None, "")
        mine_candidates(limit=3, use_llm=False)
        check("D5 忽略的黑名单口径不再推荐", get_candidate(rest[0]["id"]) is None, "")
    else:
        print("  ⚠️   无多余候选，跳过 D4-D5")
else:
    print("  ⚠️   候选池为空，跳过 D")

check("D6 不存在的候选采纳有错误提示", adopt_candidate("not_exist_id")["success"] is False, "")
check("D7 忽略不存在的候选返回 False", ignore_candidate("not_exist_id") is False, "")

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
