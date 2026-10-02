"""Vequo 维阔 · NL2SQL Agent — FastAPI 后端（流式 LLMService + 动态数据库连接）"""

import re
import time
import asyncio
import logging
import hmac as _hmac          # API Token 网关用（原先是请求内 import，每次请求都执行）
import json as json_mod
import os as _os
from fastapi import FastAPI, HTTPException, Request, Header, Depends, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from routers import tables, knowledge, config, visualize, permission, mcp
from database import get_database_config, switch_database, test_database_connection
from auth import (
    create_token, verify_password, load_users, save_users, find_user, find_user_by_email,
    get_current_user, require_roles, require_login, audit, get_audit_logs, AUTH_REQUIRED, get_user_roles,
    public_user, update_user_profile, update_user_password,
    update_user_admin, record_login, get_login_history, query_audit_logs, password_strength,
)
from identity_providers import (
    OIDC_ENABLED, LDAP_ENABLED,
    oidc_authorize_url, oidc_callback, ldap_authenticate,
)
from agent.llm_service import (
    LLMService,
    generate_recommend_questions, cache_result, get_cached_result,
    _make_llm,
)
from agent.chart_agent import generate_chart
# 2026-10-01 轻量化：ml.trainer 顶层 import 会连带加载 pandas/sklearn/matplotlib 全家桶，
# 但 ML 建模只是边缘功能（4 个路由），却让每次启动多付 2~4 秒 + 约 200MB 常驻内存。
# 改为路由内惰性 import（与下方 feedback/csv_import 的写法一致）。
import uvicorn

app = FastAPI(title="Vequo 维阔 NL2SQL Agent", version="2.0.0")


@app.exception_handler(ValueError)
async def _value_error_to_400(request: Request, exc: ValueError):
    """业务层的输入校验异常映射为 400，而不是冒泡成 500。

    业务函数（knowledge_user_data、metrics、permission 等）统一用
    `raise ValueError("术语名称不能为空")` 这类写法表达"输入不合法"。此前没有全局映射，
    这些异常一路冒泡到 FastAPI 变成 500 Internal Server Error，前端只能弹出
    「服务器内部错误」，真正的可读原因（"术语名称不能为空"）被丢掉了。
    这里统一转成 400 并保留原文；同时留 warning 日志，便于区分"用户输入有误"
    和"代码真的抛了 ValueError"（后者需要看日志里的路径定位）。
    """
    logging.getLogger("api").warning("ValueError -> 400 | %s | %s", request.url.path, exc)
    return JSONResponse(status_code=400, content={"detail": str(exc)})


_cors_origins = [origin.strip() for origin in (_os.getenv("CORS_ORIGINS") or "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tables.router)
app.include_router(knowledge.router)
app.include_router(config.router)
app.include_router(visualize.router)
app.include_router(permission.router)
app.include_router(mcp.router, prefix="/api")


# ── 可选 API Token 鉴权（默认关闭；设置 API_TOKEN 后所有 /api/* 需携带凭证）──
# 放行条件二选一：① API Token（机器对机器，X-API-Token 头或 Bearer）② 后端签发的有效用户 JWT
_API_TOKEN = (_os.getenv("API_TOKEN") or "").strip()


def _is_valid_user_jwt(token: str) -> bool:
    """是否为后端签发的、未过期的用户 JWT。

    API_TOKEN 与用户 JWT 只能共用同一个 Authorization 头，因此网关必须能区分两者：
    否则一旦设置 API_TOKEN，浏览器登录后携带的 JWT 会被当成"错误的 API Token"全部拒掉。
    """
    if not token:
        return False
    try:
        from auth import decode_token
        return decode_token(token) is not None
    except Exception:
        return False


@app.middleware("http")
async def _api_token_guard(request: Request, call_next):
    if _API_TOKEN:
        # 健康检查与静态页放行；其余 /api/* 校验凭证
        path = request.url.path
        if path in ("/", "/api/health") or not path.startswith("/api"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        # 机器凭证：显式 X-API-Token 优先，其次 Authorization: Bearer <API_TOKEN>
        presented = (request.headers.get("x-api-token") or "").strip() or bearer

        # 修复（P1）：原实现只做"等于 API_TOKEN"一种比较。浏览器登录后发的是
        # Bearer <JWT>，presented 取到 JWT 与 API_TOKEN 比较必然失败 → 全站 401，
        # 且前端会把这个 401 当成"用户 token 失效"反复弹登录框，重新登录也进不去。
        # 现在改为：API Token 命中 或 有效用户 JWT，二者任一即放行（JWT 的用户身份
        # 仍由各路由自身的 get_current_user / require_roles 继续把关）。
        token_ok = False
        if presented:
            try:
                token_ok = _hmac.compare_digest(presented, _API_TOKEN)
            except (TypeError, ValueError):
                # 非 ASCII 等 compare_digest 不接受的输入，按不匹配处理
                token_ok = False
        if token_ok or _is_valid_user_jwt(bearer):
            return await call_next(request)

        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"detail": "未授权：缺少或错误的 API Token"})
    return await call_next(request)


class AskRequest(BaseModel):
    query: str
    history: list[dict] | None = None  # [{role, content}, ...]
    no_confirm: bool = False  # 澄清重问（前端已就歧义让用户补充过）→ 跳过二次弹窗直接生成



class MonitorRuleRequest(BaseModel):
    """指标监控规则（对标 Tableau Pulse）：定时执行 SQL 检测异动"""
    name: str
    sql: str
    change_pct: float = 10.0  # 变化阈值 %，超过即告警


class MonitorRuleUpdate(BaseModel):
    name: str | None = None
    sql: str | None = None
    change_pct: float | None = None
    enabled: bool | None = None


class ConfirmRequest(BaseModel):
    """二次确认弹窗：按用户确认的 SQL 直接执行（遵循 LLM 推断 / 按修改后 SQL）"""
    query: str
    sql: str
    history: list[dict] | None = None


class DocAskRequest(BaseModel):
    """非结构化问答（P2-1，对标 ThoughtSpot Spotter 3）：基于知识库片段回答 + 引用溯源"""
    query: str
    k: int = 5


class ActionProposeRequest(BaseModel):
    """行动建议（P2-2，对标 Sigma Agents）：LLM 只产参数，SQL 由注册表确定性编译"""
    query: str
    context: str = ""


class ActionExecuteRequest(BaseModel):
    """行动执行（P2-2）：必须二次确认，写操作过白名单 + 审计"""
    action: str
    params: dict
    confirmed: bool = False


class NotifyChannelRequest(BaseModel):
    name: str
    webhook_url: str
    type: str = "generic"


class PythonPlanRequest(BaseModel):
    """NL2Python 代码生成请求（对标 Fabric Code Interpreter / Vanna Python 沙箱）。

    只生成代码、不执行。数据可来自：① 前端回传的 rows；② 服务端按 query 取的
    结果缓存（ACL 指纹隔离，防越权复用）。
    """
    query: str
    columns: list[str] | None = None
    rows: list[dict] | None = None


class PythonRunRequest(BaseModel):
    """NL2Python 执行请求 —— 必须是用户已确认过的代码（二次确认闸门）。"""
    code: str
    query: str = ""
    columns: list[str] | None = None
    rows: list[dict] | None = None
    timeout: float | None = None


class DashboardRequest(BaseModel):
    """一键生成看板（对标 ThoughtSpot SpotterViz / 帆软 FineBI 智能仪表板）"""
    query: str
    max_cards: int = 5          # 卡片数上限（3-8）
    matched_tables: list[str] | None = None


# ── LLM 配置（自定义模型：运行时切换 API）─────────────────

class LLMConfigRequest(BaseModel):
    model: str = "deepseek-v4-flash"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    temperature: float = Field(0.2, ge=0.0, le=2.0)
    max_tokens: int = 8192
    test_only: bool = False  # 仅测试，不保存


@app.get("/api/llm/providers")
def get_llm_providers_api():
    """返回可用 LLM 供应商列表（含 base_url / 示例模型预设，供前端一键填充）。"""
    from agent.llm_providers import list_providers
    return {"success": True, "providers": list_providers()}


@app.get("/api/llm/config")
def get_llm_config_api():
    """返回当前 LLM 配置与系统内置默认配置"""
    from config import get_llm_config, get_llm_default
    cfg = get_llm_config()
    dft = get_llm_default()
    return {
        "success": True,
        "config": {
            # api_key 不回传前端：前端默认空着由用户自填，避免泄露当前密钥
            "model": cfg["model"],
            "api_key": "",
            "base_url": cfg["base_url"],
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
        },
        "default": {
            "model": dft["model"],
            "api_key": "",   # 默认密钥同样不回传（防匿名窃取 .env 真实 key）
            "base_url": dft["base_url"],
            "temperature": dft["temperature"],
            "max_tokens": dft["max_tokens"],
        },
    }


@app.post("/api/llm/config", dependencies=[Depends(require_roles("admin"))])
def save_llm_config_api(req: LLMConfigRequest):
    """保存 LLM 配置（热生效）；test_only=True 只测试连接"""
    from config import update_llm_config, test_llm_connection, get_llm_config

    # 用户未填写 api_key 时，保留当前生效的 key（避免清空系统默认 DeepSeek key）
    if not req.api_key:
        req.api_key = get_llm_config()["api_key"]

    # 先临时应用配置用于测试
    if req.test_only:
        from config import LLM_CONFIG
        backup = dict(LLM_CONFIG)
        update_llm_config(req.model_dump())
        try:
            ok, msg = test_llm_connection()
            return {"success": ok, "message": msg}
        finally:
            LLM_CONFIG.clear()
            LLM_CONFIG.update(backup)
            from config import _persist_llm_config
            _persist_llm_config()

    update_llm_config(req.model_dump())
    # 保存后测试
    ok, msg = test_llm_connection()
    cfg = get_llm_config()
    return {
        # ⚠️ 这里必须如实返回测试结果。此前恒为 True，而前端按 `success !== false`
        # 决定提示颜色 → 不管 Key 通不通都显示蓝色的"配置已保存"，用户以为配好了，
        # 实际已经把可用配置换成了调不通的（2026-09-28 实测：在「切换模型」里保存了一个
        # 无效 Key，之后所有问数都报"AI 未能生成 SQL"，而配置页始终显示保存成功）。
        "success": bool(ok),
        "saved": True,
        "message": (f"配置已保存，连接测试通过：{msg}" if ok else
                    f"配置已保存，但连接测试未通过：{msg}。当前配置不可用，"
                    f"所有问数都会失败——请核对 API Key 与接口地址是否匹配，"
                    f"或点「恢复默认」换回系统内置配置。"),
        "config": {
            "model": cfg["model"],
            "api_key": "",
            "base_url": cfg["base_url"],
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
        },
    }


@app.post("/api/llm/restore-default", dependencies=[Depends(require_roles("admin"))])
def restore_llm_default_api():
    """将当前生效配置恢复为系统内置默认（冻结的 LLM_DEFAULT_*）"""
    from config import restore_llm_default, get_llm_config, test_llm_connection
    restore_llm_default()
    ok, msg = test_llm_connection()
    cfg = get_llm_config()
    return {
        # 同 /api/llm/config：恢复默认若仍测不通（默认 Key 欠费/被吊销），
        # 必须让前端显示为失败，而不是一句蓝色的"已恢复默认"。
        "success": bool(ok),
        "restored": True,
        "message": (f"已恢复为系统默认（{cfg['model']}），连接测试通过：{msg}" if ok else
                    f"已恢复为系统默认（{cfg['model']}），但连接测试未通过：{msg}。"
                    f"系统内置 Key 也可能已失效或欠费，请联系管理员更新。"),
        "config": {
            "model": cfg["model"],
            "api_key": "",
            "base_url": cfg["base_url"],
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
        },
    }


class AskResponse(BaseModel):
    type: str
    query: str
    data: dict
    elapsed_ms: int


class AnalysisRequest(BaseModel):
    query: str
    sql: str = ""
    result: dict | None = None


class QuestionReportRequest(BaseModel):
    """智能问析「生成报告」请求。

    只传问题与可选 SQL：报告要什么数据由后端自己取（跟着问题走），
    不接受前端把数据塞进来——否则权限过滤就绕过去了（前端能伪造任意行，
    后端拿不到"这些数据该不该给这个人看"的判断依据）。
    - question：用户那句原话，报告标题就用它
    - sql：上一轮问答产出的 SQL，仅作提示；后端仍会重新取数保证数据完整
    - title：想自定义标题时传，留空则用 question
    """
    question: str
    sql: str = ""
    title: str = ""


class ChartRequest(BaseModel):
    columns: list[str]
    rows: list[dict]
    title: str = ""
    force_type: str = ""  # bar | line | pie


class CustomChartRequest(BaseModel):
    title: str
    chart_type: str = "bar"
    description: str


class RecommendChartRequest(BaseModel):
    query: str
    columns: list[str]
    rows: list[dict]


class TrainRequest(BaseModel):
    table: str
    target: str
    features: list[str]
    model_type: str  # linear/decision_tree/random_forest/logistic/kmeans/isolation
    params: dict | None = None


class PredictRequest(BaseModel):
    model_name: str
    data: dict


class DatabaseConfig(BaseModel):
    db_type: str = "postgresql"
    host: str
    port: int = 5432
    database: str
    user: str
    password: str


class DatabaseSourceRequest(BaseModel):
    """多数据源注册（P1-1 对标 Spotter automatic model selection 渐进式落地）。"""
    name: str                # 源名称（唯一，用于 UI 展示与去重）
    db_type: str = "postgresql"
    host: str
    port: int = 5432
    database: str
    user: str
    password: str = ""


class MetricRequest(BaseModel):
    """指标注册：固定业务口径（指标名 / 口径 SQL / 单位 / 适用表 / 维度 / 有效期）"""
    name: str
    aliases: list[str] = []
    unit: str = ""
    tables: list[str] = []
    sql_expression: str = ""
    formula: str = ""
    description: str = ""
    dims: list[str] = []
    valid_from: str = ""  # 口径生效起始日期（ISO），空 = 至今有效
    valid_to: str = ""    # 口径失效日期（ISO，不含），空 = 永久有效


class LoginRequest(BaseModel):
    """登录：邮箱 + 密码（兼容旧账号用户名登录）"""
    email: str
    password: str


class EmailCodeRequest(BaseModel):
    """邮箱注册验证码请求"""
    email: str


class LdapLoginRequest(BaseModel):
    username: str
    password: str


class UpdateMeRequest(BaseModel):
    """账户设置：改昵称/头像/邮箱/手机/部门/职位/简介/偏好（仅本人）"""
    display_name: str | None = None
    avatar: str | None = None
    email: str | None = None
    phone: str | None = None
    department: str | None = None
    title: str | None = None
    bio: str | None = None
    preferences: dict | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class VerifyPasswordRequest(BaseModel):
    """管理员二次密码确认"""
    password: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    department: str | None = None
    title: str | None = None


class UserUpdateRequest(BaseModel):
    """管理员编辑用户"""
    role: str | None = None
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    department: str | None = None
    title: str | None = None
    bio: str | None = None
    note: str | None = None
    enabled: bool | None = None
    password: str | None = None


class RegisterRequest(BaseModel):
    """用户自助注册（开放接口）：邮箱验证码 + 密码，默认待授权角色，管理员授权前无数据权限"""
    email: str
    code: str
    password: str
    display_name: str = ""


# ── 训练与记忆管理请求体（Vanna 风格）──────────────────

class TrainSQLRequest(BaseModel):
    question: str
    sql: str
    table_name: str = ""


class TrainDocRequest(BaseModel):
    content: str


class FeedbackRequest(BaseModel):
    query: str
    sql: str = ""
    correct: bool = True
    table_name: str = ""


# ── 数据库连接（当前项目 pg8000/SQLAlchemy 体系）────────────

@app.get("/api/database/config", dependencies=[Depends(require_roles("admin"))])
def database_config():
    """当前活动数据源的连接参数（host/port/user/database）。

    权限修复（P1）：原端点无鉴权，返回内容含内网主机名、端口与库用户名，
    等价于把「后端连的是哪台机、哪个库」直接交给任何调用方（含开放模式下的匿名访客），
    配合 /api/database/test 可攒出内网拓扑。前端仅「系统设置」页（管理员）用它，
    因此直接收紧到 admin。
    """
    return get_database_config()


@app.post("/api/database/test", dependencies=[Depends(require_roles("admin"))])
def test_database(config: DatabaseConfig):
    """安全修复（P0）：原端点无鉴权，host/port/user/password 全由请求体控制 →
    可被用作 SSRF / 内网端口探测（错误信息还能区分"无法连接/认证失败/库不存在"，
    足以判定主机存活与服务类型），也可用来验证内网弱口令。此处要求 admin。"""
    try:
        test_database_connection(config.model_dump())
        return {"success": True, "message": "数据库连接成功"}
    except Exception as exc:
        # 友好包装（P2 优化）：pg8000 原始报错含 GBK 乱码与堆栈，直接回传体验差；
        # 常见错误映射为可读信息
        msg = str(exc)
        low = msg.lower()
        if "28p01" in low or "password" in low and ("fail" in low or "auth" in low):
            detail = "认证失败：用户名或密码错误"
        elif "3d000" in low or "does not exist" in low and "database" in low:
            detail = "数据库不存在"
        elif "could not connect" in low or "connection refused" in low or "econnrefused" in low:
            detail = "无法连接：请检查主机与端口（服务未启动或网络不通）"
        else:
            detail = f"数据库连接失败：{msg[:300]}"
        raise HTTPException(status_code=400, detail=detail) from exc


@app.post("/api/database/switch", dependencies=[Depends(require_roles("admin"))])
def switch_database_connection(config: DatabaseConfig):
    try:
        cfg = config.model_dump()
        # 密码为空或掩码占位（前端加载配置后回填 '********'）→ 回退当前环境密码，
        # 否则会把字面掩码当真实密码连接，报 28P01 认证失败。
        if not cfg.get("password") or cfg.get("password") == "********":
            import database as _dbmod
            cfg["password"] = getattr(_dbmod, "DB_PASSWORD", "") or ""
        switch_database(cfg)
        # 切换成功后持久化到 .env（与「保存配置」行为一致，避免重启后回退旧库）。
        # 注意 DatabaseConfig 字段是 database 而非 name，save_db_config_to_env 需要 name，
        # 必须补上，否则 KeyError 被吞掉导致持久化静默失败（切库不保存、重启回退）。
        try:
            from database import save_db_config_to_env
            save_cfg = dict(cfg)
            save_cfg.setdefault("name", save_cfg.get("database"))
            save_db_config_to_env(save_cfg)
        except Exception:
            pass
        # 同步 agent 的连接池（psycopg2 体系）
        try:
            from db.connection_manager import clear_active_cache
            clear_active_cache()
        except Exception:
            pass
        try:
            from db.executor import reload_pool
            reload_pool()
        except Exception:
            pass
        # 清空业务知识缓存（数据库已变）
        try:
            from routers.knowledge import clear_knowledge_cache
            clear_knowledge_cache()
        except Exception:
            pass
        # 清空结果缓存（TTL 缓存随库切换失效，避免跨库复用错误结果）
        try:
            from agent.llm_service import clear_cache
            clear_cache()
        except Exception:
            pass
        audit("database_switch", database=config.database, db_type=config.db_type)
        return {"success": True, "message": "数据库已切换", "config": get_database_config()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"数据库切换失败：{exc}") from exc


# ── 多数据源管理（P1-1 对标 Spotter automatic model selection 渐进式落地）──

def _refresh_db_side_effects():
    """数据库切换后的缓存清理（agent 连接池 / 知识缓存 / 结果缓存 / ER 方案缓存 + 预热），
    多源激活与 /api/database/switch 统一复用（B6 修复：switch 端点此前漏清 visualize 缓存）。"""
    try:
        from db.connection_manager import clear_active_cache
        clear_active_cache()
    except Exception:
        pass
    try:
        from db.executor import reload_pool
        reload_pool()
    except Exception:
        pass
    try:
        from routers.knowledge import clear_knowledge_cache
        clear_knowledge_cache()
    except Exception:
        pass
    try:
        from agent.llm_service import clear_cache
        clear_cache()
    except Exception:
        pass
    try:
        from routers.visualize import _plan_cache
        _plan_cache.clear()
    except Exception:
        pass
    try:
        from routers.visualize import prewarm_plan
        from database import get_database_config
        prewarm_plan(get_database_config().get("name", ""))
    except Exception:
        pass


@app.get("/api/database/sources", dependencies=[Depends(require_roles("admin"))])
def list_database_sources_api():
    """已注册数据源列表（不含密码，含活动标记）。

    权限修复（P1）：同族写接口（新增/删除/激活）本来就限 admin，读接口却敞着，
    列表里含每个源的 host/port/user/database —— 普通员工能据此推出还有哪些库、
    分别在哪台机。收紧到 admin，与写接口口径一致。
    """
    from database import get_database_sources
    return {"success": True, "sources": get_database_sources()}


@app.post("/api/database/sources", dependencies=[Depends(require_roles("admin"))])
def add_database_source_api(req: DatabaseSourceRequest):
    """注册新数据源（先测试连接，成功才保存）。"""
    from database import add_database_source
    r = add_database_source(req.model_dump())
    if not r.get("success"):
        raise HTTPException(status_code=400, detail=r.get("error", "注册失败"))
    audit("database_source_add", name=req.name, db_type=req.db_type, database=req.database)
    return r


@app.delete("/api/database/sources/{source_id}", dependencies=[Depends(require_roles("admin"))])
def remove_database_source_api(source_id: str):
    """删除数据源（不能删除活动源）。"""
    from database import remove_database_source
    if not remove_database_source(source_id):
        raise HTTPException(status_code=400, detail="删除失败：数据源不存在或是当前活动源")
    audit("database_source_remove", source_id=source_id)
    return {"success": True}


@app.post("/api/database/sources/{source_id}/activate", dependencies=[Depends(require_roles("admin"))])
def activate_database_source_api(source_id: str):
    """把某数据源设为活动源（切换连接 + 清理缓存）。"""
    from database import activate_database_source
    r = activate_database_source(source_id)
    if not r.get("success"):
        raise HTTPException(status_code=400, detail=r.get("error", "切换失败"))
    _refresh_db_side_effects()
    audit("database_source_activate", source_id=source_id)
    return {"success": True, "message": "数据源已激活", "config": get_database_config()}


@app.get("/api/database/sources/search", dependencies=[Depends(require_roles("admin"))])
def search_database_sources_api(q: str = "", limit: int = 8):
    """跨数据源搜索表（只读各非活动源元数据，不切换连接）。

    活动源内部搜索没命中时，这里能发现「表在哪个源」→ 前端展示建议切换。

    权限修复（P1）：原端点无鉴权，虽然只返回元数据，但会把**非活动源**的表名
    一并暴露（员工本不该知道自己没被授权的库里有什么表）。前端只有系统设置页用，
    收紧到 admin。
    """
    from database import search_across_sources
    return {"success": True, "results": search_across_sources(q, limit=limit)}


# ── 指标注册表（固定业务口径）──────────────────────────

@app.get("/api/metrics")
def list_metrics():
    """全部指标（内置只读 + 用户自定义）。

    仅返回**当前库有效**的指标（get_effective_metrics 按主表是否存在于当前库过滤）——
    保证「指标口径」页/知识页看到的每一条都以当前库(yans)为基准，不混入其它库口径。
    """
    from agent.metric_registry import get_effective_metrics
    return {"metrics": get_effective_metrics()}


@app.get("/api/metrics/search")
def search_metrics(q: str = ""):
    """按问题命中指标（用于前端展示/调试）。同时返回关键词命中与指标向量/BM25 检索结果，
    便于核对「同义/换说法查询」是否被第三层检索正确召回。"""
    from agent.metric_registry import find_metrics, get_metric_hint
    from agent.metric_memory import get_metric_memory
    rag = []
    try:
        rag = get_metric_memory().retrieve(q, top_k=5, with_scores=True)
    except Exception:
        rag = []
    return {"keyword": find_metrics(q, limit=5), "rag": rag, "hint": get_metric_hint(q)}


@app.get("/api/metrics/lineage")
def metric_lineage():
    """指标血缘（改造6）：派生指标 → 依赖的基础指标，供前端血缘图展示"""
    from agent.metric_registry import get_metric_lineage
    return get_metric_lineage()


@app.post("/api/metrics")
def create_metric(req: MetricRequest, authorization: str = Header(None)):
    """新增指标口径：管理员 直接入库；普通员工提交后走审批（P1 口径管理）"""
    from agent.metric_registry import create_user_metric
    from auth import get_user_roles
    u = get_current_user(authorization)
    if u["role"] == "guest":
        raise HTTPException(status_code=401, detail="请先登录")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="指标名不能为空")
    # 适用表必填（P2 定义闭环）：空表定义会让指标无法编译执行，并污染问题匹配（曾致"设备"坏指标）
    if not req.tables or not any(str(t).strip() for t in req.tables):
        raise HTTPException(status_code=400,
                            detail="请填写适用表（至少一张）——空表定义会让指标无法编译执行，并干扰问题匹配")
    # 口径 SQL 合法性校验（P2 定义闭环）：sql_expression 是聚合表达式（无 SELECT），包一层校验
    if req.sql_expression:
        from agent.sql_validator import validate_sql_safety
        valid, err, _clean = validate_sql_safety(f"SELECT {req.sql_expression}")
        if not valid:
            raise HTTPException(status_code=400, detail=f"口径 SQL 不合法: {err}")
        # 语法级校验：聚合表达式包 SELECT 后必须能被 sqlglot 解析（防存坏表达式，如误填整条 SELECT）
        try:
            import sqlglot
            sqlglot.parse_one(f"SELECT {req.sql_expression}", read="postgres")
        except Exception:
            raise HTTPException(status_code=400,
                                detail=f"口径 SQL 语法不合法（应为聚合表达式，如 SUM(x)/NULLIF(SUM(y),0)）: {req.sql_expression[:60]}")
    roles = set(get_user_roles(u))
    if not (roles & {"admin"}):
        # 普通员工 → 审批流（通过后由管理员在审批中心确认入库）
        from security.approval import create_request as _create_req
        item = _create_req(u["username"], "metric",
                           {"action": "create_metric", "metric": req.model_dump()},
                           reason=f"员工定义指标口径「{name}」")
        audit("metric_defined", user=u["username"], metric=name, pending=True)
        return {"success": True, "pending": True,
                "message": "指标定义已提交，待管理员审核通过后生效", "request": item}
    try:
        result = create_user_metric(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit("metric_defined", user=u["username"], metric=name, pending=False)
    return {"success": True, "pending": False, **result}


class MetricCompileRequest(BaseModel):
    """口径编译：自然语言公式 → SQL 聚合表达式草稿（P2 定义闭环）"""
    query: str = ""
    formula: str = ""
    tables: list[str] = []


def _llm_compile_metric(query: str, formula: str, tables: list[str]) -> tuple[str, list[str]]:
    """LLM 生成口径 SQL 草稿：自然语言公式 → 聚合表达式（不含 SELECT）。"""
    from agent.llm_service import _make_llm, _build_schema_fast
    from agent.metric_registry import find_metrics
    from langchain_core.messages import HumanMessage
    cand = [t for t in (tables or []) if t]
    if not cand:
        for m in find_metrics(query or formula):
            cand.extend(m.get("tables") or [])
    cand = cand[:8]
    schema = _build_schema_fast(cand) if cand else ""
    prompt = (
        "你是制造业指标口径 SQL 专家。根据业务公式生成一段可直接嵌入 SELECT 的聚合表达式 SQL"
        "（只输出表达式本身，不要 SELECT 关键字、不要任何解释）。\n"
        f"业务公式: {formula or query or ''}\n"
        f"可用表结构:\n{schema or '（未提供，按常见字段名推断）'}\n"
        "示例输出: SUM(good_qty) / NULLIF(SUM(input_qty),0) * 100"
    )
    try:
        llm = _make_llm(max_tokens=256)
        resp = llm.invoke([HumanMessage(content=prompt)])
        expr = str(resp.content or "").strip().splitlines()[0].strip()
    except Exception:
        expr = ""
    if not expr:
        return "", cand
    # 安全校验：包一层 SELECT 走 validate_sql_safety
    from agent.sql_validator import validate_sql_safety
    valid, _err, _clean = validate_sql_safety(f"SELECT {expr}")
    if not valid:
        return "", cand
    return expr, cand


@app.post("/api/metrics/compile")
def metric_compile_api(req: MetricCompileRequest, authorization: str = Header(None)):
    """自然语言口径 → SQL 草稿：确定性编译器（零 LLM）优先，回退 LLM 生成 + 安全校验。"""
    u = get_current_user(authorization)
    if u["role"] == "guest":
        raise HTTPException(status_code=401, detail="请先登录")
    if not (req.query.strip() or req.formula.strip()):
        raise HTTPException(status_code=400, detail="请提供业务公式或问题描述")
    try:
        from agent.metric_compiler import try_compile_metric
        compiled = try_compile_metric(req.formula.strip() or req.query.strip())
    except Exception:
        compiled = None
    if compiled:
        # 编译命中 → 返回该指标口径表达式（已存在，供参考）
        expr = (compiled.get("mql") or {}).get("metric_expression") or compiled.get("sql", "")
        if "=" in expr:
            expr = expr.split("=", 1)[1].strip()
        return {"success": True, "source": "compiler", "sql": expr,
                "tables": compiled.get("tables") or [], "metric": compiled.get("metric", ""),
                "unit": compiled.get("unit", ""), "compiled": True}
    # LLM 生成聚合表达式
    expr, tables = _llm_compile_metric(req.query, req.formula, req.tables)
    if not expr:
        raise HTTPException(status_code=400,
                            detail="暂时无法生成口径 SQL，可仅填写自然语言公式后提交（分析时按公式现场生成）")
    return {"success": True, "source": "llm", "sql": expr,
            "tables": tables, "metric": req.query.strip() or req.formula.strip(),
            "unit": "", "compiled": False}


@app.put("/api/metrics/{name}", dependencies=[Depends(require_roles("admin"))])
def update_metric(name: str, req: MetricRequest):
    """更新用户自定义指标（内置指标只读）"""
    from agent.metric_registry import BUILTIN_METRICS, _load_user_metrics, save_user_metrics
    if any(m["name"] == name for m in BUILTIN_METRICS):
        raise HTTPException(status_code=400, detail="内置指标只读，不能修改")
    user_metrics = _load_user_metrics()
    target = req.model_dump()
    target["name"] = target.get("name") or name
    for i, m in enumerate(user_metrics):
        if m["name"] == name:
            user_metrics[i] = target
            save_user_metrics(user_metrics)
            # 第三层：向量库即时重建
            try:
                from agent.metric_memory import get_metric_memory
                get_metric_memory().rebuild()
            except Exception:
                pass
            return {"success": True, "metric": target}
    # 不存在则新增
    user_metrics.append(target)
    save_user_metrics(user_metrics)
    # 第三层：向量库即时重建
    try:
        from agent.metric_memory import get_metric_memory
        get_metric_memory().rebuild()
    except Exception:
        pass
    return {"success": True, "metric": target}


@app.delete("/api/metrics/{name}", dependencies=[Depends(require_roles("admin"))])
def delete_metric(name: str):
    """删除用户自定义指标（内置指标只读）"""
    from agent.metric_registry import BUILTIN_METRICS, _load_user_metrics, save_user_metrics
    if any(m["name"] == name for m in BUILTIN_METRICS):
        raise HTTPException(status_code=400, detail="内置指标只读，不能删除")
    user_metrics = _load_user_metrics()
    remaining = [m for m in user_metrics if m["name"] != name]
    if len(remaining) == len(user_metrics):
        raise HTTPException(status_code=404, detail=f"指标「{name}」不存在")
    save_user_metrics(remaining)
    # 第三层：向量库即时重建（删除指标后不再被检索命中）
    try:
        from agent.metric_memory import get_metric_memory
        get_metric_memory().rebuild()
    except Exception:
        pass
    return {"success": True}


# ── 指标候选自动挖掘（对标 ThoughtSpot SpotterModel / Fabric Copilot 辅助建模 /
#    Aloudata CAN 指标自动发现）：自动提炼候选 → **人工审核**后才进注册表。
#    自动化只负责"覆盖率"，"正确性"必须留给人 —— 口径错了就是数据事故。
@app.post("/api/metrics/mine", dependencies=[Depends(require_roles("admin"))])
def mine_metric_candidates(limit: int = 10, use_llm: bool = True):
    """从历史成功 SQL 中挖掘尚未治理的指标口径候选（只进候选池，不自动入库）。"""
    from agent.metric_miner import mine_candidates
    res = mine_candidates(limit=max(1, min(limit, 30)), use_llm=use_llm)
    try:
        u = get_current_user()
        audit("metric_mine", user=u.get("username", ""), role=u.get("role", ""),
              total=res.get("total", 0))
    except Exception:
        pass
    return res


@app.post("/api/metrics/scan-schema", dependencies=[Depends(require_roles("admin"))])
def scan_schema_candidates_api(limit: int = 20, use_llm: bool = True):
    """语义层自动构建 Agent（P0-1，对齐极昆仑语义层构建 Agent）：扫描库表结构 → 候选指标。

    确定性扫描（维度表/事实表识别 + 数值列 SUM/AVG 候选 + 时间列标注）+ LLM 只做 NL 命名
    （中文名/同义词/单位/口径），候选并入 metric_miner 候选池（source=schema_scan），
    指标管理页人工采纳后才入库 —— human-in-loop，口径正确性留给人。
    与 /api/metrics/mine（历史 SQL 源）互补。
    """
    from agent.semantic_builder import scan_schema_candidates
    res = scan_schema_candidates(limit=max(1, min(limit, 40)), use_llm=use_llm)
    try:
        u = get_current_user()
        audit("metric_scan_schema", user=u.get("username", ""), role=u.get("role", ""),
              total=res.get("total", 0), added=res.get("added", 0))
    except Exception:
        pass
    return res


@app.get("/api/metrics/candidates")
def list_metric_candidates():
    """查看当前候选池（供指标管理页展示待审核清单）。"""
    from agent.metric_miner import list_candidates
    return {"success": True, "candidates": list_candidates()}


@app.put("/api/metrics/candidates/{cid}", dependencies=[Depends(require_roles("admin"))])
def update_metric_candidate(cid: str, patch: dict):
    """人工编辑候选（改名称/同义词/单位/说明）后再采纳。"""
    from agent.metric_miner import update_candidate
    c = update_candidate(cid, patch or {})
    if not c:
        raise HTTPException(status_code=404, detail="候选不存在或已被处理")
    return {"success": True, "candidate": c}


@app.post("/api/metrics/candidates/{cid}/adopt",
          dependencies=[Depends(require_roles("admin"))])
def adopt_metric_candidate(cid: str, body: dict | None = None):
    """人工采纳候选：写入指标注册表（human-in-loop 的确认环节）。"""
    from agent.metric_miner import adopt_candidate
    body = body or {}
    res = adopt_candidate(
        cid,
        name=str(body.get("name") or ""),
        aliases=body.get("aliases"),
        unit=str(body.get("unit") or ""),
        description=str(body.get("description") or ""),
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "采纳失败"))
    try:
        u = get_current_user()
        audit("metric_adopt", user=u.get("username", ""), role=u.get("role", ""),
              metric=(res.get("metric") or {}).get("name", ""), cid=cid)
    except Exception:
        pass
    return res


@app.delete("/api/metrics/candidates/{cid}",
            dependencies=[Depends(require_roles("admin"))])
def ignore_metric_candidate(cid: str):
    """忽略候选：移出候选池并记入黑名单，后续挖掘不再推荐同一口径。"""
    from agent.metric_miner import ignore_candidate
    if not ignore_candidate(cid):
        raise HTTPException(status_code=404, detail="候选不存在或已被处理")
    return {"success": True}


# 2026-10-02（用户需求）：候选口径「快捷管理」——指标管理页多选/全选后一键入库或忽略。
# 复用单条 adopt/ignore（自带黑名单、采纳留痕、名称兜底），这里只做批量循环与失败收集。
@app.post("/api/metrics/candidates/batch",
          dependencies=[Depends(require_roles("admin"))])
def batch_metric_candidates(body: dict):
    """候选批量处置：body = {ids: [...], action: "adopt"|"ignore"}。

    adopt  → 逐条入库（空名候选会失败并计入 failed，不影响其余）；
    ignore → 逐条移出候选池 + 黑名单。
    """
    from agent.metric_miner import adopt_candidate, ignore_candidate
    ids = body.get("ids") or []
    action = str(body.get("action") or "").strip()
    if action not in ("adopt", "ignore"):
        raise HTTPException(status_code=400, detail="action 仅支持 adopt / ignore")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    ok: list[str] = []
    failed: list[dict] = []
    for cid in ids[:200]:  # 上限保护：一次批量别把请求拖爆
        cid = str(cid)
        try:
            if action == "adopt":
                res = adopt_candidate(cid)  # 空参 = 用候选自带名称/别名/单位
                if res.get("success"):
                    ok.append(cid)
                else:
                    failed.append({"id": cid, "error": str(res.get("error", "采纳失败"))[:120]})
            else:
                if ignore_candidate(cid):
                    ok.append(cid)
                else:
                    failed.append({"id": cid, "error": "候选不存在或已被处理"})
        except Exception as e:
            failed.append({"id": cid, "error": str(e)[:120]})
    try:
        u = get_current_user()
        audit("metric_batch_%s" % action, user=u.get("username", ""),
              role=u.get("role", ""), total=len(ok), failed=len(failed))
    except Exception:
        pass
    return {"success": True, "ok": ok, "failed": failed}


# ── 经营记忆中心（P1-2，对标 FineBI 经营记忆：跨会话业务上下文复用）──

class MemoryNoteRequest(BaseModel):
    """业务口径备注（人工录入，注入 SQL 生成 prompt 的软约束）"""
    metric: str = ""
    note: str = ""


@app.get("/api/memory/preferences")
def memory_preferences_api(limit: int = 5):
    """高频（指标×维度）组合偏好（跨会话自动累计，只读）"""
    from agent.memory_center import get_preferences
    return {"success": True, "preferences": get_preferences(limit=limit)}


@app.get("/api/memory/notes")
def memory_notes_api(metric: str = "", authorization: str = Header(None)):
    """口径备注列表（按指标过滤 + 角色可见性）"""
    from agent.memory_center import list_notes
    u = get_current_user(authorization)
    return {"success": True,
            "notes": list_notes(metric, u.get("role", ""), u.get("username", ""))}


@app.post("/api/memory/notes")
def memory_add_note_api(req: MemoryNoteRequest, authorization: str = Header(None)):
    """新增口径备注（管理员；普通员工提交的仅供自己参考）"""
    from agent.memory_center import add_note
    from auth import get_user_roles
    u = get_current_user(authorization)
    if not (set(get_user_roles(u)) & {"admin"}):
        raise HTTPException(status_code=403, detail="仅管理员/分析师可录入口径备注")
    try:
        item = add_note(req.metric, req.note, u.get("username", ""), ",".join(get_user_roles(u)) or u.get("role", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit("memory_note_added", user=u["username"], metric=req.metric[:40])
    return {"success": True, "note": item}


@app.delete("/api/memory/notes/{nid}")
def memory_delete_note_api(nid: str, authorization: str = Header(None)):
    """删除口径备注（管理员）"""
    from agent.memory_center import delete_note
    from auth import get_user_roles
    u = get_current_user(authorization)
    if not (set(get_user_roles(u)) & {"admin"}):
        raise HTTPException(status_code=403, detail="仅管理员/分析师可删除口径备注")
    if not delete_note(nid):
        raise HTTPException(status_code=404, detail="备注不存在")
    audit("memory_note_deleted", user=u["username"])
    return {"success": True}


# ── 认证与权限（JWT 登录 / RBAC / 审计）──────────────────

def _user_actions(user: dict) -> list[str]:
    """计算用户的操作权限（export/share/download），admin 全放行；供登录/me 返回给前端按钮显隐"""
    try:
        from security.enforcer import build_acl_context
        from security import model as perm_model
        ctx = build_acl_context(user)
        return [a for a in perm_model.ACTION_PERMS if ctx.can_do(a)]
    except Exception:
        return []


@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request):
    """登录：邮箱/用户名 + 密码 → JWT token + 用户信息（新注册账号 username=邮箱）"""
    email = req.email.strip().lower()
    user = find_user(email) or find_user_by_email(email)
    if not user or not verify_password(req.password, user.get("password_hash", "")):
        audit("login_failed", email=req.email)
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    if not user.get("enabled", True):
        audit("login_blocked", username=user["username"])
        raise HTTPException(status_code=403, detail="该账号已被禁用，请联系管理员")
    record_login(user["username"], ip=request.client.host if request.client else "", ua=request.headers.get("user-agent", ""))
    token = create_token(user["username"], user["role"])
    audit("login", username=user["username"], role=user["role"])
    return {"token": token, **public_user(user), "actions": _user_actions(user)}


@app.post("/api/auth/email/send-code")
def send_email_code(req: EmailCodeRequest):
    """发送邮箱注册验证码（默认 60 秒冷却 / 5 分钟有效）"""
    from email_codes import send_code, EmailCodeError, EmailCooldownError
    try:
        return send_code(req.email)
    except EmailCooldownError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except EmailCodeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/auth/register")
def register(req: RegisterRequest):
    """用户自助注册：邮箱验证码 + 密码，默认「待授权」角色（pending），管理员授权前无任何数据访问权限。

    注册即签发 token（自动登录）；前端随后以 pending 身份进入工作台，
    数据查询被 enforcer 全表拒绝，直到管理员在权限页分配 普通员工 角色。
    """
    from auth import register_user
    from email_codes import verify_code
    if not verify_code(req.email, req.code):
        audit("register_failed", email=req.email, reason="验证码错误或已过期")
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    try:
        u = register_user(req.email, req.password, req.display_name)
    except ValueError as e:
        audit("register_failed", email=req.email, reason=str(e))
        raise HTTPException(status_code=400, detail=str(e)) from e
    token = create_token(u["username"], "pending")
    audit("user_registered", username=u["username"])
    return {"token": token, **u, "actions": []}


@app.get("/api/auth/idp/status")
def idp_status():
    """返回已启用的外部身份源（供前端决定是否展示对应登录入口）"""
    return {"oidc": OIDC_ENABLED, "ldap": LDAP_ENABLED}


@app.post("/api/auth/ldap/login")
def ldap_login(req: LdapLoginRequest):
    """LDAP 登录：LDAP bind 认证 → 映射/建本地用户 → 签发本地 JWT"""
    if not LDAP_ENABLED:
        raise HTTPException(status_code=400, detail="LDAP 登录未启用")
    try:
        user = ldap_authenticate(req.username.strip(), req.password)
    except ValueError as e:
        audit("login_failed", username=req.username, method="ldap")
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        audit("login_failed", username=req.username, method="ldap")
        raise HTTPException(status_code=401, detail=f"LDAP 认证失败：{str(e)[:120]}") from e
    token = create_token(user["username"], user["role"])
    audit("login", username=user["username"], role=user["role"], method="ldap")
    return {"token": token, **public_user(user)}


@app.get("/api/auth/oidc/authorize")
def oidc_authorize():
    """生成 OIDC 授权跳转 URL（前端拿到 auth_url 后整页跳转）"""
    if not OIDC_ENABLED:
        raise HTTPException(status_code=400, detail="OIDC 登录未启用")
    try:
        return oidc_authorize_url()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OIDC 配置错误：{str(e)[:160]}") from e


@app.get("/api/auth/oidc/callback")
def oidc_callback_route(code: str = "", state: str = ""):
    """OIDC 回调：code 换 token → 映射/建本地用户 → 签发本地 JWT（返回 JSON，前端自行落地）"""
    if not OIDC_ENABLED:
        raise HTTPException(status_code=400, detail="OIDC 登录未启用")
    if not code:
        raise HTTPException(status_code=400, detail="缺少授权码 code")
    try:
        user = oidc_callback(code, state)
    except ValueError as e:
        audit("login_failed", method="oidc", detail=str(e))
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        audit("login_failed", method="oidc", detail=str(e))
        raise HTTPException(status_code=401, detail=f"OIDC 认证失败：{str(e)[:160]}") from e
    token = create_token(user["username"], user["role"])
    audit("login", username=user["username"], role=user["role"], method="oidc")
    return {"token": token, **public_user(user)}


@app.get("/api/auth/me")
def auth_me(authorization: str = Header(None)):
    u = get_current_user(authorization)
    resp = {"username": u["username"], "role": u["role"], "auth_required": AUTH_REQUIRED}
    if u["role"] != "guest":
        user = find_user(u["username"])
        if user:
            resp.update(public_user(user))
    # 操作级权限：前端导出/下载/分享按钮的显隐依据（admin 全放行）
    try:
        from security.enforcer import build_acl_context
        from security import model as perm_model
        ctx = build_acl_context(u)
        resp["actions"] = [a for a in perm_model.ACTION_PERMS if ctx.can_do(a)]
    except Exception:
        resp["actions"] = []
    return resp


@app.put("/api/auth/me")
def update_me(req: UpdateMeRequest, authorization: str = Header(None)):
    """账户设置：改昵称/头像/邮箱/手机/部门/职位/简介/偏好（仅本人）"""
    u = get_current_user(authorization)
    if u["role"] == "guest":
        raise HTTPException(status_code=401, detail="请先登录")
    fields = req.model_dump(exclude_unset=True)
    if req.avatar and len(req.avatar) > 200 * 1024:
        raise HTTPException(status_code=400, detail="头像图片过大（上限 200KB）")
    try:
        user = update_user_profile(u["username"], **fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    audit("profile_updated", username=u["username"])
    return {"success": True, "user": user}


@app.put("/api/auth/password")
def change_password(req: ChangePasswordRequest, authorization: str = Header(None)):
    """修改密码：旧密码校验 + 新密码强度校验"""
    u = get_current_user(authorization)
    if u["role"] == "guest":
        raise HTTPException(status_code=401, detail="请先登录")
    user = find_user(u["username"])
    if not user or not verify_password(req.old_password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="旧密码错误")
    new_pw = req.new_password
    strength = password_strength(new_pw)
    if not strength["ok"]:
        raise HTTPException(status_code=400, detail=strength["message"])
    if new_pw == req.old_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")
    update_user_password(u["username"], new_pw)
    audit("password_changed", username=u["username"])
    return {"success": True, "force_relogin": True, "message": "密码已修改，请重新登录"}


@app.get("/api/auth/login-history")
def my_login_history(authorization: str = Header(None)):
    """当前用户的最近登录记录（供账户设置展示）"""
    u = get_current_user(authorization)
    if u["role"] == "guest":
        raise HTTPException(status_code=401, detail="请先登录")
    return {"history": get_login_history(u["username"], limit=20)}


# 二次密码确认防暴力锁（按用户隔离：username -> {fails, locked_until}）
# 若用全局单锁，admin A 输错 5 次会把 admin B 也锁 60s —— 必须按用户独立计数。
_verify_locks: dict[str, dict] = {}


@app.post("/api/auth/verify-password", dependencies=[Depends(require_roles("admin"))])
def verify_password_api(req: VerifyPasswordRequest, authorization: str = Header(None)):
    """管理员二次密码确认：只验密码、不发 token、不改会话。

    防暴力：单个用户连续 5 次失败 → 该用户锁定 60 秒（429），不影响其他管理员。
    """
    now = time.time()
    u = get_current_user(authorization)
    username = u["username"]
    lock = _verify_locks.setdefault(username, {"fails": 0, "locked_until": 0.0})
    if now < lock["locked_until"]:
        remain = int(lock["locked_until"] - now)
        raise HTTPException(status_code=429, detail=f"尝试次数过多，请 {remain} 秒后再试")
    user = find_user(username)
    if not user or not verify_password(req.password, user.get("password_hash", "")):
        lock["fails"] += 1
        if lock["fails"] >= 5:
            lock["fails"] = 0
            lock["locked_until"] = now + 60
            raise HTTPException(status_code=429, detail="尝试次数过多，请 60 秒后再试")
        return {"valid": False, "message": "密码错误"}
    lock["fails"] = 0
    audit("admin_verified", username=username)
    return {"valid": True, "message": "验证通过"}


@app.get("/api/auth/users", dependencies=[Depends(require_roles("admin"))])
def list_users():
    users = []
    for u in load_users():
        clean = {k: v for k, v in u.items() if k != "password_hash"}
        # 兼容旧数据：补默认值
        clean.setdefault("enabled", True)
        clean.setdefault("email", "")
        clean.setdefault("phone", "")
        clean.setdefault("department", "")
        clean.setdefault("title", "")
        clean.setdefault("bio", "")
        clean.setdefault("note", "")
        clean.setdefault("last_login", "")
        users.append(clean)
    return {"users": users}


@app.post("/api/auth/users", dependencies=[Depends(require_roles("admin"))])
def create_user(req: UserCreateRequest):
    username = req.username.strip()
    if not username or not req.password:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")
    if find_user(username):
        raise HTTPException(status_code=400, detail=f"用户「{username}」已存在")
    # 角色校验改为动态：权限模型里注册过的角色都可用（原先写死 admin/viewer，
    # 导致生产/设备/质量等岗位角色无法通过接口创建）。
    from security.model import get_role
    if not get_role(req.role):
        raise HTTPException(status_code=400, detail=f"角色不存在：{req.role}（请先在权限管理页新建该角色）")
    strength = password_strength(req.password)
    if not strength["ok"]:
        raise HTTPException(status_code=400, detail=strength["message"])
    from auth import hash_password
    from datetime import datetime, timezone
    users = load_users()
    users.append({
        "username": username, "role": req.role, "roles": [req.role],
        "display_name": (req.display_name or username).strip(), "avatar": "",
        "email": (req.email or "").strip(), "phone": (req.phone or "").strip(),
        "department": (req.department or "").strip(), "title": (req.title or "").strip(),
        "enabled": True,
        "password_hash": hash_password(req.password),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    save_users(users)
    audit("user_created", username=username, role=req.role)
    return {"success": True, "username": username, "role": req.role}


@app.put("/api/auth/users/{username}", dependencies=[Depends(require_roles("admin"))])
def update_user(username: str, req: UserUpdateRequest):
    """管理员编辑用户：角色/昵称/资料/启用禁用/重置密码。内置演示账号禁止改角色/禁用。"""
    if not find_user(username):
        raise HTTPException(status_code=404, detail=f"用户「{username}」不存在")
    patch = req.model_dump(exclude_unset=True)
    if username in ("admin", "viewer") and ("role" in patch or "enabled" in patch):
        raise HTTPException(status_code=400, detail="内置演示账号不能修改角色或禁用")
    try:
        user = update_user_admin(username, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    audit("user_updated", username=username, **{k: v for k, v in patch.items() if k != "password"})
    return {"success": True, "user": user}


@app.delete("/api/auth/users/{username}", dependencies=[Depends(require_roles("admin"))])
def delete_user(username: str):
    if username in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="内置演示账号不能删除")
    users = load_users()
    remaining = [u for u in users if u["username"] != username]
    if len(remaining) == len(users):
        raise HTTPException(status_code=404, detail=f"用户「{username}」不存在")
    save_users(remaining)
    audit("user_deleted", username=username)
    return {"success": True}


@app.get("/api/auth/audit", dependencies=[Depends(require_roles("admin"))])
def list_audit(limit: int = 100, offset: int = 0, event: str = "", user: str = "", format: str = ""):
    """操作审计日志（admin）：支持事件/用户筛选与分页；format=csv 导出"""
    res = query_audit_logs(limit=min(limit if limit else 100, 500), offset=offset, event=event, user=user)
    if format == "csv":
        import io
        buf = io.StringIO()
        buf.write("\ufeffts,event,user,detail\n")
        for e in res["items"]:
            detail = (e.get("detail") or {})
            if isinstance(detail, dict):
                detail = json_mod.dumps(detail, ensure_ascii=False)
            buf.write(",".join([
                str(e.get("ts", "")),
                str(e.get("event", "")),
                str(e.get("user") or e.get("username") or ""),
                str(detail or "").replace(",", "，").replace('"', '""'),
            ]) + "\n")
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                                headers={"Content-Disposition": "attachment; filename=audit_logs.csv"})
    return {"audit_logs": res["items"], "total": res["total"]}


# ── Agent 主接口 ─────────────────────────────────────────

@app.post("/api/agent/upload-csv", dependencies=[Depends(require_roles("admin"))])
async def upload_csv_api(file: UploadFile = File(...), authorization: str = Header(None)):
    """CSV 即传即分析（P2-C，对标 Spotter 3）：上传 CSV 建临时表，可立即提问分析。

    安全修复（P0）：原端点声明了 authorization 参数但从未使用（= 无鉴权），
    而 import_csv 内部会在**当前业务库**执行 CREATE TABLE / INSERT —— 平台对外承诺"只读 SELECT"。
    匿名可反复建表、写入脏数据、抢占磁盘与连接。此处要求 admin，与 /api/tables/import-csv 保持一致。

    临时表 1 小时自动清理（TTL），不污染业务表；写入走参数化绑定，无注入。
    """
    from agent.csv_import import import_csv
    try:
        content = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取上传文件失败：{e}")
    # 2026-10-01 可靠性修复：import_csv 是同步重 IO（解析最大 5MB CSV + 建表 + 逐行 INSERT），
    # 在 async 路由里直接调用会阻塞事件循环——导入期间所有 SSE 流式问答全部停摆。
    # 丢进线程池执行，事件循环保持畅通。
    import asyncio
    r = await asyncio.to_thread(import_csv, content, file.filename or "")
    if not r.get("success"):
        raise HTTPException(status_code=400, detail=r.get("error", "导入失败"))
    return {"success": True, **r}


@app.get("/api/agent/tmp-tables")
def list_tmp_tables_api():
    """当前存活的 CSV 临时表（P2-C）。"""
    from agent.csv_import import list_tmp_tables
    return {"success": True, "tables": list_tmp_tables()}


@app.delete("/api/agent/tmp-tables/{table}", dependencies=[Depends(require_roles("admin"))])
def drop_tmp_table_api(table: str):
    """立即删除一张 CSV 临时表（P2-C）。"""
    from agent.csv_import import drop_table
    if not drop_table(table):
        raise HTTPException(status_code=404, detail="临时表不存在或不属于上传数据")
    return {"success": True}


@app.post("/api/agent/ask")
def agent_ask_api(req: AskRequest, authorization: str = Header(None)):
    t0 = time.time()
    # 权限中心 v2：按当前用户的全部角色构建统一权限上下文（数据集/行/列/指标四级），
    # 注入查询链路后由 MQL→SQL 引擎层一处拦截改写。
    u = get_current_user(authorization)
    from security.enforcer import build_acl_context, acl_fingerprint
    from security.context import set_acl, clear_acl
    acl = build_acl_context(u)
    set_acl(acl)  # 同步接口在当前线程执行，这里设一次即可（run() 内还会再绑一次）
    service = LLMService(req.query, req.history, acl=acl, no_confirm=req.no_confirm)
    final_response = {}
    try:
        for event in service.run():
            if event["type"] == "done":
                final_response = event.get("response", {})
            elif event["type"] == "error":
                # 权限拦截（表权限）→ 403；其他错误 → 500
                code = 403 if event.get("code") == "forbidden" else 500
                raise HTTPException(status_code=code, detail=event.get("message", "未知错误"))
    finally:
        clear_acl()  # 防 ContextVar 泄漏到同线程的下一个请求（uvicorn 线程池复用）
    elapsed = int((time.time() - t0) * 1000)

    # 缓存结果供分析/预测端点复用（key 含 ACL 指纹，防越权复用）
    if service.sql_result.get("rows"):
        cache_result(req.query, getattr(service, "executed_sql", "") or service.sql,
                     service.sql_result, service.schema_context, acl_fp=acl_fingerprint(acl))

    # 注：权限白盒（acl 字段）已由 LLMService._build_response() 统一注入，
    # 同步与流式两条链路共用，此处不再重复拼装。

    # 操作审计（用户 / 问题 / 原始SQL / 改写后SQL / 指标口径引用 / 结果行数 / 意图 / 权限生效明细）
    try:
        _acl = service.acl_applied or {}
        audit("agent_ask", user=u["username"], role=u["role"], query=req.query,
              intent=service.intent, sql=service.sql,
              executed_sql=getattr(service, "executed_sql", "") or service.sql,
              metric_ref=getattr(service, "build_metric_ref", lambda: [])(),
              rows=service.sql_result.get("row_count", 0), elapsed_ms=elapsed,
              roles=acl.roles, acl_applied=service.acl_applied or None,
              masked_fields=_acl.get("masked") or None,          # 本次实际脱敏的字段（字段级溯源）
              row_filter_tables=_acl.get("row_filters") or None, # 本次注入的行过滤条件
              acl_trace=(getattr(service, "_build_acl_trace", lambda: None)()))
    except Exception:
        pass

    return AskResponse(type=final_response.get("type", ""), query=req.query, data=final_response, elapsed_ms=elapsed)



@app.get("/api/monitor/rules", dependencies=[Depends(get_current_user)])
def monitor_rules_api():
    """监控规则列表（含最新值/状态，所有登录用户可读）"""
    from agent.monitor import list_rules
    return {"rules": list_rules()}


@app.post("/api/monitor/rules", dependencies=[Depends(require_roles("admin"))])
def monitor_add_rule_api(req: MonitorRuleRequest):
    """新建监控规则（管理员）：配置 SQL + 变化阈值，后台定时检测异动"""
    from agent.monitor import add_rule
    rule = add_rule(req.name, req.sql, req.change_pct)
    audit("monitor_rule_add", rule=rule["id"], name=rule["name"], change_pct=rule["change_pct"])
    return {"success": True, "rule": rule}


@app.put("/api/monitor/rules/{rule_id}", dependencies=[Depends(require_roles("admin"))])
def monitor_update_rule_api(rule_id: str, req: MonitorRuleUpdate):
    from agent.monitor import update_rule
    rule = update_rule(rule_id, name=req.name, sql=req.sql,
                       change_pct=req.change_pct, enabled=req.enabled)
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"success": True, "rule": rule}


@app.delete("/api/monitor/rules/{rule_id}", dependencies=[Depends(require_roles("admin"))])
def monitor_delete_rule_api(rule_id: str):
    from agent.monitor import delete_rule
    if not delete_rule(rule_id):
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"success": True}


@app.post("/api/monitor/run", dependencies=[Depends(require_roles("admin"))])
def monitor_run_api():
    """立即全量执行一次所有启用规则（调试/初始化基线用）"""
    from agent.monitor import run_all
    n = run_all()
    return {"success": True, "alerts": n}


@app.get("/api/monitor/alerts", dependencies=[Depends(get_current_user)])
def monitor_alerts_api(limit: int = 30):
    """最近异动事件列表（登录用户可读，供总览页展示）"""
    from agent.monitor import list_alerts
    return {"alerts": list_alerts(limit=min(max(limit, 1), 100))}


@app.post("/api/agent/stream")
async def agent_stream_api(req: AskRequest, authorization: str = Header(None)):
    """流式输出端点 — SSE 协议"""
    from agent.stream import ask_stream
    # 权限中心 v2：构建统一权限上下文，交给流式链路（内部后台线程会重新绑定 ContextVar）
    u = get_current_user(authorization)
    from security.enforcer import build_acl_context
    acl = build_acl_context(u)
    return StreamingResponse(
        ask_stream(req.query, req.history, acl=acl, user=u, no_confirm=req.no_confirm),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@app.post("/api/agent/execute_confirm")
def agent_execute_confirm_api(req: ConfirmRequest, authorization: str = Header(None)):
    """二次确认弹窗：按用户确认的 SQL 直接执行（遵循 LLM 推断 / 按修改后 SQL）。

    走与主查询一致的表权限/表存在性校验，执行 + 图表 + 语义缓存沉淀，
    返回结构与流式 finalData 一致，前端复用现有渲染路径。
    """
    t0 = time.time()
    u = get_current_user(authorization)
    from security.enforcer import build_acl_context, acl_fingerprint
    from security.context import set_acl, clear_acl
    acl = build_acl_context(u)
    set_acl(acl)
    try:
        # 表权限校验：SQL 引用的表必须在角色允许集合内（None = 不限制）
        from agent.llm_service import _extract_sql_tables
        ref_tables = _extract_sql_tables(req.sql)
        if acl.allowed_tables is not None:
            denied = sorted(ref_tables - acl.allowed_tables)
            if denied:
                raise HTTPException(status_code=403,
                                    detail=f"查询涉及无权访问的表（{', '.join(denied)}），请联系管理员开通权限。")
        # 表存在性校验：防御用户/LLM 提交不存在的表名（跨库混用）
        from db.tools import get_real_tables
        real_names = {t["table_name"].split(".")[-1].lower() for t in get_real_tables()}
        bad = sorted(t for t in ref_tables if t not in real_names)
        if bad:
            raise HTTPException(status_code=400, detail=f"当前库不存在表: {', '.join(bad)}")
        # 列名校验：LLM 推断草稿可能用错列名（如 yans 的 order_status 被写成 status）→
        # 执行前拦截并明确提示，避免「点了遵循执行却无结果/静默失败」
        from agent.llm_service import _validate_sql_columns
        missing_cols = _validate_sql_columns(req.sql)
        if missing_cols:
            raise HTTPException(status_code=400,
                                detail="SQL 引用了当前库不存在的列: " + "; ".join(missing_cols[:6])
                                       + "。请在弹窗中修改为实际列名后再执行。")
        # 执行（含 10s 超时 / 5000 行上限 / EXPLAIN 闸门护栏）
        from db.executor import execute_sql, fill_param_placeholders
        # 兜底：LLM 推断草稿可能带 BETWEEN %s AND %s 占位符 → 用库内实际日期范围自动填充
        base_sql = fill_param_placeholders(req.sql)
        # 权限中心 v2 行级/列级改写（与主流程 _exec_sql 一致，防确认链路绕过 RLS 越权看数据）
        from security.enforcer import rewrite_sql as _acl_rewrite
        if acl is not None and acl.needs_sql_rewrite():
            base_sql, acl_err, _applied = _acl_rewrite(base_sql, acl)
            if acl_err:
                raise HTTPException(status_code=403,
                                    detail=f"权限校验：{acl_err}（请联系管理员确认您的行/列权限配置）")
        effective_sql = base_sql
        result = execute_sql(effective_sql)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "SQL 执行失败"))
        # 图表（数据结构自适应）
        from agent.chart_agent import generate_chart
        chart = generate_chart(result.get("columns") or [], result.get("rows") or [], req.query)
        _rc = result.get("row_count", 0)
        answer = f"已按您确认的查询执行，返回 {_rc} 行。"
        if _rc == 0:
            # 预警/不足/缺货类问法：0 行 = 当前确实没有符合条件的记录（如没有低于安全库存的
            # 材料），是正常业务结果，不是查询错误——明确告知，避免误导性归因
            if re.search(r"预警|低于安全|低于|缺货|不足|告警|超.*安全|偏低", req.query):
                answer += " 当前没有满足预警条件的记录（没有低于安全库存/缺货的材料），属正常结果；如您预期应有预警，请核对数据或放宽筛选条件。"
            else:
                answer += (" 查询返回 0 行：可能是筛选条件过严，或 JOIN 使用的关联列在两表间没有匹配值"
                           "（如产品名/编号不一致）。您可在弹窗中修改 SQL（改用外键列、去掉过滤条件）后重试。")
        # 语义缓存沉淀：仅沉淀有结果的 SQL（0 行/空结果不沉淀，防坏示例复用）；
        # 存用户确认的「原始 SQL」（未 RLS 改写），命中后主流程会重新按当前用户权限改写，
        # 避免把行级条件写死进缓存导致其他角色命中时条件叠加/越权。
        try:
            from agent.llm_service import _semantic_store, _current_db_key
            if result.get("rows"):
                _semantic_store(req.query, req.sql, [], "", chart.get("type", ""),
                                _current_db_key(), acl_fingerprint(acl))
        except Exception:
            pass
        # 结果溯源（血缘）：与主查询流式响应一致，确定性静态解析最终执行的 SQL
        # （含 RLS 改写后），保证「遵循执行」路径同样能看到每个数字的来源
        try:
            from agent.lineage import build_lineage as _build_lineage
            _dialect = ""
            try:
                from database import get_db_type as _gdt
                if _gdt() == "mysql":
                    _dialect = "mysql"
            except Exception:
                pass
            lineage = _build_lineage(effective_sql, dialect=_dialect)
            # 结果列表头翻译：词典未覆盖的英文字段用 AI 补中文短名（带缓存，异常静默）
            try:
                from agent.field_semantics import enrich_lineage_labels
                enrich_lineage_labels(lineage)
            except Exception:
                pass
        except Exception:
            lineage = {"tables": [], "columns": []}
        # 洞察正文（2026-09-03）：此前「遵循 LLM 执行」只回模板话术 + 图表，没有任何分析。
        # 分析类问法（相关/对比/趋势…）→ 结论式洞察（正面回答 + 确定性相关证据 + 建议）；
        # 普通查询 → 通用数据洞察。生成失败回退规则摘要，绝不阻塞返回。
        insight_text = ""
        try:
            from agent.llm_service import generate_insight_text
            insight_text = generate_insight_text(
                req.query, effective_sql,
                result.get("columns") or [], result.get("rows") or [],
                warning="")
        except Exception:
            insight_text = ""
        return {
            "type": "data_query",
            "query": req.query,
            "sql": effective_sql,
            "matched_tables": [],
            "result": {"columns": result.get("columns") or [],
                       "rows": result.get("rows") or [],
                       "row_count": result.get("row_count", 0)},
            "chart": chart,
            "lineage": lineage,
            "answer": answer,
            "analysis": insight_text,
            "metric_resolution": {"status": "no_hit", "hits": [], "hints": []},
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    finally:
        clear_acl()


@app.post("/api/agent/infer_confirm")
def agent_infer_confirm_api(req: AskRequest, authorization: str = Header(None)):
    """用户主动触发「按 LLM 推荐方案」：为未注册口径问题生成查询方案（含 sql_draft）。

    与 execute_confirm 的区别：本端点只【生成方案】并返回给前端打开二次确认弹窗
    （弹窗内可 遵循 LLM 执行 / 修改 SQL / 定义口径），不直接执行任何 SQL——
    严格遵守「LLM 产出 SQL 的唯一执行入口 = 用户二次确认后 execute_confirm」红线。
    适用场景：口径未命中引导条上点「✨ 推荐 LLM 执行」，或推断自动失败后手动重试。
    """
    t0 = time.time()
    u = get_current_user(authorization)
    from security.enforcer import build_acl_context
    from security.context import set_acl, clear_acl
    acl = build_acl_context(u)
    set_acl(acl)
    try:
        # 确定性双指标方案快速通道（2026-09-03）：无表权限限制（全表可访问）且问法
        # 命中 ≥2 注册指标、可按日配对时，注册口径直接拼 SQL 秒出方案（零 LLM、零表匹配），
        # 免去用户等 15~40s LLM 推断还偶发失败的体验。低权限账号走原链路（由 execute_confirm 校验权限）。
        if acl.allowed_tables is None:
            try:
                from agent.pair_plan import build_pair_plan
                _fast = build_pair_plan((req.query or "").strip())
                if _fast:
                    audit("infer_confirm", username=(u or {}).get("username", "guest"),
                          query=req.query, detail="deterministic-pair")
                    return {"analysis": _fast, "reason": None, "deterministic": True,
                            "elapsed_ms": int((time.time() - t0) * 1000)}
            except Exception:
                pass
        from agent.llm_service import LLMService
        svc = LLMService((req.query or "").strip(), req.history or [], acl=acl)
        if not svc.query:
            raise HTTPException(status_code=400, detail="query 不能为空")
        svc.matched_tables = svc._match_tables()
        if svc.allowed_tables is not None:
            svc.matched_tables = [
                t for t in svc.matched_tables
                if t["table_name"].split(".")[-1].lower() in svc.allowed_tables
            ]
        if not svc.matched_tables:
            return {"analysis": None, "reason": "no_table",
                    "message": "未能匹配到相关业务表，建议带上指标/表名关键词换个说法。"}
        analysis = svc._infer_analysis_confirm()
        if not analysis:
            return {"analysis": None, "reason": "timeout",
                    "message": "方案生成超时或失败，请稍后重试；或点「我来定义口径」注册后秒查。"}
        audit("infer_confirm", username=(u or {}).get("username", "guest"), query=req.query)
        return {"analysis": analysis, "reason": None,
                "elapsed_ms": int((time.time() - t0) * 1000)}
    finally:
        clear_acl()


@app.post("/api/agent/attribution")
def agent_attribution_api(req: AnalysisRequest):
    """规则化归因（Phase 6.1）：环比下跌检测 + 维度贡献分解（纯计算，不调 LLM）"""
    from agent.attribution import attribute_drop_api
    return attribute_drop_api(req.query, req.result or {}, [])


def _cache_acl_fp(authorization: str | None) -> str:
    """从请求头解析当前用户并计算 ACL 指纹（缓存隔离，防低权限命中高权限缓存）。"""
    from auth import get_current_user
    from security.enforcer import build_acl_context, acl_fingerprint
    return acl_fingerprint(build_acl_context(get_current_user(authorization)))


# ── 分析 / 预测 / 推荐（独立端点，不阻塞主查询）────────

@app.post("/api/agent/analyze")
async def agent_analyze_api(req: AnalysisRequest, authorization: str = Header(None)):
    """独立分析端点 — 对已执行的 SQL 结果做数据解读（流式）"""
    from agent.stream import analysis_stream

    sql_result = req.result or {}
    if not sql_result.get("rows"):
        cached = get_cached_result(req.query, acl_fp=_cache_acl_fp(authorization))
        if cached:
            sql_result = cached["sql_result"]

    if not sql_result.get("rows"):
        raise HTTPException(status_code=400, detail="无可用数据，请先执行查询")

    return StreamingResponse(
        analysis_stream(req.sql, req.query, sql_result),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/agent/predict")
async def agent_predict_api(req: AnalysisRequest, authorization: str = Header(None)):
    """独立预测端点 — 对已执行的 SQL 结果做趋势预测（流式）"""
    from agent.stream import predict_stream

    sql_result = req.result or {}
    if not sql_result.get("rows"):
        cached = get_cached_result(req.query, acl_fp=_cache_acl_fp(authorization))
        if cached:
            sql_result = cached["sql_result"]

    if not sql_result.get("rows"):
        raise HTTPException(status_code=400, detail="无可用数据，请先执行查询")

    return StreamingResponse(
        predict_stream(sql_result),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ── NL2Python 库外计算（对标 Fabric Code Interpreter / Vanna Python 沙箱 /
#    Smartbi 库外 Python 引擎）：SQL 算不出的相关性 / 分位数 / 聚类 / 假设检验等。
#    设计原则：生成与执行分离 —— plan 只出代码，run 执行「用户已确认」的代码，
#    与未注册口径走 human-in-loop 二次确认为同一条原则。
def _python_data(req_query: str, columns, rows, authorization: str):
    """取沙箱输入数据：优先用回传的 rows，否则按 query 取同 ACL 的结果缓存。"""
    columns, rows = list(columns or []), list(rows or [])
    if not rows:
        cached = get_cached_result(req_query, acl_fp=_cache_acl_fp(authorization))
        if cached:
            sr = cached.get("sql_result") or {}
            columns = sr.get("columns") or []
            rows = sr.get("rows") or []
    return columns, rows


@app.post("/api/agent/python/plan")
def agent_python_plan_api(req: PythonPlanRequest, authorization: str = Header(None)):
    """生成 Python 分析代码（**只生成，不执行**），供前端展示并让用户确认。"""
    u = get_current_user(authorization)
    from agent.python_agent import generate_python_plan

    columns, rows = _python_data(req.query, req.columns, req.rows, authorization)
    if not rows:
        raise HTTPException(status_code=400, detail="无可用数据，请先执行一次查询")

    plan = generate_python_plan(req.query, columns, rows)
    try:
        audit("python_plan", user=u["username"], role=u["role"], query=req.query,
              rows=len(rows), ok=plan.get("success", False))
    except Exception:
        pass
    return {"query": req.query, "row_count": len(rows), **plan}


@app.post("/api/agent/python/run", dependencies=[Depends(require_login())])
def agent_python_run_api(req: PythonRunRequest, authorization: str = Header(None)):
    """在受限沙箱中执行用户已确认的 Python 代码（只读数据注入，无写回 / 无网络）。

    安全修复（P0）：原端点无鉴权，开放模式（AUTH_REQUIRED=0）下 get_current_user 返回 guest
    也不拒绝 → 网络可达即可驱动沙箱执行。沙箱本身存在逃逸面，必须至少要求登录。
    """
    u = get_current_user(authorization)
    from agent.python_sandbox import run_python

    if not (req.code or "").strip():
        raise HTTPException(status_code=400, detail="代码为空")
    columns, rows = _python_data(req.query, req.columns, req.rows, authorization)

    res = run_python(req.code, columns, rows, timeout=req.timeout)
    try:
        audit("python_run", user=u["username"], role=u["role"], query=req.query,
              rows=len(rows), ok=res.get("success", False),
              blocked=res.get("blocked", False), elapsed_ms=res.get("elapsed_ms", 0),
              error=(res.get("error") or "")[:200] or None)
    except Exception:
        pass
    return {"query": req.query, "row_count": len(rows), **res}


@app.post("/api/dashboard/generate")
def dashboard_generate_api(req: DashboardRequest, authorization: str = Header(None)):
    """一键生成看板（对标 SpotterViz / FineBI 智能仪表板）。

    Agent 把一个分析主题拆成 3-8 个互补视角 → 逐卡确定性编译优先取数（走行/列级
    ACL）→ 按数据形态校正图型 → 批量配洞察 → 返回栅格可渲染结构。
    """
    t0 = time.time()
    u = get_current_user(authorization)
    from agent.dashboard_agent import build_dashboard
    from security.enforcer import build_acl_context
    from security.context import set_acl, clear_acl

    acl = build_acl_context(u)
    set_acl(acl)  # 看板卡片并发取数，ACL 通过 ContextVar 传给每个工作线程
    try:
        res = build_dashboard(req.query, max_cards=max(3, min(req.max_cards, 8)))
    finally:
        clear_acl()

    try:
        audit("dashboard_generate", user=u["username"], role=u["role"], query=req.query,
              cards=res.get("card_count", 0), ok=res.get("ok_count", 0),
              elapsed_ms=int((time.time() - t0) * 1000))
    except Exception:
        pass
    return {**res, "elapsed_ms": int((time.time() - t0) * 1000)}


# ── P1 能力端点 ────────────────────────────────────────────

@app.post("/api/agent/attribution-tree")
def attribution_tree_api(req: AnalysisRequest, authorization: str = Header(None)):
    """多指标归因树（P1-1，对标瓴羊）：主指标变动 → 指标层贡献 → 维度层下钻。

    加性指标（可加和，贡献度加总 100%）与比率指标（不可加和，仅作解释因素）分开处理。
    """
    from agent.attribution_tree import build_attribution_tree
    sql_result = req.result or {}
    if not sql_result.get("rows"):
        cached = get_cached_result(req.query, acl_fp=_cache_acl_fp(authorization))
        if cached:
            sql_result = cached["sql_result"]
    if not sql_result.get("rows"):
        raise HTTPException(status_code=400, detail="无可用数据，请先执行查询")
    return build_attribution_tree(req.query, sql_result)


@app.get("/api/insights/blind-spots")
def blind_spots_api(limit: int = 5):
    """盲点发现（P1-2，对标 FineBI）：提示"有数据但从没被问过"的表与维度。"""
    from agent.blind_spot import find_blind_spots
    return find_blind_spots(limit=max(1, min(limit, 12)))


@app.post("/api/insights/scan")
def insights_scan_api(table: str = "", limit: int = 3, max_insights: int = 8, push: bool = False,
                      authorization: str = Header(None)):
    """主动洞察自动探查（P1-4，对标 Tableau SpotIQ）。

    传 table → 只探查该表；不传 → 按行数探查重点表。
    与 monitor 的区别：monitor 要人工配规则盯已知指标，这里无需配置、自动扫维度组合找异常。
    push=True → 扫描后把洞察推送给订阅了 insight 事件的通道（交付闭环，需已配通道）。

    权限（2026-09-24 P0 修复）：原实现**无 authorization 参数、不校验登录、不注入 ACL**，
    任何人 `POST /api/insights/scan?table=任意表` 即可读到该表各维度聚合值。
    现按 `agent_ask_api` 同款范式注入 ACL 上下文，表级白名单在 `insight_scan` 内 fail-closed。
    """
    from security.enforcer import build_acl_context
    from security.context import set_acl, clear_acl
    acl = build_acl_context(get_current_user(authorization))
    set_acl(acl)
    try:
        from agent.insight_scan import scan_table, scan_top_tables, push_insights
        if table:
            r = scan_table(table)
        else:
            r = scan_top_tables(limit=max(1, min(limit, 8)), max_insights=max(1, min(max_insights, 20)))
        if push and r.get("success"):
            r["delivery"] = push_insights(r.get("insights") or [])
        return r
    finally:
        clear_acl()


@app.get("/api/skills")
def list_skills_api():
    """分析路径 Skill 列表（P1-3，对标 FineBI 分析路径沉淀）。"""
    from agent.skill_store import list_skills
    return {"success": True, "skills": list_skills()}


@app.delete("/api/skills/{sid}", dependencies=[Depends(require_roles("admin"))])
def delete_skill_api(sid: str):
    from agent.skill_store import delete_skill
    if not delete_skill(sid):
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return {"success": True}


@app.put("/api/skills/{sid}", dependencies=[Depends(require_roles("admin"))])
def rename_skill_api(sid: str, body: dict):
    from agent.skill_store import rename_skill
    s = rename_skill(sid, str(body.get("name") or ""))
    if not s:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return {"success": True, "skill": s}


# ── P2 能力端点 ────────────────────────────────────────────

@app.post("/api/knowledge/ask")
def knowledge_ask_api(req: DocAskRequest, authorization: str = Header(None)):
    """非结构化问答 + 引用溯源（P2-1，对标 Spotter 3）：基于知识库片段回答并标注引用。"""
    u = get_current_user(authorization)
    from agent.doc_qa import answer_with_citations
    r = answer_with_citations(req.query, k=max(1, min(req.k, 8)))
    try:
        audit("knowledge_ask", user=u["username"], role=u["role"], query=req.query,
              ok=r.get("success", False), citations=len(r.get("citations") or []))
    except Exception:
        pass
    return r


@app.get("/api/actions")
def list_actions_api():
    """动作清单（P2-2，对标 Sigma Agents）：含参数 schema，供前端渲染写回表单。"""
    from agent.action_agent import list_actions
    return {"success": True, "actions": list_actions()}


@app.post("/api/actions/propose", dependencies=[Depends(require_roles("admin"))])
def propose_action_api(req: ActionProposeRequest):
    """行动建议：LLM 从注册表挑动作并填参数（只产参数 JSON，绝不产 SQL）。"""
    from agent.action_agent import propose_action
    return propose_action(req.query, req.context)


@app.post("/api/actions/compile", dependencies=[Depends(require_roles("admin"))])
def compile_action_api(req: ActionExecuteRequest):
    """动作预览：确定性编译参数化 SQL（不执行），供用户二次确认前查看。"""
    from agent.action_agent import compile_action
    return compile_action(req.action, req.params)


@app.post("/api/actions/execute", dependencies=[Depends(require_roles("admin"))])
def execute_action_api(req: ActionExecuteRequest):
    """执行写回动作（P2-2）：二次确认 + 白名单 + 参数化绑定 + 审计。"""
    u = get_current_user()
    from agent.action_agent import execute_action
    r = execute_action(req.action, req.params, confirmed=req.confirmed,
                       operator=u.get("username", "") if u else "")
    try:
        audit("action_execute", user=u.get("username", ""), role=u.get("role", ""),
              action=req.action, ok=r.get("success", False),
              affected=r.get("rows_affected"), error=(r.get("error") or "")[:160] or None)
    except Exception:
        pass
    return r


@app.get("/api/ops/overview")
def ops_overview_api():
    """运行时观测（对标 Fabric data agent 监控）：编译命中率 / 耗时 / 成功率 / 最近失败。

    编译命中率是确定性编译覆盖率的直接体现——命中率低说明大量查询走 LLM 兜底
    （慢且不稳），应在指标管理页采纳候选口径来提升。
    """
    from agent.observability import get_overview
    return get_overview()


@app.get("/api/notify/channels", dependencies=[Depends(require_roles("admin"))])
def list_notify_channels_api():
    """推送通道列表（P2-3，对标 Pulse/钉钉/企微）。

    安全修复（P0）：原端点无鉴权，且 notifier.list_channels() 直接 dict(c) 返回完整通道配置，
    其中 webhook_url 内嵌企微/钉钉/飞书的机器人 token → 任意人拿到即可伪装系统身份
    向企业群推送消息（钓鱼/伪造告警），或反向作为 SSRF 目标。此处限 admin。
    """
    from agent.notifier import list_channels
    return {"success": True, "channels": list_channels()}


@app.post("/api/notify/channels", dependencies=[Depends(require_roles("admin"))])
def add_notify_channel_api(req: NotifyChannelRequest):
    from agent.notifier import add_channel
    return {"success": True, "channel": add_channel(req.name, req.webhook_url, req.type)}


@app.delete("/api/notify/channels/{cid}", dependencies=[Depends(require_roles("admin"))])
def delete_notify_channel_api(cid: str):
    from agent.notifier import delete_channel
    if not delete_channel(cid):
        raise HTTPException(status_code=404, detail="通道不存在")
    return {"success": True}


@app.get("/api/notify/subscriptions")
def list_notify_subscriptions_api():
    from agent.notifier import list_subscriptions
    return {"success": True, "subscriptions": list_subscriptions()}


@app.post("/api/notify/subscriptions", dependencies=[Depends(require_roles("admin"))])
def add_notify_subscription_api(body: dict):
    from agent.notifier import add_subscription
    s = add_subscription(str(body.get("channel_id") or ""),
                         body.get("event_types") or [],
                         float(body.get("min_severity") or 0.0),
                         int(body.get("throttle_min") or 30))
    return {"success": True, "subscription": s}


@app.delete("/api/notify/subscriptions/{sid}", dependencies=[Depends(require_roles("admin"))])
def delete_notify_subscription_api(sid: str):
    from agent.notifier import delete_subscription
    if not delete_subscription(sid):
        raise HTTPException(status_code=404, detail="订阅不存在")
    return {"success": True}


# ── 定时报表订阅（交付闭环，对标 Tableau Pulse / 白泽 定时推送）──

class ReportScheduleRequest(BaseModel):
    """新建定时报表订阅。report_type 当前支持 insights（主动洞察日报）。"""
    name: str
    interval_min: int = 60
    report_type: str = "insights"


@app.get("/api/report/schedules")
def list_report_schedules_api():
    """定时报表订阅列表。"""
    from agent.report_scheduler import list_schedules
    return {"success": True, "schedules": list_schedules()}


@app.post("/api/report/schedules", dependencies=[Depends(require_roles("admin"))])
def add_report_schedule_api(req: ReportScheduleRequest):
    from agent.report_scheduler import add_schedule
    sched = add_schedule(req.name, req.interval_min, req.report_type)
    return {"success": True, "schedule": sched}


@app.delete("/api/report/schedules/{sid}", dependencies=[Depends(require_roles("admin"))])
def delete_report_schedule_api(sid: str):
    from agent.report_scheduler import delete_schedule
    if not delete_schedule(sid):
        raise HTTPException(status_code=404, detail="订阅不存在")
    return {"success": True}


@app.post("/api/agent/recommend")
def agent_recommend_api(req: AnalysisRequest, authorization: str = Header(None)):
    """独立推荐问题端点 — 根据查询上下文生成后续问题"""
    sql_result = req.result or {}
    sql = req.sql
    schema_context = ""

    if not sql_result.get("rows"):
        cached = get_cached_result(req.query, acl_fp=_cache_acl_fp(authorization))
        if cached:
            sql_result = cached["sql_result"]
            sql = sql or cached.get("sql", "")
            schema_context = cached.get("schema_context", "")

    if not sql_result.get("rows"):
        raise HTTPException(status_code=400, detail="无可用数据，请先执行查询")

    questions = generate_recommend_questions(req.query, sql, sql_result, schema_context)
    return {"questions": questions}


# ── 图表生成 ────────────────────────────────────────────

@app.post("/api/chart/generate")
def chart_generate(cfg: ChartRequest):
    return generate_chart(cfg.columns, cfg.rows, cfg.title, force_type=cfg.force_type)


@app.post("/api/chart/custom")
def chart_custom(cfg: CustomChartRequest, authorization: str = Header(None)):
    """自定义图表: 用户描述 → LLMService SQL 生成 → 执行 → 生成图表（带行列级权限）"""
    # 权限注入：与 /api/agent/ask、/api/agent/stream 一致，统一走 enforcer 生效权限（v2 单轨）。
    # 历史问题：v1 函数读取的是 v2 结构文件（顶层无 role 键）→ 返回空/None = 无限制，
    # 导致 chart/custom 实际无任何权限拦截；改用 build_acl_context 后与 ask/stream 同源。
    u = get_current_user(authorization)
    from security.enforcer import build_acl_context
    acl = build_acl_context(u)
    service = LLMService(cfg.description, acl=acl)
    service.intent = "data"
    service.matched_tables = []
    try:
        service.matched_tables = service._match_tables()[:8]
    except Exception:
        pass

    if service.matched_tables:
        from agent.llm_service import _build_schema_fast
        candidate_names = [t["table_name"] for t in service.matched_tables]
        service.schema_context = _build_schema_fast(candidate_names)

    list(service._generate_sql_stream())  # 流式积攒 SQL
    sql = service.sql
    if not sql or not sql.strip().upper().startswith("SELECT"):
        raise HTTPException(status_code=400, detail="无法生成有效的SQL，请换一种描述方式")

    # 统一走安全执行（应用行列级权限改写，避免绕过）
    result = service._exec_sql(sql)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=f"SQL执行失败: {result.get('error','')}")

    if not result["rows"] or len(result["rows"]) == 0:
        raise HTTPException(status_code=400, detail="查询无数据，请调整描述")

    chart = generate_chart(result["columns"], result["rows"], cfg.title, force_type=cfg.chart_type)
    return {
        "title": cfg.title,
        "chart": chart,
        "sql": sql,
        "data": {"columns": result["columns"], "rows": result["rows"][:20]},
    }


@app.post("/api/chart/recommend-type")
def chart_recommend_type(cfg: RecommendChartRequest):
    """AI 推荐最佳图表类型"""
    from langchain_core.messages import SystemMessage, HumanMessage

    col_info = ", ".join(cfg.columns)
    row_count = len(cfg.rows)
    try:
        llm = _make_llm(max_tokens=512)
        prompt = """你是数据可视化专家。根据以下信息推荐最佳图表类型。

可用类型: bar(柱状图), barh(横向柱状图), stacked(堆叠柱状图), line(折线图), area(面积图), pie(饼图), donut(环形图), scatter(散点图)

规则:
- 排名/对比多类 → barh
- 多指标对比 → bar
- 堆叠组成/占比对比 → stacked
- 趋势/时间序列 → line 或 area
- 占比/分布(<8类) → donut
- 两数值列相关性 → scatter
- 默认 → bar

只输出类型关键词,不解释。"""
        resp = llm.invoke([SystemMessage(content=prompt), HumanMessage(
            content=f"查询: {cfg.query}\n列: {col_info}\n共{row_count}行, 示例行: {str(cfg.rows[0])}")])
        rec = resp.content.strip().lower()
        valid = ["bar", "barh", "stacked", "line", "area", "pie", "donut", "scatter"]
        if rec not in valid:
            rec = "bar"
    except Exception:
        rec = "bar"
    return {"recommended": rec}


# ── ML 建模 ─────────────────────────────────────────────

@app.get("/api/ml/tables")
def ml_tables():
    from ml.trainer import get_numeric_tables
    return {"tables": get_numeric_tables()}


@app.get("/api/ml/models")
def ml_models():
    from ml.trainer import MODEL_TYPES, list_trained_models
    return {"model_types": MODEL_TYPES, "trained": list_trained_models()}


@app.post("/api/ml/train", dependencies=[Depends(require_roles("admin"))])
def ml_train(req: TrainRequest):
    from ml.trainer import MODEL_TYPES, train_model
    if req.model_type not in MODEL_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的模型类型: {req.model_type}")
    try:
        result = train_model(req.table, req.target, req.features, req.model_type, req.params)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ml/predict", dependencies=[Depends(require_roles("admin"))])
def ml_predict(req: PredictRequest):
    from ml.trainer import predict
    try:
        return predict(req.model_name, req.data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── 训练与记忆管理（Vanna 风格：Train → Retrieve → Feedback 闭环）──

@app.post("/api/train/sql", dependencies=[Depends(require_roles("admin"))])
def train_sql_api(req: TrainSQLRequest):
    """训练一条 (问题→SQL) 示例（对齐 Vanna train(question, sql)）"""
    from agent.memory import get_memory
    try:
        # 校验 SQL 是安全的 SELECT
        from agent.sql_validator import validate_sql_safety
        valid, err, cleaned = validate_sql_safety(req.sql)
        if not valid:
            raise HTTPException(status_code=400, detail=f"SQL 不合法: {err}")
        mem_id = get_memory().add_sql(req.question, cleaned, table_name=req.table_name, source="manual")
        return {"success": True, "memory_id": mem_id,
                "message": f"已保存示例: 「{req.question[:30]}」"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")


@app.post("/api/train/documentation", dependencies=[Depends(require_roles("admin"))])
def train_documentation_api(req: TrainDocRequest):
    """训练业务文档/口径（对齐 Vanna train(documentation)）"""
    from agent.memory import get_memory
    try:
        doc_id = get_memory().add_documentation(req.content, source="manual")
        return {"success": True, "memory_id": doc_id, "message": "业务知识已保存"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")


@app.post("/api/train/rebuild", dependencies=[Depends(require_roles("admin"))])
def train_rebuild_api():
    """从当前数据库自动重建 DDL 索引（对齐 Vanna train(ddl) 自动版）"""
    from agent.memory import get_memory
    try:
        result = get_memory().rebuild_ddl()
        # 第三层：同步重建指标口径向量库
        try:
            from agent.metric_memory import get_metric_memory
            metric_result = get_metric_memory().rebuild()
        except Exception:
            metric_result = {"rebuilt": 0, "embedded": False}
        return {"success": True, **result, "metrics": metric_result,
                "message": f"已构建 {result['saved']} 张表的 DDL 索引、{metric_result['rebuilt']} 个指标向量"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重建失败: {e}")


@app.get("/api/memory/list")
def memory_list_api(limit: int = 50):
    """查看当前数据库的全部记忆（对应 Vanna /memories）"""
    from agent.memory import get_memory
    return get_memory().list_memories(limit=min(limit, 200))


@app.delete("/api/memory/{memory_id}", dependencies=[Depends(require_roles("admin"))])
def memory_delete_api(memory_id: int):
    """删除一条记忆（对应 Vanna /delete [id]）"""
    from agent.memory import get_memory
    deleted = get_memory().delete_memory(memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"success": True, "message": "已删除"}


@app.post("/api/feedback", dependencies=[Depends(require_login())])
def feedback_api(req: FeedbackRequest):
    """用户反馈：SQL 正确 → 沉淀记忆 + 语义缓存（对齐 Vanna train on feedback）；
    错误 → 审计留痕 + 从语义/结果缓存剔除该问题（防错误 SQL 被复用）。

    安全修复（P0）：原端点无鉴权，且正向反馈路径**不做 SQL 安全校验**（对比 /api/train/sql
    会走 validate_sql_safety），并集以 acl_fingerprint(None) 落库 → 匿名攻击者可把任意
    (问题, SQL) 写进长期记忆与语义缓存，污染其他用户后续的 NL2SQL 结果（持久化提示注入 /
    供应链投毒）。此处要求登录，并补 SQL 只读校验后才允许沉淀。
    """
    if not req.correct:
        # 负反馈：审计留痕（不污染正向记忆），并尝试从语义缓存/结果缓存剔除该问题
        try:
            audit("feedback_negative", query=req.query[:200], sql=(req.sql or "")[:500], table=req.table_name or "")
        except Exception:
            pass
        # P1-B 知识自生长（半自动，对标 Data Neo「反馈→规则」）：
        # 被点踩的查询若含未注册聚合口径，提炼为候选供管理员审核——被点踩的口径
        # 进入治理视野而非被遗忘。保留人工审核闸门：只进候选池，绝不自动入库。
        try:
            from agent.metric_miner import suggest_from_query
            if req.sql:
                n = suggest_from_query(req.query, req.sql, source="feedback_negative")
                if n:
                    audit("feedback_miner_candidates", query=req.query[:200], added=n)
        except Exception:
            pass
        try:
            from agent.llm_service import _semantic_remove, _current_db_key
            removed = _semantic_remove(req.query, _current_db_key())
            if removed:
                audit("feedback_cache_purged", query=req.query[:200], removed=removed)
        except Exception:
            pass
        try:
            import cache_store
            from agent.llm_service import _cache_key
            from security.enforcer import acl_fingerprint
            key = _cache_key(req.query, acl_fingerprint(None))
            if cache_store.cache_get(key):
                cache_store.cache_delete(key)
        except Exception:
            pass
        return {"success": True, "message": "已记录负反馈（该 SQL 不会用于学习，且已清理相关缓存），管理员可在审计日志中核查"}
    if not req.sql:
        return {"success": True, "message": "反馈已记录"}
    try:
        from agent.memory import get_memory
        mem_id = get_memory().add_sql(req.query, req.sql, table_name=req.table_name, source="feedback")
        # P1-B 知识自生长：用户修正/确认后的 SQL 同样提炼口径候选（半自动，人工审核）——
        # 被确认是对的查询路径里若含未注册聚合，管理员采纳后同类问题即可编译命中。
        try:
            from agent.metric_miner import suggest_from_query
            suggest_from_query(req.query, req.sql, source="feedback_positive")
        except Exception:
            pass
        # 正确反馈同时沉淀语义缓存：构造 matched_tables（feedback 提供表名则用之，否则留空由 SQL 解析）
        try:
            from agent.llm_service import _semantic_store, _current_db_key
            from security.enforcer import acl_fingerprint
            matched = [{"table_name": req.table_name}] if req.table_name else []
            _semantic_store(req.query, req.sql, matched, "", "",
                            _current_db_key(), acl_fingerprint(None))
        except Exception:
            pass
        return {"success": True, "memory_id": mem_id, "message": "已学习该正确 SQL 并写入语义缓存，下次同类问题将直接参考"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")


# ═══ 客服中心（AI 客服 + 人工客服工单）═════════════════════
# 与「智能问析」链路隔离：只做日常使用答疑 + 人工工单，不查数据库、不写分析记忆。

class SupportAiMessage(BaseModel):
    role: str = "user"
    content: str = ""

class SupportAiChatReq(BaseModel):
    messages: list[SupportAiMessage] = Field(default_factory=list)
    client_id: str = ""

class SupportConvoReq(BaseModel):
    client_id: str = ""
    summary: str = ""     # 转人工时携带的 AI 阶段上下文摘要
    user_name: str = ""

class SupportSendReq(BaseModel):
    text: str = ""
    client_id: str = ""

class SupportFeedbackReq(BaseModel):
    """业务知识「有误」反馈：不新开会话，直接作为一条大号待办消息进入用户的人工客服会话"""
    item_key: str = ""
    item_kind: str = ""
    item_title: str = ""
    item_scene: str = ""
    item_table: str = ""
    item_desc: str = ""
    note: str = ""
    client_id: str = ""
    user_name: str = ""

class SupportFeedbackStatusReq(BaseModel):
    status: str = "done"          # done = 已处理 / open = 未处理 / rejected = 反馈不成立（知识无误）
    admin_note: str = ""
    reply: str = ""               # 发给用户的回复（反馈不成立时用来告知「该知识无误」）


def _support_identity(authorization: str = None, client_id: str = ""):
    """解析客服身份：登录用户 → username；开放模式游客 → guest:<client_id>（按浏览器区分）"""
    u = get_current_user(authorization) or {}
    uname = u.get("username") or "guest"
    if uname == "guest":
        uname = f"guest:{(client_id or 'anonymous').strip()[:32]}"
    # 多角色语义（与 require_roles/get_user_roles 一致），避免主角色非 admin 但 roles 含 admin 被误判
    roles = set(get_user_roles(u)) if u else {"guest"}
    return uname, roles


@app.post("/api/support/ai-chat")
def support_ai_chat_api(req: SupportAiChatReq):
    """客服 AI：携带会话内多轮记忆（前端传最近若干轮 history），不持久化。

    返回 {answer, need_human} —— need_human=true 时前端展示「转人工」入口。
    """
    from agent.support import ai_support_reply
    history = [{"role": m.role, "content": (m.content or "")[:2000]} for m in (req.messages or [])[-12:]]
    if not history or not history[-1].get("content"):
        raise HTTPException(status_code=400, detail="缺少提问内容")
    try:
        return ai_support_reply(history)
    except Exception as e:
        return {"answer": f"客服 AI 暂不可用：{e}\n可直接转接人工客服留言。", "need_human": True, "reason": "exception"}


@app.get("/api/support/conversations")
def support_list_convos_api(authorization: str = Header(None),
                            x_client_id: str = Header(None, alias="X-Client-Id")):
    """会话列表：管理员看全部（含未读），普通用户只看自己的"""
    from agent.support import list_convos, _convo_summary
    uname, roles = _support_identity(authorization, x_client_id or "")
    is_admin = "admin" in roles
    out = []
    for c in list_convos():
        if not is_admin and c["user_key"] != uname:
            continue
        out.append(_convo_summary(c, unread_who="admin" if is_admin else "user"))
    return {"conversations": out, "is_admin": is_admin, "me": uname}


@app.post("/api/support/conversations")
def support_create_convo_api(req: SupportConvoReq, authorization: str = Header(None),
                             x_client_id: str = Header(None, alias="X-Client-Id")):
    """新建/复用人工客服会话；summary 为「转人工」时携带的问题摘要"""
    from agent.support import create_convo
    uname, _ = _support_identity(authorization, req.client_id or x_client_id or "")
    name = (req.user_name or "").strip() or (uname.split(":", 1)[-1] if uname.startswith("guest:") else uname)
    convo = create_convo(uname, name, req.summary)
    return {"conversation": convo}


@app.get("/api/support/conversations/{cid}/messages")
def support_messages_api(cid: str, authorization: str = Header(None),
                         x_client_id: str = Header(None, alias="X-Client-Id")):
    from agent.support import get_convo, mark_read
    uname, roles = _support_identity(authorization, x_client_id or "")
    c = get_convo(cid)
    if not c:
        raise HTTPException(status_code=404, detail="会话不存在")
    if "admin" not in roles and c["user_key"] != uname:
        raise HTTPException(status_code=403, detail="无权查看该会话")
    # 打开即已读（管理员开用户会话 → 标记管理员侧已读；用户开自己会话 → 标记用户侧已读）
    mark_read(cid, "admin" if "admin" in roles else "user")
    return {"conversation": get_convo(cid)}


@app.post("/api/support/conversations/{cid}/messages")
def support_send_api(cid: str, req: SupportSendReq, authorization: str = Header(None),
                     x_client_id: str = Header(None, alias="X-Client-Id")):
    """发消息：管理员以「人工客服」身份回复，普通用户只能在自己的会话发言"""
    from agent.support import get_convo, add_message
    uname, roles = _support_identity(authorization, req.client_id or x_client_id or "")
    c = get_convo(cid)
    if not c:
        raise HTTPException(status_code=404, detail="会话不存在")
    is_admin = "admin" in roles
    if is_admin:
        sender, sender_name = "agent", "人工客服"
    else:
        if c["user_key"] != uname:
            raise HTTPException(status_code=403, detail="无权在该会话发言")
        sender, sender_name = "user", c["user_name"]
    msg = add_message(cid, sender, sender_name, req.text)
    if not msg:
        raise HTTPException(status_code=400, detail="消息内容为空")
    return {"message": msg, "conversation": get_convo(cid)}


@app.post("/api/support/knowledge-feedback")
def support_knowledge_feedback_api(req: SupportFeedbackReq, authorization: str = Header(None),
                                   x_client_id: str = Header(None, alias="X-Client-Id")):
    """业务知识「反馈有误」：把反馈作为一条大号待办消息追加进该用户的人工客服会话。

    刻意不调用 create 新会话接口：由后端复用同一用户的未关闭会话，前端不必新开聊天框。
    """
    from agent.support import add_feedback
    uname, _ = _support_identity(authorization, req.client_id or x_client_id or "")
    name = (req.user_name or "").strip() or (uname.split(":", 1)[-1] if uname.startswith("guest:") else uname)
    out = add_feedback(uname, name, {
        "key": req.item_key, "kind": req.item_kind, "title": req.item_title,
        "scene": req.item_scene, "table": req.item_table, "desc": req.item_desc,
    }, req.note)
    return {"success": True, "conversation": out["conversation"], "message": out["message"]}


@app.post("/api/support/feedback/{mid}/status", dependencies=[Depends(require_roles("admin"))])
def support_feedback_status_api(mid: str, req: SupportFeedbackStatusReq):
    """管理员处理业务知识反馈：标记 已处理 / 反馈不成立 / 退回未处理。

    status=rejected 且带 reply 时，会把回复作为一条「人工客服」消息真正发给用户，
    用于告知「经核实该知识无误」并留痕。
    """
    from agent.support import set_feedback_status
    out = set_feedback_status(mid, req.status, req.admin_note, "管理员", req.reply)
    if not out:
        raise HTTPException(status_code=404, detail="反馈不存在")
    return {"success": True, "message": out["message"],
            "reply_message": out.get("reply_message"),
            "conversation": out["conversation"]}


@app.post("/api/support/conversations/{cid}/read")
def support_read_api(cid: str, authorization: str = Header(None),
                     x_client_id: str = Header(None, alias="X-Client-Id")):
    from agent.support import mark_read
    uname, roles = _support_identity(authorization, x_client_id or "")
    mark_read(cid, "admin" if "admin" in roles else "user")
    return {"success": True}


@app.post("/api/support/conversations/{cid}/close", dependencies=[Depends(require_roles("admin"))])
def support_close_api(cid: str):
    from agent.support import close_convo
    if not close_convo(cid):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"success": True}


@app.get("/api/support/diagnostics", dependencies=[Depends(require_roles("admin"))])
def support_diag_api(limit: int = 20):
    """客服 AI 失败诊断：返回最近若干条解析/调用失败的原始信息（模型原始返回、异常原因）"""
    from agent.support import read_diag_log
    return {"entries": read_diag_log(limit=min(max(limit, 1), 100))}


@app.get("/api/support/unread")
def support_unread_api(authorization: str = Header(None),
                       x_client_id: str = Header(None, alias="X-Client-Id")):
    """未读数：管理员 = 所有用户新消息数；普通用户 = 客服回复未读数（供红点/角标）"""
    from agent.support import unread_total
    uname, roles = _support_identity(authorization, x_client_id or "")
    if "admin" in roles:
        return {"unread": unread_total("admin"), "is_admin": True}
    return {"unread": unread_total("user", uname), "is_admin": False}


# ── 引导问题（根据当前数据库重点动态生成）──────────────

@app.get("/api/suggest/questions")
def suggest_questions_api(limit: int = 6, seed: int | None = None):
    """根据当前数据库的重点动态生成引导问题（替换前端固定快捷提问）。

    seed 可选：前端每次传随机值 → 加权随机轮换，推荐问题不再一成不变。
    """
    from agent.suggest import generate_guide_questions
    return generate_guide_questions(limit=min(max(limit, 2), 8), seed=seed)


# ── 全库分析报告（流式 / 导出）──────────────────────────

def _build_report_context(allowed_tables: set[str] | None = None,
                          row_filters: dict[str, str] | None = None) -> tuple[str, str, dict, list]:
    """构建报告上下文：表清单 + 行数 + 数据摘要 + 行业推断。

    权限过滤（安全关键）：allowed_tables 非 None 时只统计有权限的表；
    row_filters 提供时数据摘要按行级条件过滤。返回 (context, industry, counts, real_tables)。
    """
    from db.executor import execute_sql, get_table_row_counts
    try:
        counts = get_table_row_counts()
    except Exception:
        counts = {}
    real_tables = []
    try:
        from db.tools import get_real_tables
        all_real = [t["table_name"] for t in get_real_tables()]
        if allowed_tables is not None:
            allowed_lower = {t.lower() for t in allowed_tables}
            real_tables = [t for t in all_real
                           if t.split(".")[-1].lower() in allowed_lower]
        else:
            real_tables = all_real
    except Exception:
        pass

    from db.metadata import find_table_by_name
    table_info_lines = []
    for name in real_tables:
        detail = find_table_by_name(name)
        alias = detail["table_alias"] if detail else name
        rows = counts.get(name.split(".")[-1], counts.get(name, "?"))
        if row_filters:
            _rf = row_filters.get(name) or row_filters.get(name.split(".")[-1])
            if _rf:
                try:
                    from agent.sql_validator import validate_sql_safety
                    valid, _err, _clean = validate_sql_safety(f"SELECT 1 WHERE {_rf}")
                    if not valid:
                        raise ValueError("invalid row filter")
                    from database import quote_ident
                    if "." in name:
                        s, t = name.split(".", 1)
                        qname = f"{quote_ident(s)}.{quote_ident(t)}"
                    else:
                        qname = quote_ident(name)
                    count_result = execute_sql(f"SELECT COUNT(*) AS row_count FROM {qname} WHERE {_rf}")
                    if count_result["success"] and count_result["rows"]:
                        rows = count_result["rows"][0].get("row_count", "?")
                    else:
                        rows = "?"
                except Exception:
                    rows = "?"
        table_info_lines.append(f"- {alias}({name}): {rows} 行")
    table_info = "\n".join(table_info_lines)

    data_brief = []
    for name in real_tables[:6]:
        try:
            from database import quote_ident
            if "." in name:
                s, t = name.split(".", 1)
                qname = f"{quote_ident(s)}.{quote_ident(t)}"
            else:
                qname = quote_ident(name)
            row_cond = ""
            if row_filters:
                _rf = row_filters.get(name) or row_filters.get(name.split(".")[-1])
                if _rf:
                    from agent.sql_validator import validate_sql_safety
                    valid, _err, _clean = validate_sql_safety(f"SELECT 1 WHERE {_rf}")
                    if valid:
                        row_cond = f" WHERE {_rf}"
            r = execute_sql(f"SELECT * FROM {qname}{row_cond} LIMIT 3")
            if r["success"] and r["rows"]:
                detail = find_table_by_name(name)
                alias = detail["table_alias"] if detail else name
                data_brief.append(f"【{alias}】\n" + "\n".join(str(row) for row in r["rows"]))
        except Exception:
            pass

    context = f"""数据库包含以下表:
{table_info}

关键数据摘要:
{chr(10).join(data_brief) if data_brief else '（数据库暂不可用）'}"""

    # 行业参数化：根据当前库表名推断行业（有制造业表则注明），供报告术语参考
    industry = ""
    try:
        from db.tools import get_real_tables
        rt = [t["table_name"].lower() for t in get_real_tables()]
        if any(k in " ".join(rt) for k in ("production", "work_order", "equipment", "quality", "mes", "qms", "factory")):
            industry = "制造业（生产/质量/设备/库存类数据）"
    except Exception:
        pass
    return context, industry, counts, real_tables


@app.get("/api/overview/charts")
def overview_charts(authorization: str = Header(None)):
    """总览页「数据洞察图表」：自动探查当前库，生成可解释的数据分析图表。

    每个图表带白话解释，供非专业人员看懂「数据在讲什么」。
    全部确定性计算（不调用 LLM）；SQL 走只读校验，**表清单按当前用户 ACL 过滤**。

    权限（2026-09-24 修）：原实现不注入 ACL 上下文 ⇒ `generate_overview_charts`
    内部的 `get_acl()` 恒为 None，表过滤失效、全库表都会被扫（文档曾声称"行/列级
    ACL 改写"，与实现不符）。现补上 set_acl，与 `get_attention_points` 同口径。
    """
    from security.enforcer import build_acl_context
    from security.context import set_acl, clear_acl
    acl = build_acl_context(get_current_user(authorization))
    set_acl(acl)
    try:
        from agent.data_charts import generate_overview_charts
        return generate_overview_charts()
    except Exception as e:
        return {"success": False, "charts": [], "error": str(e)[:160]}
    finally:
        clear_acl()   # 防 ContextVar 泄漏到同线程的下一个请求（uvicorn 线程池复用）


@app.post("/api/overview/report")
async def overview_report(authorization: str = Header(None), _req: AnalysisRequest = None):
    """AI 快速生成数据总览报告 — 流式输出（内容按当前用户数据权限过滤）"""
    from security.enforcer import build_acl_context
    u = get_current_user(authorization)
    acl = build_acl_context(u)
    from agent.report_agent import generate_report_stream

    async def event_stream():
        try:
            # 首 token 前先推占位（P2 优化）：LLM 首 token 延迟可达 20-30s，
            # 让前端立即有「正在生成」反馈而非长时间空白；
            # context 构建（表行数统计 + 数据摘要）也放进流内，避免请求挂起无响应
            yield f"data: {json_mod.dumps({'t': '正在汇总数据并生成分析报告…'})}\n\n"
            context, industry, _counts, _tables = await asyncio.to_thread(
                _build_report_context,
                allowed_tables=acl.allowed_tables, row_filters=acl.row_filters or None)
            async for token in generate_report_stream(context, industry=industry):
                payload = json_mod.dumps({"t": token})
                yield f"data: {payload}\n\n"
            done_payload = json_mod.dumps({"done": True})
            yield f"data: {done_payload}\n\n"
        except Exception as e:
            err_payload = json_mod.dumps({"error": "报告生成失败，请稍后重试"})
            yield f"data: {err_payload}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


def _require_action(authorization: str, action: str) -> dict:
    """操作级权限检查（export/share/download）：无权则 403（fail-close）。

    对标竞品「操作权限」维度——普通员工默认只能「看」，不能「导出/下载/分享」，
    需角色策略里显式开通对应操作权限。
    """
    from security.enforcer import build_acl_context
    u = get_current_user(authorization)
    ctx = build_acl_context(u)
    if not ctx.can_do(action):
        audit("access_denied", username=u["username"], roles=u.get("roles", []),
              tenant_id=ctx.tenant_id, reason=f"无权操作：{action}")
        raise HTTPException(status_code=403,
                            detail=f"权限不足：当前角色未开通「{action}」操作权限，请联系管理员")
    return u


@app.post("/api/overview/report-export")
async def overview_report_export(authorization: str = Header(None)):
    """一次性返回数据总览报告全文（Markdown + 表行数），供前端导出文件。

    权限：需角色策略开通 export 操作权限（admin 全放行，其余 fail-close）。
    说明：前端「生成报告」按钮已按 canDo('export') 显隐，但仅前端隐藏不足以保护数据，
          此处补后端校验，避免无权用户绕过界面直接调用接口拿到报告全文。

    内容按当前用户数据权限过滤：仅含允许访问的表 + 行级条件内的数据摘要。
    """
    from security.enforcer import build_acl_context
    u = _require_action(authorization, "export")
    acl = build_acl_context(u)
    from agent.report_agent import generate_report_stream
    context, industry, counts, real_tables = _build_report_context(
        allowed_tables=acl.allowed_tables, row_filters=acl.row_filters or None)

    full: list[str] = []
    async for token in generate_report_stream(context, industry=industry):
        full.append(token)

    return {
        "success": True,
        "markdown": "".join(full),
        "tables": {t: counts.get(t.split(".")[-1], counts.get(t, 0)) for t in real_tables},
        "table_count": len(real_tables),
    }


class ReportAssetRequest(BaseModel):
    """报告资产保存请求（P1-4：报告沉淀复用）"""
    title: str = ""
    markdown: str = ""
    tables: list[str] = []


@app.get("/api/reports")
def list_report_assets_api(authorization: str = Header(None)):
    """报告资产列表（P1-4）：管理员 可见全部，普通角色仅自己创建的（按角色可见性过滤）"""
    from agent.report_assets import list_reports
    u = get_current_user(authorization)
    return {"success": True, "reports": list_reports(u.get("role", ""), u.get("username", ""))}


@app.post("/api/reports")
def save_report_asset_api(req: ReportAssetRequest, authorization: str = Header(None)):
    """保存当前生成报告为可复用资产（管理员）"""
    from agent.report_assets import save_report
    from auth import get_user_roles
    u = get_current_user(authorization)
    if not (set(get_user_roles(u)) & {"admin"}):
        raise HTTPException(status_code=403, detail="仅管理员/分析师可将报告保存为资产")
    if not req.markdown.strip():
        raise HTTPException(status_code=400, detail="报告内容为空，无法保存")
    asset = save_report(req.title, req.markdown, req.tables,
                        u.get("username", ""), ",".join(get_user_roles(u)) or u.get("role", ""))
    audit("report_asset_saved", user=u["username"], title=(req.title or "")[:60])
    return {"success": True, "asset": asset}


@app.get("/api/reports/{rid}")
def get_report_asset_api(rid: str, authorization: str = Header(None)):
    """报告资产详情（含正文）；普通角色仅能查看自己创建的报告"""
    from agent.report_assets import get_report
    from auth import get_user_roles
    u = get_current_user(authorization)
    asset = get_report(rid)
    if not asset:
        raise HTTPException(status_code=404, detail="报告不存在")
    if not (set(get_user_roles(u)) & {"admin"}) and asset.get("created_by") != u.get("username"):
        raise HTTPException(status_code=403, detail="无权查看该报告")
    return {"success": True, "report": asset}


@app.post("/api/reports/{rid}/regenerate")
async def regenerate_report_asset_api(rid: str, authorization: str = Header(None)):
    """一键再生成：重新构建数据上下文 → 重跑报告生成 → 覆盖保存（按当前用户权限过滤）"""
    from agent.report_assets import get_report, update_report
    from agent.report_agent import generate_report_stream
    from security.enforcer import build_acl_context
    from auth import get_user_roles
    u = get_current_user(authorization)
    asset = get_report(rid)
    if not asset:
        raise HTTPException(status_code=404, detail="报告不存在")
    if not (set(get_user_roles(u)) & {"admin"}):
        raise HTTPException(status_code=403, detail="仅管理员/分析师可重新生成报告")
    acl = build_acl_context(u)
    context, industry, _counts, _tables = await asyncio.to_thread(
        _build_report_context,
        allowed_tables=acl.allowed_tables, row_filters=acl.row_filters or None)
    full: list[str] = []
    async for token in generate_report_stream(context, industry=industry):
        full.append(token)
    markdown = "".join(full)
    updated = update_report(rid, {"markdown": markdown, "tables": _tables})
    audit("report_asset_regenerated", user=u["username"], title=(asset.get("title") or "")[:60])
    return {"success": True, "report": updated}


@app.delete("/api/reports/{rid}")
def delete_report_asset_api(rid: str, authorization: str = Header(None)):
    """删除报告资产（管理员）"""
    from agent.report_assets import delete_report
    from auth import get_user_roles
    u = get_current_user(authorization)
    if not (set(get_user_roles(u)) & {"admin"}):
        raise HTTPException(status_code=403, detail="仅管理员/分析师可删除报告")
    if not delete_report(rid):
        raise HTTPException(status_code=404, detail="报告不存在")
    audit("report_asset_deleted", user=u["username"])
    return {"success": True}


@app.post("/api/overview/report-html")
def overview_report_html(authorization: str = Header(None)):
    """生成单文件 HTML 数据总览报告（标签页 + 流程图 + 颜色高亮），直接返回 HTML 内容。

    权限：需角色策略开通 export 操作权限（admin 全放行，其余 fail-close）。
          与同族的 /overview/report-export、/overview/report-download 必须同口径——
          这条端点此前只校验了登录（get_current_user），漏掉 export 校验，
          未被授予导出权限的角色、以及开放模式下的 guest，都能直接 POST 拿到整份总览报告。
    内容按当前用户数据权限过滤（表级 allowed_tables + 行级 row_filters）；
    缓存 key 含 ACL 指纹，不同权限用户不共享缓存（防越权复用）。
    前端拿到后用 Blob URL 在新标签页打开，无需静态服务。
    走 10 分钟 TTL 缓存：LLM 生成总结约 30-80s，重复生成秒回；切库自动失效。
    """
    from security.enforcer import build_acl_context, acl_fingerprint
    u = _require_action(authorization, "export")
    acl = build_acl_context(u)
    fp = acl_fingerprint(acl)
    from database import get_db

    try:
        db = next(get_db())
        try:
            from routers.knowledge import _cached
            from agent.html_report import build_html_report
            html = _cached(f"report_html:{fp}", build_html_report, db,
                           acl.allowed_tables, acl.row_filters or None)
        finally:
            try:
                db.close()
            except Exception:
                pass
        return Response(content=html, media_type="text/html; charset=utf-8")
    except Exception as e:
        return Response(
            content=f"<html><body style='font-family:sans-serif;padding:40px'><h2>报告生成失败</h2>"
                    f"<p style='color:#b91c1c'>{str(e)}</p></body></html>",
            media_type="text/html; charset=utf-8",
            status_code=500,
        )


@app.post("/api/overview/report-download")
def overview_report_download(authorization: str = Header(None), format: str = "docx"):
    """数据总览报告导出下载 — 支持 Word(docx) / PDF。

    权限：需角色策略开通 export 操作权限（admin 全放行，其余 fail-close），
          与 report-export 同源同口径——否则无权用户拿到 Markdown 后仍可本地转文件。

    内容按当前用户数据权限过滤（表级 allowed_tables + 行级 row_filters）；
    缓存 key 含 ACL 指纹，不同权限用户不共享缓存。复用 report-html 的 TTL 缓存 HTML。
    """
    fmt = (format or "docx").lower()
    if fmt not in ("docx", "pdf"):
        raise HTTPException(status_code=400, detail="format 仅支持 docx / pdf")
    from security.enforcer import build_acl_context, acl_fingerprint
    u = _require_action(authorization, "export")
    acl = build_acl_context(u)
    fp = acl_fingerprint(acl)
    from database import get_db
    try:
        db = next(get_db())
        try:
            from routers.knowledge import _cached
            from agent.html_report import build_html_report
            from agent.report_export import parse_report_blocks, blocks_to_docx, blocks_to_pdf
            html = _cached(f"report_html:{fp}", build_html_report, db,
                           acl.allowed_tables, acl.row_filters or None)
            blocks = parse_report_blocks(html)
            data = blocks_to_docx(blocks) if fmt == "docx" else blocks_to_pdf(blocks)
        finally:
            try:
                db.close()
            except Exception:
                pass
    except ImportError:
        # 服务器缺少 docx/reportlab 依赖时的可操作提示
        raise HTTPException(
            status_code=500,
            detail="服务器缺少文档生成依赖（python-docx / reportlab），"
                   "请在服务器后端目录执行：pip install python-docx reportlab 后重启服务。")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"报告导出失败：{e}")

    media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
             if fmt == "docx" else "application/pdf")
    from datetime import datetime
    fname = f"数据总览报告_{datetime.now().strftime('%Y%m%d_%H%M')}.{fmt}"
    from urllib.parse import quote
    return Response(
        content=data,
        media_type=media,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}",
            "Content-Length": str(len(data)),
        },
    )


# ═══════════════════════════════════════════════════════════════
# 智能问析：针对「用户提问」生成分析报告
#
# 与上面那套 /api/overview/report* 的区别——那套是**整库总览**报告，
# 不看用户问了什么，把库里所有表列一遍；这套是**跟着问题走**的：
# 用用户那句问题去取数，出指标卡 + 图示 + 结构化分析章节。
#
# 权限口径与总览报告一致：表级 allowed_tables + 行级 row_filters，
# 导出/下载走 _require_action("export")（admin 全放行，其余 fail-close）。
# 报告是"把数据带出去"的动作，所以生成与导出都要求 export 权限。
# ═══════════════════════════════════════════════════════════════

def _qreport_acl(authorization: str):
    """取当前用户的 ACL 上下文（表级 + 行级），并强制校验 export 操作权限。

    这个函数只服务于 /api/ask/report（生成提问分析报告）一个端点，所以把 export 校验
    一并放在这里：报告是「把数据带出去」的动作，生成与导出必须同口径。
    此前这里只调 get_current_user 取身份，没做操作权限校验——上方注释明写
    「生成与导出都要求 export 权限」，实现却对不上，未开通导出权限的账号能直接拿到含数据的报告 HTML。
    """
    from security.enforcer import build_acl_context
    u = _require_action(authorization, "export")
    return u, build_acl_context(u)


@app.post("/api/ask/report")
async def ask_generate_report(req: QuestionReportRequest, authorization: str = Header(None)):
    """针对用户提问生成分析报告（一次性返回完整 HTML + 元信息）。

    返回 HTML 字符串而不是文件流：前端要把它塞进预览 iframe 里当场看，
    也要能一键新标签打开、一键下载。给 HTML 源串，三条路都走得通。

    耗时说明：取数（约 1-3s）+ LLM 写分析（约 20-60s），
    所以前端要显示明确的"生成中"状态，不能当作瞬时接口用。
    """
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    u, acl = _qreport_acl(authorization)
    from agent import question_report as QR

    data = await asyncio.to_thread(
        QR.collect_question_data, question, req.sql or "",
        acl.allowed_tables, acl.row_filters or None)

    if not data.get("ok"):
        # 取数失败也要给用户一个能看懂的出口，而不是 500
        err = str(data.get("error") or "未知原因")
        audit("qreport_failed", username=u.get("username", ""),
              question=question[:60], reason=err[:120])
        raise HTTPException(
            status_code=422,
            detail=f"没能为这个问题取到数据：{err[:160]}。"
                   f"可以换个问法，或者先用问析把数据查出来再生成报告。")

    # analyze_stream 是异步生成器，直接 async for 收；不要 await（await 一个生成器
    # 会报 "object async_generator can't be used in 'await' expression"）。
    buf: list[str] = []
    async for t in QR.analyze_stream(
            question, data["columns"], data["rows"],
            data.get("table", ""), data.get("sql", "")):
        buf.append(t)
    sections = QR.split_sections("".join(buf))

    try:
        db_name = ""
        from database import get_database_config
        cfg = get_database_config() or {}
        db_name = cfg.get("database") or cfg.get("db") or ""
    except Exception:
        db_name = ""

    html = QR.build_report_html(question, data, sections,
                               user=u.get("username", ""), db_name=db_name)
    title = (req.title or question).strip()[:80]
    audit("qreport_generated", username=u.get("username", ""),
          question=question[:60], table=data.get("table", ""), rows=data.get("row_count", 0))

    return {
        "success": True,
        "title": title,
        "html": html,
        "rows": data.get("row_count", 0),
        "columns": data.get("columns", []),
        # 与 columns 一一对应的中文业务名，给前端/第三方调用方直接用。
        # 渲染层自己有兜底翻译，这里回传是为了让调用方不必再实现一遍同样的映射。
        "display_columns": data.get("display_columns", []),
        "sql": data.get("sql", ""),
        "table": data.get("table", ""),
        "sections": [n for n, _ in sections],
    }


@app.post("/api/ask/report/download")
def ask_report_download(req: QuestionReportRequest, authorization: str = Header(None),
                        format: str = "docx"):
    """把提问报告导出成 Word / PDF。

    权限与总览报告同源：需角色策略开通 export 操作权限。
    导出内容复用同一套取数 + 分析链路，再走 report_export 的块渲染，
    保证「屏幕上看到的」和「下载到的」结论一致（只是图示降级成文字说明）。
    """
    fmt = (format or "docx").lower()
    if fmt not in ("docx", "pdf"):
        raise HTTPException(status_code=400, detail="format 仅支持 docx / pdf")
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    u = _require_action(authorization, "export")
    from security.enforcer import build_acl_context
    acl = build_acl_context(u)
    from agent import question_report as QR

    data = QR.collect_question_data(question, req.sql or "",
                                   acl.allowed_tables, acl.row_filters or None)
    if not data.get("ok"):
        raise HTTPException(
            status_code=422,
            detail=f"没能为这个问题取到数据：{str(data.get('error') or '未知原因')[:160]}")

    sections = QR.split_sections(
        QR.analyze_sync(question, data["columns"], data["rows"],
                        data.get("table", ""), data.get("sql", "")))

    try:
        db_name = ""
        from database import get_database_config
        cfg = get_database_config() or {}
        db_name = cfg.get("database") or cfg.get("db") or ""
    except Exception:
        db_name = ""

    blocks = QR.build_report_blocks(question, data, sections,
                                   user=u.get("username", ""), db_name=db_name)
    try:
        from agent.report_export import blocks_to_docx, blocks_to_pdf
        data_bytes = blocks_to_docx(blocks) if fmt == "docx" else blocks_to_pdf(blocks)
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="服务器缺少文档生成依赖（python-docx / reportlab），"
                   "请在服务器后端目录执行：pip install python-docx reportlab 后重启服务。")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"报告导出失败：{e}")

    from datetime import datetime as _dt
    from urllib.parse import quote
    safe = re.sub(r"[\\/:*?\"<>|]", "", (req.title or question))[:28]
    fname = f"{safe}_{_dt.now().strftime('%Y%m%d_%H%M')}.{fmt}"
    media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
             if fmt == "docx" else "application/pdf")
    audit("qreport_exported", username=u.get("username", ""),
          question=question[:60], format=fmt)
    return Response(
        content=data_bytes,
        media_type=media,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}",
            "Content-Length": str(len(data_bytes)),
        },
    )


@app.get("/api/ask/report/file/{rid}")
def ask_report_file_download(rid: str, format: str = "docx",
                             authorization: str = Header(None)):
    """按 rid 导出「提问即报告」链路生成的报告。

    与 ask_report_download 的区别：那条链路是按 question+sql 重新取数导出，
    这条是把**预览的那份 HTML 原样**转成 Word/PDF——预览看到什么，下载到的就是
    什么，两边内容严格一致（报告是给评委/领导看的，两个版本数字对不上是事故）。
    权限同源：需角色策略开通 export 操作权限。
    """
    fmt = (format or "docx").lower()
    if fmt not in ("docx", "pdf", "html"):
        raise HTTPException(status_code=400, detail="format 仅支持 docx / pdf / html")
    from agent import periodic_report as PR

    if fmt == "html":
        # 预览恢复：前端会话从本地存储回来时只留着 rid（一份报告 HTML 几十 KB，
        # 整份塞 localStorage 会挤爆配额、连带丢历史），按 rid 把预览的那份取回去重开。
        # 鉴权只要求登录、不查 export 权限——普通员工能看报告但未必能导出，
        # 不能把「再看一眼」也拦掉；但 rid 得是他自己那份，不能拿别人的 rid 看别人的数。
        u = get_current_user(authorization)
        item = PR.load_report_html(rid)
        if not item:
            raise HTTPException(
                status_code=404,
                detail="报告缓存已过期（服务重启或超过 2 小时），请重新提问生成。")
        owner = item.get("username") or ""
        if owner and owner != u.get("username") and u.get("role") != "admin":
            raise HTTPException(status_code=403, detail="这份报告不属于当前账号")
        return Response(content=item["html"], media_type="text/html; charset=utf-8")

    u = _require_action(authorization, "export")
    item = PR.load_report_html(rid)
    if not item:
        raise HTTPException(
            status_code=404,
            detail="报告缓存已过期或不存在，请重新提问生成报告后再导出。")
    try:
        from agent.report_export import parse_report_blocks, blocks_to_docx, blocks_to_pdf
        blocks = parse_report_blocks(item["html"])
        data_bytes = blocks_to_docx(blocks) if fmt == "docx" else blocks_to_pdf(blocks)
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="服务器缺少文档生成依赖（python-docx / reportlab），"
                   "请在服务器后端目录执行：pip install python-docx reportlab 后重启服务。")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"报告导出失败：{e}")

    from datetime import datetime as _dt
    from urllib.parse import quote
    safe = re.sub(r"[\\/:*?\"<>|]", "", item.get("title") or "")[:28] or "分析报告"
    fname = f"{safe}_{_dt.now().strftime('%Y%m%d_%H%M')}.{fmt}"
    media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
             if fmt == "docx" else "application/pdf")
    audit("qreport_exported", username=u.get("username", ""), rid=rid, format=fmt)
    return Response(
        content=data_bytes,
        media_type=media,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}",
            "Content-Length": str(len(data_bytes)),
        },
    )


@app.get("/api/health")
def health():
    """系统健康检查（P2 工程成熟度）：数据库连接 / LLM 配置 / 运行时观测概要。

    供部署后监控（nginx/健康探针/告警接入）使用：status=ok 全部正常，
    degraded=数据库不可用（LLM 未配置只标记 llm.ok=false 不降级，可离线演示）。
    """
    from database import get_database_config
    import time as _time

    checks: dict = {}
    # 1. 数据库连接（真实 SELECT 1，走只读执行链路）
    db_ok = False
    try:
        from db.executor import execute_sql
        r = execute_sql("SELECT 1 AS ok")
        db_ok = bool(r.get("success")) and bool(r.get("rows"))
    except Exception:
        db_ok = False
    try:
        _cfg = get_database_config()
    except Exception:
        _cfg = {}
    checks["database"] = {"ok": db_ok, "config": _cfg}

    # 2. LLM 配置（有 api_key 即可用；未配置不影响服务启动，只影响 LLM 路径）
    llm_ok = False
    try:
        from config import LLM_CONFIG
        _llm = LLM_CONFIG.get("_default") if isinstance(LLM_CONFIG, dict) else None
        _cur = LLM_CONFIG.get(_llm) if _llm and isinstance(LLM_CONFIG.get(_llm), dict) else LLM_CONFIG
        llm_ok = bool((_cur or {}).get("api_key"))
    except Exception:
        llm_ok = False
    checks["llm"] = {"ok": llm_ok}

    # 3. 运行时观测概要（编译命中率 / 成功率 / P95 —— 部署后跟踪准确率与性能）
    try:
        from agent.observability import get_overview
        ov = get_overview()
        checks["observability"] = {
            "ok": True,
            "total": ov.get("total", 0),
            "compile_rate": ov.get("compile_rate"),
            "ok_rate": ov.get("ok_rate"),
            "p95_ms": ov.get("p95_ms"),
        }
    except Exception:
        checks["observability"] = {"ok": False}

    overall = "ok" if db_ok else "degraded"
    return {
        "status": overall,
        "checks": checks,
        "routes": len([r for r in app.routes if hasattr(r, "path")]),
        "time": _time.strftime("%Y-%m-%d %H:%M:%S"),
    }


class FeedbackReportRequest(BaseModel):
    query: str = ""
    sql: str = ""
    executed_sql: str = ""
    feedback_type: str = "其他"
    note: str = ""


class FeedbackResolveRequest(BaseModel):
    resolution: str = "resolved"


@app.post("/api/feedback/report")
def submit_feedback_api(req: FeedbackReportRequest, authorization: str = Header(None)):
    """提交分析结果纠错反馈（口径不对/结果不对等）→ 进入复核队列。任何登录用户可提交。"""
    from feedback import submit_feedback
    u = get_current_user(authorization)
    try:
        item = submit_feedback(u, req.query, req.sql, req.feedback_type,
                               req.note, req.executed_sql)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        audit("feedback_submit", username=u["username"], role=u["role"],
              query=req.query[:200], feedback_type=req.feedback_type)
    except Exception:
        pass
    return {"success": True, "message": "已提交反馈，感谢你的纠错（管理员会复核并修正口径）", "id": item["id"]}


@app.get("/api/feedback/queue", dependencies=[Depends(require_roles("admin"))])
def list_feedback_api(status: str = "", limit: int = 100):
    """复核队列：查看用户纠错反馈（可按 pending/resolved/rejected 过滤）。"""
    from feedback import list_feedback
    items = list_feedback(status or None, limit=limit)
    return {"success": True, "total": len(items), "items": items}


@app.put("/api/feedback/queue/{fid}", dependencies=[Depends(require_roles("admin"))])
def resolve_feedback_api(fid: str, req: FeedbackResolveRequest, authorization: str = Header(None)):
    """处理反馈：resolved=已修正口径 / rejected=驳回。"""
    from feedback import resolve_feedback
    u = get_current_user(authorization)
    try:
        item = resolve_feedback(fid, req.resolution, u)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "item": item}


@app.on_event("startup")
def _warmup_knowledge_cache():
    """启动后后台线程预热业务知识场景缓存（_build_scenes 首次构建含字段翻译较慢，
    预热后总览页/知识页首次打开即快；失败静默，不影响启动）"""
    import threading

    def _warm():
        try:
            import time as _t
            _t.sleep(3)  # 等数据库连接池就绪
            from routers import knowledge as _K
            from routers.knowledge import _build_scenes
            from database import get_db, get_database_config
            _db = next(get_db())
            # B4 修复：记录预热起点库指纹，写入前若库已切换则丢弃预热结果（防旧库数据写回新缓存）
            def _db_fp():
                try:
                    _c = get_database_config()
                    return f"{_c.get('host', '')}:{_c.get('port', 0)}:{_c.get('name', '')}"
                except Exception:
                    return "default"
            _warm_fp = _db_fp()
            try:
                def _warm_key(key, producer):
                    """安全预热：构建结果非空才写缓存（预热期连接未就绪可能构建出空数据，
                    空缓存会坑 10 分钟；失败/为空则跳过，让真实请求自行构建）"""
                    try:
                        if _db_fp() != _warm_fp:
                            print(f"[预热] 库已切换({_warm_fp}→{_db_fp()}), 丢弃 {key} 预热结果")
                            return
                        _db.rollback()  # 清理可能的事务终止状态（25P02）
                        _t0 = _t.time()
                        val = producer(_db)
                        if val:
                            # 缓存条目为 3 元组 (expire_ts, value, heat_version)，须与 _cached 读取格式一致，
                            # 否则 _cached 里 entry[2] 越界抛 IndexError → 接口 500；加锁防与 clear 竞态
                            with _K._cache_lock:
                                _K._cache[key] = (_t.time() + _K._CACHE_TTL, val, _K._heat_version)
                            print(f"[预热] {key} 已填充 ({_t.time()-_t0:.1f}s)")
                        else:
                            print(f"[预热] {key} 结果为空, 跳过缓存")
                    except Exception as _e3:
                        print(f"[预热] {key} 失败: {_e3}")

                _warm_key("scenes", _build_scenes)
                # 表列表 / 快捷提问示例 / 总览统计 / 表间关系 / 术语词典 一并预热
                from routers.tables import (
                    _build_tables_list, _build_analysis_examples,
                    _build_overview, _build_relationships,
                )
                from routers.knowledge import _build_terms
                _warm_key("tables_list", _build_tables_list)
                _warm_key("analysis_examples", _build_analysis_examples)
                _warm_key("overview", _build_overview)
                _warm_key("relationships", _build_relationships)
                _warm_key("terms", _build_terms)
                # ── 问数链路冷启动预热（2026-09-29）：消除「第一个问数请求」背的
                # information_schema 全库查询 / 指标 RAG 向量索引懒重建 / embedding
                # 模型加载 三项开销。这三项都是懒加载：表结构 60s TTL 缓存、
                # metric_memory 首用懒 rebuild、bge 模型首次 get_embedding_fn 才加载。
                # 未预热时，启动后第一条问数（尤其走 LLM 路径）会把这 1~3s 摊在用户
                # 请求上。纯前置加载，不改任何取数/编译/路由逻辑，失败静默不影响启动。
                try:
                    _t0 = _t.time()
                    from agent.llm_service import _all_table_columns
                    _ncols = len(_all_table_columns() or {})
                    from agent.metric_memory import get_metric_memory
                    _r = get_metric_memory().rebuild()
                    from agent.embeddings import get_embedding_fn, get_embedding_mode
                    get_embedding_fn()
                    print(f"[预热] 问数链路就绪：表结构 {_ncols} 表 / 指标向量 {_r.get('rebuilt', 0)} 条 / "
                          f"embedding={get_embedding_mode()}（{_t.time()-_t0:.1f}s）")
                except Exception as _e4:
                    print(f"[预热] 问数链路预热失败（不影响启动）: {_e4}")
                # ── ML 模块预热（2026-10-01）：ml.trainer 已改为用时懒加载
                # （启动提速 2~4s / 省 200MB），但演示场景需要 ML 功能首次点击即响应，
                # 故启动后由本后台线程预先完成 pandas/sklearn/matplotlib 的加载——
                # 启动速度不受影响，演示时不背首次加载的 3~5s。失败静默不影响其他功能。
                try:
                    _t0 = _t.time()
                    from ml.trainer import list_trained_models  # noqa: F401 — import 即完成重库加载
                    list_trained_models()
                    print(f"[预热] ML 模块就绪（{_t.time()-_t0:.1f}s）")
                except Exception as _e5:
                    print(f"[预热] ML 模块预热失败（不影响启动，首次使用时会再尝试）: {_e5}")
            finally:
                try:
                    _db.close()
                except Exception:
                    pass
        except Exception as _e:
            import traceback as _tb
            print(f"[预热] 失败: {_e}")
            _tb.print_exc()

    threading.Thread(target=_warm, daemon=True).start()


@app.on_event("startup")
def _start_metric_monitor():
    """启动指标监测后台线程（对标 Tableau Pulse：指标异动自动检测，非被动问答）。"""
    try:
        from agent.monitor import start_monitor
        start_monitor()
    except Exception as _e:
        print(f"[监测] 启动失败: {_e}")


@app.get("/")
def root():
    return {"message": "ok"}


def _port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    """探测端口是否已被监听。

    用 connect 而不是 bind：Windows 上 SO_REUSEADDR 语义允许「抢占式绑定」，
    bind 探测会失效（明明占了也说没占），connect 才是可靠判断。
    """
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


if __name__ == "__main__":
    # 并发说明：默认单进程（workers=1）。本项目支持运行时动态切换数据库
    # （switch_database 修改进程内全局 engine），多 worker 下切库后各进程连接会
    # 指向不一致（仅当前进程生效）；仅在确认无需动态切库、且需要更高吞吐的部署
    # 场景才通过 WORKERS 环境变量开启多进程（每进程会各自建立数据库连接池）。
    # 单进程内的 LLM 并发上限由 agent/llm_service.py 的 LLM_MAX_CONCURRENCY 控制。
    workers = int(_os.getenv("WORKERS", "1"))

    # 端口占用预检：uvicorn 在 bind 失败时只会打印
    # "ERROR: [Errno 10048] error while attempting to bind on address ('0.0.0.0', 8010)"
    # 且此前 startup 事件已经跑完（日志里先出现 "Application startup complete." 再报错），
    # 很容易被误读成「后端崩了/起不来」——实际上绝大多数情况是上一个实例还在运行。
    # 这里提前给出可操作的中文提示，避免重复启动时的误判。
    if _port_in_use("127.0.0.1", 8010):
        print(
            "[启动失败] 端口 8010 已被占用：后端多半已经在运行了，无需重复启动。\n"
            "  1) 确认是否已在服务：  浏览器打开 http://127.0.0.1:8010/  （正常返回 {\"message\":\"ok\"}）\n"
            "  2) 查看占用进程 PID：  Get-NetTCPConnection -LocalPort 8010 -State Listen | Select-Object OwningProcess\n"
            "  3) 需要重启时先停旧实例：Stop-Process -Id <上面的PID>\n"
            "  4) 再启动：           cd backend; .\\.venv\\Scripts\\python.exe main.py"
        )
        raise SystemExit(1)

    uvicorn.run(app, host="0.0.0.0", port=8010, workers=workers)
