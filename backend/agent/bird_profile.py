# -*- coding: utf-8 -*-
"""BIRD-Bench 评测档位（benchmark profile）。

只在跑 BIRD Mini-Dev 评测时启用；**生产问析链路的默认行为完全不变**
（所有分支都由 `enabled()` 短路，默认 False）。

为什么需要单独一个档位
----------------------
1. 现网提示词是为中文制造业库（MES）写的，含一批「默认 LIMIT 20」「良率=SUM(good_qty)/…」
   「中文别名」等规则。直接拿去跑 BIRD 会**主动制造错误**：
   BIRD 官方口径是 `set(pred_rows) == set(gold_rows)`，
   多一行少一行、多一列少一列都判错，而未加要求的 `LIMIT 20`
   会把原本要返回全量行的结果集静默截断。
2. BIRD 每题自带 `evidence`（专家标注的领域提示，如
   "Exclusively virtual refers to Virtual = 'F'"、年份存成 YYYYMM 等）。
   不注入等于让模型盲猜列名与取值编码 —— 实测这正是首题答错的主因。
3. BIRD 每个库只有 3~14 张表，**不需要向量召回**，全量 schema 直给最稳。
   实测表召回漏掉 `schools`（答案所在表）直接导致答错。
4. 需要按 BIRD 的 schema 命名规则输出：SQLite DDL 里带引号的标识符保留原大小写，
   裸标识符折叠成小写 —— 所以 schema 里必须明确告诉模型哪些名字需要加双引号。

线程安全：上下文存在 threading.local 里，评测脚本可多线程并发跑不同题目。
"""
import os
import re
import threading

_tls = threading.local()
_SCHEMA_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def enabled() -> bool:
    """是否处于 BIRD 评测档位。"""
    if getattr(_tls, "enabled", None):
        return True
    return os.getenv("NL2SQL_PROMPT_PROFILE", "").strip().lower() == "bird"


def set_context(evidence: str = "", db_id: str = "", active: bool = True) -> None:
    _tls.enabled = bool(active)
    _tls.evidence = evidence or ""
    _tls.db_id = db_id or ""


def clear_context() -> None:
    _tls.enabled = False
    _tls.evidence = ""
    _tls.db_id = ""


def evidence() -> str:
    ev = getattr(_tls, "evidence", "") or os.getenv("NL2SQL_BIRD_EVIDENCE", "")
    return (ev or "").strip()


def db_id() -> str:
    d = getattr(_tls, "db_id", "") or os.getenv("NL2SQL_BIRD_DB_ID", "")
    if d:
        return d
    try:
        from database import get_database_config
        name = (get_database_config() or {}).get("name") or ""
        return name[len("bird__"):] if name.startswith("bird__") else name
    except Exception:
        return ""


# ── 提示词规则块 ────────────────────────────────────────────
def rules_block() -> str:
    """BIRD 专用硬规则（英文题面 + 英文 schema，用英文写更对齐）。"""
    rules = [
        "## Hard rules (violating any one of them counts as a failure)",
        "1. Output exactly ONE read-only SELECT statement. Never emit INSERT/UPDATE/DELETE/"
        "DROP/CREATE/ALTER/TRUNCATE.",
        "2. Copy table and column names EXACTLY as written in the schema below. Never invent or "
        "translate names. Names shown inside double quotes in the schema MUST be double-quoted "
        "in your SQL; all other names are plain lower-case identifiers.",
        "3. Do NOT add a LIMIT unless the question explicitly asks for a top-N / first / last / "
        "least / most / highest / lowest value or an explicit row cap. An unrequested LIMIT "
        "silently truncates the answer and is scored as wrong.",
        "4. Select exactly the columns the question asks for. Do NOT append extra identifier "
        "columns (ids, codes, names, addresses) that the question did not ask about — extra "
        "columns make the result set differ from the expected answer.",
        "5. Join only along the foreign keys listed under 'Foreign keys'. If two tables are not "
        "directly related, chain through the intermediate table. Never guess an ON condition "
        "from similar-looking column names.",
        "6. Ratios / percentages / averages: cast to a floating type "
        "(e.g. CAST(a AS REAL) / NULLIF(b, 0)) so integer division does not silently floor the "
        "result, and always guard the denominator with NULLIF(denominator, 0).",
        "7. Follow the 'Domain hints (evidence)' section literally — it defines ambiguous words, "
        "value encodings and date formats and takes precedence over your own assumptions.",
        "8. Do not add WHERE filters (especially on time or status) that the question did not "
        "ask for. If a filter is implied by the domain hints, apply exactly that filter.",
        "9. Prefer straightforward, literal interpretation: the expected answer is derived from "
        "the data, not from business intuition. Do not 'improve' the question.",
        "10. DISTINCT is a decision, not decoration. Use it ONLY when duplicates would appear "
        "without it — e.g. listing names/categories that the same join would repeat, or the "
        "question says 'unique' / 'distinct'. Do NOT use it for pure aggregates (COUNT/SUM/AVG "
        "over a single table) and do NOT use it just because a join is involved. Getting this "
        "wrong changes the row count and the answer is scored wrong.",
        "11. Over-filtering is scored as wrong just like under-filtering. If the question does "
        "not restrict rows, return all of them; do not add 'AND x IS NOT NULL' or extra bounds "
        "the question never mentioned.",
    ]
    if os.getenv("BIRD_DB_BACKEND", "pg").strip().lower() == "sqlite":
        rules.append(
            "12. This database is SQLite, NOT PostgreSQL. Date/time functions MUST use "
            "strftime('%Y', col); NEVER use EXTRACT(YEAR FROM col), SUBSTRING(x FROM a FOR b) "
            "or other PostgreSQL-only syntax. String aggregation is GROUP_CONCAT (not "
            "STRING_AGG). String concatenation uses ||."
        )
    return "\n".join(rules)


def dialect_name() -> str:
    """BIRD 档位当前数据库方言名（SQLite / PostgreSQL）。"""
    return "SQLite" if os.getenv("BIRD_DB_BACKEND", "pg").strip().lower() == "sqlite" else "PostgreSQL"


def few_shot_examples() -> list:
    """跨库通用的 SQL 模板 few-shot 示例（针对 BIRD 最高频错误模式）。

    实测错误里「百分比整数除法」「日期函数」「多表 JOIN 聚合」反复出错。
    示例刻意用通用表名（orders/categories/items/records），只教写法、不绑定
    BIRD 特定 schema，避免模型照抄错误表名。
    """
    sqlite = os.getenv("BIRD_DB_BACKEND", "pg").strip().lower() == "sqlite"
    date_sql = (
        "SELECT CAST(SUM(CASE WHEN strftime('%Y', created_at) = '2013' THEN 1 ELSE 0 END) "
        "AS REAL) * 100 / NULLIF(COUNT(*), 0) FROM records"
        if sqlite else
        "SELECT CAST(SUM(CASE WHEN EXTRACT(YEAR FROM created_at) = 2013 THEN 1 ELSE 0 END) "
        "AS REAL) * 100 / NULLIF(COUNT(*), 0) FROM records"
    )
    return [
        {"question": "What percentage of records are from the year 2013?",
         "sql": date_sql},
        {"question": "For each category, how many items are there?",
         "sql": "SELECT c.name, COUNT(i.id) FROM categories c JOIN items i "
                "ON i.category_id = c.id GROUP BY c.id, c.name"},
        {"question": "List the top 3 items by score.",
         "sql": "SELECT i.name FROM items i ORDER BY i.score DESC LIMIT 3"},
    ]


# ── 紧凑全量 schema ────────────────────────────────────────
def compact_schema(conn=None) -> str:
    """当前库的紧凑全量 schema：所有表 + 所有列(含类型) + 主键/外键。

    比现网 CREATE TABLE 风格 DDL 更省 token —— BIRD 里 california_schools 单表就有
    90+ 列，富文本 DDL 会直接把 prompt 预算撑爆并触发 DDL 截断（截掉的可能正是关键表）。
    """
    key = None
    try:
        from database import get_database_config
        cfg = get_database_config() or {}
        key = "%s:%s:%s:%s" % (cfg.get("db_type"), cfg.get("host"), cfg.get("port"), cfg.get("name"))
    except Exception:
        key = "default"
    with _CACHE_LOCK:
        hit = _SCHEMA_CACHE.get(key)
    if hit:
        return hit

    if os.getenv("BIRD_DB_BACKEND", "pg").strip().lower() == "sqlite":
        text = _compact_schema_sqlite()
        if text:
            with _CACHE_LOCK:
                _SCHEMA_CACHE[key] = text
        return text

    from db.executor import execute_sql

    tabs = execute_sql(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
    if not tabs.get("success"):
        return ""
    names = [r["table_name"] for r in (tabs.get("rows") or [])]

    cols = execute_sql(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='public' ORDER BY table_name, ordinal_position")
    by_table: dict[str, list] = {}
    for r in (cols.get("rows") or []):
        by_table.setdefault(r["table_name"], []).append((r["column_name"], (r["data_type"] or "").lower()))

    fks = execute_sql(
        "SELECT tc.table_name AS child, kcu.column_name AS ccol, "
        "       ccu.table_name AS parent, ccu.column_name AS pcol "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema "
        "JOIN information_schema.constraint_column_usage ccu "
        "  ON ccu.constraint_name=tc.constraint_name AND ccu.table_schema=tc.table_schema "
        "WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='public'")
    fk_lines = []
    fk_by_child: dict[str, list] = {}
    for r in (fks.get("rows") or []):
        line = "%s.%s = %s.%s" % (r["child"], r["ccol"], r["parent"], r["pcol"])
        fk_lines.append(line)
        fk_by_child.setdefault(r["child"], []).append(
            "%s -> %s.%s" % (r["ccol"], r["parent"], r["pcol"]))

    out = ["# Database schema", "# 标识符规则：schema 中出现在双引号内的名字必须原样加双引号；"
           "其余名字是小写裸标识符。", ""]
    for t in names:
        cs = by_table.get(t) or []
        out.append("TABLE %s (%s)" % (t, ", ".join(
            ('"%s"' % c if _needs_quote(c) else c) + " " + _short_type(dt) for c, dt in cs)))
        if fk_by_child.get(t):
            out.append("  JOIN keys: " + "; ".join(fk_by_child[t]))
    out.append("")
    out.append("## Foreign keys (use ONLY these for joins)")
    out.extend(fk_lines or ["(none declared)"])
    text = "\n".join(out)
    with _CACHE_LOCK:
        _SCHEMA_CACHE[key] = text
    return text


def _needs_quote(name: str) -> bool:
    """该列名在 PG 里是否需要（且必须以）双引号书写。

    与官方 gold SQL 的写法保持一致：SQLite DDL 里裸写的标识符被 PG 折叠成小写
    （`T1.CDSCode` 实际是 cdscode），带引号的保留原样（`T2."Enrollment (K-12)"`）。
    """
    return name != name.lower() or bool(re.search(r"[^a-z0-9_]", name))


def _short_type(dt: str) -> str:
    m = {
        "bigint": "int", "integer": "int", "smallint": "int",
        "double precision": "float", "numeric": "num", "real": "float",
        "character varying": "text", "text": "text", "character": "text",
        "timestamp without time zone": "timestamp", "date": "date", "boolean": "bool",
    }
    return m.get((dt or "").lower(), (dt or "").lower())


def clear_schema_cache() -> None:
    with _CACHE_LOCK:
        _SCHEMA_CACHE.clear()


# ── SQLite 后端（沙箱环境 PG 崩溃时使用）────────────────────
def _win_path(p: str) -> str:
    """把 Git Bash 的 /d/xxx Unix 路径转成 Windows 的 D:/xxx。"""
    m = re.match(r"^/([a-zA-Z])/(.*)$", p or "")
    if m:
        return m.group(1).upper() + ":/" + m.group(2)
    return p or ""


def _sqlite_path() -> str:
    d = db_id()
    base = _win_path(os.getenv("BIRD_SQLITE_DIR", "").strip())
    if not d or not base:
        return ""
    return os.path.join(base, d + ".sqlite")


def sqlite_tables() -> list:
    """SQLite 后端的表名列表（供 BIRD 分支的 matched_tables 使用，绕过 PG）。"""
    import sqlite3
    path = _sqlite_path()
    if not path or not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect(path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name")
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


def _fk_from_dev_tables(db_id_: str):
    """从官方 dev_tables.json 读外键（列索引 → 列名）。

    BIRD 的 SQLite 文件多数没声明 FOREIGN KEY 约束，`PRAGMA foreign_key_list`
    返回空；而官方 dev_tables.json 里有专家标注的关联键，用它最准。
    返回 (fk_lines, fk_by_child)。
    """
    import json
    try:
        base = _win_path(os.getenv("BIRD_SQLITE_DIR", ""))
        qdir = os.path.join(os.path.dirname(base), "questions") if base else ""
        path = os.path.join(qdir, "dev_tables.json") if qdir else ""
        if not path or not os.path.exists(path):
            return [], {}
        data = json.load(open(path, encoding="utf-8"))
        meta = next((x for x in data if x.get("db_id") == db_id_), None)
        if not meta:
            return [], {}
        cols = meta.get("column_names_original") or []      # [table_idx, col_name]
        tables = meta.get("table_names_original") or []

        def _cname(i):
            try:
                return cols[i][1]
            except Exception:
                return None

        def _tname(i):
            try:
                return tables[i]
            except Exception:
                return None

        fk_lines, fk_by_child = [], {}
        for fk in (meta.get("foreign_keys") or []):
            try:
                src_idx, dst_idx = fk[0], fk[1]
            except Exception:
                continue
            scol, dcol = _cname(src_idx), _cname(dst_idx)
            try:
                st, dt = _tname(cols[src_idx][0]), _tname(cols[dst_idx][0])
            except Exception:
                continue
            if not all([scol, dcol, st, dt]):
                continue
            fk_lines.append("%s.%s = %s.%s" % (st, scol, dt, dcol))
            fk_by_child.setdefault(st, []).append("%s -> %s.%s" % (scol, dt, dcol))
        return fk_lines, fk_by_child
    except Exception:
        return [], {}


def _compact_schema_sqlite() -> str:
    """SQLite 后端的紧凑 schema：sqlite_master 读表/列 + 官方 dev_tables.json 读外键。

    与 PG 版 `compact_schema` 输出同构（TABLE xxx (col type, ...) + JOIN keys +
    Foreign keys），只是数据源换成 SQLite。标识符规则比 PG 宽松：SQLite 裸标识符
    本身大小写不敏感，仅含空格/特殊字符的列仍需双引号。
    """
    import sqlite3
    path = _sqlite_path()
    if not path or not os.path.exists(path):
        return ""
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")
        names = [r[0] for r in cur.fetchall()]
        by_table = {}
        for t in names:
            info = cur.execute('PRAGMA table_info("%s")' % t.replace('"', '""')).fetchall()
            by_table[t] = [(r[1], r[2] or "text") for r in info]
    finally:
        conn.close()

    fk_lines, fk_by_child = _fk_from_dev_tables(db_id())

    out = ["# Database schema (SQLite)",
           "# 标识符规则：含空格/特殊字符的名字需双引号；其余裸写即可（SQLite 大小写不敏感）。",
           ""]
    for t in names:
        cs = by_table.get(t) or []
        out.append("TABLE %s (%s)" % (t, ", ".join(
            ('"%s"' % c if _needs_quote(c) else c) + " " + _short_type(dt) for c, dt in cs)))
        if fk_by_child.get(t):
            out.append("  JOIN keys: " + "; ".join(fk_by_child[t]))
    out.append("")
    out.append("## Foreign keys (use ONLY these for joins)")
    out.extend(fk_lines or ["(none declared)"])
    return "\n".join(out)
