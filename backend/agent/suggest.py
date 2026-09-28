"""引导问题生成器 — 根据当前数据库的重点动态生成快捷提问

思路：以当前库真实表为输入，结合元数据（中文别名/描述/关键词）与真实行数，
识别业务重点（良率/产量/库存/停机/工单/检验等），套用问题模板生成引导问题。
- 行数大的表 = 重点表 → 权重高
- 模板问题用「真实表名 + 真实字段名」动态组装，不再输出与库无关的静态文案
- 每类业务最多出 1 条，避免重复
- 数据库不变时缓存 10 分钟，避免每次刷新重复查库
- 返回结构化条目：{id, question, category, source, table, weight}，前端可排序/展示依据
"""

import random
import re
import time
import threading

from database import get_database_config
from db.executor import get_table_row_counts
from db.metadata import TABLES

# ── 模板定义：keywords → 类别 + 问题模板 + 度量/维度字段候选词 ──
# 问题模板占位符：{table} 表显示名、{dim} 维度字段、{measure} 度量字段
# 模板结构：(keywords, category, pattern, measure_hints, dim_hints, require, exclude)
#   require ：关键字文本**必须**包含这些词之一才可命中（限定业务域，防止词面撞表）；
#   exclude ：关键字文本包含任一词即跳过该表（排除错配：销售撞工单 order、停机撞设备主数据等）。
# 2026-09-03 修复：引导池错配（sales×工单表生成"销售计划产量排行"、purchase×库存表、
# 停机×设备主数据、工单完成×停机/产出表、质量排行×无类型列的表）。
QUESTION_TEMPLATES = [
    # 生产良率：只对产出事实表（含 good_qty/input_qty）
    (["良率", "质量", "合格", "品质", "quality", "yield"], "quality",
     "{table}各{dim}的良率分析", ["良率", "合格", "good_qty", "yield"], ["工序", "产线", "process", "line", "班组", "工段"],
     ["mes_process_output", "process_output", "工序产量"], ["eqp_", "停机", "inv_", "库存", "qms_", "检验", "inspect", "work_order", "工单", "dim_"]),
    # 不良类型排行：需要"类型/原因"列，只落在缺陷明细表
    (["不良", "缺陷", "次品", "返工", "defect"], "quality",
     "{table}的不良类型排行 TOP10", ["不良", "defect", "缺陷"], ["类型", "原因", "type", "reason", "code"],
     ["qms_defect_detail", "defect_type", "不良明细"], ["inspection", "检验", "process_output", "工序产量", "work_order", "工单", "dim_", "eqp_", "停机"]),
    # 质检不合格：只落在检验/抽检表
    (["检验", "抽检", "质检", "inspection", "inspect"], "quality",
     "质量抽检不合格最多的{dim}", ["不合格", "不良", "defect", "缺陷"], ["工序", "产线", "process", "line"],
     ["qms_inspection", "inspection", "检验", "抽检"], ["mes_process_output", "process_output", "eqp_", "停机", "inv_", "库存", "work_order", "工单", "dim_"]),
    # 产量/投入趋势：只对产出/工单事实表
    (["产量", "产出", "产能", "输出", "production", "output"], "production",
     "统计各{dim}最近7天的{measure}趋势", ["产量", "产出", "qty", "数量", "output", "amount"], ["产线", "工序", "line", "process"],
     ["mes_process_output", "mes_work_order", "process_output", "work_order", "产出", "产量"], ["eqp_", "停机", "qms_", "检验", "inventory", "库存", "dim_"]),
    # 工单完成：仅限工单语义表（yans 无 actual_qty 时完成率算不了，但状态/计划口径可用）
    (["工单", "排产", "计划", "执行", "work_order"], "work_order",
     "{table}的工单完成情况统计", ["实际", "完成", "actual", "qty", "数量", "状态", "status"], ["状态", "status"],
     ["工单", "work_order"], ["eqp_", "停机", "qms_", "检验", "process_output", "inv_", "库存", "dim_"]),
    # 库存预警：仅限库存快照表
    (["库存", "仓库", "安全库存", "存货", "inventory", "stock", "warehouse"], "inventory",
     "{table}低于安全库存的{dim}预警", ["库存", "stock", "qty", "可用", "数量"], ["物料", "产品", "product", "material", "仓库", "warehouse"],
     ["inv_inventory", "inventory", "库存", "仓库", "available", "safety"], ["eqp_", "停机", "qms_", "mes_", "dim_", "检验"]),
    # 停机原因：仅限停机记录表（排除设备主数据等维度表）
    (["停机", "设备", "维护", "故障", "equipment", "downtime", "machine"], "equipment",
     "{table}停机原因分析", ["停机", "时长", "分钟", "downtime"], ["原因", "reason", "设备", "equipment"],
     ["downtime", "停机"], ["dim_", "主数据", "mes_", "qms_", "inv_", "库存"]),
    # 销售：需要真实销售域表（yans 无销售表 → 自然无候选，不再误撞工单表 order 字样）
    (["销售", "订单", "客户", "sales", "order", "customer"], "sales",
     "销售{measure}排行", ["金额", "amount", "价格", "price", "数量", "qty"], ["客户", "customer", "产品", "product"],
     ["sales", "销售", "客户", "customer", "订单金额", "unit_price", "quantity"], ["工单", "work_order", "计划", "plan", "产线", "工序", "inventory", "库存", "snapshot"]),
    # 采购：需要真实采购域表
    (["采购", "供应商", "物料", "purchase", "supplier", "material"], "purchase",
     "物料采购{measure}排行", ["数量", "qty", "金额", "amount"], ["供应商", "supplier", "物料", "material"],
     ["采购", "供应商", "purchase", "supplier", "procurement"], ["inventory", "库存", "快照", "snapshot", "available", "工单", "work_order", "产线", "工序"]),
    # 人事
    (["员工", "考勤", "人事", "绩效", "employee", "attendance", "hr"], "hr",
     "部门员工人数统计", ["人数", "数量", "count", "员工"], ["部门", "department", "dept"],
     ["员工", "人事", "考勤", "employee", "attendance"], ["工单", "work_order", "qms_", "eqp_", "inv_", "库存", "产线", "工序"]),
]

# 兜底通用问题（始终可用）
FALLBACK_QUESTIONS = [
    "查看当前数据库有哪些表",
    "分析各产线最近7天的产量趋势",
    "不良类型排行 TOP10",
]

# 数据库分析类常见问题（用户要求混入推荐池）：秒出（analyze_db/lookup 概览），
# 不依赖注册口径，用于"换一批"时提供探索类选择。
_DB_ANALYSIS_QUESTIONS = [
    "查看当前数据库有哪些表",
    "当前数据库有多少张表",
    "这个数据库主要记录什么业务",
    "分析当前数据库的整体概况",
    "各张表大概有多少行数据",
    "数据库里有哪些业务表，分别是干嘛的",
]

_cache: dict = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 60  # 1 分钟（配合加权随机轮换：每次刷新组合不同，又不至于频繁重建）

_NUMERIC_TYPES = {
    "int", "integer", "bigint", "smallint", "numeric", "decimal",
    "float", "double", "real", "number",
    "bigserial", "serial", "smallserial",
}
_CATEGORICAL_TYPES = {"varchar", "char", "text"}

# PG/MySQL 类型全称 → 短名归一化（information_schema 返回的是全称）
_TYPE_ALIASES = {
    "character varying": "varchar",
    "character": "char",
    "timestamp with time zone": "timestamp",
    "timestamp without time zone": "timestamp",
    "time with time zone": "time",
    "time without time zone": "time",
    "double precision": "double",
    "bigserial": "bigint",
    "serial": "integer",
    "smallserial": "smallint",
}


def _norm_type(t: str) -> str:
    return _TYPE_ALIASES.get(t, t)

# 元数据字段（用于给真实库里没有中文描述的表补充字段信息）
_META_FIELDS = {t["table_name"]: t.get("fields", []) for t in TABLES}
# 字段详情查询的小缓存（带 db_key 隔离 + 5 分钟 TTL + 锁，防跨库串数据与并发写坏）
_fields_cache_lock = threading.Lock()
_fields_cache: dict = {}   # (db_key, 表名) -> (ts, fields)
_FIELDS_TTL = 300


def _db_key() -> str:
    cfg = get_database_config()
    return f"{cfg.get('db_type', 'pg')}:{cfg.get('host', '')}:{cfg.get('port', '')}:{cfg.get('name', '')}"


def _table_keywords(table: dict) -> str:
    """聚合一张表的所有可匹配文本（别名/描述/关键词/表名）"""
    parts = [
        str(table.get("table_alias", "")),
        str(table.get("description", "")),
        " ".join(str(k) for k in table.get("keywords", [])),
        str(table.get("table_name", "")),
    ]
    return " ".join(p for p in parts if p)


def _table_label(table: dict) -> str:
    """表显示名：优先中文别名，否则去掉 schema 前缀的表名"""
    alias = str(table.get("table_alias") or "").strip()
    name = str(table.get("table_name") or "")
    if alias and alias != name and re.search(r"[\u4e00-\u9fff]", alias):
        return alias
    return name.split(".")[-1] or name


def _get_table_fields(table: dict) -> list[dict]:
    """获取表字段：元数据优先（含中文描述），否则动态查真实库（带 db_key+TTL 缓存）"""
    name = str(table.get("table_name") or "")
    if not name:
        return []
    cache_key = (_db_key(), name)
    _now = time.time()
    with _fields_cache_lock:
        hit = _fields_cache.get(cache_key)
        if hit and _now - hit[0] < _FIELDS_TTL:
            return hit[1]
    fields = list(table.get("fields") or _META_FIELDS.get(name) or [])
    if not fields:
        try:
            from db.tools import get_table_detail
            detail = get_table_detail(name)
            fields = (detail or {}).get("fields", []) or []
        except Exception:
            fields = []
    with _fields_cache_lock:
        _fields_cache[cache_key] = (_now, fields)
    return fields


def _friendly_label(field: dict) -> str:
    """字段显示名：优先从描述提取中文业务词（如 描述"工序ID → dim_process" → "工序"），否则用字段名"""
    desc = str(field.get("description") or "").strip()
    m = re.match(r"([\u4e00-\u9fff]{2,6})", desc)
    if m:
        return m.group(1)
    return str(field.get("name") or "")


def _is_id_field(f: dict) -> bool:
    """判断字段是否为 ID/主键/外键（不适合做度量；维度仅在命中业务词时可用）"""
    name = str(f.get("name") or "").lower()
    key = str(f.get("key") or "")
    return key in ("PK", "FK") or name.endswith("id") or name in ("id", "uid", "code")


def _dim_rank(f: dict) -> int:
    """维度字段优先级：业务语义更明确的字段排前面"""
    name = str(f.get("name") or "").lower()
    if "name" in name or "名称" in str(f.get("description") or ""):
        return 0
    if any(w in name for w in ("status", "category", "type", "shift", "reason", "warehouse")):
        return 1
    return 2


def _pick_fields(fields: list[dict], measure_hints: list[str], dim_hints: list[str]) -> tuple[dict | None, dict | None]:
    """从字段里挑度量字段(数值型，排除ID)与维度字段(分类型)。

    命中业务候选词的字段优先；都不命中则取第一个数值字段 / 语义最明确的分类字段。
    返回 (measure, dim)。
    """
    measures_hit: list[dict] = []
    measures_other: list[dict] = []
    dims_hit: list[dict] = []
    dims_other: list[dict] = []
    for f in fields:
        name = str(f.get("name") or "")
        ftype = _norm_type(str(f.get("type") or "").lower().strip())
        desc = str(f.get("description") or "")
        text = f"{name} {desc}".lower()
        is_id = _is_id_field(f)

        if ftype in _NUMERIC_TYPES:
            if is_id:
                continue  # ID/外键不适合做度量
            if any(h.lower() in text for h in measure_hints):
                measures_hit.append(f)
            else:
                measures_other.append(f)
        elif ftype in _CATEGORICAL_TYPES:
            hint_hit = any(h.lower() in text for h in dim_hints)
            if is_id and not hint_hit:
                continue
            (dims_hit if hint_hit else dims_other).append(f)

    measures = measures_hit + measures_other
    dims = dims_hit + sorted(dims_other, key=_dim_rank)
    return (measures[0] if measures else None), (dims[0] if dims else None)


def _render(pattern: str, table_label: str, dim_label: str, measure_label: str) -> str:
    """填充模板。缺维度字段时退化为表级问法，缺度量字段时退化为数据概览"""
    if "{dim}" in pattern and not dim_label:
        return f"统计{table_label}的{measure_label}" if measure_label else f"查看{table_label}的数据概览"
    if "{measure}" in pattern and not measure_label:
        return f"查看{table_label}的数据概览"
    return pattern.format(table=table_label, dim=dim_label, measure=measure_label)


def _metric_questions(tables: list[dict], total_cap: int = 200) -> list[dict]:
    """注册口径推荐问题：以指标注册表为唯一来源，逐条经确定性编译器实证「命中才入选」。

    背景：原来基于业务域模板的引导问题（"质量抽检不合格最多的工序"等）很多并未登记
    口径 → 用户点击后只能走 LLM 兜底，慢且容易失败（GLM 长 schema 生成弱）。
    用户要求「推荐问题都改为口径中有的」；随后又要求口径池要够大（换一批组合丰富）。
    实现：每个生效指标套多组句式（表级统计/分析 × 各维度排行 × 时间趋势）→
    try_compile_metric 实证（零 LLM），能编译出 SQL 的才进推荐池。
    """
    out: list[dict] = []
    seen_q: set[str] = set()
    try:
        from agent.metric_registry import get_effective_metrics
        from agent.metric_compiler import try_compile_metric
        metrics = get_effective_metrics()
    except Exception:
        return out
    if not metrics:
        return out
    tmap: dict = {}
    for t in tables or []:
        tmap[str(t.get("table_name") or "").split(".")[-1].lower()] = t

    def _dims_of(fields: list[dict]) -> tuple[list[str], list[str]]:
        """(文本/枚举维度中文标签 ≤3, 时间维度中文标签 ≤1)，供排行/趋势句式"""
        text_lbls: list[str] = []
        time_lbls: list[str] = []
        for f in fields:
            if _is_id_field(f):
                continue
            tp = _norm_type(str(f.get("type") or "")).lower()
            lbl = _friendly_label(f)
            if not lbl:
                continue
            if tp in _CATEGORICAL_TYPES:
                if lbl not in text_lbls:
                    text_lbls.append(lbl)
            elif any(k in tp for k in ("date", "timestamp")):
                if lbl not in time_lbls:
                    time_lbls.append(lbl)
        return text_lbls[:3], time_lbls[:1]

    for m in metrics:
        if len(out) >= total_cap:
            break
        name = str(m.get("name") or "").strip()
        tables_of = [str(x) for x in (m.get("tables") or [])]
        if not name or not tables_of:
            continue
        bare = tables_of[0].split(".")[-1].lower()
        td = tmap.get(bare)
        label = _table_label(td) if td else bare
        fields = _get_table_fields(td) if td else []
        dims, t_dims = _dims_of(fields)
        # 句式家族（全部实证过滤，编译器认不出的自动丢弃）：
        # 1) 表级：统计/分析 {表} 的 {指标}
        # 2) 维度级：各 {维度} 的 {指标} 排行 / 排名
        # 3) 时间级：按 {时间} 统计 {表} 的 {指标} 趋势
        candidates: list[str] = [f"统计{label}的{name}", f"分析{label}的{name}"]
        for d in dims:
            candidates.append(f"各{d}的{name}排行")
            candidates.append(f"各{d}的{name}排名")
        if t_dims:
            candidates.append(f"按{t_dims[0]}统计{label}的{name}趋势")
        for q in candidates:
            if len(out) >= total_cap:
                break
            if q in seen_q:
                continue
            try:
                c = try_compile_metric(q)
                if not (c and c.get("sql")):
                    continue
            except Exception:
                continue
            seen_q.add(q)
            out.append({"question": q, "category": "metric", "source": "metric",
                        "table": bare, "weight": round(random.uniform(1.3, 2.0), 2)})
    return out


def _weighted_sample(pool: list[dict], limit: int, rng) -> list[dict]:
    """按 weight 加权随机采样 limit 条（重点问题常见但不固定，每次进页面组合不同）。

    池子 ≤ limit 时原样返回；否则逐次按剩余权重随机抽取（不放回）。
    """
    if len(pool) <= limit:
        return pool[:]
    weighted = [[c, max(float(c.get("weight") or 1.0), 0.1)] for c in pool]
    total = sum(w for _, w in weighted)
    picked: list[dict] = []
    for _ in range(limit):
        if not weighted:
            break
        r = rng.uniform(0, total)
        acc = 0.0
        for i, (c, w) in enumerate(weighted):
            acc += w
            if r <= acc:
                picked.append(c)
                total -= w
                weighted.pop(i)
                break
        else:
            picked.append(weighted.pop(0)[0])
    return picked


def _needs_ml(tables: list[dict]) -> bool:
    """存在 2 个以上数值字段的表 → 可生成 ML 建模引导

    按「数值字段」计数（与 docstring 一致）：ML 建模需数值特征，
    原实现用总字段数(≥4)作代理，会把只有文本列的表误判为可建模。
    """
    for t in tables:
        numeric = 0
        for f in _get_table_fields(t):
            if _norm_type(str(f.get("type") or "").lower().strip()) in _NUMERIC_TYPES:
                numeric += 1
                if numeric >= 2:
                    return True
    return False


def _ml_target(tables: list[dict], counts: dict) -> str:
    """从重点表推断 ML 目标字段：优先字段名/描述命中业务词，否则取第一个非ID数值字段"""
    for t in sorted(tables, key=lambda x: (counts.get(x["table_name"]) or 0), reverse=True):
        fields = _get_table_fields(t)
        for f in fields:
            name = str(f.get("name") or "")
            desc = str(f.get("description") or "")
            if _is_id_field(f):
                continue  # ID/主键不适合做预测目标
            if _norm_type(str(f.get("type") or "").lower().strip()) in _NUMERIC_TYPES:
                for w in ("良率", "产量", "库存", "停机", "不良"):
                    if w in name or w in desc:
                        return _friendly_label(f)
                return _friendly_label(f)
    return "产量"


def generate_guide_questions(limit: int = 6, seed: int | None = None) -> dict:
    """根据当前数据库重点生成引导问题（结构化条目，带缓存 + 加权随机轮换）。

    seed：可选，传入固定值可复现（测试用）；前端传随机值使每次刷新组合不同。
    """
    # 缓存键含 limit（P1 修复）：data 已按 limit 截断，同库不同 limit 混用缓存
    # 会导致 limit=10 请求只拿到 6 条（或反之超量）。
    key = f"{_db_key()}#n{int(limit)}"
    now = time.time()
    # 传 seed → 跳过缓存（前端每次随机 seed，需要实时轮换组合；无 seed 才走缓存复用）
    use_cache = seed is None
    if use_cache:
        with _cache_lock:
            if key in _cache and (now - _cache[key]["time"]) < _CACHE_TTL:
                return _cache[key]["data"]

    try:
        from db.tools import get_all_tables
        tables = get_all_tables()
    except Exception:
        tables = []

    # 真实行数（重点表 = 数据多的表）
    try:
        counts = get_table_row_counts()
    except Exception:
        counts = {}

    # 1. 模板命中：收集每类的**所有**候选（不覆盖，保留每张表每种模板的命中）
    category_cands: dict[str, list[dict]] = {}
    for t in tables:
        name = t["table_name"]
        rows = counts.get(name) or t.get("row_count") or 0
        field_count = t.get("field_count", 0) or 0
        weight = 1.0 + min(rows / 5000.0, 3.0) + min(field_count / 10.0, 0.5)  # 行数越多、字段越多权重越高
        kw_text = _table_keywords(t)
        fields = _get_table_fields(t)
        for keywords, category, pattern, measure_hints, dim_hints, require, exclude in QUESTION_TEMPLATES:
            if not any(k in kw_text for k in keywords):
                continue
            if require and not any(k in kw_text for k in require):
                continue  # 业务域限定：关键字文本必须含 require 词（防"设备主数据"命中停机模板等）
            if exclude and any(k in kw_text for k in exclude):
                continue  # 明确排除错配（防 sales 撞工单 order 字样、采购撞库存表）
            measure, dim = _pick_fields(fields, measure_hints, dim_hints)
            question = _render(
                pattern,
                _table_label(t),
                _friendly_label(dim) if dim else "",
                _friendly_label(measure) if measure else "",
            )
            category_cands.setdefault(category, []).append({
                "table": name, "measure": measure, "dim": dim,
                "weight": weight, "question": question,
            })

    # 2. 记忆中的高频成功问题（Vanna 学习到的，用户真实问过的）
    memory_questions: list[dict] = []
    try:
        from agent.memory import get_memory
        memories = get_memory().list_memories(limit=50).get("sql_examples", [])
        seen_q = set()
        for m in memories:
            q = str(m.get("question", "")).strip()
            if q and q not in seen_q:
                seen_q.add(q)
                memory_questions.append({"question": q, "table": m.get("table_name", "")})
            if len(memory_questions) >= 3:
                break
    except Exception:
        memory_questions = []

    # 3. 组装候选池：模板（每类 top-3，按权重）+ 记忆 + ML + 兜底
    pool: list[dict] = []
    for category, cands in category_cands.items():
        cands.sort(key=lambda x: x["weight"], reverse=True)
        for c in cands[:3]:  # 每类保留 top-3（原来只留 1 条 → 池子大幅变宽）
            pool.append({"question": c["question"], "category": category,
                         "source": "template", "table": c["table"], "weight": c["weight"]})
    for m in memory_questions:
        q = str(m.get("question") or "").strip()
        # 2026-09-03：memory 泛问过滤——太宽泛且无口径命中的问句（如"各关键工序的数量"）
        # 放进引导池用户一点就弹窗/澄清，体验差。仅保留：① 命中注册指标/歧义（有解析方向）
        # 或 ② 较长且包含明确业务词的问题。
        try:
            from agent.metric_registry import resolve_metric_intent
            _st = resolve_metric_intent(q, None).get("status", "no_hit")
        except Exception:
            _st = "no_hit"
        if _st == "no_hit" and len(q) < 14:
            continue
        pool.append({"question": q, "category": "memory",
                     "source": "memory", "table": m.get("table", ""), "weight": 1.0})
    if _needs_ml(tables):
        pool.append({"question": f"训练模型预测{_ml_target(tables, counts)}",
                     "category": "ml", "source": "ml", "table": "", "weight": 0.8})
    for q in FALLBACK_QUESTIONS:
        pool.append({"question": q, "category": "explore", "source": "fallback",
                     "table": "", "weight": 0.5})

    # 4. 席位分配：①注册口径问题占大部分（每条已实证命中编译器，点击即秒查）；
    #    ②「分析数据库」常见问题固定 1~2 席（概览类秒出，用户要求混入）；
    #    ③不足再按权重从常规池（模板/记忆/兜底）补齐。最终整体洗牌后返回。
    rng = random.Random(seed)
    picked: list[dict] = []
    try:
        metric_questions = _metric_questions(tables)
    except Exception:
        metric_questions = []
    if metric_questions:
        rng.shuffle(metric_questions)
        picked = metric_questions[:max(1, limit - 2)]      # 口径占大头
    # 数据库分析类（探索）席位
    db_items = [{"question": q, "category": "explore", "source": "db_analysis",
                 "table": "", "weight": 1.2} for q in _DB_ANALYSIS_QUESTIONS]
    if db_items:
        rng.shuffle(db_items)
        picked = picked + db_items[:limit - len(picked)]
    # 常规池兜底（极少情况：口径与 db 都不足 limit）
    if len(picked) < limit:
        skip_q = {str(c["question"]).strip() for c in picked}
        rest_pool = [c for c in pool if str(c.get("question") or "").strip() not in skip_q]
        picked = picked + _weighted_sample(rest_pool, limit - len(picked), rng)
    rng.shuffle(picked)   # 口径/探索混排，避免口径总在前的单调感

    result: list[dict] = []
    seen: set[str] = set()
    for c in picked:
        q = str(c["question"]).strip()
        if not q or q in seen:
            continue
        seen.add(q)
        result.append({
            "id": f"{c['source']}-{len(result)}",
            "question": q,
            "category": c.get("category", ""),
            "source": c.get("source", "template"),
            "table": c.get("table", ""),
            "weight": round(float(c.get("weight") or 0), 2),
        })

    data = {"questions": result[:limit], "db": key}
    if use_cache:
        with _cache_lock:
            _cache[key] = {"time": now, "data": data}
    return data
