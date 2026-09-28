# -*- coding: utf-8 -*-
"""最小编排器（P0-A 演示版，对齐 Agentic Harness 自主任务规划——参赛路线图演示）

不改变现有主链路：作为**独立端点** /api/agent/orchestrate，把复杂问题拆成「任务链」
顺序执行，每步复用现有能力节点（确定性编译查数 / 规则归因 / 趋势预测 / 信号洞察 /
LLM 汇总），产出**可审计 flow**（步骤 / 输入 / 输出 / 耗时 / 状态）——演示"Agent
会自己往下走"，同时守住全部架构红线（每步查询走 LLMService 内部的确定性编译优先
+ 权限改写 + 未注册口径二次确认；失败/弹窗 → 该步标记 skipped，不阻断整体）。

任务类型判定（确定性词表规则，低置信不猜）：
  attribution  为什么|原因|为何|导致|下降|上升|怎么降|怎么升|影响
  predict      预测|预估|下个月|未来|走势|会怎样|会如何|接下来|趋势|预计
  insight      异常|风险|关注|盲点|预警|机会|值得注意
  report       报告|总结|复盘|建议|改善|汇总|整体情况|综合

链路组合（按优先级）：
  归因+预测 → [查数, 归因, 预测, 报告]
  归因      → [查数, 归因, 报告]
  预测      → [查数, 预测, 报告]
  洞察      → [查数, 洞察, 报告]
  其他      → [查数, 洞察, 报告]
"""

from __future__ import annotations

import logging
import re
import time

_logger = logging.getLogger("orchestrator")

_TASK_RE = {
    "attribution": r"为什么|原因|为何|导致|下降|上升|怎么降|怎么升|影响|下滑",
    "predict": r"预测|预估|预判|下个月|下季度|未来|走势|会怎样|会如何|接下来|预计|趋势",
    "insight": r"异常|风险|关注|盲点|预警|机会|值得注意",
    "report": r"报告|总结|复盘|建议|改善|汇总|整体情况|综合",
}


def plan_chain(query: str) -> list[str]:
    """规则判定任务链（去重、保序）。"""
    hits = [k for k, rx in _TASK_RE.items() if re.search(rx, query or "")]
    chain: list[str] = []
    if "attribution" in hits and "predict" in hits:
        chain = ["query", "attribution", "predict", "report"]
    elif "attribution" in hits:
        chain = ["query", "attribution", "report"]
    elif "predict" in hits:
        chain = ["query", "predict", "report"]
    elif "insight" in hits:
        chain = ["query", "insight", "report"]
    else:
        chain = ["query", "insight", "report"]
    return chain


def _now() -> float:
    return time.time()


def _run_query(query: str, acl) -> dict:
    """查数节点：LLMService(fast=True)——内部含确定性编译优先 + 权限改写 + 二次确认红线。"""
    t0 = _now()
    try:
        from agent.llm_service import LLMService
        svc = LLMService(query, fast=True, acl=acl)
        final = None
        for ev in svc.run():
            if ev.get("type") == "done":
                final = ev.get("response") or {}
        if not final:
            return {"status": "skipped", "title": "查数", "summary": "链路无输出", "elapsed_ms": 0}
        if final.get("type") == "analysis_confirm":
            return {"status": "skipped", "title": "查数",
                    "summary": "口径未定义，已触发二次确认（架构红线）——跳过该步",
                    "elapsed_ms": int((_now() - t0) * 1000)}
        if not svc.sql or not (svc.sql_result or {}).get("success"):
            err = (svc.sql_result or {}).get("error") or svc.error or "查询失败"
            return {"status": "failed", "title": "查数", "summary": str(err)[:120],
                    "sql": svc.sql or "", "elapsed_ms": int((_now() - t0) * 1000)}
        rows = (svc.sql_result or {}).get("rows") or []
        cols = (svc.sql_result or {}).get("columns") or []
        return {
            "status": "done", "title": "查数",
            "summary": f"命中 {len(cols)} 列 × {len(rows)} 行",
            "sql": svc.sql, "columns": cols[:12], "rows": rows[:8],
            "rows_total": len(rows),
            "elapsed_ms": int((_now() - t0) * 1000),
        }
    except Exception as e:
        return {"status": "failed", "title": "查数", "summary": f"异常: {str(e)[:120]}",
                "elapsed_ms": int((_now() - t0) * 1000)}


def _run_attribution(query: str, query_step: dict) -> dict:
    """归因节点：规则归因（环比下跌检测 + 维度贡献，纯计算）。"""
    t0 = _now()
    try:
        if query_step.get("status") != "done":
            return {"status": "skipped", "title": "归因", "summary": "依赖查数结果，查数未完成",
                    "elapsed_ms": int((_now() - t0) * 1000)}
        from agent.attribution import detect_and_attribute
        sql_result = {"success": True, "columns": query_step.get("columns", []),
                      "rows": query_step.get("rows", [])}
        text = detect_and_attribute(query, sql_result)
        if not text or "未检测到" in text or "无明显" in text:
            return {"status": "done", "title": "归因",
                    "summary": text or "未检测到明显波动（规则归因：环比/维度贡献）",
                    "elapsed_ms": int((_now() - t0) * 1000)}
        return {"status": "done", "title": "归因", "summary": text[:300],
                "elapsed_ms": int((_now() - t0) * 1000)}
    except Exception as e:
        return {"status": "failed", "title": "归因", "summary": f"异常: {str(e)[:120]}",
                "elapsed_ms": int((_now() - t0) * 1000)}


def _run_predict(query: str, query_step: dict) -> dict:
    """预测节点：复用 generate_predict（对查询结果时间序列做趋势预测）。"""
    t0 = _now()
    try:
        if query_step.get("status") != "done":
            return {"status": "skipped", "title": "预测", "summary": "依赖查数结果，查数未完成",
                    "elapsed_ms": int((_now() - t0) * 1000)}
        from agent.llm_service import generate_predict
        sql_result = {"success": True, "columns": query_step.get("columns", []),
                      "rows": query_step.get("rows", [])}
        out = []
        for ev in generate_predict(sql_result):
            if isinstance(ev, dict):
                t = ev.get("text") or ev.get("prediction") or ""
                if t:
                    out.append(t)
            else:
                out.append(str(ev))
        text = "".join(out)[:400] or "预测完成（数据不足时给出趋势判断）"
        return {"status": "done", "title": "趋势预测", "summary": text,
                "elapsed_ms": int((_now() - t0) * 1000)}
    except Exception as e:
        return {"status": "failed", "title": "趋势预测", "summary": f"异常: {str(e)[:120]}",
                "elapsed_ms": int((_now() - t0) * 1000)}


def _run_insight(query: str, query_step: dict) -> dict:
    """洞察节点：确定性信号统计（数值列摘要 + Top 维度），不调 LLM。"""
    t0 = _now()
    try:
        if query_step.get("status") != "done":
            return {"status": "skipped", "title": "洞察", "summary": "依赖查数结果，查数未完成",
                    "elapsed_ms": int((_now() - t0) * 1000)}
        rows = query_step.get("rows") or []
        cols = query_step.get("columns") or []
        if not rows:
            return {"status": "done", "title": "洞察", "summary": "无数据可洞察",
                    "elapsed_ms": int((_now() - t0) * 1000)}
        num_cols = []
        for c in cols:
            vals = [r.get(c) for r in rows if r.get(c) not in (None, "")]
            if vals and all(_is_number(v) for v in vals):
                num_cols.append((c, [float(v) for v in vals]))
        lines = []
        for c, vals in num_cols[:3]:
            lines.append(f"「{c}」合计 {sum(vals):.2f}，均值 {sum(vals) / len(vals):.2f}，"
                         f"最大 {max(vals):.2f}（最小 {min(vals):.2f}）")
        if not lines:
            lines.append("结果以分类/文本列为主，无数值列可做统计")
        return {"status": "done", "title": "洞察", "summary": "；".join(lines),
                "elapsed_ms": int((_now() - t0) * 1000)}
    except Exception as e:
        return {"status": "failed", "title": "洞察", "summary": f"异常: {str(e)[:120]}",
                "elapsed_ms": int((_now() - t0) * 1000)}


def _run_report(query: str, steps: list[dict]) -> dict:
    """报告节点：LLM 汇总前序步骤（只产 NL，不产 SQL）。"""
    t0 = _now()
    try:
        done = [s for s in steps if s.get("status") == "done"]
        if not done:
            return {"status": "skipped", "title": "综合报告", "summary": "前序步骤均未成功，跳过",
                    "elapsed_ms": int((_now() - t0) * 1000)}
        from agent.llm_service import _make_llm
        from langchain_core.messages import HumanMessage
        ctx = "\n".join(f"- {s['title']}: {s.get('summary', '')[:200]}" for s in done)
        prompt = (
            "你是数据分析专家。基于以下多步分析结果，用 2-4 句话输出一份简洁的"
            "综合结论与行动建议（只输出中文结论，不要列标题）。\n"
            f"用户问题：{query}\n分析结果：\n{ctx}"
        )
        llm = _make_llm(temp=0.3, max_tokens=400)
        text = str(llm.invoke([HumanMessage(content=prompt)]).content or "").strip()[:500]
        if not text:
            return {"status": "skipped", "title": "综合报告", "summary": "LLM 汇总失败",
                    "elapsed_ms": int((_now() - t0) * 1000)}
        return {"status": "done", "title": "综合报告", "summary": text,
                "elapsed_ms": int((_now() - t0) * 1000)}
    except Exception as e:
        return {"status": "failed", "title": "综合报告", "summary": f"异常: {str(e)[:120]}",
                "elapsed_ms": int((_now() - t0) * 1000)}


def _is_number(v) -> bool:
    try:
        float(str(v).replace(",", "").strip())
        return True
    except (TypeError, ValueError):
        return False


def orchestrate(query: str, acl=None) -> dict:
    """编排主入口：判定任务链 → 顺序执行节点 → 产出可审计 flow。

    返回 {success, query, chain, flow: [{step, title, status, summary, sql, elapsed_ms}],
          conclusion, elapsed_ms, error}
    """
    if not query or not str(query).strip():
        return {"success": False, "error": "问题不能为空"}
    t0 = _now()
    chain = plan_chain(query)
    steps: list[dict] = []
    for node in chain:
        if node == "query":
            step = _run_query(query, acl)
        elif node == "attribution":
            step = _run_attribution(query, steps[0] if steps else {})
        elif node == "predict":
            step = _run_predict(query, steps[0] if steps else {})
        elif node == "insight":
            step = _run_insight(query, steps[0] if steps else {})
        else:  # report
            step = _run_report(query, steps)
        step["step"] = len(steps) + 1
        steps.append(step)

    conclusion = ""
    for s in reversed(steps):
        if s.get("status") == "done" and s.get("title") == "综合报告":
            conclusion = s.get("summary", "")
            break
    return {
        "success": True,
        "query": query,
        "chain": chain,
        "flow": steps,
        "conclusion": conclusion,
        "elapsed_ms": int((_now() - t0) * 1000),
        "error": "",
    }
