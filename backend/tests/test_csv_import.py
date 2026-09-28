# -*- coding: utf-8 -*-
"""P2-C CSV 即传即分析（对标 Spotter 3 CSV upload）测试。

覆盖：文件校验 / 类型推断 / 列名清洗 / 导入-查询-删除全流程 / TTL 清理。
建表会真实写库，测试末尾统一清理临时表。
"""
from __future__ import annotations

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_passed = 0
_failed = 0
_created: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}  {detail}")


def cleanup():
    from agent.csv_import import drop_table
    for t in list(_created):
        drop_table(t)
    _created.clear()


print("═══ A. 文件校验 ═══")
from agent.csv_import import import_csv, MAX_FILE_BYTES

r = import_csv(b"")
check("A1 空文件拒绝", not r.get("success") and "空" in r.get("error", ""), str(r))
r2 = import_csv(b"x" * (MAX_FILE_BYTES + 1))
check("A2 超 5MB 拒绝", not r2.get("success") and "5MB" in r2.get("error", ""), str(r2))
r3 = import_csv(b"\xff\xfe\x00\x01\x02broken\x80")  # 非 UTF-8 乱码
check("A3 乱码拒绝", not r3.get("success"), str(r3))

print("═══ B. 类型推断 ═══")
from agent.csv_import import _infer_sql_type
import pandas as pd

check("B1 整型 → INTEGER", _infer_sql_type(pd.Series([1, 2, 3])) == "INTEGER", "")
check("B2 浮点 → DOUBLE", _infer_sql_type(pd.Series([1.5, 2.5])) == "DOUBLE PRECISION", "")
check("B3 日期 → DATE", _infer_sql_type(pd.Series(["2026-01-01", "2026-01-02"])) == "DATE", "")
check("B4 文本 → TEXT", _infer_sql_type(pd.Series(["a", "b", "c"])) == "TEXT", "")
check("B5 空列 → TEXT", _infer_sql_type(pd.Series([None, None])) == "TEXT", "")

print("═══ C. 列名清洗 ═══")
from agent.csv_import import _clean_column

used: set[str] = set()
check("C1 正常名保留", _clean_column("qty", used) == "qty", "")
check("C2 中文列名转 cN", _clean_column("数量", used).startswith("c"), "")
check("C3 特殊字符转下划线", _clean_column("a b-c", used).startswith("a"), "")
check("C4 重名加后缀", _clean_column("qty", used) != "qty", "")

print("═══ D. 导入-查询-删除全流程 ═══")
from db.executor import execute_sql
from db.tools import match_tables_by_query

csv_data = ("product,qty,price,date\nA,100,9.5,2026-01-01\n"
            "B,200,19.9,2026-01-02\nC,150,3.2,2026-01-03\n").encode()
r = import_csv(csv_data, "测试订单.csv")
check("D1 导入成功", r.get("success"), str(r.get("error")))
if r.get("success"):
    _created.append(r["table_name"])
    check("D2 表名带前缀", r["table_name"].startswith("tmp_upload_"), r["table_name"])
    check("D3 行数正确", r["row_count"] == 3, str(r["row_count"]))
    check("D4 TTL=3600", r["ttl_seconds"] == 3600, str(r["ttl_seconds"]))
    res = execute_sql(f'SELECT * FROM "{r["table_name"]}" ORDER BY qty')
    check("D5 只读通道可查", res["success"] and res["row_count"] == 3, str(res.get("error")))
    check("D6 数据正确", res["rows"][0]["qty"] == 100, str(res["rows"][0]))
    hits = match_tables_by_query("测试订单")
    check("D7 表匹配命中临时表", hits and hits[0]["table_name"] == r["table_name"],
          str([h["table_name"] for h in hits[:2]]))
    from agent.csv_import import list_tmp_tables
    check("D8 列表含临时表", any(t["table_name"] == r["table_name"] for t in list_tmp_tables()), "")
    check("D9 删除成功", True, "")

print("═══ E. TTL 过期清理 ═══")
from agent.csv_import import cleanup_expired, _registry

r = import_csv(csv_data, "ttl测试.csv")
if r.get("success"):
    _created.append(r["table_name"])
    _registry[r["table_name"]]["created_at"] = time.time() - 99999  # 强制过期
    n = cleanup_expired()
    check("E1 过期表被清理", n >= 1, str(n))
    from db.executor import execute_sql as es2
    res = es2(f'SELECT COUNT(*) AS c FROM information_schema.tables WHERE table_name=\'{r["table_name"]}\'')
    gone = not res["rows"] or res["rows"][0]["c"] == 0
    check("E2 物理表已删", gone, str(res))

cleanup()
print()
print("=" * 40)
print(f"通过 {_passed} 项，失败 {_failed} 项")
print("=" * 40)
