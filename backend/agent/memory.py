"""Vanna 风格 Agent 记忆系统（Train → Retrieve → Generate → Feedback 闭环核心）

三类记忆（对齐 Vanna 的 DDL / Documentation / Question-SQL Pairs）：
1. DDL 记忆      — 每张表的建表结构（自动从 information_schema 提取）
2. 文档记忆      — 业务口径、表/字段中文含义、行业知识（从元数据自动提取 + 用户可添加）
3. SQL 示例记忆  — 成功的 (用户问题 → SQL) 对（自动保存 + 用户反馈确认保存）

存储：SQLite（backend/.agent_memory/agent_memory.db）
- 按 db_key(db_type+host+port+name) 隔离，切换数据库后记忆自动切换
- 检索：关键词相似度（Jaccard + difflib，对齐 Vanna DemoAgentMemory）
        + 可选向量检索（本地 sentence-transformers 可用时增强排序）
"""

import json
import os
import re
import sqlite3
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

from database import get_database_config, get_db_type, quote_ident
from db.executor import execute_sql

# ── 记忆库位置 ──────────────────────────────────────────
MEMORY_DIR = Path(__file__).resolve().parent.parent / ".agent_memory"
MEMORY_DB = MEMORY_DIR / "agent_memory.db"


def _current_db_key() -> str:
    """当前数据库唯一标识（用于记忆隔离）"""
    cfg = get_database_config()
    return f"{cfg.get('db_type', 'pg')}:{cfg.get('host', '')}:{cfg.get('port', '')}:{cfg.get('name', '')}"


# ── 文本相似度（无 embedding 时的检索核心，对齐 Vanna 的内存实现）──

def _tokenize(text: str) -> set[str]:
    """中英文混合分词：英文按词、中文按字+常用词"""
    text = str(text).lower()
    tokens = set()
    # 英文单词
    for w in re.findall(r"[a-z][a-z0-9_]*", text):
        tokens.add(w)
    # 中文：2-gram
    zh = re.findall(r"[\u4e00-\u9fff]+", text)
    for seg in zh:
        if len(seg) <= 2:
            tokens.add(seg)
        else:
            for i in range(len(seg) - 1):
                tokens.add(seg[i:i + 2])
    return tokens


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _text_similarity(query: str, candidate: str) -> float:
    """混合相似度：Jaccard + difflib 序列匹配，取加权"""
    q_tokens = _tokenize(query)
    c_tokens = _tokenize(candidate)
    jac = _jaccard(q_tokens, c_tokens)
    seq = SequenceMatcher(None, query.lower(), candidate.lower()).ratio()
    # 中文场景 difflib 更好，英文/标识符场景 Jaccard 更好
    return 0.45 * jac + 0.55 * seq


def _hybrid_score(query: str, candidate: str) -> float:
    """检索评分：相似度 + token 命中率（用于 query 短、候选长的场景）
    候选文本包含用户问题的任意关键词 → 强相关，避免中文长文本相似度被稀释。
    """
    base = _text_similarity(query, candidate)
    q_tokens = _tokenize(query)
    if not q_tokens:
        return base
    cand_lower = candidate.lower()
    hits = sum(1 for t in q_tokens if t in cand_lower)
    hit_ratio = hits / len(q_tokens)
    return max(base, 0.35 * hit_ratio + 0.2 * base)


# ── 可选向量检索 ────────────────────────────────────────

def _get_embedder():
    """获取本地 embedding 函数；不可用返回 None（不阻塞主流程）"""
    try:
        from agent.embeddings import get_embedding_fn
        return get_embedding_fn()
    except Exception:
        return None


def _vector_scores_numpy(query_vec: list, vecs: list) -> list[float]:
    """numpy 矩阵点积计算 query_vec 与每条记忆向量的余弦相似度（P0-1 检索加速）。

    替代逐条 Python 的 `_cosine` 循环：一次矩阵乘法在 BLAS 内完成，
    记忆条数上万时首包延迟显著下降。vecs 与记忆行对齐；长度不符/空 → 该项 0.0。
    """
    try:
        import numpy as np
    except Exception:
        return [0.0] * len(vecs)
    q = np.asarray(query_vec, dtype=np.float32)
    qn = float(np.linalg.norm(q))
    if qn == 0 or not vecs:
        return [0.0] * len(vecs)
    q = q / qn
    scores = [0.0] * len(vecs)
    valid_idx: list[int] = []
    mat_rows: list[list] = []
    for i, v in enumerate(vecs):
        if v is not None and len(v) == len(query_vec):
            valid_idx.append(i)
            mat_rows.append(v)
    if not mat_rows:
        return scores
    M = np.asarray(mat_rows, dtype=np.float32)
    M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    sims = (M @ q).astype(float)
    for j, i in enumerate(valid_idx):
        scores[i] = float(sims[j])
    return scores


def _bm25_scores(query: str, texts: list[str]) -> list[float]:
    """BM25 稀疏关键词打分（中文 2-gram / 英文词），IDF 按语料统计。

    比 `_text_similarity` 的 difflib 更快（无序列对齐）、排序更稳（关键词 IDF 加权）。
    供混合检索（向量 + 关键词）与无 embedding 时的降级使用。
    """
    import math
    if not texts:
        return []
    q_tokens = _tokenize(query)
    if not q_tokens:
        return [0.0] * len(texts)
    doc_tokens = [_tokenize(t) for t in texts]
    n = len(texts)
    df: dict[str, int] = {}
    for dt in doc_tokens:
        for tok in dt:
            df[tok] = df.get(tok, 0) + 1
    avg_len = sum(len(dt) for dt in doc_tokens) / max(n, 1)
    k1, b = 1.5, 0.75
    out: list[float] = []
    for dt in doc_tokens:
        s = 0.0
        dl = len(dt)
        for tok in q_tokens:
            if tok not in dt:
                continue
            idf = math.log((n - df[tok] + 0.5) / (df[tok] + 0.5) + 1.0)
            s += idf * (k1 + 1) / (1 + k1 * (1 - b + b * dl / avg_len))
        out.append(s)
    return out


# ── DDL 提取（自动训练数据源 1）─────────────────────────

def _list_business_tables() -> list[str]:
    """列出当前库业务表：优先 public schema，否则全部非系统 schema。
    非 public 表返回 'schema.table' 形式（如 factory.attendance）。
    """
    try:
        if get_db_type() == "mysql":
            r = execute_sql(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_type='BASE TABLE' ORDER BY table_name"
            )
            if r["success"] and r["rows"]:
                return [row["table_name"] for row in r["rows"]]
        else:
            r = execute_sql(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_type='BASE TABLE' "
                "ORDER BY (table_schema='public') DESC, table_schema, table_name"
            )
            if r["success"] and r["rows"]:
                refs = []
                for row in r["rows"]:
                    refs.append(row["table_name"] if row["table_schema"] == "public"
                                else f"{row['table_schema']}.{row['table_name']}")
                return refs
    except Exception:
        pass
    return []


def _split_table_ref(table_ref: str) -> tuple[str, str]:
    """'schema.table' → (schema, table)；'table' → (public, table)"""
    if "." in table_ref:
        schema, table = table_ref.split(".", 1)
        return schema, table
    return "public", table_ref


def _fetch_table_ddl(table_ref: str) -> str:
    """从 information_schema 提取单表 DDL（PG/MySQL 通用，支持任意 schema）"""
    schema, table = _split_table_ref(table_ref)
    # 标识符校验：表名/schema 只允许字母数字下划线，杜绝 f-string 拼接注入
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table or ""):
        return ""
    if schema != "public" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
        return ""
    try:
        if get_db_type() == "mysql":
            r = execute_sql(
                f"SELECT column_name, data_type, is_nullable, "
                f"CASE WHEN column_key='PRI' THEN 'PRIMARY KEY' ELSE '' END AS key_info "
                f"FROM information_schema.columns "
                f"WHERE table_name='{table}' AND table_schema=DATABASE() "
                f"ORDER BY ordinal_position"
            )
        else:
            r = execute_sql(
                f"SELECT c.column_name, c.data_type, c.is_nullable, "
                f"CASE WHEN tc.constraint_type='PRIMARY KEY' AND kcu.column_name IS NOT NULL THEN 'PRIMARY KEY' ELSE '' END AS key_info "
                f"FROM information_schema.columns c "
                f"LEFT JOIN information_schema.table_constraints tc "
                f"  ON c.table_name=tc.table_name AND c.table_schema=tc.table_schema "
                f"  AND tc.constraint_type='PRIMARY KEY' "
                f"LEFT JOIN information_schema.key_column_usage kcu "
                f"  ON tc.constraint_name=kcu.constraint_name AND tc.constraint_schema=kcu.constraint_schema "
                f"  AND c.column_name=kcu.column_name "
                f"WHERE c.table_name='{table}' AND c.table_schema='{schema}' "
                f"ORDER BY c.ordinal_position"
            )
        if not r["success"] or not r["rows"]:
            return ""
        cols = []
        for row in r["rows"]:
            col_def = f"{quote_ident(row['column_name'])} {row['data_type']}"
            if row.get("key_info"):
                col_def += " " + row["key_info"]
            cols.append("  " + col_def)
        # 表名引用：public 省略 schema；非 public 用 schema.table 分别加引号
        # 注意不能写成 "schema.table"（会被当成带点的表名），必须 "schema"."table"
        if schema == "public":
            q_table = quote_ident(table)
        else:
            q_table = f"{quote_ident(schema)}.{quote_ident(table)}"
        return f"CREATE TABLE {q_table} (\n" + ",\n".join(cols) + "\n)"
    except Exception:
        return ""


# ── AgentMemory 核心类 ──────────────────────────────────

class AgentMemory:
    """训练记忆存储 + 检索（SQLite，按数据库隔离）"""

    def __init__(self, db_path: str | os.PathLike | None = None):
        self.db_path = str(db_path or MEMORY_DB)
        _dir = os.path.dirname(self.db_path)
        if _dir:  # 裸文件名（如 "mem.db"）无目录，makedirs("") 会抛 FileNotFoundError
            os.makedirs(_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_tables()
        # embedding 懒加载
        self._embed_fn = None
        self._embed_tried = False

    # ── 基础 ──
    def _ensure_tables(self) -> None:
        with self._lock:
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sql_memories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                db_key TEXT NOT NULL,
                question TEXT NOT NULL,
                sql TEXT NOT NULL,
                table_name TEXT DEFAULT '',
                source TEXT DEFAULT 'auto',
                success INTEGER DEFAULT 1,
                embedding BLOB,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_sql_db ON sql_memories(db_key);
            CREATE TABLE IF NOT EXISTS documentations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                db_key TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT DEFAULT 'auto',
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_doc_db ON documentations(db_key);
            CREATE TABLE IF NOT EXISTS ddl_memories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                db_key TEXT NOT NULL,
                table_name TEXT NOT NULL,
                ddl TEXT NOT NULL,
                created_at REAL,
                UNIQUE(db_key, table_name)
            );
            """)
            self._conn.commit()

    def _get_embed(self):
        if not self._embed_tried:
            self._embed_fn = _get_embedder()
            self._embed_tried = True
        return self._embed_fn

    def _embed_text(self, text: str):
        fn = self._get_embed()
        if fn is None:
            return None
        try:
            vec = fn(text)
            return json.dumps(vec).encode("utf-8")
        except Exception:
            return None

    def _vec_to_list(self, blob) -> list | None:
        if not blob:
            return None
        try:
            return json.loads(blob.decode("utf-8"))
        except Exception:
            return None

    @staticmethod
    def _cosine(a: list, b: list) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    # ── DDL 记忆 ────────────────────────────────────────
    def rebuild_ddl(self, tables: list[str] | None = None) -> dict:
        """从当前数据库自动提取所有表 DDL 并入库（Vanna 的 train(ddl) 自动版）"""
        db_key = _current_db_key()
        with self._lock:
            self._conn.execute("DELETE FROM ddl_memories WHERE db_key=?", (db_key,))
        if tables is None:
            tables = _list_business_tables()
        saved, failed = 0, 0
        for t in tables:
            try:
                ddl = _fetch_table_ddl(t)
                if ddl:
                    with self._lock:
                        self._conn.execute(
                            "INSERT OR REPLACE INTO ddl_memories(db_key, table_name, ddl, created_at) VALUES(?,?,?,?)",
                            (db_key, t, ddl, time.time()),
                        )
                    saved += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        with self._lock:
            self._conn.commit()
        return {"saved": saved, "failed": failed, "db": db_key}

    def get_ddl(self, table_name: str) -> str:
        db_key = _current_db_key()
        with self._lock:
            row = self._conn.execute(
                "SELECT ddl FROM ddl_memories WHERE db_key=? AND table_name=?",
                (db_key, table_name),
            ).fetchone()
        return row[0] if row else ""

    def get_ddl_by_keywords(self, query: str, k: int = 4) -> list[dict]:
        """按问题关键词检索相关表 DDL（表名/中文别名/关键词命中优先）"""
        db_key = _current_db_key()
        with self._lock:
            rows = self._conn.execute(
                "SELECT table_name, ddl FROM ddl_memories WHERE db_key=?", (db_key,)
            ).fetchall()
        if not rows:
            return []
        scored = []
        for table_name, ddl in rows:
            score = _hybrid_score(query, table_name + " " + ddl[:300])
            # 表名直接出现在问题中 → 强加分
            if table_name.lower() in query.lower():
                score += 0.5
            scored.append({"table_name": table_name, "ddl": ddl, "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return [{"table_name": s["table_name"], "ddl": s["ddl"], "score": round(s["score"], 4)} for s in scored[:k]]

    # ── 文档记忆 ────────────────────────────────────────
    def add_documentation(self, content: str, source: str = "user") -> int:
        """添加业务文档/口径（Vanna 的 train(documentation)）"""
        content = content.strip()
        if not content:
            return 0
        db_key = _current_db_key()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO documentations(db_key, content, source, created_at) VALUES(?,?,?,?)",
                (db_key, content, source, time.time()),
            )
            self._conn.commit()
            return cur.lastrowid

    def search_documentation(self, query: str, k: int = 4) -> list[str]:
        """检索相关业务文档"""
        db_key = _current_db_key()
        with self._lock:
            rows = self._conn.execute(
                "SELECT content FROM documentations WHERE db_key=?", (db_key,)
            ).fetchall()
        if not rows:
            return []
        scored = [(content, _hybrid_score(query, content)) for (content,) in rows]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [c for c, s in scored if s > 0.05][:k]

    # ── SQL 示例记忆 ────────────────────────────────────
    def add_sql(self, question: str, sql: str, table_name: str = "", source: str = "auto") -> int:
        """保存一条 (问题→SQL) 示例（Vanna 的 train(question, sql) / auto_train）
        重复问题更新而非新增，避免示例膨胀。
        """
        question = question.strip()
        sql = sql.strip()
        if not question or not sql:
            return 0
        db_key = _current_db_key()
        embed = self._embed_text(question)
        with self._lock:
            # 同库同问题 → 更新（保留最新成功 SQL）
            existing = self._conn.execute(
                "SELECT id FROM sql_memories WHERE db_key=? AND question=?", (db_key, question)
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE sql_memories SET sql=?, table_name=?, source=?, success=1, embedding=?, created_at=? WHERE id=?",
                    (sql, table_name, source, embed, time.time(), existing[0]),
                )
                self._conn.commit()
                return existing[0]
            cur = self._conn.execute(
                "INSERT INTO sql_memories(db_key, question, sql, table_name, source, success, embedding, created_at) "
                "VALUES(?,?,?,?,?,1,?,?)",
                (db_key, question, sql, table_name, source, embed, time.time()),
            )
            self._conn.commit()
            return cur.lastrowid

    def search_sql(self, query: str, k: int = 3, threshold: float = 0.1) -> list[dict]:
        """检索相似 (问题→SQL) 示例 — 关键词预筛 ∪ 向量召回，再混合打分

        历史问题：向量只给「关键词已入选」的候选重排 → 近义问法（"产线不良排行" vs
        "各产线的不良数量"）关键词分数为 0，根本进不了候选池，语义召回名存实亡。
        改为：先算全量向量分，候选 = 关键词过阈 ∪ 向量过阈，保留原混合打分。
        仅服务于 LLM 生成 SQL 链路（修复/多候选/推断），不参与确定性编译路径。
        """
        db_key = _current_db_key()
        with self._lock:
            rows = self._conn.execute(
                # 不做 LIMIT 500 预筛：否则单库记忆超过 500 条后最早的示例永不被检索
                "SELECT id, question, sql, table_name, embedding FROM sql_memories "
                "WHERE db_key=? AND success=1 ORDER BY created_at DESC",
                (db_key,),
            ).fetchall()

        if not rows:
            return []

        # 向量召回准备：全量计算一次向量分（numpy 矩阵，行数上限为单库记忆数）
        _vec_by_id: dict = {}
        q_vec = None
        fn = self._get_embed()
        if fn is not None:
            try:
                q_vec = fn(query)
            except Exception:
                q_vec = None
        if q_vec is not None:
            try:
                _vs = _vector_scores_numpy(
                    q_vec, [self._vec_to_list(r[4]) for r in rows])
                _vec_by_id = {r[0]: float(s) for r, s in zip(rows, _vs)}
            except Exception:
                _vec_by_id = {}
        # bge-small-zh-v1.5 中文实测：同义 0.85+，业务相关 0.55~0.7，无关 <0.45
        _VEC_THRESHOLD = 0.55

        # 候选 = 关键词过阈 ∪ 向量过阈
        scored = []
        for rid, question, sql, table_name, emb in rows:
            kw_score = _text_similarity(query, question)
            vec_score = _vec_by_id.get(rid, 0.0)
            if kw_score >= threshold or (q_vec is not None and vec_score >= _VEC_THRESHOLD):
                scored.append({"id": rid, "question": question, "sql": sql,
                               "table_name": table_name, "embedding": emb,
                               "kw": kw_score, "vec": vec_score})

        # 混合打分（向量可用时 关键词:向量 = 1:1，否则纯关键词）
        if q_vec is not None:
            for item in scored:
                item["score"] = 0.5 * item["kw"] + 0.5 * item.get("vec", 0.0)
        else:
            for item in scored:
                item["score"] = item["kw"]
        for item in scored:
            item.pop("embedding", None)

        scored.sort(key=lambda x: x["score"], reverse=True)
        return [
            {"id": s["id"], "question": s["question"], "sql": s["sql"],
             "table_name": s["table_name"], "score": round(s["score"], 4)}
            for s in scored[:k]
        ]

    def retrieve(self, query: str, k_ddl: int = 4, k_docs: int = 4, k_sql: int = 3) -> dict:
        """统一检索入口（P0-1）：一次返回 DDL / 文档 / 相似 SQL 三类记忆。

        与分散的 get_ddl_by_keywords / search_documentation / search_sql 等价，
        供 llm_service 后续收敛为「只调这一入口」；内部可切换索引后端而不改调用方。
        """
        return {
            "ddl": self.get_ddl_by_keywords(query, k=k_ddl),
            "docs": self.search_documentation(query, k=k_docs),
            "sql_examples": self.search_sql(query, k=k_sql),
        }

    def list_memories(self, limit: int = 50) -> dict:
        """列出当前库全部记忆（管理接口用）"""
        db_key = _current_db_key()
        with self._lock:
            sqls = self._conn.execute(
                "SELECT id, question, sql, table_name, source, created_at FROM sql_memories "
                "WHERE db_key=? ORDER BY created_at DESC LIMIT ?", (db_key, limit)
            ).fetchall()
            docs = self._conn.execute(
                "SELECT id, content, source, created_at FROM documentations "
                "WHERE db_key=? ORDER BY created_at DESC LIMIT ?", (db_key, limit)
            ).fetchall()
            ddl_rows = self._conn.execute(
                "SELECT table_name, created_at FROM ddl_memories WHERE db_key=? ORDER BY table_name", (db_key,)
            ).fetchall()
        return {
            "db_key": db_key,
            "sql_examples": [
                {"id": r[0], "question": r[1], "sql": r[2], "table_name": r[3], "source": r[4],
                 "created_at": r[5]} for r in sqls
            ],
            "documentations": [
                {"id": r[0], "content": r[1], "source": r[2], "created_at": r[3]} for r in docs
            ],
            "ddl_tables": [r[0] for r in ddl_rows],
        }

    def delete_memory(self, memory_id: int) -> bool:
        """删除记忆（优先按 sql_memories 找，其次 documentations）"""
        db_key = _current_db_key()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sql_memories WHERE id=? AND db_key=?", (memory_id, db_key)
            )
            if cur.rowcount:
                self._conn.commit()
                return True
            cur = self._conn.execute(
                "DELETE FROM documentations WHERE id=? AND db_key=?", (memory_id, db_key)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_stats(self) -> dict:
        db_key = _current_db_key()
        with self._lock:
            n_sql = self._conn.execute(
                "SELECT COUNT(*) FROM sql_memories WHERE db_key=?", (db_key,)
            ).fetchone()[0]
            n_doc = self._conn.execute(
                "SELECT COUNT(*) FROM documentations WHERE db_key=?", (db_key,)
            ).fetchone()[0]
            n_ddl = self._conn.execute(
                "SELECT COUNT(*) FROM ddl_memories WHERE db_key=?", (db_key,)
            ).fetchone()[0]
        return {"sql_examples": n_sql, "documentations": n_doc, "ddl_tables": n_ddl}


# ── 全局单例 ────────────────────────────────────────────
_memory: AgentMemory | None = None
_memory_lock = threading.Lock()


def get_memory() -> AgentMemory:
    global _memory
    if _memory is None:
        with _memory_lock:
            if _memory is None:  # 双检锁：防多线程首次并发创建多个实例（各持连接写同库）
                _memory = AgentMemory()
    return _memory
