"""结果缓存存储层 — Redis（可配）+ 进程内内存回退

- 配置 REDIS_URL 后启用 Redis（跨实例共享、可横向扩展）；未配置/连接失败自动回退内存。
- 缓存 key 由调用方注入 ACL 指纹，确保不同权限用户不共享缓存（防越权复用）。
- 序列化 JSON，带 TTL 过期。
"""

from __future__ import annotations

import json
import os
import threading
import time

try:
    import redis as _redis
except ImportError:  # 未安装 redis 库时优雅降级
    _redis = None

REDIS_URL = os.getenv("REDIS_URL", "").strip()
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))
CACHE_MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "500"))

_mem: dict[str, dict] = {}
_lock = threading.Lock()
_client_singleton = None
_client_init_lock = threading.Lock()

# Redis 熔断（P2 修复）：连续失败达阈值 → 短时熔断走内存，
# 避免「Redis 半挂」时每条请求都试 Redis 失败又回退，导致读写后端不一致。
_REDIS_CIRCUIT_FAIL_THRESHOLD = 3
_REDIS_CIRCUIT_OPEN_SECONDS = 30
_redis_fail_count = 0
_redis_circuit_until = 0.0


def _client():
    """返回复用的 Redis 客户端单例（内部自带连接池，避免每次读写新建连接）。"""
    global _client_singleton
    if not (_redis and REDIS_URL):
        return None
    if _client_singleton is None:
        with _client_init_lock:
            if _client_singleton is None:
                try:
                    _client_singleton = _redis.Redis.from_url(
                        REDIS_URL, decode_responses=True,
                        socket_connect_timeout=2, socket_timeout=2,
                        health_check_interval=30)
                except Exception:
                    return None
    return _client_singleton


def _usable_client():
    """Redis 客户端（未熔断时）；熔断期或不可用返回 None → 走内存。"""
    if _redis_circuit_until > time.time():
        return None
    return _client()


def _record_redis_ok() -> None:
    global _redis_fail_count
    _redis_fail_count = 0


def _record_redis_fail() -> None:
    global _redis_fail_count, _redis_circuit_until
    _redis_fail_count += 1
    if _redis_fail_count >= _REDIS_CIRCUIT_FAIL_THRESHOLD:
        _redis_circuit_until = time.time() + _REDIS_CIRCUIT_OPEN_SECONDS
        _redis_fail_count = 0


def cache_get(key: str):
    c = _usable_client()
    if c is not None:
        try:
            raw = c.get(key)
            _record_redis_ok()
            return json.loads(raw) if raw else None
        except Exception:
            _record_redis_fail()
    with _lock:
        e = _mem.get(key)
        if not e:
            return None
        ttl = e.get("ttl", CACHE_TTL_SECONDS)
        if time.time() - e["t"] > ttl:
            _mem.pop(key, None)
            return None
        return e["v"]


def cache_set(key: str, value, ttl: int | None = None) -> None:
    """写入缓存。ttl 为条目级过期秒数（支持按表类型分级：维度表长 TTL / 事实表短 TTL）；
    不传则用全局 CACHE_TTL_SECONDS。"""
    if ttl is None:
        ttl = CACHE_TTL_SECONDS
    c = _usable_client()
    if c is not None:
        try:
            c.setex(key, ttl, json.dumps(value, ensure_ascii=False, default=str))
            _record_redis_ok()
            return
        except Exception:
            _record_redis_fail()
    with _lock:
        _mem[key] = {"t": time.time(), "ttl": ttl, "v": value}
        if len(_mem) > CACHE_MAX_ENTRIES:
            oldest = min(_mem.items(), key=lambda kv: kv[1]["t"])[0]
            _mem.pop(oldest, None)


def cache_delete(key: str) -> None:
    c = _usable_client()
    if c is not None:
        try:
            c.delete(key)
            _record_redis_ok()
            return
        except Exception:
            _record_redis_fail()
    with _lock:
        _mem.pop(key, None)


def cache_enabled() -> bool:
    """当前是否使用 Redis（供前端/日志判断缓存外部化状态）"""
    return bool(_redis and REDIS_URL)


def cache_clear() -> int:
    """清空进程内缓存条目，返回清除条数（切库/手动刷新时调用）。

    Redis 侧不做全量清理：结果缓存 key 含 db_key 前缀（切库后 db_key 变化，
    旧 key 自然失配、由 TTL 兜底回收），且 flushdb 会误伤同实例其他数据。
    """
    with _lock:
        n = len(_mem)
        _mem.clear()
        return n
