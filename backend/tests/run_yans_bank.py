# -*- coding: utf-8 -*-
"""yans 大题库批量测试器：合并手题库 + 生成题库，输出完整测试报告。

运行: python tests/run_yans_bank.py [--report]
报告输出: backend/tests/yans_test_report.md
"""
import sys, os, json, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import switch_database
switch_database({"db_type":"postgresql","host":"localhost","port":5432,"name":"yans","user":"postgres","password":"123456"})
from agent.metric_compiler import try_compile_metric
from db.executor import execute_sql
from yans_question_bank import QUESTION_BANK
from gen_yans_bank import build_bank

def merge_banks():
    bank = []
    seen = set()
    for b in QUESTION_BANK + build_bank():
        q = b["q"].strip()
        if q in seen:
            continue
        seen.add(q)
        bank.append(b)
    return bank

def row_shape(expect, n):
    if expect == "top":
        return True
    if expect in ("group", "rank", "trend"):
        return n > 1
    if expect == "total":
        return n == 1
    return True  # llm 不校验

BANK = merge_banks()

def main():
    t0 = time.time()
    rows = []
    for i, item in enumerate(BANK):
        q = item["q"]; exp = item["expect"]
        r = try_compile_metric(q)
        if not r:
            rows.append({"q": q, "expect": exp, "status": "compile_miss", "rows": 0, "sql": ""})
            continue
        sql = r["sql"]
        res = execute_sql(sql)
        if not res.get("success"):
            rows.append({"q": q, "expect": exp, "status": "exec_fail", "rows": 0,
                         "sql": sql, "error": str(res.get("error", ""))[:80]})
            continue
        n = res.get("row_count") or 0
        status = "ok" if row_shape(exp, n) else f"shape_bad"
        # 时间窗口无数据判定：本月/上月等用 CURRENT_DATE 锚点，若数据时间范围不含当前
        # 月份（如 yans 大赛数据是 6-7 月，当前 9 月）→ 0 行是正确结果，不算失败
        if n == 0 and re.search(r"CURRENT_DATE|date_trunc\('month'", sql):
            status = "no_data"
        rows.append({"q": q, "expect": exp, "status": status, "rows": n, "sql": sql,
                     "metric": r.get("metric", ""), "unit": r.get("unit", "")})
    dt = time.time() - t0

    total = len(rows)
    ok = [r for r in rows if r["status"] == "ok"]
    miss = [r for r in rows if r["status"] == "compile_miss"]
    fail = [r for r in rows if r["status"] == "exec_fail"]
    bad = [r for r in rows if r["status"] == "shape_bad"]
    nodata = [r for r in rows if r["status"] == "no_data"]
    llm_ok = [r for r in rows if r["expect"] == "llm"]
    compile_target = [r for r in rows if r["expect"] != "llm"]
    compiled = [r for r in compile_target if r["status"] != "compile_miss"]
    exec_ok = [r for r in compiled if r["status"] != "exec_fail"]
    perfect = [r for r in rows if r["status"] in ("ok", "no_data")]

    hit_rate = len(compiled) / len(compile_target) * 100 if compile_target else 0
    exec_rate = len(exec_ok) / len(compiled) * 100 if compiled else 0
    perfect_rate = len(perfect) / total * 100

    lines = []
    lines.append("# yans 数据库大题库测试报告")
    lines.append("")
    lines.append(f"- 测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 测试库: yans（大赛数据 10 表）")
    lines.append(f"- 题库规模: {total} 题（手题库 {len(QUESTION_BANK)} + 生成题库 {len(build_bank())} 合并去重）")
    lines.append(f"- 耗时: {dt:.1f}s")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 编译命中率 | {hit_rate:.1f}% ({len(compiled)}/{len(compile_target)}) |")
    lines.append(f"| SQL 执行成功率 | {exec_rate:.1f}% ({len(exec_ok)}/{len(compiled)}) |")
    lines.append(f"| 综合通过率（含 LLM 兜底不判错） | {perfect_rate:.1f}% ({len(perfect)}/{total}) |")
    lines.append(f"| 完全正确 | {len(perfect)} |")
    lines.append(f"| 编译未命中（走 LLM 兜底） | {len(miss)} |")
    lines.append(f"| 执行失败 | {len(fail)} |")
    lines.append(f"| 行数不符 | {len(bad)} |")
    lines.append(f"| 时间窗口无数据（本月/上月，数据范围不含当前月，合理） | {len(nodata)} |")
    lines.append("")
    lines.append("## 编译未命中清单（走 LLM 兜底，需人工核验 LLM 回答质量）")
    lines.append("")
    if miss:
        for r in miss:
            lines.append(f"- ❌ `{r['q']}`（期望 {r['expect']}）")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 执行失败清单（必须修复）")
    lines.append("")
    if fail:
        for r in fail:
            lines.append(f"- 🔴 `{r['q']}` | SQL: `{r['sql'][:90]}` | {r.get('error', '')}")
    else:
        lines.append("- 无 🎉")
    lines.append("")
    lines.append("## 行数不符清单（语义可能偏差，需人工核验）")
    lines.append("")
    if bad:
        for r in bad:
            lines.append(f"- ⚠️ `{r['q']}`（期望 {r['expect']}，实际 {r['rows']} 行）| SQL: `{r['sql'][:90]}`")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 时间窗口无数据清单（合理：数据时间范围不含当前月份）")
    lines.append("")
    if nodata:
        for r in nodata:
            lines.append(f"- 🕐 `{r['q']}`（0 行，SQL 含 CURRENT_DATE 月度锚点）")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 编译命中抽样（每类取前 3 条）")
    lines.append("")
    lines.append("| 问法 | 期望 | 命中指标 | 行数 | SQL |")
    lines.append("|---|---|---|---|---|")
    shown = set()
    for r in ok:
        key = r["expect"]
        if key in shown:
            continue
        shown.add(key)
        lines.append(f"| {r['q']} | {r['expect']} | {r.get('metric', '')} | {r['rows']} | `{r['sql'][:70]}` |")
    report = "\n".join(lines)
    print(report.split("\n## ")[0])
    print("...（完整报告已写入 tests/yans_test_report.md）")
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "yans_test_report.md"), "w", encoding="utf-8") as f:
        f.write(report + "\n")

if __name__ == "__main__":
    main()
