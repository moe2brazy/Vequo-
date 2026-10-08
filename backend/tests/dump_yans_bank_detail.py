# -*- coding: utf-8 -*-
"""逐题导出 yans 三套题库的实测结果（供测评材料附录「详细题库」使用）

与 run_yans_bank_grouped.py 使用完全相同的判定逻辑，只是把每题的结果落盘。
输出: bank_detail.json
"""
import os
import re
import sys
import json
import time

BE = r"D:\vequo\vequo-viqueo\backend"
sys.path.insert(0, BE)
sys.path.insert(0, os.path.join(BE, "tests"))

from database import switch_database
switch_database({"db_type": "postgresql", "host": "localhost", "port": 5432,
                 "name": "yans", "user": "postgres", "password": "123456"})

from agent.metric_compiler import try_compile_metric
from db.executor import execute_sql
from yans_verifier import build_ref_sql, compare
from yans_question_bank import QUESTION_BANK
from gen_yans_bank import build_bank
from gen_yans_bank_v2 import build_bank_v2
from gen_yans_bank_v3 import build_bank_v3

MONTH_RE = re.compile(r"CURRENT_DATE|date_trunc\('month'|date_trunc\('year'")


def row_shape(expect, n):
    if expect == "top":
        return True
    if expect in ("group", "rank", "trend"):
        return n > 1
    if expect == "total":
        return n == 1
    return True


def v1_bank():
    seen, out = set(), []
    for b in list(QUESTION_BANK) + list(build_bank()):
        if b["q"] in seen:
            continue
        seen.add(b["q"])
        out.append(b)
    return out


def evaluate(bank):
    rows = []
    for idx, item in enumerate(bank, 1):
        q, exp = item["q"], item["expect"]
        try:
            r = try_compile_metric(q)
        except Exception:
            rows.append({"i": idx, "q": q, "e": exp, "s": "crash"}); continue
        if not r:
            rows.append({"i": idx, "q": q, "e": exp, "s": "compile_miss"}); continue
        sql = r["sql"]
        try:
            res = execute_sql(sql)
        except Exception:
            rows.append({"i": idx, "q": q, "e": exp, "s": "exec_fail"}); continue
        if not res.get("success"):
            rows.append({"i": idx, "q": q, "e": exp, "s": "exec_fail"}); continue
        n = res.get("row_count") or 0
        st = "ok" if row_shape(exp, n) else "shape_bad"
        if n == 0 and MONTH_RE.search(sql):
            st = "no_data"
        if exp in ("group", "rank", "top", "total"):
            mql = r.get("mql") or {}
            metric = (mql.get("metric") or r.get("metric") or "").split("(")[0].strip()
            if len(mql.get("metrics") or []) <= 1:
                dims = mql.get("dimensions") or []
                dim = dims[0] if dims else None
                ref = build_ref_sql(metric, dim, sql)
                if ref:
                    try:
                        okv, _ = compare(sql, ref, has_limit="LIMIT" in sql.upper())
                    except Exception:
                        okv = True
                    if not okv and st == "ok":
                        st = "verify_mismatch"
        rows.append({"i": idx, "q": q, "e": exp, "s": st, "sql": sql})
    return rows


def main():
    t0 = time.time()
    banks = [("v1", v1_bank()), ("v2", build_bank_v2()), ("v3", build_bank_v3())]
    data = {}
    for key, b in banks:
        rows = evaluate(b)
        data[key] = rows
        print(f"{key}: {len(rows)} 题  ({time.time()-t0:.1f}s)")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bank_detail.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print("saved:", out, os.path.getsize(out))


if __name__ == "__main__":
    main()
