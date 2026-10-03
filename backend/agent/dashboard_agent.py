# -*- coding: utf-8 -*-
"""一键生成看板（对标 ThoughtSpot SpotterViz / 帆软 FineBI 智能仪表板 /
Power BI Copilot 报表页生成）。

## 竞品做法
- **SpotterViz**：Agent 自动规划「一组可视化 + 布局 + 每图 insight」，而不是单个图表；
  自动挑图型、排栅格、配标题与结论文案。
- **FineBI / Sigma**：智能配图 + 仪表板自动布局，一张看板覆盖一个分析主题的多个视角。

## 我们的增量在哪
已有能力其实已经把"单图"这件事做完了：
  - 数据形态驱动的选图（`llm_service._validate_chart_type`，12 类强规则）
  - ECharts + AntV 双引擎渲染（前端 `src/charts/`）
  - 确定性编译（`metric_compiler.try_compile_metric`）
  - 只读校验 + 行/列级 ACL 改写（`investigator._safe_execute`）
真正缺的只有最上面一层：**把一个分析主题拆成多个互补视角 + 排布 + 配文案**。

所以本模块不重复造轮子，只做「规划 → 并发取数 → 校正图型 → 批量配洞察 → 排栅格」。

## 架构约定对齐
每张卡片的 SQL 仍走「确定性编译优先 → LLM 兜底」（与 investigator 同源），
并在返回值里标注 `sql_source`，便于审计哪些卡片走了 LLM 生成路径。

对外 API：
  - plan_dashboard(query)   → 只出规划（卡片清单），不给前端渲染用
  - build_dashboard(query)  → 规划 + 取数 + 出图 + 洞察，可直接渲染
"""

import json
from concurrent.futures import ThreadPoolExecutor

from langchain_core.messages import SystemMessage

_MAX_WORKERS = 4

_PLAN_SYSTEM = """你是资深 BI 看板设计师。把用户的分析主题拆成一组**互补**的分析卡片。

## 拆分思路（按重要性递减，挑 3-6 张，不要贪多）
1. 整体概览（总量 / 总体水平）
2. 结构拆解（按主要维度分组：产线、产品、区域、部门…）
3. 时间趋势（按月/按日的变化）
4. 排行对比（TOP N 或倒数）
5. 构成占比 / 相关关系 / 异常识别（按数据特点选）

## 输出要求
每张卡片必须是一个**能用一条 SQL 独立回答**的问题。标题 6-12 字，结论导向。
chart_type 从这些里选：line（趋势）、bar（分类对比）、barh（排行）、pie/rose（占比）、
radar（多指标横评）、heatmap（双维度交叉）、scatter（两数值相关）、table（明细/无数值列）。
span：1 = 半宽（默认），2 = 整宽（趋势图、热力图等需要宽度的重要图）。

只输出 JSON，不要 Markdown 代码块：
{{"title": "看板总标题（8-16字）",
  "cards": [{{"title": "卡片标题", "question": "这张卡片要回答的问题（可直接生成SQL）",
             "chart_type": "bar", "span": 1}}]}}

## 硬性约束
- 只能用给定 schema 里真实存在的表和字段，绝不臆造表名/字段名
- 不要设计需要多步计算或外部数据的卡片
- 卡片之间不要重复（同一维度同一指标只出现一次）"""

_PLAN_PROMPT = """## 用户需求
{query}

## 可用数据（schema）
{schema}

请设计看板卡片。只输出 JSON。"""

_INSIGHT_SYSTEM = """你是数据分析师。为下列每张看板卡片各写一句洞察。

要求：
1. 结论先行，必须带上具体数字（不要只说"有所增长"）
2. 每句 30 字以内，中文
3. 只描述数据本身呈现的事实，不要编造业务原因
4. 数据为空或明显异常时，如实说明

只输出 JSON：{{"insights": ["洞察1", "洞察2", ...]}}"""

_INSIGHT_PROMPT = """## 分析主题
{query}

## 各卡片数据
{cards}

请按顺序为每张卡片输出一句洞察。只输出 JSON。"""


def _make_llm_safe(temp=0.0, max_tokens=800, json_mode=True):
    try:
        from agent.llm_service import _make_llm
        return _make_llm(temp=temp, max_tokens=max_tokens, json_mode=json_mode)
    except Exception:
        return None


def _loads(text):
    try:
        from agent.llm_service import _loads_lenient
        return _loads_lenient(text)
    except Exception:
        return None


def plan_dashboard(query: str, schema: str = "", max_cards: int = 6) -> dict:
    """LLM 规划看板卡片（只出规划，不取数）。返回 {success, title, cards, error}。"""
    llm = _make_llm_safe(max_tokens=1200)
    if llm is None:
        return {"success": False, "title": "", "cards": [], "error": "LLM 不可用"}

    if not schema:
        try:
            from agent.investigator import _schema_for
            schema = _schema_for(query)
        except Exception:
            schema = ""

    try:
        raw = str(llm.invoke([
            SystemMessage(content=_PLAN_SYSTEM),
            SystemMessage(content=_PLAN_PROMPT.format(query=(query or "")[:300],
                                                      schema=(schema or "")[:6000])),
        ]).content or "")
    except Exception as e:
        return {"success": False, "title": "", "cards": [], "error": "看板规划失败: %s" % e}

    data = _loads(raw)
    cards = (data or {}).get("cards") if isinstance(data, dict) else None
    if not cards:
        return {"success": False, "title": "", "cards": [], "error": "看板规划返回格式异常"}

    cleaned = []
    for c in cards[:max_cards]:
        if not isinstance(c, dict):
            continue
        q = str(c.get("question") or "").strip()
        if not q:
            continue
        span = c.get("span", 1)
        try:
            span = 2 if int(span) >= 2 else 1
        except (TypeError, ValueError):
            span = 1
        cleaned.append({
            "title": str(c.get("title") or q[:12])[:20],
            "question": q[:200],
            "chart_type": str(c.get("chart_type") or "")[:20],
            "span": span,
        })
    if not cleaned:
        return {"success": False, "title": "", "cards": [], "error": "未规划出有效卡片"}
    return {"success": True, "title": str((data or {}).get("title") or query[:16])[:24],
            "cards": cleaned, "error": ""}


def _run_card(card: dict, schema: str, acl) -> dict:
    """执行单张卡片：生成 SQL → 安全执行 → 校正图型。"""
    from agent.investigator import _generate_sql, _safe_execute

    out = dict(card)
    out.update({"sql": "", "sql_source": "", "columns": [], "rows": [],
                "row_count": 0, "chart_type": "table", "error": ""})
    try:
        sql, source = _generate_sql(card["question"], schema)
        if not sql:
            out["error"] = "未能生成查询"
            return out
        # 线程内不继承主线程的 ContextVar，需显式绑定 ACL（否则权限改写会漏）
        if acl is not None:
            try:
                from security.context import set_acl
                set_acl(acl)
            except Exception:
                pass
        res = _safe_execute(sql)
        if not res.get("success"):
            out["error"] = str(res.get("error") or "")[:160]
            return out
        rows = res.get("rows") or []
        cols = res.get("columns") or []
        out.update({"sql": sql, "sql_source": source, "columns": cols,
                    "rows": rows[:200], "row_count": res.get("row_count", len(rows))})
        # 数据形态驱动选图（与问答主流程同一套规则，LLM 的图型建议只作初始 hint）
        try:
            from agent.llm_service import _validate_chart_type
            out["chart_type"] = _validate_chart_type(card.get("chart_type") or "", cols, rows,
                                                     card["question"])
        except Exception:
            out["chart_type"] = card.get("chart_type") or "bar"
    except Exception as e:
        out["error"] = str(e)[:160]
    return out


def _batch_insights(query: str, cards: list[dict]) -> list[str]:
    """一次性为所有卡片配洞察（比逐卡调用省 N-1 次往返）。"""
    llm = _make_llm_safe(max_tokens=600)
    if llm is None:
        return [""] * len(cards)
    brief = []
    for i, c in enumerate(cards):
        if c.get("error"):
            brief.append("#%d %s：取数失败（%s）" % (i, c.get("title"), c["error"][:40]))
            continue
        try:
            from agent.llm_service import _rows_json_capped
            sample = _rows_json_capped((c.get("rows") or [])[:8], 600)[0]
        except Exception:
            sample = "[]"
        brief.append("#%d %s（%d 行，列：%s）\n样例：%s"
                     % (i, c.get("title"), c.get("row_count", 0),
                        "、".join((c.get("columns") or [])[:8]), sample))
    try:
        raw = str(llm.invoke([
            SystemMessage(content=_INSIGHT_SYSTEM),
            SystemMessage(content=_INSIGHT_PROMPT.format(query=(query or "")[:200],
                                                         cards="\n\n".join(brief)[:4000])),
        ]).content or "")
        data = _loads(raw) or {}
        ins = data.get("insights") if isinstance(data, dict) else None
        if isinstance(ins, list):
            out = [str(x)[:120] for x in ins]
            return (out + [""] * len(cards))[:len(cards)]
    except Exception:
        pass
    return [""] * len(cards)


def build_dashboard(query: str, max_cards: int = 6, schema: str = "") -> dict:
    """一键生成看板：规划 → 并发取数 → 校正图型 → 批量洞察 → 返回可渲染结构。

    返回 {success, title, cards:[{title, question, sql, sql_source, columns, rows,
    row_count, chart_type, span, insight, error}], error}
    """
    if not (query or "").strip():
        return {"success": False, "title": "", "cards": [], "error": "问题为空"}

    if not schema:
        try:
            from agent.investigator import _schema_for
            schema = _schema_for(query)
        except Exception:
            schema = ""

    plan = plan_dashboard(query, schema=schema, max_cards=max_cards)
    if not plan.get("success"):
        return plan

    # ACL 在主线程取一次，再传给每个工作线程（ContextVar 不跨线程）
    acl = None
    try:
        from security.context import get_acl
        acl = get_acl()
    except Exception:
        pass

    cards = plan["cards"]
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(cards))) as ex:
        results = list(ex.map(lambda c: _run_card(c, schema, acl), cards))

    insights = _batch_insights(query, results)
    for c, ins in zip(results, insights):
        c["insight"] = ins

    ok_cards = [c for c in results if not c.get("error")]
    return {
        "success": bool(ok_cards),
        "title": plan.get("title") or (query or "")[:16],
        "cards": results,
        "card_count": len(results),
        "ok_count": len(ok_cards),
        "error": "" if ok_cards else "所有卡片取数失败",
    }
