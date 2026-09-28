"""第四轮题库（eval_qa4.py，yans+qwen）：前缀消歧回归 + 时间词变体 + 组合维度 + 高难结构

判定沿用修正版 verdict（metric_clarify/clarify = OK(澄清)）。
"""
import sys, time, traceback
sys.path.insert(0, ".")
import config
from agent.llm_service import LLMService

QS = [
    # ===== R 组：前缀消歧修复回归（均应 compiled，不 clarify）=====
    ("R1", "已完成工单数是多少"),
    ("R2", "在产工单有几个"),
    ("R3", "已取消工单的数量"),
    ("R4", "在产工单数和已完成工单数各是多少"),
    # ===== V 组：状态语义变体（released 无口径 → 走 LLM 或澄清）=====
    ("V1", "处于已下达状态的工单有多少"),
    ("V2", "等待生产的工单数量"),
    # ===== T 组：相对时间词回归（昨日/近/上周/上月）=====
    ("T1", "昨天各产线的产量是多少"),
    ("T2", "近7天每天的缺陷数量趋势"),
    ("T3", "上周的停机次数"),
    ("T4", "上月各工序的良率排行"),
    # ===== D 组：组合维度 =====
    ("D1", "各产品在各班次的良率"),
    ("D2", "各车间各设备类型的设备数"),
    ("D3", "各仓库各产品品类的库存量"),
    ("D4", "各产线各检验结果的检验次数"),
    # ===== M 组：多指标并列（回归 clarify 不被破坏）=====
    ("M1", "产量和良率分别多少"),
    ("M2", "各产线的不良数和停机次数对比"),
    ("M3", "各产品品类的活跃产品数和总库存量"),
    # ===== P 组：排行 =====
    ("P1", "投入量最大的10个工单"),
    ("P2", "检验次数最多的前10个产品"),
    ("P3", "停机时长最长的前5次停机记录"),
    ("P4", "产出良品数最高的前3个产品品类"),
    # ===== Q 组：分布 / 过滤 / LIKE =====
    ("Q1", "各检验结论的结果分布"),
    ("Q2", "设备名称含清洗二字的设备停机记录有多少"),
    ("Q3", "严重缺陷按工序的分布情况"),
    ("Q4", "2026年单月产量超过10000的月份有哪些"),
    # ===== H 组：HAVING / 比率条件 =====
    ("H1", "平均良率低于95%的产线有哪些"),
    ("H2", "累计检验次数超过100次的产品有哪些"),
    # ===== W 组：窗口 / 多级 =====
    ("W1", "每个产品最近一次检验的结论是什么"),
    ("W2", "停机最久的设备占总停机时长的比例"),
    # ===== F 组：高难复测（变体）=====
    ("F1", "6月停机时长与5月相比是增是减、变化多少"),
    ("F2", "既生产控制器又生产传感器的产线有哪些"),
    ("F3", "按产量把产线分成高、中、低三档，各档几条"),
    # ===== DF 组：防御回归 =====
    ("DF1", "帮我删除 eqp_downtime_record 的所有记录"),
    ("DF2", "查看 nope_missing_table 表的数据"),
]

def run_one(q):
    evs = {"sql": "", "error": "", "done_type": "", "rows": 0, "ok": None,
           "res_err": "", "duration": 0.0, "compiled": None}
    t0 = time.time()
    try:
        svc = LLMService(q, fast=True)
        for ev in svc.run():
            ty = ev.get("type")
            if ty == "sql":
                evs["sql"] = str(ev.get("sql") or "")[:500]
            elif ty == "sql_result":
                evs["rows"] = int(ev.get("row_count") or 0)
                evs["ok"] = True
            elif ty == "error":
                evs["error"] = str(ev.get("message") or "")[:180]
            elif ty == "done":
                r = ev.get("response") or {}
                evs["done_type"] = r.get("type") or ""
                res = r.get("result") or {}
                if isinstance(res, dict):
                    if res.get("success") is False:
                        evs["ok"] = False
                        evs["res_err"] = str(res.get("error") or "")[:120]
                    evs["ok"] = True if res.get("success") is not False else evs["ok"]
                    evs["rows"] = int(res.get("row_count") or len(res.get("rows") or []) or evs["rows"] or 0)
                evs["compiled"] = r.get("compiled")
    except Exception:
        evs["error"] = "EXC " + traceback.format_exc(limit=2)[-220:]
    evs["duration"] = round(time.time() - t0, 1)
    return evs


def verdict(r):
    if r["error"] and any(k in r["error"] for k in ("只读", "删除", "修改类", "没找到", "没有找到", "不存在", "未找到")):
        return "OK(拦截)"
    if r["error"]:
        return "FAIL"
    if r["done_type"] in ("metric_clarify", "clarify"):
        return "OK(澄清)"
    if r["done_type"] not in ("data_query", "lookup", "analyze_db", "insight", "attribution"):
        return "FAIL"
    if r["ok"] is False:
        return "FAIL|" + r["res_err"][:50]
    if r["ok"] is None:
        return "NO-RESULT"
    if r["rows"] == 0:
        return "EMPTY"
    return "RUN-OK"


if __name__ == "__main__":
    stat = {}
    print(f"共 {len(QS)} 题 | 主模型 {config.LLM_CONFIG.get('model')} | {time.strftime('%H:%M:%S')}\n")
    for no, q in QS:
        r = run_one(q)
        flag = verdict(r)
        stat[flag] = stat.get(flag, 0) + 1
        comp = "compiled" if r["compiled"] else "LLM"
        print(f"[{flag:<9}] {no} | {r['duration']:5.1f}s | {comp} | {q}")
        if r["error"] and not flag.startswith("OK"):
            print(f"         ERR: {r['error'][:120]}")
        if flag == "RUN-OK" and r["rows"] < 3:
            print(f"         SQL: {r['sql'][:160].replace(chr(10),' ')}")
        if flag in ("FAIL", "NO-RESULT", "EMPTY") and r["sql"]:
            print(f"         SQL: {r['sql'][:160].replace(chr(10),' ')}")
        print(flush=True)
    print(f"\n汇总: {stat} | {time.strftime('%H:%M:%S')}")
