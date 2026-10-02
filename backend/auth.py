"""认证与权限（Phase 4）：JWT 登录 + RBAC + 表权限 + 操作审计

- 零新依赖：JWT 用标准库 HMAC-SHA256 自实现；密码用 PBKDF2-SHA256 加盐哈希。
- 默认「开放模式」（AUTH_REQUIRED=0）完全兼容现状；设 AUTH_REQUIRED=1 后启用登录。
- 用户存 backend/auth_users.json（首次自动创建 管理员/viewer 三个演示账号）。
- 表权限配置存 backend/auth_permissions.json：role → {tables: [...]}（空 = 无限制）。
- 审计日志写 backend/logs/audit.jsonl（JSON Lines，只增）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Header

_BASE = Path(__file__).resolve().parent
_USERS_FILE = _BASE / "auth_users.json"
_SECRET_FILE = _BASE / "auth_secret.key"
_LOG_DIR = _BASE / "logs"

AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "0") == "1"
TOKEN_TTL = int(os.getenv("AUTH_TOKEN_TTL", str(12 * 3600)))  # 秒，默认 12h

_lock = threading.RLock()  # 可重入锁：register_user 外层加锁后内部再调 load_users/save_users（均用 _lock）不会死锁

# ── 密钥 ─────────────────────────────────────────────────

def _load_or_create_secret() -> str:
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    _SECRET_FILE.write_text(secret, encoding="utf-8")
    return secret


_SECRET = os.getenv("AUTH_SECRET", "") or _load_or_create_secret()


# ── 密码哈希（PBKDF2-SHA256 + salt）────────────────────

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expect = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return hmac.compare_digest(actual, expect)
    except Exception:
        return False


# ── 用户存储 ─────────────────────────────────────────────

def _resolve_initial_password(env_key: str) -> tuple[str, bool]:
    """初始密码：优先取环境变量；未配置则生成一次性强随机密码。返回 (密码, 是否随机生成)。"""
    v = (os.getenv(env_key) or "").strip()
    if v:
        return v, False
    return secrets.token_urlsafe(24), True


_admin_pwd, _admin_generated = _resolve_initial_password("ADMIN_INITIAL_PASSWORD")
_viewer_pwd, _viewer_generated = _resolve_initial_password("VIEWER_INITIAL_PASSWORD")

DEFAULT_USERS = [
    {"username": "admin", "password": _admin_pwd, "role": "admin",
     "display_name": "管理员", "generated": _admin_generated},
    {"username": "viewer", "password": _viewer_pwd, "role": "viewer",
     "display_name": "访客", "generated": _viewer_generated},
]


def _announce_generated_passwords() -> None:
    """把随机生成的初始密码输出到 stdout 与日志（仅在首次初始化时调用一次）。

    修复（P0）：上一版改动在环境变量未配置时用 `secrets.token_urlsafe(24)` 生成随机密码，
    但既不打印也不返回 —— 而密码只在播种 auth_users.json 时以**哈希**形式落盘。
    后果：任何全新部署（或误删 auth_users.json 后重启）都会**永久无法登录管理员账号**，
    且没有任何报错线索。安全与可用性必须同时满足：密码要随机，但首次必须可获取。
    """
    for u in DEFAULT_USERS:
        if not u.get("generated"):
            continue
        msg = (f"[auth] 首次初始化：账号 {u['username']} 的随机初始密码为 {u['password']} "
               f"—— 请立即登录并修改，此密码不会再显示。"
               f"也可通过环境变量 {u['username'].upper()}_INITIAL_PASSWORD 预设。")
        try:
            print(msg, flush=True)
        except Exception:
            pass
        try:
            import logging as _logging
            _logging.getLogger(__name__).warning(msg)
        except Exception:
            pass


# 用户列表内存缓存：写入时失效，避免每个请求都读盘（并发读/写竞态也由同一把锁串行化）
_users_cache: list[dict] | None = None


def load_users() -> list[dict]:
    global _users_cache
    if _users_cache is not None:
        return _users_cache
    with _lock:
        # double-check：等锁期间可能已被其他线程填充
        if _users_cache is not None:
            return _users_cache
        if not _USERS_FILE.exists():
            users = []
            for u in DEFAULT_USERS:
                users.append({
                    "username": u["username"],
                    "role": u["role"],
                    "display_name": u.get("display_name", u["username"]),
                    "avatar": "",
                    "password_hash": hash_password(u["password"]),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
            save_users(users)
            _announce_generated_passwords()   # 随机初始密码必须在此输出，否则无法登录
            _users_cache = users
            return users
        try:
            _users_cache = json.loads(_USERS_FILE.read_text(encoding="utf-8"))
            return _users_cache
        except Exception:
            return []


def save_users(users: list[dict]) -> None:
    global _users_cache
    with _lock:
        _USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
        _users_cache = None  # 缓存失效，下次读取重新加载


def find_user(username: str) -> dict | None:
    for u in load_users():
        if u["username"] == username:
            # 兼容旧数据：display_name/avatar 缺失时补默认值（不落盘，仅读取兜底）
            u.setdefault("display_name", u["username"])
            u.setdefault("avatar", "")
            return u
    return None


# ── 邮箱格式校验（email_codes.py 发码时也复用）──────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    email = (email or "").strip()
    return 0 < len(email) <= 254 and bool(_EMAIL_RE.fullmatch(email))


def find_user_by_email(email: str) -> dict | None:
    """按用户资料里的 email 字段查找（兼容旧账号：注册时 username=邮箱，无需走此路径）"""
    email = (email or "").strip().lower()
    for u in load_users():
        if (u.get("email") or "").strip().lower() == email:
            u.setdefault("display_name", u["username"])
            u.setdefault("avatar", "")
            return u
    return None


def public_user(user: dict) -> dict:
    """脱敏后的用户信息（不含密码哈希），供 API 返回"""
    return {
        "username": user.get("username", ""),
        "role": user.get("role", ""),
        "display_name": user.get("display_name", user.get("username", "")),
        "avatar": user.get("avatar", ""),
        "email": user.get("email", ""),
        "phone": user.get("phone", ""),
        "department": user.get("department", ""),
        "title": user.get("title", ""),
        "bio": user.get("bio", ""),
        "preferences": user.get("preferences") or {},
        "enabled": bool(user.get("enabled", True)),
        "created_at": user.get("created_at", ""),
        "last_login": user.get("last_login", ""),
    }


def update_user_profile(username: str, **fields) -> dict | None:
    """更新用户资料（昵称/头像/邮箱/手机/部门/职位/简介/偏好），返回脱敏后的用户信息；用户不存在返回 None"""
    allowed = {"display_name", "avatar", "email", "phone", "department", "title", "bio", "preferences"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        raise ValueError("没有可更新的内容")
    users = load_users()
    for u in users:
        if u["username"] == username:
            if "display_name" in updates:
                name = str(updates["display_name"]).strip()
                if not name:
                    raise ValueError("昵称不能为空")
                u["display_name"] = name
            if "avatar" in updates:
                u["avatar"] = updates["avatar"]  # 允许空串（清除头像）
            for k in ("email", "phone", "department", "title", "bio"):
                if k in updates:
                    u[k] = str(updates[k] or "").strip()
            if "preferences" in updates:
                prefs = updates["preferences"]
                if not isinstance(prefs, dict):
                    raise ValueError("偏好设置格式不正确")
                u["preferences"] = {**u.get("preferences", {}), **prefs}
            save_users(users)
            return public_user(u)
    return None


def update_user_admin(username: str, patch: dict) -> dict | None:
    """管理员编辑用户：角色/昵称/邮箱/手机/部门/职位/简介/启用禁用/重置密码。"""
    users = load_users()
    for u in users:
        if u["username"] == username:
            if "role" in patch:
                role = str(patch["role"]).strip()
                # 角色校验改为动态：只要权限模型（auth_permissions.json）里注册过即可。
                # 原先写死 ("admin", "viewer")，导致岗位角色（生产/设备/质量人员）配了也改不上，
                # 管理员改角色时直接 ValueError。仍保留 pending 兜底：未分配角色允许保留原状。
                from security.model import get_role
                if role != "pending" and not get_role(role):
                    raise ValueError(f"角色不存在：{role}（请先在权限管理页新建该角色）")
                u["role"] = role
                u["roles"] = [role]
            if "display_name" in patch:
                u["display_name"] = str(patch["display_name"] or "").strip() or username
            for k in ("email", "phone", "department", "title", "bio", "note"):
                if k in patch:
                    u[k] = str(patch[k] or "").strip()
            if "enabled" in patch:
                u["enabled"] = bool(patch["enabled"])
            if "password" in patch and patch["password"]:
                strength = password_strength(str(patch["password"]))
                if not strength["ok"]:
                    raise ValueError(strength["message"])
                u["password_hash"] = hash_password(str(patch["password"]))
            save_users(users)
            return public_user(u)
    return None


def update_user_password(username: str, new_password: str) -> bool:
    """更新用户密码；用户不存在返回 False"""
    users = load_users()
    for u in users:
        if u["username"] == username:
            u["password_hash"] = hash_password(new_password)
            save_users(users)
            return True
    return False


def register_user(email: str, password: str, display_name: str = "") -> dict:
    """按邮箱注册新账户：username=邮箱（小写），默认「待授权」角色（pending），
    管理员分配角色前无任何数据访问权限。

    pending 不在 auth_permissions.json 的角色表里（无 policy），enforcer 的
    build_acl_context 对「非 guest 且无角色表授权」的用户默认落到空集
    （allowed_tables=set()），从而看不到任何表——待管理员在权限页分配 普通员工
    角色后才有数据权限。邮箱已注册抛 ValueError。
    """
    email = (email or "").strip().lower()
    password = str(password)
    # 与发码接口共用同一套面向普通用户的中文提示，避免「前端/发码说 A、注册说 B」
    from email_codes import email_format_error
    reason = email_format_error(email)
    if reason:
        raise ValueError(reason)
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    name = str(display_name).strip() or email.split("@")[0]
    if len(name) > 50:
        name = name[:50]  # 昵称超长截断，避免前端展示异常
    # 原子：查重 + 追加 + 落盘在同一把锁内，避免并发注册相同邮箱产生重复用户/覆盖丢失
    with _lock:
        if find_user(email):
            raise ValueError(f"邮箱「{email}」已注册")
        users = load_users()
        for u in users:
            if (u.get("email") or "").strip().lower() == email:
                raise ValueError(f"邮箱「{email}」已注册")
        users.append({
            "username": email,
            "email": email,
            "role": "pending",          # 待授权：管理员分配角色前无数据权限
            "roles": ["pending"],
            "display_name": name,
            "avatar": "",
            "password_hash": hash_password(password),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        save_users(users)
        return public_user(users[-1])


# ── RBAC：用户 → 多角色 + 用户属性（行级权限变量来源）──────

def get_user_roles(user: dict) -> list[str]:
    """用户的角色列表：roles 数组优先（多角色），回退单 role 字段（旧数据兼容）"""
    roles = [str(r).strip() for r in (user.get("roles") or []) if str(r).strip()]
    if not roles:
        single = str(user.get("role") or "").strip()
        roles = [single] if single else []
    seen, out = set(), []
    for r in roles:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def set_user_roles(username: str, roles: list[str]) -> list[str]:
    """设置用户角色（RBAC：用户加入角色后自动继承角色权限）。

    同时把 role 字段同步为「主角色」：admin 优先，其次列表首个，
    保证既有依赖单 role 的逻辑（前端菜单 / JWT / require_roles）行为一致。

    ⚠️ `pending` 是「待授权」哨兵（自助注册账号的初始 role/roles），**不是**
    auth_permissions.json 里注册过的角色。权限页保存成员时会把该用户当前 roles
    原样回传（待授权用户的 payload = ['pending'] + 管理员新勾选的角色），若把哨兵
    当角色做存在性校验，必然抛「角色不存在：pending」——表现就是「新注册的两个账号
    一点保存就报错，但本人登录完全正常」（登录不校验角色，只有写角色才校验）。
    所以这里先剔除哨兵，只对真实角色校验；剔除后为空（只点保存没勾角色 / 取消全部
    角色）时回落到 ['pending']，即回到「待授权」零数据权限，这也是目前唯一能整体
    撤销某个账号数据权限的方式。
    """
    clean = [str(r).strip() for r in (roles or []) if str(r).strip()]
    real = [r for r in clean if r != "pending"]   # pending 只是哨兵，不参与校验
    from security.model import get_role
    unknown = [r for r in real if not get_role(r)]
    if unknown:
        raise ValueError(f"角色不存在：{', '.join(unknown)}（请先在权限管理页新建该角色）")
    if not real:
        real = ["pending"]                        # 未分配任何角色 → 回到待授权
    users = load_users()
    for u in users:
        if u["username"] == username:
            u["roles"] = real
            u["role"] = "admin" if "admin" in real else real[0]
            save_users(users)
            return real
    raise ValueError(f"用户「{username}」不存在")


def set_user_attributes(username: str, attributes: dict) -> dict:
    """设置用户属性（如 region=华东、dept=生产部），供行级权限模板 ${user.region} 使用"""
    clean = {str(k).strip(): v for k, v in (attributes or {}).items() if str(k).strip()}
    users = load_users()
    for u in users:
        if u["username"] == username:
            u["attributes"] = clean
            save_users(users)
            return clean
    raise ValueError(f"用户「{username}」不存在")


# ── JWT（HMAC-SHA256，标准库实现）────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def create_token(username: str, role: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({
        "sub": username, "role": role,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(datetime.now(timezone.utc).timestamp()) + TOKEN_TTL,
    }, separators=(",", ":")).encode())
    signing = f"{header}.{payload}"
    sig = _b64url(hmac.new(_SECRET.encode(), signing.encode(), hashlib.sha256).digest())
    return f"{signing}.{sig}"


def decode_token(token: str) -> dict | None:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        signing = f"{header_b64}.{payload_b64}"
        expect = _b64url_decode(sig_b64)
        actual = hmac.new(_SECRET.encode(), signing.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(actual, expect):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if payload.get("exp", 0) < int(datetime.now(timezone.utc).timestamp()):
            return None
        return payload
    except Exception:
        return None


# ── FastAPI 依赖 ─────────────────────────────────────────

def get_current_user(authorization: str = Header(None)) -> dict:
    """从 Authorization: Bearer <token> 解析当前用户；开放模式且无 token 时返回 guest。

    注意：开放模式返回 guest（而非 admin）——保证管理端点（挂 require_roles("admin")）
    必须真实登录才能访问，否则权限体系形同虚设。
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token:
        payload = decode_token(token)
        if not payload:
            # 开放模式（AUTH_REQUIRED=0）下无效/过期 token 降级 guest，避免体验卡死；
            # 管理端点仍被 require_roles 拦截（guest 不在 管理员 内），权限不受影响
            if not AUTH_REQUIRED:
                return {"username": "guest", "role": "guest"}
            raise HTTPException(status_code=401, detail="登录已过期或无效，请重新登录")
        user = find_user(payload.get("sub", ""))
        if not user:
            if not AUTH_REQUIRED:
                return {"username": "guest", "role": "guest", "roles": ["guest"], "attributes": {}}
            raise HTTPException(status_code=401, detail="用户不存在")
        # 禁用账号：即使 JWT 未过期也拒绝（禁止禁用后继续查数）
        if not user.get("enabled", True):
            raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")
        # 多角色 + 用户属性一并返回：数据权限（行/列/指标）全靠这两项计算
        return {"username": user["username"], "role": user["role"],
                "roles": get_user_roles(user), "attributes": user.get("attributes") or {}}
    if AUTH_REQUIRED:
        raise HTTPException(status_code=401, detail="需要登录（AUTH_REQUIRED 已开启）")
    # 开放模式：游客身份，可查询但不可管理
    return {"username": "guest", "role": "guest", "roles": ["guest"], "attributes": {}}


def require_roles(*roles: str):
    """RBAC 依赖工厂：仅允许指定角色访问。

    注意：依赖函数**只能**声明 authorization 头参数（Header(None)），
    绝不能声明 `user: dict = None` 之类的复杂类型参数——FastAPI 会把未标注
    Query/Header/Path/Body 的复杂类型（dict/Model）解析为**请求 body 字段**，
    从而污染同一路由真正的 body 模型（如 DatabaseConfig 被拆成 user+config 两个
    body 参数，导致所有带 body 的受保护端点 422）。
    """
    def _dep(authorization: str = Header(None)) -> dict:
        u = get_current_user(authorization)
        # 多角色语义：按 get_user_roles 的完整角色集合判断（与 enforcer 多角色合并一致），
        # 避免主角色非目标、但 roles 数组已含目标角色的用户被误拒。
        user_roles = set(get_user_roles(u))
        # 未登录（guest）→ 401 引导登录（前端弹登录框）；已登录但角色不足 → 403（只提示）
        if not user_roles or user_roles == {"guest"}:
            raise HTTPException(status_code=401, detail="请先登录")
        if not (user_roles & set(roles)):
            raise HTTPException(status_code=403, detail=f"权限不足：需要 {'/'.join(roles)} 角色")
        return u
    return _dep


def require_login():
    """安全修复（P0）：仅要求"已登录"，不限定角色。

    用于那些**写操作 / 敏感信息 / 可执行任意代码**的端点：
    开放模式（AUTH_REQUIRED=0）下 get_current_user 对无 token 请求返回 guest，
    这些端点此前因此可被任意匿名访客调用（如 /api/agent/python/run、/api/feedback、
    /api/agent/upload-csv）。注意 guest 一律拒绝（401），保持"游客只能只读查询"的边界。
    """
    def _dep(authorization: str = Header(None)) -> dict:
        u = get_current_user(authorization)
        user_roles = set(get_user_roles(u))
        if not user_roles or user_roles == {"guest"}:
            raise HTTPException(status_code=401, detail="请先登录后再执行此操作")
        return u
    return _dep


# ── 操作审计 ─────────────────────────────────────────────

def audit(event: str, **detail) -> None:
    """追加一条审计日志（backend/logs/audit.jsonl）"""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **{k: (v if isinstance(v, (str, int, float, bool, list, dict)) or v is None else str(v))
               for k, v in detail.items()},
        }
        with _lock:
            with open(_LOG_DIR / "audit.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def get_audit_logs(limit: int = 100) -> list[dict]:
    entries = []
    try:
        path = _LOG_DIR / "audit.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return entries


def password_strength(password: str) -> dict:
    """简单密码强度校验：长度 + 字符种类。返回 {ok, score, message}。"""
    pw = password or ""
    if len(pw) < 6:
        return {"ok": False, "score": 0, "message": "密码长度不能少于 6 位"}
    kinds = 0
    if any(c.islower() for c in pw):
        kinds += 1
    if any(c.isupper() for c in pw):
        kinds += 1
    if any(c.isdigit() for c in pw):
        kinds += 1
    if any(not c.isalnum() for c in pw):
        kinds += 1
    score = min(4, kinds)
    if len(pw) >= 12:
        score += 1
    # 弱口令：全数字/全字母/连续重复
    if pw.isdigit() or pw.isalpha():
        return {"ok": False, "score": 1, "message": "密码太弱：请混合字母、数字或符号"}
    if len(set(pw)) <= 2:
        return {"ok": False, "score": 1, "message": "密码太弱：字符过于单一"}
    return {"ok": True, "score": min(5, score), "message": "密码强度良好"}


# ── 登录记录（最近登录 / 会话轨迹）────────────────────────

_LOGIN_FILE = _LOG_DIR / "login_history.jsonl"


def record_login(username: str, ip: str = "", ua: str = "") -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "username": username,
            "ip": (ip or "")[:64],
            "ua": (ua or "")[:160],
        }
        with _lock:
            with open(_LOGIN_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        # 同时把 last_login 写回用户对象
        users = load_users()
        for u in users:
            if u["username"] == username:
                u["last_login"] = entry["ts"]
                save_users(users)
                break
    except Exception:
        pass


def get_login_history(username: str, limit: int = 20) -> list[dict]:
    entries = []
    try:
        if _LOGIN_FILE.exists():
            for line in _LOGIN_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                    if e.get("username") == username:
                        entries.append(e)
                except Exception:
                    continue
    except Exception:
        pass
    return entries[-limit:]


# ── 审计日志查询（筛选 / 分页 / 导出用）────────────────────

def query_audit_logs(limit: int = 100, offset: int = 0, event: str = "", user: str = "") -> dict:
    """读取审计日志，支持按事件类型 / 用户过滤与分页。返回 {items, total}。"""
    entries = []
    try:
        path = _LOG_DIR / "audit.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                    if event and e.get("event") != event:
                        continue
                    if user and e.get("user") != user and e.get("username") != user:
                        continue
                    entries.append(e)
                except Exception:
                    continue
    except Exception:
        pass
    total = len(entries)
    return {"items": entries[offset:offset + limit], "total": total}
