# -*- coding: utf-8 -*-
"""指标与同义词自动挖掘（对标 ThoughtSpot SpotterModel / Microsoft Fabric Copilot
辅助建模 / Aloudata CAN 指标自动发现）。

## 竞品做法（高度一致）
三家都是同一个套路：**自动提炼候选 + 人工审核确认**。
- SpotterModel：从数据源自动推断实体、指标、关系，生成语义模型草案，人工确认；
- Fabric Copilot：在建模界面里建议度量值与同义词，用户接受才落地；
- Aloudata CAN：自动发现指标线索，人工确认后纳入指标平台。

没有任何一家敢让 Agent 把指标直接写进生产口径库 —— 口径错了是数据事故，
自动化只能负责"覆盖率"，"正确性"必须留给人。（Vanna 的 auto_train 之所以常被诟病，
正是因为它把"能跑"当成了"口径对"。）

## 我们的方案
1. 挖掘源：历史成功 SQL（agent/memory 的 sql_examples，含用户真实问法）+ 库表元数据
2. **确定性解析**：sqlglot 提取聚合表达式 + 表 + 分组维度，不靠 LLM 猜算式
3. 去重：与已注册指标按「归一化表达式 + 表」比对，已注册的不重复推荐
4. **LLM 只做 NL 部分**：为每个候选生成中文名 / 同义词 / 单位 / 口径描述
   —— 正好落在架构约定「LLM 只产 NL，不产 SQL」的安全区内
5. **候选不自动入库**：必须经人工在指标管理页确认采纳（human-in-loop 二次确认）

对外 API：
  - mine_candidates(use_llm=True) → 挖掘并刷新候选池，返回候选清单
  - list_candidates()             → 查看当前候选池
  - adopt_candidate(cid, ...)     → 人工采纳：写入指标注册表
  - ignore_candidate(cid)         → 人工忽略：从候选池移除
"""

import hashlib
import json
import os
import re
import time

from langchain_core.messages import SystemMessage

# ── 候选池持久化（2026-09-09 修复：此前纯进程内存，重启即丢，功能"看似没实现"）──
# 默认落盘到 backend/data/metric_candidates.json；写失败（安全策略拦截/只读）时静默
# 降级为内存模式，不影响运行。METRIC_CANDIDATES_PERSIST=0 可显式关闭落盘。
def _state_path() -> str:
    try:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
        d = os.path.join(base, "data")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "metric_candidates.json")
    except Exception:
        return ""


def _persist_enabled() -> bool:
    try:
        v = os.getenv("METRIC_CANDIDATES_PERSIST", "1")
        return v not in ("0", "false", "False", "")
    except Exception:
        return True


def _load_state() -> None:
    """启动加载：候选 + 忽略集合 + 已采纳归档（供审计）。失败静默，保持空池。"""
    global _CAND, _IGNORED, _ADOPTED
    if not _persist_enabled():
        return
    try:
        p = _state_path()
        if not p or not os.path.exists(p):
            return
        with open(p, "r", encoding="utf-8") as f:
            st = json.load(f) or {}
        if isinstance(st.get("candidates"), dict):
            _CAND = {k: v for k, v in st["candidates"].items() if isinstance(v, dict)}
        if isinstance(st.get("ignored"), list):
            _IGNORED = set(x for x in st["ignored"] if isinstance(x, str))
        if isinstance(st.get("adopted"), list):
            _ADOPTED = [x for x in st["adopted"] if isinstance(x, dict)]
    except Exception:
        pass


def _save_state() -> None:
    """保存状态。任何写盘异常都不向外抛（候选池不是关键路径，失败仅降级为内存态）。"""
    if not _persist_enabled():
        return
    try:
        p = _state_path()
        if not p:
            return
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"candidates": _CAND, "ignored": sorted(_IGNORED),
                       "adopted": _ADOPTED}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# 候选池。已采纳记录留痕归档（_ADOPTED），供治理审计与重启后回溯。
_CAND: dict[str, dict] = {}
_IGNORED: set[str] = set()
_ADOPTED: list[dict] = []
_load_state()

_NAMING_SYSTEM = """你是数据治理专家，负责把 SQL 聚合表达式整理成业务指标定义。

为下列每个表达式给出：name（中文指标名，2-6 字，业务用语）、aliases（3-5 个中文同义词或常见叫法，
含用户口语说法）、unit（单位，如 件/元/次/小时/%，无量纲用空字符串）、
description（一句话口径说明：从哪张表哪个字段、怎么算，40 字内）。

规则：
- 名称必须是业务语言，不能是字段名直译（good_qty → 「合格产量」而不是「好数量」）
- 同义词要覆盖用户真实可能的问法（口语、简称、别称）
- 无法确定单位时用空字符串，不要编造

只输出 JSON：{"items": [{"idx": 0, "name": "...", "aliases": [...], "unit": "...", "description": "..."}]}"""

_NAMING_PROMPT = """## 数据库上下文
{schema}

## 待命名的聚合表达式
{items}

请按顺序为每个表达式输出定义。只输出 JSON。"""


def _norm_expr(expr: str) -> str:
    """表达式归一化：去空白 + 去表别名前缀 + 小写。

    关键在于**去掉表别名**：历史 SQL 里同一个口径会写成 SUM(mp.good_qty)、
    SUM(t.good_qty)、SUM(good_qty)，而注册表里登记的是 SUM(good_qty)。
    不剥前缀的话三者会被当成三个不同指标，去重形同虚设（实测会漏掉已注册口径）。
    """
    s = re.sub(r"\s+", "", (expr or "")).lower()
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(expr)
        if tree is not None:
            for c in tree.find_all(exp.Column):
                c.set("table", None)
            return re.sub(r"\s+", "", tree.sql()).lower()
    except Exception:
        pass
    return s


def _cid(expr: str, tables: tuple, filter_sig: str = "") -> str:
    """候选 ID：由表达式 + 表集合 + 过滤语义签名决定。

    保证「同一口径反复出现」只对应一个候选；同时区分「同表达式不同过滤条件」
    （COUNT(*) 全量 vs COUNT(*) WHERE result='fail'）这两个不同口径（2026-09-09）。
    """
    key = _norm_expr(expr) + "|" + ",".join(sorted(tables)) + "|" + filter_sig
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def _metric_key(expr: str, tables: tuple, filter_sig: str = "") -> tuple:
    """口径身份键（用于与已注册指标去重 / 候选间去重）。"""
    return (_norm_expr(expr), tuple(sorted(tables)), filter_sig)


def _extract_aggs(sql: str, dialect: str = "") -> list[dict]:
    """用 sqlglot 确定性提取 SELECT 投影里的聚合表达式（不靠 LLM）。

    返回 [{expr, alias, tables, dims}]；解析失败返回空列表。
    """
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:
        return []
    tree = None
    for d in (dialect, "", "postgres", "mysql"):
        try:
            tree = sqlglot.parse_one(sql, read=(d or None))
            if tree is not None:
                break
        except Exception:
            continue
    if tree is None:
        return []

    tables: list[str] = []
    alias_map: dict[str, str] = {}
    for t in tree.find_all(exp.Table):
        nm = f"{t.db}.{t.name}" if t.db else t.name
        if nm and nm not in tables:
            tables.append(nm)
        # 别名 → 真实表 映射：用于把聚合字段定位到"事实表"
        alias_map[(t.alias_or_name or t.name or "").lower()] = nm

    dims: list[str] = []
    grp = tree.args.get("group")
    if grp is not None:
        for e in (grp.expressions or []):
            node = e.this if hasattr(e, "this") and e.__class__.__name__ == "Paren" else e
            nm = getattr(node, "name", "") or str(node)[:40]
            if nm and nm not in dims:
                dims.append(nm)

    out: list[dict] = []
    for proj in (tree.expressions or []):
        if proj is None:
            continue  # 容错：sqlglot 对乱 SQL 可能产出 None 投影节点（防 AttributeError）
        agg = None
        for node in proj.walk():
            # 窗口函数不是"指标口径"（它依赖分区上下文），跳过
            if isinstance(node, exp.AggFunc) and not isinstance(node, exp.Window):
                agg = node
                break
        if agg is None:
            continue
        # 不带方言生成：带方言会对 ToChar 等函数报"不支持 format"告警，
        # 而我们只用这段 SQL 做去重与展示，方言差异无意义
        try:
            expr_sql = agg.sql()
        except Exception:
            expr_sql = str(agg)

        # 聚合字段所在的事实表：JOIN 的维度表不算口径来源。
        # 否则同一个 SUM(good_qty) 会因为 JOIN 了不同维度表被拆成多个重复候选。
        src_tables = set()
        for col in agg.find_all(exp.Column):
            qual = (col.table or "").lower()
            if qual and qual in alias_map:
                src_tables.add(alias_map[qual])
        if not src_tables:
            # 裸列（无表前缀）：单表查询可唯一归属；多表无法判定 → 保守取全部
            src_tables = set(tables)

        out.append({
            "expr": expr_sql,
            "alias": str(getattr(proj, "alias_or_name", "") or ""),
            "tables": tuple(sorted(src_tables)) or tuple(tables),
            "dims": list(dims),
            # 过滤语义签名：WHERE/HAVING 子句（去表别名、归一化）。无过滤为空串。
            # 用于把「COUNT(*) 全量」与「COUNT(*) WHERE result='fail'」区分成两个口径：
            # 去重键/候选 ID 若只看聚合表达式+表，两者会互相覆盖（见 2026-09-09 修复）。
            "filter_sig": _filter_sig_of(tree) if (tree.args.get("where") or tree.args.get("having")) else "",
            # 该 SQL 是否带行级过滤（裸 COUNT(*) 不算业务口径；条件 COUNT(*) 是）
            "filtered": bool(tree.args.get("where")) or bool(tree.args.get("having")),
        })
    return out


def _filter_sig_of(tree) -> str:
    """WHERE/HAVING 子句 → 稳定字符串：去表别名前缀、去空白、小写（用于口径去重键）。

    取条件表达式本体（where.this / having.this），不含 WHERE 关键字本身，
    避免与候选展示时手工拼的 "WHERE " 前缀重复（见 2026-09-09 修复）。
    """
    try:
        import sqlglot
        from sqlglot import exp
        parts = []
        for key in ("where", "having"):
            node = tree.args.get(key)
            if node is None:
                continue
            inner = node.this if hasattr(node, "this") and getattr(node, "this", None) is not None else node
            clone = inner.copy()
            for c in clone.find_all(exp.Column):
                try:
                    c.set("table", None)
                except Exception:
                    pass
            parts.append(re.sub(r"\s+", "", clone.sql()).lower())
        return "|".join(parts)
    except Exception:
        return ""


def _historical_sql(limit: int = 200) -> list[dict]:
    """取历史成功 SQL（用户真实问法 + SQL），作为挖掘源。"""
    try:
        from agent.memory import get_memory
        data = get_memory().list_memories() or {}
        rows = data.get("sql_examples") or []
        out = []
        for r in rows[:limit]:
            if isinstance(r, dict) and r.get("sql"):
                out.append({"question": str(r.get("question") or "")[:200],
                            "sql": str(r.get("sql") or "")})
        return out
    except Exception:
        return []


def _registered_keys() -> set[tuple]:
    """已注册指标的 (归一化表达式, 表集合, 过滤签名) 键，用于去重。

    注册指标的 sql_expression 通常是裸聚合（SUM/COUNT + CASE），过滤语义内嵌在
    表达式本身，因此其身份键的过滤签名为空。候选侧带 WHERE 的聚合（如
    COUNT(*) WHERE result='fail'）filter_sig 非空 → 与「全量 COUNT(*)」正确区分，
    不会再被误判为已注册而丢弃（2026-09-09 修复）。
    """
    keys = set()
    try:
        from agent.metric_registry import get_all_metrics
        for m in get_all_metrics():
            expr = m.get("sql_expression") or m.get("formula") or ""
            if not expr:
                continue
            tbls = tuple(sorted({str(t).split(".")[-1].lower()
                                 for t in (m.get("tables") or [])}))
            keys.add((_norm_expr(expr), tbls, ""))
    except Exception:
        pass
    return keys


def _db_type() -> str:
    try:
        from database import get_db_type
        return get_db_type() or ""
    except Exception:
        return ""


def _batch_naming(cands: list[dict], schema_hint: str = "") -> None:
    """批量让 LLM 生成中文名 / 同义词 / 单位 / 说明（只产 NL，不产 SQL）。"""
    try:
        from agent.llm_service import _make_llm, _loads_lenient
    except Exception:
        return
    items = []
    for i, c in enumerate(cands):
        items.append("#%d 表达式：%s（来自表：%s）\n   用户问法示例：%s"
                     % (i, c["expr"], "、".join(c["tables"][:4]) or "未知",
                        c.get("sample_question") or "—"))
    try:
        llm = _make_llm(temp=0.0, max_tokens=1200, json_mode=True)
        raw = str(llm.invoke([
            SystemMessage(content=_NAMING_SYSTEM),
            SystemMessage(content=_NAMING_PROMPT.format(
                schema=(schema_hint or "（无）")[:1500],
                items="\n".join(items)[:3500])),
        ]).content or "")
        data = _loads_lenient(raw) or {}
        arr = data.get("items") if isinstance(data, dict) else None
        if not isinstance(arr, list):
            return
        for it in arr:
            if not isinstance(it, dict):
                continue
            try:
                idx = int(it.get("idx"))
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(cands)):
                continue
            c = cands[idx]
            name = str(it.get("name") or "").strip()[:20]
            if name:
                c["name"] = name
            aliases = [str(a).strip()[:20] for a in (it.get("aliases") or []) if str(a).strip()]
            if aliases:
                c["aliases"] = aliases[:6]
            c["unit"] = str(it.get("unit") or "").strip()[:10]
            desc = str(it.get("description") or "").strip()[:160]
            if desc:
                c["description"] = desc
    except Exception:
        pass


def suggest_from_query(query: str, sql: str, source: str = "runtime") -> int:
    """运行时口径信号沉淀（P1-1 编译覆盖率闭环）。

    由 llm_service.run() 在「LLM 兜底路径执行成功且复核通过」时调用：把该查询的
    聚合表达式与已注册口径比对，未注册的进候选池（标记来源 runtime）。
    管理员在指标管理页一键采纳 → 后续同类问题编译命中 → 更快更稳。
    返回新增候选数。与 mine_candidates 的区别：mine 批量扫历史，这里单条实时沉淀。
    """
    if not sql:
        return 0
    dialect = _db_type()
    registered = _registered_keys()
    added = 0
    for agg in _extract_aggs(sql, dialect)[:3]:
        expr = agg["expr"]
        # 裸 COUNT(*)（无 WHERE/HAVING）语义太泛（"全表多少行"），不构成业务口径；
        # 但带过滤的 COUNT(*)（如 COUNT WHERE result='fail'）是明确业务口径，必须能沉淀。
        if not expr or (expr.strip().upper() in ("COUNT(*)",) and not agg.get("filtered")):
            continue
        tables = tuple(sorted({t.split(".")[-1].lower() for t in agg["tables"]}))
        f_sig = agg.get("filter_sig") or ""
        key = _metric_key(expr, tables, f_sig)
        if key in registered:
            continue
        cid = _cid(expr, agg["tables"], f_sig)
        if cid in _IGNORED or cid in _CAND:
            continue
        # 带过滤的聚合：候选表达式带上过滤语义，便于管理页辨识（如 COUNT(*) WHERE result='fail'）
        disp = expr if not f_sig else f"{expr} WHERE {f_sig.replace('|', ' AND ')}"
        _CAND[cid] = {
            "id": cid, "expr": disp, "tables": list(agg["tables"]),
            "dims": agg.get("dims") or [], "hit_count": 1,
            "sample_question": (query or "")[:200],
            "name": agg.get("alias") or "", "aliases": [], "unit": "",
            "description": "", "status": "pending", "source": source,
            "mined_at": int(time.time()),
        }
        added += 1
    if added:
        _save_state()
    return added


def mine_candidates(limit: int = 20, use_llm: bool = True) -> dict:
    """挖掘指标候选：历史 SQL → 聚合表达式 → 去重 → LLM 命名 → 进候选池。

    返回 {success, candidates, total, skipped_registered, error}
    """
    src = _historical_sql()
    if not src:
        return {"success": False, "candidates": [], "total": 0,
                "skipped_registered": 0, "error": "没有可用的历史查询记录"}
    dialect = _db_type()
    registered = _registered_keys()

    # 1. 确定性解析：聚合表达式 → 候选（按 表达式+表 聚合，统计命中次数）
    bucket: dict[str, dict] = {}
    for item in src:
        for agg in _extract_aggs(item["sql"], dialect):
            expr = agg["expr"]
            # 裸 COUNT(*)（无过滤）语义太泛（"全表多少行"）不构成业务口径；
            # 带条件的 COUNT(*)（WHERE result='fail' / LIKE / 时间区间…）是明确口径，放行
            if not expr or (expr.strip().upper() in ("COUNT(*)",) and not agg.get("filtered")):
                continue
            tables = tuple(sorted({t.split(".")[-1].lower() for t in agg["tables"]}))
            f_sig = agg.get("filter_sig") or ""
            key = _metric_key(expr, tables, f_sig)
            if key in registered:
                continue
            cid = _cid(expr, agg["tables"], f_sig)
            if cid in _IGNORED:
                continue
            c = bucket.get(cid)
            if c is None:
                # 带过滤聚合：表达式附上过滤语义便于展示与命名（如 COUNT(*) WHERE result='fail'）
                # 2026-10-03 修复（P0）：`disp`（含 WHERE）是**给人看的展示串**，不是合法
                # SQL 表达式。修复前它被原样写进候选的 "expr" 字段，而 adopt_candidate /
                # create_user_metric 直接取 c["expr"] 存进注册表 sql_expression →
                # 采纳一条带过滤的候选即永久写入坏口径。编译产物实测为
                #   SELECT COUNT(*) WHERE result='fail' AS "…" FROM …   （COUNT 与 WHERE
                # 之间缺逗号，PG 直接语法错），而 _safe_metric_expr 对它返回 True
                # （WHERE 不在黑名单）→ try_compile_metric 返回非 None → 不回退 LLM
                # → 整条查询硬失败且无日志。
                # 现在：expr 存**干净的可执行表达式**，disp 另存到 display 字段供 UI 展示。
                disp = expr if not f_sig else f"{expr} WHERE {f_sig.replace('|', ' AND ')}"
                c = bucket[cid] = {
                    "id": cid, "expr": expr, "display": disp,
                    "tables": list(agg["tables"]),
                    "dims": agg.get("dims") or [], "hit_count": 0,
                    "sample_question": item["question"],
                    "name": agg.get("alias") or "", "aliases": [], "unit": "",
                    "description": "", "status": "pending", "mined_at": int(time.time()),
                }
            c["hit_count"] += 1
            for d in (agg.get("dims") or []):
                if d not in c["dims"]:
                    c["dims"].append(d)
            if not c["sample_question"] and item["question"]:
                c["sample_question"] = item["question"]

    skipped = len(registered)
    # 2. 排序：命中次数优先（越常被问到的口径越值得先治理）
    cands = sorted(bucket.values(), key=lambda c: (-c["hit_count"], c["expr"]))[:limit]
    if not cands:
        return {"success": True, "candidates": list(_CAND.values()), "total": len(_CAND),
                "skipped_registered": skipped, "error": "没有发现新的指标候选（可能都已注册）"}

    # 3. LLM 补 NL（中文名 / 同义词 / 单位 / 口径描述）
    if use_llm:
        _batch_naming(cands)

    # 4. 入池（保留已有候选的人工编辑）
    for c in cands:
        old = _CAND.get(c["id"])
        if old and old.get("status") == "edited":
            continue
        _CAND[c["id"]] = c
    if cands:
        _save_state()

    return {"success": True, "candidates": list(_CAND.values()), "total": len(_CAND),
            "skipped_registered": skipped, "error": ""}


def list_candidates() -> list[dict]:
    return list(_CAND.values())


def get_candidate(cid: str) -> dict | None:
    return _CAND.get(cid)


def update_candidate(cid: str, patch: dict) -> dict | None:
    """人工编辑候选（改名字/同义词/单位），再采纳。编辑过的候选不会被挖掘覆盖。"""
    c = _CAND.get(cid)
    if not c:
        return None
    for k in ("name", "aliases", "unit", "description"):
        if k in patch:
            c[k] = patch[k]
    c["status"] = "edited"
    _save_state()
    return c


def adopt_candidate(cid: str, name: str = "", aliases: list | None = None,
                    unit: str = "", description: str = "") -> dict:
    """人工采纳：把候选写入指标注册表（human-in-loop 的"确认"这一步）。"""
    c = _CAND.get(cid)
    if not c:
        return {"success": False, "error": "候选不存在或已被处理"}
    try:
        from agent.metric_registry import create_user_metric
        # 2026-10-03 修复（P0）：兜底剥离展示串里的 WHERE 子句。
        # 候选池在本次修复前已经落盘过含「COUNT(*) WHERE result='fail'」这类
        # 展示串（expr 字段），修源头只挡新挖掘的，存量脏数据仍会被采纳进注册表。
        _expr_src = c.get("expr") or ""
        _expr_clean = re.split(r"\bWHERE\b", _expr_src, flags=re.IGNORECASE)[0].strip() or _expr_src
        if not _expr_clean:
            return {"success": False, "error": "该候选的表达式为空，请重新挖掘后再采纳"}
        metric = {
            "name": (name or c.get("name") or "").strip(),
            "aliases": aliases if aliases is not None else (c.get("aliases") or []),
            "unit": unit if unit != "" else (c.get("unit") or ""),
            "tables": c.get("tables") or [],
            "sql_expression": _expr_clean,
            "formula": _expr_clean,
            "description": (description or c.get("description") or "").strip()
                           or ("自动挖掘：%s" % (c.get("display") or _expr_clean)),
            "dims": c.get("dims") or [],
        }
        if not metric["name"]:
            return {"success": False, "error": "指标名不能为空，请先填写"}
        if _expr_clean != _expr_src:
            # 说明写进 description，**不污染 sql_expression**（算式必须保持可执行）
            metric["description"] = (metric["description"]
                                     + "（已自动剥离展示串中的过滤条件，如需保留请改为 CASE WHEN 形式）")
        created = create_user_metric(metric)
        # 采纳留痕归档（重启后可回溯治理历史），再从候选池移除
        _ADOPTED.append({
            "id": cid, "expr": c.get("expr") or "", "name": metric["name"],
            "aliases": metric.get("aliases") or [], "unit": metric.get("unit") or "",
            "source": c.get("source") or "runtime", "adopted_at": int(time.time()),
        })
        _CAND.pop(cid, None)
        _save_state()
        return {"success": True, "metric": created}
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": "入库失败: %s" % str(e)[:160]}


def ignore_candidate(cid: str) -> bool:
    """忽略候选：从池中移除并记入黑名单，后续挖掘不再推荐同一口径。"""
    if cid in _CAND:
        _CAND.pop(cid)
        _IGNORED.add(cid)
        _save_state()
        return True
    return False


def add_schema_candidates(cands: list[dict]) -> int:
    """P0-1 语义层自动构建：并入 schema_scan 候选（与历史 SQL 挖掘共用同一候选池）。

    - 以「归一化表达式 + 表集合」为去重键（与 _registered_keys 同口径）；
    - 已注册 / 已忽略 / 已存在 的候选跳过，不重复推荐；
    - 候选不自动入库，管理页人工采纳（human-in-loop）。
    返回新增候选数。
    """
    if not cands:
        return 0
    registered = _registered_keys()
    added = 0
    for c in cands:
        if not isinstance(c, dict):
            continue
        expr = str(c.get("expr") or "").strip()
        tables = c.get("tables") or []
        if not expr or not tables:
            continue
        # 2026-10-03 修复：原 key 是二元组，而 _registered_keys 产出的是三元组
        # (_norm_expr, tbls, filter_sig) —— 二元组永远不等于三元组，
        # `key in registered` 恒为 False，schema_scan 通道的「已注册口径去重」
        # 完全失效：已注册的口径（良率、停机时长等）会反复作为「新候选」出现在
        # 人工审核列表里。改用 _metric_key（它已正确返回三元组），
        # 且表名要与 _registered_keys 同口径归一（裸名 + 小写），否则仍对不上。
        _norm_tbls = {str(t).split(".")[-1].lower() for t in tables}
        key = _metric_key(expr, tuple(_norm_tbls), "")
        if key in registered:
            continue
        cid = _cid(expr, tuple(tables))
        if cid in _IGNORED or cid in _CAND:
            continue
        c["id"] = cid
        c.setdefault("hit_count", 1)
        c.setdefault("status", "pending")
        c.setdefault("mined_at", int(time.time()))
        c.setdefault("source", "schema_scan")
        _CAND[cid] = c
        added += 1
    if added:
        _save_state()
    return added
