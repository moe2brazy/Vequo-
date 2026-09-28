"""动态业务主题引擎 — 根据当前数据库的表/字段语义识别业务主题，不局限于固定四主题

需求背景（文档《业务知识页面主题展示问题》）：用户连接的数据库是销售表，
系统应展示「采购分析、销售分析」等主题，而不是写死的生产/质量/设备/库存。

设计：
- 规则通道（主）：TOPIC_RULES 覆盖多个业务域（销售/采购/生产/质量/设备/库存/
  财务/人资/物流/项目），按「表名 + 字段名」关键词加权打分归类，确定性强、零延迟；
- LLM 通道（兜底，可选）：规则未命中且表量较大时，可用 LLM 从表清单推断主题
  （默认关闭，避免不稳定与额外成本；需要时置 USE_LLM_FALLBACK=1）。
"""

from __future__ import annotations

from typing import Iterable

# ── 主题规则库：keywords 命中表名/字段名即计分 ──
TOPIC_RULES: list[dict] = [
    {
        "key": "sales", "name": "销售分析", "icon": "💰", "desc": "订单、销售业绩、客户、回款",
        "roles": ["admin"],
        "keywords": ["sales", "sale", "order", "customer", "invoice", "订单", "销售", "客户", "回款", "收款", "成交"],
        "tables": ["sales_order", "order_detail", "customer", "invoice", "sales_record"],
    },
    {
        "key": "purchase", "name": "采购分析", "icon": "🛒", "desc": "采购订单、供应商、到货、应付",
        "roles": ["admin"],
        "keywords": ["purchase", "supplier", "procurement", "vendor", "po_", "采购", "供应商", "进货", "到货", "应付"],
        "tables": ["purchase_order", "supplier", "procurement", "purchase_record"],
    },
    {
        "key": "production", "name": "生产分析", "icon": "🏭", "desc": "产量趋势、工序良率、工单执行",
        "roles": ["admin", "viewer"],
        "keywords": ["mes_", "work_order", "process_output", "production", "output", "yield", "工单", "产量", "工序", "产线", "生产"],
        # dim_product（产品主数据）与 dim_process / dim_production_line 同属产线主数据维表，
        # 缺失会导致 yans 等制造库「产品表」不进任何场景 → 知识页数据表计数少 1（9/10）
        "tables": ["mes_work_order", "mes_process_output", "dim_process", "dim_production_line",
                   "dim_product", "production_record", "work_order"],
    },
    {
        "key": "quality", "name": "质量分析", "icon": "✅", "desc": "缺陷类型、检验结果、不良分布",
        "roles": ["admin", "viewer"],
        "keywords": ["qms_", "defect", "inspection", "quality", "test", "检验", "质检", "缺陷", "不良", "合格"],
        "tables": ["qms_inspection", "qms_defect_detail", "quality_inspection", "inspection_record"],
    },
    {
        "key": "equipment", "name": "设备分析", "icon": "⚙️", "desc": "停机时长、设备状态、运行效率",
        "roles": ["admin"],
        "keywords": ["eqp_", "downtime", "equipment", "machine", "maintenance", "设备", "停机", "保养", "点检", "维修"],
        "tables": ["eqp_downtime_record", "dim_equipment", "equipment_record", "maintenance_record"],
    },
    {
        "key": "inventory", "name": "库存分析", "icon": "📦", "desc": "库存水位、物料周转、安全库存",
        "roles": ["admin", "viewer"],
        "keywords": ["inv_", "stock", "inventory", "warehouse", "material", "库存", "仓库", "物料", "呆滞", "出入库"],
        "tables": ["inv_inventory_snapshot", "stock_record", "warehouse", "inventory_snapshot"],
    },
    {
        "key": "finance", "name": "财务分析", "icon": "💹", "desc": "收入、成本、费用、利润",
        "roles": ["admin"],
        "keywords": ["finance", "account", "payroll", "cost", "expense", "revenue", "ledger", "财务", "账", "成本", "费用", "工资", "薪资", "利润", "收入"],
        "tables": ["finance_record", "account", "payroll", "cost_record", "ledger"],
    },
    {
        "key": "hr", "name": "人力分析", "icon": "👥", "desc": "员工、考勤、请假、组织",
        "roles": ["admin"],
        "keywords": ["employee", "attendance", "leave", "hr_", "staff", "员工", "考勤", "请假", "部门", "人员", "人事"],
        "tables": ["employee", "attendance", "leave_record", "hr_employee", "department"],
    },
    {
        "key": "logistics", "name": "物流分析", "icon": "🚚", "desc": "发货、运输、配送、签收",
        "roles": ["admin"],
        "keywords": ["logistics", "shipment", "transport", "delivery", "物流", "运输", "发货", "配送", "签收", "运单"],
        "tables": ["shipment", "transport_record", "delivery_record"],
    },
    {
        "key": "project", "name": "项目管理", "icon": "🗂️", "desc": "项目进度、任务、里程碑",
        "roles": ["admin"],
        "keywords": ["project", "task", "milestone", "项目", "任务", "里程碑", "进度"],
        "tables": ["project", "project_task", "milestone", "task_record"],
    },
]

# 表名前缀 → 主题 key（精确快速路径，避免关键词误伤；与 SCENE_TABLE_PREFIXES 对齐）
_PREFIX_INDEX: dict[str, str] = {}
for _rule in TOPIC_RULES:
    for _t in _rule.get("tables", []):
        _PREFIX_INDEX.setdefault(_t, _rule["key"])


def _table_keywords(table_name: str) -> list[str]:
    """把表名拆成可匹配的关键词片段（含下划线拆分）"""
    name = table_name.lower()
    parts = [p for p in name.replace(".", "_").split("_") if p]
    return parts


def detect_topic_key(table_name: str, fields: Iterable[str] | None = None) -> str | None:
    """判定单张表属于哪个主题 key（无命中返回 None）。

    Args:
        table_name: 表名（可带 schema 前缀，如 factory.quality_inspection）
        fields: 字段名列表（可选，提供可提升销售/采购等语义识别准确率）
    """
    bare = table_name.split(".", 1)[-1].lower()
    # 1. 精确表名/前缀索引
    if bare in _PREFIX_INDEX:
        return _PREFIX_INDEX[bare]
    for rule in TOPIC_RULES:
        for t in rule.get("tables", []):
            if bare == t or bare.startswith(t + "_"):
                return rule["key"]
    # 2. 关键词打分（表名 2 分、字段名 0.5 分）
    scores: dict[str, int] = {}
    for rule in TOPIC_RULES:
        kw = [k.lower() for k in rule["keywords"]]
        score = 0
        for k in kw:
            if k in bare:
                score += 2
        if fields:
            for k in kw:
                for f in fields:
                    if k in f.lower():
                        score += 0.5
        if score:
            scores[rule["key"]] = score
    if not scores:
        return None
    return max(scores, key=scores.get)


def detect_topics(table_names: Iterable[str],
                  field_map: dict[str, list[str]] | None = None) -> list[dict]:
    """对整库表清单识别主题。

    Args:
        table_names: 表名列表
        field_map: {表名: [字段名]}，可选，用于提升语义识别准确率

    Returns:
        命中了表的主题列表（按命中表数降序），每项:
        {key, name, icon, desc, roles, table_names: [...]}
    """
    field_map = field_map or {}
    buckets: dict[str, dict] = {}
    for rule in TOPIC_RULES:
        buckets[rule["key"]] = {
            "key": rule["key"], "name": rule["name"], "icon": rule["icon"],
            "desc": rule["desc"], "roles": rule["roles"], "table_names": [],
        }
    for t in table_names:
        bare = t.split(".", 1)[-1].lower()
        if bare.startswith(("_", "pg_", "metadata_")):
            continue
        key = detect_topic_key(t, field_map.get(t) or field_map.get(bare))
        if key:
            buckets[key]["table_names"].append(t)
    result = [b for b in buckets.values() if b["table_names"]]
    result.sort(key=lambda b: len(b["table_names"]), reverse=True)
    return result
