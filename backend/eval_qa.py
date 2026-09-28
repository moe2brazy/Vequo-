# -*- coding: utf-8 -*-
"""复杂题批量评测：直连主链跑 NL2SQL，收集每题的链路结果便于找 bug。"""
import sys, time, traceback, json
sys.path.insert(0, r"D:\vue_first2 (2)\vue_first2\vue_first(2)\vue_first\backend")
from agent.llm_service import LLMService

QUESTIONS = [
    # (题号, 问题, 期望要点)
    (1,  "统计各工序名称的良率并按良率从高到低排序，展示前5名", "join dim_process+聚合+排序"),
    (2,  "产出不良数最高的5条产线是哪些，按不良数降序", "topN by line 跨表"),
    (3,  "本月与上月的总产量对比，相差多少", "时间对比两组"),
    (4,  "最近7天非计划停机的停机时长按原因汇总", "时间+多条件+分组"),
    (5,  "把库存按可用量与安全库存的差距分档：缺货/正常/过剩，各档产品数", "case 分档 + 计数"),
    (6,  "列出完成率低于80%的工单（完成数量/计划数量）", "跨表比率明细"),
    (7,  "按工单汇总实际产出数量并与计划数量对比，产出缺口最大的前10个工单", "join + 差值 topN"),
    (8,  "最近一个月各产品的产量变化，哪些产品产量明显下降", "趋势+洞察"),
    (9,  "质量怎么样", "模糊问法"),
    (10, "各产品在2026年周末的产量趋势", "weekend 复杂过滤"),
    (11, "状态为已完成且计划数量大于1000的工单有哪些，按计划数量降序", "枚举+数值双条件"),
    (12, "生产过产品编号以 P 开头的产品的产线，它们的停机总时长是多少", "IN/子查询+like"),
    (13, "当前数据库共有多少种产品品类", "distinct count"),
    (14, "按车间统计最近30天各工序的一次合格情况（投入与良品）", "车间+多表+近30天"),
    (15, "最近10条检验记录，按检验日期倒序，展示检验单号与结论", "明细时间排序"),
    (16, "既生产了A产品又生产了B产品的车间有哪些", "集合语义 交叉"),
    (17, "停机原因排行前3分别占总停机时长的比例", "top 聚合+占比"),
    (18, "名称含“清洗”的设备发生过多少次停机，总时长多少", "like+关联主数据"),
    (19, "本月停机时长比上月是增是减，变化多少分钟", "同比双时间"),
    (20, "按班次统计各班的良品率并计算与整体良品率的差距", "多比率"),
    (21, "工单表总共有多少条记录", "count 单表"),
    (22, "查询不存在表 nope_missing 的数据", "防御: 不应 500"),
    (23, "帮我删除 mes_process_output 表的所有数据", "只读防御"),
    (24, "工序产出表中完成状态的产量是多少", "字段语义错配观察"),
    (25, "工序产量表的良率是多少", "中文别名+注册指标"),
]

def run_one(q: str):
    evs = {"intent": "", "sql": "", "error": "", "done_type": "",
           "rows": 0, "ok": None, "res_err": "", "duration": 0.0}
    t0 = time.time()
    try:
        svc = LLMService(q, fast=True)
        for ev in svc.run():
            ty = ev.get("type")
            if ty == "sql":
                evs["sql"] = str(ev.get("sql") or "")[:400]
            elif ty == "sql_result":
                evs["rows"] = int(ev.get("row_count") or 0)
                evs["ok"] = True
            elif ty == "error":
                evs["error"] = str(ev.get("message") or "")[:220]
            elif ty == "done":
                r = ev.get("response") or {}
                evs["done_type"] = r.get("type") or ""
                evs["intent"] = str(r.get("intent") or svc.intent or "")
                res = r.get("result") or {}
                if isinstance(res, dict):
                    if res.get("success") is False:
                        evs["ok"] = False
                        evs["res_err"] = str(res.get("error") or "")[:160]
                    evs["ok"] = True if res.get("success") is not False else evs["ok"]
                    evs["rows"] = int(res.get("row_count") or len(res.get("rows") or []) or evs["rows"] or 0)
    except Exception:
        evs["error"] = "EXC " + traceback.format_exc(limit=2)[-300:]
    evs["duration"] = round(time.time() - t0, 1)
    return evs


def verdict(r):
    if r["error"] and any(k in r["error"] for k in ("只读", "删除", "修改类", "没找到", "没有找到", "不存在")):
        return "OK(拦截)"
    if r["error"]:
        return "FAIL"
    if r["done_type"] in ("metric_clarify", "clarify"):
        return "OK(澄清)"
    if r["done_type"] in ("metric_clarify", "clarify"):
        return "OK(澄清)"
    if r["done_type"] not in ("data_query", "lookup", "analyze_db", "insight", "attribution"):
        return "FAIL"
    if r["ok"] is False:
        return "FAIL|" + r["res_err"][:60]
    if r["ok"] is None:
        return "NO-RESULT"
    if r["rows"] == 0:
        return "EMPTY"
    return "RUN-OK"


if __name__ == "__main__":
    out = []
    for no, q, note in QUESTIONS:
        print(f"\n=== [{no}] {q} ({note}) ===", flush=True)
        r = run_one(q)
        flag = verdict(r)
        r.update({"no": no, "question": q, "note": note, "flag": flag})
        out.append(r)
        print(f"  flag={flag} intent={r['intent']} done={r['done_type']} ok={r['ok']} rows={r['rows']} t={r['duration']}s", flush=True)
        if r["error"]:
            print("  ERROR:", r["error"], flush=True)
        elif r["sql"] and flag != "OK":
            print("  SQL:", r["sql"], flush=True)
    # 汇总
    print("\n\n===== 汇总 =====")
    for r in out:
        print(f"[{r['flag']}] #{r['no']} {r['question'][:44]}")
    fails = [r for r in out if r["flag"] == "FAIL"]
    print(f"\n共 {len(out)} 题，FAIL {len(fails)}")
    json.dump(out, open("/tmp/eval_out.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
