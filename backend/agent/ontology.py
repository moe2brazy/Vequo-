"""轻量业务本体（阶段二）— 实体关系图谱，叠加在 FK 图之上

把「业务对象与业务对象」的关系显式建模（区别于表级 related_tables/FK 图）：
- 实体：别名、归属表、主键、显示列、类别（维度/事实）
- 关系路径：实体间通过事实表外键可达，复用 join_planner 的 FK 图做路径枚举

供归因分析注入「业务关系指引」（阶段四），也作后续本体推理的可查询底座。
纯数据 + 纯函数，可单测，不依赖 LLM。
"""

from __future__ import annotations

ENTITIES: list[dict] = [
    {"name": "产品", "aliases": ["产品", "商品"], "table": "dim_product", "key": "product_id", "display": "product_name", "kind": "dim"},
    {"name": "产线", "aliases": ["产线", "生产线"], "table": "dim_production_line", "key": "line_id", "display": "line_name", "kind": "dim"},
    {"name": "工序", "aliases": ["工序", "工位"], "table": "dim_process", "key": "process_id", "display": "process_name", "kind": "dim"},
    {"name": "设备", "aliases": ["设备", "机器"], "table": "dim_equipment", "key": "equipment_id", "display": "equipment_name", "kind": "dim"},
    {"name": "工单", "aliases": ["工单", "订单", "生产工单"], "table": "mes_work_order", "key": "work_order_id", "display": "work_order_id", "kind": "fact"},
    {"name": "产量记录", "aliases": ["产量", "产出", "工序产量"], "table": "mes_process_output", "key": "output_id", "display": "output_id", "kind": "fact"},
    {"name": "质检记录", "aliases": ["质检", "检验", "抽检"], "table": "qms_inspection", "key": "inspection_id", "display": "inspection_id", "kind": "fact"},
    {"name": "停机记录", "aliases": ["停机", "设备停机"], "table": "eqp_downtime_record", "key": "downtime_id", "display": "downtime_id", "kind": "fact"},
    {"name": "库存", "aliases": ["库存", "库存量"], "table": "inv_inventory_snapshot", "key": "snapshot_id", "display": "snapshot_id", "kind": "fact"},
]


def find_entities(query: str) -> list[dict]:
    """按别名匹配 query 中的实体（最长匹配优先，去重）。"""
    if not query:
        return []
    scored: list[tuple[int, dict]] = []
    for e in ENTITIES:
        best = ""
        for a in e["aliases"]:
            if a and a in query and len(a) > len(best):
                best = a
        if best:
            scored.append((len(best), e))
    scored.sort(key=lambda x: x[0], reverse=True)
    seen: set[str] = set()
    out: list[dict] = []
    for _, e in scored:
        if e["name"] not in seen:
            seen.add(e["name"])
            out.append(e)
    return out


def relation_paths(table_a: str, table_b: str, fk_map: dict, max_hops: int = 2):
    """两实体所属表之间的外键可达路径（复用 join_planner）。"""
    try:
        from agent.join_planner import plan_joins
        return plan_joins([table_a, table_b], fk_map, max_hops=max_hops)["paths"]
    except Exception:
        return []


def _entities_for_tables(tables: set) -> list[dict]:
    """表名集合 → 实体列表（按 ENTITIES 的 table 匹配，保持声明顺序）。"""
    return [e for e in ENTITIES if e["table"] in tables]


def build_ontology_hint(query: str, fk_map: dict | None = None) -> str:
    """生成「业务关系指引」段落（软约束）；无命中实体返回空串。"""
    if fk_map is None:
        try:
            from db.tools import get_foreign_keys
            fk_map = get_foreign_keys()
        except Exception:
            fk_map = {}
    ents = find_entities(query)[:3]
    if not ents:
        # 归因问常不点明实体（如"为什么良率下降"）→ 从命中指标的事实表 + 其外键直达维度表反推实体
        try:
            from agent.metric_registry import find_metrics
            hits = find_metrics(query)
            if hits:
                tables = set(hits[0].get("tables") or [])
                for ft in list(tables):
                    for fk in fk_map.get(ft, []):
                        ref = fk.get("ref_table", "")
                        if ref:
                            tables.add(ref)
                ents = _entities_for_tables(tables)[:3]
        except Exception:
            ents = []
    if not ents:
        return ""

    lines = ["## 业务关系指引（归因分析参考，软约束，非强制执行）"]
    ent_desc = [f"{e['name']}（{e['table']}，主键 {e['key']}，显示 {e['display']}）" for e in ents]
    lines.append("涉及实体：" + "、".join(ent_desc))

    path_lines: list[str] = []
    for i in range(len(ents)):
        for j in range(i + 1, len(ents)):
            for p in relation_paths(ents[i]["table"], ents[j]["table"], fk_map)[:2]:
                chain = " ⋈ ".join(p.tables)
                path_lines.append(f"- {chain}：{'；'.join(p.edges)}")
    if path_lines:
        lines.append("关联路径（只能沿以下外键 JOIN）：")
        lines.extend(path_lines)
    lines.append("建议：按产品/产线/工序/设备/时间维度拆分对比，定位导致指标变化的环节。")
    return "\n".join(lines)
