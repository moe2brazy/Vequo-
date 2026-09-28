# -*- coding: utf-8 -*-
"""运行时观测（P1-1 高优先级第 3 项，对标 Fabric data agent 监控）。

Fabric 的 data agent 有在线指标（准确率/延迟/调用量）；我们此前只有离线黄金集评测
+ 文本日志（_log_run_metrics）。本模块把每轮查询的结构化观测收进内存环形缓冲：
- 编译命中率（compiled vs llm）：确定性编译的覆盖率——命中率低 = LLM 兜底多 = 慢且不稳；
- 平均/P95 耗时、成功率、各意图分布；
- 最近 N 轮明细（可查最近失败，定位瓶颈）。

数据只留最近 N 条（默认 500），不做持久化——观测是辅助，不该引入存储依赖。
对外 API：
  record(...)      每轮查询调用一次（在 run() 收尾处）
  get_overview()   汇总指标（供 /api/ops/overview）
  recent(limit)    最近轮次明细
"""

from __future__ import annotations

import statistics
import threading
import time

_MAX = int(__import__("os").getenv("OBS_MAX_ROUNDS", "500"))

_lock = threading.Lock()
_rounds: list[dict] = []


def record(query: str, intent: str = "", tables: list[str] | None = None,
           ok: bool = False, rows: int = 0, elapsed_ms: int = 0,
           compiled: bool = False, refined: bool = False,
           warning: str = "", error: str = ""):
    """记录一轮查询观测。线程安全；超过上限淘汰最旧。"""
    try:
        entry = {
            "ts": time.time(),
            "query": (query or "")[:100],
            "intent": intent or "?",
            "tables": (tables or [])[:5],
            "ok": bool(ok),
            "rows": rows,
            "elapsed_ms": elapsed_ms,
            "compiled": bool(compiled),
            "refined": bool(refined),
            "warning": (warning or "")[:120],
            "error": (error or "")[:150],
        }
        with _lock:
            _rounds.append(entry)
            if len(_rounds) > _MAX:
                del _rounds[:len(_rounds) - _MAX]
    except Exception:
        pass


def get_overview() -> dict:
    """汇总最近观测窗口的指标。空窗口也返回完整结构（字段与非空一致，避免调用方分支）。"""
    with _lock:
        rs = list(_rounds)
    if not rs:
        return {
            "success": True, "total": 0, "window": _MAX,
            "ok_rate": 0.0, "compile_rate": 0.0, "llm_rate": 0.0, "refine_rate": 0.0,
            "avg_ms": 0, "p95_ms": 0,
            "intent_dist": {}, "recent_fails": [], "recent": [],
        }

    n = len(rs)
    ok_n = sum(1 for r in rs if r["ok"])
    compiled_n = sum(1 for r in rs if r["compiled"])
    refined_n = sum(1 for r in rs if r["refined"])
    ms = [r["elapsed_ms"] for r in rs if r["elapsed_ms"] > 0]

    def _p95(vals):
        if not vals:
            return 0
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(len(vals) * 0.95))]

    # 意图分布
    intent_dist: dict[str, int] = {}
    for r in rs:
        intent_dist[r["intent"]] = intent_dist.get(r["intent"], 0) + 1
    # 最近失败（按时间倒序）
    fails = [r for r in reversed(rs) if not r["ok"]][:5]
    # 最近轮次（按时间倒序，剔除 error 截断）
    recent = [r for r in reversed(rs)][:20]
    return {
        "success": True,
        "total": n,
        "window": _MAX,
        "ok_rate": round(ok_n / n, 3),
        "compile_rate": round(compiled_n / n, 3),   # 确定性编译覆盖率（越低越要补口径）
        "llm_rate": round((n - compiled_n) / n, 3),
        "refine_rate": round(refined_n / n, 3),
        "avg_ms": round(statistics.fmean(ms), 1) if ms else 0,
        "p95_ms": _p95(ms),
        "intent_dist": intent_dist,
        "recent_fails": fails,
        "recent": recent,
    }


def recent(limit: int = 20) -> list[dict]:
    with _lock:
        return [dict(r) for r in reversed(_rounds[-limit:])]
