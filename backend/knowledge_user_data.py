"""用户业务知识数据持久化（收藏 / 反馈 / 人工覆盖 / 自定义术语 / 模板使用）

存储于 backend/knowledge_user_data.json，按 username 分桶：
{
  "<username>": {
    "favorites": ["obj:dim_product", ...],
    "feedback": {"<key>": {"vote": "up"|"down", "reason": "...", "ts": "..."}},
    "overrides": {"<key>": {"kind": "...", "patch": {...}}},
    "custom_terms": [{term, en, definition, category, knowledge_type, abbreviation, data_type, mapped_table}],
    "template_use_count": {"tpl_001": 3}
  }
}

key 约定：obj:<table> / met:<name> / rule:<name> / topic:<name> / term:<term> / tpl:<id>
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_DATA_FILE = Path(__file__).resolve().parent / "knowledge_user_data.json"
_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is not None:
            return _cache
        if _DATA_FILE.exists():
            try:
                _cache = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
            except Exception:
                _cache = {}
        else:
            _cache = {}
        if not isinstance(_cache, dict):
            _cache = {}
        return _cache


def _save(data: dict) -> None:
    global _cache
    with _lock:
        _DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _cache = data


def _bucket(data: dict, username: str) -> dict:
    username = (username or "guest").strip() or "guest"
    b = data.setdefault(username, {})
    b.setdefault("favorites", [])
    b.setdefault("feedback", {})
    b.setdefault("overrides", {})
    b.setdefault("custom_terms", [])
    b.setdefault("template_use_count", {})
    return b


def get_user_data(username: str) -> dict:
    """返回当前用户的全部个人知识数据（脱敏、无密码等无关字段）"""
    data = _load()
    b = _bucket(data, (username or "guest").strip() or "guest")
    return {
        "username": (username or "guest").strip() or "guest",
        "favorites": list(b.get("favorites") or []),
        "feedback": dict(b.get("feedback") or {}),
        "overrides": {k: dict(v) for k, v in (b.get("overrides") or {}).items()},
        "custom_terms": [dict(t) for t in (b.get("custom_terms") or [])],
        "template_use_count": dict(b.get("template_use_count") or {}),
    }


def set_favorite(username: str, key: str, value: bool) -> list:
    data = _load()
    b = _bucket(data, username)
    favs = b["favorites"]
    key = str(key or "").strip()
    if not key:
        return list(favs)
    if value and key not in favs:
        favs.append(key)
    elif not value and key in favs:
        favs.remove(key)
    _save(data)
    return list(favs)


def set_feedback(username: str, key: str, vote: str, reason: str = "") -> dict:
    data = _load()
    b = _bucket(data, username)
    key = str(key or "").strip()
    vote = ("up" if vote == "up" else "down") if key else ""
    if not key or not vote:
        raise ValueError("反馈内容不完整")
    b["feedback"][key] = {
        "vote": vote,
        "reason": str(reason or "")[:500],
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _save(data)
    return b["feedback"][key]


def set_override(username: str, key: str, kind: str, patch: dict) -> dict:
    """保存人工覆盖（编辑自动生成的知识）。patch 为要修改的字段，enabled=false 表示停用。"""
    data = _load()
    b = _bucket(data, username)
    key = str(key or "").strip()
    kind = str(kind or "").strip()
    if not key or not kind:
        raise ValueError("覆盖目标不完整")
    clean = {}
    for k, v in (patch or {}).items():
        if k in ("enabled",):
            clean[k] = bool(v)
        elif v is None:
            continue
        else:
            clean[k] = v
    existing = b["overrides"].get(key, {})
    merged = dict(existing)
    merged["kind"] = kind
    merged["patch"] = {**existing.get("patch", {}), **clean}
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    b["overrides"][key] = merged
    _save(data)
    return merged


def remove_override(username: str, key: str) -> bool:
    data = _load()
    b = _bucket(data, username)
    key = str(key or "").strip()
    if key in b["overrides"]:
        b["overrides"].pop(key)
        _save(data)
        return True
    return False


def add_custom_term(username: str, term: dict) -> dict:
    data = _load()
    b = _bucket(data, username)
    name = str((term or {}).get("term", "")).strip()
    if not name:
        raise ValueError("术语名称不能为空")
    existing = b["custom_terms"]
    # 同名覆盖更新
    for t in existing:
        if t.get("term") == name:
            t.update({
                "en": str(term.get("en") or "").strip(),
                "definition": str(term.get("definition") or "").strip(),
                "category": str(term.get("category") or "").strip(),
                "knowledge_type": str(term.get("knowledge_type") or "业务对象").strip(),
                "abbreviation": str(term.get("abbreviation") or "").strip(),
                "data_type": str(term.get("data_type") or "").strip(),
                "mapped_table": str(term.get("mapped_table") or "").strip(),
                "custom": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            _save(data)
            return dict(t)
    new_term = {
        "term": name,
        "en": str(term.get("en") or "").strip(),
        "definition": str(term.get("definition") or "").strip(),
        "category": str(term.get("category") or "").strip(),
        "knowledge_type": str(term.get("knowledge_type") or "业务对象").strip(),
        "abbreviation": str(term.get("abbreviation") or "").strip(),
        "data_type": str(term.get("data_type") or "").strip(),
        "mapped_table": str(term.get("mapped_table") or "").strip(),
        "custom": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    existing.append(new_term)
    _save(data)
    return dict(new_term)


def remove_custom_term(username: str, term_name: str) -> bool:
    data = _load()
    b = _bucket(data, username)
    name = str(term_name or "").strip()
    before = len(b["custom_terms"])
    b["custom_terms"] = [t for t in b["custom_terms"] if t.get("term") != name]
    if len(b["custom_terms"]) != before:
        _save(data)
        return True
    return False


def record_template_use(username: str, template_id: str) -> int:
    data = _load()
    b = _bucket(data, username)
    tid = str(template_id or "").strip()
    if not tid:
        return 0
    b["template_use_count"][tid] = b["template_use_count"].get(tid, 0) + 1
    _save(data)
    return b["template_use_count"][tid]


# ── 覆盖合并（在缓存结果之上做纯内存操作，避免每次编辑触发 17s 全库重建）────────

def _merged_overrides(username: str) -> dict:
    return dict(get_user_data(username).get("overrides") or {})


def apply_scene_overrides(username: str, data: dict) -> dict:
    """把用户人工覆盖应用到 scenes 结果：改 label/desc/字段翻译、停用对象/指标/规则/主题。"""
    ov = _merged_overrides(username)
    if not ov:
        return data
    scenes = dict(data.get("scenes") or {})

    for key, item in ov.items():
        patch = item.get("patch") or {}
        enabled = patch.get("enabled", True)
        if key.startswith("obj:"):
            table = key[4:]
            for s in scenes.values():
                for o in s.get("objects") or []:
                    if o.get("table") == table:
                        _apply_patch(o, patch, enabled)
        elif key.startswith("met:"):
            name = key[4:]
            for s in scenes.values():
                for m in s.get("metrics") or []:
                    if m.get("name") == name:
                        _apply_patch(m, patch, enabled)
        elif key.startswith("rule:"):
            name = key[5:]
            for s in scenes.values():
                for r in s.get("rules") or []:
                    if r.get("name") == name:
                        _apply_patch(r, patch, enabled)
        elif key.startswith("topic:"):
            name = key[6:]
            for s in scenes.values():
                for t in s.get("topics") or []:
                    if t.get("name") == name:
                        _apply_patch(t, patch, enabled)
        elif key.startswith("field:"):
            ref = key[6:]
            if "." in ref:
                table, column = ref.split(".", 1)
                for s in scenes.values():
                    for o in s.get("objects") or []:
                        if o.get("table") == table:
                            for c in o.get("columns") or []:
                                if c.get("name") == column:
                                    if not enabled:
                                        # 停用字段：打标记（前端隐藏）
                                        c["_disabled"] = True
                                    elif "translation" in patch:
                                        c["translation"] = patch["translation"]
    return {"scenes": scenes}


def _apply_patch(obj: dict, patch: dict, enabled: bool) -> None:
    if not enabled:
        obj["_disabled"] = True
        return
    for k, v in patch.items():
        if k == "enabled":
            continue
        obj[k] = v


def apply_term_overrides(username: str, data: dict) -> dict:
    """把人工覆盖 + 自定义术语合并到术语词典结果。"""
    ov = _merged_overrides(username)
    terms = [dict(t) for t in (data.get("terms") or [])]
    for key, item in ov.items():
        if not key.startswith("term:"):
            continue
        name = key[5:]
        patch = item.get("patch") or {}
        enabled = patch.get("enabled", True)
        for t in terms:
            if t.get("term") == name:
                if not enabled:
                    t["_disabled"] = True
                else:
                    for k, v in patch.items():
                        if k != "enabled":
                            t[k] = v
                break
    # 追加用户自定义术语（在自动生成之后）
    custom = get_user_data(username).get("custom_terms") or []
    existing_names = {t.get("term") for t in terms}
    for c in custom:
        if c.get("term") in existing_names:
            # 同名：用自定义覆盖自动生成的
            for t in terms:
                if t.get("term") == c.get("term"):
                    t.update(c)
                    break
        else:
            terms.append(dict(c))
    # 过滤停用的术语
    terms = [t for t in terms if not t.get("_disabled")]
    return {"terms": terms}
