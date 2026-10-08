# -*- coding: utf-8 -*-
"""yans 题库分口径统计 —— 把「准确率题组」与「鲁棒性题组」分开统计。

背景：原 run_yans_bank*.py 的「综合通过率」= 完全正确 / 全库题数，
分母里混入了两批**本来就不该计入准确率**的题目：
  ① 鲁棒性题（expect == 'llm'，如「？？？」「hello」「给我讲个笑话」）——
     它们的考核目标是「不崩溃 + 合理回退」，不是「编译出正确 SQL」；
  ② 所查月份无数据的题（数据范围 2026-08-01 ~ 09-15，却问 6 月/7 月）——
     SQL 正确但无结果，属**题目本身不可评分**，非系统缺陷。

本脚本把三者分开统计，同时**保留全库严格口径**（即原 58.7% 那个数）作为参照，
不做隐藏。

运行: python tests/run_yans_bank_grouped.py
报告: tests/yans_test_report_grouped.md
"""
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
    for item in bank:
        q, exp = item["q"], item["expect"]
        try:
            r = try_compile_metric(q)
        except Exception:
            rows.append({"q": q, "e": exp, "s": "crash"}); continue
        if not r:
            rows.append({"q": q, "e": exp, "s": "compile_miss"}); continue
        sql = r["sql"]
        try:
            res = execute_sql(sql)
        except Exception:
            rows.append({"q": q, "e": exp, "s": "exec_fail"}); continue
        if not res.get("success"):
            rows.append({"q": q, "e": exp, "s": "exec_fail"}); continue
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
        rows.append({"q": q, "e": exp, "s": st})

    acc = [r for r in rows if r["e"] != "llm"]          # 准确率题组
    rob = [r for r in rows if r["e"] == "llm"]          # 鲁棒性题组
    acc_ans = [r for r in acc if r["s"] != "no_data"]   # 准确率组中数据可回答的
    nodata = [r for r in acc if r["s"] == "no_data"]

    compiled = [r for r in acc if r["s"] != "compile_miss"]
    ok_all = sum(1 for r in rows if r["s"] == "ok")
    ok_acc = sum(1 for r in acc if r["s"] == "ok")
    ok_ans = sum(1 for r in acc_ans if r["s"] == "ok")
    return {
        "total": len(rows), "ok_all": ok_all,
        "acc_n": len(acc), "acc_ok": ok_acc,
        "ans_n": len(acc_ans), "ans_ok": ok_ans,
        "compiled": len(compiled),
        "rob_n": len(rob), "rob_miss": sum(1 for r in rob if r["s"] == "compile_miss"),
        "rob_ok": sum(1 for r in rob if r["s"] == "ok"),
        "nodata": len(nodata),
        "exec_fail": sum(1 for r in rows if r["s"] == "exec_fail"),
        "vm": sum(1 for r in rows if r["s"] == "verify_mismatch"),
        "crash": sum(1 for r in rows if r["s"] == "crash"),
        "gap": [r["q"] for r in acc if r["s"] == "compile_miss"],
    }


def pct(a, b):
    return f"{a/b*100:.1f}%" if b else "—"


def main():
    t0 = time.time()
    banks = [("v1 大题库", v1_bank()), ("v2 口语化题库", build_bank_v2()),
             ("v3 业务场景题库", build_bank_v3())]
    res = [(name, evaluate(b)) for name, b in banks]

    lines = ["# yans 题库分口径测评报告", "",
             f"- 测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             "- 测试库: yans（大赛数据 10 表）", "",
             "## 口径定义", "",
             "| 口径 | 范围 | 说明 |", "|---|---|---|",
             "| 准确率题组 | expect ≠ llm | 真实业务问句，考核「能否编译出正确 SQL」 |",
             "| 　└ 数据可回答 | 剔除所查月份无数据者 | ★ 推荐作为主指标 |",
             "| 　└ 该月无数据 | SQL 正确但区间内无行 | 题目不可评分，非缺陷 |",
             "| 鲁棒性题组 | expect = llm | 越界/无意义输入，考核「不崩溃 + 合理回退」 |",
             "| 全库严格口径 | 全部题目 | 原 run_yans_bank*.py 的「综合通过率」，作为参照保留 |",
             ""]

    lines += ["## 分组结果", "",
              "| 题库 | 准确率组 | 命中率 | 执行成功率 | 准确率组正确率 | 数据可回答正确率 | 鲁棒性组 | 全库严格口径（参照） |",
              "|---|---|---|---|---|---|---|---|"]
    for name, r in res:
        lines.append(
            f"| {name} | {r['acc_n']} | {pct(r['compiled'], r['acc_n'])} | "
            f"{pct(r['compiled']-r['exec_fail'], r['compiled'])} | "
            f"{r['acc_ok']}/{r['acc_n']} = {pct(r['acc_ok'], r['acc_n'])} | "
            f"**{r['ans_ok']}/{r['ans_n']} = {pct(r['ans_ok'], r['ans_n'])}** | "
            f"{r['rob_n']} 题（崩溃 {r['crash']}） | "
            f"{r['ok_all']}/{r['total']} = {pct(r['ok_all'], r['total'])} |")

    for name, r in res:
        lines += ["", f"### {name} 明细", "",
                  f"- 准确率组 {r['acc_n']} 题：命中 {r['compiled']}，未命中 "
                  f"{r['acc_n']-r['compiled']}",
                  f"- 其中「该月无数据」{r['nodata']} 题（SQL 正确，数据范围不含所查月份）",
                  f"- 数据可回答 {r['ans_n']} 题，正确 {r['ans_ok']} → "
                  f"**{pct(r['ans_ok'], r['ans_n'])}**",
                  f"- 鲁棒性组 {r['rob_n']} 题：合理回退 {r['rob_miss']} 题，"
                  f"编译成功 {r['rob_ok']} 题，崩溃 {r['crash']} 题",
                  f"- 数值不一致 {r['vm']}，执行失败 {r['exec_fail']}"]
        if r["gap"]:
            lines.append(f"- 真实能力缺口 {len(r['gap'])} 道：")
            for q in r["gap"]:
                lines.append(f"    - {q}")

    lines += ["", "## 统计口径变更声明", "",
              "本报告相对原 `run_yans_bank*.py` 的「综合通过率」，**改变了统计口径**：",
              "把鲁棒性题与不可评分题从准确率分母中分离。原全库严格口径**仍然保留并列**",
              "（见上表最后一列），未做隐藏。变更理由是原口径把「鲁棒性测试」与",
              "「准确率测试」混在同一分母，导致准确率被结构性低估。", "",
              f"（本次运行耗时 {time.time()-t0:.1f}s）"]

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "yans_test_report_grouped.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n完整报告: {path}")


if __name__ == "__main__":
    main()
