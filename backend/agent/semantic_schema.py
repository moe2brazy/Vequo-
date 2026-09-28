"""语义模型 JSON Schema — 指标定义的权威结构描述（供校验/文档/前端表单生成）

指标（metric）是语义层的核心对象。本 Schema 作为单一事实来源，
语义校验器（semantic_validator.py）据此做结构校验，未来可驱动前端表单、
CI 校验与文档生成，逐步收敛到「模型即代码」。
"""

# 指标定义 JSON Schema（Draft 7 子集，可用 jsonschema 库校验，亦可手写实现）
METRIC_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "https://workbuddy.local/semantic/metric.schema.json",
    "title": "MetricDefinition",
    "type": "object",
    "required": ["name", "sql_expression", "tables"],
    "properties": {
        "name": {
            "type": "string", "minLength": 1,
            "description": "指标唯一名称（业务口径唯一标识）",
        },
        "aliases": {
            "type": "array", "items": {"type": "string"},
            "description": "同义词/别名，用于自然语言命中",
        },
        "unit": {"type": "string", "description": "单位（件/元/%/天…）"},
        "tables": {
            "type": "array", "minItems": 1, "items": {"type": "string"},
            "description": "口径依赖的事实表/维度表（带 schema 或裸名均可）",
        },
        "sql_expression": {
            "type": "string", "minLength": 1,
            "description": "口径聚合表达式（如 SUM(good_qty) / NULLIF(SUM(input_qty),0) * 100）",
        },
        "formula": {"type": "string", "description": "人类可读口径公式"},
        "description": {"type": "string", "description": "口径说明"},
        "dims": {
            "type": "array", "items": {"type": "string"},
            "description": "可下钻/分组的维度（中文）",
        },
        "valid_from": {"type": "string", "description": "口径生效起始日期（ISO：YYYY-MM-DD）"},
        "valid_to": {"type": "string", "description": "口径失效日期（ISO：YYYY-MM-DD），缺省=永久"},
        "depends_on": {
            "type": "array", "items": {"type": "string"},
            "description": "派生指标依赖的基础指标名（血缘）",
        },
    },
    "additionalProperties": True,
}
