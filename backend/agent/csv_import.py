# -*- coding: utf-8 -*-
"""CSV 即传即分析（P2-C，对标 ThoughtSpot Spotter 3 CSV upload）。

Spotter 的做法：上传 CSV 后**即时分析**（无需预建数据模型），文件只保留在会话内
（约 1 小时），不污染企业数据资产。本实现对齐：

1. **上传即建表**：pandas 读前 200 行推断列类型 → 生成唯一临时表名
   `tmp_upload_<ts>_<rand>` → CREATE TABLE + 参数化批量 INSERT（无注入）；
2. **注册进表匹配**：把临时表描述（表名/别名/描述/关键词/字段）追加到
   `db.metadata.TABLES`，用户直接问「这个文件里…」即可命中；
3. **TTL 生命周期**：默认 1 小时，超时自动 DROP 临时表并从 TABLES 移除
   （惰性清理：下次导入时顺带清扫过期表，不引入常驻线程）。

安全边界：
- 文件大小上限 5MB（对齐 Spotter）、行数上限 50 万；
- 写入走 SQLAlchemy 参数化绑定（与 write-back 同通道），列名/表名用白名单正则
  清洗（仅 [a-zA-Z0-9_]），杜绝拼接注入；
- 临时表独立命名空间（tmp_upload_ 前缀），不碰业务表。
"""
from __future__ import annotations

import io
import logging
import re
import threading
import time
import uuid

_logger = logging.getLogger("csv_import")

MAX_FILE_BYTES = 5 * 1024 * 1024      # 5MB（对齐 Spotter）
MAX_ROWS = 500_000
TTL_SECONDS = 3600                    # 1 小时（对齐 Spotter 会话保留时长）
_MAX_IMPORTED = 20                    # 最多同时保留 20 张临时表

_TABLE_PREFIX = "tmp_upload_"
_VALID_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# 已导入的临时表注册表：table_name -> {created_at, file_name, row_count, n_cols}
_registry: dict[str, dict] = {}
# 保护 metadata.TABLES 的追加/移除（多请求并发上传/删除时防丢条目）
_tables_lock = threading.Lock()


def _next_table_name() -> str:
    return f"{_TABLE_PREFIX}{int(time.time())}_{uuid.uuid4().hex[:6]}"


def _clean_column(name: str, used: set[str]) -> str:
    """列名清洗：非标识符字符转下划线；重名加后缀；空名用 col_N。"""
    raw = str(name or "").strip()
    cleaned = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]", "_", raw)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    # 中文列名转拼音不可行 → 统一映射为 c_序号（原始列名记入 desc）
    if not cleaned or not _VALID_IDENT.match(cleaned) or any(ord(ch) > 127 for ch in cleaned):
        base = f"c{len(used) + 1}"
    else:
        base = cleaned
    n = base
    i = 2
    while n.lower() in used:
        n = f"{base}_{i}"
        i += 1
    used.add(n.lower())
    return n


def _infer_sql_type(series) -> str:
    """按 pandas dtype 推断 SQL 类型（PG/MySQL 通用类型）。"""
    non_null = series.dropna()
    if non_null.empty:
        return "TEXT"
    if series.dtype.kind == "M":       # datetime
        return "DATE"
    if series.dtype.kind in ("i", "u"):  # 整型
        return "INTEGER"
    if series.dtype.kind == "f":        # 浮点
        return "DOUBLE PRECISION"
    # object/文本：抽查是否为纯数字/日期串
    sample = non_null.astype(str).head(20)
    if all(re.match(r"^[-+]?\d+(\.\d+)?$", v.strip()) for v in sample if v.strip()):
        return "DOUBLE PRECISION"
    if all(re.match(r"^\d{4}-\d{1,2}-\d{1,2}", v.strip()) for v in sample if v.strip()):
        return "DATE"
    return "TEXT"


def _execute_ddl(sql: str) -> None:
    """用 SQLAlchemy 连接执行 DDL/DML（execute_sql 只读，写操作走此通道）。"""
    from sqlalchemy import text
    from db.executor import _engine
    conn = _engine().connect()
    try:
        conn.execute(text(sql))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def import_csv(file_bytes: bytes, file_name: str = "") -> dict:
    """解析 CSV → 建临时表 → 注册表匹配。返回 {success, table_name, row_count,
    columns, ttl_seconds, error}。"""
    try:
        import pandas as pd
    except Exception as e:
        return {"success": False, "error": f"pandas 不可用: {e}"}

    if not file_bytes:
        return {"success": False, "error": "文件为空"}
    if len(file_bytes) > MAX_FILE_BYTES:
        return {"success": False, "error": f"文件超过 5MB 上限（{len(file_bytes) // 1024 // 1024}MB）"}

    # 1. 样本解析（前 200 行，用于列名清洗与初步类型推断）
    try:
        df = pd.read_csv(io.BytesIO(file_bytes), nrows=200)
    except Exception as e:
        return {"success": False, "error": f"CSV 解析失败：{str(e)[:120]}"}
    if df.empty or df.shape[1] == 0:
        return {"success": False, "error": "CSV 无有效列"}

    # 2. 全量解析（类型最终以全量数据为准：样本列全 int、后续行混入字符串时，
    #    样本推断的 INTEGER 会在写入时失败——统一降级 TEXT 更稳）
    try:
        df_full = pd.read_csv(io.BytesIO(file_bytes))
        truncated = len(df_full) > MAX_ROWS
        df_full = df_full.head(MAX_ROWS)
    except Exception as e:
        return {"success": False, "error": f"CSV 解析失败：{str(e)[:120]}"}
    if df_full.empty or df_full.shape[1] == 0:
        return {"success": False, "error": "CSV 无有效列"}

    # 3. 列名清洗 + 类型推断（全量数据为准；字段结构与既有 TABLES 对齐：name/type/key/description/sample）
    used: set[str] = set()
    cols = []
    for i, raw in enumerate(df_full.columns):
        cname = _clean_column(raw, used)
        full_series = df_full.iloc[:, i]
        sql_type = _infer_sql_type(full_series)
        # 若全量类型与样本不一致（如样本全 int、全量含 str），样本列类型无参考意义，
        # 直接用全量推断结果（_infer_sql_type 对混合类型返回 TEXT）。
        cols.append({
            "name": cname,
            "sql_type": sql_type,
            "original": str(raw).strip()[:60],
            # 示例值：取全量首个非空值（供 LLM/schema context 理解列含义）
            "sample": str(next((v for v in full_series.dropna().head(10) if str(v).strip()), ""))[:40],
        })
    col_defs = ", ".join(f'"{c["name"]}" {c["sql_type"]}' for c in cols)
    table = _next_table_name()

    # 4. 建表（先清扫过期临时表）
    cleanup_expired()
    try:
        _execute_ddl(f'CREATE TABLE "{table}" ({col_defs})')
    except Exception as e:
        return {"success": False, "error": f"建临时表失败：{str(e)[:120]}"}

    # 5. 参数化批量写入
    try:
        df_full = df_full.where(pd.notnull(df_full), None)
        rows = df_full.itertuples(index=False, name=None)
        # SQLAlchemy executemany 要求每行是 tuple/dict（list 会报错）
        params_list = [tuple(r) for r in rows]
    except Exception as e:
        try:
            _execute_ddl(f'DROP TABLE IF EXISTS "{table}"')
        except Exception:
            pass
        return {"success": False, "error": f"数据读取失败：{str(e)[:120]}"}

    if params_list:
        # 命名占位符 + dict 参数（SQLAlchemy executemany 用位置参数处理混合类型会
        # 触发 '<' not supported between int and str 的内部类型推断 bug，命名参数无此问题）
        keys = [f"v{i}" for i in range(len(cols))]
        placeholders = ", ".join(":" + k for k in keys)
        insert_sql = f'INSERT INTO "{table}" VALUES ({placeholders})'

        def _native(v):
            """numpy 标量 → Python 原生类型（int64/float64 转 int/float，None 保留）。"""
            if v is None:
                return None
            if hasattr(v, "item"):
                try:
                    return v.item()
                except Exception:
                    return v
            return v

        params_list = [{k: _native(row[i]) for i, k in enumerate(keys)}
                       for row in params_list]
        try:
            from sqlalchemy import text
            from db.executor import _engine
            conn = _engine().connect()
            try:
                # executemany 分批（每批 5000），避免超大单次绑定
                for i in range(0, len(params_list), 5000):
                    conn.execute(text(insert_sql), params_list[i:i + 5000])
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            try:
                _execute_ddl(f'DROP TABLE IF EXISTS "{table}"')
            except Exception:
                pass
            return {"success": False, "error": f"数据写入失败：{str(e)[:120]}"}

    row_count = len(params_list)
    # 6. 注册进表匹配（db.metadata.TABLES 运行时追加；fields 结构与既有元数据对齐）
    try:
        from db.metadata import TABLES
        base = re.sub(r"\.(csv|txt|tsv)$", "", file_name or "上传文件").strip() or "上传文件"
        keywords = [base] + [c["original"] for c in cols[:5] if c["original"]]
        with _tables_lock:
            TABLES.append({
                "table_name": table,
                "table_alias": base,
                "category": "fact",
                "description": f"用户上传的临时数据文件「{base}」（{row_count} 行，{len(cols)} 列），"
                               f"1 小时后自动清理；可问「{base}」或列名相关的问题",
                "row_count": row_count,
                "keywords": keywords,
                "fields": [{"name": c["name"], "type": c["sql_type"],
                            "key": "", "description": c["original"], "sample": c.get("sample", "")}
                           for c in cols],
                "related_tables": [],
            })
        # 表列结构缓存失效（P2 修复）：enforcer 的 _table_columns 有进程内缓存，
        # 新建表后不清会拿到旧列清单，导致星号展开/列归属判断错误
        try:
            from security.enforcer import invalidate_column_cache
            invalidate_column_cache()
        except Exception:
            pass
    except Exception as e:
        _logger.warning("临时表注册元数据失败（不影响查询）: %s", e)

    _registry[table] = {"created_at": time.time(), "file_name": file_name or "",
                        "row_count": row_count, "n_cols": len(cols)}
    # 超上限淘汰最旧
    if len(_registry) > _MAX_IMPORTED:
        oldest = sorted(_registry.items(), key=lambda kv: kv[1]["created_at"])[0][0]
        try:
            drop_table(oldest)
        except Exception:
            pass

    return {"success": True, "table_name": table, "row_count": row_count,
            "columns": [c["name"] for c in cols],
            "ttl_seconds": TTL_SECONDS, "file_name": file_name or "",
            "truncated": truncated}


def drop_table(table: str) -> bool:
    """立即删除一张临时表（DROP + 从 TABLES/registry 移除）。

    安全：table 必须是 `tmp_upload_` 前缀 + 合法标识符（白名单校验），
    防 SQL 注入（历史漏洞：仅前缀校验时 `"tmp_upload_x"; DROP TABLE ... --"` 可拼接多语句）。
    """
    if not table or not table.startswith(_TABLE_PREFIX):
        return False
    if not _VALID_IDENT.match(table):
        return False
    try:
        _execute_ddl(f'DROP TABLE IF EXISTS "{table}"')
    except Exception:
        pass
    _registry.pop(table, None)
    try:
        from db.metadata import TABLES
        with _tables_lock:
            TABLES[:] = [t for t in TABLES if t.get("table_name") != table]
    except Exception:
        pass
    # 表列结构缓存失效（P2 修复）：删表后 enforcer 缓存需同步清
    try:
        from security.enforcer import invalidate_column_cache
        invalidate_column_cache()
    except Exception:
        pass
    return True


def cleanup_expired(now: float | None = None) -> int:
    """清理超 TTL 的临时表，返回清理数（惰性：导入/删除时调用）。"""
    now = now or time.time()
    expired = [t for t, info in _registry.items()
               if now - info.get("created_at", 0) > TTL_SECONDS]
    for t in expired:
        drop_table(t)
    return len(expired)


def list_tmp_tables() -> list[dict]:
    """当前存活的临时表（供管理/展示）。"""
    cleanup_expired()
    return [{"table_name": t, "file_name": i.get("file_name", ""),
             "row_count": i.get("row_count", 0), "n_cols": i.get("n_cols", 0),
             "remaining_sec": max(0, int(TTL_SECONDS - (time.time() - i.get("created_at", 0))))}
            for t, i in _registry.items()]
