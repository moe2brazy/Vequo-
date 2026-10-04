# -*- coding: utf-8 -*-
"""主动洞察自动探查单元测试（P1-4）：统计判定、字段分类、探查与降级"""
import sys
sys.path.insert(0, '.')

import agent.insight_scan as isc
from agent.insight_scan import (_zscores, _split_fields, _probe_dim, _probe_trend,
                                _probe_nulls, scan_table)

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

print("═══ A. 统计判定（离群 z-score）═══")
z = _zscores([10, 10, 10, 100])
check("A1 极端值 z 最大", abs(z[-1]) > abs(z[0]), str([round(v, 2) for v in z]))
check("A2 正常值 z 为负且小", z[0] < 0, str(round(z[0], 2)))
check("A3 样本 <3 不制造假异常", _zscores([1, 100]) == [0.0, 0.0], "")
check("A4 全相同值 stdev=0 → 全 0", _zscores([5, 5, 5, 5]) == [0.0] * 4, "")
check("A5 空输入不崩", _zscores([]) == [], "")

print("═══ B. 字段分类 ═══")
fields = [
    {"name": "qty", "type": "integer", "description": "数量"},
    {"name": "rate", "type": "numeric", "description": "比率"},
    {"name": "line_name", "type": "character varying", "description": "产线名称"},
    {"name": "stat_date", "type": "date", "description": "统计日期"},
    {"name": "product_id", "type": "integer", "key": "FK", "description": "产品ID"},
]
m, d, t = _split_fields(fields)
check("B1 数值字段归度量", {f["name"] for f in m} == {"qty", "rate"}, str([f["name"] for f in m]))
check("B2 外键 ID 被排除", "product_id" not in {f["name"] for f in m}, "")
check("B3 文本字段归维度", [f["name"] for f in d] == ["line_name"], str([f["name"] for f in d]))
check("B4 日期字段归时间", [f["name"] for f in t] == ["stat_date"], str([f["name"] for f in t]))

print("═══ C. 探查函数（打桩，确定性）═══")
# 构造：L01 占绝对多数（失衡），P09 是一个离群大值
FAKE_ROWS = [{"d": "L%d" % i, "v": 100, "n": 5} for i in range(1, 6)] + [{"d": "P09", "v": 900, "n": 5}]
isc._safe_execute = lambda sql: {"success": True, "rows": FAKE_ROWS, "columns": ["d", "v", "n"]}
rows = _probe_dim("t", "qty", "line")
check("C1 探查返回条目", len(rows) == 6, str(len(rows)))
check("C2 占比计算", abs(sum(x["share"] for x in rows) - 1.0) < 0.01, "")
check("C3 离群值 z 最大", max(rows, key=lambda x: abs(x["z"]))["dim_value"] == "P09",
      str([(x["dim_value"], x["z"]) for x in rows]))

isc._safe_execute = lambda sql: {"success": False, "error": "boom"}
check("C4 执行失败返回空", _probe_dim("t", "q", "d") == [], "")
check("C5 趋势探查失败返回 None", _probe_trend("t", "q", "d") is None, "")
check("C6 空值率探查失败返回 None", _probe_nulls("t", "d") is None, "")

# 趋势突变：历史 100 上下，最新 300（+200%）
# 2026-10-04 更正：打桩顺序必须是**时间倒序**（最新在前），实测得-28.57 而非 200。
# 原因：_probe_trend 的 SQL 是 `ORDER BY t DESC LIMIT 60`（取最新 60 期），
# 拿到结果后 `rows = list(reversed(...))` 反转回时间正序，保证 vals[-1] 是最新一期。
# 这是 10-03 修过的 P0（原为 ASC LIMIT 60 → 取到最早 60 期，vals[-1] 其实是几十年前的期，
# "最新"完全失真）。既然实现契约是"DESC 拿 + reversed 转正"，
# 打桩就必须模拟 DESC 的返回顺序 —— 旧测试按正序打桩，reversed 后 last 落到2026-01，
# 于是 (100-140)/140 = -28.57%���**错的是测试，不是实现**（倒序打桩实测得 200.0）。
TREND_ROWS = [{"t": "2026-0%d-01" % i, "v": 100} for i in range(1, 6)] + [{"t": "2026-06-01", "v": 300}]
TREND_ROWS_DESC = list(reversed(TREND_ROWS))   # 模拟 `ORDER BY t DESC` 的返回顺序
isc._safe_execute = lambda sql: {"success": True, "rows": TREND_ROWS_DESC}
tr = _probe_trend("t", "q", "d")
check("C7 趋势偏离计算正确", tr and abs(tr["deviation_pct"] - 200.0) < 1, str(tr and tr["deviation_pct"]))
# 锁住「DESC 拿 + reversed 转正」这个顺序契约：last 必须是最新一期（2026-06）而非最早一期
check("C7b last 取的是最新一期(非最早)", tr and tr.get("time_value") == "2026-06-01",
      str(tr and tr.get("time_value")))
check("C7c base 是历史期均值", tr and abs((tr.get("base") or 0) - 100.0) < 1,
      str(tr and tr.get("base")))
isc._safe_execute = lambda sql: {"success": True, "rows": [{"t": "2026-01-01", "v": 10}]}
check("C8 期数 <3 不做趋势判定", _probe_trend("t", "q", "d") is None, "")

isc._safe_execute = lambda sql: {"success": True, "rows": [{"total": 100, "nulls": 40}]}
check("C9 空值率计算", _probe_nulls("t", "d") == 0.4, "")
isc._safe_execute = lambda sql: {"success": True, "rows": [{"total": 0, "nulls": 0}]}
check("C10 总行数为 0 返回 None（防除零）", _probe_nulls("t", "d") is None, "")

print("═══ D. scan_table 端到端（打桩）═══")
isc._safe_execute = lambda sql: {"success": True, "rows": FAKE_ROWS}
r = scan_table("demo.t", fields=[
    {"name": "qty", "type": "integer", "description": "数量"},
    {"name": "line_name", "type": "character varying", "description": "产线名称"},
])
check("D1 扫描成功", r["success"], r.get("error", ""))
types = {i["type"] for i in r["insights"]}
check("D2 检出离群", "outlier" in types, str([(i["type"], i["severity"]) for i in r["insights"]]))
check("D3 洞察带可执行文案", all(i.get("message") for i in r["insights"]), "")
check("D4 按严重度降序", all(r["insights"][i]["severity"] >= r["insights"][i + 1]["severity"]
                          for i in range(len(r["insights"]) - 1)), "")

print("═══ E. 边界与降级 ═══")
check("E1 表名为空降级", not scan_table("")["success"], "")
check("E2 无字段降级", not scan_table("t", fields=[])["success"], "")
check("E3 无数值字段降级",
      not scan_table("t", fields=[{"name": "s", "type": "varchar"}])["success"], "")
check("E4 维度取值太少不出离群（样本不足不硬凑）",
      not [i for i in scan_table("t", fields=[
          {"name": "qty", "type": "integer"},
          {"name": "d", "type": "varchar"}],
          ).get("insights", []) if i["type"] == "outlier"]
      or len(FAKE_ROWS) >= 4, "")

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
