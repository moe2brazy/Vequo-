# -*- coding: utf-8 -*-
"""分析路径 Skill 沉淀（P1-3，对标 FineBI「分析路径沉淀与复用」）。

## 与语义缓存的区别（不要混淆）
- **语义缓存**（`llm_service._semantic_lookup`）：解决「**同一个问题**再来一次」→
  直接复用上次的 SQL，连 LLM 都不调。
- **Skill**（本模块）：解决「**相似的分析路径**再来一次」→ 用户这周问
  「各产线的产量」，下周问「各工序的产量」，问题不同但**分析路径相同**
  （同一张事实表 + GROUP BY 维度 + SUM 度量 + 柱状图）。把这条路径沉淀成
  Skill，下次相似问题直接把它的 SQL 作为 few-shot 示例注入 prompt，
  让 LLM 照着"上次这么写是对的"来生成，提升准确率与一致性。

## 关键设计：命中后只做 few-shot 注入，绝不直接执行
这是与架构约定「确定性编译为主、SQL 由编译器产出」保持一致的关键：
Skill 命中的 SQL **不会**被直接拿去执行（那样等于开了"LLM 历史输出直接执行"的口子，
且历史 SQL 可能已过时/越权）。它只作为**参考示例**注入 `build_sql_prompt` 的
`sql_examples` 通道（既有通道，无需改 prompt 结构），生成与执行仍走原链路。

## 确定性优先，向量只做加分项
本机 embedding 可能不可用（sentence_transformers 未装），所以匹配以**规则打分**为主：
表名/维度/指标标签的重合度 + 问法字面重合（字符 bigram Jaccard）。
向量相似度可用时才作为额外加权，不可用不影响功能。

对外 API：
  - record_success(query, sql, chart_type, ...) 沉淀一条
  - match_skills(query, k)                      匹配相似路径
  - skill_hint(query)                           生成可注入 prompt 的参考文本
  - list_skills / delete_skill / clear_skills   管理
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

_logger = logging.getLogger("skill_store")

_MAX_SKILLS = 200
# 与 monitor / metric_miner 一致：默认内存模式，SKILL_PERSIST=1 才落盘
# （本机安全策略会拦截部分文件写入，落盘做成可选项避免无谓失败）
_PERSIST = os.getenv("SKILL_PERSIST", "0") == "1"
_FILE = Path(__file__).resolve().parent.parent / "skill_store.json"

_lock = threading.Lock()
_skills: list[dict] = []

# 权重：表重合 / 维度-指标标签重合 / 问法字面重合
_W_TABLE, _W_ENTITY, _W_TEXT = 0.35, 0.35, 0.30
_MIN_SCORE = 0.22          # 低于此分视为不相关，不注入
_MIN_HITS_TO_USE = 1       # 至少被验证成功 1 次才允许作为参考（防脏示例）


# ── 要素抽取（sqlglot 确定性解析，不靠 LLM）──────────────────

def parse_path(sql: str) -> dict:
    """从 SQL 解析分析路径要素：{tables, dims, measures}。解析失败返回空结构。"""
    out = {"tables": [], "dims": [], "measures": []}
    if not sql:
        return out
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:
        return out
    tree = None
    for d in ("", "postgres", "mysql"):
        try:
            tree = sqlglot.parse_one(sql, read=(d or None))
            if tree is not None:
                break
        except Exception:
            continue
    if tree is None:
        return out

    for t in tree.find_all(exp.Table):
        nm = t.name
        if nm:
            nm = nm.lower()
            if nm not in out["tables"]:
                out["tables"].append(nm)
    grp = tree.args.get("group")
    if grp is not None:
        for e in (grp.expressions or []):
            for col in e.find_all(exp.Column):
                if col.name and col.name.lower() not in out["dims"]:
                    out["dims"].append(col.name.lower())
    for agg in tree.find_all(exp.AggFunc):
        if isinstance(agg, exp.Window):
            continue
        for col in agg.find_all(exp.Column):
            if col.name and col.name.lower() not in out["measures"]:
                out["measures"].append(col.name.lower())
    return out


def _meta_labels(tables: list[str], dims: list[str], measures: list[str]) -> list[str]:
    """把表名/列名翻译成中文业务标签（用于与用户问法做字面匹配）。"""
    labels: list[str] = []
    try:
        from db.metadata import TABLES
        for t in tables:
            for mt in TABLES:
                if str(mt.get("table_name", "")).split(".")[-1].lower() == t:
                    alias = str(mt.get("table_alias") or "")
                    if alias:
                        labels.append(alias.replace("表", ""))
                    for f in (mt.get("fields") or []):
                        fn = str(f.get("name") or "").lower()
                        if fn in dims or fn in measures:
                            desc = str(f.get("description") or "")
                            m = re.match(r"([\u4e00-\u9fff]{2,6})", desc)
                            if m:
                                labels.append(m.group(1))
                    break
    except Exception:
        pass
    return [l for l in labels if l]


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _skill_name(tables: list[str], dims: list[str], measures: list[str]) -> str:
    """规则生成 Skill 名（不调 LLM，省一次往返；可在管理页改名）。"""
    labels = _meta_labels(tables[:1], dims, measures)
    if labels:
        return "·".join(labels[:3])
    parts = []
    if dims:
        parts.append("按" + "/".join(dims[:2]))
    if measures:
        parts.append("统计" + "/".join(measures[:2]))
    return " ".join(parts) or "通用查询路径"


# ── 沉淀 ────────────────────────────────────────────────

def _dedup_key(path: dict) -> str:
    return "|".join([
        ",".join(sorted(path.get("tables") or [])),
        ",".join(sorted(path.get("dims") or [])),
        ",".join(sorted(path.get("measures") or [])),
    ])


def record_success(query: str, sql: str, chart_type: str = "",
                   tables: list[str] | None = None) -> dict | None:
    """沉淀一条成功分析路径。同源路径（同表+同维度+同指标）只累加命中次数。

    返回 Skill 条目；SQL 无法解析出有效要素时返回 None（不沉淀无意义路径）。
    """
    if not sql:
        return None
    path = parse_path(sql)
    if not path["tables"]:
        return None
    if tables:
        for t in tables:
            b = str(t).split(".")[-1].lower()
            if b and b not in path["tables"]:
                path["tables"].append(b)

    key = _dedup_key(path)
    now = time.time()
    with _lock:
        for s in _skills:
            if s.get("_key") == key:
                s["hit_count"] = int(s.get("hit_count") or 0) + 1
                s["last_used"] = now
                # 保留最新一次的成功 SQL 作为参考示例（写法可能更优）
                s["sql"] = sql
                if chart_type:
                    s["chart_type"] = chart_type
                _save()
                return dict(s)

        skill = {
            "id": uuid.uuid4().hex[:10],
            "name": _skill_name(path["tables"], path["dims"], path["measures"]),
            "sample_question": (query or "")[:120],
            "sql": sql,
            "chart_type": chart_type or "",
            "tables": path["tables"],
            "dims": path["dims"],
            "measures": path["measures"],
            "labels": _meta_labels(path["tables"], path["dims"], path["measures"]),
            "hit_count": 1,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_used": now,
            "_key": key,
        }
        _skills.append(skill)
        if len(_skills) > _MAX_SKILLS:
            # 淘汰最久未用且命中次数最少的（保留高频路径）
            _skills.sort(key=lambda x: (int(x.get("hit_count") or 0),
                                        float(x.get("last_used") or 0)))
            del _skills[:len(_skills) - _MAX_SKILLS]
        _save()
        return dict(skill)


# ── 匹配 ────────────────────────────────────────────────

def _score(query: str, skill: dict) -> float:
    """规则打分：标签命中率 + 问法覆盖率 + 样本问法相似度。

    踩过的坑（不要改回去）：
    - **英文表名不能参与中文问法的字面匹配**。用户问"各产线产量"不会出现
      mes_process_output，把这些表名塞进词表既拉低命中率，又会把 bigram 并集
      撑大数倍，导致 Jaccard 分母爆炸、所有问题都低于阈值（实测全部落空）。
    - **用覆盖率而不是对称 Jaccard** 衡量维度/指标重合：标签词表远大于短问句，
      对称 Jaccard 会被词表规模压扁；覆盖率（问法里有多少成被标签覆盖）才反映
      "这个问题是不是在问这条路径上的东西"。
    - 标签用**子串级**判断（共享 bigram 即算命中），因为标签多是多字词
      （"工序产量"），而问法只出现其中一部分（"产量"），整词匹配永远命中不了。
    """
    q_bg = _bigrams(query or "")

    # 只取中文业务标签；没有标签时退回维度/指标列名（英文场景）
    labels = [l for l in (skill.get("labels") or []) if l]
    if not labels:
        labels = list(skill.get("dims") or []) + list(skill.get("measures") or [])
    if not labels or not q_bg:
        return 0.0

    # 1) 标签命中率：有多少标签与问法共享至少一个 bigram
    hit = 0
    for lb in labels:
        if _bigrams(lb) & q_bg:
            hit += 1
    s_table = hit / len(labels)

    # 2) 问法覆盖率：问法里有多少成被标签词表覆盖
    ent_bg: set[str] = set()
    for lb in labels:
        ent_bg |= _bigrams(lb)
    s_entity = len(q_bg & ent_bg) / len(q_bg) if ent_bg else 0.0

    # 3) 与样本问法的相似度（对称 Jaccard 在这里合适：两者都是短问句）
    s_text = _jaccard(q_bg, _bigrams(skill.get("sample_question") or ""))

    return _W_TABLE * s_table + _W_ENTITY * s_entity + _W_TEXT * s_text


def match_skills(query: str, k: int = 2, min_score: float = _MIN_SCORE) -> list[dict]:
    """匹配相似分析路径，按分数降序返回（含 score 字段）。"""
    if not query:
        return []
    with _lock:
        pool = list(_skills)

    scored = []
    for s in pool:
        if int(s.get("hit_count") or 0) < _MIN_HITS_TO_USE:
            continue
        sc = _score(query, s)
        if sc >= min_score:
            scored.append((sc, s))
    scored.sort(key=lambda x: x[0], reverse=True)

    # 向量可作为加分项，但不可用时不阻断（本机常缺 sentence_transformers）
    try:
        from agent.llm_service import _cos_sim, _embed_query
        vec = _embed_query(query)
        if vec:
            boosted = []
            for sc, s in scored[:10]:
                sv = s.get("_vec")
                if sv is None:
                    sv = _embed_query(s.get("sample_question") or "")
                    s["_vec"] = sv
                if sv:
                    sc = min(1.0, sc * 0.7 + _cos_sim(vec, sv) * 0.3)
                boosted.append((sc, s))
            boosted.sort(key=lambda x: x[0], reverse=True)
            scored = boosted
    except Exception:
        pass

    out = []
    for sc, s in scored[:k]:
        d = {kk: vv for kk, vv in s.items() if not kk.startswith("_")}
        d["score"] = round(sc, 3)
        out.append(d)
    return out


def skill_hint(query: str, k: int = 2) -> str:
    """生成可注入 prompt 的参考文本（few-shot 素材）。无命中返回空串。"""
    hits = match_skills(query, k=k)
    if not hits:
        return ""
    lines = []
    for h in hits:
        lines.append(f"· 参考问法：{h['sample_question']}\n  参考 SQL：{h['sql']}")
    return "\n".join(lines)


def as_sql_examples(query: str, k: int = 2) -> list[dict]:
    """转成 `build_sql_prompt` 的 sql_examples 格式（既有 few-shot 通道，无需改 prompt）。"""
    hits = match_skills(query, k=k)
    return [{"question": h.get("sample_question") or "",
             "sql": h.get("sql") or "",
             "table_name": (h.get("tables") or [""])[0],
             "source": "skill"} for h in hits]


# ── 管理 ────────────────────────────────────────────────

def list_skills() -> list[dict]:
    with _lock:
        return [{k: v for k, v in s.items() if not k.startswith("_")} for s in _skills]


def delete_skill(skill_id: str) -> bool:
    with _lock:
        before = len(_skills)
        _skills[:] = [s for s in _skills if s.get("id") != skill_id]
        if len(_skills) != before:
            _save()
            return True
    return False


def clear_skills() -> int:
    with _lock:
        n = len(_skills)
        _skills.clear()
        _save()
        return n


def rename_skill(skill_id: str, name: str) -> dict | None:
    with _lock:
        for s in _skills:
            if s.get("id") == skill_id:
                s["name"] = (name or "").strip()[:40] or s["name"]
                _save()
                return {k: v for k, v in s.items() if not k.startswith("_")}
    return None


# ── 持久化（可选）────────────────────────────────────────

def _save() -> None:
    if not _PERSIST:
        return
    try:
        snapshot = [{k: v for k, v in s.items() if not k.startswith("_")} for s in _skills]
        _FILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        _logger.warning("Skill 持久化失败（保持内存模式）: %s", e)


def _load() -> None:
    if not _PERSIST:
        return
    global _skills
    try:
        if _FILE.exists():
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _skills = [s for s in data if isinstance(s, dict) and s.get("id")]
    except Exception:
        _skills = []


# 缺陷修复（P0）：_load() 原先**只定义、从不调用** —— 持久化开关（SKILL_PERSIST=1）打开后，
# 进程启动阶段没有任何地方把磁盘数据读回内存，_skills 永远从空列表开始。
# 表现为"已经沉淀的分析路径重启后全部消失"，而磁盘上明明有 skill_store.json。
_load()
