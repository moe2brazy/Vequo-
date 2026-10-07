# -*- coding: utf-8 -*-
"""周期性报告引擎 —— 让「生产周报」「不良设备报告」这类问法在对话里直接出报告。

与另外两条报告链路的分工（别混）：
- agent/html_report.py     → 整库总览报告（总览页按钮，扫全库，不看问题）
- agent/question_report.py → 按问题取数的即席分析报告（原先靠回答操作栏的按钮触发）
- 本模块                   → **周期性、口径固定**的报告（周报/月报/不良设备报告…），
  由 report_intent 在主问答流里识别意图后走到这里，用户只管问，不需要找按钮。

为什么数字可信（口径写死，LLM 不碰 SQL）：
周报月报是周期性产物，每周算的必须是同一个口径——今天 SUM(input_qty) 明天
SUM(good_qty)，用户看不出来，但两个月的数字就没法比了。所以取数全部走
report_templates 里写死的 SQL 模板，章节小结用 _digest 从数字现算（确定性），
LLM 只负责把算准的数字写成一段总体结论，且被明确禁止编造数字。

对外入口：
    generate_events(...) → 事件生成器（step / done / error），供流式问答管道消费
    save_report_html / load_report_html → 预览件转存，供 Word/PDF 导出复用同一份 HTML
"""

from __future__ import annotations

import datetime
import html as html_mod
import logging
import re
import threading
import time
import uuid
from typing import AsyncGenerator  # noqa: F401  (保留签名提示)

_log = logging.getLogger("periodic_report")

# ── 主题色（与 question_report 同源，Vequo 品牌蓝）──────────
C_BLUE = "#2E7CF0"
C_INK = "#1d2129"
C_GRAY = "#4e5969"
C_MUTE = "#a9aeb8"
C_BORDER = "#e5e6eb"
C_BG_SOFT = "#f7f9fc"
C_UP = "#d93f2b"

# ═════════════════════════════════════════════════════════════
# 1. 数据水位对齐：库里数据只到昨天，就别按"到今天"的区间出空报告
# ═════════════════════════════════════════════════════════════

# 每类模板的数据水位查询：拿"最新一条数据是什么时候"来校准统计区间。
# 演示库/生产库的数据写入常有滞后，用户要"本周报告"，区间算到今天，
# 但数据只写到周三——直接出报告就是一片"没有数据"，用户只会觉得系统坏了。
_WATERLINE_SQL = {
    "production": "SELECT MAX(stat_date) FROM mes_process_output",
    "quality": "SELECT MAX(inspection_date) FROM qms_inspection",
    "equipment_defect": "SELECT MAX(start_time)::date FROM eqp_downtime_record",
}


def _to_date(v) -> datetime.date | None:
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def _align_window(rr) -> None:
    """按数据水位重算统计区间（就地修改 rr）。

    只有当库内最新数据早于请求区间终点时才调整，并把调整过程写进
    rr.adjusted / rr.asof / rr.original，报告封面上会如实说明，
    不搞"悄悄改区间"——数字对不上区间比区间空着更让人不信任。
    """
    if rr.period == "none":
        return
    from db.executor import execute_sql
    sql = _WATERLINE_SQL.get(rr.kind)
    if not sql:
        return
    try:
        res = execute_sql(sql)
        if not res.get("success") or not res.get("rows"):
            return
        asof = _to_date(list(res["rows"][0].values())[0])
    except Exception as e:
        _log.warning("数据水位查询失败（%s），按原区间出报告: %s", rr.kind, e)
        return
    if not asof:
        return
    end = _to_date(rr.end)
    if not end or asof >= end:
        return
    from agent.report_intent import _span
    start2, end2 = _span(rr.period, asof, rr.prev)
    rr.original = f"{rr.start} ~ {rr.end}"
    rr.start, rr.end = start2, end2
    rr.asof = asof.isoformat()
    rr.adjusted = True


# ═════════════════════════════════════════════════════════════
# 2. 图示：复用 question_report 的自绘 SVG（矢量、零依赖、可导出）
# ═════════════════════════════════════════════════════════════

def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _items(rows: list[dict], dim: str, meas: str) -> list[tuple[str, float]]:
    out = []
    for r in rows:
        k = str(r.get(dim) if dim in r else (list(r.values())[0] if r else ""))
        v = r.get(meas) if meas in r else (list(r.values())[-1] if r else None)
        try:
            out.append((k, float(v)))
        except (TypeError, ValueError):
            continue
    return [(k, v) for k, v in out if k and k != "None"]


def _unit_of(name: str) -> str:
    return "%" if re.search(r"率|占比|比例", str(name or "")) else ""


def _esc_num_html(text: str) -> str:
    """正文数字高亮（与 question_report._sections_html 同一规则）。"""
    t = html_mod.escape(text)
    return re.sub(r"(\d[\d,]*(?:\.\d+)?\s*%?)", r'<b class="n">\1</b>', t)


def _figure(title: str, caption: str, svg: str) -> str:
    # 图注用 <p> 而不是 <div>：导出 Word/PDF 时 parse_report_blocks 只认
    # p/h 系标签，用 div 图注会在导出件里丢掉。
    return (f'<figure class="fig"><p class="fig-t" style="font-size:13.5px;font-weight:700;'
            f'margin-bottom:12px">{html_mod.escape(title)}</p>{svg}'
            f'<p class="fig-c" style="font-size:11.5px;color:{C_MUTE};margin-top:10px">'
            f'{html_mod.escape(caption)}</p></figure>')


def _kpi_html(section: dict) -> str:
    """总览节（单行多列）→ 指标卡 + 达标仪表。"""
    from agent.question_report import _gauge_svg
    cols: list[str] = section.get("columns") or []
    rows: list[dict] = section.get("rows") or []
    if not rows:
        return '<p class="empty">本区间没有数据</p>'
    row = rows[0]
    cards = []
    gauge = ""
    for c in cols:
        v = row.get(c)
        if v is None or not re.search(r"\d", str(v)):
            continue
        unit = "%" if re.search(r"率|占比", c) else ""
        try:
            val_txt = f"{float(v):,}" if float(v) != int(float(v)) else f"{int(float(v)):,}"
        except (TypeError, ValueError):
            val_txt = str(v)
        cards.append((c, val_txt, unit))
        if unit == "%" and not gauge:
            try:
                gauge = _figure(f"{c} 达标情况", f"{c}（%）相对满刻度 100%",
                                _gauge_svg(_num(v), c, "%"))
            except Exception:
                gauge = ""
    html = ['<div class="kpis">']
    for name, val, unit in cards:
        html.append(f'<div class="kpi"><div class="kpi-v">{html_mod.escape(val)}'
                    f'{unit}</div><div class="kpi-k">{html_mod.escape(name)}</div></div>')
    html.append("</div>")
    return "".join(html) + gauge


def _section_chart(section: dict) -> str:
    """按模板声明的图表类型出图；缺维度/数值列时降级为表格。"""
    chart = section.get("chart") or "bar"
    cols: list[str] = section.get("columns") or []
    rows: list[dict] = section.get("rows") or []
    dim, meas = section.get("dim") or "", section.get("meas") or ""
    if chart == "kpi":
        return _kpi_html(section)
    if not rows:
        return '<p class="empty">本区间没有数据</p>'
    if chart == "table" or not dim or not meas:
        return ""
    from agent.question_report import _bar_chart_svg, _line_chart_svg, _donut_svg
    items = _items(rows, dim, meas)
    if not items:
        return ""
    unit = _unit_of(meas)
    if chart == "line":
        svg = _line_chart_svg(items, unit)
        cap = f"按{dim}排列的{meas}走势，共 {len(items)} 个点"
    elif chart == "donut":
        svg = _donut_svg(items, unit)
        cap = f"各{dim}的{meas}占比构成（前 8 项）"
    else:
        svg = _bar_chart_svg(items, unit)
        cap = f"按{dim}分组的{meas}对比，蓝色为最高项"
    return _figure(f"{meas}（按{dim}）", cap, svg)


def _table_html(section: dict) -> str:
    cols: list[str] = section.get("columns") or []
    rows: list[dict] = section.get("rows") or []
    if not cols or not rows:
        return ""
    from agent.question_report import _data_table_html
    return _data_table_html(cols, rows, limit=15, display_cols=cols)


def _sections_body(sections: list[dict]) -> str:
    out: list[str] = []
    for sec in sections:
        title = html_mod.escape(str(sec.get("title") or ""))
        out.append(f'<section class="sec"><h3 class="sec-h">{title}</h3><div class="sec-b">')
        if sec.get("skipped"):
            out.append(f'<p class="warn">⚠️ 本节未纳入统计：{html_mod.escape(str(sec.get("error") or ""))}。'
                       f"缺少这张表的数据，本章节数字不参与合计——宁可缺一章说清楚，也不给一份数字悄悄变少的报告。</p>")
        elif not sec.get("ok"):
            out.append(f'<p class="warn">⚠️ 本节取数失败：{html_mod.escape(str(sec.get("error") or ""))}</p>')
        else:
            digest = str(sec.get("digest") or "").strip()
            if digest:
                out.append(f'<p>{_esc_num_html(digest)}</p>')
            chart = _section_chart(sec)
            if chart:
                out.append(chart)
            tbl = _table_html(sec)
            if tbl:
                out.append(tbl)
        out.append("</div></section>")
    return "".join(out)


# ═════════════════════════════════════════════════════════════
# 3. AI 总结：只写判断，不碰数字；写不出来就用确定性小结兜底
# ═════════════════════════════════════════════════════════════

_SUMMARY_SYS = ("你是制造业数据分析专家。给定一份报告中各章节的数据小结，"
                "写一段 150~260 字的总体结论与 2~3 条具体建议。"
                "只能引用给定小结里出现过的数字，严禁编造、换算或补充任何新数字；"
                "写成连贯段落，不要分点编号，不要 markdown 标记。")


def _llm_summary(rr, sections: list[dict]) -> str:
    lines = []
    for sec in sections:
        if sec.get("ok") and not sec.get("skipped") and sec.get("digest"):
            lines.append(f"「{sec.get('title')}」：{sec['digest']}")
    if not lines:
        return ""
    ctx = "\n".join(lines)
    prompt = (f"报告：《{rr.title}》，统计区间 {rr.window_text()}。\n"
              f"各章节数据小结：\n{ctx}\n\n请写总体结论与建议。")
    try:
        from agent.llm_service import _make_llm
        from langchain_core.messages import HumanMessage, SystemMessage
        for temp in (0.3, 0.5):
            try:
                resp = _make_llm(temp=temp, max_tokens=700).invoke(
                    [SystemMessage(content=_SUMMARY_SYS), HumanMessage(content=prompt)])
                text = re.sub(r"\s+", " ", str(getattr(resp, "content", "") or "")).strip()
                _log.info("报告 AI 总结 temp=%.2f 返回长度=%d", temp, len(text))
                if text:
                    return text
            except Exception as e:
                _log.warning("报告 AI 总结 temp=%.2f 失败: %s", temp, e)
    except Exception as e:
        _log.warning("报告 AI 总结不可用（%s），改用确定性小结", e)
    # 兜底：把各章确定性小结拼成一段，保证没有 AI 也有一份完整报告
    return "总体情况：" + " ".join(lines)


# ═════════════════════════════════════════════════════════════
# 4. 组装：单文件自包含 HTML（内联 SVG，可直接打印成 PDF）
# ═════════════════════════════════════════════════════════════

_CSS = f"""
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"Microsoft YaHei","PingFang SC",-apple-system,sans-serif;
  background:#f4f6f9;color:{C_INK};padding:28px 16px;line-height:1.75;
  -webkit-font-smoothing:antialiased}}
.wrap{{max-width:880px;margin:0 auto}}
.cover{{background:linear-gradient(135deg,{C_BLUE} 0%,#1F66D6 100%);color:#fff;
  border-radius:18px;padding:32px 34px 26px;margin-bottom:20px;
  box-shadow:0 10px 30px -12px rgba(46,124,240,.45)}}
.cover .eyebrow{{font-size:12px;letter-spacing:2.5px;opacity:.82;font-weight:600}}
.cover h1{{font-size:25px;font-weight:700;margin:9px 0 14px;line-height:1.45}}
.cover .meta{{display:flex;flex-wrap:wrap;gap:8px 20px;font-size:12px;opacity:.9}}
.cover .note{{margin-top:10px;font-size:12px;opacity:.92}}
.pill{{display:inline-block;padding:2px 10px;border-radius:999px;font-size:11.5px;
  background:rgba(255,255,255,.2);border:1px solid rgba(255,255,255,.35)}}
.card{{background:#fff;border-radius:16px;padding:24px 26px;margin-bottom:16px;
  box-shadow:0 1px 3px rgba(16,24,40,.06),0 12px 32px -18px rgba(16,24,40,.16)}}
.card h2{{font-size:16px;font-weight:700;margin-bottom:16px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:12px;
  margin-bottom:14px}}
.kpi{{background:{C_BG_SOFT};border:1px solid {C_BORDER};border-radius:14px;padding:15px 17px}}
.kpi-v{{font-size:25px;font-weight:700;color:{C_BLUE};letter-spacing:-.5px;
  font-variant-numeric:tabular-nums;line-height:1.25}}
.kpi-k{{font-size:12.5px;color:{C_GRAY};margin-top:3px;font-weight:600}}
.sec{{border-left:3px solid {C_BLUE};padding:2px 0 2px 16px;margin-bottom:20px}}
.sec:last-child{{margin-bottom:0}}
.sec-h{{font-size:15.5px;font-weight:700;color:{C_INK};margin-bottom:9px}}
.sec-b p{{font-size:14px;color:#374151;margin-bottom:8px}}
.sec-b p:last-child{{margin-bottom:0}}
.sec-b .warn{{background:#fff6f5;border:1px solid #ffd9d3;color:#a8321f;
  border-radius:9px;padding:9px 13px}}
.n{{color:{C_UP};font-weight:700}}
.fig{{margin:14px 0 18px;padding:18px;background:{C_BG_SOFT};border:1px solid #eaeef5;
  border-radius:14px}}
.fig:last-child{{margin-bottom:0}}
.tbl-wrap{{overflow-x:auto;border:1px solid {C_BORDER};border-radius:12px;margin-top:6px}}
.dt{{width:100%;border-collapse:collapse;font-size:12.5px}}
.dt th{{background:#f5f7fa;color:{C_GRAY};font-weight:600;text-align:left;
  padding:9px 12px;white-space:nowrap;border-bottom:1px solid {C_BORDER}}}
.dt td{{padding:8px 12px;border-bottom:1px solid #f2f4f7;color:#374151;white-space:nowrap}}
.dt tbody tr:last-child td{{border-bottom:none}}
.dt tbody tr:nth-child(even){{background:#fcfdfe}}
.dt td.num{{text-align:right;font-variant-numeric:tabular-nums;color:{C_INK};font-weight:600}}
.tbl-more{{padding:8px 12px;font-size:11.5px;color:{C_MUTE};background:#fafbfc}}
.empty{{color:{C_MUTE};font-size:13px;padding:12px 0}}
.sqlbox{{background:#1e293b;color:#cbd5e1;border-radius:12px;padding:14px 16px;
  font-family:"JetBrains Mono",Consolas,monospace;font-size:11.5px;line-height:1.7;
  overflow-x:auto;white-space:pre-wrap;word-break:break-all;margin-bottom:10px}}
.foot{{text-align:center;color:{C_MUTE};font-size:11.5px;padding:14px 0 6px}}
@media print{{
  body{{background:#fff;padding:0}}
  .card,.kpi,.fig{{box-shadow:none;break-inside:avoid}}
  .cover{{break-after:avoid}}
  .sec{{break-inside:avoid}}
}}
"""


def build_html(rr, data: dict, summary: str, user: str = "", db_name: str = "") -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    sections: list[dict] = data.get("sections") or []
    ok_n = sum(1 for s in sections if s.get("ok") and not s.get("skipped"))

    meta = [f'<span class="pill">统计区间 {html_mod.escape(rr.window_text())}</span>',
            f'<span class="pill">{html_mod.escape(rr.period_label)}</span>',
            f'<span>数据源：{html_mod.escape(db_name or "当前业务库")}</span>',
            f'<span>生成时间：{html_mod.escape(now)}</span>',
            f'<span>生成人：{html_mod.escape(user or "—")}</span>',
            f'<span class="pill">取数成功 {ok_n}/{len(sections)} 章</span>']
    note = ""
    if rr.adjusted:
        note = (f'<div class="note">⚠️ 库内数据截至 {html_mod.escape(rr.asof)}，'
                f"本报告已按数据水位对齐统计区间（原请求区间 "
                f"{html_mod.escape(rr.original or rr.window_text())}）。</div>")

    body: list[str] = []

    # 总结先行：读者第一眼要的是结论，不是图表
    body.append('<div class="card"><h2>一、总体结论与建议</h2>'
                f'<section class="sec"><div class="sec-b"><p>{_esc_num_html(summary)}</p>'
                "</div></section></div>")

    body.append('<div class="card"><h2>二、分章数据与图示</h2>' + _sections_body(sections) + "</div>")

    sqls = [s for s in sections if s.get("sql")]
    if sqls:
        appendix = "".join(
            f'<div class="sqlbox">【{html_mod.escape(str(s.get("title") or ""))}】\n'
            f'{html_mod.escape(str(s.get("sql") or ""))}</div>' for s in sqls)
        body.append('<div class="card"><h2>附：各章取数口径（原文 SQL）</h2>' + appendix + "</div>")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html_mod.escape(rr.title)} — 分析报告</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="cover">
    <div class="eyebrow">Vequo 智能问析 · 周期分析报告</div>
    <h1>{html_mod.escape(rr.title)}</h1>
    <div class="meta">{''.join(meta)}</div>
    {note}
  </div>
  {''.join(body)}
  <div class="foot">Vequo 智能问析 · 本报告由系统自动生成，数字来自当前数据库实时查询，口径 SQL 见附录 · {html_mod.escape(now)}</div>
</div>
</body>
</html>"""


# ═════════════════════════════════════════════════════════════
# 5. 预览件转存：Word/PDF 导出复用同一份 HTML（预览 = 下载，内容一致）
# ═════════════════════════════════════════════════════════════

_STORE: dict[str, dict] = {}
_STORE_TTL = 2 * 3600   # 2 小时内可反复导出；过期重新提问即可
_STORE_MAX = 50          # 容量上限：单份 HTML 几十~几百 KB，无上限会常驻内存
_store_lock = threading.Lock()   # 2026-10-03 新增：FastAPI 同步路由跑在 threadpool 里，
                                 # 原「遍历→pop→写入」三步无锁，并发生成报告会互相顶掉条目


def save_report_html(title: str, html: str, username: str = "") -> str:
    """把预览件存进进程内缓存，返回 rid。

    username 一起记下来：前端会话从本地存储恢复时只留着 rid，要按 rid 把这份
    HTML 取回去重开预览，那时候得确认这份报告确实是这个人生成的。
    """
    now = time.time()
    rid = uuid.uuid4().hex[:12]
    with _store_lock:
        for k in [k for k, v in _STORE.items() if now - v["ts"] > _STORE_TTL]:
            _STORE.pop(k, None)
        # 容量上限：超了丢最旧的一份（否则 TTL 清理只在 save 时触发，
        # 一旦不再生成报告，几十份大 HTML 会永久驻留）
        while len(_STORE) >= _STORE_MAX:
            oldest = min(_STORE, key=lambda k: _STORE[k]["ts"])
            _STORE.pop(oldest, None)
        _STORE[rid] = {"title": title, "html": html, "ts": now, "username": username or ""}
    return rid


def load_report_html(rid: str) -> dict | None:
    with _store_lock:
        item = _STORE.get(str(rid or ""))
        if not item or time.time() - item["ts"] > _STORE_TTL:
            return None
        return item


# ═════════════════════════════════════════════════════════════
# 6. 主入口：事件生成器（对接 /api/agent/stream 管道）
# ═════════════════════════════════════════════════════════════

def _db_name() -> str:
    try:
        from database import get_database_config
        cfg = get_database_config() or {}
        return cfg.get("database") or cfg.get("db") or ""
    except Exception:
        return ""


def generate_events(query: str, rr, acl=None, user: dict | None = None):
    """按报告请求产出流式事件。事件形状与 LLMService.run() 一致：
    {"type":"step","name":..,"detail":..} / {"type":"done","response":{..}} / {"type":"error","message":..}
    """
    import datetime as _dt
    from agent.report_templates import get_template, collect, NO_DATASOURCE

    username = (user or {}).get("username", "")
    yield {"type": "step", "name": "报告识别",
           "detail": f"识别为「{rr.title}」请求，统计区间 {rr.window_text()}"}

    try:
        from security.context import set_acl, clear_acl
        if acl is not None:
            set_acl(acl)
    except Exception:
        clear_acl = lambda: None  # noqa: E731

    try:
        template = get_template(rr.kind)
        if template is None:
            # 识别出意图但库里没有对应数据源（如销售）→ 明确说清楚，不硬出报告
            if rr.kind in NO_DATASOURCE:
                yield {"type": "done", "response": {
                    "type": "report", "query": query,
                    "answer": NO_DATASOURCE[rr.kind],
                    "report_title": "", "report_html": "", "report_id": "",
                }}
                return
            # 泛化请求（如只说"生成报告"）→ 走按问取数的即席报告链路兜底
            yield {"type": "step", "name": "按问取数", "detail": "按问题语义生成取数方案"}
            yield from _adhoc_report_events(query, acl, username)
            return

        yield {"type": "step", "name": "模板取数",
               "detail": f"按固定口径取数，共 {len(template.sections)} 个章节"}
        _align_window(rr)
        data = collect(template, rr,
                       acl.allowed_tables if acl is not None else None,
                       (acl.row_filters or None) if acl is not None else None,
                       acl)
        ok_n = sum(1 for s in data.get("sections", []) if s.get("ok") and not s.get("skipped"))
        if not data.get("ok"):
            raise RuntimeError("所有章节取数均失败，请确认当前库连接与表权限")
        yield {"type": "step", "name": "生成图表", "detail": f"{ok_n} 个章节取数成功，绘制 SVG 图示"}
        yield {"type": "step", "name": "撰写结论", "detail": "AI 总结生成中（失败自动用确定性小结）"}

        sections = data.get("sections") or []
        summary = _llm_summary(rr, sections)
        html = build_html(rr, data, summary, user=username, db_name=_db_name())
        rid = save_report_html(rr.title, html, username)
        n_rows = sum(len(s.get("rows") or []) for s in sections)
        answer = (f"已生成《{rr.title}》，统计区间 {rr.window_text()}，"
                  f"{ok_n} 个章节取数成功（共 {n_rows} 行明细）。"
                  f"报告包含总体结论、分章图示与明细表，已打开预览，可下载 Word / PDF。")
        try:
            from auth import audit
            audit("report_by_question", username=username, query=str(query)[:60],
                  title=rr.title[:40], rows=n_rows)
        except Exception:
            pass
        yield {"type": "done", "response": {
            "type": "report", "query": query, "answer": answer,
            "report_title": rr.title, "report_html": html, "report_id": rid,
            "elapsed_ms": 0,
        }}
    except Exception as e:
        _log.warning("周期报告生成失败: %s", e, exc_info=True)
        yield {"type": "error", "message": f"报告生成失败：{e}"}
    finally:
        try:
            clear_acl()
        except Exception:
            pass


def _adhoc_report_events(query: str, acl, username: str):
    """泛化报告请求的兜底：复用 question_report 的按问取数链路。"""
    try:
        from agent import question_report as QR
        data = QR.collect_question_data(
            query, "", acl.allowed_tables if acl is not None else None,
            (acl.row_filters or None) if acl is not None else None,
            200, acl)
        if not data.get("ok"):
            raise RuntimeError(str(data.get("error") or "未能为这个问题取到数据"))
        sections = QR.split_sections(
            QR.analyze_sync(query, data["columns"], data["rows"],
                            data.get("table", ""), data.get("sql", "")))
        html = QR.build_report_html(query, data, sections, user=username, db_name=_db_name())
        title = f"{query[:24]} 分析报告"
        rid = save_report_html(title, html, username)
        yield {"type": "done", "response": {
            "type": "report", "query": query,
            "answer": f"已按你的问题生成分析报告（{len(sections)} 个章节），已打开预览。",
            "report_title": title, "report_html": html, "report_id": rid,
        }}
    except Exception as e:
        raise RuntimeError(
            f"没能为这个问题生成报告：{str(e)[:160]}。"
            "可以试试明确的问法，比如「生成生产周报」「生成不良设备月报」。") from e
