"""分析结果反馈队列 — 用户纠错/举报 → 复核闭环（P2-1）

用户对某次分析结果标注「口径不对 / 结果不对 / 图表不对 / 其他」并附说明，
进入复核队列（jsonl 持久化）；管理员可查看、标记处理，形成持续改进闭环。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

# 与 metrics_registry.json 等运行时数据同目录（backend/ 下）；本文件在 backend/ 根，用 parent 即可
_FEEDBACK_PATH = Path(__file__).resolve().parent / "feedback_queue.jsonl"
_lock = threading.Lock()

FEEDBACK_TYPES = ("口径不对", "结果不对", "图表不对", "数据范围不对", "其他")


def submit_feedback(user: dict, query: str, sql: str, feedback_type: str,
                    note: str = "", executed_sql: str = "") -> dict:
    """提交一条反馈，返回条目（含 id）。feedback_type 非法时抛 ValueError。"""
    if feedback_type not in FEEDBACK_TYPES:
        raise ValueError(f"反馈类型仅支持: {'/'.join(FEEDBACK_TYPES)}")
    item = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "user": (user.get("username") or user.get("name") or "unknown"),
        "role": user.get("role", ""),
        "query": (query or "")[:500],
        "sql": (executed_sql or sql or "")[:2000],
        "feedback_type": feedback_type,
        "note": (note or "")[:1000],
        "status": "pending",   # pending → resolved / rejected
        "resolution": "",
        "resolved_by": "",
        "resolved_at": None,
    }
    with _lock:
        with open(_FEEDBACK_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return item


def _load_all() -> list[dict]:
    """读取队列全部条目（跳过损坏行），未排序。"""
    items: list[dict] = []
    if not _FEEDBACK_PATH.exists():
        return items
    try:
        with open(_FEEDBACK_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return items


def list_feedback(status: str | None = None, limit: int = 100) -> list[dict]:
    """列出反馈队列（按时间倒序）；status 过滤 pending/resolved/rejected。"""
    items = _load_all()
    if status:
        items = [i for i in items if i.get("status") == status]
    items.sort(key=lambda i: i.get("ts", 0), reverse=True)
    return items[:limit]


def resolve_feedback(feedback_id: str, resolution: str, user: dict) -> dict:
    """处理反馈：resolution 为 'resolved'（已修正口径）或 'rejected'（驳回）。"""
    if resolution not in ("resolved", "rejected"):
        raise ValueError("resolution 仅支持 resolved / rejected")
    # 必须读全部条目（不能用 list_feedback 默认 limit=100），否则重写时会丢失
    # limit 之外的历史反馈 —— 这是曾经的数据丢失隐患
    items = _load_all()
    found = None
    for i in items:
        if i.get("id") == feedback_id:
            found = i
            break
    if not found:
        raise KeyError(f"反馈 {feedback_id} 不存在")
    found["status"] = resolution
    found["resolution"] = "已修正口径" if resolution == "resolved" else "驳回（无需处理）"
    found["resolved_by"] = user.get("username") or "admin"
    found["resolved_at"] = time.time()
    # 重写文件（保留原有顺序）
    with _lock:
        with open(_FEEDBACK_PATH, "w", encoding="utf-8") as f:
            for i in items:
                f.write(json.dumps(i, ensure_ascii=False) + "\n")
    return found
