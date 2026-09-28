"""多步调查 Agent —— 对标 Databricks Genie Agent Mode 的自主调查循环。

Genie Agent Mode 的核心机制（官方文档/博客）：
  1. 规划（Plan）：把问题分解为「确认事实 + 若干待验证假设」
  2. 调查（Investigate）：逐条执行查询验证假设，动态反思决定下一步
  3. 综合（Synthesize）：产出报告——量化事实 + 主要贡献者 + SQL 引用 + 行动建议
  4. 透明：每个推理步骤、SQL、中间结果对用户可见，可审计

本实现按现有代码能力裁剪落地：
  - 复用主流程的 SQL 生成（build_sql_prompt few-shot）、安全校验（validate_sql_safety
    只读 + LIMIT）、执行（execute_sql）、schema 构建（_build_schema_fast）
  - 步骤数受限（默认 4 步）、每步结果截断，防失控与超时
  - 全程返回可审计的 steps（问题 / SQL / 行数 / 摘要），报告由 LLM 综合生成
  - LLM 任何一步失败 → 该步降级跳过，不阻断整体；报告保证生成（最差为事实摘要）
"""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any

_MAX_STEPS = 4          # 假设验证最大步数（防失控）
_MAX_STEP_ROWS = 100    # 每步 SQL 结果行数上限
_SQL_RE = re.compile(r"```(?:sql)?\s*(SELECT[\s\S]*?|WITH[\s\S]*?)\s*```", re.IGNORECASE)


def _logger():
    import logging
    return logging.getLogger("investigator")


# ── 小工具 ──────────────────────────────────────────────

def _extract_sql(text: str) -> str:
    """从 LLM 输出提取 SQL：优先 ```sql 代码块，否则取首个 SELECT/WITH 语句。"""
    if not text:
        return ""
    m = _SQL_RE.search(text)
    if m:
        return m.group(1).strip().rstrip(";")
    m = re.search(r"(SELECT[\s\S]*?|WITH[\s\S]*?)(?:;|\n\n|$)", text, re.IGNORECASE)
    return (m.group(1).strip().rstrip(";") if m else "")


def _loads_lenient(text: str) -> Any:
    """容错解析 LLM JSON：直接解析 → 提取 ```json 块 → 截取首个 { 到末尾 }。"""
    text = (text or "").strip()
    for candidate in (text,):
        try:
            return json.loads(candidate)
        except Exception:
            pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


def _safe_execute(sql: str) -> dict:
    """只读校验 + 权限改写（行/列级 ACL）+ LIMIT 保护 + 执行；失败返回错误信息。

    权限：从 security.context 读取当前请求的 ACL（端点已 set_acl），通过
    enforcer.rewrite_sql 注入行过滤/列脱敏——与主流程 LLMService 一致，
    防止低权限用户通过调查功能绕过行级权限读全库。无 ACL 上下文时原样执行。
    """
    if not sql:
        return {"success": False, "error": "SQL 为空"}
    try:
        from agent.sql_validator import validate_sql_safety
        from db.executor import execute_sql
        ok, err, cleaned = validate_sql_safety(sql)
        if not ok:
            return {"success": False, "error": f"安全校验未通过：{err}"}
        # 权限改写（行过滤/列脱敏）：ContextVar 中的 ACL 由调用方（API 端点）绑定
        try:
            from security.context import get_acl
            from security.enforcer import rewrite_sql
            acl = get_acl()
            if acl is not None:
                eff, acl_err, _applied = rewrite_sql(cleaned, acl)
                if acl_err:
                    return {"success": False, "error": f"权限校验未通过：{acl_err}"}
                cleaned = eff
        except Exception:
            pass  # ACL 模块异常不阻断（最坏退化为只读校验）
        return execute_sql(cleaned)
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


def _schema_for(query: str, matched_tables: list | None = None) -> str:
    """构建调查用 schema 上下文（复用主流程快速 schema 构建，应用表级权限）。"""
    try:
        from agent.llm_service import _build_schema_fast
        # 表级权限：从当前 ACL 取 allowed_tables（None=不限制），传给 schema 构建
        # 过滤 JOIN 路径引入的无权中间表，防止 schema 泄漏无权表结构
        allowed_tables = None
        try:
            from security.context import get_acl
            acl = get_acl()
            if acl is not None and getattr(acl, "allowed_tables", None) is not None:
                allowed_tables = set(acl.allowed_tables)
        except Exception:
            pass
        tables = matched_tables or []
        names = [t.get("table_name") for t in tables if isinstance(t, dict)] or []
        if not names:
            from db.tools import match_tables_by_query
            names = [t.get("table_name") for t in match_tables_by_query(query)[:6]]
        # 候选表也按表级权限过滤：allowed_tables 不仅过滤 JOIN 中间表，
        # 无权的候选表（如关键词误匹配）不进 schema，防越权表结构泄漏到 prompt
        if allowed_tables is not None:
            names = [n for n in names if n.split(".")[-1].lower() in allowed_tables]
        return _build_schema_fast(names or [], allowed_tables=allowed_tables, query=query)
    except Exception:
        return ""


def _knowledge_context(query: str, max_chars: int = 1200) -> str:
    """检索知识库（业务文档/术语口径 + 记忆 SQL 示例）作为调查上下文。

    对标 Snowflake Cortex Agents 的「结构化（SQL）+ 非结构化（文档）跨源编排」：
    让规划/假设生成/综合报告都能参考已沉淀的业务口径与历史成功查询，
    而非只依赖表结构。检索失败返回空串（不阻断调查）。
    """
    try:
        from agent.memory import get_memory
        mem = get_memory()
        parts: list[str] = []
        docs = mem.search_documentation(query, k=3)
        if docs:
            parts.append("业务口径/术语：" + "；".join(str(d)[:180] for d in docs[:3]))
        exs = mem.search_sql(query, k=2)
        if exs:
            for e in exs[:2]:
                q = str(e.get("question", ""))[:80]
                s = str(e.get("sql", ""))[:200]
                if q and s:
                    parts.append(f"历史相似查询：{q} → {s}")
        return "\n".join(parts)[:max_chars]
    except Exception:
        return ""


def _result_summary(r: dict, limit: int = 5) -> str:
    """结果摘要：行数 + 前 N 行（JSON 截断）。"""
    if not r.get("success"):
        return f"执行失败：{r.get('error', '')[:120]}"
    rows = r.get("rows") or []
    if not rows:
        return "查询返回 0 行"
    head = json.dumps(rows[:limit], ensure_ascii=False, default=str)
    return f"{r.get('row_count', len(rows))} 行；示例：{head[:400]}"


def _compile_sql(query: str) -> tuple[str, str] | None:
    """确定性编译优先：尝试用指标编译器按注册口径生成 SQL。

    架构约定（用户明确定义）：SQL 由确定性编译器生成，LLM 不直接产 SQL。
    返回 (sql, "compiled")；未命中注册口径返回 None（调用方再走 LLM 兜底并标记）。
    """
    try:
        from agent.metric_compiler import try_compile_metric
        cp = try_compile_metric(query)
        if cp and cp.get("sql"):
            return cp["sql"], "compiled"
    except Exception:
        pass
    return None


def _generate_sql(question: str, schema: str) -> tuple[str, str]:
    """为假设生成 SQL：确定性编译优先，LLM 兜底。

    返回 (sql, source)，source ∈ {"compiled", "llm"}——
    便于审计哪些查询走了 LLM 生成路径（应逐步向确定性编译收敛）。
    """
    # 1. 确定性编译：注册口径命中直接用编译 SQL（架构约定优先路径）
    compiled = _compile_sql(question)
    if compiled:
        return compiled
    # 2. LLM 兜底（未注册口径的临时路径）：仅产出 SQL 草案，仍走只读校验+权限改写
    try:
        from agent.prompt_builder import build_sql_prompt
        from agent.llm_service import _make_llm
        from langchain_core.messages import SystemMessage, HumanMessage
        # 跨源编排：注入知识库检索的业务文档（术语/口径）与历史相似 SQL 示例
        docs, sql_examples = [], []
        try:
            from agent.memory import get_memory
            mem = get_memory()
            docs = mem.search_documentation(question, k=2)
            sql_examples = mem.search_sql(question, k=2)
        except Exception:
            pass
        prompt = build_sql_prompt(question, schema, docs=docs or None,
                                  sql_examples=sql_examples or None, max_tokens=2400)
        llm = _make_llm(temp=0.0, max_tokens=1200, timeout=60)
        resp = llm.invoke([SystemMessage(content=prompt), HumanMessage(content=question)])
        text = str(getattr(resp, "content", "") or "")
        # LLM 可能输出 JSON 样式（{"sql": "...", "chart_type": ...}）：此时 SQL 字段里的
        # 双引号被 JSON 转义为 \"，直接按文本提取会残留反斜杠导致 PG 42601 语法错误。
        # 先尝试解析 JSON 取 sql 字段（自动反转义），失败再退回文本提取。
        data = _loads_lenient(text)
        if isinstance(data, dict):
            for key in ("sql", "query", "statement"):
                if data.get(key):
                    return _extract_sql(str(data[key])), "llm"
        return _extract_sql(text), "llm"
    except Exception as e:
        _logger().warning("假设 SQL 生成失败: %s", e)
        return "", "llm"


# ── 四阶段 ──────────────────────────────────────────────

def _plan(query: str, schema: str) -> dict:
    """阶段一：LLM 规划 —— 确认 SQL + 待验证假设列表。

    返回 {"confirm_sql": str, "hypotheses": [{"title": str, "question": str}]}
    解析失败返回空 dict（调用方按 0 假设处理）。
    """
    prompt = (
        "你是数据分析调查规划员。用户提出一个业务问题，请规划一次数据调查。\n"
        "先写一条 confirm_sql：用 SELECT 查询确认问题提到的事实（如总趋势/关键指标的量与变化），"
        "注意只读、加 LIMIT 100。\n"
        "再列出 2-4 个 hypotheses：每个假设是一个『待验证的业务原因方向』，"
        "用一句 question 描述如何用数据验证它（指明分组维度/对比期间/过滤条件），"
        "不要写 SQL，SQL 由后续生成。\n"
        "只输出 JSON：{\"confirm_sql\": \"...\", \"hypotheses\": [{\"title\": \"假设名\", \"question\": \"验证描述\"}]}\n\n"
        f"## 用户问题\n{query}\n\n"
        f"## 表结构（可选参考）\n{schema[:2500]}\n\n"
        f"## 业务知识（参考，来自知识库）\n{_knowledge_context(query)}"
    )
    try:
        from agent.llm_service import _make_llm
        from langchain_core.messages import SystemMessage, HumanMessage
        llm = _make_llm(temp=0.2, json_mode=True, max_tokens=1400, timeout=60)
        resp = llm.invoke([SystemMessage(content=prompt), HumanMessage(content=query)])
        data = _loads_lenient(str(getattr(resp, "content", "") or ""))
        if not isinstance(data, dict):
            return {}
        # confirm_sql 是 JSON 字段值，已是纯 SQL 字符串：直接 strip 即可。
        # 不可再过 _extract_sql——其正则按首个空行/分号截断，会切断多行 CTE SQL。
        confirm = str(data.get("confirm_sql") or "").strip().rstrip(";")
        if "```" in confirm:  # 极少数情况 LLM 在字段内又套了代码块标记
            confirm = _extract_sql(confirm)
        hyps = []
        for h in (data.get("hypotheses") or [])[:_MAX_STEPS]:
            if isinstance(h, dict) and h.get("question"):
                hyps.append({
                    "title": str(h.get("title") or h["question"])[:40],
                    "question": str(h["question"])[:300],
                })
        return {"confirm_sql": confirm, "hypotheses": hyps}
    except Exception as e:
        _logger().warning("调查规划失败: %s", e)
        return {}


def _synthesize(query: str, confirm_summary: str, steps: list[dict], schema: str) -> str:
    """阶段三：LLM 综合生成调查报告（量化事实 + 贡献者 + 建议）。"""
    try:
        from agent.llm_service import _make_llm
        from langchain_core.messages import SystemMessage, HumanMessage
        step_text = "\n".join(
            f"- [{s.get('title')}] {s.get('question')}\n  查询结果: {s.get('summary', '')}"
            for s in steps if s.get("summary")
        ) or "（假设验证均未产生有效数据）"
        prompt = (
            "你是数据分析专家。基于一次多步调查的结果，写一份简明的调查报告（Markdown）。\n"
            "结构要求：\n"
            "## 结论\n先回答用户问题（1-2 句），量化关键事实（数值引用调查数据）。\n"
            "## 关键发现\n按重要程度列出 2-4 条发现，每条标注数据来源（对应调查步骤）。\n"
            "## 建议\n给 1-2 条可执行建议。\n"
            "只依据给出的数据，不要编造数值。\n\n"
            f"## 用户问题\n{query}\n\n"
            f"## 事实确认\n{confirm_summary}\n\n"
            f"## 假设验证结果\n{step_text}\n\n"
            f"## 业务知识（参考，来自知识库）\n{_knowledge_context(query)}\n\n"
            f"## 表结构参考\n{schema[:1500]}"
        )
        llm = _make_llm(temp=0.3, max_tokens=1000, timeout=90)
        resp = llm.invoke([SystemMessage(content=prompt), HumanMessage(content=query)])
        text = str(getattr(resp, "content", "") or "").strip()
        return text or "## 结论\n调查完成，但未能生成有效分析结论。"
    except Exception as e:
        _logger().warning("综合报告失败，回退事实摘要: %s", e)
        return f"## 结论\n调查完成。{confirm_summary}"


# ── 主入口 ──────────────────────────────────────────────

def investigate(query: str, matched_tables: list | None = None,
                max_steps: int = _MAX_STEPS) -> dict:
    """多步调查主入口。

    Args:
        query: 用户问题（"为什么…" / "什么原因…" 类调查问题）
        matched_tables: 预选表（可选；缺省按关键词匹配）
        max_steps: 假设验证步数上限（默认 4，防失控）

    Returns:
        {"ok": bool, "report_md": str, "report_html": str,
         "steps": [{"title", "question", "sql", "row_count", "summary"}], "error": str}
    """
    t0 = time.time()
    if not query or not query.strip():
        return {"ok": False, "report_md": "", "report_html": "", "steps": [], "error": "问题不能为空"}
    query = query.strip()
    # max_steps 上限收紧到 _MAX_STEPS（规划阶段本就能生成最多 4 个假设），
    # 防止外部直接调函数时传入超大值导致失控的 LLM 调用
    try:
        max_steps = max(1, min(int(max_steps or _MAX_STEPS), _MAX_STEPS))
    except (TypeError, ValueError):
        max_steps = _MAX_STEPS
    schema = _schema_for(query, matched_tables)
    steps: list[dict] = []

    # 阶段一：规划
    plan = _plan(query, schema)
    confirm_sql = plan.get("confirm_sql") or ""
    hyps = (plan.get("hypotheses") or [])[:max_steps]
    if not confirm_sql and not hyps:
        # 规划失败（如 LLM 不可用）→ 退化为单次查询：直接尝试回答原始问题
        sql, source = _generate_sql(query, schema)
        if sql:
            r = _safe_execute(sql)
            summary = _result_summary(r)
            steps.append({
                "title": "直接调查", "question": query,
                "sql": sql, "sql_source": source,
                "row_count": r.get("row_count", 0) if r.get("success") else 0,
                "summary": summary,
            })
            report_md = f"## 结论\n{summary}"
            return {
                "ok": True, "report_md": report_md,
                "report_html": _md_to_html_safe(report_md),
                "steps": steps, "error": "", "elapsed_ms": int((time.time() - t0) * 1000),
            }
        return {
            "ok": False, "report_md": "", "report_html": "",
            "steps": steps, "error": "调查规划失败，且无法生成查询",
        }

    # 阶段二：确认事实
    confirm_summary = ""
    confirm_source = "llm"
    if confirm_sql:
        # 确定性编译优先：先用指标编译器按用户原始问题编译确认 SQL，
        # 命中则替换 LLM 规划的 confirm_sql（架构约定：注册口径走确定性编译）
        compiled_confirm = _compile_sql(query)
        if compiled_confirm:
            confirm_sql = compiled_confirm[0]
            confirm_source = "compiled"
        r = _safe_execute(confirm_sql)
        confirm_summary = _result_summary(r)
        steps.append({
            "title": "事实确认", "question": "确认问题涉及的核心事实",
            "sql": confirm_sql, "sql_source": confirm_source,
            "row_count": r.get("row_count", 0) if r.get("success") else 0,
            "summary": confirm_summary,
        })
        # 确认 0 行（常见于静态演示数据：LLM 用 CURRENT_DATE 过滤，数据实际停在更早日期）
        # → 追加数据范围查询，把"数据在哪里"摊开，报告据此给出准确结论
        if r.get("success") and not r.get("rows"):
            try:
                from agent.llm_service import _extract_sql_tables
                tables = sorted(_extract_sql_tables(confirm_sql))
                if tables:
                    tbl = tables[0].split(".")[-1]
                    scope = _safe_execute(
                        f"SELECT MIN(stat_date) AS min_date, MAX(stat_date) AS max_date, "
                        f"COUNT(*) AS n FROM {tbl}"
                    )
                    if scope.get("success") and scope.get("rows"):
                        scope_sql = f"SELECT MIN(stat_date), MAX(stat_date), COUNT(*) FROM {tbl}"
                        steps.append({
                            "title": "数据范围", "question": f"定位 {tbl} 的实际数据时间范围",
                            "sql": scope_sql,
                            "row_count": scope.get("row_count", 0),
                            "summary": _result_summary(scope),
                        })
            except Exception:
                pass

    # 阶段二续：逐条验证假设（串行执行）
    for h in hyps:
        try:
            sql, source = _generate_sql(h["question"], schema)
            if not sql:
                continue
            r = _safe_execute(sql)
            steps.append({
                "title": h["title"], "question": h["question"],
                "sql": sql, "sql_source": source,
                "row_count": r.get("row_count", 0) if r.get("success") else 0,
                "summary": _result_summary(r),
            })
        except Exception as e:
            _logger().warning("假设验证异常: %s", e)

    # 阶段三：综合报告（有数据才综合；全部失败则报错）
    if not confirm_summary and not any(s.get("summary") and "失败" not in s["summary"] for s in steps):
        return {"ok": False, "report_md": "", "report_html": "", "steps": steps,
                "error": "调查执行失败：所有查询均未返回有效数据"}
    report_md = _synthesize(query, confirm_summary, steps, schema)

    return {
        "ok": True,
        "report_md": report_md,
        "report_html": _md_to_html_safe(report_md),
        "steps": steps,
        "error": "",
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


# ── 安全 HTML 渲染（供前端 v-html，全部转义防 XSS）──────

def _md_to_html_safe(md: str) -> str:
    """把调查报告 markdown 转成安全的 HTML：所有原文 html.escape，只保留结构标签。"""
    out: list[str] = []
    for line in (md or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if line.startswith("## "):
            out.append(f"<h4 class='text-sm font-semibold text-gray-800 mt-2'>{html.escape(line[3:])}</h4>")
        elif line.startswith("# "):
            out.append(f"<h3 class='text-base font-semibold text-gray-900'>{html.escape(line[2:])}</h3>")
        elif re.match(r"^[-*]\s+", line):
            item = re.sub(r"^[-*]\s+", "", line)
            out.append(f"<div class='text-sm text-gray-600 pl-2'>• {html.escape(item)}</div>")
        elif line.strip().startswith("```"):
            continue  # 代码块首尾行跳过
        else:
            out.append(f"<div class='text-sm text-gray-700'>{html.escape(line)}</div>")
    return "\n".join(out) if out else html.escape(md or "")
