"""规则化归因（Phase 6.1）：环比下跌检测 + 维度贡献度分解（纯计算，不依赖 LLM）

场景：查询结果按时间汇总，本期较上期下跌时，自动给出：
  1) 下跌幅度（环比）
  2) 若用户问题或结果带有维度（如各产线），按维度计算贡献度，返回贡献最大的前 3 个维度

设计约束：
- 确定性：全部由 SQL + 数值计算得出，不调用 LLM（避免编造归因）。
- 数据不足（只有 1 期 / 无时间列 / 无下跌）→ 返回空串，不打扰用户。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

_logger = logging.getLogger("attribution")


def _is_date_value(v) -> bool:
    """粗略判断值是否为日期格式（ISO / 中文日期 / MM-DD）"""
    if v is None:
        return False
    s = str(v).strip()
    if not s:
        return False
    if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", s):
        return True
    if re.match(r"^\d{4}年\d{1,2}月", s):
        return True
    if re.match(r"^\d{1,2}[-/]\d{1,2}", s):
        return True
    return False


def _pick_time_col(columns: list[str], rows: list[dict] | None = None) -> str | None:
    """从结果列中挑时间列：列名含时间词 + 值格式验证（避免"年/月/日"单字误伤维度列）。

    防 bug：列名只含"日/月/年"单字时（如维度列"年度目标"）必须再验证取值是否为日期格式，
    否则把维度列当时间列 → 排序错乱 → 误报环比下跌。
    """
    if not columns:
        return None
    rows = rows or []
    # 候选按列名匹配强度分级：强（date/日期/时间/月份/年份）> 弱（单字 年/月/日）
    strong, weak = [], []
    for c in columns:
        cl = c.lower()
        if any(k in cl for k in ("date", "time", "日期", "时间", "月份", "年份", "年月")):
            strong.append(c)
        elif any(k in cl for k in ("年", "月", "日")):
            weak.append(c)

    def _verify(c: str) -> bool:
        vals = [r.get(c) for r in rows[:5] if r.get(c) is not None]
        if not vals:
            return False
        return sum(1 for v in vals if _is_date_value(v)) >= max(1, len(vals) // 2)

    for c in strong:
        if _verify(c):
            return c
    for c in weak:
        if _verify(c):
            return c
    return None


def _pick_metric_col(columns: list[str], time_col: str | None, rows: list[dict] | None = None) -> str | None:
    """挑主指标列：优先「前几行可转 float 的数值列」，再兜底非时间非 id/名称语义列。

    防 bug：仅按列名排除无法识别"良率/产量/占比"等真实指标列的真实类型，
    必须用取值验证——维度列（如"产线"）取值不能转 float，自动跳过。
    """
    rows = rows or []
    # 第一优先：能转 float 的数值列
    for c in columns:
        if c == time_col:
            continue
        cl = c.lower()
        if any(k in cl for k in ("id", "编号", "编码", "名称", "name", "code", "状态", "类型", "类别")):
            continue
        vals = [_to_num(r.get(c)) for r in rows[:5]]
        numeric = [v for v in vals if v is not None]
        if len(numeric) >= max(1, len(vals) // 2):
            return c
    # 兜底：第一个非时间、非明显维度列
    for c in columns:
        if c == time_col:
            continue
        cl = c.lower()
        if any(k in cl for k in ("id", "编号", "编码", "名称", "name", "code", "状态", "类型", "类别")):
            continue
        return c
    return None


def _to_num(v) -> float | None:
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_date_loose(s: str):
    """宽松解析常见日期格式（含非补零 2024-2-1、中文 2024年2月1日、带时间 ISO），
    失败返回 None（调用方回退字符串排序）。不依赖 fromisoformat 的宽松解析（3.11 行为不稳定）。"""
    s2 = s.strip()
    # 中文日期：2024年2月1日 / 2024年2月
    m_cn = re.match(r"^(\d{4})年(\d{1,2})月(?:\s*(\d{1,2})日?)?", s2)
    if m_cn:
        try:
            return datetime(int(m_cn.group(1)), int(m_cn.group(2)), int(m_cn.group(3) or 1))
        except (ValueError, TypeError):
            return None
    # 带时间 ISO：2024-02-01 10:30 或 2024-2-1T10:30
    m_dt = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{1,2})", s2)
    if m_dt:
        try:
            return datetime(int(m_dt.group(1)), int(m_dt.group(2)), int(m_dt.group(3)),
                            int(m_dt.group(4)), int(m_dt.group(5)))
        except (ValueError, TypeError):
            return None
    # 纯日期：2024-02-01 / 2024/2/1 / 2024-2
    m_d = re.match(r"^(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?", s2)
    if m_d:
        try:
            return datetime(int(m_d.group(1)), int(m_d.group(2)), int(m_d.group(3) or 1))
        except (ValueError, TypeError):
            return None
    return None


def _date_sort_key(v) -> str:
    """时间排序键：兼容 ISO / 中文日期 / 非补零日期（2024-2-1）与时间戳。
    纯字符串排序会把 "2024-2-1" 排在 "2024-10-1" 之后 → 环比取错期。
    """
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    s = str(v).strip()
    # 纯整数：8 位 YYYYMMDD / 6 位 YYYYMM 优先按日期解析（P2 修复：
    # 20240101 这类紧凑日期被当时间戳会排到 1970 年，且与同列 ISO 字符串混存时排序错乱）
    if re.fullmatch(r"\d+", s):
        f = float(s)
        if len(s) == 8:
            y, m, d = int(f // 10000), int(f // 100 % 100), int(f % 100)
            if 1900 <= y <= 2199:
                try:
                    return datetime(y, m, d).isoformat()
                except ValueError:
                    pass
        if len(s) == 6:
            y, m = int(f // 100), int(f % 100)
            if 1900 <= y <= 2199:
                try:
                    return datetime(y, m, 1).isoformat()
                except ValueError:
                    pass
        return f"{f:020.6f}"   # 数字时间戳（对齐位数）
    if s.replace(".", "", 1).isdigit():
        return f"{float(s):020.6f}"
    parsed = _parse_date_loose(s)
    if parsed is not None:
        return parsed.isoformat()
    return s


def _sort_rows(rows: list[dict], time_col: str) -> list[dict]:
    """按时间列排序（支持 ISO 日期字符串 / datetime / 时间戳 / 非补零日期）"""
    return sorted(rows, key=lambda r: _date_sort_key(r.get(time_col)))


def detect_and_attribute(query: str, sql_result: dict, matched_tables: list[dict] | None = None) -> str:
    """主入口：检测环比下跌并给出维度归因（纯规则），无信号返回空串。

    Args:
        query: 用户问题
        sql_result: 已执行的结果（含 columns/rows）
        matched_tables: 命中的候选表（用于寻找维度表的字段，可选）
    """
    try:
        columns = sql_result.get("columns") or []
        rows = sql_result.get("rows") or []
        if len(rows) < 2 or len(columns) < 2:
            return ""
        time_col = _pick_time_col(columns, rows)
        # 无时间列 → 没有"期"的概念，环比归因不适用（避免把任意两行当两期误报）
        if not time_col:
            return ""
        metric_col = _pick_metric_col(columns, time_col, rows)
        if not metric_col:
            return ""
        sorted_rows = _sort_rows(rows, time_col)
        # 若结果含维度列（如"各产线的产量趋势" GROUP BY line_id, stat_date），
        # 先按时间列汇总每个时段的指标合计，再取最后两期（否则逐行取会把同一期
        # 的不同维度行误当作两期，环比算错）。
        dim_cols = [c for c in columns if c not in (time_col, metric_col)]
        if dim_cols:
            per_period: dict[str, float] = {}
            time_key_src: dict[str, object] = {}
            for r in sorted_rows:
                tk = str(r.get(time_col))
                per_period[tk] = per_period.get(tk, 0.0) + (_to_num(r.get(metric_col)) or 0.0)
                if tk not in time_key_src:
                    time_key_src[tk] = r.get(time_col)
            ordered = sorted(per_period.items(), key=lambda kv: _date_sort_key(time_key_src.get(kv[0], kv[0])))
            values = [v for _, v in ordered if v is not None]
            period_pairs = [(k, v) for k, v in ordered if v is not None]
        else:
            # 每行只转一次 _to_num（原实现在同一推导里调两次），避免重复解析
            _pairs = [(r, _to_num(r.get(metric_col))) for r in sorted_rows]
            values = [v for _, v in _pairs if v is not None]
            period_pairs = [(str(r.get(time_col)), v) for r, v in _pairs if v is not None]
        if len(values) < 2:
            return ""
        v_prev, v_cur = values[-2], values[-1]
        if v_prev is None or v_cur is None or v_prev == 0:
            return ""
        delta = v_cur - v_prev
        if delta >= 0:
            return ""  # 未下跌，不做归因
        pct = abs(delta) / abs(v_prev) * 100
        period_label = ""
        if len(period_pairs) >= 2:
            period_label = f"（{period_pairs[-2][0]} → {period_pairs[-1][0]}）"
        head = (f"📉 {metric_col} 环比下降 {pct:.1f}%{period_label}，"
                f"从 {v_prev:,.0f} 降至 {v_cur:,.0f}（减少 {abs(delta):,.0f}）。")
        # 维度贡献：若结果含维度列（非时间、非指标），分解下降来源。
        # 优先做「跨期贡献度」：数据含 ≥2 期时，逐维度算 本期-上期 变动量并排序，
        # 负贡献最大者即下降主因（对齐竞品 variance decomposition 思路，纯确定性计算）；
        # 仅单期数据时退回「本期构成」说明。
        lines = [head]
        if dim_cols:
            dim = dim_cols[0]
            cell: dict[tuple[str, str], float] = {}
            time_of: dict[str, object] = {}
            for r in rows:
                tk = str(r.get(time_col))
                dv = str(r.get(dim) or "未知")
                time_of[tk] = r.get(time_col)
                cell[(tk, dv)] = cell.get((tk, dv), 0.0) + (_to_num(r.get(metric_col)) or 0.0)
            periods = sorted({tk for tk, _ in cell}, key=lambda t: _date_sort_key(time_of.get(t, t)))
            if len(periods) >= 2:
                p_prev, p_cur = periods[-2], periods[-1]
                cur_vals = {dv: v for (tk, dv), v in cell.items() if tk == p_cur}
                prev_vals = {dv: v for (tk, dv), v in cell.items() if tk == p_prev}
                # 遍历 prev ∪ cur 的并集：上期存在、本期消失的维度（贡献为负的主因）也要计入，
                # 否则归因会漏掉"某维度本期归零"这类真正的下降主因。
                deltas = [(dv, cur_vals.get(dv, 0.0) - prev_vals.get(dv, 0.0),
                           prev_vals.get(dv, 0.0), cur_vals.get(dv, 0.0))
                          for dv in (prev_vals.keys() | cur_vals.keys())]
                deltas.sort(key=lambda x: x[1])  # 负贡献最大在前（下降主因）
                lines.append(f"按{dim}分解（{p_prev} → {p_cur}，贡献度降序）：")
                for name, d, vp, vc in deltas[:3]:
                    if vp == 0:
                        lines.append(f"· {name}: 本期 {vc:,.0f}（上期无此维度）")
                    elif vc == 0:
                        lines.append(f"· {name}: 本期归零（上期 {vp:,.0f}，变动 {d:+,.0f}）")
                    else:
                        lines.append(f"· {name}: 本期 {vc:,.0f}（上期 {vp:,.0f}，变动 {d:+,.0f}）")
            else:
                lines.append("按维度分解（本期构成）：")
                dim_sum = {}
                for r in rows:
                    key = str(r.get(dim) or "未知")
                    dim_sum[key] = dim_sum.get(key, 0.0) + (_to_num(r.get(metric_col)) or 0.0)
                if dim_sum:
                    dim_total = sum(dim_sum.values())
                    top = sorted(dim_sum.items(), key=lambda x: x[1], reverse=True)[:3]
                    for name, val in top:
                        share = val / dim_total * 100 if dim_total else 0
                        lines.append(f"· {name}: {val:,.0f}（占本期 {share:.1f}%）")
        lines.append("如需进一步定位原因，可按维度（产线/产品/工序等）查看本期与上期对比明细。")
        return "\n".join(lines)
    except Exception as e:
        _logger.warning("归因分析失败，返回空串: %s", e)
        return ""


def llm_explain(query: str, rule_text: str, sql_result: dict, matched_tables: list[dict] | None = None) -> str:
    """在规则归因结果上追加 LLM 生成的归因解读（改造4：规则算贡献 + LLM 写叙述）。

    设计：规则部分（环比幅度、维度贡献度）保持确定性计算不动；LLM 只在规则结果之上
    生成"可能原因 + 建议"的自然语言解读。LLM 不可用/超时 → 原样返回规则文本（降级）。
    """
    if not rule_text:
        return rule_text
    try:
        from agent.llm_service import _make_llm
        from langchain_core.messages import SystemMessage, HumanMessage
        columns = sql_result.get("columns") or []
        rows = sql_result.get("rows") or []
        # 抽样数据做上下文（防超长），只取最近几期 + 维度贡献相关行
        sample_rows = rows[-6:] if len(rows) > 6 else rows
        data_desc = (
            f"列: {', '.join(map(str, columns))}\n"
            f"最近 {len(sample_rows)} 行数据: {sample_rows}"
        )[:1200]
        llm = _make_llm(temp=0.4, max_tokens=600)
        prompt = (
            "你是制造业数据分析专家。基于以下【规则归因结果】和【数据摘要】，"
            "输出一段归因解读，要求：\n"
            "1. 先复述关键下降事实（幅度、时间区间）；\n"
            "2. 按「对下降的贡献程度从大到小」列出 2-3 个因素，优先引用规则结果中的"
            "维度贡献数据（如某产线/产品变动 X）；\n"
            "3. 给出 1 条最可能的业务原因推测与 1 条可执行的改善建议。\n"
            "不要编造数据中没有的事实，不要输出标题和列表编号，"
            "直接输出一段通顺的中文（不超过 150 字）。\n\n"
            f"## 规则归因结果\n{rule_text}\n\n"
            f"## 数据摘要\n{data_desc}\n\n"
            f"## 用户问题\n{query}"
        )
        resp = llm.invoke([SystemMessage(content=prompt), HumanMessage(content=query)])
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        if not text:
            return rule_text
        # 保留规则部分（可信计算），追加 LLM 解读
        return f"{rule_text}\n\n🧠 归因解读：{text}"
    except Exception:
        return rule_text


def attribute_drop_api(query: str, sql_result: dict, matched_tables: list[dict] | None = None) -> dict:
    """API 包装：返回结构化归因结果（规则 + 可选 LLM 解读）"""
    text = detect_and_attribute(query, sql_result, matched_tables)
    if not text:
        return {"success": True, "attribution": "", "has_drop": False}
    # 规则结果上追加 LLM 解读（可选增强，失败自动降级为纯规则）
    text = llm_explain(query, text, sql_result, matched_tables)
    return {"success": True, "attribution": text, "has_drop": True}
