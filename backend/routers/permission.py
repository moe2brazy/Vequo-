# -*- coding: utf-8 -*-
"""权限管理 API（语义层 + MQL→SQL 架构下的权限控制后台）

统一入口：所有写操作经 security.approval.submit_change 落地 →
敏感变更自动进入审批流，非敏感变更直接生效，全部留审计。
查询链路侧（LLMService/_exec_sql）已在引擎层统一走 security.enforcer.rewrite_sql，
本路由只负责「配置管理 / 审批 / 审计 / 模拟器」四类能力。
"""
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from typing import Any, Optional

from auth import (get_current_user, get_user_roles, load_users,
                  get_audit_logs)
from security import model as perm_model
from security import enforcer, approval
from database import get_db
from config import AUTH_FULL_MODE

router = APIRouter(prefix="/api/permission", tags=["权限管理"])


# ── 权限控制辅助（支持多角色 roles 数组）───────────────────

def _user_roles(u: dict) -> set[str]:
    roles = {str(r) for r in (u.get("roles") or []) if str(r)}
    if u.get("role"):
        roles.add(str(u["role"]))
    return roles


def _require_login(authorization: str = Header(None)) -> dict:
    u = get_current_user(authorization)
    if u["role"] == "guest":
        raise HTTPException(status_code=401, detail="请先登录")
    return u


def _require_admin(authorization: str = Header(None)) -> dict:
    """仅允许 admin。

    修复：本文件原先**重复定义了两次** _require_admin（「同名函数重复定义」），
    Python 顶层顺序执行时后者静默覆盖前者，前一份成为彻底死代码；
    且被保留的那份 403 文案是"需要 管理员/分析师 角色"，会让运维误以为分析师也能审批权限。
    现合并为一份，文案与真实语义一致。
    """
    u = get_current_user(authorization)
    if u["role"] == "guest":
        raise HTTPException(status_code=401, detail="请先登录")
    if not (_user_roles(u) & {"admin"}):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return u


def _ok(**kw) -> dict:
    return {"ok": True, **kw}


# ── 请求模型 ─────────────────────────────────────────────

class RolePayload(BaseModel):
    key: str
    name: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[int] = None


class TemplateApplyPayload(BaseModel):
    role: str               # 目标角色 key
    template: str           # 模板 key（readonly / region_manager）


class DatasetPayload(BaseModel):
    key: str
    name: Optional[str] = None
    tables: Optional[list[str]] = None
    description: Optional[str] = None
    sensitive: Optional[bool] = None


class PolicyPayload(BaseModel):
    role: str
    section: str            # datasets / tables / rows / columns / metrics
    value: Any


class SensitivePayload(BaseModel):
    kind: str               # columns / metrics / datasets
    items: list[str]


class UserRolesPayload(BaseModel):
    username: str
    roles: list[str]


class UserAttributesPayload(BaseModel):
    username: str
    attributes: dict


class UserGrantsPayload(BaseModel):
    """员工级数据访问授权：{ "<table>": ["col1", "col2", ...] }；空 dict = 撤销授权"""
    grants: dict


class UserMaintainPayload(BaseModel):
    """账号维护（权限页用）：改资料 / 重置密码 / 启用禁用。

    所有字段可选，只处理传进来的那些（exclude_unset 语义）。
    - profile 类字段：display_name / email / phone / department / title / bio / note
    - password：重置为新密码，后端走 password_strength 强校验
    - enabled：启用（true）/ 禁用（false）
    """
    display_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    department: Optional[str] = None
    title: Optional[str] = None
    bio: Optional[str] = None
    note: Optional[str] = None
    password: Optional[str] = None
    enabled: Optional[bool] = None


class ChangePayload(BaseModel):
    action: str
    payload: dict
    reason: Optional[str] = ""
    kind: Optional[str] = ""


class RequestPayload(BaseModel):
    kind: str
    payload: dict
    reason: Optional[str] = ""


class ReviewPayload(BaseModel):
    approve: bool
    comment: Optional[str] = ""


class SimulatePayload(BaseModel):
    username: str
    sql: str
    execute: Optional[bool] = False
    dialect: Optional[str] = ""   # 空 = 自动探测（postgres 默认）


def _safe_user(u: dict) -> dict:
    """用户在权限页的可见视图（不含 password_hash）。

    补 enabled / last_login / 部门职位等字段的原因：权限页原先只拿到角色和授权，
    表格里**看不出谁被禁用了、谁从没登录过、谁还没分配角色**——管理员要维护账号，
    就得先看见状态。这些字段 /api/auth/users 早就有了，这里对齐一下。
    """
    _grants = perm_model.get_user_grants(u.get("username", ""))
    _roles = get_user_roles(u)
    # 未分配：只有 pending 角色（自助注册产生），或被清空
    _unassigned = (not _roles) or _roles == ["pending"]
    return {
        "username": u.get("username", ""),
        "role": u.get("role", ""),
        "roles": _roles,
        "attributes": u.get("attributes") or {},
        "display_name": u.get("display_name", u.get("username", "")),
        "avatar": u.get("avatar", ""),
        "created_at": u.get("created_at", ""),
        "note": str(u.get("note") or ""),
        # 例外授权（per-user 表/字段白名单）：空 = 走角色授权；非空 = 覆盖角色，仅可看这些表
        "has_grants": bool(_grants),
        "grant_tables": sorted(_grants.keys()) if _grants else [],
        # ── 账号维护状态 ──
        "enabled": bool(u.get("enabled", True)),
        "last_login": u.get("last_login", "") or "",
        "email": u.get("email", ""),
        "phone": u.get("phone", ""),
        "department": u.get("department", ""),
        "title": u.get("title", ""),
        "bio": u.get("bio", ""),
        # 内置演示账号：后端禁止改角色/禁用/删除，前端据此置灰按钮
        "builtin": u.get("username") in ("admin", "viewer"),
        # 还没配任何角色（pending 或空）：数据权限为零，需要管理员补角色
        "unassigned": _unassigned,
    }


# ── 元信息 / 模型视图 ────────────────────────────────────

@router.get("/meta", dependencies=[Depends(_require_admin)])
def meta():
    """权限系统常量（供前端渲染下拉框/说明）"""
    # 用户属性键集合（行级规则「取值来源」下拉用）：所有用户 attributes 键并集 + username
    # + 多租户组织隔离标准键（tenant_id/org_id，即使暂无用户配置也提示可用）
    attr_keys: set[str] = {"username", "tenant_id", "org_id"}
    for u in load_users():
        for k in (u.get("attributes") or {}):
            if str(k).strip():
                attr_keys.add(str(k).strip())
    return _ok(
        system_roles=perm_model.SYSTEM_ROLES,
        column_modes=perm_model.COLUMN_MODES,
        metric_modes=perm_model.METRIC_MODES,
        mask_types=enforcer.MASK_TYPES,
        statuses=list(approval.STATUS),
        kinds={k: {"label": v} for k, v in approval.KINDS.items()},
        sections=["datasets", "tables", "rows", "columns", "metrics"],
        auth_full_mode=AUTH_FULL_MODE,   # P4：轻量模式前端隐藏审批中心
        user_attribute_keys=sorted(attr_keys),
    )


@router.get("/templates", dependencies=[Depends(_require_admin)])
def list_templates():
    """角色模板清单（供权限页「新建角色」下拉）"""
    return _ok(templates=perm_model.list_role_templates())


@router.post("/templates/apply", dependencies=[Depends(_require_admin)])
def apply_template(p: TemplateApplyPayload, u: dict = Depends(_require_admin)):
    return _change("apply_role_template", {"role": p.role, "template": p.template}, u,
                   reason=f"按模板「{p.template}」套用角色「{p.role}」")


@router.get("/model", dependencies=[Depends(_require_admin)])
def model_view():
    """权限模型全量（角色/数据集/策略/敏感标记）"""
    m = perm_model.load_model()
    return _ok(
        version=m.get("version"),
        roles=m.get("roles") or [],
        datasets=m.get("datasets") or [],
        policies=m.get("policies") or {},
        sensitive=m.get("sensitive") or {},
        updated_at=m.get("updated_at", ""),
        updated_by=m.get("updated_by", ""),
    )


@router.post("/reload", dependencies=[Depends(_require_admin)])
def reload_model(u: dict = Depends(_require_admin)):
    """重新从磁盘读取权限模型，刷新进程内缓存。

    为什么需要它：`security/model.py::load_model` 把模型缓存在模块级 `_cache` 里，
    正常只在整个后端进程重启时才重新读盘。若直接用脚本/`security.model` 的 API 改
    `auth_permissions.json`（而不是走本文件的 /change 接口），跑着的服务仍端着旧模型，
    表现为「新角色已建好、账号也能登录，但看不到任何场景」——排查时很容易误判成配错了。
    这里补一个不重启就能生效的入口，顺带把列级缓存一起失效。
    """
    m = perm_model.load_model(refresh=True)
    try:
        enforcer.invalidate_column_cache()
    except Exception:
        pass
    return _ok(
        message="权限模型已重新加载",
        revision=m.get("updated_at", ""),
        roles=[r.get("key") for r in (m.get("roles") or [])],
        policies=sorted((m.get("policies") or {}).keys()),
        updated_by=m.get("updated_by", ""),
    )


# ── 角色 / 数据集 CRUD ───────────────────────────────────
# 注意：所有写操作统一走 approval.submit_change（唯一变更入口）——
# 保证「敏感变更走审批 + 全部变更留审计」；admin 的敏感变更落地同时补记 auto_approved。

def _change(action: str, payload: dict, u: dict, reason: str = "") -> dict:
    """统一提交一次权限变更（当前操作者身份注入审计）"""
    try:
        payload = dict(payload)
        payload.setdefault("action", action)
        res = approval.submit_change(payload, actor=u["username"],
                                     actor_roles=list(_user_roles(u)), reason=reason)
        return _ok(**res)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/roles", dependencies=[Depends(_require_admin)])
def save_role(p: RolePayload, u: dict = Depends(_require_admin)):
    return _change("upsert_role", {"role": p.model_dump()}, u,
                   reason=f"保存角色「{p.key}」")


@router.delete("/roles/{key}", dependencies=[Depends(_require_admin)])
def remove_role(key: str, u: dict = Depends(_require_admin)):
    return _change("delete_role", {"role": key}, u, reason=f"删除角色「{key}」")


@router.post("/datasets", dependencies=[Depends(_require_admin)])
def save_dataset(p: DatasetPayload, u: dict = Depends(_require_admin)):
    return _change("upsert_dataset", {"dataset": p.model_dump()}, u,
                   reason=f"保存数据集「{p.key}」")


@router.delete("/datasets/{key}", dependencies=[Depends(_require_admin)])
def remove_dataset(key: str, u: dict = Depends(_require_admin)):
    return _change("delete_dataset", {"key": key}, u, reason=f"删除数据集「{key}」")


# ── 策略节 / 敏感标记 ────────────────────────────────────

def _validate_row_rules(value: Any, db: Any) -> None:
    """校验 rows 策略中的声明式 rule（表/字段必须真实存在，防悬空配置）"""
    if not isinstance(value, dict):
        return
    full = _introspect_schema(db)
    for t, cfg in value.items():
        if not isinstance(cfg, dict):
            continue
        rule = cfg.get("rule")
        if not isinstance(rule, dict) or not rule:
            continue
        bare = str(t).split(".")[-1].lower()
        cols = {c["name"].lower() for c in (full.get(bare, {}).get("columns") or [])}
        op = str(rule.get("op") or "eq").lower()
        if op == "in_subquery":
            ref = str(rule.get("ref_table") or "").split(".")[-1].lower()
            ref_cols = {c["name"].lower() for c in (full.get(ref, {}).get("columns") or [])}
            on, where = rule.get("on") or {}, rule.get("where") or {}
            for side, col, pool, label in (
                    ("on.left", str(on.get("left") or "").lower(), cols, bare),
                    ("on.right", str(on.get("right") or "").lower(), ref_cols, ref),
                    ("where.field", str(where.get("field") or "").lower(), ref_cols, ref)):
                if col and col not in pool:
                    raise HTTPException(status_code=400,
                                        detail=f"行规则 {t}.{label}: 字段「{col}」在表 {label} 中不存在")
            if not ref:
                raise HTTPException(status_code=400, detail=f"行规则 {t}: in_subquery 缺少 ref_table")
        else:
            field = str(rule.get("field") or "").lower()
            if field and field not in cols:
                raise HTTPException(status_code=400,
                                    detail=f"行规则 {t}: 字段「{field}」在表 {bare} 中不存在")


def _validate_column_masks(value: Any) -> None:
    """列策略保存前校验脱敏算法取值。

    此前前端下拉误把整个选项对象写进 mask 字段，落库后变成 "{'key': 'partial_1_1', ...}"，
    引擎识别不了便静默降级成固定替换 '***'——配置看着对、效果全是星号。
    这里在入口直接拦下，让错误暴露在保存那一刻而不是查询结果里。
    """
    if not isinstance(value, dict):
        return
    bad: list[str] = []
    for t, cols in value.items():
        if not isinstance(cols, dict):
            continue
        for col, rule in cols.items():
            if not isinstance(rule, dict):
                continue
            if str(rule.get("mode") or "").lower() != "mask":
                continue
            key = perm_model.coerce_mask_key(rule.get("mask"))
            if key not in enforcer.MASK_KEYS:
                bad.append(f"{t}.{col}")
    if bad:
        raise HTTPException(
            status_code=400,
            detail="列脱敏算法无效（" + "、".join(bad) + "）。"
                   "请从「脱敏方式」下拉重新选择，可选：" + "、".join(enforcer.MASK_KEYS))


def _row_rule_warnings(value: Any, role: str) -> list[str]:
    """行规则保存前检查：配了规则的表是否真的授权给了该角色。

    表没授权时行过滤根本不会注入（引擎侧 _table_visible 过滤），
    配置看着生效、实际查不到效果——提前把原因说出来。
    可见性判据直接复用引擎的 _policy_tables，避免两处语义分叉：
    None = 未配置授权 = 不限制；admin 为超级管理员，不受数据权限约束。
    """
    if not isinstance(value, dict):
        return []
    role_key = str(role or "").strip()
    if not role_key or role_key == "admin":
        return []
    allowed = enforcer._policy_tables(perm_model.get_policy(role_key) or {})
    if allowed is None:
        return []
    warns: list[str] = []
    for t, cfg in value.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", True):
            continue
        has_rule = bool(str(cfg.get("expr") or "").strip()) or isinstance(cfg.get("rule"), dict)
        if not has_rule:
            continue
        bare = str(t).split(".")[-1].strip().lower()
        if bare not in allowed:
            warns.append(f"表「{bare}」未授权给角色「{role}」，该行规则不会生效")
    return warns


@router.post("/policies", dependencies=[Depends(_require_admin)])
def set_policy(p: PolicyPayload, u: dict = Depends(_require_admin),
               db: Any = Depends(get_db)):
    if p.section == "rows":
        _validate_row_rules(p.value, db)
    elif p.section == "columns":
        _validate_column_masks(p.value)
    res = _change("set_role_section",
                  {"role": p.role, "section": p.section, "value": p.value},
                  u, reason=f"更新角色「{p.role}」{p.section} 策略")
    if p.section == "rows":
        warns = _row_rule_warnings(p.value, p.role)
        if warns:
            res["warnings"] = warns + ["请先在「数据集」中为角色授权对应表。"]
    return res


@router.post("/sensitive", dependencies=[Depends(_require_admin)])
def set_sensitive(p: SensitivePayload, u: dict = Depends(_require_admin)):
    return _change("set_sensitive", {"kind": p.kind, "items": p.items}, u,
                   reason=f"更新敏感{p.kind}标记")


# ── 用户授权（角色 / 属性）───────────────────────────────

@router.get("/users", dependencies=[Depends(_require_admin)])
def users():
    try:
        return _ok(users=[_safe_user(u) for u in load_users()],
                   roles=perm_model.list_roles())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取用户失败: {e}")


@router.post("/users/roles", dependencies=[Depends(_require_admin)])
def set_roles(p: UserRolesPayload, u: dict = Depends(_require_admin)):
    return _change("set_user_roles", {"username": p.username, "roles": p.roles}, u,
                   reason=f"调整用户「{p.username}」角色")


@router.post("/users/attributes", dependencies=[Depends(_require_admin)])
def set_attributes(p: UserAttributesPayload, u: dict = Depends(_require_admin)):
    return _change("set_user_attributes",
                   {"username": p.username, "attributes": p.attributes}, u,
                   reason=f"调整用户「{p.username}」属性")


# ── 账号维护（改资料 / 重置密码 / 启用禁用 / 删除）──────────
#
# 这些动作原先只存在于 /api/auth/users/*（账户页的管理员模式里）。
# 用户要求「把管理功能加到权限管理页面」，所以在这里补一份入口，
# 让管理员不用在两个页面之间跳。
#
# 两者共用同一套底层实现（auth.update_user_admin / save_users），
# 差异只在审计口径：这里统一 audit(user_updated / user_deleted)，
# 并在 reason 里写清具体动作，审计日志里能一眼看出"重置了谁的密码"。

_BUILTIN_USERS = ("admin", "viewer")


def _load_or_404(username: str) -> dict:
    from auth import find_user
    user = find_user(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"用户「{username}」不存在")
    return user


@router.post("/users/{username}/maintain", dependencies=[Depends(_require_admin)])
def maintain_user(username: str, p: UserMaintainPayload, u: dict = Depends(_require_admin)):
    """维护单个账号：改资料、重置密码、启用/禁用。

    为什么把这些揉进一个端点：它们在 UI 上是一个"编辑账号"表单，
    分开四个端点前端要发四次请求、失败还会半途而废（改了名字没改成密码）。
    一起提交，要么全成要么全不成。
    """
    from auth import update_user_admin, audit as _audit

    _load_or_404(username)
    patch = p.model_dump(exclude_unset=True)
    # 去掉显式传 None 的项（前端表单里没填的字段会传 null，不该覆盖成空）
    patch = {k: v for k, v in patch.items() if v is not None}
    if not patch:
        raise HTTPException(status_code=400, detail="没有要修改的内容")

    # 内置账号保护：与 /api/auth/users/{username} 保持一致
    if username in _BUILTIN_USERS:
        frozen = {"enabled"} & set(patch.keys())
        if frozen:
            raise HTTPException(status_code=400, detail="内置演示账号不能禁用")

    # 防手滑：不允许管理员禁用自己的账号
    if username == u.get("username") and patch.get("enabled") is False:
        raise HTTPException(status_code=400, detail="不能禁用自己的账号（否则会把自己锁在门外）")

    # 密码走强校验，错误信息原样抛给前端
    if "password" in patch:
        from auth import password_strength
        st = password_strength(str(patch["password"]))
        if not st["ok"]:
            raise HTTPException(status_code=400, detail=st["message"])

    try:
        user = update_user_admin(username, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not user:
        raise HTTPException(status_code=404, detail=f"用户「{username}」不存在")

    # 审计：密码只记"改过"，不记明文；其余字段如实记录
    detail = {k: v for k, v in patch.items() if k != "password"}
    if "password" in patch:
        detail["password"] = "已重置"
    action = "重置密码" if "password" in patch else (
        "禁用账号" if patch.get("enabled") is False else
        "启用账号" if patch.get("enabled") is True else "修改资料")
    _audit("user_updated", username=username, action=action, **detail)

    return _ok(user=_safe_user(_load_or_404(username)), action=action)


@router.delete("/users/{username}", dependencies=[Depends(_require_admin)])
def remove_user(username: str, u: dict = Depends(_require_admin)):
    """删除账号。内置演示账号（admin / viewer）不允许删除；不允许删除自己。"""
    from auth import load_users as _load, save_users, audit as _audit

    if username in _BUILTIN_USERS:
        raise HTTPException(status_code=400, detail="内置演示账号不能删除")
    if username == u.get("username"):
        raise HTTPException(status_code=400, detail="不能删除自己的账号")

    users = _load()
    remaining = [x for x in users if x.get("username") != username]
    if len(remaining) == len(users):
        raise HTTPException(status_code=404, detail=f"用户「{username}」不存在")
    save_users(remaining)
    _audit("user_deleted", username=username)
    return _ok(deleted=username, remaining=len(remaining))


# ── 员工级数据访问授权（per-user 表/字段白名单）───────────

@router.get("/schema", dependencies=[Depends(_require_admin)])
def schema(db: Any = Depends(get_db)):
    """数据库全部表 + 字段（管理员为员工授权时勾选用；绕过行/列权限）"""
    return _ok(schema=_introspect_schema(db))


@router.get("/users/{username}/grants", dependencies=[Depends(_require_admin)])
def get_user_grants_endpoint(username: str, db: Any = Depends(get_db)):
    """某员工当前的数据访问授权（表/字段）"""
    grants = perm_model.get_user_grants(username)
    return _ok(username=username, grants=grants, has_grants=bool(grants),
               schema=_introspect_schema(db))


@router.post("/users/{username}/grants", dependencies=[Depends(_require_admin)])
def set_user_grants_endpoint(username: str, p: UserGrantsPayload,
                             u: dict = Depends(_require_admin),
                             db: Any = Depends(get_db)):
    """保存某员工的数据访问授权（表/字段白名单），敏感变更走审批流留痕"""
    # 校验：授权对象必须是真实存在的库表/字段（避免配置悬空）
    full = _introspect_schema(db)
    unknown = []
    for t, cols in (p.grants or {}).items():
        bare = str(t).split(".")[-1].strip().lower()
        if bare not in full:
            unknown.append(bare)
            continue
        valid = {c["name"].lower() for c in (full[bare].get("columns") or [])}
        for c in (cols or []):
            if str(c).strip().lower() not in valid:
                unknown.append(f"{bare}.{c}")
    if unknown:
        raise HTTPException(status_code=400,
                            detail="以下表/字段在数据库中不存在：" + ", ".join(unknown[:20]))
    return _change("set_user_grants", {"username": username, "grants": p.grants}, u,
                   reason=f"调整用户「{username}」的数据访问授权（表/字段）")


@router.get("/data-catalog")
def data_catalog(authorization: str = Header(None), db: Any = Depends(get_db)):
    """当前登录用户可访问的数据目录（表 + 字段，含脱敏/拒标）。

    - 管理员：返回全部表与字段。
    - 未授权普通员工（无角色表授权、无个人授权）：返回空（granted=false, empty=true）。
    - 其余：按生效权限（per-user 授权 + 角色策略）裁剪可见表与字段。
    供「数据资源」页按权限展示，再次登录即按最新授权刷新。
    """
    u = get_current_user(authorization)
    full = _introspect_schema(db)
    if u["role"] == "guest":
        # 开放模式匿名访客：保持「未配置即不限制」，返回全部表与字段
        tables = [{"table_name": t,
                   "columns": [{"name": c["name"], "type": c.get("type", ""),
                                "primary_key": c.get("primary_key", False),
                                "masked": False, "mask": "", "denied": False}
                               for c in (info.get("columns") or [])]}
                  for t, info in full.items()]
        return _ok(is_admin=False, granted=True, empty=False, tables=tables)
    ctx = enforcer.build_acl_for_username(u["username"])
    if ctx.superuser:
        tables = [{"table_name": t,
                   "columns": [{"name": c["name"], "type": c.get("type", ""),
                                "primary_key": c.get("primary_key", False),
                                "masked": False, "mask": "", "denied": False}
                               for c in (info.get("columns") or [])]}
                  for t, info in full.items()]
        return _ok(is_admin=True, granted=True, empty=False, tables=tables)

    allowed = ctx.allowed_tables or set()
    out = []
    for t in sorted(allowed):
        info = full.get(t)
        if not info:
            # 授权了但库里已无此表（防止悬空），仅返回表名
            out.append({"table_name": t, "columns": []})
            continue
        wl = ctx.column_whitelist.get(t)
        denies = ctx.column_denies.get(t, set())
        masks = ctx.column_masks.get(t, {})
        cols = []
        for c in (info.get("columns") or []):
            cl = c["name"].lower()
            if cl in denies:
                continue
            if wl is not None and cl not in wl:
                continue
            cols.append({"name": c["name"], "type": c.get("type", ""),
                         "primary_key": c.get("primary_key", False),
                         "masked": cl in masks, "mask": masks.get(cl, ""),
                         "denied": False})
        out.append({"table_name": t, "columns": cols})
    return _ok(is_admin=False, granted=bool(allowed) or ctx.has_user_grants,
               empty=len(allowed) == 0, tables=out)


@router.get("/available-schema", dependencies=[Depends(_require_login)])
def available_schema(db: Any = Depends(get_db)):
    """已登录用户可申请的库表清单（仅结构元信息，不含任何数据行）。

    供普通员工在「反馈/申请授权」弹窗中勾选希望访问的数据表与字段，
    提交后由管理员在审批中心审核。仅暴露表名/字段名/类型等元数据，
    不泄露业务数据；真实数据访问仍由引擎层 rewrite_sql 强制拦截。
    """
    full = _introspect_schema(db)
    tables = [{"table_name": t,
               "columns": [{"name": c["name"], "type": c.get("type", ""),
                             "primary_key": c.get("primary_key", False)}
                            for c in (info.get("columns") or [])]}
              for t, info in sorted(full.items())]
    return _ok(tables=tables)


# ── 库表内省（带 TTL 缓存，避免每请求打信息_schema）──────

_SCHEMA_CACHE: dict = {"ts": 0.0, "data": {}}


def _copy_schema(data: dict) -> dict:
    """返回结构浅拷贝（防调用方意外修改缓存对象，污染全局结构缓存）"""
    return {t: dict(info, columns=list(info.get("columns") or []))
            for t, info in data.items()}


def _introspect_schema(db: Any = None) -> dict:
    """返回 { table: {chinese_name, columns:[{name,type,primary_key,comment}]} }"""
    import time
    now = time.time()
    if _SCHEMA_CACHE["data"] and now - _SCHEMA_CACHE["ts"] < 120:
        return _copy_schema(_SCHEMA_CACHE["data"])
    try:
        from sqlalchemy import inspect as sa_inspect, text
        own = db is None
        if own:
            db = next(get_db())
        try:
            inspector = sa_inspect(db.get_bind())
            table_labels: dict[str, str] = {}
            try:
                cols = inspector.get_columns("metadata_tables")
                names = {c["name"] for c in cols}
                if {"table_name", "table_chinese_name"}.issubset(names):
                    for r in db.execute(text("SELECT table_name, table_chinese_name FROM metadata_tables")):
                        table_labels[str(r[0])] = (r[1] or "")
            except Exception:
                pass
            out: dict[str, dict] = {}
            for t in inspector.get_table_names():
                if t.startswith("_") or t.startswith("pg_") or t.startswith("metadata_"):
                    continue
                pk = set((inspector.get_pk_constraint(t) or {}).get("constrained_columns", []))
                out[t] = {
                    "chinese_name": table_labels.get(t, ""),
                    "columns": [{"name": c["name"], "type": str(c["type"]),
                                 "primary_key": c["name"] in pk,
                                 "comment": c.get("comment") or ""}
                                for c in inspector.get_columns(t)],
                }
            _SCHEMA_CACHE["ts"] = now
            _SCHEMA_CACHE["data"] = out
            return _copy_schema(out)
        finally:
            if own:
                try:
                    db.close()
                except Exception:
                    pass
    except Exception as e:
        if _SCHEMA_CACHE["data"]:
            return _copy_schema(_SCHEMA_CACHE["data"])
        raise HTTPException(status_code=500, detail=f"库表内省失败: {e}")


# ── 生效权限白盒 / 模拟器 ────────────────────────────────

@router.get("/effective/{username}", dependencies=[Depends(_require_admin)])
def effective(username: str):
    """某用户的生效权限摘要（多角色合并结果，供配置核对）"""
    from auth import find_user
    ctx = enforcer.build_acl_for_username(username)
    return _ok(acl=ctx.as_dict(),
               user_found=find_user(username) is not None)


@router.post("/simulate")
def simulate(p: SimulatePayload, user: dict = Depends(_require_admin)):
    """权限模拟器：给定用户 + SQL → 返回改写结果与应用明细（可选项执行）

    安全约束：执行（execute=True）只能以「调用者自身」权限进行——分析师模拟他人
    （如 admin）权限时仅可预览改写结果，禁止真实执行，杜绝借模拟器越权读全库明文。
    """
    from auth import find_user
    sql = (p.sql or "").strip()
    if not sql:
        raise HTTPException(status_code=400, detail="SQL 不能为空")
    caller_roles = _user_roles(user)
    is_admin = "admin" in caller_roles
    # 越权防护：非管理员执行模拟时，被模拟人必须是本人
    if p.execute and not is_admin and p.username != user.get("username"):
        raise HTTPException(status_code=403,
                            detail="模拟他人权限时禁止执行查询；仅管理员可跨用户执行")
    ctx = enforcer.build_acl_for_username(p.username)
    dialect = p.dialect or "postgres"
    try:
        eff, err, applied = enforcer.rewrite_sql(sql, ctx, dialect=dialect)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"改写失败: {e}")

    result = {
        "ok": True,
        "username": p.username,
        "user_found": find_user(p.username) is not None,
        "roles": ctx.roles,
        "sql": sql,
        "rewritten_sql": eff,
        "error": err,
        "applied": applied,
        "acl": ctx.as_dict(),
    }
    if p.execute and not err:
        try:
            from db.executor import execute_sql
            # 非管理员已在上方约束为「模拟本人」，这里再按调用者自身 ACL 复核一次
            # （纵深防御：即便上层约束被绕过，执行上下文也始终是调用者权限）
            if not is_admin:
                own_ctx = enforcer.build_acl_context(user)
                eff, err2, _applied2 = enforcer.rewrite_sql(sql, own_ctx, dialect=dialect)
                if err2:
                    result["execution"] = {"success": False, "error": err2}
                    return result
            r = execute_sql(eff)
            result["execution"] = {"success": r.get("success"), "error": r.get("error"),
                                   "row_count": r.get("row_count", 0),
                                   "rows": (r.get("rows") or [])[:20]}
        except Exception as e:
            result["execution"] = {"success": False, "error": str(e)}
    return result


# ── 审计与变更历史 ───────────────────────────────────────

@router.get("/changes", dependencies=[Depends(_require_admin)])
def changes(limit: int = 200):
    """权限变更历史（含审批流落地记录）"""
    return _ok(changes=approval.get_change_history(limit=min(max(1, limit), 1000)))


@router.get("/audit", dependencies=[Depends(_require_admin)])
def audit_logs(limit: int = 100, event: str = ""):
    """操作审计（查询行为 / 权限变更，来源 auth.audit）"""
    logs = get_audit_logs(limit=min(max(1, limit), 1000))
    if event:
        logs = [x for x in logs if x.get("event") == event]
    return _ok(logs=logs)


# ── 审批流 ───────────────────────────────────────────────

@router.get("/requests", dependencies=[Depends(_require_admin)])
def requests(status: str = "", limit: int = 200):
    """审批单列表（可按状态过滤）"""
    return _ok(requests=approval.list_requests(status=status, limit=min(max(1, limit), 1000)))


@router.post("/requests", dependencies=[Depends(_require_login)])
def create_req(p: RequestPayload, u: dict = Depends(_require_login)):
    """提交权限申请（任何登录用户）。

    - 普通员工（非 admin）：仅允许为自己申请「数据访问授权」（data_access），
      强制绑定 requester 用户名与 set_user_grants 动作 —— 杜绝通过该入口
      伪造 role_grant/set_user_roles 等越权申请（管理员误点审批即形成越权）。
    - 管理员：保留原任意 kind 能力（权限管理页「发起申请」表单使用）。
    """
    kind = p.kind
    payload = dict(p.payload or {})
    if "admin" not in _user_roles(u):
        if kind != "data_access":
            raise HTTPException(status_code=403,
                                detail="普通员工仅可提交「数据访问授权」申请，其他类型变更请直接联系管理员")
        grants = payload.get("grants")
        if not isinstance(grants, dict):
            raise HTTPException(status_code=400, detail="授权申请参数格式错误：grants 需为 {表: [字段]} 对象")
        for t, cols in grants.items():
            if not isinstance(t, str) or not isinstance(cols, (list, tuple)):
                raise HTTPException(status_code=400, detail="授权申请参数格式错误：字段列表需为数组")
        payload = {"action": "set_user_grants", "username": u["username"],
                   "grants": {str(t): [str(c) for c in (cols or [])] for t, cols in grants.items()}}
        kind = "data_access"
    try:
        item = approval.create_request(u["username"], kind, payload, reason=p.reason)
        return _ok(message="申请已提交，等待管理员审核", request=item)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"提交失败: {e}")


@router.post("/requests/{req_id}/review", dependencies=[Depends(_require_admin)])
def review(req_id: str, p: ReviewPayload, u: dict = Depends(_require_admin)):
    """审批：通过则立即落地变更，驳回只记录"""
    try:
        item = approval.review_request(req_id, p.approve, reviewer=u["username"],
                                       comment=p.comment)
        return _ok(message="已通过并落地变更" if p.approve else "已驳回",
                   request=item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/change", dependencies=[Depends(_require_admin)])
def change(p: ChangePayload, u: dict = Depends(_require_admin)):
    """统一变更入口：敏感 + 非 admin → 审批单；其余 → 直接落地并留痕

    仅管理员可调用（前端权限管理页未使用该接口，收紧防止普通员工绕过
    /requests 的 data_access 限制提交任意变更申请）。
    """
    try:
        payload = dict(p.payload)
        if not payload.get("action"):
            payload["action"] = p.action
        res = approval.submit_change(payload, actor=u["username"],
                                     actor_roles=list(_user_roles(u)),
                                     reason=p.reason, kind=p.kind)
        return _ok(**res)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"变更失败: {e}")
