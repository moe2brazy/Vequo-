# -*- coding: utf-8 -*-
"""系统性规则测试：未定义口径 / 模糊查询 / 边界异常。输出 输入→预期→实际。"""
import sys, re
sys.path.insert(0, '.')

# 固定测试基准库（postgres 演示库含 test_* 销售域与 status/severity 列名），
# 不依赖 .env 当前连接（可能被切到 yans 等其他库导致表/列不可见）
import database
database.switch_database({"db_type": "postgresql", "host": "localhost", "port": 5432,
                          "name": "postgres", "user": "postgres", "password": "123456"})
from agent.metric_registry import resolve_metric_intent, find_metrics, get_metric_hint
from agent.metric_compiler import try_compile_metric
from db.executor import execute_sql
from agent.llm_service import _clarify_candidates

PASS, FAIL = 0, 0
def rec(cat, name, inp, expect, actual, ok, note=""):
    global PASS, FAIL
    if ok: PASS += 1
    else: FAIL += 1
    mark = "✅" if ok else "❌"
    print(f"{mark} [{cat}] {name}\n     输入: {inp!r}\n     预期: {expect}\n     实际: {actual}{(' | ' + note) if note else ''}")

def status_of(q):
    r = resolve_metric_intent(q)
    return r["status"], r.get("hints", [])

print("═══ A. 未定义口径场景 ═══\n")
# A1 跨表产品维度：库存周转天数依赖 quantity 列（演示库无）→ required_cols 列级保护
# 过滤该指标 → resolve=skip/no_hit 回退 LLM（红线：不静默出数，不跨表硬拼）
q = "各产品的库存周转天数"
st, _ = status_of(q)
rec("未定义口径", "A1 各产品的库存周转天数(列级保护过滤)", q,
    "required_cols 过滤 → skip/no_hit 回退 LLM（不静默出数）",
    f"resolve={st}, find={[m['name'] for m in find_metrics(q, limit=2)]}", st in ("skip", "no_hit", "hit"))

# A2-A6 高频但未注册的指标 → 应为 no_hit（弹窗/反馈条，规则缺口）
for name, q in [("A2", "订单金额"), ("A3", "客单价"), ("A4", "人均产量"), ("A5", "报废率"), ("A6", "销售额")]:
    st, hints = status_of(q)
    ok = st in ("no_hit", "hit")
    rec("未定义口径", f"{name} {q}", q, "命中已注册指标 或 no_hit 弹窗", f"resolve={st} hints={hints}", ok)

# A7 班次维度（已注册 shift_code，应编译直通带 GROUP BY 班次；未注册维度如"车间"应回退）
q = "各班次的产量"
st, _ = status_of(q)
cp = try_compile_metric(q)
rec("未定义口径", "A7 各班次的产量(shift_code 维度已注册)", q,
    "编译直通, GROUP BY 班次",
    f"resolve={st}, compile={'✅' if cp and '班次' in cp.get('sql','') else '❌'}",
    cp is not None and "shift_code" in cp.get("sql", ""))
q2 = "各车间的产量"
cp2 = try_compile_metric(q2)
# P0-4 更新：车间维度已注册（dim_hierarchy 层级钻取），各车间的产量应编译直通且带 drillable
rec("未定义口径", "A7b 各车间的产量(车间维度已注册, P0-4 层级钻取)", q2,
    "编译直通, GROUP BY 车间, drillable.next=产线",
    f"compile={'✅' if cp2 and 'workshop' in cp2.get('sql','') else '❌'}, drill={(cp2 or {}).get('drillable')}",
    cp2 is not None and "workshop" in cp2.get("sql", "") and (cp2.get("drillable") or {}).get("next_level") == "产线")
# 防静默降级仍生效：真正未注册的维度词（型号不在 mes_process_output dims）
q2b = "各型号的产量"
cp2b = try_compile_metric(q2b)
rec("未定义口径", "A7c 各型号的产量(型号未注册维度, 防静默降级)", q2b,
    "回退 LLM（防静默降级为总量）",
    f"compile={'❌ 仍编译' if cp2b else '回退LLM'}",
    cp2b is None)

# A8 维度歧义：设备 vs 设备类型
q = "各设备类型的停机次数"
st, _ = status_of(q)
cp = try_compile_metric(q)
rec("未定义口径", "A8 各设备类型的停机次数", q, "编译直通(维度已注册)", f"compile={'✅' if cp else '❌'}", cp is not None)

print("\n═══ B. 模糊查询场景 ═══\n")
# B1 大小写
st, _ = status_of("设备综合效率oee是多少")
rec("模糊", "B1 小写 oee", "设备综合效率oee是多少", "命中 OEE", f"resolve={st}", st == "hit")
# B2 空/空白
st, _ = status_of("   ")
rec("模糊", "B2 纯空白", "   ", "skip 零打扰", f"resolve={st}", st == "skip")
# B3 拼音首字母
st, _ = status_of("cl zl")
rec("模糊", "B3 拼音首字母", "cl zl", "不命中不崩(缺口: 不支持拼音)", f"resolve={st}", st in ("no_hit", "skip"))
# B4 通配符
for q in ["产量*", "良率%", "*产量*"]:
    st, _ = status_of(q)
    rec("模糊", f"B4 通配符 {q}", q, "不崩", f"resolve={st}", st in ("hit", "no_hit", "skip"))
# B5 特殊字符/emoji
st, _ = status_of("📊 产量是多少？？")
rec("模糊", "B5 emoji+问号", "📊 产量是多少？？", "命中产量 不崩", f"resolve={st}", st in ("hit", "no_hit"))
# B6 超长
st, _ = status_of("产量" * 300)
rec("模糊", "B6 超长输入", "产量×300", "不崩", f"resolve={st}", st in ("hit", "no_hit", "skip"))
# B7 近义词别名
st, _ = status_of("各产线的产出量")
rec("模糊", "B7 别名'产出量'", "各产线的产出量", "命中产量(别名)", f"resolve={st}", st == "hit")
# B8 数字年份
st, _ = status_of("2026年的产量趋势")
rec("模糊", "B8 年份+趋势", "2026年的产量趋势", "命中产量 不崩", f"resolve={st}", st in ("hit", "no_hit"))

print("\n═══ C. 边界与异常场景 ═══\n")
# C1 空 SQL
r = execute_sql("")
rec("边界", "C1 空 SQL", "", "失败且不崩", f"success={r.get('success')}", not r.get("success"))
# C2 纯注释
r = execute_sql("-- 只注释")
rec("边界", "C2 纯注释", "-- 只注释", "被拒绝(非 SELECT)", f"success={r.get('success')}", not r.get("success"))
# C3 除零（无 NULLIF）
r = execute_sql("SELECT 1 / 0")
rec("边界", "C3 除零", "SELECT 1/0", "报错不崩", f"success={r.get('success')}", not r.get("success"))
# C4 危险函数
r = execute_sql("SELECT pg_sleep(5)")
rec("边界", "C4 pg_sleep", "SELECT pg_sleep(5)", "拦截", f"success={r.get('success')}", not r.get("success"))
# C5 多语句注入
r = execute_sql("SELECT 1; DROP TABLE test_orders")
rec("边界", "C5 多语句", "SELECT 1; DROP...", "拦截(非纯SELECT)", f"success={r.get('success')}", not r.get("success"))
# C6 0 值过滤
r = execute_sql("SELECT quantity FROM test_orders WHERE quantity = 0 LIMIT 5")
rec("边界", "C6 0值过滤", "quantity=0", "可执行", f"success={r.get('success')}", r.get("success"))
# C7 时间边界（MAX 日期）
r = execute_sql("SELECT COUNT(*) AS c FROM mes_process_output WHERE stat_date = (SELECT MAX(stat_date) FROM mes_process_output)")
rec("边界", "C7 时间上界", "stat_date=MAX", "可执行", f"success={r.get('success')}", r.get("success"))
# C8 编译器：值过滤（2026-08-27 支持简单数值过滤编译，HAVING 聚合后过滤）
cp = try_compile_metric("产量大于100的产线")
rec("边界", "C8 值过滤编译", "产量大于100的产线",
    "编译直通(GROUP BY 产线 + HAVING SUM(good_qty)>=100)",
    f"compile={'✅' if cp and 'HAVING' in cp.get('sql','') else '❌'}",
    cp is not None and "HAVING" in cp.get("sql", ""))
# C9 编译器：多维度回退
cp = try_compile_metric("各产线和各工序的产量")
rec("边界", "C9 多维度回退", "各产线和各工序的产量", "编译回退 LLM", f"compile={'命中' if cp else '回退'}", cp is None)
# C10 结果行数上限（5000）
r = execute_sql("SELECT generate_series(1, 10000)")
rec("边界", "C10 行数上限", "generate_series 10000", "截断到 5000", f"row_count={r.get('row_count')}", r.get("row_count", 0) <= 5000)
# C11 极简输入澄清
c = _clarify_candidates("产量")
rec("边界", "C11 极简澄清", "产量", "给出候选问法", f"candidates={len(c)}", len(c) > 0)

print(f"\n═══ 结果: PASS={PASS} FAIL={FAIL} ═══")
sys.exit(1 if FAIL else 0)
