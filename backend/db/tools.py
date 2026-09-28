"""数据库工具 — 元数据查询 & 关键词匹配表 & 动态扫表"""

import os
import time
import threading

from .metadata import TABLES, find_table_by_name, search_tables
from database import get_db_type

# ── 表列表 TTL 缓存 ─────────────────────────────────────
# get_real_tables 在问析主链路里被反复调用（每请求至少 1 次），每次都查
# information_schema 属重复往返。这里加 60s TTL 缓存；key 含库名，切换数据库
# 后 key 变化自然失效，无需显式清理。导入/删表等 DDL 变更最长 60s 内可见。
_META_TTL = float(os.getenv("META_CACHE_TTL", "60"))
_real_tables_cache: dict = {"key": None, "ts": 0.0, "value": []}
_real_tables_lock = threading.Lock()
# 外键关系 TTL 缓存（P0 性能优化 2026-09-01）：get_foreign_keys 每次真查 information_schema
# 约 14ms，被 ontology（归因路径）/llm_service 反复调用属重复往返。复用表列表同款 TTL + key 机制。
_fk_cache: dict = {"key": None, "ts": 0.0, "value": {}}


def _real_tables_cache_key():
    try:
        from database import get_database_config
        cfg = get_database_config()
        return (cfg.get("db_type"), cfg.get("host"), cfg.get("port"), cfg.get("name"))
    except Exception:
        return None


def _schema_filter() -> str:
    return "table_schema='public'" if get_db_type() != "mysql" else "table_schema=DATABASE()"


def _split_table_ref(table_ref: str) -> tuple[str, str]:
    """'schema.table' → (schema, table)；'table' → (public, table)"""
    if "." in table_ref:
        schema, table = table_ref.split(".", 1)
        return schema, table
    return "public", table_ref


def get_real_tables() -> list[dict]:
    """动态查询当前数据库的真实表列表（PG/MySQL 通用），带 TTL 缓存。
    - 优先 public schema；无 public 表时返回全部业务 schema 的表
    - 非 public 表名返回 'schema.table' 形式（如 factory.attendance）
    """
    key = _real_tables_cache_key()
    now = time.time()
    with _real_tables_lock:
        if key is not None and _real_tables_cache["key"] == key and now - _real_tables_cache["ts"] < _META_TTL:
            return _real_tables_cache["value"]

    try:
        from .executor import execute_sql
        if get_db_type() == "mysql":
            result = execute_sql(
                "SELECT table_name, "
                "(SELECT COUNT(*) FROM information_schema.columns WHERE table_name=t.table_name AND table_schema=DATABASE()) AS field_count "
                "FROM information_schema.tables t "
                "WHERE table_schema=DATABASE() AND table_type='BASE TABLE' "
                "AND table_name NOT LIKE 'tmp_upload_%' "  # 排除 CSV 临时表（会话内数据，不污染业务表清单）
                "ORDER BY table_name"
            )
            if result["success"] and result["rows"]:
                tables = [{"table_name": r["table_name"], "field_count": r["field_count"]} for r in result["rows"]]
            else:
                tables = []
        else:
            # PostgreSQL：列出所有业务 schema 的表（排除系统 schema）
            result = execute_sql(
                "SELECT table_schema, table_name, "
                "(SELECT COUNT(*) FROM information_schema.columns c "
                " WHERE c.table_schema=t.table_schema AND c.table_name=t.table_name) AS field_count "
                "FROM information_schema.tables t "
                "WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_type='BASE TABLE' "
                "AND table_name NOT LIKE 'tmp_upload_%' "  # 排除 CSV 临时表（会话内数据）
                "ORDER BY (table_schema='public') DESC, table_schema, table_name"
            )
            if result["success"] and result["rows"]:
                tables = []
                for r in result["rows"]:
                    name = r["table_name"] if r["table_schema"] == "public" else f"{r['table_schema']}.{r['table_name']}"
                    tables.append({"table_name": name, "field_count": r["field_count"]})
            else:
                tables = []
    except Exception:
        tables = []

    with _real_tables_lock:
        _real_tables_cache["key"] = key
        _real_tables_cache["ts"] = time.time()
        _real_tables_cache["value"] = tables
    return tables


def _merge_tables_with_metadata(real_tables: list[dict]) -> list[dict]:
    """将真实表与硬编码元数据合并"""
    meta_map = {t["table_name"]: t for t in TABLES}
    merged = []
    for rt in real_tables:
        name = rt["table_name"]
        meta = meta_map.get(name)
        if meta:
            merged.append({
                "table_name": name,
                "table_alias": meta["table_alias"],
                "category": meta.get("category", "dim"),
                "description": meta.get("description", ""),
                "row_count": meta.get("row_count", 0),
                "field_count": rt["field_count"],  # 真实字段数优先
            })
        else:
            # 真实库有但元数据没有的表
            merged.append({
                "table_name": name,
                "table_alias": name,
                "category": "dim",
                "description": "",
                "row_count": 0,
                "field_count": rt["field_count"],
            })
    return merged


def get_all_tables() -> list[dict]:
    """返回所有表的摘要信息 — 优先真实库，回退元数据"""
    real = get_real_tables()
    if real:
        return _merge_tables_with_metadata(real)
    # 数据库不可用时回退到硬编码元数据
    return [
        {
            "table_name": t["table_name"],
            "table_alias": t["table_alias"],
            "category": t["category"],
            "description": t["description"],
            "row_count": t["row_count"],
            "field_count": len(t["fields"]),
        }
        for t in TABLES
    ]


def get_table_detail(table_name: str) -> dict | None:
    """返回单表完整详情 — 优先真实DB字段，回退元数据"""
    meta = find_table_by_name(table_name)
    real_fields = _get_real_fields(table_name)

    if not meta and not real_fields:
        return None

    fields = real_fields if real_fields else (meta["fields"] if meta else [])
    return {
        "table_name": table_name,
        "table_alias": meta["table_alias"] if meta else table_name,
        "category": meta["category"] if meta else "dim",
        "description": meta["description"] if meta else "",
        "row_count": meta["row_count"] if meta else 0,
        "field_count": len(fields),
        "keywords": meta.get("keywords", []) if meta else [],
        "related_tables": meta.get("related_tables", []) if meta else [],
        "fields": fields,
    }


def _get_real_fields(table_name: str) -> list[dict] | None:
    """查询真实数据库的表字段（PG/MySQL 通用，支持 schema.table）"""
    try:
        from .executor import execute_sql
        schema, table = _split_table_ref(table_name)
        # 仅允许安全的表名/schema（字母数字下划线），杜绝注入
        if not table.replace("_", "").isalnum():
            return None
        if schema and not schema.replace("_", "").isalnum():
            return None
        if get_db_type() == "mysql":
            result = execute_sql(
                f"SELECT column_name, data_type, "
                f"CASE WHEN column_name IN ("
                f"  SELECT column_name FROM information_schema.key_column_usage "
                f"  WHERE table_name='{table}' AND table_schema=DATABASE() AND constraint_name='PRIMARY'"
                f") THEN 'PK' ELSE '' END AS key_type "
                f"FROM information_schema.columns "
                f"WHERE table_name='{table}' AND table_schema=DATABASE() "
                f"ORDER BY ordinal_position"
            )
        else:
            result = execute_sql(
                f"SELECT column_name, data_type, "
                f"CASE WHEN column_name IN ("
                f"  SELECT kcu.column_name FROM information_schema.table_constraints tc "
                f"  JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name "
                f"  WHERE tc.table_name='{table}' AND tc.table_schema='{schema}' AND tc.constraint_type='PRIMARY KEY'"
                f") THEN 'PK' ELSE '' END AS key_type "
                f"FROM information_schema.columns "
                f"WHERE table_name='{table}' AND table_schema='{schema}' "
                f"ORDER BY ordinal_position"
            )
        if result["success"] and result["rows"]:
            return [
                {
                    "name": r["column_name"],
                    "type": r["data_type"],
                    "key": r["key_type"],
                    "description": "",
                    "sample": "",
                }
                for r in result["rows"]
            ]
    except Exception:
        pass
    return None


def get_foreign_keys() -> dict[str, list[dict]]:
    """获取当前数据库全库外键关系（PG/MySQL 通用），带 TTL 缓存（对齐 get_real_tables）。

    返回: { "表名": [{"column": 外键列, "ref_table": 引用表, "ref_column": 引用列}, ...] }
    表名按当前 get_real_tables() 的命名规则（非 public 带 schema 前缀）。
    失败时返回 {}（不影响主流程）。
    """
    now = time.time()
    key = _real_tables_cache_key()
    with _real_tables_lock:
        if key is not None and _fk_cache["key"] == key and now - _fk_cache["ts"] < _META_TTL:
            return _fk_cache["value"]
    result = _load_foreign_keys_uncached()
    if key is not None:
        with _real_tables_lock:
            _fk_cache["key"] = key
            _fk_cache["ts"] = time.time()
            _fk_cache["value"] = result
    return result


def _load_foreign_keys_uncached() -> dict[str, list[dict]]:
    """实际查询全库外键（无缓存，供 get_foreign_keys 内部使用）。"""
    try:
        from .executor import execute_sql
        if get_db_type() == "mysql":
            r = execute_sql(
                "SELECT tc.table_name, kcu.column_name, ccu.table_name AS ref_table, "
                "  ccu.column_name AS ref_column "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "  AND tc.table_schema = DATABASE()"  # 修复：限定当前库，防跨库串表
            )
        else:
            r = execute_sql(
                "SELECT tc.table_schema, tc.table_name, kcu.column_name, "
                "  ccu.table_schema AS ref_schema, ccu.table_name AS ref_table, "
                "  ccu.column_name AS ref_column "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema "
                "WHERE tc.constraint_type = 'FOREIGN KEY'"
            )
        if not (r["success"] and r["rows"]):
            return {}
        out: dict[str, list[dict]] = {}
        for row in r["rows"]:
            if get_db_type() == "mysql":
                tname, ref_table = row["table_name"], row["ref_table"]
            else:
                schema = row.get("table_schema") or "public"
                ref_schema = row.get("ref_schema") or schema  # 跨 schema 外键也能正确拼接
                tname = row["table_name"] if schema == "public" else f"{schema}.{row['table_name']}"
                ref_table = row["ref_table"] if ref_schema == "public" else f"{ref_schema}.{row['ref_table']}"
            # 自引用外键：同一张表内的 parent_id → id（如 department.parent_id → department.id）。
            # 判定只看表是否相同——原条件 `column_name == ref_column` 恒为 False（自引用时
            # 两列名必然不同，如 parent_id vs id），导致自引用关系漏过滤、干扰 LLM 多表 JOIN 推理。
            if tname == ref_table:
                continue  # 自引用
            out.setdefault(tname, []).append({
                "column": row["column_name"],
                "ref_table": ref_table,
                "ref_column": row["ref_column"],
            })
        return out
    except Exception:
        return {}


def get_dynamic_table_detail(table_name: str) -> dict | None:
    """动态获取未知表的详细信息，包括字段结构，无需硬编码元数据（PG/MySQL 通用）"""
    # 标识符白名单校验：table_name 可能来自 LLM/外部输入，含单引号会污染下方 information_schema 查询；
    # 拆出 schema/table 后各自校验，与 _get_real_fields 同口径（防 SQL 注入）。
    schema, tbl = _split_table_ref(table_name or "")
    if not tbl.replace("_", "").isalnum() or (schema != "public" and not schema.replace("_", "").isalnum()):
        return None
    try:
        from .executor import execute_sql

        # 1. 获取表的基本信息（MySQL 无 pg_class 描述，忽略描述查询）
        table_desc = ""
        if get_db_type() != "mysql":
            table_info_result = execute_sql(
                f"SELECT table_name, obj_description(c.oid) as description "
                f"FROM information_schema.tables t "
                f"JOIN pg_class c ON c.relname = t.table_name "
                f"WHERE t.table_name = '{tbl}' AND t.table_schema = '{schema}'"
            )
            if table_info_result["success"] and table_info_result["rows"]:
                table_desc = table_info_result["rows"][0]["description"] or ""

        # 2. 获取字段信息（已实现的函数，内部会再次校验）
        fields = _get_real_fields(table_name)
        if not fields:
            return None

        # 3. 获取行数（估算）：schema.table 需分别 quote，否则 quote_ident 把整体当单个标识符，
        #    生成 "factory.inventory"（含点）导致表不存在
        from database import quote_ident
        qualified = f"{quote_ident(schema)}.{quote_ident(tbl)}" if schema and schema != "public" else quote_ident(tbl)
        row_count_result = execute_sql(f"SELECT COUNT(*) as row_count FROM {qualified} LIMIT 100000")
        row_count = row_count_result["rows"][0]["row_count"] if row_count_result["success"] and row_count_result["rows"] else 0

        # 4. 获取相关表（基于外键关系）
        related_tables = []
        try:
            fk_result = execute_sql(
                f"SELECT "
                f"  tc.table_name, "
                f"  kcu.column_name, "
                f"  ccu.table_name AS foreign_table_name, "
                f"  ccu.column_name AS foreign_column_name "
                f"FROM information_schema.table_constraints AS tc "
                f"JOIN information_schema.key_column_usage AS kcu "
                f"  ON tc.constraint_name = kcu.constraint_name "
                f"  AND tc.table_schema = kcu.table_schema "
                f"JOIN information_schema.constraint_column_usage AS ccu "
                f"  ON ccu.constraint_name = tc.constraint_name "
                f"  AND ccu.table_schema = tc.table_schema "
                f"WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = '{tbl}' AND tc.table_schema = '{schema}'"
            )
            if fk_result["success"] and fk_result["rows"]:
                for row in fk_result["rows"]:
                    if row["foreign_table_name"] != table_name:  # Avoid self-references
                        related_tables.append(row["foreign_table_name"])
        except Exception:
            pass
        
        # 5. 构建表描述
        if not table_desc:
            # 尝试基于表名生成一个基本描述
            if table_name.startswith('dim_') or table_name.endswith('_dim'):
                table_desc = f"维度表 - {table_name}"
            elif table_name.startswith('fact_') or table_name.endswith('_fact'):
                table_desc = f"事实表 - {table_name}"
            else:
                table_desc = f"表 - {table_name}"
        
        # 6. 确定表类别
        if table_name.startswith('dim_') or table_name.endswith('_dim'):
            category = "master"
        elif table_name.startswith('fact_') or table_name.endswith('_fact'):
            category = "fact"
        else:
            category = "dim"
            
        return {
            "table_name": table_name,
            "table_alias": table_name,
            "category": category,
            "description": table_desc,
            "row_count": row_count,
            "field_count": len(fields),
            "keywords": [table_name],  # Use table name as keyword
            "related_tables": list(set(related_tables)),  # Remove duplicates
            "fields": fields,
        }
    except Exception as e:
        print(f"Error getting dynamic table detail for {table_name}: {str(e)}")
        return None


def get_table_fields(table_name: str) -> list[dict] | None:
    """返回单表字段列表"""
    t = find_table_by_name(table_name)
    if not t:
        return None
    return t["fields"]


def match_tables_by_query(query: str) -> list[dict]:
    """根据自然语言查询匹配相关表,返回匹配度排序的表列表"""
    matched = search_tables(query)
    return [
        {
            "table_name": t["table_name"],
            "table_alias": t["table_alias"],
            "category": t["category"],
            "description": t["description"],
            "row_count": t["row_count"],
            "field_count": len(t["fields"]),
            "keywords_matched": [kw for kw in t["keywords"] if kw in query or query in kw],
        }
        for t in matched
    ]
