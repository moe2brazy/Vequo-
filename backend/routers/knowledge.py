"""
业务知识 API —— 从数据库动态获取表信息，组织为业务知识结构。
将数据库中的真实表名、字段名映射为业务对象、指标等，供前端知识库页面使用。
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text
from database import get_db
from typing import Any, List, Dict, Optional
import time
import json
import io
import threading
import re
from pathlib import Path

# 动态主题引擎 + 字段语义翻译层（文档需求：不局限于固定四主题 / 同名字段不同含义）
from agent.topic_engine import detect_topics
from agent.field_semantics import explain_field, explain_field_detailed
from agent.metric_registry import get_all_metrics

router = APIRouter(prefix="/api/knowledge", tags=["业务知识"])

# ========== 简单 TTL 缓存（避免每次切换页面重复全库扫描） ==========
_CACHE_TTL = 600  # 秒：业务知识结构依赖数据库结构，短时间不变；10 分钟缓存避免频繁全库重建（17s）
_cache: Dict[str, tuple] = {}  # key -> (expire_ts, value)
_cache_lock = threading.Lock()


# stale-while-revalidate：过期后进入宽限期（_REBUILD_GRACE），期间返回旧值并后台异步重建，
# 消除缓存过期/冷启动时首个请求同步阻塞（topics 实测 12s）的痛点。
_REBUILD_GRACE = int(_CACHE_TTL * 0.5)  # 过期后额外 0.5×TTL 宽限，总旧值窗口 = 1.5×TTL
_refreshing: Dict[str, bool] = {}  # key -> 是否正在后台重建（互斥，避免并发重复刷新）


def _schedule_refresh(key: str, producer, args, kwargs):
    """后台线程异步重建缓存；同 key 并发只重建一次，失败保留旧值。

    注意：producer 的首个位置参数通常是请求级 Session（来自 Depends(get_db)），
    请求结束即被 close。后台线程若复用该 Session 会抛错导致重建静默失败。
    因此这里在后台线程内自行新建 Session（用完关闭），避免复用已关闭的请求级 Session。
    """
    with _cache_lock:
        if _refreshing.get(key):
            return
        _refreshing[key] = True

    def _worker():
        db = None
        try:
            from sqlalchemy.orm import Session as _Session
            # 首参若是 SQLAlchemy Session（请求级、即将/已被 close），替换为独立 Session
            if args and isinstance(args[0], _Session):
                from database import SessionLocal
                db = SessionLocal()
                value = producer(db, *args[1:], **kwargs)
            else:
                value = producer(*args, **kwargs)
            with _cache_lock:
                _cache[key] = (time.time() + _CACHE_TTL, value, _heat_version)
        except Exception as e:
            # 重建失败：保留旧值，不写缓存（旧值仍在宽限期内可读）；
            # 打日志便于排查（P2 修复：此前完全静默）
            import logging as _lg
            _lg.getLogger("knowledge").warning("知识库缓存重建失败 %s: %s", key, e)
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
            with _cache_lock:
                _refreshing.pop(key, None)

    threading.Thread(target=_worker, daemon=True).start()


def _cached(key: str, producer, *args, **kwargs):
    """带 TTL 的缓存 + stale-while-revalidate。

    快路径：命中且未过期直接返回。
    过期但仍在宽限期内（且热度匹配）：先返回旧值，后台线程异步重建（用户无感知）。
    冷启动 / 宽限期外 / 热度变化：加锁同步重建，二次检查避免缓存击穿
    （首次 _build_scenes 约 17s；并发 miss 时仅一个请求执行 producer）。
    """
    now = time.time()
    entry = _cache.get(key)
    # 快路径：未过期且热度匹配
    if entry and entry[0] > now and entry[2] == _heat_version:
        return entry[1]
    # 过期但在宽限期内且热度匹配：返回旧值 + 触发后台重建
    if entry and entry[2] == _heat_version and now < entry[0] + _REBUILD_GRACE:
        _schedule_refresh(key, producer, args, kwargs)
        return entry[1]
    # 冷启动 / 宽限期外 / 热度变化：加锁同步重建
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[0] > now and entry[2] == _heat_version:
            return entry[1]
        if entry and entry[2] == _heat_version and now < entry[0] + _REBUILD_GRACE:
            _schedule_refresh(key, producer, args, kwargs)
            return entry[1]
        value = producer(*args, **kwargs)
        _cache[key] = (now + _CACHE_TTL, value, _heat_version)
        return value


def clear_knowledge_cache():
    """清空全部业务知识缓存（数据库切换后调用）"""
    with _cache_lock:
        _cache.clear()
        # 复位在途刷新标记：否则后台刷新线程完成时会把「切库前」旧库数据写回新缓存
        _refreshing.clear()


# ========== 阈值规则注册表（文档示例：缺陷PPM>1000 → 质量高风险） ==========
RULE_REGISTRY: List[dict] = [
    {
        "scene": "quality", "metric": "缺陷PPM",
        "condition": "缺陷PPM > 1000", "label": "质量高风险",
        "formula": "不良数 / 抽样总数 × 1000000（百万分之不良率）",
    },
    {
        "scene": "quality", "metric": "批次合格率",
        "condition": "批次合格率 < 95%", "label": "质量波动",
        "formula": "合格批次 / 检验批次 × 100%",
    },
    {
        "scene": "inventory", "metric": "库存水位",
        "condition": "可用库存 < 安全库存", "label": "库存预警",
        "formula": "available_qty < safety_stock_qty → 触发补货预警",
    },
    {
        "scene": "equipment", "metric": "停机时长",
        "condition": "单次停机 > 120 分钟", "label": "长停机事件",
        "formula": "duration > 120 → 需复盘停机原因",
    },
    {
        "scene": "production", "metric": "工单延期",
        "condition": "计划完工 < 实际完工", "label": "交付延期",
        "formula": "actual_end_time > planned_end_time → 延期工单",
    },
]

# ========== 知识热度（访问计数，高频知识浮上来） ==========
_HEAT_FILE = Path(__file__).resolve().parent.parent / "knowledge_heat.json"
_heat: Dict[str, int] = {}
# None 哨兵区分「未加载」与「已加载但为空 {}」，避免空 dict 时每次命中都重复读盘
_heat_loaded = False
_heat_lock = threading.Lock()
# 热度版本：/hit 时自增，_cached 据此在热度变化后重建 scenes（替代全量清缓存）
_heat_version = 0
# 防无上限增长 + 防高频写盘（P2 修复）：
_HEAT_MAX_KEYS = 2000       # 热度表条目上限，超出时淘汰热度最低的一半
_HEAT_SAVE_INTERVAL = 300   # 写盘节流（秒）：热度变化后最多每 5 分钟落盘一次
_last_heat_save = 0.0


def _load_heat() -> Dict[str, int]:
    global _heat, _heat_loaded
    if not _heat_loaded:
        try:
            if _HEAT_FILE.exists():
                _heat = json.loads(_HEAT_FILE.read_text(encoding="utf-8"))
        except Exception:
            _heat = {}
        finally:
            _heat_loaded = True
    return _heat


def _save_heat() -> None:
    try:
        with _heat_lock:
            _HEAT_FILE.write_text(json.dumps(_heat, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _bump_heat(key: str) -> None:
    global _heat_version, _last_heat_save
    with _heat_lock:
        h = _load_heat()
        h[key] = h.get(key, 0) + 1
        if len(h) > _HEAT_MAX_KEYS:
            # 淘汰热度最低的一半（防 dict 无上限增长）
            for k in sorted(h, key=h.get)[: len(h) // 2]:
                h.pop(k, None)
        _heat_version += 1
    # 写盘节流：高频 /hit 不再每次同步写盘（此前每次请求都落盘造成磁盘 IO 尖峰）
    now = time.time()
    if now - _last_heat_save > _HEAT_SAVE_INTERVAL:
        _last_heat_save = now
        _save_heat()


# ========== 场景配置：表名前缀 → 场景映射 ==========
SCENE_TABLE_PREFIXES = {
    "production": {
        "key": "production",
        "icon": "🏭",
        "name": "生产分析",
        "desc": "产量趋势、工序良率、工单执行",
        "prefixes": ["mes_", "dim_product", "dim_process", "dim_production_line", "work_order", "output"],
    },
    "quality": {
        "key": "quality",
        "icon": "✅",
        "name": "质量分析",
        "desc": "缺陷类型、检验结果、不良分布",
        "prefixes": ["qms_", "defect", "inspection", "quality", "test"],
    },
    "equipment": {
        "key": "equipment",
        "icon": "⚙️",
        "name": "设备分析",
        "desc": "停机时长、设备状态、运行效率",
        "prefixes": ["eqp_", "dim_equipment", "downtime", "machine"],
    },
    "inventory": {
        "key": "inventory",
        "icon": "📦",
        "name": "库存分析",
        "desc": "库存水位、物料周转、安全库存",
        "prefixes": ["inv_", "stock", "inventory", "warehouse"],
    },
}


# ========== 表图标映射（根据表名匹配） ==========
TABLE_ICON_RULES = [
    {"pattern": "dim_product", "icon": "📦", "label": "产品"},
    {"pattern": "dim_process", "icon": "⚡", "label": "工序"},
    {"pattern": "dim_production_line", "icon": "🏗️", "label": "产线"},
    {"pattern": "dim_equipment", "icon": "⚙️", "label": "设备"},
    {"pattern": "mes_work_order", "icon": "📋", "label": "工单"},
    {"pattern": "mes_process_output", "icon": "📊", "label": "工序产出"},
    {"pattern": "qms_inspection", "icon": "🔬", "label": "检验"},
    {"pattern": "qms_defect_detail", "icon": "⚠️", "label": "不良明细"},
    {"pattern": "eqp_downtime_record", "icon": "⏸️", "label": "停机记录"},
    {"pattern": "inv_inventory_snapshot", "icon": "📸", "label": "库存快照"},
]


def _get_icon_and_label(table_name: str) -> tuple:
    """根据表名返回对应图标和中文标签"""
    table_lower = table_name.lower()
    # 2026-09-03：按 pattern 长度降序匹配——'dim_product' 是 'dim_production_line' 的前缀子串，
    # 若不排序会把产线表误标成"产品"（product+ion）。先匹配更具体的表名。
    for rule in sorted(TABLE_ICON_RULES, key=lambda r: -len(r["pattern"])):
        if rule["pattern"] in table_lower:
            return rule["icon"], rule["label"]
    # 未命中规则（如 123 库 factory.* 表）：metadata.py 中文别名兜底，让非专业人员也能看懂
    try:
        from db.metadata import TABLES as _MD_TABLES
        for t in _MD_TABLES:
            tn = t.get("table_name", "")
            if tn.split(".")[-1] == table_name or tn == table_name:
                label = t.get("table_alias") or table_name.replace("_", " ").title()
                if label.endswith("表") and len(label) > 1:
                    label = label[:-1]
                return "📋", label
    except Exception:
        pass
    return "📋", table_name.replace("_", " ").title()


def _generate_table_desc(table_name: str, columns: List[Dict]) -> str:
    """根据表名和字段自动生成描述"""
    desc_parts = []
    for col in columns[:5]:
        if col.get("comment"):
            desc_parts.append(col["comment"])
    if desc_parts:
        return f"数据表 {table_name}，包含 {len(columns)} 个字段：{', '.join(desc_parts[:3])}"
    name_parts = table_name.replace("_", " ").title()
    return f"{name_parts} 数据表，共 {len(columns)} 个字段"


def _get_core_tables(db: Session) -> set:
    """识别核心表（业务主干 = 主数据/维度表）。

    原实现只看「有无外键」，在**没有物理外键约束**的库（本项目的 yans 库实测 0 个 FK，
    业务关联全靠字段同名约定）上恒返回空集 → 前端「核心表」徽标永不出现。

    兜底判据（按优先级）：
      1. 有物理外键 → 直接采用（最可信）
      2. 表名是维度/主数据表前缀（dim_ / mst_ / md_ / sys_ 等）→ 业务主干
      3. 出现在多张表的同名列里（弱外键约定，如 line_id 被多表引用）

    注意**不能**把 mes_/qms_/inv_/eqp_ 等事实表也算进来：实测那样会让
    本库 10/10 张表全是 core=true，徽标等于没有区分度，反而是噪声。
    """
    inspector = inspect(db.get_bind())
    all_tables = [t for t in inspector.get_table_names()
                  if not t.startswith(("_", "pg_"))]

    # 1. 物理外键
    fk_tables = set()
    for table in all_tables:
        try:
            if inspector.get_foreign_keys(table):
                fk_tables.add(table)
        except Exception:
            pass
    if fk_tables:
        return fk_tables

    # 2. 维度/主数据表前缀 —— 这是数据建模的通用约定，比「有没有外键」更贴近
    #    「核心表」的业务含义：主数据被大量事实表引用，本身就是主干。
    dim_prefixes = ("dim_", "mst_", "md_", "sys_", "master_", "dim.")
    core = {t for t in all_tables
            if not t.startswith("metadata_") and t.lower().startswith(dim_prefixes)}
    if core:
        return core

    # 3. 弱外键：同名列出现在 ≥2 张表里
    col_owner: dict[str, set] = {}
    table_cols: dict[str, set] = {}
    for table in all_tables:
        if table.startswith("metadata_"):
            continue
        try:
            cols = {c["name"] for c in inspector.get_columns(table)}
        except Exception:
            continue
        table_cols[table] = cols
        for c in cols:
            if c.endswith("_id") or c in ("line_id", "product_id"):
                col_owner.setdefault(c, set()).add(table)
    shared = {c for c, ts in col_owner.items() if len(ts) >= 2}
    for table, cols in table_cols.items():
        if cols & shared:
            core.add(table)
    return core


def _classify_table(table_name: str) -> Optional[str]:
    """根据表名判断所属场景，返回场景 key 或 None"""
    table_lower = table_name.lower()
    for key, scene in SCENE_TABLE_PREFIXES.items():
        for prefix in scene["prefixes"]:
            if table_lower == prefix or table_lower.startswith(prefix):
                return key
    return None


def _row_count(db: Session, table_name: str) -> int:
    try:
        # 支持 "schema.table" 形式；public 用裸表名（内部引号转义防注入）
        def _q(s: str) -> str:
            return '"' + s.replace('"', '""') + '"'
        if "." not in table_name:
            qref = _q(table_name)
        else:
            sch, tbl = table_name.split(".", 1)
            qref = f"{_q(sch)}.{_q(tbl)}"
        result = db.execute(text(f'SELECT COUNT(*) FROM {qref}'))
        return int(result.scalar() or 0)
    except Exception:
        # 这里既是「行数变 0」的**最终掩盖点**（异常被吞成 0，前端无从分辨
        # 「表真的空」与「查询失败」），也必须 rollback —— 不回滚会让本 Session
        # 后续所有查询继续被拒(25P02)，把一次失败放大成整页行数全 0。
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def _build_scene_object(db: Session, inspector, table_name: str) -> Dict:
    """把一张表组织成一个业务对象（含字段翻译）"""
    columns = inspector.get_columns(table_name)
    icon, label = _get_icon_and_label(table_name)
    pk_constraint = inspector.get_pk_constraint(table_name)
    pk_columns = set(pk_constraint.get("constrained_columns", []))
    core_tables = _get_core_tables(db)
    heat = _load_heat().get(table_name, 0)

    return {
        "table": table_name,
        "label": label,
        "icon": icon,
        "desc": _generate_table_desc(table_name, columns),
        "is_core": table_name in core_tables,
        "row_count": _row_count(db, table_name),
        "heat": heat,
        "fields": [column["name"] for column in columns],
        "columns": [
            {
                "name": column["name"],
                "type": str(column["type"]),
                "nullable": column.get("nullable", True),
                "comment": column.get("comment", "") or "",
                "primary_key": column["name"] in pk_columns,
                # 字段翻译：解决同名字段不同表含义（status 是生产状态还是机械状态）+ 详细解释
                "translation": explain_field_detailed(
                    table_name, column["name"], str(column["type"]), column.get("comment") or ""),
            }
            for column in columns
        ],
    }


def _scene_metrics(scene_table_names: list[str]) -> List[Dict]:
    """指标注册表中与该场景表匹配的指标（主题 → 业务指标 层级）"""
    hits = []
    for m in get_all_metrics():
        m_tables = m.get("tables") or []
        matched = [t for t in m_tables if any(
            s.endswith(t) or t.endswith(s) for s in scene_table_names
        )]
        if matched:
            hits.append({
                "name": m["name"],
                "formula": m.get("formula") or m.get("sql_expression") or "",
                "unit": m.get("unit", ""),
                "description": m.get("description", ""),
                "tables": matched,
            })
    return hits


def _scene_rules(scene_key: str, objects: List[Dict]) -> List[Dict]:
    """主题下的业务规则：阈值规则注册表 + 场景表枚举字段（带注释）+ 场景通用口径规则兜底"""
    rules = []
    for r in RULE_REGISTRY:
        if r["scene"] == scene_key:
            rules.append({
                "name": f"{r['metric']}：{r['label']}",
                "condition": r["condition"],
                "formula": r["formula"],
                "source": "规则注册表",
            })
    seen = set()
    for obj in objects:
        for col in obj.get("columns") or []:
            fname = col["name"].lower()
            comment = col.get("comment") or ""
            if comment and any(k in fname for k in ("status", "type", "result", "flag", "level", "state", "grade", "reason")):
                key = f"{obj['table']}.{col['name']}"
                if key in seen:
                    continue
                seen.add(key)
                rules.append({
                    "name": f"{obj['label']}：{col['name']}",
                    "condition": f"字段取值：{comment}",
                    "formula": f"位于表 {obj['table']}，判断依据为 {col['name']} 字段",
                    "source": "字段枚举",
                })
    # 场景通用口径规则兜底（保证每个场景至少有条目，避免空场景）
    for d in _SCENE_DEFAULT_RULES.get(scene_key, []):
        if not any(r["name"] == d["name"] for r in rules):
            rules.append({**d, "source": "口径约定"})
    return rules


# 场景通用口径规则（按业务域约定，保证每个场景都有规则）
_SCENE_DEFAULT_RULES: Dict[str, List[Dict]] = {
    "production": [
        {"name": "产量统计口径：合格产出", "condition": "产量 = 合格产出数量求和（投入 = 合格 + 不良）", "formula": "SUM(good_qty)"},
        {"name": "良率口径：合格数÷投入数", "condition": "良率 = 合格数量 ÷ 投入数量 × 100%", "formula": "good_qty * 100.0 / input_qty"},
    ],
    "quality": [
        {"name": "质检不合格率口径", "condition": "不合格率 = 不合格数 ÷ 抽样数 × 100%", "formula": "defect_qty * 100.0 / sample_qty"},
        {"name": "缺陷判级", "condition": "按缺陷严重程度分级（致命/严重/轻微）", "formula": "severity 字段判定"},
    ],
    "equipment": [
        {"name": "故障停机 vs 计划保养", "condition": "区分计划内停机与故障停机", "formula": "is_planned 字段判定"},
        {"name": "长停机事件", "condition": "单次停机超过 120 分钟需复盘", "formula": "downtime_minutes > 120"},
    ],
    "inventory": [
        {"name": "缺货预警", "condition": "可用库存 < 安全库存 → 触发补货", "formula": "available_qty < safety_stock_qty"},
        {"name": "快照时点口径", "condition": "跨期对比须按快照日期对齐，同日只取最新快照", "formula": "snapshot_date 对齐"},
    ],
    "sales": [
        {"name": "销售额口径", "condition": "销售额 = 数量 × 单价（注意含税/不含税口径）", "formula": "quantity * unit_price"},
        {"name": "客户贡献度", "condition": "按客户汇总销售额衡量贡献，A 类客户优先保障", "formula": "GROUP BY customer"},
    ],
    "purchase": [
        {"name": "采购金额口径", "condition": "采购金额 = 采购数量 × 单价", "formula": "quantity * unit_price"},
        {"name": "供应商评分", "condition": "按交期、质量、价格综合评估供应商", "formula": "交期偏差/不良率/价格指数"},
    ],
    "finance": [
        {"name": "收入成本口径", "condition": "毛利 = 收入 - 成本", "formula": "revenue - cost"},
        {"name": "费用归集", "condition": "费用按部门/科目归集，跨期对比剔除一次性项", "formula": "GROUP BY department"},
    ],
    "hr": [
        {"name": "编制口径", "condition": "在编人数按部门汇总，不含离职/停薪", "formula": "status = '在职'"},
        {"name": "考勤异常", "condition": "缺勤/迟到次数超阈值需关注", "formula": "异常次数 > 阈值"},
    ],
    "logistics": [
        {"name": "运输时效", "condition": "运输时长 = 签收时间 - 发货时间", "formula": "delivery_time - ship_time"},
        {"name": "异常件", "condition": "破损/延误订单单独标记跟踪", "formula": "异常标记字段"},
    ],
    "project": [
        {"name": "进度偏差", "condition": "实际进度 vs 计划进度对比，偏差超阈值预警", "formula": "actual % - plan %"},
        {"name": "里程碑达成", "condition": "里程碑完成 = 实际完成时间 ≤ 计划时间", "formula": "actual_date <= plan_date"},
    ],
}


def _scene_topics(scene_key: str, objects: List[Dict], metrics: List[Dict]) -> List[Dict]:
    """生成场景级分析主题（按场景业务域 + 实际指标/时间字段，避免全局主题套娃）"""
    has_time = any(
        any(k in c["name"].lower() for k in ("date", "time", "day", "month", "year"))
        for o in objects for c in (o.get("columns") or [])
    )
    metric_names = [m["name"] for m in metrics]
    topics = []
    base = {
        "production": [
            {"id": "prod_trend", "icon": "📈", "name": "产量趋势", "desc": "按日期统计产量变化，识别高峰低谷", "questions": ["最近 7 天产量趋势如何？", "产量最高的十个产品是哪些？", "各产线的投入产出对比", "白班和夜班的产量有差别吗？"]},
            {"id": "prod_yield", "icon": "🏭", "name": "良率与质量表现", "desc": "按工序/产线对比良率，定位质量短板", "questions": ["全厂良率是多少？", "各产线良率排名", "哪个产线良率最低？", "各工序良率从低到高", "关键工序的良率怎么样？"]},
            {"id": "prod_wo", "icon": "📋", "name": "工单执行分析", "desc": "工单达成率、延期情况与在制负荷", "questions": ["工单按期完成率？", "各状态的工单各有多少？", "在建工单有多少？", "还没开工的工单有多少？"]},
            {"id": "prod_cross", "icon": "🔗", "name": "跨表关联分析", "desc": "串起产量、产线、停机、工单多张表，看它们之间是否同源", "questions": ["良率最低的产线，停机时间是不是也最长？", "哪个车间的综合表现最好？", "出过不良的工单，都是什么状态的？"]},
        ],
        "quality": [
            {"id": "qlty_defect", "icon": "⚠️", "name": "缺陷 Pareto 分析", "desc": "按缺陷类型汇总，聚焦 80/20 主要问题", "questions": ["主要缺陷类型有哪些？", "缺陷件数按类型排序", "严重缺陷有多少？"]},
            {"id": "qlty_rate", "icon": "✅", "name": "合格率趋势", "desc": "按日期/批次跟踪合格率变化", "questions": ["质检合格率是多少？", "各工序的质检合格率", "最近合格率趋势？"]},
            {"id": "qlty_inspect", "icon": "🔬", "name": "检验批次统计", "desc": "检验批次、不合格批次与复检情况", "questions": ["累计抽检了多少件？", "一次抽检中合格和不合格各多少？", "各工序的质检合格率"]},
        ],
        "equipment": [
            {"id": "eqp_downtime", "icon": "⏸️", "name": "停机损失分析", "desc": "按设备/原因汇总停机时长，定位最大损失源", "questions": ["停机总时长是多少？", "停机时长最长的是什么原因？", "各产线停机时长对比", "计划停机和故障停机各占多少？"]},
            {"id": "eqp_util", "icon": "⚙️", "name": "设备利用率", "desc": "设备运行/待机/维修时间占比", "questions": ["设备总数是多少？", "设备状态分布", "各类型设备有多少台？", "设备利用率多少？"]},
        ],
        "inventory": [
            {"id": "inv_level", "icon": "📦", "name": "库存水位分析", "desc": "库存总量与各仓库分布", "questions": ["当前总库存多少？", "各仓库库存分布", "哪些产品的总库存最高？"]},
            {"id": "inv_warn", "icon": "🚨", "name": "缺货预警", "desc": "低于安全库存的产品清单", "questions": ["有多少产品的库存低于安全线？", "缺货量最大的是哪些？"]},
        ],
        "sales": [
            {"id": "sales_trend", "icon": "📈", "name": "销售趋势", "desc": "按日期/月度统计销售额变化", "questions": ["最近月度销售趋势？", "哪个时期销售最高？"]},
            {"id": "sales_cust", "icon": "👥", "name": "客户贡献分析", "desc": "按客户汇总销售额，识别大客户", "questions": ["贡献最大的客户？", "客户销售分布？"]},
        ],
        "purchase": [
            {"id": "pu_amount", "icon": "🛒", "name": "采购金额分析", "desc": "按供应商/物料汇总采购金额", "questions": ["采购总额？", "主要供应商采购占比？"]},
            {"id": "pu_supplier", "icon": "🏭", "name": "供应商表现", "desc": "供应商交期与质量表现", "questions": ["哪些供应商交期差？", "供应商来料不良率？"]},
        ],
        "finance": [
            {"id": "fin_pnl", "icon": "💹", "name": "收入成本分析", "desc": "收入、成本与毛利变化", "questions": ["本期毛利多少？", "成本构成如何？"]},
        ],
        "hr": [
            {"id": "hr_headcount", "icon": "👥", "name": "人力编制分析", "desc": "各部门在编人数与变化", "questions": ["各部门人数？", "人力变化趋势？"]},
        ],
        "logistics": [
            {"id": "lg_time", "icon": "🚚", "name": "运输时效分析", "desc": "运输时长与准时率", "questions": ["平均运输时长？", "哪些订单延误？"]},
        ],
        "project": [
            {"id": "pj_progress", "icon": "🗂️", "name": "项目进度分析", "desc": "项目进度与里程碑达成", "questions": ["项目进度如何？", "哪些项目延期？"]},
        ],
    }.get(scene_key, [])
    for t in base:
        topics.append({**t, "kind": "场景主题"})
    # 指标衍生主题：场景里有核心指标 → 生成"指标趋势/排行"
    for m in metric_names[:4]:
        if has_time and not any(t["name"] == f"{m}趋势" for t in topics):
            topics.append({
                "id": f"metric_{m}", "icon": "📊", "name": f"{m}趋势",
                "desc": f"按时间跟踪「{m}」的变化", "questions": [f"最近 {m} 趋势如何？", f"{m} 的高峰/低谷在什么时候？"],
                "kind": "指标主题",
            })
    return topics[:5]


# ========== 获取业务知识场景 ==========

def _username_of(authorization: str, request=None) -> str:
    """从 Authorization 头解析用户名。

    2026-10-05 修复数据串号：原实现 token 无效/过期时**静默回退 "guest"**，
    而这个用户名是所有知识数据写接口的归属键（收藏/术语/人工覆盖/反馈/模板使用），
    于是出现两条实际后果：
      ① 写串号：A 的 token 过期后继续发写请求，数据落进所有匿名访客共用的 guest 桶；
      ② 读串号：A 下次读回的也是 guest 桶 → **看到别人的收藏与人工覆盖**。
    实测无效 token 确实得到 'guest'，且该桶已有内容。

    现在分两种情况：
    - token 有效 → 真实用户名（不变）
    - token 无效/缺失 → **按客户端指纹**分桶（`anon-<hash>`），不再共用 guest。
      这样匿名用户之间不会互相看到数据；已登录用户过期后重新登录即可取回自己的桶。
    注意：这里**不抛 401** —— 知识库的读接口（scenes/terms）对匿名开放是既有行为，
    改成强制登录会影响演示；真正的 fail-close 应该在写接口上单独做。
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token:
        from auth import decode_token
        payload = decode_token(token)
        if payload and payload.get("sub"):
            return str(payload["sub"])
    # 匿名：按客户端分桶，避免所有匿名访客共用 guest
    try:
        import hashlib
        raw = ""
        if request is not None:
            raw = "%s|%s|%s" % (
                request.headers.get("user-agent", ""),
                request.headers.get("accept-language", ""),
                request.client.host if getattr(request, "client", None) else "",
            )
        digest = hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:12]
        return "anon-%s" % digest
    except Exception:
        return "anon-unknown"


def _require_action_export(authorization: str) -> dict:
    """操作级权限检查（export）：无权则 403（fail-close）。

    与 main.py::_require_action 同语义，供 knowledge router 复用；
    未携带/携带无效 token 时不回退 guest，而是直接拒绝（导出必须明确身份）。
    """
    if not (authorization and authorization.lower().startswith("bearer ")):
        raise HTTPException(status_code=401, detail="请先登录后再导出")
    try:
        from auth import get_current_user
        from security.enforcer import build_acl_context
        u = get_current_user(authorization)
        ctx = build_acl_context(u)
        if not ctx.can_do("export"):
            raise HTTPException(
                status_code=403,
                detail="权限不足：当前角色未开通「导出」操作权限，请联系管理员在权限管理页开通")
        return u
    except HTTPException:
        raise
    except Exception as e:  # 解析失败一律 fail-close，绝不因异常放行
        raise HTTPException(status_code=401, detail="身份校验失败，请重新登录") from e


@router.get("/scenes")
def get_knowledge_scenes(request: Request, authorization: str = Header(None), db: Session = Depends(get_db)):
    """从当前数据库动态组织业务场景，包含各场景下的业务对象（表）/ 指标 / 规则

    权限：已登录非管理员只返回「涉及的表全部可见」的场景（与 /topics 规则一致）；
    guest（开放模式匿名）与超级管理员不受限。
    """
    data = _cached("scenes", _build_scenes, db)
    data = _acl_filter_scenes(data, authorization)
    # 叠加当前用户的人工覆盖（编辑/停用），纯内存操作，不触发全库重建
    from knowledge_user_data import apply_scene_overrides
    return apply_scene_overrides(_username_of(authorization, request), data)


@router.post("/hit")
def knowledge_hit(body: dict):
    """知识热度上报：前端查看场景/对象详情时调用，用于「高频知识浮上来」。
    只更新热度（版本自增，_cached 按版本重建），不再全量清缓存——
    原实现每次清空 scenes 缓存会触发约 17s 全库重建，缓存形同虚设。"""
    key = (body or {}).get("key", "")
    if key:
        _bump_heat(key)
    return {"success": True}


def _build_scenes(db: Session) -> Dict:
    inspector = inspect(db.get_bind())
    tables = [
        name for name in inspector.get_table_names()
        if not name.startswith(("_", "pg_", "metadata_"))
    ]

    # 字段名映射（供主题引擎提升语义识别准确率）
    field_map: Dict[str, list[str]] = {}
    for t in tables:
        try:
            field_map[t] = [c["name"] for c in inspector.get_columns(t)]
        except Exception:
            field_map[t] = []

    # 动态主题识别（替代固定四主题：销售库 → 销售/采购分析）
    topics = detect_topics(tables, field_map)

    scenes: Dict[str, Dict] = {}
    for topic in topics:
        key = topic["key"]
        scene_table_names = topic["table_names"]
        objects = []
        for table_name in scene_table_names:
            try:
                objects.append(_build_scene_object(db, inspector, table_name))
            except Exception as e:
                print(f"组织业务对象 {table_name} 失败: {e}")
        # 热度排序：高频知识浮上来
        objects.sort(key=lambda o: o.get("heat", 0), reverse=True)
        metrics = _scene_metrics(scene_table_names)
        scenes[key] = {
            "key": key,
            "icon": topic["icon"],
            "name": topic["name"],
            "desc": topic["desc"],
            "roles": topic.get("roles", []),
            "objects": objects,
            "metrics": metrics,
            "rules": _scene_rules(key, objects),
            "topics": _scene_topics(key, objects, metrics),
        }

    return {"scenes": scenes}


# ========== 场景实时快照（按注册口径确定性执行取当前值，知识页快照卡/指标全览数据源） ==========
_GROUP_LABEL: Dict[str, str] = {
    "mes_work_order": "工单执行",
    "mes_process_output": "产出与质量",
    "qms_inspection": "检验概况",
    "qms_defect_detail": "缺陷明细",
    "eqp_downtime_record": "停机概况",
    "dim_equipment": "设备档案",
    "inv_inventory_snapshot": "库存水位",
}


def _q_table(table_name: str) -> str:
    """安全引用表名（支持 schema.table）"""
    def _q(s: str) -> str:
        return '"' + s.replace('"', '""') + '"'
    if "." not in table_name:
        return _q(table_name)
    sch, tbl = table_name.split(".", 1)
    return f"{_q(sch)}.{_q(tbl)}"


def _snapshot_sql(expr: str, table_name: str) -> str:
    """由注册口径的聚合表达式 + 主表拼出单值查询；危险输入返回 None"""
    if not expr or not table_name:
        return None
    e = expr.strip()
    if re.search(r";|--|/\*|\*/|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER", e, re.IGNORECASE):
        return None
    return "SELECT " + e + " AS v FROM " + _q_table(table_name)


def _resolve_expr(expr: str, cols: set) -> str:
    """解析表达式：{a|b} 占位选首个真实存在的列；否则保留原样（交由执行兜底）"""
    if "{" not in expr:
        return expr
    def _rep(m):
        for cand in m.group(1).split("|"):
            if cand.strip().lower() in cols:
                return cand.strip()
        return m.group(0)
    return re.sub(r"\{([^{}]+)\}", _rep, expr)


def _metric_value(db: Session, expr: str, table_name: str) -> dict:
    """执行单值聚合查询，返回 {ok, value}；任何异常都 ok=False，绝不抛出拖垮整组"""
    sql = _snapshot_sql(expr, table_name)
    if not sql:
        return {"ok": False, "value": None}
    try:
        result = db.execute(text(sql))
        row = result.fetchone()
        v = row[0] if row is not None else None
        if v is None:
            return {"ok": False, "value": None}
        return {"ok": True, "value": v}
    except Exception:
        # pg8000: 一条 SQL 出错后同事务被标记 aborted，后续查询全拒 → 立即回滚恢复会话
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": False, "value": None}


def _snapshot_allowed_tables(user: dict):
    """当前用户可见表的裸名小写集合；None 表示不受限（guest / 超级管理员 / 未配置授权）。"""
    if not user or user.get("role") == "guest":
        return None
    from security.enforcer import build_acl_context
    ctx = build_acl_context(user)
    if ctx.superuser:
        return None
    allowed = ctx.allowed_tables
    return None if allowed is None else {t.split(".")[-1].lower() for t in allowed}


def _snapshot_scope_key(allowed) -> str:
    """缓存作用域标识：不同角色可见表不同，不能共用同一条快照缓存。"""
    if allowed is None:
        return "all"
    import hashlib
    return "t" + hashlib.md5(",".join(sorted(allowed)).encode()).hexdigest()[:10]


def build_scene_snapshot(db: Session, scene_key: str, allowed_tables=None) -> Dict:
    """按场景表集合匹配注册口径，逐条确定性执行聚合取当前值，按主表分组。

    组/顺序与知识页「指标全览」对应；列占位解析 + 单条异常兜底，
    保证任一指标失败不拖垮整组（前后端互通的关键）。

    权限：`allowed_tables` 为该用户可见表裸名集合（None = 不受限）。被挡在授权外的
    事实表，其指标整体不参与计算 —— 原先这里直接对全库表跑聚合，任何角色都能拿到
    自己无权域的指标现值（如质量人员能读到设备停机总时长）。缓存 key 由调用方按
    作用域隔离，避免 A 角色算出的裁剪结果被 B 角色读到。
    """
    try:
        inspector = inspect(db.get_bind())
        tables = [n for n in inspector.get_table_names()
                  if not n.startswith(("_", "pg_", "metadata_"))]
        field_map: Dict[str, set] = {}
        for t in tables:
            try:
                field_map[t] = {c["name"].lower() for c in inspector.get_columns(t)}
            except Exception:
                field_map[t] = set()
        topic = next((b for b in detect_topics(tables) if b["key"] == scene_key), None)
        if topic is None:
            return {"scene": scene_key, "name": scene_key, "groups": [], "updated_at": int(time.time())}
        scene_names = topic["table_names"]

        groups: Dict[str, dict] = {}
        group_order: list = []
        seen: set = set()
        for m in get_all_metrics():
            name = m.get("name") or ""
            if name in seen:
                continue
            m_tables = m.get("tables") or []
            if not m_tables:
                continue
            primary = m_tables[0]
            if primary not in scene_names:
                continue
            if allowed_tables is not None and primary.split(".")[-1].lower() not in allowed_tables:
                continue
            if primary not in _GROUP_LABEL:
                # 维表档案计数（产线数/工序数…）不进指标全览，避免与业务事实组混杂
                continue
            seen.add(name)
            expr = str(m.get("sql_expression") or "").strip()
            if not expr:
                continue
            expr = _resolve_expr(expr, field_map.get(primary, set()))
            if primary not in groups:
                groups[primary] = {"g": _GROUP_LABEL.get(primary, primary), "table": primary, "items": []}
                group_order.append(primary)
            val = _metric_value(db, expr, primary)
            groups[primary]["items"].append({
                "name": name,
                "unit": m.get("unit", ""),
                "formula": m.get("formula") or expr,
                "value": val["value"],
                "ok": val["ok"],
                "expr": expr,
            })
        return {
            "scene": scene_key,
            "name": topic.get("name", scene_key),
            "groups": [groups[k] for k in group_order],
            "updated_at": int(time.time()),
        }
    except Exception as e:
        return {"scene": scene_key, "name": scene_key, "groups": [], "error": str(e),
                "updated_at": int(time.time())}


@router.get("/scene-snapshot/{scene_key}")
def get_scene_snapshot(scene_key: str, authorization: str = Header(None),
                       db: Session = Depends(get_db)):
    """GET /api/knowledge/scene-snapshot/{scene} → 该场景全部注册口径的实时聚合值（按主表分组）

    权限修复（P1）：原实现不接 Authorization，缓存 key 又只有 `snapshot:{场景}`，
    于是 ① 任何访问者（含开放模式匿名）都能拿到全部场景的指标现值；② 谁先访问谁
    把结果写进缓存，后续角色读到的是同一个对象 —— 跨角色串数据的典型形态。
    现按用户可见表裁剪 + 缓存按授权集合隔离。
    """
    key = scene_key.strip().lower()
    if not key or not key.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="无效场景")
    from auth import get_current_user
    allowed = _snapshot_allowed_tables(get_current_user(authorization))
    scope = _snapshot_scope_key(allowed)
    return _cached(f"snapshot:{scope}:{key}", build_scene_snapshot, db, key, allowed)


# ========== 获取术语词典 ==========


# 术语中文名映射（参考《制造业生产质量分析数据介绍》字段中文含义；key = 表名.字段名）
TERM_CN_MAP = {
    "dim_product.product_id": "产品 ID",
        "dim_product.product_code": "产品编码",
        "dim_product.product_name": "产品名称",
        "dim_product.product_model": "产品型号",
        "dim_product.product_category": "产品类别",
        "dim_product.unit": "单位",
        "dim_product.is_active": "是否启用",
        "dim_process.process_id": "工序 ID",
        "dim_process.process_code": "工序编码",
        "dim_process.process_name": "工序名称",
        "dim_process.process_seq": "工序顺序",
        "dim_process.standard_yield_rate": "标准良率",
        "dim_process.is_key_process": "是否关键工序",
        "dim_production_line.line_id": "产线 ID",
        "dim_production_line.line_code": "产线编码",
        "dim_production_line.line_name": "产线名称",
        "dim_production_line.workshop_name": "车间名称",
        "dim_production_line.line_manager": "产线负责人",
        "dim_production_line.line_status": "产线状态",
        "dim_equipment.equipment_id": "设备 ID",
        "dim_equipment.equipment_code": "设备编码",
        "dim_equipment.equipment_name": "设备名称",
        "dim_equipment.equipment_type": "设备类型",
        "dim_equipment.line_id": "所属产线 ID",
        "dim_equipment.install_date": "安装日期",
        "dim_equipment.equipment_status": "设备状态",
        "mes_work_order.work_order_id": "工单 ID",
        "mes_work_order.work_order_no": "工单编号",
        "mes_work_order.product_id": "产品 ID",
        "mes_work_order.line_id": "产线 ID",
        "mes_work_order.plan_qty": "计划数量",
        "mes_work_order.start_date": "计划开始日期",
        "mes_work_order.end_date": "计划结束日期",
        "mes_work_order.order_status": "工单状态",
        "mes_process_output.output_id": "产量记录 ID",
        "mes_process_output.work_order_id": "工单 ID",
        "mes_process_output.product_id": "产品 ID",
        "mes_process_output.process_id": "工序 ID",
        "mes_process_output.line_id": "产线 ID",
        "mes_process_output.stat_date": "统计日期",
        "mes_process_output.input_qty": "投入数量",
        "mes_process_output.good_qty": "合格数量",
        "mes_process_output.defect_qty": "不良数量",
        "mes_process_output.rework_qty": "返工数量",
        "mes_process_output.shift_code": "班次",
        "qms_inspection.inspection_id": "检验记录 ID",
        "qms_inspection.inspection_no": "检验单号",
        "qms_inspection.work_order_id": "工单 ID",
        "qms_inspection.product_id": "产品 ID",
        "qms_inspection.process_id": "工序 ID",
        "qms_inspection.inspection_date": "检验日期",
        "qms_inspection.sample_qty": "抽检数量",
        "qms_inspection.defect_qty": "不良数量",
        "qms_inspection.inspection_result": "检验结果",
        "qms_defect_detail.defect_id": "不良明细 ID",
        "qms_defect_detail.inspection_id": "检验记录 ID",
        "qms_defect_detail.defect_type": "不良类型",
        "qms_defect_detail.defect_code": "不良代码",
        "qms_defect_detail.defect_qty": "不良数量",
        "qms_defect_detail.severity_level": "严重等级",
        "qms_defect_detail.responsible_process_id": "责任工序 ID",
        "eqp_downtime_record.downtime_id": "停机记录 ID",
        "eqp_downtime_record.equipment_id": "设备 ID",
        "eqp_downtime_record.line_id": "产线 ID",
        "eqp_downtime_record.start_time": "停机开始时间",
        "eqp_downtime_record.end_time": "停机结束时间",
        "eqp_downtime_record.downtime_minutes": "停机分钟",
        "eqp_downtime_record.downtime_reason": "停机原因",
        "eqp_downtime_record.is_planned": "是否计划停机",
        "inv_inventory_snapshot.snapshot_id": "库存快照 ID",
        "inv_inventory_snapshot.snapshot_date": "快照日期",
        "inv_inventory_snapshot.product_id": "产品 ID",
        "inv_inventory_snapshot.warehouse_code": "仓库编码",
        "inv_inventory_snapshot.available_qty": "可用库存",
        "inv_inventory_snapshot.frozen_qty": "冻结库存",
        "inv_inventory_snapshot.safety_stock_qty": "安全库存",
}

# 表中文名映射（同上，第 2 节文件清单）
TABLE_CN_MAP = {
    "dim_product": "产品主数据",
        "dim_process": "工序主数据",
        "dim_production_line": "产线主数据",
        "dim_equipment": "设备主数据",
        "mes_work_order": "生产工单",
        "mes_process_output": "工序产量",
        "qms_inspection": "质量检验",
        "qms_defect_detail": "不良明细",
        "eqp_downtime_record": "设备停机记录",
        "inv_inventory_snapshot": "库存快照",
}


def _normalize_term_name(name: str) -> str:
    return name.replace("_", " ").replace("id", "ID").strip()


def _infer_term_category(column_name: str, table_name: str) -> str:
    name = column_name.lower()
    table = table_name.lower()
    if any(k in name for k in ["defect", "fault", "reject", "bad", "inspection", "quality", "yield"]):
        return "质量"
    if any(k in name for k in ["order", "work_order", "production", "process", "output", "yield"]):
        return "生产"
    if any(k in name for k in ["downtime", "equipment", "machine", "maintenance", "uptime"]):
        return "设备"
    if any(k in name for k in ["stock", "inventory", "warehouse", "safety_stock", "material"]):
        return "库存"
    if table.startswith("qms_") or table.startswith("defect") or table.startswith("inspection"):
        return "质量"
    if table.startswith("mes_") or table.startswith("work_order") or table.startswith("output"):
        return "生产"
    if table.startswith("eqp_") or table.startswith("machine"):
        return "设备"
    if table.startswith("inv_") or table.startswith("stock") or table.startswith("inventory"):
        return "库存"
    return "通用"


def _term_english_alias(name: str) -> str:
    common = {
        "工序": "Process",
        "良率": "Yield",
        "缺陷": "Defect",
        "停机时长": "Downtime",
        "安全库存": "Safety Stock",
        "产品": "Product",
        "设备": "Equipment",
        "库存": "Inventory",
        "工单": "Work Order",
        "检验": "Inspection",
        "不良": "Defect",
    }
    if name in common:
        return common[name]
    return " ".join(w.capitalize() for w in name.replace("_", " ").split())


def _abbreviation(name: str) -> str:
    if name.lower().endswith("_id"):
        return name[:-3].upper() + " ID"
    parts = name.replace("_", " ").split()
    if len(parts) > 1:
        return ''.join(p[0].upper() for p in parts if p)
    return name.upper()


def _classify_knowledge_type(column_name: str, column_type: str, table_name: str) -> str:
    """将术语归类为：业务对象 / 业务指标 / 业务规则 / 分析主题"""
    name = column_name.lower()
    col_type = str(column_type).lower()

    # 业务指标：数值型字段，表示可度量、可统计的量
    metric_keywords = [
        "rate", "count", "amount", "qty", "quantity", "duration",
        "price", "cost", "value", "weight", "percent", "ratio", "score",
        "yield", "output", "total", "sum", "avg", "max", "min",
        "temperature", "speed", "pressure", "volume", "length",
    ]
    is_numeric = any(t in col_type for t in ["int", "float", "numeric", "decimal", "double", "real"])
    if is_numeric and any(k in name for k in metric_keywords):
        return "业务指标"

    # 业务规则：枚举/状态/标志字段，表示约束和判断逻辑
    rule_keywords = [
        "status", "type", "level", "flag", "state", "result", "grade",
        "category", "class", "stage", "phase", "mode", "reason",
        "is_", "has_", "check", "pass", "fail", "qualified",
    ]
    if any(k in name for k in rule_keywords):
        return "业务规则"

    # 业务对象：外键/ID字段，表示实体引用
    if name.endswith("_id") or name.endswith("_key"):
        return "业务对象"

    # 业务对象：维度表的主键或名称字段
    if table_name.startswith("dim_"):
        return "业务对象"

    # 默认归为业务指标或业务对象
    if is_numeric:
        return "业务指标"
    return "业务对象"


# ========== 专家级术语词典（行业专家口径，命中优先于模板生成） ==========
EXPERT_TERMS: Dict[str, str] = {
    "yield_rate": "良率（Yield Rate）：合格产出数量占投入总量的百分比，衡量制程质量稳定性的核心 KPI。口径：合格数 ÷ 投入数 × 100%。用于产线/工序/班次维度横向对比与趋势监控；良率异常波动通常提示工艺参数漂移、设备劣化或来料质量问题，是质量工程师定位制程缺陷的首要分析指标。",
    "good_qty": "合格数量（Good Qty）：该工序/产线实际产出中检验合格的件数，是产量统计的口径基准。通常与 input_qty 对比计算良率，与 defect_qty 相加等于总产出。生产日报、产能利用率、交付达成率均以此字段为核心。",
    "input_qty": "投入数量（Input Qty）：进入本工序/产线加工的原料或半成品数量，是制程流转的起始量。用于计算良率（分母）、产能负荷与在制品（WIP）水位；投入产出不平衡时优先核查首序上料记录与上工序产出。",
    "defect_qty": "不良数量（Defect Qty）：检验判定为不合格的件数，与缺陷明细表（defect_detail）按批次关联。用于计算不良率、缺陷 PPM 及质量损失金额；是质量成本分析与改善专案（如 SPC、8D）的核心输入。",
    "defect_type": "缺陷类型（Defect Type）：对不合格品按失效模式分类的枚举值（如划伤、错件、虚焊、尺寸超差）。是缺陷 Pareto 分析（80/20）的分组维度，配合缺陷代码可定位责任工序与根因。",
    "order_status": "工单状态（Order Status）：生产工单生命周期状态（未开始/进行中/已完工/已取消）。用于工单执行进度监控、按期交付率统计与在制产能核算；异常状态（长期挂起/逾期未完工）需重点稽核。",
    "work_order_id": "工单编号（Work Order ID）：生产任务唯一标识，串联工单、工序产出、检验记录与设备加工记录的关联主键。所有生产追踪（Lot Traceability）均以此字段为锚点。",
    "downtime_duration": "停机时长（Downtime Duration，分钟）：设备因故障、保养、换型等原因停止生产的时间。OEE（设备综合效率）计算的直接输入；长停机（如超 120 分钟）需复盘停机原因并纳入设备可靠性改善计划。",
    "duration_minutes": "停机时长（Duration Minutes）：单次停机事件持续分钟数。用于按设备/原因/班次汇总停机损失，计算 MTBF/MTTR 等可靠性指标，是设备维保策略优化（预防性 vs 事后维修）的数据依据。",
    "equipment_status": "设备状态（Equipment Status）：设备当前运行状态（运行/待机/维修/停机）。用于设备利用率、可用率统计与产能排程；状态与工单、停机记录交叉核对可发现主数据不一致问题。",
    "available_qty": "可用库存量（Available Qty）：当前时点可直接发货/投入使用的库存数量（已扣除预留与在途）。低于安全库存触发补货预警；是库存周转率、缺货风险与服务水平（Fill Rate）计算的基础。",
    "safety_stock_qty": "安全库存量（Safety Stock Qty）：为吸收需求波动与供应不确定而设定的最低库存水位。实际库存低于该值时应触发补货；其设定依赖需求预测误差与补货提前期，过高积压资金、过低导致断供。",
    "sample_qty": "抽样数量（Sample Qty）：质检批次中实际抽取检验的样本数。与不合格数（fail_qty）配合计算抽检不合格率；抽样方案（如 GB/T 2828 抽样水准）决定样本量与质量判定结论的可信度。",
    "fail_qty": "不合格数量（Fail Qty）：抽样检验中判定不合格的件数。用于计算抽检不合格率（fail_qty ÷ sample_qty）与批次接收判定；不合格率超限批次需触发全检或让步接收评审。",
    "inspection_result": "检验结论（Inspection Result）：批次/样本检验判定结果（合格/不合格/待判）。按结论汇总可得到批合格率与质量趋势；不合格批次的处置（退货/返工/让步）记录是质量追溯链的关键环节。",
    "process_id": "工序编号（Process ID）：工艺路线中加工环节的唯一标识，关联工序主数据（名称、标准工时、标准良率）。生产产出、质量检验均按工序归集，是制程分析的基本维度。",
    "process_name": "工序名称（Process Name）：加工环节的业务名称（如 SMT 贴片、回流焊、AOI 检测）。与工艺路线、设备能力矩阵配合用于排产与瓶颈识别。",
    "product_code": "产品编码（Product Code）：物料/成品唯一编码（如 SKU），跨 BOM、工单、库存、检验多表关联的核心维度。产品维度分析（畅销/呆滞/质量表现）均以此为分组键。",
    "product_name": "产品名称（Product Name）：产品的中文业务名称，用于识别与展示。分析时建议以 product_code 为关联键（名称可能重复或变更），名称仅作可读性展示。",
    "warehouse_code": "仓库编码（Warehouse Code）：仓库/库位唯一标识。库存分布、周转、库龄分析均按仓库维度拆分；多仓库场景下需关注调拨在途与账实一致。",
    "batch_no": "批次号（Batch No）：同一生产条件下产出的物料批次标识，是质量追溯（正向追踪/反向溯源）的最小单元。配合生产日期可分析批次间质量波动与工艺变更影响。",
    "serial_no": "序列号（Serial No）：单件唯一标识（一物一码），用于精密件/安全件的全生命周期追踪（生产、流通、售后）。序列级追溯可实现精准召回与防窜货。",
    "planned_end_time": "计划完工时间（Planned End Time）：工单/任务计划完成截止时刻。与实际完工时间对比得到按期交付率与延期时长；排产合理性评估的重要依据。",
    "actual_end_time": "实际完工时间（Actual End Time）：工单/任务实际完成时刻。与计划对比形成交付偏差（延期/提前），用于交期承诺能力评估与产能瓶颈定位。",
    "plan_qty": "计划数量（Plan Qty）：工单/计划下达的目标数量。与产出对比得到计划达成率；是产能规划、物料需求计划（MRP）与绩效考核的基础。",
    "actual_qty": "实际数量（Actual Qty）：实际完成/产出数量。与计划数量对比计算达成率，偏差分析定位排产或执行问题。",
    "oee": "设备综合效率（OEE）：可用率 × 性能 × 良率的综合指标，衡量设备有效产出能力。行业标杆一般 85%（世界级）；OEE 分解可定位损失来源（停机、速度、质量损失）。",
    "lead_time": "交付周期（Lead Time）：从下单到交付的总时长，含生产、检验、物流环节。客户服务水平的核心指标；缩短交付周期需识别各环节耗时占比与瓶颈。",
    "qualified": "合格（Qualified）：检验判定结果为合格的状态标记。按合格/不合格汇总得到批合格率；是质量报表与供应商评分的基础。",
    "reject": "不合格/拒收（Reject）：判定为不合格或拒收的标记。汇总后进入缺陷分析、退货处理与质量损失核算流程。",
    "inspection_count": "检验次数（Inspection Count）：统计周期内执行的检验批次数。用于检验工作量与质量监控强度分析；检验频次异常变化提示质量控制策略调整。",
    "failure_count": "故障次数（Failure Count）：统计周期内设备/产品发生故障的次数。配合停机时长计算 MTBF（平均无故障时间），是设备可靠性核心指标。",
}

# ── 扩展专家词典：通用企业字段 + 当前库重点字段（双段式：先通俗、再专业） ──
EXTRA_EXPERT_TERMS: Dict[str, str] = {
    "order_id": "订单/工单编号——每张订单或工单的唯一流水号，相当于它的身份证。专业口径：贯穿订单、生产、发货、结算全流程的关联主键，所有单据明细都靠它串起来，是查询与追溯的第一检索字段。",
    "customer_name": "客户名称——买东西的一方叫什么。专业口径：客户维度的分组键，用于按客户统计销售额、回款与贡献度（如 A 类客户占比），也是客诉与售后归集的依据。",
    "quantity": "数量——这批业务涉及多少件。专业口径：可度量的计数指标，与单价相乘得到金额，可做数量趋势、占比与预警分析；注意区分净重/毛重、实收/应发等口径差异。",
    "unit_price": "单价——每件多少钱。专业口径：金额类指标，与数量相乘得金额；分析时需注意含税/不含税、含运/不含运的口径，跨期对比要剔除价格变动（用数量加权）。",
    "order_date": "下单日期——订单生成的日期。专业口径：时间维度字段，按日/月/年聚合可做销售趋势、季节性分析；与发货日期对比可算订单处理周期。",
    "material_id": "物料编号——原材料/半成品/成品的唯一编码。专业口径：BOM、库存、采购、生产各环节关联的核心维度键，物料维度分析（采购频次、库存周转、呆滞）均以此分组。",
    "material_name": "物料名称——物料的中文业务名称。专业口径：用于识别展示；分析时建议以物料编号为关联键（名称可能重复），名称仅作可读性辅助。",
    "supplier": "供应商——这批货从哪家买的。专业口径：采购分析的分组维度，用于供应商评分（交期、质量、价格）、采购集中度与替代供应商风险评估。",
    "stock_qty": "库存数量——当前仓里实际有多少。专业口径：库存水位指标，与安全库存对比判断缺货/积压风险；快照类字段需结合快照日期理解时点性，勿跨期直接相加。",
    "safety_qty": "安全库存——为保证供应留的底线数量。专业口径：低于它就触发补货预警；其值应随需求波动与交期变化定期重算，过高占用资金、过低导致断供。",
    "unit_cost": "单位成本——每件花了多少钱。专业口径：成本类指标，与数量相乘得总成本；用于毛利测算（售价-成本）、成本构成分析与降本目标设定。",
    "category": "类别——把业务对象分成几大类。专业口径：分类维度字段，是 Pareto 分析、占比统计的标准分组键；类别体系建议保持稳定，避免分析口径漂移。",
    "is_active": "是否启用——这条数据还生效吗。专业口径：逻辑标志位（1=启用，0=停用）；分析时默认过滤停用数据，避免把废弃主数据计入统计。",
    "line_id": "产线编号——这条生产线的唯一标识。专业口径：生产分析的核心维度，产量、良率、停机均按产线归集，用于产线间横向对比与瓶颈定位。",
    "start_date": "开始日期——事情从哪天开始。专业口径：时间维度起点，与结束日期配合计算持续时间/周期；跨周期分析时注意日期边界（含/不含当天）。",
    "end_date": "结束日期——事情到哪天结束。专业口径：与开始日期配合计算工期与按期达成率；对未完成记录该字段为空，统计时需单独处理。",
    "line_name": "产线名称——生产线的业务名称。专业口径：产线维度可读标签，分析建议用产线编号关联，名称用于展示；注意产线合并/改名会污染历史口径。",
    "workshop": "车间——生产现场按区域划分的单位。专业口径：组织维度字段，用于车间级产能、质量、成本的归集对比；可配合产线做两级钻取分析。",
    "supervisor": "负责人——这块业务谁负责。专业口径：责任归属字段，用于责任区域绩效统计与异常问责；人员变动时需同步更新主数据，否则历史统计失真。",
    "active_orders": "在制订单数——当前正在进行中的订单数量。专业口径：产能负荷指标，反映产线占用情况；过高说明排产饱和，需评估交期风险。",
    "output_id": "产出记录编号——每次产出的唯一流水号。专业口径：工序产出表的记录主键，串联批次、工序、数量明细，用于产出追溯与产量核对。",
    "shift_code": "班次代码——哪个班次干的活。专业口径：班组维度的分组键（如 A/B/C 班），用于班次间产量、质量对比与交接班分析。",
    "process_seq": "工序顺序——这道工序排第几步。专业口径：工艺路线中的先后次序，用于识别关键路径、瓶颈工序与流转逻辑；排序异常会导致工艺分析失真。",
    "is_critical": "是否关键工序——这道工序重要吗。专业口径：关键工序标记（如特殊过程、瓶颈工序），通常需要重点质量管控与设备保障，是 SPC 监控的优先对象。",
    "std_yield_rate": "标准良率——这道工序正常该达到的合格比例。专业口径：工艺基准值，实际良率低于标准即视为异常；用于制程能力评估与改善目标设定。",
    "result": "检验结论——这批检出来合格还是不合格。专业口径：质检判定枚举（合格/不合格/待判），按结论汇总得批合格率；是质量报表与批次处置的核心依据。",
    "severity": "严重程度——这个问题有多严重。专业口径：缺陷分级（如轻微/一般/严重/致命），用于风险排序与资源优先分配；严重缺陷需立即停线处理。",
    "disposal": "处置方式——不合格品怎么处理。专业口径：缺陷处置枚举（返工/报废/让步接收/退回），汇总可分析质量损失结构与改善效果。",
    "equipment_name": "设备名称——这台设备的业务名称。专业口径：设备维度可读标签；分析建议用设备编号关联，名称用于展示与巡检记录核对。",
    "equipment_type": "设备类型——这台设备属于哪类。专业口径：设备分类维度（如 SMT 贴片机、注塑机、检测设备），用于分类产能、维护成本与可靠性分析。",
    "model": "型号——设备的型号规格。专业口径：设备技术属性，用于同型号设备的备件通用性、故障率对比与采购选型参考。",
    "purchase_date": "购置日期——设备哪天买进来的。专业口径：设备生命周期起点，用于折旧计算、设备年龄与老化风险分析（老旧设备故障率上升）。",
    "downtime_id": "停机记录编号——每次停机的唯一流水号。专业口径：停机事件主键，关联设备、开始/结束时间、原因，是设备可靠性分析的记录单元。",
    "downtime_minutes": "停机时长（分钟）——这次停了多久。专业口径：停机损失度量，按设备/原因/班次汇总可算停机损失金额、OEE 损失与改善优先级。",
    "is_planned": "是否计划内——这次停机是计划好的吗。专业口径：区分计划停机（保养、换型）与故障停机；计算 OEE 时通常仅故障停机计入可用率损失。",
    "reason": "停机/故障原因——为什么停。专业口径：根因分类字段，配合停机时长做 Pareto 分析，定位最主要停机原因以制定针对性改善措施。",
    "frozen_qty": "冻结数量——被锁定不能动的数量。专业口径：库存可用量的扣减项（如质检冻结、销售预留），可用量=总量-冻结量-在途；冻结异常需核查业务单据。",
    "snapshot_date": "快照日期——这份库存数据是哪天拍的。专业口径：快照类数据的时点标识，跨期对比必须按快照日期对齐；同一日期只取最新快照避免重复统计。",
    "inspection_id": "检验记录编号——每次检验的唯一流水号。专业口径：质检记录主键，关联批次、样品数、不合格数，是质量追溯与批次判定的记录单元。",
    "inspection_date": "检验日期——哪天检验的。专业口径：质检时间维度，按日/周聚合可看检验量与合格率趋势；与生产日期对比可计算检验滞后时间。",
    "capacity": "产能/容量——能生产或容纳多少。专业口径：规模能力指标（如工厂产能、仓库容量），用于产能利用率与负荷分析；注意区分设计产能与实际产能。",
    "established": "成立日期——这个主体哪天成立的。专业口径：组织主数据的成立时间，用于组织年龄、存续年限与生命周期分析。",
    "factory_name": "工厂名称——这家工厂叫什么。专业口径：多工厂场景的组织维度，用于跨工厂对比与集团汇总；分析建议用工厂编号关联。",
    "department": "部门——属于哪个部门。专业口径：组织维度字段，用于部门绩效、人力与成本归集；组织调整（合并/拆分）会改变历史口径。",
    "spec": "规格（Spec）——产品型号、尺寸等参数描述，用于区分同编码下的不同规格。专业口径：自由文本字段，规范化命名后可按规格做产品线分析。",
}

# 合并词典（精确字段名优先查主词典，其次扩展词典）
EXPERT_TERMS_ALL: Dict[str, str] = {**EXPERT_TERMS, **EXTRA_EXPERT_TERMS}


def _generate_rich_definition(column_name: str, column_type: str, knowledge_type: str, table_name: str, comment: str) -> str:
    """根据术语类型生成专业且通俗的解释（用字段翻译层的中文含义开头）"""
    if comment:
        return comment

    # 中文业务含义（字段翻译层：表级语义 > 通用语义 > 直译）
    cn = explain_field(table_name, column_name, comment)
    type_str = str(column_type)

    if knowledge_type == "业务指标":
        usage = "用于度量业务规模或水平，是数据分析的核心 KPI 字段，可做求和、均值、趋势与占比分析。"
    elif knowledge_type == "业务规则":
        usage = "用于标识业务对象的分类或状态，是数据筛选、分组统计与异常判定的依据。"
    elif knowledge_type == "业务对象":
        usage = "用于标识或关联业务实体，是数据建模的维度字段，也常作为表间关联的键。"
    else:
        usage = "用于描述或归类业务信息，支持日常查询与展示。"
    return f"「{cn}」({column_name})：{type_str} 类型，来自表 {table_name}。{usage}"


@router.get("/terms")
def get_knowledge_terms(request: Request, authorization: str = Header(None), db: Session = Depends(get_db)):
    """从数据库字段注释中提取术语词典；无注释时返回常见业务术语。

    权限：已登录非管理员只返回可见表相关的术语（按 mapped_table 过滤）；
    guest（开放模式匿名）与超级管理员不受限。
    """
    data = _cached("terms", _build_terms, db)
    data = _acl_filter_terms(data, authorization)
    # 叠加人工覆盖 + 自定义术语
    from knowledge_user_data import apply_term_overrides
    return apply_term_overrides(_username_of(authorization, request), data)


def _acl_filter_terms(data: dict, authorization: str) -> dict:
    """按当前用户生效 ACL 过滤术语词典：只保留映射到可见表的术语（无映射的通用术语保留）"""
    from routers.tables import _acl_visible, _bare
    full, allowed = _acl_visible(authorization)
    if full:
        return data
    terms = [t for t in (data.get("terms") or [])
             if not t.get("mapped_table") or _bare(t.get("mapped_table")) in allowed]
    return {"terms": terms}


def _acl_filter_scenes(data: dict, authorization: str) -> dict:
    """按当前用户生效 ACL 过滤业务场景：只保留「涉及的表全部可见」的场景，
    与 /api/tables/topics 的过滤规则一致，防止无权表通过场景卡片/详情泄露。"""
    from routers.tables import _acl_visible, _bare
    full, allowed = _acl_visible(authorization)
    if full:
        return data
    scenes = {}
    for key, s in (data.get("scenes") or {}).items():
        objs = s.get("objects") or []
        # 场景涉及的表集合（object.table 可能是 schema.table 或裸名）
        involved = {_bare(str(o.get("table") or "")) for o in objs}
        if not involved:
            continue  # 无明确对象归属的场景不展示（无法判定可见性）
        if not involved <= allowed:
            continue  # 涉及任何无权表 → 整场景隐藏
        scenes[key] = s
    return {"scenes": scenes}


def _acl_filter_relations(data: dict, authorization: str) -> dict:
    """按当前用户生效 ACL 过滤表间关系：只保留两端表都可见的关系"""
    from routers.tables import _acl_visible, _bare
    full, allowed = _acl_visible(authorization)
    if full:
        return data
    rels = [r for r in (data.get("relations") or [])
            if _bare(str(r.get("source_table") or "")) in allowed
            and _bare(str(r.get("target_table") or "")) in allowed]
    return {"relations": rels}


def _acl_filter_graph(data: dict, authorization: str) -> dict:
    """按当前用户生效 ACL 过滤知识图谱：节点只保留可见表，边只保留两端都可见的关系"""
    from routers.tables import _acl_visible, _bare
    full, allowed = _acl_visible(authorization)
    if full:
        return data
    nodes = [n for n in (data.get("nodes") or [])
             if _bare(str(n.get("id") or "")) in allowed]
    node_ids = {n["id"] for n in nodes}
    edges = [e for e in (data.get("edges") or [])
             if e.get("source") in node_ids and e.get("target") in node_ids]
    return {"nodes": nodes, "edges": edges}


# 需从术语词典排除的字段（按英文字段名，用户要求移除，如基础主键）
EXCLUDED_TERMS = {"product_id"}


def _build_terms(db: Session) -> Dict:
    inspector = inspect(db.get_bind())
    terms = []
    seen = set()
    tables = [
        name for name in inspector.get_table_names()
        if not name.startswith(("_", "pg_", "metadata_"))
    ]

    for table_name in tables:
        try:
            columns = inspector.get_columns(table_name)
        except Exception:
            continue
        for column in columns:
            comment = (column.get("comment") or "").strip()
            term_name = column["name"]
            if term_name in EXCLUDED_TERMS:
                continue
            col_type = str(column["type"])
            ktype = _classify_knowledge_type(term_name, col_type, table_name)
            # 释义优先级：专家级术语词典 > 数据库注释 > 模板生成
            expert = EXPERT_TERMS_ALL.get(term_name.lower())
            if expert:
                definition = expert
            elif comment:
                definition = comment
            else:
                definition = _generate_rich_definition(term_name, col_type, ktype, table_name, comment)
            key = f"{term_name}：{definition[:30]}"
            if key in seen:
                continue
            seen.add(key)
            terms.append({
                "term": term_name,
                "term_cn": TERM_CN_MAP.get(f"{table_name}.{term_name}", term_name),
                "table_cn": TABLE_CN_MAP.get(table_name, table_name),
                "definition": definition,
                "category": _infer_term_category(term_name, table_name),
                "knowledge_type": ktype,
                "data_type": col_type,
                "en": _term_english_alias(term_name),
                "abbreviation": _abbreviation(term_name),
                "mapped_table": table_name,
                "mapped_field": term_name,
            })

    if not terms:
        terms = [
            {"term": "工序", "term_cn": "工序", "table_cn": "工序主数据", "definition": "产品生产过程中经过的加工环节，是生产管理的核心业务对象，每个工序有独立的编号、名称和工艺参数。", "category": "生产", "knowledge_type": "业务对象", "data_type": "VARCHAR", "en": "Process", "abbreviation": "GX", "mapped_table": "dim_process", "mapped_field": "process_id"},
            {"term": "良率", "term_cn": "良率", "table_cn": "工序产量", "definition": "合格产出数量占总产出数量的百分比，是制造业最核心的质量指标。良率越高说明生产过程越稳定，质量控制越有效。", "category": "质量", "knowledge_type": "业务指标", "data_type": "DECIMAL", "en": "Yield Rate", "abbreviation": "LY", "mapped_table": "mes_process_output", "mapped_field": "yield_rate"},
            {"term": "缺陷", "term_cn": "缺陷", "table_cn": "不良明细", "definition": "产品质量不符合要求的异常项，用于定义和分类生产过程中的不合格现象，是质量分析的基础规则维度。", "category": "质量", "knowledge_type": "业务规则", "data_type": "VARCHAR", "en": "Defect", "abbreviation": "QX", "mapped_table": "qms_defect_detail", "mapped_field": "defect_type"},
            {"term": "停机时长", "term_cn": "停机时长", "table_cn": "设备停机记录", "definition": "设备因故障、保养或换模等原因停止运行的时间长度（分钟），是设备效率分析的关键指标，直接影响产能计算。", "category": "设备", "knowledge_type": "业务指标", "data_type": "INTEGER", "en": "Downtime", "abbreviation": "TJSC", "mapped_table": "eqp_downtime_record", "mapped_field": "duration"},
            {"term": "安全库存", "term_cn": "安全库存", "table_cn": "库存快照", "definition": "为应对需求波动和供应不确定性而设定的最低库存水位，低于该水位将触发补货预警，是库存管理的核心规则。", "category": "库存", "knowledge_type": "业务规则", "data_type": "INTEGER", "en": "Safety Stock", "abbreviation": "AQKC", "mapped_table": "inv_inventory_snapshot", "mapped_field": "safety_stock"},
        ]

    return {"terms": terms}


# ========== 获取知识统计 ==========

@router.get("/stats")
def get_knowledge_stats(authorization: str = Header(None), db: Session = Depends(get_db)):
    """统计业务知识库规模：场景数、表数、字段数、术语数

    权限：已登录非管理员按可见表计算规模（与 /api/tables/overview 一致），
    不泄露无权表的存在与规模。
    """
    from routers.tables import _acl_visible, _bare
    inspector = inspect(db.get_bind())
    tables = [
        name for name in inspector.get_table_names()
        if not name.startswith(("_", "pg_", "metadata_"))
    ]
    full, allowed = _acl_visible(authorization)
    if not full:
        tables = [t for t in tables if _bare(t) in allowed]
    total_columns = 0
    for table_name in tables:
        try:
            total_columns += len(inspector.get_columns(table_name))
        except Exception:
            pass

    scene_keys = set()
    for table_name in tables:
        key = _classify_table(table_name)
        if key:
            scene_keys.add(key)

    terms = get_knowledge_terms(authorization, db)["terms"]

    return {
        "scene_count": len(scene_keys),
        "table_count": len(tables),
        "column_count": total_columns,
        "term_count": len(terms),
    }


# ========== 获取表间关系（外键） ==========


@router.get("/relations")
def get_knowledge_relations(authorization: str = Header(None), db: Session = Depends(get_db)):
    """扫描数据库外键，返回表与表之间的关系信息，供图谱展示使用

    权限：已登录非管理员只返回两端表都可见的关系（不泄露无权表的存在与关联）。
    """
    data = _cached("relations", _build_relations, db)
    return _acl_filter_relations(data, authorization)


def _build_relations(db: Session) -> Dict:
    inspector = inspect(db.get_bind())
    relations = []
    tables = [
        name for name in inspector.get_table_names()
        if not name.startswith(("_", "pg_", "metadata_"))
    ]

    for table_name in tables:
        try:
            fks = inspector.get_foreign_keys(table_name)
        except Exception:
            fks = []

        for fk in fks:
            constrained = fk.get("constrained_columns") or []
            referred = fk.get("referred_columns") or []
            # 修复：referred_table 为空时不要用 referred_schema 顶替（schema 名不是表名）
            referred_table = fk.get("referred_table") or ""
            if not referred_table:
                continue
            for i, src_col in enumerate(constrained):
                tgt_col = referred[i] if i < len(referred) else (referred[0] if referred else None)
                relations.append({
                    "source_table": table_name,
                    "source_column": src_col,
                    "target_table": referred_table,
                    "target_column": tgt_col,
                    "type": "foreign_key",
                    "description": fk.get("name") or f"{table_name}.{src_col} -> {referred_table}.{tgt_col}",
                })

    return {"relations": relations}


# ========== 知识图谱专用接口（含知识类型分类） ==========

def _classify_table_type(table_name: str) -> str:
    """根据表名前缀和数据特征判断表的整体知识类型

    兼容两套命名：旧库 dim_/mes_/qms_/eqp_/inv_ 前缀 + 123 库 factory.* 裸表名。
    """
    name = (table_name or "").split(".")[-1].lower()
    if name.startswith("dim_") or name.endswith(("_info", "department", "workshop", "line", "employee", "product", "material", "supplier", "customer", "factory")):
        return "业务对象"
    # 指标类：生产/产量/质量趋势/设备运行
    if any(k in name for k in ("process_output", "production_record", "downtime", "output", "yield", "attendance", "work_order")):
        return "业务指标"
    # 规则类：质检/不良/库存/状态判定
    if any(k in name for k in ("inspection", "defect", "inventory", "quality", "maintenance", "order_item")):
        return "业务规则"
    return "业务对象"


def _gen_table_desc_rich(table_name: str, columns: list, table_type: str) -> str:
    """生成表的业务描述"""
    col_names = [c["name"] for c in columns[:5]]
    if table_type == "业务对象":
        return f"{table_name} 是一个业务对象表，记录了 {', '.join(col_names)} 等维度信息，是数据分析的实体基础。"
    elif table_type == "业务指标":
        return f"{table_name} 是一个业务指标表，包含 {', '.join(col_names)} 等度量字段，用于 KPI 监控和趋势分析。"
    elif table_type == "业务规则":
        return f"{table_name} 是一个业务规则表，包含 {', '.join(col_names)} 等分类/判定字段，用于数据筛选和异常识别。"
    return f"{table_name} 数据表，共 {len(columns)} 个字段。"


@router.get("/graph")
def get_knowledge_graph(authorization: str = Header(None), db: Session = Depends(get_db)):
    """返回知识图谱数据：节点（含知识类型分类）+ 边（外键关系）

    权限：已登录非管理员只保留可见表节点、两端可见的边；guest 与超级管理员不受限。
    """
    data = _cached("graph", _build_graph, db)
    return _acl_filter_graph(data, authorization)


def _build_graph(db: Session) -> Dict:
    inspector = inspect(db.get_bind())
    # 遍历所有业务 schema（public + 其他业务 schema，排除系统 schema），保证图谱完整
    schemas = []
    try:
        rows = db.execute(text(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('pg_catalog','information_schema','pg_toast') "
            "AND schema_name NOT LIKE 'pg_%' ORDER BY (schema_name='public') DESC, schema_name"
        )).fetchall()
        schemas = [r[0] for r in rows]
    except Exception:
        schemas = ["public"]
    if not schemas:
        schemas = ["public"]

    tables: list[str] = []
    for sch in schemas:
        try:
            for name in inspector.get_table_names(schema=sch):
                if name.startswith(("_", "metadata_")):
                    continue
                tables.append(name if sch == "public" else f"{sch}.{name}")
        except Exception:
            continue

    # 构建节点
    nodes = []
    node_ids = set()
    for table_ref in tables:
        sch, tbl = _split_schema_table(table_ref)
        try:
            columns = inspector.get_columns(tbl, schema=sch) if sch != "public" else inspector.get_columns(tbl)
        except Exception:
            continue
        table_type = _classify_table_type(tbl)
        icon, label = _get_icon_and_label(tbl)
        row_count = _row_count(db, table_ref)

        # 子字段分类
        sub_fields = []
        for c in columns:
            sub_fields.append({
                "name": c["name"],
                "type": str(c["type"]),
                "ktype": _classify_knowledge_type(c["name"], str(c["type"]), tbl),
            })

        nodes.append({
            "id": table_ref,
            "name": table_ref,
            "label": label,
            "icon": icon,
            "nodeType": table_type,
            "columns": len(columns),
            "rowCount": row_count,
            "connected": False,
            "subFields": sub_fields,
            "desc": _gen_table_desc_rich(tbl, columns, table_type),
        })
        node_ids.add(table_ref)

    # 构建边（外键关系，跨 schema 也关联）
    edges = []
    # 裸表名 → 完整引用（用于外键目标匹配，避免跨 schema 时错误加源表前缀）
    bare_to_ref = {}
    for r in tables:
        _s, _t = _split_schema_table(r)
        bare_to_ref[_t] = r

    for table_ref in tables:
        sch, tbl = _split_schema_table(table_ref)
        try:
            fks = inspector.get_foreign_keys(tbl, schema=sch) if sch != "public" else inspector.get_foreign_keys(tbl)
        except Exception:
            continue
        for fk in fks:
            src_cols = fk.get("constrained_columns", [])
            referred_table = fk.get("referred_table") or ""
            tgt_cols = fk.get("referred_columns", [])
            if not src_cols or not referred_table or not tgt_cols:
                continue
            # 目标表匹配（修复跨 schema 同名表连错 + referred_schema 兜底当表名的误用）：
            # 优先用 referred_schema 构造完整引用；再按裸名匹配；都不到则跳过（目标不在图中）
            table_set = set(tables)
            referred_schema = fk.get("referred_schema") or sch
            target_full = f"{referred_schema}.{referred_table}" if referred_schema != "public" else referred_table
            if target_full in table_set:
                target_ref = target_full
            elif referred_table in bare_to_ref:
                target_ref = bare_to_ref[referred_table]
            else:
                continue
            src_col = src_cols[0]
            tgt_col = tgt_cols[0]
            edges.append({
                "source": table_ref,
                "target": target_ref,
                "sourceColumn": src_col,
                "targetColumn": tgt_col,
                "type": "foreign_key",
                "label": f"{src_col} → {tgt_col}",
                "description": fk.get("name") or f"{table_ref}.{src_col} → {target_ref}.{tgt_col}",
            })

    # 标记已连接节点
    connected = set()
    for e in edges:
        connected.add(e["source"])
        connected.add(e["target"])
    for node in nodes:
        node["connected"] = node["id"] in connected

    return {"nodes": nodes, "edges": edges}


def _split_schema_table(ref: str) -> tuple[str, str]:
    """把 'schema.table' 拆成 (schema, table)；无 schema 时返回 ('public', table)"""
    if "." in ref:
        sch, tbl = ref.split(".", 1)
        return sch, tbl
    return "public", ref


# ========== 获取分析模板（后端动态生成 + 内置兜底） ==========

_TEMPLATE_ICONS = {
    "生产": "🏭", "质量": "📊", "设备": "⚙️", "库存": "📦",
    "销售": "📈", "采购": "🛒", "人事": "👥", "财务": "💰",
    "基础数据": "🗂️",
}

# 内置兜底模板：动态场景不足时保证前端至少有 6 个模板可展示
# 4 大专题报告（质量/设备/生产/库存）+ 2 个周期报告（周报/月报）
# 注意：id 必须显式声明且稳定 —— 用户收藏/使用次数按 id 落库，见 _templates_from_scenes 说明
_BUILTIN_TEMPLATES = [
    {
        "id": "tpl_001",
        "icon": "📊",
        "name": "质量分析报告",
        "scene": "质量分析",
        "desc": "自动生成质量概况：工序合格率、检验结论分布、不良类型排行、严重度构成",
        "tags": ["质量", "周报", "自动报告"],
        "example_question": "请生成一份本周质量分析报告",
        "metrics_count": 5,
        "tables_count": 3,
        "questions": [
            {"q": "各工序的质检合格率", "note": "按工序的抽检合格率对比，识别质量短板工序"},
            {"q": "各检验结果的检验次数", "note": "检验结论（pass/fail）分布"},
            {"q": "各缺陷类型的缺陷数排行", "note": "不良类型 Top 排行，聚焦主要缺陷"},
            {"q": "各严重程度的缺陷数量", "note": "严重度（critical/major/minor）构成"},
            {"q": "质检合格率是多少", "note": "整体抽检合格率"},
        ],
    },
    {
        "id": "tpl_002",
        "icon": "⚙️",
        "name": "设备停机分析报告",
        "scene": "设备分析",
        "desc": "分析停机原因构成、停机时长排行与计划性结构，定位设备改进点",
        "tags": ["设备", "停机", "效率"],
        "example_question": "分析设备停机时间和不良率是否相关",
        "metrics_count": 4,
        "tables_count": 2,
        "questions": [
            {"q": "各停机原因的停机时长排行", "note": "停机原因按累计时长排序"},
            {"q": "各设备的停机时长排行", "note": "停机最久的设备"},
            {"q": "各计划类型的停机次数", "note": "计划内/计划外停机结构（外=故障）"},
            {"q": "平均停机时长是多少", "note": "单次停机平均时长"},
        ],
    },
    {
        "id": "tpl_003",
        "icon": "🏭",
        "name": "生产产量分析报告",
        "scene": "生产分析",
        "desc": "汇总各工序产量、良率与返工情况，掌握产线生产表现",
        "tags": ["生产", "产量", "良率"],
        "example_question": "请分析各工序的良率，找出良率下降的工序",
        "metrics_count": 4,
        "tables_count": 2,
        "questions": [
            {"q": "总产量是多少", "note": "整体产量"},
            {"q": "各工序的产量排行", "note": "按工序产量排序"},
            {"q": "各工序的良率", "note": "工序良率对比"},
            {"q": "各工序的返工数量", "note": "返工量分布，识别质量薄弱工序"},
        ],
    },
    {
        "id": "tpl_004",
        "icon": "📦",
        "name": "库存分析报告",
        "scene": "库存分析",
        "desc": "监控库存水位与分布，识别低于安全库存的预警项，掌握产品结构",
        "tags": ["库存", "预警", "水位"],
        "example_question": "找出库存低于安全线的产品，生成补货清单",
        "metrics_count": 4,
        "tables_count": 2,
        "questions": [
            {"q": "各仓库的库存量", "note": "仓库间库存分布"},
            {"q": "各产品的库存量排行", "note": "库存水位最高的产品"},
            {"q": "库存预警数是多少", "note": "低于安全库存的预警项（0 = 库存健康）"},
            {"q": "各产品类别的产品数", "note": "产品结构分布"},
        ],
    },
    {
        "id": "tpl_005",
        "icon": "📅",
        "name": "生产周报",
        "scene": "周期报告",
        # period.days 只驱动报告的「统计口径」注释与覆盖检查；**真正的取数窗口由下面
        # questions 里的「近N天」文本决定**。改周期时两处必须一起改，否则注释会与实际
        # 取数区间不符。label 省略时由 days 自动生成「近 N 天」，避免两个字段各自漂移。
        "period": {"days": 7},
        "desc": ("一次汇总近 7 天的产量、良率、质量与设备表现，输出可直接汇报的周报。"
                 "周期以数据最新日期为锚点（非系统当前周），滞后数据集同样出数；"
                 "库内不足 7 天时按实际天数统计，并在报告中注明覆盖区间"),
        "tags": ["周报", "周期报告", "自动报告"],
        "example_question": "近7天的产量和良率表现怎么样",
        "metrics_count": 6,
        "tables_count": 5,
        "questions": [
            {"q": "近7天的产量", "note": "本周总产量（口径：合格数 + 不良数）"},
            {"q": "近7天每天的产量", "note": "按日产量走势，看周内波动"},
            {"q": "近7天各产线的产量", "note": "产线间产量对比"},
            {"q": "近7天各工序的良率", "note": "工序良率对比，识别质量短板"},
            {"q": "近7天的质检合格率", "note": "本周抽检合格率（合格数 ÷ 抽检数）"},
            {"q": "近7天各设备的停机时长排行", "note": "停机最久的设备，聚焦设备改进点"},
            {"q": "近7天各产线的工单数", "note": "各产线工单量分布"},
            {"q": "各缺陷类型的缺陷数排行", "note": "缺陷类型 Top 排行（缺陷明细表无时间列，为全量口径）"},
        ],
    },
    {
        "id": "tpl_006",
        "icon": "🗓️",
        "name": "生产月报",
        "scene": "周期报告",
        # 同 tpl_005：period.days 须与 questions 里的「近N天」一起改
        "period": {"days": 30},
        "desc": ("汇总近 30 天的产量、良率、质量与设备整体表现，输出月度经营概览。"
                 "周期以数据最新日期为锚点（非系统当前月），滞后数据集同样出数；"
                 "库内不足 30 天时按实际天数统计，并在报告中注明覆盖区间"),
        "tags": ["月报", "周期报告", "自动报告"],
        "example_question": "近30天的产量和良率整体表现如何",
        "metrics_count": 6,
        "tables_count": 5,
        "questions": [
            {"q": "近30天的产量", "note": "本月总产量（口径：合格数 + 不良数）"},
            {"q": "近30天各产线的产量", "note": "产线月度产量对比"},
            {"q": "近30天各工序的良率", "note": "工序良率对比，识别质量短板"},
            {"q": "近30天的质检合格率", "note": "本月抽检合格率（合格数 ÷ 抽检数）"},
            {"q": "近30天各停机原因的停机时长排行", "note": "停机原因按累计时长排序，定位主要损失来源"},
            {"q": "近30天各产线的工单数", "note": "各产线工单量分布"},
            {"q": "各缺陷类型的缺陷数排行", "note": "缺陷类型 Top 排行（缺陷明细表无时间列，为全量口径）"},
            {"q": "各产品类别的产品数", "note": "产品结构分布"},
        ],
    },
]


def _templates_from_scenes(scenes: Dict) -> List[Dict]:
    """模板中心 = 内置 6 大报告（保底，可一键执行）+ 场景动态模板（追加，与库匹配）。

    顺序策略：内置模板固定在前（质量/设备/生产/库存 4 大专题报告 + 周报/月报 2 个周期报告，
    在任何库都可用，演示与确定性优先）；真实业务场景模板（问题来自场景主题，更贴近当前库）
    追加在后；最后只为「未带 id 的动态模板」补号，保证每个模板 id 唯一、前端执行端点可定位。

    内置模板的 id 在 _BUILTIN_TEMPLATES 中**显式声明且必须保持稳定**：用户收藏与
    模板使用次数按 id 落库（knowledge_user_data.json），一旦因新增内置模板而整体重排，
    历史收藏/统计就会错位到别的模板上。
    """
    templates: List[Dict] = []
    # 1) 内置 4 大报告（deepcopy 防污染）
    import copy as _copy
    for tpl in _BUILTIN_TEMPLATES:
        templates.append(_copy.deepcopy(tpl))
    # 2) 场景动态模板（与内置同业务域跳过，避免重复；场景名可能已带「分析/主题」后缀）
    def _base_of(nm: str) -> str:
        for suf in ("分析主题", "分析", "主题", "专题"):
            if nm.endswith(suf):
                return nm[: -len(suf)]
        return nm
    seen_bases = {_base_of(t.get("name") or "") for t in templates}
    for key, scene in scenes.items():
        name = scene.get("name") or key
        base = _base_of(name)
        metrics = scene.get("metrics") or []
        objects = scene.get("objects") or []
        topics = scene.get("topics") or []
        icon = _TEMPLATE_ICONS.get(name, "🧩")
        question = ""
        for t in topics:
            qs = t.get("questions") or []
            if qs:
                question = qs[0]
                break
        if not question:
            question = f"请生成一份{base}分析报告"
        # 动态模板编排：取该场景分析主题下的真实问题（前 4 个）作为一键执行步骤；
        # 主题问题多为已验证问法（注册口径），执行器命中率最高；未命中的自动跳过
        qs: List[Dict] = []
        for t in topics:
            for tq in (t.get("questions") or [])[:2]:
                if len(qs) >= 4:
                    break
                qs.append({"q": tq})
            if len(qs) >= 4:
                break
        if not qs:
            qs = [{"q": question}]
        tpl_name = f"{base}分析报告"
        if base in seen_bases or tpl_name in {t.get("name") for t in templates}:
            continue
        seen_bases.add(base)
        templates.append({
            "icon": icon,
            "name": tpl_name,
            "scene": name if name.endswith("分析") else f"{base}分析",
            "desc": f"自动汇总{base}相关业务对象与指标，生成一份{base}分析报告",
            "tags": [base, "分析报告"],
            "example_question": question,
            "metrics_count": len(metrics),
            "tables_count": len(objects),
            "questions": qs,
        })
    # 3) 只为动态场景模板补 id（内置模板 id 已显式声明，保持稳定以防收藏/统计错位）
    _next = len(_BUILTIN_TEMPLATES) + 1
    for tpl in templates:
        if not tpl.get("id"):
            tpl["id"] = f"tpl_{_next:03d}"
            _next += 1
    return templates


# ========== 模板推荐：按业务人员身份（授权表范围 + 使用习惯）动态裁剪与排序 ==========
# 不再「6 个内置模板人人可见」：权限决定哪些模板能看/能执行，行为（使用次数、收藏）
# 决定谁置顶 —— 两层合起来才是"根据身份推荐常见的分析模板"。

_TEMPLATE_TABLES_CACHE: Dict[Any, set] = {}


def _match_question_tables(q: str, finder=None) -> set:
    """单条问题命中的指标 → 涉及的数据表集合（裸名小写）。未命中返回空集。

    复用 metric_registry.find_metrics —— 与执行器 try_compile_metric 同一套指标召回，
    纯字符串匹配，不查库不执行 SQL；未命中指标的步骤在执行器里本来就会 skip（不取数），
    所以不贡献表范围。
    """
    if not q:
        return set()
    try:
        if finder is None:
            from agent.metric_registry import find_metrics as finder
        hits = finder(q, limit=2) or []
    except Exception:
        return set()
    out: set = set()
    for m in hits:
        for t in (m.get("tables") or []):
            out.add(str(t).split(".")[-1].lower())
    return out


def _template_required_tables(tpl: Dict) -> set:
    """模板全部步骤涉及的数据表集合（裸名小写）。结果按 (模板id, 问题文本) 缓存
    —— 模板 questions 静态，避免每次进模板页都重跑一遍指标匹配。"""
    qs = tuple(str((item or {}).get("q") or "") for item in (tpl.get("questions") or []))
    ck = (str(tpl.get("id") or ""), qs)
    hit = _TEMPLATE_TABLES_CACHE.get(ck)
    if hit is not None:
        return hit
    tables: set = set()
    try:
        from agent.metric_registry import find_metrics
        for q in qs:
            tables |= _match_question_tables(q, find_metrics)
    except Exception:
        tables = set()
    _TEMPLATE_TABLES_CACHE[ck] = tables
    return tables


def _template_persona(request: Request, authorization: str) -> tuple:
    """当前业务人员的「身份画像」：(full, allowed, used_count, fav_ids, roles)。

    full/allowed = 授权表范围（_acl_visible，None=不受限）；used/favs 来自
    knowledge_user_data（按模板 id 落库的使用次数与收藏）；roles = 权限系统解析出的
    角色名列表。授权范围决定"哪些模板能看能执行"，使用行为决定"哪些优先展示"。
    """
    from routers.tables import _acl_visible
    full, allowed = _acl_visible(authorization)
    used: Dict[str, int] = {}
    favs: set = set()
    roles: list = []
    try:
        from auth import get_current_user
        from security.enforcer import build_acl_context
        ctx = build_acl_context(get_current_user(authorization))
        roles = [str(r) for r in (getattr(ctx, "roles", None) or []) if str(r).strip()]
    except Exception:
        roles = []
    try:
        from knowledge_user_data import get_user_data
        ud = get_user_data(_username_of(authorization, request)) or {}
        used = {str(k): int(v or 0) for k, v in (ud.get("template_use_count") or {}).items()}
        favs = {str(f) for f in (ud.get("favorites") or []) if str(f).startswith("tpl:")}
    except Exception:
        pass
    return full, allowed, used, favs, roles


def _recommend_templates(request: Request, templates: List[Dict], authorization: str,
                         table_domains: Dict[str, str] | None = None) -> Dict:
    """按身份把模板清单裁成「这个人能用、且对他有用」的样子。

    - 不受限（guest/超管/未配置表授权）：全部保留，常用与收藏的置顶；
    - 受限：先解析每个模板步骤命中的数据表——
      ① 全部在授权范围 → 保留（acl_status=full）；
      ② 部分在授权范围 → 整单隐藏（计入 summary.hidden）。不做"精简保留"：
         设备人员看到只剩一步的"生产周报"只会困惑（2026-09-25 实测反馈）；
      ③ 一步都不能在授权范围内出数 → 隐藏。
      执行端（run_template_report 的 allowed_tables）仍逐步骤做 fail-closed 校验，
      与列表口径一致且多一层纵深防御。
    返回 {templates, summary}；summary 供前端渲染推荐横幅（可访问几张表、推荐几个、
    隐藏几个）。
    """
    full, allowed, used, favs, roles = _template_persona(request, authorization)
    domains = table_domains or {}
    allowed_lc = {str(t).split(".")[-1].lower() for t in (allowed or set())}

    def _dom_labels(tabs: set) -> str:
        ds = sorted({domains.get(t, "") for t in tabs} - {""})
        return "、".join(ds)

    visible: List[Dict] = []
    hidden = 0
    for tpl in templates:
        tid = str(tpl.get("id") or "")
        req = _template_required_tables(tpl)
        reasons: List[str] = []
        used_n = used.get(tid) or 0
        if tid in favs:
            reasons.append("您收藏过")
        if used_n:
            reasons.append(f"您用过 {used_n} 次")
        if full:
            tpl["acl_status"] = "full"
            if req:
                _d = _dom_labels(req)
                if _d:
                    reasons.append(f"覆盖您授权的「{_d}」数据，全部步骤都能出数")
            tpl["recommend_reason"] = "；".join(reasons)
            visible.append(tpl)
            continue
        # ── 受限用户 ──
        if not req:
            hidden += 1        # 步骤命中不了任何注册口径：受限用户看不到（避免空壳模板）
            continue
        missing = req - allowed_lc
        if not missing:
            tpl["acl_status"] = "full"
            _d = _dom_labels(req)
            if _d:
                reasons.append(f"覆盖您授权的「{_d}」数据，全部步骤都能出数")
            tpl["recommend_reason"] = "；".join(reasons)
            visible.append(tpl)
            continue
        # 部分覆盖：整单隐藏。曾有"精简保留"设计（只留授权内的步骤），实测反馈是
        # 设备人员会看到只剩一步的"生产周报"，比藏掉更困惑 —— 宁可少推，不推残的。
        hidden += 1
        continue

    # 排序：完全覆盖 > 按权限精简过的；同档内 常用次数多 > 收藏过 > 原序（Python 稳定排序）
    visible.sort(key=lambda t: (
        0 if t.get("acl_status") == "full" else 1,
        -int(used.get(str(t.get("id") or "")) or 0),
        0 if str(t.get("id") or "") in favs else 1,
    ))
    # 推荐标记：完全覆盖模板的前 3 个（含个性化置顶的）
    for i, t in enumerate(visible):
        t["recommended"] = bool(t.get("acl_status") == "full" and i < 3)

    return {
        "templates": visible,
        "summary": {
            "mode": "full" if full else "scoped",
            "visible": len(visible),
            "recommended": sum(1 for t in visible if t["recommended"]),
            "hidden": hidden,
            "allowed_tables": (None if full else len(allowed_lc)),
            "roles": roles,
        },
    }


@router.get("/templates")
def get_knowledge_templates(request: Request, authorization: str = Header(None),
                            db: Session = Depends(get_db)):
    """分析模板列表：按当前业务人员的身份动态推荐（不预置死模板清单）。

    推荐依据两层：
    - 权限身份：当前用户被授权的数据库表范围 —— 只推荐"步骤取数的表全部在授权范围内"
      的模板；部分覆盖的整单不展示；完全无法出数的不展示；
    - 行为身份：该用户此前的模板使用次数与收藏 —— 常用/收藏的置顶并标注推荐。
    场景动态模板仍复用 /scenes 的 ACL 过滤；内置 6 大报告由 _recommend_templates 裁剪。
    """
    data = _cached("scenes", _build_scenes, db)
    scenes = _acl_filter_scenes(data, authorization)["scenes"]
    templates = _templates_from_scenes(scenes)
    # 表 → 业务域中文名（来自场景对象的归属，用于推荐理由里"覆盖您授权的「质量」数据"）
    domains: Dict[str, str] = {}
    for s in (scenes or {}).values():
        nm = str(s.get("name") or "")
        if not nm:
            continue
        for o in (s.get("objects") or []):
            t = str(o.get("table") or "").split(".")[-1].lower()
            if t and t not in domains:
                domains[t] = nm
    return _recommend_templates(request, templates, authorization, domains)


def _find_knowledge_template(scenes: Dict, template_id: str) -> Optional[Dict]:
    """按 id 在「动态场景模板 + 内置模板」中定位模板。"""
    for tpl in _templates_from_scenes(scenes):
        if tpl.get("id") == template_id:
            return tpl
    return None


@router.post("/templates/{template_id}/run")
def run_knowledge_template(request: Request, template_id: str,
                           authorization: str = Header(None),
                           db: Session = Depends(get_db)):
    """一键执行分析模板：按模板 questions[] 逐题走「确定性编译」管线，
    组装单文件 HTML 报告（图表 SVG + 表格 + 规则结论 + SQL 溯源）。

    - LLM 不参与执行：全部步骤命中指标注册表口径 → 演示稳定可复现；
    - 未命中口径的步骤自动标记 skip（提示到问数页执行），不阻断整体；
    - 鉴权：与 /scenes 一致（ACL 过滤后的可见场景内模板才可执行）；
      另把授权表集合下传执行器，逐步骤按编译出的取数表做 fail-closed 校验 ——
      此前内置模板不受场景 ACL 约束，受限用户可执行任意内置报告（数据越权），已堵。

    ⚠ 2026-10-07 修复「一键生成报告」全部 500：
      本函数原先签名里**没有 `request`**，但函数体第 2001 行调用了
      `_username_of(authorization, request)` ⇒ `NameError: name 'request' is not defined`
      ⇒ 8 个模板 100% 报 500（本文件其余 13 处调用都声明了 `request: Request`，
      唯独此处漏了）。审计后已把 `request: Request` 提到参数表首位。
      **教训：FastAPI 路由里凡在函数体引用 `request`，签名必须显式声明**——
      Python 不会报错到定义处，只会在**运行时**炸，这类 bug 静态查不出来。
    """
    data = _cached("scenes", _build_scenes, db)
    scenes = _acl_filter_scenes(data, authorization)["scenes"]
    tpl = _find_knowledge_template(scenes, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail=f"模板不存在或当前用户无权访问: {template_id}")

    from routers.tables import _acl_visible
    from agent.template_runner import run_template_report
    full_acl, allowed = _acl_visible(authorization)
    result = run_template_report(
        tpl,
        allowed_tables=None if full_acl
        else {str(t).split(".")[-1].lower() for t in (allowed or set())})
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    username = _username_of(authorization, request)
    _audit_knowledge(username, "knowledge_template_run", template=tpl.get("name"))
    # 简化步骤（不含 rows 全量，避免响应过大）
    slim = [{
        "q": s.get("q"), "status": s.get("status"),
        "metric": s.get("metric"), "unit": s.get("unit"),
        "chart_type": s.get("chart_type", ""),
        "note": s.get("note", ""),
        "hint": s.get("hint", ""),
        "rows": len(s.get("rows") or []),
    } for s in result["steps"]]
    return {
        "success": True,
        "template": {"id": tpl.get("id"), "name": tpl.get("name"), "scene": tpl.get("scene")},
        "html": result["html"],
        "steps": slim,
        "elapsed": round(result["elapsed"], 2),
        # 周期报告的统计口径注释（含各事实表实际覆盖天数）；非周期模板为 None
        "period": result.get("period"),
    }


# =========================================================
# 用户知识数据：收藏 / 反馈 / 人工覆盖 / 自定义术语 / 模板使用
# （存储逻辑在 knowledge_user_data.py；这里只做 HTTP 层 + 审计）
# =========================================================

from knowledge_user_data import (
    get_user_data, set_favorite, set_feedback, set_override, remove_override,
    add_custom_term, remove_custom_term, record_template_use,
    apply_scene_overrides, apply_term_overrides,
)


def _audit_knowledge(username: str, event: str, **detail) -> None:
    try:
        from auth import audit
        audit(event, user=username, **detail)
    except Exception:
        pass


@router.get("/user-data")
def api_get_user_data(request: Request, authorization: str = Header(None)):
    """返回当前用户的收藏 / 反馈 / 人工覆盖 / 自定义术语 / 模板使用统计。"""
    return get_user_data(_username_of(authorization, request))


@router.post("/favorite")
def api_set_favorite(body: dict, request: Request, authorization: str = Header(None)):
    key = str((body or {}).get("key") or "")
    value = bool((body or {}).get("value"))
    username = _username_of(authorization, request)
    favorites = set_favorite(username, key, value)
    _audit_knowledge(username, "knowledge_favorite", key=key, value=value)
    return {"success": True, "favorites": favorites}


@router.post("/feedback")
def api_set_feedback(body: dict, request: Request, authorization: str = Header(None)):
    key = str((body or {}).get("key") or "")
    vote = str((body or {}).get("vote") or "")
    reason = str((body or {}).get("reason") or "")
    username = _username_of(authorization, request)
    result = set_feedback(username, key, vote, reason)
    _audit_knowledge(username, "knowledge_feedback", key=key, vote=vote)
    return {"success": True, "feedback": result}


@router.post("/override")
def api_set_override(body: dict, request: Request, authorization: str = Header(None)):
    key = str((body or {}).get("key") or "")
    kind = str((body or {}).get("kind") or "")
    patch = (body or {}).get("patch") or {}
    username = _username_of(authorization, request)
    result = set_override(username, key, kind, patch)
    _audit_knowledge(username, "knowledge_edit", key=key, kind=kind)
    return {"success": True, "override": result}


@router.delete("/override/{key:path}")
def api_remove_override(key: str, request: Request, authorization: str = Header(None)):
    username = _username_of(authorization, request)
    removed = remove_override(username, key)
    _audit_knowledge(username, "knowledge_edit_clear", key=key)
    return {"success": True, "removed": removed}


@router.post("/terms")
def api_add_term(body: dict, request: Request, authorization: str = Header(None)):
    username = _username_of(authorization, request)
    term = add_custom_term(username, body or {})
    _audit_knowledge(username, "knowledge_term_add", term=term.get("term"))
    return {"success": True, "term": term}


@router.delete("/terms/{term:path}")
def api_remove_term(term: str, request: Request, authorization: str = Header(None)):
    username = _username_of(authorization, request)
    removed = remove_custom_term(username, term)
    _audit_knowledge(username, "knowledge_term_remove", term=term)
    return {"success": True, "removed": removed}


@router.post("/template-use")
def api_template_use(body: dict, request: Request, authorization: str = Header(None)):
    tid = str((body or {}).get("template_id") or "")
    username = _username_of(authorization, request)
    count = record_template_use(username, tid)
    return {"success": True, "use_count": count}


# =========================================================
# 知识健康度看板：覆盖率统计，让维护有目标
# =========================================================

@router.get("/health")
def get_knowledge_health(request: Request, authorization: str = Header(None),
                         db: Session = Depends(get_db)):
    scenes_data = _cached("scenes", _build_scenes, db)
    scenes_data = _acl_filter_scenes(scenes_data, authorization)
    scenes = scenes_data.get("scenes") or {}
    terms_data = _cached("terms", _build_terms, db)
    terms_data = _acl_filter_terms(terms_data, authorization)
    terms = terms_data.get("terms") or []
    username = _username_of(authorization, request)
    custom_terms = (get_user_data(username).get("custom_terms") or [])

    def _has_cn(s: str) -> bool:
        return any('\u4e00' <= ch <= '\u9fff' for ch in str(s or ''))

    tables = 0
    tables_without_alias = 0
    fields_total = 0
    fields_without_translation = 0
    metrics_total = 0
    metrics_without_formula = 0
    rules_total = 0
    topics_total = 0
    disabled = 0

    for s in scenes.values():
        for o in s.get("objects") or []:
            if o.get("_disabled"):
                disabled += 1
                continue
            tables += 1
            if not _has_cn(o.get("label", "")):
                tables_without_alias += 1
            for c in o.get("columns") or []:
                if c.get("_disabled"):
                    continue
                fields_total += 1
                tr = str(c.get("translation") or "").strip()
                if not tr or tr == c.get("name") or tr == str(c.get("name", "")).replace("_", " "):
                    fields_without_translation += 1
        for m in s.get("metrics") or []:
            if m.get("_disabled"):
                continue
            metrics_total += 1
            if not str(m.get("formula") or "").strip():
                metrics_without_formula += 1
        rules_total += len([r for r in (s.get("rules") or []) if not r.get("_disabled")])
        topics_total += len([t for t in (s.get("topics") or []) if not t.get("_disabled")])

    # 无关联的表（孤岛表）：从关系数据统计
    orphan_tables = 0
    try:
        rels_data = _cached("relations", _build_relations, db)
        rels = rels_data.get("relations") or []
        linked = set()
        for r in rels:
            linked.add(r.get("source_table"))
            linked.add(r.get("target_table"))
        for s in scenes.values():
            for o in s.get("objects") or []:
                if o.get("_disabled"):
                    continue
                t = o.get("table")
                if t not in linked:
                    orphan_tables += 1
    except Exception:
        orphan_tables = 0

    total_scored = max(1, tables + fields_total + metrics_total)
    score = round(100 * (tables - tables_without_alias + fields_total - fields_without_translation + metrics_total - metrics_without_formula) / total_scored, 1)

    return {
        "score": score,
        "tables": tables,
        "tables_without_alias": tables_without_alias,
        "fields_total": fields_total,
        "fields_without_translation": fields_without_translation,
        "metrics_total": metrics_total,
        "metrics_without_formula": metrics_without_formula,
        "rules_total": rules_total,
        "topics_total": topics_total,
        "terms_total": len(terms),
        "custom_terms_total": len(custom_terms),
        "orphan_tables": orphan_tables,
        "disabled_total": disabled,
    }


# =========================================================
# 知识全局搜索：跨场景 / 对象 / 指标 / 规则 / 主题 / 术语
# =========================================================

@router.get("/search")
def search_knowledge(q: str = "", authorization: str = Header(None), db: Session = Depends(get_db)):
    query = (q or "").strip().lower()
    if not query:
        return {"results": []}

    scenes_data = _cached("scenes", _build_scenes, db)
    scenes_data = _acl_filter_scenes(scenes_data, authorization)
    scenes = scenes_data.get("scenes") or {}
    terms_data = _cached("terms", _build_terms, db)
    terms_data = _acl_filter_terms(terms_data, authorization)
    terms = terms_data.get("terms") or []

    def hit(*parts: str) -> bool:
        hay = " ".join(str(p) for p in parts).lower()
        return query in hay

    results = []
    for skey, s in scenes.items():
        scene_name = s.get("name") or skey
        for o in s.get("objects") or []:
            if o.get("_disabled"):
                continue
            if hit(o.get("table"), o.get("label"), o.get("desc")):
                results.append({
                    "key": f"obj:{o.get('table')}", "type": "业务对象",
                    "title": o.get("label") or o.get("table"),
                    "subtitle": o.get("table"), "scene": scene_name,
                    "table": o.get("table"), "desc": o.get("desc"),
                })
        for m in s.get("metrics") or []:
            if m.get("_disabled"):
                continue
            if hit(m.get("name"), m.get("formula"), m.get("description")):
                results.append({
                    "key": f"met:{m.get('name')}", "type": "业务指标",
                    "title": m.get("name"), "subtitle": m.get("formula") or "",
                    "scene": scene_name,
                })
        for r in s.get("rules") or []:
            if r.get("_disabled"):
                continue
            if hit(r.get("name"), r.get("condition")):
                results.append({
                    "key": f"rule:{r.get('name')}", "type": "业务规则",
                    "title": r.get("name"), "subtitle": r.get("condition") or "",
                    "scene": scene_name,
                })
        for t in s.get("topics") or []:
            if t.get("_disabled"):
                continue
            if hit(t.get("name"), t.get("desc")):
                results.append({
                    "key": f"topic:{t.get('name')}", "type": "分析主题",
                    "title": t.get("name"), "subtitle": t.get("desc") or "",
                    "scene": scene_name,
                })
    for t in terms:
        if t.get("_disabled"):
            continue
        if hit(t.get("term"), t.get("en"), t.get("definition"), t.get("category"), t.get("abbreviation")):
            results.append({
                "key": f"term:{t.get('term')}", "type": "术语",
                "title": t.get("term"), "subtitle": t.get("definition") or "",
                "scene": t.get("category") or "",
            })

    # 每类最多返回 20 条，避免结果过多
    return {"results": results[:100]}


# =========================================================
# 导出：术语词典 / 指标口径（CSV / Markdown）
# =========================================================

@router.get("/export")
def export_knowledge(request: Request, scope: str = "terms", format: str = "md",
                     authorization: str = Header(None), db: Session = Depends(get_db)):
    """导出术语词典/指标口径为 MD / CSV 文件。

    权限：需角色策略开通 export 操作权限（admin 全放行，其余 fail-close）。
    说明：本端点原先完全无操作级校验，**匿名调用即可下载全量知识**（且因 guest 不受 ACL
          限制，匿名拿到的术语比登录的普通员工还多）。前端此前用 window.open 打开本URL，
          浏览器不会携带 Authorization 头，等于任何人都能绕过导出权限。
          现补后端校验；前端已同步改为「带 token 的 fetch + blob 下载」。
    """
    _require_action_export(authorization)
    fmt = (format or "md").lower()
    username = _username_of(authorization, request)

    # 白名单：原先只判断 `scope == "terms"`，任何非法值都静默落到 metrics 分支
    # 并返回 200 + 指标口径文件 —— 调用方拼错参数时完全无从察觉。
    scope = (scope or "terms").strip().lower()
    if scope not in ("terms", "metrics"):
        raise HTTPException(status_code=400, detail=f"不支持的导出范围：{scope}（仅支持 terms / metrics）")
    if fmt not in ("md", "csv"):
        raise HTTPException(status_code=400, detail=f"不支持的导出格式：{fmt}（仅支持 md / csv）")

    if scope == "terms":
        data = _cached("terms", _build_terms, db)
        data = _acl_filter_terms(data, authorization)
        data = apply_term_overrides(username, data)
        rows = data.get("terms") or []
        if fmt == "csv":
            buf = io.StringIO()
            buf.write("\ufeffterm,en,definition,category,knowledge_type,abbreviation,data_type,mapped_table\n")
            for t in rows:
                vals = [t.get("term_cn") or t.get("term", "")]
                vals += [t.get(k, "") for k in ("en", "definition", "category", "knowledge_type", "abbreviation", "data_type", "mapped_table")]
                buf.write(",".join(str(v).replace(",", "，").replace('"', '""') for v in vals) + "\n")
            content = buf.getvalue()
            return StreamingResponse(iter([content]), media_type="text/csv; charset=utf-8",
                                    headers={"Content-Disposition": "attachment; filename=knowledge_terms.csv"})
        lines = ["# 业务术语词典", ""]
        lines.append(f"共 {len(rows)} 个术语")
        lines.append("")
        for t in rows:
            lines.append(f"## {t.get('term_cn') or t.get('term')}")
            if t.get("en"):
                lines.append(f"- 英文名：{t.get('en')}")
            if t.get("knowledge_type"):
                lines.append(f"- 知识类型：{t.get('knowledge_type')}")
            if t.get("category"):
                lines.append(f"- 业务分类：{t.get('category')}")
            if t.get("abbreviation"):
                lines.append(f"- 简称：{t.get('abbreviation')}")
            if t.get("definition"):
                lines.append(f"- 定义：{t.get('definition')}")
            if t.get("mapped_table"):
                lines.append(f"- 来源：{t.get('table_cn') or t.get('mapped_table')}{'.' + str(t.get('mapped_field')) if t.get('mapped_field') else ''}")
            lines.append("")
        content = "\n".join(lines)
        return StreamingResponse(iter([content]), media_type="text/markdown; charset=utf-8",
                                headers={"Content-Disposition": "attachment; filename=knowledge_terms.md"})

    # 指标口径导出
    scenes_data = _cached("scenes", _build_scenes, db)
    scenes_data = _acl_filter_scenes(scenes_data, authorization)
    scenes_data = apply_scene_overrides(username, scenes_data)
    scenes = scenes_data.get("scenes") or {}
    metrics = []
    for s in scenes.values():
        for m in s.get("metrics") or []:
            if not m.get("_disabled"):
                metrics.append({**m, "scene": s.get("name")})

    if fmt == "csv":
        buf = io.StringIO()
        buf.write("\ufeffname,formula,unit,description,scene,tables\n")
        for m in metrics:
            buf.write(",".join(
                str(m.get(k, "")).replace(",", "，").replace('"', '""')
                for k in ("name", "formula", "unit", "description", "scene")
            ) + "," + "|".join(m.get("tables") or []) + "\n")
        content = buf.getvalue()
        return StreamingResponse(iter([content]), media_type="text/csv; charset=utf-8",
                                headers={"Content-Disposition": "attachment; filename=knowledge_metrics.csv"})

    lines = ["# 指标口径清单", ""]
    lines.append(f"共 {len(metrics)} 个指标")
    lines.append("")
    for m in metrics:
        lines.append(f"## {m.get('name')}")
        if m.get("scene"):
            lines.append(f"- 业务场景：{m.get('scene')}")
        if m.get("formula"):
            lines.append(f"- 口径公式：{m.get('formula')}")
        if m.get("unit"):
            lines.append(f"- 计量单位：{m.get('unit')}")
        if m.get("description"):
            lines.append(f"- 说明：{m.get('description')}")
        if m.get("tables"):
            lines.append(f"- 涉及表：{', '.join(m.get('tables'))}")
        lines.append("")
    content = "\n".join(lines)
    return StreamingResponse(iter([content]), media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": "attachment; filename=knowledge_metrics.md"})
