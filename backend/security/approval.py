"""敏感权限申请-审批流 + 权限变更历史

设计：
- 所有权限变更（无论管理员直接改，还是审批通过后落地）都只走 apply_change() 一条路径，
  保证「变更历史」永不遗漏（单点审计）。
- 触及敏感对象（敏感列/敏感指标/敏感数据集/角色授权）的变更：
  · 非 admin 提交 → 生成待审批单（不立即生效）
  · admin 直接改 → 立即生效，同时补记一条 auto_approved 审批单，保留完整变更轨迹
- 存储：backend/auth_approvals.json（申请单）+ backend/logs/permission_changes.jsonl（变更历史）
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from security import model as perm_model
from config import AUTH_FULL_MODE

_BASE = Path(__file__).resolve().parent.parent
_APPROVALS_FILE = _BASE / "auth_approvals.json"
_CHANGE_LOG = _BASE / "logs" / "permission_changes.jsonl"

_lock = threading.RLock()

STATUS = ("pending", "approved", "rejected", "auto_approved")

# 申请类型 → 中文名（前端展示）
KINDS = {
    "role_grant": "用户角色授权",
    "role_def": "角色定义变更",
    "dataset": "数据集权限变更",
    "column": "列权限/脱敏变更",
    "row": "行权限变更",
    "metric": "指标口径权限变更",
    "sensitive": "敏感对象标记变更",
    "user_attr": "用户属性变更",
    "data_access": "员工数据访问授权",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> list[dict]:
    if not _APPROVALS_FILE.exists():
        return []
    try:
        data = json.loads(_APPROVALS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list[dict]) -> None:
    _APPROVALS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def log_change(actor: str, action: str, detail: dict, status: str = "applied") -> None:
    """权限变更历史（只增不改）"""
    try:
        _CHANGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": _now(), "actor": actor, "action": action,
                 "status": status, "detail": detail}
        with _lock:
            with open(_CHANGE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
    try:  # 同时进主审计流，方便统一查询
        from auth import audit
        audit("permission_change", actor=actor, action=action,
              target=str(detail.get("target") or ""), status=status)
    except Exception:
        pass


def get_change_history(limit: int = 200) -> list[dict]:
    out: list[dict] = []
    try:
        if _CHANGE_LOG.exists():
            for line in _CHANGE_LOG.read_text(encoding="utf-8").splitlines()[-limit:]:
                try:
                    item = json.loads(line)
                    # 把 detail.target / detail.payload 提升到顶层，方便前端直接渲染
                    detail = item.get("detail") or {}
                    item.setdefault("target", detail.get("target") or "")
                    item.setdefault("payload", detail.get("payload"))
                    out.append(item)
                except Exception:
                    continue
    except Exception:
        pass
    return list(reversed(out))


# ── 变更落地（唯一入口）─────────────────────────────────

def apply_change(payload: dict, actor: str) -> dict:
    """执行一次权限变更指令。payload.action 决定语义，返回 {ok, message}"""
    action = str(payload.get("action") or "")
    before = _snapshot(payload)

    if action == "set_role_section":
        role = str(payload["role"])
        section = str(payload["section"])
        perm_model.set_role_section(role, section, payload.get("value"), actor=actor)
        msg = f"角色「{role}」的 {section} 策略已更新"
    elif action == "upsert_role":
        perm_model.upsert_role(dict(payload.get("role") or {}), actor=actor)
        msg = f"角色「{(payload.get('role') or {}).get('key')}」已保存"
    elif action == "apply_role_template":
        perm_model.apply_role_template(str(payload.get("role") or ""),
                                      str(payload.get("template") or ""), actor=actor)
        msg = f"角色「{payload.get('role')}」已按模板「{payload.get('template')}」套用"
    elif action == "delete_role":
        perm_model.delete_role(str(payload["role"]), actor=actor)
        msg = f"角色「{payload['role']}」已删除"
    elif action == "upsert_dataset":
        perm_model.upsert_dataset(dict(payload.get("dataset") or {}), actor=actor)
        msg = f"数据集「{(payload.get('dataset') or {}).get('key')}」已保存"
    elif action == "delete_dataset":
        perm_model.delete_dataset(str(payload["key"]), actor=actor)
        msg = f"数据集「{payload['key']}」已删除"
    elif action == "set_sensitive":
        perm_model.set_sensitive(str(payload["kind"]), list(payload.get("items") or []), actor=actor)
        msg = f"敏感{payload['kind']}标记已更新"
    elif action == "set_user_roles":
        from auth import set_user_roles
        set_user_roles(str(payload["username"]), list(payload.get("roles") or []))
        msg = f"用户「{payload['username']}」角色已更新为 {', '.join(payload.get('roles') or []) or '（无）'}"
    elif action == "set_user_attributes":
        from auth import set_user_attributes
        set_user_attributes(str(payload["username"]), dict(payload.get("attributes") or {}))
        msg = f"用户「{payload['username']}」属性已更新"
    elif action == "set_user_grants":
        perm_model.set_user_grants(str(payload["username"]),
                                  dict(payload.get("grants") or {}), actor=actor)
        msg = f"用户「{payload['username']}」的数据访问授权（表/字段白名单）已更新"
    elif action == "create_metric":
        from agent.metric_registry import create_user_metric
        create_user_metric(dict(payload.get("metric") or {}))
        msg = f"指标「{(payload.get('metric') or {}).get('name', '')}」已创建"
    else:
        raise ValueError(f"未知的权限变更指令：{action}")

    log_change(actor, action, {"target": _target_of(payload), "payload": payload,
                               "before": before, "after": _snapshot(payload)})
    # 权限即代码：把 auth_permissions.json 的本次变更提交到 Git（容错，失败不影响业务）
    commit_hash = None
    try:
        from security.permission_vcs import commit_permission_change
        commit_hash = commit_permission_change(actor, action, _target_of(payload))
    except Exception:
        commit_hash = None
    return {"ok": True, "message": msg, "commit": commit_hash}


def _target_of(payload: dict) -> str:
    action = str(payload.get("action") or "")
    if action == "set_role_section":
        return f"{payload.get('role')}.{payload.get('section')}"
    if action == "create_metric":
        return str((payload.get("metric") or {}).get("name") or "")
    if action in ("upsert_role",):
        return str((payload.get("role") or {}).get("key") or "")
    if action in ("delete_role",):
        return str(payload.get("role") or "")
    if action == "apply_role_template":
        return str(payload.get("role") or "")
    if action == "upsert_dataset":
        return str((payload.get("dataset") or {}).get("key") or "")
    if action == "delete_dataset":
        return str(payload.get("key") or "")
    if action == "set_sensitive":
        return str(payload.get("kind") or "")
    if action in ("set_user_roles", "set_user_attributes", "set_user_grants"):
        return str(payload.get("username") or "")
    return ""


def _snapshot(payload: dict) -> dict:
    """取变更对象的当前状态（供 before/after 对照，只取相关片段避免日志膨胀）"""
    try:
        action = str(payload.get("action") or "")
        if action == "set_role_section":
            pol = perm_model.get_policy(str(payload.get("role") or ""))
            return {str(payload.get("section")): pol.get(str(payload.get("section")))}
        if action in ("upsert_role", "delete_role"):
            key = (payload.get("role") or {}).get("key") if isinstance(payload.get("role"), dict) \
                else payload.get("role")
            return {"role": perm_model.get_role(str(key or ""))}
        if action == "apply_role_template":
            return {"role": perm_model.get_role(str(payload.get("role") or ""))}
        if action in ("upsert_dataset", "delete_dataset"):
            key = (payload.get("dataset") or {}).get("key") if isinstance(payload.get("dataset"), dict) \
                else payload.get("key")
            return {"dataset": next((d for d in perm_model.list_datasets()
                                     if d.get("key") == str(key or "")), None)}
        if action == "set_sensitive":
            return {"sensitive": perm_model.load_model().get("sensitive", {}).get(str(payload.get("kind")))}
        if action in ("set_user_roles", "set_user_attributes"):
            from auth import find_user
            u = find_user(str(payload.get("username") or "")) or {}
            return {"roles": u.get("roles") or ([u["role"]] if u.get("role") else []),
                    "attributes": u.get("attributes") or {}}
        if action == "set_user_grants":
            return {"grants": perm_model.get_user_grants(str(payload.get("username") or ""))}
    except Exception:
        pass
    return {}


# ── 敏感判定 ─────────────────────────────────────────────

def is_sensitive_payload(payload: dict) -> tuple[bool, str]:
    """判断变更是否触及敏感对象 → 是否需要审批。返回 (需审批, 原因)"""
    action = str(payload.get("action") or "")
    if action in ("upsert_role", "delete_role", "apply_role_template", "set_user_roles", "set_sensitive", "create_metric"):
        return True, "涉及角色定义 / 用户授权 / 敏感对象标记 / 指标口径定义"
    if action == "set_user_grants":
        return True, "涉及员工数据访问授权（表/字段白名单）"
    if action == "set_role_section":
        section = str(payload.get("section"))
        value = payload.get("value") or {}
        if section == "datasets":
            hit = [k for k in (value or []) if k in perm_model.sensitive_datasets()]
            if hit:
                return True, f"授权了敏感数据集：{', '.join(hit)}"
        if section == "columns":
            sens = perm_model.sensitive_columns()
            hit = [f"{t}.{c}" for t, cols in (value or {}).items() for c in cols
                   if f"{t}.{c}".lower() in sens]
            if hit:
                return True, f"变更了敏感字段策略：{', '.join(hit)}"
        if section == "metrics":
            sens = perm_model.sensitive_metrics()
            hit = [m for m in (value or {}) if m in sens]
            if hit:
                return True, f"变更了敏感指标口径：{', '.join(hit)}"
        if section == "rows":
            for t in (value or {}):
                if perm_model.is_sensitive_change("row", t):
                    return True, f"变更了敏感数据集下的行权限：{t}"
    if action == "upsert_dataset" and (payload.get("dataset") or {}).get("sensitive"):
        return True, "新增/修改了敏感数据集"
    if action == "delete_dataset":
        key = str(payload.get("key") or "")
        if key in perm_model.sensitive_datasets():
            return True, f"删除了敏感数据集：{key}"
    return False, ""


# ── 审批单 ───────────────────────────────────────────────

def create_request(requester: str, kind: str, payload: dict, reason: str = "",
                   auto_status: str = "pending", reviewer: str = "") -> dict:
    item = {
        "id": uuid.uuid4().hex[:12],
        "created_at": _now(),
        "requester": requester,
        "kind": kind if kind in KINDS else "role_grant",
        "kind_label": KINDS.get(kind, kind),
        "target": _target_of(payload),
        "payload": payload,
        "reason": reason,
        "status": auto_status if auto_status in STATUS else "pending",
        "reviewer": reviewer,
        "reviewed_at": _now() if auto_status != "pending" else "",
        "review_comment": "管理员直接变更（自动留痕）" if auto_status == "auto_approved" else "",
    }
    with _lock:
        items = _load()
        items.append(item)
        _save(items)
    return item


def list_requests(status: str = "", limit: int = 200) -> list[dict]:
    items = _load()
    if status:
        items = [i for i in items if i.get("status") == status]
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items[:limit]


def get_request(req_id: str) -> dict | None:
    return next((i for i in _load() if i.get("id") == req_id), None)


def review_request(req_id: str, approve: bool, reviewer: str, comment: str = "") -> dict:
    """审批：通过则立即落地变更；驳回只记录。返回更新后的申请单"""
    with _lock:
        items = _load()
        target = next((i for i in items if i.get("id") == req_id), None)
        if not target:
            raise ValueError(f"申请单 {req_id} 不存在")
        if target.get("status") != "pending":
            raise ValueError(f"申请单 {req_id} 已处理（当前状态：{target.get('status')}）")
        if approve:
            apply_change(dict(target.get("payload") or {}), actor=f"{reviewer}(approve:{req_id})")
            target["status"] = "approved"
        else:
            target["status"] = "rejected"
            log_change(reviewer, "reject_request",
                       {"target": target.get("target"), "request_id": req_id,
                        "payload": target.get("payload")}, status="rejected")
        target["reviewer"] = reviewer
        target["reviewed_at"] = _now()
        target["review_comment"] = comment
        _save(items)
        return target


def submit_change(payload: dict, actor: str, actor_roles: list[str], reason: str = "",
                  kind: str = "") -> dict:
    """统一变更入口（API 调用它）：

    - 敏感变更 + 非 admin + 完整模式（AUTH_FULL_MODE=1） → 生成待审批单，不落地
    - 敏感变更 + 非 admin + 轻量模式（AUTH_FULL_MODE=0） → 直接落地 + auto_approved 留痕
    - 敏感变更 + admin    → 立即落地 + 自动留痕审批单
    - 非敏感变更          → 立即落地
    """
    sensitive, why = is_sensitive_payload(payload)
    is_admin = "admin" in (actor_roles or [])
    kind = kind or _kind_of(payload)
    if sensitive and not is_admin and AUTH_FULL_MODE:
        req = create_request(actor, kind, payload, reason=reason or why)
        return {"ok": True, "pending": True, "request": req,
                "message": f"该变更涉及敏感权限（{why}），已提交审批，等待管理员审核"}
    res = apply_change(payload, actor=actor)
    if sensitive:
        create_request(actor, kind, payload, reason=reason or why,
                       auto_status="auto_approved", reviewer=actor)
    return {"ok": True, "pending": False, "message": res["message"]}


def _kind_of(payload: dict) -> str:
    action = str(payload.get("action") or "")
    if action == "set_role_section":
        return {"datasets": "dataset", "tables": "dataset", "rows": "row",
                "columns": "column", "metrics": "metric"}.get(str(payload.get("section")), "dataset")
    if action in ("upsert_role", "delete_role"):
        return "role_def"
    if action in ("upsert_dataset", "delete_dataset"):
        return "dataset"
    if action == "create_metric":
        return "metric"
    if action == "set_sensitive":
        return "sensitive"
    if action == "set_user_roles":
        return "role_grant"
    if action == "set_user_attributes":
        return "user_attr"
    if action == "set_user_grants":
        return "data_access"
    return "role_grant"
