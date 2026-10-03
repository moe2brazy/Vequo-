# -*- coding: utf-8 -*-
"""多渠道 IM 交付（P2-3，对标 Aloudata / Tableau Pulse / 帆软 FineBI 的消息推送）。

## 竞品做法
Pulse / Aloudata / FineBI 的共同点：把洞察（指标异动 / 主动洞察 / 盲点）从"等用户
来问"变成"主动推送到用户所在的 IM（企微 / 钉钉 / 飞书 / Slack / 邮件）"。
核心机制三件套：**通道（webhook）+ 订阅（谁收什么）+ 节流（别轰炸）**。

## 本实现
- 通道：企微 / 钉钉 / 飞书 webhook + 通用 JSON，各用对应的 markdown 格式；
- 订阅：{通道, 事件类型, 最低严重度, 节流分钟}，事件类型复用已有的
  monitor_alert / insight / blind_spot；
- 节流：同一通道 + 同一事件键，在节流窗口内只推一次（防告警风暴刷屏）；
- 推送：HTTP POST 异步发送（fire-and-forget），失败静默 + 记日志，绝不阻塞主链路。

默认关闭（NOTIFY_ENABLED=0）：webhook 属于外发动作，需管理员显式开启并配置通道。
事件由 monitor / insight_scan / blind_spot 的调用方主动触发 push_event。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import request

_logger = logging.getLogger("notifier")

_ENABLED = os.getenv("NOTIFY_ENABLED", "0") == "1"
_PERSIST = os.getenv("NOTIFY_PERSIST", "0") == "1"
_FILE = Path(__file__).resolve().parent.parent / "notifier.json"
_TIMEOUT = float(os.getenv("NOTIFY_HTTP_TIMEOUT", "8"))

_lock = threading.Lock()
_channels: list[dict] = []
_subscriptions: list[dict] = []
_last_sent: dict[tuple[str, str], float] = {}
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")

# 事件类型 → 人类可读名
EVENT_LABELS = {
    "monitor_alert": "指标异动",
    "insight": "主动洞察",
    "blind_spot": "数据盲点",
    "report": "定时报表",
}


# ── 通道管理 ──────────────────────────────────────────────

_PRIVATE_IP_RE = re.compile(
    r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|127\.|0\.)", re.IGNORECASE
)


def _validate_webhook_url(url: str) -> str:
    """SSRF 防护（P2）：仅允许 http/https，且 host 不能是本机/环回/内网地址。

    webhook 由管理员配置，但配置错误（如内网地址）可能被事件推送利用为 SSRF 通道，
    因此在写入通道时即校验，发送前再校验一次（纵深防御，防持久化文件被手改绕过）。
    """
    from urllib.parse import urlparse
    u = urlparse(url)
    host = (u.hostname or "").lower().rstrip(".")
    if u.scheme not in ("http", "https") or not host:
        raise ValueError("webhook 地址仅支持 http/https")
    if host in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("webhook 地址不能指向本机（SSRF 防护）")
    if _PRIVATE_IP_RE.match(host):
        raise ValueError("webhook 地址不能指向内网地址（SSRF 防护）")
    return url


def add_channel(name: str, webhook_url: str, ctype: str = "generic") -> dict:
    """新增推送通道。ctype ∈ {wecom, dingtalk, feishu, generic}。"""
    webhook_url = _validate_webhook_url((webhook_url or "").strip())
    c = {
        "id": uuid.uuid4().hex[:10],
        "name": (name or "").strip()[:40] or "未命名通道",
        "webhook_url": webhook_url,
        "type": ctype if ctype in ("wecom", "dingtalk", "feishu") else "generic",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        _channels.append(c)
        _save()
    return dict(c)


def list_channels() -> list[dict]:
    with _lock:
        return [dict(c) for c in _channels]


def delete_channel(channel_id: str) -> bool:
    with _lock:
        before = len(_channels)
        _channels[:] = [c for c in _channels if c.get("id") != channel_id]
        _subscriptions[:] = [s for s in _subscriptions if s.get("channel_id") != channel_id]
        if len(_channels) != before:
            _save()
            return True
    return False


# ── 订阅管理 ──────────────────────────────────────────────

def add_subscription(channel_id: str, event_types: list[str],
                     min_severity: float = 0.0, throttle_min: int = 30) -> dict:
    s = {
        "id": uuid.uuid4().hex[:10],
        "channel_id": channel_id,
        "event_types": [e for e in event_types if e in EVENT_LABELS] or ["insight"],
        "min_severity": float(min_severity),
        "throttle_min": max(1, int(throttle_min)),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        _subscriptions.append(s)
        _save()
    return dict(s)


def list_subscriptions() -> list[dict]:
    with _lock:
        return [dict(s) for s in _subscriptions]


def delete_subscription(sub_id: str) -> bool:
    with _lock:
        before = len(_subscriptions)
        _subscriptions[:] = [s for s in _subscriptions if s.get("id") != sub_id]
        if len(_subscriptions) != before:
            _save()
            return True
    return False


# ── 推送核心 ──────────────────────────────────────────────

def _format_payload(ctype: str, title: str, content: str) -> tuple[str, str]:
    """按渠道类型构造 (content_type, body)。markdown 卡片在企微/钉钉/飞书各有格式。"""
    if ctype == "wecom":
        return "application/json", json.dumps({
            "msgtype": "markdown",
            "markdown": {"content": f"### {title}\n{content}"},
        }, ensure_ascii=False)
    if ctype == "dingtalk":
        return "application/json", json.dumps({
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"### {title}\n\n{content}"},
        }, ensure_ascii=False)
    if ctype == "feishu":
        return "application/json", json.dumps({
            "msg_type": "text",
            "content": {"text": f"【{title}】\n{content}"},
        }, ensure_ascii=False)
    # generic：兼容任意 webhook（如 Slack incoming webhook / 自建）
    return "application/json", json.dumps({"text": f"【{title}】\n{content}"}, ensure_ascii=False)


def _send(channel: dict, title: str, content: str) -> bool:
    url = channel.get("webhook_url")
    if not url:
        return False
    try:
        # 纵深防御（P2）：发送前再校验一次，防持久化文件被手改绕过 add_channel 校验
        _validate_webhook_url(url)
        ctype, body = _format_payload(channel.get("type") or "generic", title, content)
        req = request.Request(url, data=body.encode("utf-8"), method="POST",
                              headers={"Content-Type": ctype})
        request.urlopen(req, timeout=_TIMEOUT)
        return True
    except Exception as e:
        _logger.warning("推送失败 [%s]: %s", channel.get("name"), e)
        return False


def push_event(event_type: str, title: str, content: str,
               severity: float = 1.0, event_key: str = "") -> int:
    """把一条事件推送给所有订阅了该类型且达到严重度的通道。

    返回实际推送的通道数。节流：同一通道 + 同一事件键在 throttle_min 内只推一次。
    异步发送（fire-and-forget），不阻塞调用方；未开启 NOTIFY_ENABLED 时直接返回 0。
    """
    if not _ENABLED:
        return 0
    with _lock:
        channels = list(_channels)
        subs = [s for s in _subscriptions
                if event_type in s.get("event_types", []) and severity >= s.get("min_severity", 0.0)]
    if not subs:
        return 0

    now = time.time()
    # 清理过期节流记录（防内存泄漏）：超过 24h 的窗口记录不再需要
    try:
        with _lock:
            stale = [k for k, ts in _last_sent.items() if now - ts > 86400]
            for k in stale:
                del _last_sent[k]
    except Exception:
        pass
    sent = 0
    for s in subs:
        ch = next((c for c in channels if c.get("id") == s.get("channel_id")), None)
        if not ch:
            continue
        # 节流键：event_key 优先，其次标题，再退化为内容前缀——
        # 避免 title 为空时所有事件共用空键互相压制（P2 修复）
        key = event_key or title or (content or "")[:20]
        tk = (s.get("channel_id"), key)
        with _lock:
            last = _last_sent.get(tk, 0.0)
            if now - last < s.get("throttle_min", 30) * 60:
                continue  # 节流窗口内已推过
            # 2026-10-03 修复（P1）：节流记录必须在**发送成功后**才写。
            # 原实现在 submit 之前就 `_last_sent[tk] = now`，而 _send 的失败处理
            # 只 warning 一行、无重试无补偿 → webhook 不可达时（改配置/网络抖动/
            # 机器人被踢）默认 throttle_min=30 分钟内**所有**异动/盲点/报表全部被
            # 节流拦掉，一条不推；恢复后窗口已过，用户收到的是残缺序列，
            # 且日志里只有 warn、没有「本应推送 N 条」。
            # 现在：占位改到提交后立即写（保持并发去重），失败时由 _send 回调清除，
            # 让下一次事件仍能投递。
            _last_sent[tk] = now
        fut = _pool.submit(_send, ch, title, content)

        def _clear_on_fail(_f= fut, _tk=tk):
            # 发送失败 → 撤销节流占位，下一次事件仍可投递
            try:
                if _f.exception() is not None:
                    with _lock:
                        if _last_sent.get(_tk) == now:
                            _last_sent.pop(_tk, None)
            except Exception:
                pass

        try:
            fut.add_done_callback(_clear_on_fail)
        except Exception:
            pass
        sent += 1
    return sent


# ── 持久化（可选）────────────────────────────────────────

def _save() -> None:
    if not _PERSIST:
        return
    try:
        _FILE.write_text(json.dumps({"channels": _channels, "subscriptions": _subscriptions},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        _logger.warning("通知配置持久化失败（保持内存模式）: %s", e)


def _load() -> None:
    global _channels, _subscriptions
    if not _PERSIST:
        return
    try:
        if _FILE.exists():
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _channels = [c for c in data.get("channels", []) if isinstance(c, dict)]
                _subscriptions = [s for s in data.get("subscriptions", []) if isinstance(s, dict)]
    except Exception:
        _channels, _subscriptions = [], []


# 缺陷修复（P0）：与 skill_store 同一类问题 —— _load() 只定义、从不调用，
# 于是 NOTIFY_PERSIST=1 时重启后管理员配置的推送通道与订阅全部"消失"
# （磁盘文件在，内存里却从空开始），用户侧表现为"配置了但不生效"。
_load()
