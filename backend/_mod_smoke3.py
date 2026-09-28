# -*- coding: utf-8 -*-
"""agent 子模块功能冒烟 v3：真实签名"""
import sys, io, contextlib
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

def c1():
    from agent.chart_agent import generate_chart
    r = generate_chart(["工序","良率"], [{"工序":"A","良率":98.5},{"工序":"B","良率":99.1},{"工序":"C","良率":97.2}], "各工序良率")
    return r.get("type") or r.get("svg")
t("chart_agent.generate_chart(dict行)", c1)

def c2():
    from agent.insight_scan import scan_top_tables
    r = scan_top_tables(limit=2)
    return r
t("insight_scan.scan_top_tables", c2)

def c3():
    from agent.attribution import detect_and_attribute
    r = detect_and_attribute("各工序的良率", {"success": True, "rows": [{"工序":"A","良率":98.5}]}, None)
    return r
t("attribution.detect_and_attribute", c3)

def c4():
    from agent.metric_miner import mine_candidates
    r = mine_candidates(limit=3)
    return r
t("metric_miner.mine_candidates", c4)

def c5():
    from agent.html_report import build_html_report
    from database import SessionLocal
    db = SessionLocal()
    try:
        r = build_html_report(db, allowed_tables=None, row_filters=None)
        return r
    finally:
        db.close()
t("html_report.build_html_report(db)", c5)

def c6():
    from agent.lineage import build_lineage
    r = build_lineage("产量")
    return r
t("lineage.build_lineage", c6)

def c7():
    from agent.data_charts import generate_overview_charts
    r = generate_overview_charts()
    return r
t("data_charts.generate_overview_charts", c7)

def c8():
    from agent.metric_memory import get_metric_memory
    r = get_metric_memory("各工序的良率")
    return r
t("metric_memory.get_metric_memory", c8)

def c9():
    from agent.conversation_memory import build_session_context
    r = build_session_context([{"role":"user","content":"各产线产量"},{"role":"assistant","content":"已查询"}])
    return r
t("conversation_memory.build_session_context", c9)

def c10():
    from agent.blind_spot import find_blind_spots
    r = find_blind_spots(limit=2)
    return r
t("blind_spot.find_blind_spots", c10)

def c11():
    from agent.field_semantics import explain_field
    r = explain_field("mes_work_order", "order_status")
    return r
t("field_semantics.explain_field", c11)

def c12():
    from agent.csv_import import list_tmp_tables
    r = list_tmp_tables()
    return r if r is not None else "[]"
t("csv_import.list_tmp_tables", c12)

def c13():
    from agent.investigator import Investigator
    inv = Investigator()
    r = inv.plan("停机原因分析", ["eqp_downtime_record"])
    return r
t("investigator.plan", c13)

def c14():
    from agent.dashboard_agent import build_dashboard
    r = build_dashboard()
    return r
t("dashboard_agent.build_dashboard", c14)

ok = sum(1 for _, o in results if o)
print(f"\n模块冒烟 v3: {ok}/{len(results)}")
