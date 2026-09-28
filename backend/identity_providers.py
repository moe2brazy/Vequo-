"""企业身份源集成（P0）：OIDC（授权码 + discovery 可配）+ LDAP（bind 认证）

设计目标
- 在自研 JWT 之上新增通用可配的外部身份源，外部身份自动映射到本地用户
  （role / attributes / display_name），自研用户名密码登录保留为 fallback。
- 零侵入：不启用时（默认）完全不影响现有登录；开关均走环境变量。
- 通用可配：不绑定具体 IdP，OIDC 支持标准 discovery 或显式端点，LDAP 支持
  服务账号搜索模式与直接 DN 模板两种。

外部身份 → 本地用户映射
- username：优先 username claim / DN 映射
- role：外部角色 claim（OIDC）或组 DN（LDAP）经映射表 → 本地角色，未命中取默认角色
- attributes：claim / LDAP 属性按映射表抽取（供行级权限 ${user.xxx} 使用）

依赖：authlib（OIDC）、ldap3（LDAP），未安装时对应 provider 惰性报错。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from typing import Any

# ── 配置读取（环境变量驱动，可写进 backend/.env）──────────

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key, "")
    if v == "":
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _env_json(key: str, default: Any) -> Any:
    raw = _env(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


# OIDC 配置
OIDC_ENABLED = _env_bool("OIDC_ENABLED")
OIDC_CONFIG = {
    "issuer": _env("OIDC_ISSUER"),
    "client_id": _env("OIDC_CLIENT_ID"),
    "client_secret": _env("OIDC_CLIENT_SECRET"),
    "redirect_uri": _env("OIDC_REDIRECT_URI"),
    "scope": _env("OIDC_SCOPE", "openid profile email"),
    # 未配置 issuer 时可显式给出端点
    "authorization_endpoint": _env("OIDC_AUTHORIZATION_ENDPOINT"),
    "token_endpoint": _env("OIDC_TOKEN_ENDPOINT"),
    "userinfo_endpoint": _env("OIDC_USERINFO_ENDPOINT"),
    "jwks_uri": _env("OIDC_JWKS_URI"),
    # 身份映射
    "username_claim": _env("OIDC_USERNAME_CLAIM", "preferred_username"),
    "role_claim": _env("OIDC_ROLE_CLAIM", "roles"),
    "role_map": _env_json("OIDC_ROLE_MAP", {}),        # 外部角色 -> 本地角色
    "default_role": _env("OIDC_DEFAULT_ROLE", "viewer"),
    "attr_map": _env_json("OIDC_ATTR_MAP", {}),        # 外部 claim -> 本地 attribute
}

# LDAP 配置
LDAP_ENABLED = _env_bool("LDAP_ENABLED")
LDAP_CONFIG = {
    "host": _env("LDAP_HOST"),
    "port": int(_env("LDAP_PORT", "389")),
    "use_ssl": _env_bool("LDAP_USE_SSL"),
    # 服务账号搜索模式
    "bind_dn": _env("LDAP_BIND_DN"),
    "bind_password": _env("LDAP_BIND_PASSWORD"),
    "search_base": _env("LDAP_SEARCH_BASE"),
    "search_filter": _env("LDAP_SEARCH_FILTER", "(uid={username})"),
    # 直接 DN 模板模式（与搜索模式二选一）
    "user_dn_template": _env("LDAP_USER_DN_TEMPLATE"),
    # 身份映射
    "group_attr": _env("LDAP_GROUP_ATTR", "memberOf"),
    "role_map": _env_json("LDAP_ROLE_MAP", {}),        # 组 DN -> 本地角色
    "default_role": _env("LDAP_DEFAULT_ROLE", "viewer"),
    "attr_map": _env_json("LDAP_ATTR_MAP", {}),        # LDAP 属性 -> 本地 attribute
    "display_name_attr": _env("LDAP_DISPLAY_NAME_ATTR", "cn"),
}

# state 签名密钥（复用 auth 的 AUTH_SECRET，保证跨进程一致）
from auth import _SECRET  # noqa: E402  （auth 模块导出的 JWT 密钥）


# ── 外部身份标准化 ────────────────────────────────────────

def _norm_identity(source: str, username: str, roles: list[str], attributes: dict,
                   display_name: str = "") -> dict:
    return {
        "source": source,
        "username": username,
        "roles": roles,          # 外部角色原始值
        "attributes": attributes,
        "display_name": display_name,
    }


def map_external_identity(source: str, external_roles: list[str],
                          external_attrs: dict, username: str = "",
                          display_name: str = "") -> dict:
    """把外部身份映射为本地用户字段（username/role/attributes/display_name）。

    - 角色：取映射表中第一个命中的外部角色；未命中用默认角色。
    - attributes：按映射表抽取外部 claim/属性。
    """
    cfg = OIDC_CONFIG if source == "oidc" else LDAP_CONFIG
    role_map: dict = cfg.get("role_map") or {}
    local_role = cfg.get("default_role", "viewer")
    for ext_role in external_roles:
        if ext_role in role_map:
            local_role = role_map[ext_role]
            break
    attr_map: dict = cfg.get("attr_map") or {}
    attributes = {}
    for ext_key, local_key in attr_map.items():
        if ext_key in external_attrs:
            attributes[local_key] = external_attrs[ext_key]
    return {
        "username": username,
        "role": local_role,
        "attributes": attributes,
        "display_name": display_name or username,
        "source": source,
    }


def ensure_local_user(mapped: dict) -> dict:
    """首次外部登录自动创建本地用户（幂等），后续登录更新其角色/属性。

    本地用户不存密码（password_hash 置空），仅能通过外部身份源登录。
    返回完整本地用户记录。
    """
    from auth import load_users, save_users
    username = mapped["username"]
    users = load_users()
    found = None
    for u in users:
        if u["username"] == username:
            found = u
            break
    if found is None:
        found = {
            "username": username,
            "role": mapped["role"],
            "roles": [mapped["role"]],
            "attributes": mapped["attributes"],
            "display_name": mapped["display_name"],
            "avatar": "",
            "password_hash": "",          # 外部用户无本地密码
            "idp": mapped["source"],
            "created_at": _now_iso(),
        }
        users.append(found)
    else:
        # 已存在：同步外部最新角色/属性（外部身份源为唯一事实源）
        found["roles"] = [mapped["role"]]
        found["role"] = mapped["role"]
        found["attributes"] = mapped["attributes"]
        found["display_name"] = mapped["display_name"] or found.get("display_name", username)
        found["idp"] = mapped["source"]
    save_users(users)
    return found


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── OIDC ──────────────────────────────────────────────────

def _oidc_discovery(issuer: str) -> dict:
    import requests
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _oidc_endpoints() -> dict:
    cfg = OIDC_CONFIG
    if cfg["issuer"]:
        disc = _oidc_discovery(cfg["issuer"])
        return {
            "authorization_endpoint": disc.get("authorization_endpoint", cfg["authorization_endpoint"]),
            "token_endpoint": disc.get("token_endpoint", cfg["token_endpoint"]),
            "userinfo_endpoint": disc.get("userinfo_endpoint", cfg["userinfo_endpoint"]),
        }
    return {
        "authorization_endpoint": cfg["authorization_endpoint"],
        "token_endpoint": cfg["token_endpoint"],
        "userinfo_endpoint": cfg["userinfo_endpoint"],
    }


def oidc_authorize_url() -> dict:
    """生成 OIDC 授权跳转 URL（含防 CSRF 的签名 state）。"""
    from authlib.integrations.requests_client import OAuth2Session
    cfg = OIDC_CONFIG
    ep = _oidc_endpoints()
    if not ep["authorization_endpoint"]:
        raise RuntimeError("OIDC 未配置 authorization_endpoint / issuer")
    state = _sign_state(secrets.token_hex(16))
    client = OAuth2Session(cfg["client_id"], cfg["client_secret"],
                           redirect_uri=cfg["redirect_uri"], scope=cfg["scope"])
    url, _state = client.create_authorization_url(ep["authorization_endpoint"], state=state)
    return {"auth_url": url, "state": state}


def oidc_callback(code: str, state: str) -> dict:
    """OIDC 回调：校验 state → code 换 token → 取 userinfo → 映射身份 → 建本地用户。

    返回标准化本地用户（含签发 JWT 所需字段）。
    """
    from authlib.integrations.requests_client import OAuth2Session
    cfg = OIDC_CONFIG
    if not _verify_state(state):
        raise ValueError("OIDC state 校验失败（可能存在 CSRF）")
    ep = _oidc_endpoints()
    client = OAuth2Session(cfg["client_id"], cfg["client_secret"],
                           redirect_uri=cfg["redirect_uri"], scope=cfg["scope"])
    client.fetch_token(ep["token_endpoint"], code=code)
    userinfo = client.get(ep["userinfo_endpoint"]).json() if ep["userinfo_endpoint"] else {}

    # 身份抽取
    username = userinfo.get(cfg["username_claim"]) or userinfo.get("sub") or userinfo.get("email") or ""
    external_roles = userinfo.get(cfg["role_claim"]) or []
    if isinstance(external_roles, str):
        external_roles = [external_roles]
    external_roles = [str(r) for r in external_roles]
    display_name = userinfo.get("name") or userinfo.get("preferred_username") or username

    mapped = map_external_identity("oidc", external_roles, userinfo, username=username,
                                   display_name=display_name)
    user = ensure_local_user(mapped)
    return user


# ── LDAP ──────────────────────────────────────────────────

def ldap_authenticate(username: str, password: str) -> dict:
    """LDAP 认证：bind 验证 → 抽取属性/组 → 映射身份 → 建本地用户。

    支持两种模式（二选一）：
    1. 服务账号搜索模式：用 bind_dn 绑定，按 search_filter 查用户 DN，再用用户 DN 二次 bind 验证。
    2. 直接 DN 模板模式：user_dn_template 直接拼 DN，一次 bind 验证。
    """
    from ldap3 import Server, Connection, ALL, SUBTREE

    cfg = LDAP_CONFIG
    server = Server(cfg["host"], port=cfg["port"], use_ssl=cfg["use_ssl"], get_info=ALL)

    # 需要读取的 LDAP 属性：映射属性 + 显示名 + 组属性（务必含 group_attr，否则组信息丢失）
    search_attrs = list(cfg["attr_map"].keys()) + [cfg["display_name_attr"], cfg["group_attr"]]

    user_dn = None
    attributes: dict = {}

    if cfg["user_dn_template"]:
        # 模式2：直接 DN（一次 bind 验证 + 读属性/组）
        user_dn = cfg["user_dn_template"].format(username=username)
        conn = Connection(server, user=user_dn, password=password, auto_bind=False)
        if not conn.bind():
            raise ValueError("LDAP 认证失败：用户名或密码错误")
        try:
            conn.search(user_dn, "(objectClass=*)", SUBTREE, attributes=search_attrs)
            if conn.entries:
                attributes = {k: v.value for k, v in conn.entries[0].entry_attributes_as_dict.items()}
        except Exception:
            attributes = {}
    else:
        # 模式1：服务账号搜索（服务账号 bind → 查用户 DN → 用户 DN 二次 bind 验证密码）
        if not cfg["bind_dn"] or not cfg["search_base"]:
            raise RuntimeError("LDAP 未配置 bind_dn/search_base（搜索模式）或 user_dn_template（模板模式）")
        service = Connection(server, user=cfg["bind_dn"], password=cfg["bind_password"], auto_bind=False)
        if not service.bind():
            raise RuntimeError("LDAP 服务账号绑定失败，请检查 bind_dn / bind_password")
        filt = cfg["search_filter"].format(username=username)
        service.search(cfg["search_base"], filt, SUBTREE, attributes=search_attrs)
        if not service.entries:
            raise ValueError("LDAP 认证失败：用户不存在")
        entry = service.entries[0]
        user_dn = entry.entry_dn
        attributes = {k: v.value for k, v in entry.entry_attributes_as_dict.items()}
        # 二次 bind 验证密码
        user_conn = Connection(server, user=user_dn, password=password, auto_bind=False)
        if not user_conn.bind():
            raise ValueError("LDAP 认证失败：用户名或密码错误")

    # 身份抽取
    display_name = _first(attributes.get(cfg["display_name_attr"])) or username
    groups = _extract_groups(attributes, cfg)
    mapped = map_external_identity("ldap", groups, attributes,
                                   username=username, display_name=display_name)
    user = ensure_local_user(mapped)
    return user


def _extract_groups(attributes: dict, cfg: dict) -> list[str]:
    """从已读取的 LDAP 属性中抽取组 DN 列表（用于角色映射）。"""
    raw = attributes.get(cfg["group_attr"]) or []
    if not isinstance(raw, list):
        raw = [raw]
    return [str(g).strip() for g in raw if str(g).strip()]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


# ── state 签名（防 CSRF，无状态）──────────────────────────

def _sign_state(random_hex: str) -> str:
    sig = hmac.new(_SECRET.encode(), random_hex.encode(), hashlib.sha256).hexdigest()
    return f"{random_hex}.{sig}"


def _verify_state(state: str) -> bool:
    try:
        random_hex, sig = state.rsplit(".", 1)
        expect = hmac.new(_SECRET.encode(), random_hex.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, sig)
    except Exception:
        return False
