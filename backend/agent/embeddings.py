"""本地 Embedding 提供者 — 解决原 RAG 依赖外部 embedding 接口的问题

策略（按优先级尝试，绝不阻塞主流程）：
1. sentence-transformers 本地模型（默认 BAAI/bge-small-zh-v1.5，离线可用）
2. OpenAI 兼容接口（独立环境变量 EMBEDDING_API_URL/KEY/MODEL 优先；
   未配置时回退 LLM_CONFIG 的 api_key/base_url + embedding_model）
3. 零依赖字符 n-gram 哈希向量兜底（保证语义缓存不因 embedding 缺失而整体禁用，
   重复问题 100% 命中；真 embedding 可用时自动优先）

说明：bge-small-zh-v1.5 约 100MB，首次使用自动下载（HuggingFace）；
设置环境变量 EMBEDDING_MODEL 可指定其它模型；EMBEDDING_DISABLE=1 强制禁用。
独立 API 配置示例（如硅基流动）：
  EMBEDDING_API_URL=https://api.siliconflow.cn/v1
  EMBEDDING_API_KEY=sk-xxx
  EMBEDDING_API_MODEL=BAAI/bge-m3
"""

import os
import logging
import threading

_logger = logging.getLogger("embeddings")

_EMBED_FN = None
_TRIED = False
# 当前生效的 embedding 提供者：None=不可用 / "local"=sentence-transformers / "api"=OpenAI 兼容 / "ngram"=字符哈希兜底
_EMBED_MODE = None
# 双检锁：防止并发首次调用重复加载 100MB 本地模型（竞态）
_embed_lock = threading.Lock()


def get_embedding_mode() -> str | None:
    """返回当前 embedding 提供者类型（供调用方判断向量质量，决定是否启用向量检索路径）。

    ngram 兜底仅对「精确/近精确重复」可靠，无法做语义近似匹配，其哈希碰撞会产生
    虚假低相似度；调用方（如 metric_memory）据此关闭向量路径，回退纯关键词检索。
    """
    return _EMBED_MODE


def _build_sentence_transformers_fn(model_name: str):
    """构建本地 sentence-transformers embedding 函数（懒加载模型）"""
    from sentence_transformers import SentenceTransformer

    _model = SentenceTransformer(model_name)

    def _encode(text: str) -> list[float]:
        vec = _model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec]

    return _encode


def _build_openai_fn():
    """构建 OpenAI 兼容接口 embedding 函数。

    优先独立环境变量 EMBEDDING_API_*（推荐：LLM 接口通常无 embedding 能力）；
    未配置时回退 LLM_CONFIG（需显式配置 embedding_model 才可用）。
    """
    from langchain_openai import OpenAIEmbeddings

    api_url = os.getenv("EMBEDDING_API_URL")
    api_key = os.getenv("EMBEDDING_API_KEY")
    api_model = os.getenv("EMBEDDING_API_MODEL")
    if api_url and api_key:
        _emb = OpenAIEmbeddings(
            model=api_model or "BAAI/bge-m3",
            api_key=api_key,
            base_url=api_url,
        )
    else:
        from config import LLM_CONFIG
        _emb = OpenAIEmbeddings(
            model=LLM_CONFIG.get("embedding_model", "text-embedding-3-small"),
            api_key=LLM_CONFIG["api_key"],
            base_url=LLM_CONFIG["base_url"],
        )

    def _encode(text: str) -> list[float]:
        vec = _emb.embed_query(text)
        return [float(x) for x in vec]

    return _encode


def _build_ngram_fn():
    """零依赖字符 n-gram 哈希向量（真 embedding 不可用时的兜底，保证语义缓存可用）。

    语义缓存依赖 embedding 做相似度检索；若本地模型/API 均不可用而返回 None，
    整个语义缓存会被禁用（历史教训：日志 0 命中、12.8s 重复查询无缓存兜底）。
    本函数先用停用词归一化（"各产线的产量"与"每个产线的产量"→"产线产量"），
    再按与 memory._tokenize 一致的切分（英文词 + 中文连续段 2-gram）取 md5 哈希桶向量：
    - 相同/仅措辞差异的问题 → 归一化后向量相同 → 余弦 1.0 → 命中
    - 语义不相关问题 → 无任何共享 token → 余弦精确为 0，不误命中

    关键设计（修复历史 bug）：早期版本用「全串 unigram+bigram + 256 维」，
    单字（如"机"）与哈希碰撞会产生约 0.2~0.3 的虚假相似度，导致「无词重叠」的
    无关查询（如"qwezxc随机串无意义"）也被指标向量库误召回。改为：
    1) 切分与 BM25 完全一致（英文词 + 中文连续段 2-gram，不含单字/跨语言边界 bigram）；
    2) 维度提到 4096 消除哈希碰撞噪声 → 无共享 token 时余弦精确为 0。
    真 embedding（本地模型或 API）可用时自动优先，本函数仅作兜底。
    """
    import hashlib
    import re as _re
    DIM = 4096
    # 中文问析停用词（单字集合：语气/虚词/通用查询动词，过滤后保留业务语义词）
    _STOP = frozenset(
        "的了呢吗啊吧么是在各每个和与及或对把被要想看查请问多少怎样如何怎么分别按分汇总统计分析展示显示给我一下"
        "哪些什么哪个目前当前现在最近所有全部一共总共情况数据请帮我我们你们它它们这那这些那些"
    )

    def _normalize(text: str) -> str:
        # 去标点/空白（含全角问号、空格等），保留中文/字母数字；再过滤停用词
        t = (text or "").strip().lower()
        t = "".join(ch for ch in t if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
        return "".join(ch for ch in t if ch not in _STOP)

    def _tokens(text: str) -> set[str]:
        """与 memory._tokenize 一致的切分：英文按词、中文按连续段的 2-gram（无单字、无跨语言边界）。"""
        tokens: set[str] = set()
        for w in _re.findall(r"[a-z][a-z0-9_]*", text):
            tokens.add(w)
        for seg in _re.findall(r"[\u4e00-\u9fff]+", text):
            if len(seg) <= 2:
                tokens.add(seg)
            else:
                for i in range(len(seg) - 1):
                    tokens.add(seg[i:i + 2])
        return tokens

    def _encode(text: str) -> list[float]:
        vec = [0.0] * DIM
        for g in _tokens(_normalize(text)):
            h = int(hashlib.md5(g.encode("utf-8")).hexdigest()[:8], 16)
            vec[h % DIM] += 1.0
        norm = (sum(x * x for x in vec)) ** 0.5 or 1.0
        return [x / norm for x in vec]

    return _encode


def get_embedding_fn():
    """获取 embedding 函数；不可用时返回 None（调用方自行降级）"""
    global _EMBED_FN, _TRIED, _EMBED_MODE
    if _TRIED:
        return _EMBED_FN
    with _embed_lock:
        # 双检：等锁期间可能已被其他线程初始化
        if _TRIED:
            return _EMBED_FN
        _TRIED = True

        if os.getenv("EMBEDDING_DISABLE", "0") == "1":
            _EMBED_MODE = None
            return None

        # 1. 本地 sentence-transformers（首选，离线可用）
        # 2026-10-01 可靠性修复：默认值原为 HF hub ID——离线/无网演示机上
        # SentenceTransformer 会尝试联网下载并长时间挂住，最终静默降级为哈希向量。
        # 仓库里本来就有 backend/models/bge-small-zh-v1.5/（93MB），默认改用本地目录，
        # 只有显式配置 EMBEDDING_MODEL 时才覆盖。
        _default_model = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      "models", "bge-small-zh-v1.5")
        model_name = os.getenv("EMBEDDING_MODEL", _default_model)
        try:
            _EMBED_FN = _build_sentence_transformers_fn(model_name)
            _EMBED_MODE = "local"
            return _EMBED_FN
        except Exception as e:
            _logger.warning("本地模型(%s)不可用，尝试 API 接口: %s: %s", model_name, type(e).__name__, e)

        # 2. OpenAI 兼容接口（独立 EMBEDDING_API_* 配置，或 LLM_CONFIG 配了 embedding_model）
        try:
            if os.getenv("EMBEDDING_API_URL") and os.getenv("EMBEDDING_API_KEY"):
                _EMBED_FN = _build_openai_fn()
                _EMBED_MODE = "api"
                return _EMBED_FN
            from config import LLM_CONFIG
            if LLM_CONFIG.get("embedding_model"):
                _EMBED_FN = _build_openai_fn()
                _EMBED_MODE = "api"
                return _EMBED_FN
        except Exception as e:
            _logger.warning("API 接口不可用，降级为本地哈希向量: %s: %s", type(e).__name__, e)

        # 3. 零依赖 n-gram 哈希向量兜底（保证语义缓存可用，不因 embedding 缺失整体禁用）
        _logger.info("使用零依赖字符哈希向量兜底（重复问题可命中；近似命中率低于真 embedding）")
        _EMBED_FN = _build_ngram_fn()
        _EMBED_MODE = "ngram"
        return _EMBED_FN
