# -*- coding: utf-8 -*-
"""跨会话经营记忆中心（P1-2，对标 FineBI 经营记忆中心：系统级、跨会话、可复用）

## 与既有记忆模块的分工（避免重复造轮子）
- `conversation_memory` / `conv_memory`：**会话内**多轮记忆（实体/槽位继承）；
- `memory`（AgentMemory）：**SQL 示例**记忆（few-shot 素材）；
- `skill_store`：**已验证分析路径**沉淀（可编排技能雏形）；
- 本模块：**系统级经营记忆**——跨会话、带 ACL、直接服务「业务上下文复用」：
  1. **指标偏好**：高频（指标×维度）组合自动累计（如「良率×工序」常被组合查询），
     新会话问相似问题时注入，减少 LLM 重复试探；
  2. **业务口径备注**：人工录入的口径说明（管理员 可写，全员可读），
     命中指标时注入 prompt（口径最高优先级之外的补充业务上下文）。

## 设计
- 确定性累计（不做 LLM 总结，避免不可审计）；JSON 落盘（与项目存储风格一致）；
- ACL 隔离：备注按创建者可见性过滤（管理员 全量，普通角色仅自己）；
- 只注入、不执行——与架构红线一致（LLM 仍走确定性编译/二次确认链路）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

_logger = logging.getLogger("memory_center")

_FILE = Path(__file__).resolve().parent.parent / "memory_center.json"
_lock = threading.Lock()

_MAX_PREFERENCES = 100
_MAX_NOTES = 200


def _load() -> dict:
    default = {"preferences": {}, "notes": []}
    try:
        if _FILE.exists():
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        _logger.warning("经营记忆加载失败: %s", e)
    return default


def _save(data: dict) -> None:
    try:
        _FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        _logger.warning("经营记忆保存失败（保持内存）: %s", e)


# ── 指标偏好（自动累计，跨会话）────────────────────────

def record_preference(metric: str, dims: list[str] | None = None) -> None:
    """查询成功后累计（指标×维度）组合频次。幂等、线程安全。"""
    if not metric:
        return
    dims = [str(d).strip() for d in (dims or []) if str(d).strip()]
    key = metric + "|" + ",".join(sorted(dims))
    now = time.time()
    with _lock:
        data = _load()
        pref = data.setdefault("preferences", {})
        p = pref.get(key)
        if p is None:
            p = pref[key] = {"metric": metric, "dims": dims, "count": 0,
                             "first_seen": time.strftime("%Y-%m-%d %H:%M:%S")}
        p["count"] = int(p.get("count") or 0) + 1
        p["last_used"] = now
        # 淘汰：保留频次最高的 Top 100
        if len(pref) > _MAX_PREFERENCES:
            for k in sorted(pref, key=lambda k: int(pref[k].get("count") or 0))[:len(pref) - _MAX_PREFERENCES]:
                pref.pop(k, None)
        _save(data)


def get_preferences(limit: int = 5) -> list[dict]:
    """高频（指标×维度）组合，按频次降序。"""
    with _lock:
        data = _load()
        prefs = list((data.get("preferences") or {}).values())
    prefs.sort(key=lambda p: int(p.get("count") or 0), reverse=True)
    return prefs[:limit]


# ── 业务口径备注（人工录入，ACL 隔离）────────────────────

def add_note(metric: str, note: str, created_by: str, role: str = "") -> dict:
    metric = (metric or "").strip()[:40]
    note = (note or "").strip()
    if not metric or not note:
        raise ValueError("指标名与备注内容不能为空")
    item = {
        "id": uuid.uuid4().hex[:10],
        "metric": metric,
        "note": note[:500],
        "created_by": created_by or "unknown",
        "role": role or "guest",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        data = _load()
        notes = data.setdefault("notes", [])
        notes.append(item)
        if len(notes) > _MAX_NOTES:
            data["notes"] = notes[-_MAX_NOTES:]
        _save(data)
    return item


def list_notes(metric: str = "", role: str = "", username: str = "") -> list[dict]:
    """备注列表：按指标过滤 + 按角色可见性（管理员 全量，普通角色仅自己）。"""
    with _lock:
        data = _load()
        notes = list(data.get("notes") or [])
    if metric:
        notes = [n for n in notes if n.get("metric") == metric or metric in (n.get("metric") or "")]
    if role not in ("admin",):
        notes = [n for n in notes if n.get("created_by") == username]
    notes.sort(key=lambda n: str(n.get("created_at") or ""), reverse=True)
    return notes


def delete_note(nid: str) -> bool:
    with _lock:
        data = _load()
        notes = data.get("notes") or []
        before = len(notes)
        data["notes"] = [n for n in notes if n.get("id") != nid]
        if len(data["notes"]) != before:
            _save(data)
            return True
    return False


# ── 注入生成（prompt 素材，只注入不执行）──────────────────

def memory_hint(query: str, role: str = "", username: str = "", k: int = 3) -> str:
    """按问句命中经营记忆，生成可注入 prompt 的上下文段落；无命中返回空串。

    - 指标偏好：问句命中的指标若存在高频组合 → 提示「该指标常与 X 维度一起分析」；
    - 口径备注：问句命中的指标若有人工备注 → 注入备注（口径补充说明）。
    """
    lines: list[str] = []
    try:
        from agent.metric_registry import find_metrics
        hits = find_metrics(query)
        hit_names = {m["name"] for m in hits}
        if not hit_names:
            hit_names = {_extract_metric_word(query)} if _extract_metric_word(query) else set()
    except Exception:
        hit_names = {_extract_metric_word(query)} if _extract_metric_word(query) else set()
    if not hit_names:
        return ""

    with _lock:
        data = _load()
        prefs = list((data.get("preferences") or {}).values())
        notes = list(data.get("notes") or [])

    for name in hit_names:
        # 偏好：与问句中其他指标词/维度组合相关的高频组合
        for p in prefs:
            if p.get("metric") == name and int(p.get("count") or 0) >= 3:
                dims = p.get("dims") or []
                if dims and not any(d in query for d in dims):
                    lines.append(f"· 经营记忆：指标「{name}」历史上常与维度「{'、'.join(dims[:3])}」一起分析（累计 {p['count']} 次），可参考该分析角度")
    # 备注：按角色可见性过滤后注入
    for n in notes:
        if n.get("metric") in hit_names:
            if role not in ("admin",) and n.get("created_by") != username:
                continue
            lines.append(f"· 口径备注（{n.get('created_by') or '管理员'}）：{n.get('note')}")
    if not lines:
        return ""
    return "\n".join(lines[:k + 2])


def _extract_metric_word(query: str) -> str:
    """兜底：从问句提取疑似指标词（2-4 字中文，优先注册表候选）。"""
    try:
        from agent.metric_registry import _guess_metric_words
        words = _guess_metric_words(query or "")
        return words[0] if words else ""
    except Exception:
        m = re.search(r"([\u4e00-\u9fa5]{2,6}(?:率|量|额|数|天数|次数|时长|金额|单价))", query or "")
        return m.group(1) if m else ""
