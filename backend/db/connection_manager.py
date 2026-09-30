"""数据库连接管理器 — 支持多连接配置, 持久化到 JSON"""

import json
import os
import psycopg2

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".db_connections.json")

# 当前活跃连接
_current_connection = None


def _load_configs() -> list[dict]:
    if not os.path.exists(CONFIG_FILE):
        return []
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_configs(configs: list[dict]):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)


def test_connection(config: dict) -> tuple[bool, str]:
    """测试连接是否可用, 返回 (成功, 版本或错误信息)"""
    try:
        conn = psycopg2.connect(
            dbname=config.get("dbname", "postgres"),
            user=config.get("user", "postgres"),
            password=config.get("password", ""),
            host=config.get("host", "localhost"),
            port=config.get("port", 5432),
            connect_timeout=5,
        )
        ver = conn.server_version
        conn.close()
        return True, f"PostgreSQL {ver // 10000}.{(ver % 10000) // 100}"
    except Exception as e:
        return False, str(e)


def list_connections() -> list[dict]:
    """列出所有保存的连接(不返回密码)"""
    configs = _load_configs()
    for c in configs:
        c.pop("password", None)
    return configs


def save_connection(config: dict) -> dict:
    """新增或更新连接配置"""
    configs = _load_configs()
    # 查找是否已存在相同名称
    name = config.get("name", "默认连接")
    found = False
    for c in configs:
        if c.get("name") == name:
            c.update(config)
            found = True
            break
    if not found:
        config["name"] = name
        configs.append(config)
    _save_configs(configs)
    return {"saved": True, "name": name}


def delete_connection(name: str) -> dict:
    configs = _load_configs()
    configs = [c for c in configs if c.get("name") != name]
    _save_configs(configs)
    return {"deleted": True}


def get_connection_config(name: str = None) -> dict | None:
    """获取指定连接配置(含密码),用于实际连接

    修复：指定 name 时严格匹配，未命中返回 None（switch_connection 会报"连接不存在"），
    不再静默回退第一个配置——传错连接名切到默认库是隐蔽的错库/越权风险。
    """
    configs = _load_configs()
    if name:
        for c in configs:
            if c.get("name") == name:
                return {k: c.get(k) for k in ["dbname", "user", "password", "host", "port"]}
        return None
    # 未指定 name 才返回第一个
    if configs:
        c = configs[0]
        return {k: c.get(k) for k in ["dbname", "user", "password", "host", "port"]}
    return None


def switch_connection(name: str) -> dict:
    """切换活跃连接"""
    global _current_connection
    config = get_connection_config(name)
    if not config:
        return {"success": False, "error": f"连接 '{name}' 不存在"}
    ok, msg = test_connection(config)
    if ok:
        _current_connection = config
        # 切换后重建连接池
        try:
            from db.executor import reload_pool
            reload_pool()
        except Exception:
            pass
        return {"success": True, "name": name, "version": msg}
    return {"success": False, "error": msg}


def get_active_config() -> dict:
    """获取当前活跃的数据库配置"""
    global _current_connection
    if _current_connection:
        return _current_connection
    # 优先使用当前项目 database.py 的活动连接配置（支持动态切换数据库）
    try:
        from database import get_database_config
        from config import DB_CONFIG
        cfg = get_database_config()
        merged = {
            "dbname": cfg.get("name") or cfg.get("database", DB_CONFIG.get("dbname")),
            "user": cfg.get("user", DB_CONFIG.get("user")),
            "host": cfg.get("host", DB_CONFIG.get("host")),
            "port": cfg.get("port", DB_CONFIG.get("port")),
            "password": DB_CONFIG.get("password"),
        }
        _current_connection = merged
        return merged
    except Exception:
        pass
    # 从配置文件加载
    saved = get_connection_config()
    if saved:
        _current_connection = saved
        return saved
    # 回退到 config.py 中的 DB_CONFIG
    from config import DB_CONFIG
    return DB_CONFIG


def clear_active_cache():
    """清除缓存的活动连接配置（切换数据库后调用）"""
    global _current_connection
    _current_connection = None


def get_connection_status() -> dict:
    config = get_active_config()
    ok, msg = test_connection(config)
    return {
        "connected": ok,
        "message": msg,
        "host": config.get("host", "?"),
        "port": config.get("port", "?"),
        "dbname": config.get("dbname", "?"),
        "user": config.get("user", "?"),
    }


# 本地库自动发现属于「拿一组常见口令去试连 localhost」的撞库式操作，
# 是显式开启的本机管理工具，默认关闭：只有 ALLOW_LOCAL_DB_DISCOVERY=1 时才真正探测。
ALLOW_LOCAL_DB_DISCOVERY = (os.getenv("ALLOW_LOCAL_DB_DISCOVERY") or "").strip().lower() in ("1", "true", "yes")

# 候选凭据：只保留「本地 trust 认证的空口令」与「用户名同口令」两种常见开发配置。
# 原先还带 123456 / admin 这类通用弱口令——一旦被端口扫描类接口调用，
# 等于对内网做口令爆破；命中后还会把明文口令原样回传给调用方。
_LOCAL_DISCOVERY_CREDS = [("postgres", ""), ("postgres", "postgres")]


def discover_local_databases() -> list[dict]:
    """自动发现本地 PostgreSQL 数据库（扫描端口 + 少量候选凭据）。

    默认关闭，见 ALLOW_LOCAL_DB_DISCOVERY。
    返回结果**不含口令**：调用方拿到的是「哪个端口上跑着哪些库」，
    要用它连接需自行补口令——服务端不再把探测命中的明文口令回传出去。
    """
    if not ALLOW_LOCAL_DB_DISCOVERY:
        return []

    results = []
    ports = [5432, 5433, 5434]
    creds = _LOCAL_DISCOVERY_CREDS
    seen = set()

    for port in ports:
        for user, pwd in creds:
            try:
                conn = psycopg2.connect(
                    host="localhost", port=port, user=user, password=pwd,
                    dbname="postgres", connect_timeout=3
                )
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT datname FROM pg_database "
                        "WHERE datistemplate=false ORDER BY datname"
                    )
                    dbs = [r[0] for r in cur.fetchall()]
                conn.close()

                key = f"{port}:{user}"
                if key not in seen:
                    seen.add(key)
                    results.append({
                        "host": "localhost",
                        "port": port,
                        "user": user,
                        "database_list": dbs,
                    })
                break  # 该端口已连通，不再试其他凭据
            except Exception:
                continue

    return results
