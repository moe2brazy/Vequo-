# -*- coding: utf-8 -*-
"""agent 各子模块核心功能冒烟：真实调用产出，验证不崩溃 + 结果非空"""
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
        results.append((name, ok, str(v)[:60] if not ok else ""))
        print(f"  {'✅' if ok else '❌'} {name}")
        if not ok: print(f"      返回: {str(v)[:80]}")
    except Exception as e:
        results.append((name, False, f"{type(e).__name__}: {str(e)[:80]}"))
        print(f"  ❌ {name}: {type(e).__name__}: {str(e)[:80]}")

# 1. 图表生成
def c1():
    from agent.chart_agent import generate_chart
    return generate_chart(["工序", "良率"], [["A", 98.5], ["B", 99.1], ["C", 97.2]], "各工序良率")
t("chart_agent.generate_chart", c1)

# 2. 洞察扫描
def c2():
    from agent.insight_scan import run_insight_scan
    r = run_insight_scan(limit=3)
    return r
t("insight_scan.run_insight_scan", c2)

# 3. 关注点
def c3():
    from routers.tables import get_attention_points
    r = get_attention_points()
    return r
t("tables.get_attention_points", c3)

# 4. 归因
def c4():
    from agent.attribution import analyze_attribution
    r = analyze_attribution("良率", "各工序的良率", {"query":"各工序的良率"})
    return r
t("attribution.analyze_attribution", c4)

# 5. 指标矿工（schema 扫描）
def c5():
    from agent.metric_miner import mine_candidates
    r = mine_candidates(limit=3)
    return r
t("metric_miner.mine_candidates", c5)

# 6. 报表生成
def c6():
    from agent.html_report import build_html_report
    r = build_html_report("测试报告", [{"title":"章节", "content":"内容", "type":"text"}], {})
    return r
t("html_report.build_html_report", c6)

# 7. 关系图谱
def c7():
    from agent.lineage import build_lineage
    r = build_lineage("产量")
    return r
t("lineage.build_lineage", c7)

# 8. CSV 导入（最小）
def c8():
    from agent.csv_import import parse_csv_preview
    r = parse_csv_preview("a,b\n1,2\n3,4")
    return r
t("csv_import.parse_csv_preview", c8)

# 9. 数据图表（dashboard）
def c9():
    from agent.data_charts import build_dashboard_charts
    r = build_dashboard_charts(limit=3)
    return r
t("data_charts.build_dashboard_charts", c9)

# 10. Python 沙箱
def c10():
    from agent._sandbox_runner import run_python_sandbox
    r = run_python_sandbox("result = 1 + 1", {"timeout": 5})
    return r.get("result") if isinstance(r, dict) else r
t("sandbox.run_python_sandbox", c10)

ok = sum(1 for _, o, _ in results if o)
print(f"\n模块冒烟: {ok}/{len(results)}")
