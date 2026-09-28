# -*- coding: utf-8 -*-
"""yans 全新题库 v2 批量评测：编译命中率 / 执行成功率 / 独立参照双跑数值对照。
独立参照口径复用 yans_verifier 的 REF_AGG/REF_DIM（手写权威实现，与编译器无关）。
运行: python tests/run_yans_bank_v2.py → 报告 tests/yans_test_report_v2.md
"""
import sys, os, json, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import switch_database
switch_database({"db_type":"postgresql","host":"localhost","port":5432,"name":"yans","user":"postgres","password":"123456"})
from agent.metric_compiler import try_compile_metric
from db.executor import execute_sql
from gen_yans_bank_v2 import build_bank_v2
from yans_verifier import build_ref_sql, compare

def row_shape(expect, n):
    if expect == "top":
        return True
    if expect in ("group", "rank", "trend"):
        return n > 1
    if expect == "total":
        return n == 1
    return True  # llm 不校验

def main():
    t0 = time.time()
    bank = build_bank_v2()
    rows = []
    for item in bank:
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
        status = "ok" if row_shape(exp, n) else "shape_bad"
        if n == 0 and re.search(r"CURRENT_DATE|date_trunc\('month'", sql):
            status = "no_data"
        # 独立参照双跑（group/rank/top/total 全部纳入；多指标对比类跳过）
        verify = ""
        if exp in ("group", "rank", "top", "total"):
            mql = r.get("mql") or {}
            metric = (mql.get("metric") or r.get("metric") or "").split("(")[0].strip()
            if len(mql.get("metrics") or []) <= 1:
                dims = mql.get("dimensions") or []
                dim = dims[0] if dims else None
                ref = build_ref_sql(metric, dim, sql)
                if ref:
                    ok_v, detail = compare(sql, ref, has_limit="LIMIT" in sql.upper())
                    verify = "一致" if ok_v else f"数值不一致|{detail}"
                    if not ok_v and status == "ok":
                        status = "verify_mismatch"
                else:
                    verify = "无参照"
        rows.append({"q": q, "expect": exp, "status": status, "rows": n, "sql": sql,
                     "metric": r.get("metric", ""), "verify": verify})

    total = len(rows)
    ok = [r for r in rows if r["status"] == "ok"]
    miss = [r for r in rows if r["status"] == "compile_miss"]
    fail = [r for r in rows if r["status"] == "exec_fail"]
    bad = [r for r in rows if r["status"] == "shape_bad"]
    vm = [r for r in rows if r["status"] == "verify_mismatch"]
    nodata = [r for r in rows if r["status"] == "no_data"]
    compile_target = [r for r in rows if r["expect"] != "llm"]
    compiled = [r for r in compile_target if r["status"] != "compile_miss"]
    exec_ok = [r for r in compiled if r["status"] != "exec_fail"]
    perfect = [r for r in rows if r["status"] == "ok"]

    hit_rate = len(compiled) / len(compile_target) * 100 if compile_target else 0
    exec_rate = len(exec_ok) / len(compiled) * 100 if compiled else 0
    perfect_rate = len(perfect) / total * 100

    print("═══ yans 全新题库 v2 评测 ═══")
    print(f"总题数: {total}  耗时: {time.time()-t0:.1f}s")
    print(f"✅ 完全正确: {len(ok)}   ❌ 编译未命中: {len(miss)}   🔴 执行失败: {len(fail)}")
    print(f"⚠️ 行数不符: {len(bad)}   🔍 数值不一致(真bug): {len(vm)}   🕐 时间窗口无数据: {len(nodata)}")
    print(f"编译命中率: {hit_rate:.1f}%  执行成功率: {exec_rate:.1f}%  综合通过率: {perfect_rate:.1f}%")
    if vm:
        print("\n── 🔍 数值不一致（独立参照双跑抓到的真 bug）──")
        for v in vm[:12]:
            print(f"  {v['q']} | {v['verify'][:90]}")
            print(f"    SQL: {v['sql'][:90]}")
    if miss:
        print("\n── ❌ 编译未命中 ──")
        for m in miss[:15]:
            print(f"  {m['q']} (期望 {m['expect']})")
    if fail:
        print("\n── 🔴 执行失败 ──")
        for f_ in fail[:10]:
            print(f"  {f_['q']} | {f_['sql'][:80]}")
    if bad:
        print("\n── ⚠️ 行数不符 ──")
        for b_ in bad[:10]:
            print(f"  {b_['q']} (期望 {b_['expect']}, 实际 {b_['rows']} 行)")

    # 报告
    lines = ["# yans 全新题库 v2 评测报告", "",
             f"- 测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 测试库: yans（大赛数据 10 表）", f"- 题库规模: {total} 题（v2 全新风格，与 v1 刻意不重复）",
             f"- 耗时: {time.time()-t0:.1f}s", "",
             "## 总览", "", "| 指标 | 数值 |", "|---|---|",
             f"| 编译命中率 | {hit_rate:.1f}% ({len(compiled)}/{len(compile_target)}) |",
             f"| SQL 执行成功率 | {exec_rate:.1f}% ({len(exec_ok)}/{len(compiled)}) |",
             f"| 综合通过率（含 LLM 兜底不判错） | {perfect_rate:.1f}% ({len(perfect)}/{total}) |",
             f"| 完全正确 | {len(perfect)} |",
             f"| 编译未命中（走 LLM 兜底） | {len(miss)} |",
             f"| 执行失败 | {len(fail)} |", f"| 行数不符 | {len(bad)} |",
             f"| 🔍 数值不一致（独立参照双跑） | {len(vm)} |",
             f"| 时间窗口无数据（合理） | {len(nodata)} |", ""]
    if vm:
        lines.append("## 🔍 数值不一致清单（必须修复）")
        for v in vm:
            lines.append(f"- `{v['q']}` | {v['verify']} | SQL: {v['sql'][:90]}")
        lines.append("")
    if fail:
        lines.append("## 🔴 执行失败清单（必须修复）")
        for f_ in fail:
            lines.append(f"- `{f_['q']}` | {f_['sql'][:90]} | {f_.get('error','')}")
        lines.append("")
    if miss:
        lines.append("## 编译未命中清单（走 LLM 兜底，需人工核验）")
        for m in miss:
            lines.append(f"- `{m['q']}`（期望 {m['expect']}）")
        lines.append("")
    if bad:
        lines.append("## ⚠️ 行数不符清单")
        for b_ in bad:
            lines.append(f"- `{b_['q']}`（期望 {b_['expect']}，实际 {b_['rows']} 行）")
        lines.append("")
    lines.append("## 验证方法")
    lines.append("")
    lines.append("编译 SQL 与独立手写参照 SQL（REF_AGG 聚合 + REF_DIM JOIN，与编译器零关联）分别执行，同窗口（复用 WHERE/LIMIT）逐行归一化对比聚合数值（1e-6 容差，并列 Top 容忍）。")
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "yans_test_report_v2.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n完整报告: tests/yans_test_report_v2.md")

if __name__ == "__main__":
    main()
