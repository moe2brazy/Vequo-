# -*- coding: utf-8 -*-
"""验证器口径一致性自检 —— 防止验证器与指标注册表再次静默跑偏。

背景：tests/yans_verifier.py 的 REF_AGG 是「独立权威参照口径」，被设计为
不依赖 metric_compiler。但它必须与 agent/metric_registry.py 声明的业务口径
**同值**，否则会把产品正确输出误判为「数值不一致」（2026-10 曾发生：产量、
库存量、严重缺陷数 3 处口径陈旧，导致 810 题中误报 90 处）。

本测试不复用两边的表达式文本（写法不同），而是**各自在真库上执行一次**，
比较标量结果。只要数值不同即判失败。

运行: python tests/test_verifier_consistency.py
退出码: 0 = 全部一致；1 = 存在口径分歧
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import switch_database
switch_database({"db_type": "postgresql", "host": "localhost", "port": 5432,
                 "name": "yans", "user": "postgres", "password": "123456"})

from agent.metric_registry import BUILTIN_METRICS
from db.executor import execute_sql
from yans_verifier import REF_AGG

_PLACEHOLDER = re.compile(r"\{([^}]*)\}")
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "verifier_consistency_report.txt")


def resolve_placeholder(expr: str) -> str:
    """把注册表的 {a|b} 可选项解析为第一项（yans 库的实际列名）。"""
    def _pick(m):
        opts = [o.strip() for o in m.group(1).split("|") if o.strip()]
        return opts[0] if opts else ""
    return _PLACEHOLDER.sub(_pick, expr)


def first_row(sql: str):
    """执行 SQL，返回 (首行取值元组, 错误信息)。"""
    r = execute_sql(sql)
    if not r.get("success"):
        return None, str(r.get("error", ""))[:100]
    rows = r.get("rows") or []
    if not rows:
        return None, "no rows"
    return tuple(rows[0].values()), ""


def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    # 注册表： (表裸名, 指标名) -> 表达式
    reg = {}
    for m in BUILTIN_METRICS:
        name = m.get("name", "")
        expr = m.get("sql_expression") or ""
        if not name or not expr:
            continue
        for t in (m.get("tables") or []):
            reg.setdefault((t.split(".")[-1], name), resolve_placeholder(expr))

    compared, mismatches, errors, no_ref = [], [], [], []

    for (table, metric), ref_expr in sorted(REF_AGG.items()):
        reg_expr = reg.get((table, metric))
        if reg_expr is None:
            no_ref.append(f"{table}.{metric}")
            continue

        vals, err = first_row(
            f'SELECT ({reg_expr}) AS reg_v, ({ref_expr}) AS ref_v FROM {table} f')

        if vals is None or len(vals) != 2:
            # 退化：两边分别单独执行，定位问题出在哪一边
            a, ea = first_row(f'SELECT ({reg_expr}) AS v FROM {table} f')
            b, eb = first_row(f'SELECT ({ref_expr}) AS v FROM {table} f')
            if a is not None and b is not None:
                vals = (a[0], b[0])
            else:
                errors.append(f"{table}.{metric} | 合并SQL失败({err}) | "
                              f"reg={'OK' if a is not None else ea} | "
                              f"ref={'OK' if b is not None else eb}")
                continue

        fa, fb = to_float(vals[0]), to_float(vals[1])
        if fa is None or fb is None:
            errors.append(f"{table}.{metric} | 非数值 reg={vals[0]} ref={vals[1]}")
            continue

        if abs(fa - fb) > max(1e-6, abs(fb) * 1e-6):
            mismatches.append((table, metric, fa, fb, reg_expr, ref_expr))
        else:
            compared.append(f"{table}.{metric}")

    lines = ["═══ 验证器口径一致性自检 ═══",
             f"✅ 口径一致: {len(compared)}",
             f"🔴 口径分歧: {len(mismatches)}",
             f"⚪ 注册表无对应指标（仅验证器有，跳过）: {len(no_ref)}",
             f"⚠️  执行异常: {len(errors)}", ""]

    lines.append("── ✅ 一致明细 ──")
    for n in compared:
        lines.append(f"  {n}")

    if mismatches:
        lines += ["", "── 🔴 口径分歧明细（验证器必须同步为注册表口径）──"]
        for table, metric, fa, fb, re_, rf in mismatches:
            lines.append(f"  {table}.{metric}")
            lines.append(f"      注册表: {re_[:120]}  → {fa}")
            lines.append(f"      验证器: {rf[:120]}  → {fb}")

    if errors:
        lines += ["", "── ⚠️ 执行异常 ──"]
        for e in errors:
            lines.append(f"  {e}")

    if no_ref:
        lines += ["", "── ⚪ 仅验证器存在的指标（无注册表口径可比，人工核对）──"]
        for n in no_ref:
            lines.append(f"  {n}")

    ok = not mismatches and not errors
    lines += ["", "结论: " + ("通过 —— 验证器口径与指标注册表一致" if ok
                              else "失败 —— 存在口径分歧，请同步后重跑")]

    text = "\n".join(lines)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
