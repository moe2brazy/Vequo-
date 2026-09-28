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
    return dd


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
                     rf"|哪台{cn}|哪些{cn}|哪个{cn}|几台{cn}|哪条{cn}|哪几个{cn}|这几个{cn}|这些{cn}|{cn}分别|分别{cn}", query, re.IGNORECASE):
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
    return val[:14]


def _auto_rank_dim(query: str, meta: dict, time_col: str | None) -> str | None:
    """排行/最高级意图但问句没写任何维度词时（「列出抽检数最高的 TOP10」「抽检数排行」），
    自动补一个表内已注册维度做分组，避免编译器退化成「整体汇总一行」答非所问。

    - 日期诉求（哪天最多/每天）→ 优先取时间列对应的注册维度；
    - 其余排行 → 优先带 JOIN 的业务维度（可显示中文名，如 产品/工序/产线），否则首个注册维度；
    - 没有任何可分组维度 → None（保持原汇总/回退路径，不瞎编）。
    """
    rank_intent = bool(re.search(
        r"TOP\s*\d+|最高|最低|最多|最少|最大|最小|最长|最短|最好|最差|最晚|最早|最新|最旧"
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
            if val:
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


def _build_time_filter(fact_table: str, fact_col: str, query: str) -> str | None:
    """翻译「近N天/近N个月/上月/本月/今年/去年」→ PG 日期过滤。

    锚点语义区分：
    - 相对区间（近N天/近N个月）：锚点用 `MAX(事实列)`，静态/滞后数据集下仍能命中数据；
    - 绝对日历周期（本月/今年/上月/去年/本季度）：锚点用 `CURRENT_DATE`，符合「本月=当前日历月」
      的语义（滞后数据下"本月"无数据即返回 0/NULL，与评测口径一致）。
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
    if "上月" in query or "上个月" in query:
        return f"{fact_col} >= date_trunc('month', CURRENT_DATE) - INTERVAL '1 month' AND {fact_col} < date_trunc('month', CURRENT_DATE)"
    if "本月" in query or "这个月" in query:
        return f"{fact_col} >= date_trunc('month', CURRENT_DATE)"
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
        where = f"{time_col} >= date_trunc('month', CURRENT_DATE)"
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
            prev_gte = f"{time_col} >= {ms} - INTERVAL '1 month'"
            prev_lt = f"{time_col} < {ms}"
            prev_label = "上月"
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
            prev_lt = f"{time_col} < {qs}"
            prev_label = "上季度"
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
            prev_lt = f"{time_col} < {ys}"
            prev_label = "去年"
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

    cur_agg = f"SUM(CASE WHEN EXTRACT(MONTH FROM {time_col}) = {cur_m} THEN {inner} ELSE 0 END)"
    prev_agg = f"SUM(CASE WHEN EXTRACT(MONTH FROM {time_col}) = {prev_m} THEN {inner} ELSE 0 END)"

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
    r"相关|关系|关联|对比|比较|差异|影响|是不是|是否.*(有关|相关)|有没有.*(关系|影响|关联)"
    r"|谁高|谁低|哪个.*(高|低)|越长|越短|越多|越少|越高|越低|成正比|反比")


def _suppress_multi_metric_compile(query: str) -> bool:
    """问句是否「多指标 + 关系/对比意图」→ 是则禁止编译。"""
    if not query or not _MULTI_ANALYSIS_INTENT_RE.search(query):
        return False
    try:
        from agent.metric_registry import get_effective_metrics
        named: set[str] = set()
        for m in get_effective_metrics():
            words = [str(m.get("name") or "")] + [str(a) for a in (m.get("aliases") or [])]
            for w in words:
                w = re.sub(r"\(.*?\)", "", w).strip()
                if len(w) >= 2 and w in query:
                    named.add(str(m.get("name") or ""))
                    break
            if len(named) >= 2:
                break
        return len(named) >= 2
    except Exception:
        return False


def try_compile_metric(query: str) -> dict | None:
    """尝试把 query 编译成确定性 SQL；失败返回 None（调用方回退 LLM）。

    返回 {"sql", "title", "metric", "unit", "tables", "compiled": True, "mql": {...}}
    - mql 是与 Aloudata 对齐的结构化中间表示（指标+维度+时间+筛选+取数），
      可审计、可展示「本次命中哪个指标、口径是什么」，前端据此做可解释性展示。
    """
    if not query:
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
        return compile_period_compare(query)
    # 相邻两次间隔（「各设备相邻两次停机的间隔天数」）→ 确定性编译
    _iv = compile_interval(query)
    if _iv:
        return _iv
    # 显式双月份对比（「各产线7月产量相对6月的变化率」）→ 确定性编译
    _pd = compile_period_diff(query)
    if _pd:
        return _pd
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
    if not dims and not _want_date(query) and re.search(
            r"各[\u4e00-\u9fa5]{1,6}|按[\u4e00-\u9fa5]{1,6}(?!\s*(天|日|月|周|季度|年))|每个[\u4e00-\u9fa5]{1,6}|每(?!次|天|日|月|周|季度|年)[\u4e00-\u9fa5]{1,6}(?!\s*(天|日|月|周|季度|年))|分[\u4e00-\u9fa5]{1,6}", query):
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
        if re.search(r"最低|最少|最小|最短|最差|最早|最旧|最慢", query):
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
        else:
            time_desc = "自定义时间范围"
    where = f" WHERE {time_filter}" if time_filter else ""
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
            sql = (
                f'SELECT d.{ddisplay} AS "{dim}", {date_expr} AS "日期", {proj} '
                f'FROM {fact} f JOIN {dtable} d ON f.{dim_def["fact_col"]} = d.{dkey}'
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
            sql = (
                f'SELECT d.{ddisplay} AS "{dim}", {proj} '
                f'FROM {fact} f JOIN {dtable} d ON f.{dim_def["fact_col"]} = d.{dkey}'
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
