# -*- coding: utf-8 -*-
"""agent 子模块功能冒烟 v2：用真实导出函数名与签名"""
import sys, io, contextlib, inspect
sys.path.insert(0, '.')
from database import switch_database
switch_database({"db_type":"postgresql","host":"localhost","port":5432,"name":"yans","user":"postgres","password":"123456"})

results = []
def t(name, fn):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            v = fn()
        ok = v is not None and v != "" and v != [] and v != {}
        results.append((name, ok))
        print(f"  {'✅' if ok else '❌'} {name}" + ("" if ok else f" → {str(v)[:70]}"))
    except Exception as e:
        results.append((name, False))
        print(f"  ❌ {name}: {type(e).__name__}: {str(e)[:90]}")

# 1. 图表生成（真实签名）
def c1():
    from agent.chart_agent import generate_chart
    print("   sig:", str(inspect.signature(generate_chart))[:80])
    r = generate_chart(["工序", "良率"], [["A", 98.5], ["B", 99.1]], "各工序良率", {"title":"测试"})
    return r
t("chart_agent.generate_chart", c1)

# 2. 洞察扫描（真实函数 scan_top_tables）
def c2():
    from agent.insight_scan import scan_top_tables
    print("   sig:", str(inspect.signature(scan_top_tables))[:100])
    r = scan_top_tables(limit=2)
    return r
t("insight_scan.scan_top_tables", c2)

# 3. 归因（detect_and_attribute）
def c3():
    from agent.attribution import detect_and_attribute
    print("   sig:", str(inspect.signature(detect_and_attribute))[:100])
    r = detect_and_attribute("良率", {}, "各工序的良率", limit=2)
    return r
t("attribution.detect_and_attribute", c3)

# 4. 指标矿工
def c4():
    from agent.metric_miner import mine_candidates
    r = mine_candidates(limit=3)
    return r
t("metric_miner.mine_candidates", c4)

# 5. 报表
def c5():
    from agent.html_report import build_html_report
    print("   sig:", str(inspect.signature(build_html_report))[:120])
    r = build_html_report("测试报告", [{"title":"一","content":"内容","type":"text"}], {})
    return r
t("html_report.build_html_report", c5)

# 6. 血缘
def c6():
    from agent.lineage import build_lineage
    r = build_lineage("产量")
    return r
t("lineage.build_lineage", c6)

# 7. 总览图表
def c7():
    from agent.data_charts import generate_overview_charts
    print("   sig:", str(inspect.signature(generate_overview_charts))[:100])
    r = generate_overview_charts()
    return r
t("data_charts.generate_overview_charts", c7)

# 8. 指标记忆（SQL 记忆检索）
def c8():
    from agent.metric_memory import search_sql_memories
    print("   sig:", str(inspect.signature(search_sql_memories))[:100])
    r = search_sql_memories("各工序的良率", limit=3)
    return r
t("metric_memory.search_sql_memories", c8)

# 9. 会话记忆
def c9():
    from agent.conversation_memory import get_conversation_summary
    print("   sig:", str(inspect.signature(get_conversation_summary))[:100])
    r = get_conversation_summary("测试会话", [{"role":"user","content":"各产线产量"}])
    return r
t("conversation_memory.get_conversation_summary", c9)

# 10. 盲点洞察
def c10():
    from agent.blind_spot import scan_blind_spots
    print("   sig:", str(inspect.signature(scan_blind_spots))[:100])
    r = scan_blind_spots(limit=2)
    return r
t("blind_spot.scan_blind_spots", c10)

# 11. 字段语义
def c11():
    from agent.field_semantics import infer_field_semantics
    print("   sig:", str(inspect.signature(infer_field_semantics))[:100])
    r = infer_field_semantics("mes_work_order", "order_status")
    return r
t("field_semantics.infer_field_semantics", c11)

# 12. 临时表管理
def c12():
    from agent.csv_import import list_tmp_tables
    r = list_tmp_tables()
    return r
t("csv_import.list_tmp_tables", c12)

ok = sum(1 for _, o in results if o)
print(f"\n模块冒烟 v2: {ok}/{len(results)}")
