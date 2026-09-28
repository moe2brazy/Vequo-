"""第三轮复杂题批量评测（yans 库，fast=True 直连主链，不写记忆）

重点：结构性复杂 SQL（集合/反连接/CASE/跨表日期/窗口/多表双指标/复合过滤），
混入少量注册口径题作对照。判定基于真实执行结果（sql_result 事件 + result.success）。
"""
import sys, time, traceback
sys.path.insert(0, ".")
import config
from agent.llm_service import LLMService

QS = [
    # ===== A 组：注册口径对照（应 compiled 秒回）=====
    ("A1", "各产线的产量排行"),
    ("A2", "各工序的良率排行"),
    ("A3", "已完成工单有多少"),
    ("A4", "各设备类型的运行设备数"),
    ("A5", "按停机原因汇总非计划停机时长"),
    ("A6", "产出缺口最大的前5个工单"),
    ("A7", "库存分档统计：缺货/正常/过剩各档产品数"),
    # ===== B 组：多表 JOIN + 排行 / 对比 =====
    ("B1", "按车间统计停机次数并排序"),
    ("B2", "各产品品类的平均良率"),
    ("B3", "检验不合格率最高的前5个产品"),
    ("B4", "各产线从下单到完工的平均耗时天数"),
    ("B5", "各工序名称对应的缺陷件数排行"),
    ("B6", "关键工序与普通工序的平均良率对比"),
    # ===== C 组：时间 + 排行 / 窗口 =====
    ("C1", "2026年6月每天的产量趋势"),
    ("C2", "最近30天各产线产量对比"),
    ("C3", "各月份停机时长的环比变化"),
    ("C4", "按周统计各产品的产出良品总数"),
    ("C5", "各产线最近一次停机是什么时候、停了多久"),
    # ===== D 组：集合 / 反连接 / 跨事实表 =====
    ("D1", "既生产控制器又生产传感器的产线有哪些"),
    ("D2", "有工单但从未有任何产出记录的工单有哪些"),
    ("D3", "生产过产品但从未被检验过的产品有哪些"),
    ("D4", "停机次数超过5次但良品产出为0的设备有哪些"),
    ("D5", "同一天里既有非计划停机又有不合格检验的车间有哪些"),
    # ===== E 组：CASE / 分档 / 计算列 =====
    ("E1", "各产线投入量与良品数的差额最大的前5条"),
    ("E2", "按班次对比白班和夜班的良率差异"),
    ("E3", "计划量超过1000且完工不足800的工单有哪些"),
    ("E4", "单次停机超过90分钟的设备名称与次数"),
    ("E5", "各工单涉及工序数量最多的前10个工单"),
    # ===== F 组：多指标 / 复合 =====
    ("F1", "各仓库的库存预警产品数与该仓产品总数"),
    ("F2", "各车间当月产量与该车间停机时长的对比"),
    ("F3", "每个车间生产了多少个产品品类"),
    ("F4", "按产品统计检验次数和缺陷数并算缺陷率"),
]

def run_one(q):
    evs = {"sql": "", "error": "", "done_type": "", "rows": 0, "ok": None,
           "res_err": "", "duration": 0.0, "compiled": None, "hits": []}
    t0 = time.time()
    try:
        svc = LLMService(q, fast=True)
        for ev in svc.run():
            ty = ev.get("type")
            if ty == "sql":
                evs["sql"] = str(ev.get("sql") or "")[:600]
            elif ty == "sql_result":
                evs["rows"] = int(ev.get("row_count") or 0)
                evs["ok"] = True
            elif ty == "error":
                evs["error"] = str(ev.get("message") or "")[:200]
            elif ty == "done":
                r = ev.get("response") or {}
                evs["done_type"] = r.get("type") or ""
                res = r.get("result") or {}
                if isinstance(res, dict):
                    if res.get("success") is False:
                        evs["ok"] = False
                        evs["res_err"] = str(res.get("error") or "")[:140]
                    else:
                        # result 携带结果（编译/exec 路径可能无独立 sql_result 事件）→ 视为执行完成
                        evs["ok"] = True
                    evs["rows"] = int(res.get("row_count") or len(res.get("rows") or []) or evs["rows"] or 0)
                evs["compiled"] = r.get("compiled")
    except Exception:
        evs["error"] = "EXC " + traceback.format_exc(limit=2)[-260:]
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
        return "FAIL|" + r["res_err"][:50]
    if r["ok"] is None:
        return "NO-RESULT"
    if r["rows"] == 0:
        return "EMPTY"
    return "RUN-OK"


if __name__ == "__main__":
    n_pass = n_fail = n_empty = n_nores = n_okblock = 0
    print(f"共 {len(QS)} 题 | 主模型 {config.LLM_CONFIG.get('model')} | 开始时间 {time.strftime('%H:%M:%S')}\n")
    for no, q in QS:
        r = run_one(q)
        flag = verdict(r)
        if flag == "RUN-OK": n_pass += 1
        elif flag.startswith("FAIL"): n_fail += 1
        elif flag == "EMPTY": n_empty += 1
        elif flag == "NO-RESULT": n_nores += 1
        else: n_okblock += 1
        comp = "compiled" if r["compiled"] else "LLM     "
        print(f"[{flag:<10}] {no} | {r['duration']:5.1f}s | {comp} | {q}")
        if r["error"] and flag != "OK(拦截)":
            print(f"          ERR: {r['error'][:130]}")
        if r["sql"] and (flag not in ("RUN-OK", "OK(拦截)")):
            print(f"          SQL: {r['sql'][:170].replace(chr(10),' ')}")
        if flag == "RUN-OK" and r["rows"] < 3:
            print(f"          SQL: {r['sql'][:170].replace(chr(10),' ')}")
        print(flush=True)
    print(f"\n汇总: RUN-OK={n_pass} FAIL={n_fail} EMPTY={n_empty} NO-RESULT={n_nores} OK(拦截)={n_okblock} | {time.strftime('%H:%M:%S')}")
