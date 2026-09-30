"""主动监测告警 —— 对标 Tableau Pulse 的指标异动检测。

Pulse 机制：盯住关键指标 → 变化时自动推送摘要（而非被动等用户来问）。
本实现按现有架构落地：
- 监控规则（管理员配置）：{name, sql, change_pct 变化阈值%}
- 后台 daemon 线程按固定间隔执行规则 SQL → 取首个数值 → 与上次基线对比 →
  变化幅度超阈值 → 生成异动事件（可查、可展示）
- 规则与异动事件内存保存 + JSON 文件持久化（写入失败自动回退内存模式，不崩）
- 所有 SQL 走只读校验 + LIMIT 保护（与主流程同级别）
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

_logger = logging.getLogger("monitor")

_RULES_FILE = Path(__file__).resolve().parent.parent / "monitor_rules.json"
_DEFAULT_INTERVAL_MIN = int(os.getenv("MONITOR_INTERVAL_MIN", "10"))  # 默认 10 分钟
# 默认内存模式（规则重启后丢失）：避免在文件系统受控（防病毒实时扫描）环境下
# 写文件被外部进程终止；需要规则持久化时显式 MONITOR_PERSIST=1。
_PERSIST = os.getenv("MONITOR_PERSIST", "0") == "1"
_MAX_ALERTS = 200

# 用 RLock：_save() 内部会再取锁，而 add_rule/update_rule/delete_rule/run_all
# 都在持锁状态下调用 _save()（见各调用点）——普通 Lock 会在此处死锁，监控系统卡死。
_lock = threading.RLock()
_rules: list[dict] = []
_alerts: list[dict] = []
_running = False
_stop_ev = threading.Event()
_thread: threading.Thread | None = None


# ── 持久化 ──────────────────────────────────────────────

def _load() -> None:
    """启动时读取规则文件（含上次基线 last_value，重启后对比不断档）。"""
    global _rules
    if not _PERSIST:
        return
    try:
        if _RULES_FILE.exists():
            data = json.loads(_RULES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _rules = [r for r in data if isinstance(r, dict) and r.get("id")]
    except Exception:
        _rules = []


def _save() -> None:
    if not _PERSIST:
        return
    try:
        with _lock:
            snapshot = list(_rules)
        _RULES_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        _logger.warning("监控规则持久化失败（保持内存模式）: %s", e)


# ── 检测核心 ────────────────────────────────────────────

def _safe_execute(sql: str) -> dict:
    """只读校验 + 行/列级 ACL 改写 + 执行（与主流程同级别护栏）。

    【权限修复】原先这里只做只读校验就直接执行，漏掉了 enforcer.rewrite_sql 这一步。
    影响面比表面大：monitor 的规则检测、insight_scan 的异常扫描（scan_table /
    scan_top_tables）、data_charts 的各路图表取数，最终都汇聚到这个函数，
    于是行级过滤与列级脱敏在这三条链路上全部不生效——表级白名单是各自另做的，
    所以表现为「该看的表拦住了，但只看本车间的账号仍能扫到全厂口径、
    被隐藏/脱敏的列仍会出现在洞察与图表结果里」。

    ACL 从 security.context 的 ContextVar 读（API 端点在请求内 set_acl）。
    后台线程（监控规则定时巡检）拿不到 ACL → acl 为 None → 原样执行，
    与 investigator._safe_execute 的既有口径一致。
    """
    if not sql or not sql.strip():
        return {"success": False, "error": "SQL 为空"}
    try:
        from agent.sql_validator import validate_sql_safety
        from db.executor import execute_sql
        ok, err, cleaned = validate_sql_safety(sql)
        if not ok:
            return {"success": False, "error": f"安全校验未通过：{err}"}
        # 权限改写（行过滤 / 列隐藏 / 列脱敏）：与 investigator / 主流程同一套
        try:
            from security.context import get_acl
            from security.enforcer import rewrite_sql
            acl = get_acl()
            if acl is not None:
                eff, acl_err, _applied = rewrite_sql(cleaned, acl)
                if acl_err:
                    # 改写失败一律 fail-close：宁可这条规则/扫描报错，也不能拿未过滤的结果
                    return {"success": False, "error": f"权限校验未通过：{acl_err}"}
                cleaned = eff
        except ImportError:
            pass  # ACL 模块不可用（离线脚本/评测环境）时退化为只读校验
        return execute_sql(cleaned)
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


def _first_value(r: dict) -> float | None:
    """取结果第一行的第一个数值（监控 SQL 约定返回单值，如 SELECT COUNT(*)...）。"""
    if not r.get("success") or not r.get("rows"):
        return None
    row = r["rows"][0]
    for v in row.values():
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def check_rule(rule: dict) -> dict | None:
    """执行一条规则并与上次基线对比；超阈值返回异动事件，否则 None。

    副作用：更新规则 last_value / last_run / last_error（供列表展示与下次对比）。
    """
    if not rule.get("enabled", True):
        return None
    r = _safe_execute(rule.get("sql", ""))
    now = time.time()
    if not r.get("success"):
        rule["last_error"] = str(r.get("error", ""))[:120]
        rule["last_run"] = now
        return None
    rule["last_run"] = now
    rule["last_error"] = ""
    val = _first_value(r)
    prev = rule.get("last_value")
    rule["last_value"] = val
    if val is None or prev is None or prev == 0:
        return None  # 首次运行只建立基线 / 无量纲不可比
    change_pct = (val - prev) / abs(prev) * 100
    try:
        thr = float(rule.get("change_pct") or 10)
    except (TypeError, ValueError):
        # 持久化数据被写坏（如 "abc"）时用默认阈值，避免整条链路抛异常（P2 修复）
        thr = 10.0
    if abs(change_pct) < thr:
        return None
    return {
        "id": uuid.uuid4().hex[:12],
        "rule_id": rule.get("id"),
        "rule_name": rule.get("name", "未命名"),
        "value": round(val, 4),
        "prev_value": round(prev, 4),
        "change_pct": round(change_pct, 2),
        "direction": "up" if change_pct > 0 else "down",
        "detected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "message": f"「{rule.get('name', '')}」{change_pct:+.1f}%（{prev:g} → {val:g}）",
    }


def run_all() -> int:
    """全量跑一次所有启用规则，返回本次产生的异动数。"""
    with _lock:
        rules = list(_rules)
    n = 0
    for rule in rules:
        try:
            ev = check_rule(rule)
            if ev:
                with _lock:
                    _alerts.append(ev)
                    if len(_alerts) > _MAX_ALERTS:
                        del _alerts[: len(_alerts) - _MAX_ALERTS]
                n += 1
                _logger.info("指标异动: %s", ev["message"])
        except Exception as e:
            _logger.warning("监测规则执行异常 %s: %s", rule.get("id"), e)
    with _lock:
        _save()  # 落盘最新基线
    return n


# ── 后台线程 ────────────────────────────────────────────

def _loop() -> None:
    interval = max(1, _DEFAULT_INTERVAL_MIN) * 60
    while not _stop_ev.wait(interval):
        try:
            run_all()
        except Exception as e:
            # 单轮整体失败不能静默（P2 修复）：打日志，下一轮继续
            _logger.exception("监测循环异常: %s", e)


def start_monitor() -> None:
    """启动监测线程（幂等：已启动则跳过）。由应用启动时调用。"""
    global _running, _thread
    if _running:
        return
    _load()
    _running = True
    _stop_ev.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="metric-monitor")
    _thread.start()
    _logger.info("指标监测已启动（间隔 %s 分钟，规则 %s 条）", _DEFAULT_INTERVAL_MIN, len(_rules))


def stop_monitor() -> None:
    global _running
    _stop_ev.set()
    _running = False


# ── 管理 API（供 main.py 路由）──────────────────────────

def list_rules() -> list[dict]:
    with _lock:
        return [dict(r) for r in _rules]


def list_alerts(limit: int = 20) -> list[dict]:
    with _lock:
        return list(reversed(_alerts[-limit:]))


def add_rule(name: str, sql: str, change_pct: float = 10.0) -> dict:
    rule = {
        "id": uuid.uuid4().hex[:12],
        "name": (name or "").strip()[:60] or "未命名监控",
        "sql": sql,
        "change_pct": max(0.1, float(change_pct)),
        "enabled": True,
        "last_value": None,
        "last_run": None,
        "last_error": "",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        _rules.append(rule)
        _save()
    return dict(rule)


def update_rule(rule_id: str, name: str | None = None, sql: str | None = None,
                change_pct: float | None = None, enabled: bool | None = None) -> dict | None:
    with _lock:
        for r in _rules:
            if r.get("id") == rule_id:
                if name is not None:
                    r["name"] = name.strip()[:60]
                if sql is not None:
                    r["sql"] = sql
                if change_pct is not None:
                    r["change_pct"] = max(0.1, float(change_pct))
                if enabled is not None:
                    r["enabled"] = bool(enabled)
                _save()
                return dict(r)
    return None


def delete_rule(rule_id: str) -> bool:
    with _lock:
        before = len(_rules)
        _rules[:] = [r for r in _rules if r.get("id") != rule_id]
        if len(_rules) != before:
            _save()
            return True
    return False
