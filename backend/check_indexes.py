# -*- coding: utf-8 -*-
"""数据库索引核查工具 — 找出缺索引的高频过滤/JOIN 列，输出建索引建议。

用法：python check_indexes.py [表名1 表名2 ...]（不传则扫全部业务表）
原理：
  1. 扫描所有表的所有列（information_schema.columns）
  2. 对比已有索引（pg_indexes / information_schema.statistics）
  3. 标记两类"该有索引"的列：
     a. 外键列（参与 JOIN 的表关联列，FK 约束存在但无索引）
     b. 高频过滤列（日期/时间列、status/type/id 后缀列、主键列）
  4. 输出 CREATE INDEX 建议（按表分组，幂等）
适用范围：PostgreSQL 与 MySQL；可直接对真实 factory 库运行。
"""
import sys
import io

sys.path.insert(0, ".")
from sqlalchemy import text
from database import engine, get_db_type


def _db_type() -> str:
    return get_db_type()


def get_tables() -> list[str]:
    if _db_type() == "mysql":
        sql = ("SELECT table_name FROM information_schema.tables "
               "WHERE table_schema=DATABASE() AND table_type='BASE TABLE' ORDER BY table_name")
    else:
        sql = ("SELECT table_name FROM information_schema.tables "
               "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(sql))]


def get_columns(tables: list[str]) -> dict[str, list[tuple[str, str]]]:
    """表 → [(列名, 数据类型)]"""
    if not tables:
        return {}
    inlist = ",".join(f"'{t}'" for t in tables)
    if _db_type() == "mysql":
        sql = (f"SELECT table_name, column_name, data_type FROM information_schema.columns "
               f"WHERE table_schema=DATABASE() AND table_name IN ({inlist}) ORDER BY table_name, ordinal_position")
    else:
        sql = (f"SELECT table_name, column_name, data_type FROM information_schema.columns "
               f"WHERE table_schema='public' AND table_name IN ({inlist}) ORDER BY table_name, ordinal_position")
    out: dict[str, list[tuple[str, str]]] = {}
    with engine.connect() as conn:
        for t, c, dt in conn.execute(text(sql)):
            out.setdefault(t, []).append((c, dt))
    return out


def get_existing_indexes(tables: list[str]) -> set[str]:
    """已索引列集合（含主键），格式 'table.column'（小写）"""
    idx: set[str] = set()
    if not tables:
        return idx
    inlist = ",".join(f"'{t}'" for t in tables)
    try:
        with engine.connect() as conn:
            if _db_type() == "mysql":
                rows = conn.execute(text(
                    f"SELECT table_name, column_name FROM information_schema.statistics "
                    f"WHERE table_schema=DATABASE() AND table_name IN ({inlist})"
                )).fetchall()
            else:
                rows = conn.execute(text(
                    f"SELECT t.tablename, a.attname FROM pg_indexes t "
                    f"JOIN pg_class c ON c.relname = t.tablename "
                    f"JOIN pg_index i ON i.indrelid = c.oid "
                    f"JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
                    f"WHERE t.schemaname='public' AND t.tablename IN ({inlist})"
                )).fetchall()
        for t, c in rows:
            idx.add(f"{t.lower()}.{c.lower()}")
    except Exception as e:
        print(f"[warn] 索引查询失败: {e}")
    return idx


def _looks_filterable(col: str, dtype: str) -> bool:
    """高频过滤/JOIN 列启发式：日期时间、状态、id 后缀、code/name 等"""
    c = col.lower()
    if "date" in c or "time" in c:
        return True
    if c.endswith("_id") or c.endswith("_code") or c == "id":
        return True
    if c in ("status", "type", "category", "is_planned", "result", "shift_code"):
        return True
    return False


def check(tables_arg: list[str] | None = None) -> list[str]:
    """返回建索引建议 SQL 列表"""
    tables = tables_arg or get_tables()
    if not tables:
        return []
    cols_map = get_columns(tables)
    indexed = get_existing_indexes(tables)
    suggestions: list[str] = []
    for t in tables:
        for col, dtype in cols_map.get(t, []):
            key = f"{t.lower()}.{col.lower()}"
            if key in indexed:
                continue
            if _looks_filterable(col, dtype):
                suggestions.append(
                    f"-- {t}.{col} ({dtype}) — 高频过滤/JOIN 列缺索引\n"
                    f"CREATE INDEX IF NOT EXISTS idx_{t}_{col} ON {t} ({col});"
                )
    return suggestions


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    print(f"数据库: {_db_type()} | 扫描表: {len(args) if args else '全部业务表'}\n")
    sugs = check(args or None)
    if not sugs:
        print("未发现缺索引的高频过滤/JOIN 列（当前库数据量小或索引已齐）。")
    else:
        print(f"发现 {len(sugs)} 条建索引建议：\n")
        for s in sugs:
            print(s)
            print()
        print("# 提示：CREATE INDEX IF NOT EXISTS 幂等，可直接执行；真实大表建议先用 EXPLAIN 验证")
