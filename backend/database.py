import os
import json
import re
import csv
import shutil
import tempfile
import time
import urllib.request
import zipfile
from decimal import Decimal, InvalidOperation
from threading import Lock
from pathlib import Path
from typing import Any, Dict, List
from sqlalchemy import create_engine, text, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from dotenv import load_dotenv

load_dotenv()

DB_TYPE = os.getenv("DB_TYPE", "postgresql").strip().lower()
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "a07_manufacturing")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root")

SUPPORTED_DB_TYPES = {"postgresql", "mysql", "mssql", "snowflake", "clickhouse", "bigquery"}
DEFAULT_DB_NAME = "postgres"

# 数据源 dialect 映射：db_type -> (SQLAlchemy dialect, 默认端口, 可选驱动依赖, URL query 附加参数)
# 说明：PostgreSQL/MySQL 用纯 Python 驱动（pg8000/pymysql）已验证；其余 4 种为
# 连接层骨架，驱动需 `pip install <driver>`（见 requirements.txt 注释），且元数据层
# （get_table_row_counts/get_foreign_keys/get_real_tables 等方言特定 SQL）待接真实库逐一适配。
DIALECT_MAP = {
    "postgresql": ("postgresql+pg8000", 5432, "pg8000", None),
    "mysql": ("mysql+pymysql", 3306, "pymysql", None),
    # mssql+pyodbc 必须带 driver 参数，否则连接直接报错；可用环境变量 MSSQL_DRIVER 覆盖
    "mssql": ("mssql+pyodbc", 1433, "pyodbc", {"driver": os.getenv("MSSQL_DRIVER", "ODBC Driver 17 for SQL Server")}),
    "snowflake": ("snowflake", 443, "snowflake-sqlalchemy", None),
    "clickhouse": ("clickhouse+connect", 8123, "clickhouse-connect", None),
    "bigquery": ("bigquery", 0, "sqlalchemy-bigquery", None),
}

DATABASE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
DATABASE_PROFILES_PATH = Path(__file__).with_name(".database_profiles.json")
DATABASE_SOURCES_PATH = Path(__file__).with_name(".database_sources.json")

if DB_TYPE not in SUPPORTED_DB_TYPES:
    DB_TYPE = "postgresql"

_engine_lock = Lock()
try:
    _env_port = int(DB_PORT)
except (ValueError, TypeError):
    _env_port = 0
_current_config = {
    "db_type": DB_TYPE,
    "host": DB_HOST,
    "port": _env_port,
    "name": DB_NAME,
    "user": DB_USER,
}


def build_engine(config: dict, isolation_level=None):
    """按数据库类型生成 SQLAlchemy 引擎。

    PostgreSQL/MySQL 用纯 Python 驱动（已验证）；Snowflake/BigQuery/ClickHouse/SQL Server
    为连接层骨架——dialect 映射就绪，驱动安装 + 元数据层方言适配为后续（见 DIALECT_MAP）。
    """
    db_type = config.get("db_type", "postgresql")
    database = config.get("name") or config.get("database")
    if db_type == "mysql":
        # MySQL 管理操作（创建工作区/删库）传 name=None 时不携带 database 参数
        # （回退 DEFAULT_DB_NAME="postgres" 会让 MySQL 报 "Unknown database 'postgres'"）
        url = URL.create(
            "mysql+pymysql",
            username=config["user"],
            password=config.get("password", ""),
            host=config["host"],
            port=int(config.get("port", 3306)),
            database=database,
        )
        return create_engine(url, pool_pre_ping=True, poolclass=QueuePool,
                             pool_size=5, max_overflow=10, pool_recycle=1800,
                             isolation_level=isolation_level)
    if db_type in DIALECT_MAP and db_type not in ("postgresql", "mysql"):
        # 其余数据源：统一 dialect 分支（特殊参数如 Snowflake 的 account/warehouse、
        # BigQuery 的 credentials 需在接入真实库时扩展）
        dialect, def_port, _driver, url_query = DIALECT_MAP[db_type]
        try:
            _port = int(config.get("port") or def_port)
        except (ValueError, TypeError):
            _port = def_port
        url = URL.create(
            dialect,
            username=config.get("user", ""),
            password=config.get("password", ""),
            host=config["host"],
            port=_port if def_port else None,
            database=database,
            query=url_query,
        )
        # 注意：非 PG 数据源不注册 search_path 监听器（string_agg/SET search_path 是 PG 方言，
        # 挂到 MySQL/mssql/clickhouse 引擎上会让每次建连都白跑一次失败查询）
        return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=1800)
    database = database or DEFAULT_DB_NAME
    url = URL.create(
        "postgresql+pg8000",
        username=config["user"],
        password=config.get("password", ""),
        host=config["host"],
        port=int(config.get("port", 5432)),
        database=database,
    )
    engine = create_engine(url, pool_pre_ping=True, poolclass=QueuePool,
                           pool_size=5, max_overflow=10, pool_recycle=1800,
                           isolation_level=isolation_level)
    # 连接建立后，把当前库的所有业务 schema（如 factory）加入 search_path。
    # 这样 inspector.get_table_names() / get_columns() / quote_ident(裸表名) 等
    # 全部接口都能直接解析到非 public schema 的表 —— 无需逐接口适配，前端表名保持裸名。
    @event.listens_for(engine, "connect")
    def _set_business_search_path(dbapi_conn, connection_record):
        try:
            cur = dbapi_conn.cursor()
            cur.execute(
                "SELECT string_agg(schema_name, ',' ORDER BY schema_name) "
                "FROM information_schema.schemata "
                "WHERE schema_name NOT LIKE 'pg_%' AND schema_name NOT IN ('information_schema', 'public')"
            )
            row = cur.fetchone()
            extra = (row[0] if row and row[0] else "").strip()
            if extra:
                cur.execute(f"SET search_path TO public, {extra}")
            cur.close()
        except Exception:
            pass
    return engine


engine = build_engine({**_current_config, "password": DB_PASSWORD})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_database_config():
    """返回当前连接配置（不含密码）"""
    return {k: _current_config[k] for k in ("db_type", "host", "port", "name", "user")}


def test_database_connection(config: dict):
    """测试连接是否可用"""
    test_engine = build_engine(config)
    try:
        with test_engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    finally:
        test_engine.dispose()


def switch_database(config: dict):
    """切换当前数据库（config 含 host/port/database|name/user/password）"""
    global engine, SessionLocal, _current_config
    cfg = dict(config)
    cfg.setdefault("db_type", DB_TYPE)
    if "database" in cfg and "name" not in cfg:
        cfg["name"] = cfg["database"]
    new_engine = build_engine(cfg)
    try:
        with new_engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception:
        new_engine.dispose()
        raise

    with _engine_lock:
        old_engine = engine
        engine = new_engine
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            _port = int(cfg.get("port") or 0)
        except (ValueError, TypeError):
            _port = 0
        _current_config = {
            "db_type": cfg.get("db_type", "postgresql"),
            "host": cfg["host"],
            "port": _port,
            "name": cfg["name"],
            "user": cfg["user"],
        }
        old_engine.dispose()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =========================================================
# 数据库工作区（Profiles）管理
# =========================================================

def validate_database_name(name: str) -> str:
    normalized = name.strip()
    if not DATABASE_NAME_RE.fullmatch(normalized):
        raise ValueError("数据库名只能使用字母、数字和下划线，且必须以字母或下划线开头")
    return normalized


def get_database_profiles() -> List[str]:
    names = [DB_NAME]
    try:
        with open(DATABASE_PROFILES_PATH, "r", encoding="utf-8") as profiles_file:
            saved_names = json.load(profiles_file)
        # 已保存的历史库（含导入生成的数字开头名，如 "123"）全部展示；
        # 命名规范只在「新建库 / 保存配置」时强制，避免用户切不回自己导入的库。
        if isinstance(saved_names, list):
            names.extend(name for name in saved_names if isinstance(name, str) and name.strip())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return list(dict.fromkeys(names))


# ── 原子写文件（防 Windows 文件锁 / 杀软瞬时占用导致 Permission denied）─────────
# 背景：切库保存 .env / .database_profiles.json 偶发 Errno 13 —— 编辑器以独占读打开、
# Defender 实时扫描瞬时锁文件都会让 open("w") 被拒。策略：写同目录 .tmp 后 os.replace
# 原子替换；PermissionError 按 250ms/500ms/750ms/1s 退避重试（扛过瞬时锁），仍失败才抛。
def _write_text_atomic(path: Path, content: str, retries: int = 4) -> None:
    tmp = path.with_name(path.name + ".tmp")
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(0.25 * (attempt + 1))
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
    raise last_err if last_err is not None else PermissionError("write denied")


def remember_database_profile(name: str) -> None:
    profiles = get_database_profiles()
    if name not in profiles:
        profiles.append(name)
    _write_text_atomic(DATABASE_PROFILES_PATH,
                       json.dumps(profiles, ensure_ascii=False, indent=2))


def forget_database_profile(name: str) -> None:
    remaining_profiles = [profile for profile in get_database_profiles() if profile != name]
    _write_text_atomic(DATABASE_PROFILES_PATH,
                       json.dumps(remaining_profiles, ensure_ascii=False, indent=2))


# =========================================================
# 多数据源注册表（P1-1 对标 Spotter automatic model selection 的渐进式落地）
# =========================================================
# 设计：注册多个数据源（每个含独立连接信息），但保持「单活动引擎」架构——
# 激活某源即走既有 switch_database() 切换全局连接。语义缓存/记忆/ACL 的
# db_key 已带 host:port:name 标识，天然按源隔离，多源不会串数据。
# 跨源发现（search_across_sources）只读各源元数据、不切换连接，提示用户
# 「该表属于数据源 X」后由用户一键激活——不做自动切换，避免副作用。

def get_database_sources() -> list[dict]:
    """全部已注册数据源（不含密码）。当前活动源由 is_active 标记。"""
    try:
        with open(DATABASE_SOURCES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        sources = data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        sources = []
    # 兜底：从未注册过（空列表）或活动源丢失（文件被清/损坏）时，
    # 把当前 .env 连接登记为默认活动源——保证 UI 永远有可用的当前源。
    if not sources or not any(s.get("is_active") for s in sources):
        sources = [{
            "id": "default",
            "name": "默认数据源",
            "db_type": _current_config.get("db_type", "postgresql"),
            "host": _current_config.get("host", ""),
            "port": _current_config.get("port", 0),
            "database": _current_config.get("name", ""),
            "user": _current_config.get("user", ""),
            "is_active": True,
            "created_at": 0,
        }] + [s for s in sources if s.get("id") != "default"]
    return [{k: v for k, v in s.items() if k != "password"} for s in sources]


def _save_sources(sources: list[dict]) -> None:
    try:
        with open(DATABASE_SOURCES_PATH, "w", encoding="utf-8") as f:
            json.dump(sources, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def add_database_source(config: dict) -> dict:
    """注册一个新数据源（先测试连接，成功才保存）。返回 {success, source, error}。"""
    name = str(config.get("name") or "").strip()[:40]
    db_type = str(config.get("db_type") or "postgresql").strip().lower()
    host = str(config.get("host") or "").strip()
    database = str(config.get("database") or config.get("name") or "").strip()
    user = str(config.get("user") or "").strip()
    password = str(config.get("password") or "")
    if not name or not host or not database:
        return {"success": False, "error": "名称/主机/数据库名不能为空"}
    if db_type not in SUPPORTED_DB_TYPES:
        return {"success": False, "error": f"不支持的数据源类型: {db_type}"}
    try:
        port = int(config.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    # 测试连接（复用既有 test_database_connection，失败不保存）
    try:
        test_database_connection({
            "db_type": db_type, "host": host, "port": port,
            "name": database, "user": user, "password": password,
        })
    except Exception as e:
        return {"success": False, "error": f"连接测试失败：{str(e)[:120]}"}
    # 已有同名源 → 更新而非重复添加（防 UI 重复提交）
    try:
        with open(DATABASE_SOURCES_PATH, "r", encoding="utf-8") as f:
            sources = json.load(f)
        if not isinstance(sources, list):
            sources = []
    except Exception:
        sources = []
    # 首次注册（列表为空）→ 先登记默认源（当前连接，保持 UI 有活动源可依赖）
    if not sources:
        sources = [{
            "id": "default",
            "name": "默认数据源",
            "db_type": _current_config.get("db_type", "postgresql"),
            "host": _current_config.get("host", ""),
            "port": _current_config.get("port", 0),
            "database": _current_config.get("name", ""),
            "user": _current_config.get("user", ""),
            "password": DB_PASSWORD,
            "is_active": True,
            "created_at": 0,
        }]
    for s in sources:
        if s.get("name") == name:
            s.update({"db_type": db_type, "host": host, "port": port,
                      "database": database, "user": user, "password": password})
            _save_sources(sources)
            return {"success": True, "source": {k: v for k, v in s.items() if k != "password"}}
    new_source = {
        "id": f"src_{int(time.time())}_{len(sources)}",
        "name": name, "db_type": db_type, "host": host, "port": port,
        "database": database, "user": user, "password": password,
        "is_active": False, "created_at": time.time(),
    }
    sources.append(new_source)
    _save_sources(sources)
    return {"success": True, "source": {k: v for k, v in new_source.items() if k != "password"}}


def remove_database_source(source_id: str) -> bool:
    """删除数据源（不能删除当前活动源）。"""
    try:
        with open(DATABASE_SOURCES_PATH, "r", encoding="utf-8") as f:
            sources = json.load(f)
    except Exception:
        return False
    target = next((s for s in sources if s.get("id") == source_id), None)
    if not target or target.get("is_active"):
        return False
    sources = [s for s in sources if s.get("id") != source_id]
    _save_sources(sources)
    return True


def activate_database_source(source_id: str) -> dict:
    """把某数据源设为活动源（测试连接 → switch_database → 标记）。返回 {success, error}。"""
    try:
        with open(DATABASE_SOURCES_PATH, "r", encoding="utf-8") as f:
            sources = json.load(f)
    except Exception:
        return {"success": False, "error": "数据源列表读取失败"}
    target = next((s for s in sources if s.get("id") == source_id), None)
    if not target:
        return {"success": False, "error": "数据源不存在"}
    try:
        switch_database({
            "db_type": target.get("db_type", "postgresql"),
            "host": target["host"], "port": int(target.get("port") or 0),
            "name": target.get("database") or target.get("name"),
            "user": target.get("user", ""),
            "password": target.get("password", ""),
        })
    except Exception as e:
        return {"success": False, "error": f"切换失败：{str(e)[:120]}"}
    for s in sources:
        s["is_active"] = (s.get("id") == source_id)
    _save_sources(sources)
    # 持久化为默认库：数据源激活同样写入 .env，重启后默认该库（用户切到哪个默认哪个）
    try:
        save_db_config_to_env({
            "db_type": target.get("db_type", "postgresql"),
            "host": target["host"], "port": int(target.get("port") or 0),
            "name": target.get("database") or target.get("name"),
            "user": target.get("user", ""),
            "password": target.get("password", ""),
        })
    except Exception:
        pass
    return {"success": True}


def search_across_sources(keyword: str, limit: int = 8) -> list[dict]:
    """跨数据源搜索表（只读各源元数据，不切换连接）。

    返回 [{source_id, source_name, table_name, label, description}]。
    与当前活动源内搜索互补：活动源没命中时，这里能发现「表在哪个源」，
    前端展示「该表属于数据源 X」→ 用户一键激活后再问。
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    try:
        with open(DATABASE_SOURCES_PATH, "r", encoding="utf-8") as f:
            sources = json.load(f)
        if not isinstance(sources, list):
            return []
    except Exception:
        return []
    results: list[dict] = []
    for s in sources:
        if s.get("is_active"):
            continue  # 活动源内部搜索走正常链路，不重复
        src_id = s.get("id", "")
        src_name = s.get("name", "")
        db_type = s.get("db_type", "postgresql")
        host = s.get("host", "")
        port = s.get("port") or 0
        database = s.get("database") or s.get("name") or ""
        user = s.get("user", "")
        password = s.get("password", "")
        try:
            src_engine = build_engine({"db_type": db_type, "host": host, "port": port,
                                       "name": database, "user": user, "password": password})
            with src_engine.connect() as conn:
                if db_type == "mysql":
                    rows = conn.exec_driver_sql(
                        "SELECT table_name, table_comment FROM information_schema.tables "
                        "WHERE table_schema = DATABASE()"
                    ).fetchall()
                else:
                    rows = conn.exec_driver_sql(
                        "SELECT table_name, COALESCE(obj_description((quote_ident(table_schema) || '.' || quote_ident(table_name))::regclass, 'pg_class'), '') "
                        "FROM information_schema.tables "
                        "WHERE table_schema NOT IN ('pg_catalog','information_schema')"
                    ).fetchall()
            src_engine.dispose()
        except Exception:
            continue  # 某源连不上（离线/密码过期）→ 跳过，不影响其他源
        for tbl, desc in rows[:500]:
            tbl_name = str(tbl or "")
            if not tbl_name:
                continue
            desc_text = str(desc or "")
            hay = f"{tbl_name} {desc_text}".lower()
            # 三种匹配：① 全文子串（中文关键词命中中文注释 / 英文关键词命中英文表名）；
            # ② 英文表名按 _/. 分词后的单词前缀（'inventory' 命中 inv_inventory_snapshot）；
            # ③ 中文关键词转拼音不可行 → 依赖注释（数据侧补注释后自动可用）。
            hit = kw in hay
            if not hit:
                for part in re.split(r"[_.\s]+", tbl_name.lower()):
                    if part and part.startswith(kw):
                        hit = True
                        break
            if hit:
                results.append({
                    "source_id": src_id, "source_name": src_name,
                    "table_name": tbl_name,
                    "label": tbl_name.split(".")[-1],
                    "description": desc_text[:100],
                })
                if len(results) >= limit:
                    return results
    return results


def _quote_ident(name: str) -> str:
    # 转义标识符内部的引号，防止拼接 SQL 时破坏引号结构（PG: "" 转义，MySQL: `` 转义）
    if _current_config["db_type"] == "mysql":
        return f"`{name.replace('`', '``')}`"
    return f'"{name.replace(chr(34), chr(34) * 2)}"'


def quote_ident(name: str) -> str:
    """按当前数据库类型返回带引号的标识符（公开 API）"""
    return _quote_ident(name)


def get_db_type() -> str:
    """返回当前数据库类型（postgresql / mysql）"""
    return _current_config.get("db_type", "postgresql")


def _database_exists(conn, name: str) -> bool:
    if _current_config["db_type"] == "mysql":
        return conn.execute(
            text("SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :name"),
            {"name": name},
        ).scalar() is not None
    return conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}).scalar() is not None


def switch_database_workspace(name: str, create_if_missing: bool = True) -> Dict[str, Any]:
    """切换到已保存的工作区数据库；不存在时可自动创建。

    命名规范（字母/下划线开头）只在「新建库」时强制；对**已存在**的库
    （含历史导入产生的数字开头名，如 "123"）直接放行切换——否则用户
    永远切不回自己导入的库（2026-08-18 修复）。
    """
    target_name = (name or "").strip()
    if not target_name:
        raise ValueError("数据库名不能为空")
    remember_database_profile(DB_NAME)

    base = dict(_current_config)
    base["name"] = target_name

    # 用服务器管理库连接（不依赖目标库存在）
    admin_cfg = dict(base)
    admin_cfg["password"] = DB_PASSWORD
    if admin_cfg["db_type"] == "mysql":
        admin_cfg["name"] = None  # 连接 MySQL 服务器
    else:
        admin_cfg["name"] = DEFAULT_DB_NAME

    admin_engine = build_engine(admin_cfg, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = _database_exists(conn, target_name)
            if not exists:
                # 不存在的库：仍强制命名规范（防注入 / 防乱建库）
                if not DATABASE_NAME_RE.fullmatch(target_name):
                    raise ValueError("数据库名只能使用字母、数字和下划线，且必须以字母或下划线开头")
                if not create_if_missing:
                    raise ValueError(f"数据库 '{target_name}' 不存在")
                if admin_cfg["db_type"] == "mysql":
                    conn.execute(text(f'CREATE DATABASE `{target_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'))
                else:
                    conn.execute(text(f'CREATE DATABASE "{target_name}"'))
    finally:
        admin_engine.dispose()

    switch_database({**base, "password": DB_PASSWORD})
    remember_database_profile(target_name)
    return {"name": target_name, "profiles": get_database_profiles()}


def delete_database_workspace(name: str) -> Dict[str, Any]:
    """删除工作区数据库（不能删除当前正在使用的）"""
    target_name = validate_database_name(name)
    if target_name == DB_NAME:
        raise ValueError("不能删除当前使用的数据库，请先切换到另一个数据库")
    if target_name not in get_database_profiles():
        raise ValueError(f"数据库 '{target_name}' 不在已保存的工作区列表中")

    admin_cfg = dict(_current_config)
    admin_cfg["password"] = DB_PASSWORD
    admin_cfg["name"] = None if admin_cfg["db_type"] == "mysql" else DEFAULT_DB_NAME
    admin_engine = build_engine(admin_cfg, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = _database_exists(conn, target_name)
            if not exists:
                raise ValueError(f"数据库 '{target_name}' 不存在")
            if admin_cfg["db_type"] == "mysql":
                conn.execute(text(f'DROP DATABASE `{target_name}`'))
            else:
                conn.execute(text(f'DROP DATABASE "{target_name}"'))
    finally:
        admin_engine.dispose()

    forget_database_profile(target_name)
    return {"name": target_name, "profiles": get_database_profiles()}


# =========================================================
# 配置持久化（写入 .env）
# =========================================================

ENV_PATH = Path(__file__).with_name(".env")


def save_db_config_to_env(config: Dict[str, Any]) -> None:
    """保存数据库连接配置到 .env（含数据库软件类型），并重载连接"""
    db_type = config.get("db_type", "postgresql")
    if db_type not in SUPPORTED_DB_TYPES:
        raise ValueError("DB_TYPE 仅支持 postgresql 或 mysql")

    # .env 值转义：密码可能含 #（注释）、空格、= 等，直接写会让 load_dotenv 下次解析截断/报错；
    # 用双引号包裹并按 python-dotenv 规则转义反斜杠与内嵌双引号。
    def _env_quote(v: str) -> str:
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    updates = {
        "DB_TYPE": db_type,
        "DB_HOST": _env_quote(config["host"]),
        "DB_PORT": _env_quote(config["port"]),
        "DB_NAME": _env_quote(config["name"]),
        "DB_USER": _env_quote(config["user"]),
        "DB_PASSWORD": _env_quote(config.get("password", "")),
    }

    lines: list[str] = []
    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    def _parse_key(line: str) -> str:
        # 忽略注释行（# 开头）与前导空白，避免把 `# DB_HOST=xxx` 误判成存在的键
        s = line.strip()
        if not s or s.startswith("#"):
            return ""
        return s.split("=", 1)[0].strip()

    existing_keys = {_parse_key(line) for line in lines if _parse_key(line)}
    new_lines = []
    for line in lines:
        key = _parse_key(line)
        if key in updates:
            new_lines.append(f"{key}={updates[key]}\n")
        else:
            new_lines.append(line)
    for key, value in updates.items():
        if key not in existing_keys:
            new_lines.append(f"{key}={value}\n")

    _write_text_atomic(ENV_PATH, "".join(new_lines))

    # 重载环境变量并切换连接（取原始值，不带引号）
    raw_password = str(config.get("password", ""))
    global DB_TYPE, DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
    DB_TYPE, DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD = (
        db_type, str(config["host"]), str(config["port"]), str(config["name"]),
        str(config["user"]), raw_password,
    )
    switch_database({
        "db_type": db_type,
        "host": str(config["host"]),
        "port": int(config["port"]),
        "name": str(config["name"]),
        "user": str(config["user"]),
        "password": raw_password,
    })
    remember_database_profile(str(config["name"]))


# =========================================================
# 数据源导入（CSV / SQLite / ZIP / URL / 文件夹）
# =========================================================

def _quote_ident_import(name: str) -> str:
    return f"`{name}`" if _current_config["db_type"] == "mysql" else f'"{name}"'


def infer_column_type(values: List[str]) -> str:
    cleaned = [v.strip() for v in values if str(v).strip() != ""]
    if not cleaned:
        return "TEXT"
    if all(_looks_like_int(v) for v in cleaned):
        return "INTEGER"
    if all(_looks_like_decimal(v) for v in cleaned):
        return "NUMERIC"
    if all(_looks_like_date(v) for v in cleaned):
        return "DATE"
    return "TEXT"


def _looks_like_int(value: str) -> bool:
    try:
        return Decimal(value.strip()) == Decimal(value.strip()).to_integral_value()
    except (InvalidOperation, ValueError):
        return False


def _looks_like_decimal(value: str) -> bool:
    try:
        Decimal(value.strip())
        return True
    except (InvalidOperation, ValueError):
        return False


def _looks_like_date(value: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip()))


def infer_primary_key(columns: List[Dict[str, Any]]) -> str | None:
    """Infer a primary key only when a conventional identifier is unique."""
    ranked_columns = sorted(
        columns,
        key=lambda col: (
            0 if str(col["name"]).strip().lower() == "id" else
            1 if str(col["name"]).strip().lower().endswith("_id") else
            2 if str(col["name"]).strip() in {"编号", "序号", "主键"} else 3,
        ),
    )
    for column in ranked_columns:
        name = str(column["name"]).strip()
        normalized = name.lower()
        is_identifier = normalized == "id" or normalized.endswith("_id") or name in {"编号", "序号", "主键"}
        values = [str(value).strip() for value in column.get("values", [])]
        if is_identifier and values and all(values) and len(set(values)) == len(values):
            return re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_") or None
    return None


def build_create_table_sql(table_name: str, columns: List[Dict[str, Any]], primary_key: str | None = None) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_]+", "_", table_name).strip("_") or "imported_table"
    primary_key = primary_key or infer_primary_key(columns)
    lines = [f'CREATE TABLE IF NOT EXISTS {_quote_ident_import(safe_name)} (']
    column_defs = []
    for col in columns:
        name = re.sub(r"[^a-zA-Z0-9_]+", "_", col["name"]).strip("_") or "column"
        col_type = infer_column_type(col.get("values", []))
        if primary_key and name == re.sub(r"[^a-zA-Z0-9_]+", "_", primary_key).strip("_"):
            column_defs.append(f'{_quote_ident_import(name)} {col_type} PRIMARY KEY')
        else:
            column_defs.append(f'{_quote_ident_import(name)} {col_type}')
    lines.append(",\n    ".join(column_defs))
    lines.append(")")
    return "\n".join(lines)


def build_insert_sql(table_name: str, column_names: List[str]):
    safe_table_name = re.sub(r"[^a-zA-Z0-9_]+", "_", table_name).strip("_") or "imported_table"
    safe_columns = [re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_") or "column" for name in column_names]
    placeholders = ", ".join([":p" + str(i) for i in range(len(safe_columns))])
    columns_sql = ", ".join([_quote_ident_import(name) for name in safe_columns])
    if _current_config["db_type"] == "mysql":
        # MySQL 不支持 ON CONFLICT，使用 INSERT IGNORE 实现幂等导入
        return text(f'INSERT IGNORE INTO {_quote_ident_import(safe_table_name)} ({columns_sql}) VALUES ({placeholders})')
    return text(f'INSERT INTO {_quote_ident_import(safe_table_name)} ({columns_sql}) VALUES ({placeholders}) ON CONFLICT DO NOTHING')


def import_csv_to_db(file_path: str, table_name: str, primary_key: str | None = None) -> Dict[str, Any]:
    # UTF-8 is the normal browser-upload encoding; GBK is common in exported
    # Chinese database documents.  Do not silently mangle either.
    last_error: UnicodeDecodeError | None = None
    rows = []
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with open(file_path, "r", encoding=encoding, newline="") as f:
                rows = list(csv.DictReader(f))
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    else:
        raise ValueError("CSV 编码不受支持，请使用 UTF-8 或 GB18030") from last_error

    if not rows:
        raise ValueError("CSV 文件为空")

    columns = []
    for key in rows[0].keys():
        values = [row.get(key, "") for row in rows]
        columns.append({"name": key, "values": values})

    create_sql = build_create_table_sql(table_name, columns, primary_key=primary_key)
    safe_table_name = re.sub(r"[^a-zA-Z0-9_]+", "_", table_name).strip("_") or "imported_table"
    inserted_rows = 0
    with engine.begin() as conn:
        conn.execute(text(create_sql))
        if rows:
            # executemany 批量插入（替代逐行 execute，大文件导入显著提速）
            field_names = [re.sub(r"[^a-zA-Z0-9_]+", "_", key).strip("_") or "column" for key in rows[0].keys()]
            sql = build_insert_sql(safe_table_name, field_names)
            params_list = [
                {f"p{i}": row.get(key, "") for i, key in enumerate(rows[0].keys())}
                for row in rows
            ]
            result = conn.execute(sql, params_list)
            # executemany 的 rowcount 在部分驱动下不可靠（PG/MySQL 行为不一），
            # 无效时回退到行数近似值
            inserted_rows = max(result.rowcount or 0, 0)
            if inserted_rows <= 0:
                inserted_rows = len(rows)

    return {
        "table_name": table_name,
        "row_count": len(rows),
        "inserted_rows": inserted_rows,
        "skipped_rows": len(rows) - inserted_rows,
        "columns": [col["name"] for col in columns],
    }


def _safe_url_download(url: str, dst: str) -> None:
    """安全的 URL 下载（防 SSRF）：协议白名单 + 拒绝内网/回环/链路本地地址 + 大小上限。

    公网部署时若该接口暴露，恶意请求可借 urlretrieve 探测内网（127.0.0.1/10.x/192.168.x 等）。
    """
    from urllib.parse import urlparse
    import socket

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("仅支持 http/https 数据源")
    host = parsed.hostname or ""
    # 解析 DNS 拿到真实 IP，拒绝内网/回环/链路本地/保留网段
    try:
        infos = socket.getaddrinfo(host, None)
        ip_candidates = {info[4][0] for info in infos}
    except Exception:
        raise ValueError(f"无法解析数据源地址: {host}")

    import ipaddress
    for ip in ip_candidates:
        try:
            addr = ipaddress.ip_address(ip.split("%")[0])
            if (addr.is_private or addr.is_loopback or addr.is_link_local
                    or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
                raise ValueError(f"禁止访问内网/保留地址: {ip}")
        except ValueError as e:
            if "禁止访问" in str(e):
                raise
            continue

    # 大小上限（默认 200MB）——下载前无法得知大小，用流式边下边数
    max_bytes = 200 * 1024 * 1024
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; DataImport)"})
    with urllib.request.urlopen(req, timeout=30) as resp, open(dst, "wb") as f:
        total = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("数据源文件超过 200MB 上限")
            f.write(chunk)


def import_database_source(source: str, target_table_name: str | None = None, primary_key: str | None = None) -> Dict[str, Any]:
    if source.startswith(("http://", "https://")):
        with tempfile.TemporaryDirectory() as tmp_dir:
            downloaded_path = os.path.join(tmp_dir, os.path.basename(source.split("?")[0]) or "downloaded.db")
            _safe_url_download(source, downloaded_path)
            return import_database_file(downloaded_path, target_table_name=target_table_name, primary_key=primary_key)

    if os.path.isdir(source):
        return import_folder(source, target_table_name=target_table_name, primary_key=primary_key)

    if os.path.isfile(source):
        if Path(source).suffix.lower() == ".zip":
            return import_zip_archive(source, target_table_name=target_table_name, primary_key=primary_key)
        return import_database_file(source, target_table_name=target_table_name, primary_key=primary_key)

    raise ValueError("暂不支持该数据源")


def collect_import_candidate_files(folder_path: str) -> List[str]:
    candidates: List[str] = []
    for root, _, files in os.walk(folder_path):
        for name in files:
            lower_name = name.lower()
            if lower_name.endswith((".csv", ".sqlite", ".sqlite3", ".db", ".zip")):
                candidates.append(os.path.join(root, name))
            elif lower_name.endswith((".md", ".markdown", ".txt", ".json", ".yaml", ".yml")):
                continue

    candidates.sort(key=lambda p: (0 if os.path.splitext(p)[1].lower() in {".csv"} else 1, os.path.basename(p).lower()))
    return candidates


def import_folder(folder_path: str, target_table_name: str | None = None, primary_key: str | None = None) -> Dict[str, Any]:
    supported_files = collect_import_candidate_files(folder_path)

    if not supported_files:
        raise ValueError("文件夹中没有找到可导入的 CSV/SQLite/ZIP 文件")

    effective_table_name = target_table_name if len(supported_files) == 1 else None
    imported = []
    for path in supported_files:
        if path.lower().endswith(".zip"):
            imported.append(import_zip_archive(path, target_table_name=effective_table_name, primary_key=primary_key))
        else:
            imported.append(import_database_file(path, target_table_name=effective_table_name, primary_key=primary_key))

    return {"mode": "folder", "files": imported}


def import_zip_archive(zip_path: str, target_table_name: str | None = None, primary_key: str | None = None) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # ZIP filenames are untrusted: reject path traversal and avoid
            # extracting directory entries as files.
            destination = Path(tmp_dir).resolve()
            for member in zf.infolist():
                member_path = (destination / member.filename).resolve()
                if not member_path.is_relative_to(destination):
                    raise ValueError("ZIP 包含不安全的文件路径")
                if member.is_dir():
                    member_path.mkdir(parents=True, exist_ok=True)
                    continue
                member_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as source, open(member_path, "wb") as target:
                    shutil.copyfileobj(source, target)
        return import_folder(tmp_dir, target_table_name=target_table_name, primary_key=primary_key)


def import_database_file(file_path: str, target_table_name: str | None = None, primary_key: str | None = None) -> Dict[str, Any]:
    path = Path(file_path)
    ext = path.suffix.lower()

    if ext == ".csv":
        table_name = target_table_name or path.stem
        return import_csv_to_db(str(path), table_name, primary_key=primary_key)

    if ext in {".sqlite", ".sqlite3", ".db"}:
        import sqlite3
        conn = sqlite3.connect(str(path))
        try:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            imported_tables = []
            for (table_name,) in tables:
                if table_name.startswith("sqlite_"):
                    continue
                # SQLite 表名来自导入文件的元数据，可能含双引号/分号（恶意 .db 文件），
                # 直接拼进 SELECT/PRAGMA 会被注入。双引号包裹的标识符需将内部 " 翻倍转义。
                qname = table_name.replace('"', '""')
                query = conn.execute(f'SELECT * FROM "{qname}"')
                rows = query.fetchall()
                columns = [desc[0] for desc in query.description or []]
                source_primary_keys = [column[1] for column in conn.execute(f'PRAGMA table_info("{qname}")') if column[5]]
                detected_primary_key = source_primary_keys[0] if len(source_primary_keys) == 1 else primary_key
                if not rows:
                    create_sql = build_create_table_sql(
                        table_name,
                        [{"name": col, "values": []} for col in columns],
                        primary_key=detected_primary_key,
                    )
                    with engine.begin() as conn_pg:
                        conn_pg.execute(text(create_sql))
                    imported_tables.append({"table_name": table_name, "row_count": 0, "columns": columns})
                    continue
                create_sql = build_create_table_sql(table_name, [{"name": col, "values": [str(row[i]) for row in rows]} for i, col in enumerate(columns)], primary_key=detected_primary_key)
                safe_table_name = re.sub(r"[^a-zA-Z0-9_]+", "_", table_name).strip("_") or "imported_table"
                with engine.begin() as conn_pg:
                    conn_pg.execute(text(create_sql))
                    # 性能优化：SQLite 导入此前逐行 execute（万行级文件极慢）；
                    # 与 CSV 导入路径对齐，改为一次性 executemany，大表导入提速数十倍。
                    sql = build_insert_sql(safe_table_name, columns)
                    params_list = [
                        {f"p{i}": row[i] for i in range(len(columns))}
                        for row in rows
                    ]
                    result = conn_pg.execute(sql, params_list)
                    inserted_rows = max(result.rowcount or 0, 0)
                    if inserted_rows <= 0:
                        inserted_rows = len(rows)
                imported_tables.append({
                    "table_name": table_name,
                    "row_count": len(rows),
                    "inserted_rows": inserted_rows,
                    "skipped_rows": len(rows) - inserted_rows,
                    "columns": columns,
                })
            return {"mode": "sqlite", "tables": imported_tables}
        finally:
            conn.close()

    raise ValueError("暂不支持的文件类型")
