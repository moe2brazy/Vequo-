# -*- coding: utf-8 -*-
"""对「智能问析聊天区轮换引导问题」全量跑一遍，规则化核验对错（只测不写）。

判定原则（不依赖 LLM 自评）：
  1. 命中确定性编译 → 直接执行 SQL，校验 表存在/列存在/行数>0/抽样数值；
  2. 未命中 → 跑真实主链（LLMService.run），看终点：
       data_query       → 校验 表列合法 + 行数 + 口径语义对拍 + analysis 是否为空
       analysis_confirm → 记录（口径未注册 → 弹窗，属红线内预期行为）
       metric_clarify / clarify / no_access / chat → 记录分类
  3. 口径语义对拍：按 docs/postgres库口径定义.md 的通用公式 + yans 实际表字段，
     检查 SQL 引用的聚合列与期望口径列是否一致（如 良率→good/input、停机→downtime_minutes…）。
"""
import sys, os, json, time, re, threading, queue, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.tools import get_real_tables, get_all_tables
from agent.metric_registry import resolve_metric_intent, find_metrics
from agent.metric_compiler import try_compile_metric
from agent.llm_service import LLMService, _extract_sql_tables, _validate_sql_columns
from db.executor import execute_sql

POOL = "tests/suggest_questions_pool.json"

# ---- 真实表/列 ----
def build_schema():
    real = get_real_tables()
    names = {t["table_name"] for t in real}
    cols: dict[str, set[str]] = {}
    for t in get_all_tables():
        n = t["table_name"]
        if n not in names:
            continue
        fs = t.get("fields") or []
        if not fs:
            continue
        cset = set()
        for f in fs:
            if isinstance(f, dict):
                cset.add(str(f.get("name") or f.get("column_name") or ""))
            else:
                cset.add(str(f))
        cols[n] = cset
    return names, cols

SCHEMA = None
def schema():
    global SCHEMA
    if SCHEMA is None:
        SCHEMA = build_schema()
    return SCHEMA

def col_of(sql_lower: str, table: str, want: str) -> bool:
    """检查 sql 中对 table 的某列引用（宽松：含 "want" 标识即认为命中该列族）"""
    return want in sql_lower

def run_llm(q: str, timeout: float = 75.0) -> dict:
    """线程里跑主链，收 done；超时/异常返回记录"""
    res = {"state": "ok"}
    out_q: queue.Queue = queue.Queue()

    def _run():
        try:
            svc = LLMService(q)
            last = None
            for ev in svc.run():
                t = ev.get("type")
                if t in ("done", "error", "sql"):
                    out_q.put(("ev", {k: v for k, v in ev.items()}))
            out_q.put(("done", None))
        except Exception as e:
            out_q.put(("exc", f"{type(e).__name__}: {e}"))

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return {"state": "timeout", "detail": f"> {timeout}s 未结束"}
    events = []
    try:
        while True:
            kind, payload = out_q.get_nowait()
            if kind == "done":
                break
            if kind == "exc":
                res["state"] = "exc"
                res["detail"] = payload
                return res
            events.append(payload)
    except queue.Empty:
        pass
    res["events"] = events
    return res

def verify_query(q: str, qsrc: str) -> dict:
    r = {"question": q, "src": qsrc}
    real_names, real_cols = schema()
    try:
        mr = resolve_metric_intent(q, None)
        r["resolve"] = {"status": mr.get("status"), "hits": [h.get("name") for h in mr.get("hits", [])][:6]}
    except Exception as e:
        r["resolve"] = {"status": "err", "err": str(e)[:120]}
    cpl = None
    try:
        cpl = try_compile_metric(q)
    except Exception as e:
        cpl = {"error": f"{type(e).__name__}: {str(e)[:150]}"}
    if isinstance(cpl, dict) and cpl.get("sql"):
        # 编译命中 → 执行验证
        sql = cpl["sql"]
        r["path"] = "compiled"
        r["sql"] = sql
        tabs = _extract_sql_tables(sql)
        bad_t = sorted(t for t in tabs if t not in real_names)
        notes = []
        if bad_t:
            notes.append(f"引用不存在表: {bad_t}")
        try:
            missing = _validate_sql_columns(sql)
            if missing:
                notes.append(f"引用不存在列: {'; '.join(missing[:6])}")
        except Exception as e:
            notes.append(f"列校验异常: {str(e)[:100]}")
        exec_r = execute_sql(sql)
        if not exec_r.get("success"):
            notes.append(f"执行失败: {(exec_r.get('error') or '')[:160]}")
            r["level"] = "FAIL"
        else:
            rc = exec_r.get("row_count", 0)
            r["row_count"] = rc
            r["sample"] = exec_r.get("rows") or []
            if rc == 0 and not any(k in q for k in ("预警", "低于", "安全")):
                notes.append("0 行：可能是条件过严或 JOIN 失配")
            if not bad_t and not notes:
                r["level"] = "PASS"
            else:
                r["level"] = "WARN" if not notes else "FAIL" if any("不存在" in n or "执行失败" in n for n in notes) else "WARN"
        if notes:
            r["notes"] = notes
        return r

    # 未编译 → 主链真实跑
    rr = run_llm(q)
    r["path"] = "llm"
    if rr["state"] != "ok":
        r["level"] = rr["state"].upper()
        r["notes"] = [rr.get("detail", "")]
        return r
    evs = rr.get("events") or []
    done = next((e for e in evs if e.get("type") == "done"), None)
    if not done:
        r["level"] = "WARN"
        r["notes"] = ["主链无 done 事件（可能中断/error）"]
        err = next((e for e in evs if e.get("type") == "error"), None)
        if err:
            r["notes"].append(f"error: {(err.get('message') or '')[:160]}")
        return r
    resp = done.get("response") or {}
    r["resp_type"] = resp.get("type")
    if resp.get("type") == "analysis_confirm":
        r["level"] = "INFO"
        r["notes"] = ["口径未注册/多指标 → 二次确认弹窗（红线内预期），需用户在弹窗确认方案"]
        r["matched_tables"] = resp.get("matched_tables") or []
        return r
    if resp.get("type") in ("metric_clarify", "clarify"):
        r["level"] = "INFO"
        r["notes"] = [f"触发澄清/歧义确认: {resp.get('answer', '')[:100]}"]
        return r
    if resp.get("type") == "no_access":
        r["level"] = "FAIL"
        r["notes"] = ["权限不足（不应出现于管理全表测试）"]
        return r
    if resp.get("type") not in ("data_query",):
        r["level"] = "INFO"
        r["notes"] = [f"终点类型 {resp.get('type')}（非查询），不适用口径核验"]
        return r
    sql = resp.get("sql") or ""
    tabs = _extract_sql_tables(sql)
    bad_t = sorted(t for t in tabs if t not in real_names)
    notes = []
    if bad_t:
        notes.append(f"引用不存在表: {bad_t}")
    try:
        missing = _validate_sql_columns(sql)
        if missing:
            notes.append(f"引用不存在列: {'; '.join(missing[:6])}")
    except Exception as e:
        notes.append(f"列校验异常: {str(e)[:100]}")
    rc = (resp.get("result") or {}).get("row_count", 0)
    rows = (resp.get("result") or {}).get("rows") or []
    r["row_count"] = rc
    r["sql"] = sql[:500]
    r["sample"] = rows[:3]
    analysis = resp.get("analysis") or ""
    r["has_analysis"] = bool(analysis.strip()) if isinstance(analysis, str) else bool(analysis)
    # —— 口径语义对拍（依据 docs/postgres库口径定义.md + yans 实字段）——
    sql_l = sql.lower()
    sem = []
    if any(k in q for k in ("良率", "合格")):
        if "mes_process_output" not in tabs:
            sem.append("良率类问法未使用 mes_process_output")
        elif not (("good_qty" in sql_l) and ("input_qty" in sql_l)):
            sem.append("良率 SQL 缺 good_qty/input_qty 分子分母（口径=good/input）")
    if "不良" in q and "排行" in q:
        # 不良类型排行应落在 qms_defect_detail 或 mes_process_output
        if not any(t in tabs for t in ("qms_defect_detail", "mes_process_output")):
            sem.append("不良排行未用 qms_defect_detail/mes_process_output")
    if ("停机" in q) or ("设备" in q and "停机" in q):
        if "eqp_downtime_record" not in tabs:
            sem.append("停机类问法未使用 eqp_downtime_record")
    if ("产量" in q or "投入" in q) and ("mes_process_output" not in tabs) and ("mes_work_order" not in tabs):
        sem.append("产量/投入问法未用 mes_process_output/mes_work_order")
    if "安全库存" in q or ("低于" in q and "库存" in q):
        if "inv_inventory_snapshot" not in tabs:
            sem.append("库存预警未用 inv_inventory_snapshot")
    if "工单完成" in q or ("工单" in q and "完成" in q):
        if "mes_work_order" not in tabs:
            sem.append("工单完成未用 mes_work_order")
    if ("检验" in q or "检验结果" in q) and "qms_inspection" not in tabs:
        sem.append("检验问法未用 qms_inspection")
    if "工单状态" in q or ("工单" in q and "状态" in q):
        if "mes_work_order" not in tabs:
            sem.append("工单状态未用 mes_work_order")
    if sem:
        notes.append("口径对拍: " + "; ".join(sem))
    # —— 结论分级 ——
    if bad_t:
        r["level"] = "FAIL"
    elif any("不存在列" in n for n in notes):
        r["level"] = "FAIL"
    elif sem and rc:
        r["level"] = "WARN"
    elif not rows:
        r["level"] = "WARN"
        notes.append("结果 0 行（可能空窗/条件过严）")
    else:
        r["level"] = "PASS"
    if notes:
        r["notes"] = notes
    return r

def main():
    with open(POOL, encoding="utf-8") as f:
        pool = json.load(f)
    first = int(os.environ.get("FIRST", "0") or "0")
    if first > 0:
        pool = pool[:first]
    out = []
    for i, item in enumerate(pool, 1):
        q = item["question"]
        print(f"[{i}/{len(pool)}] {q}", flush=True)
        t0 = time.time()
        try:
            res = verify_query(q, item.get("src", ""))
        except Exception as e:
            res = {"question": q, "src": item.get("src", ""), "level": "EXC",
                   "notes": [f"{type(e).__name__}: {str(e)[:200]}"], "tb": traceback.format_exc()[-500:]}
        res["elapsed_s"] = round(time.time() - t0, 1)
        out.append(res)
        print("   =>", res.get("level"), res.get("notes", ""), f"({res['elapsed_s']}s)", flush=True)
    with open("tests/verify_suggest_report.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    # 汇总
    from collections import Counter
    c = Counter(x.get("level", "?") for x in out)
    print("\n=== 汇总 ===", dict(c), flush=True)

if __name__ == "__main__":
    main()
