# -*- coding: utf-8 -*-
"""会话记忆增强（P1-1，对标 ThoughtSpot Spotter 3 的 conversational memory）。

## 与 session_state.py 的分工
- session_state：**槽位**（时间范围/粒度/排序/指标）提取与变化说明——解决"改成按月"这类
  槽位级修改；
- 本模块：**实体 + 主题 + 继承**记忆——解决槽位覆盖不到的两类多轮难题：
  1. **维度实体指代**："那 L01 呢？"——L01 来自上一轮结果里的产线取值，不是任何槽位；
  2. **会话主题继承**："再看看不良"（短句、无新指标/维度）——主题（产量、各产线）要从
     会话上下文继承，而不是当新问题重新理解。

## 设计原则
- **确定性优先**：实体提取（非数值/非时间列的去重取值）、实体命中（子串匹配）、
  短句判定（长度 + 指代词）都是纯规则，可单测；LLM 只负责在拿到上下文后做最终改写。
- 上一轮结果行由前端回传（history 里带 rows，封顶 30 行），后端不额外查库。
"""

from __future__ import annotations

import re

# 指代型短句信号词（与"那X呢"句式互补）：
# 含"换/改"类修改词（换成按月/改成按周）——短句 + 修改词 = 对上一轮的延续
_ANAPHORA_WORDS = ("那", "这个", "那个", "它", "上面", "刚才", "同上", "同样", "也", "再",
                   "分别", "换成", "改成", "改为", "换", "改", "看看", "看")
# 新问题通常出现的**强主题指示词**——命中说明是全新话题，不做继承理解。
# 注意：不含"按"（按周/按月/按产线常出现在修改句里，如"换成按月"），
# 不含"是"（"是什么"是询问句式，但"那是什么原因"仍需继承主题）。
_TOPIC_WORDS = ("各", "每", "统计", "分析", "对比", "排行", "排名", "多少", "哪些", "为什么", "哪个")

_NUM_RE = re.compile(r"^[-+]?\d+(\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?|\d{1,2}[-/]\d{1,2}$|\d{4}年\d{1,2}月")


def _is_num(v) -> bool:
    return bool(_NUM_RE.match(str(v).strip()))


def _is_date(v) -> bool:
    return bool(_DATE_RE.search(str(v).strip()))


def extract_entities(rows: list[dict] | None, columns: list[str] | None,
                     max_per_col: int = 20) -> dict[str, list[str]]:
    """从上一轮结果提取维度实体：{列名: [取值...]}。数值列/时间列排除。

    例：上轮结果 [{"产线": "L01", "产量": 100}, ...] → {"产线": ["L01", "L02", ...]}
    用户说"那 L01 呢"时，命中实体 L01，就能把它解释为"上一轮结果里的产线取值"。
    """
    rows = rows or []
    columns = columns or []
    if not rows or not columns:
        return {}
    cat_cols: list[str] = []
    for c in columns:
        vals = [r.get(c) for r in rows[:30]]
        non_null = [v for v in vals if v is not None and str(v).strip() != ""]
        if not non_null:
            continue
        # 全部数值 → 度量列；全部日期 → 时间列；混合 → 保守跳过
        if all(_is_num(v) for v in non_null):
            continue
        if all(_is_date(v) for v in non_null):
            continue
        # 2026-10-03 修复（P0）：**混合列（原注释说"保守跳过"，代码却 append）**。
        # 部分行数值 + 部分行文本的列是生产常态（前端把 NULL 显示成 0、或上游口径
        # 变更留下的历史脏值）。它一旦被当维度列，entity_hits 会把该列里的**数值**
        # 当成用户提到的维度取值写进 prompt：
        #   entities={'产线': ['L01', '5']} + 问句「良率大于5的产线」
        #   → 命中 ('产线', '5') → prompt 注入「用户提到的『5』（产线）来自上一轮…」
        #   → LLM 很可能生成 WHERE line_name = '5' → 0 行或无关数据。
        # 这里按注释的原意补上：含任何数值的列都不作为维度列。
        if any(_is_num(v) for v in non_null):
            continue
        cat_cols.append(c)
        if len(cat_cols) >= 3:
            break
    out: dict[str, list[str]] = {}
    for c in cat_cols:
        seen: list[str] = []
        for r in rows:
            v = str(r.get(c) or "").strip()
            if v and v not in seen:
                seen.append(v)
            if len(seen) >= max_per_col:
                break
        if seen:
            out[c] = seen
    return out


def entity_hits(query: str, entities: dict[str, list[str]]) -> list[str]:
    """本轮问题里命中的上一轮实体（如「L01」→ 产线取值）。

    返回 ["「L01」（产线）", ...]，用于生成"用户提到的是上轮结果的维度值"提示。
    """
    q = query or ""
    hits: list[str] = []
    for col, vals in entities.items():
        for v in vals:
            # 2026-10-03 修复（P0）：`len(v) >= 1` 太松 —— 任何单字符都能命中，
            # 而问句里的单字符通常是阈值/序号（「良率大于5」「取前3行」）而不是维度值。
            # 收紧为 >= 2，与 `conversation_memory.py:155` 的口径对齐；
            # 并额外排除纯数字（即便上游漏了混合列，这里再加一道闸）。
            if not v or len(v) < 2 or v.isdigit():
                continue
            if v in q:
                hits.append(f"「{v}」（{col}）")
                break  # 每列最多一条，避免同一列多个取值刷屏
    return hits


def is_anaphora_short(query: str, has_prev: bool = True) -> bool:
    """是否指代型短句（"那L01呢"/"再看看不良"/"换成按月"）。

    判据：短（≤20 字）+ 命中指代词 或 "那X呢"句式，且不含明确的新主题指示词。
    长句或含 各/按/每/统计 等指示词 → 大概率是新问题，不按继承处理。
    """
    q = (query or "").strip()
    if not q or not has_prev:
        return False
    if len(q) > 20:
        return False
    # 含强主题指示词 → 是全新话题（即使同时有指代词），不做继承理解
    if any(w in q for w in _TOPIC_WORDS):
        return False
    if any(w in q for w in _ANAPHORA_WORDS):
        return True
    if re.search(r"^那?.{0,8}呢$", q):
        return True
    return False


def build_conv_context(query: str, prev_question: str = "", prev_sql: str = "",
                       prev_rows: list[dict] | None = None,
                       prev_cols: list[str] | None = None,
                       prev_slots: dict | None = None,
                       cur_slots: dict | None = None) -> str:
    """构建多轮记忆上下文块（在 session_state 的槽位变化之上叠加实体/主题记忆）。

    返回文本，空串表示无可用多轮上下文。注入位置：prompt 的 prev_context。
    """
    parts: list[str] = []
    prev_slots = prev_slots or {}
    cur_slots = cur_slots or {}

    # ① 实体记忆：上轮结果里的维度值，本轮命中 → 明确是"上轮结果的取值"
    entities = extract_entities(prev_rows, prev_cols)
    hits = entity_hits(query, entities)
    if hits:
        parts.append("会话记忆：用户提到的 " + "、".join(hits)
                     + " 来自上一轮查询结果的维度取值，本次应沿用上一轮主题并按这些值过滤。")

    # ② 继承判定：短句继承（无新主题指示词）
    if is_anaphora_short(query):
        inherit = []
        if not cur_slots.get("metrics") and prev_slots.get("metrics"):
            inherit.append("指标继承上一轮（" + "、".join(prev_slots["metrics"]) + "）")
        if not cur_slots.get("granularity") and prev_slots.get("granularity"):
            inherit.append("时间粒度沿用上一轮")
        if not cur_slots.get("time_range") and prev_slots.get("time_range"):
            tr = prev_slots["time_range"]
            label = tr.get("key") if isinstance(tr, dict) else str(tr)
            inherit.append(f"时间范围沿用上一轮（{label}）")
        if inherit:
            parts.append("会话记忆：本轮为对上一轮查询的延续/修改（" + "；".join(inherit)
                         + "），请基于上一轮 SQL 做针对性修改，而不是重新理解成一个全新问题。")

    return "\n".join(parts)
