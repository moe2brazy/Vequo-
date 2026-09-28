# -*- coding: utf-8 -*-
"""语义层自动构建 Agent（P0-1，对齐极昆仑「语义层构建 Agent」：实施周期数周→数天）

## 竞品做法
极昆仑：自动识别库表结构/字段关系/业务逻辑 → 生成候选语义层 → 人工确认（数周→数天）。
Aloudata CAN：自动发现指标线索，人工确认后纳入平台。没有一家敢全自动入库。

## 我们的方案（与 metric_miner 同构：确定性扫描 + LLM 只产 NL + human-in-loop）
1. **确定性扫描**（零 LLM，复用 db.tools）：表清单 → 字段（动态查询，含类型/描述/样例值）
   → 维度表识别（dim_ 前缀或被外键引用）→ 事实表识别（含数值列）→ 数值列聚合候选
   （SUM/AVG/COUNT）→ 时间列标注（time_col，供编译器元数据候选）；
2. **LLM 只做 NL 部分**：为每个候选生成中文名/同义词/单位/口径描述（复用 _batch_naming 提示词体系）；
3. **候选不自动入库**：并入 metric_miner 候选池（source="schema_scan"），指标管理页人工采纳
   → create_user_metric 入库（human-in-loop 二次确认，口径正确性留给人）。

对外 API：
  - scan_schema_candidates(use_llm=True) → 扫描并刷新候选池，返回候选清单
  与 metric_miner.mine_candidates（历史 SQL 源）互补：一个扫「库结构」，一个挖「历史用法」。
"""

import logging

_logger = logging.getLogger("semantic_builder")

# 数值类型白名单（PG/MySQL 通用）
_NUMERIC_TYPES = {
    "integer", "bigint", "smallint", "numeric", "decimal", "real",
    "double precision", "float", "float4", "float8", "int", "int2", "int4", "int8",
}
# 时间类型（time_col 标注）
_TIME_TYPES = {"date", "time", "timestamp", "timestamptz", "datetime", "timestamp without time zone", "timestamp with time zone"}
# 排除不做候选的数值列（主键/外键/序号类）
_SKIP_COL_RE = (
    "id$", "_id$", "code$", "seq$", "no$", "num$", "qty_plan$", "sort", "order",
)


def _is_numeric(ftype: str) -> bool:
    return (ftype or "").strip().lower() in _NUMERIC_TYPES


def _is_time(ftype: str) -> bool:
    t = (ftype or "").strip().lower()
    return t in _TIME_TYPES or t.startswith("timestamp") or t.startswith("datetime")


def _skip_col(name: str) -> bool:
    import re
    n = name.lower()
    return any(re.search(p, n) for p in _SKIP_COL_RE) or n in ("id", "status", "is_planned")


def _norm_expr(expr: str) -> str:
    """表达式归一化（与 metric_miner 同口径，用于去重比对）"""
    import re
    s = re.sub(r"\s+", "", (expr or "")).lower()
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(expr)
        if tree is not None:
            for c in tree.find_all(exp.Column):
                c.set("table", None)
            return re.sub(r"\s+", "", tree.sql()).lower()
    except Exception:
        pass
    return s


def _registered_keys() -> set[tuple]:
    """已注册指标 (归一化表达式, 表集合) 键（与 metric_miner._registered_keys 同逻辑）"""
    keys = set()
    try:
        from agent.metric_registry import get_all_metrics
        for m in get_all_metrics():
            expr = m.get("sql_expression") or m.get("formula") or ""
            if not expr:
                continue
            tbls = tuple(sorted({str(t).split(".")[-1].lower() for t in (m.get("tables") or [])}))
            keys.add((_norm_expr(expr), tbls))
    except Exception:
        pass
    return keys


def _llm_naming(cands: list[dict]) -> None:
    """LLM 批量命名候选（只产 NL：中文名/同义词/单位/口径描述），失败静默。"""
    if not cands:
        return
    try:
        from agent.llm_service import _make_llm, _loads_lenient
        from langchain_core.messages import SystemMessage
    except Exception:
        return
    system = """你是数据治理专家，负责把数据库字段整理成业务指标定义。
为下列每个候选给出：name（中文指标名，2-6 字，业务用语）、aliases（3-5 个中文同义词或常见叫法）、
unit（单位，如 件/元/次/小时/%，无量纲用空字符串）、description（一句话口径说明：哪张表哪个字段、怎么算，40 字内）。
规则：名称必须是业务语言，不能是字段名直译（good_qty →「合格产量」而不是「好数量」）；
同义词覆盖用户真实问法；无法确定单位时用空字符串，不要编造。
只输出 JSON：{"items": [{"idx": 0, "name": "...", "aliases": [...], "unit": "...", "description": "..."}]}"""
    items = []
    for i, c in enumerate(cands):
        items.append(
            "#%d 表：%s｜字段：%s（%s）｜聚合：%s｜样例值：%s"
            % (i, c["tables"][0], c.get("field") or "", c.get("field_type") or "",
               c["expr"], (c.get("sample") or "—")[:20]))
    prompt = f"## 待命名的聚合候选\n{chr(10).join(items)[:3500]}\n\n请按顺序为每个候选输出定义。只输出 JSON。"
    try:
        llm = _make_llm(temp=0.0, max_tokens=1200, json_mode=True)
        raw = str(llm.invoke([SystemMessage(content=system), SystemMessage(content=prompt)]).content or "")
        data = _loads_lenient(raw) or {}
        arr = data.get("items") if isinstance(data, dict) else None
        if not isinstance(arr, list):
            return
        for it in arr:
            if not isinstance(it, dict):
                continue
            try:
                idx = int(it.get("idx"))
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(cands)):
                continue
            c = cands[idx]
            name = str(it.get("name") or "").strip()[:20]
            if name:
                c["name"] = name
            aliases = [str(a).strip()[:20] for a in (it.get("aliases") or []) if str(a).strip()]
            if aliases:
                c["aliases"] = aliases[:6]
            c["unit"] = str(it.get("unit") or "").strip()[:10]
            desc = str(it.get("description") or "").strip()[:160]
            if desc:
                c["description"] = desc
    except Exception:
        pass


def scan_schema_candidates(limit: int = 20, use_llm: bool = True) -> dict:
    """扫描当前库表结构 → 生成候选指标（确定性规则 + LLM 命名）→ 并入指标候选池。

    返回 {success, candidates, total, added, skipped_registered, error}
    """
    try:
        from db.tools import get_real_tables, get_dynamic_table_detail, get_foreign_keys
    except Exception as e:
        return {"success": False, "candidates": [], "total": 0, "added": 0,
                "skipped_registered": 0, "error": f"元数据读取失败: {e}"}

    try:
        tables = get_real_tables()
        fk_map = get_foreign_keys()
    except Exception as e:
        return {"success": False, "candidates": [], "total": 0, "added": 0,
                "skipped_registered": 0, "error": f"库表扫描失败: {e}"}
    if not tables:
        return {"success": False, "candidates": [], "total": 0, "added": 0,
                "skipped_registered": 0, "error": "当前数据库没有可扫描的表"}

    # 维度表识别：dim_ 前缀 或被外键引用（ref_table）
    referenced = {fk.get("ref_table") for fks in fk_map.values() for fk in (fks or [])}
    children = set(fk_map.keys())
    registered = _registered_keys()

    cands: list[dict] = []
    seen: set[str] = set()
    for t in tables:
        name = t["table_name"]
        try:
            detail = get_dynamic_table_detail(name) or {}
        except Exception:
            detail = {}
        fields = detail.get("fields") or []
        numeric_cols = [f for f in fields if _is_numeric(str(f.get("type") or "")) and not _skip_col(str(f.get("name") or ""))]
        time_cols = [f for f in fields if _is_time(str(f.get("type") or ""))]
        is_fact = bool(numeric_cols) and (name in children or not referenced)
        if not is_fact:
            continue  # 无数值列 / 纯维度表 → 跳过（维度由编译器 _FACT_META 另行管理）

        for f in numeric_cols[:8]:
            col = f["name"]
            for agg, label in (("SUM", "合计"), ("AVG", "平均")):
                expr = f"{agg}({col})"
                key = (_norm_expr(expr), tuple(sorted({name.split('.')[-1].lower()})))
                if key in registered or key in seen:
                    continue
                seen.add(key)
                cands.append({
                    "expr": expr,
                    "field": col,
                    "field_type": str(f.get("type") or ""),
                    "sample": str(f.get("sample") or "")[:60],
                    "table": name,
                    "tables": [name],
                    "time_col": (time_cols[0]["name"] if time_cols else ""),
                    "name": f"{label}({col})", "aliases": [], "unit": "",
                    "description": f"来自表 {name} 的 {col} 字段（自动扫描候选）",
                    "source": "schema_scan",
                })
                if len(cands) >= limit * 3:
                    break
            if len(cands) >= limit * 3:
                break

    if not cands:
        return {"success": True, "candidates": [], "total": 0, "added": 0,
                "skipped_registered": len(registered), "error": "未发现新的候选指标（可能都已注册）"}

    # LLM 补 NL（中文名/同义词/单位/口径描述）
    if use_llm:
        try:
            _llm_naming(cands[:limit])
        except Exception:
            pass

    # 并入 metric_miner 候选池（human-in-loop：管理页人工采纳后才入库）
    added = 0
    try:
        from agent.metric_miner import add_schema_candidates
        added = add_schema_candidates(cands[:limit])
    except Exception as e:
        _logger.warning("候选入池失败: %s", e)
        return {"success": False, "candidates": cands[:limit], "total": len(cands),
                "added": 0, "skipped_registered": len(registered), "error": f"候选入池失败: {e}"}

    try:
        from agent.metric_miner import list_candidates
        pool = [c for c in list_candidates() if c.get("source") == "schema_scan"]
    except Exception:
        pool = cands[:limit]

    return {"success": True, "candidates": pool, "total": len(pool),
            "added": added, "skipped_registered": len(registered), "error": ""}
