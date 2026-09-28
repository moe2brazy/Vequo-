"""会话状态（P0-4）— 槽位 + 上一轮结构化查询上下文

解决多轮对话里的核心痛点：指代消解（"改成按月""那上个月呢"）目前依赖纯文本
历史 + 提示词暗示，脆弱。这里把上一轮查询的关键信息结构化：

- `SessionState`：槽位（时间范围/时间粒度/排序/取数/指标/维度）+ 上一轮 SQL/表/列
- `extract_slots(query)`：规则化槽位提取（纯函数，可单测，不依赖 LLM）
- `slot_diff(prev, cur)`：上一轮 vs 本轮槽位变化，生成人可读的「变化说明」
- `build_prev_context_block(...)`：结构化上一轮上下文（供 prompt_builder 注入）

v1 仅覆盖高价值、规则清晰的槽位（时间范围、粒度、排序、取数、指标、维度），
避免过度启发式引入噪声；后续可在其上叠加 LLM 槽位纠偏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── 时间粒度 ────────────────────────────────────────────
_GRANULARITY = [
    (r"按\s*小时|每小时", "hour"),
    (r"按\s*天|按\s*日|每天|每日|逐日", "day"),
    (r"按\s*周|每周|星期", "week"),
    (r"按\s*月|每月|月度|按月|月份", "month"),
    (r"按\s*季度|按\s*季|每季度", "quarter"),
    (r"按\s*年|每年|年度", "year"),
]

# ── 时间范围 ────────────────────────────────────────────
_TIME_RANGE = [
    (r"近\s*(\d+)\s*天|最近\s*(\d+)\s*天", "last_N_days"),
    (r"近\s*(\d+)\s*个月|最近\s*(\d+)\s*个月", "last_N_months"),
    (r"上个月|上月", "last_month"),
    (r"这个月|本月", "this_month"),
    (r"昨天|昨日", "yesterday"),
    (r"今天|今日", "today"),
    (r"本周|这周", "this_week"),
    (r"上周", "last_week"),
    (r"今年|本年", "this_year"),
    (r"去年", "last_year"),
    (r"本季度|这个季度", "this_quarter"),
    (r"上个季度|上季度", "last_quarter"),
]

# ── 排序 / 取数 ─────────────────────────────────────────
_ORDER = [
    (r"前\s*(\d+)\s*名|TOP\s*(\d+)|前\s*(\d+)\s*位", "top"),
    (r"最高|最多|排名第一|第一", "desc"),
    (r"最低|最少", "asc"),
    (r"排行|排名|降序|从高到低", "rank"),
]

# ── 指标（与 metric_registry 内建指标对齐）──────────────
_METRICS = [
    "产量", "良率", "不良率", "合格数", "不良数", "库存量",
    "安全库存", "缺货量", "质检不合格率", "工单数", "停机次数", "停机时长",
    "投入量", "产值", "销量", "销售额", "金额",
]


@dataclass
class SessionState:
    slots: dict = field(default_factory=dict)
    last_query: dict = field(default_factory=dict)
    updated_at: float = 0.0


def extract_slots(query: str) -> dict:
    """规则化槽位提取。返回 dict，缺失键即表示「未指定」。"""
    if not query:
        return {}
    q = str(query)
    slots: dict = {}

    # 粒度
    for pat, val in _GRANULARITY:
        if re.search(pat, q):
            slots["granularity"] = val
            break
    # 时间范围
    for pat, val in _TIME_RANGE:
        m = re.search(pat, q)
        if m:
            num = next((g for g in m.groups() if g), None)
            slots["time_range"] = {"key": val, "n": int(num) if num else None}
            break
    # 排序 / 取数
    for pat, val in _ORDER:
        m = re.search(pat, q)
        if m:
            num = next((g for g in m.groups() if g), None)
            slots["order"] = val
            if num:
                slots["limit"] = int(num)
            break
    # 指标（去重：被更长指标覆盖的短指标剔除，如「不良率」不应再命中「良率」）
    raw = [m for m in _METRICS if m in q]
    metrics = [m for m in raw if not any(m != other and m in other for other in raw)]
    if metrics:
        slots["metrics"] = metrics
    # 注：维度（按/各/每 + 名词）中文无词边界、纯规则易产生噪声（如"各产线"被截成整句），
    # 且维度信息已由上一轮「结果列/涉及表」覆盖，故 v1 不提取维度槽位，避免污染 prompt。
    return slots


def slot_diff(prev_slots: dict, cur_slots: dict) -> list[str]:
    """上一轮 vs 本轮槽位变化说明（人可读）。用于引导 LLM 在上一轮 SQL 上精准修改。"""
    notes: list[str] = []
    if not prev_slots:
        return notes

    def _label(k, v):
        if k == "granularity":
            return {"hour": "按小时", "day": "按天", "week": "按周",
                    "month": "按月", "quarter": "按季度", "year": "按年"}.get(v, str(v))
        if k == "time_range":
            return v.get("key") if isinstance(v, dict) else str(v)
        if k == "order":
            return {"top": "取前N", "desc": "降序", "asc": "升序", "rank": "排名"}.get(v, str(v))
        return str(v)

    # 覆盖型槽位：时间范围、粒度、排序、取数
    for k in ("time_range", "granularity", "order", "limit"):
        pv, cv = prev_slots.get(k), cur_slots.get(k)
        if cv and cv != pv:
            if pv:
                notes.append(f"{k} 由「{_label(k, pv)}」改为「{_label(k, cv)}」")
            else:
                notes.append(f"新增 {k}：{_label(k, cv)}")
    # 追加型槽位：维度、指标
    for k in ("dimensions", "metrics"):
        pv = set(prev_slots.get(k) or [])
        cv = set(cur_slots.get(k) or [])
        added = cv - pv
        if added:
            notes.append(f"新增 {k}：{'、'.join(sorted(added))}")
    return notes


def build_prev_context_block(prev_sql: str, prev_tables: list[str], prev_cols: list[str],
                             prev_slots: dict, cur_slots: dict,
                             prev_question: str = "") -> str:
    """结构化上一轮上下文块（供 prompt 注入）。返回空串表示无可用上下文。

    顺序刻意把「变化」放在最前（SQL 之前），保证在 prompt 预算截断时仍保留
    最关键的多轮信号（指代/修改意图）。
    """
    if not prev_sql:
        return ""
    parts: list[str] = []
    if prev_question:
        parts.append(f"上一轮问题: {prev_question}")
    diff = slot_diff(prev_slots, cur_slots)
    if diff:
        parts.append("本次相对上一轮的变化: " + "；".join(diff))
    parts.append(f"上一轮 SQL:\n{prev_sql}")
    if prev_tables:
        parts.append(f"涉及表: {', '.join(t for t in prev_tables if t)}")
    if prev_cols:
        parts.append(f"结果列: {', '.join(prev_cols)}")
    if prev_slots:
        parts.append("上一轮口径: " + "; ".join(
            f"{k}={v.get('key', v) if isinstance(v, dict) else v}" for k, v in prev_slots.items()))
    return "\n".join(parts)
