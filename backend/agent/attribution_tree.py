# -*- coding: utf-8 -*-
"""多指标归因树（P1-1，对标瓴羊 Quick Audience / 瓴羊归因分析）。

## 与现有 attribution.py 的分工
- `attribution.detect_and_attribute`：单指标 × 单维度的**文本归因**（环比降幅 + top 维度贡献）；
- 本模块：**多指标联动**的**树形归因** —— 主指标变动 → 由哪些子指标贡献 → 每个子指标
  再下钻到维度。返回结构化树，供前端折叠渲染，而不是一段文本。

## 关键设计：加性 / 乘性指标必须分开处理
这是归因树最容易做错的地方（瓴羊、GrowingIO 等产品文档都强调了这点）：
- **加性指标**（产量、投入量、不良数、金额…）：可加和。
  父指标 Δ = Σ 子指标 Δ，贡献度 = 子 Δ / 父 Δ，可以直接分解。
- **乘性（比率）指标**（良率、不良率、达成率…）：不可加和。
  良率从 90% 降到 88% 与产量减少 1000 件，两者量纲不同，不能放进同一个加和式里，
  否则贡献度加起来不是 100%，甚至符号相反。

所以树的两类节点分开：
- `kind="additive"`：参与加和分解，带 contribution_pct（各节点加总 = 100%）；
- `kind="ratio"`：作为**解释性因素**单独列出，只报自身变动（pct_change），
  不参与加和，避免量纲污染。

## 确定性约定
全部由 SQL 结果做数值分解，不调用 LLM（避免编造归因）。数据不足 → success=False。
"""

from __future__ import annotations

import logging

from agent.attribution import (
    _date_sort_key,
    _pick_time_col,
    _to_num,
)

_logger = logging.getLogger("attribution_tree")

# 比率型指标的识别线索：列名命中 → 判为比率；否则按取值范围兜底
_RATIO_HINTS = ("率", "占比", "比例", "百分比", "达成", "rate", "ratio", "pct", "percent", "yield")
# 2026-10-03 新增：列名带这些量词后缀 → 是加性计数/累计量，绝不是比率。
# 用于挡住「0/1 计数列因取值落在 [0,1] 被误判为比率型」的坑（见 _is_ratio_metric）。
_COUNT_HINTS = ("次数", "数量", "个数", "台数", "条数", "单数", "人数", "工单数", "不良数",
                "count", "qty", "quantity", "num", "total", "sum", "件数", "批次数")
# 明显不是指标的列（主键 / 外键 / 纯编号），即使数值型也排除
_ID_HINTS = ("id", "编号", "编码", "code", "no", "序号")


def _is_id_col(cl: str) -> bool:
    """ID 列判定（P1 修复）：英文提示词用整词/后缀边界匹配，
    避免 'valid_qty'/'candidate_qty' 等含 'id' 子串的真实指标列被误伤；
    中文提示词（编号/编码/序号）保持子串匹配（'产品编号' 含 '编号'）。"""
    for h in _ID_HINTS:
        if ord(h[0]) > 127:  # 中文：子串匹配
            if h in cl:
                return True
        else:  # 英文：整词 / 下划线前后缀 / 尾部 id
            if cl == h or cl.endswith("_" + h) or cl.startswith(h + "_"):
                return True
            if h == "id" and cl.endswith("id"):
                return True
    return False


def _numeric_cols(columns: list[str], time_col: str | None, rows: list[dict]) -> list[str]:
    """挑出所有可用作指标的数值列（排除时间列与 ID 列）。"""
    out: list[str] = []
    for c in columns:
        if c == time_col:
            continue
        cl = c.lower()
        if _is_id_col(cl) and cl not in ("qty", "quantity"):
            continue
        vals = [_to_num(r.get(c)) for r in rows[:20]]
        nums = [v for v in vals if v is not None]
        if not nums:
            continue
        # 至少一半的采样行能转成数字才认为是数值列
        if len(nums) >= max(1, len(vals) // 2):
            out.append(c)
    return out


def _is_ratio_metric(col: str, rows: list[dict]) -> bool:
    """判断是否为比率型（乘性）指标。"""
    cl = col.lower()
    if any(h in cl for h in _RATIO_HINTS):
        return True
    # 2026-10-03 修复（P1）：0/1 计数列不得判为比率型。
    # 大量注册口径是 0/1 计数（如 `SUM(CASE WHEN is_planned = FALSE THEN 1 ELSE 0 END)`
    # = 非计划停机次数）。按设备分组后多数取值为 0 或 1，全部落在 [0,1] → 原实现
    # 命中「高度可能是比率」→ root_mode 被设成 "avg" → 求和变加权平均。实测：
    #   sum 模式 -> (1.0, 2.0, 1.0)   真实：1 → 2，+100%
    #   avg 模式 -> (0.5, 1.0, 0.5)   产出：0.5 → 1.0（底层从计数变成比率）
    # 且 weight_metric 会挑一个无关加性列当权重，把「次数变化率」算成
    # 「按停机时长加权的次数变化率」。现在：列名带量词后缀 → 直接判为加性。
    if any(h in cl for h in _COUNT_HINTS):
        return False
    vals = [_to_num(r.get(col)) for r in rows[:20]]
    nums = [v for v in vals if v is not None]
    # 取值全部落在 [0,1] 且至少有 3 个样本 → 高度可能是比率
    if len(nums) >= 3 and all(0.0 <= v <= 1.0 for v in nums):
        # 若取值全是整数（0/1 计数形态），不是比率
        if all(float(v).is_integer() for v in nums):
            return False
        return True
    return False


def _periods_sorted(rows: list[dict], time_col: str) -> list[str]:
    """按时间列排序后的期次列表（去重、升序）。"""
    time_of: dict[str, object] = {}
    for r in rows:
        tk = str(r.get(time_col))
        if tk not in time_of:
            time_of[tk] = r.get(time_col)
    return sorted(time_of, key=lambda t: _date_sort_key(time_of.get(t, t)))


def _agg(rows: list[dict], time_col: str, metric: str, mode: str = "sum",
         weight_metric: str | None = None) -> dict[str, float]:
    """按期次汇总某指标。

    - mode="sum"（加性指标）：同期多行**相加**（产量、投入量、金额…）；
    - mode="avg"（比率指标）：同期多行**加权平均**，不能相加。
      加权平均而不是算术平均，是因为各维度行的"基数"不同（L01 产 1000 件、
      L02 产 200 件，两者的良率直接取算术平均会失真）。权重默认取某个加性指标
      （如投入量），没有可用权重时退化为算术平均。
    """
    num: dict[str, float] = {}
    den: dict[str, float] = {}
    for r in rows:
        tk = str(r.get(time_col))
        v = _to_num(r.get(metric))
        if v is None:
            continue
        if mode == "sum":
            num[tk] = num.get(tk, 0.0) + v
            continue
        w = _to_num(r.get(weight_metric)) if weight_metric else None
        w = w if (w is not None and w >= 0) else 1.0
        num[tk] = num.get(tk, 0.0) + v * w
        den[tk] = den.get(tk, 0.0) + w
    if mode == "sum":
        return num
    return {k: (num[k] / den[k] if den.get(k) else 0.0) for k in num}


def _cross_period(rows: list[dict], time_col: str, metric: str,
                  periods: list[str], mode: str = "sum",
                  weight_metric: str | None = None) -> tuple[float, float, float] | None:
    """计算某指标「上期 → 本期」的变动。返回 (prev, cur, delta)，期次不足返回 None。"""
    if len(periods) < 2:
        return None
    per = _agg(rows, time_col, metric, mode=mode, weight_metric=weight_metric)
    p_prev, p_cur = periods[-2], periods[-1]
    prev, cur = per.get(p_prev, 0.0), per.get(p_cur, 0.0)
    return prev, cur, cur - prev


def _pick_root_metric(query: str, metrics: list[str]) -> str:
    """选主指标：优先用户问题里点名的列，否则取第一个（SQL 投影顺序通常是主指标在前）。"""
    q = str(query or "")
    for m in metrics:
        if m and m in q:
            return m
    return metrics[0]


def _dim_contrib(rows: list[dict], time_col: str, metric: str, dim: str,
                 periods: list[str], top_n: int, mode: str = "sum",
                 weight_metric: str | None = None) -> list[dict]:
    """某指标在某维度下的跨期贡献分解（确定性）：逐维度值算 本期-上期。

    比率指标同样不能跨行相加，走加权平均（权重由 weight_metric 指定）。
    """
    if len(periods) < 2:
        return []
    p_prev, p_cur = periods[-2], periods[-1]
    cur_vals: dict[str, float] = {}
    prev_vals: dict[str, float] = {}
    for r in rows:
        tk = str(r.get(time_col))
        if tk not in (p_prev, p_cur):
            continue
        dv = str(r.get(dim) or "未知")
        v = _to_num(r.get(metric)) or 0.0
        if mode == "sum":
            d = cur_vals if tk == p_cur else prev_vals
            d[dv] = d.get(dv, 0.0) + v
        else:
            w = _to_num(r.get(weight_metric)) if weight_metric else None
            w = w if (w is not None and w >= 0) else 1.0
            # 加权平均：分别累计 (值×权重) 与 权重，最后再除
            d = cur_vals if tk == p_cur else prev_vals
            d[dv] = d.get(dv, 0.0) + v * w
            d["__w__" + dv] = d.get("__w__" + dv, 0.0) + w
    if mode != "sum":
        for d in (cur_vals, prev_vals):
            for k in [k for k in d if not k.startswith("__w__")]:
                w = d.pop("__w__" + k, 0.0)
                d[k] = d[k] / w if w else 0.0

    out: list[dict] = []
    # 遍历本期与上期维度值的并集（P1 修复）：上期存在、本期归零的维度
    # 常常正是下降主因，只遍历 cur_vals 会把它漏报。
    for dv in sorted(set(cur_vals) | set(prev_vals)):
        vc = cur_vals.get(dv, 0.0)
        vp = prev_vals.get(dv, 0.0)
        out.append({
            "name": dv,
            "kind": "dimension",
            "level": 2,
            "metric": metric,
            "dim": dim,
            "prev": round(vp, 4),
            "cur": round(vc, 4),
            "delta": round(vc - vp, 4),
            "pct_change": round((vc - vp) / abs(vp) * 100, 2) if vp else None,
        })
    # 下降贡献最大在前（负 delta 升序），即"变动主因"优先
    out.sort(key=lambda x: x["delta"])
    return out[:top_n]


def build_attribution_tree(query: str, sql_result: dict,
                           max_dims: int = 2, top_n: int = 3) -> dict:
    """构建多指标归因树。

    返回 {success, root, text, error}；root 节点结构：
      {name, metric, level, kind, prev, cur, delta, pct_change, contribution_pct, children[]}
    contribution_pct 只对 kind="additive" 的加性节点有意义（同一层加总 = 100%）。
    """
    empty = {"success": False, "root": None, "text": "", "error": ""}
    try:
        columns = sql_result.get("columns") or []
        rows = sql_result.get("rows") or []
        if len(rows) < 2 or len(columns) < 2:
            return {**empty, "error": "数据不足（需要至少 2 行 2 列）"}

        time_col = _pick_time_col(columns, rows)
        if not time_col:
            return {**empty, "error": "结果中没有时间列，无法做跨期归因"}

        metrics = _numeric_cols(columns, time_col, rows)
        if not metrics:
            return {**empty, "error": "结果中没有可用的数值指标列"}

        periods = _periods_sorted(rows, time_col)
        if len(periods) < 2:
            return {**empty, "error": "数据只有 1 期，无法做跨期对比"}

        root_metric = _pick_root_metric(query, metrics)
        root_ratio = _is_ratio_metric(root_metric, rows)
        # 比率指标跨期要用加权平均，权重取一个加性指标（如投入量）：
        # 各维度行基数不同（L01 产 1000 件 / L02 产 200 件），算术平均会失真。
        weight_metric = next((m for m in metrics
                              if m != root_metric and not _is_ratio_metric(m, rows)), None)
        root_mode = "avg" if root_ratio else "sum"
        root_cp = _cross_period(rows, time_col, root_metric, periods,
                                mode=root_mode, weight_metric=weight_metric)
        if root_cp is None or root_cp[0] == 0:
            return {**empty, "error": "主指标上期无基线，无法计算变动"}
        r_prev, r_cur, r_delta = root_cp

        dim_cols = [c for c in columns
                    if c not in (time_col,) and c not in metrics][:max_dims]

        root = {
            "name": root_metric,
            "metric": root_metric,
            "level": 0,
            "kind": "ratio" if root_ratio else "additive",
            "prev": round(r_prev, 4),
            "cur": round(r_cur, 4),
            "delta": round(r_delta, 4),
            "pct_change": round(r_delta / abs(r_prev) * 100, 2),
            "contribution_pct": 100.0,
            "children": [],
        }

        # ── 第一层：其他指标对本指标变动的贡献 ──────────────
        additive_children: list[dict] = []
        ratio_children: list[dict] = []
        for m in metrics:
            if m == root_metric:
                continue
            m_ratio = _is_ratio_metric(m, rows)
            cp = _cross_period(rows, time_col, m, periods,
                               mode=("avg" if m_ratio else "sum"),
                               weight_metric=weight_metric)
            if cp is None:
                continue
            prev, cur, delta = cp
            node = {
                "name": m,
                "metric": m,
                "level": 1,
                "kind": "ratio" if m_ratio else "additive",
                "prev": round(prev, 4),
                "cur": round(cur, 4),
                "delta": round(delta, 4),
                "pct_change": round(delta / abs(prev) * 100, 2) if prev else None,
                "children": [],
            }
            (additive_children if node["kind"] == "additive" else ratio_children).append(node)

        # 加性子节点：贡献度 = 自身变动 / 主指标变动（量纲一致才可加和）
        # 分母用「加性子节点变动的绝对值之和」而非主指标变动，保证贡献度加总 = 100%，
        # 避免因口径差异（如主指标含子节点未覆盖的部分）导致加总不等于 100%。
        denom = sum(abs(c["delta"]) for c in additive_children)
        for c in additive_children:
            # 2026-10-03 修复：原实现用 abs(delta) 算贡献度，导致贡献度全为正——
            # 「不良数上升 200」被标成「正贡献 60%」，符号与业务含义相反
            #（tree_to_text 会渲染成「投入量 -500（贡献 60%）」这种自相矛盾的表述）。
            # 现在分两栏：contribution_pct 带符号（正=推高主指标，负=反向拖累），
            # contribution_abs_pct 是绝对值占比，供前端画条形用，两者加总仍是 100%。
            c["contribution_pct"] = round(c["delta"] / denom * 100, 2) if denom else 0.0
            c["contribution_abs_pct"] = round(abs(c["delta"]) / denom * 100, 2) if denom else 0.0
        # 比率节点不参与加和，明确标记，避免前端把它混进贡献度求和
        for c in ratio_children:
            c["contribution_pct"] = None

        # ── 第二层：每个指标下钻到维度 ────────────────────
        for c in additive_children + ratio_children:
            if not dim_cols:
                break
            for dim in dim_cols:
                c["children"].extend(_dim_contrib(
                    rows, time_col, c["metric"], dim, periods, top_n,
                    mode=("avg" if c["kind"] == "ratio" else "sum"),
                    weight_metric=weight_metric))
        for dim in dim_cols:
            root["children"].extend(_dim_contrib(
                rows, time_col, root_metric, dim, periods, top_n,
                mode=root_mode, weight_metric=weight_metric))

        root["children"] = additive_children + ratio_children + root["children"]

        return {
            "success": True,
            "root": root,
            "text": tree_to_text(root, periods),
            "error": "",
        }
    except Exception as e:
        _logger.warning("多指标归因树构建失败: %s", e)
        return {**empty, "error": str(e)[:120]}


def tree_to_text(root: dict, periods: list[str] | None = None) -> str:
    """把归因树渲染成中文摘要（供无前端渲染的场景 / 日志 / LLM 上下文使用）。"""
    if not root:
        return ""
    label = f"（{periods[-2]} → {periods[-1]}）" if periods and len(periods) >= 2 else ""
    arrow = "下降" if (root.get("delta") or 0) < 0 else "上升"
    lines = [f"{root['name']}{arrow} {abs(root.get('pct_change') or 0):.1f}%{label}"
             f"（{root['prev']:,.0f} → {root['cur']:,.0f}）"]

    additive = [c for c in root.get("children", []) if c.get("kind") == "additive"]
    ratio = [c for c in root.get("children", []) if c.get("kind") == "ratio"]
    dims = [c for c in root.get("children", []) if c.get("kind") == "dimension"]

    if additive:
        # 2026-10-03：contribution_pct 现在带符号（正=推高主指标，负=反向拖累），
        # 故按绝对值占比排序（用户最关心"谁影响最大"），并对负值明确标注方向，
        # 避免出现「投入量 -500（贡献 60%）」这种符号与业务含义相反的表述。
        lines.append("加性指标贡献（可加和，绝对值占比合计 100%；负贡献表示反向拖累）：")
        for c in sorted(additive, key=lambda x: -(x.get("contribution_abs_pct")
                                                   or abs(x.get("contribution_pct") or 0))):
            _cp = c.get("contribution_pct")
            _dir = "（反向拖累）" if (_cp is not None and _cp < 0) else ""
            lines.append(f"· {c['name']}: {c['delta']:+,.0f}（贡献 {_cp}%{_dir}）")
    if ratio:
        lines.append("比率指标变动（不可加和，仅作解释因素）：")
        for c in ratio:
            pc = c.get("pct_change")
            lines.append(f"· {c['name']}: {c['prev']:,.2f} → {c['cur']:,.2f}"
                         + (f"（{pc:+.1f}%）" if pc is not None else ""))
    if dims:
        lines.append("维度分解（变动主因优先）：")
        for c in dims:
            lines.append(f"· {c['dim']}={c['name']}: {c['delta']:+,.0f}"
                         + (f"（{c['pct_change']:+.1f}%）" if c.get("pct_change") is not None else ""))
    return "\n".join(lines)
