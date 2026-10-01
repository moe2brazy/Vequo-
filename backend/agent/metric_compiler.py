"""指标编译器（阶段五）— 高频刚性指标「零 LLM」生成 SQL

对命中注册指标、且维度/时间可标准化的查询，用模板确定性拼 SQL：
- 结果 100% 可预期、零 token、可作为 LLM 生成结果的「黄金参照系」
- 编译失败一律返回 None → 调用方回退 LLM 生成，绝不硬凑（宁可不编，不可编错）

v2 数据驱动的编译元数据（对标 Wren AI 的 MDL 语义模型）：
- 从「单一 mes_process_output 白名单」扩展为「_FACT_META 多事实表元数据」，
  覆盖全部注册指标的事实表（生产/质量/工单/设备/库存/销售/物料/工厂）。
- 维度支持两种：JOIN 维度表（如 产线→dim_production_line）与 直接分组字符串列（如 状态/仓库/客户）。
- 仍保守：仅 PG；MySQL 回退 LLM；单指标、单维度；任意值过滤（> < =）一律回退。
"""

from __future__ import annotations

import os
import re
import time

from database import get_db_type

# ── 事实表编译元数据（表名 → {时间列, 维度映射}）────────────────
# 维度定义两种形式：
#   JOIN 维度表：{"fact_col": 事实表外键列, "join": (维度表, key列, display列)}
#   直接分组：   {"fact_col": 事实表字符串列, "join": None}
# 维度名即中文显示 label（GROUP BY 后 AS "维度名"）。
_FACT_META = {
    # 生产域
    "mes_process_output": {
        "time_col": "stat_date",
        "dims": {
            "产线": {"fact_col": "line_id", "join": ("dim_production_line", "line_id", "line_name")},
            "车间": {"fact_col": "line_id", "join": ("dim_production_line", "line_id", ["workshop_name", "workshop"])},  # P0-4 层级钻取：产线→车间（同表列级）
            "产品": {"fact_col": "product_id", "join": ("dim_product", "product_id", "product_name")},
            # 产品类别：维度表展示列（非注册指标词，但「各产品类别内…」分组内TopN 等问法需要它做外层维度）。
            # 「类别」后缀是合法维度层级，不是修饰语。
            # 2026-09-19 修复：展示列此前硬编码 "product_category"，而 _resolve_col 对 **str 候选
            # 直接原样返回、不做列存在性校验** —— postgres 库 dim_product 的真实列是 category，
            # 于是编译产物 `... JOIN dim_product d ... GROUP BY d.product_category` 直接报
            # 「字段不存在」，postgres#10/#12/#28 三道**原本通过**的题被编译成坏 SQL（净损失 3 题）。
            # 改为候选列表后由 _resolve_col 查 information_schema 自适应：postgres→category，
            # yans→product_category（该库列名不同），两库各自正确。
            "产品类别": {"fact_col": "product_id", "join": ("dim_product", "product_id", ["category", "product_category"])},
            "工序": {"fact_col": "process_id", "join": ("dim_process", "process_id", "process_name")},
            "班次": {"fact_col": "shift_code", "join": None},   # 直接分组字符串列（D/N 等班次代码）
            # 工单：按 work_order_id 关联 mes_work_order 展示「工单号」（MO-xxxx）而非内部 id。
            # 「投入量最大的10个工单」等问法期望看到业务工单号，直接分组 id 数字答非所问（gold P1）。
            "工单": {"fact_col": "work_order_id", "join": ("mes_work_order", "work_order_id", "work_order_no")},
        },
    },
    # 质量抽检域
    "qms_inspection": {
        "time_col": "inspection_date",
        "dims": {
            # via = 桥接表（两跳 join）：抽检表没有 line_id，需经 mes_work_order 才能到产线。
            # 实测 1376 行全部 join 上、零 NULL、零丢失（多对一，不会放大行数）。
            "产线": {"fact_col": "work_order_id",
                    "via": ("mes_work_order", "work_order_id", "line_id"),
                    "join": ("dim_production_line", "line_id", "line_name")},
            "车间": {"fact_col": "work_order_id",
                    "via": ("mes_work_order", "work_order_id", "line_id"),
                    "join": ("dim_production_line", "line_id", ["workshop_name", "workshop"])},
            "产品": {"fact_col": "product_id", "join": ("dim_product", "product_id", "product_name")},
            "工序": {"fact_col": "process_id", "join": ("dim_process", "process_id", "process_name")},
            "结果": {"fact_col": ["inspection_result", "result"], "join": None},
        },
    },
    # 缺陷明细域（无时间列）
    "qms_defect_detail": {
        "time_col": None,
        "dims": {
            "缺陷类型": {"fact_col": "defect_type", "join": None},
            "不良类型": {"fact_col": "defect_type", "join": None},   # 别名（同列，覆盖「不良类型排行」问法）
            "类型": {"fact_col": "defect_type", "join": None},   # 别名（同列，覆盖「缺陷最多的类型」口语问法）
            "严重度": {"fact_col": ["severity_level", "severity"], "join": None},
            "严重程度": {"fact_col": ["severity_level", "severity"], "join": None},   # 别名（同列，覆盖「严重程度」问法）
            "工序": {"fact_col": ["responsible_process_id", "process_id"], "join": ("dim_process", "process_id", "process_name")},
            "检验": {"fact_col": "inspection_id", "join": None},
            # 2026-09-19 新增：「各产品类别的不良类型分布」(postgres#22) 要按 category 分组，
            # 此前该表未注册产品维度 → 编译器只 GROUP BY defect_type，**丢了问句点名的「产品类别」**，
            # 行集直接不对（gold 7 行 vs 编译 5 行，口径再准也判负）。
            # 展示列走候选列表（postgres→category，yans→product_category）；
            # yans 库该表无 product_id 列，_detect_dim 的列存在性保护会让本维度不命中 → 不影响 yans 回归。
            "产品类别": {"fact_col": "product_id", "join": ("dim_product", "product_id", ["category", "product_category"])},
        },
    },
    # 设备主数据域（yans 基准：equipment_status 状态分布）
    "dim_equipment": {
        "time_col": None,
        "dims": {
            "设备状态": {"fact_col": "equipment_status", "join": None},
            "设备类型": {"fact_col": "equipment_type", "join": None},
            "类型": {"fact_col": "equipment_type", "join": None},   # 别名（同列，覆盖「各类型的设备数」口语问法）
            "产线": {"fact_col": "line_id", "join": ("dim_production_line", "line_id", "line_name")},
            "车间": {"fact_col": "line_id", "join": ("dim_production_line", "line_id", ["workshop_name", "workshop"])},
        },
    },
    # 产品主数据域（yans 基准：product_category 类别分布）
    "dim_product": {
        "time_col": None,
        "dims": {
            "产品类别": {"fact_col": "product_category", "join": None},
            "类别": {"fact_col": "product_category", "join": None},   # 别名（同列）
        },
    },
    # 工序主数据域（yans 基准：standard_yield_rate 标准良率）
    "dim_process": {
        "time_col": None,
        "dims": {
            "工序": {"fact_col": "process_name", "join": None},
        },
    },
    # 产线主数据域（yans 基准：workshop_name 车间分布）
    "dim_production_line": {
        "time_col": None,
        "dims": {
            "车间": {"fact_col": "workshop_name", "join": None},
        },
    },
    # 工单域
    "mes_work_order": {
        "time_col": "start_date",
        "dims": {
            "产线": {"fact_col": "line_id", "join": ("dim_production_line", "line_id", "line_name")},
            "车间": {"fact_col": "line_id", "join": ("dim_production_line", "line_id", ["workshop_name", "workshop"])},
            "产品": {"fact_col": "product_id", "join": ("dim_product", "product_id", "product_name")},
            "状态": {"fact_col": ["order_status", "status"], "join": None},
            "工单状态": {"fact_col": ["order_status", "status"], "join": None},   # 别名（同列，覆盖「工单状态」问法）
        },
    },
    # 设备停机域
    "eqp_downtime_record": {
        "time_col": "start_time",
        "dims": {
            "设备类型": {"fact_col": "equipment_id", "join": ("dim_equipment", "equipment_id", "equipment_type")},
            "设备": {"fact_col": "equipment_id", "join": ("dim_equipment", "equipment_id", "equipment_name")},
            "产线": {"fact_col": "line_id", "join": ("dim_production_line", "line_id", "line_name")},
            "车间": {"fact_col": "line_id", "join": ("dim_production_line", "line_id", ["workshop_name", "workshop"])},
            "原因": {"fact_col": ["downtime_reason", "reason"], "join": None},
            "停机原因": {"fact_col": ["downtime_reason", "reason"], "join": None},   # 别名（同列，覆盖「停机原因」问法）
            "计划类型": {"fact_col": "is_planned", "join": None},   # 布尔列：计划内/计划外（true/false 分组）
        },
    },
    # 库存域
    "inv_inventory_snapshot": {
        "time_col": "snapshot_date",
        "dims": {
            "产品": {"fact_col": "product_id", "join": ("dim_product", "product_id", "product_name")},
            "仓库": {"fact_col": "warehouse_code", "join": None},
        },
    },
    # 销售域
    "test_orders": {
        "time_col": "order_date",
        "dims": {
            "工厂": {"fact_col": "factory_id", "join": ("test_factories", "factory_id", "factory_name")},
            "客户": {"fact_col": "customer_name", "join": None},
            "产品": {"fact_col": "product_name", "join": None},
            "状态": {"fact_col": "status", "join": None},
        },
    },
    # 工厂域（无时间列，仅维度分组）
    "test_factories": {
        "time_col": None,
        "dims": {
            "城市": {"fact_col": "city", "join": None},
        },
    },
    # 物料域（无时间列，仅维度分组）
    "test_materials": {
        "time_col": None,
        "dims": {
            "物料": {"fact_col": "material_name", "join": None},
            "供应商": {"fact_col": "supplier", "join": None},
        },
    },
}

# ── 列名自适应（同一指标在两套库命名下都能编译）────────────────
# postgres 演示库与 yans 大赛库部分列名不同（如 reason / downtime_reason、
# status / order_status、workshop / workshop_name）。维度 fact_col 与 JOIN 展示列
# 支持候选列表，编译时按当前库 information_schema 实际列选择；指标表达式支持
# {colA|colB} 候选语法（_EXPR_SAFE_RE 已放行 '|'），先解析成实际列再走安全检查。
_COL_CACHE: dict = {}
_COL_TTL = 60.0
_TABLE_COLS_CACHE: dict = {}
_TABLE_COLS_TTL = 60.0


def _table_cols(table: str) -> set[str]:
    """当前库某表的实际列集合（TTL 缓存，key 含 db_type:host:port:name）。

    用于维度列存在性校验：表在但列不在（如 yans 的 qms_defect_detail 无 disposal/
    product_id、mes_work_order 无 actual_qty）→ 该维度/指标不命中，避免生成坏 SQL。
    """
    cfg = {}
    try:
        from database import get_database_config
        cfg = get_database_config()
    except Exception:
        pass
    key = (cfg.get("db_type", ""), cfg.get("host", ""), cfg.get("port", 0),
           cfg.get("name", ""), table)
    now = time.time()
    hit = _TABLE_COLS_CACHE.get(key)
    if hit and now - hit[0] < _TABLE_COLS_TTL:
        return hit[1]
    out: set[str] = set()
    try:
        from database import engine
        from sqlalchemy import text as _sql_text
        schema = table.split(".")[0] if "." in table else None
        with engine.connect() as conn:
            if schema:
                rows = conn.execute(_sql_text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = :t"
                ), {"s": schema, "t": table.split(".")[-1]}).fetchall()
            else:
                # 裸表名跨 schema 匹配（对齐 db/tools.get_real_tables）：current_schema()
                # 会漏掉经 search_path 可达的非 public schema 表（如 123 库 factory 表），
                # 导致 required_cols 列级保护/列存在性校验 fail-open（B2 修复）。
                rows = conn.execute(_sql_text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                    "AND lower(table_name) = lower(:t) LIMIT 500"
                ), {"t": table}).fetchall()
        out = {r[0] for r in rows}
    except Exception:
        pass
    _TABLE_COLS_CACHE[key] = (now, out)
    return out


def _resolve_col(table: str, candidates) -> str:
    """把候选列名（str 或 list）解析为当前库实际存在的列，带 TTL 缓存。

    缓存 key 含 db_type:host:port:name（get_database_config），切换数据库
    （postgres↔yans 等）或同名库不同主机后列解析结果自动失效，不会串用。
    表名带 schema 前缀（factory.xxx）时按前缀 schema 查询。
    """
    if isinstance(candidates, str):
        return candidates
    if not candidates:
        return ""
    cfg = {}
    try:
        from database import get_database_config
        cfg = get_database_config()
    except Exception:
        pass
    key = (cfg.get("db_type", ""), cfg.get("host", ""), cfg.get("port", 0),
           cfg.get("name", ""), table, tuple(candidates))
    now = time.time()
    hit = _COL_CACHE.get(key)
    if hit and now - hit[0] < _COL_TTL:
        return hit[1]
    exist: set[str] = set()
    try:
        from database import engine
        from sqlalchemy import text as _sql_text
        schema = table.split(".")[0] if "." in table else None
        with engine.connect() as conn:
            if schema:
                rows = conn.execute(_sql_text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = :t"
                ), {"s": schema, "t": table.split(".")[-1]}).fetchall()
            else:
                # 裸表名跨 schema 匹配（对齐 db/tools.get_real_tables）：current_schema()
                # 会漏掉经 search_path 可达的非 public schema 表（如 123 库 factory 表），
                # 导致 required_cols 列级保护/列存在性校验 fail-open（B2 修复）。
                rows = conn.execute(_sql_text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                    "AND lower(table_name) = lower(:t) LIMIT 500"
                ), {"t": table}).fetchall()
        exist = {r[0] for r in rows}
    except Exception:
        pass
    chosen = next((c for c in candidates if c in exist), candidates[0])
    _COL_CACHE[key] = (now, chosen)
    return chosen


def _resolve_expr(expr: str, fact: str) -> str:
    """把指标表达式里的 {colA|colB} 候选语法解析为当前库实际列。"""
    def _rep(m: re.Match) -> str:
        return _resolve_col(fact, m.group(1).split("|"))
    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_|]*)\}", _rep, expr)


def _resolve_dim_def(fact: str, dim_def: dict) -> dict:
    """解析维度定义的 fact_col 与 JOIN 展示列（列名自适应，返回浅拷贝）。"""
    dd = dict(dim_def)
    dd["fact_col"] = _resolve_col(fact, dd["fact_col"])
    if dd.get("join"):
        dt, dk, disp = dd["join"]
        dd["join"] = (dt, dk, _resolve_col(dt, disp))
    if dd.get("via"):
        vt, von, vkey = dd["via"]
        # 桥接列的候选解析在**桥接表**上做（不是事实表）
        dd["via"] = (vt, _resolve_col(vt, von), _resolve_col(vt, vkey))
    return dd


def dimension_mismatch_hint(query: str) -> str | None:
    """问句点名了系统里注册过的分组维度 X，但命中指标所在的事实表**根本不支持** X
    （既没有对应外键列，也没配 via 桥接）→ 返回一句给用户核对的提示。

    用途：这类题会被编译链守卫放行给 LLM，而 LLM 实测会编出答非所问的分组
    （「各产线库存量」→ `GROUP BY snapshot_id`；库存表压根不关联产线）。
    提示里带上该数据**实际支持的维度**，让用户有可核对的东西。
    """
    try:
        from agent.metric_registry import find_metrics
        hits = find_metrics(query) or []
    except Exception:
        return None
    facts: list[str] = []
    for h in hits:
        for t in (h.get("tables") or []):
            if t in _FACT_META and t not in facts:
                facts.append(t)
    if not facts:
        return None
    all_dims: set[str] = set()
    for _m in _FACT_META.values():
        all_dims.update((_m.get("dims") or {}).keys())
    x = None
    for dn in sorted(all_dims, key=len, reverse=True):
        if re.search(rf"(?:各|按|每个|每|分|哪些|哪个){re.escape(dn)}", query):
            x = dn
            break
    if not x:
        return None
    supported: list[str] = []
    for fact in facts:
        cols = _table_cols(fact)
        for dn, dd in (_FACT_META[fact].get("dims") or {}).items():
            if dd.get("via"):
                supported.append(dn)
                continue
            fc = dd.get("fact_col")
            cands = fc if isinstance(fc, list) else [fc]
            if not cols or any(c in cols for c in cands):
                supported.append(dn)
        if x in supported:
            return None   # 该表支持这个维度，编译失败是别的原因，不是维度不通
    seen = list(dict.fromkeys(supported))[:4]
    tail = f"（这份数据可按 {'、'.join(seen)} 分组）" if seen else "（这份数据没有可直接关联的分组字段）"
    return f"问句里的「{x}」在这份数据里没有对应的分组依据{tail}，下面的分组由 AI 推断，请核对。"


def _dim_join_clause(dim_def: dict, alias: str) -> tuple[str, str]:
    """维度 join 子句 + 与维表 key 比较的列表达式。

    普通维度（一跳）：("", "f.<fact_col>")。
    via 维度（两跳）：(" JOIN <桥接表> <alias>v ON f.<fact_col> = <alias>v.<on列>",
                      "<alias>v.<外键列>")
    —— 事实表没有该维度外键时（如 qms_inspection 无 line_id）经桥接表过渡。
    """
    via = dim_def.get("via")
    if via:
        vt, von, vkey = via
        return (f" JOIN {vt} {alias}v ON f.{dim_def['fact_col']} = {alias}v.{von}",
                f"{alias}v.{vkey}")
    return "", f"f.{dim_def['fact_col']}"


# 派生视图（向后兼容旧调用方）
_COMPILABLE_TABLES = set(_FACT_META)  # 所有可编译事实表
_FACT_TIME_COL = {t: m["time_col"] for t, m in _FACT_META.items() if m.get("time_col")}  # 有时间列的表（同比环比用）


def _pg_numeric(expr: str) -> str:
    """PG 下把 SUM/COUNT/AVG(col) 结果转 numeric，避免整数除法截断（良率等比率指标）。"""
    return re.sub(r"(SUM|COUNT|AVG)\(([a-z_][a-z0-9_.]*)\)", r"\1(\2)::numeric", expr)


def _agg_override(expr: str, query: str) -> str:
    """按问句中的最高级/平均词覆盖聚合函数（仅对「单一 SUM(col)」形态生效）。

    - 平均/均值 → AVG(col)（"各产线的平均合格数"）
    - 最长/最大 → MAX(col)（"最长的一次停机时长"）
    - 最短/最小 → MIN(col)
    比率类表达式（如 SUM(a)/SUM(b)）不覆盖，避免破坏良率等口径。

    关键约束：MAX/MIN 覆盖仅对「单行事件度量列」（downtime_minutes：停机是事件，
    每行独立时长，MAX 语义正确）生效；**累计量指标（产量 good_qty / 库存量
    available_qty / 返工 rework_qty 等）不得覆盖**——「产量最大的产线」正确语义是
    「总产量最大」（SUM 分组后取最大），覆盖成 MAX(单行) 会返回「单日产量最大」的
    错误答案（v2 题库独立双跑抓到：库存量最大的产品/产量最大的产线 数值不一致）。
    """
    m = re.fullmatch(r"SUM\(([a-zA-Z_][a-zA-Z0-9_]*)\)", (expr or "").strip())
    if not m:
        return expr
    col = m.group(1)
    if re.search(r"平均|均值|平均值", query):
        return f"AVG({col})"
    # 最高级词（最长/最大/最短/最小）**不再覆盖聚合函数**（2026-09-14 v4 题库题12 修复）。
    # 原白名单允许 downtime_minutes 被覆盖成 MAX/MIN，但「停机时长最长的设备」正确语义是
    # **按设备分组后 SUM 取最大**（累计停机最久），覆盖成 MAX(downtime_minutes) 返回的是
    # 「单次停机时长最大的设备」——数值（120）与累计（760）完全不是一回事，编译路径 0.1s
    # 高置信答错。单次语义（"最长的一次停机时长"）极罕见，交由 LLM 兜底。故最高级词一律
    # 保持 SUM，排序由调用方 ORDER BY 处理。
    return expr


# 编译期表达式白名单：只允许聚合函数/列引用/算术/比较/NULLIF/COALESCE/CASE 与数字等，
# 出现任何 SQL 注入特征（分号、注释、引号闭合、子查询）一律返回 None 回退 LLM（宁可不编）。
_EXPR_SAFE_RE = re.compile(r"^[\w\s().,+\-*/%<>=:'_|]*$")
_EXPR_FORBIDDEN_RE = re.compile(r";|--|/\*|\*/|\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|UNION)\b", re.IGNORECASE)


def _safe_metric_expr(expr: str) -> bool:
    """判断指标表达式是否可安全编译（防注入）。"""
    if not expr or _EXPR_FORBIDDEN_RE.search(expr):
        return False
    return bool(_EXPR_SAFE_RE.fullmatch(expr))


def _detect_dim(query: str, fact_table: str) -> list[str]:
    """检测该事实表下的维度（需分组信号：各X/按X/每个X/每X/分X/前N名X/X的/的X）。

    按维度名长度降序 + 子串去重，避免「设备」吞掉「设备类型」。
    「X的/的X」覆盖值过滤后缀表达，如「产量大于5000的产线」→ 产线。
    「整个X/全部X/全X」是范围限定词（全量汇总语义），**不是**分组维度
    （如「整个车间的总投入数量」应编译为全车间总量，而非按车间分组——2026-08-31 修复）。
    """
    dims = _FACT_META.get(fact_table, {}).get("dims", {})
    tcols = _table_cols(fact_table)
    found = []
    for cn in sorted(dims, key=len, reverse=True):
        if any(cn in f for f in found):
            continue  # cn 是已命中更长维度的子串 → 跳过
        # 列存在性保护：该维度 fact_col 在当前库不存在（如 yans 的 qms_defect_detail 无
        # product_id/处置方式）→ 不命中，避免编译出坏 SQL（表在列缺的维度）
        _dd = dims[cn]
        _fc = _dd.get("fact_col")
        _cands = _fc if isinstance(_fc, list) else [_fc]
        if tcols and not any(c in tcols for c in _cands):
            continue
        # via 维度额外保护：事实表列存在还不够，桥接表及其两个列都必须真实存在。
        # via 的三元组约定为 str（不做候选列解析），否则跳过该维度回退 LLM ——
        # 宁可不编，也不能生成连不上的 JOIN。
        _via = _dd.get("via")
        if _via and isinstance(_via, (tuple, list)) and len(_via) == 3:
            _vt, _von, _vkey = _via
            if not (isinstance(_von, str) and isinstance(_vkey, str)):
                continue
            _vcols = _table_cols(_vt)
            if _vcols and (_von not in _vcols or _vkey not in _vcols):
                continue
        # 「整个X/全部X/全X/总的X」= 范围限定（总量），非分组维度（P0-4 注册车间维度后暴露的回归；
        # 「总的工序数」→ 总量单值，不能 GROUP BY 工序）
        if re.search(rf"整个{cn}|全部{cn}|全{cn}|总的{cn}", query):
            continue
        # 2026-09-14 补两组「排名目标」形态（实测漏判 → _auto_rank_dim 乱补维度 → 答非所问）：
        #   ① 原式 `最…的?\d+` 不允许「的」和数字之间夹「前」→「不良数量最多的**前5**道工序」漏判；
        #   ② 单位表缺 道/台/种/家/项/位/次，且原式把单位写成必选 →「前5道工序」漏判；
        #   ③ 缺「最晚/最早/最新/最旧」（时间型 Top-N，如「最晚的3条停机记录」）。
        _rank_pat = (
            rf"最(?:大|高|多|久|长|小|低|少|短|晚|早|新|旧)\s*(?:的)?\s*(?:前)?\s*\d+\s*"
            rf"(?:个|条|名|张|批|道|台|种|家|项|位|次)?\s*{cn}"
            rf"|前\s*\d+\s*(?:个|条|名|张|批|道|台|种|家|项|位|次)?\s*{cn}"
        )
        if re.search(rf"各{cn}|按{cn}|每个{cn}|每{cn}|分{cn}|TOP\s*\d+\s*{cn}|{_rank_pat}|的{cn}(?!(?:总库存|库存|库存量|总量))|{cn}的|{cn}最高|{cn}最低|最高的{cn}|最低的{cn}|最多的{cn}|最少的{cn}|最大的{cn}|最小的{cn}|{cn}分析|{cn}分布|{cn}排行|{cn}排名"
                     rf"|哪[个台条道张批家座间种类位名次笔项]{cn}|[一这那某几\d]+[个台条道张批家座间种类位名次笔项]{cn}|哪些{cn}|几台{cn}|哪几个{cn}|这几个{cn}|这些{cn}|{cn}分别|分别{cn}", query, re.IGNORECASE):
            found.append(cn)
    return found


def _trend_grain_fmt(query: str) -> str:
    """趋势时间粒度 → PG to_char 格式：周（IYYY-IW ISO 周）> 日（YYYY-MM-DD）> 月（YYYY-MM）> 季 > 年。

    粒度判定按「显式分组词」优先：显式周词→周；显式日词（每天/每日/逐日/按天/哪天…）→日
    —— 即使句中带月份限定（「2026年6月每天的产量」= 6月内按天，不能因裸"月"字退化成月粒度）；
    显式月词→月；季度词→季；年词→年；兜底裸月字/年字 → 月/年；无 → 日。
    """
    if re.search(r"按\s*(?:周|星期)|每周|每星期|周度|各周|按星期", query):
        return "IYYY-IW"
    if re.search(r"每天|每日|逐日|逐天|按\s*(?:日|天)|各天|各日|日粒度|哪一天|最高的一天|最多的一天|哪天", query):
        return "YYYY-MM-DD"
    if re.search(r"按\s*月|每月|按月|月度|各月|月粒度", query):
        return "YYYY-MM"
    if re.search(r"按\s*季度|每季|每季度|季度粒度", query):
        return "YYYY-Q"
    if re.search(r"按\s*年|每年|按年|年度|年粒度", query):
        return "YYYY"
    if "月" in query:
        return "YYYY-MM"
    if "年" in query:
        return "YYYY"
    return "YYYY-MM-DD"


def _want_date(query: str) -> bool:
    """是否按时间维度（天/月/年等）聚合。"""
    return bool(re.search(r"按\s*(天|日|月|周|季度|年)|每天|每日|每月|按月|按年|趋势|按日期|月度|各日期|各天|逐日|逐天|每周|每星期|按星期|周度|各周|每季度|每季|每半年|各月|单月|的月份|哪些月份|各月份|月份\s*(?:有|里|内|中)", query))


# ── 维度层级钻取（P0-4：dim_hierarchy，对齐白泽多维下钻/FineBI 钻取联动）──
_DRILL_DOWN_RE = re.compile(r"下钻|钻取|细化|继续看|继续查")
# 维度值过滤下钻：问句「{父级值}(里|内|中)?各{子维度}」→ 值前缀提取（如「一车间各产线的产量」→ 值=一车间）
_DRILl_VALUE_RE = re.compile(r"^(.{1,14}?)(?:里|内|中|的)?\s*各")
_SAFE_DIM_VALUE_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9_\-（）()·\s]+$")


def _valid_hierarchy(metric: dict, fact: str) -> list[str]:
    """校验指标 dim_hierarchy 合法：父→子有序、每级均为该事实表已注册维度。非法返回空（降级为普通编译）。"""
    h = metric.get("dim_hierarchy") or []
    dims = _FACT_META.get(fact, {}).get("dims", {})
    return [lv for lv in h if isinstance(lv, str) and lv in dims]


def _drill_value(query: str) -> str | None:
    """提取维度值过滤下钻的值前缀（「一车间各产线的产量」→「一车间」）；无值前缀返回 None。"""
    m = _DRILl_VALUE_RE.search(query)
    if not m:
        return None
    val = m.group(1).strip()
    if not val or val.endswith("各") or not _SAFE_DIM_VALUE_RE.fullmatch(val):
        return None
    # 时间范围词不是维度父级值（「近30天各产线的产量」的「近30天」是时间过滤，
    # 不能生成 workshop='近30天' 这类错误过滤）→ 返回 None 走普通时间聚合。
    # 覆盖：近N天/本月/上月/今年 + 昨天/今天/前天/本周/上周 等单日周词（此前漏掉"昨天"，
    # 导致「昨天各产线的产量」被下钻值提取误判 → 生成 workshop_name='昨天' 错误过滤）
    # + 月份（6月份/6月）+ 日期（6月1号）+ 日期区间（6月1号到7月15号）+ 年份（2026年）
    if re.search(r"近\s*\d+\s*(天|日|周|个月|月|年)|本\s*(月|周|年|季度)|上\s*(月|周|年|季度)|去\s*年|今\s*年|当\s*天|当\s*日"
                 r"|昨\s*(?:天|日|晚)|今\s*(?:天|日|晚)|前\s*天|明\s*天|后\s*天|大前天|大后天|这\s*(?:周|星期)|上\s*(?:周|星期)|下\s*(?:周|星期)"
                 r"|\d+\s*月\s*\d+\s*[号日]\s*(?:到|至|~|—|-|－)\s*\d+\s*月\s*\d+\s*[号日]|\d+\s*月\s*\d+\s*[号日]"
                 r"|\d+\s*月份?|\d+\s*月\s*(?:初|上旬|中旬|下旬|底|份)|20\d{2}\s*年|\d{4}\s*-\s*\d{1,2}", val):
        return None
    # 口语/分析前缀不是维度父级值（「看看各产线的产量/帮我查一下各车间的设备数/分析各产线的产量」
    # 的「看看/帮我查一下/分析」是口语助词或分析动词，不能生成 line_name='看看'/'分析' 这类错误过滤）
    if re.search(r"看看|帮我|给我|查一下|麻烦|请问|我想知道|有没有|统计|分析|汇总|查看|看看|浏览|对比|比较"
                 r"|计算|算一算|跑一下|梳理|拆解|研究|评估|解读|输出|列出|展示|显示|给我|我们|所有|全部|目前|现在|一下", val):
        return None
    # ── 2026-09-28 加：值必须真实存在于某张维表（确定性校验 + 长短语否决）──
    # 病根示例（实测）：「传感器这个产品类别下各产线的产量」被切成
    # val='传感器这个产品类别下' → 生成 `WHERE d.workshop_name = '传感器这个产品类别下'`
    # → SQL 跑通、0 行、静默返回（用户看到「没数据」，比报错更难排查）。
    # 「一车间各产线的产量」的 val='一车间' 之所以对，是因为它**恰是真实取值**；
    # 而 '传感器这个产品类别下' 在任何维表列里都不存在 —— 这条件本身就能把两类分开。
    # 数据来源：llm_service 已维护的「枚举值 → 表.列」倒排索引（按库落盘缓存 1h），
    # 零额外 DB 成本；索引不可用时**退回原行为**（只做长度否决），不改变既有语义。
    # 长短语（>8 字）几乎必然是"用户自己的说法"而非列取值（真实枚举值都很短：
    # 一车间/传感器/L04/设备故障），先按长度否决，省一次索引查询。
    if len(val) > 8:
        return None
    if not _drill_value_exists(val):
        return None
    return val[:14]


def _explicit_value_filters(query: str, fact: str) -> list[str]:
    """问句里**点名**了维表枚举值 → 返回 EXISTS 形式的 WHERE 子句（可为空列表）。

    场景与边界（2026-09-28 新增，配套 _drill_value 的真实值校验）：
      「传感器这个产品类别下各产线的产量」
        → 传感器 是 dim_product.product_category 的真实取值 → 加
          EXISTS (SELECT 1 FROM dim_product p WHERE p.product_id = f.product_id
                  AND p.product_category = '传感器')
      「一车间各产线的良率」
        → **不生成**：一车间是层级父级值，由 _drill_value 的下钻通道处理
          （两条通道都开会产生重复条件）。

    规则（全部确定性，不引入任何猜测）：
      ① 值必须**逐字**出现在库内某低基数文本列（来自倒排索引）；
      ② 该值所在的列必须能被本次事实表 JOIN 到（同名 `X_id` 关联）——否则宁可
         不加（例如值在另一张无关表里）；
      ③ 该列不能是**本次分组的维度列**（那是分组语义，不是过滤语义）；
      ④ 该列不能是时间列；
      ⑤ 层级父级值交给 _drill_value，本函数跳过（见上）。
    """
    out: list[str] = []
    q = str(query or "")
    if not q or not fact:
        return out
    try:
        from agent.llm_service import (_literal_table_index, _all_table_columns,
                                       _safe_ident)
    except Exception:
        return out
    try:
        idx = _literal_table_index() or {}
    except Exception:
        return out
    if not idx:
        return out
    try:
        cm = _all_table_columns() or {}
    except Exception:
        cm = {}
    # 事实表的列（用于确认 JOIN 键存在）
    fact_cols: set[str] = set()
    for k, v in (cm or {}).items():
        if str(k).split(".")[-1].lower() == fact:
            fact_cols = {str(c).lower() for c in (v or [])}
            break
    if not fact_cols:
        return out
    meta = _FACT_META.get(fact, {})
    # 本次问题命中/可能分组的维度列名（这些列是分组语义，不当过滤用）
    dim_cols: set[str] = set()
    for _lb, _cfg in (meta.get("dims") or {}).items():
        if isinstance(_cfg, dict):
            jo = _cfg.get("join")
            if jo:
                dim_cols.add(str(jo[0]).lower())
            if _cfg.get("fact_col"):
                dim_cols.add(str(_cfg["fact_col"]).lower())
    time_col = str(meta.get("time_col") or "").lower()
    seen_vals: set[str] = set()
    # 本次问句里是否**显式出现分组信号**（各/每/分别/按…）。
    # 用于区分「值过滤」与「值分组」：同一列同一取值，两种语义的产物完全不同 ——
    #   「贴片机设备一共有多少台」→ WHERE equipment_type='贴片机'（过滤，答案 6）
    #   「各设备类型的设备数」   → GROUP BY equipment_type（分组，答案 8 行 ×6）
    # 判据取「该取值在问句中出现的**上下文**」：若紧邻其左侧是分组词（各/每/按/分别），
    # 则该取值属分组语义，不做值过滤；否则视为点名过滤。
    # 2026-09-29 新增：此前无条件跳过 dim_cols 导致「贴片机设备一共有多少台」编译成
    # `COUNT(*) FROM dim_equipment`（48 台，真值 6 台）——高置信度错答。
    _GROUP_SIG = ("各", "每", "按", "分别", "每个", "所有", "全部", "各个")

    def _is_group_context(val: str) -> bool:
        _i = q.find(val)
        if _i < 0:
            return False
        _left = q[max(0, _i - 2):_i]
        return any(_left.endswith(_g) for _g in _GROUP_SIG)

    for val, ents in sorted(idx.items(), key=lambda x: -len(str(x[0]))):
        if len(val) < 2 or val in seen_vals:
            continue
        if val not in q:
            continue
        for ent in (ents or []):
            try:
                tab, col = str(ent[0]).lower(), str(ent[1]).lower()
            except Exception:
                continue
            # ③④ 维度列 / 时间列不做值过滤（但见上方说明：处于非分组上下文时例外）
            if col in dim_cols and tab == fact and _is_group_context(val):
                continue
            if col == time_col:
                continue
            # ② 事实表要能 JOIN 到该表：同名 X_id（表名去掉 dim_ 前缀后 + _id）
            if tab == fact:
                # 同表列：直接等值
                if col in fact_cols and _safe_ident(col):
                    cond = f"f.{col} = '{_esc_sql_literal(val)}'"
                    if cond not in out:
                        out.append(cond)
                        seen_vals.add(val)
                break
            base = tab[4:] if tab.startswith("dim_") else tab
            jkey = f"{base}_id"
            if jkey in fact_cols and _safe_ident(col) and _safe_ident(tab) and _safe_ident(jkey):
                cond = (f"EXISTS (SELECT 1 FROM {tab} _v WHERE _v.{jkey} = f.{jkey} "
                        f"AND _v.{col} = '{_esc_sql_literal(val)}')")
                if cond not in out:
                    out.append(cond)
                    seen_vals.add(val)
            break
        if len(out) >= 2:   # 最多两个点名值，避免 prompt/SQL 膨胀
            break
    return out


def _esc_sql_literal(v: str) -> str:
    """SQL 字符串字面量转义（单引号翻倍）。值来自库内真实枚举，此处只做防御。"""
    return str(v or "").replace("'", "''")


def _drill_value_exists(val: str) -> bool:
    """val 是否是「库内某张表某个低基数文本列」的真实取值（倒排索引判定）。

    索引不可用/为空时返回 True（放宽，保持旧行为）——这道校验只做**否决**，
    绝不引入新的下钻值，因此放宽不会产生新的错误过滤，只会退回旧表现。
    """
    if not val:
        return False
    try:
        from agent.llm_service import _literal_table_index
        idx = _literal_table_index() or {}
    except Exception:
        return True
    if not idx:
        return True
    try:
        return val in idx
    except Exception:
        return True


def _drill_value_belongs(val: str, meta: dict, parent_level: str) -> bool:
    """val 是否**属于父级维度那一列**的真实取值（比 _drill_value_exists 更严）。

    父级维度的候选列 = 该维度注册的展示列（含 JOIN 目标表的列）。
    例：「产线」的父级是「车间」，其展示列为 dim_production_line.workshop_name，
    取值 {一车间/二车间/三车间}；`设备故障` 不在此集合内 → 拒绝下钻。

    取不到注册信息时返回 True（放宽）；索引不可用时同样放宽。
    """
    if not val:
        return False
    try:
        cfg = (meta.get("dims") or {}).get(parent_level) or {}
        if not isinstance(cfg, dict):
            return True
        cols: list[str] = []
        join = cfg.get("join")
        if join and len(join) >= 3:
            disp = join[2]
            if isinstance(disp, str):
                cols.append(disp)
            elif isinstance(disp, (list, tuple)):
                cols.extend([str(x) for x in disp if x])
        elif cfg.get("fact_col"):
            cols.append(str(cfg["fact_col"]))
        if not cols:
            return True
        try:
            from agent.llm_service import _literal_table_index
            idx = _literal_table_index() or {}
        except Exception:
            return True
        if not idx:
            return True
        ents = idx.get(val) or []
        for _ent in ents:
            try:
                col = str(_ent[1])
            except Exception:
                continue
            if col in cols:
                return True
        return False
    except Exception:
        return True


def _auto_rank_dim(query: str, meta: dict, time_col: str | None) -> str | None:
    """排行/最高级意图但问句没写任何维度词时（「列出抽检数最高的 TOP10」「抽检数排行」），
    自动补一个表内已注册维度做分组，避免编译器退化成「整体汇总一行」答非所问。

    - 日期诉求（哪天最多/每天）→ 优先取时间列对应的注册维度；
    - 其余排行 → 优先带 JOIN 的业务维度（可显示中文名，如 产品/工序/产线），否则首个注册维度；
    - 没有任何可分组维度 → None（保持原汇总/回退路径，不瞎编）。
    """
    # 2026-10-01 补 最优/最久/最佳：_left_guard_vocab 加入这三个最高级词后，
    # 「最优良率」「最久停机时长」左侧能剥干净 → 编译命中，但此处 rank_intent 不认，
    # 无维度时不补分组 → 产物退化成「全厂汇总一行」（最优良率 返回整体良率），
    # 比走 LLM 更糟：行数对、指标对、语义全错且看不出来。实测 3 词 × 9 指标 = 27 条全中招。
    rank_intent = bool(re.search(
        r"TOP\s*\d+|最高|最低|最多|最少|最大|最小|最长|最短|最好|最差|最晚|最早|最新|最旧"
        r"|最优|最久|最佳"
        r"|哪些|哪个|排名|排行|前\s*\d+\s*名", query, re.IGNORECASE))
    if not rank_intent:
        return None
    dcfg = meta.get("dims") or {}
    if not isinstance(dcfg, dict) or not dcfg:
        return None
    date_intent = bool(re.search(r"哪天|哪一天|那一天|当天|当日|每日|每天|哪个月|哪周", query))
    if date_intent:
        for lb, cfg in dcfg.items():
            if isinstance(cfg, dict) and cfg.get("fact_col") == time_col:
                return lb
        return None  # 表没有时间维度注册 → 不瞎补
    joined = [lb for lb, cfg in dcfg.items() if isinstance(cfg, dict) and cfg.get("join")]
    pool = joined or list(dcfg.keys())
    return pool[0] if pool else None


def _resolve_drill(query: str, metric: dict, fact: str, dims: list[str]) -> tuple[str | None, dict | None]:
    """P0-4 钻取解析：返回 (最终分组维度, drillable信息或None)。

    - 命中维度在层级中且问句带父级值前缀 → 值过滤下钻（如「一车间各产线的产量」）；
    - 问句含下钻词 → 命中维度切换到层级下一级；
    - 命中维度非末级 → 输出 drillable（前端图表点击联动素材）。
    """
    hierarchy = _valid_hierarchy(metric, fact)
    if not hierarchy or not dims:
        return (dims[0] if dims else None), None
    dim = dims[0]
    drillable = None
    if dim in hierarchy:
        idx = hierarchy.index(dim)
        # 值过滤下钻：命中子级且问句带父级值 → 该值限定在父级展示列（WHERE d.parent_col = 值）
        if idx > 0:
            val = _drill_value(query)
            # 2026-09-28 加：值必须属于**本层级父级维度**那一列的真实取值。
            # 只校验"值存在"不够——「设备故障各产线的停机时长」里 `设备故障` 是
            # eqp_downtime_record.downtime_reason 的真实取值，但它不是"产线"的父级
            # （产线的父级是车间），若放行会生成把停机原因当车间用的错误过滤。
            # 这里按父级维度的注册列（含 JOIN 展示列）做精确匹配，两张都不匹配就放弃下钻，
            # 退回普通聚合（宁可少做一层下钻，不可生成语义错误的 WHERE）。
            if val and _drill_value_belongs(val, metric, hierarchy[idx - 1]):
                return dim, {"filter_value": val, "parent_level": hierarchy[idx - 1]}
        # 下钻词：切到下一级（「各产线的产量，下钻到设备」等）
        if _DRILL_DOWN_RE.search(query) and idx + 1 < len(hierarchy):
            return hierarchy[idx + 1], None
        # 非末级 → 可继续下钻（前端联动）
        if idx + 1 < len(hierarchy):
            drillable = {"current_level": dim, "next_level": hierarchy[idx + 1], "hierarchy": hierarchy}
    return dim, drillable


def _parse_value_filter(query: str) -> tuple[str, float, bool] | None:
    """解析「指标 + 比较词 + 数值」的简单值过滤 → (SQL 比较符, 数值, 是否百分数)；不支持返回 None。

    支持中文比较词与符号（大于/超过/高于/≥/> → >=；小于/低于/不超过/≤/< → <=），
    数值支持小数与百分号（95% → 数值 95.0 + is_percent=True，由调用方按指标单位决定尺度）。
    仅数值比较（防注入），其他谓词回退 LLM。
    """
    m = re.search(r"(大于|超过|高于|不少于|不低于|至少|>=|>)\s*(\d+(?:\.\d+)?)\s*(%|％)?", query)
    if m:
        return ">=", float(m.group(2)), bool(m.group(3))
    m = re.search(r"(小于|低于|不超过|至多|最多|<=|<)\s*(\d+(?:\.\d+)?)\s*(%|％)?", query)
    if m:
        return "<=", float(m.group(2)), bool(m.group(3))
    return None


_CN_DIGITS = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_num(token: str) -> int | None:
    """中文数字 → int。覆盖 一~十 / 十五 / 二十 / 三十二，无法解析返回 None。

    2026-10-01 新增：时间词表此前只认阿拉伯数字（``近\\s*(\\d+)\\s*个月``），
    「最近三个月」「近三个月」「过去半年」全部落空 → time_range 被静默丢弃、
    SQL 无 WHERE、返回全历史。库里只有两个月数据时答案看着是对的，数据一全就是错答案。
    """
    t = (token or "").strip()
    if not t:
        return None
    if t.isdigit():
        return int(t)
    if not all(ch in _CN_DIGITS for ch in t):
        return None
    if t == "十":
        return 10
    if "十" in t:
        hi, _, lo = t.partition("十")
        return (_CN_DIGITS.get(hi, 1) if hi else 1) * 10 + (_CN_DIGITS.get(lo, 0) if lo else 0)
    return _CN_DIGITS.get(t)


def _build_time_filter(fact_table: str, fact_col: str, query: str) -> str | None:
    """翻译「近N天/近N个月/上月/本月/今年/去年」→ PG 日期过滤。

    锚点语义区分：
    - 相对区间（近N天/近N个月）：锚点用 `MAX(事实列)`，静态/滞后数据集下仍能命中数据；
    - 绝对日历周期（今年/去年/本季度/上季度）：锚点用 `CURRENT_DATE`，符合日历语义；
    - **月级**日历周期（本月/上月）：锚点同样用 `MAX(事实列)`，不再用 `CURRENT_DATE`。

    2026-09-29 规则审计实测（34 条真实 SQL 回放）：`CURRENT_DATE` 锚点在滞后数据集上
    恒为空——库内数据最新到 2026-07-15、今天是 09-29，「本月」「上月」条件必然 0 行，
    此前只能等 LLM 链路下游的改写规则事后救（实测救回 2 次）。
    移到编译期做：同一问句永远同一条 SQL，不必靠改写链反推事实表与时间列。
    口径变化（"本月"实际查的是最新有数据的那个月）由 llm_service 补发说明。
    """
    anchor = f"(SELECT MAX({fact_col}) FROM {fact_table})"
    # 绝对年份/月份「2026年」「2026年6月」（字面日期区间，静态历史库仍命中）
    ym = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", query)
    if ym:
        y, mo = int(ym.group(1)), int(ym.group(2))
        y2, mo2 = (y + 1, 1) if mo == 12 else (y, mo + 1)
        return (f"{fact_col} >= '{y}-{mo:02d}-01' AND "
                f"{fact_col} < '{y2}-{mo2:02d}-01'")
    y = re.search(r"20\d{2}\s*年", query)
    if y:
        yy = int(y.group(0).strip("年 "))
        return (f"{fact_col} >= '{yy}-01-01' AND {fact_col} < '{yy + 1}-01-01'")
    # 相对单日/周（CURRENT_DATE 锚点）：昨天/今天/前天 / 本周/上周
    if re.search(r"昨\s*(?:天|日|晚)", query):
        return (f"{fact_col} >= CURRENT_DATE - INTERVAL '1 day' "
                f"AND {fact_col} < CURRENT_DATE")
    if re.search(r"今\s*(?:天|日)", query):
        return f"{fact_col} >= CURRENT_DATE"
    if re.search(r"前\s*天", query):
        return (f"{fact_col} >= CURRENT_DATE - INTERVAL '2 days' "
                f"AND {fact_col} < CURRENT_DATE - INTERVAL '1 day'")
    if re.search(r"(?:本|这)\s*(?:周|星期)|本周", query):
        return f"{fact_col} >= date_trunc('week', CURRENT_DATE)"
    if re.search(r"上\s*(?:周|星期)|上周", query):
        return (f"{fact_col} >= date_trunc('week', CURRENT_DATE) - INTERVAL '7 days' "
                f"AND {fact_col} < date_trunc('week', CURRENT_DATE)")
    m = re.search(r"近\s*(\d+)\s*天", query)
    if m:
        # 「近N天」= 锚点日 + 过去 N-1 天，共 N 个日历日；INTERVAL 'N days' 会多算 1 天
        n = int(m.group(1))
        return f"{fact_col} >= {anchor} - INTERVAL '{max(0, n - 1)} days'"
    m = re.search(r"近\s*(\d+)\s*个月", query)
    if m:
        return f"{fact_col} >= {anchor} - INTERVAL '{m.group(1)} months'"
    # ── 中文数字 /「过去」/「半年」兜底（2026-10-01）──────────────────
    # 上面两条只吃阿拉伯数字，这里是纯增量：只有上面都没命中才走到这儿，
    # 因此不会改变任何原有问句的编译结果。
    m = re.search(r"(?:近|最近|过去|前)\s*([0-9]+|[一二两三四五六七八九十]+)\s*个?\s*月", query)
    if m:
        n = _cn_num(m.group(1))
        if n:
            return f"{fact_col} >= {anchor} - INTERVAL '{n} months'"
    m = re.search(r"(?:近|最近|过去|前)\s*([0-9]+|[一二两三四五六七八九十]+)\s*(?:个)?\s*(?:天|日)", query)
    if m:
        # 与「近N天」同一口径：锚点日 + 过去 N-1 天，共 N 个日历日
        n = _cn_num(m.group(1))
        if n:
            return f"{fact_col} >= {anchor} - INTERVAL '{max(0, n - 1)} days'"
    if re.search(r"(?:近|最近|过去|前|这)\s*半年|半年(?:以|之)?内", query):
        return f"{fact_col} >= {anchor} - INTERVAL '6 months'"
    if "上月" in query or "上个月" in query:
        return (f"{fact_col} >= date_trunc('month', {anchor}) - INTERVAL '1 month' "
                f"AND {fact_col} < date_trunc('month', {anchor})")
    if "本月" in query or "这个月" in query:
        return f"{fact_col} >= date_trunc('month', {anchor})"
    if "本季度" in query or "本季" in query or "季初至今" in query or "本季至今" in query:
        return f"{fact_col} >= date_trunc('quarter', CURRENT_DATE)"   # QTD：本季度至今
    if "上季度" in query or "上季" in query:
        return f"{fact_col} >= date_trunc('quarter', CURRENT_DATE) - INTERVAL '3 months' AND {fact_col} < date_trunc('quarter', CURRENT_DATE)"
    if "今年" in query or "本年" in query or "年初至今" in query or "今年以来" in query:
        return f"{fact_col} >= date_trunc('year', CURRENT_DATE)"      # YTD：年初至今
    if "去年" in query:
        return f"{fact_col} >= date_trunc('year', CURRENT_DATE) - INTERVAL '1 year' AND {fact_col} < date_trunc('year', CURRENT_DATE)"
    return None


# ── 同比/环比（period-over-period）确定性编译 ──────────────

_COMPARE_YOY = re.compile(r"同比|去年同期|较去年同期|同比增长")
_COMPARE_MOM = re.compile(r"环比|较上月|较上期|环比增长|较上季|较上季度")


def _has_compare_intent(query: str) -> bool:
    return bool(_COMPARE_YOY.search(query) or _COMPARE_MOM.search(query))


# ── 累计窗口（P0-3：cumulative 指标类型，对齐白泽/极昆仑时间智能）──
# 仅对「明确时间窗口累计」语义触发（本年累计/年初至今/累计到本月/近N天累计/本季累计）。
# 裸「累计」（合计语义，如「各车间的累计产量」）**不**触发，维持既有编译行为（零回归）。
_CUMULATIVE_YTD_RE = re.compile(r"本年累计|年初至今|今年以来累计|全年累计|本年至今|YTD|ytd")
_CUMULATIVE_QTD_RE = re.compile(r"本季累计|季初至今|本季度累计|本季至今|QTD|qtd")
_CUMULATIVE_MTD_RE = re.compile(r"本月累计|累计到本月|月初至今|本月至今|当月累计|MTD|mtd")
_CUMULATIVE_NDAYS_RE = re.compile(r"(?:近|最近)\s*(\d+)\s*天(?:的)?累计")
_CUMULATIVE_INTENT_RE = re.compile(
    r"本年累计|年初至今|今年以来累计|全年累计|本年至今|YTD|ytd|"
    r"本季累计|季初至今|本季度累计|本季至今|QTD|qtd|"
    r"本月累计|累计到本月|月初至今|本月至今|当月累计|MTD|mtd|"
    r"(?:近|最近)\s*\d+\s*天(?:的)?累计"
)


def compile_cumulative(query: str) -> dict | None:
    """确定性编译「时间窗口累计」：本年/本季/本月/近N天 → 按周期累计序列。

    仅支持：单指标 + 单事实表（有时间列）+ 无分组维度 + PG。失败返回 None（回退 LLM）。
    累计序列（period + 窗口累计 SUM）同时满足「总量」与「趋势」两种读法，评测兼容性好。
    """
    if get_db_type() == "mysql":
        return None
    from agent.metric_registry import find_metrics
    hits = find_metrics(query)
    if len(hits) != 1:
        return None
    m = hits[0]
    tables = m.get("tables") or []
    if len(tables) != 1 or tables[0] not in _FACT_TIME_COL:
        return None
    fact = tables[0]
    time_col = _FACT_TIME_COL[fact]
    if _detect_dim(query, fact):
        return None  # 累计 + 维度分组 → 复杂，回退 LLM（保守）
    expr = _pg_numeric(m.get("sql_expression") or "")
    if not expr or not _safe_metric_expr(expr):
        return None
    name = m["name"].split("(")[0].strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fa5 ]+", name):
        return None

    anchor = f"(SELECT MAX({time_col}) FROM {fact})"
    nd = _CUMULATIVE_NDAYS_RE.search(query)
    if nd:
        n = int(nd.group(1))
        where = f"{time_col} >= {anchor} - INTERVAL '{max(0, n - 1)} days'"
        fmt, window, period_label = "YYYY-MM-DD", f"近{n}天", "日期"
    elif _CUMULATIVE_YTD_RE.search(query):
        where = f"{time_col} >= date_trunc('year', CURRENT_DATE)"
        fmt, window, period_label = "YYYY-MM", "本年至今(YTD)", "月份"
    elif _CUMULATIVE_QTD_RE.search(query):
        where = f"{time_col} >= date_trunc('quarter', CURRENT_DATE)"
        fmt, window, period_label = "YYYY-MM", "本季至今(QTD)", "月份"
    elif _CUMULATIVE_MTD_RE.search(query):
        # 与 `_build_time_filter` 的「本月」保持一致：锚到库内最新数据月，
        # 否则滞后数据集下 MTD 恒为 0 行（YTD/QTD 仍用日历锚点，语义是"今年/本季"）。
        where = f"{time_col} >= date_trunc('month', {anchor})"
        fmt, window, period_label = "YYYY-MM-DD", "本月至今(MTD)", "日期"
    else:
        return None

    sql = (
        f"WITH base AS ("
        f"SELECT to_char(f.{time_col}, '{fmt}') AS \"{period_label}\", {expr} AS v "
        f"FROM {fact} f WHERE {where} GROUP BY 1) "
        f'SELECT "{period_label}", SUM(v) OVER (ORDER BY "{period_label}") AS "{name}(累计)" '
        f'FROM base ORDER BY "{period_label}" LIMIT 200'
    )
    mql = {
        "metric": name,
        "metric_key": m.get("name"),
        "metric_expression": m.get("sql_expression"),
        "metric_type": "cumulative",
        "window": window,
        "fact_table": fact,
        "value_filter": None,
        "compiled_by": "cumulative_compile",
    }
    return {
        "sql": sql, "title": f"{name}{window}累计", "metric": name,
        "metrics": [name], "unit": m.get("unit", ""), "tables": [fact],
        "compiled": True, "mql": mql,
    }


def _compare_ranges(query: str, anchor: str, time_col: str, is_yoy: bool):
    """计算本期与对比期的日期区间，返回 (cur_gte, cur_lt, prev_gte, prev_lt, prev_label)。

    返回 None 表示无法确定周期（回退 LLM）。
    """
    m = re.search(r"近\s*(\d+)\s*天", query)
    if m:
        n = int(m.group(1))
        cur_gte = f"{time_col} >= {anchor} - INTERVAL '{n} days'"
        cur_lt = f"{time_col} <= {anchor}"
        if is_yoy:
            prev_gte = f"{time_col} >= {anchor} - INTERVAL '{n} days' - INTERVAL '1 year'"
            prev_lt = f"{time_col} <= {anchor} - INTERVAL '1 year'"
            prev_label = f"去年同{n}天"
        else:
            prev_gte = f"{time_col} >= {anchor} - INTERVAL '{2 * n} days'"
            prev_lt = f"{time_col} < {anchor} - INTERVAL '{n} days'"
            prev_label = f"前{n}天"
        return cur_gte, cur_lt, prev_gte, prev_lt, prev_label
    if "本月" in query or "这个月" in query:
        ms = f"date_trunc('month', {anchor})"
        cur_gte = f"{time_col} >= {ms}"
        cur_lt = f"{time_col} <= {anchor}"
        if is_yoy:
            prev_gte = f"{time_col} >= {ms} - INTERVAL '1 year'"
            prev_lt = f"{time_col} <= {anchor} - INTERVAL '1 year'"
            prev_label = "去年同月"
        else:
            # 期长对齐（关键，2026-10-01 修复）：上期上界 = anchor 往前推一个周期（闭区间），
            # 而不是「上月整月」。数据只到月中时，上月整月(30天) vs 本月至今(15天)
            # 会让环比系统性大幅偏负（日均明明持平也显示 -50%）。
            prev_gte = f"{time_col} >= {ms} - INTERVAL '1 month'"
            prev_lt = f"{time_col} <= {anchor} - INTERVAL '1 month'"
            prev_label = "上月同期"
        return cur_gte, cur_lt, prev_gte, prev_lt, prev_label
    if "本季度" in query or "本季" in query:
        qs = f"date_trunc('quarter', {anchor})"
        cur_gte = f"{time_col} >= {qs}"
        cur_lt = f"{time_col} <= {anchor}"
        if is_yoy:
            prev_gte = f"{time_col} >= {qs} - INTERVAL '1 year'"
            prev_lt = f"{time_col} <= {anchor} - INTERVAL '1 year'"
            prev_label = "去年同季"
        else:
            prev_gte = f"{time_col} >= {qs} - INTERVAL '3 months'"
            prev_lt = f"{time_col} <= {anchor} - INTERVAL '3 months'"
            prev_label = "上季度同期"
        return cur_gte, cur_lt, prev_gte, prev_lt, prev_label
    if "今年" in query or "本年" in query:
        ys = f"date_trunc('year', {anchor})"
        cur_gte = f"{time_col} >= {ys}"
        cur_lt = f"{time_col} <= {anchor}"
        if is_yoy:
            prev_gte = f"{time_col} >= {ys} - INTERVAL '1 year'"
            prev_lt = f"{time_col} <= {anchor} - INTERVAL '1 year'"
            prev_label = "去年同期"
        else:
            prev_gte = f"{time_col} >= {ys} - INTERVAL '1 year'"
            prev_lt = f"{time_col} <= {anchor} - INTERVAL '1 year'"
            prev_label = "去年同期"
        return cur_gte, cur_lt, prev_gte, prev_lt, prev_label
    return None


_WIN_RANK_RE = re.compile(r"排名是多少|的名次|名次是|排名值|第几名")
_WIN_TOPN_RE = re.compile(
    r"各[\u4e00-\u9fa5]{1,4}(?:内|中|里)?[\u4e00-\u9fa5]{0,12}?"
    r"(?:最高|最低|最多|最少|最大|最小|第[一二三四五六七八九十两\d]+[高低大]?)\s*(?:的)?\s*[\u4e00-\u9fa5]{1,4}")
_WIN_INNER_RE = re.compile(
    r"(?:最高|最低|最多|最少|最大|最小|第[一二三四五六七八九十两\d]+[高低大]?)\s*(?:的)?\s*"
    r"([\u4e00-\u9fa5]{1,4}?)(?=\s|$|是|哪|，|。|？|!|！)")
_WIN_RN_RE = re.compile(r"第([一二三四五六七八九十两\d]+)[高低大]?")
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def compile_window_rank(query: str) -> dict | None:
    """确定性编译「窗口函数」问法：名次列(RANK) / 分组内Top-N(ROW_NUMBER PARTITION BY)。

    上一轮高价值题测评实测（13 题）：窗口函数 6 题全灭、4 题 GEN_FAIL —— LLM
    （deepseek-v4.1-flash）对 ROW_NUMBER OVER / RANK 生成能力严重不足，但这类题
    **本质是确定算法**（名次、分组内取最高/最低/第N），本应确定性编译。

    两种模式：
    A 名次列：「各产线的产量名次是多少」 → RANK() OVER (ORDER BY 度量) + 维度
    B 分组内TopN：「各车间内产量第二高的产线是哪个」 → ROW_NUMBER() PARTITION BY 外层
       取 rn=N（外层=「各X内」的 X，内层=排序词后的实体 Z）

    约束：单指标 + 单事实表 + 维度均为注册维度；任何不满足 → 返回 None 回退 LLM（宁可不编）。
    """
    if get_db_type() == "mysql":
        return None
    from agent.metric_registry import find_metrics
    hits = find_metrics(query)
    if len(hits) != 1:
        return None
    m = hits[0]
    tables = m.get("tables") or []
    if len(tables) != 1 or tables[0] not in _COMPILABLE_TABLES:
        return None
    fact = tables[0]
    meta = _FACT_META[fact]
    expr = _pg_numeric(_agg_override(_resolve_expr(m.get("sql_expression") or "", fact), query))
    if not expr or not _safe_metric_expr(expr):
        return None
    name = m["name"].split("(")[0].strip()
    unit = m.get("unit", "")
    dims_meta = meta.get("dims") or {}

    def _dim_def(dim: str) -> dict | None:
        return _resolve_dim_def(fact, dims_meta[dim]) if dim in dims_meta else None

    def _dir() -> str:
        if re.search(r"最低|最少|最小|最短|最差|最早|最旧|最慢", query):
            return "ASC"
        return "DESC"

    # ── 模式 A：名次列 ──
    if _WIN_RANK_RE.search(query):
        dims = _detect_dim(query, fact)
        if len(dims) != 1:
            return None
        dim = dims[0]
        ddef = _dim_def(dim)
        if not ddef:
            return None
        # via（两跳 join）维度：硬编码一跳 JOIN 会拼错 → 拒编回退 LLM（与 compile_period_* 一致）。
        if ddef.get("via"):
            return None
        rank = f"RANK() OVER (ORDER BY {expr} {_dir()})"
        if ddef.get("join"):
            dt, dk, ddisp = ddef["join"]
            sql = (f'SELECT d.{ddisp} AS "{dim}", {rank} AS "名次", {expr} AS "{name}" '
                   f'FROM {fact} f JOIN {dt} d ON f.{ddef["fact_col"]} = d.{dk} '
                   f'GROUP BY d.{ddisp} ORDER BY "名次"')
        else:
            col = ddef["fact_col"]
            c = col if isinstance(col, str) else col[0]
            sql = (f'SELECT f.{c} AS "{dim}", {rank} AS "名次", {expr} AS "{name}" '
                   f'FROM {fact} f GROUP BY f.{c} ORDER BY "名次"')
        return {
            "sql": sql, "title": f"{name}名次", "metric": name, "metrics": [name],
            "unit": unit, "tables": [fact], "compiled": True,
            "mql": {"metric": name, "metric_key": m.get("name"),
                    "metric_expression": m.get("sql_expression"),
                    "dimensions": [dim], "compiled_by": "window_rank",
                    "window": "RANK"},
        }

    # ── 模式 B：分组内 Top-N ──
    if not _WIN_TOPN_RE.search(query):
        return None
    inner_m = _WIN_INNER_RE.search(query)
    if not inner_m:
        return None
    inner = inner_m.group(1)
    outer = None
    for dw in dims_meta:
        if dw == inner:
            continue
        # 「各产品类别内」的外层是「产品类别」而非「产品」——「产品类别」不是注册维度，
        # 若按「产品」编译会得到「按类别汇总」的错答案（实测「各产品类别内产量最高的产品」
        # 被编译成 GROUP BY product_category 的单行总量）。带 类别/品类/类型/型号/系列
        # 后缀的「各X」应回退 LLM，禁止降级成父维度。
        if re.search(rf"各{dw}(?:类别|品类|类型|型号|系列|种类)", query):
            continue
        if re.search(rf"各{dw}(?:内|中|里)?", query):
            outer = dw
            break
    if not outer:
        return None
    odef, idef = _dim_def(outer), _dim_def(inner)
    if not odef or not idef:
        return None
    # via（两跳 join）维度：硬编码一跳 JOIN 会拼错（f.work_order_id = d.line_id 之类）→ 拒编回退。
    if odef.get("via") or idef.get("via"):
        return None
    # 排序方向 + rn（第N高/低）
    order_dir = _dir()
    rn = 1
    if (mm := _WIN_RN_RE.search(query)):
        g = mm.group(1)
        rn = int(g) if g.isdigit() else _CN_NUM.get(g, 1)
    # 组装：外层/内层各独立 JOIN（同一张维度表复用 alias），度量 + 窗口
    joins: list[str] = []
    group_exprs: list[str] = []
    select_parts: list[str] = []
    alias_map: dict[str, str] = {}
    part_expr = None
    for label, ddef, is_outer in ((outer, odef, True), (inner, idef, False)):
        if ddef.get("join"):
            dt, dk, ddisp = ddef["join"]
            al = alias_map.get(dt)
            if al is None:
                al = f"d{len(alias_map) + 1}"
                alias_map[dt] = al
                joins.append(f"JOIN {dt} {al} ON f.{ddef['fact_col']} = {al}.{dk}")
            g = f"{al}.{ddisp}"
            select_parts.append(f'{al}.{ddisp} AS "{label}"')
        else:
            col = ddef["fact_col"]
            c = col if isinstance(col, str) else col[0]
            g = f"f.{c}"
            select_parts.append(f'{c} AS "{label}"')
        group_exprs.append(g)
        if is_outer:
            part_expr = g
    if part_expr is None:
        return None
    row_num = f"ROW_NUMBER() OVER (PARTITION BY {part_expr} ORDER BY {expr} {order_dir})"
    inner_sel = ", ".join(select_parts + [f'{expr} AS "{name}"', f"{row_num} AS rn"])
    outer_sel = ", ".join([f'"{outer}"', f'"{inner}"', f'"{name}"'])
    sql = (f"SELECT {outer_sel} FROM ("
           f"SELECT {inner_sel} FROM {fact} f {' '.join(joins)} "
           f"GROUP BY {', '.join(group_exprs)}) t "
           f"WHERE rn = {rn} ORDER BY \"{outer}\"")
    return {
        "sql": sql, "title": f"{name}{outer}内Top{rn}", "metric": name, "metrics": [name],
        "unit": unit, "tables": [fact], "compiled": True,
        "mql": {"metric": name, "metric_key": m.get("name"),
                "metric_expression": m.get("sql_expression"),
                "dimensions": [outer, inner], "compiled_by": "window_rank",
                "window": "ROW_NUMBER", "rn": rn},
    }


_MONTH_PAIR_RE = re.compile(
    r"(\d+)月[^。；！？\d月]{0,20}?(?:相对|较|比|与|和|vs|VS)[^。；！？\d月]{0,10}?(\d+)月")

# 相邻两次间隔的事件词 → (事实表, 时间列, 实体外键列, 实体维度表, 实体展示列)
_INTERVAL_META = {
    # (事实表, 时间列, 实体外键列, 实体维度表, 实体展示列, 时间列类型)
    # 类型 date：date-date=整数天数，需 ::timestamp 才能 EXTRACT EPOCH；timestamp 直接用。
    "停机": ("eqp_downtime_record", "start_time", "equipment_id", "dim_equipment", "equipment_name", "timestamp"),
    "检验": ("qms_inspection", "inspection_date", "product_id", "dim_product", "product_name", "date"),
}


def compile_interval(query: str) -> dict | None:
    """相邻两次事件间隔（「各设备相邻两次停机的间隔天数」）→ 确定性编译。

    上一轮实测 LLM 把「相邻两次的间隔」误算成「平均间隔」（AVG），而正确语义是
    第 1 次→第 2 次的间隔。生成 ROW_NUMBER self-join（rn=1 → rn=2）的间隔 SQL。
    """
    if get_db_type() == "mysql":
        return None
    if not re.search(r"(?:相邻|连续|前后|接连)两次", query):
        return None
    ev = None
    for kw in _INTERVAL_META:
        if kw in query:
            ev = kw
            break
    if not ev:
        return None
    fact, time_col, entity_col, ent_table, ent_disp, ttype = _INTERVAL_META[ev]
    # 间隔单位
    if re.search(r"分钟", query):
        div, unit = 60.0, "分钟"
    elif re.search(r"小时", query):
        div, unit = 3600.0, "小时"
    else:
        div, unit = 86400.0, "天"
    # date 类型时间列：date-date = 整数天数，EXTRACT(EPOCH) 会报错 → 加 ::timestamp cast
    _cast = "::timestamp" if ttype == "date" else ""

    sql = (
        f"WITH t AS (SELECT {entity_col}, {time_col}, "
        f"ROW_NUMBER() OVER (PARTITION BY {entity_col} ORDER BY {time_col}) rn "
        f"FROM {fact}) "
        f"SELECT e.{ent_disp} AS \"{ev}\", "
        f"ROUND(EXTRACT(EPOCH FROM (t2.{time_col}{_cast} - t1.{time_col}{_cast}))/ {div}, 1) AS \"间隔{unit}\" "
        f"FROM t t1 JOIN t t2 ON t1.{entity_col} = t2.{entity_col} AND t1.rn = 1 AND t2.rn = 2 "
        f"JOIN {ent_table} e ON e.{entity_col} = t1.{entity_col} ORDER BY e.{ent_disp}"
    )
    return {
        "sql": sql, "title": f"{ev}间隔", "metric": f"{ev}间隔", "metrics": [f"{ev}间隔"],
        "unit": unit, "tables": [fact, ent_table], "compiled": True,
        "mql": {"metric": f"{ev}间隔", "dimensions": [ev], "compiled_by": "interval",
                "window": "ROW_NUMBER self-join"},
    }


def compile_period_diff(query: str) -> dict | None:
    """显式双月份对比（「各产线7月产量相对6月的变化率」）→ 确定性编译。

    仅支持：简单 SUM 指标 + 单维度 + 显式两个月份 + 变化率/增长率（可选 TopN）。
    复杂表达（比率指标/多维度/跨年）回退 LLM。数据单年时用 EXTRACT(MONTH) 分月。
    """
    if get_db_type() == "mysql":
        return None
    m = _MONTH_PAIR_RE.search(query)
    if not m:
        return None
    cur_m, prev_m = int(m.group(1)), int(m.group(2))
    if not (1 <= cur_m <= 12 and 1 <= prev_m <= 12) or cur_m == prev_m:
        return None
    from agent.metric_registry import find_metrics
    hits = find_metrics(query)
    if len(hits) != 1:
        return None
    mt = hits[0]
    tables = mt.get("tables") or []
    if len(tables) != 1 or tables[0] not in _FACT_TIME_COL:
        return None
    fact = tables[0]
    time_col = _FACT_TIME_COL[fact]
    meta = _FACT_META[fact]
    raw = (mt.get("sql_expression") or "").strip()
    # 只支持简单 SUM(inner) 表达式（产量/不良数/投入量/停机时长等）；比率指标回退
    mm = re.fullmatch(r"SUM\((.*)\)", raw, re.S)
    if not mm:
        return None
    inner = mm.group(1).strip()
    if not inner or not _safe_metric_expr(inner):
        return None
    name = mt["name"].split("(")[0].strip()
    unit = mt.get("unit", "")
    dims = _detect_dim(query, fact)
    if len(dims) != 1:
        return None
    dim = dims[0]
    ddef = _resolve_dim_def(fact, meta["dims"][dim])
    if not ddef:
        return None
    # via（两跳 join）维度：同 compile_period_compare_grouped，硬编码一跳 JOIN 会拼错 → 拒编。
    if ddef.get("via"):
        return None

    # 2026-10-01 修复（跨年翻倍）：EXTRACT(MONTH) 不带年份约束时，数据跨 2025/2026 两年
    # 会把两年的同名月份各自加总（数值约翻倍、变化率失真）。这里用标量子查询把
    # 月份锚定到数据最新一年（PG 对标量子查询只求值一次，性能无损）；显式年份的问句
    # （如「2025年7月」）本来就不进本编译器，不受影响。
    data_year = f"(SELECT date_trunc('year', MAX({time_col})) FROM {fact})"
    year_ok = f"{time_col} >= {data_year}"
    cur_agg = f"SUM(CASE WHEN EXTRACT(MONTH FROM {time_col}) = {cur_m} AND {year_ok} THEN {inner} ELSE 0 END)"
    prev_agg = f"SUM(CASE WHEN EXTRACT(MONTH FROM {time_col}) = {prev_m} AND {year_ok} THEN {inner} ELSE 0 END)"

    if ddef.get("join"):
        dt, dk, ddisp = ddef["join"]
        dim_col = f"d.{ddisp}"
        from_clause = f"{fact} f JOIN {dt} d ON f.{ddef['fact_col']} = d.{dk}"
        group = f"d.{ddisp}"
    else:
        c = ddef["fact_col"] if isinstance(ddef["fact_col"], str) else ddef["fact_col"][0]
        dim_col = f"f.{c}"
        from_clause = f"{fact} f"
        group = f"f.{c}"

    # 并列展示（「各车间6月与7月产量各是多少」）vs 对比（「7月相对6月变化率」）两种语义：
    # 前者输出两列各自的值，后者输出变化率/增长率。
    is_list = bool(re.search(r"各是多少|各多少|分别", query)) and \
        not re.search(r"变化率|增长率|增幅|增速|相对|较|比", query)

    if is_list:
        cur_label, prev_label = f"{cur_m}月", f"{prev_m}月"
        sql = (
            f"WITH m AS (SELECT {dim_col} AS \"{dim}\", {cur_agg} AS m_cur, {prev_agg} AS m_prev "
            f"FROM {from_clause} GROUP BY {group}) "
            f"SELECT \"{dim}\", m_cur AS \"{cur_label}产量\", m_prev AS \"{prev_label}产量\" "
            f"FROM m ORDER BY \"{dim}\""
        )
        return {
            "sql": sql, "title": f"{name}分月对比", "metric": name, "metrics": [name],
            "unit": unit, "tables": [fact], "compiled": True,
            "mql": {"metric": name, "metric_key": mt.get("name"),
                    "metric_expression": mt.get("sql_expression"),
                    "dimensions": [dim], "compiled_by": "period_diff",
                    "compare": f"{prev_label} vs {cur_label}"},
        }

    rate_label = "增长率" if re.search(r"增长率|增幅|增速", query) else "变化率"
    limit = 0
    if (m2 := re.search(r"前\s*(\d+)", query)):
        limit = int(m2.group(1))

    sql = (
        f"WITH m AS (SELECT {dim_col} AS \"{dim}\", {cur_agg} AS m_cur, {prev_agg} AS m_prev "
        f"FROM {from_clause} GROUP BY {group}) "
        f"SELECT \"{dim}\", ROUND((m_cur - m_prev) * 100.0 / NULLIF(m_prev, 0), 2) AS \"{rate_label}\" "
        f"FROM m ORDER BY \"{rate_label}\" DESC"
    )
    if limit:
        sql += f" LIMIT {limit}"
    return {
        "sql": sql, "title": f"{name}{rate_label}", "metric": name, "metrics": [name],
        "unit": "%", "tables": [fact], "compiled": True,
        "mql": {"metric": name, "metric_key": mt.get("name"),
                "metric_expression": mt.get("sql_expression"),
                "dimensions": [dim], "compiled_by": "period_diff",
                "compare": f"{prev_m}月→{cur_m}月"},
    }


def compile_period_compare(query: str) -> dict | None:
    """确定性编译「同比/环比」：本期 vs 对比期 + 增长率。

    仅支持：单指标 + 有时间列的单一事实表 + 无分组维度（总体对比）+ PG。
    失败返回 None（回退 LLM，绝不硬凑）。
    """
    if get_db_type() == "mysql":
        return None
    from agent.metric_registry import find_metrics
    hits = find_metrics(query)
    if len(hits) != 1:
        return None
    m = hits[0]
    tables = m.get("tables") or []
    if len(tables) != 1 or tables[0] not in _FACT_TIME_COL:
        return None
    fact = tables[0]
    time_col = _FACT_TIME_COL[fact]
    if _detect_dim(query, fact):
        return None  # 有维度分组的对比 → LLM（仅总体对比）
    expr = _pg_numeric(m.get("sql_expression") or "")
    if not expr:
        return None
    name = m["name"].split("(")[0].strip()
    unit = m.get("unit", "")
    anchor = f"(SELECT MAX({time_col}) FROM {fact})"

    is_yoy = bool(_COMPARE_YOY.search(query))
    is_mom = bool(_COMPARE_MOM.search(query))
    if is_yoy and is_mom:
        return None  # 同时出现同比+环比 → 歧义，回退 LLM

    ranges = _compare_ranges(query, anchor, time_col, is_yoy)
    if not ranges:
        return None
    cur_gte, cur_lt, prev_gte, prev_lt, prev_label = ranges
    kind = "同比" if is_yoy else "环比"

    sql = (
        f"WITH cur AS (SELECT {expr} AS v FROM {fact} f WHERE {cur_gte} AND {cur_lt}), "
        f"prev AS (SELECT {expr} AS v FROM {fact} f WHERE {prev_gte} AND {prev_lt}) "
        f'SELECT cur.v AS "本期", prev.v AS "{prev_label}", '
        f'CASE WHEN prev.v = 0 OR prev.v IS NULL THEN NULL '
        f'ELSE ROUND((cur.v - prev.v) / prev.v * 100, 2) END AS "{kind}增长率%" '
        f"FROM cur, prev"
    )
    mql = {
        "metric": name,
        "metric_key": m.get("name"),
        "metric_expression": m.get("sql_expression"),
        "compare": kind,
        "prev_label": prev_label,
        "fact_table": fact,
        "value_filter": None,
        "compiled_by": "period_compare",
    }
    return {
        "sql": sql, "title": f"{name}{kind}", "metric": name,
        "metrics": [name], "unit": unit, "tables": [fact],
        "compiled": True, "mql": mql,
    }


# ── 分组两期对比（「哪些产线本月产量超过上个月」）确定性编译 ────────────
# 2026-09-29 实测背景：这类问法此前一律掉 LLM，LLM 四次生成四种 SQL 形态
# （两个恒等 SUM / 单聚合跨两月 / DATE_TRUNC 自比较 HAVING X>X / CTE 里两期起止同值），
# 稳定率极低；换措辞还可能被**普通编译静默降级**成单期聚合（0.1s 高置信错答，
# 实测「各产线本月产量比上月高」被编译成「上月产量按产线排序」，比较语义整个丢掉）。
# 与既有 compile_period_compare 的关系：那个只做**总体**对比（有维度即放弃），
# 本函数补上**分组**对比；两者互不改动，各自失败都返回 None 回退 LLM。
_CMP_CUR_RE = re.compile(r"本月|这个月|当月|本季度|本季|今年|本年|近\s*\d+\s*(?:天|周|个月)")
_CMP_PREV_RE = re.compile(r"上个月|上月|上季度|上季|去年|去年同期|前\s*\d+\s*(?:天|周|个月)")
_CMP_PRED_RE = re.compile(r"超过|高于|大于|跑赢|优于|低于|少于|小于|不如|不及|落后|"
                          r"增长|下降|提升|减少|变化|增幅|增速|环比|同比|相对|相比|对比|比|较")
_CMP_UP_RE = re.compile(r"超过|高于|大于|跑赢|优于|增长|提升|高过|比[^。？，]{0,8}?(?:高|多)")
_CMP_DOWN_RE = re.compile(r"低于|少于|小于|不如|不及|落后|下降|减少|比[^。？，]{0,8}?(?:低|少)")
_CMP_FILTER_RE = re.compile(r"哪些|哪条|哪几|哪个|哪台|哪道|列出|找出|有哪些")


def _has_grouped_compare_intent(query: str) -> bool:
    """「本期词 + 上期词 + 比较谓词」三段式对比意图。

    本期词只认**相对时间词**（本月/本季/今年/近N天）；显式双月（「7月相对6月」）
    不在此列，由 compile_period_diff 接管，避免两者抢题。
    """
    return bool(_CMP_CUR_RE.search(query) and _CMP_PREV_RE.search(query)
                and _CMP_PRED_RE.search(query))


def _grouped_compare_ranges(query: str):
    """两期区间（用 CTE 里的 b.mx / b.p0 表达）→ (period_fn, cur_lo, cur_hi, prev_lo, prev_hi, 本期标签, 上期标签)。

    期长对齐（关键）：上期上界 = 数据上限往前推一个周期，而不是「上一个完整自然月」。
    实测数据只到 7-15，若上期取 6 月整月（30 天）对本期 15 天，所有分组都显示下降 → 0 行；
    取到 6-15 才是同口径对比（7-01~07-15 vs 06-01~06-15）。
    """
    nd = re.search(r"近\s*(\d+)\s*天", query)
    if nd and re.search(r"前\s*\d+\s*天", query):
        n = int(nd.group(1))
        return ("", f"b.mx - INTERVAL '{max(0, n - 1)} days'", "b.mx",
                f"b.mx - INTERVAL '{2 * n - 1} days'", f"b.mx - INTERVAL '{n} days'",
                f"近{n}天", f"前{n}天")
    if re.search(r"本季度|本季", query):
        return ("quarter", "b.p0", "b.mx", "b.p0 - INTERVAL '3 months'", "b.mx - INTERVAL '3 months'",
                "本季度", "上季度")
    if re.search(r"今年|本年", query):
        return ("year", "b.p0", "b.mx", "b.p0 - INTERVAL '1 year'", "b.mx - INTERVAL '1 year'",
                "今年", "去年")
    # 2026-10-01：纯「同比」无显式本期词 → 年同比（环比默认月环比，见下方默认分支）。
    if _COMPARE_YOY.search(query):
        return ("year", "b.p0", "b.mx", "b.p0 - INTERVAL '1 year'", "b.mx - INTERVAL '1 year'",
                "今年", "去年")
    return ("month", "b.p0", "b.mx", "b.p0 - INTERVAL '1 month'", "b.mx - INTERVAL '1 month'",
            "本月", "上月")


def compile_period_compare_grouped(query: str) -> dict | None:
    """确定性编译「分组两期对比」：本期 vs 上期，按维度分组，可选筛选（本期>上期）。

    仅支持：单指标（简单 SUM 形态）+ 单事实表（有时间列）+ 恰好 1 个分组维度 + PG。
    失败返回 None（回退 LLM，绝不硬凑）。
    """
    if get_db_type() == "mysql":
        return None
    # 2026-10-01 放开：纯「同比/环比 + 维度」也走分组两期对比。此前只认
    # 「本期词+上期词+谓词」三段式，导致句首/句尾「各产线产量同比」「环比各产线产量」
    # 被普通编译降级成单期聚合、彻底丢掉对比语义（0.1s 高置信错答）。
    # 整体对比由 compile_period_compare 接管，这里只管带维度的分组对比；
    # 同比+环比同时出现属歧义，由 try_compile_metric 统一回退 LLM。
    if not (_has_grouped_compare_intent(query) or _has_compare_intent(query)):
        return None
    from agent.metric_registry import find_metrics
    hits = find_metrics(query)
    if len(hits) != 1:
        return None
    m = hits[0]
    tables = m.get("tables") or []
    if len(tables) != 1 or tables[0] not in _FACT_TIME_COL:
        return None
    fact = tables[0]
    time_col = _FACT_TIME_COL[fact]
    raw = (m.get("sql_expression") or "").strip()
    sm = re.fullmatch(r"SUM\((.*)\)", raw, re.S)   # 只支持简单 SUM(inner)；比率指标回退
    if not sm:
        return None
    inner = _resolve_expr(sm.group(1).strip(), fact)
    if not inner or not _safe_metric_expr(inner):
        return None

    dims = _detect_dim(query, fact)
    if not dims:
        # 「哪些产线本月产量超过上个月」没有「各X/按X」分组信号 → 维度名直接出现即算，
        # 但仍要求该维度的 fact_col 在当前库真实存在（列缺失则跳过，避免编出坏 SQL）。
        for cn in sorted(_FACT_META[fact].get("dims", {}), key=len, reverse=True):
            if cn not in query:
                continue
            try:
                _dd = _resolve_dim_def(fact, _FACT_META[fact]["dims"][cn])
            except Exception:
                continue
            _fc = _dd.get("fact_col")
            _c = _fc if isinstance(_fc, str) else (_fc[0] if _fc else "")
            if _c and _c in _table_cols(fact):
                dims = [cn]
                break
    if len(dims) != 1:
        return None
    dim = dims[0]
    try:
        ddef = _resolve_dim_def(fact, _FACT_META[fact]["dims"][dim])
    except Exception:
        return None
    if not ddef:
        return None

    name = m["name"].split("(")[0].strip()
    unit = m.get("unit", "")
    # via（两跳 join）维度在 from_clause 拼接处被硬编码一跳 JOIN 覆盖（1527），
    # 会拼出错的 ON 条件（如 ON f.work_order_id = d.line_id，把工单ID 当产线ID join）。
    # 暂不在此编译器里支持两跳 join → 显式拒编，回退 LLM，避免静默写出答非所问 SQL。
    if ddef.get("via"):
        return None
    if ddef.get("join"):
        dt, dk, ddisp = ddef["join"]
        dim_col = f"d.{ddisp}"
        from_clause = f"{fact} f JOIN {dt} d ON f.{ddef['fact_col']} = d.{dk}"
        group = f"d.{ddisp}"
    else:
        c = ddef["fact_col"] if isinstance(ddef["fact_col"], str) else ddef["fact_col"][0]
        dim_col = f"f.{c}"
        from_clause = f"{fact} f"
        group = f"f.{c}"

    pfn, cur_lo, cur_hi, prev_lo, prev_hi, cur_label, prev_label = _grouped_compare_ranges(query)
    p0_expr = "mx" if not pfn else f"date_trunc('{pfn}', mx)"
    cur_c = f"f.{time_col} >= {cur_lo} AND f.{time_col} <= {cur_hi}"
    prev_c = f"f.{time_col} >= {prev_lo} AND f.{time_col} <= {prev_hi}"
    span = f"f.{time_col} >= {prev_lo} AND f.{time_col} <= {cur_hi}"

    # 筛选型（「哪些产线本月产量超过上个月」→ 只留本期>上期的分组）
    # vs 展示型（「各产线本月产量环比上月」→ 全量分组 + 差值 + 变化率）
    up, down = bool(_CMP_UP_RE.search(query)), bool(_CMP_DOWN_RE.search(query))
    if up and down:
        return None  # 方向矛盾（又高又低）→ 回退 LLM
    if up or down or _CMP_FILTER_RE.search(query):
        op = ">" if up else "<" if down else ">"
        sql = (
            f"WITH a AS (SELECT MAX({time_col}) AS mx FROM {fact}), "
            f"b AS (SELECT mx, {p0_expr} AS p0 FROM a), "
            f'm AS (SELECT {dim_col} AS "{dim}", '
            f"SUM(CASE WHEN {cur_c} THEN {inner} ELSE 0 END) AS m_cur, "
            f"SUM(CASE WHEN {prev_c} THEN {inner} ELSE 0 END) AS m_prev "
            f"FROM {from_clause} CROSS JOIN b WHERE {span} GROUP BY {group}) "
            f'SELECT "{dim}", m_cur AS "{cur_label}{name}", m_prev AS "{prev_label}{name}", '
            f'm_cur - m_prev AS "差值" FROM m WHERE m_cur {op} m_prev '
            f'ORDER BY "差值" DESC LIMIT 200'
        )
        title = f"{name}{cur_label}高于{prev_label}的{dim}"
    else:
        sql = (
            f"WITH a AS (SELECT MAX({time_col}) AS mx FROM {fact}), "
            f"b AS (SELECT mx, {p0_expr} AS p0 FROM a), "
            f'm AS (SELECT {dim_col} AS "{dim}", '
            f"SUM(CASE WHEN {cur_c} THEN {inner} ELSE 0 END) AS m_cur, "
            f"SUM(CASE WHEN {prev_c} THEN {inner} ELSE 0 END) AS m_prev "
            f"FROM {from_clause} CROSS JOIN b WHERE {span} GROUP BY {group}) "
            f'SELECT "{dim}", m_cur AS "{cur_label}{name}", m_prev AS "{prev_label}{name}", '
            f'm_cur - m_prev AS "差值", '
            f'ROUND((m_cur - m_prev) * 100.0 / NULLIF(m_prev, 0), 2) AS "变化率%" '
            f'FROM m ORDER BY "差值" DESC LIMIT 200'
        )
        title = f"{dim}{name}{prev_label}→{cur_label}对比"
    return {
        "sql": sql, "title": title, "metric": name, "metrics": [name],
        "unit": unit, "tables": [fact] + ([ddef["join"][0]] if ddef.get("join") else []),
        "compiled": True,
        "mql": {"metric": name, "metric_key": m.get("name"),
                "metric_expression": m.get("sql_expression"),
                "dimensions": [dim], "compiled_by": "period_compare_grouped",
                "compare": f"{prev_label} vs {cur_label}",
                "period_align": "same_length_to_data_max"},
    }


# ── 声明式多表指标编译（P0-2：compile_plan，对齐 Aloudata 多跳 join）──
# 原理：指标注册时可声明 compile_plan.sql_template（确定性 SQL 骨架，含 {t:列名} 时间占位符），
# 编译器只做「时间条件参数填充」——SQL 结构在注册时人工审定并过安全校验，杜绝运行时拼装风险。
# 首个落地：库存周转天数（inv_inventory_snapshot + test_orders 双事实表，此前必须走 LLM）。
# 仅支持 kind=cte_multi_fact 一种骨架；失败一律回退 LLM（宁可不编，不可编错）。
_PLAN_PH_RE = re.compile(r"\{t:([a-zA-Z_][a-zA-Z0-9_]*)\}")


def compile_plan_metric(query: str, metric: dict) -> dict | None:
    """按指标注册的 compile_plan 确定性编译多表指标；失败返回 None。"""
    plan = metric.get("compile_plan") or {}
    if plan.get("kind") != "cte_multi_fact":
        return None
    # 维度/分组意图 → compile_plan 模板不支持维度参数化（固定全量口径）→ 回退 LLM
    # （如「各产品的库存周转天数」，模板只会编译出全量单值 = 答非所问；description 含分组指引）
    if re.search(r"各[\u4e00-\u9fa5]{1,6}|按[\u4e00-\u9fa5]{1,6}(?!\s*(天|日|月|周|季度|年))|每个[\u4e00-\u9fa5]{1,6}|分[\u4e00-\u9fa5]{1,6}", query):
        return None
    template = str(plan.get("sql_template") or "").strip()
    time_cols = plan.get("time_cols") or {}
    if not template or not time_cols:
        return None
    # 校验模板占位符与 time_cols 一一对应（防模板里有未声明列 → 静默漏过滤）
    ph_cols = set(_PLAN_PH_RE.findall(template))
    if not ph_cols or ph_cols != set(time_cols.keys()):
        return None
    # 时间条件填充：无时间词 → "列名 IS NOT NULL"（全部）；有时间词 → 按列独立生成（防跨表时间列错配）
    for col, tbl in time_cols.items():
        cond = _build_time_filter(str(tbl), col, query)
        if cond is None and _has_time_intent(query):
            return None  # 有时间词但不在支持集 → 回退 LLM（description 含完整 SQL 指导）
        template = template.replace(f"{{t:{col}}}", cond or f"{col} IS NOT NULL")
    if _PLAN_PH_RE.search(template):
        return None  # 残留占位符 → 模板不完整，回退
    # 最终 SQL 语法校验（sqlglot），防模板/填充拼装错误
    try:
        import sqlglot
        sqlglot.parse_one(template, read="postgres")
    except Exception:
        return None
    name = metric["name"].split("(")[0].strip()
    mql = {
        "metric": name,
        "metric_key": metric.get("name"),
        "metric_expression": metric.get("sql_expression"),
        "metric_type": metric.get("metric_type") or "simple",
        "fact_table": metric.get("tables") or [],
        "time_cols": list(time_cols.keys()),
        "value_filter": None,
        "compiled_by": "compile_plan",
    }
    return {
        "sql": template, "title": f"{name}查询", "metric": name,
        "metrics": [name], "unit": metric.get("unit", ""),
        "tables": list(metric.get("tables") or []),
        "compiled": True, "mql": mql,
    }


def _has_time_intent(query: str) -> bool:
    """问句是否含时间意图词（与 _build_time_filter 支持集一致，供 compile_plan 判定）。"""
    return bool(re.search(r"近\s*\d+\s*天|近\s*\d+\s*个月|上月|上个月|本月|这个月|本季度|本季|上季度|上季|今年|本年|年初至今|今年以来|去年", query))


# ── 多事实表外键图桥接编译（P0-2 L3：对齐 Aloudata 多跳 join，默认关闭）──
# 模式：两指标各落一个事实表，且问题维度在两表都通过「同一维度表」JOIN 定义时，
# 用「维度表 + 两聚合子查询 LEFT JOIN」确定性编译（星型桥接），杜绝事实表直连的行数膨胀。
# 默认关闭（METRIC_JOIN_AUTO=1 开启）：该路径对数据模型要求苛刻，宁可不编，不可编错。
def compile_multi_fact_bridge(query: str) -> dict | None:
    if os.getenv("METRIC_JOIN_AUTO", "0") != "1":
        return None
    if get_db_type() == "mysql":
        return None
    from agent.metric_registry import find_metrics
    hits = find_metrics(query)
    if len(hits) != 2:
        return None
    facts = []
    exprs = []
    infos = []
    for m in hits:
        tables = m.get("tables") or []
        if len(tables) != 1 or tables[0] not in _COMPILABLE_TABLES:
            return None
        # 列名自适应（resolve → pg_numeric，桥接场景无聚合覆盖）
        expr = _pg_numeric(_resolve_expr(m.get("sql_expression") or "", tables[0]))
        if not expr or not _safe_metric_expr(expr):
            return None
        mname = m["name"].split("(")[0].strip()
        if not mname or not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fa5 ]+", mname):
            return None
        facts.append(tables[0])
        exprs.append(expr)
        infos.append({"name": mname, "unit": m.get("unit", ""), "key": m.get("name")})
    if len(set(facts)) != 2:
        return None  # 同表双指标走普通编译，无需桥接
    # 维度：两表必须同时命中同一维度，且都通过同一维度表+key JOIN 定义
    dims_a = set(_detect_dim(query, facts[0]))
    dims_b = set(_detect_dim(query, facts[1]))
    common = dims_a & dims_b
    if not common:
        return None
    for dim in sorted(common, key=len, reverse=True):
        defs = []
        ok = True
        for ft in facts:
            dd = _resolve_dim_def(ft, _FACT_META[ft].get("dims", {}).get(dim) or {})
            if not dd.get("join"):
                ok = False
                break
            defs.append(dd)
        if not ok:
            continue
        dt_a, key_a, disp_a = defs[0]["join"]
        dt_b, key_b, disp_b = defs[1]["join"]
        if dt_a != dt_b or key_a != key_b:
            continue  # 两表必须桥接到同一维度表且同一关联键
        dtable, dkey = dt_a, key_a
        # 组装：维度表 + 两聚合子查询 LEFT JOIN（防行数膨胀）
        subs = []
        for i, ft in enumerate(facts):
            fk = defs[i]["fact_col"]
            tc = _FACT_META[ft].get("time_col")
            tf = _build_time_filter(ft, tc, query) if tc else None
            where = f" WHERE {tf}" if tf else ""
            subs.append(
                f"(SELECT f.{fk} AS {dkey}, {exprs[i]} AS \"{infos[i]['name']}\" "
                f"FROM {ft} f{where} GROUP BY f.{fk})"
            )
        sql = (
            f'SELECT d.{dkey} AS "{dim}", a."{infos[0]["name"]}", b."{infos[1]["name"]}" '
            f'FROM {dtable} d '
            f'LEFT JOIN {subs[0]} a ON a.{dkey} = d.{dkey} '
            f'LEFT JOIN {subs[1]} b ON b.{dkey} = d.{dkey} '
            f'ORDER BY a."{infos[0]["name"]}" DESC NULLS LAST LIMIT 20'
        )
        mql = {
            "metric": infos[0]["name"], "metric_key": infos[0]["key"],
            "metric_expression": None, "metric_type": "simple",
            "fact_table": facts, "bridge_dim": dim, "bridge_table": dtable,
            "value_filter": None, "compiled_by": "multi_fact_bridge",
        }
        return {
            "sql": sql, "title": "与".join(i["name"] for i in infos) + "对比查询",
            "metric": infos[0]["name"],
            "metrics": [i["name"] for i in infos],
            "unit": infos[0]["unit"], "tables": facts,
            "compiled": True, "mql": mql,
        }
    return None


# 双指标分析意图（2026-09-03）：find_metrics 按"最长匹配词"只保留单个指标（如「停机时长和产量」
# 只留更长的「停机时长」），会把"X 与 Y 是否相关/对比/影响"这类**双指标分析问法压成单指标静默编译**，
# 秒回一个与问题无关的单指标聚合（答非所问）。检测到 ≥2 个不同已注册指标 + 关系/对比/影响意图时，
# 禁止确定性编译，交回二次确认弹窗 → LLM 推断双指标方案（保证结果真的"在分析"）。
_MULTI_ANALYSIS_INTENT_RE = re.compile(
    r"相关|关系|关联|对比|比较|差异|差别|影响|是不是|是否.*(有关|相关)|有没有.*(关系|影响|关联)"
    r"|谁高|谁低|哪个[^。；，、！？]{0,20}(?<!最)(高|低|大|小)|越长|越短|越多|越少|越高|越低|成正比|反比")


def _suppress_multi_metric_compile(query: str) -> bool:
    """问句是否「多指标 + 关系/对比意图」→ 是则禁止编译。"""
    if not query or not _MULTI_ANALYSIS_INTENT_RE.search(query):
        return False
    try:
        from agent.metric_registry import get_effective_metrics
        named: set[str] = set()
        # 命中指标词 + 词长，用于「否定前缀去重」：中文指标名常互为子串（「良率」⊂「不良率」），
        # 直接 `w in query` 会把「不良率」里的「良率」也当成一个独立命中，把单指标极值问句
        # （「哪个工序的不良率最低」）误判成双指标对比。规则：短词若**只**以「否定前缀 + 短词」
        # （不/无/未/非 + 短词，即某个更长指标名的后缀）的形式出现、没有独立/并列出现
        # （句首或「和/与/、」之后），才判定它是长词的一部分而跳过；否则（如「良率和不良率
        # 哪个高」里句首的「良率」）保留为独立指标。
        _matched: list[tuple[int, str, str]] = []
        for m in get_effective_metrics():
            words = [str(m.get("name") or "")] + [str(a) for a in (m.get("aliases") or [])]
            for w in words:
                w = re.sub(r"\(.*?\)", "", w).strip()
                if len(w) >= 2 and w in query:
                    _matched.append((len(w), w, str(m.get("name") or "")))
                    break
        _matched.sort(reverse=True)
        _selected: dict[str, str] = {}
        for _ln, _w, _mn in _matched:
            _only_neg_suffix = True
            for _p in (m.start() for m in re.finditer(re.escape(_w), query)):
                if _p > 0 and query[_p - 1] in "不无未非":
                    continue  # 该处是「否定前缀+短词」，属更长指标名后缀
                _only_neg_suffix = False
                break
            if _only_neg_suffix and any(_w in _prev for _prev in _selected.values()):
                continue
            _selected[_mn] = _w
        named = set(_selected.keys())
        return len(named) >= 2
    except Exception:
        return False


def compile_intersection_list(query: str) -> dict | None:
    """「既满足 A 又满足 B 的<维度>有哪些」→ 确定性交集 SQL（2026-09-29 新增）。

    为什么单独开一条通道：这类问法里**没有任何注册指标词**（答案对象是维度值清单，
    不是聚合数字），所以 find_metrics 返回空，走不到普通编译的值过滤逻辑，
    整体回退 LLM。实测后果很糟 ——「既使用过传感器又使用过控制器的产线有哪些」
    被 LLM 答成 `SELECT dim_production_line.* FROM dim_production_line LIMIT 20`
    （**全表无过滤**，6 行，真值 5 行），行数看着接近、用户完全看不出错。
    而本文件早已具备产出正确形态的能力：_explicit_value_filters 对这句话
    返回的就是恰好两条 EXISTS 子句（也就是正确答案）。

    产物形态：从某个事实表筛出满足全部条件的维度值，返回维度明细行。
      SELECT <维度展示列> FROM <事实表> f
      WHERE EXISTS(...条件1...) AND EXISTS(...条件2...)
      GROUP BY <维度展示列> ORDER BY ... LIMIT ...

    生效前提（全部确定性，不满足即返回 None 回退 LLM）：
      ① 问句含交集信号词（既…又… / 同时 / 并且 / 都…过…）；
      ② _explicit_value_filters 能识别出 **≥2 个**真实枚举值条件 —— 少于 2 个不叫交集；
      ③ 能推断出要返回哪个维度（由 _detect_dim 给出，且只能 1 个）。
    """
    q = str(query or "")
    if not q:
        return None
    # ① 交集信号词。注意「和/与」太泛（并列问句常用），不单列，必须有「既/又/同时」类强信号。
    if not re.search(r"既.{1,20}又|又.{1,20}既|同时.{0,6}(?:满|具|有|用过|生产过)|"
                     r"并且|且都|都.{0,4}(?:用过|生产过|出现过)", q):
        return None
    # ③ 先定维度与事实表：交集对象得先有归属
    try:
        from agent.metric_registry import get_effective_metrics
        _ = get_effective_metrics()
    except Exception:
        return None
    best: tuple[str, str, list[str]] | None = None
    # 事实表**优先序**（2026-09-29）：_COMPILABLE_TABLES 是 set，遍历顺序不确定，
    # 实测「既使用过传感器又使用过控制器」曾被选到 mes_work_order（工单表）——虽然那里
    # 也有 product_id，但"用过某类产品"的业务事实记录在**产出表**里，工单表只是个"计划"。
    # 且工单表无对应列时产物会直接执行失败（rows=None）。
    # 排序原则：产出/停机/质检这类**事实发生记录**优先，主数据与工单次之。
    _FACT_PRIORITY = ["mes_process_output", "eqp_downtime_record", "qms_inspection",
                      "qms_defect_detail", "inv_inventory_snapshot", "mes_work_order"]
    _ordered = ([t for t in _FACT_PRIORITY if t in _COMPILABLE_TABLES]
                + sorted(t for t in _COMPILABLE_TABLES if t not in _FACT_PRIORITY))
    for fact in _ordered:
        try:
            conds = _explicit_value_filters(q, fact)
        except Exception:
            conds = []
        if len(conds) < 2:      # ② 至少两个条件才是交集
            continue
        # 值条件里的关联列必须真的存在于该事实表 —— 否则产物会执行失败。
        # _explicit_value_filters 已按 _all_table_columns 校验过列存在性，这里只需确认
        # 生成的子句确实引用了本事实表别名 f.（同表列分支）或 f.X_id（跨表分支）。
        if not all((" f." in c or "= f." in c) for c in conds):
            continue
        try:
            dims = _detect_dim(q, fact)
        except Exception:
            dims = []
        if len(dims) != 1:      # ③ 维度必须唯一可定
            continue
        best = (fact, dims[0], conds)
        break
    if not best:
        return None
    fact, dim, conds = best
    try:
        from agent.llm_service import _safe_ident as _si
    except Exception:
        return None
    meta = _FACT_META.get(fact, {})
    cfg = (meta.get("dims") or {}).get(dim) or {}
    if not isinstance(cfg, dict):
        return None
    # 维度在事实表上的关联列（如 产线→line_id），作为「按维度分组的半连接」的键
    dim_key = cfg.get("fact_col")
    dim_key = dim_key[0] if isinstance(dim_key, (list, tuple)) else dim_key
    if not _si(str(dim_key)):
        return None
    join = cfg.get("join")

    # ── 关键：把「明细行条件」改造成「按维度分组的半连接」条件 ──
    # 为什么必须改（2026-09-29 实测的静默错答）：
    #   _explicit_value_filters 产出的是 **明细行级** EXISTS——
    #   `EXISTS (... _v.product_id = f.product_id AND _v.product_category='传感器')`，
    #   它要求**同一行明细**同时满足两个类别。但 product_id 一个产品只归一个类别
    #   （dim_product 里 P001=控制器、P002=传感器，互斥），同一行永远不可能既是传感器
    #   又是控制器 → 交集恒为 0 行。实测该 SQL「执行成功、0 行」，页面显示
    #   「当前数据范围内没有匹配的记录」，看着像"库里真没有"，实则是编译逻辑错 ——
    #   比回退 LLM 更危险。
    #   业务语义是**维度粒度**的：「一条产线既生产过传感器、也生产过控制器」，
    #   所以条件要挂在 line_id 上：该产线的产出里存在传感器，也存在控制器。
    # 实现：每个条件改成 `f.<dim_key> IN (SELECT <dim_key> FROM <事实表> f2
    #   JOIN/JOIN... WHERE <原条件换成 f2 前缀>)` —— 即"存在某个满足该条件的
    #   同维度取值"。多个条件 AND 起来即是维度的交集。
    dim_conds: list[str] = []
    for c in conds:
        c2 = c.replace("f.", "f2.")
        cond = (f"f.{dim_key} IN (SELECT DISTINCT f2.{dim_key} FROM {fact} f2 "
                f"WHERE {c2})")
        if cond not in dim_conds:
            dim_conds.append(cond)
    if len(dim_conds) < 2:
        return None
    where = " AND ".join(dim_conds)

    if join:
        # 维度在关联维表上：JOIN 进来展示
        try:
            tab, key, show = join[0], join[1], join[2]
        except Exception:
            return None
        show_col = show[0] if isinstance(show, (list, tuple)) else show
        if not (_si(tab) and _si(key) and _si(str(show_col))):
            return None
        sql = (f"SELECT d.{show_col} AS \"{dim}\" FROM {fact} f "
               f"JOIN {tab} d ON f.{key} = d.{key} "
               f"WHERE {where} GROUP BY d.{show_col} ORDER BY d.{show_col} LIMIT 100")
    else:
        col = dim_key
        sql = (f"SELECT f.{col} AS \"{dim}\" FROM {fact} f "
               f"WHERE {where} GROUP BY f.{col} ORDER BY f.{col} LIMIT 100")
    return {
        "sql": sql,
        "title": f"{dim}清单（多条件交集）",
        "metric": f"{dim}清单",
        "unit": "",
        "tables": [fact] + ([join[0]] if join else []),
        "compiled": True,
        "mql": {"metric": f"{dim}清单", "dimensions": [dim],
                "filters": dim_conds, "intersection": True},
    }


def try_compile_metric(query: str) -> dict | None:
    """尝试把 query 编译成确定性 SQL；失败返回 None（调用方回退 LLM）。

    返回 {"sql", "title", "metric", "unit", "tables", "compiled": True, "mql": {...}}
    - mql 是与 Aloudata 对齐的结构化中间表示（指标+维度+时间+筛选+取数），
      可审计、可展示「本次命中哪个指标、口径是什么」，前端据此做可解释性展示。
    """
    if not query:
        return None
    # ── 多条件交集清单（「既满足 A 又满足 B 的<维度>有哪些」）→ 确定性编译 ──
    # 放在最前：这类问法没有注册指标词，普通编译路径到不了，必须单独接管。
    # 详见 compile_intersection_list 的说明（实测 LLM 会答成全表无过滤的错答案）。
    _inter = compile_intersection_list(query)
    if _inter:
        return _inter
    # ── 占比 / 比例 / 构成类语义：编译器**没有**任何占比表达力，必须回退 LLM ──
    # 2026-09-29 修复（实测发现的高置信度错答）：本文件的编译产物只有「分组聚合绝对值」，
    # 一旦问句含占比语义，编译会把问题**静默降级**成绝对值：
    #   「设备故障造成的停机时长占全部停机时长的比例是多少」
    #     → `SELECT SUM(downtime_minutes) FROM eqp_downtime_record
    #        WHERE downtime_reason='设备故障' LIMIT 1`（只有分子，没有分母、没有除法）
    #   「各停机原因的停机时长占比」
    #     → 分组绝对值 + ORDER BY DESC（完全丢掉"占全部的比例"这一列）
    # 危险点在于它 0.1s 速出、零 LLM、看起来有数有列，用户拿到一行「停机时长 1234」
    # 以为答完了 —— 比回退 LLM 慢几秒更糟。占比的正确产物要么是窗口占比
    # （SUM(x)/SUM(SUM(x)) OVER ()），要么是分子分母同名过滤后相除，两者都在
    # 直生路径（见 llm_service._build_agg_hint 的占比 hint）里已支持。
    # 判据取「占比/比例/百分比/比重/构成/组成/份额/结构 + 占…的(比例|百分比)」；
    # 注意「率」字不在此列（良率/不良率/完成率是注册指标，有确定分子分母，编译正确）。
    #
    # 2026-09-29 修复（实测「非计划停机占比/严重缺陷占比」等注册口径被本闸门误杀）：
    # 注册口径名本身就含「占比」的（非计划停机占比/严重缺陷占比/设备故障停机占比 等），
    # 其 sql_expression 已正确表达「分子/分母相除」，编译只是原样执行注册算式，
    # 根本不存在「降级成绝对值」的风险 —— 之前一刀切 return None 把它们也打回 LLM 直生。
    # 修正：只有「未命中『名字/别名含占比』的注册口径」时，才说明是未注册的占比问法
    # （如「各停机原因占全部停机时长的比例」），才回退 LLM。
    if re.search(r"占比|比例|百分比|比重|份额|构成|组成|占比是多少|占.{0,8}的(?:比例|百分比)", query):
        try:
            from agent.metric_registry import find_metrics
            _ratio_hits = find_metrics(query)
        except Exception:
            _ratio_hits = []
        _hit_is_ratio = any(
            re.search(r"占比|比例|百分比", str(h.get("name") or ""))
            or any(re.search(r"占比|比例|百分比", a) for a in (h.get("aliases") or []))
            for h in _ratio_hits
        )
        if not _hit_is_ratio:
            return None
    # P0-修复（2026-09-03）：双指标相关性/对比问法禁止被压成单指标编译（见 _suppress_multi_metric_compile）
    if _suppress_multi_metric_compile(query):
        return None
    # 「X最早/最晚的是哪个」→ 时间型 Top，需 LLM（指标编译器无法安全推断时间排序列）。
    # 「X最高/最低/最多/最少的是哪个」→ 数值型 Top-1，下方按 order_dir 编译（ORDER BY ... LIMIT 1）。
    if re.search(r"最早|最晚", query) and \
       re.search(r"哪个|哪台|哪条|哪道|哪名|哪一位|是谁|哪家|哪间|哪座", query):
        return None
    # 同比/环比意图 → 走专用编译（避免被当成普通时间过滤错误编译出「本期值」）
    if _has_compare_intent(query):
        # 同比+环比同时出现 → 歧义，回退 LLM（与 compile_period_compare 内部守卫一致）
        if _COMPARE_YOY.search(query) and _COMPARE_MOM.search(query):
            return None
        _pc = compile_period_compare(query)
        if _pc:
            return _pc
        # 2026-09-29：总体对比编译器接不住的（它内部有「有维度即放弃」守卫，
        # 如「各产线本月产量环比上月」）不再直接返回 None，继续往下交给分组对比编译器。
    # 相邻两次间隔（「各设备相邻两次停机的间隔天数」）→ 确定性编译
    _iv = compile_interval(query)
    if _iv:
        return _iv
    # 显式双月份对比（「各产线7月产量相对6月的变化率」）→ 确定性编译
    _pd = compile_period_diff(query)
    if _pd:
        return _pd
    # 分组两期对比（「哪些产线本月产量超过上个月」）→ 确定性编译
    _gpc = compile_period_compare_grouped(query)
    if _gpc:
        return _gpc
    # 有三段式对比意图但编译器没接住 → 禁止落到普通编译：普通编译只有单期 WHERE，
    # 会把「本月 vs 上月」压成「上月的分组聚合」（实测「各产线本月产量比上月高」产出
    # WHERE 上月区间 + GROUP BY 产线，比较语义整个丢掉 —— 0.1s 静默错答，比走 LLM 更糟）。
    if _has_grouped_compare_intent(query):
        return None
    # 显式双时间点**并列对比**（「各车间6月与7月的产量各是多少」）→ 回退 LLM。
    # 非环比/同比词，但出现 ≥2 个不同时间点 + 并列语义（与/和/分别/各是多少/对比）——
    # 编译器不支持「按时间点条件聚合分列」，实测「各车间6月与7月产量」被误编译成
    # SUM(good+defect) 不分月的单时间总量（0.1s 高置信答错，比走 LLM 更危险）。
    _tp = set(re.findall(r"\d+月|本月|上月|上个月|今年|去年|去年同期|Q\d", query))
    if len(_tp) >= 2 and re.search(r"各是多少|各多少|分别|对比|与|和|及|、|vs|VS", query):
        return None
    # OFFSET 分页（「产量排名第2到第5」）→ 编译器只支持「前N」，不支持「第M到第N」的
    # OFFSET，会误编译成「前N名」（LIMIT N 无 OFFSET，返回第1~N 而非第M~N）→ 回退 LLM。
    # 2026-09-14 v4 题库题5 修复。
    if re.search(r"第[一二三四五六七八九十两\d]+\s*(?:到|至|~|-)\s*第?[一二三四五六七八九十两\d]+", query):
        return None
    # 时间窗口累计意图（本年累计/年初至今/累计到本月/近N天累计）→ 走累计编译
    # 注：裸「累计」（合计语义）不在此列，保持既有行为；compare 优先于 cumulative
    if _CUMULATIVE_INTENT_RE.search(query):
        return compile_cumulative(query)
    # 裸「累计/累加」+ 时间序列维度（每天/每日/逐日/按月/按日/按日期）→ SUM() OVER 累加
    # 序列，编译器不支持，会误编译成「按日分组聚合」丢累加语义 → 回退 LLM。
    # 2026-09-14 v4 题库题10 修复：「各产线6月1日至6月7日每天的累计产量」被误编译成
    # 每日普通聚合（223 行 vs 累计 33 行）。
    if re.search(r"累计|累加", query) and re.search(r"每天|每日|逐日|逐月|按日|按天|按月|按周|按日期", query):
        return None
    # 复杂 HAVING 过滤 + 第二排序指标（「停机次数超过5次的设备中，累计停机时长最长的前5台」）：
    # 编译器只支持单指标，会把「次数 + 时长」误编译成单「停机次数」列（漏时长、HAVING/排序也错）。
    # 2026-09-14 v5 题库题3 修复 → 回退 LLM。
    if re.search(r"(?:次数|时长|数量|金额)\s*(?:超过|低于|大于|小于|不少于|不低于)\s*\d+", query) \
            and re.search(r"中|里|其中", query) and re.search(r"最长|最高|最多|最短|最低|最少", query):
        return None
    # LAG 前后差值（「产量相比前一天变化了多少」）→ 编译器不支持窗口差值（LAG），
    # 会误编译成普通按日聚合（漏「环比前一天」差值语义，甚至漏单产线过滤）。
    # 2026-09-14 v5 题库题6 修复 → 回退 LLM。
    if re.search(r"相比前|环比前|日环比|前一天|逐日变化|变化了多少|较前一|增减了", query):
        return None
    # v1 仅 PG 编译（当前库）；MySQL 回退 LLM，避免方言坑
    if get_db_type() == "mysql":
        return None

    # 需要输出「名次/排名」列（窗口函数 RANK() OVER）→ 编译器**不支持窗口函数**，
    # 强行编译会退化成纯排序聚合、把名次列丢掉 —— 实测「各工序的良率排名是多少」
    # 被编译成 `SELECT 工序, 良率 ... ORDER BY 良率`，少了 `RANK()` 名次列而答错，
    # 且是 0.3s 速出、零 LLM、高置信的错答案。此类交 LLM（它能写窗口函数）。
    # 注意只拦「名次是答案对象」的问法（排名是多少/名次/第几名）；
    # 「产量排名前10的工单」这类只要排序的问法不受影响（实测仍能编译通过）。
    # 2026-09-14：先尝试**窗口确定性编译**（RANK 名次列 / 分组内TopN），失败再回退 LLM。
    if re.search(r"排名是多少|的名次|名次是|排名值|第几名", query) or _WIN_TOPN_RE.search(query):
        _wr = compile_window_rank(query)
        if _wr:
            return _wr
    # 分组内TopN 但外层是「X类别/品类」等未注册维度（「各产品类别内产量最高的产品」）：
    # 窗口编译已回退（外层不是注册维度），但普通编译会把「类别内最高的产品」误编译成
    # 「按类别汇总」（实测 GROUP BY product_category 得到单行总量，0.1s 高置信答错）。
    # 禁止降级，直接回退 LLM。
    if re.search(r"各[\u4e00-\u9fa5]{1,4}(?:类别|品类|类型|系列|种类)内[\u4e00-\u9fa5]{0,10}?"
                 r"(?:最高|最低|最多|最少|最大|最小|第[一二三四五六七八九十两\d]+)", query):
        return None
    if re.search(r"排名是多少|的名次|名次是|排名值|第几名", query):
        return None

    # 明细级 Top-N（「不良数最多的前2条产出记录」「最晚的3条停机记录」这类**行级**榜单）：
    # 正确产物是明细行，而编译器只产出**聚合**，强行编译会把它压成一个假聚合 ——
    # 实测「不良数最多的前2条产出记录」被编译成 `GROUP BY 产线`（答非所问，且行数也错）。
    # 交回 LLM 做行级查询（列名/关联键已有硬约束，TOP-N 的 LIMIT 由前置校验 9 兜底）。
    if re.search(r"(?:前|首)\s*\d+\s*(?:条|个|行|笔|项)?\s*[^，。；、\s]{0,4}?(?:记录|明细|数据|行)", query):
        return None

    from agent.metric_registry import find_metrics
    hits = find_metrics(query)
    if not hits or len(hits) > 2:
        return None  # 无指标 / 超过 2 个指标 → LLM（只安全编译 1~2 个同表指标）
    # P0-2 声明式多表指标（compile_plan）：命中即优先确定性编译（如「库存周转天数」双事实表 CTE）。
    # 编译失败（如时间表达不在支持集）→ 自然落到普通编译，普通编译会因多表而回退 LLM（红线不变）。
    for _m in hits:
        if _m.get("compile_plan"):
            _cp = compile_plan_metric(query, _m)
            if _cp:
                return _cp
            break
    # 用户显式写出列名（如"(available_qty)"）且同时命中多个口径（别名重叠，如
    # "可用库存总量"命中 库存量+总库存）→ 回退 LLM 按列直查，避免把两个口径
    # 同时编译进一条 SQL 造成"两列聚合"的错答。
    if len(hits) >= 2 and re.search(r"\([a-zA-Z_][a-zA-Z0-9_]*\)", query):
        return None

    # 值过滤：支持简单数值比较（聚合后 HAVING 过滤）；其他谓词回退 LLM（防注入）
    value_filter = _parse_value_filter(query)
    if value_filter is None and re.search(r"[<>]=?|大于|小于|超过|低于|高于|不少于|不低于|至少", query):
        # 2026-09-29 修复：值过滤词若落在命中口径词（name/别名）内部，是口径语义而非值过滤——
        # 典型「低于安全库存」是「库存缺口量」的别名，「低于」不是 HAVING 比较词；「库存缺口量」
        # 别名「低于安全库存的产品」同理。把这些口径词从 query 遮蔽后再判，避免误回退 LLM。
        _vfq = query
        for _m in hits:
            for _w in ([str(_m.get("name") or "")] + [str(a) for a in (_m.get("aliases") or [])]):
                _w = str(_w).strip()
                if _w and _w in _vfq:
                    _vfq = _vfq.replace(_w, " " * len(_w))
        if re.search(r"[<>]=?|大于|小于|超过|低于|高于|不少于|不低于|至少", _vfq):
            return None

    # 收集指标信息：全部必须落在同一可编译事实表
    metrics_info: list[dict] = []
    fact: str | None = None
    hierarchy: list[str] = []   # P0-4：命中指标的维度层级（父→子），供钻取解析
    for m in hits:
        tables = m.get("tables") or []
        if len(tables) != 1 or tables[0] not in _COMPILABLE_TABLES:
            return None  # 非单事实表 / 非白名单 → LLM
        if fact is None:
            fact = tables[0]
        elif tables[0] != fact:
            # P0-2 L3：多指标落不同事实表 → 尝试「共享维度表桥接」确定性编译（默认关闭，失败回退 LLM）
            return compile_multi_fact_bridge(query)
        if not hierarchy:
            hierarchy = _valid_hierarchy(m, fact)
        # 列名自适应：{colA|colB} 候选先解析成当前库实际列；再聚合覆盖（平均/最大）
        # 与 numeric 转换（顺序：resolve → agg_override → pg_numeric，保证候选列
        # 表达式也能被「平均/最长」正确覆盖）
        expr = _pg_numeric(_agg_override(_resolve_expr(m.get("sql_expression") or "", tables[0]), query))
        if not expr or not _safe_metric_expr(expr):
            return None
        mname = m["name"].split("(")[0].strip()
        if not mname or not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fa5 ]+", mname):
            return None  # 指标名含引号/特殊字符 → 无法安全做别名，回退 LLM
        metrics_info.append({
            "name": mname,
            "key": m.get("name"),
            "unit": m.get("unit", ""),
            "expr": expr,
            "sql_expression": m.get("sql_expression"),
        })

    meta = _FACT_META[fact]
    time_col = meta.get("time_col")
    name = metrics_info[0]["name"]
    unit = metrics_info[0]["unit"]

    # 维度：支持 0~2 个维度分组（二维分组：各A各B的X；超过 2 个 → LLM）
    dims = _detect_dim(query, fact)
    # 口径词污染剔除（2026-09-29 实测修复）：_detect_dim 在用户 query 里找维度词，
    # 但 query 里的某个维度词可能**不是分组信号、而是命中口径名/别名的一部分**——
    # 典型「各产线的设备故障停机占比」：query 里的「设备」来自指标名「设备故障停机占比」，
    # 被误当成分组维度 → 编出「设备×产线」二维分组；「各产品的设备故障停机占比」更糟：
    # eqp_downtime_record 无「产品」维度，只剩指标名里的「设备」污染词，静默按设备分组。
    # 判据用**位置区间**：维度词在 query 中的出现位置若落在某命中口径词（name/别名）
    # 的区间内，说明它是口径词的一部分而非分组信号 → 剔除；反之（如「各产品的库存缺口量」
    # 里「产品」位于「各产品」、不在「库存缺口量」区间内）保留，避免误伤正常分组。
    if dims:
        _spans: list[tuple[int, int]] = []
        for _m in hits:
            for _w in ([str(_m.get("name") or "")] + [str(a) for a in (_m.get("aliases") or [])]):
                _w = str(_w).strip()
                if not _w:
                    continue
                _i = query.find(_w)
                if _i >= 0:
                    _spans.append((_i, _i + len(_w)))
        _keep = []
        for _d in dims:
            _di = query.find(_d)
            # 维度词出现位置落在口径词区间内 → 判定为口径词的一部分，剔除
            if any(_di >= s and _di < e for s, e in _spans):
                # 2026-09-29 修复：但若维度词前紧邻分组信号（各/按/每/分/哪个/哪些/几个），
                # 说明它是用户的明确分组意图，而非口径词的一部分——典型「各产线的不良」：
                # 别名「各产线的不良」把分组维度「产线」也包了进来，按区间判断会把「产线」
                # 误剔导致维度清空 → 回退 LLM。而真正要防的「各产品的设备故障停机占比」里
                # 「设备」前是「的」（无分组信号），仍应剔除。故仅当维度词前**无**分组信号
                # 时才按「口径词一部分」剔除。
                _before = query[:_di]
                if not re.search(r"(各|按|每|分|哪个|哪些|几个|这几个|每个|各个)$", _before):
                    continue
            _keep.append(_d)
        dims = _keep
    if len(dims) > 2:
        return None
    # 排行/最高级意图但没写维度词（「列出抽检数最高的 TOP10」「抽检数排行」）：
    # 自动补表内注册维度分组，避免编译器退化成「整体汇总一行」答非所问。
    if not dims and not _want_date(query):
        _auto_dim = _auto_rank_dim(query, meta, time_col)
        if _auto_dim:
            dims = [_auto_dim]
    # 防静默降级：查询含「各X/按X/每个X/分X」等分组信号，但 X 无法解析到任何已注册维度
    # （如"各班次的产量"中 shift_code 未注册）→ 必须回退 LLM，禁止按"无维度总量"编译（答非所问）。
    # （「排行/排名」意图已由上面自动补维兜底，不再回退。）
    if not dims and not _want_date(query):
        # 2026-09-29 修复：分组信号若落在命中口径词内部，是口径语义而非分组意图——
        # 典型「停机分钟」别名里「分」匹配「分钟」、「平均每单计划量」里「每单」是
        # 指标语义（每张工单的平均），都不是「按X分组」。遮蔽口径词后再判分组信号，
        # 避免把单指标总量问法误回退 LLM。
        _gq = query
        for _m in hits:
            for _w in ([str(_m.get("name") or "")] + [str(a) for a in (_m.get("aliases") or [])]):
                _w = str(_w).strip()
                if _w and _w in _gq:
                    _gq = _gq.replace(_w, " " * len(_w))
        if re.search(
                r"各[\u4e00-\u9fa5]{1,6}|按[\u4e00-\u9fa5]{1,6}(?!\s*(天|日|月|周|季度|年))|每个[\u4e00-\u9fa5]{1,6}|每(?!次|天|日|月|周|季度|年)[\u4e00-\u9fa5]{1,6}(?!\s*(天|日|月|周|季度|年))|分[\u4e00-\u9fa5]{1,6}", _gq):
            return None
    dim = dims[0] if dims else None
    want_date = _want_date(query) if time_col else False
    # P0-4 钻取解析：层级维度 + 下钻词 + 父级值过滤（「一车间各产线的产量」→ 定位一车间下钻）
    drillable: dict | None = None
    drill_filter: dict | None = None
    if dim and hierarchy:
        dim, drill = _resolve_drill(query, hits[0], fact, [dim])
        if drill is None:
            drillable = None
        elif "filter_value" in drill:
            drill_filter = drill  # 值过滤下钻（WHERE 父级列 = 值），不再提示继续下钻
        elif "current_level" in drill:
            drillable = drill
            drillable["metric"] = name  # 前端构造下钻问题（「{值}各{下一级}的{指标}」）需要指标名

    # ── 关系/对比类语义补全（图表多样性配套修复）──────────────────
    # 「停机时长和停机次数的关系」这类问题：两个指标、无维度、无时间分组，
    # 原逻辑会编译成 LIMIT 1 单行聚合 → 散点/关系图无数据可画（chart=none）。
    # 修复：自动选注册顺序的第一个维度分组，产出多行双指标数据（确定性，可审计）。
    if (not dim and not want_date and len(metrics_info) >= 2
            and re.search(r"关系|相关|散点|关联|对比", query) and meta["dims"]):
        dim = next(iter(meta["dims"]))
        mql_note_rel = True
    else:
        mql_note_rel = False

    # 取数：前N名 / Top-1（数值型「X最高/最低/最多/最少的是哪个」）
    limit = 20
    order_dir = "DESC"
    m2 = re.search(r"前\s*(\d+)(?:\s*(?:名|条|个|张|家|项|台|道|种))?|TOP\s*(\d+)|最(?:大|高|多|久|长|小|低|少|短|晚|早|新|旧)\s*(?:的)?\s*(?:前)?\s*(\d+)\s*(?:个|条|名|张|批|道|台|种|家|项)?", query, re.IGNORECASE)
    if m2:
        limit = int(next(g for g in m2.groups() if g))
    # 2026-09-14：**「各X/按X/每个X」且未指定前 N → 返回全部维度行**。
    # 此前一律默认 20，在真实数据量下会静默截断（yans 实测：各设备实际 40 台只回 20 条、
    # 各产品实际 28 个只回 20 条，占未注册口径失败的 42%），用户拿到的列表是残缺的。
    elif re.search(r"各[\u4e00-\u9fa5]{1,8}|按[\u4e00-\u9fa5]{1,8}|每个[\u4e00-\u9fa5]{1,8}", query):
        limit = 1000
    _top_which = re.search(r"哪个|哪台|哪条|哪道|哪名|哪一位|是谁|哪家|哪间|哪座|哪一家|是啥|是什么", query)
    if _top_which and re.search(r"最多|最高|最大|最长|最久|最新|最晚", query):
        limit, order_dir = 1, "DESC"
    elif _top_which and re.search(r"最少|最低|最小|最短|最早|最旧", query):
        limit, order_dir = 1, "ASC"
    # 2026-09-14：**非 Top-1 的排名同样要按最高级词定方向**。此前只在「哪个+最高级」
    # 这种 Top-1 情形设置 order_dir，其余一律沿用默认 DESC —— 实测「标准良率最低的
    # 前3道工序」被编译成 ORDER BY … DESC，返回了**最高**的前 3 个（99.5/99/98.5，
    # 而正确答案是 96.5/97/97）。方向错是最隐蔽的一类错误：行数对、指标对，就是排序反了。
    # 「最晚/最新」按时间语义取最大 → DESC；「最早/最旧」→ ASC。
    else:
        # 2026-10-01 补充：显式排序短语此前完全没被识别，一律落到默认 DESC。
        # 实测「各工序良率从低到高」→ ORDER BY "良率" DESC，首行是 99.21（最高的）；
        # 「各产线产量从少到多」→ 同样 DESC，首行 690403（最多的）。
        # 行数对、指标对、就是方向反了 —— 用户完全看不出异常。
        # 放在最高级词之前判断：带「从X到Y」的问句方向语义比「最X」更明确。
        if re.search(r"从\s*(?:低|小|少|短|早|旧|慢|差)\s*(?:到|至)\s*(?:高|大|多|长|晚|新|快|好)"
                     r"|升序|从小到大|从低到高|从少到多", query):
            order_dir = "ASC"
        elif re.search(r"从\s*(?:高|大|多|长|晚|新|快|好)\s*(?:到|至)\s*(?:低|小|少|短|早|旧|慢|差)"
                       r"|降序|从大到小|从高到低|从多到少", query):
            order_dir = "DESC"
        elif re.search(r"最低|最少|最小|最短|最差|最早|最旧|最慢", query):
            order_dir = "ASC"
        elif re.search(r"最高|最多|最大|最长|最好|最新|最晚|最快", query):
            order_dir = "DESC"

    # 时间范围（人类可读描述，供 mql 展示）
    time_desc = "全部"
    time_filter = _build_time_filter(fact, time_col, query) if time_col else None
    if time_filter is not None:
        if (mm := re.search(r"近\s*(\d+)\s*天", query)):
            time_desc = mm.group(0)
        elif (mm := re.search(r"近\s*(\d+)\s*个月", query)):
            time_desc = mm.group(0)
        elif "上月" in query or "上个月" in query:
            time_desc = "上月"
        elif "本月" in query or "这个月" in query:
            time_desc = "本月"
        elif "今年" in query or "本年" in query:
            time_desc = "今年"
        elif "去年" in query:
            time_desc = "去年"
        elif (mm := re.search(r"(?:近|最近|过去|前)\s*(?:[0-9]+|[一二两三四五六七八九十]+)\s*个?\s*月"
                              r"|(?:近|最近|过去|前|这)\s*半年", query)):
            time_desc = mm.group(0)
        elif (mm := re.search(r"(?:近|最近|过去|前)\s*(?:[0-9]+|[一二两三四五六七八九十]+)\s*(?:个)?\s*(?:天|日)", query)):
            time_desc = mm.group(0)
        else:
            time_desc = "自定义时间范围"
    where = f" WHERE {time_filter}" if time_filter else ""
    # ── 点名枚举值过滤（2026-09-28 新增）──────────────────────────────────
    # 场景：「传感器这个产品类别下各产线的产量」——问句里点名了维表取值
    # （传感器 = dim_product.product_category 的真实取值），但既不是时间过滤、
    # 也不是父子层级下钻。改造前这类问句要么被 `_drill_value` 误当成父级值
    # （生成 `workshop_name='传感器这个产品类别下'` → 0 行静默失败），要么被
    # 忽略掉条件（结果偏大但不报错）。
    # 实现用 EXISTS 子查询而不是新增 JOIN：FROM/GROUP BY 的拼装逻辑在下方多个
    # 分支里各自分支（趋势/单维/双维/多维），逐个加 JOIN 要改 4 处且容易漏；
    # EXISTS 只往 WHERE 追加一个自包含子句，与所有分支正交，零副作用。
    # 只认倒排索引里**逐字存在**的值（不猜、不模糊匹配）；索引不可用时不生效。
    if not drill_filter:
        try:
            # 口径词遮蔽（2026-09-29 实测修复）：命中口径名/别名里含有的枚举值词会被
            # _explicit_value_filters 误当成「点名值过滤」——典型「各产线的设备故障停机占比」
            # 里「设备故障」是 downtime_reason 真实枚举值，被加 `WHERE downtime_reason='设备故障'`，
            # 导致分子分母同时被限死在子集里，占比全变 100%（语义全错）；「换线调机时长」等同理。
            # 处理：把 query 里命中口径词（name/别名）的区间替换成空格后再交给值过滤，
            # 口径词内的枚举值不再被当过滤值；口径词外的真实点名值（如「传感器这个产品类别下…」）不受影响。
            _vf_query = query
            for _m in hits:
                for _w in ([str(_m.get("name") or "")] + [str(a) for a in (_m.get("aliases") or [])]):
                    _w = str(_w).strip()
                    if _w and _w in _vf_query:
                        _vf_query = _vf_query.replace(_w, " " * len(_w))
            _enum_conds = _explicit_value_filters(_vf_query, fact)
            for _c in _enum_conds:
                where = f" WHERE {_c}" if not where else f"{where} AND {_c}"
        except Exception:
            pass
    # P0-4 钻取值过滤：维度层级父级列 = 值（「一车间各产线的产量」→ d.workshop='一车间'）。
    # 仅 JOIN 型层级出现（值来自维度表展示列），用 d. 前缀避免与事实表同名列歧义。
    if drill_filter:
        pdef = _resolve_dim_def(fact, meta["dims"].get(drill_filter["parent_level"]) or {})
        if pdef.get("join"):
            _pt, _pk, _pdisp = pdef["join"]
            cond = f"d.{_pdisp} = '{drill_filter['filter_value']}'"
            where = f" WHERE {cond}" if not where else f"{where} AND {cond}"
        else:
            # 直接分组型父级（少见）：退化为无过滤（值无法限定到安全列），保持普通编译
            drill_filter = None

    # ── 结构化 MQL（确定性意图表示，可审计 / 可解释）──
    primary = metrics_info[0]
    metric_defs = [
        {"name": mi["name"],
         "definition": f"{mi['name']} = {mi['sql_expression']}" + (f"（单位：{mi['unit']}）" if mi["unit"] else "")}
        for mi in metrics_info
    ]
    mql = {
        "metric": primary["name"],
        "metric_key": primary["key"],
        "metric_expression": primary["sql_expression"],
        "metric_definition": metric_defs[0]["definition"],
        "unit": unit,
        "fact_table": fact,
        "metrics": metric_defs,
        "dimensions": ([dim] if dim else []),
        "time_granularity": (None if not want_date else
                             ("month" if "月" in query else ("year" if "年" in query else "day"))),
        "time_range": time_desc,
        "value_filter": ({"op": value_filter[0],
                          "value": (value_filter[1] if (value_filter[2] and unit == "%") else
                                    (value_filter[1] / 100.0 if value_filter[2] else value_filter[1]))}
                         if value_filter else None),
        "limit": limit,
        "compiled_by": "metric_compiler",
        # P0-4 层级钻取白盒信息（前端图表点击联动素材）
        "drillable": drillable,
        # 关系/对比语义自动补维度的说明（白盒可解释）：原查询无维度，编译器按
        # "关系|相关|散点|关联|对比"语义选了首个注册维度分组，产出多行供关系分析。
        "note": ("关系/对比语义下自动补充维度分组（原查询未指定维度）"
                 if mql_note_rel else None),
    }

    proj = ", ".join(f'{mi["expr"]} AS "{mi["name"]}"' for mi in metrics_info)
    involved_tables = [fact]
    # 值过滤 → HAVING（聚合后过滤；表达式用主指标聚合式）
    having = ""
    # 百分比阈值尺度对齐：unit="%" 的指标表达式返回 0~100（如良率 SUM(good_qty)*100.0/...），
    # 阈值保持百分数刻度；其余（ratio 0~1 或无量纲）的 "%" 阈值折算为 0~1 小数。
    if value_filter:
        _op, _raw_val, _is_pct = value_filter
        _val = _raw_val if (_is_pct and unit == "%") else (_raw_val / 100.0 if _is_pct else _raw_val)
        having = f' HAVING {primary["expr"]} {_op} {_val}'

    if dim and want_date:
        # ── 维度 + 时间双分组（趋势类修复）────────────────────────
        # 「各产线的产量趋势」：_want_date 识别到"趋势"，但原 dim 分支优先执行、
        # 把时间维度丢掉 → SQL 只有 GROUP BY 产线，画不了按维度的折线。
        # 修复：GROUP BY 维度 + 时间，产出「维度 × 日期」多序列数据（折线图形态）。
        fmt = _trend_grain_fmt(query)
        date_expr = f"to_char(f.{time_col}, '{fmt}')"
        trend_limit = max(limit, 60)   # 趋势需要足够的时间点，20 行不够画
        dim_def = _resolve_dim_def(fact, meta["dims"][dim])
        if dim == "产品" and ("类别" in query or "品类" in query or re.search(r"\bcategory\b", query, re.IGNORECASE)):
            if dim_def.get("join"):
                _dt, _dk, _ = dim_def["join"]
                dim_def = {"fact_col": dim_def["fact_col"], "join": (_dt, _dk, _resolve_col(_dt, ["product_category", "category"]))}
        if dim_def.get("join"):
            dtable, dkey, ddisplay = dim_def["join"]
            involved_tables.append(dtable)
            _vjoin, _oncol = _dim_join_clause(dim_def, "d")
            if dim_def.get("via"):
                involved_tables.append(dim_def["via"][0])
            sql = (
                f'SELECT d.{ddisplay} AS "{dim}", {date_expr} AS "日期", {proj} '
                f'FROM {fact} f{_vjoin} JOIN {dtable} d ON {_oncol} = d.{dkey}'
                f'{where} GROUP BY d.{ddisplay}, {date_expr}{having} '
                f'ORDER BY "日期", "{dim}" LIMIT {trend_limit}'
            )
        else:
            sql = (
                f'SELECT f.{dim_def["fact_col"]} AS "{dim}", {date_expr} AS "日期", {proj} '
                f'FROM {fact} f{where} GROUP BY f.{dim_def["fact_col"]}, {date_expr}{having} '
                f'ORDER BY "日期", "{dim}" LIMIT {trend_limit}'
            )
        mql["dimensions"] = [dim, "日期"]
        mql["limit"] = trend_limit
    elif len(dims) == 2:
        # 二维分组（各A各B的X）。
        # 2026-09-19：放开「JOIN 维度」组合。此前二维分组**仅支持两维均为直接分组列**，
        # 任一维是 JOIN 维度表就 return None 回退 LLM —— 实测 postgres#22「各产品类别的
        # 不良类型分布」：产品类别在 dim_product（JOIN）、不良类型在事实表，旧逻辑两头落空
        # （编译器不接管，LLM 又只 GROUP BY defect_type 丢掉问句点名的维度，7 行变 5 行）。
        # 放开后由编译器确定性产出「JOIN 维 + 事实表维」两组分组列；同表同键的维度复用同一
        # JOIN 别名（如「产线」「车间」同属 dim_production_line），避免重复连接与别名冲突。
        d1, d2 = dims[0], dims[1]
        def1 = _resolve_dim_def(fact, meta["dims"][d1])
        def2 = _resolve_dim_def(fact, meta["dims"][d2])
        # via（两跳 join）维度在双维度分支下需要两个互不冲突的桥接表别名，此处不支持
        # → 回退 LLM。宁可不编，也不能拼出连不上的 JOIN（双维度场景罕见，不值得为此冒险）。
        if def1.get("via") or def2.get("via"):
            return None
        # 含 JOIN 维度时**仅在问句有显式分组信号**（各/按/每个/每X）才编译，否则回退 LLM。
        # 反例（yans 回归实测）：「良率最低的工序在哪些产线上执行」同时命中「工序」「产线」
        # 两个维度，但语义是"先筛出良率最低的工序、再列出执行它的产线"，**不是**二维分组；
        # 放开 JOIN 后编译器按 (产线,工序) 分组算良率 → 20 行 vs gold 5 行，会把一道原本
        # 由 LLM 答对的高价值题改坏（yans 6/6 → 5/6）。加分组信号守卫即恢复原行为。
        # 两维均为直接分组列（join=None）时**不加**此守卫，严格保持改动前的编译行为。
        if (def1.get("join") or def2.get("join")) and not re.search(
                r"各[\u4e00-\u9fa5]{1,8}|按[\u4e00-\u9fa5]{1,8}"
                r"|每个[\u4e00-\u9fa5]{1,8}|每[\u4e00-\u9fa5]{1,8}", query):
            return None
        _sel, _grp, _joins, _alias = [], [], [], {}
        for _dn, _dd in ((d1, def1), (d2, def2)):
            if _dd.get("join"):
                _dt, _dk, _disp = _dd["join"]
                _ak = (_dt, _dd["fact_col"], _dk)
                _al = _alias.get(_ak)
                if _al is None:
                    _al = "d%d" % (len(_alias) + 1)
                    _alias[_ak] = _al
                    _joins.append((_dt, _al, _dd["fact_col"], _dk))
                    involved_tables.append(_dt)
                _sel.append(f'{_al}.{_disp} AS "{_dn}"')
                _grp.append(f"{_al}.{_disp}")
            else:
                _sel.append(f'f.{_dd["fact_col"]} AS "{_dn}"')
                _grp.append(f'f.{_dd["fact_col"]}')
        _join_sql = "".join(
            f" JOIN {_t} {_al} ON f.{_fc} = {_al}.{_dk}" for (_t, _al, _fc, _dk) in _joins)
        sql = (
            f'SELECT {", ".join(_sel)}, {proj} '
            f'FROM {fact} f{_join_sql}{where} GROUP BY {", ".join(_grp)}{having} '
            f'ORDER BY "{name}" {order_dir} LIMIT {limit}'
        )
        mql["dimensions"] = [d1, d2]
    elif dim:
        # 维度分组：JOIN 维度表 或 直接分组字符串列
        dim_def = _resolve_dim_def(fact, meta["dims"][dim])
        # 「产品类别/产品品类/类别」应分组到 category 列，而非默认展示列 product_name。
        # 问题里明确出现「类别/品类」或英文列名 category 时，改用 dim_product.category 分组。
        if dim == "产品" and ("类别" in query or "品类" in query or re.search(r"\bcategory\b", query, re.IGNORECASE)):
            if dim_def.get("join"):
                _dt, _dk, _ = dim_def["join"]
                dim_def = {"fact_col": dim_def["fact_col"], "join": (_dt, _dk, _resolve_col(_dt, ["product_category", "category"]))}
        if dim_def.get("join"):
            dtable, dkey, ddisplay = dim_def["join"]
            involved_tables.append(dtable)
            _vjoin, _oncol = _dim_join_clause(dim_def, "d")
            if dim_def.get("via"):
                involved_tables.append(dim_def["via"][0])
            sql = (
                f'SELECT d.{ddisplay} AS "{dim}", {proj} '
                f'FROM {fact} f{_vjoin} JOIN {dtable} d ON {_oncol} = d.{dkey}'
                f'{where} GROUP BY d.{ddisplay}{having} ORDER BY "{name}" {order_dir} LIMIT {limit}'
            )
        else:
            sql = (
                f'SELECT f.{dim_def["fact_col"]} AS "{dim}", {proj} '
                f'FROM {fact} f{where} GROUP BY f.{dim_def["fact_col"]}{having} '
                f'ORDER BY "{name}" {order_dir} LIMIT {limit}'
            )
    elif want_date:
        fmt = _trend_grain_fmt(query)
        # 排行式时间聚合（「投入量最大的5周 / 产量最高的一天」）：按指标排序截取 TOP N，
        # 不能像纯趋势那样 ORDER BY 日期全量返回。
        _tm = re.search(
            r"(?:最大|最高|最多|最长)\s*(?:的)?\s*(?:(\d+|[一二两三四五六七八九十]+)\s*(?:个|条)?\s*)?"
            r"(?:周|星期|天|日|个月|月|季|季度|年|期)|"
            r"产量最高的一天|最高(?:的)?(?:那|这)?一?天|哪(?:一天|天|周|个月|月)", query)
        if _tm:
            _tmn = _tm.group(1) or "1"
            _CNM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
                    "六": 6, "七": 7, "八": 8, "九": 9}
            _lim = int(_tmn) if _tmn.isdigit() else _CNM.get(_tmn, 1)
            sql = (
                f"SELECT to_char(f.{time_col}, '{fmt}') AS \"日期\", {proj} "
                f"FROM {fact} f{where} GROUP BY to_char(f.{time_col}, '{fmt}'){having} "
                f'ORDER BY "{name}" DESC LIMIT {_lim}'
            )
        else:
            sql = (
                f"SELECT to_char(f.{time_col}, '{fmt}') AS \"日期\", {proj} "
                f"FROM {fact} f{where} GROUP BY to_char(f.{time_col}, '{fmt}'){having} "
                f'ORDER BY "日期" LIMIT {limit}'
            )
    else:
        sql = f'SELECT {proj} FROM {fact} f{where}{having} LIMIT 1'

    return {
        "sql": sql,
        "title": (f"{name}查询" if len(metrics_info) == 1
                  else "与".join(mi["name"] for mi in metrics_info) + "查询"),
        "metric": name,
        "metrics": [mi["name"] for mi in metrics_info],
        "unit": unit,
        "tables": involved_tables, "compiled": True, "mql": mql,
        "drillable": drillable,  # P0-4：可下钻信息（None 表示不可下钻）
    }
