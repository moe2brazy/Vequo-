"""结果溯源（血缘）—— 对标 Genloop Living Context Graph / Smartbi 全链路溯源。

确定性实现（符合架构约定：不靠 LLM 现编）：对最终执行的 SQL 做静态解析
（sqlglot，项目已依赖），为每个结果列标注：
  - 输出列名（AS 别名）
  - 来源表达式（expr）
  - 来源表（真实表名 + 中文名 table_alias）
  - 来源字段（字段名 + 元数据描述）
  - 聚合方式（SUM/COUNT/AVG/...，无聚合为空）
解析失败返回空结构（前端隐藏溯源区，不影响结果展示）。

健壮性约定：
  - 多表 JOIN 裸列归属：字段名唯一命中某表才归属；多表都有同名字段（歧义）
    留空，不瞎猜（宁可显示"常量/表达式"也不标错来源）。
  - 派生表/CTE 投影别名（SELECT d FROM (SELECT stat_date AS d ...) s）可回溯，
    避免把外层别名列错标成真实表的同名/错名来源。
  - CTE 引用不当作真实表展示（不产生幽灵表 chip）。
"""

from __future__ import annotations

def _sql_dialect() -> str:
    """sqlglot 生成用的方言（与当前库一致）。

    2026-09-14：`.sql()` 不传方言时 sqlglot 用默认方言生成，遇到
    `TO_CHAR(col, 'YYYY-MM')` 会打印
    "Argument 'format' is not supported for expression 'ToChar' when targeting Dialect."
    —— 这是**日志噪音**（血缘标注只取展示字符串，结果不受影响），但极易被误读成执行报错，
    排查时白费时间（实测 65 题日志里出现 7 次）。显式传方言即可消除。
    """
    try:
        from db.tools import get_db_type
        return "mysql" if str(get_db_type()).lower() == "mysql" else "postgres"
    except Exception:
        try:
            from agent.metric_compiler import get_db_type
            return "mysql" if str(get_db_type()).lower() == "mysql" else "postgres"
        except Exception:
            return "postgres"



import functools

_AGG_FUNC_NAMES = ("Sum", "Count", "Avg", "Max", "Min", "ArrayAgg", "StringAgg")

# ── 算子级血缘（P2-4，对标 Aloudata BIG）─────────────────────────
# 列级血缘回答「字段从哪张表来、经过什么聚合」；算子级血缘进一步回答
# 「这个结果列由哪些算子一步步算出来」——把 ROUND(SUM(a*b)/COUNT(c),2) 展开成
# ROUND → DIV → [SUM → MUL(a,b), COUNT(c)] 的算子链。
# 价值：审计口径时能看清每一步计算（尤其复合指标、比率、CASE 分档），
# 而不是只看到一个 SUM/COUNT 标签。

# 二元运算 → 可读符号
_BINARY_OPS = {"Add": "+", "Sub": "-", "Mul": "*", "Div": "/", "Mod": "%",
               "Pow": "^", "And": "AND", "Or": "OR", "EQ": "=", "NEQ": "!=",
               "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}


def _op_tree(node) -> dict:
    """把表达式节点递归转成算子树。

    叶子：{"kind": "col", "name", "table"} 或 {"kind": "lit", "val"}
    内部：{"kind": "op", "op": 算子名, "args": [子节点...]}
    解析不了就退化为字面量，保证不抛异常。
    """
    try:
        from sqlglot import exp
    except Exception:
        return {"kind": "lit", "val": str(node)[:40]}
    if node is None:
        return {"kind": "lit", "val": ""}
    if isinstance(node, exp.Column):
        return {"kind": "col", "name": node.name or "", "table": node.table or ""}
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null, exp.Star)):
        return {"kind": "lit", "val": str(node)}
    # 聚合函数
    for fn in _AGG_FUNC_NAMES:
        cls = getattr(exp, fn, None)
        if cls is not None and isinstance(node, cls):
            args = ([_op_tree(node.this)] if node.this is not None else []) + \
                   [_op_tree(a) for a in (node.expressions or [])]
            return {"kind": "op", "op": fn.upper(), "args": args}
    # CAST：算子名记 CAST，参数为被转换的表达式（类型在 label 里可读性差，略去）
    if isinstance(node, exp.Cast) and node.this is not None:
        return {"kind": "op", "op": "CAST", "args": [_op_tree(node.this)]}
    # 二元运算
    for py, sym in _BINARY_OPS.items():
        cls = getattr(exp, py, None)
        if cls is not None and isinstance(node, cls):
            return {"kind": "op", "op": sym,
                    "args": [_op_tree(node.this), _op_tree(node.expression)]}
    # CASE WHEN：算子名 CASE，参数为各分支（含默认值）
    if isinstance(node, exp.Case):
        args = []
        for w in (node.args.get("ifs") or []):
            args.append(_op_tree(w))
        if node.args.get("default") is not None:
            args.append(_op_tree(node.args["default"]))
        return {"kind": "op", "op": "CASE", "args": args}
    # 通用函数（ROUND/COALESCE/NULLIF/If/窗口函数…）：
    # 注意 sqlglot 把第一个参数放在 node.this，其余在 node.expressions，
    # 只取 expressions 会漏掉首参（实测 ROUND(SUM(..),2) 曾解析成空 Round()）。
    # 具名参数（ROUND 的 decimals、If 的 true/false、SUBSTRING 的 start/length 等）
    # 存在 node.args 里，也要带上，否则 ROUND(..,2) 会丢精度位、If 会丢分支。
    if isinstance(node, exp.Func):
        args = ([_op_tree(node.this)] if node.this is not None else []) + \
               [_op_tree(a) for a in (node.expressions or [])]
        for key, val in (node.args or {}).items():
            if key in ("this", "expressions") or val is None:
                continue
            if isinstance(val, (list, tuple)):
                args.extend(_op_tree(v) for v in val)
            else:
                args.append(_op_tree(val))
        return {"kind": "op", "op": type(node).__name__, "args": args}
    # 其他表达式：尽力取子表达式
    try:
        children = [c for c in node.iter_expressions()]
    except Exception:
        children = []
    if children:
        return {"kind": "op", "op": type(node).__name__, "args": [_op_tree(c) for c in children]}
    return {"kind": "lit", "val": str(node)[:40]}


def _op_chain(tree: dict) -> str:
    """把算子树压平成可读的算子链字符串（如 ROUND(DIV(SUM(MUL(good_qty,unit_price)),COUNT(order_id)),2)）。"""
    if tree.get("kind") == "col":
        return tree.get("name") or ""
    if tree.get("kind") == "lit":
        return tree.get("val") or ""
    args = [_op_chain(a) for a in (tree.get("args") or [])]
    return "%s(%s)" % (tree.get("op", "?"), ", ".join(a for a in args if a))


def build_operator_lineage(sql: str, dialect: str = "") -> dict:
    """算子级血缘：在 build_lineage 基础上，为每个结果列追加 ops（算子树）与 chain（算子链）。

    返回结构与 build_lineage 完全兼容（tables/columns），每个 column 多两个字段：
      ops:   {"kind":"op","op":"DIV","args":[...]}  递归算子树（前端可折叠渲染）
      chain: "ROUND(DIV(SUM(MUL(good_qty,unit_price)),COUNT(order_id)),2)"  可读算子链
    """
    out = build_lineage(sql, dialect=dialect)
    if not out.get("columns"):
        return out
    try:
        import sqlglot
        from sqlglot import exp
        ast = None
        for d in ([dialect] if dialect else ["postgres", "mysql"]):
            if not d:
                continue
            try:
                ast = sqlglot.parse_one(sql, read=d)
                break
            except Exception:
                continue
        if ast is None:
            return out
        select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
        exprs = (select.args.get("expressions") or []) if select is not None else []
        # 跳过 * 投影（与 build_lineage 的 columns 顺序对齐：* 与 Alias 会错位，
        # 故按表达式顺序取非 * 的投影，与 columns 里非 * 项一一对应）
        col_idx = 0
        for e in exprs:
            if isinstance(e, exp.Star):
                continue
            if col_idx >= len(out["columns"]):
                break
            inner = e.this if isinstance(e, exp.Alias) else e
            tree = _op_tree(inner)
            out["columns"][col_idx]["ops"] = tree
            out["columns"][col_idx]["chain"] = _op_chain(tree)
            col_idx += 1
    except Exception:
        pass
    return out


@functools.lru_cache(maxsize=1)
def _table_meta() -> dict:
    try:
        from db.metadata import TABLES
        out = {}
        for t in TABLES:
            if isinstance(t, dict) and t.get("table_name"):
                out[t["table_name"]] = t
        return out
    except Exception:
        return {}


def _column_cn(table_name: str, field: str, field_desc: str = "") -> str:
    """结果列的中文短名（表头翻译用，如 rework_qty → 返工数量）。

    完全确定性：只查字段词典 / 内置语义 / 元数据描述，查不到返回空串
    （空串由上层 enrich_lineage_labels 决定是否用 AI 补全，保持本模块零 LLM 依赖）。
    """
    try:
        from agent.field_semantics import field_label
        return field_label(table_name, field, field_desc)
    except Exception:
        return ""


def _field_desc(table_name: str, field: str, meta: dict) -> str:
    if not field or not table_name:
        return ""
    t = meta.get(table_name) or meta.get(table_name.split(".")[-1])
    if not t:
        return ""
    for f in t.get("fields") or []:
        if isinstance(f, dict) and f.get("name") == field:
            return str(f.get("description") or "")
    return ""


def _table_meta_of(name: str, meta: dict) -> dict | None:
    """取表元数据（兼容 schema.table 写法）。"""
    return meta.get(name) or meta.get(name.split(".")[-1]) or None


def _resolve_projection(inner, tables: list[dict], table_by_alias: dict,
                        meta: dict, sub_maps: dict) -> dict:
    """解析单个投影表达式 → {field, tbl_alias, real_table, agg}（不含 output/label）。

    tables: 该 SELECT 作用域内的真实表清单（含别名）
    sub_maps: 派生表/CTE 投影别名 → 内部来源映射（用于回溯外层裸列）
    """
    from sqlglot import exp
    agg, agg_node = "", None
    for fn_name in _AGG_FUNC_NAMES:
        fn_cls = getattr(exp, fn_name, None)
        if fn_cls is None:
            continue
        node = inner.find(fn_cls)
        if node is not None:
            agg, agg_node = fn_name.upper(), node
            break
    # 列引用：聚合场景优先在聚合函数参数内找（避免 SUM(a)+SUM(b) 等复合表达式取到范围外的列）
    if agg_node is not None:
        col_refs = list(agg_node.find_all(exp.Column))
    else:
        col_refs = list(inner.find_all(exp.Column))
    field, tbl_alias = "", ""
    for c in col_refs:
        field, tbl_alias = c.name, c.table or ""
        break
    # COUNT(*) / COUNT(1) / 无列引用的聚合 → 字段记为 *（前端显示"全表计数"）
    if not field and agg:
        field = "*"

    single_table = tables[0]["name"] if len(tables) == 1 else ""
    if tbl_alias:
        # 带前缀：别名 → 真实表；别名找不到（如引用派生表别名）则保留原名，
        # 后续 label 匹配失败即显示原名，不强行归属
        real_table = table_by_alias.get(tbl_alias, tbl_alias)
    elif single_table:
        # 单表：字段确在该表存在才直接归属；否则可能是外层引用派生表/CTE 的
        # 投影别名（SELECT d FROM (SELECT stat_date AS d ...) s），回溯子查询映射
        t_meta = _table_meta_of(single_table, meta)
        if t_meta and field and any(f.get("name") == field for f in (t_meta.get("fields") or [])):
            real_table = single_table
        else:
            sub = sub_maps.get(field) if field else None
            if sub:
                real_table, field = sub["table"], sub["field"]
                if not agg and sub.get("agg"):
                    agg = sub["agg"]
            else:
                real_table = single_table  # 未知字段兜底归该表（能执行成功的 SQL 字段必存在）
    elif field:
        # 多表裸列：字段名在真实表中的唯一命中才归属（歧义留空，不瞎猜）
        matches = []
        for t in tables:
            t_meta = _table_meta_of(t["name"], meta)
            if t_meta and any(f.get("name") == field for f in (t_meta.get("fields") or [])):
                matches.append(t["name"])
        if len(matches) == 1:
            real_table = matches[0]
        elif not matches:
            # 真实表无此字段：尝试经派生表/CTE 投影别名回溯
            # （SELECT d FROM (SELECT stat_date AS d ...) s → d 真实来源 stat_date）
            sub = sub_maps.get(field)
            if sub:
                real_table, field = sub["table"], sub["field"]
                if not agg and sub.get("agg"):
                    agg = sub["agg"]
            else:
                real_table = ""
        else:
            real_table = ""
    else:
        real_table = ""
    return {"field": field, "tbl_alias": tbl_alias, "real_table": real_table, "agg": agg}


def _subquery_alias_maps(ast) -> dict:
    """收集所有派生表/CTE 的投影别名 → 内部来源列映射（用于外层裸列回溯）。

    例: SELECT d, total FROM (SELECT stat_date AS d, SUM(good_qty) AS total
                              FROM mes_process_output GROUP BY stat_date) s
    → {"d": {"table": "mes_process_output", "field": "stat_date", "agg": ""},
       "total": {"table": "mes_process_output", "field": "good_qty", "agg": "SUM"}}
    同名别名后写覆盖前写（近似）；子查询内部表作用域独立解析。
    """
    out: dict[str, dict] = {}
    try:
        from sqlglot import exp
        meta = _table_meta()
        for sub in ast.find_all(exp.Subquery):
            sel = sub.this if isinstance(sub.this, exp.Select) else (
                sub.this.find(exp.Select) if sub.this is not None else None)
            if sel is None:
                continue
            # 子查询内部真实表清单
            sub_tables, seen = [], set()
            for t in sel.find_all(exp.Table):
                full = ".".join(x for x in (t.catalog, t.db, t.name) if x) or t.name
                key = full.lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                sub_tables.append({"name": full, "alias": t.alias or ""})
            s_by_alias = {t["alias"] or t["name"]: t["name"] for t in sub_tables}
            for e in sel.args.get("expressions") or []:
                if isinstance(e, exp.Star):
                    continue
                key = str(e.alias or "") if isinstance(e, exp.Alias) else (
                    getattr(e, "output_name", None) or e.sql(dialect=_sql_dialect()) or "")
                if not key:
                    continue
                inner = e.this if isinstance(e, exp.Alias) else e
                r = _resolve_projection(inner, sub_tables, s_by_alias, meta, {})
                out[key] = {"table": r["real_table"], "field": r["field"], "agg": r["agg"]}
    except Exception:
        pass
    return out


def build_lineage(sql: str, dialect: str = "") -> dict:
    """解析 SQL 生成血缘结构：{"tables": [...], "columns": [...]}。

    tables:  [{name, alias, label(中文名)}]
    columns: [{output, expr, table, table_label, field, field_desc, agg}]
    dialect: 显式指定 sqlglot 方言（如 "mysql"），为空时依次尝试 postgres/mysql
             自动探测（MySQL 库的反引号 SQL 用 postgres 方言解析会失败）。
    解析失败返回空结构（前端据此隐藏溯源区）。
    """
    out = {"tables": [], "columns": []}
    if not sql or not sql.strip():
        return out
    try:
        import sqlglot
        from sqlglot import exp

        # 方言探测：显式指定 > 逐个尝试（postgres 优先，MySQL 反引号语法兜底）
        ast = None
        for d in ([dialect] if dialect else ["postgres", "mysql"]):
            if not d:
                continue
            try:
                ast = sqlglot.parse_one(sql, read=d)
                break
            except Exception:
                continue
        if ast is None:
            return out

        # CTE 名集合：过滤 find_all(Table) 中的 CTE 引用，避免幽灵表 chip
        cte_names = set()
        for c in ast.find_all(exp.CTE):
            try:
                cte_names.add(str(c.alias).lower())
            except Exception:
                pass

        # ── 1. 表清单（含别名），去重保序；CTE 引用过滤 ──
        tables: list[dict] = []
        seen: set[str] = set()
        for t in ast.find_all(exp.Table):
            # 完整表名（含 schema）：factory.employee 不能只取 employee
            full = ".".join(x for x in (t.catalog, t.db, t.name) if x) or t.name
            key = full.lower()
            if not key or key in seen or key in cte_names:
                continue
            seen.add(key)
            tables.append({"name": full, "alias": t.alias or ""})
        meta = _table_meta()
        # 别名 → 真实表名映射（FROM mes_process_output mp → mp → mes_process_output）
        table_by_alias: dict[str, str] = {}
        for t in tables:
            t_meta = _table_meta_of(t["name"], meta)
            t["label"] = (t_meta or {}).get("table_alias") or t["name"]
            table_by_alias[t["alias"] or t["name"]] = t["name"]
        out["tables"] = tables

        # 派生表/CTE 投影别名映射（回溯用）
        sub_maps = _subquery_alias_maps(ast)

        # ── 2. 投影列解析 ──
        select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
        exprs = (select.args.get("expressions") or []) if select is not None else []
        single_table = tables[0]["name"] if len(tables) == 1 else ""
        for e in exprs:
            if isinstance(e, exp.Star):
                out["columns"].append({
                    "output": "*", "expr": "*",
                    "table": single_table,
                    "table_label": (tables[0]["label"] if single_table else ""),
                    "field": "*", "field_desc": "", "agg": "", "cn": "",
                })
                continue
            if isinstance(e, exp.Alias):
                output = str(e.alias or "")
                inner = e.this
            else:
                output = (getattr(e, "output_name", None) or e.sql(dialect=_sql_dialect()) or "")[:60]
                inner = e
            r = _resolve_projection(inner, tables, table_by_alias, meta, sub_maps)
            label = ""
            for t in tables:
                if (t["alias"] or t["name"]) == (r["tbl_alias"] or r["real_table"]) \
                        or t["name"] == r["real_table"]:
                    label = t["label"]
                    break
            _desc = _field_desc(r["real_table"], r["field"], meta)
            out["columns"].append({
                "output": output,
                "expr": (inner.sql(dialect=_sql_dialect()) or "")[:200],
                "table": r["real_table"],
                "table_label": label,
                "field": r["field"],
                "field_desc": _desc,
                "agg": r["agg"],
                # 结果列中文短名（表头翻译用）：rework_qty → 返工数量；查不到为空串
                "cn": _column_cn(r["real_table"], r["field"], _desc),
            })
    except Exception:
        pass  # 解析失败：返回空结构，前端隐藏溯源区
    return out
