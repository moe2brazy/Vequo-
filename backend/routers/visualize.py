# -*- coding: utf-8 -*-
"""
可视化渲染规划 Agent（visualize planner）

职责：分析当前数据库结构（表数量、外键数量、字段数、schema 分布），
输出 ER 图 / 知识图谱的**渲染方案**——布局类型、间距、字段显示数、分组方式等。

设计原则（贴合人类浏览）：
- 表少（≤8）或没有外键 → 用「环形」布局分散排列，不硬套分层（无层级可分层时
  所有表会挤成一列，必然"一坨"）。
- 表多且有外键 → 用「分层」布局（父表在左、子表在右）。
- 表少时卡片显示全部字段；表多时只显示主键+外键，保持卡片紧凑。
- 按 schema 分组着色；关系只显示可靠类型（外键 + 预定义业务关联）。
"""
from fastapi import APIRouter, Depends
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
import threading

from database import get_db, get_database_config
from agent.llm_service import _make_llm, _loads_lenient

router = APIRouter(prefix="/api/visualize", tags=["visualize"])

# ── 预渲染缓存：连库/切库时由 prewarm_plan 异步算好（LLM 思考），用户点开 ER 图秒回 ──
_plan_cache: dict[str, dict] = {}
_plan_working: set[str] = set()


def _full_key(db_name: str) -> str:
    """缓存 key = host:port:dbname（B3 修复：仅 db_name 会让不同 host 的同名库串用 ER 方案）"""
    try:
        cfg = get_database_config()
        return f"{cfg.get('host', '')}:{cfg.get('port', 0)}:{db_name}"
    except Exception:
        return db_name


def prewarm_plan(db_name: str) -> None:
    """切库/连库后异步预热渲染方案（LLM 思考 + 缓存），避免用户首次点开 ER 图时等待 LLM"""
    key = _full_key(db_name)
    # 「先查后加」是两个非原子步骤，而这个函数会被并发调用（快速连续切库、连库与切库同时触发）：
    # 两个调用可能同时通过检查、各自起一个后台线程跑同一份 LLM 预热（重复计费 + 重复耗时）。
    # set.add 自身是原子的，用它的返回值当「我是不是第一个」的判据，一步完成判定与占位。
    if key in _plan_cache:
        return
    if not _plan_working.add(key):
        return  # 已有线程在预热这个 key，不重复起
    # 快照当前引擎：线程执行时可能已切库，防止 A 库方案误存 B 库键（竞态修复）
    try:
        from database import engine as _engine_snapshot
    except Exception:
        _plan_working.discard(key)
        return

    def _run():
        try:
            from sqlalchemy.orm import sessionmaker
            session_cls = sessionmaker(bind=_engine_snapshot)
            s = session_cls()
            try:
                stats = _collect_stats(s)
                plan = _llm_plan(*stats)
                if plan is None:
                    plan = _rule_based_plan(*stats)
                _plan_cache[key] = plan
            finally:
                s.close()
        except Exception as e:
            print(f"[visualize] 预热渲染方案失败: {e}")
        finally:
            _plan_working.discard(key)

    threading.Thread(target=_run, daemon=True).start()


def _plan_key() -> str:
    try:
        cfg = get_database_config()
        return f"{cfg.get('host', '')}:{cfg.get('port', 0)}:{cfg['name']}"
    except Exception:
        return "default"

# 排除系统 schema（PG / MySQL 通用）
_SYS_SCHEMAS = ("information_schema", "performance_schema", "mysql", "sys", "pg_catalog")


def _collect_stats(db: Session):
    inspector = inspect(db.get_bind())
    tables = [
        t for t in inspector.get_table_names()
        if not t.startswith(("_", "pg_", "metadata_"))
    ]

    # 外键数量（跨所有业务 schema；字面量写法兼容 PG/MySQL，不用 bindparams 绑定 tuple）
    fk_count = 0
    try:
        row = db.execute(text(
            "SELECT COUNT(*) FROM information_schema.table_constraints "
            "WHERE constraint_type = 'FOREIGN KEY' "
            "AND table_schema NOT IN ('information_schema','performance_schema','mysql','sys','pg_catalog') "
            "AND table_schema NOT LIKE 'pg_%'"
        )).scalar()
        fk_count = int(row or 0)
    except Exception:
        pass

    # 每表字段数
    col_counts = []
    for t in tables:
        try:
            col_counts.append(len(inspector.get_columns(t)))
        except Exception:
            col_counts.append(0)

    # schema 分布（表名带前缀则属于非 public schema）
    schemas = {t.split(".", 1)[0] for t in tables if "." in t}
    return tables, fk_count, col_counts, schemas


def _clamp(v: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return lo


def _rule_based_plan(tables, fk_count, col_counts, schemas) -> dict:
    """规则兜底方案（LLM 不可用时使用）"""
    n = len(tables)
    max_cols = max(col_counts) if col_counts else 0
    layout = "circular" if (n <= 8 or fk_count == 0) else "layered"
    card_max_fields = 12 if n <= 8 else 5
    density = "low"
    if n > 0:
        ratio = fk_count / n
        density = "high" if ratio > 2 else ("medium" if ratio > 0.8 else "low")
    return {
        "layout": layout,
        "cardMaxFields": card_max_fields,
        "colGap": 220,
        "rowGap": 600,
        "hGap": 260,
        "maxPerRow": 3,
        "ringPadding": 240,
        "initZoom": 0.9,
        "relationTypes": ["foreign_key", "business"],
        "groupBySchema": len(schemas) > 1,
        "showIsolated": True,
        "tableCount": n,
        "foreignKeyCount": fk_count,
        "schemaCount": len(schemas),
        "maxColumnsPerTable": max_cols,
        "density": density,
        "llmReasoned": False,
        "note": f"检测到 {n} 张表、{fk_count} 条外键"
                + (f"、按 {len(schemas)} 个 schema 分组" if schemas else "")
                + f"，密度{density}；已自动选择「{'环形' if layout == 'circular' else '分层'}」布局适配浏览。",
    }


def _llm_plan(tables, fk_count, col_counts, schemas):
    """让 LLM 思考后给出渲染方案；任何失败返回 None（由调用方回退规则方案）"""
    n = len(tables)
    table_desc = ", ".join(f"{t}({c}列)" for t, c in zip(tables, col_counts))
    if len(table_desc) > 700:
        table_desc = table_desc[:700] + "…"
    schema_desc = ", ".join(sorted(schemas)) if schemas else "public(默认)"

    prompt = f"""你是数据库可视化渲染规划专家。请分析下面的数据库结构，决定 ER 图如何渲染。
重要原则：用户最反感"卡片粘在一起"和"卡片太小看不清"。**间距宁大勿小**——
前端已做「层内折行」优化（每层卡片排成多行，避免单层十几张表纵向叠成超高竖条），
所以：rowGap（行与行之间的纵向间距）要足够大（480~620），让卡片之间有明显空隙；
colGap（层与层之间的横向间距）适中（180~300）即可，因为横向有折行空间兜底；
hGap（同一行内卡片之间的横向间距）建议 220~320。

数据库结构：
- 表数量：{n}
- 表及字段数：{table_desc}
- 外键关系数量：{fk_count}
- schema：{schema_desc}

请输出 JSON（不要输出任何多余文字、不要 markdown 代码块）：
{{
  "layout": "layered 或 circular（表少≤8 或没有外键用 circular 环形；表多且有外键用 layered 分层）",
  "cardMaxFields": 整数（卡片上显示的字段数：表少用 12 全显示，表多用 5 只显示主外键）,
  "colGap": 整数（层间横向间距 px，建议 180~300）,
  "rowGap": 整数（层内行间纵向间距 px，建议 480~620，宁大勿小）,
  "hGap": 整数（行内卡片横向间距 px，建议 220~320）,
  "maxPerRow": 整数（每行最多放几张卡片，建议 3，最多 4）,
  "ringPadding": 整数（环形布局半径外留白 px，建议 220~340）,
  "initZoom": 数字（初始缩放比例 0.88~0.95：**保证卡片和字大而清晰**）,
  "groupBySchema": true 或 false（有多个 schema 时建议 true）,
  "showIsolated": true 或 false（无外键的孤立表是否也展示）,
  "density": "low 或 medium 或 high",
  "reasoning": "一句话说明你为什么这么选"
}}"""

    try:
        llm = _make_llm(temp=0.2, max_tokens=800, json_mode=True, timeout=25)
        resp = llm.invoke(prompt)
        raw = resp.content if hasattr(resp, "content") else str(resp)
        data = _loads_lenient(raw)
        if not isinstance(data, dict):
            return None
        layout = data.get("layout")
        if layout not in ("layered", "circular"):
            layout = "circular" if (n <= 8 or fk_count == 0) else "layered"
        plan = {
            "layout": layout,
            "cardMaxFields": _clamp(data.get("cardMaxFields") or 0, 3, 16),
            "colGap": _clamp(data.get("colGap") or 0, 180, 340),
            "rowGap": _clamp(data.get("rowGap") or 0, 440, 660),
            "hGap": _clamp(data.get("hGap") or 0, 200, 340),
            "maxPerRow": max(2, min(4, int(data.get("maxPerRow") or 3))),
            "ringPadding": _clamp(data.get("ringPadding") or 0, 200, 360),
            "initZoom": max(0.85, min(1.0, float(data.get("initZoom") or 0.9))),
            "relationTypes": ["foreign_key", "business"],
            "groupBySchema": bool(data.get("groupBySchema", False)),
            "showIsolated": data.get("showIsolated", True) is not False,
            "tableCount": n,
            "foreignKeyCount": fk_count,
            "schemaCount": len(schemas),
            "maxColumnsPerTable": max(col_counts) if col_counts else 0,
            "density": data.get("density") if data.get("density") in ("low", "medium", "high") else "low",
            "llmReasoned": True,
            "note": str(data.get("reasoning") or "") or "LLM 已生成渲染方案",
        }
        return plan
    except Exception as e:
        print(f"[visualize] LLM 规划失败，回退规则方案: {e}")
        return None


@router.get("/plan")
def get_visualization_plan(db: Session = Depends(get_db)):
    """渲染规划 Agent（LLM 思考优先，失败回退规则）：分析库结构输出渲染方案。
    预渲染缓存命中时秒回；未命中（未预热）则同步计算并缓存。"""
    key = _plan_key()
    cached = _plan_cache.get(key)
    if cached:
        return cached
    stats = _collect_stats(db)
    plan = _llm_plan(*stats)
    if plan is None:
        plan = _rule_based_plan(*stats)
    _plan_cache[key] = plan
    return plan
