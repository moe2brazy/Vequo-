"""指标语义层向量检索（蓝图第三层：给 LLM 装业务大脑）

把每个指标的语义文本（名称 + 别名 + 口径 + 维度 + 描述）向量化，用户提问时按需检索
最相关指标并注入 prompt（开卷考试）。与 memory.py 的 DDL/文档/SQL 向量检索互补，
但对象是「指标口径本身」——专门解决同义 / 换说法查询（如“做得好不好”→ 良率）靠
关键词子串匹配会漏掉的问题。

设计要点（零回归）：
- 复用 memory.py 的 _get_embedder / _vector_scores_numpy（同一套本地 embedding，零额外依赖）
- embedding 不可用时：调用方（get_metric_hint）自动退回纯关键词匹配，与改造前行为一致
- 指标新增/修改：由 main.py 的 /api/metrics 端点触发 rebuild() 即时生效（对齐蓝图“动态生效”）
- 向量库空时 retrieve 自动懒重建一次（首用即热，无需启动时预热）
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading

from agent.metric_registry import get_effective_metrics

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", ".agent_memory", "metric_memories.db")


def _metric_text(m: dict) -> str:
    """构造指标语义文本（中英文混合，利于本地 embedding 对齐同义表述）。"""
    parts: list[str] = [m.get("name", "")]
    parts += m.get("aliases", []) or []
    parts.append(m.get("description", "") or "")
    formula = m.get("formula", "") or m.get("sql_expression", "")
    if formula:
        parts.append(formula)
    dims = m.get("dims", []) or []
    if dims:
        parts.append("维度:" + " ".join(dims))
    return " | ".join(p for p in parts if p)


class MetricMemory:
    """指标口径向量库（SQLite 持久化，按 embedding blob 存储）。"""

    def __init__(self, db_path: str | os.PathLike | None = None):
        self.db_path = os.path.abspath(str(db_path or _DB_PATH))
        _dir = os.path.dirname(self.db_path)
        if _dir:
            os.makedirs(_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_table()
        self._embed_fn = None
        self._embed_tried = False
        self._embed_mode = None  # "local"/"api"/"ngram"/None（ngram 兜底不参与语义向量检索，见 retrieve）

    def _ensure_table(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metric_vectors(
                    metric_name TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    embedding BLOB
                )
                """
            )
            self._conn.commit()

    def _get_embed(self):
        if not self._embed_tried:
            try:
                from agent.memory import _get_embedder
                from agent.embeddings import get_embedding_mode
                self._embed_fn = _get_embedder()
                self._embed_mode = get_embedding_mode()
            except Exception:
                self._embed_fn = None
                self._embed_mode = None
            self._embed_tried = True
        return self._embed_fn

    def rebuild(self) -> dict:
        """全量重建指标向量库（指标增删改后调用）。返回 {rebuilt, embedded}。

        只索引「当前生效」的指标（get_effective_metrics），与 get_metric_hint 的
        关键词路径（find_metrics 也走 effective）保持一致——避免把已过期指标的口径
        注入 prompt / 白盒。
        """
        fn = self._get_embed()
        metrics = get_effective_metrics()
        saved = 0
        with self._lock:
            self._conn.execute("DELETE FROM metric_vectors")
            for m in metrics:
                text = _metric_text(m)
                vec = None
                if fn:
                    try:
                        vec = fn(text)
                    except Exception:
                        vec = None
                blob = json.dumps(vec).encode("utf-8") if vec is not None else None
                self._conn.execute(
                    "INSERT OR REPLACE INTO metric_vectors(metric_name, text, embedding) VALUES(?,?,?)",
                    (m["name"], text, blob),
                )
                saved += 1
            self._conn.commit()
        return {"rebuilt": saved, "embedded": fn is not None}

    # 相关性下限：combined 分数低于此值视为「不相关」直接丢弃，避免弱相关指标污染 prompt。
    # floor = max(ABS_FLOOR, top * REL_RATIO) → 既保证绝对下限，又保证只保留相对最强的若干项。
    ABS_FLOOR = 0.12
    REL_RATIO = 0.35

    def retrieve(self, query: str, top_k: int = 5, threshold: float | None = None,
                 with_scores: bool = False) -> list:
        """检索与 query 语义最相关的指标（混合向量 + BM25，带相关性下限）。

        返回：with_scores=False → 指标 dict 列表；True → [{"metric", "score"}]。
        库空 → 懒重建一次（首用即热）；库仍空 → 返回 []（调用方退回关键词）。

        设计（蓝图第三层「混合向量 + 关键词」）：
        - embedding 可用：combined = 0.65*向量余弦 + 0.35*BM25（各自归一化到 [0,1]），
          向量保证语义召回、BM25 保证精确词命中不被压过。
        - embedding 不可用：退化为纯 BM25（仍是真正的检索增强，非空操作）。
        - 相关性下限：combined <= 0 或不达 floor 的项一律丢弃，杜绝「只要有一点点词重叠
          就注入一堆弱相关指标」的 prompt 噪声与白盒误报。
        """
        fn = self._get_embed()
        with self._lock:
            rows = self._conn.execute(
                "SELECT metric_name, text, embedding FROM metric_vectors"
            ).fetchall()
        if not rows:
            # 懒重建一次（首用即热）
            self.rebuild()
            with self._lock:
                rows = self._conn.execute(
                    "SELECT metric_name, text, embedding FROM metric_vectors"
                ).fetchall()
            if not rows:
                return []
        by_name = {m["name"]: m for m in get_effective_metrics()}
        names = [r[0] for r in rows]
        texts = [r[1] for r in rows]
        n = len(rows)

        # ── 向量余弦（embedding 可用且确有向量时）──
        vec_scores = [0.0] * n
        use_vec = False
        # ngram 兜底向量仅对「精确/近精确重复」可靠，无法做语义近似匹配；其哈希碰撞会
        # 产生虚假低相似度，混入 combined 后会绕过相关性下限过滤（"东西合格的比例"这类
        # 模糊查询会被误召回为"质检不合格率"）。故 ngram 模式下关闭向量路径，回退纯 BM25。
        vec_eligible = fn is not None and self._embed_mode != "ngram"
        if vec_eligible:
            try:
                qv = fn(query)
                valid_idx: list[int] = []
                vecs: list = []
                for i, r in enumerate(rows):
                    if r[2]:
                        try:
                            vecs.append(json.loads(r[2]))
                            valid_idx.append(i)
                        except Exception:
                            pass
                if vecs:
                    from agent.memory import _vector_scores_numpy
                    sims = _vector_scores_numpy(qv, vecs)
                    full = [0.0] * n
                    for j, i in enumerate(valid_idx):
                        full[i] = max(0.0, sims[j])  # 余弦可能为负，截断到 0
                    vec_scores = full
                    use_vec = any(v > 0 for v in vec_scores)
            except Exception:
                use_vec = False

        # ── BM25 词重叠 ──
        from agent.memory import _bm25_scores
        bm25 = _bm25_scores(query, texts)

        # ── 混合（绝对强度，不做 max 归一化）──
        # 历史 bug：先对 vec/bm25 各自 max 归一化，再算相关性下限——max 归一化会把
        # 「最强项」恒定为 1.0，任何微弱相似（甚至哈希碰撞噪声）都会被放大并通过下限，
        # 导致「无词重叠」的无关查询（如"qwezxc随机串无意义"）也被误召回。
        # 修复：用绝对强度参与下限判定。向量余弦天然 ∈ [0,1]；BM25 无上界，用固定
        # 饱和值映射到 [0,1]（保留绝对强度，多关键词强命中≈1.0）。
        bm25_scaled = [min(1.0, s / 8.0) for s in bm25]
        if use_vec:
            combined = [0.65 * max(0.0, v) + 0.35 * b for v, b in zip(vec_scores, bm25_scaled)]
        else:
            combined = bm25_scaled

        # ── 相关性下限过滤（绝对阈值，杜绝噪声放大）──
        top = max(combined) if combined else 0.0
        floor = threshold if threshold is not None else max(self.ABS_FLOOR, top * self.REL_RATIO)
        scored: list[tuple[dict, float]] = []
        for i in range(n):
            if combined[i] > 0 and combined[i] >= floor:
                m = by_name.get(names[i])
                if m:
                    scored.append((m, combined[i]))
        scored.sort(key=lambda x: x[1], reverse=True)

        if with_scores:
            return [{"metric": m, "score": round(s, 4)} for m, s in scored[:top_k]]
        return [m for m, _ in scored[:top_k]]


# ── 全局单例 ────────────────────────────────────────────
_mm: "MetricMemory | None" = None
_mm_lock = threading.Lock()


def get_metric_memory() -> "MetricMemory":
    global _mm
    if _mm is None:
        with _mm_lock:
            if _mm is None:  # 双检锁：防多线程首次并发
                _mm = MetricMemory()
    return _mm
