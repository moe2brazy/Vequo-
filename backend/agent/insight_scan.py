# -*- coding: utf-8 -*-
"""主动洞察自动探查（P1-4，对标 Tableau SpotIQ）。

## 与现有 monitor.py 的区别
- `monitor`（对标 Pulse）：**人工配置**规则 {name, sql, change_pct}，只盯指定指标的阈值异动
  —— 前提是你知道该盯什么。
- 本模块（对标 SpotIQ）：**无需配置**，给一张表就自动扫描「度量 × 维度」组合，
  用统计方法把值得看的异常挑出来 —— 解决"不知道该盯什么"。

## 探查的四类信号（全部确定性计算，不调用 LLM）
1. `outlier` 离群：某维度取值下的度量显著偏离同类（|z| > 2）；
2. `imbalance` 失衡：某维度取值集中度过高（占比 > 60%），如某产线吃掉 80% 的不良；
3. `trend_break` 突变：有时间列时，最新一期相对历史均值偏离超阈值；
4. `null_rate` 空值：某字段空值率过高，数据质量预警。

## 成本与护栏
- 组合数有上限（默认 3 度量 × 4 维度），每条 SQL 都走只读校验 + 行/列级 ACL 改写，
  与问答主流程同级别权限；
- 统计判定在 Python 侧做（不写复杂 SQL），保持可读与可调；
- 数据不足（维度取值太少、行数太少）直接跳过，不硬凑结论。

对外 API：
  - scan_table(table_name, ...)  自动探查一张表
  - scan_top_tables(limit)       批量探查当前库的重点表
"""

from __future__ import annotations

import logging
import statistics
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

_logger = logging.getLogger("insight_scan")

# ── 判定阈值（可在调用时覆盖）──────────────────────────────
Z_THRESHOLD = 2.0          # 离群：|z| 超过该值
IMBALANCE_SHARE = 0.60     # 失衡：单个维度取值占比超过该值
TREND_BREAK_PCT = 30.0     # 突变：最新一期相对历史均值偏离超过该百分比
NULL_RATE_ALERT = 0.30     # 空值率预警阈值
MIN_DIM_VALUES = 4         # 维度取值少于此数不做离群判定（样本太少不可靠）
MIN_ROWS = 5               # 表行数少于此数直接跳过


def _safe_execute(sql: str) -> dict:
    """只读校验 + 行/列级 ACL 改写 + 执行（统一走 monitor 的护栏，保持一致）。

    注意这里**不再自带一份实现**：monitor._safe_execute 已经接上 enforcer.rewrite_sql
    （原先它只做只读校验，导致本模块的扫描取数也一并漏掉行级过滤与列脱敏）。
    下面的 fallback 仅在 agent.monitor 导入失败时兜底，只保证只读安全。
    """
    try:
        from agent.monitor import _safe_execute as _mon_exec
        return _mon_exec(sql)
    except Exception:
        from agent.sql_validator import validate_sql_safety
        from db.executor import execute_sql
        ok, err, cleaned = validate_sql_safety(sql)
        if not ok:
            return {"success": False, "error": f"安全校验未通过：{err}"}
        return execute_sql(cleaned)


def _table_allowed(table_name: str) -> bool:
    """当前用户是否有权访问该表（表级 ACL，fail-closed）。

    口径与 `routers/tables.py:_acl_table_filter` 一致：
    - ACL 取不到 / superuser / allowed_tables 为 None（未配置表授权）⇒ 放行；
    - 否则只放行白名单内的裸表名（忽略 schema 前缀，大小写不敏感）。
    """
    try:
        from security.context import get_acl
        acl = get_acl()
    except Exception:
        return True          # 取不到 ACL 上下文（如离线脚本/评测）⇒ 不拦
    if acl is None or getattr(acl, "superuser", False):
        return True
    allowed = getattr(acl, "allowed_tables", None)
    if allowed is None:
        return True
    bare = str(table_name or "").split(".")[-1].lower()
    return bare in {str(t).split(".")[-1].lower() for t in allowed}


def _q(ident: str) -> str:
    try:
        from database import quote_ident
        return quote_ident(ident)
    except Exception:
        return '"%s"' % ident.replace('"', '""')


def _q_table(table: str) -> str:
    """表名安全引用：schema.table（如 factory.attendance）拆开分别引用为 "factory"."attendance"，
    裸表名直接引用。此前整表名被 quote 成 "factory.attendance" 单个标识符，PG 报表不存在。"""
    if "." in (table or ""):
        schema, tbl = table.split(".", 1)
        return f"{_q(schema)}.{_q(tbl)}"
    return _q(table)


def _to_num(v):
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def _split_fields(fields: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """把字段分成 (度量/数值, 维度/分类, 时间)。"""
    from agent.suggest import _CATEGORICAL_TYPES, _NUMERIC_TYPES, _is_id_field, _norm_type
    measures, dims, times = [], [], []
    for f in fields:
        t = _norm_type(str(f.get("type") or "").lower().strip())
        if t in _NUMERIC_TYPES and not _is_id_field(f):
            measures.append(f)
        elif t in _CATEGORICAL_TYPES:
            dims.append(f)
        elif any(k in t for k in ("date", "time")):
            times.append(f)
    return measures, dims, times


def _zscores(vals: list[float]) -> list[float]:
    """z-score；样本量 <3 或标准差为 0 时返回全 0（不制造假异常）。"""
    n = len(vals)
    if n < 3:
        return [0.0] * n
    try:
        mu = statistics.fmean(vals)
        sd = statistics.pstdev(vals)
    except Exception:
        return [0.0] * n
    if sd == 0:
        return [0.0] * n
    return [(v - mu) / sd for v in vals]


def _probe_dim(table: str, measure: str, dim: str) -> list[dict]:
    """探查「度量 × 维度」：返回 [{dim_value, value, share, z}]，失败返回空。"""
    sql = (f"SELECT {_q(dim)} AS d, SUM({_q(measure)}) AS v, COUNT(*) AS n "
           f"FROM {_q_table(table)} WHERE {_q(dim)} IS NOT NULL "
           f"GROUP BY {_q(dim)} ORDER BY v DESC LIMIT 50")
    r = _safe_execute(sql)
    if not r.get("success") or not r.get("rows"):
        return []
    rows = []
    for row in r["rows"]:
        v = _to_num(row.get("v"))
        if v is None:
            continue
        rows.append({"dim_value": str(row.get("d")), "value": v,
                     "n": int(_to_num(row.get("n")) or 0)})
    if not rows:
        return []
    total = sum(x["value"] for x in rows)
    zs = _zscores([x["value"] for x in rows])
    for x, z in zip(rows, zs):
        x["share"] = (x["value"] / total) if total else 0.0
        x["z"] = round(z, 2)
    return rows


def _probe_nulls(table: str, field: str) -> float | None:
    """某字段的空值率。"""
    sql = (f"SELECT COUNT(*) AS total, "
           f"SUM(CASE WHEN {_q(field)} IS NULL THEN 1 ELSE 0 END) AS nulls "
           f"FROM {_q_table(table)}")
    r = _safe_execute(sql)
    if not r.get("success") or not r.get("rows"):
        return None
    row = r["rows"][0]
    total = _to_num(row.get("total"))
    nulls = _to_num(row.get("nulls"))
    if not total:
        return None
    return (nulls or 0.0) / total


def _probe_trend(table: str, measure: str, time_col: str) -> dict | None:
    """时间趋势：最新一期 vs 历史均值。"""
    # 数据正确性修复（P0）：原为 ORDER BY 时间列 ASC LIMIT 60 —— 取到的是**最早**的 60 期，
    # 而下面却把 vals[-1] 当成"最新一期"做突变检测。表历史超过 60 期时，
    # "最新"其实是第 60 期（几年前），趋势突变洞察的结论完全失真。
    # 改为倒序取最新 60 期，再反转回时间正序。
    sql = (f"SELECT {_q(time_col)} AS t, SUM({_q(measure)}) AS v "
           f"FROM {_q_table(table)} GROUP BY {_q(time_col)} ORDER BY {_q(time_col)} DESC LIMIT 60")
    r = _safe_execute(sql)
    if not r.get("success") or not r.get("rows"):
        return None
    rows = list(reversed(r["rows"]))   # 倒序结果反转为时间正序，保证 vals[-1] 真的是最新一期
    vals = [(_to_num(x.get("v")) or 0.0) for x in rows]
    times = [str(x.get("t")) for x in rows]
    if len(vals) < 3:
        return None
    last, hist = vals[-1], vals[:-1]
    base = statistics.fmean(hist)
    if base == 0:
        return None
    dev = (last - base) / abs(base) * 100
    return {"last": last, "base": round(base, 4), "deviation_pct": round(dev, 2),
            "time_value": times[-1], "points": len(vals)}


def scan_table(table_name: str, max_measures: int = 3, max_dims: int = 4,
               fields: list[dict] | None = None, row_count: int | None = None) -> dict:
    """自动探查一张表，返回洞察列表。

    返回 {success, table, insights, scanned, error}
    insight: {type, table, metric, dim, dim_value, value, expected, deviation_pct,
              severity, message}
    """
    empty = {"success": False, "table": table_name, "insights": [], "scanned": 0, "error": ""}
    if not table_name:
        return {**empty, "error": "表名为空"}
    # 表级 ACL（2026-09-24 修）：本函数接受的表名来自**请求参数**
    # （`POST /api/insights/scan?table=xxx` 此前连登录都不校验），
    # 内部 SQL 走 `_safe_execute`（只读校验，无表级权限）⇒ 受限角色可扫任意表。
    # 入口处 fail-closed 拦截，所有调用方（接口 / 定时报表 / 总览页）一并受保护。
    if not _table_allowed(table_name):
        return {**empty, "error": "当前角色无权访问该数据表，请联系管理员开通权限"}
    try:
        if fields is None:
            try:
                from db.tools import get_table_detail
                fields = (get_table_detail(table_name) or {}).get("fields") or []
            except Exception:
                fields = []
        if not fields:
            return {**empty, "error": "未取到字段信息"}

        from agent.suggest import _friendly_label
        measures, dims, times = _split_fields(fields)
        measures = measures[:max_measures]
        dims = dims[:max_dims]
        if not measures:
            return {**empty, "error": "该表没有可用的数值字段"}

        insights: list[dict] = []
        scanned = 0
        label = table_name.split(".")[-1]

        # ① 离群 + ② 失衡：逐 (度量 × 维度) 组合探查
        jobs = [(m, d) for m in measures for d in dims]
        if jobs:
            # 2026-10-03 修复（ACL 传播，fail-open）：ACL 存在 security/context.py 的
            # ContextVar 里，而 ThreadPoolExecutor **不传播 contextvars**
            # （实测：主线程 get_acl() 返回对象、worker 线程返回 None）。
            # `_probe_dim` 是 ①② 两类洞察**唯一**的取数入口，整个在 worker 线程执行
            # → get_acl() 为 None → monitor.py 里的 `if acl is not None: rewrite_sql(...)`
            # 整段跳过 → 洞察数值是**全表口径**，而同一张表在主问数链路是带行过滤的。
            # 表现：受限账号（如仅可见本车间）看到「某工序缺陷数显著高于同类（22690 vs …）」
            # 这是全厂口径的结论，文案里还带具体数值，用户无从发现口径被放大。
            # 表级白名单由 scan_table 的 _table_allowed 独立拦住了，所以没报「无权访问」，
            # 只是数值范围悄悄变大了 —— 与文件头「每条 SQL 都走行/列级 ACL 改写」的声明矛盾。
            # 修法：submit 前 copy_context()，把当前 ContextVar 复制进 worker。
            with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as ex:
                futs = []
                for _j in jobs:
                    # 默认参数绑定 _j，避免 lambda 闭包延迟求值导致全部拿到同一个 job
                    _ctx = copy_context()
                    futs.append(ex.submit(
                        lambda jj=_j: _ctx.run(_probe_dim, table_name, jj[0]["name"], jj[1]["name"])))
                results = []
                for f in futs:
                    try:
                        results.append(f.result())
                    except Exception as e:
                        _logger.warning("维度探查失败(%s)：%s", table_name, e)
                        results.append([])
            for (m, d), rows in zip(jobs, results):
                scanned += 1
                if len(rows) < MIN_DIM_VALUES:
                    continue
                m_lab = _friendly_label(m)
                d_lab = _friendly_label(d)
                # 离群：|z| 最大且超阈值
                top = max(rows, key=lambda x: abs(x["z"]))
                if abs(top["z"]) >= Z_THRESHOLD:
                    direction = "显著高于" if top["z"] > 0 else "显著低于"
                    insights.append({
                        "type": "outlier",
                        "table": table_name,
                        "metric": m["name"],
                        "metric_label": m_lab,
                        "dim": d["name"],
                        "dim_label": d_lab,
                        "dim_value": top["dim_value"],
                        "value": round(top["value"], 4),
                        "expected": round(statistics.fmean([x["value"] for x in rows]), 4),
                        "z": top["z"],
                        "severity": round(min(abs(top["z"]) / 4.0, 1.5), 2) + 0.5,
                        "message": (f"{label}：{d_lab}「{top['dim_value']}」的{m_lab}"
                                    f"{direction}同类（{top['value']:,.0f} vs 均值 "
                                    f"{statistics.fmean([x['value'] for x in rows]):,.0f}，z={top['z']}）"),
                    })
                # 失衡：头部占比过高
                head = rows[0]
                if head["share"] >= IMBALANCE_SHARE:
                    insights.append({
                        "type": "imbalance",
                        "table": table_name,
                        "metric": m["name"],
                        "metric_label": m_lab,
                        "dim": d["name"],
                        "dim_label": d_lab,
                        "dim_value": head["dim_value"],
                        "value": round(head["value"], 4),
                        "share": round(head["share"], 4),
                        "severity": round(head["share"], 2),
                        "message": (f"{label}：{m_lab}高度集中在{d_lab}「{head['dim_value']}」"
                                    f"（占 {head['share'] * 100:.0f}%）"),
                    })

        # ③ 趋势突变（有时间列才做）
        if times:
            t = times[0]["name"]
            for m in measures[:2]:
                tr = _probe_trend(table_name, m["name"], t)
                scanned += 1
                if not tr or abs(tr["deviation_pct"]) < TREND_BREAK_PCT:
                    continue
                direction = "跃升" if tr["deviation_pct"] > 0 else "骤降"
                insights.append({
                    "type": "trend_break",
                    "table": table_name,
                    "metric": m["name"],
                    "metric_label": _friendly_label(m),
                    "dim": t,
                    "dim_label": t,
                    "dim_value": tr["time_value"],
                    "value": round(tr["last"], 4),
                    "expected": tr["base"],
                    "deviation_pct": tr["deviation_pct"],
                    "severity": round(min(abs(tr["deviation_pct"]) / 100.0, 1.5), 2) + 0.5,
                    "message": (f"{label}：{_friendly_label(m)}在 {tr['time_value']} "
                                f"{direction} {abs(tr['deviation_pct']):.0f}%"
                                f"（{tr['last']:,.0f} vs 历史均值 {tr['base']:,.0f}）"),
                })

        # ④ 空值率（只查分类字段，成本可控）
        for d in dims[:2]:
            rate = _probe_nulls(table_name, d["name"])
            scanned += 1
            if rate is not None and rate >= NULL_RATE_ALERT:
                insights.append({
                    "type": "null_rate",
                    "table": table_name,
                    "metric": d["name"],
                    "metric_label": _friendly_label(d),
                    "dim": d["name"],
                    "dim_label": _friendly_label(d),
                    "dim_value": "",
                    "value": round(rate, 4),
                    "expected": 0.0,
                    "severity": round(rate, 2),
                    "message": (f"{label}：字段「{_friendly_label(d)}」空值率 "
                                f"{rate * 100:.0f}%，可能影响按该维度的分析结论"),
                })

        insights.sort(key=lambda x: x["severity"], reverse=True)
        return {"success": True, "table": table_name,
                "insights": attach_action_suggestions(insights),
                "scanned": scanned, "error": ""}
    except Exception as e:
        _logger.warning("主动洞察探查失败 %s: %s", table_name, e)
        return {**empty, "error": str(e)[:160]}


def scan_top_tables(limit: int = 3, max_insights: int = 8) -> dict:
    """批量探查当前库的重点表（按行数取前 N 张），汇总洞察。"""
    try:
        from db.tools import get_all_tables
        from db.executor import get_table_row_counts
        tables = get_all_tables() or []
        counts = get_table_row_counts() or {}
    except Exception as e:
        return {"success": False, "insights": [], "error": str(e)[:160]}

    ranked = sorted(tables,
                    key=lambda t: counts.get(t["table_name"]) or t.get("row_count") or 0,
                    reverse=True)
    picked = [t for t in ranked
              if (counts.get(t["table_name"]) or t.get("row_count") or 0) >= MIN_ROWS][:limit]
    if not picked:
        return {"success": False, "insights": [], "error": "没有数据量足够的表可供探查"}
    # 批量探查同样按 ACL 过滤（2026-09-24）：否则不传 table 走这里，
    # 一样能把无权表扫一遍（scan_table 内的拦截在这里作为第二道防线仍生效）。
    picked = [t for t in picked if _table_allowed(t.get("table_name"))]
    if not picked:
        return {"success": False, "insights": [],
                "error": "当前角色没有可访问的数据表，请联系管理员开通权限"}

    all_ins: list[dict] = []
    scanned_tables = []
    for t in picked:
        r = scan_table(t["table_name"])
        if r.get("success"):
            all_ins.extend(r.get("insights") or [])
            scanned_tables.append({"table": t["table_name"], "scanned": r.get("scanned", 0)})

    all_ins.sort(key=lambda x: x["severity"], reverse=True)
    return {"success": True, "insights": attach_action_suggestions(all_ins[:max_insights]),
            "scanned_tables": scanned_tables, "error": ""}


_TYPE_LABELS = {"outlier": "异常离群", "imbalance": "集中失衡", "trend_break": "趋势突变",
                "null_heavy": "空值偏高", "correlation": "强相关"}


# ── 洞察 → 行动建议（对标 Fabric operations agents：把「发现异常」升级为「可执行动作」）──
# 确定性规则：按洞察命中的表映射到动作注册表里的动作。只推荐、不执行——
# 具体参数由用户在 ActionPanel 里填写/确认（写操作永远过二次确认闸门）。
_ACTION_HINTS: list[tuple[tuple[str, ...], str, str]] = [
    # (表名关键词元组, 动作 id, 建议理由)
    (("inventory", "snapshot", "库存"), "update_safety_stock", "该异常与库存相关，可调整产品的安全库存阈值以对齐预警策略"),
    (("orders", "订单"), "update_order_status", "该异常与订单相关，可更新订单状态完成业务闭环"),
]


def suggest_action_for_insight(insight: dict) -> dict | None:
    """给一条洞察附加「可执行动作」建议（确定性规则，不调 LLM）。

    返回 {action, name, reason, target_table, require_approval} 或 None（无可执行动作）。
    只做推荐：写操作仍由 ActionPanel 走「编译预览 → 二次确认 → 执行」闸门。
    """
    if not isinstance(insight, dict):
        return None
    table = str(insight.get("table") or "").lower()
    message = str(insight.get("message") or "")
    hay = f"{table} {message}".lower()
    for keys, action_id, reason in _ACTION_HINTS:
        if any(k in hay for k in keys):
            try:
                from agent.action_agent import ACTIONS
                act = ACTIONS.get(action_id)
                if not act:
                    continue
                return {"action": action_id, "name": act.get("name", action_id),
                        "reason": reason, "target_table": act.get("target_table", ""),
                        "require_approval": act.get("require_approval", True)}
            except Exception:
                return None
    return None


def attach_action_suggestions(insights: list[dict]) -> list[dict]:
    """给洞察列表批量附加 action_suggestion 字段（不修改原对象，返回新列表）。"""
    out = []
    for ins in insights or []:
        item = dict(ins)
        sug = suggest_action_for_insight(item)
        if sug:
            item["action_suggestion"] = sug
        out.append(item)
    return out


def push_insights(insights: list[dict], max_push: int = 5) -> dict:
    """把主动洞察逐条推送给订阅了 insight 事件的通道（交付闭环，对标 Pulse/白泽）。

    与扫描分离：扫描是只读、纯计算；本函数是副作用（外发推送），由调用方显式触发。
    实际是否发送由 notifier 的 NOTIFY_ENABLED 开关 + 通道/订阅配置决定——未开启时
    push_event 直接返回 0，本函数自然空转，零副作用。

    返回 {success, pushed, total, skipped}。
    """
    from agent.notifier import push_event
    total = len(insights or [])
    pushed = 0
    for ins in (insights or [])[:max_push]:
        t = ins.get("type", "insight")
        label = _TYPE_LABELS.get(t, "数据洞察")
        title = f"[{label}] {ins.get('metric') or ''}"
        content = ins.get("message") or ""
        if not content:
            continue
        # event_key 用 表+指标+类型 做节流去重：同一异常在窗口内不重复轰炸
        key = f"{ins.get('table', '')}:{ins.get('metric', '')}:{t}"
        n = push_event("insight", title, content,
                       severity=float(ins.get("severity") or 1.0), event_key=key)
        if n:
            pushed += 1
    return {"success": True, "pushed": pushed, "total": total,
            "skipped": total - pushed}

