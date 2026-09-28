from typing import Any, Dict

from fastapi import APIRouter, Depends

from auth import require_roles
from pydantic import BaseModel

from database import (
    delete_database_workspace,
    get_database_config,
    get_database_profiles,
    save_db_config_to_env,
    switch_database_workspace,
    test_database_connection,
)
# 切库/改连接后触发"预渲染"：让渲染规划 Agent 提前分析库结构并缓存方案，
# 用户点开 ER 图时直接命中缓存秒出，不用现场等 LLM。
from routers.visualize import prewarm_plan

router = APIRouter(prefix="/api/config", tags=["配置"])


class DBConfigRequest(BaseModel):
    db_type: str = "postgresql"
    host: str = "localhost"
    port: int = 5432
    name: str = "postgres"
    user: str = "postgres"
    password: str = "root"
    test_connection: bool = True


def _active_db_config(mask: bool = False) -> Dict[str, Any]:
    config = get_database_config()
    # 运行时动态读取密码（导入时绑定的 DB_PASSWORD 在保存配置后仍是旧值）
    import database as _dbmod
    password = getattr(_dbmod, "DB_PASSWORD", "") or ""
    if mask:
        password = "********"  # GET 回显掩码，避免明文密码暴露（与 LLM 密钥掩码策略一致）
    return {
        "db_type": config["db_type"],
        "host": config["host"],
        "port": config["port"],
        "name": config["name"],
        "user": config["user"],
        "password": password,
    }


@router.get("/databases")
def list_databases() -> Dict[str, Any]:
    config = get_database_config()
    return {"active": config["name"], "databases": get_database_profiles()}


@router.post("/databases/{database_name}/activate", dependencies=[Depends(require_roles("admin"))])
def activate_database(database_name: str) -> Dict[str, Any]:
    try:
        # 切换工作区：已存在的库直接切换，不存在的库自动创建
        result = switch_database_workspace(database_name, create_if_missing=True)
        # 持久化为默认库：用户切到哪个，.env 就记哪个——重启后仍默认该库，绝不自动回退
        # （此前只改进程内连接，重启回到 .env 旧值，用户反复遇到"怎么又变回 postgres"）
        try:
            from database import get_database_config, DB_PASSWORD, save_db_config_to_env
            _cfg = get_database_config()
            save_db_config_to_env({
                "db_type": _cfg["db_type"], "host": _cfg["host"], "port": _cfg["port"],
                "name": database_name, "user": _cfg["user"], "password": DB_PASSWORD,
            })
        except Exception:
            pass
        # 清除缓存的活跃连接配置（db/connection_manager 的连接状态/测试用旧库名会串库）
        try:
            from db.connection_manager import clear_active_cache
            clear_active_cache()
        except Exception:
            pass
        # 清空业务知识缓存（数据库结构已变，避免切库后 10 分钟内返回旧库 scenes/terms/graph）
        try:
            from routers.knowledge import clear_knowledge_cache
            clear_knowledge_cache()
        except Exception:
            pass
        # 清空结果缓存与 schema 缓存（防跨库复用错误结果/表结构）
        try:
            from agent.llm_service import clear_cache
            clear_cache()
        except Exception:
            pass
        # 预渲染：切库后立即让渲染 Agent 分析库结构并缓存方案（点开 ER 图秒出）
        try:
            prewarm_plan(result["name"])
        except Exception:
            pass
        return {"success": True, "message": f"已切换到数据库 {result['name']}", "active": result["name"], **result}
    except Exception as exc:
        return {"success": False, "message": f"切换数据库失败: {exc}"}


@router.delete("/databases/{database_name}", dependencies=[Depends(require_roles("admin"))])
def remove_database(database_name: str) -> Dict[str, Any]:
    try:
        result = delete_database_workspace(database_name)
        return {"success": True, "message": f"数据库 {result['name']} 已删除", **result}
    except Exception as exc:
        return {"success": False, "message": f"删除数据库失败: {exc}"}


@router.get("/db")
def get_db_config() -> Dict[str, Any]:
    return _active_db_config(mask=True)


@router.post("/db", dependencies=[Depends(require_roles("admin"))])
def save_db_config(payload: DBConfigRequest) -> Dict[str, Any]:
    try:
        # 密码为空或掩码占位 → 保留当前密码（前端回填掩码后保存不应覆盖真实密码）
        if not payload.password or payload.password == "********":
            import database as _dbmod
            payload.password = getattr(_dbmod, "DB_PASSWORD", "") or ""
        save_db_config_to_env(payload.model_dump())
        # 同步 agent 的连接池
        try:
            from db.executor import reload_pool
            from db.connection_manager import clear_active_cache
            clear_active_cache()
            reload_pool()
        except Exception:
            pass
        # 清空业务知识缓存（数据库连接已变更）
        try:
            from routers.knowledge import clear_knowledge_cache
            clear_knowledge_cache()
        except Exception:
            pass
        # 预渲染：连接配置变更后立即让渲染 Agent 分析新库结构并缓存方案
        try:
            prewarm_plan(payload.name)
        except Exception:
            pass
        if payload.test_connection:
            try:
                test_database_connection(payload.model_dump())
                # 保存配置 = 立即生效：同步切换主引擎（与 activate 行为一致），
                # 避免「.env 已变但运行引擎仍是旧库」的配置漂移（2026-08-18 修复：
                # 曾因只写 .env 不切引擎，导致磁盘变 MySQL、重启后全系统连不上库）。
                try:
                    from database import switch_database
                    switch_database(dict(payload.model_dump()))
                except Exception as exc:
                    return {"success": False,
                            "message": f"配置已保存并连接成功，但切换主引擎失败: {exc}",
                            "config": _active_db_config()}
                return {"success": True, "message": "数据库配置已保存并连接成功", "config": _active_db_config()}
            except Exception as exc:
                return {"success": False, "message": f"配置已保存，但测试连接失败: {exc}", "config": _active_db_config()}
        return {"success": True, "message": "数据库配置已保存", "config": _active_db_config()}
    except Exception as exc:
        return {"success": False, "message": f"保存配置失败: {exc}"}
