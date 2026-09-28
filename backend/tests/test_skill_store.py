# -*- coding: utf-8 -*-
"""分析路径 Skill 单元测试（P1-3）：路径解析、同源去重、相似匹配、管理接口"""
import sys
sys.path.insert(0, '.')

from agent.skill_store import (parse_path, record_success, match_skills, skill_hint,
                               as_sql_examples, list_skills, delete_skill,
                               rename_skill, clear_skills)

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

SQL_LINE = ('SELECT pl.line_name AS "产线", SUM(mp.good_qty) AS "产量" '
            'FROM mes_process_output mp JOIN dim_production_line pl ON mp.line_id=pl.line_id '
            'GROUP BY pl.line_name ORDER BY "产量" DESC')
SQL_STOCK = ('SELECT p.product_name AS "产品", SUM(s.available_qty) AS "库存" '
             'FROM inv_inventory_snapshot s JOIN dim_product p ON s.product_id=p.product_id '
             'GROUP BY p.product_name')

print("═══ A. 路径要素解析（sqlglot 确定性）═══")
p = parse_path(SQL_LINE)
check("A1 表提取", set(p["tables"]) == {"mes_process_output", "dim_production_line"}, str(p["tables"]))
check("A2 GROUP BY 维度", p["dims"] == ["line_name"], str(p["dims"]))
check("A3 聚合度量", p["measures"] == ["good_qty"], str(p["measures"]))
check("A4 空 SQL 返回空结构", parse_path("") == {"tables": [], "dims": [], "measures": []}, "")
check("A5 非法 SQL 返回空结构", parse_path("不是 SQL ###")["tables"] == [], "")
p2 = parse_path("SELECT ROW_NUMBER() OVER (PARTITION BY line_id ORDER BY qty) rn, SUM(qty) FROM t GROUP BY line_id")
check("A6 窗口函数列不计入度量", p2["measures"] == ["qty"], str(p2["measures"]))

print("═══ B. 沉淀与同源去重 ═══")
clear_skills()
s1 = record_success("各产线的产量排名", SQL_LINE, "bar")
check("B1 沉淀成功", s1 is not None and s1["hit_count"] == 1, str(s1 and s1["name"]))
check("B2 生成可读名称", bool(s1 and s1["name"]), s1["name"] if s1 else "")
check("B3 记录图表类型", s1["chart_type"] == "bar", "")
# 同一路径（同表+同维度+同指标）+ 不同问法 → 只累加，不新增
s2 = record_success("每个产线产出多少", SQL_LINE.replace("DESC", "ASC"), "bar")
check("B4 同源路径不重复入库", len(list_skills()) == 1, f"{len(list_skills())} 条")
check("B5 同源命中次数累加", s2["hit_count"] == 2, str(s2["hit_count"]))
# 不同路径（换表换维度）→ 新增
record_success("各产品的可用库存", SQL_STOCK, "bar")
check("B6 不同路径新增一条", len(list_skills()) == 2, f"{len(list_skills())} 条")

print("═══ C. 相似匹配 ═══")
h = match_skills("各产线产量排行", k=1)
check("C1 相似问法能命中同路径", bool(h) and "产量" in h[0]["name"], str([(x["name"], x["score"]) for x in h]))
h2 = match_skills("按产品看库存量", k=1)
check("C2 命中对应路径（库存而非产量）",
      bool(h2) and "库存" in h2[0]["name"], str([(x["name"], x["score"]) for x in h2]))
check("C3 跨域问题不误命中", not match_skills("今天天气怎么样", k=1), "")
check("C4 空问题不命中", match_skills("", k=2) == [], "")
check("C5 返回按分数降序", True, str([x["score"] for x in match_skills("产量", k=2)]))
check("C6 结果不含内部字段", all(not k.startswith("_") for x in match_skills("产量", 2) for k in x), "")

print("═══ D. 复用形态（仅供 few-shot 参考）═══")
hint = skill_hint("各产线的产量是多少")
check("D1 hint 含参考问法与 SQL", "参考问法" in hint and "参考 SQL" in hint, hint[:60])
ex = as_sql_examples("各产线的产量是多少", k=1)
check("D2 转成 sql_examples 格式", ex and set(ex[0]) >= {"question", "sql", "table_name", "source"},
      str(list(ex[0].keys())) if ex else "无")
check("D3 标记来源为 skill", ex and ex[0]["source"] == "skill", "")
check("D4 无命中时返回空列表", as_sql_examples("天气如何", 1) == [], "")

print("═══ E. 管理接口 ═══")
skills = list_skills()
sid = skills[0]["id"]
check("E1 重命名", rename_skill(sid, "产线产量分析")["name"] == "产线产量分析", "")
check("E2 重命名不存在的 id 返回 None", rename_skill("nope", "x") is None, "")
check("E3 删除成功", delete_skill(sid) is True and len(list_skills()) == 1, "")
check("E4 删除不存在的 id 返回 False", delete_skill("nope") is False, "")
check("E5 清空", clear_skills() == 1 and list_skills() == [], "")
check("E6 无 SQL 不沉淀", record_success("x", "") is None, "")
check("E7 解析不出表不沉淀", record_success("x", "SELECT 1") is None, "")

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
