# -*- coding: utf-8 -*-
"""多 Agent 评价闭环单元测试（P0-2，对标 Smartbi 白泽四 Agent）

覆盖：评价 Agent 打分结构、分数钳制、异常降级、多候选交叉比对的"只择优不劣化"约束。
"""
import sys
sys.path.insert(0, '.')

from agent.llm_service import LLMService

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def _svc(query="各产线的产量是多少", sql="SELECT line_name, SUM(good_qty) AS total FROM mes_process_output GROUP BY line_name"):
    s = LLMService(query, fast=True)
    s.sql = sql
    s.sql_result = {
        "success": True,
        "columns": ["line_name", "total"],
        "rows": [{"line_name": "L01", "total": 3684}, {"line_name": "L02", "total": 2215}],
        "row_count": 2,
    }
    return s

print("═══ A. 评价 Agent 打分（真实 LLM）═══")
s = _svc()
ev = s._llm_result_evaluate()
if ev:
    check("A1 返回 score", isinstance(ev.get("score"), int) and 0 <= ev["score"] <= 100, f"score={ev.get('score')}")
    check("A2 四维齐全", set(ev.get("dims", {})) == {"metric_match", "dimension_coverage",
                                                     "filter_consistency", "usability"}, str(ev.get("dims")))
    check("A3 各维分数在 0-100", all(0 <= v <= 100 for v in ev["dims"].values()), str(ev["dims"]))
    check("A4 有评语", bool(ev.get("comment")), ev.get("comment", "")[:40])
else:
    print("  ⚠️  评价不可用（LLM 未配置或超时），跳过 A1-A4 —— 属降级路径，不阻断主流程")
    check("A0 降级返回空 dict 不抛异常", ev == {}, str(ev))

print("═══ B. 答非所问应显著低分（语义判别力）═══")
bad = _svc("各产线的产量是多少", "SELECT process_id FROM mes_process_output LIMIT 5")
bad.sql_result = {"success": True, "columns": ["process_id"],
                  "rows": [{"process_id": 1}], "row_count": 1}
ev_bad = bad._llm_result_evaluate()
if ev and ev_bad:
    check("B1 错结果分数低于对结果", ev_bad["score"] < ev["score"], f"错 {ev_bad['score']} vs 对 {ev['score']}")
    check("B2 维度完整性被扣分", ev_bad["dims"]["dimension_coverage"] < ev["dims"]["dimension_coverage"],
          f"{ev_bad['dims']['dimension_coverage']} vs {ev['dims']['dimension_coverage']}")
else:
    print("  ⚠️  跳过 B（评价不可用）")

print("═══ C. 交叉比对：只择优，不劣化（确定性打桩）═══")

def _stub(s, cand_scores, validate_ok=True):
    """打桩：候选 SQL 执行成功、规则校验通过、评分由注入映射决定。"""
    s._generate_candidates = lambda n=2: list(cand_scores.keys())
    s._exec_sql = lambda sql: {"success": True, "columns": ["line_name", "total"],
                               "rows": [{"line_name": "L01", "total": 1}], "row_count": 1}
    s._validate_result = lambda: "" if validate_ok else "不通过"
    s._llm_result_evaluate = lambda: {"score": cand_scores.get(s.sql, 0), "dims": {}, "comment": ""}

s = _svc()
s._eval_score = 50
_stub(s, {"CAND_HIGH": 90, "CAND_LOW": 30})
replaced = s._cross_validate(n=2)
check("C1 高分候选被采纳", replaced and s.sql == "CAND_HIGH", f"sql={s.sql}")
check("C2 分数更新为更优值", s._eval_score == 90, str(s._eval_score))
check("C3 标记交叉比对生效", s._cross_validated and s._refined, "")

s = _svc()
s._eval_score = 80
_stub(s, {"CAND_MID": 60})
replaced = s._cross_validate(n=2)
check("C4 更差候选不采纳（不劣化）", (not replaced) and s.sql.startswith("SELECT"), f"sql={s.sql[:30]}")

s = _svc()
s._eval_score = 80
_stub(s, {"CAND_EQ": 80})
replaced = s._cross_validate(n=2)
check("C5 同分不替换（需严格更优）", not replaced, "")

s = _svc()
s._eval_score = 40
_stub(s, {"CAND_BAD": 95}, validate_ok=False)
replaced = s._cross_validate(n=2)
check("C6 规则校验不通过的候选被剔除", not replaced, "")

s = _svc()
s._eval_score = 40
s._generate_candidates = lambda n=2: (_ for _ in ()).throw(RuntimeError("LLM 挂了"))
check("C7 生成候选异常时不崩、不替换", s._cross_validate(n=2) is False, "")

s = _svc()
orig_sql = s.sql
s._eval_score = 40
_stub(s, {"CAND_FAIL": 99})
s._exec_sql = lambda sql: {"success": False, "error": "boom", "rows": [], "row_count": 0}
check("C8 执行失败的候选被跳过", s._cross_validate(n=2) is False and s.sql == orig_sql, "")

print("═══ D. 复杂查询判定（auto 模式门控复用）═══")
s = _svc()
check("D1 含 GROUP BY 判为复杂", s._is_complex_query(), "")
s.sql = "SELECT * FROM mes_process_output LIMIT 10"
check("D2 单表明细不判复杂", not s._is_complex_query(), "")

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
