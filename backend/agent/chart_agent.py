"""Chart Agent — 根据查询结果生成多种图表"""

import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.font_manager import FontProperties

_zh_font = None
_COLORS = ["#3b82f6", "#ef4444", "#10b981", "#f59e0b", "#8b5cf6", "#06b6d4", "#ec4899", "#f97316", "#84cc16", "#14b8a6"]


def _get_zh_font():
    global _zh_font
    if _zh_font is not None:
        return _zh_font
    for path in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc"]:
        import os
        if os.path.exists(path):
            _zh_font = FontProperties(fname=path)
            return _zh_font
    _zh_font = FontProperties()
    return _zh_font


def _fig_to_svg(fig) -> str:
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="svg", bbox_inches="tight", transparent=True)
        buf.seek(0)
        svg = buf.read().decode("utf-8")
        if svg.startswith("<?xml"):
            svg = svg[svg.index("<svg"):]
        return svg
    finally:
        # savefig 异常也关闭 fig，避免 matplotlib 资源泄漏
        plt.close(fig)


def _get_cats(rows, columns, limit=15):
    # 与 _get_vals 保持一致：用 .get 防御缺键，避免 rows 结构不一致时 KeyError 导致整图失败
    key = columns[0] if columns else ""
    return [str(row.get(key, "") or "") for row in rows[:limit]]


def _get_vals(rows, col, limit=15):
    """取数值列的值。与 _numeric_cols 的清洗逻辑保持一致：千分位逗号剥掉、非数字降级为 0。

    否则「1,234」这类带逗号的数字串会被 _numeric_cols 判定为数值列，但这里 float() 直接
    抛 ValueError，导致整张图返回 error。
    """
    vals = []
    for row in rows[:limit]:
        raw = row.get(col, 0) or 0
        try:
            vals.append(float(str(raw).replace(",", "")))
        except (ValueError, TypeError):
            vals.append(0.0)
    return vals


def _numeric_cols(columns, rows, limit=5):
    """筛选数值列：跳过字符串/维度列（如 process_name），只保留能转 float 的列。

    修复点：
    1) 空值/None 不参与判定（避免整列 None 被 float(0) 误判为数值列）；
    2) 全 0 / 全空列不算数值列（避免把布尔/占位列当指标列）。
    """
    num_cols = []
    for col in columns[1:]:
        vals = []
        is_num = True
        for row in rows[:limit]:
            v = row.get(col)
            if v is None or str(v).strip() == "":
                continue  # 空值跳过，不判定为文本
            try:
                # 千分位清洗："1,234" → 1234（带逗号的数字串 float() 会失败导致整图 error）
                vals.append(float(str(v).replace(",", "")))
            except (ValueError, TypeError):
                is_num = False
                break
        if is_num and vals and any(x != 0 for x in vals):
            num_cols.append(col)
    return num_cols


# ── 柱状图 ──────────────────────────────────────────────
def _bar_chart(columns, rows):
    if len(columns) < 2: return ""
    font = _get_zh_font()
    cats = _get_cats(rows, columns)
    num_cols = _numeric_cols(columns, rows)
    if not num_cols: return ""
    fig, ax = plt.subplots(figsize=(max(6, len(cats) * 0.5), 4))
    x = range(len(cats))
    width = 0.8 / len(num_cols) if len(num_cols) > 1 else 0.6
    for i, col in enumerate(num_cols):
        vals = _get_vals(rows, col)
        ax.bar([xi + i * width for xi in x], vals, width, label=col, color=_COLORS[i % 10])
    ax.set_xticks([xi + width * (len(num_cols) - 1) / 2 for xi in x])
    ax.set_xticklabels(cats, fontproperties=font, rotation=30, ha="right", fontsize=9)
    if len(num_cols) > 1: ax.legend(prop=font, fontsize=9)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    return _fig_to_svg(fig)


# ── 横向柱状图 ──────────────────────────────────────────
def _barh_chart(columns, rows):
    if len(columns) < 2: return ""
    font = _get_zh_font()
    cats = _get_cats(rows, columns)
    num_cols = _numeric_cols(columns, rows)
    if not num_cols: return ""
    fig, ax = plt.subplots(figsize=(6, max(3, len(cats) * 0.4)))
    y = range(len(cats))
    height = 0.7 / len(num_cols) if len(num_cols) > 1 else 0.5
    for i, col in enumerate(num_cols):
        vals = _get_vals(rows, col)
        ax.barh([yi + i * height for yi in y], vals, height, label=col, color=_COLORS[i % 10])
    ax.set_yticks([yi + height * (len(num_cols) - 1) / 2 for yi in y])
    ax.set_yticklabels(cats, fontproperties=font, fontsize=10)
    ax.invert_yaxis()
    if len(num_cols) > 1: ax.legend(prop=font, fontsize=9)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    return _fig_to_svg(fig)


# ── 堆叠柱状图 ──────────────────────────────────────────
def _stacked_bar_chart(columns, rows):
    if len(columns) < 2: return ""
    font = _get_zh_font()
    cats = _get_cats(rows, columns)
    num_cols = _numeric_cols(columns, rows)
    if not num_cols: return ""
    fig, ax = plt.subplots(figsize=(max(6, len(cats) * 0.5), 4))
    x = range(len(cats))
    bottom = np.zeros(len(cats))
    for i, col in enumerate(num_cols):
        vals = _get_vals(rows, col)
        ax.bar(x, vals, 0.6, bottom=bottom, label=col, color=_COLORS[i % 10])
        bottom += np.array(vals)
    ax.set_xticks(x)
    ax.set_xticklabels(cats, fontproperties=font, rotation=30, ha="right", fontsize=9)
    ax.legend(prop=font, fontsize=9)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    return _fig_to_svg(fig)


# ── 折线图 ──────────────────────────────────────────────
def _line_chart(columns, rows):
    if len(columns) < 2: return ""
    font = _get_zh_font()
    cats = _get_cats(rows, columns, 30)
    num_cols = _numeric_cols(columns, rows)
    if not num_cols: return ""
    fig, ax = plt.subplots(figsize=(max(6, len(cats) * 0.3), 4))
    x = range(len(cats))
    for i, col in enumerate(num_cols):
        vals = _get_vals(rows, col, 30)
        ax.plot(x, vals, marker="o", color=_COLORS[i % 10], linewidth=2, markersize=4, label=col)
    ax.set_xticks(x)
    ax.set_xticklabels(cats, fontproperties=font, rotation=30, ha="right", fontsize=9)
    ax.legend(prop=font, fontsize=9)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.3)
    return _fig_to_svg(fig)


# ── 面积图 ──────────────────────────────────────────────
def _area_chart(columns, rows):
    if len(columns) < 2: return ""
    font = _get_zh_font()
    cats = _get_cats(rows, columns, 30)
    num_cols = _numeric_cols(columns, rows)
    if not num_cols: return ""
    fig, ax = plt.subplots(figsize=(max(6, len(cats) * 0.3), 4))
    x = range(len(cats))
    for i, col in enumerate(num_cols):
        vals = _get_vals(rows, col, 30)
        ax.fill_between(x, vals, alpha=0.3, color=_COLORS[i % 10], label=col)
        ax.plot(x, vals, color=_COLORS[i % 10], linewidth=2)
    ax.set_xticks(x)
    ax.set_xticklabels(cats, fontproperties=font, rotation=30, ha="right", fontsize=9)
    ax.legend(prop=font, fontsize=9)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.3)
    return _fig_to_svg(fig)


# ── 饼图 ────────────────────────────────────────────────
def _pie_chart(columns, rows):
    if len(columns) < 2: return ""
    font = _get_zh_font()
    labels = _get_cats(rows, columns, 10)
    num_cols = _numeric_cols(columns, rows)
    if not num_cols: return ""
    vals = _get_vals(rows, num_cols[0], 10)
    if sum(abs(v) for v in vals) == 0:
        return ""  # 全 0 数据不画饼图（autopct 百分比除零）
    fig, ax = plt.subplots(figsize=(5, 5))
    wedges, texts, autotexts = ax.pie(vals, labels=labels, autopct="%1.1f%%",
                                       colors=_COLORS[:len(labels)], textprops={"fontproperties": font, "fontsize": 10})
    for t in autotexts: t.set_fontsize(8)
    return _fig_to_svg(fig)


# ── 环形图 ──────────────────────────────────────────────
def _donut_chart(columns, rows):
    if len(columns) < 2: return ""
    font = _get_zh_font()
    labels = _get_cats(rows, columns, 10)
    num_cols = _numeric_cols(columns, rows)
    if not num_cols: return ""
    vals = _get_vals(rows, num_cols[0], 10)
    if sum(abs(v) for v in vals) == 0:
        return ""  # 全 0 数据不画环形图
    fig, ax = plt.subplots(figsize=(5, 5))
    wedges, texts, autotexts = ax.pie(vals, labels=labels, autopct="%1.1f%%",
                                       colors=_COLORS[:len(labels)], pctdistance=0.75,
                                       textprops={"fontproperties": font, "fontsize": 10},
                                       wedgeprops={"width": 0.4, "edgecolor": "white"})
    for t in autotexts: t.set_fontsize(8)
    return _fig_to_svg(fig)


# ── 散点图 ──────────────────────────────────────────────
def _scatter_chart(columns, rows):
    if len(columns) < 2: return ""
    font = _get_zh_font()
    num_cols = _numeric_cols(columns, rows)
    if len(num_cols) < 1: return ""
    fig, ax = plt.subplots(figsize=(5, 5))
    for i, col in enumerate(num_cols):
        # 取两列作 x, y，或单列作索引
        if len(num_cols) >= 2 and i == 0:
            x_vals = _get_vals(rows, num_cols[0], 50)
            y_vals = _get_vals(rows, num_cols[1], 50)
            ax.scatter(x_vals, y_vals, c=_COLORS[i % 10], s=40, alpha=0.7, label=num_cols[0] + " vs " + num_cols[1])
            ax.set_xlabel(num_cols[0], fontproperties=font)
            ax.set_ylabel(num_cols[1], fontproperties=font)
            break
        else:
            x_vals = list(range(len(rows[:50])))
            y_vals = _get_vals(rows, col, 50)
            ax.scatter(x_vals, y_vals, c=_COLORS[i % 10], s=30, alpha=0.7, label=col)
    ax.legend(prop=font, fontsize=9)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.grid(alpha=0.3)
    return _fig_to_svg(fig)


# ── AntV G2Plot 承接的扩展图表类型 ───────────────────────
# 前端采用「Apache ECharts + AntV G2Plot」双引擎：ECharts 画常规图，
# 下面这些类型交给 G2Plot 渲染（表现力更强或 ECharts 未注册）。
# 后端 matplotlib 无法画这些图，但仍要产出「视觉语义最接近」的 SVG 作为兜底
# （G2Plot 懒加载失败 / 数据不合规时前端回退显示，保证结果不空白）。
ANTV_TYPES = {
    "radar", "funnel", "gauge", "liquid", "rose", "heatmap",
    "sunburst", "treemap", "waterfall", "sankey", "box", "histogram",
    "radial-bar", "ring-progress", "bullet", "wordcloud",
}

# AntV 类型 → matplotlib 近似兜底图；None 表示无合理近似（前端回退表格展示）
ANTV_SVG_FALLBACK = {
    "radar": "bar",
    "funnel": "barh",
    "rose": "pie",
    "heatmap": "bar",
    "waterfall": "bar",
    "sunburst": "pie",
    "treemap": "pie",
    "radial-bar": "barh",
    "histogram": "bar",
    "box": "bar",
    "bullet": "bar",
    "wordcloud": "bar",
    "gauge": None,
    "liquid": None,
    "ring-progress": None,
    "sankey": None,
}


def _antv_chart(chart_type: str, columns: list, rows: list) -> dict:
    """AntV 扩展图表：类型原样返回（前端按类型路由到 G2Plot），SVG 用近似图兜底"""
    fb = ANTV_SVG_FALLBACK.get(chart_type)
    svg = ""
    if fb:
        try:
            svg = CHART_FUNCS[fb](columns, rows)
        except Exception:
            svg = ""
    return {"type": chart_type, "svg": svg}


# ── Generate ─────────────────────────────────────────────
CHART_FUNCS = {
    "bar": _bar_chart,
    "barh": _barh_chart,
    "stacked": _stacked_bar_chart,
    "line": _line_chart,
    "area": _area_chart,
    "pie": _pie_chart,
    "donut": _donut_chart,
    "scatter": _scatter_chart,
}

CHART_AUTO = {
    "趋势": "line", "日": "line", "月": "line", "走势": "line", "时间": "line",
    "占比": "pie", "分布": "donut", "组成": "pie", "比例": "pie",
    "排行": "barh", "排名": "barh", "top": "barh", "最高": "barh", "最低": "barh",
    "相关": "scatter", "关联": "scatter",
}


def generate_chart(columns: list[str], rows: list[dict], query_hint: str = "", force_type: str = "") -> dict:
    """
    chart types: bar | barh | stacked | line | area | pie | donut | scatter
    """
    if not rows or len(rows) < 2 or len(columns) < 2:
        return {"type": "none", "svg": ""}

    query_lower = query_hint.lower()

    # 数值列检测：与内部 _numeric_cols 保持同一判定（空值跳过、全 0/全空列不算数值列），
    # 避免顶层宽松判定选出列、内部画图函数又判定为空 → 输出空白 SVG。
    if not _numeric_cols(columns, rows):
        return {"type": "none", "svg": ""}

    # AntV 扩展类型：交给前端 G2Plot 渲染，此处只产近似 SVG 兜底
    ft = (force_type or "").lower()
    if ft in ANTV_TYPES:
        return _antv_chart(ft, columns, rows)

    # 决策
    if force_type in CHART_FUNCS:
        chart_type = force_type
    else:
        chart_type = "bar"
        for kw, ct in CHART_AUTO.items():
            if kw in query_lower:
                chart_type = ct
                break

    try:
        func = CHART_FUNCS.get(chart_type)
        if not func: return {"type": "none", "svg": ""}
        svg = func(columns, rows)
        return {"type": chart_type, "svg": svg}
    except Exception as e:
        return {"type": "error", "svg": "", "error": str(e)}
