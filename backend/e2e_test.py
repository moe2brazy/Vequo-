"""端到端实测：直接驱动 LLMService，验证 9 项修复链路（真实 DB + 真实 LLM）"""
import json
from agent.llm_service import LLMService


def run_query(query, history=None, label="QUERY"):
    print(f"\n{'='*60}\n[{label}] {query}\n{'='*60}")
    svc = LLMService(query, history or [])
    final = None
    steps = []
    for ev in svc.run():
        t = ev.get("type")
        if t == "step":
            steps.append(ev.get("name", ""))
        elif t == "sql":
            print("  📌 SQL:", ev.get("sql", "")[:200])
        elif t == "done":
            final = ev.get("response", {})
        elif t == "error":
            print("  ❌ ERROR:", ev.get("message"))
            return None
    if not final:
        print("  ⚠️ 无 done 响应")
        return None
    q = final.get("quality", {})
    print("  type:", final.get("type"))
    print("  steps:", " → ".join(steps))
    print("  SQL:", (final.get("sql") or "")[:200])
    res = final.get("result", {}) or {}
    print("  rows:", len(res.get("rows", [])), "cols:", res.get("columns"))
    print("  chart_type:", (final.get("chart") or {}).get("type"))
    print("  quality:", json.dumps(q, ensure_ascii=False))
    analysis = final.get("analysis")
    if isinstance(analysis, str):
        print("  analysis:", analysis[:300].replace("\n", " "))
    elif isinstance(analysis, dict):
        # analysis_confirm 类型：analysis 是 LLM 推断结果 dict，不是文本
        print("  analysis(confirm):", json.dumps(analysis, ensure_ascii=False)[:300])
    return final


if __name__ == "__main__":
    # 1) 已知能跑通、且涉及时间趋势的查询
    r1 = run_query("统计各产线最近7天的产量趋势", label="Q1 时间趋势")

    # 2) 多轮指代：依赖上一轮的 sql/表/列（issue #9）
    hist = []
    if r1:
        hist = [
            {"role": "user", "content": "统计各产线最近7天的产量趋势"},
            {"role": "assistant", "content": "已查询",
             "sql": r1.get("sql"), "matched_tables": r1.get("matched_tables"),
             "columns": (r1.get("result") or {}).get("columns", [])},
        ]
    r2 = run_query("把上面的改成按月统计", history=hist, label="Q2 多轮指代(改按月)")

    # 3) 聚合 + 排行类（覆盖聚合口径校验 issue #4/#5）
    r3 = run_query("物料采购量排行", label="Q3 采购量排行")

    # 4) 介绍类意图（issue #1 不能误判为闲聊）
    r4 = run_query("介绍一下这个数据库有哪些表", label="Q4 介绍数据库(意图)")
