# -*- coding: utf-8 -*-
"""会话级记忆累积（多轮会话加深，对标 ThoughtSpot Spotter 3 conversational memory）。

## 与既有模块的分工
- `session_state`：**单轮**槽位提取（"换成按月"的粒度/时间/排序）；
- `conv_memory`：**上一轮**实体记忆 + 短句继承（"那 L01 呢"）；
- 本模块：**整个会话**的累积状态——解决上面两个覆盖不到的深水区：

  1. **跨轮实体**：第 1 轮查了各产线，第 3 轮问「那 L01 呢」，L01 来自第 1 轮
     结果——单轮只回看上一轮会接不住；
  2. **会话级槽位继承**：第 1 轮「近 7 天产量」、第 2 轮「各产线」（没提时间）、
     第 3 轮「那 L01 呢」——时间范围应该一直生效到第 3 轮（单轮比较会丢）；
  3. **轮次引用**：「上面那个问题」「刚才的结果」→ 明确指向最近轮次。

## 设计
- 纯规则、可单测；LLM 只负责拿到上下文后做最终改写（延续确定性优先）。
- 输入 = 前端回传的 history（assistant 消息带 sql/columns/rows，user 消息带问题）。
"""

from __future__ import annotations

import re

from agent.conv_memory import extract_entities
from agent.session_state import extract_slots

_MAX_ROUNDS = 8


def _extract_sql_from_content(content: str) -> str:
    """从 AI 回复文本里正则捞最后一条 SQL（history 无结构化字段时兜底）。"""
    m = re.search(r'((?:SELECT|WITH)[\s\S]{10,1200}?)(?:\n\n|```|$)', content or "", re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _extract_filters(sql: str) -> dict[str, str]:
    """从 SQL 提取 WHERE 简单等值谓词 {列标识: 值}（如 line_name -> L01）。

    用 sqlglot 确定性解析；只取 `col = '字面量'` 这类简单过滤（维度约束），
    复杂表达式/范围/子查询跳过。解析失败返回空。

    列标识 = 表名.列名（带表前缀时）或列名（无前缀），
    避免多表 JOIN 下 t1.status 与 t2.status 互相覆盖（P1 修复）。
    """
    if not sql:
        return {}
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql)
        where = tree.find(exp.Where)
        if where is None:
            return {}
        filters: dict[str, str] = {}
        for eq in where.find_all(exp.EQ):
            left, right = eq.left, eq.right
            if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
                key = f"{left.table}.{left.name}".lower() if left.table else left.name.lower()
                filters[key] = str(right.this)
        return filters
    except Exception:
        return {}


def accumulate_session(history: list[dict], max_rounds: int = _MAX_ROUNDS) -> dict:
    """遍历整个 history，累积会话级状态。

    返回：
      slots     所有轮槽位合并（后轮覆盖前轮）→ 会话当前生效的 指标/时间/粒度/排序；
      entities  所有轮结果行的维度实体（跨轮合并去重，每列 ≤20 个）；
      turns     [{round, question, sql, cols, rows}] 最近 N 轮（供轮次引用）；
      last_question / last_sql  最近一轮的问题与 SQL（兼容旧逻辑）。
    """
    acc: dict = {"slots": {}, "entities": {}, "turns": [], "filters": {},
                 "last_question": "", "last_sql": ""}
    recent: list[dict] = list(history or [])[-max_rounds:]
    pending_q = ""
    for h in recent:
        role = h.get("role")
        if role == "user":
            pending_q = str(h.get("content") or "").strip()[:120]
            # 明确取消过滤（"全部/所有/不限/整体"）→ 清空会话过滤，避免旧过滤继续污染新查询
            if any(w in pending_q for w in ("全部", "所有", "不限", "取消过滤", "不过滤", "整体", "不要过滤")):
                acc["filters"] = {}
            slots = extract_slots(pending_q)
            for k, v in slots.items():          # 后轮覆盖前轮
                acc["slots"][k] = v
            acc["last_question"] = pending_q
        elif role in ("assistant", "ai"):
            sql = str(h.get("sql") or "").strip()
            if not sql:
                sql = _extract_sql_from_content(str(h.get("content") or ""))
            cols = h.get("columns") if isinstance(h.get("columns"), list) else []
            rows = h.get("rows") if isinstance(h.get("rows"), list) else []
            if sql:
                acc["last_sql"] = sql[:1200]
                # WHERE 过滤条件累积（后轮覆盖前轮）——「那 L01 呢」定的 L01，
                # 下一轮「改成按周看」也应保留（深度：过滤跨轮继承）
                for k, v in _extract_filters(sql).items():
                    acc["filters"][k] = v
                acc["turns"].append({
                    "round": len(acc["turns"]) + 1,
                    "question": pending_q,
                    "sql": sql[:1200],
                    "cols": [str(c) for c in cols[:10]],
                    "rows": [r for r in rows[:30] if isinstance(r, dict)],
                })
                # 跨轮实体合并（保留已有，追加新值）
                ents = extract_entities(rows, cols)
                for col, vals in ents.items():
                    bucket = acc["entities"].setdefault(col, [])
                    for v in vals:
                        if v not in bucket:
                            bucket.append(v)
                    acc["entities"][col] = bucket[:20]
    return acc


def _is_new_topic(query: str, acc: dict) -> bool:
    """本轮是否换了主题（指标与累积会话指标无交集）。

    上下文继承（槽位/过滤）只在「会话延续」时注入——用户明确问了一个与之前
    不同指标的问题（如会话在聊产量，突然问「各产品的不良数量」），旧主题的
    过滤（line_name='L01'）与时间范围（近7天）若继续注入会污染新查询。
    判定规则：本轮提取的指标集与累积指标集都非空且无交集 → 视为新主题。
    """
    try:
        cur = extract_slots(query)
        acc_metrics = set((acc.get("slots") or {}).get("metrics") or [])
        cur_metrics = set(cur.get("metrics") or [])
        if not cur_metrics or not acc_metrics:
            return False  # 无法判定 → 保守继承（短指代/修改句走继承）
        return not (cur_metrics & acc_metrics)  # 无交集 = 新主题
    except Exception:
        return False


def build_session_context(query: str, acc: dict) -> str:
    """基于会话级状态构建注入 prompt 的上下文块（空串 = 无可用多轮上下文）。

    输出三类提示（全部确定性）：
      ① 跨轮实体命中：query 里的词在**任一历史轮**的结果取值中出现；
      ② 会话级槽位继承：当前没提、但会话累积里有（含多轮前设定的）；
      ③ 轮次引用：含「上面/刚才/上次」时，明确指向最近一轮的问题与 SQL。
    """
    parts: list[str] = []
    q = (query or "").strip()
    if not q:
        return ""

    # ① 跨轮实体命中（深度：不只上一轮，而是整个会话的结果实体池）
    hits: list[str] = []
    for col, vals in (acc.get("entities") or {}).items():
        for v in vals:
            if v and len(v) >= 2 and v in q:
                hits.append(f"「{v}」（{col}，来自之前某轮查询结果）")
                break  # 每列最多一条，避免刷屏
    if hits:
        parts.append("会话记忆：用户提到的 " + "、".join(hits)
                     + " 是本次会话之前查询结果里的取值，应沿用会话主题并按这些值过滤。")

    # 主题切换检测：明确问了不同指标 → 旧会话的过滤/时间/指标不再适用（防污染新查询）
    new_topic = _is_new_topic(q, acc)
    if new_topic:
        parts.append("会话记忆：本轮问题切换到了新的分析主题，请忽略之前的指标与过滤条件，"
                     "按本轮提问全新作答。")

    # ② 会话级槽位继承 + ②b 过滤继承：仅「会话延续」时注入（新主题跳过）
    if not new_topic:
        # ② 会话级槽位继承（深度：多轮累积，后轮覆盖前轮的最新状态）
        slots = acc.get("slots") or {}
        inherit: list[str] = []
        if slots.get("metrics"):
            inherit.append("指标保持会话中的「" + "、".join(slots["metrics"]) + "」")
        tr = slots.get("time_range")
        if tr:
            label = tr.get("key") if isinstance(tr, dict) else str(tr)
            inherit.append(f"时间范围沿用会话中的「{label}」")
        if slots.get("granularity"):
            inherit.append(f"时间粒度沿用会话中的「{slots['granularity']}」")
        if slots.get("order"):
            inherit.append(f"排序沿用会话中的「{slots['order']}」")
        if inherit:
            parts.append("会话记忆：本轮为对本次会话的延续（" + "；".join(inherit)
                         + "），基于会话已确定的上下文做针对性修改，不要当成全新问题。")

        # ②b 会话过滤条件继承（深度：WHERE 跨轮保留——"那 L01 呢"定的过滤，下一轮仍生效）
        filters = acc.get("filters") or {}
        if filters:
            # 值做单引号转义（P1 修复）：O'Brien 这类值直接拼接会产出残缺 SQL 文本
            fil_txt = "、".join(f"{k} = '{str(v).replace(chr(39), chr(39)*2)}'"
                                for k, v in filters.items())
            parts.append("会话记忆：本次会话已确定的过滤条件为「" + fil_txt
                         + "」，除非本轮明确修改，否则继续保留这些过滤。")

    # ③ 轮次引用：「上面/刚才/上次」→ 明确指向最近一轮的问题与 SQL
    if any(w in q for w in ("上面", "刚才", "上次", "之前", "前面")):
        turns = acc.get("turns") or []
        if turns:
            last = turns[-1]
            q_prev = last.get("question") or ""
            sql_prev = (last.get("sql") or "")[:800]
            if q_prev or sql_prev:
                parts.append("会话记忆：用户引用的是最近一轮的结果——"
                             + (f"问题「{q_prev}」" if q_prev else "最近一次查询")
                             + (f"，其 SQL 为：\n{sql_prev}" if sql_prev else "")
                             + "。请基于该轮做针对性修改。")

    return "\n\n".join(parts)
