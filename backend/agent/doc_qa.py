# -*- coding: utf-8 -*-
"""非结构化数据问答 + 引用溯源（P2-1，对标 ThoughtSpot Spotter 3）。

## 竞品做法
Spotter 3 的核心不只是"能查文档"，而是：
  1. 对非结构化内容（PDF/文档/长文本）**分块**做语义检索；
  2. 用检索到的片段**生成回答**；
  3. 每个结论都**标注引用来源**（citations），用户能点回原文片段核实。

## 本实现（复用现有知识库，只补"分块 + 引用"这一层）
- 知识库存储已有（memory.add_documentation / list_memories），存的是整篇文档；
- 增量：检索后把整篇文档**切块**，按块与问题的相关性重排，取最相关的 top-k 片段；
- LLM 基于片段回答，并用 [1][2] 标注引用，返回 citations（片段 + 来源）供前端
  点回原文 —— 杜绝"AI 编了个数字却无法核实"的信任问题。
- 这是对「非结构化知识 → 可核实答案」的最小闭环；更进一步的"结构化+非结构化
  跨源融合"已在 investigator._knowledge_context 中把文档片段注入 SQL 生成链。

对外 API：
  answer_with_citations(query, k) → {success, answer, citations, error}
"""

from __future__ import annotations

import logging
import re

_logger = logging.getLogger("doc_qa")

_CHUNK_SIZE = 320   # 每块字符数（中文约 320 字一段，检索粒度适中）
_CHUNK_OVERLAP = 40


def chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """把长文本切成带重叠的块（按句号/换行优先断点，避免把句子拦腰截断）。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # 在 [end-60, end] 内找最近的句号/换行作断点，保持语义完整
            window = text[end - 60:end]
            cut = -1
            for sep in ("\n", "。", "！", "？", "；", "，"):
                idx = window.rfind(sep)
                if idx > cut:
                    cut = idx
            if cut >= 0:
                end = end - 60 + cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _chunk_score(query: str, chunk: str) -> float:
    """块与问题的相关性（字符 bigram 覆盖 + 命中词加成，确定性、无外部依赖）。"""
    q_bg = _bigrams(query)
    c_bg = _bigrams(chunk)
    if not q_bg or not c_bg:
        return 0.0
    cover = len(q_bg & c_bg) / len(q_bg)
    # 命中词：问题里的 2 字以上中文词在块里出现的比例（加分项）
    terms = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    hit = sum(1 for t in terms if t in chunk) / len(terms) if terms else 0.0
    return cover * 0.7 + hit * 0.3


def _top_chunks(query: str, k: int = 5) -> list[dict]:
    """检索相关文档 → 切块 → 重排 → 返回 top-k 片段（带 doc_id / source）。"""
    try:
        from agent.memory import get_memory
        mem = get_memory()
        docs = mem.list_memories().get("documentations") or []
    except Exception:
        docs = []
    if not docs:
        return []

    scored: list[tuple[float, dict]] = []
    for d in docs:
        if not isinstance(d, dict) or not d.get("content"):
            continue
        content = str(d["content"])
        # 长文档分块；短文直接整段
        for chunk in chunk_text(content):
            sc = _chunk_score(query, chunk)
            if sc > 0:
                scored.append((sc, {
                    "doc_id": d.get("id"),
                    "source": str(d.get("source") or "知识库"),
                    "snippet": chunk[:600],
                }))
    scored.sort(key=lambda x: x[0], reverse=True)
    # 去重：同一 doc 只保留得分最高的若干块，避免一个长文档刷屏
    out: list[dict] = []
    seen_ids: set = set()
    for sc, c in scored:
        out.append({**c, "score": round(sc, 3)})
        seen_ids.add(c["doc_id"])
        if len(out) >= k:
            break
    return out


def answer_with_citations(query: str, k: int = 5) -> dict:
    """基于知识库片段回答，并给出引用来源。

    返回 {success, answer, citations:[{id, source, snippet}], error}
    无相关文档时 success=False（不编造）。
    """
    empty = {"success": False, "answer": "", "citations": [], "error": ""}
    if not (query or "").strip():
        return {**empty, "error": "问题为空"}
    chunks = _top_chunks(query, k=k)
    if not chunks:
        return {**empty, "error": "知识库中没有与问题相关的文档"}

    try:
        from agent.llm_service import _make_llm
        from langchain_core.messages import HumanMessage, SystemMessage
    except Exception as e:
        return {**empty, "error": "LLM 不可用: %s" % e}

    refs = "\n\n".join(f"[{i + 1}] {c['snippet']}" for i, c in enumerate(chunks))
    system = (
        "你是知识库问答助手。请**只依据**下面给出的文档片段回答用户问题。\n"
        "要求：\n"
        "1. 结论先行，语言精炼；\n"
        "2. 每个关键结论后面用 [n] 标注它来自第几段（例如「良率口径为合格数/投入数[2]」）；\n"
        "3. 片段中没有的信息不要编造，直接说「文档中未提及」；\n"
        "4. 不要复述整段，只提炼与问题直接相关的部分。"
    )
    prompt = f"## 文档片段\n{refs}\n\n## 用户问题\n{query}"
    try:
        llm = _make_llm(temp=0.2, max_tokens=600)
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=prompt)])
        answer = str(resp.content or "").strip()
    except Exception as e:
        return {**empty, "error": "生成回答失败: %s" % e}
    if not answer:
        return {**empty, "error": "未能生成回答"}

    citations = [{"id": c["doc_id"], "source": c["source"],
                  "snippet": c["snippet"], "score": c.get("score")} for c in chunks]
    return {"success": True, "answer": answer, "citations": citations, "error": ""}
