# -*- coding: utf-8 -*-
"""双指标分析问法的「确定性方案组装」（零 LLM，秒出）。

背景（2026-09-03）：「停机时长×不良率 是否相关」这类双指标分析问法被强制走二次确认
（见 metric_compiler._suppress_multi_metric_compile，防止被压成单指标静默误答）。
若直接依赖 LLM 推断方案，用户要等 15~40s 且偶发失败（体验=卡 bug）。
本模块在 infer_confirm 前置一条确定性通道：问法命中 ≥2 个已注册指标、且目标表支持
按日期配对（生产质量 mes_process_output / 停机 eqp_downtime_record / 抽检 qms_inspection
/ 工单 mes_work_order / 库存 inv_inventory_snapshot），直接用注册口径拼"按日双列"SQL，
秒级产出与 LLM 推断同结构的 analysis（sql_draft 供前端弹窗"遵循 LLM 执行"）。
组装失败（含问法带明确时间范围、指标表不配对等）返回 None → 调用方回退原 LLM 推断。
"""
import re

# 表 → (时间列, 是否需 date() 包裹为"日期"键)。分组粒度统一为「自然日」。
_DATE_KEY: dict[str, str] = {
    "mes_process_output": "stat_date",
    "eqp_downtime_record": "start_time",   # 时间戳 → date(start_time)
    "qms_inspection": "inspection_date",
    "mes_work_order": "start_date",
    "inv_inventory_snapshot": "snapshot_date",
    "qms_defect_detail": "",               # 无时间列 → 不支持按日配对
}
# 需要 date() 包裹（时间戳/日期混合统一）
_NEED_DATE_WRAP = {"eqp_downtime_record"}

# 问法明确带时间范围 → 回退 LLM（确定性组装只做全期按日，避免臆测过滤）
_TIME_INTENT_RE = re.compile(
    r"近\s*\d+\s*(天|日|周|个月|月|年)|本\s*(月|周|年|季度)|上\s*(月|周|年|季度)|去\s*年|今\s*年"
    r"|\d+\s*月\s*\d+\s*[号日]|\d+\s*月份?|昨天|今天|前天|上周|本周|这个月|当月")


def _hit_metrics(query: str) -> dict[str, dict]:
    """问句中"可见命中"的指标（name→metric）。

    过滤规则：某指标的所有命中词若都只是**另一个更长命中词的子串**（如问句含"不良率"时，
    "良率"是"不良率"的子串 → 良率被连带命中）→ 视为附带噪声忽略，避免把 停机×不良率
    误配成 良率×不良率。每个指标只保留其最长命中词参与判断。
    """
    from agent.metric_registry import get_effective_metrics
    raw: list[tuple[int, str, dict]] = []
    for m in get_effective_metrics():
        words = [str(m.get("name") or "")] + [str(a) for a in (m.get("aliases") or [])]
        for w in words:
            w = re.sub(r"\(.*?\)", "", w).strip()
            if len(w) >= 2 and w in query:
                raw.append((len(w), w, m))
    # 每指标保留最长命中词
    best: dict[str, tuple[int, str, dict]] = {}
    for ln, w, m in raw:
        nm = str(m.get("name") or "")
        if nm not in best or ln > best[nm][0]:
            best[nm] = (ln, w, m)
    visible: dict[str, dict] = {}
    for nm, (ln, w, m) in best.items():
        sub = any(ln < l2 and w in w2 for (l2, w2, _m2) in raw)
        if not sub:
            visible[nm] = m
    return visible


def _metric_by_name(name: str):
    from agent.metric_registry import get_effective_metrics
    for m in get_effective_metrics():
        if str(m.get("name") or "") == name:
            return m
    return None


def _day_key_expr(table: str, col: str) -> str:
    return f"date({col})" if table in _NEED_DATE_WRAP else col


def _dateable(t: str) -> bool:
    """该事实表是否支持按自然日配对（有日期键）。"""
    return bool(_DATE_KEY.get(t))


def _table_of(m: dict) -> str:
    ts = [t for t in (m.get("tables") or []) if t]
    return ts[0] if ts else ""


def _expr_cols_ok(m: dict) -> bool:
    """表达式里引用的裸列必须在注册表实际存在（防注册口径与当前库列不一致）"""
    expr = str(m.get("sql_expression") or "")
    t = _table_of(m)
    if not t or not expr:
        return False
    if not re.search(r"^(SUM|COUNT|AVG|MAX|MIN|COUNT\s*\()", expr) and "CASE" not in expr:
        # 需是聚合表达式
        if not re.search(r"SUM\(|COUNT\(|AVG\(|MAX\(|MIN\(", expr):
            return False
    try:
        from agent.metric_compiler import _table_cols
        cols = _table_cols(t)
    except Exception:
        cols = set()
    if not cols:
        return True  # 拿不到列集时放行（执行阶段仍会校验）
    # 提取裸列 token（小写去聚合壳），粗校验：含 "(" 的列引用允许任意列名（函数/表达式），
    # 仅当存在形如 列名 的直接引用时检查。
    for tok in re.findall(r"\b[a-z_][a-z0-9_]{1,40}\b", expr):
        if tok.lower() in ("sum", "count", "avg", "max", "min", "case", "when", "then",
                           "else", "end", "nullif", "coalesce", "filter", "where", "as",
                           "and", "or", "not", "true", "false", "over", "partition",
                           "distinct", "100", "0", "1", "2", "3", "1000"):
            continue
        if re.search(r"\d", tok):
            continue
        if tok.lower() not in cols:
            # 可能是函数参数里的布尔/关键字残留 → 宽松：仅拦截"明显列名却不存在"
            return False
    return True


def build_pair_plan(query: str) -> dict | None:
    """组装确定性双指标方案；不可组装返回 None（回退 LLM 推断）。"""
    q = (query or "").strip()
    if not q or _TIME_INTENT_RE.search(q):
        return None
    vis = _hit_metrics(q)
    metrics = list(vis.values())
    eqp_tables = {"eqp_downtime_record"}
    has_downtime_word = bool(re.search(r"停机|设备停机|downtime", q, re.IGNORECASE))

    # —— 停机×生产质量 是相关分析骨架：问句含 停机/设备语义但指标别名没覆盖
    #（如"停机时间"若注册别名缺失会漏配）→ 兜底把「停机时长」并入命中集
    if has_downtime_word:
        eqp_m = next((m for m in metrics if _table_of(m) in eqp_tables), None)
        if eqp_m is None:
            _dm = _metric_by_name("停机时长")
            if _dm is not None and str(_dm.get("name")) not in vis:
                vis[str(_dm.get("name"))] = _dm
                metrics.append(_dm)

    # —— 停机×质量句里另一指标别名缺失的兜底（如"缺陷数量"无注册别名、问句未命中任何
    # mes 指标）：按问句业务词补齐 mes_process_output 的近似口径（如实加 risks 注明近似）。
    _added_approx = ""
    eqp_list = [m for m in metrics if _table_of(m) in eqp_tables]
    other_list = [m for m in metrics if _table_of(m) not in eqp_tables]
    if has_downtime_word and eqp_list and not other_list:
        _fallback = None
        if re.search(r"缺陷|不良|次品|废品|rework|返工", q):
            _fallback = _metric_by_name("不良数") or _metric_by_name("缺陷数")
        elif re.search(r"产量|产出|good", q, re.IGNORECASE):
            _fallback = _metric_by_name("产量")
        elif re.search(r"良率|合格|yield", q, re.IGNORECASE):
            _fallback = _metric_by_name("良率")
        if _fallback is not None and str(_fallback.get("name")) not in vis:
            vis[str(_fallback.get("name"))] = _fallback
            metrics.append(_fallback)
            other_list.append(_fallback)
            _added_approx = str(_fallback.get("name") or "")

    # 少于两个可组装指标（如单指标问法"在产工单数"、无对端业务词的停机句）→ 不组装
    if len(metrics) < 2:
        return None

    # 配对优先级：停机域指标 × 其它（生产质量）域 → 其它×其它兜底
    order: list[tuple[dict, dict]] = []
    if eqp_list and other_list:
        for e in eqp_list:
            for o in other_list:
                order.append((e, o))
    # 剩余两两（同域/同表等）
    for i in range(len(metrics)):
        for j in range(i + 1, len(metrics)):
            pair = (metrics[i], metrics[j])
            if pair not in order and (pair[1], pair[0]) not in order:
                order.append(pair)
    # 同表配对优先于跨表（结构更简单）；其次按顺序
    order.sort(key=lambda p: 0 if _table_of(p[0]) == _table_of(p[1]) else 1)
    # 最后兜底：对端指标所在表不可按日配对（如"缺陷件数"在无时间列的缺陷明细表）时，
    # 用 mes_process_output 的「不良数」近似补一个可配对方案（risks 注明近似）
    if eqp_list and (not other_list or not any(_dateable(_table_of(o)) for o in other_list)):
        _fb = _metric_by_name("不良数") or _metric_by_name("缺陷数")
        if _fb is not None and str(_fb.get("name")) not in vis:
            order.append((eqp_list[0], _fb))
            _added_approx = str(_fb.get("name") or "")

    for a, b in order:
        ta, tb = _table_of(a), _table_of(b)
        if not ta or not tb or not _dateable(ta) or not _dateable(tb):
            continue
        if not _expr_cols_ok(a) or not _expr_cols_ok(b):
            continue
        expr_a = str(a.get("sql_expression") or "").strip()
        expr_b = str(b.get("sql_expression") or "").strip()
        if not expr_a or not expr_b:
            continue
        name_a = str(a.get("name") or "指标A")
        name_b = str(b.get("name") or "指标B")
        dka = _day_key_expr(ta, _DATE_KEY[ta])
        dkb = _day_key_expr(tb, _DATE_KEY[tb])
        if ta == tb:
            sql = (f"SELECT {dka} AS \"日期\", {expr_a} AS \"{name_a}\", {expr_b} AS \"{name_b}\" "
                   f"FROM {ta} f GROUP BY {dka} ORDER BY 1")
        else:
            sql = (
                f"WITH A AS (SELECT {dka} AS d, {expr_a} AS x1 FROM {ta} GROUP BY {dka}), "
                f"B AS (SELECT {dkb} AS d, {expr_b} AS x2 FROM {tb} GROUP BY {dkb}) "
                f"SELECT COALESCE(A.d, B.d) AS \"日期\", A.x1 AS \"{name_a}\", B.x2 AS \"{name_b}\" "
                f"FROM A FULL JOIN B ON A.d = B.d ORDER BY 1"
            )
        # 表达式安全：拒绝分号/注释等（注册口径白名单外的一律不组装）
        if re.search(r";|--|/\*|\*/|INSERT|UPDATE|DELETE|DROP", sql, re.IGNORECASE):
            continue
        try:
            from db.executor import execute_sql
            rr = execute_sql(sql)
            if not rr.get("success") or not rr.get("rows"):
                continue
        except Exception:
            continue
        risks = [
            "方案由注册口径确定性组装（非 LLM 推断），可核对下方 SQL",
            "按全部日期聚合，如需限定时间范围请手动修改 SQL 后再执行",
        ]
        if has_downtime_word and not any(_table_of(m) in eqp_tables for m in list(vis.values())):
            risks.append("问句中的设备/停机词未命中注册指标别名，已按注册口径「停机时长」近似配对")
        if _added_approx:
            risks.append(f"问句中的业务词未命中注册指标，已按注册口径「{_added_approx}」近似配对（如需精确口径可自定义）")
        if re.search(r"各\s*(产线|车间|工序|产品|班次)|每个\s*(产线|车间|工序)", q):
            risks.append("问句含维度拆分意图，本方案先按日期汇总两指标；如需按产线/车间拆分，请在弹窗 SQL 中补充分组列")
        return {
            "understanding": (f"按自然日汇总 {name_a} 与 {name_b} 两组数据，"
                              f"用于分析二者关系/差异；指标口径来自注册表，未用 LLM 臆测。"),
            "metrics": [
                {"name": name_a, "agg": "CUSTOM", "column": "",
                 "formula": expr_a, "table": ta},
                {"name": name_b, "agg": "CUSTOM", "column": "",
                 "formula": expr_b, "table": tb},
            ],
            "dimensions": ["日期"],
            "filters": [],
            "time_range": "",
            "confidence": 0.85,
            "risks": risks,
            "sql_draft": sql,
            "deterministic": True,
        }
    return None
