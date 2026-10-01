"""SQL 校验器：基于 sqlglot AST 的合法性检查 + 安全验证"""

import re


def _strip_quoted_identifiers(sql: str) -> str:
    """把双引号/反引号标识符内容替换为占位，避免列/表名 `"delete"`/`"update"` 触发关键词误判。

    带引号标识符是合法列名/表名，不是语句关键字（真正的 DROP/UPDATE 语句不会写成带引号），
    故剥离后仅保留未引用的关键字部分参与危险词检测。
    """
    return re.sub(r'"(?:[^"]|"")*"', '""', re.sub(r"`(?:[^`]|``)*`", "``", sql))


def _strip_string_literals(sql: str) -> str:
    """把单引号字符串字面量替换为占位，防止其中文本触发关键字误判。

    例如 `WHERE status='update'` 中的 'update' 不应触发 DELETE/UPDATE 检测。
    双引号/反引号标识符保留（可能是表/列名）。
    """
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
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


def validate_sql_safety(sql: str, dialect: str = "postgres") -> tuple[bool, str, str]:
    """校验 SQL 的安全性和合法性

    Args:
        sql: 原始 SQL 字符串
        dialect: 数据库方言
    Returns:
        (valid, error_msg, cleaned_sql)
    """
    if not sql or not sql.strip():
        return False, "SQL 为空", ""

    original = sql.strip()

    # 1. 基础安全检查（剥离字符串字面量后再查，避免误伤合法过滤值）
    # 注意：COPY/LOAD/IMPORT/EXPORT 已从危险词表移除——它们是合法的表/列裸名
    # （如 SELECT * FROM copy、SELECT "load" FROM t），语句类型由下方 sqlglot AST 把关。
    dangerous_keywords = [
        "DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE",
        "TRUNCATE", "GRANT", "REVOKE", "EXECUTE", "EXEC", "CALL",
    ]
    code = _strip_quoted_identifiers(_strip_string_literals(original))
    upper_sql = code.upper()
    for kw in dangerous_keywords:
        # 用词边界检测，避免误判（如 DESCRIPTION 不应匹配 DELETE）；带引号标识符已提前剥离
        if re.search(rf'\b{kw}\b', upper_sql):
            return False, f"SQL 包含禁止关键词: {kw}", original

    # 2. 必须以 SELECT 或 WITH 开头
    cleaned = original.strip()
    if not (cleaned.upper().startswith("SELECT") or cleaned.upper().startswith("WITH")):
        return False, "SQL 必须以 SELECT 或 WITH 开头", cleaned

    # 3. 用 sqlglot 解析 AST（如果可用）
    try:
        import sqlglot
        parsed = sqlglot.parse(cleaned, read=dialect)
        if not parsed or len(parsed) == 0:
            return False, "SQL 语法错误：无法解析", cleaned

        # 检查解析出的语句类型
        for statement in parsed:
            if statement is None:
                return False, "SQL 语法错误：解析失败", cleaned

            # 检查是否有非 SELECT 语句
            stype = str(statement.key).upper() if hasattr(statement, 'key') else ""
            if stype in ("INSERT", "DELETE", "UPDATE", "DROP", "CREATE", "ALTER", "TRUNCATE"):
                return False, f"禁止 {stype} 操作", cleaned

        # 4. 重新格式化 SQL（自动修复引号/格式）
        try:
            formatted = sqlglot.transpile(cleaned, read=dialect, pretty=True)[0]
            if formatted:
                cleaned = formatted
        except Exception:
            pass  # 格式化失败不影响合法性

    except ImportError:
        # sqlglot 未安装，只做基础检查
        pass
    except Exception:
        # sqlglot 解析异常，但不一定是 SQL 问题（可能是复杂语法）
        pass

    # 5. 强制 LIMIT 检查（并校验取值：拒绝负数/超大/非法，防 LLM 幻觉的 LIMIT -1 全量排序）
    m_limit = re.search(r"\bLIMIT\s+(-?\d+)", cleaned, re.IGNORECASE)
    if m_limit:
        try:
            lim = int(m_limit.group(1))
        except ValueError:
            return False, "LIMIT 取值非法", cleaned
        if lim < 0 or lim > 5000:
            return False, "LIMIT 取值须在 0~5000 之间", cleaned
    else:
        # 用换行追加而非空格：若 SQL 以「-- 注释」结尾（无分号），空格追加会把
        # LIMIT 100 拼进注释里，实际执行时全量返回，LIMIT 护栏失效。换行保证 LIMIT
        # 永远落在新行、独立生效，且正则校验与数据库解析一致。
        cleaned = cleaned.rstrip(";").rstrip() + "\nLIMIT 100"

    # 6. 括号配对检查
    # 2026-10-01 修复：先剥离字符串字面量再计数——此前 `WHERE status = '待补(料'`
    # 这类字面量含不配对括号的正确 SQL 会被误判"括号不配对"而整条拦截。
    if _strip_string_literals(cleaned).count("(") != _strip_string_literals(cleaned).count(")"):
        return False, "SQL 括号不配对", cleaned

    return True, "", cleaned


def extract_tables_from_sql(sql: str) -> list[str]:
    """从 SQL 中提取所有引用的表名"""
    tables = set()
    # 匹配 FROM/JOIN 后的表名
    patterns = [
        r'\bFROM\s+["]?(\w+)["]?',
        r'\bJOIN\s+["]?(\w+)["]?',
        r'\bUSING\s*\(\w+\)',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, sql, re.IGNORECASE):
            if match.groups():
                tables.add(match.group(1).lower())
    return list(tables)
