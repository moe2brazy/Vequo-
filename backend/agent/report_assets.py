# -*- coding: utf-8 -*-
"""报告资产沉淀（P1-4，对标白泽智能报表/Aloudata 报告工作台：报告可沉淀复用）

现状：报告生成/导出后即终止，不沉淀为可复用资产。
本模块：报告入库（标题/章节/关联表/创建人/权限）→ 列表检索 → 详情 → 一键再生成。

- 存储：backend/report_assets.json（与项目既有 JSON 存储风格一致，零新依赖）
- 权限：管理员 可见全部；普通角色仅可见自己创建的报告（与指标管理权限口径一致）
- 章节：按报告模板分隔符（---xxx---）自动切分，供前端「章节骨架」展示与再生成复用
- 再生成：由调用方（main.py 路由）重新构建数据上下文并重跑生成链路，本模块负责覆盖保存
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

_logger = logging.getLogger("report_assets")

_FILE = Path(__file__).resolve().parent.parent / "report_assets.json"
_lock = threading.Lock()

# 报告模板章节分隔符（与 report_agent 输出约定一致）
_SECTION_RE = re.compile(r"---(.+?)---")

_MAX_ASSETS = 200


def _load() -> list[dict]:
    try:
        if _FILE.exists():
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception as e:
        _logger.warning("报告资产加载失败: %s", e)
    return []


def _save(assets: list[dict]) -> None:
    try:
        _FILE.write_text(json.dumps(assets, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        _logger.warning("报告资产保存失败（保持内存）: %s", e)


def split_sections(markdown: str) -> list[dict]:
    """按报告模板分隔符切分章节 → [{title, content}]；无分隔符时整篇为一段。"""
    if not markdown:
        return []
    parts = _SECTION_RE.split(markdown or "")
    sections: list[dict] = []
    # split 结果：[前文, title1, content1, title2, content2, 后文]
    for i in range(1, len(parts) - 1, 2):
        title = (parts[i] or "").strip()
        content = (parts[i + 1] or "").strip()
        if title and content:
            sections.append({"title": title, "content": content})
    if not sections:
        sections.append({"title": "全文", "content": (markdown or "").strip()})
    return sections


def save_report(title: str, markdown: str, tables: list[str] | None,
                created_by: str, role: str) -> dict:
    """保存报告资产；返回资产条目。同名报告追加新版本（时间戳区分）。"""
    asset = {
        "id": uuid.uuid4().hex[:10],
        "title": (title or "数据总览报告").strip()[:80],
        "markdown": markdown or "",
        "sections": split_sections(markdown),
        "tables": [str(t) for t in (tables or [])][:20],
        "created_by": created_by or "unknown",
        "role": role or "guest",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        assets = _load()
        assets.append(asset)
        if len(assets) > _MAX_ASSETS:
            assets = assets[-_MAX_ASSETS:]
        _save(assets)
    return asset


def list_reports(role: str = "", username: str = "") -> list[dict]:
    """列表（摘要，不含正文），按角色可见性过滤 + 时间倒序。"""
    with _lock:
        assets = _load()
    if role in ("admin",):
        vis = assets
    else:
        vis = [a for a in assets if a.get("created_by") == username]
    vis = sorted(vis, key=lambda a: str(a.get("created_at") or ""), reverse=True)
    out = []
    for a in vis:
        item = {k: v for k, v in a.items() if k != "markdown"}
        item["section_count"] = len(a.get("sections") or [])
        out.append(item)
    return out


def get_report(rid: str) -> dict | None:
    with _lock:
        for a in _load():
            if a.get("id") == rid:
                return dict(a)
    return None


def update_report(rid: str, patch: dict) -> dict | None:
    """再生成后覆盖内容（markdown/sections/tables 更新，created_at 刷新）。"""
    with _lock:
        assets = _load()
        for a in assets:
            if a.get("id") == rid:
                if "markdown" in patch:
                    a["markdown"] = patch["markdown"] or ""
                    a["sections"] = split_sections(patch["markdown"])
                if "tables" in patch:
                    a["tables"] = [str(t) for t in (patch["tables"] or [])][:20]
                a["regenerated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _save(assets)
                return dict(a)
    return None


def delete_report(rid: str) -> bool:
    with _lock:
        assets = _load()
        before = len(assets)
        assets = [a for a in assets if a.get("id") != rid]
        if len(assets) != before:
            _save(assets)
            return True
    return False


def count_assets() -> int:
    with _lock:
        return len(_load())
