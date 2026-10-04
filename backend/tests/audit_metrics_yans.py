# -*- coding: utf-8 -*-
"""指标口径 × yans 库 三路独立审查（schema实查 / SQL试执行 / 语义交叉）——审计报告生成器"""
import io, json, os, re, sys

# 2026-10-04 修复：原三处硬编码旧机器绝对路径
#   D:\vue_first2 (2)\vue_first2\vue_first(2)\vue_first\...
# 在当前仓库（任意机器）上全部失效 —— schema 读不到会直接 FileNotFoundError，
# 脚本根本跑不起来。改为按 __file__ 推导 backend/ 目录。
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_TESTS_DIR)

SCHEMA_PATH = os.path.join(_TESTS_DIR, "yans_schema.json")
SCHEMA = json.load(open(SCHEMA_PATH, encoding="utf-8"))
YANS_TABLES = set(SCHEMA.keys())

# 聚合/函数/关键字/类型字面量（列名校验忽略集）
KW = {"SUM","AVG","COUNT","MIN","MAX","COALESCE","NULLIF","ABS","ROUND","FLOOR","CEIL","CASE","WHEN","THEN","ELSE","END",
      "DISTINCT","CAST","INTEGER","DECIMAL","NUMERIC","AS","TRUE","FALSE","NULL","AND","OR","NOT","IN","IS","BETWEEN","LIKE",
      "EXTRACT","DATE","LOWER","UPPER","OVER","PARTITION","ROW_NUMBER","ORDER","BY","DESC","ASC","LIMIT","::","*","-","+","/"}
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

def expr_cols(expr: str) -> list:
    # 剔除字符串字面量（'in_progress' 等枚举常量不是列），再提 token
    expr2 = re.sub(r"'[^']*'", "''", expr)
    toks = TOKEN_RE.findall(expr2)
    return [t for t in toks if t.upper() not in KW and not t.isdigit()]

def main():
    sys.path.insert(0, _BACKEND_DIR)
    from agent.metric_registry import get_all_metrics
    from db.executor import execute_sql

    metrics = get_all_metrics()
    rows = []
    by_name = {}
    for m in metrics:
        by_name.setdefault(m.get("name"), []).append(m)

    for m in metrics:
        name = m.get("name") or "?"
        tbls = m.get("tables") or []
        tbl = tbls[0] if tbls else ""
        rec = {"name": name, "table": tbl, "expr": m.get("sql_expression") or "", "unit": m.get("unit") or "",
               "src": "user" if m.get("source") == "user" or m.get("owner") else "builtin", "issues": [], "verdict": "PASS"}
        if tbl not in YANS_TABLES:
            rec.update(verdict="NOT_YANS", note=f"主表 {tbl} 不在 yans(10 表) 内 → 跨库条目，yans 场景不可见/不应作为基准")
            rows.append(rec); continue
        cols = {c["n"] for c in SCHEMA[tbl]}
        # ── 1) Schema 列存在性 ──
        miss = [t for t in expr_cols(rec["expr"]) if t not in cols]
        if miss:
            rec["issues"].append(f"列不存在: {sorted(set(miss))} (表 {tbl} 实际列: {sorted(cols)})")
        # ── 2) 试执行 ──
        sql = f"SELECT {rec['expr']} AS v FROM \"{tbl}\""
        try:
            r = execute_sql(sql)
            if not r.get("success"):
                rec["issues"].append(f"执行失败: {str(r.get('error'))[:120]}")
            else:
                rec["value"] = r.get("rows")[0].get("v") if r.get("rows") else None
        except Exception as e:
            rec["issues"].append(f"执行异常: {str(e)[:120]}")
        # ── 3) 语义交叉 ──
        v = rec.get("value")
        if rec["unit"] == "%" and v is not None and not (-2 <= float(v) <= 102):
            rec["issues"].append(f"单位 % 但数值 {v} 超出合理域")
        if rec["unit"] == "%" and ("/" in rec["expr"]) and "NULLIF" not in rec["expr"]:
            rec["issues"].append("含除式但无 NULLIF → 除零风险")
        # 重复/同义（同表同表达式不同名 → 建议合并，防页面/命中重复）
        dup = [o["name"] for o in by_name.get(name, []) if o is not m] or \
              [o for o in metrics if o is not m and (o.get("tables") or [""])[0] == tbl and (o.get("sql_expression") or "") == rec["expr"] and name > (o.get("name") or "")]
        if dup:
            rec["issues"].append(f"与 [{', '.join(d for d in dup if isinstance(d, str))}] 同表同公式（同义重复，建议合并别名/只留其一）")
        rec["verdict"] = "FAIL" if rec["issues"] and any(i.startswith(("列不存在","执行失败","执行异常","单位 %")) for i in rec["issues"]) else ("WARN" if rec["issues"] else "PASS")
        rows.append(rec)

    # 输出报告
    buf = io.StringIO()
    groups = {"PASS": [], "WARN": [], "FAIL": [], "NOT_YANS": []}
    for r in rows: groups[r["verdict"]].append(r)
    def dump(lst):
        for r in lst:
            buf.write(f"- [{r['verdict']}] {r['name']} @ {r['table']} | expr={r['expr'][:80]} | 值={r.get('value')} | unit={r['unit']} | {r.get('src','')}\n")
            for i in r.get("issues", []):
                buf.write(f"    ! {i}\n")
    buf.write(f"== 总 {len(rows)} | PASS {len(groups['PASS'])} | WARN {len(groups['WARN'])} | FAIL {len(groups['FAIL'])} | NOT_YANS(其它库) {len(groups['NOT_YANS'])} ==\n")
    buf.write("\n### FAIL（列错/执行错/数值越界，yans 下必须修）\n"); dump(groups["FAIL"])
    buf.write("\n### WARN（重复/除零风险等，需人工决策）\n"); dump(groups["WARN"])
    buf.write("\n### PASS 名称清单\n")
    buf.write(", ".join(r["name"] for r in groups["PASS"]) + "\n")
    buf.write("\n### NOT_YANS 清单\n")
    buf.write(", ".join(f"{r['name']}@{r['table']}" for r in groups["NOT_YANS"]) + "\n")
    out = buf.getvalue()
    io.open(os.path.join(_TESTS_DIR, "metric_audit_report.txt"), "w", encoding="utf-8").write(out)
    print(out[:6000])

if __name__ == "__main__":
    main()
