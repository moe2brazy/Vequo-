# -*- coding: utf-8 -*-
"""分析模板一键执行器（确定性管线）。

模板 = 编排脚本（questions[]）。执行时逐题走「指标语义层 + 确定性编译」：
    try_compile_metric(q) → execute_sql(sql) → chart_agent.generate_chart → 规则结论
LLM 不参与 —— 演示 100% 稳定可复现（符合「确定性编译为主」架构红线）。
编译未命中的步骤标记 skip（该问法需 AI 分析），建议到问数页手动执行，
不阻断其余步骤。执行产物为单文件 HTML 报告（内嵌图表 SVG + 表格 + 结论 + SQL），
可直接在新标签页打开 / 下载，无需静态服务。

与竞品报告模板的区别（方案依据）：
- Power BI/Quick BI 的模板是「固定报表 + LLM 直出 SQL」；本执行器每步都命中
  指标注册表确定性口径，数值可复核、评测可复现 —— 评审演示稳定性卖点。
"""
from __future__ import annotations

import html as _html
import time
from datetime import datetime
from typing import Any, Dict, List

# 报告内单步最多展示的行/列（报告紧凑可读）
_MAX_ROWS = 20
_MAX_COLS = 6


def _esc(v: Any) -> str:
    return _html.escape("" if v is None else str(v))


def _is_rate(col: str) -> bool:
    """率/占比/平均类列：值本身是百分比或均值，不适合按合计算占比。"""
    return any(k in col for k in ("率", "占比", "平均", "均值", "时长", "%"))


def _fmt(v: Any, col: str = "") -> str:
    """数值格式化：率/均值列保留 2 位小数；整数列原样；其余最多 2 位小数。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            f = float(v)
        except Exception:
            return _esc(v)
        if _is_rate(col):
            return f"{f:.2f}"
        if abs(f - round(f)) < 1e-9 and abs(f) < 1e15:
            return str(int(f))
        return f"{f:.2f}"
    return _esc(v)


def _conclusion(columns: List[str], rows: List[dict]) -> str:
    """根据结果形态生成一句话规则结论（不依赖 LLM）。

    支持：单值总量 / 趋势(首列日期) / 分组排行(取 Top1-2；量列附合计占比)。
    """
    if not rows or not columns:
        return "当前条件下没有数据（正常结果：本周期无此类记录）。"
    try:
        vals: List[float] = []
        for r in rows:
            v = r.get(columns[-1])
            vals.append(float(v) if v not in (None, "") else 0.0)
        total = sum(vals)
    except Exception:
        vals = [0.0] * len(rows)
        total = 0.0

    last_col = columns[-1]
    if len(columns) == 1:
        return f"{_esc(columns[0])} = {_fmt(rows[0].get(columns[0]), columns[0])}"
    if len(rows) == 1:
        row = rows[0]
        return "、".join(f"{_esc(c)} = {_fmt(row.get(c), c)}" for c in columns)

    first_col = columns[0]
    # 趋势：首列为日期/周/月 → 给期初-期末变化
    if any(k in first_col for k in ("日期", "时间", "月份", "周", "年")):
        first, last = rows[0], rows[-1]
        try:
            fv, lv = float(first.get(last_col) or 0), float(last.get(last_col) or 0)
            delta = lv - fv
            trend = "上升" if delta > 0 else ("下降" if delta < 0 else "持平")
            return (f"{_esc(first_col)}从 {_esc(first.get(first_col))}（{_fmt(fv, last_col)}）到 "
                    f"{_esc(last.get(first_col))}（{_fmt(lv, last_col)}），整体{trend}"
                    + (f" {_fmt(abs(delta), last_col)}" if delta else ""))
        except Exception:
            pass
    # 排行/分组：Top1 与 Top2（并列可容忍）
    # 2026-10-03 修复（P1）：原实现 `float(r.get(last_col) or 0)` 无兜底，而同文件
    # :412-429 会主动把布尔列转成中文字符串（is_planned → 「计划内/计划外」）。
    # 一旦该列落在**最后一列**（SELECT equipment_name, is_planned … 这类分组查询即会），
    # float("计划内") 直接 ValueError，且 run_template_report 无异常捕获 →
    # **整份模板报告 500**（设备停机类模板命中 eqp_downtime_record 极易触发）。
    # 现在统一走 _sort_num：非数值行排到末尾而不是让整份报告崩掉。
    def _sort_num(r):
        v = r.get(last_col)
        try:
            return float(v) if v not in (None, "") else float("-inf")
        except (TypeError, ValueError):
            return float("-inf")

    top = sorted(rows, key=_sort_num, reverse=True)
    t1, t2 = top[0], top[1] if len(top) > 1 else None
    v1 = _fmt(t1.get(last_col), last_col)
    share = (float(t1.get(last_col) or 0) / total * 100) if total else 0.0
    is_amount = not _is_rate(last_col)   # 量列才给占比
    base = f"排名第一的{_esc(first_col)}是「{_esc(t1.get(first_col))}」（{v1}"
    if is_amount:
        base += f"，占 {share:.1f}%"
    base += "）"
    if t2 is not None:
        v2 = _fmt(t2.get(last_col), last_col)
        share2 = (float(t2.get(last_col) or 0) / total * 100) if total else 0.0
        base += f"；其次为「{_esc(t2.get(first_col))}」（{v2}"
        if is_amount:
            base += f"，{share2:.1f}%"
        base += "）"
    if is_amount:
        base += f"。共 {len(rows)} 组，合计 {_fmt(total, last_col)}。"
    return base


def _render_table(columns: List[str], rows: List[dict]) -> str:
    cols = columns[:_MAX_COLS]
    heads = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    trs = []
    for r in rows[:_MAX_ROWS]:
        tds = "".join(f"<td>{_fmt(r.get(c), c)}</td>" for c in cols)
        trs.append(f"<tr>{tds}</tr>")
    more = f'<tr class="more"><td colspan="{len(cols)}">… 共 {len(rows)} 行（仅展示前 {min(len(rows), _MAX_ROWS)} 行）</td></tr>' \
        if len(rows) > _MAX_ROWS else ""
    return (f'<table><thead><tr>{heads}</tr></thead><tbody>{"".join(trs)}{more}</tbody></table>')


def _step_html(idx: int, st: Dict[str, Any]) -> str:
    q = _esc(st["q"])
    # 模板里的 step「口径说明」：与执行后生成的结论分开渲染，
    # 用来交代该步的统计范围/口径（如「缺陷明细表无时间列，为全量口径」），
    # 避免报告读者把全量口径误读成周期口径。
    hint = _esc(st.get("hint") or "")
    hint_html = f'<p class="hint">{hint}</p>' if hint else ""
    if st.get("status") == "skip":
        # skip_note 有值 = 按权限跳过（说明原因即可）；无值 = 未命中确定性口径，引导到问数页
        note = st.get("skip_note") or f"该步骤需要 AI 综合推理（未命中确定性口径），请到智能问析页执行：{q}"
        return (f'<section class="step skip">'
                f'<h3><span class="no">{idx}</span>{q}</h3>'
                f'{hint_html}'
                f'<p class="note warn">{_esc(note)}</p>'
                f'</section>')
    metric = _esc(st.get("metric") or "")
    unit = _esc(st.get("unit") or "")
    meta = f"指标口径：{metric}" + (f"（{unit}）" if unit else "")
    rows = st.get("rows") or []
    n = len(rows)
    meta += f" ｜ {n} 行"
    svg = st.get("svg") or ""
    parts = []
    if hint_html:
        parts.append(hint_html)
    if st.get("note"):
        parts.append(f'<p class="note">{_esc(st["note"])}</p>')
    if svg:
        parts.append(f'<div class="chart">{svg}</div>')
    if rows:
        parts.append(_render_table(st.get("columns") or [], rows))
    sql = st.get("sql") or ""
    parts.append(
        '<details class="sql"><summary>查看 SQL（确定性编译）</summary>'
        f'<pre>{_esc(sql)}</pre></details>'
    )
    return (f'<section class="step">'
            f'<h3><span class="no">{idx}</span>{q}</h3>'
            f'<p class="meta">{meta}</p>'
            f'{"".join(parts)}</section>')


def _fmt_day(v: Any) -> str:
    """日期 / 时间戳统一截成 YYYY-MM-DD 展示。"""
    s = "" if v is None else str(v)
    return s[:10] if len(s) >= 10 else s


def _period_coverage(period: Dict[str, Any], tables: List[str]) -> Dict[str, Any] | None:
    """检查模板声明的周期在库内的**真实覆盖情况**，供报告给出统计口径注释。

    为什么需要：相对窗口「近N天」在编译器里以 `MAX(时间列)` 为锚点，所以库内数据
    不足 N 天时，查询本身已经「有几天算几天」（不会查空）。真正缺的不是降级取数，
    而是**把真实覆盖区间讲出来** —— 否则 4 天的数据会被当成一整周来汇报。

    ⚠️ 判定「库内够不够一个周期」必须用**全表去重日期数 all_days**，不能用窗口内的
    去重日期数：窗口是 `MAX(时间列) - INTERVAL 'N-1 days'`，对 timestamp 列是一个
    **滚动 N×24 小时**区间，会在首日按时刻切一刀。实测 yans 库：eqp_downtime_record
    的 MAX(start_time)=2026-07-15 19:10 → 窗口起点 2026-07-09 19:10，而 07-09 的
    8 条记录全在 19:10 之前被排除，窗口内只数到 6 个日历日。若拿它当覆盖天数，
    就会对一个数据充足的库误报「数据不足一个完整周期」。

    返回 {label, days, full, lines, tables}；任何异常返回 None（少一条注释不影响报告主体）。
    """
    try:
        from agent.metric_compiler import _FACT_TIME_COL
        from db.executor import execute_sql
    except Exception:
        return None
    try:
        days = int(period.get("days") or 0)
    except Exception:
        return None
    if days <= 0:
        return None
    label = str(period.get("label") or f"近 {days} 天")

    rows: List[Dict[str, Any]] = []
    today = ""
    window = max(0, days - 1)
    for t in dict.fromkeys(tables):          # 去重且保持出现顺序
        col = _FACT_TIME_COL.get(t)
        if not col:
            continue                          # 无时间列的表（如缺陷明细）不参与周期覆盖判定
        # 一条查询同时取：窗口内首条记录、全表末条记录、全表去重天数、窗口内去重天数
        sql = (
            f"SELECT (SELECT MIN({col}) FROM {t} WHERE {col} >= "
            f"    (SELECT MAX({col}) FROM {t}) - INTERVAL '{window} days') AS win_min, "
            f"  (SELECT MAX({col}) FROM {t}) AS win_max, "
            f"  (SELECT COUNT(DISTINCT {col}::date) FROM {t}) AS all_days, "
            f"  (SELECT COUNT(DISTINCT {col}::date) FROM {t} WHERE {col} >= "
            f"    (SELECT MAX({col}) FROM {t}) - INTERVAL '{window} days') AS win_days, "
            f"  CURRENT_DATE AS today"
        )
        res = execute_sql(sql)
        if not (res.get("success") and res.get("rows")):
            continue
        r = dict(res["rows"][0])
        if not r.get("win_max"):
            continue
        today = _fmt_day(r.get("today")) or today
        rows.append({
            "table": t,
            "start": _fmt_day(r.get("win_min")),
            "end": _fmt_day(r.get("win_max")),
            "win_days": int(r.get("win_days") or 0),
            "all_days": int(r.get("all_days") or 0),
        })
    if not rows:
        return None

    # 是否够一个完整周期：看全表去重天数，不看被滚动窗口切过的 win_days
    full = all(r["all_days"] >= days for r in rows)
    lines: List[str] = [f"统计周期：{label}（锚点＝各事实表的数据最新日期）"]
    for r in rows:
        lines.append(f"{r['table']}：{r['start']} ~ {r['end']}，"
                     f"周期内 {r['win_days']} 天有数据（该表全量 {r['all_days']} 天）")
    short = [r for r in rows if r["all_days"] < days]
    if short:
        detail = "、".join(f"{r['table']} 仅 {r['all_days']} 天" for r in short)
        lines.append(f"⚠️ 库内数据不足一个完整周期（{label}）：{detail}；"
                     f"相关指标已按「有几天用几天」的实际天数统计，请据此判断可比性")
    latest = max(r["end"] for r in rows)
    if today and latest and latest < today:
        lines.append(f"⚠️ 数据最新日期为 {latest}，早于系统当前日期 {today}；"
                     f"本报告统计的是数据末期区间，非系统当前周期")
    return {"label": label, "days": days, "full": full, "lines": lines, "tables": rows}


def _render_html(tpl: Dict[str, Any], steps: List[Dict[str, Any]],
                 db_name: str, elapsed: float,
                 period_info: Dict[str, Any] | None = None) -> str:
    name = _esc(tpl.get("name") or "分析报告")
    scene = _esc(tpl.get("scene") or "")
    desc = _esc(tpl.get("desc") or "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    done = sum(1 for s in steps if s.get("status") != "skip")
    skip = len(steps) - done
    bodies = "".join(_step_html(i + 1, s) for i, s in enumerate(steps))
    # 周期口径注释（周报/月报）：把「实际覆盖了几天」写进报告，避免超范围汇报
    period_html = ""
    period_chip = ""
    if period_info and period_info.get("lines"):
        _lis = "".join(f"<li>{_esc(x)}</li>" for x in period_info["lines"])
        period_html = (f'<div class="period"><div class="period-h">📅 统计口径</div>'
                       f'<ul>{_lis}</ul></div>')
        period_chip = f'<div>统计周期 <b>{_esc(period_info["label"])}</b></div>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} · 智能问析</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #f3f1e8; color: #2c2c2a; line-height: 1.65; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 28px 20px 60px; }}
  header {{ background: #fff; border: 1px solid #d5dad1; border-radius: 14px;
           padding: 22px 26px; margin-bottom: 20px; }}
  h1 {{ font-size: 22px; font-weight: 600; }}
  .scene {{ display: inline-block; margin-top: 8px; background: #E6F1FB; color: #0C447C;
           border-radius: 999px; padding: 2px 12px; font-size: 12px; }}
  .desc {{ color: #5F5E5A; margin-top: 10px; font-size: 14px; }}
  .gen {{ color: #888780; margin-top: 8px; font-size: 12px; }}
  .period {{ margin-top: 12px; background: #F7F9F4; border: 1px solid #dfe5d6;
             border-radius: 10px; padding: 10px 14px; font-size: 12.5px; color: #4a4a46; }}
  .period-h {{ font-weight: 600; color: #2c2c2a; margin-bottom: 4px; }}
  .period ul {{ margin: 0; padding-left: 18px; }}
  .period li {{ margin: 2pt 0; }}
  .summary {{ display: flex; gap: 12px; margin: 12px 0 20px; flex-wrap: wrap; }}
  .summary div {{ background: #fff; border: 1px solid #d5dad1; border-radius: 10px;
                 padding: 10px 18px; font-size: 13px; }}
  .summary b {{ font-size: 18px; color: #0F6E56; }}
  section.step {{ background: #fff; border: 1px solid #d5dad1; border-radius: 12px;
                  padding: 16px 20px; margin-bottom: 16px; }}
  section.skip {{ background: #FAEEDA; border-color: #FAC775; }}
  h3 {{ font-size: 15px; font-weight: 600; margin-bottom: 6px; }}
  h3 .no {{ display: inline-block; min-width: 22px; text-align: center; background: #E1F5EE;
            color: #085041; border-radius: 6px; margin-right: 8px; font-size: 13px; }}
  .meta {{ color: #888780; font-size: 12px; margin-bottom: 10px; }}
  p.note {{ background: #EAF3DE; border-left: 3px solid #639922; padding: 8px 12px;
            border-radius: 0 8px 8px 0; font-size: 14px; margin-bottom: 12px; }}
  p.note.warn {{ background: #FAEEDA; border-left-color: #BA7517; }}
  p.hint {{ color: #5F5E5A; font-size: 12.5px; margin: 0 0 10px; }}
  p.hint::before {{ content: "口径说明："; color: #888780; }}
  .chart {{ margin: 10px 0; overflow-x: auto; }}
  .chart svg {{ max-width: 100%; height: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin: 10px 0 6px; }}
  th, td {{ border: 1px solid #e0ddd2; padding: 6px 10px; text-align: left; }}
  th {{ background: #F1EFE8; font-weight: 600; white-space: nowrap; }}
  tr.more td {{ color: #888780; background: #fafaf6; }}
  details.sql {{ margin-top: 10px; }}
  details.sql summary {{ cursor: pointer; font-size: 12px; color: #185FA5;
                          user-select: none; }}
  pre {{ background: #F1EFE8; border-radius: 8px; padding: 10px 12px; font-size: 11.5px;
         overflow-x: auto; color: #444441; margin-top: 6px; }}
  footer {{ color: #888780; text-align: center; font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>{name}</h1>
    <span class="scene">{scene}</span>
    <div class="desc">{desc}</div>
    <div class="gen">生成时间：{now} ｜ 数据源：{_esc(db_name)} ｜ 编译耗时 {elapsed:.1f}s</div>
    {period_html}
  </header>
  <div class="summary">
    <div>分析步骤 <b>{done}/{len(steps)}</b></div>
    <div>AI 推理步骤 <b>{skip}</b></div>
    <div>执行方式 <b>确定性编译</b></div>
    {period_chip}
  </div>
  {bodies}
  <footer>由智能问析「分析模板」一键生成 · 全部指标走注册口径确定性编译，可复核可复现</footer>
</div>
</body>
</html>"""


def run_template_report(tpl: Dict[str, Any], db_name: str = "",
                        allowed_tables: set | None = None) -> Dict[str, Any]:
    """执行模板编排：逐题确定性编译执行，返回 {steps, html, elapsed, period}。

    若模板声明了 `period`（周报/月报这类周期报告），报告头部会附「统计口径」注释：
    说明周期锚点、各事实表在周期内的实际覆盖天数；库内数据不足一个完整周期时，
    按「有几天用几天」统计并在报告中明确标注。

    allowed_tables：当前用户授权表集合（裸名小写；None = 不受限）。编译出取数表后
    逐步骤做 fail-closed 校验 —— 不在授权范围的步骤标记 skip（按权限跳过），与模板
    列表端的按权限裁剪保持同一口径，杜绝受限用户借内置模板越权取数。
    """
    from agent.metric_compiler import try_compile_metric
    from agent.chart_agent import generate_chart
    from db.executor import execute_sql

    if not db_name:
        try:
            from database import get_database_config
            db_name = get_database_config().get("name") or ""
        except Exception:
            db_name = ""

    questions = tpl.get("questions") or []
    if not questions:
        return {"steps": [], "html": "", "elapsed": 0.0, "period": None,
                "error": "该模板未配置可执行的分析步骤（questions）"}

    t0 = time.time()
    steps: List[Dict[str, Any]] = []
    fact_tables: List[str] = []          # 供周期覆盖检查（保持出现顺序、去重）
    for item in questions:
        q = str(item.get("q") or "").strip()
        if not q:
            continue
        st: Dict[str, Any] = {"q": q, "hint": str(item.get("note") or "").strip()}
        compiled = try_compile_metric(q)
        if not compiled:
            st["status"] = "skip"
            steps.append(st)
            continue
        _ft = ((compiled.get("mql") or {}) or {}).get("fact_table")
        # 表级 ACL：受限用户（allowed_tables 非 None）时，取数表不在授权范围
        # （含编译产物缺表信息，无法证实有权）→ 按权限跳过，fail-closed。
        # 不受限用户不做此校验，行为与历史版本一致。
        if allowed_tables is not None and (
                not _ft or str(_ft).split(".")[-1].lower() not in allowed_tables):
            st["status"] = "skip"
            st["skip_note"] = "该步骤的数据表不在当前用户的授权范围，已按权限跳过。"
            steps.append(st)
            continue
        if _ft and _ft not in fact_tables:
            fact_tables.append(_ft)
        st["sql"] = compiled.get("sql") or ""
        st["metric"] = compiled.get("metric") or ""
        st["unit"] = compiled.get("unit") or ""
        res = execute_sql(st["sql"])
        if not res.get("success"):
            st["status"] = "skip"
            st["_reason"] = str(res.get("error") or "执行失败")[:100]
            steps.append(st)
            continue
        columns = [str(c) for c in (res.get("columns") or [])]
        rows = list(res.get("rows") or [])
        # 按报告语义修正列名（is_planned 布尔列 → 计划类型）；rows 的 key 同步改名
        display_cols = [
            "计划类型" if c == "is_planned" else c for c in columns
        ]
        if columns != display_cols:
            rows = [
                {display_cols[i] if i < len(display_cols) else k: v
                 for i, (k, v) in enumerate(row.items())}
                for row in rows
            ]
            columns = display_cols
        # is_planned 布尔语义化：True=计划内 / False=计划外（故障）
        if "计划类型" in columns:
            rows = [
                {k: ("计划内" if v is True else ("计划外" if v is False else v))
                 for k, v in row.items()}
                for row in rows
            ]
        chart = generate_chart(display_cols, rows, q)
        st["status"] = "ok"
        st["columns"] = display_cols
        st["rows"] = rows
        st["svg"] = chart.get("svg") or ""
        st["chart_type"] = chart.get("type") or ""
        st["note"] = _conclusion(display_cols, rows)
        steps.append(st)

    # 周期报告：探测库内真实覆盖天数（供报告注释），失败降级为无注释。
    # 放在 elapsed 之前统计，报告头部的「编译耗时」才是完整生成耗时。
    period_info = None
    if tpl.get("period"):
        try:
            period_info = _period_coverage(tpl["period"], fact_tables)
        except Exception:
            period_info = None

    elapsed = time.time() - t0
    html = _render_html(tpl, steps, db_name, elapsed, period_info)
    return {"steps": steps, "html": html, "elapsed": elapsed, "period": period_info}
