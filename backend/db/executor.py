"""SQL 执行器 — 基于 SQLAlchemy engine（自动适配 PostgreSQL / MySQL）

安全护栏：
- 仅允许 SELECT（含 WITH...SELECT 前置 CTE）
- 拦截写操作/危险函数（DROP/INSERT/UPDATE/DELETE/LOCK/pg_sleep/COPY 等）
- 拦截多条语句（分号拼接）
- statement_timeout 硬超时（PG: SET LOCAL；MySQL: MAX_EXECUTION_TIME hint）
- 结果行数硬上限（默认 5000，超出截断并标记 truncated）
"""

import os
import re
import time

from sqlalchemy import text


def get_active_config():
    """返回当前活跃的数据库配置（兼容旧调用方）"""
    try:
        from database import get_database_config
        from config import DB_CONFIG
        cfg = get_database_config()
        return {
            "dbname": cfg.get("name", DB_CONFIG.get("dbname")),
            "user": cfg.get("user", DB_CONFIG.get("user")),
            "host": cfg.get("host", DB_CONFIG.get("host")),
            "port": cfg.get("port", DB_CONFIG.get("port")),
            "password": DB_CONFIG.get("password"),
        }
    except Exception:
        from config import DB_CONFIG
        return dict(DB_CONFIG)


def _engine():
    """获取当前全局 SQLAlchemy engine（切换数据库后自动指向新库）"""
    from database import engine
    return engine


def _is_mysql() -> bool:
    """判断当前活跃数据库是否为 MySQL。

    必须读 get_db_type()（_current_config，切换库后同步更新），
    不能读 DB_TYPE 常量——它只在进程启动时从 .env 读取，
    切库后不变，会导致 MySQL 库误走 PG 分支（如 SET LOCAL statement_timeout → 1193）。
    """
    try:
        from database import get_db_type
        return get_db_type() == "mysql"
    except Exception:
        return False


def _pg_to_mysql(sql: str) -> str:
    """把 PG 风格的 SQL 转换为 MySQL 兼容（仅在 MySQL 下调用）"""
    s = sql

    # 0. 先保护字符串字面量（单引号内内容不参与改写），防止值里的双引号/::type 被污染：
    #    如 'he said "hi"' 的 "hi" 会被规则1改写成 `hi`；'1::numeric' 会被规则2删除。
    #    逐字符扫描（含 '' 转义，与 _strip_string_literals 一致）：正则版 r"'(\\.|[^'])*'"
    #    会把 'it''s' 错误拆成 'it' + 's' 两段，中间内容被规则2/3误改。
    literals: list[str] = []

    def _hold(match_text: str) -> str:
        literals.append(match_text)
        return f"\x00L{len(literals) - 1}\x00"

    _buf: list[str] = []
    _i, _n = 0, len(s)
    while _i < _n:
        if s[_i] == "'":
            _j = _i + 1
            while _j < _n:
                if s[_j] == "'":
                    if _j + 1 < _n and s[_j + 1] == "'":  # '' 转义
                        _j += 2
                        continue
                    break
                _j += 1
            _buf.append(_hold(s[_i:_j + 1]))
            _i = _j + 1
        else:
            _buf.append(s[_i])
            _i += 1
    s = "".join(_buf)

    # 1. 双引号标识符 "name" → `name`
    s = re.sub(r'"([^"]+)"', r'`\1`', s)

    # 2. ::type 类型转换 → 去掉（MySQL 用原生类型）
    #    注意带精度的写法 ::numeric(10,2) / ::numeric(10) —— 必须连同括号一起吃掉，
    #    否则会残留 "(10,2)" 造成语法错误。
    s = re.sub(r'::(?:integer|int|numeric|decimal|float|double precision|text|varchar|date|timestamp|boolean|bigint|smallint|real|money)(?:\(\s*\d+(?:\s*,\s*\d+)?\s*\))?', '', s)

    # 3. ILIKE → LIKE
    s = re.sub(r'\bILIKE\b', 'LIKE', s)

    # 4. NOW() 兼容（两者都有）
    # 还原字符串字面量
    for i, lit in enumerate(literals):
        s = s.replace(f"\x00L{i}\x00", lit)
    return s


def _inject_mysql_timeout_hint(sql: str, timeout_ms: int) -> str:
    """把 MAX_EXECUTION_TIME hint 注入到顶层 SELECT 之后。

    MySQL 优化器 hint 语法：`SELECT /*+ MAX_EXECUTION_TIME(ms) */ ...`，
    必须紧跟查询关键字，且只能出现在最外层查询（CTE 内的 SELECT 无效）。
    处理两种形态：
      - 纯 SELECT ... → 注入到 SELECT 后
      - WITH ... SELECT ... → 找顶层 SELECT（通常是 ")" 之后的第一个 SELECT）
    """
    stripped = sql.lstrip()
    hint = f"/*+ MAX_EXECUTION_TIME({timeout_ms}) */"
    upper = stripped.upper()
    if upper.startswith("WITH"):
        # 顶层 SELECT 前一定是 WITH 子句的右括号；用 ")" 后第一个 SELECT 定位
        m = re.search(r'\)\s*(SELECT)\b', stripped, re.IGNORECASE)
        if m:
            return stripped[:m.end() - len(m.group(1))] + "SELECT " + hint + stripped[m.end():]
        # 兜底：第一个 SELECT（CTE 内也接受，总比没有强）
        m2 = re.search(r'\bSELECT\b', stripped, re.IGNORECASE)
        if m2:
            return stripped[:m2.start()] + "SELECT " + hint + stripped[m2.end():]
        return sql
    if upper.startswith("SELECT"):
        return "SELECT " + hint + stripped[len("SELECT"):]
    return sql


def get_pooled_conn():
    """兼容旧接口：返回一个 engine 连接"""
    return _engine().connect()


def put_pooled_conn(conn):
    """兼容旧接口：关闭连接"""
    try:
        conn.close()
    except Exception:
        pass


# ── 安全护栏配置 ─────────────────────────────────────────
SQL_TIMEOUT_MS = 10000          # 单条查询硬超时 10s
SQL_MAX_ROWS = 5000             # 结果行数硬上限
# EXPLAIN 干跑闸门：执行前先 EXPLAIN 估算扫描行数，超阈值提前拒绝（省 DB 资源、避免 10s 超时白等）。
# 只拦「明显大扫描」（如全表扫描 / 缺索引的跨表 JOIN），阈值设高避免误伤正常查询。
EXPLAIN_GATE_ENABLED = os.getenv("EXPLAIN_GATE_ENABLED", "1") == "1"
EXPLAIN_ROW_THRESHOLD = int(os.getenv("EXPLAIN_ROW_THRESHOLD", "1000000"))  # 估算行数上限（默认 100 万）
# 写操作 / 危险函数 / 危险语句（大小写不敏感，词边界匹配）
_BLOCKED_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|GRANT|REVOKE|"
    r"MERGE|CALL|EXEC|EXECUTE|ATTACH|DETACH|VACUUM|ANALYZE|REINDEX|CLUSTER|"
    r"LOCK|UNLOCK|COPY|LOAD|DUMP|RESTORE|REFRESH)\b",
    re.IGNORECASE,
)
# 危险函数：睡眠/锁定/文件操作/跨库访问等（防 DoS、提权与数据外泄）
# 2026-10-03 补齐（原清单缺以下高危项，均为"SELECT 开头 + 无黑名单词"即可绕过）：
#   dblink / dblink_exec —— 跨库读取任意数据。可直连内网其他库并带出账号密码，
#       绕过本库所有表级/列级/行级权限（本项目护栏完全失效）。危害最高。
#   pg_read_binary_file / pg_stat_file / pg_ls_dir —— 读服务器文件、目录枚举。
#   set_config —— 可在查询里改 GUC（含 statement_timeout、role 等会话设置）。
#   pg_notify / pg_rotate_logfile —— 副作用写入。
#   query_to_xml / table_to_xml —— 部分场景可探测 schema。
#   xp_cmdshell / sp_oacreate —— MySQL 侧命令执行。
#   into outfile/dumpfile 原有，但因整条 alternation 末尾带 `\s*\(`，
#   导致 "INTO OUTFILE '/tmp/x'"（后面是字符串字面量而非左括号）匹配不到 —— 已在下方
#   单独补一条不依赖 `\s*\(` 的正则。
_BLOCKED_FUNCS = re.compile(
    r"\b(dblink|dblink_exec|dblink_open|dblink_fetch|dblink_send_query|"
    r"pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|pg_ls_logdir|"
    r"pg_logdir_ls|pg_write_file|pg_rotate_logfile|pg_notify|"
    r"pg_sleep|pg_sleep_for|pg_sleep_until|"
    r"pg_advisory_lock|pg_advisory_xact_lock|"
    r"pg_cancel_backend|pg_terminate_backend|pg_reload_conf|"
    r"set_config|current_setting|"
    r"lo_import|lo_export|load_file|benchmark|sleep|shutdown|"
    r"xp_cmdshell|sp_oacreate|sp_executesql|"
    r"query_to_xml|table_to_xml)\s*\(",
    re.IGNORECASE,
)
# 文件写出：不带 `\s*\(` 约束，单独匹配（否则 "INTO OUTFILE '/path'" 逃逸）
_BLOCKED_FILE_WRITE = re.compile(
    r"\bINTO\s+(OUTFILE|DUMPFILE)\b",
    re.IGNORECASE,
)
# PG 的 SELECT ... INTO newtable（建表，无任何写操作关键词，会被SELECT 开头规则放行）
#
# 【2026-10-08 修复】原正则要求 INTO 后紧跟表名：
#     INTO\s+[A-Za-z_][A-Za-z0-9_."]*\s*(,|FROM...)
# 但 PG 允许在表名之前插入持久性修饰符，实测以下三种变体全部绕过只读闸门，
# 且在 PG侧**真的建出了表**（回滚事务内验证 SELECT count(*) FROM _evil 成功）：
#     SELECT * INTO TEMP _evil FROM t
#     SELECT * INTO TEMPORARY TABLE _evil FROM t
#     SELECT * INTO UNLOGGED _evil FROM t
# 因此这里必须先把可选修饰符（TEMP / TEMPORARY / TEMP TABLE / UNLOGGED / GLOBAL /
# LOCAL，以及它们的任意组合与重复）整体吃掉，再匹配真实表名。
#
# 修饰符清单依据 PG 文档 SELECT INTO 的persistence_clause：
#   [ [ GLOBAL | LOCAL ] { TEMPORARY | TEMP } | UNLOGGED ] TABLE [ IF NOT EXISTS ] new_table
_INTO_PERSISTENCE = r"(?:(?:GLOBAL|LOCAL)\s+)?(?:(?:TEMPORARY|TEMP)\s+)?(?:TABLE\s+)?(?:UNLOGGED\s+)?(?:TABLE\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
_BLOCKED_SELECT_INTO = re.compile(
    r"\bSELECT\b[\s\S]*?\bINTO\s+" + _INTO_PERSISTENCE + r"[A-Za-z_][A-Za-z0-9_.\"]*\s*(,|FROM\b|FROM\s)",
    re.IGNORECASE,
)
# 分号拼接多条语句：分号后还有非空白内容（允许结尾分号）
_MULTI_STMT = re.compile(r";\s*\S")


def _sanitize_sql(sql: str) -> str:
    """单遍扫描：同时剥离字符串字面量、行注释、块注释，返回纯代码骨架。

    必须**单遍**处理（不能先剥字符串再剥注释、也不能反过来）：
    - 先剥字符串再剥注释：块注释内的未闭合单引号（如 `/* 'a */ ; DROP x`）会被
      _strip_string_literals 当成字符串一路吞到行尾，`*/` 与后续危险语句随之消失，
      导致 DROP/分号被放行 —— 写操作拦截可被绕过。
    - 先剥注释再剥字符串：字符串里的 `--`/`/*` 会误判为注释截断字符串。

    单遍扫描按字符流推进，字符串与注释各自闭合互不干扰：
      - 单引号字符串（含 '' 转义）→ 替换为 'x'（内容不参与关键字检测）
      - 双引号/反引号标识符 → 原样保留（可能是表/列名，关键字检测对其有效）
      - -- 行注释、/* */ 块注释 → 删除
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":  # '' 转义
                        j += 2
                        continue
                    break
                j += 1
            out.append("'x'")
            i = j + 1
        elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
            # 行注释：跳过到行尾
            j = sql.find("\n", i)
            i = n if j == -1 else j
        elif ch == "/" and i + 1 < n and sql[i + 1] == "*":
            # 块注释：跳过到 */
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _check_sql_safety(sql: str) -> str | None:
    """安全检查，返回错误消息（None 表示通过）"""
    # 单遍剥离字符串字面量与注释（顺序敏感，见 _sanitize_sql 说明，防绕过写拦截）
    s_stripped = _sanitize_sql(sql).strip()
    if not s_stripped:
        return "SQL 为空"
    upper = s_stripped.upper()
    # 仅允许 SELECT 开头的语句（允许 WITH 前置 CTE，最终以 SELECT 结束）
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return "仅允许 SELECT 查询"
    if _MULTI_STMT.search(s_stripped):
        return "禁止一次执行多条语句"
    # 骨架已剥离字符串字面量，但双引号/反引号标识符内容保留（可能是表/列名，关键字检测对其有效）
    if _BLOCKED_KEYWORDS.search(s_stripped):
        return "SQL 包含被禁止的写操作/管理语句"
    if _BLOCKED_FUNCS.search(s_stripped):
        return "SQL 包含被禁止的危险函数"
    # 2026-10-03 补三条（原先均可绕过）：
    # ① INTO OUTFILE/DUMPFILE 任意文件写 —— 旧正则整条 alternation 末尾带 `\s*\(`，
    #    而 OUTFILE 后接的是字符串字面量（已被替换为 'x'）而非左括号 → 匹配不到。
    if _BLOCKED_FILE_WRITE.search(s_stripped):
        return "禁止 SELECT ... INTO OUTFILE/DUMPFILE（任意文件写）"
    # ② PG 的 SELECT ... INTO newtable 建表 —— 不含任何写操作关键词，能通过
    #    "仅允许 SELECT 开头" 这条检查，留下影子表占用资源。
    if _BLOCKED_SELECT_INTO.search(s_stripped):
        return "禁止 SELECT ... INTO（建表）"
    # LIMIT 取值校验：拒绝负数（PG 中 LIMIT -1 语义为「无限制」会全量排序）与超大/非法值，
    # 防止 LLM 幻觉的 LIMIT 绕过 fetchmany 前的数据库端提前停止，长时间占用数据库。
    # 校验**所有** LIMIT（多个/嵌套 CTE 里的都要查，防第一个合法后面越界的绕过）；
    # LIMIT 后非纯数字（ALL / 表达式 / 子查询）无法静态校验 → 直接拒绝。
    for m in re.finditer(r"\bLIMIT\s+([^\s,;)]+)", s_stripped, re.IGNORECASE):
        tok = m.group(1).strip()
        if tok.upper() == "ALL":
            return "LIMIT ALL 不受行数上限约束，禁止使用"
        if not re.fullmatch(r"-?\d+", tok):
            return f"LIMIT 取值非法：{tok}（仅支持 0~{SQL_MAX_ROWS} 的整数）"
        lim = int(tok)
        if lim < 0 or lim > SQL_MAX_ROWS:
            return f"LIMIT 取值须在 0~{SQL_MAX_ROWS} 之间"
    return None


def _strip_string_literals(sql: str) -> str:
    """把 SQL 中的字符串字面量替换为占位，防止其中文本触发关键字误判。

    覆盖：'...'（含 '' 转义）、"..."（标识符，保留以检测列名）、`...`（MySQL 标识符）。
    处理原则：
      - 单引号字符串：内容替换为 'x'（最常含用户文本，必须剥离）
      - 双引号/反引号标识符：内容保留（可能是表/列名，关键字检测对其有效）
    """
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":  # '' 转义
                        j += 2
                        continue
                    break
                j += 1
            out.append("'x'")
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _normalize_value(v):
    """把 Decimal / datetime / bytes / UUID / numpy 等转成可 JSON 序列化的值"""
    if v is None:
        return None
    # PG interval 类型（如 end_time - start_time）经 pg8000 返回 datetime.timedelta，
    # 它没有 isoformat、也非 bytes/UUID/numpy/Decimal，若不处理会原样进入 rows，
    # FastAPI jsonable_encoder 序列化时抛 TypeError → 接口 500。
    import datetime as _dt
    if isinstance(v, _dt.timedelta):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    # bytes / bytearray → base64 字符串（PG bytea、MySQL BLOB）
    if isinstance(v, (bytes, bytearray)):
        try:
            import base64
            return base64.b64encode(bytes(v)).decode("ascii")
        except Exception:
            return str(v)
    # UUID
    try:
        import uuid as _uuid
        if isinstance(v, _uuid.UUID):
            return str(v)
    except Exception:
        pass
    # numpy 标量/数组
    if type(v).__module__.startswith("numpy"):
        try:
            import numpy as np
            if isinstance(v, np.ndarray):
                return v.tolist()
            return v.item()
        except Exception:
            return str(v)
    if hasattr(v, "to_integral_value"):
        try:
            from decimal import Decimal
            d = Decimal(v)
            # NaN / Infinity 无法 JSON 序列化 → 转字符串
            if not d.is_finite():
                return str(d)
            if d == d.to_integral_value():
                return int(d)
            return float(d)
        except Exception:
            try:
                return float(v)
            except Exception:
                return str(v)
    return v


def _explain_estimate(conn, sql: str) -> int | None:
    """EXPLAIN 干跑，估算查询最大扫描/返回行数；失败或不支持返回 None（跳过闸门）。

    - PG：EXPLAIN (FORMAT JSON) → 递归取各节点最大 "Plan Rows"
    - MySQL：EXPLAIN → 取各表 rows 最大值
    只做规划不取数（毫秒级）；未 ANALYZE 的表估算可能不准，故阈值设高仅拦明显大扫描。
    """
    try:
        if _is_mysql():
            result = conn.execute(text(f"EXPLAIN {sql}"))
            keys = [str(k).lower() for k in result.keys()]
            if "rows" not in keys:
                return None
            est = 0
            for row in result.fetchall():
                d = dict(zip(keys, row))
                try:
                    est = max(est, int(d.get("rows") or 0))
                except (ValueError, TypeError):
                    pass
            return est if est > 0 else None
        # PostgreSQL：EXPLAIN (FORMAT JSON)
        res = conn.execute(text(f"EXPLAIN (FORMAT JSON) {sql}")).scalar()
        if isinstance(res, str):  # 部分驱动返回 JSON 字符串，需手动解析
            import json as _json
            res = _json.loads(res)
        plan = res[0]["Plan"]

        def _max_rows(node):
            rows = int(node.get("Plan Rows") or 0)
            for child in node.get("Plans") or []:
                rows = max(rows, _max_rows(child))
            return rows

        return _max_rows(plan)
    except Exception:
        # EXPLAIN 失败（复杂 CTE / 不存在的列 / 驱动不支持）会把 PG 事务标记为
        # aborted：必须立即回滚，否则同一连接后续的 SET LOCAL statement_timeout 与
        # 主 SQL 全部报 25P02「当前事务被终止」（多步调查连续执行多条 SQL 时必现）。
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def execute_sql(sql: str) -> dict:
    """
    执行 SQL 查询，返回结果。使用 SQLAlchemy engine（支持 PG/MySQL）。
    只允许 SELECT，带超时/行数上限/危险语句拦截护栏。
    """
    # ── 安全检查（先于连接，最快失败）──
    err = _check_sql_safety(sql)
    if err:
        return {
            "success": False,
            "elapsed_ms": 0,
            "row_count": 0,
            "columns": [],
            "rows": [],
            "error": err,
            "blocked": True,
        }

    t0 = time.time()
    conn = None
    try:
        effective_sql = _pg_to_mysql(sql) if _is_mysql() else sql
        truncated = False
        conn = _engine().connect()
        try:
            # EXPLAIN 干跑闸门：估算扫描行数超阈值 → 提前拒绝（省 DB 资源、避免慢查询 10s 超时白等）。
            # 被拦截后 error 会回灌 LLM 重试，引导改写为带过滤/索引的更高效 SQL。
            _est = _explain_estimate(conn, effective_sql)
            if _est is not None and _est > EXPLAIN_ROW_THRESHOLD:
                return {
                    "success": False,
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "row_count": 0,
                    "columns": [],
                    "rows": [],
                    "error": (f"查询估算扫描约 {_est:,} 行，可能存在全表扫描或缺少索引，"
                              f"请缩小时间范围或添加过滤条件后再试"),
                    "blocked": True,
                }
            if _is_mysql():
                # MySQL：MAX_EXECUTION_TIME 优化器 hint 必须紧跟 SELECT（或 WITH）之后，
                # 放在语句最前面只会被当作普通注释，超时保护不生效。
                effective_sql = _inject_mysql_timeout_hint(effective_sql, SQL_TIMEOUT_MS)
            else:
                # PG：事务内 SET LOCAL statement_timeout（随事务结束自动恢复，不污染连接池）
                conn.execute(text(f"SET LOCAL statement_timeout = {SQL_TIMEOUT_MS}"))
            result = conn.execute(text(effective_sql))
            # MySQL information_schema 返回大写列名，统一转小写
            # 2026-10-03 修复：原代码把列名一律lower() 后用 dict(zip(columns,row)) 组行。
            # dict 对重复 key 只保留最后一个 → 多表 JOIN 的同名列（如 SELECT o.order_id, c.order_id）
            # 被静默合并，每行第二个值被第一个覆盖：row_count 正常、success=True，
            # 只是某列数据悄悄变成了另一列的值（NL2SQL 最不能出的一类错）。
            # 现在保留原始列名，仅对 lower() 后的重名加序号去重。
            raw_cols = [str(c) for c in result.keys()]
            columns, _seen = [], {}
            for _c in raw_cols:
                _base = _c.lower()
                _seen[_base] = _seen.get(_base, 0) + 1
                columns.append(_c if _seen[_base] == 1 else f"{_c}_{_seen[_base]}")
            raw_rows = result.fetchmany(SQL_MAX_ROWS + 1)
            if len(raw_rows) > SQL_MAX_ROWS:
                truncated = True
                raw_rows = raw_rows[:SQL_MAX_ROWS]
            rows = [dict(zip(columns, row)) for row in raw_rows]
            # 数值/日期规范化
            for r in rows:
                for k, v in list(r.items()):
                    r[k] = _normalize_value(v) if v is not None else None
            elapsed_ms = int((time.time() - t0) * 1000)
            return {
                "success": True,
                "elapsed_ms": elapsed_ms,
                "row_count": len(rows),
                "columns": columns,
                "rows": rows,
                "truncated": truncated,
                "max_rows": SQL_MAX_ROWS,
            }
        finally:
            # 显式回滚 + 关闭：语句失败后 PG/pg8000 事务处于 aborted 态，若不回滚，
            # 连接池复用同一物理连接会级联 25P02「当前事务被终止」（多步调查连发多条
            # SQL 时必现）。SELECT 无副作用，成功时回滚与提交等价，统一回滚最安全。
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        return {
            "success": False,
            "elapsed_ms": elapsed_ms,
            "row_count": 0,
            "columns": [],
            "rows": [],
            "error": str(e),
        }


def reload_pool():
    """连接配置变更后重建连接（engine 全局共享，无需操作）"""
    pass


def get_table_row_counts() -> dict[str, int]:
    """动态获取当前数据库所有表的行数。

    PG 用 pg_class.reltuples 估算（快）；但 reltuples 对**从未 ANALYZE** 的表为 -1
    （数据导入后未做统计时普遍出现），此时回退 COUNT(*) 精确计数兜底，避免报告
    把有数据的表误报成 0 行。MySQL 用 information_schema.table_rows（一般准确）。

    兼容性：非 public schema 的表（如 factory.attendance）**同时**注册两个 key——
    `factory.attendance`（带前缀，与 get_all_tables() 表名一致）和 `attendance`
    （裸名，兼容按裸表名查询的旧调用方）。public schema 表只有裸名 key。
    此前只存裸名导致 data_charts/insight_scan/suggest 用 get_all_tables 的带前缀
    表名查 counts 全部 miss，把有数据的 factory.* 表误判为 0 行（123 库总览无图 bug）。
    """
    counts = {}
    try:
        from database import engine, get_db_type
        with engine.connect() as conn:
            if get_db_type() == "mysql":
                for (t, rows) in conn.execute(text(
                    "SELECT table_name, table_rows FROM information_schema.tables "
                    "WHERE table_schema=DATABASE()"
                )):
                    counts[t] = int(rows or 0)
            else:
                zero_tables: list[tuple[str, str]] = []
                for (s, t, rows) in conn.execute(text(
                    "SELECT n.nspname AS schema_name, c.relname AS table_name, "
                    "       c.reltuples::bigint AS row_count "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                    "AND c.relkind = 'r' ORDER BY n.nspname, c.relname"
                )):
                    r = int(rows or 0)
                    key = f"{s}.{t}" if s != "public" else t
                    if r > 0:
                        counts[key] = r
                        if s != "public":
                            counts.setdefault(t, r)
                    else:
                        # reltuples <= 0：未 ANALYZE（-1）或空表 → 精确计数兜底
                        zero_tables.append((s, t))
                for s, t in zero_tables:
                    key = f"{s}.{t}" if s != "public" else t
                    try:
                        q = f'"{s}"."{t}"' if s != "public" else f'"{t}"'
                        n = conn.execute(text(f'SELECT COUNT(*) FROM {q}')).scalar()
                        counts[key] = int(n or 0)
                    except Exception:
                        counts[key] = 0
                    finally:
                        # B5 修复：某表 COUNT 失败后 PG 事务进入 aborted 态（25P02），
                        # 不 rollback 会导致后续 COUNT 全部连坐失败置 0 → 每表独立恢复事务
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    if s != "public":
                        counts.setdefault(t, counts[key])
    except Exception:
        pass
    return counts


def fill_param_placeholders(sql: str) -> str:
    """自动填充 SQL 中的未绑定参数占位符（%s），兜底「遵循 LLM 执行」场景。

    LLM 推断生成的 SQL 草稿可能带 BETWEEN %s AND %s 之类的占位符，直接执行会报
    "A value is required for bind parameter"。这里按查询涉及表的实际日期范围
    （date/timestamp 列 min/max）按出现顺序交替填充；失败或非占位符 SQL 原样返回。
    """
    if "%s" not in sql:
        return sql
    try:
        from database import engine, quote_ident
        import re as _re
        # 标识符白名单：表名/列名进入 SQL 前必须符合，杜绝 LLM 草稿的 FROM/JOIN 注入
        _IDENT = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        # 提取 SQL 引用表（FROM/JOIN）——大小写表名都匹配（P2 修复：此前仅 [a-z_]
        # 开头，大写表名（如 FROM Orders）提取不到 → 占位符不填充 → 执行失败）
        tables = set(_re.findall(r"(?:from|join)\s+[\"`]?([A-Za-z_][A-Za-z0-9_.]*)", sql, _re.IGNORECASE))
        tables = {t.split(".")[-1] for t in tables}
        tables = {t for t in tables if _IDENT.fullmatch(t)}
        if not tables:
            return sql
        lo, hi = None, None
        with engine.connect() as conn:
            for t in tables:
                qt = quote_ident(t)
                try:
                    cols = conn.execute(text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name=:t AND data_type IN "
                        "('date','timestamp without time zone','timestamp with time zone')"
                    ), {"t": t}).fetchall()
                except Exception:
                    # 2026-10-03 修复：与本文件 get_table_row_counts 已有的 B5 修复对齐 ——
                    # PG 下任一语句失败会把事务置为 aborted(25P02)，不 rollback 则本连接上
                    # 后续所有表全部连坐失败，lo/hi 永远为 None，占位符原样返回导致必然报错。
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    continue
                if not cols:
                    continue
                # 每表一次查询聚合所有日期列 min/max（替代逐列查询，表多时显著降查询数）；
                # 列名也经白名单校验 + quote_ident，防畸形列名注入
                safe_cols = [c for (c,) in cols if _IDENT.fullmatch(str(c))]
                if not safe_cols:
                    continue
                agg = ", ".join(f"MIN({quote_ident(c)}) AS m{i}, MAX({quote_ident(c)}) AS x{i}"
                                for i, c in enumerate(safe_cols))
                try:
                    row = conn.execute(text(f"SELECT {agg} FROM {qt}")).fetchone()
                except Exception:
                    # 同上：失败后必须 rollback，否则后续表全部 25P02
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    continue
                if row is None:
                    continue
                for i, c in enumerate(safe_cols):
                    v_lo, v_hi = row[2 * i], row[2 * i + 1]
                    if v_lo is None:
                        continue
                    s_lo, s_hi = str(v_lo)[:10], str(v_hi)[:10]
                    if lo is None or s_lo < lo:
                        lo = s_lo
                    if hi is None or s_hi > hi:
                        hi = s_hi
        if not lo or not hi:
            return sql
        # 按 %s 出现顺序交替填充 start/end
        parts = sql.split("%s")
        filled = parts[0]
        for i, p in enumerate(parts[1:]):
            filled += ("'" + lo + "'" if i % 2 == 0 else "'" + hi + "'") + p
        return filled
    except Exception:
        return sql
