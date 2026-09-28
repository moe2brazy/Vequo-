# -*- coding: utf-8 -*-
"""盲点发现（P1-2，对标 FineBI「盲点发现」）：主动提示"有数据但从没被问过"的地方。

## 竞品做法
FineBI 的盲点发现解决的是"用户不知道自己不知道什么"——引导问题只能覆盖用户想得到的
方向，而盲点发现从**数据资产 vs 查询行为**的差集里找线索：库里有、但从没人问过。

## 本实现找三类盲点（全部确定性，不靠 LLM 猜）
1. **未探索的表**：数据量大（说明是业务重点）却从没被任何历史查询碰过的表；
2. **未拆解的维度**：常被查的表，某些分类字段从未出现在 GROUP BY 里
   （用户一直在看总量，没按这个维度拆过）；
3. **未问津的度量**：表里的数值字段从未被聚合过（一直看 A 指标，没看过 B 指标）。

差集怎么算：历史 SQL（memory 的 sql_examples）用 sqlglot 确定性解析出
「查过哪些表 / GROUP BY 过哪些列 / 聚合过哪些列」，再与库表元数据的字段做差集。
没解析出信息的 SQL 不影响结果（差集只会偏保守，不会误报）。

对外 API：find_blind_spots(limit) → {success, spots, error}
"""

from __future__ import annotations

import logging
import time

from agent.suggest import (
    _CATEGORICAL_TYPES,
    _NUMERIC_TYPES,
    _friendly_label,
    _get_table_fields,
    _is_id_field,
    _norm_type,
    _table_label,
)

_logger = logging.getLogger("blind_spot")

_cache: dict = {}
_CACHE_TTL = 600  # 10 分钟（盲点变化慢，没必要频繁重算）


def _historical_sql(limit: int = 300) -> list[str]:
    """取历史成功查询的 SQL 文本。"""
    try:
        from agent.memory import get_memory
        rows = (get_memory().list_memories() or {}).get("sql_examples") or []
        return [str(r.get("sql") or "") for r in rows[:limit] if isinstance(r, dict) and r.get("sql")]
    except Exception:
        return []


def _parse_usage(sqls: list[str]) -> tuple[dict[str, int], set[str], set[str]]:
    """解析历史 SQL 的使用情况。

    返回 (表命中次数, GROUP BY 过的列名集合, 聚合函数内的列名集合)。
    列名取**裸名**（去掉表别名前缀），与元数据字段名可比。
    """
    table_hits: dict[str, int] = {}
    grouped: set[str] = set()
    measured: set[str] = set()
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:
        return table_hits, grouped, measured

    for sql in sqls:
        tree = None
        for d in ("", "postgres", "mysql"):
            try:
                tree = sqlglot.parse_one(sql, read=(d or None))
                if tree is not None:
                    break
            except Exception:
                continue
        if tree is None:
            continue
        for t in tree.find_all(exp.Table):
            nm = t.name
            if nm:
                table_hits[nm.lower()] = table_hits.get(nm.lower(), 0) + 1
        grp = tree.args.get("group")
        if grp is not None:
            for e in (grp.expressions or []):
                for col in e.find_all(exp.Column):
                    if col.name:
                        grouped.add(col.name.lower())
        for agg in tree.find_all(exp.AggFunc):
            if isinstance(agg, exp.Window):
                continue
            for col in agg.find_all(exp.Column):
                if col.name:
                    measured.add(col.name.lower())
    return table_hits, grouped, measured


def _pick_measure(fields: list[dict]) -> dict | None:
    """挑一个值得推荐的数值字段（排除 ID/外键）。"""
    cands = [f for f in fields
             if _norm_type(str(f.get("type") or "").lower().strip()) in _NUMERIC_TYPES
             and not _is_id_field(f)]
    if not cands:
        return None
    # 命中业务词的优先（产量/数量/金额/时长…）
    for f in cands:
        text = f"{f.get('name', '')} {f.get('description', '')}".lower()
        if any(w in text for w in ("qty", "数量", "产量", "amount", "金额", "时长", "minutes", "count")):
            return f
    return cands[0]


def _pick_dim(fields: list[dict], exclude: set[str]) -> dict | None:
    """挑一个"还没被拆解过"的分类字段。"""
    cands = [f for f in fields
             if _norm_type(str(f.get("type") or "").lower().strip()) in _CATEGORICAL_TYPES
             and str(f.get("name") or "").lower() not in exclude
             and not _is_id_field(f)]
    if not cands:
        return None
    for f in cands:
        text = f"{f.get('name', '')} {f.get('description', '')}".lower()
        if any(w in text for w in ("name", "名称", "status", "状态", "type", "类型",
                                   "category", "类别", "line", "产线", "工序", "产品")):
            return f
    return cands[0]


def find_blind_spots(limit: int = 5, min_rows: int = 1) -> dict:
    """找出数据盲点。返回 {success, spots, error}。

    spot: {type, table, label, detail, suggestion, severity}
      type ∈ {unexplored_table, unused_dimension, unused_measure}

    关于 min_rows：盲点的核心信号是"有数据但从没被问过"，行数只用于**排序优先级**
    而非过滤条件，所以默认只要有数据（>0）就纳入考察。用绝对行数阈值（如 50 行）
    做过滤会让小库（演示库单表仅几行）一个盲点都出不来。
    """
    try:
        now = time.time()
        cached = _cache.get("data")
        if cached and now - cached["time"] < _CACHE_TTL:
            return cached["data"]

        try:
            from db.tools import get_all_tables
            tables = get_all_tables()
        except Exception:
            tables = []
        if not tables:
            return {"success": False, "spots": [], "error": "未取到表清单"}

        try:
            from db.executor import get_table_row_counts
            counts = get_table_row_counts()
        except Exception:
            counts = {}

        table_hits, grouped, measured = _parse_usage(_historical_sql())
        # 严重度按「本库最大表的相对规模」计算：绝对行数在不同库之间不可比
        # （演示库单表几行、生产库单表千万行），相对值才能保证排序在各种库上都合理。
        _all_counts = [counts.get(str(t.get("table_name") or "")) or t.get("row_count") or 0
                       for t in tables]
        max_rows = max(_all_counts) if _all_counts else 0
        max_rows = max(max_rows, 1)

        spots: list[dict] = []
        for t in tables:
            name = str(t.get("table_name") or "")
            if not name:
                continue
            bare = name.split(".")[-1].lower()
            rows = counts.get(name) or t.get("row_count") or 0
            if rows < min_rows:
                continue
            scale = rows / max_rows   # 0~1，本库内的相对规模
            label = _table_label(t)
            hit = table_hits.get(bare, 0)
            fields = _get_table_fields(t) or []
            fields_lc = {str(f.get("name") or "").lower() for f in fields}

            # ① 整表从没被查过（表里有数据、却没人问过 → 最值得提示）
            if hit == 0:
                m = _pick_measure(fields)
                d = _pick_dim(fields, set())
                q = None
                if d and m:
                    q = f"按{_friendly_label(d)}统计{label}的{_friendly_label(m)}"
                elif m:
                    q = f"统计{label}的{_friendly_label(m)}"
                elif d:
                    q = f"查看{label}按{_friendly_label(d)}的分布"
                spots.append({
                    "type": "unexplored_table",
                    "table": name,
                    "label": label,
                    "rows": rows,
                    "detail": f"{label}有 {rows:,} 行数据，但从未被查询过",
                    "suggestion": q,
                    "severity": round(scale * 3.0, 2) + 1.0,
                })
                continue  # 整表都没查过，不必再拆到字段级

            # ② / ③ 表被查过，但某些维度/度量从没用过
            used_dims = grouped & fields_lc
            used_meas = measured & fields_lc

            dim = _pick_dim(fields, used_dims)
            if dim:
                m = _pick_measure(fields)
                if m:
                    spots.append({
                        "type": "unused_dimension",
                        "table": name,
                        "label": label,
                        "rows": rows,
                        "detail": f"{label}常被查询，但从未按「{_friendly_label(dim)}」拆解过",
                        "suggestion": f"按{_friendly_label(dim)}统计{label}的{_friendly_label(m)}",
                        "severity": round(scale * 2.0, 2) + 0.5,
                    })

            unused_meas = [f for f in fields
                           if _norm_type(str(f.get("type") or "").lower().strip()) in _NUMERIC_TYPES
                           and not _is_id_field(f)
                           and str(f.get("name") or "").lower() not in used_meas]
            if unused_meas:
                f0 = unused_meas[0]
                d2 = _pick_dim(fields, set())
                spots.append({
                    "type": "unused_measure",
                    "table": name,
                    "label": label,
                    "rows": rows,
                    "detail": f"{label}的「{_friendly_label(f0)}」从未被统计过",
                    "suggestion": (f"统计{label}的{_friendly_label(f0)}"
                                   + (f"（按{_friendly_label(d2)}）" if d2 else "")),
                    "severity": round(scale * 1.5, 2) + 0.2,
                })

        spots.sort(key=lambda x: x["severity"], reverse=True)
        data = {"success": True, "spots": spots[:limit], "error": ""}
        _cache["data"] = {"time": now, "data": data}
        return data
    except Exception as e:
        _logger.warning("盲点发现失败: %s", e)
        return {"success": False, "spots": [], "error": str(e)[:160]}


def invalidate_cache() -> None:
    """历史查询变化后清缓存（如新增训练 SQL）。"""
    _cache.pop("data", None)
