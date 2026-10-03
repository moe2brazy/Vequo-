# -*- coding: utf-8 -*-
"""定时报表订阅（交付闭环，对标 Tableau Pulse / 白泽 定时推送）。

把「主动洞察」从"等用户打开网页看"升级为"定时生成并推送"：
- 订阅：按固定间隔（分钟）周期性生成报告；
- 报告类型（report_type）：
  - "insights" —— 主动洞察日报：自动扫描重点表找异常，汇总成 markdown 推送；
- 守护线程：每 30s 醒来一次检查到期订阅，生成 + 推送（异步，不阻塞主服务）。

设计取舍：
- 只支持 interval（固定间隔），不做 cron —— 私有化单应用场景够用，避免引入 cron 解析依赖；
- 持久化可选（REPORT_SCHED_PERSIST=1），默认内存——调度是辅助能力，重启丢失可接受；
- 实际推送仍由 notifier 的 NOTIFY_ENABLED 开关 + 通道/订阅配置决定，未配通道时空转。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

_logger = logging.getLogger(__name__)

_PERSIST = os.getenv("REPORT_SCHED_PERSIST", "0") == "1"
_FILE = Path(__file__).resolve().parent.parent / "report_scheduler.json"

# 安全修复（P0）：原为 threading.Lock()（不可重入），而 _save() 内部又取同一把锁；
# 调用它的 add_schedule / _run_due / delete_schedule 都已在持锁路径上 → 一旦设置
# REPORT_SCHED_PERSIST=1，第一次新建订阅就会永久挂死该请求线程（守护线程也会在
# _run_due→_save 处卡死，调度彻底停摆）。默认 _PERSIST=0 时 _save 提前 return，掩盖了这个死锁。
_lock = threading.RLock()
_schedules: list[dict] = []      # {id, name, interval_min, report_type, next_run, enabled, created_at}
_daemon_started = False
_daemon: threading.Thread | None = None
_stop_ev = threading.Event()

_CHECK_INTERVAL = 30.0           # 守护线程轮询间隔（秒）


def _save() -> None:
    """持久化订阅列表（P2 修复：此前无锁，多线程写文件可能撕裂）。"""
    if not _PERSIST:
        return
    try:
        with _lock:
            _FILE.write_text(json.dumps(_schedules, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load() -> None:
    global _schedules
    if not _PERSIST:
        return
    try:
        if _FILE.exists():
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _schedules = data
                # 有持久化订阅 → 立即恢复调度（否则重启后订阅"死"掉，永不触发）
                if _schedules:
                    _start_daemon()
    except Exception:
        pass


def _start_daemon() -> None:
    """惰性启动守护线程（首次添加订阅时）。"""
    global _daemon_started, _daemon
    if _daemon_started:
        return
    _daemon_started = True
    _stop_ev.clear()
    _daemon = threading.Thread(target=_loop, name="report-scheduler", daemon=True)
    _daemon.start()


def stop_scheduler() -> None:
    """停止守护线程（模块卸载/应用关闭时调用，P2 修复：
    此前线程无停止机制，reload 场景可能悬挂）。"""
    global _daemon_started, _daemon
    _stop_ev.set()
    if _daemon is not None:
        _daemon.join(timeout=3)
        _daemon = None
    _daemon_started = False


def _loop() -> None:
    while not _stop_ev.wait(_CHECK_INTERVAL):
        try:
            _run_due()
        except Exception as e:
            _logger.warning("定时报表调度异常: %s", e)


def _run_due() -> int:
    """检查到期订阅，生成并推送报告。返回本次触发的订阅数。"""
    now = time.time()
    due = []
    with _lock:
        for s in _schedules:
            if s.get("enabled", True) and now >= s.get("next_run", 0):
                due.append(s)
    for s in due:
        ok = True
        try:
            _dispatch(s)
        except Exception as e:
            ok = False
            _logger.warning("报表订阅 %s 生成失败: %s", s.get("id"), e)
        with _lock:
            # 2026-10-03 修复：next_run 的推进原来在 try 内、_dispatch 之后，
            # 于是失败时 next_run 保持为过去的 due 时刻，守护线程每 30 秒重试一次
            # → 日志被刷满、真实错误被淹没（告警风暴）。现在无论成败都推进，
            # 失败时按固定间隔重试；连续失败达阈值则自动停用。
            s["next_run"] = now + max(1, int(s.get("interval_min", 60))) * 60
            s["fail_count"] = int(s.get("fail_count") or 0) + (0 if ok else 1)
            if not ok and s["fail_count"] >= 5:
                s["enabled"] = False
                _logger.warning("报表订阅 %s 连续失败 5 次，已自动停用", s.get("id"))
            _save()
    return len(due)


def _dispatch(sched: dict) -> None:
    """按订阅类型生成报告并推送。"""
    from agent.notifier import push_event
    rtype = sched.get("report_type", "insights")
    if rtype == "insights":
        content = _build_insights_report()
        push_event("report", f"数据洞察日报 · {sched.get('name', '')}",
                   content, severity=1.0, event_key=f"report:{sched.get('id')}")
    else:
        _logger.warning("未知报表类型: %s", rtype)


def _build_insights_report(max_insights: int = 8) -> str:
    """生成主动洞察日报（markdown）。扫描失败/无洞察返回说明性文本。"""
    try:
        from agent.insight_scan import scan_top_tables
        r = scan_top_tables(limit=3, max_insights=max_insights)
        ins = r.get("insights") or []
        if not ins:
            return "今日未发现显著异常。"
        lines = [f"扫描重点表后共发现 {len(ins)} 条洞察：", ""]
        for i, it in enumerate(ins, 1):
            sev = it.get("severity", 1.0)
            flag = "🔴" if sev >= 2.0 else ("🟡" if sev >= 1.0 else "🟢")
            lines.append(f"{i}. {flag} {it.get('message', '')}")
        return "\n".join(lines)
    except Exception as e:
        return f"洞察日报生成失败：{str(e)[:120]}"


# ── 对外接口 ─────────────────────────────────────────────

def add_schedule(name: str, interval_min: int = 60, report_type: str = "insights") -> dict:
    """新建定时订阅。interval_min 最小 1 分钟。返回订阅对象（含 id）。"""
    sched = {
        "id": uuid.uuid4().hex[:12],
        "name": (name or "洞察日报").strip()[:60],
        "interval_min": max(1, int(interval_min)),
        "report_type": report_type or "insights",
        "next_run": time.time() + max(1, int(interval_min)) * 60,
        "enabled": True,
        "created_at": time.time(),
    }
    with _lock:
        _schedules.append(sched)
        _save()
    _start_daemon()
    return {**sched}


def list_schedules() -> list[dict]:
    with _lock:
        return [{**s, "next_in_sec": max(0, int(s.get("next_run", 0) - time.time()))}
                for s in _schedules]


def delete_schedule(sid: str) -> bool:
    with _lock:
        before = len(_schedules)
        _schedules[:] = [s for s in _schedules if s.get("id") != sid]
        if len(_schedules) != before:
            _save()
            return True
    return False


def get_schedule(sid: str) -> dict | None:
    with _lock:
        for s in _schedules:
            if s.get("id") == sid:
                return {**s}
    return None


# 导入即加载持久化订阅（若有）
_load()
