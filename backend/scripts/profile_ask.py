# -*- coding: utf-8 -*-
"""问答链路性能剖析脚本（只读测量，不改业务逻辑）。

用法：
    .venv/Scripts/python.exe scripts/profile_ask.py

输出每次问答的：总耗时 / LLM 调用次数与总耗时 / 各阶段耗时。
用于定位"生成结果慢"到底慢在哪一步，避免凭感觉优化。
"""
import sys
import time

sys.path.insert(0, '.')

import agent.llm_service as L


# ── LLM 调用计时：所有 LLM 开销都过 _make_llm，从这里量最准 ──
_stats = {"llm_calls": 0, "llm_ms": 0.0, "llm_breakdown": {}}
_orig_make_llm = L._make_llm


def _timed_make_llm(*a, **kw):
    llm = _orig_make_llm(*a, **kw)
    inner_invoke = getattr(llm, "invoke", None)
    if inner_invoke is None:
        return llm

    def _invoke(*ia, **ik):
        t0 = time.time()
        try:
            return inner_invoke(*ia, **ik)
        finally:
            ms = (time.time() - t0) * 1000
            _stats["llm_calls"] += 1
            _stats["llm_ms"] += ms
            # 用 prompt 前 40 字归类，看是哪一类调用最贵
            try:
                key = str(ia[0][0].content)[:40].replace("\n", " ")
            except Exception:
                key = "?"
            slot = _stats["llm_breakdown"].setdefault(key, {"n": 0, "ms": 0.0})
            slot["n"] += 1
            slot["ms"] += ms
    try:
        llm.invoke = _invoke
    except Exception:
        pass
    return llm


L._make_llm = _timed_make_llm


# ── 阶段计时 ──
PHASES = ["_generate_sql_stream", "_exec_sql", "_validate_result", "_llm_result_check",
          "_llm_result_evaluate", "_cross_validate", "_llm_analysis",
          "_quick_recommended", "_build_lineage", "_build_response"]
_phase_stats = {}


def _wrap_phase(name):
    orig = getattr(L.LLMService, name, None)
    if orig is None:
        return

    def _inner(self, *a, **kw):
        t0 = time.time()
        try:
            return orig(self, *a, **kw)
        finally:
            ms = (time.time() - t0) * 1000
            s = _phase_stats.setdefault(name, {"n": 0, "ms": 0.0})
            s["n"] += 1
            s["ms"] += ms
    setattr(L.LLMService, name, _inner)


for _p in PHASES:
    _wrap_phase(_p)


def run_one(query: str):
    _stats["llm_calls"] = 0
    _stats["llm_ms"] = 0.0
    _stats["llm_breakdown"] = {}
    for k in _phase_stats:
        _phase_stats[k] = {"n": 0, "ms": 0.0}

    svc = L.LLMService(query)
    t0 = time.time()
    final = {}
    for ev in svc.run():
        if ev.get("type") == "done":
            final = ev.get("response", {})
    total = (time.time() - t0) * 1000

    print(f"\n{'='*70}\n问题：{query}")
    print(f"总耗时：{total:,.0f} ms ｜ LLM 调用 {_stats['llm_calls']} 次 / "
          f"{_stats['llm_ms']:,.0f} ms（占比 {_stats['llm_ms'] / max(total, 1) * 100:.0f}%）")

    print("  阶段耗时：")
    for k, s in sorted(_phase_stats.items(), key=lambda x: -x[1]["ms"]):
        if s["n"]:
            print(f"    {k:26s} {s['ms']:8,.0f} ms  ×{s['n']}")

    if _stats["llm_breakdown"]:
        print("  LLM 调用明细（按耗时降序）：")
        for k, s in sorted(_stats["llm_breakdown"].items(), key=lambda x: -x[1]["ms"]):
            print(f"    {s['ms']:8,.0f} ms  ×{s['n']}  {k}")

    steps = final.get("steps") or []
    if steps:
        print("  步骤耗时：")
        for st in steps:
            print(f"    {st.get('step')}. {st.get('name'):14s} {st.get('duration_ms', 0):8,} ms")
    return total


if __name__ == "__main__":
    qs = [
        "各产线的产量是多少",
        "统计各产品的不良数量",
        "最近7天各产线的产量趋势",
    ]
    totals = []
    for q in qs:
        try:
            totals.append(run_one(q))
        except Exception as e:
            print(f"  ❌ {q} 执行失败: {str(e)[:120]}")
    if totals:
        print(f"\n{'='*70}\n平均总耗时：{sum(totals) / len(totals):,.0f} ms")
