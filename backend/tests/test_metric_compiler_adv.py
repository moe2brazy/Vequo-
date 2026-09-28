# -*- coding: utf-8 -*-
"""编译器高级能力单元测试（P0-2 多表编译 / P0-3 指标类型累计 / P0-4 层级钻取）

覆盖：
- P0-3 cumulative：本年累计/近N天累计/裸累计不误触（合计语义保持）
- P0-2 compile_plan：库存周转天数（双事实表 CTE）+ 时间占位符填充
- P0-4 dim_hierarchy：drillable 输出 / 父级值过滤下钻（一车间各产线）/ 下钻词切换层级
- P0-2 L3 bridge：默认关闭
- 指标类型标注推断
- 回归：普通维度/趋势/时间过滤编译不受影响
"""
import sys
sys.path.insert(0, '.')

# 固定测试基准库（postgres 演示库），不依赖 .env 当前连接
import database
database.switch_database({"db_type": "postgresql", "host": "localhost", "port": 5432,
                          "name": "postgres", "user": "postgres", "password": "123456"})
from agent.metric_compiler import try_compile_metric
from agent.metric_registry import get_all_metrics

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

print("═══ P0-3 累计窗口编译 ═══")
r = try_compile_metric("本年累计产量是多少")
check("本年累计产量 → 累计编译", r and r.get("compiled") and "OVER" in r["sql"])
check("累计 mql 打标 compiled_by=cumulative_compile", r and (r.get("mql") or {}).get("compiled_by") == "cumulative_compile")
check("累计 window=YTD", r and (r.get("mql") or {}).get("window") == "本年至今(YTD)")
r = try_compile_metric("近7天累计销售金额")
check("近7天累计销售金额 → 累计编译", r and r.get("compiled") and "近7天" in (r.get("mql") or {}).get("window", ""))
r = try_compile_metric("各车间的累计产量是多少")
check("裸累计(合计语义)不触发累计编译", r is None or (r.get("mql") or {}).get("compiled_by") != "cumulative_compile")

print("═══ P0-2 compile_plan 多表编译（列级保护）═══")
# 库存周转天数依赖 inv_inventory_snapshot.quantity + test_orders 表，
# postgres/yans 演示库均无 quantity 列 → required_cols 列级保护应过滤该指标（回退 LLM）。
# compile_plan 机制本身在有 quantity 列的库（如 123 库 test_* 导入场景）才可编译。
r = try_compile_metric("库存周转天数是多少")
check("库存周转天数（无 quantity 列）→ 指标不适用回退 LLM", r is None, r and r["sql"][:60])
from agent.metric_registry import find_metrics
r = find_metrics("库存周转天数是多少", limit=3)
check("库存周转天数 → find_metrics 被列级保护过滤", not r, str([m["name"] for m in (r or [])]))
r = try_compile_metric("近30天库存周转天数")
check("近30天库存周转天数 → 同样回退 LLM", r is None)
r = try_compile_metric("各产品的库存周转天数")
check("compile_plan 维度意图保护（各产品→回退 LLM）", r is None)

print("═══ P0-4 维度层级钻取 ═══")
r = try_compile_metric("各车间的产量是多少")
check("各车间产量 → 编译", r and r.get("compiled") and "workshop" in r["sql"])
check("drillable.next_level=产线", r and (r.get("drillable") or {}).get("next_level") == "产线")
r = try_compile_metric("一车间各产线的产量是多少")
check("一车间各产线 → 父级值过滤下钻", r and r.get("compiled") and "workshop" in r["sql"] and "一车间" in r["sql"])
r = try_compile_metric("各产线的产量是多少")
check("末级维度无 drillable", r and r.get("compiled") and not r.get("drillable"))
r = try_compile_metric("各设备类型的停机时长")
check("设备类型停机时长 → drillable.next=设备", r and r.get("compiled") and (r.get("drillable") or {}).get("next_level") == "设备")
r = try_compile_metric("各车间的产量，下钻到产线")
check("下钻词 → 切到下一级产线", r and r.get("compiled") and "line_name" in r["sql"])

print("═══ P0-2 L3 桥接（默认关闭）═══")
r = try_compile_metric("各产品的产量和销售数量")
check("L3 默认关闭不触发", r is None or (r.get("mql") or {}).get("compiled_by") != "multi_fact_bridge")

print("═══ 指标类型标注（P0-3）═══")
types = {m["name"]: m.get("metric_type") for m in get_all_metrics()}
check("良率 → ratio", types.get("良率") == "ratio", str(types.get("良率")))
check("产量 → simple", types.get("产量") == "simple", str(types.get("产量")))
check("库存周转天数 → ratio", types.get("库存周转天数") == "ratio", str(types.get("库存周转天数")))

print("═══ 维度补齐（2026-09-01：覆盖未注册维度的简单分组题）═══")
_r = try_compile_metric("各工单(work_order_id)的总合格产量是多少")
check("工单维度 → 按 work_order_id 分组", _r and _r.get("compiled") and "work_order_id" in _r["sql"], _r and _r["sql"][:60])
_r = try_compile_metric("各严重程度(severity_level)的缺陷数量是多少")
# yans 命中「缺陷件数」SUM(defect_qty)；postgres 无 defect_qty 列 → 列保护降级为「缺陷数」COUNT(*)。
# 两者都按 severity 列分组，断言分组列 + 任一聚合形态。
check("严重程度别名 → 按 severity 列分组（SUM 件数或 COUNT 降级）",
      _r and _r.get("compiled") and "severity" in _r["sql"] and
      ("SUM(COALESCE(defect_qty,0))" in _r["sql"] or "COUNT(*)" in _r["sql"]), _r and _r["sql"][:60])
_r = try_compile_metric("停机原因分析")
check("停机原因分析 → 按原因列分组（列名自适应 reason/downtime_reason）",
      _r and _r.get("compiled") and "COUNT(*)" in _r["sql"] or (_r and _r.get("compiled") and "SUM(downtime_minutes)" in _r["sql"]),
      _r and _r["sql"][:60])
_r = try_compile_metric("各工单状态(order_status)的工单数量是多少")
check("工单状态别名 → 按 status 列分组（列名自适应）",
      _r and _r.get("compiled") and ("status" in _r["sql"] or "order_status" in _r["sql"]), _r and _r["sql"][:60])
_r = try_compile_metric("各日期(stat_date)的总合格产量是多少")
check("各日期 → 时间粒度分组（to_char）", _r and _r.get("compiled") and "to_char" in _r["sql"], _r and _r["sql"][:60])
_r = try_compile_metric("各产线(line_id)的产出记录数是多少")
check("产出记录数 → COUNT(*) 而非 SUM", _r and _r.get("compiled") and "COUNT(*)" in _r["sql"], _r and _r["sql"][:60])

print("═══ Top-1 查询与二维分组（2026-09-01 第二批）═══")
_r = try_compile_metric("不良数量最多的产线是哪条")
check("Top-1 最多 → ORDER BY DESC LIMIT 1", _r and _r.get("compiled") and "LIMIT 1" in _r["sql"] and "DESC" in _r["sql"], _r and _r["sql"][:60])
_r = try_compile_metric("可用库存最高的产品是哪个")
check("Top-1 最高 → DESC LIMIT 1", _r and _r.get("compiled") and "LIMIT 1" in _r["sql"], _r and _r["sql"][:60])
_r = try_compile_metric("各车间(workshop_name)的工单数量是多少")
check("工单域车间维度 → GROUP BY workshop 列（列名自适应）",
      _r and _r.get("compiled") and ("workshop" in _r["sql"]), _r and _r["sql"][:60])
_r = try_compile_metric("各车间(workshop_name)的停机总时长是多少")
check("停机域车间维度 → GROUP BY workshop 列（列名自适应）",
      _r and _r.get("compiled") and ("workshop" in _r["sql"]), _r and _r["sql"][:60])
_r = try_compile_metric("各供应商(supplier)的物料总库存是多少")
check("物料总库存 → 命中物料库存量且仅按供应商分组（非供应商×物料）",
      _r and _r.get("compiled") and "SUM(stock_qty)" in _r["sql"] and "material_name" not in _r["sql"], _r and _r["sql"][:80])

print("═══ AVG / COUNT DISTINCT（2026-09-01 第三批）═══")
_r = try_compile_metric("每次检验的平均抽样数是多少")
check("平均抽样数 → AVG(sample_qty)", _r and _r.get("compiled") and "AVG(sample_qty)" in _r["sql"], _r and _r["sql"][:60])
_r = try_compile_metric("平均每次停机时长是多少")
check("平均停机时长 → AVG(downtime_minutes)", _r and _r.get("compiled") and "AVG(downtime_minutes)" in _r["sql"], _r and _r["sql"][:60])
_r = try_compile_metric("每次检验的抽样数是多少")
check("无「平均」→ 仍 SUM（不误加 AVG）", _r and _r.get("compiled") and "SUM(sample_qty)" in _r["sql"] and "AVG" not in _r["sql"], _r and _r["sql"][:60])
_r = try_compile_metric("停机记录有多少种原因")
check("多少种原因 → COUNT(DISTINCT 原因列)（列名自适应）",
      _r and _r.get("compiled") and "COUNT(DISTINCT " in _r["sql"] and ("reason" in _r["sql"] or "downtime_reason" in _r["sql"]), _r and _r["sql"][:60])

print("═══ 回归：既有编译能力不受影响 ═══")
for q in ("各产线的产量", "良率趋势", "本月良率", "近7天停机时长", "各产品的销售金额排名"):
    r = try_compile_metric(q)
    check(f"回归编译: {q}", r is None or r.get("compiled"))
# 「整个X」范围限定词（P0-4 注册车间维度后暴露的回归）：应编译为全量总量而非按车间分组
for q in ("整个车间的总投入数量是多少", "整个车间的合格产量是多少", "整个车间的不良数量是多少"):
    r = try_compile_metric(q)
    check(f"整个X 总量语义: {q}", r is not None and r.get("compiled") and "GROUP BY" not in r["sql"])

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
