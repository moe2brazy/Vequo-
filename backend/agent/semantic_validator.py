"""语义模型离线校验器 — 指标定义的结构 / 引用完整性 / 血缘闭环 / 口径冲突校验

纯函数、无副作用，供 CI、管理后台「指标入库前校验」或命令行离线检查使用。
校验结果分级：errors（必须修复）/ warnings（建议关注）。

用法：
    from agent.semantic_validator import validate_registry
    report = validate_registry()          # 校验当前全部指标
    report = validate_metric(metric, known_tables={...})   # 校验单个指标
"""

from __future__ import annotations

import json
import re
from datetime import date


# SQL 关键字 / 函数名（列名提取时排除，避免把 SUM/COUNT 等误判为列）
_SQL_NOISE = {
    "sum", "count", "avg", "min", "max", "nullif", "case", "when", "then",
    "else", "end", "as", "and", "or", "not", "distinct", "round", "coalesce",
    "cast", "null", "true", "false", "in", "is", "over", "partition", "by",
    "interval", "to_char", "date_trunc", "current_date", "select", "from",
    "where", "group", "order", "having", "join", "on", "like", "between",
}

_COL_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\b", re.IGNORECASE)


def _parse_date(s: str) -> date | None:
    try:
        s = (s or "").strip()
        if not s:
            return None
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _extract_columns(expr: str) -> set[str]:
    """从口径 SQL 表达式里启发式提取列名（去掉函数名、关键字、表别名前缀）。"""
    # 先剥离表别名前缀：`t.good_qty` → `good_qty`。若不剥离，_COL_RE 会把别名 `t` 与列名
    # `good_qty` 都提取出来，`t` 会被误判为「未知列」导致合法指标（如 SUM(t.good_qty)）入库校验失败。
    normalized = re.sub(r"\b[a-z_][a-z0-9_]*\.([a-z_][a-z0-9_]*)", r"\1", expr or "", flags=re.IGNORECASE)
    cols: set[str] = set()
    for m in _COL_RE.findall(normalized):
        w = m.lower()
        if w in _SQL_NOISE:
            continue
        cols.add(w)
    # 去掉紧跟 "(" 的函数名
    return {c for c in cols if not re.search(re.escape(c) + r"\s*\(", normalized, re.I)}


def _bare(t: str) -> str:
    return str(t).split(".")[-1].strip().lower()


def validate_metric(metric: dict, known_tables: set[str] | None = None,
                    known_columns: set[str] | None = None) -> list[str]:
    """校验单个指标定义，返回问题列表（空列表 = 通过）。"""
    errors: list[str] = []
    if not isinstance(metric, dict):
        return ["指标定义必须是对象"]
    name = str(metric.get("name") or "").strip()
    if not name:
        return ["缺少必填字段 name"]
    expr = str(metric.get("sql_expression") or "").strip()
    if not expr:
        errors.append(f"指标「{name}」缺少 sql_expression")
    tables = metric.get("tables") or []
    if not tables:
        errors.append(f"指标「{name}」缺少 tables（至少一张事实表）")
    elif known_tables is not None:
        known = {_bare(x) for x in known_tables}
        for t in tables:
            if _bare(t) not in known:
                errors.append(f"指标「{name}」引用未知表「{t}」")
    # 日期格式
    for k in ("valid_from", "valid_to"):
        v = metric.get(k)
        if v and _parse_date(str(v)) is None:
            errors.append(f"指标「{name}」的 {k} 不是合法日期：{v}")
    # 列引用（仅当提供列清单时校验，启发式）
    if known_columns is not None and expr:
        cols = _extract_columns(expr)
        known_cols = {c.lower() for c in known_columns}
        for c in cols:
            if c not in known_cols:
                errors.append(f"指标「{name}」的口径引用未知列「{c}」")
    return errors


def _overlap(a: dict, b: dict) -> bool:
    """两个同名指标的有效区间是否重叠（缺省边界视为 -∞ / +∞）。"""
    a_s = _parse_date(str(a.get("valid_from") or "")) or date.min
    a_e = _parse_date(str(a.get("valid_to") or "")) or date.max
    b_s = _parse_date(str(b.get("valid_from") or "")) or date.min
    b_e = _parse_date(str(b.get("valid_to") or "")) or date.max
    return a_s < b_e and b_s < a_e


def _detect_cycle(deps: dict[str, list[str]]) -> list[list[str]]:
    """检测 depends_on 图中的环，返回环列表。"""
    cycles: list[list[str]] = []
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    stack: list[str] = []

    def dfs(n: str):
        color[n] = GRAY
        stack.append(n)
        for d in deps.get(n, []):
            if d not in deps:
                continue
            c = color.get(d, WHITE)
            if c == WHITE:
                dfs(d)
            elif c == GRAY:
                try:
                    i = stack.index(d)
                    cycles.append(stack[i:] + [d])
                except ValueError:
                    pass
        stack.pop()
        color[n] = BLACK

    for n in deps:
        if color.get(n, WHITE) == WHITE:
            dfs(n)
    return cycles


def validate_registry(metrics: list[dict] | None = None,
                      known_tables: set[str] | None = None,
                      known_columns: set[str] | None = None) -> dict:
    """全量校验指标清单，返回 {"count", "errors", "warnings"}。

    - errors：结构缺失 / 引用未知表列 / 同名版本区间重叠 / 循环依赖
    - warnings：派生依赖悬挂 / 不同名共享同一口径 SQL（疑似重复）
    """
    if metrics is None:
        from agent.metric_registry import get_all_metrics
        metrics = get_all_metrics()

    errors: list[dict] = []
    warnings: list[dict] = []
    # 同名指标可能有多版本（按季度调整口径），需保存全部历史版本逐一比对区间重叠——
    # 若只存最近一个，第 3 个版本只与第 2 个比较，第 1 与第 3 的重叠会漏报。
    name_seen: dict[str, list[dict]] = {}

    for m in metrics:
        n = m.get("name")
        for e in validate_metric(m, known_tables=known_tables, known_columns=known_columns):
            errors.append({"metric": n, "error": e})
        if n in name_seen and any(_overlap(prev, m) for prev in name_seen[n]):
            errors.append({"metric": n, "error": "同名指标存在重叠的生效版本区间"})
        name_seen.setdefault(n, []).append(m)

    # 血缘闭环：悬挂引用 + 环
    name_set = {m.get("name") for m in metrics}
    deps_graph: dict[str, list[str]] = {}
    for m in metrics:
        deps = [d for d in (m.get("depends_on") or []) if d]
        deps_graph[str(m.get("name"))] = deps
        for d in deps:
            if d not in name_set:
                warnings.append({"metric": m.get("name"),
                                 "warning": f"派生依赖指标「{d}」不存在（悬挂引用）"})
    for cycle in _detect_cycle(deps_graph):
        errors.append({"metric": " → ".join(cycle), "error": "指标血缘存在循环依赖"})

    # 口径冲突：不同名但共享同一口径 SQL
    expr_map: dict[str, list[str]] = {}
    for m in metrics:
        expr = (m.get("sql_expression") or "").strip()
        if expr:
            expr_map.setdefault(expr.lower(), []).append(str(m.get("name")))
    for expr, names in expr_map.items():
        uniq = sorted(set(names))
        if len(uniq) > 1:
            warnings.append({"metric": uniq,
                             "warning": f"多个指标共享同一口径 SQL：{uniq}"})

    return {"count": len(metrics), "errors": errors, "warnings": warnings}


def known_tables_from_datasets() -> set[str]:
    """从权限模型的数据集定义收集全部已知表（裸名，小写）。"""
    try:
        from security import model as pm
        tables: set[str] = set()
        for d in pm.list_datasets():
            for t in (d.get("tables") or []):
                tables.add(_bare(t))
        return tables
    except Exception:
        return set()


if __name__ == "__main__":
    report = validate_registry(known_tables=known_tables_from_datasets())
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
