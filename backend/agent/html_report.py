# -*- coding: utf-8 -*-
"""单文件 HTML 数据总览报告生成器。

区别于旧版 Markdown 报告：产出自包含（内联 CSS/JS，零外部依赖）的 HTML，
含标签页（数据总览 / 数据表 / 表间关系 / 样例数据）、SVG 表关系流程图、
颜色突出重点（核心表、高行数、异常指标），可直接在浏览器打开。
"""

import asyncio
import datetime
import html as html_mod
import re

from sqlalchemy.orm import Session


def _md_to_html(md: str) -> str:
    """极简 Markdown → HTML（标题/列表/加粗/数字高亮），供报告正文嵌入"""
    out: list[str] = []
    list_type: str | None = None  # 'ul' / 'ol' / None（区分有序/无序，避免 ol 被 </ul> 错误闭合）

    def _close_list() -> None:
        nonlocal list_type
        if list_type is not None:
            out.append(f"</{list_type}>")
            list_type = None

    for line in (md or "").split("\n"):
        line = line.rstrip()
        if not line:
            _close_list()
            continue
        if line.startswith("#### "):
            _close_list()
            out.append(f'<h4>{html_mod.escape(line[5:])}</h4>')
        elif line.startswith("### "):
            _close_list()
            out.append(f'<h4 class="sub">{html_mod.escape(line[4:])}</h4>')
        elif line.startswith("## "):
            _close_list()
            out.append(f'<h3>{html_mod.escape(line[3:])}</h3>')
        elif line.startswith("# "):
            _close_list()
            out.append(f'<h2>{html_mod.escape(line[2:])}</h2>')
        elif re.match(r"^[-*] ", line):
            if list_type != "ul":
                _close_list()
                out.append("<ul>")
                list_type = "ul"
            # 加粗 + 数字高亮
            t = re.sub(r"\*\*(.+?)\*\*", r'<b class="hl">\1</b>', html_mod.escape(line[2:]))
            out.append(f"<li>{t}</li>")
        # 有序列表正则：排除"2024. 销量"这类年份开头（4 位数字+点会被误判为列表序号并吞掉年份）
        elif re.match(r"^(?!\d{4}\. )\d+\. ", line):
            if list_type != "ol":
                _close_list()
                out.append("<ol>")
                list_type = "ol"
            t = re.sub(r"\*\*(.+?)\*\*", r'<b class="hl">\1</b>', html_mod.escape(re.sub(r"^\d+\. ", "", line)))
            out.append(f"<li>{t}</li>")
        else:
            _close_list()
            t = re.sub(r"\*\*(.+?)\*\*", r'<b class="hl">\1</b>', html_mod.escape(line))
            out.append(f"<p>{t}</p>")
    _close_list()
    return "\n".join(out)


# 总结小节 → 图标/主题色（专业化排版）
_SECTION_META = {
    "执行摘要": ("📌", "#4f46e5"),
    "数据整体分析": ("📊", "#7c3aed"),
    "重点维度分析": ("📈", "#d97706"),
    "总体结论": ("🎯", "#059669"),
    "改进建议": ("🛠️", "#db2777"),
}


def _highlight_nums(text: str) -> str:
    """数字/百分比/金额高亮（跳过日期里的数字段）"""
    return re.sub(r"(?<![\d-])([\d,]+(?:\.\d+)?%?)(?![\d-])", r'<b class="num">\1</b>', text)


def _style_markers(text: str) -> str:
    """行内 ⚠️/✅ 标记着色（⚠️ 风险红、✅ 亮点绿，到句号/分号/行尾）"""
    text = re.sub(r"(⚠️[^。；\n]*(?:。|；|$))", r'<span class="risk-i">\1</span>', text)
    text = re.sub(r"(✅[^。；\n]*(?:。|；|$))", r'<span class="good-i">\1</span>', text)
    return text


def _render_summary(md: str) -> str:
    """把 LLM 总结（---小节--- 结构）渲染为专业化分区卡片：
    - 每个小节一个白卡，左边色带 + 图标标题
    - ⚠️ 行 → 红色警示卡；✅ 行 → 绿色亮点卡
    - 关键数字红色高亮
    """
    if not (md or "").strip():
        return '<div class="empty">（暂无总结）</div>'
    blocks: list[tuple[str, list[str]]] = []
    cur_title: str | None = None
    cur_lines: list[str] = []
    for raw in md.split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^---(.+?)---\s*$", line)
        if m:
            if cur_title is not None and cur_lines:
                blocks.append((cur_title, cur_lines))
            cur_title = m.group(1).strip()
            cur_lines = []
        else:
            if cur_title is None:
                cur_title = "分析总结"
            cur_lines.append(line)
    if cur_title is not None and cur_lines:
        blocks.append((cur_title, cur_lines))

    if not blocks:
        blocks = [("分析总结", [md])]

    out: list[str] = []
    for title, lines in blocks:
        icon, color = _SECTION_META.get(title, ("▍", "#64748b"))
        rows: list[str] = []
        for ln in lines:
            if ln.startswith("⚠️"):
                body = _style_markers(_highlight_nums(html_mod.escape(ln[1:].lstrip())))
                rows.append(f'<div class="sum-risk">⚠️ {body}</div>')
            elif ln.startswith("✅"):
                body = _style_markers(_highlight_nums(html_mod.escape(ln[1:].lstrip())))
                rows.append(f'<div class="sum-good">✅ {body}</div>')
            else:
                body = _style_markers(_highlight_nums(html_mod.escape(ln)))
                rows.append(f'<p class="sum-p">{body}</p>')
        out.append(
            f'<div class="sum-sec" style="border-left-color:{color}">'
            f'<div class="sum-sec-h" style="color:{color}">{icon} {html_mod.escape(title)}</div>'
            f'{"".join(rows)}</div>'
        )
    return "\n".join(out)


def _scene_of(table_name: str) -> str:
    """按表名归类业务场景（用于流程图分组着色，与总览图谱一致）"""
    base = table_name.lower().split(".")[-1]
    if base.startswith(("mes_", "prod_")) or "work_order" in base or "process_output" in base \
            or base in ("workshop", "production_line", "production_record", "process"):
        return "生产"
    if base.startswith(("qms_", "quality")) or "inspect" in base or "defect" in base \
            or base in ("quality_inspection",):
        return "质量"
    if base.startswith(("eqp_", "equip", "machine")) or "downtime" in base or "maintenance" in base:
        return "设备"
    if base.startswith(("inv_", "stock", "warehouse")) or "inventory" in base \
            or base in ("material", "materials"):
        return "库存"
    if base.startswith(("sales_",)) or "sales_order" in base or "customer" in base or base.startswith("test_order"):
        return "销售"
    if "purchase" in base or "supplier" in base or "vendor" in base:
        return "采购"
    if base.startswith(("hr_", "employee", "staff", "attendance")):
        return "人事"
    if "finance" in base or "cost" in base or "invoice" in base:
        return "财务"
    return "基础数据"


SCENE_COLORS = {
    "生产": ("#2563eb", "#eff6ff"),
    "质量": ("#dc2626", "#fef2f2"),
    "设备": ("#d97706", "#fffbeb"),
    "库存": ("#059669", "#ecfdf5"),
    "销售": ("#7c3aed", "#f5f3ff"),
    "采购": ("#ea580c", "#fff7ed"),
    "人事": ("#db2777", "#fce7f3"),
    "财务": ("#0d9488", "#f0fdfa"),
    "基础数据": ("#64748b", "#f1f5f9"),
}

SCENE_ORDER = ["生产", "质量", "设备", "库存", "销售", "采购", "人事", "财务", "基础数据"]


def _build_flow_svg(rels: list[dict], labels: dict[str, str]) -> str:
    """生成表间关系 SVG 流程图：按场景分组列布局 + FK 连线（带箭头）"""
    if not rels:
        return '<div class="empty">暂无表间关系数据</div>'
    # 节点集合
    node_names: list[str] = []
    for r in rels:
        for k in (r["source_table"], r["target_table"]):
            if k not in node_names:
                node_names.append(k)
    # 按场景分组
    groups: dict[str, list[str]] = {}
    for n in node_names:
        groups.setdefault(_scene_of(n), []).append(n)
    for g in groups:
        groups[g] = sorted(groups[g], key=lambda x: labels.get(x, x))

    # 布局：每组一列，列宽 170，组间距 30；组内节点垂直排列
    col_w, row_h = 180, 58
    pos: dict[str, tuple[int, int]] = {}
    x = 20
    max_h = 0
    group_headers: list[tuple[int, str, str]] = []  # (x, scene, color)
    for scene in SCENE_ORDER:
        items = groups.get(scene)
        if not items:
            continue
        border, bg = SCENE_COLORS.get(scene, ("#64748b", "#f1f5f9"))
        group_headers.append((x, scene, border))
        y = 44
        for n in items:
            pos[n] = (x, y)
            y += row_h
        max_h = max(max_h, y)
        x += col_w + 30
    for scene, items in groups.items():
        if scene in SCENE_ORDER:
            continue
        border, bg = SCENE_COLORS.get(scene, ("#64748b", "#f1f5f9"))
        group_headers.append((x, scene, border))
        y = 44
        for n in items:
            pos[n] = (x, y)
            y += row_h
        max_h = max(max_h, y)
        x += col_w + 30

    w = x + 20
    h = max_h + 30
    parts = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;min-height:320px;background:#fbfcfe;border:1px solid #e5e7eb;border-radius:12px;">']
    # 连线（先画线再画节点，线在节点下方）
    for r in rels:
        if r["source_table"] not in pos or r["target_table"] not in pos:
            continue
        x1, y1 = pos[r["source_table"]]
        x2, y2 = pos[r["target_table"]]
        # 线从源节点右侧 → 目标节点左侧（不同列）或下方（同列）
        if x2 > x1:
            sx, sy = x1 + col_w, y1 + 20
            tx, ty = x2, y2 + 20
        else:
            sx, sy = x1 + col_w, y1 + 20
            tx, ty = x2 + col_w, y2 + 20
        mid = (sx + tx) / 2
        parts.append(
            f'<path d="M {sx} {sy} C {mid} {sy}, {mid} {ty}, {tx} {ty}" fill="none" '
            f'stroke="#818cf8" stroke-width="1.6" marker-end="url(#arr)"/>'
        )
    # 箭头定义
    parts.append('<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">'
                 '<path d="M0,0 L7,3 L0,6 z" fill="#818cf8"/></marker></defs>')
    # 场景分组标签
    for gx, scene, border in group_headers:
        parts.append(
            f'<text x="{gx + 6}" y="24" font-size="13" font-weight="600" fill="{border}">{scene}</text>'
            f'<rect x="{gx - 6}" y="34" width="{col_w + 6}" height="2" rx="1" fill="{border}" opacity="0.35"/>'
        )
    # 节点
    for n, (nx, ny) in pos.items():
        border, bg = SCENE_COLORS.get(_scene_of(n), ("#64748b", "#f1f5f9"))
        label = labels.get(n, n)
        parts.append(
            f'<rect x="{nx}" y="{ny}" width="{col_w - 8}" height="40" rx="8" '
            f'fill="{bg}" stroke="{border}" stroke-width="1.5"/>'
            f'<text x="{nx + 8}" y="{ny + 18}" font-size="12" font-weight="600" fill="#1f2937">{html_mod.escape(label)}</text>'
            f'<text x="{nx + 8}" y="{ny + 33}" font-size="10" fill="#6b7280">{html_mod.escape(n)}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def build_html_report(db: Session, allowed_tables: set[str] | None = None,
                      row_filters: dict[str, str] | None = None) -> str:
    """生成单文件 HTML 数据总览报告（标签页 + 流程图 + 颜色高亮）。

    权限过滤（安全关键）：
    - allowed_tables 非 None 时，报告只统计/展示/总结用户有权访问的表（越权表不出现）；
    - row_filters 提供时，样例数据查询注入行级 WHERE 条件（仅返回用户可见行）；
    - 关系图只保留两端都有权限的边。
    调用方需按 ACL 指纹做缓存隔离，避免不同权限用户共享缓存越权。
    """
    # ── 数据准备 ──
    from db.executor import get_table_row_counts, execute_sql
    from db.tools import get_real_tables
    from db.metadata import find_table_by_name
    from database import quote_ident
    from agent.report_agent import generate_report_stream
    from agent.sql_validator import validate_sql_safety

    try:
        counts = get_table_row_counts()
    except Exception:
        counts = {}
    try:
        all_real = [t["table_name"] for t in get_real_tables()]
    except Exception:
        all_real = []
    # 表级权限过滤：allowed_tables 非 None 时仅保留有权限的表（裸表名匹配，支持 schema.table）
    if allowed_tables is not None:
        allowed_lower = {t.lower() for t in allowed_tables}
        real_tables = [t for t in all_real
                       if t.split(".")[-1].lower() in allowed_lower]
    else:
        real_tables = all_real

    # 表信息（名称/中文/字段数/行数）
    tables_info = []
    for name in real_tables:
        detail = find_table_by_name(name)
        alias = (detail.get("table_alias") or name) if detail else name
        rows = counts.get(name.split(".")[-1], counts.get(name, 0))
        _rf = (row_filters or {}).get(name) or (row_filters or {}).get(name.split(".")[-1])
        if _rf:
            valid, _err, _clean = validate_sql_safety(f"SELECT 1 WHERE {_rf}")
            if valid:
                try:
                    qname = quote_ident(name) if "." not in name else \
                        f'{quote_ident(name.split(".", 1)[0])}.{quote_ident(name.split(".", 1)[1])}'
                    count_result = execute_sql(f"SELECT COUNT(*) AS row_count FROM {qname} WHERE {_rf}")
                    if count_result.get("success") and count_result.get("rows"):
                        rows = next(iter(count_result["rows"][0].values()), "?")
                    else:
                        rows = "?"
                except Exception:
                    rows = "?"
            else:
                rows = "?"
        try:
            fcount = len(_get_columns(db, name))
        except Exception:
            fcount = 0
        tables_info.append({"name": name, "alias": alias, "rows": rows, "fields": fcount})

    # 表关系（FK）：只保留两端都在允许集合内的边（防无权表名泄露）
    rels = []
    try:
        from routers.tables import _build_relationships
        rels = _build_relationships(db).get("relationships", [])
        if allowed_tables is not None:
            allowed_lower = {t.lower() for t in allowed_tables}
            allowed_bare = {t.split(".")[-1].lower() for t in allowed_tables}
            rels = [r for r in rels
                    if r.get("source_table", "").split(".")[-1].lower() in allowed_bare
                    and r.get("target_table", "").split(".")[-1].lower() in allowed_bare]
    except Exception:
        rels = []
    # 样例数据（前 6 表）：按行级权限注入 WHERE 条件（仅返回用户可见行）
    samples = []
    for t in tables_info[:6]:
        try:
            name = t["name"]
            qname = quote_ident(name) if "." not in name else \
                f'{quote_ident(name.split(".", 1)[0])}.{quote_ident(name.split(".", 1)[1])}'
            row_cond = ""
            if row_filters:
                _rf = row_filters.get(name) or row_filters.get(name.split(".")[-1])
                if _rf:
                    valid, _err, _clean = validate_sql_safety(f"SELECT 1 WHERE {_rf}")
                    if valid:
                        row_cond = f" WHERE {_rf}"
            r = execute_sql(f"SELECT * FROM {qname}{row_cond} LIMIT 3")
            if r["success"] and r["rows"]:
                samples.append({"table": t["alias"], "name": name, "rows": r["rows"], "columns": r["columns"]})
        except Exception:
            pass

    # LLM 总结（复用流式报告生成；context 只含用户有权限的表 + 行级过滤后的样例）
    summary_md = ""
    try:
        context_lines = [f"- {t['alias']}({t['name']}): {t['rows']} 行" for t in tables_info]
        data_brief = []
        for t in tables_info[:5]:
            try:
                name = t["name"]
                qname = quote_ident(name) if "." not in name else \
                    f'{quote_ident(name.split(".", 1)[0])}.{quote_ident(name.split(".", 1)[1])}'
                row_cond = ""
                if row_filters:
                    _rf = row_filters.get(name) or row_filters.get(name.split(".")[-1])
                    if _rf:
                        valid, _err, _clean = validate_sql_safety(f"SELECT 1 WHERE {_rf}")
                        if valid:
                            row_cond = f" WHERE {_rf}"
                r = execute_sql(f"SELECT * FROM {qname}{row_cond} LIMIT 3")
                if r["success"] and r["rows"]:
                    data_brief.append(f"【{t['alias']}】" + "; ".join(str(row) for row in r["rows"][:2]))
            except Exception:
                pass
        context = "数据库包含以下表:\n" + "\n".join(context_lines) + \
                  "\n\n关键数据摘要:\n" + ("\n".join(data_brief) if data_brief else "（暂无数据）")
        industry = "制造业（生产/质量/设备/库存类数据）" if any(
            k in " ".join(real_tables).lower() for k in ("production", "work_order", "equipment", "quality", "factory")
        ) else ""
        full: list[str] = []

        async def _collect():
            async for token in generate_report_stream(context, industry=industry):
                full.append(token)
        # 兼容两种上下文：同步路由/后台线程（无事件循环）用 asyncio.run；
        # 若已在运行中的事件循环内（async 路由）会抛 RuntimeError，此时用新线程另起循环执行。
        try:
            loop = asyncio.get_running_loop()
            in_loop = loop is not None and loop.is_running()
        except RuntimeError:
            in_loop = False
        if in_loop:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                _ex.submit(lambda: asyncio.run(_collect())).result()
        else:
            asyncio.run(_collect())
        summary_md = "".join(full)
    except Exception:
        summary_md = "（报告生成失败，可稍后重试）"

    # 2026-10-03 修复（P1）：上面行级过滤不可用时 rows 被置成字符串 "?"（有意为之，
    # card_html 会把它拼进 HTML 说明"行数未知"），但这里无条件求和 + 无条件按它排序：
    #   sum  → TypeError: unsupported operand type(s) for +: 'int' and 'str'
    #   sorted → TypeError: '<' not supported between instances of 'int' and 'str'
    # 任何配了行级过滤的账号，只要有一条 row_filter 未通过 validate_sql_safety（或
    # count 查询失败），整份数据总览报告 500。现在用「排序键」把非数值行数排到末尾、
    # 求和时跳过，"?" 语义与 card_html 保持一致。
    def _rows_num(t):
        v = t.get("rows")
        return v if isinstance(v, (int, float)) else -1

    total_rows = sum(t["rows"] for t in tables_info if isinstance(t.get("rows"), (int, float)))
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # 重点高亮：行数最多的表 / 关系最多的节点
    top_tables = sorted(tables_info, key=_rows_num, reverse=True)[:3]
    top_names = {t["name"] for t in top_tables}

    summary_html = _render_summary(summary_md)

    # 表卡片 HTML
    card_html = []
    for t in tables_info:
        hot = "hot" if t["name"] in top_names else ""
        card_html.append(
            f'<div class="tcard {hot}"><div class="tname">{html_mod.escape(t["alias"])}</div>'
            f'<div class="tmeta"><span class="mono">{html_mod.escape(t["name"])}</span></div>'
            f'<div class="tstat"><span class="stat" style="color:#2563eb">{t["fields"]} 字段</span>'
            f'<span class="stat" style="color:#dc2626">{t["rows"]} 行</span></div></div>'
        )

    # 样例数据 HTML
    sample_html = []
    for s in samples:
        thead = "".join(f"<th>{html_mod.escape(c)}</th>" for c in s["columns"][:8])
        tbody = ""
        for row in s["rows"]:
            cells = "".join(f"<td>{html_mod.escape(str(row.get(c, '')))[:40]}</td>" for c in s["columns"][:8])
            tbody += f"<tr>{cells}</tr>"
        sample_html.append(
            f'<div class="sample"><div class="sample-h">📄 {html_mod.escape(s["table"])} '
            f'<span class="mono" style="color:#9ca3af">{html_mod.escape(s["name"])}</span></div>'
            f'<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>数据总览报告 - Vequo</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:"Microsoft YaHei","PingFang SC",sans-serif; background:#f3f4f6; color:#1f2937; padding:24px; }}
.wrap {{ max-width:960px; margin:0 auto; }}
.header {{ background:linear-gradient(135deg,#4f46e5,#7c3aed); color:#fff; border-radius:16px; padding:24px 30px; margin-bottom:18px; }}
.header h1 {{ font-size:22px; font-weight:700; }}
.header p {{ opacity:.85; font-size:13px; margin-top:6px; }}
.card {{ background:#fff; border-radius:14px; padding:22px; box-shadow:0 1px 3px rgba(0,0,0,.06); }}
.card-h {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px; }}
.card-h h3 {{ font-size:16px; color:#111827; border-left:4px solid #4f46e5; padding-left:10px; margin:0; }}
.copy-btn {{ display:inline-flex; align-items:center; gap:5px; padding:7px 14px; border-radius:8px; border:1px solid #e5e7eb; background:#fff; color:#4b5563; font-size:12px; font-weight:600; cursor:pointer; transition:.15s; }}
.copy-btn:hover {{ background:#f3f4f6; border-color:#d1d5db; }}
.copy-btn.ok {{ background:#ecfdf5; border-color:#10b981; color:#047857; }}
.summary-body {{ line-height:1.9; font-size:14px; color:#374151; }}
.sum-sec {{ background:#fafbfd; border:1px solid #eef0f3; border-left:4px solid #4f46e5; border-radius:10px; padding:14px 18px; margin-bottom:14px; }}
.sum-sec-h {{ font-size:14px; font-weight:800; margin-bottom:10px; letter-spacing:.3px; }}
.sum-p {{ margin:0 0 8px; font-size:13.5px; line-height:1.85; color:#374151; }}
.sum-risk {{ background:#fef2f2; border:1px solid #fecaca; color:#b91c1c; border-radius:8px; padding:9px 13px; margin:6px 0 8px; font-size:13px; line-height:1.7; }}
.sum-good {{ background:#ecfdf5; border:1px solid #a7f3d0; color:#047857; border-radius:8px; padding:9px 13px; margin:6px 0 8px; font-size:13px; line-height:1.7; }}
.sum-p b.num, .sum-risk b.num, .sum-good b.num {{ color:#dc2626; font-weight:800; }}
.risk-i {{ color:#b91c1c; background:#fef2f2; border-radius:4px; padding:0 3px; }}
.good-i {{ color:#047857; background:#ecfdf5; border-radius:4px; padding:0 3px; }}
.footer {{ text-align:center; color:#9ca3af; font-size:12px; margin-top:20px; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>📊 数据总览报告</h1>
    <p>Vequo 自动生成 · {now} · 数据库共 {len(real_tables)} 张表 / {total_rows} 行数据</p>
  </div>
  <div class="card">
    <div class="card-h">
      <h3>🤖 AI 分析总结</h3>
      <button class="copy-btn" onclick="copySummary(this)">📋 复制总结</button>
    </div>
    <div class="summary-body" id="summaryBody">{summary_html}</div>
  </div>
  <div class="footer">Vequo 智能问析 · 数据总览报告 · {now}</div>
</div>
<script>
function copySummary(btn) {{
  const txt = document.getElementById('summaryBody').innerText.trim();
  if (!txt) return;
  const done = () => {{
    const old = btn.textContent;
    btn.textContent = '✅ 已复制';
    btn.classList.add('ok');
    setTimeout(() => {{ btn.textContent = old; btn.classList.remove('ok'); }}, 1800);
  }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(txt).then(done).catch(() => fallbackCopy(txt, done));
  }} else {{
    fallbackCopy(txt, done);
  }}
}}
function fallbackCopy(text, done) {{
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try {{ document.execCommand('copy'); done(); }} catch (e) {{}}
  document.body.removeChild(ta);
}}
</script>
</body>
</html>"""


_cols_cache: dict[tuple, list] = {}


def _get_columns(db: Session, table_name: str) -> list:
    """获取表字段（进程内 memo 缓存，按 db url + 表名隔离）；支持 schema.table。

    原 docstring 声称「带 inspector 缓存」，实际每次调用都实时 reflection（N 表 N 次查询）。
    这里加真正 memo：同一连接同一表只查一次，切库后 url 变化自动失效。
    """
    from sqlalchemy import inspect
    key = (str(db.get_bind().url), table_name)
    if key in _cols_cache:
        return _cols_cache[key]
    parts = table_name.split(".")
    if len(parts) == 2:
        cols = inspect(db.get_bind()).get_columns(parts[1], schema=parts[0])
    else:
        cols = inspect(db.get_bind()).get_columns(table_name)
    _cols_cache[key] = cols
    return cols
