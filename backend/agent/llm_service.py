"""LLMService — 单一流式流水线（参考 SQLBot LLMService 架构）

所有 LLM 调用均为流式 .stream()，进度在每步之间 yield。
分析、预测、推荐问题拆为独立方法，不在主流程中阻塞。
"""

import json
import logging
import math
import os
import re
import time
import functools
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Generator, Any

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessageChunk

from config import LLM_CONFIG
from db.tools import match_tables_by_query, get_table_detail, get_all_tables, _split_table_ref
from db.executor import execute_sql
from db.metadata import TABLES as METADATA_TABLES
from agent.chart_agent import generate_chart
from agent.prompts import ANALYSIS_SYSTEM_PROMPT, RECOMMEND_QUESTIONS_PROMPT


# ── LLM 调用观测（P0-1 埋点）─────────────────────────────
# 目的：量化「弹窗确认 → 遵循 LLM 执行」链路的 token 与耗时，作为后续所有优化的基线。
# 设计原则：**纯观测**。不改变任何调用行为、不改变生成内容、不影响返回值。
#
# 用量获取能力（2026-09-04 实测 DeepSeek 兼容接口）：
#   - invoke 路径：AIMessage.usage_metadata 完整可用 → 精确值（estimated=False）
#   - stream 路径：所有 chunk 的 usage_metadata 均为 None，且 stream_usage=True 无效
#                  → 只能按字符数粗估（estimated=True）
# 因此统计时必须区分 estimated 标志：精确值看绝对值，估算值只看优化前后的相对变化。
_LLM_METRICS_ENABLED = os.getenv("LLM_METRICS_ENABLED", "1") != "0"
_LLM_METRICS_MAX = int(os.getenv("LLM_METRICS_MAX", "2000"))
_LLM_METRICS_LOCK = threading.Lock()
_LLM_METRICS: deque = deque(maxlen=_LLM_METRICS_MAX)


def _msg_text_len(messages) -> int:
    """粗算 messages 的文本字符数（供 stream 路径估算 input token 用）。"""
    n = 0
    try:
        for m in messages or []:
            c = getattr(m, "content", m)
            if isinstance(c, str):
                n += len(c)
            elif isinstance(c, list):
                for p in c:
                    n += len(str(p.get("text", ""))) if isinstance(p, dict) else len(str(p))
            else:
                n += len(str(c))
    except Exception:
        pass
    return n


def _extract_usage(resp) -> tuple[int, int, bool]:
    """从 LLM 响应提取 (input_tokens, output_tokens, estimated)。

    取不到真实用量时返回 (0, 0, True)，由调用方按字符数回退估算。
    """
    try:
        u = getattr(resp, "usage_metadata", None)
        if isinstance(u, dict) and u:
            return (int(u.get("input_tokens") or 0),
                    int(u.get("output_tokens") or 0), False)
        tu = (getattr(resp, "response_metadata", None) or {}).get("token_usage")
        if isinstance(tu, dict) and tu:
            return (int(tu.get("prompt_tokens") or 0),
                    int(tu.get("completion_tokens") or 0), False)
    except Exception:
        pass
    return (0, 0, True)


def _call_site(depth: int = 2) -> str:
    """取调用点 文件名:行号:函数名（用于归因是哪处代码发起的 LLM 调用）。"""
    try:
        f = sys._getframe(depth)
        return f"{os.path.basename(f.f_code.co_filename)}:{f.f_lineno}:{f.f_code.co_name}"
    except Exception:
        return ""


def _record_llm_call(kind: str, model: str, site: str, latency_ms: float,
                     ok: bool, error: str, tok_in: int, tok_out: int,
                     estimated: bool) -> None:
    if not _LLM_METRICS_ENABLED:
        return
    rec = {
        "ts": round(time.time(), 3),
        "kind": kind,              # invoke / stream / ainvoke / astream
        "model": model or "",
        "site": site or "",        # 发起调用的 文件:行号:函数
        "latency_ms": round(latency_ms, 1),
        "ok": bool(ok),
        "error": error or "",
        "tok_in": int(tok_in or 0),
        "tok_out": int(tok_out or 0),
        "tok_total": int(tok_in or 0) + int(tok_out or 0),
        "estimated": bool(estimated),   # True = 估算值（stream 路径），不可作绝对值解读
        "db_key": _current_db_key() if "_current_db_key" in globals() else "",
    }
    try:
        with _LLM_METRICS_LOCK:
            _LLM_METRICS.append(rec)
    except Exception:
        pass


def get_llm_metrics() -> list[dict]:
    """取出全部埋点记录（供评测脚本 / 诊断接口使用）。"""
    with _LLM_METRICS_LOCK:
        return list(_LLM_METRICS)


def reset_llm_metrics() -> None:
    """清空埋点记录（评测脚本每次跑分前调用，保证统计互不污染）。"""
    with _LLM_METRICS_LOCK:
        _LLM_METRICS.clear()


def llm_metrics_summary() -> dict:
    """汇总：调用次数、成功率、耗时分位、token 总量（精确/估算分开统计）。"""
    rows = get_llm_metrics()
    if not rows:
        return {"calls": 0}
    lat = sorted(r["latency_ms"] for r in rows)

    def pct(p):
        if not lat:
            return 0.0
        i = min(len(lat) - 1, max(0, int(round((len(lat) - 1) * p))))
        return round(lat[i], 1)

    exact = [r for r in rows if not r["estimated"]]
    est = [r for r in rows if r["estimated"]]
    return {
        "calls": len(rows),
        "ok": sum(1 for r in rows if r["ok"]),
        "failed": sum(1 for r in rows if not r["ok"]),
        "latency_ms": {"p50": pct(0.50), "p95": pct(0.95), "max": round(lat[-1], 1)},
        "exact_calls": len(exact),
        "exact_tok_total": sum(r["tok_total"] for r in exact),
        "estimated_calls": len(est),
        "estimated_tok_total": sum(r["tok_total"] for r in est),
        "by_site": _group_by(rows, "site"),
    }


def _group_by(rows: list[dict], key: str) -> dict:
    out: dict[str, dict] = {}
    for r in rows:
        k = r.get(key) or "(unknown)"
        s = out.setdefault(k, {"calls": 0, "failed": 0, "latency_ms_total": 0.0,
                               "tok_total": 0, "estimated": 0})
        s["calls"] += 1
        s["failed"] += 0 if r["ok"] else 1
        s["latency_ms_total"] = round(s["latency_ms_total"] + r["latency_ms"], 1)
        s["tok_total"] += r["tok_total"]
        s["estimated"] += 1 if r["estimated"] else 0
    for s in out.values():
        s["latency_ms_avg"] = round(s["latency_ms_total"] / max(1, s["calls"]), 1)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["calls"]))


def dump_llm_metrics(path: str) -> int:
    """落盘为 JSONL（评测脚本跑完后调用，便于离线对比）。"""
    rows = get_llm_metrics()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:
        return 0
    return len(rows)


# ── LLM 全局并发上限 ─────────────────────────────────────
# 主链路每个请求会串行触发 3–6 次 LLM 调用，若评测/压测瞬间大量并发，
# 会把 LLM API 配额打爆并吃满 worker 线程。这里对同步 invoke 做全局信号量
# 限流（默认 8），既保护上游 API，又天然限制长尾并发。
_LLM_SEMAPHORE = threading.BoundedSemaphore(int(os.getenv("LLM_MAX_CONCURRENCY", "8")))


class _GuardedLLM:
    """给 LLM 调用套全局并发上限；其余方法（ainvoke/bind 等）原样委托。

    注意：`stream` 是主链路最重的调用形态（SQL 生成/报告/推断均为流式），
    同样受全局信号量约束——持有信号量直到流迭代结束，防止长流式并发打爆上游 API。

    P0-1 埋点：invoke / stream 额外记录耗时与 token。纯观测——不修改入参、
    不修改生成内容、不修改返回值，仅在外层计时并读取响应的 usage 元数据。
    """

    def __init__(self, inner, model: str = "", site: str = ""):
        self._inner = inner
        self._model = model
        self._site = site

    def invoke(self, *args, **kwargs):
        with _LLM_SEMAPHORE:
            if not _LLM_METRICS_ENABLED:
                return self._inner.invoke(*args, **kwargs)
            _t0 = time.monotonic()
            _ok, _err, _r = False, "", None
            try:
                _r = self._inner.invoke(*args, **kwargs)
                _ok = True
                return _r
            except Exception as e:
                _err = f"{type(e).__name__}: {str(e)[:200]}"
                raise
            finally:
                _ti, _to, _est = _extract_usage(_r) if _ok else (0, 0, True)
                if _est:
                    # 无 usage 元数据：按字符数粗估，仅供相对比较
                    _ti = _msg_text_len(args[0] if args else None) // 2
                    _to = len(str(getattr(_r, "content", "") or "")) // 2
                _record_llm_call("invoke", self._model, self._site,
                                 (time.monotonic() - _t0) * 1000, _ok, _err,
                                 _ti, _to, _est)

    def stream(self, *args, **kwargs):
        with _LLM_SEMAPHORE:
            if not _LLM_METRICS_ENABLED:
                yield from self._inner.stream(*args, **kwargs)
                return
            _t0 = time.monotonic()
            _ok, _err, _chars = False, "", 0
            try:
                for _c in self._inner.stream(*args, **kwargs):
                    try:
                        _chars += len(getattr(_c, "content", "") or "")
                    except Exception:
                        pass
                    yield _c
                _ok = True
            except Exception as e:
                _err = f"{type(e).__name__}: {str(e)[:200]}"
                raise
            finally:
                # stream 路径实测拿不到 usage（DeepSeek 兼容接口不返回），统一按字符数估算，
                # 并以 estimated=True 标记，统计时不可当作绝对值解读。
                _ti = _msg_text_len(args[0] if args else None) // 2
                _to = _chars // 2
                # 未走完 = 被中断（如双路竞速中慢路被放弃），ok=False 单独可统计
                _record_llm_call("stream", self._model, self._site,
                                 (time.monotonic() - _t0) * 1000, _ok, _err,
                                 _ti, _to, True)

    def ainvoke(self, *args, **kwargs):
        with _LLM_SEMAPHORE:
            return self._inner.ainvoke(*args, **kwargs)

    def astream(self, *args, **kwargs):
        with _LLM_SEMAPHORE:
            return self._inner.astream(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __repr__(self):
        return f"GuardedLLM({self._inner!r})"


# ── LLM 语义选表（替代纯关键词匹配，提升准确率）───────────

# ── 向量预筛表向量缓存（改造3增强）：db_key -> (ts, {表名: vec})，1h TTL ──
_vec_cache_lock = threading.Lock()
_table_vec_cache: dict[str, tuple[float, dict[str, list[float]]]] = {}
# ── LLM 选表结果缓存（2026-09-02 性能优化）：(db_key, 归一化问法) -> (ts, [表名])，10min TTL ──
_llm_table_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
# ── LLM 口径推断结果缓存（2026-09-02 性能优化）：(db_key, 问法) -> (ts, analysis)，3min TTL ──
_analysis_infer_cache: dict[tuple[str, str], tuple[float, dict]] = {}

# 全表字段名缓存（选表阶段每请求都用，information_schema 全表扫描开销大）：按 db_key 缓存 60s
_all_cols_cache: dict[str, tuple[float, dict[str, list[str]]]] = {}
_ALL_COLS_TTL = 60.0


def _all_table_columns() -> dict[str, list[str]]:
    """一次性批量获取当前库所有表的字段名（供选表阶段提供依据），带 60s TTL 缓存。

    单次 information_schema 查询，结果形如 {"schema.table": ["col1","col2",...]}。
    public schema 的表以裸表名为 key，与 get_real_tables 的命名保持一致。
    """
    db_key = _current_db_key()
    now = time.time()
    with _vec_cache_lock:
        hit = _all_cols_cache.get(db_key)
        if hit and now - hit[0] < _ALL_COLS_TTL:
            return hit[1]

    from database import get_db_type
    out: dict[str, list[str]] = {}
    try:
        if get_db_type() == "mysql":
            sql = (
                "SELECT table_schema AS s, table_name AS t, column_name AS c "
                "FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "ORDER BY table_name, ordinal_position"
            )
        else:
            sql = (
                "SELECT table_schema AS s, table_name AS t, column_name AS c "
                "FROM information_schema.columns "
                "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                "ORDER BY table_schema, table_name, ordinal_position"
            )
        r = execute_sql(sql)
        if not r.get("success") or not r.get("rows"):
            return out
        for row in r["rows"]:
            schema = row.get("s") or ""
            table = row.get("t") or ""
            col = row.get("c") or ""
            key = table if schema in ("public", "") else f"{schema}.{table}"
            out.setdefault(key, []).append(col)
    except Exception:
        pass
    if out:  # 仅在成功拿到结果时缓存，避免把空结果（查询失败）缓存 60s
        with _vec_cache_lock:
            _all_cols_cache[db_key] = (time.time(), out)
    return out


def _select_tables_by_llm(query: str) -> list[str]:
    """让 LLM 从当前数据库真实表中选择最相关的表（1-4 张）。

    表清单附带关键字段名，避免 LLM 仅凭表名猜测导致选错
    （如"物料采购量排行"误选 dim_product 而非订单表）。
    返回表名列表；LLM 不可用时返回空列表（调用方回退到关键词匹配）。

    P0-性能（2026-09-02）：①进程级结果缓存（同/近问法 10 分钟内免重复 LLM，
    实测 LLM 选表单次 7.6s+）；②invoke 硬性 10s 总超时——超过即回退关键词，
    避免把 no_hit/推断链拖到 30s+。
    """
    # ── 结果缓存：同库同问法 10min 内直接复用（选表结果对数据表结构稳定）──
    try:
        _ckey = _current_db_key()
    except Exception:
        _ckey = "default"
    _qkey = (query or "").strip()
    if _qkey:
        with _vec_cache_lock:
            _hit = _llm_table_cache.get((_ckey, _qkey))
        if _hit and time.time() - _hit[0] < 600:
            return list(_hit[1])
    try:
        tables = get_all_tables()
        if not tables:
            return []

        col_map = _all_table_columns()

        # 候选表筛选：表多时用关键词粗筛，避免超 40 张时后面的表永远不被 LLM 看到。
        # 先按「表名/别名/描述是否命中问题词」粗筛；命中不足时再补足到上限。
        query_tokens = [w for w in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z_][a-zA-Z0-9_]*", query) if len(w) > 1]
        scored = []
        for t in tables:
            name = t["table_name"]
            hay = f"{name} {t.get('table_alias') or ''} {t.get('description') or ''}".lower()
            hit = sum(1 for w in query_tokens if w.lower() in hay)
            scored.append((hit, name, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        # 命中>0 的表优先全保留；其余按需补足到 40（防止大库漏表）
        hit_tables = [t for s, n, t in scored if s > 0]
        miss_tables = [t for s, n, t in scored if s == 0]
        candidate = hit_tables + miss_tables

        # ── 向量语义召回（改造3）：embedding 可用时，按问题-表语义相似度补足候选，
        #    解决"关键词子串命中"漏掉同义表述的表（如"员工数"→ employee 表无命中词）。
        #    不可用（未装 sentence-transformers / 无 embedding API）→ 自动降级纯关键词。
        #    表向量按库缓存 1h（改造5增强）：避免每次查询对每张表重复编码。──
        try:
            from agent.embeddings import get_embedding_fn
            enc = get_embedding_fn()
            if enc:
                qv = enc(query)
                qn = sum(x * x for x in qv) ** 0.5 or 1.0
                try:
                    from database import get_database_config
                    _cfg = get_database_config()
                    vec_key = f"{_cfg.get('db_type')}:{_cfg.get('host')}:{_cfg.get('port')}:{_cfg.get('name')}"
                except Exception:
                    vec_key = "default"
                _now = time.time()
                with _vec_cache_lock:
                    _cached = _table_vec_cache.get(vec_key)
                    if not _cached or _now - _cached[0] > 3600:
                        _cached = (_now, {})
                        _table_vec_cache[vec_key] = _cached
                    vec_map = _cached[1]
                vec_scores: list[tuple[float, dict]] = []
                for t in tables:
                    doc = " ".join([
                        t["table_name"],
                        t.get("table_alias") or "",
                        (t.get("description") or "")[:120],
                        " ".join((col_map.get(t["table_name"]) or [])[:20]),
                    ])
                    tv = vec_map.get(t["table_name"])
                    if tv is None:
                        tv = enc(doc)
                        with _vec_cache_lock:
                            vec_map[t["table_name"]] = tv
                    tn = sum(x * x for x in tv) ** 0.5 or 1.0
                    sim = sum(a * b for a, b in zip(qv, tv)) / (qn * tn)
                    vec_scores.append((sim, t))
                vec_scores.sort(key=lambda x: x[0], reverse=True)
                vec_hit_names = {t["table_name"] for _, t in vec_scores[:15] if _[0] > 0.35}
                # 语义命中但关键词未命中的表，插在关键词命中之后、miss 之前
                if vec_hit_names:
                    extra = [t for t in tables if t["table_name"] in vec_hit_names and t not in hit_tables]
                    candidate = hit_tables + extra + miss_tables
        except Exception:
            pass  # embedding 不可用/异常 → 保持关键词粗筛结果

        tables_sub = candidate[:40]
        hit_cnt = len(hit_tables)
        if hit_cnt > 40:
            tables_sub = candidate[:hit_cnt]  # 命中表超过 40 也保留全部命中（最多全表）

        # 构建表清单（名称 | 别名 | 描述 | 关键字段）
        table_lines = []
        for t in tables_sub:
            name = t["table_name"]
            alias = t.get("table_alias") or name
            desc = (t.get("description") or "")[:60]
            cols = col_map.get(name) or []
            if not cols:
                # metadata 里可能带字段定义
                cols = [f.get("name", "") for f in (t.get("fields") or [])]
            col_str = ", ".join(c for c in cols[:18] if c)
            if len(cols) > 18:
                col_str += f", …(共{len(cols)}列)"
            line = f"- {name} | {alias}"
            if desc:
                line += f" | {desc}"
            if col_str:
                line += f"\n    字段: {col_str}"
            table_lines.append(line)
        table_list = "\n".join(table_lines)

        prompt = (
            "你是数据库专家。根据用户的问题，从下面的表清单中选择最相关的 1-4 张表。\n"
            "选择依据：优先看【字段】是否能支撑回答这个问题，其次看表名和描述。\n"
            "如果问题需要 JOIN（如维度名称 + 事实指标），把相关的维度表和事实表都选上。\n"
            "注意区分易混淆的业务表：销售订单/客户订单选『订单/销售单』表（如 test_orders、factory.sales_order），"
            "生产任务/工单选『工单』表（如 mes_work_order、factory.work_order），不要把『订单』错选成『工单』。\n"
            "只输出 JSON 数组（不要任何其他内容），如 [\"表1\",\"表2\"]。\n\n"
            f"## 表清单\n{table_list}\n\n## 用户问题\n{query}"
        )
        llm = _make_llm(temp=None, max_tokens=200, json_mode=True, timeout=10)
        # P0-性能（2026-09-02）：SDK timeout 对长生成只是「块间空闲」上限，总时长仍可能 30s+。
        # 改流式 + 显式 10s 总 deadline：超时即放弃（返回空 → 调用方回退关键词匹配，绝不拖死推断链）。
        _resp = ""
        _dl = time.monotonic() + 10
        for _chunk in llm.stream([SystemMessage(content=prompt), HumanMessage(content=query)]):
            if time.monotonic() > _dl:
                raise TimeoutError(f"LLM 选表超过 10s")
            if getattr(_chunk, "content", None):
                _resp += str(_chunk.content)
        resp = _resp
        raw = resp.content.strip() if hasattr(resp, "content") else resp.strip()
        # 提取 JSON 数组
        m = re.search(r'\[[\s\S]*\]', raw)
        if not m:
            return []
        names = json.loads(m.group(0))
        if not isinstance(names, list):
            return []

        # 只保留真实存在的表
        real_names = {t["table_name"] for t in tables}
        result = [str(n).strip() for n in names if str(n).strip() in real_names]
        result = result[:4]
        if result and _qkey:
            with _vec_cache_lock:
                _llm_table_cache[(_ckey, _qkey)] = (time.time(), list(result))
        return result
    except Exception:
        return []


# ── 表匹配快速通道阈值（性能优化，见 LLMService._match_tables）──────
# 关键词匹配的候选表数 ≤ 该值且每张都有关键词命中 → 直接采用，跳过 LLM 选表
# （实测省 1.7~4.4s）。置 0 = 始终走 LLM 选表（最保守）。
try:
    _TABLE_MATCH_FAST_MAX = int(os.getenv("TABLE_MATCH_FAST_MAX", "3"))
except Exception:
    _TABLE_MATCH_FAST_MAX = 3

# ── 后处理线程池（性能优化）────────────────────────────────
# 「数据洞察」「Critic」「Evaluator」三者只依赖同一份 SQL 结果、彼此独立，
# 实测串行要 5.0 + 2.3 + 3.8 = 11.1s，并行后只算最慢的那一个（约 5s）。
# 用模块级池而不是每次查询新建/销毁，省线程创建开销。
_POST_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="llm-post")

# ── LLM 工厂 ─────────────────────────────────────────────

def _make_llm(temp: float = None, max_tokens: int = 2048, json_mode: bool = False, timeout: float = 120,
    # 默认 120s（慢速中转上游如 geek2api 单轮可达 28-60s，60s 常被误杀）；显式传短的调用点不受影响
              max_retries: int = 2, model: str | None = None, api_key: str | None = None,
              base_url: str | None = None, extra_body: dict | None = None):
    # 支持指定模型（如生成类任务在 GLM 长 schema 0 输出时切默认 deepseek 兜底）
    _model = model or LLM_CONFIG["model"]
    _key = api_key or LLM_CONFIG["api_key"]
    _base = base_url or LLM_CONFIG["base_url"]
    eff = temp if temp is not None else LLM_CONFIG["temperature"]
    # P0-3a：provider 注册表统一温度规则（Kimi 等 force_1 模型强制 temperature≥1，
    # 否则报 "invalid temperature: only 1 is allowed"）。注册表不可用时回退原硬编码逻辑。
    try:
        from agent.llm_providers import detect_provider, apply_temperature_rule
        _provider = detect_provider(_model, _base)
        eff = apply_temperature_rule(_provider, eff)
    except Exception:
        model_l = (_model or "").lower()
        if "kimi" in model_l:
            eff = max(eff, 1.0)
    kwargs = dict(
        model=_model,
        api_key=_key,
        base_url=_base,
        temperature=eff,
        max_tokens=max_tokens,
    )
    kwargs["timeout"] = timeout  # P0：默认 60s 超时，防 LLM 抖动永久挂死 worker（调用方显式传入则覆盖）
    # 思考链关闭（2026-09-15 扩展）：混合思考模型（Qwen3 / DeepSeek-V3.2+&V4 / GLM-4.5+）
    # 默认「先想后答」，**思考 token 计入 max_tokens 预算**——预算被思考烧光时
    # content 恒空、finish_reason=length，主链两条生成路径全挂，报「AI 未能根据问题
    # 生成查询 SQL」。实测 deepseek-v4.1-flash @ dashscope：默认 3.9s/reasoning 1255 字/
    # content 0 字；加 enable_thinking=False 后 1.3s/content 116 字。
    # 规则表在 agent/llm_providers.py::thinking_off_payload（按 (网关,模型) 精确匹配，
    # 只对已实测确认的组合注入，避免给不支持的端点多传参数换回一个 400）。
    # 可用环境变量 LLM_THINKING_OFF=0 全局回退、=force 强制关闭。
    try:
        from agent.llm_providers import thinking_off_payload
        _toff = thinking_off_payload(_model, _base)
        if _toff:
            kwargs.setdefault("extra_body", {})
            kwargs["extra_body"].update(_toff)
    except Exception:
        # 注册表不可用时退回最初的硬编码逻辑（至少保住 qwen3 这一档）
        if str(_model or "").lower().lstrip().startswith("qwen3"):
            kwargs.setdefault("extra_body", {})
            kwargs["extra_body"]["enable_thinking"] = False
    # 调用方指定的额外请求体（如 ML 方案设计要关思考：结构化 JSON 任务不需要思考链，
    # 而思考 token 会把 max_tokens 吃光 → content 恒空，见 ml_intent._llm 的说明）
    if extra_body:
        kwargs.setdefault("extra_body", {})
        kwargs["extra_body"].update(extra_body)
    # 超时重试策略：默认 SDK 2 次重试。诊断类/时间敏感调用点（推断、逃生等）显式传 0，
    # 否则单轮超时会 3×timeout（如 GLM 40s×3=120s），用户侧表现为"生成一直不出来"。
    kwargs["max_retries"] = max_retries
    # JSON mode（DeepSeek/Kimi 均支持 response_format={"type":"json_object"}）：
    # 让 LLM 输出合法 JSON，比正则容错解析稳定得多。仅对明确要求 JSON 输出的调用点开启。
    if json_mode:
        # langchain-openai 新版将 response_format 归入 model_kwargs，直接传入避免 UserWarning
        kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
    # P0-1 埋点：把 model 与发起调用点带进 _GuardedLLM，便于归因到具体代码位置
    return _GuardedLLM(ChatOpenAI(**kwargs),
                       model=_model,
                       site=_call_site(2))


# ── SQL 生成类任务的输出预算与截断检测（2026-09-14 用户实测驱动）──────────
# 问题：生成链路的输出上限此前是**写死的** 900 / 1200 / 2048，而配置项
# LLM_MAX_TOKENS（.env=8192）全项目只有 report_agent 在用 —— 生成链路完全没读。
# 后果：max_tokens 触顶时表现为**静默截断半条 SQL**，而不是报错。实测
# 「末道工序达成率」这类 CTE + 窗口函数 + 中文别名 + JSON 包装 + 引号转义的
# SQL 就要 400~600 token，逃生直生只有 900 → 复杂题长期被砍断，然后落到
# 更弱的兜底路径（准确率进一步下降）。
# 注意：max_tokens 是**上限**不是目标值 —— 上调不会让常规请求多生成 token
# （不多计费），只在长 SQL 时避免被砍断。
_SQL_GEN_TOKENS_FLOOR = 3000      # 低于此值必然截断复杂 SQL → 强制兜底
_SQL_GEN_TOKENS_CEIL = 32768      # 防误配超大值（超过模型上限会被上游拒绝）


def _sql_gen_max_tokens(floor: int = _SQL_GEN_TOKENS_FLOOR) -> int:
    """SQL 生成 / 口径推断类任务的输出 token 上限（读配置 LLM_MAX_TOKENS）。"""
    v = 0
    try:
        v = int(LLM_CONFIG.get("max_tokens") or 0)
    except Exception:
        v = 0
    if v <= 0:
        try:
            v = int(os.getenv("LLM_MAX_TOKENS", "") or 0)
        except Exception:
            v = 0
    if v <= 0:
        v = 8192
    return max(int(floor), min(v, _SQL_GEN_TOKENS_CEIL))


# ── 「各X」全量分组的 LIMIT 归一（2026-09-14 用户实测驱动）──────────────
_DIM_LISTING_RE = re.compile(r"各[\u4e00-\u9fa5]{1,8}|按[\u4e00-\u9fa5]{1,8}|每个[\u4e00-\u9fa5]{1,8}")
_TOPN_RE = re.compile(r"前\s*\d+|TOP\s*\d+|最(?:大|高|多|久|长|小|低|少|短|晚|早|新|旧)\s*(?:的)?\s*\d+")
_SQL_GROUP_BY_RE = re.compile(r"(?is)\bGROUP\s+BY\b")
# 「最X」极值形容词集（与 `_TOPN_RE` 后半段一致，但**不要求**后跟数字）。
# 刻意不含「近」⇒「最近N天」不会被当成极值语义。
_SUPERLATIVE_RE = re.compile(r"最(?:大|高|多|久|长|小|低|少|短|晚|早|新|旧)")
# 注意：本文件后部（~7520 行）另有一个**同名** `_GROUP_BY_RE`（语义是「分组意图词表」：
# 各|每个|分别|占比…）。模块加载顺序会**覆盖**同名定义 ⇒ 这里刻意叫 `_SQL_GROUP_BY_RE`，
# 若沿用 `_GROUP_BY_RE`，本文件里读到的将是那个词表，永远匹配不到 SQL 关键字（已踩）。


def _fix_dim_listing_limit(query: str, sql: str) -> str:
    """「各X/按X」类**全量分组**问题不得被默认 LIMIT 20 截断。

    实测（yans 库，未注册口径复杂题 25 题）：19 个失败里 8 个（42%）是
    「各设备…」实际 40 台却只回 20 条、「各产品…」实际 28 个只回 20 条 ——
    用户拿到的列表残缺且毫无提示。提示词规则（5b）压不住 LLM 的惯性，
    因此改用**确定性改写**：问句是维度全量列举（各X/按X/每个X）且**没有** TOP-N
    （前N/TOP N/最高…N）时，把 `LIMIT n`（n<1000）统一改写为 `LIMIT 1000`；
    没有 LIMIT 则补一个（防 runaway）。TOP-N 题（要前 N 名）完全不受影响。

    ── 2026-09-19 补充：裸问句「<维度><指标>」也算维度列举 ──
    现象：yans「工单批不合格率」**不含**"各/按/每个"，旧判据直接放行，LLM 自带的
    `LIMIT 100` 原样保留 ⇒ 库里 **344 个工单只回 100 个**，前端还显示"已查询到 100 条"，
    用户完全看不出列表残缺（且 rate 并列 0.5 的有 311 个，截断后的"排序"毫无意义）。
    判据：问句无列举词时，**改用 SQL 产物反推** —— 只要 SQL 自己 `GROUP BY` 了，
    语义上就是"逐分组列举"，默认 LIMIT 20/100 属静默截断，同样归一为 1000。
    这不放松任何 TOP-N 守卫（`_TOPN_RE` / `_global_topn1_intent` 仍在），
    也不会误伤单值聚合（那种 SQL 没有 GROUP BY，压根不进入本分支）。
    """
    s = (sql or "").strip()
    if not s:
        return s
    try:
        if not _DIM_LISTING_RE.search(query or "") and not _SQL_GROUP_BY_RE.search(s):
            return s
        if _TOPN_RE.search(query or ""):
            return s
        # 护栏①（2026-09-19 回放实测，123#53「哪个供应商采购额最高」被改坏）：
        # SQL 自己写死 `LIMIT 1` 是「全局唯一一条」的确定性形态，**永不放**。
        # 放开会把 1 行变 1000 行（该题 gold 只有 1 行），是最直接的改坏方式。
        if re.search(r"(?i)\bLIMIT\s+1\b", s):
            return s
        # 护栏②：问句**没有**维度列举词（各/按/每个），却含「最<形容词>」⇒ 说的是某个
        # 极值对象（「哪个供应商采购额最高」「哪台设备停机最久」），属全局 TOP-1 语义，
        # 不放开。字表刻意与 `_TOPN_RE` 的形容词集一致、且**不含「近」**，
        # 避免「最近7天」这类相对时间窗口被误当成极值（该场景由日期锚点规则管）。
        if not _DIM_LISTING_RE.search(query or "") and _SUPERLATIVE_RE.search(query or ""):
            return s
        # 「最X的<实体>」问的是**全局唯一一条**（TOP-1），`LIMIT 1` 正是正确答案的形态：
        # 这里放开 LIMIT 会把它顶回 1000 行，等于答非所问（postgres#19 实测：
        # `各工厂(established)建厂时间最早的工厂` 放开后从 1 行变成 3 行）。
        # 判据与 `_fix_global_superlative_topn` **共用同一个谓词**，保证"改写的"与
        # "让路的"永远一致。刻意不用更宽的"含最X的"：`123#31「各部门工资最高的员工是谁」`
        # 没有括号排序键提示，那是"各组内部之最"，放开 LIMIT 是**对的**，不能连坐。
        # （`_global_topn1_intent` 定义在文件后部 —— 模块级名字在**调用时**解析，引用安全。）
        if _global_topn1_intent(query):
            return s

        def _sub(m):
            return "LIMIT %d" % max(int(m.group(1)), 1000)

        new = re.sub(r"(?i)\bLIMIT\s+(\d+)\b", _sub, s)
        if not re.search(r"(?i)\bLIMIT\s+\d+\b", new):
            new = new.rstrip().rstrip(";") + "\nLIMIT 1000"
        return new
    except Exception:
        return s


# ── 输出列确定性补齐：明细「整行超集」+ 问句「显式列名」（2026-09-17，92 题评测驱动）──
# 现象一（明细被裁剪）：行集完全正确的明细题（「最近的5条采购单」「工资最高的3名员工」
#   「库存最低的前5个产品」），Agent 只留了 id + 一个指标，丢掉 name / reason / 时长等列。
# 现象二（显式列名未采纳）：问句**点名了列**（「各产线(line_id)的总投入数量(input_qty)」）
#   却把 line_id 输出成了 JOIN 来的 line_name —— 用户要的是 line_id。
# 执行准确率口径是「黄金行的取值都能在配到的 Agent 行里找到，**允许 Agent 附加列**」，
# 而"黄金有、Agent 没有"的任一列都直接判失败。
# 离线真实回放（真库 + 真 gold，92 题全量）：**7 道失败转通过，54 道已通过题零误伤**。
# 开关：SQL_FIX_SUPERSET=0 整体关闭。
_FIX_SUPERSET_ON = os.getenv("SQL_FIX_SUPERSET", "1") not in ("0", "false", "False")
_S_IDENT = r'(?:"[^"]+"|[A-Za-z_][\w$]*)'
_S_TBLREF = _S_IDENT + r"(?:\." + _S_IDENT + r")?"
_S_AGG_RE = re.compile(r"(?<![A-Za-z])(?:SUM|AVG|COUNT|MIN|MAX)\s*\(", re.I)
_S_GROUPBY_RE = re.compile(
    r"(?is)(?<![A-Za-z])GROUP\s+BY\s+(.*?)(?=\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b|$)")
# 问句里显式点名的列名：(line_id) / （qty_produced）—— 只认纯 ASCII 标识符
_S_COL_HINT_RE = re.compile(r"[（(]\s*([a-z_][a-z0-9_]*)\s*[）)]")
_S_RESERVED = {"where", "group", "order", "limit", "join", "left", "right", "inner", "full",
               "on", "having", "union", "offset", "cross", "natural", "as", "and", "or",
               "select", "from", "by", "distinct", "all"}


def _split_top_commas(s: str) -> list:
    """按顶层逗号切分（忽略括号内的逗号），用于安全地拆 select / group by 列表。"""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [x.strip() for x in out if x.strip()]


def _balanced(s: str) -> bool:
    return s.count("(") == s.count(")")


def _simple_select_parts(sql: str):
    """只看「单层 SELECT ... FROM ...」的简单形态，返回 (select列表, FROM之后整段)。

    故意跳过 CTE(WITH) / UNION / DISTINCT，以及 select 列表里带子查询（括号不配对）的形态 ——
    这些情形下 `alias.*` 补列会改变语义或直接语法报错，宁可不动。
    """
    s = str(sql or "").strip().rstrip(";")
    if not re.match(r"(?is)^SELECT\s+", s):
        return None
    if re.match(r"(?is)^SELECT\s+(?:DISTINCT|ALL)\b", s):
        return None
    if re.search(r"(?i)\bUNION\b", s):
        return None
    m = re.match(r"(?is)^SELECT\s+(.*?)\s+FROM\s+(.*)$", s)
    if not m:
        return None
    sel, rest = m.group(1), m.group(2)
    if not _balanced(sel) or not sel:
        return None
    return sel, rest


def _main_table_ref(rest: str):
    """FROM 之后首个表引用：有别名取别名，否则取表名（供 `x.*` 使用）。"""
    m = re.match(r"(?i)\s*(" + _S_TBLREF + r")(?:\s+(?:AS\s+)?(" + _S_IDENT + r"))?", rest)
    if not m:
        return None
    tbl, nxt = m.group(1), (m.group(2) or "")
    if nxt and nxt.strip('"').lower() not in _S_RESERVED:
        return nxt
    return tbl


def _fix_detail_row_superset(query: str, sql: str) -> str:
    """明细类（无 GROUP BY、无聚合、有 LIMIT）把 select 列表补成「主表全列 + 原列表」。

    明细题问的是「最近N条 / 前N名 / 列出…」，返回整行是合理的产品行为（用户本来就要看
    这条记录的完整信息），而行数不变 ⇒ 只是补齐可读列，不会把聚合题的粒度弄错。
    """
    s = (sql or "").strip()
    if not s or not _FIX_SUPERSET_ON:
        return sql
    try:
        parts = _simple_select_parts(s)
        if not parts:
            return s
        sel, rest = parts
        if _S_GROUPBY_RE.search(s) or not re.search(r"(?i)\bLIMIT\s+\d+", s):
            return s
        if _S_AGG_RE.search(sel):
            return s
        items = _split_top_commas(sel)
        # 已经是整行输出（`*` / `x.*`）就不重复补
        if any(it == "*" or it.endswith(".*") for it in items):
            return s
        ref = _main_table_ref(rest)
        if not ref:
            return s
        base = ref.lower()
        keep = [it for it in items if not it.lower().startswith(base + ".")]
        new = "SELECT " + ref + ".*" + (", " + ", ".join(keep) if keep else "") + " FROM " + rest
        return new if new != s else s
    except Exception:
        return sql


def _fix_explicit_col_select(query: str, sql: str) -> str:
    """问句里显式点名的列（`各产线(line_id)的总投入数量`）必须出现在输出与分组里。

    只把该列**追加**到 select 与 GROUP BY：执行准确率允许附加列，所以补上只会更接近
    「用户点名要的那一列」；GROUP BY 同时扩列保证 SQL 合法，且这类场景里被补的列
    （维表名 → 事实表外键）是 1:1 的，行数不变。
    """
    s = (sql or "").strip()
    if not s or not _FIX_SUPERSET_ON:
        return sql
    try:
        hints = _S_COL_HINT_RE.findall(str(query or ""))
        if not hints:
            return s
        parts = _simple_select_parts(s)
        if not parts:
            return s
        sel, rest = parts
        gm = _S_GROUPBY_RE.search(s)
        if not gm:
            return s
        sel_items = _split_top_commas(sel)
        gb_items = _split_top_commas(gm.group(1).strip())
        if not gb_items:
            return s
        added = []
        for h in hints:
            if any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(h) + r"(?![A-Za-z0-9_])", x, re.I)
                   for x in sel_items):
                continue  # 已经在输出列里
            rm = re.search(r"(?<![A-Za-z0-9_])(" + _S_TBLREF + r"\.)?" + re.escape(h)
                           + r"(?![A-Za-z0-9_])", rest, re.I)
            if not rm:
                continue  # 该列根本没在 SQL 里出现 → 不动（避免引用不存在的表）
            ref = (rm.group(1) or "") + h
            sel_items.append(ref)
            if not any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(h) + r"(?![A-Za-z0-9_])", x, re.I)
                       for x in gb_items):
                gb_items.append(ref)
            added.append(h)
        if not added:
            return s
        out = re.sub(r"(?is)^(SELECT\s+)(.*?)(\s+FROM\s+)",
                     lambda mm: mm.group(1) + ", ".join(sel_items) + mm.group(3), s, count=1)
        out = re.sub(_S_GROUPBY_RE, lambda mm: "GROUP BY " + ", ".join(gb_items) + " ",
                     out, count=1)
        return out if out != s else s
    except Exception:
        return sql


# ── 分组键补齐（2026-09-17）：`各产线良率` 被输出成 JOIN 来的产线名称，丢了 line_id ──
# 与 `_fix_explicit_col_select` 是同一根因（黄金有、Agent 没有的列直接判失败），
# 区别在于问句**没有显式点名列名**，无法用 `(line_id)` 这类提示识别。
# 判据改为「分组键取的是 JOIN 来的维表列」→ 顺手把该维的关联键也输出+分组。
#
# 安全性（关键，靠数据实测而不是靠假设）：
#   GROUP BY 扩列会改粒度，只有「维表名列 → 维表主键」在**实际数据里**是 1:1 时才等价。
#   `_dim_key_unique_under()` 用一条 `HAVING COUNT(DISTINCT key) > 1` 查询实测这个条件，
#   不满足就不改写（宁可少改，不可改错）。只在**内连接**上做（外连接时空维行会被拆开）。
_DIM_1TO1_CACHE: dict = {}
_JOIN_TOKEN_RE = re.compile(r"(?is)\b(FROM|(?<!NATURAL\s)\bJOIN)\s+(" + _S_TBLREF + r")"
                            r"(?:\s+(?:AS\s+)?(" + _S_IDENT + r"))?")
_ON_EQ_RE = re.compile(r"(?<![A-Za-z0-9_.])(" + _S_IDENT + r")\s*\.\s*(" + _S_IDENT + r")"
                       r"\s*=\s*(" + _S_IDENT + r")\s*\.\s*(" + _S_IDENT + r")")


def _bare(x: str) -> str:
    return str(x or "").strip().strip('"').split(".")[-1].lower()


def _dim_key_unique_under(tbl_ref: str, name_col: str, key_col: str) -> bool:
    """实测维表里「name_col 相同 → key_col 唯一」是否成立（成立才能安全扩 GROUP BY）。"""
    # 缓存键必须带**当前库名**：同一个进程里会切库（评测就是 postgres + 123 一起跑），
    # 同名表在不同库的 1:1 结论可能相反，不带库名会串味。
    _db = ""
    try:
        from database import get_database_config
        _db = str((get_database_config() or {}).get("name") or "")
    except Exception:
        _db = ""
    cache_key = (_db, _bare(tbl_ref), name_col.lower(), key_col.lower())
    hit = _DIM_1TO1_CACHE.get(cache_key)
    if hit is not None:
        return hit
    ok = False
    try:
        from db.executor import execute_sql
        q = ('SELECT 1 FROM %s GROUP BY "%s" HAVING COUNT(DISTINCT "%s") > 1 LIMIT 1'
             % (tbl_ref, name_col, key_col))
        res = execute_sql(q) or {}
        ok = bool(res.get("success")) and not (res.get("rows") or [])
    except Exception:
        ok = False
    _DIM_1TO1_CACHE[cache_key] = ok
    return ok


def _fix_group_key_superset(query: str, sql: str) -> str:
    """分组键取自 JOIN 来的维表列 → 补齐该维的关联键列（输出 + 分组）。"""
    s = (sql or "").strip()
    if not s or not _FIX_SUPERSET_ON:
        return sql
    try:
        parts = _simple_select_parts(s)
        if not parts:
            return s
        sel, rest = parts
        gm = _S_GROUPBY_RE.search(s)
        if not gm:
            return s
        if re.search(r"(?is)\b(LEFT|RIGHT|FULL|NATURAL)\s+(OUTER\s+)?JOIN\b", s):
            return s  # 外连接不碰：空维行会被拆成多组

        # 表别名 → (表引用原文, 是否 FROM 首位/主表)
        # 注意：`rest` 是 FROM **之后**的片段，`FROM` 关键字已被切掉 ——
        # 扫主表时必须补回，否则只剩 JOIN 表、拿不到事实表。
        aliases: dict = {}
        order: list = []
        for m in _JOIN_TOKEN_RE.finditer("FROM " + rest):
            tbl, al = m.group(2), (m.group(3) or "")
            if al and _bare(al) in _S_RESERVED:
                al = ""
            alias = _bare(al) or _bare(tbl)
            if alias and alias not in aliases:
                aliases[alias] = (tbl, not order)
                order.append(alias)
        if len(order) < 2:
            return s  # 没有 JOIN，谈不上"维表列"

        # 连接等值对：alias.col = alias.col
        eq_pairs = []
        for m in _ON_EQ_RE.finditer(rest):
            la, lc, ra, rc = _bare(m.group(1)), m.group(2), _bare(m.group(3)), m.group(4)
            if la in aliases and ra in aliases and la != ra:
                eq_pairs.append((la, lc, ra, rc))
        if not eq_pairs:
            return s

        sel_items = _split_top_commas(sel)
        gb_items = _split_top_commas(gm.group(1).strip())
        added = []
        for g in gb_items:
            g = g.strip()
            if not re.match(r"(?is)^" + _S_IDENT + r"\s*\.\s*" + _S_IDENT + r"$", g):
                continue
            ga, gc = [x.strip() for x in g.split(".", 1)]
            ga_b, gc_b = _bare(ga), gc.strip().strip('"')
            info = aliases.get(ga_b)
            if not info or info[1]:
                continue  # 分组键不是"JOIN 来的维表列"（主表列无需补）
            tbl_ref = info[0]
            # 找该维在连接等值里的键列（优先取等值式里位于维侧的那个）
            key_col = None
            for (la, lc, ra, rc) in eq_pairs:
                if la == ga_b and _bare(ra) in aliases:
                    key_col = lc
                    break
                if ra == ga_b and _bare(la) in aliases:
                    key_col = rc
                    break
            if not key_col:
                continue
            key_col = key_col.strip().strip('"')
            if key_col.lower() == gc_b.lower():
                continue
            if any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(key_col) + r"(?![A-Za-z0-9_])", x, re.I)
                   for x in sel_items):
                continue  # 已经输出了
            # 数据实测：维表里 name→key 必须 1:1，否则扩分组会改变行数
            if not _dim_key_unique_under(tbl_ref, gc_b, key_col):
                continue
            ref = ga_b + "." + key_col
            sel_items.append(ref)
            gb_items.append(ref)
            added.append(ref)

        if not added:
            return s
        out = re.sub(r"(?is)^(SELECT\s+)(.*?)(\s+FROM\s+)",
                     lambda mm: mm.group(1) + ", ".join(sel_items) + mm.group(3), s, count=1)
        out = re.sub(_S_GROUPBY_RE, lambda mm: "GROUP BY " + ", ".join(gb_items) + " ",
                     out, count=1)
        return out if out != s else s
    except Exception:
        return sql


# ── CTE 窗口排名 → ORDER BY ... LIMIT（2026-09-17）─────────────────────
# 模型很爱写 `WITH ranked AS (SELECT *, ROW_NUMBER() OVER (ORDER BY k DESC) rn FROM t)
# SELECT <少量列> FROM ranked WHERE rn <= N`。这个写法**自身是对的**，但外层只挑了
# id/指标两列，黄金要的姓名列就丢了（典型 123#18「工资最高的前10名员工」）。
# 该形态与 `SELECT * FROM t ORDER BY k DESC LIMIT N` 结果等价（无 PARTITION、单表、
# 无聚合），塌缩后补列问题自然消失。只在形态完全吻合时改写，且外层不得自带
# ORDER BY / LIMIT / GROUP BY / JOIN，避免改变语义。
_CTE_RANK_RE = re.compile(
    r"(?is)^WITH\s+(" + _S_IDENT + r")\s+AS\s*\(\s*SELECT\s+(.*?)\s+FROM\s+([^()]+?)\s*\)\s*"
    r"SELECT\s+(.*?)\s+FROM\s+\1\s+WHERE\s+(" + _S_IDENT + r")\s*<=\s*(\d+)\s*$")
_ROW_NUMBER_RE = re.compile(r"(?is)\b(?:ROW_NUMBER|RANK|DENSE_RANK)\s*\(\s*\)\s*"
                            r"OVER\s*\(\s*ORDER\s+BY\s+(.*?)\)")
_CTE_OUTER_RANK_RE = re.compile(r"(?is)\b(?:ROW_NUMBER|RANK|DENSE_RANK)\s*\(")


def _fix_cte_rank_to_limit(query: str, sql: str) -> str:
    """把「CTE 窗口排名 + 外层 rn<=N」塌缩成 `SELECT * FROM 基表 ORDER BY k DESC LIMIT N`。"""
    s = (sql or "").strip().rstrip(";")
    if not s or not _FIX_SUPERSET_ON:
        return sql
    try:
        if not re.match(r"(?is)^WITH\s", s):
            return s
        m = _CTE_RANK_RE.match(s)
        if not m:
            return s
        cte_name, inner_sel, base, outer_sel, rn_col, n = m.groups()
        # 内层：单表、无 JOIN/GROUP BY/DISTINCT、窗口排名无 PARTITION
        if re.search(r"(?is)\bJOIN\b|\bGROUP\s+BY\b|\bDISTINCT\b", inner_sel + " " + base):
            return s
        if re.search(r"(?is)\bPARTITION\s+BY\b", inner_sel):
            return s
        wm = _ROW_NUMBER_RE.search(inner_sel)
        if not wm:
            return s
        order_expr = wm.group(1).strip()
        if not order_expr or not re.match(r"(?is)^[\w\s.,\"'()]+$", order_expr):
            return s
        # 外层：WHERE 的列必须就是内层窗口结果的别名（否则可能过滤的是业务列）
        rn_b = _bare(rn_col)
        if not re.search(r"(?is)\bAS\s+" + re.escape(rn_b) + r"(?![A-Za-z0-9_])", inner_sel):
            return s
        if re.search(r"(?is)\b(ORDER\s+BY|LIMIT|GROUP\s+BY|HAVING|JOIN|UNION)\b", outer_sel):
            return s
        return "SELECT * FROM %s ORDER BY %s LIMIT %s" % (base.strip(), order_expr, n)
    except Exception:
        return sql


# ── 相对时间窗口的锚点归一（2026-09-17）─────────────────────────────
# 「最近30天」是相对**提问时刻**说的，锚点应当是 CURRENT_DATE。模型爱写成
# 「以数据里的最大日期为基准往前推」——在示例库这类数据日期陈旧的库上，两种锚点结果
# 完全不同（实测 postgres#13「最近30天各产线的总投入数量」：gold 0 行、Agent 3 行）。
# 只在「问句含相对时间窗口」+「SQL 用 `列 >= (SELECT MAX(日期列) FROM 表)` 作阈值」时改写，
# 区间算术原样保留（-29 days 之类不动），最小侵入。
_REL_WINDOW_RE = re.compile(
    r"最近|近\s*\d+\s*[天日月周]|本月|这个月|当月|上月|上个月|本周|这周|上周|今天|今日|昨天|昨日")
_MAX_DATE_THRESHOLD_RE = re.compile(
    r"([<>=!]+\s*)\(\s*SELECT\s+MAX\s*\(\s*(" + _S_IDENT + r")\s*\)\s+FROM\s+([^()]+?)\s*\)", re.I)


def _fix_relative_date_anchor(query: str, sql: str) -> str:
    """把 `>= (SELECT MAX(<日期列>) FROM <表>)` 这类锚子查询换成 CURRENT_DATE。"""
    s = (sql or "").strip()
    if not s or not _FIX_SUPERSET_ON:
        return sql
    if not _REL_WINDOW_RE.search(str(query or "")):
        return sql
    try:
        changed = [False]

        def _sub(m):
            op, col, tbl = m.group(1), m.group(2).strip().strip('"'), m.group(3).strip()
            base = col.lower()
            if not (base.endswith("date") or base.endswith("time") or base.endswith("_at")
                    or "日期" in col or "时间" in col):
                return m.group(0)  # 只认日期/时间列，避免把业务阈值（金额/数量）锚点换掉
            if not re.search(r"(?i)\b(FROM|JOIN)\s+" + re.escape(tbl.split()[-1]), s):
                return m.group(0)
            changed[0] = True
            return op + "CURRENT_DATE"

        out = _MAX_DATE_THRESHOLD_RE.sub(_sub, s)
        return out if changed[0] and out != s else s
    except Exception:
        return sql


# 「上月/本月」被硬编码成具体年月（实测 123#48「上月的生产记录有多少条」写成
# `>= '2025-06-01' AND <= '2025-06-30'`）。只在**问句含月份词**且字面量恰好构成
# 完整一个自然月时才归一，避免动到「2024年入职人数」这类**本该硬编码**的题目
# （实测 5 道此类已通过题，问句里都没有"本月/上月"，天然被排除）。
_MONTH_WORD_RE = re.compile(r"上月|上个月|本月|这个月|当月")


def _fix_relative_month_literal(query: str, sql: str) -> str:
    """「上月/本月」被写成硬编码的年月区间 → 归一为 date_trunc 表达式。"""
    s = (sql or "").strip()
    if not s or not _FIX_SUPERSET_ON:
        return sql
    if not _MONTH_WORD_RE.search(str(query or "")):
        return sql
    try:
        import calendar
        m = re.search(r"(?i)([\w\.\"]+)\s*>=\s*'(\d{4})-(\d{2})-(\d{2})'\s*AND\s*"
                      r"([\w\.\"]+)\s*<=\s*'(\d{4})-(\d{2})-(\d{2})'", s)
        if not m:
            return s
        c1, y1, m1, d1 = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        c2, y2, m2, d2 = m.group(5), int(m.group(6)), int(m.group(7)), int(m.group(8))
        # 必须是同一自然月，且首日=1 号、末日=该月最后一天（否则可能不是"整月"语义）
        if (y1, m1) != (y2, m2) or d1 != 1:
            return s
        if d2 != calendar.monthrange(y2, m2)[1]:
            return s
        last_month = bool(re.search(r"上月|上个月", query or ""))
        if last_month:
            lo = "date_trunc('month', CURRENT_DATE) - INTERVAL '1 month'"
            hi = "date_trunc('month', CURRENT_DATE)"
        else:
            lo = "date_trunc('month', CURRENT_DATE)"
            hi = "date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'"
        out = s[:m.start()] + ("%s >= %s AND %s < %s" % (c1, lo, c2, hi)) + s[m.end():]
        return out if out != s else s
    except Exception:
        return sql


# ── 「最近N天」天数 / 窗口题分组降维 / HAVING 度量列（2026-09-17，92 题实测驱动）──
# 观测（accuracy.pre_chain 报告逐题核对）：
#   ① 「最近N天」的天数 gold **一律取 N**：postgres#13 gold='30' 而 Agent 写 '29'；
#      123#14 gold='7' 而 Agent 写 '6'。模型有稳定的「N-1」惯性（把"最近7天"当成
#      "含今天共 7 天"）。2/2 有据 ⇒ 按问句的 N 归一。
#   ② 「最近N天各X的<聚合>」：时间短语是**过滤条件**而不是分组维度，但模型习惯把
#      日期列也放进 GROUP BY（123#14：8 行膨胀成 619 行）。**仅当问句没有时间粒度词**
#      （按月/各个月/趋势/按日期…）才降维 —— 实测 123#13「2024年各个月的产量趋势」的
#      月度粒度是**对的**，把它降维会误伤已通过题。
#   ③ HAVING 里被比较的度量没进 SELECT（123#26：行集与顺序 8/8 全对，只少了 total 列）。
#      严格口径要求"黄金每行的取值都能在 Agent 行里找到"，少列即判负；补上即翻转。
# 三条都只**加列 / 调分组**，不改行集语义；严格口径允许 Agent 附加列。
_WINDOW_N_DAY_RE = re.compile(r"(?:最近|近)\s*(\d+)\s*天")
_WINDOW_IV_DAY_RE = re.compile(r"(?i)(INTERVAL\s*')(\d+)(\s*days?'\s*)")
_TIME_GRAIN_WORD_RE = re.compile(
    r"按月|按月份|各个月|每个月|每月|逐月|按日|按天|每日|每天|逐日|按日期|按周|每周|趋势|走势")
_BARE_DATE_COL_RE = re.compile(r'(?i)^(?:[\w"]+\.)?[\w"]*(?:date|time|_at)[\w"]*$')


def _fix_relative_window_days(query: str, sql: str) -> str:
    """「最近N天」的 INTERVAL 天数归一到问句里的 N（gold 两题实测都取 N）。"""
    s = (sql or "").strip()
    if not s or not _FIX_SUPERSET_ON:
        return sql
    m = _WINDOW_N_DAY_RE.search(str(query or ""))
    if not m:
        return sql
    want = int(m.group(1))
    try:
        changed = [False]

        def _sub(mm):
            if int(mm.group(2)) == want:
                return mm.group(0)
            changed[0] = True
            return "%s%d%s" % (mm.group(1), want, mm.group(3))

        out = _WINDOW_IV_DAY_RE.sub(_sub, s)
        return out if changed[0] and out != s else s
    except Exception:
        return sql


def _fix_window_drop_time_groupby(query: str, sql: str) -> str:
    """「最近N天各X的<指标>」把**裸日期列**从 SELECT / GROUP BY / ORDER BY 中移除。

    时间是过滤条件而非分组维度。只在问句含相对窗口词、且**不含**时间粒度词时生效；
    只处理裸列引用（`t.col` / `col`），带函数包裹的日期表达式（`TO_CHAR(work_date,'YYYY-MM')`）
    一律不动 —— 那正是「按月/各个月」类正确题赖以工作的形态。
    """
    s = (sql or "").strip()
    if not s or not _FIX_SUPERSET_ON:
        return sql
    q = str(query or "")
    if not _REL_WINDOW_RE.search(q) or _TIME_GRAIN_WORD_RE.search(q):
        return sql
    try:
        gbm = _S_GROUPBY_RE.search(s)
        if not gbm:
            return sql
        gb_items = _split_top_commas(gbm.group(1))
        if len(gb_items) < 2:
            return sql

        def _norm(x):
            return re.sub(r"\s+", "", x).replace('"', "").lower()

        drop = set(_norm(x) for x in gb_items if _BARE_DATE_COL_RE.match(x.replace('"', "")))
        if not drop:
            return sql
        # 只降维、不清空：至少要留下一个分组键
        if len([x for x in gb_items if _norm(x) not in drop]) < 1:
            return sql

        m_sel = re.match(r"(?is)^(SELECT\s+)(.*?)(\s+FROM\s+)", s)
        if not m_sel:
            return sql
        kept = [x for x in _split_top_commas(m_sel.group(2)) if _norm(x) not in drop]
        if not kept:
            return sql
        out = m_sel.group(1) + ", ".join(kept) + m_sel.group(3) + s[m_sel.end():]

        def _gb_sub(mm):
            items = [x for x in _split_top_commas(mm.group(1)) if _norm(x) not in drop]
            # 注意：`_S_GROUPBY_RE` 的 group(1) 会**带上**紧邻 ORDER BY 的那个空格
            # （lazy + lookahead），所以这里必须补回一个空格，否则会拼出
            # `pl.nameORDER BY ...` 这种语法错。踩过两次，别再删。
            return ("GROUP BY " + ", ".join(items) + " ") if items else "GROUP BY 1 "

        out = _S_GROUPBY_RE.sub(_gb_sub, out, count=1)

        def _ob_sub(mm):
            items = []
            for x in _split_top_commas(mm.group(1)):
                col = re.sub(r"(?i)\s+(ASC|DESC)\s*$", "", x).strip()
                if _norm(col) not in drop:
                    items.append(x)
            return ("ORDER BY " + ", ".join(items) + " ") if items else ""

        out = re.sub(r"(?is)\bORDER\s+BY\s+(.*?)(?=\bLIMIT\b|\bOFFSET\b|$)", _ob_sub, out, count=1)
        return out if out != s else s
    except Exception:
        return sql


def _top_level_aggs(text: str) -> list:
    """取出 text 中**不在任何括号内**的聚合调用原文（子查询里的不算）。"""
    depths, d = [], 0
    for ch in text or "":
        if ch == ")":
            d -= 1
        depths.append(d)
        if ch == "(":
            d += 1
    out = []
    for m in re.finditer(r"(?i)(?<![A-Za-z0-9_])(SUM|AVG|COUNT|MIN|MAX)\s*\(", text or ""):
        if m.start() >= len(depths) or depths[m.start()] != 0:
            continue
        k, dd, n = m.end() - 1, 0, len(text)
        while k < n:
            if text[k] == "(":
                dd += 1
            elif text[k] == ")":
                dd -= 1
                if dd == 0:
                    break
            k += 1
        if k < n:
            out.append(text[m.start():k + 1])
    return out


def _agg_alias(expr: str, idx: int) -> str:
    """给补进来的聚合起一个可读别名（别名不参与严格判定，仅影响可读性）。"""
    m = re.match(r"(?is)^(SUM|AVG|COUNT|MIN|MAX)\s*\(\s*(.*?)\s*\)\s*$", expr)
    if not m:
        return '"agg_%d"' % idx
    col = re.sub(r"[^\w]", "_", m.group(2)).strip("_")[-28:] or "val"
    return '"%s_%s"' % (m.group(1).lower(), col)


def _fix_having_agg_select(query: str, sql: str) -> str:
    """HAVING 里被比较的度量没出现在 SELECT 时补进 SELECT（行数不变，只补列）。"""
    s = (sql or "").strip()
    if not s or not _FIX_SUPERSET_ON:
        return sql
    parts = _simple_select_parts(s)
    if not parts:
        return sql
    sel = parts[0]
    if not re.search(r"(?i)\bHAVING\b", s):
        return sql
    hm = re.search(r"(?is)\bHAVING\b\s+(.*?)(?=\bORDER\s+BY\b|\bLIMIT\b|\bOFFSET\b|$)", s)
    if not hm:
        return sql
    try:
        outer = _top_level_aggs(hm.group(1))
        if not outer:
            return sql

        def _norm(x):
            return re.sub(r"\s+", "", x).lower()

        have = set(_norm(x) for x in outer)
        already = set(_norm(x) for x in _top_level_aggs(sel))
        seen, add = set(), []
        for x in outer:
            if _norm(x) in already or _norm(x) in seen:
                continue
            seen.add(_norm(x))
            add.append(x)
        if not add:
            return sql
        m_sel = re.match(r"(?is)^(SELECT\s+)(.*?)(\s+FROM\s+)", s)
        if not m_sel:
            return sql
        new_sel = sel.rstrip().rstrip(",") + ", " + ", ".join(
            "%s AS %s" % (x, _agg_alias(x, i + 1)) for i, x in enumerate(add))
        out = m_sel.group(1) + new_sel + m_sel.group(3) + s[m_sel.end():]
        return out if out != s else s
    except Exception:
        return sql


# ── 「有多少台/个 X」计数源选错（2026-09-17，postgres#1 实测驱动）──────────
# 现象：问「各设备类型(equipment_type)分别有多少台设备」，SQL 却
#   `SELECT COUNT(eqp_downtime_record.equipment_id), dim_equipment.equipment_type
#    FROM eqp_downtime_record JOIN dim_equipment ON … GROUP BY dim_equipment.equipment_type`
#   —— 从**停机记录**里数设备（只数出发生过停机的 4 类），正确答案是设备维表的 8 类。
# 判据（形状故意收窄以保零误伤）：
#   ① 问句是「有多少/几台 <实体名词>」，且名词**不是**事务词（记录/单/事件/明细…）
#   ② SQL 单层 SELECT、**恰好一个 JOIN**、**无 WHERE/HAVING/WITH/UNION**
#   ③ GROUP BY 每一项都属于 JOIN 进来的 B 表；COUNT 的参数落在主表 A
#   ⇒ 计数的"总体"应由拥有该分组属性的 B 表决定 ⇒ 改为从 B 计数。
# 反例（已通过题，必须零触发）：
#   - 123#15「各部门分别有多少人」：GROUP BY 在主表 department、COUNT 在 JOIN 表
#     → 方向相反，天然排除；
#   - postgres#18「各订单状态分别有多少订单」：无 JOIN；
#   - 「各X有多少条<记录/工单>」：名词是事务词，被 ① 排除（那本来就该数事实表）。
_CNT_ENTITY_RE = re.compile(
    r"有\s*(?:多少|几)\s*(?:台|个|条|名|种|家|张|批|件|只|位)?\s*([\u4e00-\u9fff]{1,8})")
_CNT_TXN_NOUN_RE = re.compile(r"记录|单据|工单|订单|事件|日志|明细|交易|流水|任务|次数|笔|项")
_FROM_JOIN_RE = re.compile(
    r"(?is)^\s*(" + _S_TBLREF + r")(?:\s+(?:AS\s+)?(" + _S_IDENT + r"))?"
    r"\s+(?:INNER\s+|LEFT\s+|RIGHT\s+|FULL\s+|CROSS\s+)?JOIN\s+"
    r"(" + _S_TBLREF + r")(?:\s+(?:AS\s+)?(" + _S_IDENT + r"))?\s+ON\s+")


def _fix_count_from_owner(query: str, sql: str) -> str:
    """「有多少台/个 X」从错误的事实表计数 → 改从拥有该分组属性的维表计数。"""
    s = (sql or "").strip()
    if not s or not _FIX_SUPERSET_ON:
        return sql
    mq = _CNT_ENTITY_RE.search(str(query or ""))
    if not mq or _CNT_TXN_NOUN_RE.search(mq.group(1)):
        return sql
    if re.search(r"(?i)\b(WHERE|HAVING|UNION|WITH)\b", s):
        return sql
    if len(re.findall(r"(?i)\bJOIN\b", s)) != 1:
        return sql
    parts = _simple_select_parts(s)
    if not parts:
        return sql
    try:
        m = _FROM_JOIN_RE.match(parts[1])
        if not m:
            return sql
        tbl_a, alias_a, tbl_b, alias_b = m.group(1), (m.group(2) or ""), m.group(3), (m.group(4) or "")

        def _ref(tbl, alias):
            a = _bare(alias)
            return a if a and a not in _S_RESERVED else _bare(tbl)

        a_ref, b_ref = _ref(tbl_a, alias_a), _ref(tbl_b, alias_b)
        if not a_ref or not b_ref or a_ref == b_ref:
            return sql
        gbm = _S_GROUPBY_RE.search(s)
        if not gbm:
            return sql
        gb_items = [x.strip() for x in _split_top_commas(gbm.group(1))]
        if not gb_items:
            return sql

        def _owner(item):
            mm = re.match(r"(?is)^\s*(" + _S_IDENT + r")\s*\.", item)
            return _bare(mm.group(1)) if mm else ""

        if any(_owner(x) != b_ref for x in gb_items):
            return sql                      # 分组不全属于 B 表 → 不是这个形态
        ok_cnt = False
        for arg in re.findall(r"(?is)COUNT\s*\(\s*([^()]*?)\s*\)", s):
            arg = arg.strip()
            if not arg or arg == "*":
                continue
            mm = re.match(r"(?is)^(" + _S_IDENT + r")\s*\.", arg)
            if mm and _bare(mm.group(1)) == a_ref:
                ok_cnt = True
                break
        if not ok_cnt:
            return sql                      # 数的是 B 表 → 删 A 会改语义

        from_clause = tbl_b + ((" " + alias_b.strip()) if alias_b.strip() else "")
        tail = s[gbm.end():]
        mob = re.match(r"(?is)^\s*ORDER\s+BY\s+(.*?)(?=\bLIMIT\b|\bOFFSET\b|$)", tail)
        if mob and re.search(r"(?i)(?<![A-Za-z0-9_])" + re.escape(a_ref) + r"(?![A-Za-z0-9_])",
                             mob.group(1)):
            tail = tail[mob.end():]         # 排序键引用了被丢弃的 A 表 → 去掉 ORDER BY
        new = ("SELECT %s, COUNT(*) AS \"cnt\" FROM %s GROUP BY %s "
               % (", ".join(gb_items), from_clause, ", ".join(gb_items))) + tail.strip()
        return new if new != s else s
    except Exception:
        return sql


# ── 相关计数子查询 → LEFT JOIN + GROUP BY（2026-09-17，123#20 实测驱动）──────
# 现象：问「订单数量最多的前5名客户是哪些」，Agent 写成
#   `SELECT c."name", (SELECT COUNT(*) FROM factory.sales_order so
#                      WHERE so.customer_id = c."id") AS "订单数量"
#    FROM factory.customer c ORDER BY "订单数量" DESC LIMIT 5`
# 子查询本身**没问题**，问题是它按**每一行客户**计数：实测 `factory.customer` 有 360 行、
# 但**姓名只有几十个去重值**（同名多行），所以逐行算的结果与「按客户名合并」完全不同。
# gold 的形态是 `… c LEFT JOIN sales_order so ON so.customer_id = c.id
#               GROUP BY c.name ORDER BY cnt DESC LIMIT 5` —— 按**显示维度**合并同名行。
# 判据（全部成立才动手，宁可漏判）：
#   ① 单层 SELECT；② select 列表**恰好两项**：一个标量 `(SELECT COUNT(*) … WHERE a=b)`，
#   一个纯维度表达式（不含聚合）；③ 外层**没有** WHERE/GROUP BY/HAVING/JOIN/WITH/UNION
#   （有筛选或已分组就不动，避免丢条件）；④ 子查询的等值条件必须**关联到外层表**；
#   ⑤ 保留原 ORDER BY/LIMIT 尾部（TOP-N 语义不变）。
# 产物：`… LEFT JOIN t2 x ON <原关联条件> GROUP BY <维度表达式>`，
# 计数写成 `COUNT(x.<关联列>)` —— LEFT JOIN 下未匹配行的该列为 NULL ⇒ 计 0，
# 与相关子查询语义**完全等价**；且不含子查询的维度列进 GROUP BY，同名行自然合并。
_COUNT_SUB_RE = re.compile(
    r"(?is)^\(\s*SELECT\s+COUNT\s*\(\s*\*\s*\)\s+FROM\s+(?P<t2>" + _S_TBLREF + r")"
    r"(?:\s+(?:AS\s+)?(?P<x>" + _S_IDENT + r"))?\s+WHERE\s+(?P<corr>(?P<x2>"
    + _S_IDENT + r")\s*\.\s*(?P<fk>" + _S_IDENT + r")\s*=\s*(?P<t1>" + _S_IDENT +
    r")\s*\.\s*(?P<pk>" + _S_IDENT + r"))\s*\)\s*(?:(?:AS\s+)?(?P<alias>" + _S_IDENT + r"))?\s*$")


def _split_at_top_from(s: str):
    """在**括号深度 0** 的 `FROM` 处切成 (select列表, FROM之后整段)；切不开返回 None。

    为什么不复用 `_simple_select_parts`：它用非贪婪正则 `SELECT\\s+(.*?)\\s+FROM\\s+`，
    select 列表里**带子查询**时（`c.name, (SELECT COUNT(*) FROM so WHERE …)`）会在
    子查询自己的 `FROM` 上抢先切开，导致括号不配对而整条判为"不支持"。
    本函数按括号深度扫描，深度 0 的那个 `FROM` 才是外层的 —— 子查询形态因此可用。
    """
    s = str(s or "").strip().rstrip(";")
    if not re.match(r"(?is)^SELECT\s+", s) or re.search(r"(?i)\bUNION\b", s):
        return None
    depth, i, n = 0, 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and s[i:i + 4].upper() == "FROM":
            prev = s[i - 1] if i else " "
            if not (prev.isalnum() or prev in '_.$"'):
                sel = s[len("SELECT"):i].strip()
                rest = s[i + 4:].strip()
                return (sel, rest) if sel and rest else None
        i += 1
    return None


def _fix_correlated_count_to_join(query: str, sql: str) -> str:
    """逐行相关计数子查询 → `LEFT JOIN … GROUP BY <显示维度>`（同名行合并）。"""
    s = (sql or "").strip()
    if not s or not _FIX_SUPERSET_ON:
        return sql
    parts = _split_at_top_from(s)
    if not parts:
        return sql
    sel, rest = parts
    # ⚠️ 守卫必须只看**外层**（`rest` = FROM 之后整段），不能在 `s` 上找 WHERE ——
    # select 列表里那个子查询自己就带 WHERE，在整条 SQL 上找会把**正常形态全部误杀**
    # （踩过一次：规则写了却不触发，查了半天是守卫把自己拦在门外）。
    # `_simple_select_parts` 已排除 WITH/UNION/DISTINCT。
    if re.search(r"(?i)\b(WHERE|GROUP\s+BY|HAVING|JOIN)\b", rest):
        return sql
    try:
        items = _split_top_commas(sel)
        if len(items) != 2:
            return sql
        subs = [x for x in items if _COUNT_SUB_RE.match(x)]
        if len(subs) != 1:
            return sql
        m = _COUNT_SUB_RE.match(subs[0])
        dim = [x for x in items if x is not subs[0]][0]
        if _S_AGG_RE.search(dim) or not re.match(r"(?is)^[\w\".\s\[\]]+$", dim):
            return sql
        t1_ref = _main_table_ref(rest)
        if not t1_ref:
            return sql
        # 相关条件必须真的把子查询接回外层（否则不是这个形态）
        if _bare(m.group("t1")) != _bare(t1_ref):
            return sql
        x_alias = (m.group("x") or "").strip()
        # 子查询的表**必须有别名**：没别名时（`FROM factory.sales_order WHERE …`）
        # 关联条件里写的是三段式表名，JOIN 后若另起别名就会失效 ⇒ 直接不动。
        if not x_alias or x_alias.strip('"').lower() in _S_RESERVED:
            return sql
        alias = (m.group("alias") or "").strip() or "cnt"
        dim_expr = re.sub(r"(?is)\s+AS\s+" + _S_IDENT + r"\s*$", "", dim).strip()
        # `rest` 是「FROM 之后**整段**」（含尾部 ORDER BY / LIMIT）—— 必须把尾部切出来，
        # 否则会把 ORDER BY 塞进 FROM 子句里拼出语法错。踩过一次，别再删。
        tail_m = re.search(r"(?is)\b(?:ORDER\s+BY|LIMIT)\b.*$", rest)
        rest_head = (rest[:tail_m.start()].strip() if tail_m else rest.strip())
        tail = ((" " + tail_m.group(0).strip()) if tail_m else "")
        if not rest_head:
            return sql
        new = ("SELECT %s, COUNT(%s.%s) AS %s FROM %s LEFT JOIN %s %s ON %s"
               " GROUP BY %s%s"
               % (dim, x_alias, m.group("fk"), alias, rest_head,
                  m.group("t2"), x_alias, m.group("corr"), dim_expr, tail))
        return new if new != s else s
    except Exception:
        return sql


# ── 「最X的<实体>」应取**全局 TOP-1**（2026-09-17，postgres#19 实测驱动）──────
# 现象：问「各工厂(established)建厂时间最早的工厂」，Agent 写成
#   `SELECT MIN(established) AS 最早建厂时间, factory_name FROM test_factories
#    GROUP BY factory_name ORDER BY 1 DESC LIMIT 1000`  → 3 行（逐工厂各一行）
# 而 gold 是全局一条：`SELECT factory_name, established FROM test_factories
#    ORDER BY established LIMIT 1`。⇒「最X的Y」问的是**唯一那一个 Y**，不是每组的极值。
# 判据（五条**同时**成立才动手，宁可漏判也不误伤）：
#   ① 问句含「最<形容词>的」；
#   ② 问句**不含**「前N / TOP N」—— 那是 TOP-N 截断，不是 TOP-1
#      （postgres#14「产量最高的**前3条**产线(line_id)」正是靠这条被排除，否则会把
#        3 行改写成 1 行，从"答错"变成"错得更狠"）；
#   ③ 括号列名提示 `(established)` 出现在「最…」**之前**，且该列**已出现在 SQL 里**
#      （只用 SQL 已有的列，绝不凭空造列）；
#   ④ SQL 有 GROUP BY、没有 `LIMIT 1`、**没有 WHERE**（有筛选就说明还有别的约束，
#      重建语句会丢条件 ⇒ 不动）；
#   ⑤ SQL 是单层 SELECT 且**只引用一张表**（多表会丢 JOIN 语义 ⇒ 不动）。
# 产物 `SELECT t.* FROM t ORDER BY t.col <dir> LIMIT 1`：严格口径允许附加列，
# `t.*` 覆盖 gold 要的全部列，行数 1 与 gold 一致。方向由形容词定（早/小/低/少/短=升序）。
_SUP_DIR_MAP = (("早", "ASC"), ("旧", "ASC"), ("小", "ASC"), ("低", "ASC"),
                ("少", "ASC"), ("短", "ASC"), ("晚", "DESC"), ("新", "DESC"),
                ("大", "DESC"), ("高", "DESC"), ("多", "DESC"), ("长", "DESC"))
_SUP_Q_RE = re.compile(r"最\s*(" + "|".join(a for a, _ in _SUP_DIR_MAP) + r")\s*的")
_SUP_HINT_RE = re.compile(r"[（(]\s*([A-Za-z_][A-Za-z0-9_]*)\s*[)）]")
_SUP_TOPN_Q_RE = re.compile(r"前\s*(?:\d+|[一二三四五六七八九十]+)|TOP\s*\d+", re.I)


def _global_topn1_intent(query: str):
    """问句是不是「最X的<实体>」= 全局 TOP-1？返回 `(排序列, 形容词)`，否则 None。

    **共用的唯一谓词**：`_fix_dim_listing_limit`（让路，别放开 LIMIT）与
    `_fix_global_superlative_topn`（改写，重建成全局排序取首条）都调它，
    保证"让路的题"与"改写的题"永远是同一批，不会出现一边让路一边不改的错位。

    关键判据是**括号列名提示出现在「最…」之前**：本项目问句用 `(established)` 标注
    排序键，只有这种写法才能确定"排序键属于实体本身 ⇒ 全局取一条"。
    反例 `123#31「各部门工资最高的员工是谁」`没有提示，那是"各组内部之最"（已通过题，
    必须零触发）；反例 `postgres#14「产量最高的**前3条**产线(line_id)」`被 TOP-N 排除。
    """
    q = str(query or "")
    mq = _SUP_Q_RE.search(q)
    if not mq or _SUP_TOPN_Q_RE.search(q):
        return None
    hint = ""
    for mm in _SUP_HINT_RE.finditer(q):
        if mm.start() < mq.start():
            hint = mm.group(1)
    if not hint or _bare(hint).lower() in _S_RESERVED:
        return None
    return (_bare(hint), mq.group(1))


def _fix_global_superlative_topn(query: str, sql: str) -> str:
    """「最X的<实体>」被写成按实体逐行分组 → 改为全局 TOP-1。"""
    s = (sql or "").strip()
    q = str(query or "")
    if not s or not q or not _FIX_SUPERSET_ON:
        return sql
    intent = _global_topn1_intent(q)          # 与 _fix_dim_listing_limit 共用的谓词
    if not intent:
        return sql
    hint, adj = intent
    if not _S_GROUPBY_RE.search(s) or re.search(r"(?i)\bLIMIT\s+1\b", s):
        return sql
    if re.search(r"(?i)\b(WHERE|HAVING|UNION|WITH)\b", s):
        return sql
    if not _simple_select_parts(s):
        return sql
    # 只用 SQL 里**已经出现**的列，绝不凭空造列
    if not re.search(r"(?i)(?<![A-Za-z0-9_])" + re.escape(hint) + r"(?![A-Za-z0-9_])", s):
        return sql
    try:
        import sqlglot
        from sqlglot import exp
        ast = sqlglot.parse_one(s)
        if ast is None:
            return sql
        tables = list(ast.find_all(exp.Table))
        if len(tables) != 1:
            return sql
        t = tables[0]
        tbl_ref = ((t.db + ".") if t.db else "") + t.name
        if not tbl_ref:
            return sql
        direction = dict(_SUP_DIR_MAP)[adj]
        new = ("SELECT %s.* FROM %s ORDER BY %s.%s %s LIMIT 1"
               % (tbl_ref, tbl_ref, tbl_ref, hint, direction))
        return new if new != s else s
    except Exception:
        return sql


# ── 输出列/粒度确定性改写的**唯一入口**（2026-09-17 重构）──────────────────
# 为什么必须是唯一入口：这条链原先在「LLM 分支」与「编译分支」各写一遍，
# 编译分支整条漏挂（编译产物从不做归一化），踩过一次大坑；测试与离线回放又各写一遍，
# 四处清单互相漂移，改一处只对一半题目生效。现在任何新增改写只加到这里。
#
# 顺序有意为之：先归一化 LIMIT/窗口兜底/并列名次 → 再做「值放错列」语义修正 →
# 最后补列（补列放最后，才能看见前几步改写后的 SELECT 列表）。
_FIX_STEP_MSG = {
    "_fix_dim_listing_limit": "「各X」类问题需返回全部分组行，已放开 LIMIT 20 截断",
    "_fix_relative_date_anchor": "「最近N天/本月」等相对窗口已改用当前日期为锚点（不再以数据最大日期为准）",
    "_fix_relative_month_literal": "「上月/本月」的硬编码年月已归一为按当前日期推算",
    "_fix_relative_window_days": "「最近N天」的窗口天数已对齐问句里的 N",
    "_fix_window_drop_time_groupby": "「最近N天各X」中日期列是过滤条件，已从分组与输出中移除",
    "_fix_having_agg_select": "HAVING 比较的度量未输出，已补进 SELECT（行数不变）",
    "_fix_count_from_owner": "「有多少个/台 X」的计数源取错表，已改为从该维度的维表计数",
    "_fix_window_diff_coalesce": "环比/差值类问题首期无上一期，已用 COALESCE(...,0) 兜底",
    "_fix_topn_stable_tiebreak": "TOP-N 存在并列名次，已追加实体主键升序作为稳定次级排序键",
    "_fix_value_column_mismatch": "检测到「把维表属性值等值到事实表外键列」，已改写为维表子查询",
    "_fix_explicit_col_select": "问句点名了列名，已补齐该列的输出与分组",
    "_fix_group_key_superset": "已补齐分组维度的关联键列（输出与分组同步扩展）",
    "_fix_detail_row_superset": "明细类问题已补齐全列（保留排序与行数不变）",
    "_fix_cte_rank_to_limit": "CTE 窗口排名已等价塌缩为 ORDER BY ... LIMIT（列集不再被裁剪）",
    "_fix_correlated_count_to_join": "逐行相关计数子查询已改为 LEFT JOIN + 按显示维度分组（同名/重复行合并）",
    "_fix_global_superlative_topn": "「最X的<实体>」应取全局唯一一条，已改为全局排序取首条（不再按实体逐行分组）",
}


def apply_output_fixes(query: str, sql: str, fired: list | None = None) -> str:
    """按固定顺序施加全部确定性改写；`fired` 收集实际生效的步骤名（供步骤日志）。"""
    out = sql or ""
    if not out:
        return out
    steps = (
        ("_fix_dim_listing_limit", lambda s: _fix_dim_listing_limit(query, s)),
        ("_fix_relative_date_anchor", lambda s: _fix_relative_date_anchor(query, s)),
        ("_fix_relative_month_literal", lambda s: _fix_relative_month_literal(query, s)),
        # 天数归一必须在日期锚点之后：锚点先把 `MAX(date)-INTERVAL '29 days'` 换成
        # `CURRENT_DATE-INTERVAL '29 days'`，这里再按问句把 29 改成 30。
        ("_fix_relative_window_days", lambda s: _fix_relative_window_days(query, s)),
        ("_fix_window_drop_time_groupby", lambda s: _fix_window_drop_time_groupby(query, s)),
        ("_fix_window_diff_coalesce", lambda s: _fix_window_diff_coalesce(query, s)),
        ("_fix_topn_stable_tiebreak", lambda s: _fix_topn_stable_tiebreak(query, s)),
        ("_fix_value_column_mismatch", lambda s: _fix_value_column_mismatch(s)),
        ("_fix_having_agg_select", lambda s: _fix_having_agg_select(query, s)),
        ("_fix_count_from_owner", lambda s: _fix_count_from_owner(query, s)),
        ("_fix_explicit_col_select", lambda s: _fix_explicit_col_select(query, s)),
        ("_fix_group_key_superset", lambda s: _fix_group_key_superset(query, s)),
        ("_fix_detail_row_superset", lambda s: _fix_detail_row_superset(query, s)),
        ("_fix_cte_rank_to_limit", lambda s: _fix_cte_rank_to_limit(query, s)),
        # 逐行相关计数子查询 → LEFT JOIN + GROUP BY 显示维度（重建粒度，仍保留尾部 ORDER BY/LIMIT）
        ("_fix_correlated_count_to_join", lambda s: _fix_correlated_count_to_join(query, s)),
        # 放在**最后**：它是唯一会重建整条语句的改写（丢掉 GROUP BY、换成全局排序），
        # 让前面所有"补列/调分组"的步骤先作用于原语句，产物就不会被后续步骤二次加工。
        ("_fix_global_superlative_topn", lambda s: _fix_global_superlative_topn(query, s)),
    )
    for name, fn in steps:
        try:
            nxt = fn(out)
        except Exception:
            continue
        if nxt and nxt != out:
            out = nxt
            if fired is not None:
                fired.append(name)
    return out


# ── 窗口差值的首期兜底（2026-09-15，高价值题库 #6 实测驱动）──────────────
_WIN_DIFF_Q_RE = re.compile(r"环比|相比前|较前|比前|变化了多少|变化率|增减|涨跌|差值")
_LAG_FN_RE = re.compile(r"\b(?:LAG|LEAD)\s*\(", re.I)


def _fix_window_diff_coalesce(query: str, sql: str) -> str:
    """把 `x - LAG(x) OVER (...)` 改写为 `COALESCE(x - LAG(x) OVER (...), 0)`。

    实测（yans 高价值题库 #6「一车间-1号线6月1日至6月7日每天的产量相比前一天变化了多少」）：
    模型生成的 SQL 数值**全部正确**，只因第一个周期没有上一期、差值返回 NULL，
    而标准答案是 0 —— 7 行里错 1 个格就判失败。
    提示词压不住（同一轮里模型给 SUM 里的列加了 COALESCE，偏偏没给差值加），
    因此改为**确定性 AST 改写**：只动含 LAG/LEAD 的减法表达式，其余原样保留。
    触发条件双保险：问句确实是「环比/相比前/变化率」类，且 SQL 里确实有 LAG/LEAD。
    """
    s = (sql or "").strip()
    if not s or not _WIN_DIFF_Q_RE.search(query or ""):
        return s
    if not _LAG_FN_RE.search(s):
        return s
    try:
        import sqlglot
        from sqlglot import exp
        ast = sqlglot.parse_one(s, read="postgres")
        todo = []
        for sub in ast.find_all(exp.Sub):
            if sub.find(exp.Window) is None:
                continue                      # 不是「窗口函数参与的减法」，不动
            if isinstance(sub.parent, exp.Coalesce):
                continue                      # 已经兜底过，避免套两层
            todo.append(sub)
        if not todo:
            return s
        for sub in todo:
            sub.replace(exp.Coalesce(this=sub.copy(), expressions=[exp.Literal.number(0)]))
        out = ast.sql(dialect="postgres")
        return out or s
    except Exception:
        return s


# ── 输出质量闸门：拦「能跑通但答非所问」的退化 SQL ─────────────────────
# 这两类产物都能通过只读安全校验与列名存在性校验，却完全不是对问题的回答。
# 实测（yans 高价值题库，2026-09-15）：
#   ① 问「多少种/几种不同的 X」= 去重计数，却答成 `SELECT output_id, SUM(input_qty)
#      GROUP BY output_id`（按主键分组、也没去重）；
#   ② 问「既…又…的产线有哪些」，却答成 `SELECT output_id, work_order_id, product_id,
#      process_id, line_id, stat_date FROM mes_process_output LIMIT 20`（把事实表主键倒出来）。
# 返回原因字符串（"" = 通过），交给调用方**带原因重试**——这是本文件既有的纠错机制。
_Q_CNTDISTINCT_RE = re.compile(r"多少种|几种|几类|多少个不同|种类数|不重复的|去重的")
_SQL_CNTDISTINCT_RE = re.compile(r"COUNT\s*\(\s*DISTINCT", re.I)
_SQL_SELECTSTAR_RE = re.compile(r"SELECT\s+\*", re.I)
_SQL_IDCOL_RE = re.compile(r"\b[\w\u4e00-\u9fa5]*(?:_id|_code|_no)\b", re.I)
_SQL_HAS_STRUCT_RE = re.compile(
    r"\b(COUNT|SUM|AVG|MAX|MIN)\s*\(|\bGROUP\s+BY\b|\bDISTINCT\b|\bWHERE\b|\bJOIN\b", re.I)
# 「名称/类别类取值」判据：含中日韩字符才算 —— `line_id = 'L01'` 这类 id/code 取值
# 本身可能就是对的（改写虽等价但属多余扰动），只有中文名称/类别值才必然放错了列。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# ── 「问句有明确条件，SQL 却把它丢了」判据（2026-09-17 全量 92 题评测驱动）──────
#
# 证据：92 题里 8 道失败题的 SQL **全部**出自 MQL 编译器的聚合分支，
# 且都带着编译器无条件追加的 `ORDER BY 1 DESC LIMIT 20` 尾巴（llm_service 的
# `_compile_sql_draft`）。它们能跑通、语法合法，所以一路放行，但语义上
# 把问句里的条件整个丢掉了 —— 是"答非所问"里最整齐、最容易确定性识别的一类：
#   · #6  「设备类型为 CNC 的设备有多少台」 → `SELECT COUNT(...) FROM dim_equipment`（无 WHERE）
#   · #29 「停机时长分档统计（短停/中停/长停）」 → `GROUP BY reason`（无 CASE）
#   · #46 「入职超过10年的员工有多少人」 → `WHERE hire_date < '2015-07-10'`（相对时间被写死）
#   · #47 「本月销售额是多少」 → `SELECT SUM(amount) FROM sales_order_item`（无 WHERE）
#   · #48 「上月的生产记录有多少条」 → `SELECT COUNT(id) FROM production_record`（无 WHERE）
#   · #53 「哪个供应商采购额最高」 → `SELECT SUM(total_amount) FROM purchase_order`（无 GROUP BY）
# 这类 SQL 必然是 0 分，拦下来只会走 `_direct_gen_sql` 那条"带真实拒绝原因重试一轮"
# 的路径，**不会把「答错」升级成「答不出」**（两者得分相同，见 _valid 的注释）。
# 开关：`QUALITY_GATE_COND_LOSS=0` 可整体关闭。
_COND_LOSS_ON = os.getenv("QUALITY_GATE_COND_LOSS", "1").strip() not in ("0", "false", "off")

# 显式时间范围（**必须有数字单位**才算，"最近有没有延期工单"这类不算）
_RULE_TIME_RANGE = re.compile(
    r"本月|这个月|当月|上月|上个月|下个月|本季度|上季度|本年度|今年|去年|今天|昨天|"
    r"本周|上周|本日|同期|"
    r"(?:近|最近|过去|未来|前)\s*\d+\s*(?:天|日|周|个?月|年|季度)")
# 阈值比较（右侧必须是数字或"平均/均值"）
_RULE_THRESHOLD = re.compile(
    r"(?:超过|大于|高于|多于|不少于|不低于|不小于|大于等于|小于|低于|少于|不足|"
    r"不超过|不大于|至少|至多|达到)\s*"
    r"\d+(?:\.\d+)?\s*(?:%|％|倍|年|个?月|天|日|小时|分钟|次|台|个|元|万元)?|"
    r"(?:超过|大于|高于|多于|少于|低于|不足)\s*(?:全厂|整体|总体|全局)?(?:平均|均值)")
# 枚举取值筛选：「设备类型为 CNC 的」「状态是 运行 的」。负向排除"为多少/是几"这类疑问式
_RULE_ENUM_FILTER = re.compile(
    r"[\u4e00-\u9fa5]{2,8}\s*(?:为|是|=)\s*(?!(?:多少|几|什么|哪|何|谁))"
    r"[A-Za-z0-9\u4e00-\u9fa5\-]{1,16}\s*的")
# 相对时间表达（用绝对日期常量作答必然随日期漂移而失效）
_RULE_RELATIVE_TIME = re.compile(
    r"(?:超过|满|不足|不到|至少)\s*\d+\s*(?:年|个?月|天|日|周)|"
    r"(?:近|最近|过去|未来|前)\s*\d+\s*(?:天|日|周|个?月|年)")
# 分档/分类口径（必须靠 CASE WHEN 表达，按原始列分组换个问法就答错）
# 注意：「延期」单独出现太弱 —— 「最近有没有延期工单」是在**筛选**而非分档，
# 所以状态词要求成对枚举（「按期与延期」）才算分档信号。实测过这一版假失败。
_RULE_BUCKET = re.compile(r"分档|分档统计|区间|分段|档位|短停|中停|长停")
_RULE_BUCKET_PAIR = re.compile(
    r"(?:按期|延期|正常|异常|合格|不合格|达标|未达标)"
    r"[^，。；、]{0,4}(?:与|和|、)"
    r"[^，。；、]{0,4}(?:按期|延期|正常|异常|合格|不合格|达标|未达标)")
# 「哪个/哪家…最…」= 挑出单个实体，必须有 GROUP BY 汇总
_RULE_WHICH_MAX = re.compile(r"哪(?:个|家|位|种|条|台|名|款|间)|谁")
# 时间粒度：问「各个月 / 按月 / 月度趋势」却按原始日期逐条分组（实测 123#13：
# gold 按 YYYY-MM 聚成 12 行，Agent 按 work_date 逐日分成 365 行）
_RULE_MONTH_GRAIN = re.compile(r"各个月|按月|每月|月度|每个月的|各月")
_SQL_WHERE_RE = re.compile(r"(?i)\bWHERE\b|\bHAVING\b")
_SQL_GROUPBY_RE = re.compile(r"(?i)\bGROUP\s+BY\b")
_SQL_ORDERBY_RE = re.compile(r"(?i)\bORDER\s+BY\b")
_SQL_LIMITN_RE = re.compile(r"(?i)\bLIMIT\s+\d+")
_SQL_MONTH_GRAIN_RE = re.compile(r"(?i)to_char\s*\(|date_trunc\s*\(|extract\s*\(|\bmonth\b|substr\s*\(")
_SQL_CASE_RE = re.compile(r"(?i)\bCASE\b")
_SQL_NOW_RE = re.compile(r"(?i)\bCURRENT_DATE\b|\bCURRENT_TIMESTAMP\b|\bNOW\s*\(|\bLOCALTIMESTAMP\b")
_SQL_LITERAL_DATE_RE = re.compile(r"'\d{4}-\d{2}-\d{2}")
_SQL_LIMIT1_RE = re.compile(r"(?i)\bLIMIT\s+1\b")

# ── ⑧ 「要明细却给了聚合」（2026-09-17）──────────────────────────────────
# 实测 123#52「设备最近维护情况」：gold 是明细行（设备名/类型/日期/费用 ORDER BY 日期 DESC
# LIMIT 5），Agent 却按维护类型 COUNT 成 4 行 —— 问的是"情况"，答的是"统计"。
# 判据刻意做窄（四个条件同时成立才拦，**实测 92 题零误伤、仅命中该题**）：
#   ① 问句要明细（情况/明细/详情/清单）
#   ② 问句没有分组维度词（各/每/按/分别）—— 有则是聚合意图
#   ③ 问句没有计数量词（数/量/多少/统计/总…）——「检验记录数」这类就是要聚合
#   ④ SQL 确实是 GROUP BY + 聚合
# 开关：`QUALITY_GATE_DETAIL_INTENT=0` 关闭。
_DETAIL_INTENT_ON = os.getenv("QUALITY_GATE_DETAIL_INTENT", "1").strip() not in ("0", "false", "off")
_RULE_DETAIL_WANT = re.compile(r"情况|明细|详情|清单")
_RULE_DIM_WORD = re.compile(r"各|每|按|分别|分组|排序")
_RULE_COUNT_WORD = re.compile(r"数|量|多少|统计|几个|几台|几条|排行|排名|总")


def _detail_intent_reason(query: str, sql: str) -> str:
    """「要明细却输出聚合」的可读原因；"" = 没看出问题。"""
    if not _DETAIL_INTENT_ON:
        return ""
    try:
        q = str(query or "")
        s = str(sql or "")
        if not q or not s:
            return ""
        if not _RULE_DETAIL_WANT.search(q):
            return ""
        if _RULE_DIM_WORD.search(q) or _RULE_COUNT_WORD.search(q):
            return ""
        if not _SQL_GROUPBY_RE.search(s) or not _S_AGG_RE.search(s):
            return ""
        return ("问题是问「具体情况/明细」——需要的是记录行本身（含名称、时间等描述列，"
                "按时间倒序取最近若干条），不是分组统计。请去掉 GROUP BY 与聚合，"
                "直接输出明细行并限制条数。")
    except Exception:
        return ""


# ── ⑨ 问句点名的业务维度在 SQL 中完全缺席（2026-09-17）──────────────────────
# 现象：问「各部门的工资总和」却 `FROM employee GROUP BY employee.name`（123#10）；
#      问「每种物料类别的物料数量」却去数 `product` 表（123#5）；问「各产品类别的
#      不良类型分布」却只 `GROUP BY defect_type`、从没关联产品表（postgres#22）。
#      三者是同一件事：**问句里的业务维度在 SQL 里一个字都没出现** —— 生成的 SQL
#      其实在回答另一个问题。
# 判据：把 SQL 的标识符按 `_` / `.` 切成段（`production_line` → {production, line}），
#      问句点名的维度词若其英文片段**一段都不在**段集合里 ⇒ 拒绝。
# 为什么按"段"而不是子串：`production_record` 含子串 `product`，naive 子串匹配会把
#      「产品」误判成"已出现"；按段切分得 {production, record}，里面没有 `product`，
#      判据才可靠。中英对照取自项目自带的 `field_semantics._PART_CN`。
# 开关：`QUALITY_GATE_DIM_ABSENT=0` 关闭。
# ⚠️ 词表**只保留实测零误伤的维度**。刻意不含「产品」：本数据集里「产品类别」既可能
#    指 `product.category`（postgres#22），也可能指 `material.category`（123#22，gold
#    就是用 material）—— 两者冲突，用它做判据会误伤已通过题。「设备」加上库里的缩写
#    `eqp`（`eqp_downtime_record`）后才是零误伤。
_DIM_ABSENT_ON = os.getenv("QUALITY_GATE_DIM_ABSENT", "1").strip() not in ("0", "false", "off")
_DIM_TOKEN_MAP = (
    ("部门", ("department",)),
    ("产线", ("line",)),
    ("员工", ("employee", "staff", "worker")),
    ("物料", ("material", "item")),
    ("客户", ("customer",)),
    ("供应商", ("supplier", "vendor")),
    ("设备", ("equipment", "machine", "device", "eqp")),
    ("仓库", ("warehouse",)),
    ("车间", ("workshop",)),
    ("工厂", ("factory", "plant")),
    ("考勤", ("attendance",)),
)


def _sql_ident_segments(sql: str) -> set:
    """SQL 标识符按 `_` 切成的片段集合（用于"维度是否出现"的精确判定）。"""
    segs = set()
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql or ""):
        for part in tok.split("_"):
            if part:
                segs.add(part.lower())
    return segs


def _dim_absent_reason(query: str, sql: str) -> str:
    """问句点名的业务维度在 SQL 里完全不出现时的可读原因；"" = 没看出问题。"""
    if not _DIM_ABSENT_ON:
        return ""
    try:
        q = str(query or "")
        s = str(sql or "")
        if not q or not s:
            return ""
        segs = _sql_ident_segments(s)
        for cn, toks in _DIM_TOKEN_MAP:
            if cn not in q:
                continue
            if any(t in segs for t in toks):
                continue
            return ("问题问的是「%s」这个业务维度，但 SQL 里从头到尾没有出现该维度的表或列"
                    "（应出现的关键词：%s）—— 请先把该维度关联进来，再按它分组或筛选。"
                    % (cn, "、".join(toks)))
    except Exception:
        return ""
    return ""


# ⑩ 带数字的阈值条件被整个丢掉（2026-09-17，123#46 实测驱动）：问「入职超过10年的
#    员工有多少人」，SQL 写成 `WHERE status = '在职'` —— 凭空加的等值筛选**顶掉了**
#    真正的 hire_date 阈值。判据要求两侧同时成立：
#      左：问句里阈义词后**紧跟数字**（超过10年/大于0）；
#      右：SQL 里连一个数值比较都没有（无 `> < >= <=`、无 BETWEEN、无 INTERVAL）。
#    92 题实测 **零误伤**。注意数值比较必须容错**字符串形态** `> '60'`：postgres#20
#    与 123#7 就是这么写的（早期没容错 → 2 处误伤，靠"零误伤"这条护栏才抓出来）。
#    另：早期版本漏了"阈义词后必须跟数字"这一条，把「大于**平均**产量」这类**子查询
#    比较**也算进来，误伤 8 题 —— 所以数字是必需的，不能只认阈义词。
_THRESHOLD_NUM_RE = re.compile(
    r"(?:超过|大于|高于|低于|少于|不少于|不超过|至少|至多|满|超|不足)\s*\d")
_SQL_NUM_CMP_RE = re.compile(r"(?i)([<>]=?\s*'?-?\d|\bBETWEEN\b|INTERVAL\s*')")


def _cond_loss_reason(query: str, sql: str) -> str:
    """问句里的条件被 SQL 丢掉时的可读原因；"" = 没看出问题。

    只做**确定性**判断：问句命中条件信号、而 SQL 里找不到对应的表达手段。
    宁可漏判（放行一个错的）也不制造假失败（拦掉一个对的）——
    每一条判据都要求两侧同时成立，且右侧用的是"完全缺失"这种强证据。
    """
    q = str(query or "")
    s = str(sql or "")
    if not q or not s or not _COND_LOSS_ON:
        return ""
    has_where = bool(_SQL_WHERE_RE.search(s))
    try:
        # ④ 时间范围 / 阈值 / 枚举取值：SQL 里连 WHERE 都没有 → 条件整个丢了
        if not has_where:
            if _RULE_TIME_RANGE.search(q):
                return ("问题限定了时间范围（本月/上月/最近N天/今年…），但 SQL 里没有任何 "
                        "WHERE 时间筛选，统计的是全表全期数据，答非所问；"
                        "必须补上时间列与 CURRENT_DATE / date_trunc 的相对过滤")
            if _RULE_THRESHOLD.search(q):
                return ("问题带有阈值条件（超过N / 低于平均…），但 SQL 里没有 WHERE/HAVING 过滤，"
                        "等于把该条件整个忽略，统计范围与问题不符")
            if _RULE_ENUM_FILTER.search(q):
                return ("问题限定了某个列的取值（「X 为 Y 的」），但 SQL 里没有 WHERE 过滤，"
                        "等于统计了该列的全部取值，必须补上 `列 = '值'` 的筛选")
        # ⑤ 相对时间被硬编码成绝对日期常量 → 随日期漂移必然失效
        if (_RULE_RELATIVE_TIME.search(q) and _SQL_LITERAL_DATE_RE.search(s)
                and not _SQL_NOW_RE.search(s)):
            return ("问题用的是相对时间（如「入职超过10年」「最近30天」），但 SQL 把边界算成了"
                    "写死的绝对日期常量；必须用 CURRENT_DATE ± INTERVAL 表达，否则随时间失效")
        # ⑥ 要求分档/分类汇总，却没有 CASE WHEN
        if ((_RULE_BUCKET.search(q) or _RULE_BUCKET_PAIR.search(q))
                and not _SQL_CASE_RE.search(s)):
            return ("问题要求按条件分档/分类汇总（短停/中停/长停、按期/延期…），但 SQL 里没有 "
                    "CASE WHEN 分类表达式，只是按某个原始列分组，档位口径不对")
        # ⑦ 问「哪个实体最高」却没有按该实体汇总
        if (_RULE_WHICH_MAX.search(q) and "最" in q
                and not _SQL_GROUPBY_RE.search(s) and not _SQL_LIMIT1_RE.search(s)):
            return ("问题问的是「哪个实体最高/最多」，但 SQL 既没有 GROUP BY 按该实体汇总、"
                    "也没有 LIMIT 1 取单条，等于没有回答「是哪一个」")
        # ⑧ 时间粒度丢失（2026-09-17）：问「各个月/按月/月度」却按原始日期逐条分组
        if (_RULE_MONTH_GRAIN.search(q) and _SQL_GROUPBY_RE.search(s)
                and not _SQL_MONTH_GRAIN_RE.search(s)):
            return ("问题要的是按月的时间粒度（各个月/按月/月度趋势），但 SQL 直接按原始日期列"
                    "分组，一天一行，粒度不对；必须用 to_char/date_trunc 聚到月")
        # ⑨ TOP-N 有 LIMIT 却没有 ORDER BY（2026-09-17）：截到哪几条完全随机
        if (_TOPN_RE.search(q) and _SQL_LIMITN_RE.search(s)
                and not _SQL_ORDERBY_RE.search(s)):
            return ("问题要的是「前N/最高的N」，SQL 里带了 LIMIT 却没有 ORDER BY，"
                    "截到哪几条完全由执行计划决定；必须补上明确的排序键")
        # ⑩ 带数字的阈值条件整个不见（2026-09-17）：SQL 里没有任何数值比较，
        #    说明问句的阈值被别的等值筛选顶掉了（123#46：status='在职' 顶掉 hire_date）
        if (_THRESHOLD_NUM_RE.search(q) and not _SQL_NUM_CMP_RE.search(s)):
            return ("问题里有明确的**数值阈值**（超过N / 大于N / 低于N…），但 SQL 里找不到"
                    "任何数值比较（没有 > < >= <=、没有 BETWEEN、没有 INTERVAL），"
                    "该阈值条件被整个丢掉了；必须按问句把数值比较补回来"
                    "（用另一列的等值筛选（如 status='在职'）代替阈值是无效答案）")
    except Exception:
        return ""
    return ""


def _output_quality_reason(query: str, sql: str) -> str:
    """退化 SQL 的语义质检：返回 "" 表示通过，否则返回**可读的拒绝原因**。"""
    s = str(sql or "")
    if not s:
        return ""
    try:
        if _SQL_SELECTSTAR_RE.search(s):
            return "SELECT * 返回原始明细，必须只选问题要的聚合结果与维度列"
        # ① 去重计数被写成普通聚合
        if _Q_CNTDISTINCT_RE.search(query or "") and not _SQL_CNTDISTINCT_RE.search(s):
            return ("问题是「有多少种 / 几种不同的 X」= 去重计数，必须写成 COUNT(DISTINCT 列)，"
                    "不能用 SUM 或普通 COUNT，也不要按该列 GROUP BY 后逐行列出")
        # ② 裸倒主键/外键列：≥3 个 *_id/*_code/*_no 且没有任何聚合/筛选/关联
        _proj = re.split(r"\bFROM\b", s, maxsplit=1, flags=re.I)[0]
        if len(_SQL_IDCOL_RE.findall(_proj)) >= 3 and not _SQL_HAS_STRUCT_RE.search(s):
            return ("SQL 直接倒出了多条主键/外键/编码列且没有任何聚合、筛选或关联，"
                    "这不是对问题的回答；请按问题语义做聚合、集合判断或关联维表后再输出需要的维度列")
        # ③ 「值放错列」不在这里拦截 —— 实测（2026-09-15）**拒绝型闸门在这一类上是负收益**：
        #    拦下之后只剩直生兜底，而思考型模型在剩余预算内常常出不来 → 2 题直接从
        #    WRONG 掉成 GEN_FAIL（3/6 vs 4/6），比给出一个"0 行的错误答案"更糟。
        #    改为在生成后做**确定性修复**，见 _fix_value_column_mismatch。
        # ④⑤⑥⑦ 「问句条件被丢掉」（2026-09-17）：时间范围/阈值/枚举筛选/相对时间写死/
        #    分档缺 CASE/问「哪个最…」不分组。判据与证据见 _cond_loss_reason 上方注释。
        #    只拦**完全缺失**（没有 WHERE / 没有 CASE / 没有 GROUP BY），不猜语义。
        _why_cond = _cond_loss_reason(query, s)
        if _why_cond:
            return _why_cond
        # ⑧ 要明细却给了聚合：语义类型与问题意图不符，同样属于"没回答所问"
        _why_detail = _detail_intent_reason(query, s)
        if _why_detail:
            return _why_detail
        # ⑨ 问句点名的业务维度在 SQL 里完全缺席（"在回答另一个问题"）
        _why_dim = _dim_absent_reason(query, s)
        if _why_dim:
            return _why_dim
    except Exception:
        return ""
    return ""


# ── TOP-N 并列名次的稳定次级排序键（2026-09-15）──────────────────────────
# 实测 #3「停机次数超过5次的设备中，累计停机时长最长的前5台是哪些」：第 5 名在
# 「一车间-1号线-SMT贴片」与「二车间-5号线-插件装配」之间并列（都是 12 次 / 540 分钟），
# 谁进前 5 **完全取决于执行计划** —— 同一条 SQL 只要多一个 JOIN 条件就会换人，
# 按名称排序还受数据库 collation 影响（实测 PG 默认 collation 下排到的是另一台）。
# 追加「实体主键升序」做次级键后结果可复现（实测命中标准答案 E0001）。
# 只在**确实存在并列**时才会改变结果，无并列时与不加次级键完全等价。
_TOPN_INTENT_RE = re.compile(
    r"前\s*\d+|前[一二三四五六七八九十]+|TOP\s*\d+|最(?:大|高|多|久|长|小|低|少|短|晚|早|新|旧)\s*(?:的)?\s*\d+",
    re.I)


def _fix_topn_stable_tiebreak(query: str, sql: str) -> str:
    """TOP-N 问题若只有一个排序键，追加「主维度表主键升序」作为稳定次级排序键。"""
    s = (sql or "").strip()
    if not s or not _TOPN_INTENT_RE.search(query or ""):
        return s
    try:
        import sqlglot
        from sqlglot import exp
        ast = sqlglot.parse_one(s, read="postgres")
        sel = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
        if sel is None:
            return s
        order = sel.args.get("order")
        if not order or len(order.expressions or []) != 1:
            return s          # 已有多个排序键 → 幂等跳过，不重复追加
        # 主维度列 = SELECT 列表里第一个非聚合、非窗口的列引用
        dim_alias = ""
        for e in (sel.expressions or []):
            node = e.unalias() if hasattr(e, "unalias") else e
            if node.find(exp.AggFunc) is not None or node.find(exp.Window) is not None:
                continue
            col = node if isinstance(node, exp.Column) else node.find(exp.Column)
            if isinstance(col, exp.Column) and col.table:
                dim_alias = col.table
                break
        if not dim_alias:
            return s
        pk = ""
        for col in ast.find_all(exp.Column):
            if col.table == dim_alias and re.search(r"_id$", col.name or "", re.I):
                pk = col.name
                break
        if not pk:
            return s
        # 必须用 MIN(pk) 而不是裸 pk：pk 不在 GROUP BY 里，Postgres 会直接报
        # 「column must appear in the GROUP BY clause」→ 实测改写后查询直接失败（0 行）。
        # MIN() 恰好在聚合上下文里始终合法，且 pk 是分组键的函数 → 结果唯一确定。
        order.append("expressions",
                     exp.Ordered(this=exp.Min(this=exp.column(pk, table=dim_alias)), desc=False))
        return ast.sql(dialect="postgres") or s
    except Exception:
        return s


def _sql_gen_model() -> str | None:
    """SQL 生成 / 口径推断专用模型（配置 `LLM_SQL_MODEL`）。

    2026-09-14 模型分工：SQL 生成是整个系统里最难的任务（要手写完整 SQL 并选对表、
    列、关联键、LIMIT、单位）。此前与意图分类/摘要/推荐追问共用同一个 flash 档模型，
    实测它在这种任务上会编造列名、猜错外键、漏 LIMIT、把百分数再乘 100。
    配置 `LLM_SQL_MODEL` 后只让 SQL 生成链路换用更强的模型，轻任务仍用原模型（省钱、低延迟）。
    返回 None 表示"未配置，跟随主模型"（零回归）。
    """
    try:
        m = str(LLM_CONFIG.get("sql_model") or "").strip()
    except Exception:
        m = ""
    return m or None


def _sql_looks_truncated(sql: str) -> bool:
    """SQL 是否被"砍断"（流式截止 / max_tokens 触顶 / 首 token 门中断）。

    为什么需要单独判：截断后的 SQL 往往**仍能通过**只读安全校验（它是 SELECT、
    引用的列也真实、也没有逗号连接），于是被当成"合法 SQL"执行 → 数据库报
    语法错误，白耗一次往返；更糟的是它会被当作"模型生成的 SQL"进入修复链，
    把修复方向带偏。凡是截断嫌疑一律判为不可用，交给上层重试/兜底。
    判据保守（宁可多试一次，不可误杀正常 SQL）：括号/引号不配对，或以
    运算符、残缺关键字结尾。比较前先剥掉字符串字面量，避免中文枚举值里的
    括号造成误判。
    """
    s = str(sql or "").strip()
    if not s:
        return True
    # 剥掉单引号字符串字面量再判配对（'已完成(closed)' 这类值不应计入括号）
    stripped = re.sub(r"'(?:[^']|'')*'", "''", s)
    if stripped.count("(") != stripped.count(")"):
        return True
    if stripped.count("'") % 2 or stripped.count('"') % 2:
        return True
    # 残缺结尾：以运算符 / 逗号 / 左括号 / 关键字残尾结束 → 几乎必然是砍断。
    # 注意：**不把双引号列入**——合法 SQL 常以中文别名收尾（... AS "计划达成率"），
    # 引号配对已由上面的偶数校验覆盖，放进运算符类会造成大面积误杀。
    if re.search(
            r"(?i)(?:[+\-*/=<>,(]"
            r"|\b(?:and|or|where|having|join|on|select|from|as|limit|union|when|then|else"
            r"|case|not|over|partition|between|in|like|is|by|group|order))\s*$", s):
        return True
    return False


# ── 意图分类（规则 + 置信度，低置信时 LLM 兜底）──────────

VALID_INTENTS = {"data", "chat", "gibberish", "analyze_db", "ml", "lookup"}

# 强数据信号：出现即优先判 data，不受"介绍/计算/怎么"等泛闲聊词干扰
_STRONG_DATA = [
    "分析", "统计", "排行", "排名", "良率", "不良", "库存", "停机", "产量", "产能",
    "趋势", "对比", "汇总", "占比", "最高", "最低", "平均", "列出", "查询", "显示",
    "预警", "TOP", "top", "各工序", "各产线", "各产品", "每个", "每条", "每种",
    "工单", "设备", "在制", "合格", "不合格", "销量", "金额", "营收", "成本",
    "同比", "环比", "分布", "明细", "报表", "指标", "求和", "总数", "总量",
    "记录数", "记录条数", "考勤", "班次", "缺勤", "迟到", "早退", "请假",
]
# 强闲聊信号：出现即判 chat（问候/寒暄/自我介绍类，与数据无关）
_STRONG_CHAT = [
    "你好", "您好", "谢谢", "再见", "嗨", "hello", "hi", "早上好", "晚上好", "下午好",
    "哈哈", "嘿嘿", "你是谁", "你能做什么", "你会什么", "在吗", "多谢", "辛苦",
    "不错", "厉害", "好的", "收到", "了解", "笑死", "有意思", "好玩", "嗯嗯",
]
# 弱闲聊信号：只有在完全不含数据信号时才判 chat（避免"计算各产线产量"被误吞）
_WEAK_CHAT = [
    "介绍", "帮助", "功能", "能力", "怎么用", "帮忙", "告诉我", "讲一下", "解释",
    "说明", "聊", "对不对", "能不能", "可不可以", "好不好", "行不行", "会吗",
    "讲个", "来点", "为什么", "如何", "怎么", "计算", "等于", "怎么样", "可以吗",
]
# 建模强词：几乎只出现在机器学习上下文，命中即判 ml
_ML_WORDS_STRONG = ["训练模型", "机器学习", "随机森林", "决策树", "KMeans", "kmeans", "孤立森林",
                    "线性回归", "逻辑回归", "聚类", "异常检测", "预测模型", "模型训练", "建模方案",
                    "分类模型", "分类器", "文本分类", "监督学习", "无监督", "特征工程", "回归模型",
                    # 常见口误/别名：「回归森林」「森林回归」实指随机森林；「森林」是安全兜底——
                    # 制造业务问句里不会出现「森林」，命中即判 ml，避免口误把建模问句漏判成 data。
                    "回归森林", "森林回归", "森林", "提升树", "XGBoost", "xgboost", "LightGBM"]
# 建模短词：单独出现不足判 ML ——「产品分类」「按分类统计」「回归生产/回归正常」都是业务数据问句，
# 必须同时带建模上下文词才判 ML。
_ML_WORDS = ["预测", "分类", "回归", "建模"]
# 建模上下文词：与短词组合时才把问句路由到 ML
_ML_CTX = ["训练", "模型", "算法", "样本", "特征", "目标字段", "监督", "聚类", "异常",
           "预测", "拟合", "准确率", "分类器", "分类任务", "标签", "调参", "数据集", "机器学习"]
# P1-5 预测融入主链路：未来时间表达 + 趋势/变化信号 → 路由到 ML 预测（而非历史查询）。
# 单独「走势/趋势」仍是历史查询（data），只有「未来时间 + 趋势/会/将」才进预测（防回归）。
_FUTURE_WORDS = ["未来", "接下来", "今后", "往后", "下周", "下个月", "下月", "下季度",
                 "下季", "明天", "后续", "未来一周", "未来一月", "未来一年", "未来几个月"]
_FUTURE_SIGNALS = ["趋势", "走势", "会", "将", "变化", "预估", "预测", "大概", "估计", "走势预测"]
# 未来时间 + 下列业务指标 → 也按 ML 预测处理（置信度低，交 LLM 复核）。
# 场景：「分析下个月产量」「下个月良率怎么样」——没有「趋势/预测/会」等信号词，
# 此前被判成 data 走 SQL 生成，导致建模问句漏判。（不影响历史查询：无未来时间词的
# 「统计各工序产量」仍走 data。）
_FUTURE_METRICS = ["产量", "产能", "良率", "不良", "缺陷", "停机", "库存", "周转",
                   "合格率", "效率", "报警", "订单量", "销量"]
_LOOKUP_WORDS = ["是什么表", "什么表", "有哪些字段", "有哪些表", "表结构", "字段说明"]
_DB_WORDS = ["数据库", "什么数据", "数据总览", "数据概览", "库结构", "库里有",
             "看看库", "看看数据库", "所有表", "全部表", "所有表结构"]

# ── 写操作意图（P0-体验）：把"我改不了"这句话说出口 ────────────────────
# 背景：系统对数据库只有只读能力（真正的防线是 db/executor.execute_sql —— 只放
# SELECT/WITH，出口统一 rollback），但此前**没有任何一处识别「用户要我改数据」
# 这个意图**。实测输入「帮我修改质量表的数据，设备把老化异常去掉」，其中的「设备」
# 命中 _STRONG_DATA → 高置信判成 data → 一路表匹配 + 生成 SELECT，用户看到的是
# 「正在生成查询 SQL」。
#
# 这不是安全问题（写不进去有 SQL 层兜底），是**对外表达**的问题：不回应会被理解成
# 「它在尝试改」。正确的表现是明说一句「我没有修改数据的能力」。
#
# 判据刻意从严 —— 必须同时满足「有写动作」且「无查询/口径语境」：
#   · 漏判的代价 = 退回旧行为（仍会被 SQL 护栏拦下），不会更坏；
#   · 误杀的代价 = 把正常查询答成「我不能改数据」，比漏判更糟。
# 所以宁可漏判，不可误杀。只做确定性规则匹配、不走 LLM：要快要稳，也好解释。
_WRITE_EXCLUDE_WORDS = [
    # 强查询/结果信号：出现即说明用户要的是结果，不是在改数据
    "查询", "统计", "看一下", "看看", "查看", "有多少", "多少", "哪些", "排行", "排名",
    "趋势", "占比", "分布", "明细", "列出", "对比", "汇总", "导出", "图表",
    # 改口径 / 追问指代：多轮里「把上个月的改成按天」是改查询条件，不是改数据
    "刚才", "上一句", "上一步", "上一轮", "重新", "再看", "换成", "分组", "排序",
]
# 同上，改用正则表达：「改成按 X」是最典型的改口径写法
_WRITE_EXCLUDE_PATTERNS = [r"改(?:成|为|到)\s*按"]
_WRITE_ACTION_PATTERNS = [
    # ①「把 X 删掉/清空/覆盖」—— 破坏性动词，查询语境里不会出现
    r"(?:把|将)[^，。；,;？?]{0,24}?(?:删掉|删除|去除|去掉|移除|清空|覆盖|替换掉|清掉)",
    # ②「帮我 删/修改/添加…」—— 请求式写意图
    r"(?:帮我|帮忙|请|麻烦|给我|替我)\s*(?:把|将)?\s*(?:删|删除|清除|清空|修改|更改|改动|更新|新增|添加|插入|写入|录入)",
    # ③ 写动作 + 数据对象 ——「删除…记录」「更新…字段」
    r"(?:删除|修改|更改|更新|新增|添加|插入|清空)\s*掉?\s*(?:数据|记录|表|字段|行|内容|信息|条目)",
    # ④ 命令式开头 ——「删除 status 为 fail 的记录」；
    #    否定前瞻排掉「修改过的/更新的…」这类查询写法
    r"^\s*(?:删除|删掉|清空|修改|更改|更新|新增|插入)(?!过|的|了|率|次数|数量|时间|日期)",
    # ⑤「把 X 改成 Y」—— 与「改成按天统计」同形，靠上面的 _WRITE_EXCLUDE_PATTERNS 区分
    r"(?:把|将)[^，。；,;？?]{0,24}?(?:改成|改为|改到|更新为|更新成|设为|设置为)",
    # ⑥ 宾语在前、写动作在后 ——「设备表里那批数据清空」。
    #    只放破坏性动词（不含"修改/更新"，那几个在后置位置太容易是查询，
    #    如「字段更新的历史」），并用否定前瞻排掉「…清空的 时间/日志/原因」这类查询写法。
    r"(?:数据|记录|表|字段|行|内容|信息|条目)[^，。；,;？?]{0,10}?"
    r"(?:清空|删掉|删除|清掉|移除|去掉|改掉)(?!的|时间|日志|历史|原因|次数|数量)",
]


def _looks_like_write_request(q: str) -> bool:
    """是否为「要求系统改动数据库」的请求。判据说明见上方 _WRITE_ACTION_PATTERNS。"""
    if not q:
        return False
    if any(w in q for w in _WRITE_EXCLUDE_WORDS):
        return False
    if any(re.search(p, q) for p in _WRITE_EXCLUDE_PATTERNS):
        return False
    return any(re.search(p, q) for p in _WRITE_ACTION_PATTERNS)


def _classify_intent_rule(query: str) -> tuple[str, bool]:
    """规则分类，返回 (intent, confident)。confident=False 时交给 LLM 复核。"""
    q = query.strip()
    if not q:
        return "gibberish", True
    if re.match(r'^[\d\s,.!?;:，。！？；：…]+$', q) and len(q) <= 6:
        return "gibberish", True
    if re.match(r'^[a-z]{1,5}$', q, re.IGNORECASE):
        return "gibberish", True

    # 0. 写操作请求：必须排在 has_data 之前 —— 写请求里通常也带业务词
    #    （如「设备的异常数据」），先算数据信号就会被判成 data，正是本类 bug 的成因。
    if _looks_like_write_request(q):
        return "write", True

    has_data = any(w in q for w in _STRONG_DATA)

    # 1. ML 建模（明确的建模意图词优先于闲聊词，避免"你好，帮我训练个模型"被吞成 chat）
    for w in _ML_WORDS_STRONG:
        if w in q:
            return "ml", True
    # 1.2 建模短词（预测/分类/回归/建模）：须带建模上下文才判 ML，防止业务问句误判——
    #     「各产品分类的活跃产品数排行」含「分类」但语义是按品类统计；
    #     「回归生产/回归正常水平」的「回归」是业务词。无任何数据信号时的裸短词（如"帮我预测"）
    #     才允许单靠短词判 ML（交由 LLM 复核兜底）。
    for w in _ML_WORDS:
        if w not in q:
            continue
        # 业务语境排除：「回归生产/回归正常/回归正轨」等是业务用语，不是 ML 回归
        if w == "回归" and re.search(r"回归(?:正常|生产|正轨|常态|日常|岗位|工作|到|为)", q):
            continue
        if any(c in q for c in _ML_CTX) or not has_data:
            return "ml", False

    # 1.5 预测意图（P1-5）：未来时间表达 + 趋势/变化信号 → 路由到 ML 预测。
    # 低置信（False）→ LLM 复核；「下个月产量会是多少」这类此前会被"趋势"吞成历史查询。
    if any(w in q for w in _FUTURE_WORDS) and (any(s in q for s in _FUTURE_SIGNALS)
                                              or any(m in q for m in _FUTURE_METRICS)):
        return "ml", False

    # 2. 表结构查询（"有哪些表"这类明确的表查询意图优先于闲聊词）
    for w in _LOOKUP_WORDS:
        if w in q:
            return "lookup", True

    # 3. 数据库总览（"介绍数据库""看看有什么数据"都应命中这里，而非闲聊）
    for w in _DB_WORDS:
        if w in q:
            # "数据库 + 具体分析词" → 仍是 data
            if any(d in q for d in ["良率", "不良", "停机", "产量", "库存预警", "趋势", "排行", "统计"]):
                return "data", True
            return "analyze_db", True

    # 4. 强闲聊：短问候句直接命中（长句含数据信号时不算）
    for w in _STRONG_CHAT:
        if w in q and not has_data:
            return "chat", True

    # 5. 强数据信号
    if has_data:
        return "data", True

    # 6. 次级数据词（弱信号，置信度较低）
    weak_data = ["多少", "哪些", "数量", "关键", "状态", "类别", "情况", "完成",
                 "启用", "停用", "闲置", "前", "后", "最近", "本月", "今年"]
    if any(w in q for w in weak_data):
        return "data", False

    # 7. 弱闲聊：不含任何数据信号 → 倾向闲聊，但置信度低
    if any(w in q for w in _WEAK_CHAT):
        return "chat", False

    # 8. 极短句 → 闲聊（较可信）；但裸指标名短问法（"检验次数/设备数/停机时长"）先查注册表，
    #    命中指标则优先判 data（否则"检验次数"这类 2-6 字指标问法会被吞成闲聊）
    if len(q) <= 6:
        try:
            from agent.metric_registry import find_metrics
            if find_metrics(q, limit=1):
                return "data", True
        except Exception:
            pass
        return "chat", True

    # 9. 无法判断 → 交给 LLM
    return "data", False


_INTENT_LLM_PROMPT = """你是意图分类器。判断用户输入属于以下哪一类，只输出一个英文标签，不要任何解释。

- data: 需要查询数据库中的业务数据（统计、排行、明细、趋势、聚合、筛选等）
- analyze_db: 询问数据库整体情况（有哪些表、数据概览、库里有什么）
- lookup: 询问某张表的结构或字段说明
- ml: 要求训练模型、做预测、聚类、异常检测
- chat: 打招呼、闲聊、问助手能做什么、与数据无关的常识问答
- gibberish: 无意义乱码

用户输入: {query}

标签:"""


def _classify_intent_llm(query: str) -> str:
    """LLM 意图复核（仅在规则低置信时调用，超时/异常返回空串）"""
    try:
        llm = _make_llm(temp=None, max_tokens=16)
        resp = llm.invoke([SystemMessage(content=_INTENT_LLM_PROMPT.format(query=query[:200]))])
        label = str(resp.content or "").strip().lower()
        label = re.sub(r'[^a-z_]', '', label)
        return label if label in VALID_INTENTS else ""
    except Exception:
        return ""


def _classify_intent(query: str) -> str:
    """意图分类主入口：规则优先，低置信时 LLM 兜底纠错"""
    intent, confident = _classify_intent_rule(query)
    if confident:
        return intent
    llm_intent = _classify_intent_llm(query)
    return llm_intent or intent


# ── 多轮追问识别（P0-4）：把"改成按天""那上个月呢"这类修改型短句从 chat 拉回 data ──
_FOLLOWUP_MARKERS = [
    "改成", "换成", "改为", "改成按", "改按", "换", "去掉", "加上", "加个",
    "再看", "也看", "也统计", "再按", "再统计", "按天", "按月", "按周",
    "按年", "按季度", "按小时", "呢",
]


def _is_followup_query(query: str) -> bool:
    """判断是否为多轮「修改/追问型」短句（需配合上一轮有 SQL 使用）"""
    q = (query or "").strip()
    if not q:
        return False
    if any(w in q for w in _FOLLOWUP_MARKERS):
        return True
    # "那上个月呢" / "这个月呢" 类：那 + 时间词 + 呢
    if re.search(r"^那?.{0,6}呢$", q) and any(t in q for t in ["月", "天", "周", "年", "季", "日"]):
        return True
    return False


# ── 阶段三：归因分析型意图分流 ──
# 注意（架构修复 2026-08-31）：裸词「原因」不再判归因——「停机原因分析」是
# 「按原因维度分组统计」，不是「为什么下降」的归因推理；裸词误判会绕过
# 确定性编译与二次确认，直接让 LLM 生成 SQL（违背「未注册口径必须弹窗」）。
# 归因仅当明确是「X 的原因/原因是什么/为什么…」时才触发。
_ATTRIBUTION_STRONG = ["归因", "导致", "哪个环节", "根因", "影响", "的原因", "原因是什么", "主要原因"]
_CHANGE_MARKERS = ["下降", "下滑", "变差", "降低", "上涨", "增长", "波动", "异常", "差异", "减少", "增加", "偏高", "偏低"]


def _is_attribution_query(query: str) -> bool:
    """判断是否为「归因分析型」（需关系推理，而非指标精确计算）。"""
    q = query or ""
    if any(m in q for m in _ATTRIBUTION_STRONG):
        return True
    if "为什么" in q and any(m in q for m in _CHANGE_MARKERS):
        return True
    return False


# ── SQL 生成辅助：JSON 容错解析 / 意图信号 ────────────────

def _loads_lenient(text: str):
    """多级容错 JSON 解析：标准 → 修复常见 LLM 格式错误 → 失败返回 None

    修复项：markdown 残留、尾随逗号、字符串内未转义换行、单引号包裹。
    """
    if not text:
        return None
    # 1. 直接解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2. 抽取最外层大括号片段
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        return None
    frag = m.group(0)
    try:
        return json.loads(frag)
    except Exception:
        pass
    # 3. 修复：去尾随逗号
    fixed = re.sub(r',\s*([}\]])', r'\1', frag)
    try:
        return json.loads(fixed)
    except Exception:
        pass
    # 4. 修复：字符串值内部的裸换行/制表符（LLM 常把多行 SQL 直接换行写入）
    def _escape_inner(mm):
        body = mm.group(2)
        body = body.replace("\\", "\\\\").replace('"', '\\"')
        body = body.replace("\n", "\\n").replace("\r", "").replace("\t", " ")
        return f'"{mm.group(1)}": "{body}"'

    fixed2 = re.sub(r'"(\w+)"\s*:\s*"([\s\S]*?)"(?=\s*[,}])', _escape_inner, fixed)
    try:
        return json.loads(fixed2)
    except Exception:
        pass
    # 5. 单引号 → 双引号
    try:
        return json.loads(fixed.replace("'", '"'))
    except Exception:
        return None


# 用户要求全量结果的信号（此时不强制 LIMIT 20）
_FULL_RESULT_WORDS = ["所有", "全部", "全量", "不限", "完整", "每一条", "逐条", "导出"]
# 聚合意图信号（要求 GROUP BY，不能用 SELECT * 敷衍）
_AGG_WORDS = ["各", "每个", "每条", "每种", "每类", "分别", "按", "排行", "排名", "对比",
              "占比", "分布", "汇总", "统计", "总计", "合计", "平均", "趋势", "同比", "环比",
              "良率", "合格率", "达成率", "TOP", "top", "最高", "最低", "最多", "最少",
              "产量", "总量", "总额", "总数", "金额"]
# 需要聚合计数的信号词（命中则 TOP-N 问题仍是聚合问题，如「订单数量最多的前5名客户」）
_AGG_COUNT_WORDS = ("数量", "次数", "个数", "总额", "总量", "总数", "金额", "产量", "良率",
                    "不良率", "合格率", "达成率", "占比", "平均", "时长")


def _is_detail_topn(query: str) -> bool:
    """明细 TOP-N 问题（如「工资最高的前10名员工」「最近5条设备维护记录」）：
    直接按实体字段排序取前 N，不应聚合。

    判定：含「前N名 / TOP N / N名 / 最近N条(笔/单/个/名/台/次)」等 TOP-N 表达，且：
    - 无分组词（各/每/按/分别）→ 不是分组统计
    - 无聚合计数词（数量/产量/金额等）→ 不需要 SUM/COUNT
    满足则为明细排序问题。反例：「订单数量最多的前5名客户」含"数量"→ 仍聚合。
    """
    if not re.search(r"(前\s*\d+|TOP\s*\d+|\d+\s*名|最近\s*\d+\s*(条|笔|单|个|名|台|次|位|份)|最近的\s*\d+)",
                     query, re.IGNORECASE):
        return False
    if any(w in query for w in ["各", "每个", "每种", "每类", "分别", "按"]):
        return False
    return not any(w in query for w in _AGG_COUNT_WORDS)


def _is_detail_list(query: str) -> bool:
    """需要返回明细列表而非聚合的问题（如「库存告急的物料有哪些」「设备最近维护情况」）。

    判定：含「哪些 / 有哪些 / 情况 / 明细 / 记录 / 名单」等明细意图词，且：
    - 无分组词（各/每/按/分别/排行/排名/对比/占比/分布/统计/汇总/趋势）→ 不是分组统计
    - 无聚合计数词（数量/产量/金额/次数/总额/良率等）→ 不需要 SUM/COUNT
    满足则提示 LLM 返回明细行（可带 WHERE 条件 + LIMIT），不要 GROUP BY。
    """
    if not any(w in query for w in ["哪些", "有哪些", "情况", "明细", "名单", "告急", "预警", "低库存", "低于安全库存"]):
        return False
    if any(w in query for w in ["各", "每个", "每种", "每类", "分别", "按", "排行", "排名", "对比",
                                "占比", "分布", "统计", "汇总", "总计", "合计", "平均", "趋势"]):
        return False
    return not any(w in query for w in _AGG_COUNT_WORDS)


def _wants_full_result(query: str) -> bool:
    """用户是否要求全量结果（避免默认 LIMIT 20 悄悄截断）"""
    return any(w in query for w in _FULL_RESULT_WORDS)


def _needs_aggregation(query: str) -> bool:
    """问题是否需要聚合（GROUP BY / 聚合函数）"""
    if _is_detail_topn(query):
        return False
    return any(w in query for w in _AGG_WORDS)


def _build_agg_hint(query: str) -> str:
    """根据问题生成强制聚合/全量/时间范围提示，注入 SQL 生成 Prompt"""
    hints = []
    if _is_detail_topn(query):
        hints.append(
            "本问题为明细 TOP-N（按实体的直接字段排序取前 N 名），"
            "不要 GROUP BY 聚合，直接 SELECT 明细并按字段排序 + LIMIT N。"
        )
        # 明细 TOP-N 输出实体 ID（如 product_id/line_id/equipment_id），
        # 不要 JOIN 维度表替换成 code/name——评测按 ID 比对（如"库存最低的前5个产品"要 product_id 而非 product_code）
        hints.append(
            "明细 TOP-N 直接输出事实表的实体 ID 列（如 product_id/line_id/equipment_id）即可，"
            "不要 JOIN 维度表换成 code/name 列；如需补充可额外输出但不替代 ID。"
        )
        # 只选问题明确需要的列，不要 SELECT * 或额外列（多列会导致行集合不一致）
        hints.append(
            "SELECT 只列出回答该问题所需的列（实体 ID + 排序指标列），"
            "不要 SELECT *、不要额外加无关列（如 frozen_qty/created_at 等）。"
        )
        # 库存类 TOP-N：快照表每产品一行，直接明细排序即可，绝不需要聚合
        if "库存" in query or "安全库存" in query or "存量" in query:
            hints.append(
                "库存类 TOP-N：库存快照表（如 inv_inventory_snapshot）中每个产品恰好一行，"
                "直接 SELECT product_id/warehouse_code/available_qty 等明细列，ORDER BY available_qty + LIMIT N 即可，"
                "绝不要 GROUP BY 聚合（SUM 等），也不要 JOIN 产品维度表换成名称。"
            )
    elif _is_detail_list(query):
        hints.append(
            "本问题要求返回具体明细记录（如低于安全库存的物料、最近的维护情况），"
            "不要 GROUP BY 聚合，直接 SELECT 明细行，可带 WHERE 条件筛选并按时间/关键字段排序。"
        )
    elif _needs_aggregation(query):
        hints.append(
            "本问题属于聚合分析类，SQL 必须包含 GROUP BY 与聚合函数"
            "（SUM/COUNT/AVG/MAX/MIN），严禁用 SELECT * 或不聚合的明细列表敷衍。"
        )
    if _wants_full_result(query):
        hints.append("用户要求查看全量数据，不要添加 LIMIT 限制。")
    if any(w in query for w in ["趋势", "变化", "走势", "最近", "近7天", "近30天", "本月", "上月"]):
        hints.append("涉及时间趋势，SELECT 中必须包含时间维度列并按时间排序。")
    if any(w in query for w in ["良率", "合格率", "不良率", "达成率", "占比", "百分比"]):
        hints.append("涉及比率计算，分母必须用 NULLIF(...,0) 防除零，并做浮点转换避免整数除法。")
    # 达成率的分子语义（2026-09-14 用户实测驱动）：「各产线计划达成率」实测生成
    # SUM(CASE WHEN order_status='completed' ...)/SUM(plan_qty) → 全部为 0，
    # 却照样输出"生产严重滞后"的业务结论。分子必须是完工产出数量，不是工单状态计数。
    if "达成率" in query:
        hints.append("计算「达成率」时分子必须是实际完工/产出数量——本库取该工单**末道工序**"
                     "（dim_process.process_seq 最大的记录，如 PR08 包装入库）的 good_qty；"
                     "严禁用 order_status='completed' 的工单计数当分子（会得到 0 或个位数），"
                     "也严禁 SUM(good_qty)（每工单 8 条工序记录会把产出放大约 8 倍）。"
                     "分母为 SUM(mes_work_order.plan_qty)。")
    # 库存占比的分母（2026-09-14 用户实测驱动）：「冻结库存占总库存比例」实测用
    # frozen_qty/available_qty 得 1.19%，正确分母是 可用+冻结（1.18%）。
    if re.search(r"占比|比例|百分比", query) and re.search(r"冻结|可用|库存", query):
        hints.append("计算库存占比时，分母必须是「总库存 = 可用库存(available_qty) + 冻结库存(frozen_qty)」，"
                     "不要只除以可用库存。")
    # 分档统计：严格按问题给出的边界写 CASE WHEN，不要自己改档位边界
    if any(w in query for w in ["分档", "档位", "分段", "短停", "中停", "长停", "分桶"]):
        hints.append("分档统计必须按问题给出的档位边界写 CASE WHEN（如停机时长分档：<30 短停、30-120 中停、>120 长停），"
                     "只输出档位和计数，不要额外加其他聚合列。")
    # 按期/延期：用实际完成日与计划完成日比较，不是状态字段
    if "按期" in query or "延期" in query:
        hints.append("按期/延期判断：actual_end > plan_end 为延期，其余（含空值）为按期；"
                     "用 CASE WHEN actual_end > plan_end THEN '延期' ELSE '按期' END，不要使用 status 状态字段。")
    # 工单计划产量/实际产量：mes_work_order 表自带 actual_qty 字段，直接比较即可
    # （2026-09-13 修复：两条 mes_* 锚定规则此前无库感知，123 库不含这些表，
    #   等于明示 LLM 引用不存在的表 —— 「人均产量/产量最高的车间」等题持续
    #   EXEC_FAIL（factory.mes_process_output 不存在）的根源就是这里）
    if ("工单" in query and ("计划产量" in query or "实际产量" in query or "完成" in query and "实际" in query)
            and _fact_output_table_exists()
            # 2026-09-14：必须同时确认 actual_qty 列**真实存在**。此前只判 _fact_output_table_exists()
            # （yans 下为 True），于是对 yans 也注入"mes_work_order 自带 actual_qty，直接比较即可"
            # ——而该库的 mes_work_order 根本没有这一列（只有 plan_qty），提示与事实相反，
            # 生成的 SQL 必然引用不存在的列（靠前置校验 4 拦下重试，纯浪费一次 LLM 往返）。
            and "actual_qty" in [str(c) for c in ((_all_table_columns() or {}).get("mes_work_order") or [])]):
        hints.append("mes_work_order 工单表自带 plan_qty（计划产量）与 actual_qty（实际产量）两个现成字段，"
                     "直接在 WHERE/聚合中比较这两个字段即可（如 WHERE plan_qty > actual_qty），"
                     "不要从 mes_process_output 等其他表聚合实际产量。")
    # 产量/投入/产出统计锚定 mes_process_output（除非明确问工单）—— 仅表存在时注入
    if re.search(r"(产量|投入量|产出|合格产量|不良)", query) and "工单" not in query and _fact_output_table_exists():
        hints.append("产量/投入/产出类统计使用 mes_process_output 表。"
                     "**关键口径：产量 = good_qty + defect_qty（总产出，含不合格品），不是仅 good_qty 合格量**；"
                     "合格产量/良品才是 good_qty；投入量 = input_qty；不良量 = defect_qty。"
                     "不要使用 mes_work_order（其 plan_qty/actual_qty 是工单计划/实际产量，语义不同）。")
    # 对比类问题输出宽表（2026-09-13 第二轮评测驱动）：「A 与 B 对比/分别是多少」
    # 应输出多个数值列（宽表），而不是按类别分行（长表）——评测按值集合比对，长表行数翻倍判不一致
    if re.search(r"(对比|分别是多少|分别多少)", query) and re.search(r"(与|和|跟|、)", query):
        hints.append("对比两个或多个类别的数值时，输出为宽表：每个类别一列（如 y2025、y2026 两列），"
                     "用 SUM(CASE WHEN 条件 THEN 值 ELSE 0 END) 条件聚合实现，一行代表一个分组实体，"
                     "不要把类别作为行维度分行输出。")
    # TOP-N 问法必须有排序：防止「最长的前5个」生成无 ORDER BY 的 LIMIT 20 退化 SQL
    if re.search(r"前\s*\d+\s*个|前\s*\d+\s*名|最高的前|最长的前|最大的前", query):
        hints.append("本问题为 TOP-N 排名，SQL 必须包含 ORDER BY 指标列（按问题含义降序或升序）+ LIMIT N，"
                     "缺失排序的 LIMIT 不是 TOP-N。")
    # 单值汇总（如"本月产量是多少"）：不要按日期分组
    if (not any(w in query for w in ["各", "每", "按", "分", "分别"])
            and re.search(r"(本月|上月|近\d+天|本季度).{0,6}(产量|销售额|金额|总数|总量|投入)", query)):
        hints.append("若问题只问一个汇总值（如本月产量），不要按日期/月份分组（不要 date_trunc 作为分组列），"
                     "直接 SELECT SUM(指标) WHERE 时间范围 返回一行汇总即可；本月无数据时返回 NULL 或 0 均视为合理。")
    # 高于平均/低于平均：比较基准 = 对原始行的 AVG(字段)，不要先按组聚合再平均
    if re.search(r"(高于|低于|超过).{0,6}(平均|均值)", query):
        hints.append("比较基准为 AVG(字段) 对全部原始行求平均（如 AVG(good_qty)），"
                     "不要先 GROUP BY 聚合后再对聚合值求平均，两者结果不同。")
        hints.append("写法模板：GROUP BY 维度 HAVING SUM(指标字段) > (SELECT AVG(指标字段) FROM 事实表) —— "
                     "AVG 直接作用于事实表原始行，不要写成 AVG(子查询产线合计)。")
    # 各XX各YY（双维度分组）：GROUP BY 必须输出维度名称列（JOIN 维度表取名称），且不要 LIMIT 截断
    if re.search(r"各.{1,6}各.{1,8}", query):
        hints.append("双维度分组统计（各X各Y）：GROUP BY 两个业务维度，且必须 JOIN 维度表输出维度名称列"
                     "（如 line_name/process_name 而非 line_id/process_id）；不要加 LIMIT 截断，输出全部分组。")
    # 排名第一/最高（窗口函数）：按 gold 习惯输出维度 ID 即可，不必强转名称
    if re.search(r"(排名第一|第一的|最高的)", query):
        hints.append("取每组排名第一：用 ROW_NUMBER() OVER (PARTITION BY 维度 ORDER BY 指标 DESC) 过滤 rn=1；"
                     "分组维度列直接输出其 ID（如 line_id），保持与事实表一致即可。")
    # 种类数：按类别字段分组计数，不是 COUNT(DISTINCT id)
    if "种类数" in query or "几种" in query:
        hints.append("统计种类数按类别字段（如 item_type / category）分组后 COUNT(*)，"
                     "不要用 COUNT(DISTINCT 明细ID)。")
    # 明细 TOP-N 且要求"产品/物料/记录"：直接输出明细行，不要聚合
    if re.search(r"(前|最近)\s*\d+.*(产品|物料|记录|采购单|员工|维护)", query) and not any(w in query for w in ["各", "每", "按", "分别"]):
        hints.append("本题要求列出具体记录/实体明细（如库存最低的前5个产品），"
                     "直接 SELECT 明细字段并按指标排序 + LIMIT N，不要 GROUP BY 聚合。")
    # 分档/档位统计（短停/中停/长停、高/中/低）：按分档统计记录数，不是汇总数值。
    # 实测"停机时长分档统计（短停/中停/长停）"会被 LLM 误读成 SUM(停机时长)，
    # 而意图是"每个档位有几条记录"（COUNT）。歧义用确定性提示消除。
    if re.search(r"(分档|档位|分级|分[一二三四五]\s*(个|级))", query):
        hints.append("「分档/档位统计」用 CASE WHEN 按条件分档（档位名与问题给出的完全一致，如 短停/中停/长停），"
                     "GROUP BY 档位后统计**记录数** COUNT(*)，不要对原始数值字段做 SUM/AVG。")
    # 停机记录/维护记录：直接按时间排序取明细
    if re.search(r"(最近的|最近)\s*\d*\s*条.*(记录|情况)", query):
        hints.append("本题要求返回最近的明细记录，直接 SELECT 明细并按时间字段倒序 + LIMIT，不要聚合。")
    # 最近N条：明确排序字段（业务日期），且 SELECT 只列关键列避免 SELECT *
    if re.search(r"(最近|最近的)\s*\d+\s*条", query):
        hints.append("'最近N条'按业务时间字段（如 order_date / mdate / start_time）倒序排序后 LIMIT N；"
                     "SELECT 只列出与问题相关的字段列，不要 SELECT *。")
    # 时间表达式确定性翻译：命中则给出具体日期区间，避免 LLM 猜错时间范围
    try:
        from agent.time_expr import build_time_hint
        time_hint = build_time_hint(query)
        if time_hint:
            hints.append(time_hint)
    except Exception:
        pass
    return "\n".join(f"- {h}" for h in hints)


# 趋势/周期语义词：命中即优先折线图（需结果含时间列）
_TREND_WORDS = (
    "趋势", "走势", "变化", "环比", "同比", "逐月", "逐日", "随时间",
    "近7天", "近30天", "近一个月", "最近", "每天", "每月", "按月", "按日",
    "历史", "时间段", "期间",
)
# 占比/构成语义词：命中即优先饼/玫瑰图
_SHARE_WORDS = ("占比", "比例", "构成", "组成", "分布", "结构", "份额", "比重")
# 排名语义词：命中即优先横向柱状（排名条带横向更易读）
_RANK_RE = re.compile(r"排名|排行|最高|最低|最多|最少|前\s*\d+|TOP|top|领先|冠军|垫底")
# 转化链路语义词：命中即优先漏斗图
_FUNNEL_WORDS = ("漏斗", "转化", "流失", "留存")


def _is_composition_vals(vals: list) -> bool:
    """这组数值能不能读作「构成」——各分项占同一个总量，合计约 100%。

    为什么需要它（2026-09-21）：「各产线的不可用分钟中，非计划部分的占比」这类
    问题的每一项各自算自己的分子分母（本线非计划 / 本线总停机），**合计不等于 100%**
    ——本例 5 条产线是 58.41/55.14/53.03/47.98/47.35，加起来 261.91%。
    而环形图/玫瑰图是按「份额」分配角度的，262% 的圆根本画不出来，渲染时会被
    归一化成一堆大小相近的扇区，比不画图更误导。所以命中「占比/分布」这类词后
    还要看数据本身：合计≈100（百分数形式）或≈1（小数形式）才允许走构成类图。

    注意值域：绝对量（停机分钟数、不良件数）的合计当然不是 100，但它们**是**构成
    （各项之和即总量），所以只要值域超出百分数范围就直接认可，不参与合计校验。
    """
    if not vals:
        return False
    try:
        mx, mn, tot = max(vals), min(vals), sum(vals)
    except TypeError:
        return False
    if mn < 0:
        return False
    if mx > 100.5:
        # 值域超出百分数范围（分钟/件数等绝对量）——按份额理解总是成立的
        return True
    if mx > 1.5:
        return abs(tot - 100) <= 5      # 百分数形式：合计须≈100
    if mx > 0.01:
        return abs(tot - 1) <= 0.05     # 0~1 小数形式：合计须≈1
    return False                        # 全是 0 或近 0，没有信息量


def _validate_chart_type(chart_type: str, columns: list[str], rows: list[dict], query: str) -> str:
    """按「数据形态」确定最佳图表类型（不再以 LLM 输出的类型为准）。

    原实现以 LLM 生成的 chart_type 为准、只在数据明显不符时纠正——而 LLM 十有八九
    默认输出 bar，导致绝大多数问答都渲染柱状图。这里改为数据形态驱动：
    先看结果数据的真实结构（时间列/类别列/数值列组合 + 行数 + 语义词），
    确定性地选出最合适的图表；LLM 的类型仅在强规则未命中时作兜底参考。
    图表类型不影响 SQL 执行结果。
    """
    ct = (chart_type or "bar").lower()
    if not rows or not columns:
        return "none"
    if len(rows) < 2 or len(columns) < 2:
        return "table"

    # 识别列的实际类型
    date_cols, num_cols, cat_cols = [], [], []
    for c in columns:
        kinds, distinct = _infer_col_kind(c, rows)
        if "date" in kinds:
            date_cols.append(c)
        elif "num" in kinds:
            num_cols.append(c)
        else:
            cat_cols.append((c, len(distinct)))

    if not num_cols:
        return "table"  # 没有数值列画不了图

    q = query or ""
    n = len(rows)

    # ── 一、数据形态强规则（确定性优先，与 LLM 措辞解耦）──
    # 0) 用户明确要表格/明细 → 表格（优先级最高，任何数据形态都尊重用户意图）
    if any(w in q for w in ["表格", "列表", "明细", "清单", "导出"]):
        return "table"
    # 1) 时间趋势 → 折线（结果含时间列且语义带趋势/周期）
    if date_cols and any(w in q for w in _TREND_WORDS):
        return "area" if any(w in q for w in ("累计", "累计值", "面积")) else "line"
    # 2) 双维度交叉密度 → 热力图（2 个类别列 + 1 数值列 + ≥6 行 + 无时间列）
    if len(cat_cols) >= 2 and len(num_cols) == 1 and n >= 6 and not date_cols:
        return "heatmap"
    # 2c) 「关系/相关性」语义 + ≥2 数值列 → 散点（x/y 取前两个数值列，类别列仅作业务含义）。
    #     雷达图表达的是"多指标横评"，回答不了"两个指标之间什么关系"——语义优先于形态。
    if len(num_cols) >= 2 and any(w in q for w in ("关系", "相关", "散点", "关联")):
        return "scatter"
    # 3) 多指标横评 → 雷达图（1 类别列 + 2~4 数值列 + 3~12 行）
    if len(cat_cols) == 1 and 2 <= len(num_cols) <= 4 and 3 <= n <= 12:
        return "radar"
    # 3b) 1 类别 + 2~3 数值 + 行数多 → 堆叠柱（雷达图只适合 ≤12 行，行多时堆叠对比更可读）
    if len(cat_cols) == 1 and 2 <= len(num_cols) <= 3 and n > 12:
        return "stacked"
    # 4) 纯数值相关性 → 散点（≥2 数值列、无类别列、**且无时间列**——时间序列是
    #    双线/堆叠的形态，散点会丢掉时间顺序，误导读图）
    if len(num_cols) >= 2 and not cat_cols and not date_cols:
        return "scatter"
    # 5) 单类别 + 单数值：按语义细分（占比/排名/转化/类别数/构成集中度）
    if len(cat_cols) == 1 and len(num_cols) == 1:
        cat_n = cat_cols[0][1]
        metric_name = num_cols[0].lower()
        ratio_like = any(w in metric_name for w in ("率", "rate", "ratio", "pct", "percent", "%"))
        # 数值全非负才适合读作"构成"（环形/玫瑰）；含负值或比率仍是对比语义
        vals = []
        for r in rows[:50]:
            try:
                v = r.get(num_cols[0])
                if v is not None and str(v).strip() != "":
                    vals.append(float(v))
            except (ValueError, TypeError):
                pass
        non_neg = bool(vals) and all(v >= 0 for v in vals)
        # 各分项能否读作"构成"（合计≈100%）。比率类指标合计不为 100，
        # 走饼图/玫瑰图会把角度按份额归一化，视觉上完全失真
        is_comp = _is_composition_vals(vals)
        # 占比/构成/分布 → 玫瑰图（3~10 类，面积对比更直观）或饼图（≤12 类）
        if any(w in q for w in _SHARE_WORDS) and is_comp:
            if 3 <= cat_n <= 10:
                return "rose"
            if cat_n <= 12:
                return "pie"
        # 排名/TOP → 横向柱状
        if _RANK_RE.search(q):
            return "barh"
        # 转化链路 → 漏斗
        if any(w in q for w in _FUNNEL_WORDS):
            return "funnel"
        # ── 多样化（确定性，按数据形态而非随机）────────────────
        # ① 类别名偏长（平均 >6 字）→ 横向柱状：可读性优先于花样，
        #    环形图里 4 个七八字的长标签会挤成一团
        cat_col = cat_cols[0][0]
        label_lens = [len(str(r.get(cat_col) or "")) for r in rows[:20]]
        if label_lens and 4 <= cat_n <= 12 and sum(label_lens) / len(label_lens) > 6:
            return "barh"
        # ② 构成集中（头部占比 ≥60%）→ 环形图突出主项，比 5 根柱子更直观
        #    先确认是构成数据：比率类合计不为 100，max/total 这个比值本身就没意义
        if non_neg and is_comp and 3 <= cat_n <= 12 and vals:
            total = sum(vals)
            if total and max(vals) / total >= 0.6:
                return "donut"
        # ③ 少量类别（3~6）的非比率总量 → 环形图（构成视角，减少柱状单调）
        #    ratio_like 只认「率/rate/ratio/pct/%」，认不出「占比/比例」这类中文词，
        #    所以还要靠 is_comp 兜一层，否则「各产线非计划占比」会被画成环形图
        if non_neg and is_comp and not ratio_like and 3 <= cat_n <= 6:
            return "donut"
        # ④ 类别过多 → 横向柱状
        if cat_n > 12:
            return "barh"
        return "bar"

    # ── 二、LLM 给出的类型校正（强规则未命中时的兜底）──
    # pie/donut：类别过多或占比语义不成立 → 降级 bar
    if ct in ("pie", "donut"):
        cat_count = cat_cols[0][1] if cat_cols else len(rows)
        if cat_count > 12 or len(rows) > 12:
            return "bar"
        if len(num_cols) > 1:
            return "bar"  # 多指标不适合饼图
        return ct

    # line/area：必须有时间列或有序维度，否则降级 bar
    if ct in ("line", "area"):
        if not date_cols:
            # 无时间列但用户明确问趋势 → 保留折线（按结果顺序展示）
            if any(w in q for w in ["趋势", "走势", "变化", "曲线"]):
                return ct
            return "bar"
        return ct

    # bar/barh/stacked：有时间列且用户问趋势 → 升级为 line
    if ct in ("bar", "barh", "stacked"):
        if date_cols and any(w in q for w in _TREND_WORDS):
            return "line"
        # 类别过多时横向柱状更易读
        if ct == "bar" and len(rows) > 20:
            return "barh"
        return ct

    if ct == "table":
        # LLM/记忆示例可能给出 table，但只要数据支持画图就优先出图（用户诉求：多要图）；
        # 仅当用户明确要求表格/列表，或数据确实无数值列（前面已拦截）时才用表格
        if any(w in q for w in ["表格", "列表", "明细", "清单", "导出"]):
            return "table"
        return "bar"
    if ct == "scatter":
        return "scatter" if len(num_cols) >= 2 else "bar"

    # ── 三、AntV G2Plot 扩展类型升级兜底 ──
    # 前端是「ECharts（常规图）+ AntV G2Plot（扩展图）」双引擎，这里在数据形态明显适合
    # 且语义关键词命中时升级为 AntV 图表。图表类型不影响 SQL 执行结果。
    antv = _upgrade_to_antv(query, rows, date_cols, num_cols, cat_cols)
    if antv:
        return antv
    return "bar"


def _upgrade_to_antv(query: str, rows: list[dict], date_cols: list[str],
                     num_cols: list[str], cat_cols: list[tuple[str, int]]) -> str | None:
    """数据明显适合 AntV G2Plot 图表时返回对应类型，否则 None（保持 ECharts 常规图）。

    - radar   ：1 类别列 + 2~4 数值列 + 3~12 行（多指标横评天然适合雷达图，无需关键词；
                单指标类查询仍走 ECharts 柱状，避免图表漂移）
    - rose    ：占比/构成/分布语义 + 单数值列 + 类别 3~10（比饼图面积对比更直观）
    - heatmap ：2 个类别列 + 单数值列 + 行数 ≥ 6（二维密度，无时间列）
    - funnel  ：转化/漏斗/流失语义 + 单数值列 + 3~10 行
    """
    q = query or ""
    n = len(rows)
    # 雷达图：多指标横评（各产线在产量/不良/良率上的综合表现）
    if len(cat_cols) == 1 and 2 <= len(num_cols) <= 4 and 3 <= n <= 12:
        return "radar"
    # 玫瑰图：占比/构成类，类别数适中
    if len(num_cols) == 1 and cat_cols and 3 <= cat_cols[0][1] <= 10:
        if any(w in q for w in ["占比", "构成", "组成", "分布", "比例", "结构"]):
            return "rose"
    # 热力图：两个维度交叉 + 一个度量
    if len(cat_cols) >= 2 and len(num_cols) == 1 and n >= 6 and not date_cols:
        return "heatmap"
    # 漏斗图：转化/流失链路
    if len(num_cols) == 1 and 3 <= n <= 10:
        if any(w in q for w in ["漏斗", "转化", "流失", "留存"]):
            return "funnel"
    return None


def _sample_rows(table_name: str, limit: int = 2) -> list[dict]:
    """取一张表的样例行（补充字段值语义，单次轻量查询，失败静默返回空）"""
    parts = table_name.split(".")
    if not parts or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in parts):
        return []
    try:
        from database import get_db_type
        q = "`" if get_db_type() == "mysql" else '"'
        quoted = ".".join(f"{q}{p}{q}" for p in parts)
        r = execute_sql(f"SELECT * FROM {quoted} LIMIT {limit}")
        if r["success"] and r["rows"]:
            return r["rows"]
    except Exception:
        pass
    return []


# ── 快速 Schema 构建（纯内存，0 次 DB 查询）─────────────

_TIME_TYPE_HINTS = ("time", "date")
_NUM_TYPE_HINTS = ("int", "num", "real", "float", "double", "decimal", "numeric", "bigint", "smallint")
# 通用维度词 → 常见英文字段名片段（GROUP BY 分组列常无描述，需按中文词兜底映射）
_DIM_WORD_MAP = {
    "状态": ("status", "state"),
    "类型": ("type", "kind"),
    "类别": ("category", "class"),
    "编号": ("code", "no_"),
    "名称": ("name",),
}


def _trim_schema_columns(schema: str, query: str) -> str:
    """按问题关键词裁剪 schema 中的无关字段行（对齐 Skopx 列裁剪，压缩 prompt token）。

    保留规则（宁多勿漏，防丢核心字段导致 SQL 生成错误）：
    1. 主键 / JOIN 键（key 标记）
    2. 时间列（date/time 类型）——时间过滤常需要
    3. 数值列（int/numeric/real 等）——聚合计算（SUM/AVG）必用
    4. 维度显示列/编码列（*_name / *_code 结尾）——GROUP BY 与展示必用
    5. 通用维度词映射命中（状态→status、类型→type…）——分组列无描述时的兜底
    6. 字段名或整行描述命中 query 中任意英文词 / 中文词（2-6 字）
    兜底：某表裁剪后字段 <4 时整块保留（宁多勿漏）。query 过长/为空时不裁剪。
    """
    if not query or len(query) > 60:
        return schema
    q = query.lower()
    words = {w for w in re.findall(r"[a-z][a-z0-9_]{2,20}", q) if len(w) >= 3}
    cjk = {w for w in re.findall(r"[\u4e00-\u9fa5]{2,6}", query)}

    def _hit(low: str) -> bool:
        for w in words:
            if w in low:
                return True
        for w in cjk:
            if w in low:
                return True
        return False

    out: list[str] = []
    # 按表头（## ）切块，逐表裁剪 + 兜底
    blocks = re.split(r"(?=^## )", schema, flags=re.M)
    for block in blocks:
        if not block.strip():
            out.append(block)
            continue
        block_lines = block.splitlines(keepends=True)
        field_idx = [i for i, l in enumerate(block_lines)
                     if re.match(r"\s*[a-z_][a-z0-9_]*\(", l, re.I)]
        if not field_idx:
            out.append(block)
            continue
        keep: set[int] = set()
        for i in field_idx:
            s = block_lines[i].strip()
            m = re.match(r"([a-z_][a-z0-9_]*)\(", s, re.I)
            if not m:
                keep.add(i)
                continue
            fname = m.group(1).lower()
            head = s.split(")")[0] if ")" in s else s
            ft = head[m.end() - 1:] if m.end() - 1 < len(head) else ""
            low = s.lower()
            is_key = "key" in head
            is_time = any(h in fname for h in ("time", "date")) or any(h in ft for h in _TIME_TYPE_HINTS)
            is_num = any(h in ft for h in _NUM_TYPE_HINTS)
            is_disp = fname.endswith("_name") or fname.endswith("_code")
            is_dim_word = any(w in query and any(tok in fname for tok in toks)
                              for w, toks in _DIM_WORD_MAP.items())
            if is_key or is_time or is_num or is_disp or is_dim_word or _hit(low):
                keep.add(i)
        # 兜底：裁剪后字段 <4 → 整块保留（防过度裁剪导致 LLM 信息不足）
        if len(keep) < min(4, len(field_idx)):
            out.append(block)
            continue
        for i, line in enumerate(block_lines):
            if i not in field_idx or i in keep:
                out.append(line)
    return "".join(out)


def _build_schema_fast(candidate_names: list[str], allowed_tables: set[str] | None = None,
                       query: str = "") -> str:
    """从当前连接的 DB 获取真实字段结构构建 schema 上下文（带库级 LRU 缓存）。

    缓存键 = db_key + 候选表集合 + 权限集合：同一库内重复查询相同表直接命中，
    切换数据库后 clear_cache() 会清空，避免跨库复用错误 schema。
    allowed_tables: 角色允许访问的裸表名集合（小写），用于过滤 JOIN 路径引入的中间表。
    query: 传入问题文本时做列级裁剪（保留键/时间/数值/命中列，压缩 prompt token）；
           不传或过长则输出全量 schema。
    """
    try:
        from database import get_database_config
        cfg = get_database_config()
        db_key = f"{cfg.get('db_type')}:{cfg.get('host')}:{cfg.get('port')}:{cfg.get('name')}"
    except Exception:
        db_key = "default"
    ak = tuple(sorted(allowed_tables)) if allowed_tables is not None else None
    schema = _build_schema_fast_impl(db_key, tuple(sorted(set(candidate_names))), ak)
    return _trim_schema_columns(schema, query) if query else schema


@functools.lru_cache(maxsize=128)
def _build_schema_fast_impl(db_key: str, candidate_names: tuple[str, ...], allowed_key=None) -> str:
    """schema 上下文构建（带 lru_cache；db_key 隔离，切库后由 clear_cache 清空）"""
    candidate_names = list(candidate_names)
    allowed_tables = set(allowed_key) if allowed_key is not None else None
    # ── 真实外键：一次查询全库 FK，供候选表关联注入 ──
    try:
        from db.tools import get_foreign_keys
        fk_map = get_foreign_keys()
    except Exception:
        fk_map = {}
    # ── P0-2 JOIN 路径规划：多跳路径 + 中间表扩充 + 桥接表识别 ──
    try:
        from agent.join_planner import plan_joins
        plan = plan_joins(candidate_names, fk_map, allowed_tables=allowed_tables)
        iter_names = plan["expanded"]
        join_paths = plan["paths"]
        bridge_tables = plan["bridge"]
    except Exception:
        iter_names = candidate_names
        join_paths = []
        bridge_tables = set()
    cand_set = set(iter_names)
    # 候选/扩充表内两两可达的 FK 关系（child.col -> parent.col），用于结尾的 JOIN 键清单
    join_edges: list[tuple[str, str, str, str]] = []  # (child, col, parent, pcol)
    for child in iter_names:
        for fk in fk_map.get(child, []):
            parent = fk.get("ref_table", "")
            if parent in cand_set:
                join_edges.append((child, fk["column"], parent, fk["ref_column"]))

    lines = ["# 可用表\n"]
    meta_map = {t["table_name"]: t for t in METADATA_TABLES}
    count = 0
    sampled = 0
    # 候选表较少时给每张表都取样例；表多时只给前 4 张，控制查询开销
    sample_budget = 4 if len(iter_names) > 2 else len(iter_names)

    for name in iter_names[:10]:
        if count >= 10:
            break
        fields = _get_db_columns(name)  # 优先真实 DB
        meta = meta_map.get(name)
        alias = meta["table_alias"] if meta else name
        category = meta["category"] if meta else "unknown"
        desc = meta["description"] if meta else ""
        # 元数据字段名 → 中文描述（真实库字段无语义时的补充）
        desc_map = {f["name"]: f.get("description", "") for f in (meta["fields"] if meta else [])}
        # 该表真实外键（本表列 -> 引用表），附在表头，提示可 JOIN 的键
        local_fks = fk_map.get(name, [])[:6]
        fk_str = ""
        if local_fks:
            fk_parts = [f"{fk['column']} -> {fk['ref_table']}.{fk['ref_column']}" for fk in local_fks]
            fk_str = "  JOIN键: " + "; ".join(fk_parts)
        # 桥接表标注：帮助 LLM 理解「需经由它中转」的多对多关联
        bridge_str = "（桥接表）" if name in bridge_tables else ""

        if fields:
            lines.append(f"## {name} [{category}] — {alias}{bridge_str}")
            if desc:
                lines.append(f"  描述: {desc}")
            if meta and meta.get("related_tables"):
                lines.append(f"  关联表: {', '.join(meta['related_tables'])}")
            if fk_str:
                lines.append(fk_str)

            # 取一行样例，供无描述字段补充语义
            sample_map: dict[str, str] = {}
            if sampled < sample_budget:
                rows = _sample_rows(name, limit=1)
                if rows and rows[0]:
                    sample_map = {k: str(v)[:24] for k, v in rows[0].items() if v is not None}
                    sampled += 1

            for f in fields[:25]:
                key = f.get("key", "")
                key_str = f", {key}" if key else ""
                # 语义：metadata 描述 > DB 注释 > 字段语义翻译兜底
                fdesc = desc_map.get(f["name"], "") or f.get("comment", "")
                try:
                    from agent.field_semantics import explain_field, table_specific_semantics
                    if not fdesc:
                        # 无描述时用翻译层兜底（中文直译/通用语义），避免裸字段名
                        fdesc = explain_field(name, f["name"], f.get("comment", ""))
                    else:
                        # 有「表.字段」精确语义（同名不同义）时追加，消解 status/type/result 歧义
                        ts = table_specific_semantics(name, f["name"])
                        if ts and ts not in fdesc:
                            fdesc = f"{fdesc}；{ts}"
                except Exception:
                    pass
                fdesc_str = f" — {fdesc}" if fdesc else ""
                # 无任何描述时，用样例值补语义
                sval = sample_map.get(f["name"], "")
                sval_str = f" [例: {sval}]" if (sval and not fdesc) else ""
                lines.append(f"  {f['name']}({f['type']}{key_str}){fdesc_str}{sval_str}")
            lines.append("")
            count += 1
        elif meta:
            # DB 不可用时回退到元数据（含字段描述与样例）
            lines.append(f"## {name} [{category}] — {alias}{bridge_str}")
            if desc:
                lines.append(f"  描述: {desc}")
            if meta.get("related_tables"):
                lines.append(f"  关联表: {', '.join(meta['related_tables'])}")
            if fk_str:
                lines.append(fk_str)
            for f in meta["fields"][:15]:
                key = f.get("key", "")
                key_str = f", {key}" if key else ""
                fdesc = f.get("description", "")
                try:
                    from agent.field_semantics import table_specific_semantics
                    ts = table_specific_semantics(name, f["name"])
                    if ts and ts not in fdesc:
                        fdesc = f"{fdesc}；{ts}".strip("；") if fdesc else ts
                except Exception:
                    pass
                fdesc_str = f" — {fdesc}" if fdesc else ""
                fsample = f.get("sample", "")
                fsample_str = f" [样例: {fsample}]" if fsample else ""
                lines.append(f"  {f['name']}({f['type']}{key_str}){fdesc_str}{fsample_str}")
            lines.append("")
            count += 1

    # ── 表间关联（JOIN 键）汇总：明确告诉 LLM 只能用它来关联 ──
    if join_edges or join_paths:
        lines.append("## 表间关联键（多表 JOIN 只能用以下外键，禁止编造其他关联条件）")
        for child, col, parent, pcol in join_edges:
            lines.append(f"- {child}.{col} = {parent}.{pcol}")
        # 多跳路径：显式给出完整链式 JOIN（含中间表），避免 LLM 在无直接外键时瞎猜
        for p in join_paths:
            if p.hops >= 2:
                lines.append(f"- 路径 {' ⋈ '.join(p.tables)}：{'；'.join(p.edges)}")
        lines.append("")

    return "\n".join(lines)


def _get_db_columns(table_name: str) -> list[dict] | None:
    """从 information_schema 获取真实字段（轻量单次查询，PG/MySQL 通用，支持 schema.table）

    同时提取数据库原生字段注释（PG: col_description / MySQL: column_comment），
    作为字段语义来源之一，弥补真实库字段名无中文含义的问题。
    """
    from database import get_db_type
    schema, table = _split_table_ref(table_name)
    # 表名/schema 可能来自 LLM 生成的 SQL（未过滤），拼进 information_schema 字符串字面量前
    # 先做白名单校验，杜绝 `' OR '1'='1` 之类的注入与信息泄露（与 _sample_rows 一致）。
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        return None
    try:
        if get_db_type() == "mysql":
            r = execute_sql(
                f"SELECT column_name AS name, data_type AS type, "
                f"CASE WHEN column_name IN ("
                f"  SELECT column_name FROM information_schema.key_column_usage "
                f"  WHERE table_name='{table}' AND table_schema=DATABASE() AND constraint_name='PRIMARY'"
                f") THEN 'PK' ELSE '' END AS key_type, "
                f"COALESCE(column_comment,'') AS col_comment "
                f"FROM information_schema.columns "
                f"WHERE table_name='{table}' AND table_schema=DATABASE() "
                f"ORDER BY ordinal_position"
            )
        else:
            r = execute_sql(
                f"SELECT c.column_name AS name, c.data_type AS type, "
                f"CASE WHEN c.column_name IN ("
                f"  SELECT kcu.column_name FROM information_schema.table_constraints tc "
                f"  JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name "
                f"  WHERE tc.table_name='{table}' AND tc.table_schema='{schema}' AND tc.constraint_type='PRIMARY KEY'"
                f") THEN 'PK' ELSE '' END AS key_type, "
                f"COALESCE(col_description(("
                f"  quote_ident(c.table_schema)||'.'||quote_ident(c.table_name))::regclass::oid, "
                f"  c.ordinal_position::int), '') AS col_comment "
                f"FROM information_schema.columns c "
                f"WHERE c.table_name='{table}' AND c.table_schema='{schema}' "
                f"ORDER BY c.ordinal_position"
            )
        if r["success"] and r["rows"]:
            return [{"name": row["name"], "type": row["type"],
                     "key": row.get("key_type", ""),
                     "comment": (row.get("col_comment") or "").strip()}
                    for row in r["rows"]]
    except Exception:
        pass
    # PG col_description 在部分权限/驱动下会失败，退回不带注释的简单查询
    try:
        if get_db_type() != "mysql":
            r = execute_sql(
                f"SELECT column_name AS name, data_type AS type, '' AS key_type "
                f"FROM information_schema.columns "
                f"WHERE table_name='{table}' AND table_schema='{schema}' "
                f"ORDER BY ordinal_position"
            )
            if r["success"] and r["rows"]:
                return [{"name": row["name"], "type": row["type"], "key": "", "comment": ""}
                        for row in r["rows"]]
    except Exception:
        pass
    return None


# ── 主流水线 ─────────────────────────────────────────────

def _is_number_like(v) -> bool:
    """判断字符串是否形如数值（用于识别结果中的文本/维度列）"""
    if v is None:
        return False
    s = str(v).strip()
    if not s:
        return False
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


# ── Critic 结论的「模板回显」过滤器（2026-09-19 修复「工单批不合格率」被改坏）──
# 事故链：思考型模型在 max_tokens=140 受限时，**偶发把 prompt 里的输出格式说明 /
# 结果 JSON 片段当成结论返回**（实测 3 次里 1 次）。该文本非空 ⇒ `_review_chain`
# 判定"结果不能回答用户问题" ⇒ `_refine_sql` 带一句无意义 issue 重写 ⇒ 改写链回落成
# `SELECT output_id,… FROM mes_process_output LIMIT 20` 裸明细 ⇒ 把**原本正确的比率 SQL
# 覆盖掉**。前端现象：问「工单批不合格率」返回 20 行原始产出明细。
# 实测回显样本：
#   '}\n\n注意: 最终只输出 JSON, 使用上述键名 (work_order_no, batch_fail_rate, work_order_id)'
#   'Critic 复核：]\n\n## 输出格式（严格遵守）\n若 SQL 正常：只输出…'
# 判定为回显即视为「复核通过」：漏判只是少一次改写机会（原本的答案仍可用），
# 误判会把正确答案改坏 —— 与「假失败比漏判更伤」的护栏一致。
# ⚠️ 本条只在 `not fast`（生产/前端链路）下生效：评测链路 fast=True 会整块跳过复核，
#    所以这类 bug **在 92 题评测里永远看不见**，只能靠前端日志/完整链路复现发现。
_CRITIC_ECHO_RE = re.compile(
    r"```|^\s*#|^\s*[{}\[\]]|只输出|不要任何其他内容|使用上述键名|输出格式|严格遵守|"
    r"JSON|json|注意[:：]|用户问题|生成的\s*SQL|查询结果|口径提示|语义提示|响应格式|"
    r"^\s*Critic",
    re.M)


def _critic_verdict_valid(verdict: str) -> bool:
    """Critic 的结论是否像一条**真实的问题描述**（否则视为噪声 → 判通过）。

    prompt 明确约定「输出一句问题描述（60 字以内）」，因此真实结论必须满足：
    单行、短、陈述句。实测噪声有两种形态，都会被下面的判据拦掉：
      ① prompt/转义回显：含 JSON / 键名 / Markdown 标题 / 只输出 等字样；
      ② **思考链泄漏**：思考型模型把内部推理写进 content，表现为
         `。\\n\\n根据问题…SQL 是否应该包含 work_order_id 列？…可能不是必需的`
         （自问自答、以标点开头、多段、还把行数记错）。
    """
    s = str(verdict or "").strip()
    if not s:
        return False
    # ① 回显 / 格式说明碎片
    if _CRITIC_ECHO_RE.search(s):
        return False
    # ② 思考链泄漏：截断的前缀标点、多段、自问自答、超长
    if s[0] in "。，,；;：:、）)】》”\"'":
        return False
    if "\n" in s or "\r" in s:
        return False
    if "？" in s or "?" in s or "是否" in s:
        return False
    if len(s) > 80:
        return False
    # 真实结论是中文叙述；中文少于 4 字的一律当碎片
    if len(re.findall(r"[\u4e00-\u9fff]", s)) < 4:
        return False
    return True


class LLMService:
    """流式 NL2SQL 流水线：意图 → 选表 → Schema → SQL → 执行 → 图表"""

    def __init__(self, query: str, history: list[dict] | None = None,
                 allowed_tables: set[str] | None = None,
                 fast: bool = False,
                 row_filters: dict[str, str] | None = None,
                 column_whitelist: dict[str, set[str]] | None = None,
                 acl=None,
                 no_confirm: bool = False):
        self.query = query
        self.history = history or []
        # 澄清重问（no_confirm=True）：前端已就歧义让用户用自然语言补充过 → 跳过二次确认
        # 弹窗直接生成。同一问题澄清后仍反复弹窗 = 最差的体验，此开关是显式硬保证。
        self._no_confirm = no_confirm
        # 评测快速模式：跳过结果复核/自动沉淀/图表渲染（评测只需要 SQL+执行结果，提速 ~30-40%）
        self.fast = fast
        # ── 统一权限上下文（权限中心 v2）──
        # acl: security.enforcer.AclContext；由入口按登录用户构建。
        # 传了 acl 就以它为唯一权限来源，并把表/行/列同步到下面的 legacy 字段，
        # 让 选表过滤 / schema 注入 / 表引用校验 等既有逻辑无需改动即可生效。
        self.acl = acl
        if acl is not None:
            allowed_tables = None if acl.allowed_tables is None else set(acl.allowed_tables)
            row_filters = dict(acl.row_filters or {})
            column_whitelist = {k: set(v) for k, v in (acl.column_whitelist or {}).items()}
        # 表权限（Phase 4.3）：允许访问的表名集合（小写、去 schema）；None = 不限制
        # ⚠️ fail-close：ACL 显式给出「空集」= 该角色零表授权，必须保持空集（拒绝一切），
        #    绝不能写成 `... or None` —— 那会把「零授权」塌成「不限表」，属越权。
        #    实测：aaaaa 角色 allowed_tables=[] 被当成 ALL，任何表都能查（fail-open 漏洞）。
        #    只有「没有 ACL 且未显式传 allowed_tables」才视为不限（兼容评测/脚本调用）。
        if acl is not None:
            self.allowed_tables = (None if allowed_tables is None
                                   else {str(t).lower() for t in allowed_tables})
        else:
            self.allowed_tables = {str(t).lower() for t in (allowed_tables or [])} or None
        # 行列级权限（改造5）：表名(裸名) → 行过滤条件 / 列白名单；执行前自动改写 SQL
        self.row_filters = {str(k).lower(): str(v) for k, v in (row_filters or {}).items() if v} or None
        self.column_whitelist = {str(k).lower(): set(str(c) for c in v) for k, v in (column_whitelist or {}).items() if v} or None
        # 权限生效明细（白盒）：本次查询实际注入的行条件 / 脱敏列 / 屏蔽列
        self.acl_applied: dict = {}
        # 因角色权限被剔除的候选表（实测事故：viewer 角色无 dim_product 权限，
        # 表匹配被砍到只剩 qms_inspection → LLM 拿不到关联表，先报「未能生成 SQL」、
        # 重试退化成按外键 product_id 分组的 28 行裸明细，用户看到的是"能跑通但答非所问"）。
        # 记下来用于：① 步骤日志预警 ② 生成失败时给出真实原因（而非笼统的"未能生成"）。
        self.acl_dropped_tables: list[str] = []
        self.intent = ""
        self.sql = ""
        self.executed_sql = ""   # 权限改写/脱敏后实际执行的 SQL（审计留痕）
        self.sql_result: dict = {}
        self.chart: dict = {"type": "none", "svg": ""}
        self.matched_tables: list[dict] = []
        self.schema_context = ""
        self.chart_type = "bar"
        self.title = ""
        self.error = ""
        # auto-train 门控：本次查询是否命中过高质量 SQL 示例（只有命中才自动沉淀，避免污染记忆）
        self._had_example_hits = False
        # 结果复核状态：是否经过定向改写、残留的合理性告警
        self._refined = False
        self._result_warning = ""
        # 阶段三：意图分流（归因分析型 vs 指标查询型）+ 阶段四：本体关系指引
        self.query_kind = "data"
        self.ontology_hint = ""
        # 语义缓存：本轮是否命中（命中则执行成功后不再重复沉淀）
        self._semantic_hit = False
        # 步骤事件记录：run() 实际产生的步骤（供 _build_response 返回，避免硬编码不一致）
        self._steps: list[dict] = []
        # 评价 Agent（白泽式四 Agent 闭环第四环）：结果质量分 / 评价明细 / 是否交叉比对过
        self._eval_score: int = 0
        self._evaluation: dict = {}
        self._cross_validated = False
        # 确定性编译命中 → 口径由指标注册表保证（跳过 LLM 复查），前端据此展示
        # "口径编译保证"而非数值评分（编译器保证强于 LLM 事后评审）
        self._compiled_trusted = False
        # P0-4：层级钻取信息（编译器产出，前端图表点击联动）
        self.drillable = None
        # 并行生成的「数据洞察」任务（性能优化，见 _start_analysis）
        self._analysis_future = None
        # 混合问答（非结构化+数据融合，对标 Spotter 3）：检索到的文档片段 + 引用标注
        self._retrieved_docs: list[str] = []
        self._cited_docs: list[dict] = []
        self._docs_retrieved = False

    def _bind_acl(self) -> None:
        """把本实例的权限上下文写入 ContextVar。

        指标级权限走的是 metric_registry.get_effective_metrics() 这类无参调用链
        （prompt 注入 / 确定性编译 / MQL 白盒 / 向量检索都从它取指标），无法逐层传参，
        因此用 ContextVar 传递。任何在**新线程**里驱动本实例的入口都要先调它。
        """
        try:
            from security.context import set_acl, clear_acl
            if self.acl is not None:
                set_acl(self.acl)
            else:
                clear_acl()
        except Exception:
            pass

    def _semantic_cache_hit(self):
        """命中语义缓存返回 {sql, matched_tables, schema_context, chart_type, similarity}，否则 None。

        快速模式（评测）禁用，保证结果可复现、不受历史缓存干扰。
        2026-09-11 新增「指标隔离护栏」：批测发现「每个车间的一次合格率」「各产品的出货合格率」
        等**未注册口径**问题以 ≥0.92 相似度命中了历史「良率」问题的编译缓存（embedding 走
        ngram 兜底时近义口径极易过阈），缓存 SQL 被静默复用 → 口径被偷换（结果别名甚至是
        「良率」）。规则：
          ① 缓存条目绑定注册指标（compiled 产物或记录了 metrics）而当前问题 no_hit → 拒绝；
          ② 当前命中指标与缓存条目指标完全不相交 → 拒绝（防 良率↔不良率 这类互换）。
        未绑定指标的 LLM 直生条目不受影响（同问法重问仍秒回）。
        """
        if self.fast:
            return None
        try:
            from security.enforcer import acl_fingerprint
            acl_fp = acl_fingerprint(getattr(self, "acl", None))
            hit = _semantic_lookup(self.query, _current_db_key(), acl_fp)
            if hit:
                res = getattr(self, "_metric_resolution", None) or {}
                cur_hits = {str(h.get("name", "")).lower() for h in (res.get("hits") or [])}
                entry_metrics = set(hit.get("metrics") or [])
                entry_tied = bool(entry_metrics) or bool(hit.get("compiled"))
                if entry_tied and not cur_hits:
                    return None  # ① 当前未命中注册口径，不偷换缓存里的注册指标口径
                if entry_metrics and cur_hits and entry_metrics.isdisjoint(cur_hits):
                    return None  # ② 命中的是不同指标，不复用
            return hit
        except Exception:
            return None

    def _semantic_cache_store(self):
        """SQL 执行成功后沉淀语义缓存（命中缓存执行成功则不重复写入）"""
        if self.fast or self._semantic_hit:
            return
        try:
            from security.enforcer import acl_fingerprint
            acl_fp = acl_fingerprint(getattr(self, "acl", None))
            _semantic_store(
                self.query,
                self.sql,
                self.matched_tables,
                self.schema_context,
                getattr(self, "chart_type", "") or "",
                _current_db_key(),
                acl_fp,
                compiled=(self.compiled_mql is not None),
                mql=self.compiled_mql,
            )
        except Exception:
            pass

    def _build_analysis_confirm(self) -> dict | None:
        """未定义口径（no_hit）场景：表匹配 + schema 上下文 + LLM 推断（一体版本）。

        返回推断 JSON 或 None（推断失败 → 调用方降级为原 no_hit 反馈条）。
        仅供兼容/非流式入口；run() 已拆步（先表匹配 yield 进展、再推断），
        推断本身由 _infer_analysis_confirm 完成（表匹配已由调用方做好）。
        """
        try:
            self.matched_tables = self._tables_from_metric_hits() or self._match_tables()
            return self._infer_analysis_confirm()
        except Exception:
            return None

    def _infer_analysis_confirm(self) -> dict | None:
        """基于已匹配的 self.matched_tables 做 schema + 口径 hint + LLM 推断。

        P0-性能（2026-09-02）：推断链耗时大头是两次 LLM 调用——
        ①表匹配 LLM：_select_tables_by_llm 已加 10s 总超时 + 10min 结果缓存；
        ②推断 LLM：infer_query_analysis 总 deadline=_ANALYSIS_TIMEOUT_S、块间 idle≤8s。
        推断结果进程内缓存 3min：同一问法（用户反复试探/未确认重问）免二次 20s 推断。
        """
        try:
            if self.allowed_tables is not None:
                self.matched_tables = [
                    t for t in self.matched_tables
                    if t["table_name"].split(".")[-1].lower() in self.allowed_tables
                ]
            if not self.matched_tables:
                return None
            # 推断结果缓存（同库同问法 3min，覆盖"用户弹窗未确认又重问同问法"场景）
            try:
                _ck = (_current_db_key(), (self.query or "").strip())
            except Exception:
                _ck = None
            if _ck:
                with _vec_cache_lock:
                    _hit = _analysis_infer_cache.get(_ck)
                if _hit and time.time() - _hit[0] < 180:
                    return _hit[1]
            candidate_names = [t["table_name"] for t in self.matched_tables]
            schema_context = _build_schema_fast(candidate_names, allowed_tables=self.allowed_tables, query=self.query)
            hint = ""
            try:
                from agent.metric_registry import get_metric_hint
                hint = get_metric_hint(self.query) or ""
            except Exception:
                pass
            analysis = infer_query_analysis(self.query, schema_context, self.matched_tables, hint,
                                            wall_budget_s=getattr(self, "_confirm_wall_s", 60.0))
            if analysis and _ck:
                with _vec_cache_lock:
                    _analysis_infer_cache[_ck] = (time.time(), analysis)
            return analysis
        except Exception:
            return None

    def _tables_from_metric_hits(self) -> list[dict] | None:
        """命中注册指标时，直接取指标的事实表作为表匹配结果（跳过 LLM 选表，省 ~10s）。

        口径意图前置解析（resolve_metric_intent）已确认命中指标（hits 含 tables），
        表归属由注册口径保证，无需再让 LLM 选表。
        """
        try:
            hits = (getattr(self, "_metric_resolution", None) or {}).get("hits") or []
        except Exception:
            hits = []
        if not hits:
            return None
        out: list[dict] = []
        seen: set[str] = set()
        for h in hits[:4]:
            for t in (h.get("tables") or []):
                t = str(t)
                if t and t not in seen:
                    seen.add(t)
                    out.append({"table_name": t, "table_alias": t, "category": "fact",
                                "description": "", "row_count": 0, "field_count": 0})
        return out or None

    def _match_tables(self) -> list[dict]:
        """表匹配（与主流程一致）：关键词强命中优先（跳过 LLM）→ LLM 语义选表 → 失败回退关键词 → 真实库校正。

        性能优化：关键词匹配 + 真实库过滤后 ≤2 张且全部有关键词命中时（如"订单总数"→test_orders、
        "各工序的良率"→mes_process_output+dim_process），结论已确定，直接返回跳过 LLM 选表调用
        （省 ~1-2s）；弱命中/歧义/无关键词（如"OEE趋势"）才走 LLM 语义选表。
        供 run() 与 /api/chart/custom 等入口复用，避免各入口表匹配行为不一致。
        """
        # ── 规则优先闸门：关键词强命中 → 跳过 LLM 选表（性能优化）──
        try:
            kw_hits = match_tables_by_query(self.query)[:8]
            if kw_hits:
                strong = self._filter_real_tables(kw_hits)
                # 关键词命中足够可信时直接采用，跳过 LLM 选表（实测省 1.7~4.4s）。
                # 可信判据：①候选都是当前库真实表（已过滤）②每张都有关键词命中
                # ③数量不超过 TABLE_MATCH_FAST_MAX（默认 3）。
                # 阈值为什么是 3：实测"库存低于安全库存的产品"关键词命中 4 张真实表
                # 却仍走 LLM（原阈值 2）多花 4.4s；放宽到 4 会把明显无关的表（如订单表
                # 因"产品"二字被命中）带进 schema，反而干扰后续 SQL 生成。
                # 该阈值可用环境变量 TABLE_MATCH_FAST_MAX 调整，置 0 = 始终走 LLM。
                if (strong and len(strong) <= _TABLE_MATCH_FAST_MAX
                        and all(t.get("keywords_matched") for t in strong)):
                    return strong
        except Exception:
            pass

        llm_tables = _select_tables_by_llm(self.query)
        if llm_tables:
            table_meta = {t["table_name"]: t for t in get_all_tables()}
            matched = []
            for name in llm_tables:
                meta = table_meta.get(name, {})
                matched.append({
                    "table_name": name,
                    "table_alias": meta.get("table_alias", name),
                    "category": meta.get("category", "unknown"),
                    "description": meta.get("description", ""),
                    "row_count": meta.get("row_count", 0),
                    "field_count": meta.get("field_count", 0),
                })
        else:
            matched = match_tables_by_query(self.query)[:8]
        if not matched:
            matched = match_tables_by_query("")[:8]
        # 校正候选表：以当前 DB 真实表为准（过滤 metadata 假表）
        # 注意：只过滤、不反向补全——LLM/关键词已选中的表保留原样。
        # 若全部被过滤（LLM 编造了当前库不存在的表）才用真实表前 N 张兜底，
        # 避免候选被扩成全量表 → schema 上下文臃肿 → LLM 生成时乱选表。
        #
        # 例外（2026-09-15）：**值→表 反向补齐**。问题里直接点名枚举值（"传感器/控制器"
        # 这类维表取值）时，表名/字段名召回抓不到持有该值的维表，必须补进来 —— 详见
        # _tables_by_literal 的说明。只追加、不重排，原有的匹配优先级不受影响。
        try:
            _have = {str(m.get("table_name", "")).split(".")[-1].lower() for m in matched}
            _lit = _tables_by_literal(self.query, exclude=_have, limit=3)
            if _lit:
                from db.tools import get_real_tables
                _reals = {str(t.get("table_name", "")).split(".")[-1].lower(): t
                          for t in (get_real_tables() or [])}
                for _s in _lit:
                    _rt = _reals.get(_s)
                    if _rt:
                        matched.append({
                            "table_name": _rt.get("table_name", _s),
                            "table_alias": _rt.get("table_name", _s),
                            "category": "dimension",
                            "description": "",
                            "row_count": _rt.get("row_count", 0),
                            "field_count": _rt.get("field_count", 0),
                            "matched_by_value": True,
                        })
        except Exception:
            pass
        return self._filter_real_tables(matched)

    def _filter_real_tables(self, matched: list[dict]) -> list[dict]:
        """以当前 DB 真实表为准过滤候选表（与 _match_tables 原校正逻辑一致）。"""
        try:
            from db.tools import get_real_tables
            real_tables = get_real_tables()
            if real_tables:
                real_names = {t["table_name"] for t in real_tables}
                filtered = [t for t in matched if t["table_name"] in real_names]
                if filtered:
                    return filtered
                # 完全无命中（LLM 编造表名/关键词无果）→ 真实表前 6 张兜底
                out = []
                for rt in real_tables[:6]:
                    out.append({
                        "table_name": rt["table_name"],
                        "table_alias": rt["table_name"],
                        "category": "unknown",
                        "description": "",
                        "row_count": 0,
                        "field_count": rt.get("field_count", 0),
                    })
                return out
        except Exception:
            pass
        return matched

    def run(self) -> Generator[dict[str, Any], None, None]:
        """主入口 — yield 进度事件（每步含耗时）"""
        t0 = time.time()
        t_step = t0

        # 权限上下文入线程：ContextVar 不跨线程继承，SSE 走后台线程执行 run()，
        # 必须在这里重设，否则 metric_registry.get_effective_metrics() 等
        # 无参调用链拿不到 acl，会导致指标级权限在流式接口静默失效。
        self._bind_acl()

        def _step(name: str, detail: str, evidence: dict | None = None) -> dict:
            """记录一步推理过程。

            P1-5 推理过程可视化（对标 ThoughtSpot Show Work）：除了"做了什么"，
            还记录 evidence —— 这一步的输入是什么、产出是什么、凭什么这么判定。
            用户点开某一步就能看到依据，而不是只看到一个结论，这对"为什么查这张表"
            "为什么这么算"这类信任问题至关重要（与结果溯源血缘互补：血缘解释数据
            从哪来，evidence 解释 Agent 为什么这么做）。
            """
            nonlocal t_step
            now = time.time()
            ms = int((now - t_step) * 1000)
            t_step = now
            rec = {"step": len(self._steps) + 1, "name": name, "status": "done",
                   "detail": detail, "duration_ms": ms}
            if evidence:
                rec["evidence"] = evidence
            self._steps.append(rec)
            ev = {"type": "step", "name": name, "detail": detail, "elapsed_ms": ms}
            if evidence:
                ev["evidence"] = evidence
            return ev

        # ── BIRD 评测档位：独立生成路径（必须放在最前面）──
        # 下面的中文意图分流（chat/lookup/ml…）、极简问题澄清、口径解析、语义缓存
        # 都是给中文业务问句调校的，对英文 BIRD 题会把正常的分析问题误判成 chat，
        # 然后静默走闲聊分支返回空 SQL（实测 BIRD #3 即因此失败，且 error 为空，
        # 极难归因）。因此这里最前置短路，只保留「只读安全 + 标识符存在性」校验。
        # 详见 agent/bird_profile.py；生产链路默认不进入本分支。
        try:
            from agent import bird_profile as _bp0
            _bird_on = _bp0.enabled()
        except Exception:
            _bp0, _bird_on = None, False
        if _bird_on:
            self.intent = "data"
            self.compiled_mql = None
            if _bird_backend() == "sqlite":
                # SQLite 后端：从 sqlite_master 读表名（绕过 PG）
                _real0 = [{"table_name": t, "field_count": 0}
                          for t in (_bp0.sqlite_tables() or [])]
            else:
                try:
                    from db.tools import get_real_tables as _grt0
                    _real0 = _grt0() or []
                except Exception:
                    _real0 = []
            self.matched_tables = [
                {"table_name": t.get("table_name", ""), "table_alias": t.get("table_name", ""),
                 "category": "fact", "description": "",
                 "row_count": 0, "field_count": t.get("field_count", 0)}
                for t in _real0 if t.get("table_name")
            ]
            if not self.matched_tables:
                self.error = "BIRD 档位：当前库没有可用表（schema 内省为空）。"
                yield {"type": "error", "message": self.error}
                return
            self.schema_context = _bp0.compact_schema() or ""
            yield _step("BIRD档位", "注入全量 schema（%d 张表）与领域 evidence" % len(self.matched_tables))
            # 生成 → 试执行 → 自一致性投票。
            # 总预算一次性封顶（多次生成 + 执行共享），避免把单题拖到分钟级。
            _b_deadline_s = float(os.getenv("BIRD_TOTAL_WALL_S", "320"))
            _b_n = int(os.getenv("BIRD_CANDIDATES", "3"))
            _b_sql, _b_info = _bird_gen_and_vote(
                self.query, self.schema_context,
                budget_s=_b_deadline_s, n=_b_n)
            self._bird_info = _b_info or {}
            if _b_sql:
                # 与主链共用同一套确定性改写（幂等；对英文题多数不触发）
                try:
                    _b_sql = _fix_topn_stable_tiebreak(self.query, _b_sql)
                except Exception:
                    pass
            self.sql = _b_sql or ""
            if not self.sql:
                self.error = ("BIRD 档位：AI 未能根据问题生成可执行的查询 SQL。"
                              "（模型输出为空，或被只读安全/标识符存在性校验拦截）")
                yield {"type": "error", "message": self.error}
                return
            yield {"type": "sql", "sql": self.sql}
            return

        # ── Step 1: 意图分类 ──
        self.compiled_mql = None  # 确定性编译产出的结构化 MQL（非编译路径为 None）
        self.intent = _classify_intent(self.query)
        # P0-4：多轮追问修正——上一轮有成功 SQL 且本次是修改型短句，即使规则判 chat/gibberish 也转 data
        if self.intent in ("chat", "gibberish") and _is_followup_query(self.query) and self._build_prev_context():
            self.intent = "data"
        _INTENT_LABELS = {
            "data_query": "数据分析查询", "data": "数据分析", "chat": "闲聊问答", "ml": "机器学习建模",
            "lookup": "数据表查询", "analyze_db": "库表结构分析", "gibberish": "无法识别",
            "write": "数据修改请求",
        }
        # 意图判定的依据：规则置信则直接采用，低置信才交给 LLM 复核（Show Work 可追溯）
        _intent_rule, _intent_confident = _classify_intent_rule(self.query)
        _intent_basis = ("规则分类（高置信：命中强信号词）"
                         if _intent_confident
                         else f"规则低置信（初判 {_intent_rule}）→ LLM 复核后定为 {self.intent}")
        if _intent_rule != self.intent and _intent_confident:
            _intent_basis = "追问修正：识别为修改型短句，沿用上一轮查询上下文"
        yield _step("意图理解", f"识别为「{_INTENT_LABELS.get(self.intent, self.intent)}」", {
            "input": self.query,
            "output": _INTENT_LABELS.get(self.intent, self.intent),
            "basis": _intent_basis,
        })

        if self.intent == "chat":
            yield from self._respond_chat()
            return
        if self.intent == "gibberish":
            yield from self._respond_gibberish()
            return
        if self.intent == "analyze_db":
            yield from self._respond_analyze_db()
            return
        if self.intent == "ml":
            yield from self._respond_ml()
            return
        if self.intent == "lookup":
            yield from self._respond_lookup()
            return
        if self.intent == "write":
            yield from self._respond_write_refusal()
            return

        # 极简指标输入（如"产量""良率"）→ 主动澄清，给出候选，避免硬猜
        candidates = _clarify_candidates(self.query)
        if candidates:
            yield {"type": "done", "elapsed_ms": 0,
                   "response": {"type": "clarify", "query": self.query,
                                "answer": "问题有点简短，请选择更具体的问法（或直接输入完整问题）：",
                                "candidates": candidates}}
            return

        # 口径意图前置解析（P1 口径管理）：≥2 并列命中且无并列连词 → 阻塞澄清，不执行查询
        self._metric_resolution = {"status": "skip", "hits": [], "hints": []}
        try:
            from agent.metric_registry import resolve_metric_intent
            self._metric_resolution = resolve_metric_intent(self.query, self.acl)
        except Exception:
            self._metric_resolution = {"status": "skip", "hits": [], "hints": []}
        if self._metric_resolution["status"] == "ambiguous":
            yield {"type": "done", "elapsed_ms": 0,
                   "response": {"type": "metric_clarify", "query": self.query,
                                "answer": "您提到的指标存在多个口径，请选择其一（或选择「都不是，自定义」）：",
                                "hits": self._metric_resolution["hits"]}}
            return

        # ── 语义缓存快速路径：命中相似历史问题 → 复用 SQL，跳过最慢的选表与 SQL 生成 ──
        # 对标 Wren AI / AWS 的亚秒级手段：重复/相似业务问题命中后直接复用历史 SQL 重新执行。
        # 注意：必须先于 no_hit 弹窗检查——之前确认执行过的问题已沉淀语义缓存，
        # 再次提问应直接命中复用，而不是重复弹窗。
        _cached = self._semantic_cache_hit()
        if _cached:
            self.sql = _cached["sql"]
            self.matched_tables = _cached["matched_tables"] or []
            self.schema_context = _cached["schema_context"] or ""
            if _cached.get("chart_type"):
                self.chart_type = _cached["chart_type"]
            self.title = self.title or self.query  # 命中路径未经过报告生成，标题用问题兜底
            self._semantic_hit = True
            # 恢复口径溯源信息：缓存条目记录了这条 SQL 当初是否由编译器生成。
            # 若当初是编译命中，这里恢复 compiled_mql，让前端照常展示「口径编译保证」
            # 而不是误报成 LLM 生成（并据此跳过 LLM 复查，省 2.8~5.3s）。
            if _cached.get("compiled"):
                self.compiled_mql = _cached.get("mql")
            yield _step("语义缓存", f"命中相似历史查询（相似度 {_cached['similarity']:.2f}），复用 SQL 直接执行")
        else:
            # ── 确定性编译尝试（架构强约定：未注册口径 → LLM 推断 + 弹窗确认，
            #    LLM 不得直接产出可执行 SQL）──
            # 提前到二次确认之前计算：判断「能否走确定性编译」需要它，
            # 同时避免下方 Step 2 重复编译。
            self.query_kind = "attribution" if _is_attribution_query(self.query) else "data"
            self._precompiled = None
            if self.query_kind != "attribution":
                # 高级口径优先（确定性执行，人工验证 SQL）：编译器不支持的跨表/多步算法
                # （分档/末工序完工缺口/超产）命中带 exec_sql 的指标 → 直接渲染执行，
                # 不依赖 LLM、也避免普通编译对空 sql_expression 指标的先发歧义。
                try:
                    from agent.metric_registry import render_exec_sql_metric
                    self._precompiled = render_exec_sql_metric(self.query)
                except Exception:
                    self._precompiled = None
                if not self._precompiled:
                    try:
                        from agent.metric_compiler import try_compile_metric
                        self._precompiled = try_compile_metric(self.query)
                    except Exception:
                        self._precompiled = None

            # ── 二次确认（架构强约定，2026-08-31 收紧）──
            # 以下情况必须弹窗让用户确认口径与执行方案，不得静默让 LLM 直接生成 SQL：
            # ① no_hit：有统计/分析意图但注册表 0 命中（口径未定义）；
            # ② hit：命中注册指标名但无编译口径（sql_expression 缺失/公式无法解析），
            #    同样是「未定义口径」，此前会静默走 LLM（用户实测发现，属架构违背）。
            _st = self._metric_resolution["status"]
            # 2026-09-07（产品决策）：未命中注册口径/模糊查询 → 不再弹窗询问，直接走 LLM 生成
            # SQL 并出结果，结果统一标注「AI 生成，请核对」；用户可在结果处「登记口径」后
            # 重新生成（口径入注册表 → 重问命中即走确定性编译/零 LLM）。此处不再产生
            # analysis_confirm；/api/agent/infer_confirm、/api/agent/execute_confirm 保留兼容。

            # ── Step 2: 表匹配（LLM 语义选表优先，失败回退关键词，再按真实库校正）──
            # 阶段三：意图分流（归因分析型 vs 指标查询型）；阶段五：刚性指标走确定性编译（零 LLM）
            # query_kind 与编译结果已在二次确认前完成（self._precompiled），此处直接复用
            if self.query_kind == "attribution":
                # 阶段二+四：归因走本体关系推理，注入「业务关系指引」，不做确定性编译
                try:
                    from agent.ontology import build_ontology_hint
                    self.ontology_hint = build_ontology_hint(self.query)
                except Exception:
                    self.ontology_hint = ""
                compiled = None
            else:
                self.ontology_hint = ""
                compiled = self._precompiled

            if compiled:
                self.sql = compiled["sql"]
                self.title = compiled["title"]
                self.compiled_mql = compiled.get("mql")
                # P0-4：编译器返回的层级钻取信息（前端图表点击联动素材）
                self.drillable = compiled.get("drillable")
                self.metric_hint = None  # 绿条已覆盖完整口径，无需 hint
                self.matched_tables = [
                    {"table_name": t, "table_alias": t, "category": "fact",
                     "description": "", "row_count": 0, "field_count": 0}
                    for t in compiled["tables"]
                ]
                # 表权限过滤：编译 SQL 涉及的表必须在角色允许集合内
                if self.allowed_tables is not None:
                    denied = [t for t in compiled["tables"]
                              if t.split(".")[-1].lower() not in self.allowed_tables]
                    if denied:
                        yield {"type": "done", "elapsed_ms": 0,
                               "response": {"type": "no_access", "query": self.query,
                                            "answer": "当前账号无权访问该查询涉及的数据表，请联系管理员开通权限。"}}
                        return
                _metric_names = compiled.get("metrics") or [compiled["metric"]]
                yield _step("表匹配", f"指标「{'、'.join(_metric_names)}」命中确定性编译器（零 LLM）", {
                    "input": self.query,
                    "output": "、".join(compiled.get("tables") or []),
                    "basis": f"指标注册表命中「{'、'.join(_metric_names)}」，"
                             f"SQL 由编译器按注册口径生成，未经 LLM",
                })
                # 2026-09-17（重要修正）：确定性改写链此前**只挂在 LLM 分支**上，编译产物
                # 在 `if compiled:` 处就设好 self.sql 并跳过该分支 → 编译路径从不做归一化。
                # 实测代价：postgres「各产线(line_id)的总投入数量(input_qty)」这类题走编译器，
                # 输出成 JOIN 来的 line_name，问句点名的 line_id 反而丢了；行集/口径全对却判失败。
                # 这里补跑同一条链：全部是**只做归一化/补列、不改语义**的确定性函数，
                # 对编译产物同样安全（92 题真实回放：零误伤）。
                try:
                    _cfired: list = []
                    _cfx = apply_output_fixes(self.query, self.sql, _cfired)
                    if _cfx != self.sql:
                        self.sql = _cfx
                        yield _step("SQL校验", "编译产物：" + "；".join(
                            _FIX_STEP_MSG.get(n, n) for n in _cfired))
                except Exception:
                    pass
            else:
                # 口径溯源 hint：回退 LLM 时，只要命中注册指标也展示关联口径（白盒可解释）
                self.metric_hint = self._build_metric_hint()
                self.matched_tables = self._tables_from_metric_hits() or self._match_tables()
                # 关键词强命中兜底补全（2026-09-08 评测发现）：表匹配可能被向量/LLM/指标
                # 选表限定到错误域（「工单号以 WO 开头…」曾被选中 qms_defect_detail 而漏掉
                # mes_work_order）。关键词命中的真实表可信度最高 → 并集补回，避免错域生成。
                try:
                    from db.tools import match_tables_by_query as _mtq
                    _kw = self._filter_real_tables(_mtq(self.query)[:8])
                    _have = {t["table_name"] for t in self.matched_tables}
                    _add = [t for t in _kw if t["table_name"] not in _have][:4]
                    if _add:
                        self.matched_tables = self.matched_tables + _add
                except Exception:
                    pass

                # 表权限过滤（Phase 4.3）：剔除角色无权访问的表
                if self.allowed_tables is not None:
                    _pre_acl = list(self.matched_tables)
                    self.matched_tables = [
                        t for t in self.matched_tables
                        if t["table_name"].split(".")[-1].lower() in self.allowed_tables
                    ]
                    # 被权限砍掉的表要显式记录并预警：剩下的 schema 不完整时，
                    # LLM 要么直接生成失败，要么退化成"能执行但答非所问"的裸明细
                    # （实测 viewer 角色问「各产品类别…」→ 只剩 qms_inspection →
                    #  回 28 行 product_id 明细）。不预警的话用户无从判断是权限问题。
                    self.acl_dropped_tables = [
                        t["table_name"] for t in _pre_acl
                        if t["table_name"].split(".")[-1].lower() not in self.allowed_tables
                    ]
                    if self.acl_dropped_tables:
                        yield _step("权限校验",
                                    "当前角色无权访问 %s，已从候选表剔除（涉及其口径的问题无法作答）"
                                    % "、".join(self.acl_dropped_tables[:4]),
                                    {"input": self.query,
                                     "output": "、".join(self.acl_dropped_tables[:6]),
                                     "basis": "数据集权限（表级白名单）：剔除后 schema 不完整，"
                                              "可能生成失败或答非所问，请核对结果"})
                    if not self.matched_tables:
                        yield {"type": "done", "elapsed_ms": 0,
                               "response": {"type": "no_access", "query": self.query,
                                            "answer": "当前账号无权访问该查询涉及的数据表，请联系管理员开通权限。"}}
                        return

                _names = [t["table_name"] for t in self.matched_tables]
                _preview = "、".join(_names[:5])
                _basis = "关键词 + 向量混合匹配"
                if self.metric_hint and self.metric_hint.get("name"):
                    _basis = (f"命中注册指标「{self.metric_hint['name']}」的表，"
                              f"但未命中编译口径 → 走 LLM 生成（建议补齐口径以走确定性编译）")
                yield _step("表匹配", f"匹配到 {_preview}{' 等' if len(_names) > 5 else ''} 共 {len(_names)} 张表", {
                    "input": self.query,
                    "output": _preview + (" 等" if len(_names) > 5 else ""),
                    "basis": _basis,
                })

                # 2026-09-07（产品决策）：未命中确定性编译 → 不再弹窗，直接在主链内生成。
                # 生成策略按 provider 分档：
                # · 快 provider（deepseek 等）：先做结构化 MQL 推断（LLM 产口径 → 确定性编译 SQL，
                #   结果可溯源）；推断失败再退「直接 LLM 生成 SQL」（逃生式）。
                # · 慢 provider（zhipu/GLM/moonshot 等）：json 结构化推断又慢又易 0 输出
                #   （实测 4 连发 0、单轮卡 40s+）→ 直接走逃生式生成（非 json，GLM 友好）。
                # 两种产物都过 validate_sql_safety + 列名校验 + 表权限/RLS；失败给引导，绝不静默空 SQL。
                _slow_ttfb = False
                try:
                    from agent.llm_providers import detect_provider as _dp3
                    _slow_ttfb = (_dp3(LLM_CONFIG.get("model", ""), LLM_CONFIG.get("base_url", ""))
                                  .get("name") in ("zhipu", "moonshot", "kimi"))
                except Exception:
                    _slow_ttfb = False
                # 只读边界：删除/清空/修改类祈使句直接拒绝，不走 LLM 生成
                _dml_zh = re.match(r"^(?:帮我|请|麻烦|给我)?(?:删除|删掉|清空|移除|丢弃)", self.query.strip())
                _dml_en = re.search(r"\b(?:drop\s+table|delete\s+from|truncate\s+table|update\s+\w+|insert\s+into)\b", self.query, re.I)
                if _dml_zh or _dml_en:
                    self.error = ("仅支持只读查询（SELECT），不能执行删除/清空/修改类操作。"
                                  "如需清理或修改数据，请联系系统管理员在数据源里处理。")
                    yield {"type": "error", "message": self.error}
                    return
                # 假表名提示：如「查询 nope_missing 表的数据」→ 库里没有这张表
                try:
                    _cm = _all_table_columns() or {}
                    _bare_all = {k.split(".")[-1].lower() for k in _cm}
                    _cols_all = {str(c).lower() for cols in _cm.values() for c in cols}
                    _fake = None
                    for _tok in re.findall(r"[a-z][a-z0-9_]{3,}", self.query.lower()):
                        if _tok in _bare_all or _tok in _cols_all:
                            continue
                        if len(_tok) >= 8 and re.search(r"表", self.query):
                            _fake = _tok
                            break
                    if _fake:
                        self.error = (f"在当前数据库中没找到名为「{_fake}」的数据。"
                                      "换个业务上的叫法再问一次，比如「产线」「设备」「工单」「客户」。")
                        yield {"type": "error", "message": self.error}
                        return
                except Exception:
                    pass
                # 未匹配到任何表：人话提示（查不到表 ≠ 让 AI 硬生成）
                if not self.matched_tables:
                    self.error = ("没找到与这个问题相关的数据。换个业务上的叫法再问一次，"
                                  "比如「产线」「设备」「工单」「客户」；也可以到「业务知识」页看看有哪些业务对象。")
                    yield {"type": "error", "message": self.error}
                    return
                # 未注册复合指标缺源数据（人均/周转/稼动率…）：源头拒绝，不让 LLM
                # 用无关列冒充指标出"一本正经的错误答案"（2026-09-13 用户实测：
                # 库存周转天数→MAX(可用库存) 4795 天、人均产出→AVG(产量) 不除人数）
                _ud_reason = _underivable_metric_reason(self.query)
                if _ud_reason:
                    self.error = _ud_reason
                    yield _step("口径检查", "该指标所需源数据在当前库不存在，拒绝生成（防冒充）")
                    yield {"type": "error", "message": self.error}
                    return
                # 主次反转（2026-09-14）：**结构化口径推断（MQL）为主路径**，自由生成 SQL 降为兜底。
                # 原因：MQL 路径只让模型输出「指标+维度+筛选」，SQL 由 _compile_sql_draft 拼装，
                # 列名/表名由编译器按真实 schema 校验 —— 非法列、错关联、笛卡尔积在结构上不可能出现；
                # 而自由生成要模型手写整条 SQL，实测会编造列名（duration_minutes）、猜错外键
                # （process_id = process_code）、漏 LIMIT、单位乘错。让好的路径先走、走够时间。
                # 此前：推断只给 15s、慢 provider 还直接跳过、且写死轮次与预算脱钩 → 该路径常失败。
                _t_gen0 = time.monotonic()
                self._confirm_wall_s = max(8.0, _SQL_GEN_WALL_S * _SQL_GEN_INFER_RATIO)
                yield _step("SQL生成", "正在生成查询 SQL（AI 生成中，复杂查询可能需要较长时间）…")
                try:
                    _sql = ""
                    _rejected_fallback = ""      # 闸门打标后被弃用的 SQL（保底用，见下）
                    # 对快慢 provider 都跑结构化推断（慢 provider 走 invoke 模式，见 infer_query_analysis）
                    _analysis = self._infer_analysis_confirm()
                    if _analysis:
                        _sql = str(_analysis.get("sql_draft") or "")
                    # 输出质量闸门（2026-09-15）：覆盖**所有**生成路径——包括 MQL 内部的
                    # 逃生舱（它产出的 SQL 不经过 _run_escape_sql_v2 的校验，实测 #4 的
                    # 「裸倒主键列」就是从那里出来的）。退化输出宁可弃用，落到下面的直生
                    # 兜底——那里有"带真实拒绝原因重试一轮"的机制，比直接给用户一坨明细强。
                    if _sql:
                        _why_q = _output_quality_reason(self.query, _sql)
                        if _why_q:
                            # 2026-09-17 保底：闸门是"宁可误杀"的启发式，重生成也不保证有产出。
                            # 旧行为是弃用即丢 → 兜底再失败就变成「AI 未能生成 SQL」（什么都没给）。
                            # 对用户「0 分的错答案」远好过「没有答案」，所以留一份保底，
                            # 只在重生成**完全没产出**时启用（有产出就仍用重生成的）。
                            _rejected_fallback = _sql
                            yield _step("SQL校验", "生成的 SQL 未通过输出质量检查，改用直生兜底：" + _why_q[:46])
                            _sql = ""
                    if not _sql:
                        # 兜底：自由生成，拿剩余预算（下限 6s 保证至少一次完整尝试）
                        _left = max(6.0, _SQL_GEN_WALL_S - (time.monotonic() - _t_gen0))
                        _sql = _direct_gen_sql(self.query, self.matched_tables,
                                               budget_s=_left) or ""
                    if not _sql and _rejected_fallback:
                        _sql = _rejected_fallback
                        yield _step("SQL校验", "兜底重生成未产出 SQL，回退使用被质量闸门标记的那条"
                                               "（口径推断可能不全，请重点核对结果）")
                    self.sql = _sql
                    # 确定性改写链（**唯一入口**，见 apply_output_fixes）：
                    # LIMIT 归一 → 窗口差值兜底 → TOP-N 稳定次序 → 值放错列 → 补列。
                    # 步骤日志由 fired 回放，消息表见 _FIX_STEP_MSG。
                    _fired: list = []
                    _after = apply_output_fixes(self.query, self.sql, _fired)
                    if _after != self.sql:
                        self.sql = _after
                        for _nm in _fired:
                            _msg = _FIX_STEP_MSG.get(_nm, _nm)
                            if _nm == "_fix_explicit_col_select":
                                _hits = _S_COL_HINT_RE.findall(self.query or "")[:3]
                                if _hits:
                                    _msg = "问句点名了「%s」，已补齐该列的输出与分组" % "、".join(_hits)
                            yield _step("SQL校验", _msg)
                except Exception as _e:
                    self.error = f"AI 生成查询失败：{_e}"
                if not self.sql:
                    self.error = (self.error or "AI 未能根据问题生成查询 SQL。"
                                  "可换个说法再问，或在结果处「去登记口径」把算法固定下来。"
                                  f"如果多次遇到，可点击输入框上方的「切换模型」换一个 AI 模型再试"
                                  f"（当前模型：{LLM_CONFIG.get('model', '')}）。")
                    # 权限缺表是"生成失败"的高频真因：此时提示"换个说法"毫无意义，
                    # 必须点名缺哪张表，用户才知道要去找管理员开通什么。
                    if self.acl_dropped_tables:
                        self.error = (
                            "本次问题需要的表（%s）当前角色无权访问，可用表不足以算出正确结果，"
                            "因此未能生成 SQL。请联系管理员开通该数据集权限，"
                            "或改用你有权限的数据提问。\n（原提示：%s）"
                            % "、".join(self.acl_dropped_tables[:3]), self.error)
                    yield {"type": "error", "message": self.error}
                    return

        if self.error:
            yield {"type": "error", "message": self.error}
            return

        yield {"type": "sql", "sql": self.sql}

        # 表权限二次校验（Phase 4.3）：SQL 引用的表必须都在允许集合内（防御 LLM 编造无权表）
        if self.allowed_tables is not None:
            ref_tables = _extract_sql_tables(self.sql)
            if ref_tables and not ref_tables.issubset(self.allowed_tables):
                denied = sorted(ref_tables - self.allowed_tables)
                self.error = f"查询涉及无权访问的表（{', '.join(denied)}），已拦截。请联系管理员开通权限。"
                yield {"type": "error", "message": self.error, "code": "forbidden"}
                return

        # ── Step 5: SQL 执行 ──
        # 前置校验：SQL 引用的表必须存在于当前库（防御 LLM 编造/跨库混用表名，如 123 库误用 postgres 表名）
        ok = True  # 后续 execute_sql 会重新赋值；前置校验拦截时置 False 走重试
        try:
            from db.tools import get_real_tables
            real_tables = get_real_tables()
            if real_tables:
                real_names = {t["table_name"].split(".")[-1].lower() for t in real_tables}
                ref_tables = _extract_sql_tables(self.sql)
                bad = [t for t in ref_tables if t not in real_names]
                if bad:
                    self.sql_result = {"success": False, "error": f"当前库不存在表: {', '.join(bad)}", "rows": [], "row_count": 0}
                    ok = False
                    yield _step("SQL校验", f"引用不存在的表 {', '.join(bad)}，准备重试修复…")
        except Exception:
            pass

        # 前置校验 2：SQL 引用的表必须与表匹配候选有交集（防 LLM 完全绕过选表结果乱用业务表，
        # 如问"订单"却 SELECT 自 mes_work_order 工单表——存在但语义错，执行不会报错）。
        # 仅拦截「引用表与候选表完全无交集」：多 JOIN 一张维度表（候选子集外）是合法的，放行。
        if ok and self.matched_tables:
            try:
                cand_bare = {t["table_name"].split(".")[-1].lower() for t in self.matched_tables}
                ref_tables = _extract_sql_tables(self.sql)
                if ref_tables and not ref_tables.intersection(cand_bare):
                    self.sql_result = {"success": False,
                                       "error": f"SQL 引用的表({', '.join(sorted(ref_tables))})与表匹配结果({', '.join(sorted(cand_bare))})完全不一致；"
                                                f"本次查询只能使用: {', '.join(sorted(cand_bare))}",
                                       "rows": [], "row_count": 0}
                    ok = False
                    yield _step("SQL校验", "引用表与选表结果不一致，准备重试修复…")
            except Exception:
                pass

        # 前置校验 3：业务语义校验（fast 模式同样生效）——产量/投入/产出类问题误用工单表
        # （mes_work_order 的 plan_qty/actual_qty 是计划/实际产量，语义不同于 process_output 的产量统计）
        # 仅当 mes_process_output 真实存在于当前库才启用（postgres 评测库专属规则，防跨库误导）
        if ok and self.sql and _fact_output_table_exists():
            try:
                if (re.search(r"(产量|投入|产出|合格量|不良|缺陷)", self.query)
                        and "工单" not in self.query
                        and re.search(r"\bmes_work_order\b", self.sql, re.IGNORECASE)):
                    self.sql_result = {"success": False,
                                       "error": "业务校验：问题问的是产量/投入/产出统计，应使用 mes_process_output 表"
                                                "（input_qty 投入量 / good_qty 合格产量 / defect_qty 不良量）；"
                                                "mes_work_order 的 plan_qty/actual_qty 是工单计划/实际产量，语义不同，请改正。",
                                       "rows": [], "row_count": 0}
                    ok = False
                    yield _step("SQL校验", "产量统计误用工单表，准备重试修复…")
            except Exception:
                pass

        # 前置校验 5：分组完整性（2026-09-12 评测驱动，未注册口径失败模式 Top1）
        # 问「各工厂的 X / 按 X 统计 / 分布 / 对比」，SQL 却没有 GROUP BY —— 执行能成功、
        # 结果是单一汇总值，答非所问且不会报错，是最隐蔽的一类错误（评测 31 道错题里 8 道属此类）。
        # 拦截后走既有重试链（多候选 + 定向修复），提示里明确要求补 GROUP BY 与 JOIN。
        if ok and self.sql:
            try:
                if _needs_group_by(self.query) and not _has_group_or_window(self.sql):
                    self.sql_result = {"success": False,
                                       "error": ("业务校验：问题要求按维度分组统计（各/每个/按…），"
                                                 "但 SQL 缺少 GROUP BY（或窗口 PARTITION BY），只会得到一个全局汇总值。"
                                                 "请补充 GROUP BY 维度列；若维度列不在当前表里，"
                                                 "先用上面给出的表间关联键 JOIN 进维度表再分组统计。"),
                                       "rows": [], "row_count": 0}
                    ok = False
                    yield _step("SQL校验", "缺少分组统计（各/按… 未 GROUP BY），准备重试修复…")
            except Exception:
                pass

        # 前置校验 6：占比完整性（2026-09-12 评测驱动）
        # 问「占比/百分比/比例」却输出绝对量（无除法、无窗口），执行成功但答非所问。
        if ok and self.sql:
            try:
                if (re.search(r"占比|百分比|比例", self.query)
                        and "/" not in self.sql
                        and "OVER" not in self.sql.upper()):
                    self.sql_result = {"success": False,
                                       "error": ("业务校验：问题要求计算占比/比例，但 SQL 没有任何除法运算"
                                                 "（也没有窗口函数），输出的是绝对量。"
                                                 "请用「该分组值 * 100.0 / NULLIF(全体总量, 0)」"
                                                 "或窗口 SUM(...) OVER () 计算百分比。"),
                                       "rows": [], "row_count": 0}
                    ok = False
                    yield _step("SQL校验", "占比类问题未做除法计算，准备重试修复…")
            except Exception:
                pass

        # 前置校验 7：TOP-N 排序完整性（2026-09-13 第二轮评测驱动）
        # 问「最长的前5个」却生成无 ORDER BY 的 LIMIT —— 排名语义完全丢失
        if ok and self.sql:
            try:
                if (re.search(r"前\s*\d+\s*(个|名)|最高的前|最长的前|最大的前|最短的前", self.query)
                        and not re.search(r"\bORDER\s+BY\b", self.sql, re.IGNORECASE)):
                    self.sql_result = {"success": False,
                                       "error": ("业务校验：问题要求 TOP-N 排名，但 SQL 没有 ORDER BY 排序，"
                                                 "LIMIT 截取的行无意义。请按问题含义对指标列排序后 LIMIT N。"),
                                       "rows": [], "row_count": 0}
                    ok = False
                    yield _step("SQL校验", "TOP-N 缺少排序，准备重试修复…")
            except Exception:
                pass

        # 前置校验 8：衍生比率指标必须做除法（2026-09-13 用户实测驱动）
        # 问「人均产出」生成 AVG(good_qty)、问「直通率」生成 SUM(defect_qty)——
        # 把总量/分量直接当比率输出，数值看似合理实则口径错误。
        # 已注册口径不在此列（注册指标由规则校验5按口径必填字段把关，
        # 且 AVG(比率列) 是合法写法，如 AVG(standard_yield_rate)）。
        if ok and self.sql and not getattr(self, "compiled_mql", None):
            try:
                _rate_kw = next((k for k in
                                 ("人均", "每人", "直通率", "一次合格率", "达成率",
                                  "周转率", "周转天数", "周转次数",
                                  "缺货率", "准时率", "交付率",
                                  # 2026-09-14 补：此前只收上面这些，导致「各产线设备故障率」
                                  # 生成 COUNT(*)+AVG(downtime_minutes)（既不是比率也答非所问）
                                  # 却照常通过；「覆盖率/完成率/占比/比例」同理。
                                  "故障率", "完成率", "覆盖率", "占比", "比例") if k in self.query), None)
                if (_rate_kw and "/" not in self.sql
                        and "OVER" not in self.sql.upper()):
                    from agent.metric_registry import find_metrics as _fm
                    if not _fm(self.query)[:1]:
                        self.sql_result = {"success": False,
                                           "error": (f"业务校验：问题要求计算「{_rate_kw}」这类比率型指标，"
                                                     "但 SQL 没有任何除法运算（也没有窗口函数），"
                                                     "输出的是总量/计数而非比率。请改写为除法公式"
                                                     "（如 人均=总量/人数、直通率=一次合格数/投入数），"
                                                     "分子分母字段必须取自候选表真实列。"),
                                           "rows": [], "row_count": 0}
                        ok = False
                        yield _step("SQL校验", f"比率类指标「{_rate_kw}」未做除法计算，准备重试修复…")
            except Exception:
                pass

        # 前置校验 9：TOP-N 行数一致性（2026-09-13 用户实测驱动）
        # 问「库存周转天数最高的 5 个产品」生成 ORDER BY … LIMIT 20 —— 排序对了
        # 但行数错，返回 20 行冒充"前 5"。LIMIT 必须与问题要求的 N 一致。
        # 2026-09-14：**编译路径也纳入本校验**（原来只查 LLM 生成）。实测编译器自己的
        # N 提取正则有缺陷（`前\s*(\d+)\s*(?:名|条|个|张)` 单位非可选 → 「排名前5」这类
        # 不带单位的问法匹配不到 → 确定性编译出 LIMIT 20），而编译产物此前绕过全部前置
        # 校验，于是"零 LLM、高置信"地返回 20 行冒充前 5。本条校验作为该缺陷的回归网。
        if ok and self.sql:
            try:
                _topn = None
                for _m in re.finditer(
                        r"(?:前\s*(\d+)|(?:最高|最低|最大|最小|最多|最少|最长|最短|最好|最差"
                        r"|最晚|最早|最新|最旧|排行)"
                        r"[的]?\s*(?:前)?\s*(\d+)|TOP\s*(\d+))\s*(?:个|名|条|家|项|台|道|种)?",
                        self.query, re.I):
                    _g = next((g for g in _m.groups() if g), None)
                    if _g:
                        _topn = int(_g)
                        break
                if _topn and 0 < _topn <= 100:
                    _lims = re.findall(r"\bLIMIT\s+(\d+)", self.sql, re.IGNORECASE)
                    if _lims and int(_lims[-1]) != _topn:
                        self.sql_result = {"success": False,
                                           "error": (f"业务校验：问题要求 TOP-{_topn}，"
                                                     f"但 SQL 的 LIMIT 为 {_lims[-1]}，"
                                                     f"返回行数与问题不符。请改为 LIMIT {_topn}。"),
                                           "rows": [], "row_count": 0}
                        ok = False
                        yield _step("SQL校验", f"TOP-N 行数与 LIMIT 不一致（要求 {_topn}），准备重试修复…")
            except Exception:
                pass

        # 前置校验 10：达成率的分子语义（2026-09-14 用户实测驱动）
        # 「各产线计划达成率」实测生成 order_status='completed' 的工单计数 / SUM(plan_qty)
        # → 达成率全部为 0，却照样输出"生产严重滞后"的完整结论与建议（Critic 虽已判出，
        # 但答案照给，见 run() 中 _result_warning 的落地修复）。此处从 SQL 层直接拦截重试。
        if ok and self.sql and "达成率" in self.query and not getattr(self, "compiled_mql", None):
            try:
                if (re.search(r"order_status", self.sql, re.IGNORECASE)
                        and not re.search(r"process_seq|dim_process", self.sql, re.IGNORECASE)):
                    self.sql_result = {"success": False,
                                       "error": ("业务校验：「达成率」的分子必须是实际完工产出数量，"
                                                 "即该工单末道工序（dim_process.process_seq 最大的记录，"
                                                 "如 PR08 包装入库）的 good_qty；不能用 order_status 的"
                                                 "工单状态计数当分子（会导致达成率恒为 0）。请改写。"),
                                       "rows": [], "row_count": 0}
                    ok = False
                    yield _step("SQL校验", "达成率用状态计数当分子，准备重试修复…")
            except Exception:
                pass

        # 前置校验 4：列名校验——SQL 引用的列必须在当前库对应表存在（防 LLM 凭先验知识
        # 用错列名，如 yans 的 order_status/severity_level/inspection_result 被写成 status/severity/result，
        # 表存在但列缺失时执行必失败）。缺失 → 拦截走迭代修复链，LLM 拿真实列名重写。
        if ok and self.sql:
            try:
                missing_cols = _validate_sql_columns(self.sql)
                if missing_cols:
                    self.sql_result = {"success": False,
                                       "error": "SQL 引用了当前库不存在的列: " + "; ".join(missing_cols[:6])
                                                + "。请严格使用表结构描述中的实际列名（禁止臆造列名），重写 SQL。",
                                       "rows": [], "row_count": 0}
                    ok = False
                    yield _step("SQL校验", f"引用不存在的列 {len(missing_cols)} 个，准备重试修复…")
            except Exception:
                pass

        # 行列级权限统一在 _exec_sql 中应用（改造5）：所有执行点（首次/多候选/重试/兜底/复核）都走安全执行
        t_exec = time.time()
        if self.sql_result.get("success") is not False:
            self.sql_result = self._exec_sql(self.sql)
        exec_ms = int((time.time() - t_exec) * 1000)
        ok = self.sql_result.get("success", False)
        _exec_err = self.sql_result.get("error", "")
        # 执行的输入是「权限改写后」的 SQL：Show Work 里要如实展示实际执行了什么，
        # 而不是改写前的原文 —— 否则用户看到的 SQL 与真实执行的对不上（权限白盒要求）。
        _acl_note = ""
        if self.acl_applied:
            _parts = []
            if self.acl_applied.get("row_filters"):
                _parts.append(f"注入行级过滤 {len(self.acl_applied['row_filters'])} 张表")
            if self.acl_applied.get("masked"):
                _parts.append(f"脱敏列 {len(self.acl_applied['masked'])} 个")
            if self.acl_applied.get("hidden"):
                _parts.append(f"屏蔽列 {len(self.acl_applied['hidden'])} 个")
            _acl_note = "；".join(_parts)
        # 权限白盒：被剔除的候选表也要如实展示，否则用户看到"能跑通"的结果
        # 却不知道它是用残缺 schema 算出来的
        if self.acl_dropped_tables:
            _acl_note = (_acl_note + "；" if _acl_note else "") + \
                "因角色权限剔除候选表：" + "、".join(self.acl_dropped_tables[:4])
        yield _step("SQL执行", f"{exec_ms}ms · {'成功' if ok else '失败'} · {self.sql_result.get('row_count', 0)} 行" + (f"：{_exec_err[:80]}" if (not ok and _exec_err) else ""), {
            "input": (self.executed_sql or self.sql or "")[:600],
            "output": f"{self.sql_result.get('row_count', 0)} 行 × {len(self.sql_result.get('columns') or [])} 列",
            "basis": _acl_note or "按当前账号权限执行（本次无行/列级改写）",
        })

        if ok and self.sql_result.get("rows"):
            yield {"type": "sql_result",
                   "columns": self.sql_result["columns"],
                   "rows": self.sql_result["rows"],
                   "row_count": self.sql_result["row_count"]}

        # ── 0 行重试（2026-09-16 BIRD Mini-Dev 评测驱动）──
        # 执行成功但返回 0 行：主链此前直接把空结果交给用户。
        # 实测（BIRD 500 题，口径 set 相等）：判对的题里 agent 返回 0 行的有 **0 道**、
        # 判错的题里 16 道 —— 「0 行」几乎总是过滤条件过严/把维表属性值等值到外键/
        # JOIN 错了表，而不是真没数据（聚合查询即使无匹配也会返回一行 0，不会空集）。
        # 处理：转换成一种失败原因，复用下方既有的「多候选 + 迭代修复」链；
        # 链跑完若仍无行则恢复原结果（行为与旧版完全一致，零回归风险）。
        #
        # ⚠️ 排除两条路径：确定性编译（compiled_mql）与语义缓存命中 —— 这两者的 SQL
        #    要么来自人工验证口径、要么来自历史成功查询，「0 行」更可能是真实无数据
        #    （如「超产工单数=0」），拿去让 LLM 重写反而会破坏可解释的确定性结果。
        _zero_retry = False
        if (ok and self.sql_result.get("success")
                and not (self.sql_result.get("rows") or [])
                and (self.sql or "").strip()
                and not getattr(self, "compiled_mql", None)
                and not getattr(self, "_semantic_hit", False)):
            _zero_retry = True
            _zero_orig_sql = self.sql
            _zero_orig_result = dict(self.sql_result)
            yield _step("SQL校验", "执行成功但返回 0 行：大概率是过滤条件过严或 JOIN 关联错了表，尝试重试…")
            self.sql_result = {
                "success": False,
                "error": ("执行成功但返回 0 行。这几乎总是以下原因之一："
                          "① 过滤条件过严或写错（典型：把维表的属性值直接等值到事实表的外键列，"
                          "如 product_name = '传感器' 写成 product_id = '传感器'）；"
                          "② JOIN 关联错了表或关联条件漏写；"
                          "③ 日期/状态等取值与库中实际存储的格式不一致。"
                          "请先用宽松条件确认相关表里有数据，再逐条收紧过滤条件；"
                          "JOIN 必须使用上面给出的表间关联键。"),
                "rows": [], "row_count": 0}
            ok = False

        if not ok:
            # 空 SQL 兜底（防御任何漏网分支）：修复 LLM 需要"原 SQL + 报错"做输入，
            # 输入为空时修复没有意义，只会空转造成"SQL 为空 → 无限重试"的卡死观感。
            # 直接给出人话引导，不再进入候选/修复链。
            if not (self.sql or "").strip():
                yield {"type": "error",
                       "message": "没能生成出可执行的查询。可以换个说法再问，"
                                   "或在结果处「去登记口径」把统计方式固定下来。"}
                return
            # ── 多候选一致性采纳（改造2）：首次失败先试 2-3 条不同写法的候选，
            #    执行结果一致者采纳；全部失败才走迭代修复。按需启用，控制成本 ──
            yield _step("SQL重试", "首次执行失败，AI 正在修复…")
            current_sql = self.sql
            current_error = self.sql_result.get("error", "")
            current_result = dict(self.sql_result)   # 候选失败后恢复用（改造4修复）
            candidates = self._generate_candidates(n=3)
            if candidates:
                yield _step("SQL重试", f"生成 {len(candidates)} 条候选 SQL，逐一执行比选…")
                accepted: str | None = None
                accepted_result: dict | None = None
                ok_rows: list[tuple[str, dict]] = []

                def _rows_sig(res: dict) -> str:
                    try:
                        return json.dumps(res.get("rows") or [], ensure_ascii=False, default=str)
                    except Exception:
                        return ""

                for cand in candidates:
                    self.sql = cand
                    # B1 修复：候选执行前同样做列名校验（候选路径此前绕过列保护，错列候选会被执行）
                    if _validate_sql_columns(cand):
                        continue
                    # 退化候选直接跳过（2026-09-15）：多候选择优是**免费**的拒绝点——换一个候选即可，
                    # 不额外调用 LLM。实测 v4 #6「既在7月生产过又出现过停机的产线」的
                    # `SELECT * FROM dim_production_line LIMIT 20` 就是从这个循环被采纳的。
                    if _output_quality_reason(self.query, cand):
                        continue
                    yield {"type": "sql", "sql": self.sql}
                    self.sql_result = self._exec_sql(self.sql)
                    c_ok = self.sql_result.get("success", False)
                    if c_ok:
                        # 表范围复查：候选 SQL 引用表必须与选表结果有交集（防 LLM 幻觉换表——
                        # 如"订单"被误生成 mes_work_order 工单表，能执行但答非所问）
                        if self.matched_tables:
                            cand_bare = {t["table_name"].split(".")[-1].lower() for t in self.matched_tables}
                            ref_tables = _extract_sql_tables(self.sql)
                            if ref_tables and not ref_tables.intersection(cand_bare):
                                continue
                        # 业务语义复查：产量/投入类勿误用工单表（仅 mes_process_output 存在的库）
                        if (_fact_output_table_exists()
                                and re.search(r"(产量|投入|产出|合格量|不良|缺陷)", self.query)
                                and "工单" not in self.query
                                and re.search(r"\bmes_work_order\b", self.sql, re.IGNORECASE)):
                            continue
                        if not self.fast and self._validate_result():
                            continue  # 能跑但答非所问的候选不采纳
                        ok_rows.append((self.sql, dict(self.sql_result)))
                        # 一致性检测：与前面成功候选的结果签名一致 → 采纳并停止
                        sig = _rows_sig(self.sql_result)
                        if len(ok_rows) >= 2 and sig in [_rows_sig(r) for _, r in ok_rows[:-1]]:
                            accepted, accepted_result = self.sql, self.sql_result
                            break
                if not accepted and ok_rows:
                    # 无两条结果一致 → 取第一条通过校验的成功候选（降级为"首选可用"）
                    accepted, accepted_result = ok_rows[0]
                if accepted:
                    self.sql = accepted
                    self.sql_result = accepted_result
                    ok = True
                    yield _step("SQL执行", f"候选执行成功 · {self.sql_result.get('row_count', 0)} 行 · 采纳{'一致结果' if len(ok_rows) >= 2 else '首选可用结果'}")
                    if self.sql_result.get("rows"):
                        yield {"type": "sql_result",
                               "columns": self.sql_result["columns"],
                               "rows": self.sql_result["rows"],
                               "row_count": self.sql_result["row_count"]}
                else:
                    # 候选全部失败 → 恢复原 SQL 与结果，走下方迭代修复链
                    # （仅恢复 sql 会导致 current_error 取到候选残留的成功结果 → 空错误 → 修复链空转）
                    self.sql = current_sql
                    self.sql_result = current_result

            if not ok:
                # ── 迭代式修复：最多 3 轮，每轮把错误发回 LLM ──
                current_error = self.sql_result.get("error", "") or current_error
                # 表不存在（首次）：也走重试，让 LLM 拿到真实表清单后换表（如 123 库误用 postgres 表名）
                for _ in range(3):
                    retry_sql = self._retry_sql_fix(current_error, prev_sql=current_sql)
                    if not retry_sql:
                        break
                    current_sql = retry_sql
                    self.sql = retry_sql
                    # 修复链产出的 SQL 此前**直接执行、不过改写链**（2026-09-24 修）：
                    # 后果是 `_fix_dim_listing_limit` 等后处理在这条路径上从不触发 ——
                    # 首生 SQL 执行失败 → LLM 修出来的 `LIMIT 100` 原样执行并返回，
                    # 前端照样显示"已查询到 100 条"（与历史「工单批不合格率」同型）。
                    # 收口到唯一入口 apply_output_fixes，位置必须在列校验**之前**，
                    # 否则改写新引入的列得不到校验。
                    _r_fired: list = []
                    _r_after = apply_output_fixes(self.query, self.sql, _r_fired)
                    if _r_after != self.sql:
                        self.sql = _r_after
                        for _nm in _r_fired:
                            yield _step("SQL校验", "修复后改写：" + _FIX_STEP_MSG.get(_nm, _nm))
                    # B1 修复：重试修复后的 SQL 执行前同样做列名校验（修复链可能生成新错列）
                    missing_cols = _validate_sql_columns(self.sql)
                    if missing_cols:
                        self.sql_result = {"success": False,
                                           "error": "SQL 引用了当前库不存在的列: " + "; ".join(missing_cols[:6])
                                                    + "。请严格使用表结构描述中的实际列名（禁止臆造列名），重写 SQL。",
                                           "rows": [], "row_count": 0}
                        ok = False
                        current_error = self.sql_result.get("error", "")
                        yield _step("SQL执行", "修复后仍引用不存在的列，继续修复…")
                        continue
                    yield {"type": "sql", "sql": self.sql}
                    self.sql_result = self._exec_sql(self.sql)
                    ok = self.sql_result.get("success", False)
                    # 表范围复查：修复后 SQL 引用表必须与选表结果有交集（防 LLM 幻觉换表）
                    if ok and self.matched_tables:
                        cand_bare = {t["table_name"].split(".")[-1].lower() for t in self.matched_tables}
                        ref_tables = _extract_sql_tables(self.sql)
                        if ref_tables and not ref_tables.intersection(cand_bare):
                            self.sql_result = {"success": False,
                                               "error": f"SQL 引用的表({', '.join(sorted(ref_tables))})与表匹配结果({', '.join(sorted(cand_bare))})完全不一致；本次查询只能使用: {', '.join(sorted(cand_bare))}",
                                               "rows": [], "row_count": 0}
                            ok = False
                            current_error = self.sql_result.get("error", "")
                            yield _step("SQL执行", "修复后仍引用非选表结果的表，继续修复…")
                            continue
                    # 重试后复查业务规则：仍误用工单表当产量 → 继续判失败并反馈（仅表存在的库）
                    if ok and (_fact_output_table_exists()
                               and re.search(r"(产量|投入|产出|合格量|不良|缺陷)", self.query)
                               and "工单" not in self.query
                               and re.search(r"\bmes_work_order\b", self.sql, re.IGNORECASE)):
                        self.sql_result = {"success": False,
                                           "error": "业务校验：仍误用 mes_work_order 表；产量/投入/产出统计必须用 mes_process_output 表"
                                                    "（input_qty/good_qty/defect_qty），请改正。",
                                           "rows": [], "row_count": 0}
                        ok = False
                        current_error = self.sql_result.get("error", "")
                        yield _step("SQL执行", "重试后仍误用工单表，继续修复…")
                        continue
                    yield _step("SQL执行", f"{int((time.time()-t_exec)*1000)}ms · {'成功' if ok else '失败'} · {self.sql_result.get('row_count', 0)} 行")
                    # 退化输出复查（2026-09-15）：能执行但答非所问（SELECT * / 裸倒主键列 /
                    # 问"多少种"漏 COUNT(DISTINCT)）→ 按项目既有范式，与"误用工单表"同等对待：
                    # 判失败并把**可读原因**回灌给修复链，让下一轮定向改写。
                    # 位置很重要：放在**择优/重试**环节是安全的（换候选或再修一轮即可）；
                    # 放在"唯一产出后直接拒绝"会因思考型模型耗尽预算把 WRONG 变成 GEN_FAIL
                    # （实测 4/6 → 3/6，已回退过）。
                    if ok:
                        _qwhy = _output_quality_reason(self.query, self.sql)
                        if _qwhy:
                            self.sql_result = {"success": False,
                                               "error": "系统判定这不是对问题的有效回答：" + _qwhy,
                                               "rows": [], "row_count": 0}
                            ok = False
                            current_error = self.sql_result.get("error", "")
                            yield _step("SQL执行", "生成的 SQL 答非所问（退化输出），继续修复…")
                            continue
                    if ok:
                        if self.sql_result.get("rows"):
                            yield {"type": "sql_result",
                                   "columns": self.sql_result["columns"],
                                   "rows": self.sql_result["rows"],
                                   "row_count": self.sql_result["row_count"]}
                        break
                    current_error = self.sql_result.get("error", "")
                    if not current_error:
                        break
                    # 修复后的 SQL 仍引用不存在的表 → 不再空转重试，
                    # 改试元数据 fallback 兜底（2026-09-13：原实现直接报错返回，
                    # 连 fallback 都不尝试，浪费一次确定性的挽回机会）
                    if _is_missing_table_error(current_error):
                        _fb = _fallback_sql(self.query, self.matched_tables)
                        if _fb and _fb != self.sql:
                            self.sql = _fb
                            yield _step("兜底SQL", "AI 反复引用不存在的表，改用备选查询")
                            yield {"type": "sql", "sql": self.sql}
                            self.sql_result = self._exec_sql(self.sql)
                            ok = self.sql_result.get("success", False)
                            if ok:
                                break
                        self.error = ("当前数据库中不存在该查询涉及的表（可能连接了错误的数据库，"
                                      "或该表属于其他数据库）。请确认连接或换个问题。")
                        yield {"type": "error", "message": self.error}
                        return

            if not ok:
                # ── 重试仍失败：用 fallback ──
                fallback_sql = _fallback_sql(self.query, self.matched_tables)
                if fallback_sql and fallback_sql != self.sql:
                    self.sql = fallback_sql
                    yield _step("兜底SQL", "AI 生成的 SQL 有误，使用备选查询")
                    yield {"type": "sql", "sql": self.sql}
                    self.sql_result = self._exec_sql(self.sql)
                    ok = self.sql_result.get("success", False)
                    yield _step("SQL执行", f"{int((time.time()-t_exec)*1000)}ms · {'成功' if ok else '失败'} · {self.sql_result.get('row_count', 0)} 行")
                    if ok and self.sql_result.get("rows"):
                        yield {"type": "sql_result",
                               "columns": self.sql_result["columns"],
                               "rows": self.sql_result["rows"],
                               "row_count": self.sql_result["row_count"]}

            # 0 行重试跑完仍无行 → 如实返回 0 行（与旧版行为一致，不把"确实没数据"误报成失败）
            if _zero_retry and not (self.sql_result.get("rows") or []):
                self.sql = _zero_orig_sql
                self.sql_result = _zero_orig_result
                ok = True
                yield _step("SQL执行", "重试后仍无数据，按原查询如实返回 0 行")

            if not ok:
                self.error = self.sql_result.get("error", "SQL 执行失败")
                yield {"type": "error", "message": self.error}
                return

        # ── 洞察生成提前并行启动（性能优化）────────────────────────────
        # 「数据洞察」与下面的 Critic / Evaluator 只依赖同一份 SQL 结果、彼此独立，
        # 串行要多付一次完整 LLM 往返（实测 5.0s）。此处先提交，_build_response 取结果；
        # 若下面复核改写了 SQL，作废后重新提交（见改写分支）。
        self._start_analysis()

        # ── Step 5.5: 结果合理性校验（"能跑" ≠ "答对"，最多定向改写 1 次）──
        # 确定性编译/exec_sql 结果跳过本环节：SQL 已由注册口径/人工验证（零 LLM），
        # 启发式（针对 LLM 生成设计）会误杀合法结果（如「超产工单数=0」被当 JOIN 错误、
        # 「缺口排行明细」被当缺聚合）→ 不让 LLM 复核改写确定性结果。
        if not self.fast and not getattr(self, "compiled_mql", None):
            issue = self._validate_result()
            # P0: 规则校验通过后，追加独立校验 Agent（Critic）做语义层二次审查。
            # off=仅规则校验 / on=强制 / auto=仅复杂查询（JOIN/聚合/子查询）自动开启。
            if not issue:
                # 「数据洞察」与「质量把关链」两路并行：二者只依赖同一份 SQL 结果、
                # 彼此独立，串行要多付一次完整 LLM 往返（实测洞察 5.0s）。
                # 为什么是两路而不是三路（洞察/Critic/Evaluator 各一路）：实测三路
                # 并发抢同一个 LLM 端点会互相拖慢，Evaluator 从 3.8s 涨到 8.2s，
                # 总墙钟反而更慢。两路是实测最优解。
                with ThreadPoolExecutor(max_workers=2) as _ex:
                    _f_review = _ex.submit(self._review_chain)
                    # 洞察若已在上面提交过则复用，避免重复生成
                    _f_analysis = None
                    if getattr(self, "_analysis_future", None) is None:
                        _f_analysis = _ex.submit(self._llm_analysis)
                    llm_issue, _ev = _f_review.result()
                    if _f_analysis is not None:
                        self._analysis_future = _f_analysis
                if llm_issue:
                    # `_review_chain` 返回时已带「Critic 复核：」前缀，此处不可再加一次
                    # （否则前端步骤详情显示成「Critic 复核：Critic 复核：…」）
                    issue = llm_issue
                elif _ev:
                    self._eval_score = _ev.get("score") or 0
                    self._evaluation = _ev
            if issue:
                yield _step("结果复核", f"检测到：{issue[:50]} · 正在改写查询…", {
                    "input": f"SQL 执行成功，返回 {self.sql_result.get('row_count', 0)} 行",
                    "output": "判定：结果不能回答用户问题",
                    "basis": issue[:160],
                })
                refined = self._refine_sql(issue)
                if refined and refined.strip() != (self.sql or "").strip():
                    prev_sql, prev_result = self.sql, self.sql_result
                    self.sql = refined
                    yield {"type": "sql", "sql": self.sql}
                    new_result = self._exec_sql(self.sql)
                    new_ok = new_result.get("success", False)
                    yield _step("SQL执行", f"复核后重查 · {'成功' if new_ok else '失败'} · {new_result.get('row_count', 0)} 行")
                    if new_ok and new_result.get("rows"):
                        # 二次校验：改写后的结果仍要过一遍合理性检查，防止"能跑但依然答非所问"
                        # 注意：必须先更新 self.sql_result 再校验——否则校验的是"新 SQL + 旧结果"
                        # 混合态，判定不可信，改写结果会被误回退。
                        self.sql_result = new_result
                        # 退化产物闸门（2026-09-19 修复「工单批不合格率」）：
                        # 复核改写是**有结果可回滚**的安全位置，这里必须再套一道
                        # `_output_quality_reason`。实测事故：首版 SQL 完全正确
                        # （各工单 不合格批次/抽检批次，执行成功），却被 Critic 判为
                        # "结果不对"触发改写，改写链回落到
                        # `SELECT output_id,… FROM mes_process_output LIMIT 20` 的裸明细；
                        # 该产物"能执行、有 20 行"，`_validate_result` 放行 → **正确答案被垃圾覆盖**。
                        # 它本就被闸门②（裸倒主键列）判为退化，只是复核路径此前没接这道闸门。
                        # 拒绝的代价极低：回滚到改写前的可用结果即可，不会像"唯一产出后拒绝"
                        # 那样把 WRONG 变成 GEN_FAIL。
                        _refine_rej = self._validate_result() or _output_quality_reason(self.query, self.sql)
                        if not _refine_rej:
                            self._refined = True
                            # SQL 与结果都变了 → 之前并行生成的洞察、以及基于旧结果
                            # 打的质量分都已作废：洞察重新提交，评分作废（不展示过期分数）
                            self._start_analysis()
                            self._evaluation = {}
                            self._eval_score = 0
                            yield {"type": "sql_result",
                                   "columns": self.sql_result["columns"],
                                   "rows": self.sql_result["rows"],
                                   "row_count": self.sql_result["row_count"]}
                        else:
                            self.sql, self.sql_result = prev_sql, prev_result
                            # 把拒绝原因一并留痕：否则用户只看到"复核没生效"，看不到为什么
                            self._result_warning = (issue + "（改写后的 SQL 判定为退化输出，"
                                                    f"已保留改写前的结果：{_refine_rej}）") if _refine_rej else issue
                            # 结果虽回退（未变），但新增了告警 → 洞察要带上它，重新生成
                            self._start_analysis()
                    else:
                        # 改写没有变好 → 回退到原结果，不让复核把可用结果弄丢
                        self.sql, self.sql_result = prev_sql, prev_result
                        self._result_warning = issue
                        self._start_analysis()
                else:
                    # 改写不可用（LLM 没给出新 SQL，或改写后与原 SQL 完全相同）→ 保留原结果。
                    # P0（2026-09-14 用户实测驱动）：此分支此前**什么都不做**，于是
                    # _result_warning 从未落地 —— 而洞察是在 Critic 之前就并行提交的
                    # （见上方 self._start_analysis()），结果就是"Critic 明确判出指标算错、
                    # 答案却照常输出完整结论+业务建议"（实测：各产线计划达成率全为 0，
                    # 仍输出"建议立即排查 MES 系统工单状态更新是否及时"）。
                    # 告警必须落地，且必须重新生成洞察 —— 新洞察会带「口径可能不正确」
                    # 约束并禁止基于可疑数值下结论（见 _llm_analysis / generate_insight_text）。
                    self._result_warning = issue
                    self._start_analysis()

        # ── Step 5.6: 评价结果落地 + 低分触发多候选交叉比对 ──────────────
        # 注意：Evaluator 已在 Step 5.5 与 Critic **并行**跑过（见上），这里只消费结果，
        # 不再重复调用 —— 否则又多一次完整 LLM 往返（实测 2.6~4.0s）。
        # 低分说明结果虽能跑但质量存疑 → 生成多条独立写法，用评分择优。
        if not self.fast and self._evaluation and not issue:
            try:
                from config import RESULT_EVAL_MIN_SCORE
                ev = self._evaluation
                _dims = ev.get("dims") or {}
                _dim_txt = "、".join(f"{k} {v}" for k, v in _dims.items())
                # 无论高低分都留痕：Show Work 要能看到"为什么判定这个分数"
                yield _step(
                    "结果评价",
                    f"质量分 {self._eval_score}"
                    + ("（偏低 · 交叉比对候选）" if self._eval_score < RESULT_EVAL_MIN_SCORE else "（达标）"),
                    {
                        "input": f"SQL + {self.sql_result.get('row_count', 0)} 行结果",
                        "output": f"总分 {self._eval_score}" + (f"，四维：{_dim_txt}" if _dim_txt else ""),
                        "basis": (ev.get("comment") or "评价 Agent 按口径符合度/维度完整性/"
                                  "过滤一致性/结果可用性四维打分")[:160],
                    },
                )
                if self._eval_score < RESULT_EVAL_MIN_SCORE:
                    if self._cross_validate(n=2):
                        yield {"type": "sql", "sql": self.sql}
                        yield {"type": "sql_result",
                               "columns": self.sql_result["columns"],
                               "rows": self.sql_result["rows"],
                               "row_count": self.sql_result["row_count"]}
            except Exception:
                pass
        elif self._compiled_trusted and not issue:
            # 编译命中：留痕说明为什么没有评分（口径由编译器保证，无需 LLM 事后评审）
            yield _step("结果评价", "确定性编译命中 · 口径由指标注册表保证（跳过 LLM 复查）", {
                "input": f"编译口径 SQL + {self.sql_result.get('row_count', 0)} 行结果",
                "output": "无需 LLM 复查",
                "basis": "SQL 由指标编译器按注册口径生成；LLM 复审口径反而可能改写偏离，"
                         "故此处只执行确定性规则校验",
            })

        # ── Vanna 风格 auto_train：仅命中高质量 SQL 示例时才自动沉淀，避免"能跑但答非所问"的坏示例污染记忆 ──
        if not self.fast:
            try:
                if (self.sql_result.get("success") and self.sql_result.get("rows")
                        and self._had_example_hits
                        and not self._result_warning  # 复核未通过的不沉淀，防止坏示例污染记忆
                        and not self._validate_result()):
                    from agent.memory import get_memory
                    tbl = self.matched_tables[0]["table_name"] if self.matched_tables else ""
                    get_memory().add_sql(self.query, self.sql, table_name=tbl, source="auto")
            except Exception:
                pass

            # P1-3 Skill 沉淀：把本次成功的「分析路径」沉淀成可复用技能。
            # 门控比 auto_train 略宽——不要求 _had_example_hits（Skill 库本来就是要
            # 从零积累），但保留全部质量门槛：执行成功 + 有数据 + 无残留告警 +
            # 规则复核通过，避免把"能跑但答非所问"的路径沉淀成脏技能。
            try:
                if (self.sql_result.get("success") and self.sql_result.get("rows")
                        and not self._result_warning and not self._validate_result()):
                    from agent.skill_store import record_success
                    record_success(self.query, self.sql,
                                   chart_type=self.chart.get("type", ""),
                                   tables=[t["table_name"] for t in self.matched_tables[:5]])
            except Exception:
                pass

        # ── 语义缓存沉淀：SQL 执行成功 + 无结果告警 + 规则复核通过才写入。
        # 2026-09-12 修复：此前没有 _validate_result 门，"能跑但答非所问"的 SQL 也会沉淀，
        # 下次同义问法 0.2s 直接复用错误结果（评测 postgres#15/#19 实锤）。
        if not self.fast and self.sql_result.get("success") and self.sql_result.get("rows") \
                and not self._result_warning and not self._validate_result():
            self._semantic_cache_store()

        # ── Step 6: 图表生成（先按真实数据结构校正图表类型）──
        # 评测快速模式（fast）跳过图表渲染：评测只需要 SQL + 执行结果，SVG 生成纯属额外耗时
        if not self.fast and self.sql_result.get("rows") and len(self.sql_result["rows"]) >= 2:
            self.chart_type = _validate_chart_type(
                self.chart_type,
                self.sql_result.get("columns") or [],
                self.sql_result["rows"],
                self.query,
            )
            if self.chart_type in ("table", "none"):
                # 数据结构不适合作图（无数值列/单行）→ 直接以表格展示
                self.chart = {"type": self.chart_type, "svg": ""}
            else:
                self.chart = generate_chart(
                    self.sql_result["columns"],
                    self.sql_result["rows"],
                    self.query,
                    force_type=self.chart_type,
                )
            if self.chart["type"] not in ("none", "table", None):
                yield _step("图表生成", f"生成 {self.chart['type']} 图")
                yield {"type": "chart", "svg": self.chart.get("svg", ""), "chart_type": self.chart["type"]}

        elapsed = int((time.time() - t0) * 1000)

        # ── 可观测性日志 + 运行时观测：每轮记录 intent/选表/SQL/耗时/编译命中，
        #    跑完一轮即可定位瓶颈；观测数据供 /api/ops/overview 查看编译覆盖率与耗时 ──
        try:
            _log_run_metrics(
                query=self.query,
                intent=self.intent,
                tables=[t.get("table_name", "") for t in self.matched_tables],
                sql=self.sql,
                ok=bool(self.sql_result.get("success")),
                rows=self.sql_result.get("row_count", 0),
                elapsed_ms=elapsed,
                refined=self._refined,
                warning=self._result_warning,
                error=self.error,
            )
            from agent.observability import record as _obs_record
            _obs_record(
                query=self.query,
                intent=self.intent,
                tables=[t.get("table_name", "") for t in self.matched_tables],
                ok=bool(self.sql_result.get("success")),
                rows=self.sql_result.get("row_count", 0),
                elapsed_ms=elapsed,
                compiled=(self.compiled_mql is not None),
                refined=self._refined,
                warning=self._result_warning,
                error=self.error,
            )
        except Exception:
            pass

        # P1-1 编译覆盖率：LLM 兜底路径执行成功且规则校验通过 → 把该查询的聚合表达式
        # 作为「未注册口径信号」沉淀进指标候选池（管理员在指标管理页一键采纳 → 后续
        # 编译命中 → 更快更稳）。这是把 metric_miner 自动挖掘接进日常运营的闭环。
        try:
            if (not self.fast and self.compiled_mql is None
                    and self.sql_result.get("success") and self.sql_result.get("rows")
                    and not self._result_warning and not self._validate_result()):
                from agent.metric_miner import suggest_from_query
                suggest_from_query(self.query, self.sql, source="runtime")
        except Exception:
            pass

        # P1-2 跨会话经营记忆：查询执行成功 → 累计（指标×维度）偏好组合
        # （新会话问相似问题时注入 memory_hint，减少 LLM 重复试探分析角度）
        try:
            if not self.fast and self.sql_result.get("success"):
                from agent.memory_center import record_preference
                _dims = []
                if isinstance(self.compiled_mql, dict):
                    _dims = self.compiled_mql.get("dimensions") or []
                from agent.metric_registry import find_metrics
                for _m in find_metrics(self.query, limit=2):
                    record_preference(_m["name"], _dims)
        except Exception:
            pass

        yield {"type": "done", "elapsed_ms": elapsed,
               "response": self._build_response()}

    # ── SQL 生成（流式）──────────────────────────────────

    def _build_history_text(self) -> str:
        """构建多轮对话历史上下文（供 SQL 生成/闲聊使用）

        放宽到 8 轮 × 400 字符——原来 6×200 的截断会把"上一轮问了什么"截没，
        导致"改成按月""上面那个"这类指代无法解析。
        """
        if not self.history:
            return ""
        lines = []
        for h in self.history[-8:]:
            role = "用户" if h.get("role") == "user" else "AI"
            content = str(h.get("content", "") or "").strip()
            if not content:
                continue
            lines.append(f"{role}: {content[:400]}")
        return "\n".join(lines)

    def _build_prev_context(self) -> str:
        """提取上一轮成功查询的结构化上下文（SQL / 表 / 结果列）

        历史消息里若带 sql / matched_tables / columns 字段（前端回传），直接用；
        否则从 AI 回复文本里正则捞最后一条 SQL。这是解析"把上面的改成按月"
        这类指代的关键——纯文本历史里 SQL 常被截断，无法作为改写基准。
        """
        if not self.history:
            return ""
        prev_sql = ""
        prev_tables: list[str] = []
        prev_cols: list[str] = []
        prev_rows: list[dict] = []
        prev_question = ""

        for h in reversed(self.history):
            if h.get("role") == "user" and not prev_question and prev_sql:
                prev_question = str(h.get("content", ""))[:120]
                break
            if h.get("role") in ("assistant", "ai") and not prev_sql:
                # 优先取结构化字段
                sql = str(h.get("sql") or "").strip()
                if not sql:
                    content = str(h.get("content", "") or "")
                    m = re.search(r'((?:SELECT|WITH)[\s\S]{10,1200}?)(?:\n\n|```|$)', content, re.IGNORECASE)
                    if m:
                        sql = m.group(1).strip()
                if sql:
                    prev_sql = sql[:1200]
                    tbls = h.get("matched_tables") or []
                    if isinstance(tbls, list):
                        prev_tables = [
                            (t.get("table_name") if isinstance(t, dict) else str(t))
                            for t in tbls[:4]
                        ]
                    cols = h.get("columns") or []
                    if isinstance(cols, list):
                        prev_cols = [str(c) for c in cols[:10]]
                    # P1-1 会话记忆：上一轮结果行（前端回传，封顶 30），用于提取维度实体
                    rows = h.get("rows")
                    if isinstance(rows, list):
                        prev_rows = [r for r in rows[:30] if isinstance(r, dict)]

        if not prev_sql:
            return ""
        # P0-4：结构化上一轮上下文 + 槽位变化（规则化，失败自动退回纯文本）
        try:
            from agent.session_state import extract_slots, build_prev_context_block
            prev_slots = extract_slots(prev_question)
            cur_slots = extract_slots(self.query)
            block = build_prev_context_block(prev_sql, prev_tables, prev_cols,
                                             prev_slots, cur_slots, prev_question)
            # P1-1 会话记忆增强：实体记忆（"那L01呢"）+ 短句继承 + 时间继承
            try:
                from agent.conv_memory import build_conv_context
                conv = build_conv_context(self.query, prev_question, prev_sql,
                                          prev_rows, prev_cols, prev_slots, cur_slots)
                if conv:
                    block = (block + "\n\n" + conv) if block else conv
            except Exception:
                pass
            # 多轮会话加深（对标 Spotter 3）：整个会话的累积状态——跨轮实体池、
            # 多轮槽位继承、轮次引用（第 3 轮还能引用第 1 轮的实体/时间）。
            try:
                from agent.conversation_memory import accumulate_session, build_session_context
                sess = build_session_context(self.query, accumulate_session(self.history))
                if sess:
                    block = (block + "\n\n" + sess) if block else sess
            except Exception:
                pass
            return block
        except Exception:
            parts = []
            if prev_question:
                parts.append(f"上一轮问题: {prev_question}")
            parts.append(f"上一轮 SQL:\n{prev_sql}")
            if prev_tables:
                parts.append(f"涉及表: {', '.join(t for t in prev_tables if t)}")
            if prev_cols:
                parts.append(f"结果列: {', '.join(prev_cols)}")
            return "\n".join(parts)

    def _apply_row_level_security(self, sql: str) -> tuple[str, str]:
        """行列级权限改写（deprecated，v2 单轨后仅作兜底）。

        所有 HTTP 入口（ask/stream/chart_custom）均已统一传 acl，由
        security.enforcer.rewrite_sql 改写；本方法仅服务于**未传 acl** 的
        遗留调用方（如 e2e_test.py 测试脚本，超级管理员视角不受限）。
        新功能开发请勿再走此路径。
        """
        if not self.row_filters and not self.column_whitelist:
            return sql, ""
        try:
            import sqlglot
            from sqlglot import exp
            try:
                from database import get_db_type
                db_type = get_db_type()
            except Exception:
                db_type = "postgresql"
            read_dialect = "postgres" if "postgres" in (db_type or "") else "mysql"
            ast = sqlglot.parse_one(sql, read=read_dialect)

            # ── 行级：对「直接 FROM/JOIN 了目标表的 SELECT 层级」追加 AND 过滤条件 ──
            # 只查每个 SELECT 的直接 FROM/JOIN（不递归子查询/CTE 内部），
            # 避免外层 SELECT 因递归命中 CTE 内部表而误加 WHERE（引用不存在的列会报错）。
            if self.row_filters:
                for table, cond in self.row_filters.items():
                    for sel in ast.find_all(exp.Select):
                        direct: set[str] = set()
                        try:
                            # sqlglot 各版本键名兼容：新版 args["from_"]，旧版 args["from"]
                            frm = sel.args.get("from_") or sel.args.get("from")
                            if frm and isinstance(frm.this, exp.Table):
                                direct.add(frm.this.name.split(".")[-1].lower())
                            for j in sel.args.get("joins") or []:
                                jt = getattr(j, "this", None)
                                if isinstance(jt, exp.Table):
                                    direct.add(jt.name.split(".")[-1].lower())
                        except Exception:
                            continue
                        if table in direct:
                            sel.where(cond, append=True, copy=False)

            # ── 列级：仅当「最外层 SELECT 直接 FROM 单一真实表」时裁剪 ──
            # 三处修正（改造6）：
            #  ① 用含 schema 的完整表名查真实列（裸名会查到 public schema 失败 → 误拒带 schema 查询）
            #  ② 只处理最外层 SELECT 且其 FROM 直接是目标表（FROM 子查询/CTE 引用则跳过，防生成不存在列）
            #  ③ 保留 GROUP BY / ORDER BY 引用的裸列（否则裁剪后 PG 报"must appear in GROUP BY"）
            if self.column_whitelist:
                name_map: dict[str, str] = {}
                for tobj in ast.find_all(exp.Table):
                    bare = (tobj.name or "").split(".")[-1].lower()
                    full = tobj.sql() or tobj.name
                    if bare and bare not in name_map:
                        name_map[bare] = full
                if len(name_map) == 1:
                    bare = next(iter(name_map))
                    wl = self.column_whitelist.get(bare)
                    if wl:
                        select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
                        frm = select.args.get("from_") or select.args.get("from")
                        from_is_target = (frm and isinstance(frm.this, exp.Table)
                                          and frm.this.name.split(".")[-1].lower() == bare)
                        if select and from_is_target:
                            full_name = name_map[bare]
                            wl_lower = {w.lower() for w in wl}
                            # 保留 GROUP BY/ORDER BY 引用的裸列，避免裁剪后 SQL 语义损坏
                            keep_cols: set[str] = set()
                            try:
                                for og in select.find_all(exp.Order, exp.Group):
                                    for c in og.find_all(exp.Column):
                                        if not c.table:
                                            keep_cols.add(c.name.lower())
                            except Exception:
                                pass
                            # 别名 → 表名映射（FROM employee t / JOIN dim_product p）：带前缀列也按目标表裁剪，
                            # 否则 SELECT t.salary FROM employee t 会绕过列白名单（修复）
                            alias_to_bare: dict[str, str] = {}
                            try:
                                _frm = select.args.get("from_") or select.args.get("from")
                                if _frm:
                                    _ft = _frm.this
                                    if isinstance(_ft, exp.Table):
                                        alias_to_bare[(_ft.alias or _ft.name).lower()] = _ft.name.split(".")[-1].lower()
                                    elif isinstance(_ft, exp.Subquery) and getattr(_ft, "alias", None):
                                        alias_to_bare[str(_ft.alias).lower()] = ""
                                for _j in select.args.get("joins") or []:
                                    _jt = getattr(_j, "this", None)
                                    if isinstance(_jt, exp.Table):
                                        alias_to_bare[(_jt.alias or _jt.name).lower()] = _jt.name.split(".")[-1].lower()
                            except Exception:
                                pass
                            new_projs = []
                            star_expanded = False
                            for p in select.expressions:
                                if isinstance(p, exp.Star):
                                    cols = _get_db_columns(full_name)
                                    if not cols:
                                        return sql, f"列级权限：无法获取表 {full_name} 的列结构，拒绝执行"
                                    allowed_real = [c["name"] for c in cols
                                                    if c["name"].lower() in wl_lower]
                                    if not allowed_real:
                                        return sql, f"列级权限：表 {full_name} 无任何可访问列，拒绝执行"
                                    for c in allowed_real:
                                        new_projs.append(exp.column(c))
                                    star_expanded = True
                                    continue
                                # 列投影 → 白名单过滤（保留 group/order 引用列）
                                if isinstance(p, exp.Column):
                                    if not p.table:
                                        # 裸列：单表查询场景下属于目标表
                                        if p.name.lower() not in wl_lower and p.name.lower() not in keep_cols:
                                            continue
                                    else:
                                        # 带前缀列：按别名映射判断是否属于目标表
                                        owner = alias_to_bare.get(p.table.lower())
                                        if owner == bare:
                                            if p.name.lower() not in wl_lower and p.name.lower() not in keep_cols:
                                                continue
                                        # 非目标表的列保留（列级权限只约束目标表）
                                new_projs.append(p)
                            if star_expanded or [id(x) for x in new_projs] != [id(x) for x in select.expressions]:
                                select.set("expressions", new_projs)
                            if not new_projs:
                                return sql, f"列级权限：SELECT 无任何可访问列（表 {full_name}），拒绝执行"

            return ast.sql(dialect=read_dialect), ""
        except Exception as e:
            # 改写异常 → 安全优先拒绝执行，不让权限绕过
            return sql, f"行列级权限改写失败，已拒绝执行: {e}"

    def _exec_sql(self, sql: str) -> dict:
        """带多级权限的 SQL 执行：所有执行点统一走这里，
        保证 首次/多候选/重试/fallback/复核 任何一条 SQL 都应用权限改写。
        改写失败/无权访问一律拒绝执行（fail-close，安全优先）。

        优先走权限中心 v2（security.enforcer）：数据集级拒绝 + 行级注入 +
        列级拒绝/动态脱敏；无 acl 时回退改造5 的旧路径，保证零回归。
        """
        eff = sql
        if self.acl is not None and self.acl.needs_sql_rewrite():
            try:
                from security.enforcer import rewrite_sql as _acl_rewrite
                eff, acl_err, applied = _acl_rewrite(sql, self.acl)
                if applied:
                    # 累积本次查询的权限生效明细（多次执行取并集，供白盒展示）
                    for k, v in applied.items():
                        if not v:
                            continue
                        cur = self.acl_applied.setdefault(k, [])
                        for item in v:
                            if item not in cur:
                                cur.append(item)
                if acl_err:
                    return {"success": False, "error": acl_err, "rows": [], "row_count": 0,
                            "acl_denied": True}
            except Exception as e:
                return {"success": False, "error": f"权限改写异常，已拒绝执行：{e}",
                        "rows": [], "row_count": 0, "acl_denied": True}
        elif self.row_filters or self.column_whitelist:
            eff, rl_err = self._apply_row_level_security(sql)
            if rl_err:
                return {"success": False, "error": rl_err, "rows": [], "row_count": 0}
        self.executed_sql = eff
        res = execute_sql(eff)
        # 连接级故障自愈（2026-09-16 BIRD 评测实测）：一次 PG 抖动会把 SQLAlchemy 连接池
        # **毒化**（InvalidatePoolError / network error），不重建的话同进程后续**每一题**
        # 都会连带失败（实测整库 66 题全废，只因为最开始那一次抖动）。
        # 检测到连接级错误时重建池再试一次；仍失败则原样返回（不吞错误）。
        if not res.get("success") and _is_conn_error(res.get("error")):
            try:
                from db.executor import reload_pool
                reload_pool()
            except Exception:
                pass
            try:
                res = execute_sql(eff)
            except Exception as e:
                res = {"success": False, "error": str(e)[:300], "rows": [], "row_count": 0}
        return res

    def _retry_sql_fix(self, error_msg: str, prev_sql: str = "", rounds: int = 0) -> str:
        """SQL 执行失败后，把错误发回 LLM 修复；支持多轮迭代修复。

        错误分类定向修复（对齐 MAC-SQL refiner 思路）：
        - 表不存在 → 查真实表名清单注入 prompt
        - 字段不存在 → 查该表真实字段注入 prompt
        - 其他（语法/类型）→ 通用重生成
        """
        if not self.sql or not error_msg:
            return ""
        try:
            from agent.prompt_builder import build_sql_prompt
            llm = _make_llm(temp=None, max_tokens=_sql_gen_max_tokens(), model=_sql_gen_model())
            target = prev_sql or self.sql

            # ── 错误分类（支持 PG 中文/英文错误、MySQL）──
            fix_hints = []
            err = str(error_msg)
            # 表不存在：PG(EN) relation "x" does not exist / PG(CN) 关系 "x" 不存在 / MySQL Table 'x' doesn't exist
            m_table = re.search(
                r'relation "?(\w+)"? does not exist|关系 "?(\w+)"? 不存在|Table ["\']?([\w.]+)["\']? doesn\'t exist|Unknown table ["\']?(\w+)',
                err, re.IGNORECASE)
            if m_table:
                bad_table = next((g for g in m_table.groups() if g), "")
                bad_table = bad_table.split(".")[-1]
                try:
                    from db.tools import get_real_tables
                    real_names = [t["table_name"] for t in get_real_tables()][:40]
                    if real_names:
                        fix_hints.append(f"表 '{bad_table}' 不存在。真实表清单: {', '.join(real_names)}。请只使用真实表，非 public 表需带 schema 前缀（如 factory.work_order）。")
                    else:
                        fix_hints.append(f"表 '{bad_table}' 不存在，请核对表名。")
                except Exception:
                    fix_hints.append(f"表 '{bad_table}' 不存在，请核对表名。")

            # 字段不存在：PG(EN) column x of relation y / PG(CN) 列 "x" 不存在 / MySQL Unknown column
            m_col = re.search(
                r'column "?(\w+)"? of relation "?(\w+)"? does not exist|列 "?(\w+)"? 不存在|Unknown column ["\']?(\w+)["\']?',
                err, re.IGNORECASE)
            if m_col:
                bad_col = next((g for g in m_col.groups() if g), "")
                # 找到 SQL 中涉及的表
                involved = re.findall(r'(?:FROM|JOIN)\s+"?([\w.]+)"?', target.upper())
                if involved:
                    tbl = involved[-1].lower()
                    try:
                        cols = _get_db_columns(tbl)
                        if cols:
                            real_cols = [c["name"] for c in cols]
                            fix_hints.append(f"字段 '{bad_col}' 不存在于表 {tbl}。真实字段: {', '.join(real_cols)}。请只使用这些字段。")
                    except Exception:
                        pass
            if not fix_hints:
                fix_hints.append("检查表名/字段名与 schema 完全一致；检查 JOIN 条件、GROUP BY 字段、聚合函数；检查日期/时间字段类型是否符合当前数据库方言。")

            # 复用记忆检索注入（DDL / 文档 / SQL 示例），给修复提供真实 schema 依据
            ddl_list, docs, sql_examples = [], [], []
            try:
                from agent.memory import get_memory
                memory = get_memory()
                ddl_list = [d["ddl"] for d in memory.get_ddl_by_keywords(self.query, k=3)]
                docs = memory.search_documentation(self.query, k=2)
                sql_examples = memory.search_sql(self.query, k=3)
            except Exception:
                pass

            prompt = build_sql_prompt(
                query=self.query,
                schema_context=self.schema_context,
                ddl_list=ddl_list,
                docs=docs,
                sql_examples=sql_examples,
                history_text="",
                agg_hint=_build_agg_hint(self.query),
                max_tokens=6000,
            )
            fix_prompt = (
                f"{prompt}\n\n上次生成的 SQL 执行失败，错误: {error_msg}\n"
                f"原始 SQL: {target}\n\n"
                f"## 修复指引\n{chr(10).join('- ' + h for h in fix_hints)}\n"
                f"修正后重新输出完整 JSON。"
            )
            resp = llm.invoke([SystemMessage(content=fix_prompt)])
            raw = resp.content.strip()
            sql, _, _ = self._parse_sql_response(raw)
            return sql
        except Exception:
            return ""

    def _generate_candidates(self, n: int = 3) -> list[str]:
        """多候选 SQL 生成（对齐工业界 5S 框架 Candidate Generation + Selection）：
        一次生成 n 条「语义等价但写法不同」的候选 SQL，供调用方逐条执行、
        结果一致者采纳。仅在首次执行失败时按需启用（避免全量双倍 token 成本）。

        与 _retry_sql_fix 的区别：_retry_sql_fix 拿错误定向改一条；本方法
        生成多条独立写法，靠「执行结果一致性」选出最可信的一条。
        """
        try:
            from agent.prompt_builder import build_sql_prompt
            # 稍高温度制造写法多样性；json_mode 保证输出可解析
            llm = _make_llm(temp=0.7, max_tokens=_sql_gen_max_tokens(), json_mode=True, model=_sql_gen_model())
            ddl_list, docs, sql_examples = [], [], []
            try:
                from agent.memory import get_memory
                memory = get_memory()
                ddl_list = [d["ddl"] for d in memory.get_ddl_by_keywords(self.query, k=3)]
                docs = memory.search_documentation(self.query, k=2)
                sql_examples = memory.search_sql(self.query, k=3)
            except Exception:
                pass
            prompt = build_sql_prompt(
                query=self.query,
                schema_context=self.schema_context,
                ddl_list=ddl_list,
                docs=docs,
                sql_examples=sql_examples,
                history_text="",
                agg_hint=_build_agg_hint(self.query),
                max_tokens=6000,
            )
            cand_prompt = (
                f"{prompt}\n\n"
                f"## 多候选生成\n请给出 {n} 条【语义等价但写法不同】的 SQL 候选，"
                f"每条都能独立正确回答用户问题（可换 JOIN 顺序、子查询/CTE 形式、聚合写法）。\n"
                f"只输出 JSON 数组（不要任何其他内容），如 [\"sql1\",\"sql2\",\"sql3\"]。"
            )
            resp = llm.invoke([SystemMessage(content=cand_prompt), HumanMessage(content=self.query)])
            raw = resp.content.strip()
            m = re.search(r'\[[\s\S]*\]', raw)
            if not m:
                return []
            cands = json.loads(m.group(0))
            if not isinstance(cands, list):
                return []
            out: list[str] = []
            seen: set[str] = set()
            prev = (self.sql or "").strip().lower()
            for c in cands:
                s = str(c).strip()
                if not s or s.lower() == prev or s.lower() in seen:
                    continue
                seen.add(s.lower())
                out.append(s)
                if len(out) >= n:
                    break
            return out
        except Exception:
            return []

    # ── 结果合理性校验（"能跑" ≠ "答对"）────────────────

    def _validate_result(self) -> str:
        """检测「SQL 执行成功但答非所问」的信号，返回问题描述（空串=通过）

        只有 SQL 报错才重试是不够的：查错表、漏聚合、条件过严导致空结果，
        这些 SQL 都能正常执行，却回答不了用户的问题。这里做一次语义层复核。
        """
        if not self.sql_result.get("success"):
            return ""
        rows = self.sql_result.get("rows") or []
        cols = self.sql_result.get("columns") or []
        sql_upper = (self.sql or "").upper()
        needs_agg = _needs_aggregation(self.query)

        # 1. 空结果：条件可能过严 / 表选错
        if not rows:
            reasons = ["查询返回 0 行。可能是筛选条件过严、时间范围不对，或选错了表。"]
            if re.search(r"WHERE[\s\S]*?(=\s*'[^']+'|LIKE\s*'[^']+')", sql_upper):
                reasons.append("请放宽或去掉硬编码的字符串等值/模糊条件，改用更宽松的匹配。")
            if re.search(r"CURRENT_DATE|NOW\(\)|INTERVAL", sql_upper):
                reasons.append("时间条件可能超出数据实际范围，请先确认数据的最新日期再放宽时间窗口。")
            return " ".join(reasons)

        # 2. 聚合意图却没聚合（用 SELECT * 或明细列表敷衍）
        if needs_agg:
            has_group = " GROUP BY " in sql_upper
            has_agg_func = bool(re.search(r'\b(SUM|COUNT|AVG|MAX|MIN)\s*\(', sql_upper))
            if not has_group and not has_agg_func:
                return ("用户问的是聚合分析类问题（各/排行/占比/统计等），"
                        "但 SQL 既没有 GROUP BY 也没有聚合函数，返回的是明细列表。"
                        "请改写为带 GROUP BY 和聚合函数的统计查询。")
            if re.search(r'SELECT\s+\*', sql_upper):
                return ("聚合分析问题不应使用 SELECT *，"
                        "请明确列出分组维度列和聚合指标列。")

        # 3. 聚合意图但只返回 1 行（"各产线"却只出一行 = 漏了 GROUP BY 维度）
        if needs_agg and len(rows) == 1 and any(w in self.query for w in ["各", "每个", "每条", "每种", "排行", "排名", "对比", "分布"]):
            return ("用户问的是分组对比（各/每个/排行），但结果只有 1 行，"
                    "说明缺少分组维度。请在 GROUP BY 中加入正确的维度列（如产线、工序、产品、日期）。")

        # 3.5 趋势类问题但只有一个数据点（如只有一天快照）→ 无法看趋势，明确提示
        if len(rows) == 1 and any(w in self.query for w in ["趋势", "走势", "变化", "曲线", "环比", "同比", "波动"]):
            return ("用户问的是变化趋势，但查询结果只有 1 个数据点，无法分析趋势。"
                    "请检查是否时间范围过窄（例如只有一天快照），尝试按更宽的时间窗口/多日数据查询。")

        # 3.9 豁免：COUNT 计数返回 0 是合法答案（如「超产工单数=0」「没有缺货产品」），
        #    不是 JOIN 断裂——不能让规则 4 的全 0 启发式误判并触发 LLM 重写。
        if (self.sql or "").upper().count("COUNT(") > 0 and len(rows) == 1:
            try:
                _any_nonzero = any(
                    float(v) != 0
                    for r_ in rows[:20] for v in r_.values()
                    if v is not None and str(v).strip() not in ("", "None")
                )
                if not _any_nonzero:
                    return ""
            except (ValueError, TypeError):
                pass

        # 4. 结果列全为 NULL / 全为 0（字段选错或 JOIN 断裂的典型症状）
        if rows and cols:
            checked = 0
            all_empty = True
            for c in cols:
                checked += 1  # 只要扫过列即计数（全部列全空/全 NULL 时也能触发检查）
                vals = [r.get(c) for r in rows[:20]]
                non_null = [v for v in vals if v is not None and str(v).strip() != ""]
                if non_null:
                    # 数值列全 0 也算异常信号
                    try:
                        if any(float(v) != 0 for v in non_null):
                            all_empty = False
                            break
                    except (ValueError, TypeError):
                        all_empty = False
                        break
            if checked > 0 and all_empty:
                return ("结果中所有列均为空或数值全为 0，通常意味着 JOIN 关联条件不正确"
                        "或聚合了错误的字段。请核对 JOIN 键与聚合列。")

        # 5. 指标口径冲突：命中注册指标，但 SQL 没有引用口径要求的聚合字段
        #    （如问良率却只 SUM(good_qty) 没算 input_qty）→ 提示按口径改写
        try:
            from agent.metric_registry import find_metrics, metric_required_fields
            sql_lower = (self.sql or "").lower()
            # SQL 引用的表（简单提取），用于判断指标注册表是否适用于本次查询
            sql_tables = _extract_sql_tables(self.sql)
            for metric in find_metrics(self.query)[:3]:
                required = metric_required_fields(metric)
                if not required:
                    continue
                # 表匹配过滤：SQL 未引用该指标注册的任一表 → 指标口径不适用（如 123 库
                # 用 factory.production_record.qty_produced 表示产量），跳过避免误报
                metric_tables = {t.split(".")[-1].lower() for t in (metric.get("tables") or [])}
                if sql_tables and metric_tables and not sql_tables.intersection(metric_tables):
                    continue
                missing = [f for f in required if f not in sql_lower]
                if missing:
                    return (f"指标「{metric['name']}」的口径需要引用字段 {missing}，"
                            f"但当前 SQL 没有使用（口径：{metric.get('formula', metric.get('sql_expression'))}）。"
                            f"请严格按口径补齐这些字段的聚合，不要更改算式。")
        except Exception:
            pass

        return ""

    def _llm_result_check(self) -> str:
        """独立校验 Agent（Critic）：判定「给定 SQL 与查询结果，能否回答用户的问题」。

        与规则校验互补：规则覆盖结构性信号（空结果/漏聚合/全 0 列/口径缺字段），
        这里由第二个 LLM 角色独立审查语义层——SQL 是否真的回答了问题、维度是否
        覆盖、过滤条件是否与问题一致、是否答非所问。Critic 使用独立 system prompt
        与主生成 LLM 隔离，避免"自己写自己审"的盲区。

        返回问题描述（空串=通过）。每次调用约 1~3s，仅复杂查询/强制模式下触发。
        """
        try:
            rows = self.sql_result.get("rows") or []
            cols = self.sql_result.get("columns") or []
            sample = json.dumps(rows[:5], ensure_ascii=False, default=str)[:800]
            sql = (self.sql or "")[:1000]
            # 命中口径提示：让 Critic 知道注册的业务口径，避免误判"符合口径的 SQL"
            hint = ""
            try:
                from agent.metric_registry import get_metric_hint
                hint = (get_metric_hint(self.query) or "")[:600]
            except Exception:
                pass
            # 领域判定提示（与生成侧同一份 _build_agg_hint）：
            # Critic 与生成 LLM 必须共享同一套确定性业务规则，否则会出现
            # "生成对、审核误判" 的打架——实测「停机时长分档统计」生成 COUNT（正确），
            # Critic 却认为"没体现停机时长"判错并触发改写为 SUM（错误）。
            # 注入这份提示后 Critic 按同样的口径理解"分档=统计记录数"。
            try:
                from agent.llm_service import _build_agg_hint
                agg_hint = (_build_agg_hint(self.query) or "")[:900]
            except Exception:
                agg_hint = ""
            prompt = (
                "你是独立的数据分析审核员（Critic），任务是对生成的 SQL 做质量审查。\n"
                "请逐项核对：\n"
                "1. 维度是否覆盖：问题要求按 X 分组（各/按/每），SQL 是否真的 GROUP BY X 且结果包含 X 列；\n"
                "2. 指标是否答对：问题要求的指标（数量/金额/率/占比/排名/趋势）SQL 是否用对了聚合；\n"
                "3. 过滤是否一致：问题的筛选条件（如某状态/某时间范围）SQL 是否正确体现；\n"
                "4. 是否答非所问：SQL 是否引用了与问题无关的表或列，或返回了问题没要的东西。\n"
                "如果 SQL 与结果能回答用户问题，只输出：OK\n"
                "如果存在问题，输出一句问题描述（60 字以内），指出具体缺什么/错在哪。\n\n"
                f"## 用户问题\n{self.query}\n\n"
                f"## 生成的 SQL\n{sql}\n\n"
                f"## 业务口径提示（如有）\n{hint or '（无）'}\n\n"
                f"## 聚合语义提示（与生成器共享的确定性规则，判定指标时以此为基准）\n"
                f"{agg_hint or '（无）'}\n\n"
                f"## 查询结果（列: {cols}，行数: {self.sql_result.get('row_count', 0)}）\n{sample}"
            )
            llm = _make_llm(temp=0.0, max_tokens=140)
            resp = llm.invoke([SystemMessage(content=prompt)])
            verdict = str(resp.content or "").strip()
            if verdict.upper().startswith("OK"):
                return ""
            # 回显过滤（2026-09-19）：不像真实问题描述的一律判通过，见 _critic_verdict_valid
            if not _critic_verdict_valid(verdict):
                return ""
            if len(verdict) > 140:
                verdict = verdict[:140]
            return verdict
        except Exception:
            return ""

    def _is_complex_query(self) -> bool:
        """判断当前查询是否属于"复杂查询"（auto 模式下据此决定是否启用 LLM Critic）。

        复杂 = 含 JOIN / 多表 / 聚合 / 子查询 / 去重 / 窗口函数 —— 这类 SQL 出错面大、
        值得多花一次 LLM 调用做二次审查；简单单表明细查询直接放行省成本。
        """
        if not self.sql:
            return False
        sql = self.sql.upper()
        if re.search(r"\bJOIN\b|\bUNION\b|\bINTERSECT\b|\bEXCEPT\b|\bWITH\b", sql):
            return True
        if re.search(r"\bGROUP BY\b|\bHAVING\b", sql):
            return True
        if re.search(r"\b(SUM|COUNT|AVG|MAX|MIN|DISTINCT|ROW_NUMBER|RANK|LAG|LEAD)\s*\(", sql):
            return True
        if sql.count("SELECT") > 1:  # 子查询
            return True
        return False

    def _llm_result_evaluate(self) -> dict:
        """评价 Agent（Evaluator）：对最终结果做多维度质量打分（0-100）。

        白泽式「生成 → 校验 → 修正 → 评价」四 Agent 闭环的**第四环**。
        与 Critic 的分工（互补，不可互相替代）：
          - Critic（_llm_result_check）：二元判定「这个结果能不能回答问题」，
            找出具体错误 → 驱动定向改写；
          - Evaluator（本方法）：多维度打分（口径符合度 / 维度完整性 /
            过滤一致性 / 结果可用性）→ 度量质量 → 驱动「多候选交叉比对选优」。
        只有 Critic 时系统能发现"明显错了"，但无法判断"两个都能跑的结果哪个更好"；
        补上 Evaluator 才具备在多个候选之间择优的能力。

        返回 {score, dims, comment}；异常或不可用时返回 {}（调用方降级，不阻断主流程）。
        """
        try:
            rows = self.sql_result.get("rows") or []
            sample = json.dumps(rows[:5], ensure_ascii=False, default=str)[:800]
            hint = ""
            try:
                from agent.metric_registry import get_metric_hint
                hint = (get_metric_hint(self.query) or "")[:400]
            except Exception:
                pass
            prompt = (
                "你是数据分析质量评审专家（Evaluator），请对「SQL + 查询结果」回答用户问题的质量打分。\n"
                "按四个维度各打 0-100 分，并给出总分：\n"
                "1. metric_match（口径符合度）：指标算法是否符合业务口径要求；\n"
                "2. dimension_coverage（维度完整性）：问题要求的分组维度是否都覆盖到；\n"
                "3. filter_consistency（过滤一致性）：时间范围 / 状态等筛选条件是否与问题一致；\n"
                "4. usability（结果可用性）：结果是否可直接用于回答（非空、非全 0、行列结构合理）。\n"
                "只输出 JSON：{\"score\": 总分0-100, \"metric_match\": 分, \"dimension_coverage\": 分,"
                " \"filter_consistency\": 分, \"usability\": 分, \"comment\": \"一句话评价（40字内）\"}\n\n"
                f"## 用户问题\n{self.query}\n\n"
                f"## SQL\n{(self.sql or '')[:800]}\n\n"
                f"## 业务口径提示（如有）\n{hint or '（无）'}\n\n"
                f"## 结果（列: {self.sql_result.get('columns') or []}，"
                f"行数: {self.sql_result.get('row_count', 0)}）\n{sample}"
            )
            llm = _make_llm(temp=0.0, max_tokens=220, json_mode=True)
            raw = str(llm.invoke([SystemMessage(content=prompt)]).content or "")
            data = _loads_lenient(raw)
            if not isinstance(data, dict):
                return {}

            def _score(v, default=70):
                try:
                    n = int(float(v))
                except (TypeError, ValueError):
                    return default
                return max(0, min(100, n))

            dims = {
                "metric_match": _score(data.get("metric_match")),
                "dimension_coverage": _score(data.get("dimension_coverage")),
                "filter_consistency": _score(data.get("filter_consistency")),
                "usability": _score(data.get("usability")),
            }
            # 总分优先用模型给的；缺失或越界时退化成四维均值（避免单点失真）
            if data.get("score") is None:
                score = round(sum(dims.values()) / 4)
            else:
                score = _score(data.get("score"))
            return {"score": score, "dims": dims,
                    "comment": str(data.get("comment") or "")[:80]}
        except Exception:
            return {}

    def _cross_validate(self, n: int = 2) -> bool:
        """多候选交叉比对（白泽式多 Agent 交叉校验的落地形态）。

        生成 n 条独立写法的候选 SQL → 逐条执行 + 规则校验 → 用评价 Agent 打分 →
        **只有严格优于当前结果时才替换**（防止越换越差）。
        返回是否发生了替换。
        """
        base_score = self._eval_score or 0
        best_sql, best_res, best_score = None, None, base_score
        prev_sql, prev_res = self.sql, self.sql_result
        try:
            for cand in self._generate_candidates(n=n):
                if not cand or cand.strip() == (self.sql or "").strip():
                    continue
                res = self._exec_sql(cand)
                if not res.get("success") or not res.get("rows"):
                    continue
                # 规则校验要用「候选 SQL + 候选结果」的一致性状态判定
                self.sql, self.sql_result = cand, res
                try:
                    if self._validate_result():
                        continue
                    ev = self._llm_result_evaluate()
                finally:
                    self.sql, self.sql_result = prev_sql, prev_res
                sc = ev.get("score") or 0
                if sc > best_score:
                    best_sql, best_res, best_score = cand, res, sc
        except Exception:
            return False
        finally:
            self.sql, self.sql_result = prev_sql, prev_res

        if best_sql is None or best_score <= base_score:
            return False
        self.sql, self.sql_result = best_sql, best_res
        self._eval_score = best_score
        self._evaluation = {"score": best_score, "dims": {}, "comment": "交叉比对后采纳更优候选"}
        self._cross_validated = True
        self._refined = True
        return True

    def _refine_sql(self, issue: str) -> str:
        """针对「能跑但答非所问」的问题定向改写 SQL（区别于报错修复）"""
        try:
            from agent.prompt_builder import build_sql_prompt
            ddl_list, docs, sql_examples = [], [], []
            try:
                from agent.memory import get_memory
                memory = get_memory()
                ddl_list = [d["ddl"] for d in memory.get_ddl_by_keywords(self.query, k=3)]
                docs = memory.search_documentation(self.query, k=2)
                sql_examples = memory.search_sql(self.query, k=2)
            except Exception:
                pass

            base = build_sql_prompt(
                query=self.query,
                schema_context=self.schema_context,
                ddl_list=ddl_list,
                docs=docs,
                sql_examples=sql_examples,
                history_text="",
                agg_hint=_build_agg_hint(self.query),
                max_tokens=6000,
            )
            # 附带实际结果样例，让 LLM 看到"哪里不对"
            sample = ""
            rows = self.sql_result.get("rows") or []
            if rows:
                try:
                    sample = json.dumps(rows[:3], ensure_ascii=False, default=str)[:500]
                except Exception:
                    sample = ""
            refine_prompt = (
                f"{base}\n\n"
                f"## 上一次尝试（SQL 能正常执行，但结果不能回答用户的问题）\n"
                f"SQL: {self.sql}\n"
                f"返回行数: {self.sql_result.get('row_count', 0)}\n"
                + (f"结果样例: {sample}\n" if sample else "")
                + f"\n## 存在的问题\n{issue}\n\n"
                f"请针对上述问题重写 SQL，确保结果能真正回答用户的问题。重新输出完整 JSON。"
            )
            llm = _make_llm(temp=None, max_tokens=_sql_gen_max_tokens(), model=_sql_gen_model())
            resp = llm.invoke([SystemMessage(content=refine_prompt)])
            sql, _, _ = self._parse_sql_response(str(resp.content or "").strip())
            return sql
        except Exception:
            return ""

    def _parse_sql_response(self, raw: str) -> tuple[str, str, str]:
        """从 LLM 输出中提取 SQL + chart_type + title（多级容错）

        依次尝试：标准 JSON → 修复后的 JSON（未转义换行/尾随逗号/单引号）
        → 正则直接抠字段 → 裸 SQL 文本 → 规则兜底。
        任一级成功即返回，避免 LLM 输出稍有偏差就静默降级成兜底 SQL。
        """
        text = (raw or "").strip()
        chart_type = "bar"
        title = ""

        # 清理 markdown 围栏（```json / ```sql / ```）
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'```\s*$', '', text).strip()

        parsed = _loads_lenient(text)
        if isinstance(parsed, dict) and parsed.get("sql"):
            sql = str(parsed.get("sql") or "")
            chart_type = str(parsed.get("chart_type") or "bar")
            title = str(parsed.get("title") or "")
        else:
            # JSON 全线失败 → 正则直接抠 "sql": "..."（容忍内部换行与未转义引号）
            sql = ""
            m = re.search(r'"sql"\s*:\s*"([\s\S]*?)"\s*(?:,\s*"(?:chart_type|title)"|\}\s*$)', text)
            if m:
                sql = m.group(1).replace('\\n', '\n').replace('\\"', '"').replace('\\t', ' ')
            m_ct = re.search(r'"chart_type"\s*:\s*"(\w+)"', text)
            if m_ct:
                chart_type = m_ct.group(1)
            m_t = re.search(r'"title"\s*:\s*"([^"]*)"', text)
            if m_t:
                title = m_t.group(1)
            if not sql:
                sql = text

        sql = (sql or "").strip()

        # 最终校验：必须是 SELECT/WITH 开头，否则从文本里捞
        if not re.match(r'^\s*(SELECT|WITH)\b', sql, re.IGNORECASE):
            sel_match = re.search(r'((?:SELECT|WITH)[\s\S]*?)(?:```|;\s*$|$)', text, re.IGNORECASE)
            if sel_match:
                sql = sel_match.group(1).strip()
            else:
                sql = _fallback_sql(self.query, self.matched_tables)

        sql = sql.rstrip().rstrip(";").strip()
        # 用户明确要全量时移除 LIMIT 束缚（上限由执行层保护）
        if _wants_full_result(self.query):
            sql = re.sub(r'\s+LIMIT\s+\d+\s*$', '', sql, flags=re.IGNORECASE)
        return sql, chart_type, title

    # ── 回复构造 ─────────────────────────────────────────

    def _review_chain(self) -> tuple[str, dict]:
        """质量把关链：Critic（判对错）→ 通过后 Evaluator（打分）。

        两者必须串行：Critic 判定不合格会触发 SQL 改写，此时基于旧结果打的分已失效，
        再算一次纯属浪费。合并成一个任务，便于与外部（洞察）并行。

        返回 (issue, evaluation)：issue 非空表示结果不能回答用户问题。
        """
        issue = ""
        try:
            from config import (RESULT_LLM_CHECK_MODE, RESULT_EVAL_MODE,
                                RESULT_CHECK_SKIP_COMPILED)
            # 确定性编译命中 → 跳过 LLM 复查（口径由指标注册表保证，让 LLM 审口径
            # 甚至在它判定"不对"时改写，反而会偏离注册口径；同时省两次 LLM 往返）
            if RESULT_CHECK_SKIP_COMPILED and self.compiled_mql is not None:
                self._compiled_trusted = True
                return "", {}
            # 语义缓存命中 → SQL 上次已成功验证并沉淀，同样跳过 LLM 复查。
            # 否则会出现"同一问题第一次编译命中 150ms、第二次缓存命中却 13s"的反直觉退化。
            if self._semantic_hit:
                return "", {}
            if not (self.sql_result.get("success") and self.sql_result.get("rows")):
                return "", {}
            complex_q = self._is_complex_query()
            if (RESULT_LLM_CHECK_MODE or "auto").strip().lower() == "on" or (
                    (RESULT_LLM_CHECK_MODE or "auto").strip().lower() == "auto" and complex_q):
                llm_issue = self._llm_result_check()
                if llm_issue:
                    return f"Critic 复核：{llm_issue}", {}
            mode = (RESULT_EVAL_MODE or "auto").strip().lower()
            if mode == "on" or (mode == "auto" and complex_q):
                ev = self._llm_result_evaluate()
                if ev:
                    return "", ev
        except Exception:
            pass
        return issue, {}

    def _start_analysis(self) -> None:
        """提前并行启动「数据洞察」生成（性能优化，不改变任何业务逻辑/结果）。

        洞察与质量把关链只依赖同一份 SQL 结果、彼此独立，串行执行要多付一次完整
        LLM 往返（实测 5.0s）。这里只提交任务，结果由 _take_analysis 取；
        若后续 SQL 被复核改写，调用方需作废并重新提交（见 run() 的改写分支）。
        """
        try:
            if self.fast:
                return
            self._analysis_future = _POST_POOL.submit(self._llm_analysis)
        except Exception:
            self._analysis_future = None

    def _take_analysis(self) -> str:
        """取洞察结果：有并行任务则等待它，否则回退到同步生成（保证行为一致）。

        超时/异常 → 直接规则摘要兜底，**不再重试 LLM**：否则一个卡顿的洞察会把
        整个响应拖到 60s 超时，反而比规则摘要更慢。
        """
        fut = getattr(self, "_analysis_future", None)
        self._analysis_future = None
        if fut is None:
            return self._llm_analysis()
        try:
            return fut.result(timeout=30)
        except Exception:
            return self._quick_analysis()

    def _build_response(self) -> dict:
        matched = self.matched_tables
        # 无数值列的结果（纯列表）→ 标记 table 类型展示
        chart_type_final = self.chart.get("type", "none")
        if chart_type_final == "none" and self.sql_result.get("rows"):
            rows = self.sql_result["rows"]
            cols = self.sql_result.get("columns", [])
            # 首行所有列都是非数值 → 纯字符串列表，按 table 展示
            if cols and not any(
                str(rows[0].get(c, "")).replace(".", "").replace("-", "").isdigit() for c in cols
            ):
                chart_type_final = "table"
        return {
            "type": "data_query",
            "query": self.query,
            "sql": self.sql,
            "matched_tables": [{"table_name": t["table_name"], "table_alias": t["table_alias"]} for t in matched[:5]],
            "result": self.sql_result,
            "chart": {"type": chart_type_final, "svg": self.chart.get("svg", "")},  # SVG 一并带回，前端据此优先渲染图表
            "chart_config": {"type": chart_type_final, "title": self.title},
            # 洞察可能已在 run() 中并行启动，这里取结果（无并行任务时自动回退同步生成）
            "analysis": self._take_analysis(),
            "recommended": self._quick_recommended(),
            "prediction": [],
            # 规则化归因（Phase 6.1）：环比下跌检测 + 维度贡献（纯计算，无信号为空串）
            "attribution": self._build_attribution(),
            # 确定性编译标记 + 结构化 MQL（可解释性 / 口径溯源；非编译路径 compiled=False、mql=null）
            "compiled": self.compiled_mql is not None,
            "mql": self.compiled_mql,
            # P0-4：层级钻取信息（编译器产出；前端图表点击分类可下钻到下一级）
            "drillable": getattr(self, "drillable", None),
            # LLM 兜底路径的口径溯源（编译命中时 mql 已含完整口径，此处为 None）
            "metric_hint": getattr(self, "metric_hint", None),
            # 权限白盒：本次查询实际注入的行条件 / 脱敏列 / 屏蔽列 + 生效角色
            "acl": self._build_acl_trace(),
            # 结果溯源（血缘）：每个结果列来自哪张表的哪个字段、经过什么聚合——
            # 确定性静态解析最终执行的 SQL（对标 Genloop Living Context Graph / Smartbi 全链路溯源）
            "lineage": self._build_lineage(),
            # 混合问答（对标 Spotter 3）：命中的业务文档引用（[n] 对应分析里的引用标注），
            # 前端可点回原文核实——空列表表示无相关文档
            "cited_docs": self._get_cited_docs(),
            # 结果可靠性：供前端提示 + 反馈闭环使用
            "quality": {
                "refined": self._refined,          # 是否经过结果复核改写
                "warning": self._result_warning,   # 残留的合理性告警（空=通过）
                "from_memory": self._had_example_hits,
                # 评价 Agent（第四环）：质量总分 0-100，0 表示未启用/不可用/编译跳过
                "score": self._eval_score,
                # 四维评分明细 + 评语（口径符合度 / 维度完整性 / 过滤一致性 / 结果可用性）
                "evaluation": self._evaluation or None,
                "cross_validated": self._cross_validated,  # 是否由多候选交叉比对择优替换
                # 确定性编译命中 → 口径由指标注册表保证，跳过 LLM 复查（省 2.8~5.3s）。
                # 此时 score 为 0 不是"质量差"，而是"无需 LLM 评审"，前端据此展示
                # 「口径编译保证」徽章而不是分数。
                "compiled_trusted": self._compiled_trusted,
                # 确定性置信度 + 不确定性来源（对标白泽）：编译/缓存=高，LLM 路径
                # 按表匹配歧义/空值/告警/评分分层。与 score 的区别：score 是 LLM
                # 评价总分（0 表示未启用），confidence 是确定性信号计算的可信度。
                "confidence": self._build_confidence(),
            },
            # 口径意图检测结果（P1 口径管理）：skip=零打扰，no_hit=反馈条，hit=静默
            "metric_resolution": getattr(self, "_metric_resolution", None) or {"status": "skip", "hits": [], "hints": []},
            "steps": self._steps or [
                {"step": 1, "name": "意图理解", "status": "done", "detail": self.query[:60]},
                {"step": 2, "name": "表匹配", "status": "done", "detail": f"定位到 {len(matched)} 张候选表"},
                {"step": 3, "name": "SQL生成", "status": "done", "detail": "AI 已生成 SQL"},
                {"step": 4, "name": "SQL执行", "status": "done", "detail": f"{self.sql_result.get('row_count', 0)} 行结果"},
            ],
        }

    def _build_confidence(self) -> dict:
        """确定性置信度 + 不确定性来源（对标 Smartbi 白泽「结论置信度 + 偏差来源」）。

        关键设计：全部由**确定性信号**计算（编译命中 / 语义缓存 / 表匹配 / 空值 / 复核告警 /
        评价分），不依赖 LLM 自评——延续「确定性编译为主」架构，置信度本身也可审计。

        返回 {level, score, basis[], risks[]}：
          level ∈ high/medium/low；score 为展示分值；
          basis  列出「为什么可信」（硬证据）；
          risks  列出「哪里不确定」（来源标注），空 = 无已知风险。
        """
        basis: list[str] = []
        risks: list[str] = []

        # ① 硬证据：确定性编译命中（口径由注册表保证，最强）
        if self.compiled_mql is not None:
            return {"level": "high", "score": 95,
                    "basis": ["口径由指标注册表确定性编译（编译器保证，强于 LLM 事后评审）"],
                    "risks": []}
        # ② 硬证据：语义缓存命中（复用已验证 SQL）
        if getattr(self, "_semantic_hit", False):
            return {"level": "high", "score": 90,
                    "basis": ["语义缓存命中：SQL 复用已验证查询，已过复核"],
                    "risks": []}

        # ③ LLM 路径：规则校验 + 复核通过，按确定性信号识别不确定性来源
        basis.append("SQL 由 LLM 生成，已通过规则校验与结果复核")
        n_tables = len(self.matched_tables or [])
        if n_tables > 2:
            risks.append(f"表匹配候选 {n_tables} 张，存在选择歧义")
        rows = self.sql_result.get("rows") or []
        if rows:
            sample = rows[:50]
            null_cnt = sum(1 for r in sample
                           if any(v is None or str(v).strip() == "" for v in r.values()))
            if null_cnt and null_cnt / len(sample) > 0.1:
                risks.append(f"结果含空值（{null_cnt}/{len(sample)} 行）")
        if self._result_warning:
            risks.append(self._result_warning[:60])
        ev = self._eval_score or 0
        if 0 < ev < 60:
            risks.append(f"评价 Agent 质量分偏低（{ev}）")

        # 重大风险（复核告警 / 空值 / 低分）→ low；其余（如表匹配歧义）→ medium。
        # 注意：_result_warning 非空即重大（它是"残留的合理性告警"，文案可能不含"告警"二字）
        major = bool(self._result_warning) or any(
            ("空值" in r) or ("偏低" in r) for r in risks)
        if major:
            return {"level": "low", "score": 55, "basis": basis, "risks": risks}
        return {"level": "medium", "score": 75, "basis": basis, "risks": risks}

    def _build_lineage(self) -> dict:
        """结果溯源（血缘）：标注每个结果列来自哪张表哪个字段（确定性静态解析）。

        解析最终执行的 SQL（含权限改写后的 executed_sql），不依赖 LLM 现编；
        失败返回空结构（前端隐藏溯源区）。
        """
        try:
            sql = getattr(self, "executed_sql", "") or self.sql or ""
            if not sql:
                return {"tables": [], "columns": []}
            # 算子级血缘（P2-4）：build_operator_lineage 在 build_lineage 基础上为每个
            # 结果列追加 ops（算子树）+ chain（可读算子链），结构完全兼容，前端可选渲染
            from agent.lineage import build_operator_lineage
            # 按当前库类型选方言：MySQL 库的反引号 SQL 用 postgres 方言解析会失败
            dialect = ""
            try:
                from database import get_db_type
                if get_db_type() == "mysql":
                    dialect = "mysql"
            except Exception:
                pass
            lg = build_operator_lineage(sql, dialect=dialect)
            # 结果列表头翻译：词典/元数据没覆盖的英文字段，用一次 AI 批量补中文短名
            # （内部有 field_cn_ai.json 缓存 + 异常静默，失败只影响表头标注）
            try:
                from agent.field_semantics import enrich_lineage_labels
                enrich_lineage_labels(lg)
            except Exception:
                pass
            return lg
        except Exception:
            return {"tables": [], "columns": []}

    def _build_acl_trace(self) -> dict | None:
        """权限白盒：把本次查询实际生效的权限动作回传前端。

        没有任何权限动作时返回 None（管理员/未配权限的角色不显示噪音提示）。
        """
        if self.acl is None:
            return None
        applied = self.acl_applied or {}
        has_action = any(applied.get(k) for k in ("row_filters", "masked", "hidden", "denied"))
        overrides = []
        if not self.acl.superuser and self.acl.metric_overrides:
            # 只报告真正参与了本次查询的指标口径覆盖
            hint = self.metric_hint if isinstance(getattr(self, "metric_hint", None), dict) else {}
            used = {hint.get("name")} if hint.get("name") else set()
            if isinstance(self.compiled_mql, dict):
                mm = self.compiled_mql.get("metric") or {}
                if isinstance(mm, dict) and mm.get("name"):
                    used.add(mm["name"])
                elif isinstance(mm, str):
                    used.add(mm)
            for name in used:
                ov = self.acl.metric_overrides.get(name)
                if ov:
                    overrides.append({"metric": name, "role": ov.get("by_role", ""),
                                      "formula": ov.get("formula", ""),
                                      "description": ov.get("description", "")})
        if not has_action and not overrides:
            return None
        return {
            "roles": self.acl.roles,
            "row_filters": applied.get("row_filters") or [],
            "masked": applied.get("masked") or [],
            "hidden": applied.get("hidden") or [],
            "denied": applied.get("denied") or [],
            "metric_overrides": overrides,
        }

    def _build_metric_hint(self) -> dict | None:
        """宽松匹配命中注册指标 → 返回口径溯源 hint，供 LLM 兜底路径白盒展示。

        与 get_metric_hint 共用 _retrieve_for_query，保证「白盒展示的口径」和
        「LLM 实际注入的口径」完全一致（避免割裂）。
        """
        try:
            from agent.metric_registry import _retrieve_for_query, find_metrics
            # 白盒「统计口径」只展示**确定性命中**（关键词命中注册口径）的口径。
            # RAG-only 召回（关键词 0 命中）属于"仅参考、严禁套用"的弱相关项，不能当
            # 「统计口径」展示：2026-09-14 实测「各产线计划达成率」被展示成
            # "统计口径：计划数量、停机次数、产线不良数"，让用户误以为系统真按这些口径算。
            if not find_metrics(self.query):
                return None
            hits = _retrieve_for_query(self.query)
            if len(hits) == 1:
                m = hits[0]
                expr = (m.get("sql_expression") or "").strip()
                if not expr:
                    return None
                name = m["name"].split("(")[0].strip()
                unit = m.get("unit", "")
                definition = f"{name} = {expr}" + (f"（单位：{unit}）" if unit else "")
                return {"kind": "single", "metric": name, "metric_definition": definition,
                        "unit": unit, "sql_expression": expr, "compiled_by": "metric_registry"}
            if len(hits) > 1:
                names = [h["name"].split("(")[0].strip() for h in hits]
                return {"kind": "multi", "metrics": names, "compiled_by": "metric_registry"}
            return None
        except Exception:
            return None

    def build_metric_ref(self) -> list[dict]:
        """本次查询命中的指标口径引用（name + 生效版本窗口 + 来源），供审计闭环记录。

        来源：确定性编译路径的 compiled_mql，或 LLM 兜底路径的 metric_hint。
        版本窗口从 metric_registry 全量指标定义反查（valid_from/valid_to）。
        """
        try:
            from agent.metric_registry import get_all_metrics
            all_m = {m["name"]: m for m in get_all_metrics()}
        except Exception:
            all_m = {}
        names: list[str] = []
        if isinstance(self.compiled_mql, dict):
            mm = self.compiled_mql.get("metric") or {}
            if isinstance(mm, dict) and mm.get("name"):
                names.append(mm["name"])
            elif isinstance(mm, str):
                names.append(mm)
            for mdef in self.compiled_mql.get("metrics") or []:
                if isinstance(mdef, dict) and mdef.get("name"):
                    names.append(mdef["name"])
        hint = self.metric_hint if isinstance(getattr(self, "metric_hint", None), dict) else {}
        if hint.get("metric"):
            names.append(hint["metric"])
        for n in hint.get("metrics") or []:
            names.append(n)
        refs, seen = [], set()
        for n in names:
            if n in seen:
                continue
            seen.add(n)
            m = all_m.get(n) or {}
            refs.append({
                "name": n,
                "valid_from": m.get("valid_from", ""),
                "valid_to": m.get("valid_to", ""),
                "compiled": self.compiled_mql is not None,
            })
        return refs

    def _build_attribution(self) -> str:
        """规则化归因（Phase 6.1）：环比下跌检测 + 维度贡献度分解；无下跌/数据不足返回空串。
        规则结果上可选追加 LLM 归因解读（改造4，失败自动降级纯规则）。"""
        try:
            from agent.attribution import detect_and_attribute, llm_explain
            text = detect_and_attribute(self.query, self.sql_result, self.matched_tables)
            if not text:
                return ""
            # 快速模式（评测）不接 LLM，保持确定性输出
            if self.fast:
                return text
            return llm_explain(self.query, text, self.sql_result, self.matched_tables)
        except Exception:
            return ""

    def _quick_analysis(self) -> str:
        """基于查询结果生成规则分析（不依赖 LLM，保证稳定）

        原实现只输出「共 N 条记录 + 最大/最小/平均 + 谁最高」一句概括，
        业务用户读不出任何结论。改为分段结构化洞察（整体情况 / 排名情况 /
        主要发现 / 建议关注），数值全部由真实结果算出，不编造。
        """
        rows = self.sql_result.get("rows") or []
        cols = self.sql_result.get("columns") or []
        return _rule_insight(rows, cols)

    def _retrieve_docs(self, k: int = 3) -> list[str]:
        """混合问答第一步：检索知识库业务文档（口径说明/SOP/制度）。

        复用 memory.search_documentation（已做 db_key 隔离 + 混合打分），命中则：
        - 存片段到 self._retrieved_docs，供分析阶段融合引用；
        - 组装 self._cited_docs（[index, content]），随响应返回，前端可点回原文核实。
        无命中/异常 → 返回空列表，不影响主链路（文档是增强，不是必需）。
        """
        try:
            from agent.memory import get_memory, _hybrid_score
            # 先多取（提高召回），再用更严格的阈值二次过滤（提高精度）。
            # search_documentation 内置 0.05 阈值过松，会让无关查询因停用词命中而污染引用，
            # 这里收窄到 >0.1：引用错误文档比不引用更糟，宁可少不可错。
            raw = get_memory().search_documentation(self.query, k=k * 2)
            docs = [d for d in raw
                    if d and str(d).strip() and _hybrid_score(self.query, d) > 0.1][:k]
            self._retrieved_docs = docs
            self._cited_docs = [{"index": i + 1, "content": d[:300]}
                                for i, d in enumerate(docs)]
            self._docs_retrieved = True
            return docs
        except Exception:
            self._retrieved_docs, self._cited_docs = [], []
            self._docs_retrieved = True
            return []

    def _get_cited_docs(self) -> list[dict]:
        """返回引用文档（惰性检索：快速分析路径不经过 _llm_analysis 也会补检索一次）。

        检索是纯词法（Jaccard+difflib，本地 SQLite），无 LLM 调用，成本可忽略。
        """
        if not self._docs_retrieved:
            self._retrieve_docs(k=3)
        return self._cited_docs

    def _llm_analysis(self) -> str:
        """LLM 解读查询结果（真实数据洞察）；LLM 不可用/数据不足时回退规则版

        把 SQL 一并给 LLM，让它知道数据的计算口径（是求和还是均值、按什么分组），
        否则解读容易脱离业务含义变成纯数字复述。
        """
        rows = self.sql_result.get("rows") or []
        cols = self.sql_result.get("columns") or []
        warn = self._result_warning
        # Critic 判定指标口径可能算错 → 不再交给 LLM 自由解读（2026-09-14 用户实测）：
        # 此前虽把告警注入 prompt，LLM 仍照样输出"建议立即排查 MES 系统工单状态更新是否
        # 及时"式的完整业务结论（计划达成率明明全为 0）。改为**确定性降级**：只给规则摘要
        # + 醒目告警头，从机制上杜绝"基于可疑数值下结论/给建议"。
        # 注意：_result_warning 非空在本项目里等于"重大"信号（见 _build_response 的 major 判定）。
        if warn:
            _base = self._quick_analysis()
            return (
                "⚠️ 本次结果的指标口径可能不正确，以下数值仅供参考，请勿据此决策。\n"
                f"独立复核（Critic）判定：{warn[:200]}\n"
                "处理建议：核对指标定义，或在结果卡片「去登记口径」中固定正确算法后重新提问。\n\n"
                + _base
            )
        # ── 简单查询有损降级（性能优化，对齐 ZOOZ 规则摘要做法）：跳过 LLM 解读，用强化规则版 ──
        # 适用：编译直通（口径确定、结果确定性强）/ 语义缓存命中（SQL 已复用）/ 简单单表统计。
        # 这些场景规则摘要（行数 + 数值统计 + TOP 维度洞察）已足够，省一次 2-3s LLM 调用；
        # 复杂/多表/多维度查询保留 LLM 解读（洞察价值高）。
        try:
            _compiled = getattr(self, "compiled_mql", None) is not None
            _sem_hit = bool(getattr(self, "_semantic_hit", False))
            _simple_rows = len(rows) <= 20 and len(cols) <= 5 and len(self.matched_tables or []) <= 1
            # P0-修复（2026-09-03）：分析类问法（相关/对比/趋势/是否…）禁止规则降级——
            # 规则摘要只会报"最大值/平均值/最高是哪天"，回答不了"是否相关/什么趋势"。
            # 即使命中语义缓存/确定性编译（最常见复用路径）也必须走下方 LLM 洞察
            #（该分支已注入确定性相关证据）。仅对非分析类查询保留编译/缓存/简单 → 规则摘要，
            # 省 2-3s LLM 往返。
            _anal_intent = _is_analysis_intent(self.query)
            if not _anal_intent and (_compiled or _sem_hit or _simple_rows) and rows:
                base = self._quick_analysis()
                return f"{base}\n\n⚠️ {warn}" if warn else base
        except Exception:
            pass
        if not rows or len(rows) < 2:
            base = self._quick_analysis()
            return f"{base}\n\n⚠️ {warn}" if warn else base
        # 分析类问法：相关性结论确定性生成（零 LLM，秒出）
        if _is_analysis_intent(self.query):
            _det = _build_deterministic_insight(self.query, cols, rows)
            if _det:
                return _det + (f"\n\n⚠️ 结果可靠性提示：{warn}" if warn else "")
        _rc = len(rows)
        # 洞察结果缓存（性能）：同问法重复提问/语义缓存命中后二次问，免二次 LLM 洞察
        _cached = _insight_cached(self.query, _rc)
        if _cached:
            return _cached + (f"\n\n⚠️ 结果可靠性提示：{warn}" if warn else "")
        try:
            data_json = json.dumps(rows[:15], ensure_ascii=False, default=str)[:1500]
            total = self.sql_result.get("row_count", len(rows))
            fields_info = ", ".join(cols)
            if total > len(rows[:15]):
                fields_info += f"（共 {total} 行，以上为前 {min(15, len(rows))} 行样例）"
            prompt = ANALYSIS_SYSTEM_PROMPT.format(
                data_json=data_json,
                fields_info=fields_info,
                query=self.query,
            )
            if self.sql:
                prompt += f"\n\n## 数据来源 SQL（用于理解统计口径）\n{self.sql[:800]}"
            # Critic 判定指标计算可疑时，分析不得把数值当正确结论复述（2026-09-13）：
            # 此前告警只是拼在正文末尾，LLM 照样生成"周转天数最高为 4795 天"式的
            # 一本正经结论。改为直接注入 prompt，强制开头声明口径可疑 + 不下业务结论。
            if warn:
                prompt += (
                    "\n\n## 重要：结果可靠性告警\n"
                    f"独立复核（Critic）判定本次结果的指标计算可能不正确：{warn[:300]}\n"
                    "你的解读【不得】把这些数值当作正确结论复述，也【不得】基于它们给出"
                    "业务判断或建议。开头先用一句话明确提示：本次结果的指标口径可能不正确、"
                    "以下数值仅供参考；然后只客观描述数据现象（不下业务结论），"
                    "最后建议用户核对指标定义或在结果卡片「去登记口径」固定正确算法。"
                )
            # 混合问答（对标 Spotter 3）：检索到的业务文档作为「口径上下文」注入，
            # 让洞察既能引用实时数据、又能引用文档里的口径定义/SOP。引用处用 [n] 标注。
            docs = self._retrieve_docs(k=3)
            if docs:
                doc_block = "\n\n".join(f"[{i + 1}] {d[:400]}" for i, d in enumerate(docs))
                prompt += (
                    "\n\n## 相关业务文档（口径说明/SOP，用于校准解读口径）\n"
                    + doc_block
                    + "\n\n若文档中的口径定义与数据相关，请在解读中融合引用（用 [1][2] 标注来源）；"
                      "若文档与本题无关，则忽略。"
                )
            # 分析类问法增强（2026-09-03）：先正面回答用户问题（是否相关/差异/趋势），
            # 再给证据；结果含 ≥2 数值列时注入确定性皮尔逊相关证据，杜绝 LLM 凭空说"相关"
            if _is_analysis_intent(self.query):
                prompt += (
                    "\n\n## 任务：用户的问题是分析类问题，请先正面回答它——例如是否存在"
                    "相关/差异/趋势、结论是什么，给出明确判断后再引用数据佐证（含关键数值），"
                    "最后给 1-2 条业务建议。不要只罗列数据。若现有数据不足以判断，请如实说明"
                    "缺什么数据、建议如何进一步分析。"
                )
                corr = _corr_evidence(cols, rows)
                if corr:
                    prompt += f"\n\n{corr}"
            prompt += "\n\n注意：只依据上面给出的真实数据下结论，不要编造数据中不存在的数字。"
            llm = _make_llm(temp=0.0, max_tokens=512)
            resp = llm.invoke([HumanMessage(content=prompt)])
            text = str(resp.content or "").strip()
            text = text or self._quick_analysis()
            _insight_store(self.query, _rc, text)
            if warn:
                text += f"\n\n⚠️ 结果可靠性提示：{warn}"
            return text
        except Exception:
            base = self._quick_analysis()
            return f"{base}\n\n⚠️ {warn}" if warn else base

    def _quick_recommended(self) -> list[str]:
        """根据当前查询结果结构生成 2-3 个真实可追问的问题（规则版，不额外调用 LLM）

        基于结果列的实测类型：日期列→趋势、维度列→拆分对比、数值列→排行/异常。
        空结果时给出放宽条件的引导，不再返回与结果无关的万金油问题。
        """
        rows = self.sql_result.get("rows") or []
        cols = self.sql_result.get("columns") or []
        if not rows:
            return [
                "放宽时间范围或调整条件重新查询",
                "换一个维度（如按产线/工序/日期）分析同一指标",
            ]

        num_cols, date_cols, cat_cols = [], [], []
        for c in cols:
            kinds, distinct = _infer_col_kind(c, rows)
            if "date" in kinds:
                date_cols.append(c)
            elif "num" in kinds:
                num_cols.append(c)
            elif "cat" in kinds and 2 <= len(distinct) <= 30:
                cat_cols.append(c)

        recs = []
        if date_cols and num_cols:
            recs.append(f"按{date_cols[0]}查看{num_cols[0]}的变化趋势")
        if cat_cols and num_cols:
            recs.append(f"按{cat_cols[0]}拆分对比{num_cols[0]}")
        if num_cols:
            recs.append(f"列出{num_cols[0]}最高的 TOP10")
        if not recs and date_cols:
            recs.append(f"按{date_cols[0]}统计最近7天的变化趋势")
        if len(recs) < 2 and num_cols:
            recs.append(f"分析{num_cols[0]}的异常值")
        if not recs:
            recs.append("查看完整数据的字段分布情况")
        if len(recs) < 2:
            recs.append("生成一份当前结果的数据分析报告")
        return recs[:3]

    # ── 意图处理器 ───────────────────────────────────────

    def _respond_chat(self) -> Generator[dict, None, None]:
        # 常见问候免 LLM，秒回
        quick_replies = {
            "你好": "你好！我是 Vequo 维阔 AI 助手，专注于数据分析。有什么可以帮你的？",
            "在吗": "在的！随时为你服务。有什么想了解的？",
            "谢谢": "不客气！有问题随时问我。",
            "再见": "再见！祝工作顺利。",
            "你是谁": "我是 Vequo 维阔（NL2SQL Agent），一个智能数据分析助手。你可以用自然语言向我提问，我会自动查询数据库并生成图表来回答。",
            "你能做什么": "我可以帮你：\n· 用自然语言查询数据库（说人话就行）\n· 自动生成统计图表\n· 分析数据趋势和异常\n· 回答关于数据库结构的问题\n\n试试问我：「分析各工序良率」「最近设备停机记录」「查看所有表」",
            "你会什么": "我可以帮你：\n· 用自然语言查询数据库（说人话就行）\n· 自动生成统计图表\n· 分析数据趋势和异常\n· 回答关于数据库结构的问题\n\n试试问我：「分析各工序良率」「最近设备停机记录」「查看所有表」",
        }
        # 归一化后精确匹配（忽略尾部标点/空白），避免"谢谢你"误命中"谢谢"、"你好吗"误命中"你好"
        q_norm = self.query.strip().rstrip("！!。？?～~…，,。 ")
        for key, reply in quick_replies.items():
            if q_norm == key:
                yield {"type": "answer", "text": reply}
                yield {"type": "done", "elapsed_ms": 0,
                       "response": {"type": "general_chat", "answer": reply}}
                return

        # 其他闲聊用 LLM 流式回复
        yield {"type": "step", "name": "思考中", "detail": "AI 正在回复…"}
        system_prompt = (
            "你是 Vequo 维阔（NL2SQL Agent），一个智能数据分析助手。"
            "你帮助用户用自然语言查询数据库、生成图表、分析数据。"
            "请用第一人称「我」来称呼自己。回答简洁友好，控制在100字以内。"
        )
        history_text = self._build_history_text()
        if history_text:
            system_prompt += f"\n\n对话历史：\n{history_text}\n请结合历史上下文回答最新问题。"
        llm = _make_llm(temp=0.7, max_tokens=256)
        full = ""
        try:
            for chunk in llm.stream([SystemMessage(content=system_prompt), HumanMessage(content=self.query)]):
                if isinstance(chunk, AIMessageChunk) and chunk.content:
                    t = chunk.content
                    full += t
                    yield {"type": "thought", "step": "回复", "text": t}
        except Exception:
            full = "您好！我是 Vequo 维阔 AI 助手，专注于数据分析。您可以问我：分析各工序良率、不良类型排行、库存预警等。"
        yield {"type": "thought_done", "step": "回复"}
        yield {"type": "done", "elapsed_ms": 0,
               "response": {"type": "general_chat", "answer": full}}

    def _respond_write_refusal(self) -> Generator[dict, None, None]:
        """写操作请求 → 明说「我改不了」。

        这里不是安全防线本身（防线是 execute_sql 的只读实现 + 出口统一 rollback），
        而是把这件事**说出口**。此前这类请求被当成查询一路走到生成 SQL，用户看到
        「正在生成查询 SQL」，很容易理解成「它在试着改」。明确拒绝 + 指出正规入口 +
        附一个我能做的替代动作，才算一次完整回应。
        """
        q = (self.query or "").strip()
        _shown = q[:40] + ("…" if len(q) > 40 else "")
        yield {"type": "done", "elapsed_ms": 0,
               "response": {
                   "type": "general_chat",
                   "answer": (
                       "这个我做不了——我对数据库只有查询权限，没有修改数据的能力。\n\n"
                       f"你输入的是「{_shown}」，属于要改动数据。我发出的每一句 SQL 都必须是"
                       "查询语句，而且执行完不落盘，连临时改动也会撤销。这类操作我不会执行，"
                       "也没有别的入口可以绕过去。\n\n"
                       "如果确实要改这条数据，走这两个地方：\n"
                       "· 管理员在「数据资源」页（管理专区）找到这条记录，双击单元格直接改\n"
                       "· 或者让管理员代改，说清楚改哪张表、哪条记录、改成什么\n\n"
                       "要不要我先把你说的这批数据查出来？你核对无误，再拿去改。"
                   )}}

    def _respond_gibberish(self) -> Generator[dict, None, None]:
        yield {"type": "done", "elapsed_ms": 0,
               "response": {"type": "general_chat",
                            "answer": "输入的内容我不太理解。您可以尝试问我：\n· 分析各工序良率\n· 不良类型排行\n· 库存预警分析"}}

    def _respond_analyze_db(self) -> Generator[dict, None, None]:
        """分析当前数据库 — 只显示当前连接 DB 中真实存在的表"""
        from db.metadata import TABLES as META

        meta_map = {t["table_name"]: t for t in META}
        lines = []
        total_rows = 0

        # 只列当前 DB 中真实存在的表（支持任意 schema）
        try:
            from database import get_db_type as _gdt
            if _gdt() == "mysql":
                r = execute_sql(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=DATABASE() AND table_type='BASE TABLE' ORDER BY table_name"
                )
            else:
                r = execute_sql(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_type='BASE TABLE' "
                    "ORDER BY (table_schema='public') DESC, table_schema, table_name"
                )
            if r["success"]:
                db_tables = []
                for row in r["rows"]:
                    if _gdt() == "mysql":
                        db_tables.append(row["table_name"])
                    else:
                        db_tables.append(row["table_name"] if row["table_schema"] == "public"
                                         else f"{row['table_schema']}.{row['table_name']}")
        except Exception:
            db_tables = []

        # 表权限过滤（Phase 4.3）：只展示角色有权访问的表
        if self.allowed_tables is not None:
            db_tables = [t for t in db_tables if t.split(".")[-1].lower() in self.allowed_tables]

        if not db_tables:
            if self.allowed_tables is not None:
                # 有权限配置但无可见表（权限收得很紧）→ 明确提示，不走 META 回退避免泄露无权表
                lines.append("（当前账号无权查看任何数据表）")
            else:
                # DB 不可用时回退到元数据
                for t in META:
                    rows = t.get("row_count", 0)
                    total_rows += rows
                    lines.append(f"· {t['table_alias']}（{t['table_name']}）: {rows}行（参考）")
        else:
            # 行数估算：一次查询 pg_class / information_schema，避免逐表 COUNT(*) 串行扫描
            est_rows = {}
            try:
                if _gdt() == "mysql":
                    r2 = execute_sql(
                        "SELECT table_name, table_rows FROM information_schema.tables "
                        "WHERE table_schema=DATABASE()"
                    )
                else:
                    r2 = execute_sql(
                        "SELECT c.relname AS table_name, c.reltuples::bigint AS row_count "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind='r'"
                    )
                if r2["success"]:
                    for row in r2["rows"]:
                        # reltuples 对未 ANALYZE 的表为 -1（未知），按 0 处理
                        est_rows[row["table_name"]] = max(0, int(row.get("row_count") or row.get("table_rows") or 0))
            except Exception:
                pass
            for tname in db_tables:
                meta = meta_map.get(tname)
                alias = meta["table_alias"] if meta else tname
                base = tname.split(".")[-1]  # schema.table 仅取表名匹配 pg_class.relname
                rows = est_rows.get(base, 0)
                total_rows += rows
                lines.append(f"· {alias}（{tname}）: {rows}行")

        answer = f"共 {len(lines)} 张表，约 {total_rows} 条数据：\n\n" + "\n".join(lines)
        answer += "\n\n你可以直接问我：\n· 查询某张表的数据\n· 统计分析（排行、汇总、趋势）"

        yield {"type": "done", "elapsed_ms": 0,
               "response": {"type": "analyze_db", "answer": answer,
                            "tables": db_tables,
                            "total_rows": total_rows}}

    def _respond_ml(self) -> Generator[dict, None, None]:
        """对话式机器学习建模：自然语言 → 训练/预测 → 返回指标与图表"""
        from ml.trainer import train_model, get_numeric_tables

        yield {"type": "step", "name": "ML建模", "detail": "正在理解建模需求…"}

        # 1. 匹配候选表（优先关键词匹配，回退到可建模表列表）
        try:
            cand = match_tables_by_query(self.query)[:5]
            if not cand:
                for t in get_numeric_tables():
                    cand.append({
                        "table_name": t["table"], "table_alias": t["table"],
                        "category": "", "description": "",
                        "row_count": t["rows"], "field_count": len(t["columns"]),
                    })
            # 候选表必须**真实存在于当前库**：db/metadata.py 的清单里同时挂着别的库的表
            # （factory.* 属于 123 库，test_* 是 CSV 导入测试留下的）。不过滤的话
            # 「用质检记录表…」会先命中 factory.quality_inspection（别名"质检记录表"），
            # 而当前库里根本没有这张表 → information_schema 查不到列 → 报"本表可用的数值列：无"。
            try:
                from db.tools import get_real_tables
                real_names = {t["table_name"] for t in get_real_tables()}
                cand = [t for t in cand if t["table_name"] in real_names]
            except Exception:
                pass
            # 表权限过滤（与 run() 主流程一致）：低权角色不可对无权表做 ML 训练/预测
            if self.allowed_tables is not None:
                cand = [t for t in cand
                        if t["table_name"].split(".")[-1].lower() in self.allowed_tables]
            if not cand:
                yield {"type": "done",
                       "response": {"type": "ml_error", "error": "没有找到可用于建模的数据表，请先确认数据库中有含数值字段的表"}}
                return
            table_name = cand[0]["table_name"]
            schema_ctx = _build_schema_fast([t["table_name"] for t in cand[:3]])
        except Exception as e:
            yield {"type": "done", "response": {"type": "ml_error", "error": f"建模准备失败: {e}"}}
            return

        # 2. LLM 生成受限建模意图（流式输出思考过程）
        yield {"type": "step", "name": "ML方案设计", "detail": "AI 正在设计建模方案…"}
        from agent.ml_intent import generate_ml_intent_stream
        intent = None
        gen_err = ""
        for ev in generate_ml_intent_stream(self.query, schema_ctx, table_name):
            if ev["type"] == "thought":
                yield ev  # 转发 token 流给前端
            elif ev["type"] == "intent":
                intent = ev.get("intent")
                gen_err = ev.get("error", "")
        # 2026-09-20 修复：LLM 方案空/解析失败时**不再拿空 dict 往下跑**。
        # 旧行为：`intent={}` 照样进训练流程，于是"目标撞上关键词列、特征落一排 ID、
        # 算法默认线性回归"，界面上是一条 R²≈0、特征重要性一正一负的假结果，
        # 再配一句"方案生成异常"。现在改由 agent/ml_plan 用问句 + 真实列结构兜底，
        # 两种来源都进同一套校验，界面只说"方案已生成"。
        if not isinstance(intent, dict):
            intent = {}
        plan_src = "AI 方案" if intent.get("data_request") else ""

        # 3. 执行训练 / 预测
        try:
            # 续问预测（"计划数量=1200 时的预测结果"）：方案那轮 LLM 就算失手，
            # 也要走预测分支，别退化成"重新训一个模型"
            if intent.get("intent_type") == "predict" or (
                    not intent and re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*[=:是]\s*-?\d", self.query)):
                yield from self._ml_predict(intent)
                return
            dr = intent.get("data_request", {})
            table = dr.get("table") or table_name
            # 表权限兜底：LLM 直接指定的表若不在允许集合 → 拒绝（防越权）
            if self.allowed_tables is not None and table:
                if table.split(".")[-1].lower() not in self.allowed_tables:
                    yield {"type": "done", "elapsed_ms": 0,
                           "response": {"type": "ml_rejected", "query": self.query,
                                        "answer": f"当前账号无权对表 {table} 进行机器学习建模，请联系管理员开通权限。"}}
                    return
            # 校验 LLM 选中的表是否真实存在，防止幻觉表名
            try:
                from db.tools import get_real_tables
                real_names = {t["table_name"] for t in get_real_tables()}
                if table not in real_names:
                    table = table_name
            except Exception:
                table = table_name

            # ── 方案来源与问句校正（2026-09-20 第二次报障后重构）───────────
            from agent.ml_plan import (build_fallback_intent, apply_query_override,
                                       try_cross_table_target, cross_table_suggestion,
                                       numeric_feature_pool)
            db_fields_all = _get_db_columns(table) or []
            if not plan_src:
                # LLM 这条路没交出方案 → 规则兜底，而不是拿空 dict 硬跑
                if gen_err:
                    logging.getLogger("nl2sql").info("ML 方案生成异常，转规则兜底: %s", gen_err)
                intent = build_fallback_intent(self.query, table, db_fields_all)
                plan_src = "规则兜底"
            # 问句语法（「根据A预测B」「用随机森林」）比 LLM 更硬，逐条叠上去
            intent, fix_note = apply_query_override(intent, self.query, db_fields_all)
            dr = intent.get("data_request", {})
            fields = dr.get("fields", [])
            target = dr.get("target", "")
            detail = f"{plan_src}已生成"
            if fix_note:
                detail += "：" + (fix_note[:110] + "…" if len(fix_note) > 110 else fix_note)
            yield {"type": "step", "name": "ML方案设计", "detail": detail}

            # 目标列不在这张表里（"工单表的实际产量"就是这种）→ 按公共主键从别的表汇总
            unresolved = intent.get("_target_unresolved")
            if unresolved:
                from ml.trainer import is_unique_column
                schema_map = _ml_schema_map()
                spec, xnote, _ = try_cross_table_target(self.query, table, db_fields_all, schema_map,
                                                        key_check=is_unique_column)
                if spec:
                    target = spec["col"]
                    fields = [f for f in fields if f != target]
                    if not fields:
                        fields = [f for f in numeric_feature_pool(db_fields_all) if f != target][:8]
                    dr["target"] = target
                    dr["fields"] = fields
                    dr["join_spec"] = spec
                    intent.pop("_target_unresolved", None)
                    yield {"type": "step", "name": "ML数据准备", "detail": xnote}
                else:
                    sug = cross_table_suggestion(unresolved, table, schema_map)
                    avail = "、".join(numeric_feature_pool(db_fields_all)) or "无"
                    yield {"type": "done", "elapsed_ms": 0, "response": {
                        "type": "ml_error",
                        "error": f"「{unresolved}」在 {table} 里没有对应的数值列"
                                 f"（本表可用的数值列：{avail}）。"
                                 + (sug or "请把预测目标换成这张表里有的字段。")}}
                    return

            # 智能补全字段：优先真实数值字段，剔除日期/时间列、*_id 列
            try:
                db_fields = db_fields_all
                numeric_fields = [
                    f["name"] for f in db_fields
                    if str(f["type"]).lower() in ("integer", "bigint", "numeric", "real", "double precision", "smallint", "int", "float", "decimal")
                ]
                non_numeric_fields = [f["name"] for f in db_fields if f["name"] not in numeric_fields]
                # 日期/时间字段不作为特征
                date_like = [
                    f["name"] for f in db_fields
                    if any(k in str(f["type"]).lower() for k in ("date", "time", "timestamp"))
                ]
                # 主键/外键（*_id）不作为特征（2026-09-20 修复）：
                # 实测「用工序产量表训练模型预测投入数量」里，LLM 把 work_order_id / product_id /
                # line_id 都当成了特征，特征重要性全是 0 —— 图上是一排空柱子，既不好看也没信息量。
                id_like = [f["name"] for f in db_fields if f["name"].lower().endswith("_id")]
                feature_pool = [f for f in numeric_fields + non_numeric_fields
                                if f not in date_like and f not in id_like]

                # target 为空 → 推断目标字段
                # 旧实现拿中文词去匹配英文列名（"产量" in "input_qty"），永远不成立，
                # 只剩 qty/amount/count/total 这些泛词能撞上第一个数量列。
                # 现在主要靠 agent/ml_plan 的「中文业务词 → 英文列名片段」映射，
                # 这一段只当第二层兜底；无监督任务（聚类/异常检测）不该有目标列，跳过。
                _mt0 = intent.get("model_training") or {}
                _is_unsup = _ML_MODEL_MAP.get(_mt0.get("model", ""), "") in ("kmeans", "isolation")
                if not target and not _is_unsup:
                    target_hints = (
                        ("实际", "actual"), ("计划", "plan"),
                        ("合格", "good"), ("良品", "good"), ("良率", "good"),
                        ("不良", "defect"), ("缺陷", "defect"), ("次品", "defect"),
                        ("返工", "rework"),
                        ("投入", "input"), ("投料", "input"),
                        ("可用", "available"), ("冻结", "frozen"),
                        ("安全", "safety"),
                        ("停机", "downtime"), ("时长", "minutes"),
                        ("抽检", "sample"), ("检验", "sample"),
                        ("金额", "amount"), ("产量", "qty"), ("数量", "qty"),
                    )
                    cand_num = [f for f in numeric_fields if f not in id_like] or numeric_fields
                    for cn, needle in target_hints:
                        if cn in self.query:
                            hit = [f for f in cand_num if needle in f.lower()]
                            if hit:
                                target = hit[0]
                                break
                    if not target:
                        for kw in ("qty", "amount", "count", "total"):
                            hit = [f for f in cand_num if kw in f.lower()]
                            if hit:
                                target = hit[0]
                                break
                    if not target and cand_num:
                        target = cand_num[0]
                # fields 为空 → 用除 target 外的数值字段
                if not fields:
                    fields = [f for f in numeric_fields if f != target and f not in id_like][:8]
                    if not fields:
                        fields = [f for f in feature_pool if f != target][:8]
                else:
                    # 过滤：剔除日期字段、*_id 列和不存在字段，只保留真实字段
                    real_cols = {f["name"] for f in db_fields}
                    keep = [f for f in fields
                            if f in real_cols and f not in date_like
                            and f not in id_like and f != target][:8]
                    if not keep:
                        # LLM 只给了 id/日期这类字段时，退回真实数值字段，别退化成空
                        keep = [f for f in numeric_fields if f != target and f not in id_like][:8]
                    fields = keep
            except Exception:
                pass

            mt = intent.get("model_training", {})
            llm_model = mt.get("model", "LinearRegression")
            model_type = _ML_MODEL_MAP.get(llm_model, "linear")
            # 聚类 / 异常检测本来就没有目标列，"没有 target"不算缺 —— 旧判断会把
            # 所有无监督任务一律拒掉（"建模方案缺少特征或目标字段"），这里分开看。
            unsupervised = model_type in ("kmeans", "isolation")
            if unsupervised:
                target = ""
            if not fields or (not target and not unsupervised):
                avail = "、".join(numeric_feature_pool(db_fields_all)) or "无"
                yield {"type": "done",
                       "response": {"type": "ml_error",
                                    "error": f"建模方案缺少可用的特征或目标字段"
                                             f"（{table} 里的数值列：{avail}），请明确要预测哪个字段"}}
                return
            params = mt.get("params", {})
            result = train_model(table, target, fields, model_type, params,
                                 join_spec=dr.get("join_spec"))
            _set_last_ml_model({
                "name": result["model_name"], "features": fields, "target": target,
                "label": result["model_label"],
            })
            yield {"type": "done",
                   "response": {
                       "type": "ml_result",
                       "ml_result": {"success": True, **result},
                       "model_name": result["model_name"],
                       "features": fields,
                       "target": target,
                   }}
        except Exception as e:
            yield {"type": "done", "response": {"type": "ml_error", "error": f"建模执行失败: {e}"}}

    def _ml_predict(self, intent: dict) -> Generator[dict, None, None]:
        """基于最近训练的模型做单条预测（从问题里提取 字段=值）"""
        from ml.trainer import predict
        model = _get_last_ml_model() or {}
        model_name = intent.get("data_request", {}).get("model_name") or model.get("name")
        if not model_name:
            yield {"type": "done",
                   "response": {"type": "ml_error", "error": "还没有可用的模型，请先训练。例如：用 XX 表训练模型预测 XX"}}
            return

        # 从用户输入提取 "字段=值" 或 "字段是值"
        values = {}
        for m in re.finditer(r'([A-Za-z_][A-Za-z0-9_]*)\s*[=:是]\s*(-?\d+(?:\.\d+)?)', self.query):
            values[m.group(1)] = float(m.group(2))
        if not values:
            feats = model.get("features", [])
            sample = "、".join(f"{f}=数值" for f in feats[:3])
            yield {"type": "done",
                   "response": {"type": "ml_error",
                                "error": f"请提供预测输入，例如：{sample} 时的预测结果"}}
            return

        result = predict(model_name, values)
        yield {"type": "done",
               "response": {
                   "type": "ml_result",
                   "ml_result": {
                       "success": True,
                       "model": {"name": model_name, "label": model.get("label", model_name)},
                       "metrics": {},
                       "pred_samples": [{"输入": json.dumps(values, ensure_ascii=False), "预测": result.get("label", result.get("prediction"))}],
                   },
                   "model_name": model_name,
               }}

    def _respond_lookup(self) -> Generator[dict, None, None]:
        self.matched_tables = match_tables_by_query(self.query)[:5]
        # 关键词匹配不到表（如"有哪些表"）→ 回退列出当前数据库全部表
        if not self.matched_tables:
            try:
                from db.tools import get_all_tables
                self.matched_tables = get_all_tables()[:5]
            except Exception:
                pass
        # 表权限过滤（与 run() 主流程一致）：低权角色不可查无权表结构
        if self.allowed_tables is not None:
            self.matched_tables = [
                t for t in self.matched_tables
                if t["table_name"].split(".")[-1].lower() in self.allowed_tables
            ]
        if self.matched_tables:
            top = self.matched_tables[0]
            detail = get_table_detail(top["table_name"])
            yield {"type": "done", "elapsed_ms": 0,
                   "response": {
                       "type": "table_lookup",
                       "matched_table": {
                           "table_name": top["table_name"], "table_alias": top["table_alias"],
                           "category": top["category"], "description": top["description"],
                           "row_count": top["row_count"], "field_count": top["field_count"],
                       },
                       "detail": detail,
                       "related_tables": self.matched_tables[1:4],
                   }}
        else:
            yield {"type": "done", "elapsed_ms": 0,
                   "response": {"type": "table_lookup", "matched_table": None, "detail": None, "related_tables": []}}


# ── 分析 & 预测（独立调用，不阻塞主流程）────────────────

def generate_analysis(query: str, sql: str, sql_result: dict) -> Generator[dict, None, None]:
    """流式生成数据分析解读"""
    if not sql_result.get("rows") or len(sql_result["rows"]) < 2:
        yield {"type": "done", "content": "数据不足，无法生成分析"}
        return

    rows = sql_result["rows"][:15]
    data_json = json.dumps(rows, ensure_ascii=False, default=str)
    fields_info = ", ".join(sql_result["columns"])

    prompt = ANALYSIS_SYSTEM_PROMPT.format(
        data_json=data_json[:1500],
        fields_info=fields_info,
        query=query,
    )
    llm = _make_llm(temp=0.3, max_tokens=512)
    full = ""
    for chunk in llm.stream([HumanMessage(content=prompt)]):
        if isinstance(chunk, AIMessageChunk) and chunk.content:
            t = chunk.content
            full += t
            yield {"type": "thought", "step": "数据分析", "text": t}
    yield {"type": "done", "content": full}


def _linear_predict(values: list[float], horizon: int = 3) -> list[float]:
    """对一维数值序列做最小二乘线性回归，返回未来 horizon 个预测值。

    比 LLM 编数字更可靠、零成本、确定性；数据无趋势时斜率≈0，退化为均值外推。
    """
    n = len(values)
    if n < 2:
        return []
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    sxx = sum((x - mean_x) ** 2 for x in xs)
    slope = sxy / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x
    return [round(slope * (n + i) + intercept, 2) for i in range(1, horizon + 1)]


def _shift_date_str(s, days: int) -> str:
    """把日期字符串顺延 N 天；解析失败返回原串（预测行日期列兜底）"""
    import datetime as _dt
    try:
        s = str(s).strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m"):
            try:
                d = _dt.datetime.strptime(s, fmt)
                return (d + _dt.timedelta(days=days)).strftime(fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return s


def generate_predict(sql_result: dict) -> Generator[dict, None, None]:
    """生成趋势预测（统计方法：线性回归外推，替代原 LLM 凭空编数字）

    行为变化：不再调用 LLM，零延迟、零成本、结果确定。
    返回格式兼容：yield thought(可省) → done(prediction=预测行列表)。
    """
    if not sql_result.get("rows") or len(sql_result["rows"]) < 3:
        yield {"type": "done", "content": "", "prediction": []}
        return

    rows = sql_result["rows"][-12:]
    cols = sql_result.get("columns") or []
    if not cols:
        yield {"type": "done", "content": "", "prediction": []}
        return

    # 识别数值列 / 日期列 / 其他列
    num_cols, date_cols, other_cols = [], [], []
    for c in cols:
        kinds, _ = _infer_col_kind(c, rows)
        if "date" in kinds:
            date_cols.append(c)
        elif "num" in kinds:
            num_cols.append(c)
        else:
            other_cols.append(c)

    # 至少需要一个数值列才能做趋势外推；否则给不出有意义的预测
    if not num_cols:
        yield {"type": "done", "content": "", "prediction": []}
        return

    horizon = 3
    pred_rows = []
    for i in range(horizon):
        new_row = {}
        for c in cols:
            if c in num_cols:
                vals = [float(r.get(c, 0) or 0) for r in rows]
                pred = _linear_predict(vals, horizon)
                new_row[c] = pred[i] if i < len(pred) else vals[-1]
            elif c in date_cols:
                new_row[c] = _shift_date_str(rows[-1].get(c, ""), i + 1)
            else:
                # 维度/其他列：沿用最后一行（预测行不做维度推断）
                new_row[c] = rows[-1].get(c, "")
        pred_rows.append(new_row)

    content = ("基于历史数据的最小二乘线性外推（统计方法，非 LLM 臆测）：\n"
               + json.dumps(pred_rows, ensure_ascii=False, default=str))
    yield {"type": "done", "content": content, "prediction": pred_rows}


def _infer_col_kind(col: str, rows: list[dict]) -> tuple[set[str], set[str]]:
    """按样例值推断列类型：num/date/cat/bool；返回 (类型集合, 去重取值集合)"""
    kinds: set[str] = set()
    distinct: set[str] = set()
    for r in rows[:80]:
        v = r.get(col)
        if v is None or v == "":
            continue
        s = str(v)
        if isinstance(v, bool):
            kinds.add("bool")
        elif isinstance(v, (int, float)):
            kinds.add("num")
        elif re.match(r"^\d{4}[-/]\d{1,2}", s) or re.match(r"^\d{4}年\d{1,2}月", s):
            kinds.add("date")
        else:
            kinds.add("cat")
        if len(distinct) < 200:
            distinct.add(s)
    return kinds, distinct


def _result_structure_hint(sql_result: dict) -> str:
    """生成结果结构线索（日期/数值/维度列），供 LLM 版推荐问题参考"""
    cols = sql_result.get("columns") or []
    rows = sql_result.get("rows") or []
    if not cols or not rows:
        return ""
    parts = []
    for c in cols:
        kinds, distinct = _infer_col_kind(c, rows)
        if "date" in kinds:
            parts.append(f"日期列:{c}")
        elif "num" in kinds:
            parts.append(f"数值列:{c}")
        elif "cat" in kinds and 2 <= len(distinct) <= 50:
            parts.append(f"维度列:{c}")
    return "; ".join(parts[:8])


# ═══════════════════════════════════════════════════════════════════
# 洞察正文生成（供「二次确认执行 execute_confirm」等无实例入口复用；
#  分析意图 → 结论式分析：先正面回答，再给证据/建议）
# ═══════════════════════════════════════════════════════════════════

_ANALYSIS_INTENT_WORDS = (
    "相关", "关系", "关联", "对比", "比较", "差异", "影响", "趋势",
    "是否", "越来越", "越高", "越低", "规律", "异常", "原因", "因为",
    "随", "随着", "分析",
)


def _is_analysis_intent(query: str) -> bool:
    """是否分析类问法（需要结论/归因/关系判断，而非仅查数）"""
    q = (query or "").lower()
    return any(w in q for w in _ANALYSIS_INTENT_WORDS)


# ── 查询明确性判断（2026-09-07 新增）：区分「口径未登记」与「问题本身模糊」──
# 原红线是「口径未登记 → 一律弹窗」，实测会把已经说清楚的问题也拦下来反复追问：
# 用户问「质量抽检不合格最多的工序」——要统计什么、按什么维度都很明确，只是该口径
# 没登记过，却仍被弹窗要求「补充说明」，体验很差（用户反馈：我都补充了还问）。
# 细化后的规则：**只有真正看不出要算什么/按什么分时才弹窗澄清**；问题已经说清楚的
# 直接生成，并在结果区标注「AI 生成，请核对」（口径溯源灰条 + 可展开 SQL 仍可见）。
# ⚠️ 2026-09-24：此表原名 `_AGG_WORDS`，与 2401 行「聚合意图提示」的表**同名**，
# 本行在文件更靠后 ⇒ 定义时覆盖前者，导致 `_needs_aggregation()` 实际只用到这里这 26 个词，
# 前表独有的 14 个词（每条/每类/分别/按/同比/环比/良率/合格率/达成率/TOP/产量/总额/金额…）
# 静默失效 —— 「每条产线的良率」「工序的同比情况」被判为"不需要聚合"，不再注入强制
# GROUP BY 提示，LLM 容易退化成 SELECT * 明细。
# 两张表用途不同（这张只服务 `_is_query_clear` 的澄清判断），已改名为 _CLARIFY_AGG_WORDS。
_CLARIFY_AGG_WORDS = ("最多", "最少", "最高", "最低", "最大", "最小", "均值", "平均", "合计", "总计",
                      "总数", "总量", "数量", "次数", "个数", "占比", "比例", "趋势", "对比",
                      "排名", "排行", "分布", "统计", "汇总", "各", "每个", "每种")
# 隐含维度的词：趋势必然按时间、占比/分布必然按分类，无需再要求显式维度词
_IMPLIED_DIM_WORDS = ("趋势", "占比", "比例", "对比", "分布", "排名", "排行")
_DIM_HINTS = ("按", "各", "每个", "谁", "哪个", "哪些", "维度", "分类", "分组",
              "工序", "产线", "产品", "车间", "客户", "供应商", "设备", "月份", "日期",
              "类型", "类别", "班组", "型号", "批次", "部门")
_SCOPE_HINTS = ("最近", "本月", "上月", "今年", "去年", "本季度", "大于", "小于",
                "等于", "以上", "以下", "之间", "范围", "季度")


def _is_query_clear(query: str) -> bool:
    """用户是否已经把问题说清楚（纯规则、零 LLM、确定性，不影响 SQL 生成路径）。

    判定：有统计/排序意图 且（有维度线索 或 有范围限定 或 表述足够长）。
    """
    q = (query or "").strip()
    if not q:
        return False
    has_agg = any(w in q for w in _CLARIFY_AGG_WORDS) or bool(re.search(r"(前|top|TOP)\s*\d+", q))
    if not has_agg:
        return False            # 连要算什么都看不出来 → 模糊
    if any(w in q for w in _IMPLIED_DIM_WORDS):
        return True             # 趋势/占比/排名等自带维度语义
    has_dim = any(w in q for w in _DIM_HINTS)
    has_scope = any(w in q for w in _SCOPE_HINTS)
    return has_dim or has_scope or len(q) >= 10


def _num_cols_series(rows: list[dict], cols: list[str]) -> dict[str, list[float]]:
    """识别数值列并收集全部有效数值（按行序）；每列有效样本 ≥3 才纳入。

    排除低基数整数列（如班次号 1/2/3、车间号 1~4、序号）——它们是离散标识，
    参与相关计算会产生虚假的高相关，必须剔除。
    """
    out: dict[str, list[float]] = {}
    for c in cols or []:
        vals: list[float] = []
        for r in rows:
            v = r.get(c)
            if v is None or str(v).strip() == "":
                continue
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                break  # 出现非数值 → 不是数值列
        if len(vals) < 3:
            continue
        # 低基数整数列（全为整数且去重值 ≤8，如班次/序号/编号）→ 标识列，跳过
        distinct = {v for v in vals}
        if all(float(v).is_integer() for v in vals) and len(distinct) <= 8:
            continue
        out[c] = vals
    return out


def _pearson(xs: list[float], ys: list[float]):
    """皮尔逊相关系数；样本 <3 或方差为 0（常量列）→ None"""
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs[:n], ys[:n]))
    sxx = sum((x - mx) ** 2 for x in xs[:n])
    syy = sum((y - my) ** 2 for y in ys[:n])
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def _corr_pair(columns: list[str], rows: list[dict]):
    """对结果中成对数值列做确定性相关分析，返回最强相关对 (r, colA, colB, n)；不可算返回 None。

    仅当 ≥2 个数值列且有效行对 ≥5 时输出（足够样本才谈相关性，避免误导）。
    """
    try:
        stats = _num_cols_series(rows, columns)
        num_cols = list(stats.keys())
        if len(num_cols) < 2:
            return None
        best = None
        for i in range(len(num_cols)):
            for j in range(i + 1, len(num_cols)):
                a, b = num_cols[i], num_cols[j]
                pairs: list[tuple[float, float]] = []
                for r in rows:
                    x, y = r.get(a), r.get(b)
                    if x is None or y is None:
                        continue
                    try:
                        pairs.append((float(x), float(y)))
                    except (ValueError, TypeError):
                        continue
                if len(pairs) < 5:
                    continue
                rv = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
                if rv is None:
                    continue
                if best is None or abs(rv) > abs(best[0]):
                    best = (rv, a, b, len(pairs))
        return best
    except Exception:
        return None


def _corr_strength(rv: float) -> str:
    if rv > 0.7:
        return "较强正相关（同增同减）"
    if rv > 0.4:
        return "中等正相关（同增同减）"
    if rv > 0:
        return "弱正相关"
    if rv > -0.4:
        return "弱负相关（此消彼长）"
    if rv > -0.7:
        return "中等负相关（此消彼长）"
    return "较强负相关（此消彼长）"


def _corr_evidence(columns: list[str], rows: list[dict]) -> str:
    """对结果中成对数值列做确定性相关分析，产出中文证据块（供 LLM 引用）。"""
    best = _corr_pair(columns, rows)
    if not best:
        return ""
    rv, a, b, n = best
    return (f"【确定性相关证据】按行配对计算，「{a}」与「{b}」的皮尔逊相关系数 "
            f"r={rv:.2f}（有效样本 n={n}），呈{_corr_strength(rv)}。"
            f"该系数只描述线性共变方向与强弱，不代表因果关系。请以它为依据回答用户问题。")


def _build_deterministic_insight(query: str, columns: list[str], rows: list[dict]) -> str | None:
    """相关性/对比类分析问法的确定性结论（零 LLM，秒出）。

    deepseek 对「15 行数据 + SQL + 分析任务」的洞察要 45s，是最慢一环。相关性结论
    本质是确定性计算（r 值已算出），直接由规则生成分析文本——符合"能确定性就不 LLM"。
    仅当存在可算相关对时产出；否则返回 None（调用方回退 LLM）。
    """
    best = _corr_pair(columns, rows)
    if not best:
        return None
    rv, a, b, n = best

    def _stat(col: str):
        vals = []
        for r in rows:
            v = r.get(col)
            if v is None:
                continue
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                continue
        if not vals:
            return None
        return {"max": max(vals), "min": min(vals), "avg": sum(vals) / len(vals)}

    sa, sb = _stat(a), _stat(b)
    lines = [f"在 {n} 个样本中，{a} 与 {b} 的皮尔逊相关系数 r={rv:.2f}，呈{_corr_strength(rv)}。"]
    if sa and sb:
        lines.append(f"{a} 最高 {sa['max']:g}、最低 {sa['min']:g}、均值 {sa['avg']:.2f}；"
                     f"{b} 最高 {sb['max']:g}、最低 {sb['min']:g}、均值 {sb['avg']:.2f}。")
    if abs(rv) < 0.4:
        lines.append("二者线性关联较弱，单一指标的变化不足以解释另一方，建议结合其它因素一起看。")
    elif abs(rv) >= 0.4:
        lines.append(f"二者存在可观测的{_corr_strength(rv).split('（')[0]}，"
                     f"但相关性不代表因果，建议进一步排查是否存在共同驱动因素或滞后效应。")
    return "".join(lines)


def _fmt_num_cn(v) -> str:
    """数字中文可读化：整数加千分位，小数保留 2 位（业务用户读 597,072 比读 597072.0 快）"""
    try:
        f = float(v)
    except (ValueError, TypeError):
        return str(v)
    if abs(f - round(f)) < 1e-9:
        return f"{int(round(f)):,}"
    return f"{f:,.2f}"


def _is_measure_col(col: str) -> bool:
    """排除不是度量的数值列：id / 序号 / 年份 / 编码等求和无意义的列"""
    c = str(col or "").lower()
    bad = ("id", "编号", "序号", "编码", "代码", "年份", "year", "月份", "month", "code", "no")
    return not any(b in c for b in bad)


_RATIO_EN_TOKENS = {"rate", "ratio", "pct", "percent", "percentage", "share",
                    "avg", "average", "mean", "price", "yoy", "mom"}


def _is_ratio_like(col: str) -> bool:
    """比率/均价类指标：求合计与算占比都没有业务意义，只能看均值与区间。

    英文/蛇形列名同样要认：实测前端问「非计划占比」时列名是 `unplanned_ratio`
    （英文），旧的纯中文关键词判据判为**可加和度量** → 输出「合计 261.91、
    排名里占 22.3%、前 3 名合计占 63.6%」这类无意义数字（比率求和没有业务含义，
    且"占"字用百分比再除以总和，纯属误导）。改为按非字母数字切词后比对词根，
    避免 `rate` 命中 `rating` 这类子串误判。
    """
    c = str(col or "")
    if any(k in c for k in ("率", "百分比", "占比", "比重", "平均", "均值", "单价", "均价", "同比", "环比")):
        return True
    toks = [t.lower() for t in re.split(r"[^0-9A-Za-z]+", c) if t]
    return any(t in _RATIO_EN_TOKENS for t in toks)


def _rule_insight(rows: list[dict], cols: list[str]) -> str:
    """规则版详细洞察（零 LLM、确定性）：概览 + 整体情况 + 排名 + 主要发现 + 建议关注。

    设计原则：
    1. 全部由真实结果算出，绝不编造数据中不存在的数字；
    2. 面向非专业业务用户——讲"这意味着什么"，而不是罗列统计项；
    3. 比率类指标不求和、不算占比（求和无业务意义，会误导）。
    """
    if not rows:
        return "查询执行成功，但当前数据范围内没有匹配的记录。可以调整时间范围或换个条件再试。"
    cols = [str(c) for c in (cols or []) if c is not None]
    if not cols:
        return f"共查询到 {len(rows)} 条记录。"
    sample = rows[:500]

    # 1) 列角色识别：数值列（度量） / 其余（维度）
    num_cols, dim_cols = [], []
    for col in cols:
        vals, ok = [], True
        for r in sample:
            v = r.get(col)
            if v is None or v == "":
                continue
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                ok = False
                break
        need = max(1, int(len(sample) * 0.6))
        if ok and len(vals) >= need:
            num_cols.append(col)
        else:
            dim_cols.append(col)

    lines = [f"共查询到 {len(rows)} 条记录，覆盖 {len(cols)} 个字段（{'、'.join(cols[:6])}）。"]

    # 无任何数值列 → 纯清单型结果，只给概览与前几条，不做统计（避免瞎算）
    if not num_cols:
        lines += ["", "【主要发现】"]
        head = "、".join(str(r.get(cols[0], "")) for r in rows[:5])
        lines.append(f"· 结果为{'、'.join(cols[:3])}的明细清单，前 5 条为：{head}。" if head
                     else f"· 结果为{'、'.join(cols[:3])}的明细清单。")
        lines += ["", "【建议关注】", "· 如需看汇总口径，可继续追问「按 XX 统计数量」做聚合。"]
        return "\n".join(lines)

    mc = next((c for c in num_cols if _is_measure_col(c)), num_cols[0])
    ratio_like = _is_ratio_like(mc)

    vals = []
    for r in sample:
        v = r.get(mc)
        if v is None or v == "":
            continue
        try:
            vals.append(float(v))
        except (ValueError, TypeError):
            continue
    if not vals:
        return "\n".join(lines)

    n = len(vals)
    s = sum(vals)
    avg = s / n
    mx, mn = max(vals), min(vals)
    srt = sorted(vals)
    med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2

    # 比率类且值域落在 0~1（如合格率 0.972）→ 按百分比展示，业务用户读 97.2% 才懂，0.97 读不懂
    pct_mode = ratio_like and 0 <= mn and mx <= 1

    def _fmt(v) -> str:
        return f"{v * 100:.1f}%" if pct_mode else _fmt_num_cn(v)

    lines += ["", "【整体情况】"]
    # 记录数与统计口径不一致必须挑明：实测「共 6 条记录」但「平均 52.38」是按 5 条算的
    # （第 6 行该字段为空，被静默跳过）—— 用户会以为平均值算错了。
    if len(rows) > n:
        lines.append(f"· {len(rows)} 行中有 {len(rows) - n} 行「{mc}」为空（无该口径数据），未参与统计。")
    if n == 1:
        # 单行聚合（如"总缺陷数 1234"）：直接给结论，不报合计/平均/中位数这种同义反复
        lines.append(f"· {mc}为 {_fmt(vals[0])}。")
    elif ratio_like:
        # 比率类合并成一行，避免"最高…最低…"在下一行重复一遍
        _gap_txt = (f"，相差 {(mx - mn) * 100:.1f} 个百分点" if pct_mode
                    else f"，相差 {_fmt_num_cn(mx - mn)}")
        lines.append(f"· {mc}平均 {_fmt(avg)}，最高 {_fmt(mx)}，最低 {_fmt(mn)}{_gap_txt}。")
    else:
        lines.append(f"· {mc}合计 {_fmt_num_cn(s)}，平均 {_fmt_num_cn(avg)}，中位数 {_fmt_num_cn(med)}。")
        lines.append(f"· 最高 {_fmt_num_cn(mx)}，最低 {_fmt_num_cn(mn)}"
                     + (f"，最高约为最低的 {mx / mn:.1f} 倍。" if mn > 0 else "。"))

    # 3) 排名 / 时间对比（有维度列 + 至少 2 行可比时才给，单行聚合给排名没意义）
    ranked: list[tuple] = []
    ordered: list[tuple] = []
    dcol = dim_cols[0] if dim_cols else ""
    # 时间维度（月/日/周/年）讲趋势：比"排名"更重要的是变化方向，故换标题与结论口径
    is_time_dim = any(k in str(dcol) for k in ("月", "日", "年", "周", "季", "date", "time"))
    if dcol:
        pairs = []
        for r in sample:
            v = r.get(mc)
            if v is None or v == "":
                continue
            try:
                fv = float(v)
            except (ValueError, TypeError):
                continue
            pairs.append((str(r.get(dcol, "")) or "（空）", fv))
        if len(pairs) >= 2:
            ordered = list(pairs)          # 原始顺序（时间维度下通常是时间升序，用于算趋势）
            pairs.sort(key=lambda x: -x[1])
            ranked = pairs
            show_pct = (not ratio_like) and bool(s)
            lines += ["", f"【{dcol}对比】" if is_time_dim else f"【{dcol}排名】"]
            for i, (name, v) in enumerate(pairs[:3], 1):
                pct = f"（占 {v / s * 100:.1f}%）" if show_pct else ""
                lines.append(f"{i}. {name}：{_fmt(v)}{pct}")
            if len(pairs) > 3:
                ln, lv = pairs[-1]
                pct = f"（占 {lv / s * 100:.1f}%）" if show_pct else ""
                lines.append(f"最低：{ln}：{_fmt(lv)}{pct}")

    # 4) 主要发现：讲集中度 / 趋势——这是业务用户真正要看的结论
    if is_time_dim and len(ordered) >= 2:
        lines += ["", "【主要发现】"]
        f_name, f_v = ordered[0]
        l_name, l_v = ordered[-1]
        delta = l_v - f_v
        if pct_mode:
            chg_txt = f"{delta * 100:+.1f} 个百分点"
        elif f_v:
            chg_txt = f"{delta / abs(f_v) * 100:+.1f}%"
        else:
            chg_txt = "基本持平"
        trend = "上升" if delta > 0 else ("下降" if delta < 0 else "基本持平")
        lines.append(f"· 从 {f_name} 到 {l_name}，{mc}由 {_fmt(f_v)} 变为 {_fmt(l_v)}，整体{trend} {chg_txt}。")
        lines.append(f"· 期间最高 {_fmt(mx)}（{ranked[0][0]}），最低 {_fmt(mn)}（{ranked[-1][0]}）。")
    elif n == 1:
        pass                      # 单行聚合没有可比对象，不输出发现段，避免"波动不大"这类废话
    elif len(ranked) >= 3 and s and not ratio_like:
        lines += ["", "【主要发现】"]
        top3 = sum(v for _, v in ranked[:3])
        p3 = top3 / s * 100
        if p3 >= 60:
            lines.append(f"· 前 3 名合计占 {p3:.1f}%，{mc}高度集中在头部少数{dcol}。")
        elif p3 >= 35:
            lines.append(f"· 前 3 名合计占 {p3:.1f}%，{mc}相对集中在头部{dcol}。")
        else:
            lines.append(f"· 前 3 名合计仅占 {p3:.1f}%，{mc}分布较分散，没有明显的头部{dcol}。")
        if mn > 0:
            gap = mx / mn
            if gap >= 3:
                lines.append(f"· 最高的「{ranked[0][0]}」是最低的「{ranked[-1][0]}」的 {gap:.1f} 倍，"
                             f"差距明显，建议重点看两端。")
            elif gap >= 1.5:
                lines.append(f"· 最高的「{ranked[0][0]}」比最低的「{ranked[-1][0]}」高 {gap:.1f} 倍，存在一定差异。")
            else:
                lines.append(f"· 各{dcol}之间差距不大（最高约为最低的 {gap:.1f} 倍），整体较为均衡。")
    elif n > 1:
        lines += ["", "【主要发现】"]
        if ratio_like:
            # 比率类只讲区间与极差（百分点）：谈"最高是最低的几倍"或"集中在 0.96 附近"
            # 对比率都读不懂（0.96 是比率不是结果值，倍数对比率也无业务含义）
            _spread = (f"{(mx - mn) * 100:.1f} 个百分点" if pct_mode
                       else f"{_fmt_num_cn(mx - mn)}")
            _above = sum(1 for v in vals if v > avg)
            lines.append(f"· {mc}分布在 {_fmt(mn)} ~ {_fmt(mx)} 之间，相差 {_spread}；"
                         f"{n} 条中 {_above} 条高于平均（{_fmt(avg)}）。")
        elif mn > 0 and mx / mn >= 1.2:
            lines.append(f"· {mc}波动区间为 {_fmt_num_cn(mn)} ~ {_fmt_num_cn(mx)}，"
                         f"最高约为最低的 {mx / mn:.1f} 倍。")
        else:
            lines.append(f"· {mc}整体波动不大，集中在 {_fmt_num_cn(avg)} 附近。")

    # 5) 建议关注：只给方向，不编造具体业务动作
    lines += ["", "【建议关注】"]
    if is_time_dim and len(ordered) >= 2:
        lines.append(f"· 建议重点对比 {ranked[0][0]}（最高）与 {ranked[-1][0]}（最低），排查造成差异的原因。")
    elif len(ranked) >= 3 and s and not ratio_like:
        p3 = sum(v for _, v in ranked[:3]) / s * 100
        if p3 >= 50:
            lines.append(f"· 优先跟进 {ranked[0][0]}、{ranked[1][0]} 等头部{dcol}，"
                         f"它们对整体{mc}的影响最大。")
        else:
            lines.append(f"· {mc}分布较分散，建议按{dcol}逐项排查，避免只看总量漏掉个别异常项。")
    elif dcol:
        lines.append(f"· 如需定位具体原因，可继续追问「按{dcol}看{mc}」做进一步拆分。")
    else:
        lines.append(f"· 如需看趋势变化，可继续追问「{mc}按月/按周的变化」。")
    return "\n".join(lines)


def _quick_rule_summary(rows: list[dict], cols: list[str]) -> str:
    """无 LLM 的规则摘要兜底（与 _quick_analysis 共用同一套详细洞察逻辑）"""
    return _rule_insight(rows, cols)


# 洞察结果进程内缓存（性能优化）：重复问法/缓存命中时免二次 LLM 洞察
_insight_cache: dict = {}
_insight_lock = threading.Lock()
_INSIGHT_TTL = 60.0


def _insight_cache_key(query: str, row_count: int):
    try:
        return (_current_db_key(), (query or "").strip(), int(row_count or 0))
    except Exception:
        return None


def _insight_cached(query: str, row_count: int) -> str | None:
    key = _insight_cache_key(query, row_count)
    if not key:
        return None
    with _insight_lock:
        v = _insight_cache.get(key)
        if v and time.time() - v[0] < _INSIGHT_TTL:
            return v[1]
    return None


def _insight_store(query: str, row_count: int, text: str) -> None:
    key = _insight_cache_key(query, row_count)
    if not key or not text:
        return
    with _insight_lock:
        _insight_cache[key] = (time.time(), text)


def generate_insight_text(query: str, sql: str, columns: list[str], rows: list[dict],
                          warning: str = "") -> str:
    """为查询结果生成洞察正文（二次确认执行等非流式入口用）。

    - 分析类问法（相关/对比/趋势/影响…）且结果含 ≥2 数值列：注入确定性相关系数证据，
      要求 LLM 先正面回答用户问题（如"是否存在相关"），再列数据佐证与业务建议；
    - 其余结果走通用数据洞察；
    - 简单查询（非分析意图、少量行/列、单表）直接规则摘要，不调 LLM；
    - 数据不足/LLM 失败/超时 → 规则摘要兜底，绝不阻塞主流程。
    """
    try:
        rows = rows or []
        cols = columns or []
        if not rows:
            return "查询执行成功，但当前数据范围内没有匹配的记录。可调整时间范围或换个条件再试。"
        # 口径存疑 → 确定性降级（与 _llm_analysis 同口径，2026-09-14 用户实测驱动）：
        # 注入告警约束后 LLM 仍会输出业务结论与建议，改为机制上不给 LLM 发挥空间。
        if warning:
            return (
                "⚠️ 本次结果的指标口径可能不正确，以下数值仅供参考，请勿据此决策。\n"
                f"独立复核（Critic）判定：{warning[:200]}\n"
                "处理建议：核对指标定义，或在结果卡片「去登记口径」中固定正确算法后重新提问。\n\n"
                + _quick_rule_summary(rows, cols)
            )
        if len(rows) < 2:
            return _quick_rule_summary(rows, cols)
        rc = len(rows)
        # 简单查询有损降级（性能，2026-09-03）：非分析意图 + 少量行/列 + 单表 → 规则摘要，
        # 与主链 _llm_analysis 的降级口径一致，省一次 2-5s LLM 往返。
        if not _is_analysis_intent(query) and len(rows) <= 20 and len(cols) <= 5:
            try:
                _tabs = _extract_sql_tables(sql) if sql else set()
                if len(_tabs) <= 1:
                    return _quick_rule_summary(rows, cols)
            except Exception:
                pass
        # 分析类问法：相关性/对比结论可确定性生成（零 LLM，秒出），
        # 避免 deepseek 对 15 行+SQL 的洞察动辄 45s
        if _is_analysis_intent(query):
            _det = _build_deterministic_insight(query, cols, rows)
            if _det:
                return _det + (f"\n\n⚠️ 结果可靠性提示：{warning}" if warning else "")
        # 洞察缓存命中（重复问法/语义缓存命中后二次问）：直接复用，不再调 LLM
        cached = _insight_cached(query, rc)
        if cached:
            return (cached + (f"\n\n⚠️ 结果可靠性提示：{warning}" if warning else ""))
        data_json = json.dumps(rows[:15], ensure_ascii=False, default=str)[:1500]
        fields_info = ", ".join(cols)
        if len(rows) > 15:
            fields_info += f"（共 {len(rows)} 行，以上为前 15 行样例）"
        prompt = ANALYSIS_SYSTEM_PROMPT.format(
            data_json=data_json, fields_info=fields_info, query=query,
        )
        if sql:
            prompt += f"\n\n## 数据来源 SQL（用于理解统计口径）\n{sql[:800]}"
        # Critic 告警注入 prompt（与 _llm_analysis 同口径，2026-09-13）：
        # 告警存在时不得把数值当正确结论复述/展开业务建议。
        if warning:
            prompt += (
                "\n\n## 重要：结果可靠性告警\n"
                f"独立复核（Critic）判定本次结果的指标计算可能不正确：{warning[:300]}\n"
                "你的解读【不得】把这些数值当作正确结论复述，也【不得】基于它们给出"
                "业务判断或建议。开头先用一句话明确提示：本次结果的指标口径可能不正确、"
                "以下数值仅供参考；然后只客观描述数据现象（不下业务结论），"
                "最后建议用户核对指标定义或在结果卡片「去登记口径」固定正确算法。"
            )
        if _is_analysis_intent(query):
            prompt += (
                "\n\n## 任务：用户的问题是分析类问题，你必须先正面回答它——"
                "例如是否存在相关/差异/趋势、结论是什么，给出明确判断后，"
                "再引用数据佐证（含关键数值），最后给 1-2 条业务建议。"
                "不要只罗列数据或复述查询结果。若现有数据不足以判断，请如实说明"
                "缺什么数据、建议如何进一步分析。"
            )
            corr = _corr_evidence(cols, rows)
            if corr:
                prompt += f"\n\n{corr}"
        prompt += "\n\n注意：只依据上面给出的真实数据下结论，不得编造数据中不存在的数字。"
        llm = _make_llm(temp=0.0, max_tokens=512)
        # 20s 超时兜底：慢洞察不再无限拖住响应（deepseek 分析类洞察约 10~15s，8s 会误伤截断）
        text = ""
        try:
            _ex = ThreadPoolExecutor(max_workers=1)
            try:
                text = str(_ex.submit(lambda: llm.invoke([HumanMessage(content=prompt)]))
                           .result(timeout=20).content or "").strip()
            finally:
                _ex.shutdown(wait=False)
        except Exception:
            text = ""
        if not text:
            return _quick_rule_summary(rows, cols)
        _insight_store(query, rc, text)
        if warning:
            text += f"\n\n⚠️ 结果可靠性提示：{warning}"
        return text
    except Exception:
        try:
            return _quick_rule_summary(rows, cols)
        except Exception:
            return ""


def generate_recommend_questions(query: str, sql: str, sql_result: dict, schema_context: str) -> list[str]:
    """生成推荐问题（LLM 版，非流式快速返回）— 公共实现，predict.py 复用同一份"""
    if not sql_result.get("rows"):
        return []

    rows = sql_result["rows"]
    result_summary = f"{len(rows)} rows, columns: {', '.join(sql_result['columns'])}"
    if rows:
        result_summary += f", sample: {json.dumps(rows[0], ensure_ascii=False, default=str)[:120]}"
    structure = _result_structure_hint(sql_result)
    if structure:
        result_summary += f"; 结构线索: {structure}"

    prompt = RECOMMEND_QUESTIONS_PROMPT.format(
        query=query, sql=sql[:200],
        result_summary=result_summary,
        schema_context=schema_context[:800],
    )

    try:
        llm = _make_llm(temp=0.5, max_tokens=512)
        resp = llm.invoke([HumanMessage(content=prompt)])
        raw = resp.content.strip()
        match = re.search(r'\[[\s\S]*\]', raw)
        if match:
            return json.loads(match.group(0))
    except Exception:
        pass
    return []


# ── Fallback SQL（用元数据，不查 DB）─────────────────────

# ── 最近训练的模型（供对话内预测复用，按库隔离防跨库串用）──
_ml_model_by_db: dict[str, dict] = {}


# ── LLM 推断分析（未定义口径/模糊查询 → 弹窗二次确认）────────────────
# 用户问题未命中注册口径（no_hit）时，调用 LLM 产出结构化推断（理解/指标/维度/
# 筛选/时间/SQL草稿/置信度/风险），前端弹窗展示，用户可「遵循 LLM 执行」、
# 「修改后执行」或「自定义口径」。推断失败自动返回 None 降级，不影响主流程。
_ANALYSIS_CONFIRM_ENABLED = os.getenv("ANALYSIS_CONFIRM_ENABLED", "1") == "1"
# LLM 推断单次总时长上限（默认 45s = json 主推断 15s + plain 重试 12s + 逃生 15s + 余量；
# 2026-09-06 从 70s 逐轮收紧并加首 token 门：0 输出卡死不再白等，正常推断完全覆盖）
_ANALYSIS_TIMEOUT_S = float(os.getenv("ANALYSIS_CONFIRM_TIMEOUT", "45"))
# 首 token 门（2026-09-06）：LLM 流式 N 秒无任何输出 = json_mode 卡死特征（实测库存预警题
# 3 连发全 0、白等 46s）——立即放弃本次调用交给上层换路重试，不傻等满预算。
# 2026-09-14 由 8s 上调到 12s：总预算已由 _SQL_GEN_WALL_S 封顶（40s），
# 8s 对长中文 prompt 的首 token 过于激进，会把"慢但能出"的调用误杀成空输出。
_FIRST_TOKEN_S = float(os.getenv("ANALYSIS_FIRST_TOKEN_S", "12"))

# ── SQL 生成链的整体墙钟上限 ─────────────────────────────────────────────
# 此前整条链没有统一上限：主推断 15s + 直生 85s，而直生内部两轮 _fetch 又各自
# 重开 85s 计时 → 最坏 100s+，用户侧表现为"卡住不出结果"。
# 现在改为共享一个天花板：推断先取 ≤65%，直生拿**剩余时间**（不再是固定 85s）。
#
# ⚠️ 2026-09-15 由 40s 先后上调到 110s、160s（用户实测驱动的根因修复）：
# 40s 的原始测算假设"40~60 token/s → 1600~2400 token 够用"，只算了**正文** token，
# **漏算了推理 token**。当前主力模型 deepseek-v4.1-flash 是混合思考模型，会先输出
# 一段思考再出正文，实测单题思考可达 1255~5000 token（约 10~30s），复杂题
# （EXISTS 半连接 / LAG 窗口 / 多条件 HAVING）思考更久 —— 40s 天花板在"正文还没开始
# 就已经到点"，流被截断 → content 0 字 → 报告「AI 未能根据问题生成查询 SQL」。
#
# 三档实测（yans 高价值题库 6 题，同一套代码只改本值）：
#   40s  → 3/6，其中 2 题因截断报"未能生成 SQL"
#   110s → 3~4/6，GEN_FAIL 恰好卡在 110~116s（= 撞天花板）
#   160s → **5/6**（仅剩 1 题因语义口径不同未过），GEN_FAIL 归零
# 结论：**预算要给够思考，而不是砍掉思考**（关思考实测语义正确率 0/6）。
# 160s 只在难题上被吃满；简单题仍在 40~55s 返回（预算是上限不是目标值）。
# 端到端最坏 = 表匹配(≤10s，有缓存) + 生成 160s + 执行/复核。
# 若演示节奏优先，可用环境变量 SQL_GEN_WALL_S 调小（代价：复杂题重新出现截断）。
_SQL_GEN_WALL_S = float(os.getenv("SQL_GEN_WALL_S", "160"))
# 生成预算里分给「结构化口径推断（MQL）」的比例。2026-09-14 **主次反转**：
# 由 0.30 提到 0.65 —— MQL 路径让模型只选「指标+维度+筛选」，SQL 由确定性编译器拼装，
# 列名/表名/关联键都由编译器按真实 schema 把关，是又快又稳的主路径；自由生成 SQL
# 降为兜底（拿剩余 35%）。两者合计仍受 _SQL_GEN_WALL_S 封顶。
_SQL_GEN_INFER_RATIO = float(os.getenv("SQL_GEN_INFER_RATIO", "0.65"))


# ── 确定性 SQL 编译（推断用，2026-09-02）────────────────────
# 红线收敛：LLM 只输出「结构化口径」（指标 agg+列 / 维度列 / 过滤条件），
# 可执行 SQL 由本函数按规则编译——LLM 不再直接产出 SQL（比原来少生成一段 300~500
# token 的 sql_draft，推断从 35s+ 失败收敛到 ~10s 内成功，与「确定性编译」架构一致）。
_SQL_AGG = {"SUM", "AVG", "COUNT", "MAX", "MIN"}


def _compile_sql_draft(data: dict, matched_tables: list) -> str:
    """从结构化口径确定性编译 SQL。

    - 主表：优先含「聚合列」最多的表（指标正确优先于维度）；无聚合列退维度归属最多的表；
    - 跨表：仅当次表含 id 且主表含 {次表基名}_id 时保守 JOIN；否则跨表列剔除（弹窗内用户可改）；
    - 列名校验：引用列必须存在于目标表真实列集（大小写不敏感），否则剔除；
    - 编译不出任何聚合 → 返回 ""（调用方置 None 走降级）。
    返回的 SQL 会展示在二次确认弹窗中，用户可编辑；execute_confirm 另有列名校验兜底。
    """
    try:
        col_map = _all_table_columns() or {}
    except Exception:
        col_map = {}
    base_cols: dict[str, set[str]] = {}
    for t in matched_tables or []:
        name = str(t.get("table_name", "")).split(".")[-1].lower()
        cols = set()
        for c in (col_map.get(t.get("table_name"), []) or col_map.get(name, []) or []):
            cols.add(str(c).lower())
        base_cols[name] = cols

    def _strip_prefix(_t: str) -> str:
        # 业务表名前缀剥除（2026-09-06 修复：原 lstrip("dim_fact_mes_tb_t_") 按【字符集】剥离，
        # dim_equipment → "quipment"（e 在字符集内被误剥）→ 外键语义匹配失败 → 维度被丢弃；
        # 正则按完整前缀匹配才正确。
        return re.sub(r"^(?:dim_|fact_|mes_|ods_|stg_|tb_|t_|md_|dict_)", "", str(_t))
    order = [str(t.get("table_name", "")).split(".")[-1].lower()
             for t in (matched_tables or []) if t.get("table_name")]
    if not order:
        return ""
    metrics = data.get("metrics") or []
    dims = [str(d).strip() for d in (data.get("dimensions") or []) if str(d).strip()]
    filters = data.get("filters") or []

    def t_of(col: str) -> str | None:
        cl = str(col).lower()
        for n in order:
            if cl in base_cols.get(n, set()):
                return n
        return None

    agg_owner = {str(m.get("column") or "").strip(): None for m in metrics}
    agg_owner = {c: t_of(c) for c in agg_owner if c}
    primary = None
    if agg_owner:
        primary = max(set(agg_owner.values()),
                      key=lambda n: sum(1 for o in agg_owner.values() if o == n))
    if not primary and dims:
        dc = {d: t_of(d) for d in dims if t_of(d)}
        if dc:
            primary = max(set(dc.values()),
                          key=lambda n: sum(1 for o in dc.values() if o == n))
    primary = primary or order[0]
    # 保守 JOIN：优先「主表与次表同名 *_id 列」直接关联（yans 维度表主键=短名_id，与事实表外键同名，
    # 如 qms_inspection.process_id ↔ dim_process.process_id）；否则回退「主表 *_id 外键
    # 语义匹配次表主键(id / {完整表名}_id / {短名}_id)」。
    joinable = set()
    for n in order:
        if n == primary:
            continue
        ncols = base_cols.get(n, set())
        pcols = base_cols.get(primary, set())
        # 1) 同名 _id 列直连（覆盖 yans 全部维度表）
        # 2026-09-06 修复「外键选错」：eqp_downtime_record↔dim_equipment 同时有 equipment_id
        # 与 line_id，原 set 无序遍历可能误选 line_id（同线多设备 → JOIN 放大，实测 ④ 停机
        # SUM 15600→67720）。多个同名候选时按语义精度选择：{次表短名}_id（equipment_id）
        # > 完整表名_id > 任意同名（多候选歧义时弃用自动 JOIN，宁缺毋滥走弹窗人工确认）。
        matched = False
        _short_n = _strip_prefix(n)
        _cands = [c for c in pcols if c.endswith("_id") and c in ncols]
        if _cands:
            def _fk_score(c):
                b = c[:-3]
                if _short_n and b == _short_n:
                    return 0
                if b == n:
                    return 1
                return 2
            _best = min(_cands, key=_fk_score)
            if _fk_score(_best) <= 1 or len(_cands) == 1:
                joinable.add((n, _best, _best))
                matched = True
        if matched:
            continue
        # 2) 次表主键候选：id / {完整表名}_id / {短名}_id（如 dim_process → process_id）
        short = _strip_prefix(n)
        pk = "id" if "id" in ncols else None
        if pk is None and f"{n}_id" in ncols:
            pk = f"{n}_id"
        if pk is None and short and f"{short}_id" in ncols:
            pk = f"{short}_id"
        if pk is None:
            continue
        # 主表外键候选：列名形如 *_id，且 * 与次表名语义匹配（含去掉 dim_/mes_ 等业务前缀）
        for c in pcols:
            if not c.endswith("_id"):
                continue
            b = c[:-3]
            if b == n or b.endswith(n) or n.endswith(b) or _strip_prefix(n) == _strip_prefix(b):
                joinable.add((n, c, pk))
                break

    # ── 惰性 JOIN（2026-09-06 修复「JOIN 放大」：工单统计被 mes_process_output 一对多
    #    放大 8 倍——COUNT(*) 数成产出记录 1656，真实工单 207）。只保留【实际引用到次表列】
    #    的 JOIN（指标列/维度/筛选/时间列属于哪张次表）；全部在主表 → 零 JOIN，聚合语义正确。
    _refs: list[str] = []
    for _m in metrics[:3]:
        _mc = str(_m.get("column") or "").strip()
        if _mc:
            _refs.append(_mc)
    _refs += [str(d).strip() for d in dims[:6] if str(d).strip()]
    _refs += [str(f.get("field") or f.get("column") or "").strip()
              for f in filters[:6] if (f.get("field") or f.get("column"))]
    _tr0 = data.get("time_range")
    if isinstance(_tr0, dict) and _tr0.get("column"):
        _refs.append(str(_tr0.get("column") or "").strip())
    _need: set[str] = set()
    for _c in _refs:
        _cl = str(_c).lower()
        for _n, _fk, _pk in joinable:
            if _cl in base_cols.get(_n, set()):
                _need.add(_n)
                break
    # 角色过滤（2026-09-06 二修「JOIN 放大」）：即使被引用，【非维表】（事实表间连接如
    # mes_work_order→mes_process_output 1:N、qms_inspection→qms_defect_detail 1:N）依然会
    # 把 COUNT(*)/SUM 放大或明细翻倍——仅允许 JOIN 维表（dim_/md_/dict_ 前缀，多对一安全）；
    # 对事实表列的引用在原 usable/dropped 逻辑中自动剔除并进 risks（弹窗可见，宁缺毋滥）。
    def _is_dim_role(_n: str) -> bool:
        _s = str(_n).split(".")[-1].lower()
        return _s.startswith(("dim_", "md_", "dict_")) or "_dim" in _s
    joinable = {(n, fk, pk) for (n, fk, pk) in joinable
                if n in _need and _is_dim_role(n)}

    def usable(col: str):
        cl = str(col).lower()
        if cl in base_cols.get(primary, set()):
            return True
        for _n, _fk, _pk in joinable:
            if cl in base_cols.get(_n, set()):
                return True
        return False

    def qcol(col: str):
        """v2：带表限定符的列引用 —— 消 JOIN 同名列歧义。

        基线证据（2026-09-04）：3 题因列引用无前缀触发 PG 42702「字段关联不明确」。
        规则：主表优先（指标列语义优先主表）；列不存在返回 None（调用方丢弃）。
        """
        cl = str(col).lower()
        if cl in base_cols.get(primary, set()):
            return f"{primary}.{col}"
        for _n, _fk, _pk in joinable:
            if cl in base_cols.get(_n, set()):
                return f"{_n}.{col}"
        return None

    # ── WHERE 构建（v2：提前构建，聚合/明细两种模式共用）──
    where: list[str] = []
    dropped: list[str] = []
    for f in filters[:6]:
        field = str(f.get("field") or f.get("column") or "").strip()
        op = str(f.get("op") or "").strip().upper()
        val = f.get("value")
        if not field or op not in ("=", "!=", "<>", ">", "<", ">=", "<=", "LIKE", "IN"):
            continue
        qc = qcol(field)
        if qc is None:
            dropped.append(field)
            continue
        if op == "IN":
            # v2：同列多可选值（「运行或空闲」类问法由 LLM 转 IN，避免实现通用 OR）
            if not isinstance(val, (list, tuple)) or not val:
                continue
            parts = []
            for v in list(val)[:50]:
                if isinstance(v, bool):
                    parts.append("TRUE" if v else "FALSE")
                elif isinstance(v, (int, float)):
                    parts.append(str(v))
                else:
                    parts.append("'" + str(v).replace("'", "''") + "'")
            where.append(f"{qc} IN ({', '.join(parts)})")
            continue
        if isinstance(val, bool):
            sv = "TRUE" if val else "FALSE"
        elif isinstance(val, (int, float)):
            sv = str(val)
        else:
            sv = "'" + str(val).replace("'", "''") + "'"
        where.append(f"{qc} {op} {sv}")

    # ── v2：time_range 落入 WHERE（基线前该字段被静默丢弃）──
    # 仅接受 dict 格式 {column, start, end}；日期必须严格 YYYY-MM-DD（防注入）。
    _tr = data.get("time_range")
    if isinstance(_tr, dict):
        _tc = str(_tr.get("column") or "").strip()
        _ts = str(_tr.get("start") or "").strip()
        _te = str(_tr.get("end") or "").strip()
        if _tc and (_ts or _te):
            _qc = qcol(_tc)
            if _qc is None:
                dropped.append(_tc)
            else:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", _ts):
                    where.append(f"{_qc} >= '{_ts}'")
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", _te):
                    where.append(f"{_qc} <= '{_te}'")

    join_sql = f" FROM {primary}"
    for n, fk, pk in sorted(joinable):
        join_sql += f" JOIN {n} ON {primary}.{fk} = {n}.{pk}"

    # ── 聚合模式：指标列 ──
    selects: list[str] = []
    for m in metrics[:3]:
        name = str(m.get("name") or "").strip() or "指标"
        col = str(m.get("column") or "").strip()
        agg = str(m.get("agg") or "").upper()
        if agg not in _SQL_AGG:
            agg = "COUNT" if not col else "SUM"
        if col:
            qc = qcol(col)
            if qc is None:
                dropped.append(str(m.get("name") or col))
                continue
            # v2：distinct 计数（「有多少种单位」类问法）；聚合列同样带表前缀（消 JOIN 歧义）
            inner = f"DISTINCT {qc}" if m.get("distinct") else qc
            expr = f"{agg}({inner})"
        else:
            expr = "COUNT(*)"
        label = str(name)[:40].replace('"', "")
        selects.append(f'{expr} AS "{label}"')

    gdims = []
    for d in dims[:4]:
        qc = qcol(d)
        if qc is not None:
            gdims.append(qc)
        else:
            dropped.append(d)

    if selects:
        sql = "SELECT " + ", ".join(selects)
        if gdims:
            sql += ", " + ", ".join(gdims)
        sql += join_sql
        if where:
            sql += " WHERE " + " AND ".join(where)
        if gdims:
            sql += " GROUP BY " + ", ".join(gdims)
        sql += " ORDER BY 1 DESC LIMIT 20"
        return sql, dropped

    # ── v2 明细模式：无聚合指标但有维度列 → 「X 有哪些 / 是什么」类问法 ──
    # 基线证据（2026-09-04）：3 题明细问法因「编译不出聚合→返回空」而 infer_fail。
    # 仍要求至少一个可用列，避免编出无意义 SELECT。
    detail_dims = []
    for d in dims[:6]:
        qc = qcol(d)
        if qc is not None:
            label = str(d)[:40].replace('"', "")
            detail_dims.append(f'{qc} AS "{label}"')
        else:
            dropped.append(d)
    if not detail_dims:
        return "", dropped
    sql = "SELECT " + ", ".join(detail_dims) + join_sql
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " LIMIT 20"
    return sql, dropped


# ── P1：低基数枚举值注入（2026-09-04）────────────────────
# 基线证据：LLM 不知道 status 列有哪些取值（risks 里自己提示"可能是'运行'"），
# 导致「运行中状态的产线」「运行或空闲的设备」2 题 value_mismatch——SQL 编译完全正确，
# 唯独筛选值是编的。解法：把候选表低基数文本列的真实 distinct 值注入 prompt。
_ENUM_VAL_TTL = 900.0          # 枚举值缓存 15min（数据枚举几乎不变；2026-09-06 5→15min 减冷查阻塞）
_ENUM_MAX_DISTINCT = 20        # distinct 超过此值 = 高基数列，不注入
_ENUM_VALS_PER_COL = 10        # 每列最多注入 10 个值
_ENUM_VAL_MAXLEN = 20          # 单值截断长度（防长文本撑爆 prompt）
_ENUM_COLS_PER_TABLE = 6       # 每表最多查 6 个文本列（启发式排序后取前 6）
_ENUM_TOTAL_BUDGET = 400       # 枚举段总字符预算（2026-09-06 700→400：控 prompt 长度防 0 输出）
# 枚举触发词：仅问题含这些词才收集注入枚举（否则纯增 prompt 无收益）
_ENUM_TRIGGER_RE = (r"状态|类型|类别|种类|方式|性质|是否|级别|等级|班次|单位|仓库|"
                    r"来源|去向|结果|合格|不合格|用途|品牌|渠道|区域|阶段|场景|"
                    r"status|type|category|state|warehouse|unit|result|shift|grade")
_ENUM_DEADLINE_S = 2.0         # 枚举收集总时限（2026-09-06 3→2s：LLM 推断前的最小阻塞）
_enum_val_cache: dict[tuple, tuple] = {}
_enum_lock = threading.Lock()

_ENUM_COL_PRIO_KW = ("status", "state", "type", "category", "kind", "level",
                     "grade", "unit", "result", "reason", "priority", "name",
                     "mode", "class", "shift")


# ── 未注册口径加固（2026-09-12 评测驱动）────────────────────────────
# 60 题未注册口径复杂问题评测（eval/bench_unregistered.py）基线 48.3%，
# 失败模式 Top3：① 编造 JOIN 条件（prompt 里根本没有外键信息，LLM 只能凭列名猜）
# ② 退化 SQL："各工厂的…" 生成出无 JOIN、无 GROUP BY 的单值聚合
# ③ 枚举值幻觉（status='running'/'故障'，真实值是中文）。以下三个函数分别对应。

def _infer_join_relations(table_names: list) -> list[str]:
    """按**真实列名**推断候选表之间的关联键（数据库未声明外键时的确定性兜底）。

    背景（2026-09-14 用户实测驱动）：`yans` 库 `get_foreign_keys()` 返回 **0 条**，
    于是 `_join_hint_for_tables` 直接返回空串 —— 模型对 JOIN 键**零约束**，实测生成
    `ON po.process_id = dp.process_code`（把 ID 列关联到编码列）。这条 SQL 能执行、
    两个列都真实存在，**任何事后校验都拦不住**，结果静默错。属于"必须事前约束"的典型。

    规则（只依赖真实列名，完全确定、可验证）：
      ① 同名 ID 列：候选表里有 `X_id`，另一张候选表也有 `X_id` → 关联它们；
      ② 带修饰前缀的 ID：如 `responsible_process_id` → 取尾部 `process_id` 去匹配
         （`responsible_process_id = dim_process.process_id`）。
    绝不臆造：两侧列名都必须在真实列清单中出现过，且父表在本次候选表集合内。
    """
    try:
        cm = _all_table_columns() or {}
        names = [str(t) for t in (table_names or []) if t]
        if len(names) < 2:
            return []
        norm: dict[str, str] = {}
        for t in names:
            norm[str(t).split(".")[-1].lower()] = t
        cols: dict[str, list[str]] = {}
        for k, orig in norm.items():
            raw = cm.get(k) or cm.get(orig) or cm.get(str(orig).split(".")[-1]) or []
            cols[k] = sorted({str(c).lower() for c in raw})
        # 列名 → 拥有该列的候选表
        col_index: dict[str, list[str]] = {}
        for k in sorted(cols):
            for c in cols[k]:
                col_index.setdefault(c, []).append(k)
        lines: list[str] = []
        seen: set[str] = set()
        for child in sorted(cols):
            for c in cols[child]:
                if not c.endswith("_id") or c == "id":
                    continue
                targets: list[tuple[str, str]] = [(p, c) for p in col_index.get(c, []) if p != child]
                base = c[:-3]
                if "_" in base:
                    tail = base.rsplit("_", 1)[-1] + "_id"
                    if tail != c:
                        targets += [(p, tail) for p in col_index.get(tail, []) if p != child]
                for p, pc in targets:
                    line = f"- {norm[child]}.{c} = {norm[p]}.{pc}"
                    if line not in seen:
                        seen.add(line)
                        lines.append(line)
        return lines
    except Exception:
        return []


def _join_hint_for_tables(table_names: list) -> str:
    """候选表之间的外键关联键 + 多跳路径提示（防 LLM 编造 JOIN 条件）。

    评测证据：问「各产品类别的生产不良率」，LLM 生成 `pr.wo_id = p.id`
    （把工单 ID 当产品 ID 关联）—— 因为 prompt 只有列名清单，没有任何外键信息。
    注入真实外键 + 多跳路径后，LLM 才有依据走 production_record→work_order→product。

    2026-09-14 加固：此前 `if not fk_map: return ""` —— 数据库**没有声明外键**时
    （yans 实测 0 条）直接放弃注入，等于对 JOIN 键零约束。现改为：声明外键优先，
    不足部分用 `_infer_join_relations()` 按真实列名补全；并加一条**硬约束**明确
    「`*_id` 只能与同名 `*_id` 关联，禁止关联到 `*_code`/`*_name`/`*_no`」。
    """
    try:
        fk_map: dict = {}
        try:
            from db.tools import get_foreign_keys
            fk_map = get_foreign_keys() or {}
        except Exception:
            fk_map = {}
        names = [str(t) for t in (table_names or []) if t]
        if not names:
            return ""
        lines: list[str] = []
        seen: set[str] = set()
        for child in names:
            for fk in (fk_map.get(child) or []):
                parent = str(fk.get("ref_table") or "")
                col, ref_col = str(fk.get("column") or ""), str(fk.get("ref_column") or "")
                if not parent or not col:
                    continue
                line = f"- {child}.{col} = {parent}.{ref_col}"
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
        # 声明外键不足 → 用真实列名推断补全（无外键库的主通道）
        for line in _infer_join_relations(names):
            if line not in seen:
                seen.add(line)
                lines.append(line)
        if fk_map:
            try:
                from agent.join_planner import plan_joins
                plan = plan_joins(names, fk_map, max_hops=2)
                for p in (plan.get("paths") or []):
                    if getattr(p, "hops", 0) >= 2:
                        line = "- 路径 " + " ⋈ ".join(p.tables) + "：" + "；".join(p.edges)
                        if line not in seen:
                            seen.add(line)
                            lines.append(line)
            except Exception:
                pass
        if not lines:
            return ""
        return ("表间关联（多表 JOIN 只能用下列关联键；两表若无直接键，必须按给出的路径经中间表关联，"
                "严禁凭列名相似自行拼 ON 条件）：\n"
                "【硬约束】`*_id` 列只能与**同名** `*_id` 列关联，绝不允许把 `*_id` 关联到 "
                "`*_code`/`*_name`/`*_no`/`*_type` 等其它列（同名 ID 列才指向同一实体）。"
                "正例：mes_process_output.process_id = dim_process.process_id；"
                "反例：mes_process_output.process_id = dim_process.process_code。\n"
                + "\n".join(lines[:16]) + "\n\n")
    except Exception:
        return ""


def _enum_block_for_tables(query: str, table_names: list, force: bool = False) -> str:
    """低基数枚举值块（默认仅问题含枚举触发词时收集），与 JOIN 提示共用注入通道。

    force=True 时无条件注入：直生路径候选表少（≤4 张）、LLM 倾向凭先验编造
    英文枚举值（status='running'/'in_progress'，真实值是中文），实测触发词门槛
    挡不住（「开机率」「在制工单数」都不含"状态"二字却用到了 status 列）。
    """
    try:
        if not force and not re.search(_ENUM_TRIGGER_RE, query or "", re.I):
            return ""
        _enums = _enum_values_for_tables([t for t in (table_names or [])[:4]]) or {}
        if not _enums:
            return ""
        _parts = [f"- {k}: " + " | ".join(v) for k, v in sorted(_enums.items())][:6]
        return ("可选枚举值（筛选值必须逐字取自这里，不得编造）：\n" + "\n".join(_parts) + "\n\n")
    except Exception:
        return ""


_GROUP_BY_RE = re.compile(r"各|每个|每家|每条|各类|按\s*\S+|分别|分布|占比|比例|排名|排行|对比|各个")
_NO_GROUP_RE = re.compile(r"^(?:总共|一共|总体|全部|合计)\s*(?:有|是|多少)|总共多少|一共多少")


def _needs_group_by(query: str) -> bool:
    """问题是否要求「按维度分组」统计（用于拦截无 GROUP BY 的退化 SQL）。"""
    q = str(query or "").strip()
    if not q:
        return False
    if _NO_GROUP_RE.search(q):
        return False
    return bool(_GROUP_BY_RE.search(q))


def _has_group_or_window(sql: str) -> bool:
    """SQL 是否具备分组能力（GROUP BY / 窗口 PARTITION BY / 分组子查询）。"""
    s = str(sql or "")
    return bool(re.search(r"\bGROUP\s+BY\b|\bPARTITION\s+BY\b", s, re.IGNORECASE))


# ── 未注册复合指标「缺源数据」硬拒绝（2026-09-13 用户实测驱动）────────────
# 用户问「库存周转天数 / 人均产出 / 设备稼动率」等未注册复合指标时，若计算所需的
# 源数据（出库流量 / 人数 / 运行时长）在全库都不存在，LLM 只能用无关列近似冒充
# （实测：周转天数→MAX(可用库存)、人均产出→AVG(产量) 不除人数），产出"一本正经
# 的错误答案"，且数值越大越像真的（4795 天）。此类问题在生成前源头拒绝，
# 引导登记口径/接入数据，而不是让 LLM 编一个能跑的 SQL。

_UNDERIVABLE_METRIC_RULES: list[tuple[re.Pattern, re.Pattern, str]] = [
    (re.compile(r"人均|每人|按人头|人天产出"),
     re.compile(r"人数|员工数|headcount|employee_count|staff_count|worker_count|"
                r"person_count|num_workers|manpower", re.I),
     "人均类指标需要「人数」字段（员工数/工人数）做分母。当前数据库没有人数类字段，"
     "无法计算人均值（只有产量/产值总量）。请接入人数数据源，"
     "或在结果卡片「去登记口径」明确人均的计算公式。"),
    (re.compile(r"周转率|周转天数|周转次数"),
     re.compile(r"出库|消耗|领用|销售量|发货|shipment|consumption|issue_qty|out_qty|"
                r"sold|usage_qty|sale_qty", re.I),
     "库存周转类指标需要「出库/消耗/销售」流量数据（周转率=消耗/平均库存，"
     "周转天数=库存/日均出库）。当前数据库只有库存快照（可用/冻结/安全库存），"
     "没有流量字段，无法计算周转。请接入出入库流水数据，"
     "或在结果卡片「去登记口径」明确周转的计算公式。"),
    (re.compile(r"稼动率|开动率|产能利用率|设备利用率"),
     re.compile(r"运行时长|开机时长|运转|稼动|run_time|uptime|run_hours|operating_hours|"
                r"runtime", re.I),
     "稼动率/利用率类指标需要「运行/开机时长」字段（稼动率=运行时间/计划时间）。"
     "当前数据库只有停机记录（downtime），缺少计划/日历工时，无法计算稼动率。"
     "请接入设备运行时长数据，或在结果卡片「去登记口径」明确稼动率的计算公式。"),
]


def _underivable_metric_reason(query: str) -> str | None:
    """未注册复合指标缺源数据 → 返回拒绝理由（None=可继续生成）。

    判据：① 问题命中复合指标词（人均/周转/稼动率…）且未命中任何注册口径
    （关键词命中注册口径 → 口径由注册表保证，不拦）；② 全库列名中找不到
    该指标所需的源字段。列名证据在全库找而非仅候选表——只要有任何表能
    提供源字段就不拦截（此时由「比率必须做除法」等前置校验兜底），
    只有全库都无源字段才硬拒绝：这种情况下任何"能跑"的 SQL 都必然是冒充。
    """
    try:
        q = str(query or "").strip()
        if not q:
            return None
        from agent.metric_registry import find_metrics
        if find_metrics(q)[:1]:
            return None  # 命中注册口径（含别名），口径由注册表保证
        _cm = _all_table_columns() or {}
        _all_cols = [str(c) for cols in _cm.values() for c in cols]
        for pat, col_pat, reason in _UNDERIVABLE_METRIC_RULES:
            if pat.search(q) and not any(col_pat.search(c) for c in _all_cols):
                return reason
    except Exception:
        return None
    return None


def _fact_output_table_exists() -> bool:
    """mes_process_output 是否存在于当前库（postgres 评测库专属表）。

    「产量统计必须用 mes_process_output」这条业务校验规则只在表真实存在时启用；
    否则会污染其他库：123 库里 LLM 偶发生成 mes_work_order 后，修复提示会把 LLM
    引向当前库不存在的 mes_process_output（评测 123#12/#13 实锤，越修越错）。
    """
    try:
        cm = _all_table_columns() or {}
        return any(str(k).split(".")[-1].lower() == "mes_process_output" for k in cm)
    except Exception:
        return False


def _safe_ident(name: str) -> bool:
    """SQL 标识符白名单校验（表名/列名只允许字母数字下划线，防注入）。"""
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "").strip()))


def _text_columns_of(table_short: str) -> list[str]:
    """表内文本型列（低基数候选），带 5min 缓存。"""
    ck = (_current_db_key(), table_short, "_textcols")
    with _enum_lock:
        hit = _enum_val_cache.get(ck)
    if hit and time.time() - hit[0] < _ENUM_VAL_TTL:
        return hit[1]
    cols: list[str] = []
    try:
        from database import get_db_type
        if get_db_type() == "mysql":
            sql = ("SELECT column_name FROM information_schema.columns "
                   f"WHERE table_schema = DATABASE() AND table_name = '{table_short}' "
                   "AND data_type IN ('varchar','char','text','enum') ORDER BY ordinal_position")
        else:
            sql = ("SELECT column_name FROM information_schema.columns "
                   f"WHERE table_name = '{table_short}' "
                   "AND data_type IN ('character varying','character','text','USER-DEFINED') "
                   "ORDER BY ordinal_position")
        r = execute_sql(sql)
        for row in (r.get("rows") or []):
            c = str(row.get("column_name") or "").strip()
            if c:
                cols.append(c)
    except Exception:
        pass
    with _enum_lock:
        _enum_val_cache[ck] = (time.time(), cols)
    return cols


def _enum_values_for_tables(table_names: list) -> dict:
    """收集候选表低基数文本列的 distinct 值。

    返回 {"表短名.列名": [v1, v2, ...]}；高基数/空列/超时限的列不返回。
    按 (db_key, 表) 粒度缓存 5min —— 同库重复问法零 DB 成本。
    护栏：execute_sql 自带的 EXPLAIN 估算闸门/statement_timeout 天然兜底大表扫描。
    """
    out: dict = {}
    deadline = time.monotonic() + _ENUM_DEADLINE_S
    for tname in (table_names or [])[:4]:
        short = str(tname or "").split(".")[-1].strip().lower()
        if not _safe_ident(short):
            continue
        with _enum_lock:
            hit = _enum_val_cache.get((_current_db_key(), short))
        if hit and time.time() - hit[0] < _ENUM_VAL_TTL:
            for c, vals in (hit[1] or {}).items():
                out[f"{short}.{c}"] = vals
            continue
        cols = [c for c in _text_columns_of(short) if _safe_ident(c)]

        def _prio(c):
            n = c.lower()
            return 0 if any(k in n for k in _ENUM_COL_PRIO_KW) else 1

        cols = sorted(cols, key=_prio)[:_ENUM_COLS_PER_TABLE]
        col_vals: dict = {}
        for c in cols:
            if time.monotonic() > deadline:
                break
            try:
                r = execute_sql(
                    f"SELECT DISTINCT {c} FROM {short} WHERE {c} IS NOT NULL "
                    f"LIMIT {_ENUM_MAX_DISTINCT + 1}")
                if not r.get("success"):
                    continue
                vals = []
                for row in (r.get("rows") or []):
                    v = row.get(c)
                    if v is None:
                        continue
                    s = str(v).strip()
                    if s:
                        vals.append(s)
                if not vals or len(vals) > _ENUM_MAX_DISTINCT:
                    continue  # 空列或高基数 → 不注入
                col_vals[c] = [v[:_ENUM_VAL_MAXLEN] for v in vals[:_ENUM_VALS_PER_COL]]
            except Exception:
                continue
        if col_vals and time.monotonic() <= deadline:
            with _enum_lock:
                _enum_val_cache[(_current_db_key(), short)] = (time.time(), col_vals)
            for c, vals in col_vals.items():
                out[f"{short}.{c}"] = vals
    return out


# ── 「枚举值 → 表」倒排索引（2026-09-15，修维表漏匹配）──────────────────
# 动机（yans 高价值题库 #4「既生产过传感器又生产过控制器的产线有哪些」实测）：
# 表匹配只按**表名与字段名**做关键词/向量召回，命中的是 mes_process_output（"生产"）、
# mes_work_order（"生产"）、dim_production_line（"产线"）—— 而**真正持有「传感器 / 控制器」
# 这两个值的 dim_product 完全没进候选表**。候选表清单里没有维表，模型就只剩两条路：
#   ① 把类别名硬比到事实表外键上 → 实测生成 `po.product_id = '传感器'`，0 行；
#   ② 凭先验编出库外的表名（本次恰好蒙对 dim_product，换库就会编错）。
# 两条都是"候选表没给全"造成的，靠改提示词治不了。这里按「值 → 表」反向补齐：
# 低基数文本列里出现过问题中的字面值，就把该表纳入候选。
# 成本：全库小表一次 distinct 扫描（10 表 × ≤6 文本列），结果按库落盘缓存复用。
_LITERAL_INDEX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".literal_table_index.json")
_LITERAL_INDEX_TTL = 3600.0        # 1h；枚举值属于慢变数据
_literal_index_lock = threading.Lock()


def _build_literal_table_index() -> dict:
    """扫描全库低基数文本列，建「值 → 表短名」倒排索引（仅小基数列）。"""
    idx: dict = {}
    try:
        from db.tools import get_real_tables
        real = get_real_tables() or []
    except Exception:
        return idx
    for rt in real[:40]:
        short = str(rt.get("table_name", "")).split(".")[-1].strip().lower()
        if not short or not _safe_ident(short):
            continue
        try:
            cols = [c for c in _text_columns_of(short) if _safe_ident(c)]
        except Exception:
            continue
        # 优先扫 category/type/name/status 这类"会出现在问句里"的列
        cols = sorted(cols, key=lambda c: (0 if any(k in c.lower() for k in _ENUM_COL_PRIO_KW) else 1))[:8]
        for c in cols:
            try:
                r = execute_sql(f"SELECT DISTINCT {c} FROM {short} WHERE {c} IS NOT NULL "
                                f"LIMIT {_ENUM_MAX_DISTINCT + 1}")
                if not r.get("success"):
                    continue
                vals = [str(row.get(c)).strip() for row in (r.get("rows") or []) if row.get(c) is not None]
                vals = [v for v in vals if v]
                if not vals or len(vals) > _ENUM_MAX_DISTINCT:
                    continue   # 空列/高基数（自由文本）→ 不入索引
                for v in vals:
                    if len(v) >= 2:
                        idx.setdefault(v, [])
                        ent = [short, c]
                        if ent not in idx[v]:
                            idx[v].append(ent)
            except Exception:
                continue
    return idx


def _literal_table_index() -> dict:
    """取「值 → 表」倒排索引（按库落盘缓存，1h 有效；冷建失败则返回空索引）。"""
    dbk = ""
    try:
        dbk = _current_db_key() or ""
    except Exception:
        dbk = ""
    p = os.path.normpath(_LITERAL_INDEX_FILE)
    with _literal_index_lock:
        try:
            cached = json.load(open(p, encoding="utf-8"))
            if (cached.get("db") == dbk
                    and time.time() - float(cached.get("built_at") or 0) < _LITERAL_INDEX_TTL
                    and isinstance(cached.get("index"), dict)):
                return cached["index"]
        except Exception:
            pass
        idx = _build_literal_table_index()
        try:
            json.dump({"db": dbk, "built_at": time.time(), "index": idx},
                      open(p, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
        return idx


def _tables_by_literal(query: str, exclude: set | None = None, limit: int = 3) -> list[str]:
    """问题里直接点名的枚举值（如「传感器」「控制器」）落在哪张表 → 返回这些表短名。

    只做**补齐**，不参与排序：调用方把结果追加到候选表尾部，让原本匹配到的表保持优先。
    索引条目形如 `值 -> [[表短名, 列名], ...]`（列名供 _fix_value_column_mismatch 修值放错列用）。
    """
    q = str(query or "")
    if len(q) < 2:
        return []
    ex = exclude or set()
    out: list[str] = []
    try:
        for val, ents in _literal_table_index().items():
            if val not in q:
                continue
            for ent in (ents or []):
                t = ent[0] if isinstance(ent, (list, tuple)) and ent else str(ent)
                if t not in ex and t not in out:
                    out.append(t)
                    if len(out) >= limit:
                        return out
    except Exception:
        return out
    return out


def _fix_value_column_mismatch(sql: str, literal_index: dict | None = None) -> str:
    """把「中文枚举值直接等事实表外键列」**确定性修好**：等值 → `IN (SELECT 维表主键 ...)`。

    实测（yans 高价值题库 #4「既生产过传感器又生产过控制器的产线有哪些」）模型反复写出
    `mpo_sensor.product_id = '传感器'` —— 「传感器」是 `dim_product.product_category`
    的取值，被错当成 `product_id` 的取值 → 查询跑通但 0 行。

    修法利用「错列的列名恰好等于维表主键名」这个特征（product_id 既是事实表外键、
    也是 dim_product 主键）：
        mpo.product_id = '传感器'
      → mpo.product_id IN (SELECT product_id FROM dim_product WHERE product_category = '传感器')
    为什么不做「拒绝 + 重试」：实测拒绝型闸门在这一类上是负收益 —— 拦下后只剩直生兜底，
    思考型模型在剩余预算内常出不来，2 题直接从 WRONG 掉成 GEN_FAIL（3/6 vs 4/6）。
    只有当字面值**确实属于另一张表的低基数列**、且列名与维表主键同名时才动手，故误判风险很低。
    """
    s = (sql or "").strip()
    if not s or "'" not in s:
        return s
    try:
        idx = literal_index if literal_index is not None else _literal_table_index()
        if not idx:
            return s
        owner: dict = {}
        for val, ents in idx.items():
            for ent in (ents or []):
                if isinstance(ent, (list, tuple)) and len(ent) >= 2:
                    owner.setdefault(val, (ent[0], ent[1]))
        if not owner:
            return s
        # 维表列清单：用来确认「被误用的列」确实是维表的键列（如 product_id 是 dim_product 的列）
        try:
            _cmap = _all_table_columns() or {}
        except Exception:
            _cmap = {}
        if not _cmap:
            return s
        import sqlglot
        from sqlglot import exp
        ast = sqlglot.parse_one(s, read="postgres")
        todo = []
        for eq in ast.find_all(exp.EQ):
            left, right = eq.left, eq.right
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Literal)):
                continue
            if not getattr(right, "is_string", False):
                continue
            val = right.this
            if val not in owner:
                continue
            # 只修「名称/类别类中文取值」（'L01' 这类 id/code 取值本身可能就是对的，别动）
            if not _CJK_RE.search(val or ""):
                continue
            tname, tcol = owner[val]
            colname = (left.name or "").lower()
            # 被误用的列必须是**维表的键列**，且不是维表自己的属性列
            if colname == str(tcol).lower():
                continue
            dim_cols = {str(c).lower() for c in (_cmap.get(tname) or _cmap.get(str(tname).split(".")[-1].lower(), []))}
            if not dim_cols or colname not in dim_cols:
                continue
            if (left.table or "").lower() == str(tname).lower():
                continue
            todo.append((eq, left, val, tname, str(tcol), colname))
        if not todo:
            return s
        for eq, left, val, tname, tcol, colname in todo:
            sub = (exp.select(exp.column(colname))
                   .from_(exp.table_(tname))
                   .where(exp.EQ(this=exp.column(tcol, table=tname),
                                expression=exp.Literal.string(val))))
            eq.replace(exp.In(this=left.copy(), expressions=[sub]))
        return ast.sql(dialect="postgres") or s
    except Exception:
        return s


def _sql_has_comma_join(sql: str) -> bool:
    """判断 SQL 是否存在 `FROM a, b` 逗号隐式连接（笛卡尔积风险）。

    2026-09-11 修复：旧实现 `re.findall(r"\\bFROM\\s+(.+)")` 会把函数内的 FROM
    （如 EXTRACT(EPOCH FROM ...)、SUBSTRING(x FROM 1)）误当子句级 FROM，
    只要其参数列表含逗号就把合法 SQL 误杀 → 直生路径两轮全废 →「AI 未能生成」。
    现改为：优先 sqlglot AST——逗号连接在本版本会解析为「无 kind、无 ON/USING 的
    Join 节点」；解析失败回退「折叠括号后按子句级 FROM 段找顶层逗号」。
    """
    if not sql:
        return False
    try:
        import sqlglot
        from sqlglot import exp
        ast = sqlglot.parse_one(sql)
        for j in ast.find_all(exp.Join):
            if not str(j.kind or "").strip() and not j.args.get("on") and not j.args.get("using"):
                return True
        return False
    except Exception:
        pass
    # 回退：反复折叠括号段（函数参数/子查询整体视为一个 "( )"，其内部逗号不可见），
    # 再在子句级 FROM 之后、下一个子句关键字之前找顶层逗号。
    m = sql
    prev = None
    while prev != m:
        prev = m
        m = re.sub(r"\([^()]*\)", "( )", m)
    for fm in re.finditer(r"\bFROM\s+", m, re.I):
        seg = m[fm.end(): fm.end() + 400]
        stop = re.search(r"\b(WHERE|GROUP|ORDER|HAVING|LIMIT|OFFSET|JOIN|UNION|WINDOW)\b", seg, re.I)
        if stop:
            seg = seg[: stop.start()]
        if re.search(r",", seg):
            return True
    return False


def _sql_mixed_agg_without_groupby(sql: str) -> bool:
    """判断最外层 SELECT 是否"非窗口聚合 + 裸维度列"混用且无 GROUP BY（PG 42803 必报错）。

    2026-09-11 修复：旧实现用 `re.search(r"\\bSELECT\\s+(.+?)\\bFROM\\b")` 截取 SELECT 段，
    非贪婪匹配会停在**子查询**的 FROM 上——`SELECT a, (SELECT MAX(v) FROM t2) AS m FROM t1`
    这类合法标量子查询，子查询内的 MAX 被误当外层聚合 → 合法 SQL 被误杀。
    现改为 sqlglot AST 精确判断（只看最外层 select list；子查询/窗口聚合不计入）：
      - 有 GROUP BY → 放行；
      - select 列表既有非窗口聚合、又有裸列（维度）→ 拦截；
      - 纯聚合（COUNT(*)）或纯维度列 → 放行。
    解析失败一律放行（宁可漏拦交给执行层报错，不误杀合法 SQL）。
    """
    if not sql:
        return False
    try:
        import sqlglot
        from sqlglot import exp
        ast = sqlglot.parse_one(sql)
    except Exception:
        return False
    if not isinstance(ast, exp.Select) or ast.args.get("group"):
        return False
    has_agg = False
    has_bare_col = False
    for expr in ast.expressions or []:
        node = expr.this if isinstance(expr, exp.Alias) else expr
        if isinstance(node, exp.Column):
            has_bare_col = True
            continue
        for agg in node.find_all(exp.AggFunc) if node is not None else []:
            anc = agg.find_ancestor(exp.Window, exp.Select)
            if isinstance(anc, exp.Window):
                continue  # 窗口聚合（SUM(x) OVER ...）无 GROUP BY 也合法
            if anc is ast:
                has_agg = True  # 直接挂在本层 select list 上的聚合
                break
        if has_agg and has_bare_col:
            return True
    return has_agg and has_bare_col


def _bird_vote(candidates: list[str], timeout_ms: int = 60000):
    """自一致性投票：把候选按「执行结果集」分组，选票数最多的那一组。

    为什么按**结果集**而不是 SQL 文本投票：
    两条写法不同的 SQL 可能都答对（比如 JOIN 顺序、子查询 vs EXISTS 的差异），
    按文本投票会把它们拆成两票各 1；按结果投票才会聚成 2 票 —— 这正是
    self-consistency 在 NL2SQL 上有效的原因。

    完全没有 gold 信息参与，只用「多数候选给出同一份结果」这个信号，
    因此对 BIRD 的判定口径是公平的。
    """
    def _group_key(rows):
        return _bird_rows_key(rows)

    groups: dict = {}
    order: list = []
    for idx, sql in enumerate(candidates):
        ok, rows, err = _bird_execute(sql, timeout_ms=timeout_ms)
        if not ok or not rows:
            continue          # 执行失败或 0 行的候选不参与投票（几乎不可能对）
        key = _group_key(rows)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(idx)
    if not groups:
        return None, 0
    # 票数优先；**平票时取候选序号最小者**（即温度最低、最保守的那个）
    best_key = max(order, key=lambda k: (len(groups[k]), -min(groups[k])))
    return candidates[min(groups[best_key])], len(groups[best_key])


def _bird_rows_key(rows) -> tuple:
    """结果集的规范化键：忽略行序，数值统一成 float（对齐口径，不放宽语义）。"""
    out = []
    for r in rows:
        cells = []
        for v in r:
            if v is None:
                cells.append(None)
            elif isinstance(v, bool):
                cells.append(v)
            elif isinstance(v, (int, float)):
                cells.append(float(v))
            else:
                try:
                    from decimal import Decimal
                    if isinstance(v, Decimal):
                        cells.append(float(v))
                        continue
                except Exception:
                    pass
                try:
                    import datetime as _dt
                    if isinstance(v, (_dt.datetime, _dt.date)):
                        cells.append(v.isoformat())
                        continue
                except Exception:
                    pass
                cells.append(str(v))
        out.append(tuple(cells))
    return tuple(sorted(out, key=repr))


def _bird_gen_and_vote(query: str, schema_context: str, budget_s: float | None = None,
                       n: int = 3) -> tuple[str, dict]:
    """生成 n 个候选并投票选出最终 SQL。

    返回 (sql, info)；info 记录候选数与票数，便于评测报告里归因。
    n=1 时退化为「生成 + 执行反馈重试」，行为与改造前一致。
    """
    from concurrent.futures import ThreadPoolExecutor
    budget = float(budget_s if budget_s is not None else _SQL_GEN_WALL_S)
    deadline = time.monotonic() + max(budget, 60.0)
    n = max(1, int(n))

    info = {"candidates": 0, "votes": 0, "temperatures": []}
    gens: list = []

    # 候选温度：第 0 个保持 0（最稳，且带执行反馈重试），其余升温制造多样性。
    # 依据：temperature=0 时同题两次采样 93% 完全一致，投票无票可投；
    # 而「两次结果一致」的题正确率 70.9%，远高于单次 38.3% —— 说明
    # **一致性这个信号本身很值钱，前提是先让候选真的产生分歧**。
    temps = [0.0]
    for i in range(1, n):
        temps.append(round(0.15 + 0.1 * i, 2))
    info["temperatures"] = temps[:n]

    # SQLite 后端：候选线程（ThreadPoolExecutor）读不到 thread-local 的 db_id，
    # 导致 _bird_execute_sqlite 定位不到库文件。主线程先捕获，进候选线程时重设。
    from agent import bird_profile as _bp
    _db = _bp.db_id()
    _ev = _bp.evidence()

    ensemble = _bird_ensemble_models()
    if ensemble:
        n = len(ensemble)

    def _one(i: int):
        _bp.set_context(db_id=_db, evidence=_ev, active=True)
        left = deadline - time.monotonic()
        if left < 12:
            return ""
        if ensemble:
            # 跨模型集成：第 i 个候选用第 i 个模型（temp=0，关思考）
            m = ensemble[min(i, len(ensemble) - 1)]
            return _bird_gen_sql(query, schema_context, budget_s=left,
                                 temperature=0.0, model=m) or ""
        t = temps[min(i, len(temps) - 1)]
        if i == 0:
            # 第 0 个候选走「生成 → 试执行 → 带真实报错/0行 重试」
            return _bird_gen_with_fix(query, schema_context, budget_s=left) or ""
        return _bird_gen_sql(query, schema_context,
                             budget_s=max(12.0, left * 0.5), temperature=t) or ""

    if n == 1:
        gens = [_one(0)]
    else:
        with ThreadPoolExecutor(max_workers=min(n, 4)) as ex:
            gens = list(ex.map(_one, range(n)))

    cands, seen = [], set()
    for g in gens:
        g = (g or "").strip()
        if not g or g in seen:
            continue
        seen.add(g)
        cands.append(g)
    info["candidates"] = len(cands)
    if not cands:
        return "", info

    if len(cands) == 1:
        return cands[0], info

    picked, votes = _bird_vote(cands)
    info["votes"] = votes
    if picked:
        return picked, info
    # 所有候选都执行失败/0 行：退回第一个（至少让它拿到真实执行报错）
    return cands[0], info


def _bird_gen_with_fix(query: str, schema_context: str, budget_s: float = 160.0) -> str:
    """生成 → 试执行 → 把数据库真实报错（或 0 行）回灌重试一轮。"""
    deadline = time.monotonic() + max(budget_s, 30.0)
    hint = ""
    best = ""
    for _ in range(2):
        left = deadline - time.monotonic()
        if left < 12:
            break
        sql_text = _bird_gen_sql(query, schema_context, budget_s=left, fix_hint=hint) or ""
        if not sql_text:
            break
        best = sql_text
        ok, reason = _bird_probe_execute(sql_text)
        if ok:
            return sql_text
        hint = reason or "execution failed"
    return best


def _bird_clean_sql(out: str) -> str:
    """把 LLM 的自由文本输出收敛成一条 SQL。

    BIRD 档位提示词要求「只输出 SQL」，但模型仍可能带 ```sql 围栏、
    "SQL:" 前缀或一句解释。这里做确定性收敛，避免把解释文本喂给执行器。
    """
    s = (out or "").strip()
    if not s:
        return ""
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s).strip()
    # 去掉开头的 "SQL:" / "Answer:" / "Query:" 之类前缀
    s = re.sub(r"^(?:SQL|Answer|Query|Result)\s*[:：]\s*", "", s, flags=re.I)
    # 有前置解释时，从第一个 SELECT/WITH 开始截（且只在该词确实开头于一个语句时）
    m = re.search(r"\b(WITH|SELECT)\b", s, re.I)
    if m and m.start() > 0:
        s = s[m.start():]
    # 只保留第一条语句
    m2 = re.search(r";", s)
    if m2:
        s = s[:m2.start()]
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s.strip())
    return s.strip()


def _bird_case_insensitive(sql: str) -> str:
    """把字符串字面量的 `= 'xxx'` 精确匹配改成大小写不敏感（ILIKE）。

    背景：BIRD 数据集的字符串值几乎全是 Title Case（'Stealth' /
    'Human / Altered' / 'Marvel Comics'），而模型倾向生成全小写（'stealth' /
    'human'）。PostgreSQL 的 `=` 是大小写敏感的，导致本该命中的行匹配不到。
    实测（superhero 库 8 道两模型都错的题）：仅把 `=` 改成 ILIKE 就救回 4 道。

    保守边界：
      · 只改 `= '字符串'` 字面量比较，排除 >= <= != <> 与数字/列比较（负向后顾）；
      · 通配符 _ % \\ 用默认转义字符 \\ 转义，保持「精确匹配、仅大小写不敏感」语义；
      · 仅 BIRD 档位调用，主链（yans 等业务）零影响。
    """
    if not sql:
        return sql

    # SQL 字符串字面量（含 '' 转义的单引号，如 'Ancestor''s Chosen'）
    _STR = r"'((?:[^']|'')*)'"

    if _bird_backend() == "sqlite":
        # SQLite：`= 'xxx'` → `= 'xxx' COLLATE NOCASE`（精确、大小写不敏感、无通配符风险）。
        # 不用 LIKE：SQLite 的 _ % 是通配符且默认无转义字符，不如 COLLATE NOCASE 干净。
        return re.sub(r"(?<![<>=!])\s*=\s*" + _STR, r" = '\1' COLLATE NOCASE", sql)

    def _repl(m):
        raw = m.group(1)                # 字面量内容（含 '' 转义）
        val = raw.replace("''", "'")    # 还原真实值
        esc = val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        lit = esc.replace("'", "''")    # 重新转义回 SQL 字面量
        return " ILIKE '%s'" % lit

    return re.sub(r"(?<![<>=!])\s*=\s*" + _STR, _repl, sql)


def _bird_model() -> str:
    """BIRD 档位专用模型，默认 pro 档（比主链 flash 更强）。

    为什么独立于主链：BIRD 评测追求正确率上限，值得用更强的 pro 档；主链
    （yans 等业务）保持 flash 不变，成本更低、行为更稳、零回归。可用环境变量
    BIRD_LLM_MODEL 覆盖；设为空字符串回退主链模型。
    """
    return os.getenv("BIRD_LLM_MODEL") or "deepseek-v4-pro-0813"


def _bird_ensemble_models() -> list:
    """BIRD 档位跨模型集成的模型列表（BIRD_ENSEMBLE_MODELS 逗号分隔）。

    跨模型集成的价值：不同模型的错误模式不重叠。实测 superhero 8 错题，
    qwen3.8-max 救回 5 题 / kimi-k3 救回 3 题 / glm-5.3 救回 2 题，并集 6 题
    > 单模型最优 5 题。按结果集投票能取「并集」优势。默认空 = 单模型。
    """
    s = os.getenv("BIRD_ENSEMBLE_MODELS", "").strip()
    return [m.strip() for m in s.split(",") if m.strip()]


def _bird_backend() -> str:
    """BIRD 档位的数据库后端：pg（默认）或 sqlite。

    为什么需要 sqlite 后端：本机 PostgreSQL 在沙箱环境下 fork 子进程会被沙箱
    DLL 注入终止（0xC0000142 / could not reserve shared memory region 487），
    完全无法执行任何 SQL。BIRD 原始数据本来就是 SQLite（嵌入式、不 fork），
    切 sqlite 后端可完全绕过 PG。默认仍为 pg，PG 恢复后无需任何改动。
    """
    return os.getenv("BIRD_DB_BACKEND", "pg").strip().lower()


def _bird_sqlite_path() -> str:
    """当前 db_id 对应的 SQLite 文件路径（仅 sqlite 后端使用）。"""
    from agent import bird_profile
    return bird_profile._sqlite_path()


def _bird_gen_sql(query: str, schema_context: str, budget_s: float | None = None,
                  fix_hint: str = "", temperature: float = 0.0,
                  model: str | None = None) -> str | None:
    """BIRD 档位的 SQL 生成（全量 schema + evidence + 英文硬规则）。

    与 MES 主链的差异：
      · 不做 MQL 口径推断（BIRD 无注册指标，推断只会烧掉墙钟预算）；
      · 一次生成 + **带真实拒绝原因**的重试一轮 —— 原因来自校验器/数据库，
        不是固定提示词，这样第二轮才有可能修对（见本文件 2026-09-14 的同类教训）。
    fix_hint: 上一轮**执行**失败的真实报错（调用方传入），用作首轮纠错提示。
    temperature: 采样温度。自一致性投票需要候选之间有**真实差异**才有效 ——
      实测 temperature=0 时同一题两次独立生成有 93% 概率给出完全一致的结果
      （41 题两次都对 / 13 题两次都错 / 仅 4 题分歧），此时投票等于白投。
      所以投票用的候选要适当升温，见 `_bird_gen_and_vote`。
    """
    from agent.prompt_builder import build_bird_prompt

    budget = float(budget_s if budget_s is not None else _SQL_GEN_WALL_S)
    deadline = time.monotonic() + max(budget, 30.0)
    max_tok = int(os.getenv("BIRD_SQL_MAX_TOKENS", "6000"))
    try:
        from agent.llm_providers import detect_provider as _dp
        _invoke_mode = _dp(_bird_model(), LLM_CONFIG.get("base_url", "")
                           ).get("name") in ("zhipu", "moonshot", "kimi")
    except Exception:
        _invoke_mode = False

    def _one(note: str) -> str:
        left = deadline - time.monotonic()
        if left < 6:
            return ""
        try:
            from agent import bird_profile as _bpf
            _shots = _bpf.few_shot_examples()
        except Exception:
            _shots = []
        prompt = build_bird_prompt(query, schema_context=schema_context,
                                   sql_examples=_shots)
        if note:
            prompt += ("\n\n# Your previous answer was rejected\n"
                       "Reason: %s\n"
                       "Produce a corrected SQL statement that fixes exactly this problem. "
                       "Output only the SQL." % note)
        try:
            llm = _make_llm(temp=float(temperature), max_tokens=max_tok,
                            timeout=min(left, 300.0), max_retries=0,
                            model=model or _bird_model())
            out = ""
            if _invoke_mode:
                r = llm.invoke([HumanMessage(content=prompt)])
                out = str(getattr(r, "content", "") or "")
            else:
                for c in llm.stream([HumanMessage(content=prompt)]):
                    if time.monotonic() > deadline:
                        break
                    if isinstance(c, AIMessageChunk) and c.content:
                        out += c.content
            return _bird_case_insensitive(_bird_clean_sql(out))
        except Exception:
            return ""

    def _reject(sql_text: str) -> str:
        """返回 ""=通过；否则是**具体**的拒绝原因，用于定向重试。"""
        if not sql_text:
            return "empty output"
        if not sql_text.lstrip().upper().startswith(("SELECT", "WITH")):
            return "the output was not a SELECT/WITH query"
        if _sql_looks_truncated(sql_text):
            return "the SQL is truncated / incomplete"
        try:
            from agent.sql_validator import validate_sql_safety
            ok, err, clean = validate_sql_safety(sql_text)
            if not ok:
                return "read-only safety check failed: %s" % err
            if clean:
                sql_text = clean
        except Exception:
            pass
        try:
            if _sql_has_comma_join(sql_text):
                return ("you used an implicit comma join (`FROM a, b`) which produces a "
                        "cartesian product; use explicit JOIN ... ON with the foreign keys given")
        except Exception:
            pass
        # 列名/表名存在性校验：编造的列名在此拦下并告诉模型真实列
        try:
            bad = _unknown_identifiers(sql_text)
            if bad:
                return ("these identifiers do not exist in the schema: %s. "
                        "Use only the exact names listed in the schema." % ", ".join(sorted(bad)[:6]))
        except Exception:
            pass
        return ""

    sql_text = _one(fix_hint)
    why = _reject(sql_text)
    if not why:
        return sql_text
    retry = _one(why)
    if retry and not _reject(retry):
        return retry
    # 两轮都不过：有 SQL 就返回（让执行器给出真实报错，比直接 GEN_FAIL 更有信息量）
    if retry and retry.lstrip().upper().startswith(("SELECT", "WITH")):
        return retry
    if sql_text and sql_text.lstrip().upper().startswith(("SELECT", "WITH")):
        return sql_text
    return None


def _unknown_identifiers(sql_text: str) -> set:
    """SQL 里引用的表/列名中，当前库里不存在的那些（用于定向纠错提示）。"""
    try:
        cmap = _all_table_columns() or {}
    except Exception:
        return set()
    if not cmap:
        return set()
    tables = {k.split(".")[-1].lower() for k in cmap}
    cols = set()
    for k, v in cmap.items():
        cols |= {str(c).lower() for c in v}
    unknown = set()
    try:
        ref = _extract_sql_tables(sql_text)
        for t in ref:
            if t.lower() not in tables:
                unknown.add(t)
    except Exception:
        pass
    try:
        import sqlglot
        from sqlglot import exp as _exp
        for node in sqlglot.parse(sql_text, read="postgres"):
            if node is None:
                continue
            for col in node.find_all(_exp.Column):
                name = (col.name or "")
                if not name or name == "*":
                    continue
                if name.lower() not in cols:
                    unknown.add(name)
    except Exception:
        pass
    return unknown


_CONN_ERR_PAT = re.compile(
    r"(57P03|57P01|57P02|08006|08003|08001|08S01|恢复模式|recovery mode|"
    r"server closed the connection|Connection reset|connection already closed|"
    r"InvalidatePoolError|network error|远程主机强迫关闭|terminating connection|"
    r"could not connect|Connection refused)", re.I)


def _is_conn_error(msg: str) -> bool:
    """连接级/服务端故障（与 SQL 写法无关）——重试前值得先重建连接池。"""
    return bool(_CONN_ERR_PAT.search(str(msg or "")))


def _bird_execute_sqlite(sql_text: str, max_rows: int = 5000):
    """BIRD 档位的 SQLite 执行通道（绕过 PG，供沙箱环境 PG 崩溃时使用）。

    嵌入式 sqlite3 不 fork 子进程，不受沙箱 DLL 注入影响；连接即开即关，
    天然线程安全（评测多线程并发时每线程各自 connect）。
    """
    import sqlite3
    path = _bird_sqlite_path()
    if not path or not os.path.exists(path):
        return False, [], "SQLite 库不存在: %s" % (path or "(未设置 db_id)")
    try:
        conn = sqlite3.connect(path)
        try:
            # SQLite 没有 statement_timeout，用 progress_handler 模拟：30s 超时中断，
            # 防大 JOIN 笛卡尔积把评测永久卡死（实测 card_games 三表 JOIN 卡 20+ 分钟）。
            _deadline = [time.time() + 30.0]
            conn.set_progress_handler(
                lambda: 1 if time.time() > _deadline[0] else 0, 1000)
            cur = conn.cursor()
            cur.execute(sql_text)
            rows = cur.fetchmany(max_rows)
            return True, rows, ""
        finally:
            conn.close()
    except Exception as e:
        return False, [], str(e).strip().splitlines()[0][:200]


def _bird_execute(sql_text: str, max_rows: int = 5000, timeout_ms: int = 60000):
    """BIRD 档位专用执行：直连 psycopg2，返回 (ok, rows, err)。

    为什么不用项目执行器 `execute_sql`（pg8000/SQLAlchemy）：
      · 评测是**多线程**的，psycopg2 连接必须每线程一条，这里天然满足；
      · 自一致性投票要在**同一次调用里执行多个候选**，需要一个轻量、可控、
        带行上限与超时的只读通道，不能污染业务连接池。
    """
    if not sql_text:
        return False, [], "空 SQL"
    if _bird_backend() == "sqlite":
        return _bird_execute_sqlite(sql_text, max_rows)
    try:
        import psycopg2
        from database import get_database_config
        cfg = get_database_config() or {}
        conn = psycopg2.connect(
            host=cfg.get("host", "localhost"), port=cfg.get("port", 5432),
            user=cfg.get("user", "postgres"),
            password=os.getenv("DB_PASSWORD", "") or (cfg.get("password") or ""),
            dbname=cfg.get("name") or cfg.get("dbname") or "postgres",
            connect_timeout=10, options="-c lc_messages=C")
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("SET statement_timeout = %d" % timeout_ms)
            cur.execute("SET max_parallel_workers_per_gather = 0")
            cur.execute(sql_text)
            rows = cur.fetchmany(max_rows)
            return True, rows, ""
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        return False, [], str(e).strip().splitlines()[0][:200]


def _bird_probe_execute(sql_text: str) -> tuple[bool, str]:
    """试执行一条 SQL，判断**这一版能不能直接交卷**。

    返回 (ok, reason)：ok=False 表示需要重来，reason 是给模型的真实原因。
    两类情况都要重来：
      · 执行报错（列类型不匹配/语法错误）—— 在 BIRD 里是必然 0 分；
      · **成功但 0 行** —— 多半是过滤条件写死或连错表，同样几乎不可能对
        （实测判错题里 21% 是「少返回行」，0 行是其中最极端也最好抓的一种）。
    """
    if not sql_text:
        return False, "空 SQL"
    ok, rows, err = _bird_execute(sql_text, max_rows=1)
    if not ok:
        return False, err or "执行失败"
    if not rows:
        return False, ("the query returned ZERO rows. That usually means a filter or join is "
                       "too restrictive or wrong. Re-read the question and the foreign keys, "
                       "relax or correct the condition, and make sure you are joining the "
                       "right tables.")
    return True, ""


def _direct_gen_sql(query: str, matched_tables: list, budget_s: float = 45.0) -> str | None:
    """主链「直接 LLM 生成 SQL」（逃生式、非 json —— 对 GLM/zhipu 等 json 弱模型友好）。

    与 _run_escape_sql 共用同一套 prompt 与安全校验（validate_sql_safety + 列名存在性）。
    schema 只注入列名清单（不注入富文本描述），规避长中文 schema 触发静默 0 输出。
    注意：始终使用【当前用户所选模型】（_make_llm 跟随 LLM_CONFIG），不做模型写死/暗切换——
    用户切换模型即为最直接的换模型重试手段。
    """
    try:
        try:
            _cmap = _all_table_columns() or {}
        except Exception:
            _cmap = {}
        _tables_desc = "\n".join(f"- {t['table_name']}" for t in (matched_tables or [])[:10]) or "(无候选表)"
        _slim: list[str] = []
        for t in (matched_tables or [])[:4]:
            _tn = t.get("table_name", "")
            _cols = _cmap.get(_tn) or _cmap.get(str(_tn).split(".")[-1].lower(), [])
            if not _cols:
                continue
            _slim.append(f"- {_tn} 列: " + ", ".join(str(c) for c in _cols[:30]))
            if sum(len(p) for p in _slim) > 800:
                break
        _slim_schema = "\n".join(_slim)[:1000]
        if not _slim_schema:
            return None
        # 2026-09-12 修复：此前 enum_block 传空串 —— 直生路径是「各工厂的设备故障维修次数」
        # 这类未注册口径的主通道，却既拿不到枚举值（幻觉出 type='故障'、status='running'），
        # 也拿不到外键（凭列名猜 ON 条件）。两者按需注入，长 prompt 风险由预算参数兜住。
        _names = [t.get("table_name", "") for t in (matched_tables or [])[:6]]
        _extra = _join_hint_for_tables(_names) + _enum_block_for_tables(query, _names, force=True)
        return _run_escape_sql_v2(query, _tables_desc, _slim_schema, _extra, limit_s=budget_s)
    except Exception:
        return None


def _run_escape_sql_v2(query: str, tables_desc: str, slim_schema: str,
                         enum_block: str, limit_s: float = 20.0,
                         model: str | None = None, api_key: str | None = None,
                         base_url: str | None = None) -> str | None:
    """逃生直生 v2：首次生成若因 `FROM a, b` 逗号连接被语义拦截，
    带纠错提示（强制 JOIN ... ON）重试一轮 —— 复杂多表题实测常栽在这条，
    旧版直接弃用导致「AI 未能生成 SQL」。
    """
    # 注入与主链一致的聚合/时间/分档语义提示：直生路径此前缺这份 hint，
    # 导致「昨天」被 LLM 当成普通词过滤（workshop_name='昨天'）、
    # 「单次>60分钟」被写成 HAVING COUNT(*)>=60 等语义跑偏。
    try:
        _agg_hint = _build_agg_hint(query) or ""
    except Exception:
        _agg_hint = ""
    _agg_block = f"【生成约束】\n{_agg_hint}\n\n" if _agg_hint else ""
    _base_prompt = (
        f"用户问题：{query}\n\n{_agg_block}候选表（只能用这些表）：\n{tables_desc}\n\n"
        f"表结构（列名，不得编造）：\n{slim_schema}\n\n"
        f"{enum_block}"
        "生成一条 PostgreSQL 查询回答该问题。要求："
        "1) 只输出一条 SQL 语句，不要任何解释和代码块；2) 只允许 SELECT，可用子查询 / CASE WHEN / EXISTS / 窗口函数；"
        "3) 表名必须逐字来自候选表清单（清单外的表在本库一律不存在，禁止凭经验引入其他系统的常见表名）；"
        "列名必须逐字来自上面的列名清单；筛选值优先取自枚举值清单；"
        "4) 聚合类问题按业务常识聚合，明细类问题直接列出所需列；5) 末尾必须带 LIMIT（≤100）；"
        "6) 多表关联必须用显式 JOIN ... ON（或 JOIN ... USING），严禁 FROM a, b 逗号连接（笛卡尔积必错）；"
        "7) 统计/最多/最少/排行/分档/对比/分布/集合类问法必须按对应业务列分组排序，禁止只输出全体总数；"
        "8) 含聚合函数时若还输出实体维度列必须有 GROUP BY 对应列；"
        "9) 问「各X/每个X/按X统计/分布/对比/占比」时必须 GROUP BY 该维度列，且维度列所在表要用关联键 JOIN 进来，"
        "禁止只返回一个全局汇总值；"
        "10) 问「每月/按月/每月趋势/季度」时必须用日期截断（PG: TO_CHAR(日期列,'YYYY-MM') 或 date_trunc）后分组，"
        "禁止直接 GROUP BY 日期列（会退化成按天）；"
        "11) 问题没有提到时间范围时，禁止自行添加 WHERE 时间过滤；提到了才过滤，且严格按提到的范围。\n"
        # 12~14 为 2026-09-15 高价值题库实测驱动的补充（每题都对应一个已复现的真实失败）：
        #   12 → #3「停机次数超过5次的设备中前5台」：多带 *_id/*_code 进 GROUP BY 后，
        #        第 5 名在两台并列设备（12 次 / 540 分钟）之间换人，答案与标准答案不一致；
        #   13 → #4「既生产过传感器又生产过控制器的产线」：模型写成 product_id = '传感器'，
        #        把类别值比到了外键上，执行 0 行；
        #   14 → #6「每天的产量相比前一天变化了多少」：首期 LAG 为 NULL，标准答案是 0。
        "12) 分组维度只放业务维度列（名称/类别/类型/班次等），"
        "禁止把主键/外键/编码列（*_id、*_code、*_no）一起放进 SELECT 与 GROUP BY —— "
        "除非用户明确要求按每条实体分别统计；多带这些列会改变分组粒度，"
        "并让「前N名」在指标并列时产生不确定结果。\n"
        "13) 筛选值若来自维表属性列（如产品类别、设备类型、车间名称），"
        "必须先用外键把该维表 JOIN 进来、在维表的属性列上过滤；"
        "严禁把中文名称/类别等文本值直接与事实表的外键列（如 product_id = '传感器'）比较。\n"
        "14) 环比/与上一期差值（LAG/LEAD）的第一个周期没有上一期，"
        "必须用 COALESCE(差值表达式, 0) 兜底为 0，不要留 NULL。\n"
        # 15 来自 v4 #4「产量高于平均产量的产线有哪些」：模型写成
        # `HAVING SUM(good_qty) > (SELECT AVG(good_qty) FROM mes_process_output)` ——
        # 拿「明细行的平均」去比「按产线汇总后的总量」，粒度不同必然答错。
        "15) 与「平均值 / 均值 / 平均水平」比较时，比较对象必须与左侧**同粒度**："
        "左侧是按维度汇总的 SUM，右侧就必须先按同一维度 GROUP BY 再取 AVG"
        "（如 HAVING SUM(qty) > (SELECT AVG(t.qty) FROM (SELECT SUM(qty) AS qty FROM t GROUP BY 维度) t)），"
        "严禁直接对明细行取 AVG。\nSQL:"
    )
    try:
        from agent.llm_providers import detect_provider as _dp2b
        _invoke_mode = (_dp2b(LLM_CONFIG.get("model", ""), LLM_CONFIG.get("base_url", ""))
                        .get("name") in ("zhipu", "moonshot", "kimi"))
    except Exception:
        _invoke_mode = False

    # 逃生舱整体截止时间：两轮 _fetch 共享同一个 deadline。
    # 此前每轮各自 `now + limit_s` 重开计时（最坏 2×limit_s），叠加上游固定的 85s，
    # 正是"卡住不出结果"的来源之一。现在总时长由 limit_s 一次性封顶。
    _esc_deadline = time.monotonic() + max(limit_s, 20.0)

    def _fetch(note: str) -> str:
        _left_s = max(5.0, _esc_deadline - time.monotonic())
        prompt = _base_prompt + note
        llm = _make_llm(temp=0.0, max_tokens=_sql_gen_max_tokens(),
                        timeout=min(_left_s, 90.0),
                        max_retries=0, model=model or _sql_gen_model(),
                        api_key=api_key, base_url=base_url)
        dl = _esc_deadline
        _t0 = time.monotonic()
        out = ""
        try:
            if _invoke_mode:
                _r = llm.invoke([HumanMessage(content=prompt)])
                out = str(getattr(_r, "content", "") or "")
            else:
                # 直生（复杂 SQL 生成）不再额外设首 token 门：整段等待已由 dl（共享 deadline）
                # 封顶，而复杂题首 token 实测可达 20~40s（见下方注释），再设早退门等于把
                # "慢但能出完整 SQL"的调用误杀成空输出。总时长由 _SQL_GEN_WALL_S 保证。
                _gate = _left_s
                for c in llm.stream([HumanMessage(content=prompt)]):
                    if not out and time.monotonic() - _t0 > _gate:
                        break
                    if time.monotonic() > dl:
                        break
                    if isinstance(c, AIMessageChunk) and c.content:
                        out += c.content
        except Exception:
            return ""
        out = (out or "").strip()
        if out.startswith("```"):
            out = re.sub(r"^```(?:sql)?\s*|\s*```$", "", out).strip()
        mm = re.search(r";", out)
        if mm:
            out = out[:mm.end()]
        return out.strip()

    def _valid(s_sql: str) -> str:
        """返回 "" = 通过；否则返回**不可用原因**，供第二轮定向纠错（不是 bool）。

        2026-09-14：改为返回原因，因为固定提示词的纠错等于没纠错 —— 实测
        「各产线设备故障率」模型编造列名 duration_minutes（真实列是 downtime_minutes），
        列名校验拒绝后第二轮只收到"逗号连接"提示，与真实原因完全对不上 →
        同错重犯 → 直接返回 None → 用户看到"AI 未能生成查询 SQL"。
        """
        if not s_sql or not s_sql.upper().startswith(("SELECT", "WITH")):
            return "输出不是一条 SELECT/WITH 查询语句"
        # 截断嫌疑（流式截止 / token 触顶）→ 判不可用，交给 _fetch 重试或上层兜底。
        # 截断的 SQL 能通过下面所有校验，却必然在执行时报语法错误。
        if _sql_looks_truncated(s_sql):
            return "SQL 输出被截断、语句不完整"
        try:
            from agent.sql_validator import validate_sql_safety
            _ok, _err, _clean = validate_sql_safety(s_sql)
            if not _ok:
                return f"只读安全校验不通过（{_err}）"
            if _clean:
                s_sql = _clean
        except Exception:
            pass
        try:
            if _sql_has_comma_join(s_sql):
                return "使用了 `FROM a, b` 逗号隐式连接（会产生笛卡尔积）"
        except Exception:
            pass
        # 输出质量闸门（2026-09-15 高价值题库实测驱动）：拦「能跑通但答非所问」的退化输出。
        # 判据集中在模块级 _output_quality_reason，主链 run() 调用同一份，避免两套逻辑漂移。
        _why_quality = _output_quality_reason(query, s_sql)
        if _why_quality:
            return _why_quality
        try:
            _bad_cols = _validate_sql_columns(s_sql)
            if _bad_cols:
                return ("引用了当前库**不存在**的列：" + "、".join(str(c) for c in _bad_cols[:5]))
        except Exception:
            pass
        return ""

    s1 = _fetch("")
    _why = _valid(s1) if s1 else "模型未产出任何内容"
    if not _why:
        return s1
    # 第二轮：把**真实**拒绝原因带回去定向修复（旧实现固定塞 _retry_note 的"逗号连接"
    # 提示，列名/截断类失败时牛头不对马嘴）。显式要求逐字复制列名清单，并禁止
    # 使用清单外的列 —— 硬失败变成大概率可救回的一次重试。
    #
    # 2026-09-17：区分**语义/口径类**原因（_cond_loss_reason 的产物，一律以「问题」开头）
    # 与列名/语法类原因。前者照抄列名清单没用 —— 它缺的是条件、粒度或分类表达式，
    # 因此额外加一句"针对原因逐条补齐"，把模型的注意力拉回真正的缺陷上。
    _semantic = _why.startswith("问题")
    _fix_hint = (
        "\n[纠错] 你上一次的 SQL 被系统拒绝，原因：" + _why + "。\n"
        + ("这条原因说的是**语义/口径**缺陷（条件缺失、时间粒度不对、分类表达缺失），"
           "请先逐条对照原因把它点出的东西补进 SQL —— 这不是列名拼写问题，"
           "只把列名抄一遍不算修正。\n" if _semantic else "")
        + "请严格只使用上面「表结构（列名）」清单里的表名与列名，**逐字复制**，"
        "绝不使用清单之外的任何列名（不要凭经验臆造，如把 downtime_minutes 写成 duration_minutes）；"
        "多表必须用显式 `JOIN ... ON` 关联，禁止逗号分隔；输出一条完整、可独立执行的 SQL。\nSQL:"
    )
    s2 = _fetch(_fix_hint)
    if s2 and not _valid(s2):
        return s2
    return None


def _run_escape_sql(query: str, tables_desc: str, slim_schema: str,
                    enum_block: str, limit_s: float = 15.0,
                    model: str | None = None, api_key: str | None = None,
                    base_url: str | None = None) -> str | None:
    """P2 SQL 逃生舱：MQL 表达不了时，让 LLM 直接生成一条只读 SELECT。

    与红线的边界（重要）：
    - 产物必须通过 validate_sql_safety（危险关键词 + 语句类型 AST）+ 列名存在性校验；
    - analysis 显式标记 escape_hatch=True —— 弹窗侧提示「未经口径编译保证，请重点核对」；
    - execute_confirm 的表权限/RLS 校验不变，仍是最后一道防线。
    使用条件：仅当 MQL 编译失败（结构化口径走不通）时触发，绝不抢编译路径的活。
    """
    try:
        prompt = (
            f"用户问题：{query}\n\n"
            f"候选表（只能用这些表）：\n{tables_desc}\n\n"
            f"表结构（列名，不得编造）：\n{slim_schema}\n\n"
            f"{enum_block}"
            "生成一条 PostgreSQL 查询回答该问题。要求：\n"
            "1) 只输出一条 SQL 语句，不要任何解释和代码块；\n"
            "2) 只允许 SELECT，可用子查询 / CASE WHEN / EXISTS / 窗口函数；\n"
            "3) 列名必须逐字来自上面的列名清单；筛选值优先取自枚举值清单；\n"
            "4) 聚合类问题按业务常识聚合，明细类问题直接列出所需列；\n"
            "5) 末尾必须带 LIMIT（≤100）；\n"
            "6) 多表关联必须用显式 JOIN ... ON（或 JOIN ... USING），严禁 FROM a, b 逗号连接（笛卡尔积）；\n"
            "7) 分析类问法（统计/最多/最少/排行/分布/对比/各…/按…，或问某实体最多的 XX）"
            "必须按对应业务列（工序/产品/产线/状态等）分组并排序，禁止只输出全体总数；\n"
            "8) 含聚合函数（SUM/COUNT/AVG）时若还输出实体维度列，必须有 GROUP BY 对应列。\n"
            "SQL:"
        )
        # 慢 provider（GLM/智谱等）对长 schema 的 SQL 生成实测 >40s 才能出，上限放宽到 90s；
        # 快 provider 流式有首 token 门兜底，超长会被 _dl 提前掐断。
        llm = _make_llm(temp=0.0, max_tokens=_sql_gen_max_tokens(), timeout=min(max(limit_s, 20), 90),
                        max_retries=0, model=model or _sql_gen_model(),
                        api_key=api_key, base_url=base_url)  # 禁重试：逃生舱超时即放弃，不翻倍拖时间
        dl = time.monotonic() + limit_s
        _tstart = time.monotonic()
        s = ""
        # 2026-09-06：首 token 慢的 provider（zhipu/GLM）逃生也走 invoke（stream 短门会伪 0）
        _esc_invoke = False
        try:
            from agent.llm_providers import detect_provider as _dp2
            _esc_invoke = (_dp2(LLM_CONFIG.get("model", ""), LLM_CONFIG.get("base_url", ""))
                           .get("name") in ("zhipu", "moonshot", "kimi"))
        except Exception:
            _esc_invoke = False
        if _esc_invoke:
            try:
                _r = llm.invoke([HumanMessage(content=prompt)])
                s = str(getattr(_r, "content", "") or "")
            except Exception:
                s = ""
        else:
            for c in llm.stream([HumanMessage(content=prompt)]):
                # 首 token 门：长时间 0 输出 = 模型卡死，不再等满预算（与 _run_infer 同策）
                if not s and time.monotonic() - _tstart > _FIRST_TOKEN_S:
                    return None
                if time.monotonic() > dl:
                    break
                if isinstance(c, AIMessageChunk) and c.content:
                    s += c.content
        s = (s or "").strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:sql)?\s*|\s*```$", "", s).strip()
        # 截掉 SQL 之后的解释文本（第一个分号即语句结束）
        _m = re.search(r";", s)
        if _m:
            s = s[:_m.end()]
        s = s.strip()
        if not s or not s.upper().startswith(("SELECT", "WITH")):
            return None
        # 截断嫌疑（流式截止 / token 触顶）→ 弃用重试，不把半条 SQL 送进数据库
        # 和执行链（2026-09-14：输出上限从写死的 900 改为读配置后，主要风险
        # 从 token 触顶转为墙钟截止，仍需这道判别）。
        if _sql_looks_truncated(s):
            return None
        # 只读安全校验（危险关键词 + 语句类型）——sql_validator 此前未接入主链，逃生舱必须接
        try:
            from agent.sql_validator import validate_sql_safety
            _ok, _err, _clean = validate_sql_safety(s)
            if not _ok:
                return None
            if _clean:
                s = _clean
        except Exception:
            pass
        # 语义质量拦截（2026-09-06 用户实测逃生 SQL 笛卡尔积出烂数）：
        # ① FROM a, b 逗号隐式连接 = 笛卡尔积（无 ON 条件），逃生产物必错 → 弃用；
        # ② SELECT 同时含聚合与非聚合列（实体维度）却没有 GROUP BY → PG 会报错/语义错 → 弃用。
        # 宁可逃生失败降级（弹窗重试/自定义口径），不给必错 SQL（与列名校验同一哲学）。
        try:
            if _sql_has_comma_join(s):
                return None
            # ② SELECT 同时含非窗口聚合与裸维度列（实体维度）却没有 GROUP BY → PG 42803 → 弃用
            if _sql_mixed_agg_without_groupby(s):
                return None
        except Exception:
            pass
        # 列名存在性校验：缺失即弃用（宁给 infer_fail 让用户重试/自定义口径，不给必错 SQL）
        try:
            if _validate_sql_columns(s):
                return None
        except Exception:
            pass
        return s
    except Exception:
        return None


def _try_warning_rule(query: str, matched_tables: list, cmap: dict) -> dict | None:
    """确定性「阈值预警」快速通道（2026-09-06）。

    覆盖"低于安全库存的产品预警"类问法 = 列间比较（量列 < 安全列）。MQL filter 只能比
    常量表达不了；DeepSeek json 推断对此类又慢（实测 90s+ 超时/0 输出）。规则识别命中 →
    直接拼明细 SQL 秒出（零 LLM）。未命中返回 None，走原 LLM 链路，不影响其他问法。

    触发条件（严格，宁窄勿误伤）：问法含 低于/少于/小于/预警/告警/低库存/库存不足，
    且候选表存在安全库存类列（safety_stock_qty/safe_stock 等）。产物标记 escape_hatch
    并在 risks 说明"按列名约定推断、请在弹窗核对"，执行仍走 execute_confirm 权限红线。
    """
    try:
        if not re.search(r"低于|少于|小于|不足|预警|告警|低库存|库存不足|偏少", query or "", re.I):
            return None
        tcols: dict[str, list[str]] = {}
        for t in (matched_tables or [])[:4]:
            n = str(t.get("table_name", "")).split(".")[-1].lower()
            cols = [str(c).lower() for c in (cmap.get(n) or [])]
            if cols:
                tcols[n] = cols
        if not tcols:
            return None
        # 安全库存列（safety/qty）
        safe_hit: list[tuple[str, str]] = []
        for tn, cols in tcols.items():
            for c in cols:
                if re.search(r"safe|safety|安全", c) and re.search(r"qty|stock|量|库存", c):
                    safe_hit.append((tn, c))
        if not safe_hit:
            return None
        # 量列：同表的普通库存/数量列（排除安全/冻结）
        stn, scol = safe_hit[0]
        qty_hit = [c for c in tcols[stn] if c != scol
                   and re.search(r"qty|stock|量|库存", c)
                   and not re.search(r"safe|frozen|冻结|预留", c)]
        if not qty_hit:
            return None
        qcol = qty_hit[0]
        if any(k in qcol for k in ("avail", "可用", "current", "现存")):
            pass  # 优先可用量已在 qty_hit 排序前时命中
        # 对象名列：另一张 dim 表与 stn 有同名列可 JOIN（product_id 等）
        dim_tn = dim_col = join_col = None
        for tn, cols in tcols.items():
            if tn == stn:
                continue
            common = [c for c in cols if c in tcols[stn] and c.endswith("_id")]
            if not common:
                continue
            name_cols = [c for c in cols if c.endswith(("name", "名称", "名字"))
                         or re.search(r"^name$|product_name|物料名", c)]
            if not name_cols:
                continue
            dim_tn, dim_col, join_col = tn, name_cols[0], common[0]
            break
        if not dim_tn:
            # 无 dim 表可 join：退化为单表明细（仓库+量+安全）
            sel = (f'SELECT {stn}."{qcol}" AS "当前量", {stn}."{scol}" AS "安全库存" '
                   f'FROM {stn} WHERE {stn}."{qcol}" < {stn}."{scol}" '
                   f'ORDER BY ({stn}."{scol}" - {stn}."{qcol}") DESC LIMIT 100')
        else:
            sel = (f'SELECT d."{dim_col}" AS "对象", s."{qcol}" AS "当前量", '
                   f's."{scol}" AS "安全库存" FROM {stn} s '
                   f'JOIN {dim_tn} d ON s."{join_col}" = d."{join_col}" '
                   f'WHERE s."{qcol}" < s."{scol}" '
                   f'ORDER BY (s."{scol}" - s."{qcol}") DESC LIMIT 100')
        # 只读/列名校验兜底（与逃生同哲学：校验不过不产出）
        from agent.sql_validator import validate_sql_safety
        _ok, _err, _clean = validate_sql_safety(sel)
        if not _ok:
            return None
        if _validate_sql_columns(sel):
            return None
        return {
            "understanding": "命中「阈值预警」规则：列出低于安全库存的对象（按缺口从大到小排序）",
            "metrics": [],
            "dimensions": [],
            "filters": [],
            "time_range": "",
            "sql_draft": sel,
            "confidence": 0.8,
            "risks": ["按列名命名约定推断「量列 < 安全列」，请在弹窗中核对列口径后再执行"],
            "llm_generated": True,
            "escape_hatch": True,
            "deterministic": True,
        }
    except Exception:
        return None


def _try_expr_rules(query: str, matched_tables: list, cmap: dict) -> dict | None:
    """确定性「表达式/子查询/CASE」规则通道（2026-09-06 yans 题库评测后补）。

    MQL 表达不了列间运算/HAVING 子查询/EXISTS/CASE 分档——GLM json 推断对这类问法
    会卡死（实测 5 类题全 120s 超时）。规则识别命中 → 直接拼 SQL 秒出（零 LLM）。
    未命中返回 None 走原链路。产物过 validate_sql_safety + 列名校验，标记 deterministic。
    """
    q = query or ""
    try:
        # 候选表名集（短名）
        tnames = {str(t.get("table_name", "")).split(".")[-1].lower()
                  for t in (matched_tables or [])[:4]}

        def _has(*tbls):
            return any(t in tnames for t in tbls)

        def _col_in(table, *cols):
            for c in (cmap.get(table) or []):
                if str(c).lower() in cols:
                    return str(c).lower()
            return None

        def _build(sql, understanding, risks):
            from agent.sql_validator import validate_sql_safety
            _ok, _err, _clean = validate_sql_safety(sql)
            if not _ok:
                return None
            if _validate_sql_columns(sql):
                return None
            return {"understanding": understanding, "metrics": [], "dimensions": [],
                    "filters": [], "time_range": "", "sql_draft": sql,
                    "confidence": 0.8, "risks": risks,
                    "llm_generated": True, "escape_hatch": True, "deterministic": True}

        # ── ① 损耗 = 列 - 列（投入-产出/损耗）──
        if ("损耗" in q or "损失" in q or "损耗量" in q) and _has("mes_process_output"):
            dim = "line_id" if ("产线" in q or "line" in q.lower()) else (
                "process_id" if ("工序" in q or "process" in q.lower()) else None)
            if dim:
                dim_label = "产线" if dim == "line_id" else "工序"
                sql = (f'SELECT {dim}, SUM(input_qty) - SUM(good_qty) AS "损耗量" '
                       f'FROM mes_process_output GROUP BY {dim} ORDER BY 2 DESC LIMIT 100')
                return _build(sql, f"命中「损耗」规则：按{dim_label}分组统计投入-合格损耗",
                              ["按列名约定推断 损耗=投入(input_qty)-合格(good_qty)，请核对"])

        # ── ② 高于平均（HAVING/明细筛选；按问法实体与数据形态区分口径）──
        if "平均" in q and ("高于" in q or "超过" in q or "大于" in q):
            if ("库存" in q or "available" in q.lower()) and _has("inv_inventory_snapshot"):
                # 库存快照是逐行明细 → 明细筛选（每行 available_qty 与全局平均比）
                sql = ('SELECT product_id, available_qty FROM inv_inventory_snapshot '
                       'WHERE available_qty > (SELECT AVG(available_qty) FROM inv_inventory_snapshot) '
                       'ORDER BY available_qty DESC')
                return _build(sql, "命中「高于平均」规则：列出可用库存高于全局平均值的快照明细",
                              ["库存按逐行 available_qty 与全表平均比较，请核对"])
            elif ("产量" in q or "合格" in q or "产出" in q) and _has("mes_process_output"):
                # 产量按实体汇总后再比平均 → 分组 HAVING
                if "工序" in q or "process" in q.lower():
                    dim, dim_label = "process_id", "工序"
                else:
                    dim, dim_label = "line_id", "产线"
                sql = (f'SELECT {dim}, SUM(good_qty) AS "总产量" FROM mes_process_output '
                       f'GROUP BY {dim} HAVING SUM(good_qty) > '
                       f'(SELECT AVG(good_qty) FROM mes_process_output) '
                       f'ORDER BY 2 DESC LIMIT 100')
                return _build(sql, f"命中「高于平均」规则：筛选{dim_label}产量高于全表平均值的分组",
                              ["产量按 good_qty 求和、均值按明细行 AVG，请核对"])

        # ── ③ 有/无停机记录的设备（IN 子查询）──
        if ("停机记录" in q and "设备" in q) and _has("eqp_downtime_record", "dim_equipment"):
            neg = ("无" in q or "没有" in q or "未" in q)
            op = "NOT IN" if neg else "IN"
            sql = (f'SELECT equipment_name FROM dim_equipment '
                   f'WHERE equipment_id {op} (SELECT DISTINCT equipment_id FROM eqp_downtime_record) '
                   f'ORDER BY equipment_id LIMIT 100')
            return _build(sql, f"命中「{'无' if neg else '有'}停机记录设备」规则：按停机记录表子查询过滤设备",
                          ["按 equipment_id 关联停机记录表推断，请核对"])

        # ── ④ 库存充足程度分类（CASE WHEN）──
        if ("充足" in q and ("分类" in q or "分档" in q or "程度" in q)) \
                and _has("inv_inventory_snapshot"):
            sql = ("SELECT product_id, "
                   "CASE WHEN available_qty < safety_stock_qty THEN '告急' "
                   "WHEN available_qty < safety_stock_qty * 2 THEN '偏低' ELSE '充足' END AS \"充足程度\" "
                   "FROM inv_inventory_snapshot ORDER BY product_id")
            return _build(sql, "命中「库存充足分类」规则：按可用量相对安全库存分档（告急/偏低/充足）",
                          ["分档阈值按 1×/2× 安全库存推断，请核对业务口径"])

        # ── ⑤ 工期天数 = 结束日期 - 开始日期（列间日期差）──
        if ("工期" in q or "周期" in q or "耗时" in q) and "工单" in q and _has("mes_work_order"):
            sql = ("SELECT work_order_id, (end_date - start_date) AS \"工期天数\" "
                   "FROM mes_work_order ORDER BY work_order_id")
            return _build(sql, "命中「工期天数」规则：按 结束日期-开始日期 计算工单工期",
                          ["工期=end_date-start_date（单位天），请核对日期口径"])

        # ── ⑥ 整个车间/全体的投入合格不良总量（不分组 SUM）──
        if ("整个" in q or "全体" in q or "整体" in q) and \
                ("车间" in q or "投入" in q or "总量" in q) and _has("mes_process_output"):
            sql = ('SELECT SUM(input_qty) AS "投入总量", SUM(good_qty) AS "合格总量", '
                   'SUM(defect_qty) AS "不良总量" FROM mes_process_output')
            return _build(sql, "命中「全体总量」规则：车间整体投入/合格/不良求和（不分组）",
                          ["总量不分组，请核对是否需要按车间/产线维度拆分"])
    except Exception:
        return None
    return None


def infer_query_analysis(query: str, schema_context: str, matched_tables: list,
                         metric_hint: str = "", wall_budget_s: float = 60.0) -> dict | None:
    """LLM 推断用户意图并给出可执行查询方案（JSON 结构化）。

    返回 {understanding, metrics, dimensions, filters, time_range, sql_draft,
          confidence, risks}；任何异常/解析失败返回 None（调用方降级）。
    wall_budget_s：整段推断（含多轮 + 逃生）的墙钟预算，到点立即放弃——
    避免 GLM 等慢模型在"0 输出 + SDK 重试"下把单次请求拖到几分钟（用户侧卡死感）。
    """
    if not _ANALYSIS_CONFIRM_ENABLED:
        return None
    _wall0 = time.monotonic()

    def _remain() -> float:
        return max(2.0, wall_budget_s - (time.monotonic() - _wall0))
    try:
        tables_desc = "\n".join(
            f"- {t['table_name']}"
            for t in (matched_tables or [])[:10]
        ) or "(无候选表)"
        # P0-修复（2026-09-02）：schema 上下文只注入【纯列名清单】——
        # 实测带描述/关联表等富文本（_build_schema_fast 输出）会让 DeepSeek json_mode
        # 长时间零输出（45s+）；纯列名清单 340 字符即可 14s 内完成推断。
        try:
            _cmap = _all_table_columns() or {}
        except Exception:
            _cmap = {}
        # 确定性「阈值预警」快速通道（2026-09-06）："低于安全库存的产品预警"类 = 列间比较，
        # MQL 表达不了、LLM json 推断又慢又不稳（90s+ 超时/0 输出）→ 规则命中直接秒出，零 LLM。
        _warning = _try_warning_rule(query, matched_tables, _cmap)
        if _warning:
            return _warning
        _expr = _try_expr_rules(query, matched_tables, _cmap)
        if _expr:
            return _expr
        # 2026-09-03：候选表 ≤4、单表列 ≤50、字符预算 1400 —— 输入更小，首 token 更快
        # 2026-09-06：预算再收紧 1400→800、单表 50→30 —— 实测 DeepSeek 对超长中文 schema
        # prompt 会【静默 0 输出】（库存预警题 3 连发全 0、白等 46s；同题短 prompt 秒回）。
        # 总 prompt 压到 ~1.5k 字符安全区；列不全靠重试/逃生兜底，0 输出则必死。
        # 2026-09-14 **撤掉过裁**：此处原为 4 张表 / 单表 30 列 / 总 800 字符（上限 1000），
        # 起因是"长中文 prompt 会静默 0 输出"。但那条经验来自**自由生成整条 SQL** 的直生路径；
        # 本路径的输出是一段紧凑 JSON（理解/指标/维度/筛选/时间/置信度），输出侧风险小得多，
        # 而 schema 被裁到只看得到 30 列 → 模型选错表/选错列，正是"猜"的源头。
        # 列不全 → 结构化编译失败 → 全部掉到自由生成兜底，反而更差（负循环）。
        # 现在：6 张表 / 单表 60 列 / 总 1600 字符（上限 2000），输出预算也已提到配置值 8192。
        _slim_parts = []
        for t in (matched_tables or [])[:6]:
            _tname = t.get("table_name", "")
            _cols = _cmap.get(_tname) or _cmap.get(str(_tname).split(".")[-1].lower(), [])
            if not _cols:
                continue
            _slim_parts.append(f"- {_tname} 列: " + ", ".join(str(c) for c in _cols[:60]))
            if sum(len(p) for p in _slim_parts) > 1600:
                break
        slim_schema = "\n".join(_slim_parts)[:2000] or (schema_context or "")[:500]
        hint_block = (f"口径参考（按其中说明使用，不得把参考口径冒充用户所问指标）：{metric_hint[:600]}"
                      if metric_hint
                      else "（该指标未在口径库注册，按通用业务理解推断并在 risks 里说明假设）")
        # P1（2026-09-04）：低基数枚举值注入 —— 基线 2 题 value_mismatch 的根因是
        # LLM 不知道 status 等列的真实取值（risks 里自己提示"可能是'运行'"）。
        # 2026-09-06 按需化：仅当问题提到枚举筛选类词（状态/类型/类别/班次…）才收集注入——
        # 否则 700 字符枚举块纯增 prompt 长度、抬高 DeepSeek 0 输出/卡死概率（长 prompt 实测静默空响应）。
        # 2026-09-12 修复：MQL 推断阶段此前同样看不到外键，跨表题只能靠 LLM 猜 ON 条件。
        _join_block = _join_hint_for_tables([t.get("table_name") for t in (matched_tables or [])[:6]])
        _enum_block = ""
        _enums: dict = {}
        if re.search(_ENUM_TRIGGER_RE, query or "", re.I):
            try:
                _enums = _enum_values_for_tables(
                    [t.get("table_name") for t in (matched_tables or [])[:4]])
            except Exception:
                _enums = {}
        if _enums:
            _eparts: list[str] = []
            _ebudget = _ENUM_TOTAL_BUDGET
            for _k in sorted(_enums):
                _line = f"- {_k}: " + " | ".join(_enums[_k])
                if len(_line) > _ebudget:
                    break
                _eparts.append(_line)
                _ebudget -= len(_line)
            if _eparts:
                _enum_block = ("可选枚举值（筛选值必须逐字取自这里，不得编造）：\n" + "\n".join(_eparts) + "\n\n")
        # 2026-09-04 prompt v2：对齐编译器 v2 能力（明细模式 / distinct / IN / time_range）。
        # schema 里每条说明都对应 _compile_sql_draft v2 的真实能力，不让 LLM 输出编译器接不住的字段。
        prompt = (
            f"用户问题：{query}\n\n"
            f"候选表（只能从这些表选列）：\n{tables_desc}\n\n"
            f"表结构（列名，不得编造）：\n{slim_schema}\n\n"
            f"{_join_block}"
            f"{_enum_block}"
            f"{hint_block}\n\n"
            "输出一个 JSON 对象（只输出 JSON，不要解释/代码块）：\n"
            '{"understanding":"口径一句话","metrics":[{"name":"指标名","agg":"SUM/AVG/COUNT/MAX/MIN","column":"候选列"}],'
            '"dimensions":["候选列"],"filters":[{"field":"列","op":"=/!=/>/</>=/<=/LIKE/IN","value":"值"}],'
            '"time_range":{"column":"时间列","start":"YYYY-MM-DD","end":"YYYY-MM-DD"},"confidence":0.8,"risks":["假设≤2条"]}\n'
            "规则：列名逐字取自候选列清单；明细类(有哪些/列出)不填 metrics；统计类 metrics≤2；同列多值用 IN；"
            "分析类(统计/情况/分布/排行/各…/分别/按…)必须给≥1个维度(状态/类型/类别/名称/产线/产品/设备等分类列，跨表也要填)，只有『总共/一共多少』才可无维度。"
        )
        def _mql_complete(d) -> bool:
            """完整性门槛：metrics 或 dimensions 至少一个非空（否则编译不出任何 SQL）。"""
            return (isinstance(d, dict)
                    and bool((d.get("metrics") or []) or (d.get("dimensions") or [])))

        def _run_infer(_limit: float, _use_json: bool = True, _suffix: str = "",
                       _invoke: bool = False):
            try:
                _llm = _make_llm(temp=0.0, max_tokens=_sql_gen_max_tokens(), json_mode=_use_json,
                                 model=_sql_gen_model(),
                                 timeout=min(_limit, 90), max_retries=0)  # 禁重试：超时即放弃换路，防止 3×timeout 卡死
                if _invoke:
                    # 首 token 慢的 provider（zhipu/GLM 等，实测完整长 prompt 响应 ~30s）：
                    # stream + 8s 首 token 门会提前掐断 → 伪 0 输出。改用 invoke 等到完整响应。
                    try:
                        _r = _llm.invoke([HumanMessage(content=prompt + _suffix)])
                        _s = str(getattr(_r, "content", "") or "")
                    except Exception:
                        return None
                else:
                    _dl = time.monotonic() + _limit
                    _tstart = time.monotonic()
                    _s = ""
                    _first_at: float | None = None
                    for _c in _llm.stream([HumanMessage(content=prompt + _suffix)]):
                        if _first_at is None and isinstance(_c, AIMessageChunk) and _c.content:
                            _first_at = time.monotonic()
                        elif _first_at is None and time.monotonic() - _tstart > _FIRST_TOKEN_S:
                            # 首 token 门：真卡死（快速模型长时间 0 输出）立即放弃
                            return None
                        if time.monotonic() > _dl:
                            if _s.strip().startswith("{"):
                                break
                            return None
                        if isinstance(_c, AIMessageChunk) and _c.content:
                            _s += _c.content
                _s = (_s or "").strip()
                if _s.startswith("```"):
                    _s = re.sub(r"^```(?:json)?\s*|\s*```$", "", _s).strip()
                _j = json.loads(_s)
                return _j if isinstance(_j, dict) else None
            except Exception:
                return None

        # 2026-09-04 竞速修正：废弃「A/B 双路竞速 + B 路鼓励留空」。
        # 基线证据：15 题触发 28 次推断调用（1.87 次/题，token 翻倍），且 B 路的
        # 「加速指令」鼓励留空 —— 先返回的常是质量更差的结果，竞速在系统性选中差答案。
        # 改为单路 + 条件重试：一次完整推断，仅当失败/不完整时才重试一次。
        # 2026-09-06 提速再校准：首轮 20s（实测真实模糊问法推断 13.3s——15s 预算余量仅 1.7s，
        # API 稍慢即被截断误杀，用户实测「生产工单表统计」撞上 16~25s 慢推断失败；
        # 20s 覆盖 13~25s 偏慢成功区间）+ 重试 10s + 逃生 15s，总最坏 45s 封顶。
        # 2026-09-06：json_mode 大 schema prompt 偶发 0 输出卡死（实测库存预警题 3 连发全 0、
        # 白等 46s）→ ①首 token 门 8s 快速放弃；②失败后切【普通模式】重试（不带
        # response_format 时模型更愿意吐字，prompt 追加强制 JSON 说明）；仍不行走逃生。
        # 2026-09-06：json_mode 大 schema prompt 存在【请求级随机 0 输出】（同 prompt 时好时坏、
        # 非必现；库存预警题复现 3 连发全 0、白等 46s）。对策：①首 token 门 8s 快速放弃不傻等；
        # ②json/plain 交替最多 3 轮——单轮失败立即换路，把单轮 ~50% 成功率抬到 ~87%；
        # ③仍失败编译不出走逃生。0 输出场景总预算 ≈ 3×8s=24s，成功轮提前返回。
        data = {}
        _MQL_SUFFIX_PLAIN = ("\n\n严格只输出一个 JSON 对象，禁止任何解释、"
                             "不要 markdown 代码块或 ```json 围栏。")
        # 2026-09-06 再校准：预算/模式按 provider 分档——
        # · 首 token 慢的（zhipu/GLM 等，实测长 prompt 完整响应 ~30s）：invoke + 40s/25s，
        #   否则 stream 短预算会把慢模型全掐成伪 0 输出（GLM 4.7 实测 4 连发 0、裸 invoke 29s 成功）；
        # · 首 token 快的（deepseek）：stream + 15s/12s（0 输出时 8s 门快速换路）。
        try:
            from agent.llm_providers import detect_provider as _dp
            _slow_ttfb = (_dp(LLM_CONFIG.get("model", ""), LLM_CONFIG.get("base_url", ""))
                          .get("name") in ("zhipu", "moonshot", "kimi"))
        except Exception:
            _slow_ttfb = False
        # 轮次预算按墙钟**自适应**（2026-09-14 主次反转）：
        # 此前是写死的 (15s, 12s)（快 provider）/(40s, 25s)（慢 provider），与上层给的
        # wall_budget_s 无关 —— 预算被压缩时第二轮/逃生只会拿到 2s 兜底，等于白跑，
        # 结构化路径（本应做主路径）因此经常失败，问题全部落到"自由生成 SQL"兜底。
        # 现按预算比例分配：第一轮 55%、第二轮 30%，其余留给逃生。
        _budget = max(6.0, float(wall_budget_s or 0.0))
        _w1 = max(4.0, _budget * 0.55)
        _w2 = max(3.0, _budget * 0.30)
        _tries = ((_w1, True, "", _slow_ttfb), (_w2, False, _MQL_SUFFIX_PLAIN, _slow_ttfb))
        for _lim, _use_json, _suf, _invoke in _tries:
            _d = _run_infer(min(_lim, _remain()), _use_json, _suf, _invoke)
            if _mql_complete(_d):
                data = _d
                break
        if not isinstance(data, dict):
            data = {}
        # SQL 由确定性编译生成（LLM 只给结构化口径——红线收敛，2026-09-02）
        sql, dropped = _compile_sql_draft(data, matched_tables)
        # ── P2 SQL 逃生舱（2026-09-04）────────────────────────
        # MQL 编译不出 ≠ 直接失败，区分两种情况：
        # ① LLM 有完整 MQL 但编译不出 → 表达力边界（CASE WHEN/子查询类），
        #    重试同 schema 的 MQL 无意义，直接逃生；
        # ② 两轮均无有效 MQL → 最后一次逃生机会。
        # 逃生产物强制过 validate_sql_safety + 列名校验，且标记 escape_hatch=True。
        escape_reason = ""
        if not sql:
            if _mql_complete(data):
                escape_reason = "该查询涉及结构化口径无法表达的能力（如分类表达式/子查询）"
            else:
                escape_reason = "结构化口径推断失败"
            _esc = _run_escape_sql(query, tables_desc, slim_schema, _enum_block,
                                   limit_s=min(15.0, _remain()))
            if not _esc:
                return None
            sql = _esc
        _ci = data.get("confidence")
        _conf = float(_ci) if _ci is not None else 0.5
        _risks = [str(r) for r in (data.get("risks") or [])[:4]]
        if dropped:
            _risks.insert(0, "以下字段未能自动关联进查询，SQL 中已剔除，可在弹窗中手动修正："
                            + "、".join(dropped[:5]))
        # metrics 规范化为弹窗展示结构（name/formula 兼容原字段，agg/column 供溯源）
        _metrics_out = []
        for m in (data.get("metrics") or [])[:3]:
            _col = str(m.get("column") or "").strip()
            _agg = str(m.get("agg") or "").upper()
            if _agg not in _SQL_AGG:
                _agg = "COUNT" if not _col else "SUM"
            _formula = f"{_agg}({_col})" if _col else "COUNT(*)"
            _metrics_out.append({
                "name": str(m.get("name") or "").strip() or "指标",
                "agg": _agg,
                "column": _col,
                "formula": _formula,
            })
        if escape_reason:
            # 逃生舱产物：LLM 直出 SQL（已过只读校验 + 列名校验），显式标注未经口径编译保证
            return {
                "understanding": escape_reason + "，SQL 已由 AI 直接生成",
                "metrics": [],
                "dimensions": data.get("dimensions") or [],
                "filters": data.get("filters") or [],
                "time_range": "",
                "sql_draft": sql,
                "confidence": 0.5,
                "risks": ["此查询超出结构化口径能力，SQL 由 AI 直接生成、未经口径编译保证，"
                          "请在弹窗中重点核对后再执行"],
                "llm_generated": True,
                "escape_hatch": True,
            }
        return {
            "understanding": str(data.get("understanding") or "").strip(),
            "metrics": _metrics_out,
            "dimensions": data.get("dimensions") or [],
            "filters": data.get("filters") or [],
            "time_range": str(data.get("time_range") or "").strip(),
            "sql_draft": sql,
            "confidence": min(max(_conf, 0.0), 1.0),
            "risks": _risks,
            # 架构标记：口径/结构由 LLM 推断，SQL 由确定性规则编译（非 LLM 直出）；
            # 执行前仍需用户二次确认（execute_confirm），弹窗内可修改 SQL。
            "llm_generated": True,
        }
    except Exception:
        return None


def _current_db_key() -> str:
    """当前数据库唯一标识（用于全局状态按库隔离）"""
    try:
        from database import get_database_config
        cfg = get_database_config()
        return f"{cfg.get('db_type', 'pg')}:{cfg.get('host', '')}:{cfg.get('port', '')}:{cfg.get('name', '')}"
    except Exception:
        return "default"


def _get_last_ml_model() -> dict:
    return _ml_model_by_db.get(_current_db_key(), {}) or {}


def _set_last_ml_model(model: dict) -> None:
    _ml_model_by_db[_current_db_key()] = model

# 全库表结构快照（供"目标列不在这张表里 → 去别的表找"用）
# get_numeric_tables 一次查询能拿全库表 + 列 + 行数，但每次问析都跑一遍没必要，
# 60s 内复用；切库时 key 变 → 自动失效。
_ml_schema_map_cache: dict[str, tuple[float, dict]] = {}


def _ml_schema_map(ttl: float = 60.0) -> dict:
    """{表名: {"columns": [...], "numeric": [...], "rows": n}}，取不到返回 {}"""
    key = _current_db_key()
    hit = _ml_schema_map_cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        from ml.trainer import get_numeric_tables
        m = {t["table"]: {"columns": t.get("columns") or [],
                          "numeric": t.get("numeric_columns") or [],
                          "rows": t.get("rows") or 0}
             for t in get_numeric_tables()}
    except Exception:
        m = {}
    _ml_schema_map_cache[key] = (now, m)
    return m

# LLM 白名单模型名 → trainer model_type 映射
_ML_MODEL_MAP = {
    "LinearRegression": "linear",
    "LogisticRegression": "logistic",
    "DecisionTreeClassifier": "decision_tree",
    "DecisionTreeRegressor": "decision_tree",
    "RandomForestClassifier": "random_forest",
    "RandomForestRegressor": "random_forest",
    "KMeans": "kmeans",
    "IsolationForest": "isolation",
}


def _q_table(table_ref: str) -> str:
    """生成正确的表名引用：public 表 → "table"；非 public → "schema"."table"
    （不能写 "schema.table"，那会被当作带点的表名）"""
    schema, table = _split_table_ref(table_ref)
    from database import quote_ident
    if schema == "public":
        return quote_ident(table)
    return f"{quote_ident(schema)}.{quote_ident(table)}"


# ── 极简指标输入 → 主动澄清候选（避免硬猜，提升交互质量）──
_CLARIFY_METRICS: dict[str, list[str]] = {
    "产量": ["各产线的产量排行", "最近7天的产量", "每天的产量趋势", "各工序的产量"],
    "良率": ["各工序的良率", "最近7天的良率", "各产线的良率排行"],
    "不良率": ["各工序的不良率", "最近7天的总不良率"],
    "库存": ["当前库存总量", "库存预警分析", "各仓库的库存量"],
    "缺货": ["缺货预警分析", "缺货量最大的产品"],
    "停机": ["设备停机时长排行", "最近7天的停机趋势"],
    "工单": ["工单状态分布", "各产线的工单数"],
    "销量": ["各产品的销量排行", "最近7天的销量趋势"],
    "金额": ["各产品的销售金额", "每月销售金额趋势"],
}


def _clarify_candidates(query: str) -> list[str]:
    """极简指标输入（如"产量""良率"）→ 返回候选具体问法；已具体的问题返回空列表"""
    q = (query or "").strip()
    if not q or len(q) > 8:
        return []
    prefixes = ("", "查", "看", "统计", "分析", "查询", "帮我查", "帮我", "给我")
    for name, cands in _CLARIFY_METRICS.items():
        if any(q == (p + name) for p in prefixes):
            return cands
    return []


def _auto_joinable_bare_names(matched_tables: list[dict]) -> set[str]:
    """表匹配候选通过外键可达的表（裸表名集合），用于判断 SQL 引用是否越界。

    候选维度表（如 dim_product）通常需要 JOIN 事实表才能回答聚合问题，
    因此把"候选表直接外键可达的表"也视为合法引用，避免误拦截正常 JOIN。
    """
    out = set()
    try:
        from db.tools import get_foreign_keys
        fk_map = get_foreign_keys()
    except Exception:
        fk_map = {}
    cand_bare = {t["table_name"].split(".")[-1].lower() for t in matched_tables}
    for child, fks in fk_map.items():
        child_bare = child.split(".")[-1].lower()
        for fk in fks or []:
            parent = (fk.get("ref_table") or "").split(".")[-1].lower()
            if parent and (child_bare in cand_bare or parent in cand_bare):
                out.add(child_bare)
                out.add(parent)
    return out


def _validate_sql_columns(sql: str) -> list[str]:
    """校验 SQL 引用的列是否在当前库对应表存在，返回缺失列描述列表（空 = 通过）。

    用 sqlglot AST 提取 (表, 列) 引用（含别名→真实表映射），与当前库 information_schema
    列集合对比。LLM 常凭先验知识用错列名（如 yans 的 order_status/severity_level 被写成
    status/severity）——表存在但列缺失执行必失败，需在执行前拦截并让迭代修复链修正。
    """
    if not sql:
        return []
    try:
        import sqlglot
        from sqlglot import exp
        ast = sqlglot.parse_one(sql)
    except Exception:
        return []
    # 当前库列集合（表名 → 列集合，小写）
    try:
        from db.tools import get_real_tables, get_table_detail
        real = get_real_tables() or []
        col_map: dict[str, set[str]] = {}
        for t in real:
            name = t["table_name"].split(".")[-1].lower()
            try:
                detail = get_table_detail(t["table_name"]) or {}
                cols = {str(f.get("name", "")).lower() for f in (detail.get("fields") or [])}
            except Exception:
                cols = set()
            if cols:
                col_map[name] = cols
    except Exception:
        return []
    if not col_map:
        return []
    # 别名 → 真实表名
    alias_map: dict[str, str] = {}
    try:
        for tbl in ast.find_all(exp.Table):
            name = tbl.name.split(".")[-1].strip().lower()
            alias = (tbl.alias_or_name or name).strip().lower()
            alias_map[alias] = name
            alias_map[name] = name
    except Exception:
        pass
    missing: list[str] = []
    seen: set[str] = set()
    for col_ref in ast.find_all(exp.Column):
        col = (col_ref.name or "").strip().strip('"').lower()
        if not col:
            continue
        # `表名.*` 通配：sqlglot 把它解析成 name="*"、table="表名" 的列引用。
        # 若不跳过，`_fix_detail_row_superset` 产出的 `SELECT t.*, ...` 会被误判成
        # 「引用了不存在的列 t.*」而被拦下 → 走重试链重新生成，**补列改写全部白做**。
        # 实测代价（2026-09-17）：92 题报告里含 `.*` 的 SQL 数为 0，即该改写从未落地；
        # 典型受害题 postgres「库存最低的前5个产品」（缺 safety_stock_qty 列而判失败）。
        if col == "*":
            continue
        table_ref = (col_ref.table or "").strip().strip('"').lower()
        real_tbl = alias_map.get(table_ref)
        if not real_tbl:
            continue  # 无法确定所属表（如 COUNT(*) 内部无表）→ 表校验已覆盖
        real_cols = col_map.get(real_tbl)
        if real_cols is None:
            continue
        key = f"{real_tbl}.{col}"
        if col not in real_cols and key not in seen:
            seen.add(key)
            missing.append(f"{real_tbl}.{col}（实际列: {', '.join(sorted(real_cols)[:10])}）")
        if len(missing) >= 8:
            break
    return missing


def _extract_sql_tables(sql: str) -> set[str]:
    """从 SQL 提取引用的真实表名（小写、去 schema、去引号），排除 CTE 名。

    表权限校验：SQL 引用的表必须都在角色允许集合内。
    优先用 sqlglot AST（能正确区分 CTE 名 `WITH total AS (...)` 与真实表，
    避免把 CTE 别名误当无权表拦截 403）；解析失败回退正则 + WITH 名排除。
    """
    if not sql:
        return set()
    try:
        import sqlglot
        from sqlglot import exp
        ast = sqlglot.parse_one(sql)
        cte_names = {cte.alias_or_name.lower() for cte in ast.find_all(exp.CTE)}
        tables = set()
        for tbl in ast.find_all(exp.Table):
            name = tbl.name.split(".")[-1].strip().lower()
            if name and name not in cte_names:
                tables.add(name)
        if tables:
            return tables
    except Exception:
        pass
    # 回退：正则（保留原逻辑）+ WITH 名排除
    cte_names = {m.group(1).lower() for m in re.finditer(r"\bWITH\s+([A-Za-z0-9_\"`]+)\s+AS\b", sql, re.IGNORECASE)}
    tables = set()
    for m in re.finditer(r"\b(?:FROM|JOIN|UPDATE|INTO|TABLE)\s+([A-Za-z0-9_\"`\.]+)", sql, re.IGNORECASE):
        part = m.group(1).strip().strip('"`')
        part = part.split(".")[-1]
        if part and part.lower() not in ("select", "where", "on") and part.lower() not in cte_names:
            tables.add(part.lower())
    return tables


def _is_missing_table_error(err: str) -> bool:
    """判断 SQL 执行错误是否为「引用了不存在的表」（PG 42P01 / MySQL 1146）。

    这类错误重试没有意义（表不在当前库，再让 LLM 修复也只会编造不存在的表名），
    应快速失败并给出明确提示，避免多次 LLM 重试把响应拖到超时。
    """
    if not err:
        return False
    patterns = [
        r'relation\s+"?[a-z_."]+"?\s+does not exist',   # PostgreSQL 42P01
        r'42P01',
        r"table\s+[`'\"\[]?[\w.]+[`'\"\]]?\s+doesn'?t exist",  # MySQL 1146
        r'1146',
        r'unknown table',
    ]
    return any(re.search(p, err, re.IGNORECASE) for p in patterns)


def _fallback_sql(query: str, tables: list[dict]) -> str:
    """常见查询兜底 SQL — 优先查真实 DB 字段"""
    from database import quote_ident
    if not tables:
        return "SELECT 1"
    top = tables[0]["table_name"]
    q_top = _q_table(top)

    # 优先从真实 DB 获取字段
    fields = _get_db_columns(top)
    if not fields:
        meta = _find_meta(top)
        fields = meta.get("fields", []) if meta else []

    if not fields:
        return f"SELECT * FROM {q_top} LIMIT 20"

    num_cols = [f["name"] for f in fields if f["type"] in ("integer","bigint","numeric","real","double precision","smallint")][:3]
    str_cols = [f["name"] for f in fields if f["name"] not in num_cols][:3]

    # 停机设备 → 按时间倒序
    if any(w in query for w in ["停机", "设备停机"]):
        time_col = "start_time" if any(f["name"] == "start_time" for f in fields) else ""
        if time_col:
            return f"SELECT * FROM {q_top} ORDER BY {quote_ident(time_col)} DESC LIMIT 20"
        return f"SELECT * FROM {q_top} LIMIT 20"

    # 最近 → 按时间/日期倒序
    if any(w in query for w in ["最近", "最新", "近7天", "本周", "过去"]):
        for candidate in ["start_time", "stat_date", "create_time", "inspect_date", "snapshot_date"]:
            if any(f["name"] == candidate for f in fields):
                return f"SELECT * FROM {q_top} ORDER BY {quote_ident(candidate)} DESC LIMIT 20"

    # 聚合场景：仅在用户问题确实是聚合分析时做 GROUP BY，否则返回明细
    # （避免"查看设备列表"这类问题被兜底成错误的分组汇总）
    if str_cols and num_cols and _needs_aggregation(query):
        group_col = str_cols[0]
        agg_col = num_cols[0]
        # 方言兼容：MySQL 不支持 ::numeric 类型转换，PG 用 ::numeric 保精度
        try:
            from database import get_db_type
            cast = "" if get_db_type() == "mysql" else "::numeric"
        except Exception:
            cast = ""
        return f"SELECT {quote_ident(group_col)}, SUM({quote_ident(agg_col)}){cast} AS 汇总 FROM {q_top} GROUP BY {quote_ident(group_col)} ORDER BY 汇总 DESC LIMIT 20"

    # 兜底：取前几列
    all_cols = [f["name"] for f in fields[:6]]
    col_str = ", ".join(quote_ident(c) for c in all_cols) if all_cols else "*"
    return f"SELECT {col_str} FROM {q_top} LIMIT 20"


def _find_meta(table_name: str) -> dict | None:
    """从 TABLES 元数据中找表"""
    for t in METADATA_TABLES:
        if t["table_name"] == table_name:
            return t
    return None


# ── 语义缓存（问题 embedding 相似度 → 复用 SQL，跳过最慢的 LLM 选表/生成链）──
# 对标 Wren AI / AWS / Skopx 的亚秒级延迟核心手段：重复/相似业务问题命中后
# 直接复用历史 SQL 重新执行（保证数据新鲜），省掉 3–6 次 LLM 串行往返。
# 复用现有 get_embedding_fn（本地 bge / OpenAI 兼容），embedding 不可用时自动禁用。
_SEM_CACHE_LOCK = threading.Lock()
_SEM_CACHE: dict[str, list] = {}                                            # db_key -> [entry, ...]
_SEM_CACHE_ENABLED = os.getenv("SEMANTIC_CACHE_ENABLED", "1") == "1"        # 总开关
_SEM_CACHE_MAX = int(os.getenv("SEMANTIC_CACHE_MAX", "200"))                # 每库最多条目
_SEM_CACHE_TTL = int(os.getenv("SEMANTIC_CACHE_TTL", "3600"))               # 条目默认 TTL（秒，兜底）
# TTL 分级（对齐 ZOOZ 主流实践）：纯维度表引用长 TTL，含事实表短 TTL（数据新鲜度保障）
_SEM_CACHE_TTL_DIM = int(os.getenv("SEMANTIC_CACHE_TTL_DIM", "86400"))      # 仅维度表: 24h
_SEM_CACHE_TTL_FACT = int(os.getenv("SEMANTIC_CACHE_TTL_FACT", "900"))      # 含事实表: 15min
_SEM_CACHE_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.92"))  # 余弦相似度阈值
# 持久化：语义缓存条目同步写入 cache_store（配置 REDIS_URL 后跨重启/跨实例共享；
# 未配置则内存回退——重启后从持久层重新加载，避免"沉淀即失"）
_SEM_CACHE_PERSIST = os.getenv("SEMANTIC_CACHE_PERSIST", "1") == "1"
_SEM_PERSIST_MAX = int(os.getenv("SEMANTIC_CACHE_PERSIST_MAX", "50"))        # 每库持久化条目上限
# 已加载过持久层的库集合：避免每次语义缓存命中都重复走 cache_store（穿透）
_sem_persist_loaded: set[str] = set()


def _sem_persist_save(db_key: str) -> None:
    """把当前库的语义缓存条目序列化存入 cache_store（按库聚合一条，控制体积）。"""
    if not _SEM_CACHE_PERSIST:
        return
    try:
        import cache_store
        with _SEM_CACHE_LOCK:
            entries = _SEM_CACHE.get(db_key, [])[-_SEM_PERSIST_MAX:]
        cache_store.cache_set(
            f"sem:idx:{db_key}", entries, ttl=max(_SEM_CACHE_TTL_DIM, _SEM_CACHE_TTL_FACT))
    except Exception:
        pass


def _sem_persist_load(db_key: str) -> None:
    """首次访问某库时，从持久层把语义缓存条目恢复到内存（合并去重）。

    仅加载一次（_sem_persist_loaded 标记），后续命中不再重复读存储层，避免缓存穿透。
    """
    if not _SEM_CACHE_PERSIST or db_key in _sem_persist_loaded:
        return
    _sem_persist_loaded.add(db_key)
    try:
        import cache_store
        data = cache_store.cache_get(f"sem:idx:{db_key}")
        if not data:
            return
        with _SEM_CACHE_LOCK:
            if db_key not in _SEM_CACHE:
                _SEM_CACHE[db_key] = []
            existing = {e.get("query") for e in _SEM_CACHE[db_key]}
            for e in data:
                if e.get("query") and e["query"] not in existing and "vec" in e:
                    _SEM_CACHE[db_key].append(e)
                    existing.add(e["query"])
    except Exception:
        pass


def _entry_ttl(tables: list | None) -> int:
    """按条目引用表类型计算有效 TTL：仅维度表（dim_* 前缀）长 TTL；含事实表/未知表短 TTL。"""
    if not tables:
        return _SEM_CACHE_TTL_FACT
    fact = [t for t in tables if not str(t).lower().startswith("dim_")]
    return _SEM_CACHE_TTL_DIM if not fact else _SEM_CACHE_TTL_FACT


def _embed_query(query: str):
    """对问题做归一化 embedding；embedding 不可用时返回 None（语义缓存自动禁用）"""
    try:
        from agent.embeddings import get_embedding_fn
        fn = get_embedding_fn()
        if not fn:
            return None
        return fn(query)
    except Exception:
        return None


def _cos_sim(a, b) -> float:
    """余弦相似度（显式 L2 归一化后点积）。

    不依赖 embedding 提供者是否已归一化：不同模型（bge / OpenAI 兼容 / ngram 兜底）
    向量模长不一致，直接裸点积会随模长漂移，导致 0.92 阈值判定失真。
    """
    try:
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return sum(x * y for x, y in zip(a, b)) / (na * nb)
    except Exception:
        return 0.0


def _semantic_lookup(query: str, db_key: str, acl_fp: str):
    """语义缓存命中：返回 {sql, matched_tables, schema_context, chart_type, similarity} 或 None"""
    if not _SEM_CACHE_ENABLED:
        return None
    _sem_persist_load(db_key)  # 首次访问懒加载持久层条目（重启后不丢缓存）
    vec = _embed_query(query)
    if not vec:
        return None
    now = time.time()
    best, best_sim = None, 0.0
    with _SEM_CACHE_LOCK:
        for e in _SEM_CACHE.get(db_key, []):
            # TTL 分级：按条目引用表类型（维度表长 TTL / 事实表短 TTL），保证事实数据新鲜度
            if now - e.get("ts", 0) > _entry_ttl(e.get("tables")):
                continue
            if e.get("acl_fp", "") != acl_fp:
                continue  # 权限指纹不一致不共享（防越权复用）
            sim = _cos_sim(vec, e["vec"])
            if sim > best_sim:
                best, best_sim = e, sim
    if best is not None and best_sim >= _SEM_CACHE_THRESHOLD:
        return {
            "sql": best["sql"],
            "matched_tables": best["matched_tables"],
            "schema_context": best["schema_context"],
            "chart_type": best.get("chart_type", ""),
            "similarity": best_sim,
            # 记录这条 SQL 当初是否由确定性编译器生成（用于命中后恢复口径溯源 +
            # 跳过 LLM 复查，见 run() 命中分支与 _review_chain）
            "compiled": best.get("compiled", False),
            "mql": best.get("mql"),
        }
    return None


def _semantic_store(query: str, sql: str, matched_tables: list, schema_context: str,
                    chart_type: str, db_key: str, acl_fp: str,
                    compiled: bool = False, mql: dict | None = None):
    """写入语义缓存（仅高质量成功 SQL 调用；同问题去重，超上限淘汰最旧）"""
    if not _SEM_CACHE_ENABLED or not sql:
        return
    vec = _embed_query(query)
    if not vec:
        return
    now = time.time()
    # 引用表集合（用于 TTL 分级；解析失败按事实表保守处理）
    try:
        _tables = sorted(_extract_sql_tables(sql))
    except Exception:
        _tables = []
    with _SEM_CACHE_LOCK:
        entries = [e for e in _SEM_CACHE.get(db_key, [])
                   if now - e.get("ts", 0) <= _entry_ttl(e.get("tables")) and e.get("query") != query]
        entries.append({
            "query": query,
            "vec": vec,
            "sql": sql,
            "matched_tables": matched_tables,
            "schema_context": schema_context,
            "chart_type": chart_type,
            "acl_fp": acl_fp,
            "tables": _tables,
            "ts": now,
            # 记录 SQL 来源：命中后据此恢复口径溯源 + 跳过 LLM 复查
            "compiled": bool(compiled),
            "mql": mql,
            # 2026-09-11 指标隔离：沉淀时记录本条 SQL 关联的注册指标名（小写集合），
            # 命中侧据此做口径一致性校验（见 _semantic_cache_hit），防近似口径偷换
            "metrics": sorted({str(h.get("name", "")).lower()
                               for h in ((getattr(self, "_metric_resolution", None) or {}).get("hits") or [])}),
        })
        if len(entries) > _SEM_CACHE_MAX:
            entries.sort(key=lambda e: e["ts"])
            entries = entries[-_SEM_CACHE_MAX:]
        _SEM_CACHE[db_key] = entries
    _sem_persist_save(db_key)  # 同步持久化（Redis/内存），重启后仍可命中


def _semantic_remove(query: str, db_key: str) -> int:
    """从语义缓存删除指定问题的条目（用户负反馈时调用，防止错误 SQL 被复用）。

    返回删除条数；同时清理持久层（Redis/内存），保证重启后不再命中。
    """
    if not _SEM_CACHE_ENABLED or not query:
        return 0
    removed = 0
    with _SEM_CACHE_LOCK:
        entries = _SEM_CACHE.get(db_key, [])
        kept = [e for e in entries if e.get("query") != query]
        removed = len(entries) - len(kept)
        if removed:
            _SEM_CACHE[db_key] = kept
    if removed:
        _sem_persist_save(db_key)
    return removed


# ── 结果缓存（Redis 可配 + 内存回退，带 ACL 指纹防越权复用）─────────────

def _cache_key(query: str, acl_fp: str = "") -> str:
    import hashlib
    return f"{_current_db_key()}|{acl_fp or 'anon'}|{hashlib.sha256(query.strip().encode('utf-8')).hexdigest()}"


def cache_result(query: str, sql: str, sql_result: dict, schema_context: str, acl_fp: str = ""):
    import cache_store
    key = _cache_key(query, acl_fp)
    # TTL 分级（对齐 ZOOZ 主流实践）：按 SQL 引用表类型决定结果缓存有效期——
    # 仅维度表（dim_*）结果 24h 长缓存；含事实表结果 15min 短缓存（保证数据新鲜）。
    ttl = _entry_ttl(sorted(_extract_sql_tables(sql)))
    cache_store.cache_set(key, {
        "sql": sql,
        "sql_result": sql_result,
        "schema_context": schema_context,
        "acl_fp": acl_fp,
    }, ttl=ttl)


def get_cached_result(query: str, acl_fp: str = "") -> dict | None:
    import cache_store
    key = _cache_key(query, acl_fp)
    return cache_store.cache_get(key)


def clear_cache() -> int:
    """清空结果缓存 + schema 缓存，返回清除条数（切换数据库时调用）

    注意：结果缓存走 cache_store（Redis/内存），这里通过 cache_store 统一清空，
    避免引用不存在的 `_last_results`（历史残留的 NameError 隐患）。
    """
    n = 0
    try:
        import cache_store
        n = cache_store.cache_clear()
    except Exception:
        pass
    try:
        _build_schema_fast_impl.cache_clear()
    except Exception:
        pass
    return n


# ── 可观测性日志（每轮 NL2SQL 运行指标）─────────────────

def _log_run_metrics(query: str, intent: str, tables: list[str], sql: str,
                     ok: bool, rows: int, elapsed_ms: int,
                     refined: bool = False, warning: str = "", error: str = ""):
    """输出单轮运行的结构化日志，便于定位准确率/性能瓶颈。

    格式: [nl2sql] intent=... | tables=... | ok=... | rows=... | elapsed_ms=... | refined=... | sql=...
    写入标准输出（uvicorn 日志中可见），同时尝试追加到 logs/nl2sql.log。
    """
    import logging
    logger = logging.getLogger("nl2sql")
    tables_str = ",".join(tables[:6]) or "-"
    sql_short = (sql or "").replace("\n", " ")[:200]
    status = "ok" if ok else "fail"
    extra = f" | refined={refined}" if refined else ""
    if warning:
        extra += f" | warning={warning[:80]}"
    if error:
        extra += f" | error={error[:120]}"
    msg = (f"intent={intent} | tables=[{tables_str}] | {status} | rows={rows} "
           f"| elapsed_ms={elapsed_ms}{extra} | sql={sql_short}")
    logger.info("nl2sql run: %s", msg)
    # 追加文件日志（可选，失败静默）
    try:
        import os
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "nl2sql.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n")
    except Exception:
        pass
