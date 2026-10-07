"""引擎层统一拦截（第六层核心）— MQL→SQL 之间一处改写，落地四级权限

四级粒度：
1. 数据集/模型级：allowed_tables（角色可访问的表集合；None = 不限制）
2. 列级：column_denies（无权字段，出现在 SQL 任何位置一律拒绝执行）
        column_masks（敏感字段动态脱敏，如手机号前3后4；仅允许在最外层投影出现）
3. 行级：row_filters（角色条件模板 → 渲染用户属性 → 注入 WHERE，带表别名限定）
4. 指标级：metric_overrides / denied_metrics（同一指标按角色不同口径 / 禁用）

多角色合并语义（正权限并集，宽松角色胜；缺配置 = 不限制）：
- 表：任一角色不限制 → 不限制；否则取并集
- 行：同一张表多个角色条件 OR 合并；若某个能访问该表的角色没配行条件 → 该表不注入
- 列：任一角色可见 → 明文；全部拒绝 → 拒绝；否则脱敏（取 priority 最高的脱敏规则）
- 指标：全部角色拒绝 → 拒绝；否则取 priority 最高角色的口径覆盖

安全底线（fail-close）：SQL 解析失败、行条件变量缺失、无权列被引用 → 一律拒绝执行。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

# 2026-10-04 补：模块级引入 sqlglot 的 exp。
# 起因：新增的 `_resolve_star_base_tables` 用 `exp.Subquery` / `exp.Table` 判定 AST 节点，
# 而 rewrite_sql 里的 `from sqlglot import exp` 只让它成为**该函数的局部变量**，
# 模块级函数看不到 → 函数体第一行就 NameError → 被自身的 `except Exception: return []`
# 吞掉 → 恒返回空列表 → 派生表/CTE 的 SELECT * 一律走 fail-close 拒绝。
# 即「因错误原因得到正确结果」：安全上没漏，但把本该能裁剪+脱敏的合法查询也一起拒了
# （这正是我在该函数 docstring 里专门警告过的失败模式，却自己又复现了一次）。
# 教训：模块级新增的函数若用到第三方类型，必须在该模块顶部 import，不能依赖调用方的局部 import。
from sqlglot import exp

from security import model as perm_model

# ── 脱敏算法目录（供前端下拉与后端生成 SQL 共用一份定义）──
MASK_TYPES = [
    {"key": "partial_3_4", "label": "保留前3后4（手机号）", "example": "138****8000"},
    {"key": "partial_6_4", "label": "保留前6后4（身份证）", "example": "110101****1234"},
    {"key": "partial_1_1", "label": "保留首尾各1位（姓名/客户）", "example": "张****三"},
    {"key": "hash", "label": "哈希不可逆（MD5）", "example": "e10adc39..."},
    {"key": "fixed", "label": "固定替换（***）", "example": "***"},
    {"key": "null", "label": "置空（NULL）", "example": "（空）"},
    {"key": "round_100", "label": "数值模糊化（取整到百）", "example": "1234 → 1200"},
    {"key": "year_only", "label": "日期只保留年份", "example": "2026-08-18 → 2026"},
]
MASK_KEYS = {m["key"] for m in MASK_TYPES}


@dataclass
class AclContext:
    """当前用户的生效权限（多角色合并后的结果）"""
    username: str = "guest"
    roles: list[str] = field(default_factory=list)
    attributes: dict = field(default_factory=dict)
    tenant_id: str = ""                                                 # 多租户/组织隔离维度（tenant_id/org_id）
    superuser: bool = False
    allowed_tables: set[str] | None = None
    row_filters: dict[str, str] = field(default_factory=dict)          # 表 → 已渲染 SQL 条件
    row_sources: dict[str, list[dict]] = field(default_factory=dict)   # 表 → [{role, expr}]（可解释）
    column_denies: dict[str, set[str]] = field(default_factory=dict)   # 表 → 无权列
    column_masks: dict[str, dict[str, str]] = field(default_factory=dict)  # 表 → {列: mask}
    column_mask_agg_ok: dict[str, set[str]] = field(default_factory=dict)   # 表 → 允许参与聚合的脱敏列
    column_whitelist: dict[str, set[str]] = field(default_factory=dict)    # v1 兼容白名单
    metric_overrides: dict[str, dict] = field(default_factory=dict)    # 指标 → 覆盖口径
    denied_metrics: set[str] = field(default_factory=set)
    allowed_actions: set[str] = field(default_factory=set)             # 操作级权限：{export, share, download}
    unresolved_vars: list[str] = field(default_factory=list)           # 行策略缺失的用户属性
    has_user_grants: bool = False                                      # 是否存在 per-user 表/字段授权
    strict: bool = False                                               # 严格合并模式（列 deny>mask>visible、行 AND）

    def needs_sql_rewrite(self) -> bool:
        # 表级白名单（allowed_tables 非 None）也算需要校验——否则纯表限制的角色
        # 会被短路跳过，无权表直接放行（viewer 等仅有 v1 表白名单的角色）
        return (self.allowed_tables is not None
                or bool(self.row_filters or self.column_denies or self.column_masks
                        or self.column_whitelist))

    def can_do(self, action: str) -> bool:
        """操作级权限判断：superuser 全部放行；否则看合并后的 allowed_actions"""
        return self.superuser or action in self.allowed_actions

    def as_dict(self) -> dict:
        """供前端「权限白盒」展示的摘要"""
        return {
            "username": self.username,
            "roles": self.roles,
            "superuser": self.superuser,
            "attributes": self.attributes,
            "tenant_id": self.tenant_id,
            "allowed_tables": (sorted(self.allowed_tables) if self.allowed_tables is not None else None),
            "row_filters": self.row_filters,
            "row_sources": self.row_sources,
            "column_denies": {t: sorted(c) for t, c in self.column_denies.items()},
            "column_masks": self.column_masks,
            "column_mask_agg_ok": {t: sorted(c) for t, c in self.column_mask_agg_ok.items()},
            "metric_overrides": self.metric_overrides,
            "denied_metrics": sorted(self.denied_metrics),
            "allowed_actions": sorted(self.allowed_actions),
            "unresolved_vars": self.unresolved_vars,
            "has_user_grants": self.has_user_grants,
            "strict": self.strict,
        }


# ── 用户 → 角色 / 属性 ───────────────────────────────────

def resolve_user_roles(user: dict) -> list[str]:
    """用户的角色列表：优先 roles 数组（RBAC 多角色），回退单 role 字段（兼容旧数据）"""
    roles = [str(r).strip() for r in (user.get("roles") or []) if str(r).strip()]
    if not roles:
        single = str(user.get("role") or "").strip()
        roles = [single] if single else []
    # 去重保序
    seen, out = set(), []
    for r in roles:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _extract_tenant(attributes: dict) -> str:
    """从用户属性提取租户/组织标识（tenant_id 优先，回退 org_id），供多租户组织隔离。

    对标 Cube 的 contextToAppId / Wren 的 session properties：不同组织的用户携带不同
    tenant 标识，配合行级 rule `source: "user.tenant_id"` 实现同库组织级数据隔离。
    """
    for key in ("tenant_id", "tenant", "org_id", "organization_id"):
        v = attributes.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _role_priority(role_key: str) -> int:
    r = perm_model.get_role(role_key)
    return int((r or {}).get("priority") or 0)


def _policy_tables(pol: dict) -> set[str] | None:
    """角色策略的授权表集合；None = 未配置任何授权 = 不限制"""
    keys = pol.get("datasets") or []
    extra = pol.get("tables") or []
    if not keys and not extra:
        return None
    tables = perm_model.dataset_tables(keys)
    tables |= {str(t).split(".")[-1].strip().lower() for t in extra}
    return tables


# ── 行策略模板渲染（变量注入 + 防 SQL 注入）───────────────

_VAR_RE = re.compile(r"\$\{\s*user\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}")
_SAFE_SCALAR = re.compile(r"^[\w\u4e00-\u9fa5 .:@+\-/]*$")


def _sql_literal(value) -> str:
    """把用户属性值渲染成安全 SQL 字面量（禁止注入）"""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        items = [_sql_literal(v) for v in value]
        return "(" + ", ".join(items) + ")" if items else "(NULL)"
    s = str(value)
    if not _SAFE_SCALAR.match(s):
        raise ValueError(f"用户属性值包含非法字符，已拒绝渲染：{s[:40]}")
    return "'" + s.replace("'", "''") + "'"


def render_row_expr(expr: str, username: str, attributes: dict) -> tuple[str, list[str]]:
    """渲染行条件模板。

    支持 ${user.xxx}：username / 任意用户属性（region、dept、factory_id…）。
    列表属性自动渲染成 IN 列表；缺失属性 → 该条件降级为 1=0（拒绝全部，fail-close）
    并把缺失变量名返回，供前端明确提示「请先给该用户配置 region 属性」。
    """
    missing: list[str] = []

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        val = username if key in ("username", "name") else attributes.get(key)
        if val is None or val == "":
            missing.append(key)
            return "NULL"
        return _sql_literal(val)

    rendered = _VAR_RE.sub(_sub, expr or "")
    if missing:
        return "1=0", missing
    return rendered, []


# ── 声明式行规则编译（P2：管理员免写 SQL）───────────────
# 行策略同时支持两种形态（rule 优先，expr 兼容保留）：
#   1) 简单比较  {"field": "city", "op": "eq", "source": "user.region"}   # source=user.属性 或固定值
#   2) 跨表子查询 {"op": "in_subquery", "ref_table": "test_factories",
#                 "on": {"left": "factory_id", "right": "factory_id"},
#                 "where": {"field": "city", "op": "eq", "source": "user.region"}}
# 编译产物仍是 ${user.xxx} 模板串，继续走 render_row_expr（变量渲染/防注入/缺失 fail-close）。

_COMPARE_OPS = {"eq": "=", "neq": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
_SUPPORTED_OPS = set(_COMPARE_OPS) | {"in", "contains"}


def _compile_compare(field: str, op: str, source) -> str:
    op = (op or "eq").strip().lower()
    if op not in _SUPPORTED_OPS:
        raise ValueError(f"不支持的比较操作符：{op}（支持 {'/'.join(sorted(_SUPPORTED_OPS))}）")
    field = str(field).strip()
    if not field:
        raise ValueError("规则缺少 field（过滤字段）")
    src = str(source if source is not None else "").strip()
    if src.startswith("user."):
        var = src.split(".", 1)[1]
        if op == "eq":
            return f"{field} = ${{user.{var}}}"
        if op == "neq":
            return f"{field} <> ${{user.{var}}}"
        if op == "in":
            return f"{field} IN ${{user.{var}}}"   # 列表属性（render 时渲染成 (a,b,c)）
        if op == "contains":
            return f"{field} LIKE CONCAT('%', ${{user.{var}}}, '%')"
        return f"{field} {_COMPARE_OPS[op]} ${{user.{var}}}"
    # 固定值（走 _sql_literal 安全渲染，防注入）
    if op == "in":
        vals = source if isinstance(source, (list, tuple)) else [source]
        return f"{field} IN {_sql_literal(vals)}"
    if op == "contains":
        return f"{field} LIKE CONCAT('%', {_sql_literal(source)}, '%')"
    return f"{field} {_COMPARE_OPS[op]} {_sql_literal(source)}"


def compile_row_rule(rule: dict) -> str:
    """声明式行规则 → SQL 条件模板串（含 ${user.xxx} 占位，交由 render_row_expr 渲染）。

    校验失败抛 ValueError（调用方应 fail-close 或回退 expr）。
    """
    if not isinstance(rule, dict):
        raise ValueError("行规则必须为对象")
    op = str(rule.get("op") or "eq").strip().lower()
    if op == "in_subquery":
        ref_table = str(rule.get("ref_table") or "").split(".")[-1].strip().lower()
        on = rule.get("on") or {}
        where = rule.get("where") or {}
        left = str(on.get("left") or "").strip()
        right = str(on.get("right") or "").strip()
        wf = str(where.get("field") or "").strip()
        if not (ref_table and left and right and wf):
            raise ValueError("in_subquery 规则需要 ref_table / on.left / on.right / where.field")
        wcond = _compile_compare(wf, str(where.get("op") or "eq"), where.get("source"))
        return f"{left} IN (SELECT {right} FROM {ref_table} WHERE {wcond})"
    field = str(rule.get("field") or "").strip()
    if not field:
        raise ValueError("行规则缺少 field（过滤字段）")
    return _compile_compare(field, op, rule.get("source"))


def _row_expr_of(cfg: dict) -> str:
    """行的生效条件：rule（声明式）优先编译，缺省/编译失败回退 expr（兼容存量配置）"""
    rule = cfg.get("rule")
    if isinstance(rule, dict) and rule:
        try:
            return compile_row_rule(rule)
        except ValueError:
            pass
    return str(cfg.get("expr") or "").strip()


# ── 生效权限构建 ─────────────────────────────────────────

def build_acl_context(user: dict) -> AclContext:
    """把「用户 → 角色 → 各级策略」合并成本次查询的生效权限"""
    username = str(user.get("username") or "guest")
    roles = resolve_user_roles(user)
    attributes = dict(user.get("attributes") or {})
    ctx = AclContext(username=username, roles=roles, attributes=attributes)
    ctx.tenant_id = _extract_tenant(attributes)  # 多租户/组织隔离维度
    ctx.strict = perm_model.is_strict_mode()  # 严格合并模式（默认关闭，零回归）

    if "admin" in roles:
        ctx.superuser = True
        return ctx  # 超级管理员：不加任何限制

    policies = {r: perm_model.get_policy(r) for r in roles}

    # ① 表级：任一角色不限制 → 不限制；否则并集
    table_sets = [_policy_tables(p) for p in policies.values()]
    if not table_sets or any(s is None for s in table_sets):
        role_allowed = None
    else:
        merged: set[str] = set()
        for s in table_sets:
            merged |= s
        role_allowed = merged

    # ② 员工级数据授权（per-user 表/字段白名单）：在角色授权基础上「并集」补充。
    # 例外是「角色覆盖不了的少数人」的额外授权，不覆盖角色已授的数据域；
    # 仅当角色未配置表授权（role_allowed=None 不限制）时，例外才收敛为精确控制。
    # 必须先于行/列合并就位：角色级的行过滤 / 列脱敏 / 列拒绝仍叠加生效（纵深防御），
    # 否则 per-user 授权会绕过角色对同一张表配的行/列防线。
    user_grants = perm_model.get_user_grants(username)
    if user_grants:
        grant_tables = {str(t).split(".")[-1].strip().lower() for t in user_grants.keys()}
        ctx.allowed_tables = grant_tables if role_allowed is None else (role_allowed | grant_tables)
        for t, cols in user_grants.items():
            bare = str(t).split(".")[-1].strip().lower()
            if cols:
                ctx.column_whitelist[bare] = {str(c).strip().lower() for c in cols}
        ctx.has_user_grants = True
    else:
        ctx.allowed_tables = role_allowed

    def _bare(name) -> str:
        return str(name).split(".")[-1].strip().lower()

    def _can_access(role: str, table: str) -> bool:
        s = _policy_tables(policies.get(role) or {})
        return True if s is None else (_bare(table) in s)

    def _table_visible(table: str) -> bool:
        """该表对最终用户是否可见（含 user_grants 授权）"""
        return ctx.allowed_tables is None or _bare(table) in ctx.allowed_tables

    # ③ 行级：同表多角色 OR（宽松）；strict 模式下 AND 收紧（取交集）
    #    只对「最终可见」的表注入；有权访问却未配条件的角色：宽松 → 该表不注入；严格 → 跳过
    row_tables: set[str] = set()
    for pol in policies.values():
        row_tables |= {t for t, cfg in (pol.get("rows") or {}).items() if cfg.get("enabled", True)}
    for table in sorted(row_tables):
        if not _table_visible(table):
            continue  # 用户最终不可见该表 → 不注入行过滤
        conds: list[str] = []
        sources: list[dict] = []
        unrestricted = False
        for role in roles:
            cfg = (policies[role].get("rows") or {}).get(table)
            expr = _row_expr_of(cfg) if cfg else ""
            if not cfg or not cfg.get("enabled", True) or not expr:
                # 该角色对此表无行规则：若它「能访问」该表则无限制（宽松）；否则不参与
                if _can_access(role, table):
                    if ctx.strict:
                        continue
                    unrestricted = True
                    break
                continue
            try:
                rendered, missing = render_row_expr(expr, username, attributes)
            except ValueError as e:
                rendered, missing = "1=0", [str(e)]
            if missing:
                ctx.unresolved_vars.extend(f"{table}:{m}" for m in missing)
                # fail-close（P1 修复）：规则因用户缺失属性变量无法渲染时，
                # 该角色视角拒绝该表全部行（此前保留未渲染的 ${user.xxx} 字面量，
                # 在 strict AND 合并下会把整表条件变成恒假、宽松 OR 下语义混乱）。
                rendered = "1=0"
            conds.append(rendered)
            sources.append({"role": role, "expr": expr, "rendered": rendered,
                            "rule": (cfg.get("rule") if isinstance(cfg.get("rule"), dict) else None)})
        if unrestricted or not conds:
            continue
        uniq: list[str] = []
        for c in conds:
            if c not in uniq:
                uniq.append(c)
        joiner = " AND " if ctx.strict else " OR "
        ctx.row_filters[table] = uniq[0] if len(uniq) == 1 else joiner.join(f"({c})" for c in uniq)
        ctx.row_sources[table] = sources

    # ④ 列级：可见 > 脱敏 > 拒绝（宽松角色胜）；strict 模式反转为 拒绝 > 脱敏 > 可见（最严）
    #    涉及角色 = 能访问该表的角色 ∪ 配置了该表列规则的角色（后者用于 per-user 授权表的纵深防御）
    col_tables: set[str] = set()
    for pol in policies.values():
        col_tables |= set((pol.get("columns") or {}).keys())
    for table in col_tables:
        if not _table_visible(table):
            continue
        involved = [r for r in roles
                    if _can_access(r, table) or (policies[r].get("columns") or {}).get(table)]
        if not involved:
            continue
        col_names: set[str] = set()
        for r in involved:
            col_names |= set(((policies[r].get("columns") or {}).get(table) or {}).keys())
        for col in col_names:
            verdicts: list[tuple[int, str, str, bool]] = []  # (priority, mode, mask, agg_ok)
            visible = False
            for r in involved:
                rule = ((policies[r].get("columns") or {}).get(table) or {}).get(col)
                mode = str((rule or {}).get("mode") or "visible")
                if not rule or mode == "visible":
                    visible = True
                    if ctx.strict:
                        continue   # 严格：继续收集其他角色的 deny/mask，最后按最严取值
                    break          # 宽松：任一角色可见即明文
                verdicts.append((_role_priority(r), mode,
                                 perm_model.coerce_mask_key(rule.get("mask")),
                                 bool((rule or {}).get("agg_ok"))))
            if ctx.strict:
                # 严格：拒绝 > 脱敏 > 可见（任一 deny 即 deny；否则任一 mask 即 mask）
                if any(v[1] == "deny" for v in verdicts):
                    ctx.column_denies.setdefault(table, set()).add(col.lower())
                    continue
                masks = [v for v in verdicts if v[1] == "mask"]
                if masks:
                    masks.sort(key=lambda x: x[0], reverse=True)
                    m, agg_ok = masks[0][2], masks[0][3]
                    ctx.column_masks.setdefault(table, {})[col.lower()] = (
                        m if m in MASK_KEYS or m.startswith("partial_") else "fixed")
                    if agg_ok:
                        ctx.column_mask_agg_ok.setdefault(table, set()).add(col.lower())
                continue
            if visible or not verdicts:
                continue
            if all(v[1] == "deny" for v in verdicts):
                ctx.column_denies.setdefault(table, set()).add(col.lower())
                continue
            verdicts.sort(key=lambda x: x[0], reverse=True)
            mask = next((v[2] for v in verdicts if v[1] == "mask"), "fixed")
            agg_ok = any(v[3] for v in verdicts if v[1] == "mask")
            ctx.column_masks.setdefault(table, {})[col.lower()] = (
                mask if mask in MASK_KEYS or mask.startswith("partial_") else "fixed")
            if agg_ok:
                ctx.column_mask_agg_ok.setdefault(table, set()).add(col.lower())

    # ③b v1 列白名单兼容（并集；任一角色无白名单 → 该表不裁剪）
    wl_tables: set[str] = set()
    for pol in policies.values():
        wl_tables |= set((pol.get("_whitelist") or {}).keys())
    for table in wl_tables:
        involved = [r for r in roles if _can_access(r, table)]
        merged_wl: set[str] = set()
        skip = False
        for r in involved:
            wl = (policies[r].get("_whitelist") or {}).get(table)
            if not wl:
                skip = True
                break
            merged_wl |= {str(c) for c in wl}
        if not skip and merged_wl:
            ctx.column_whitelist[table] = merged_wl

    # ④ 指标级：全部拒绝 → 拒绝；否则取 priority 最高的口径覆盖
    metric_names: set[str] = set()
    for pol in policies.values():
        metric_names |= set((pol.get("metrics") or {}).keys())
    for name in metric_names:
        rules = []
        for r in roles:
            rule = (policies[r].get("metrics") or {}).get(name)
            if rule:
                rules.append((_role_priority(r), r, rule))
        if not rules:
            continue
        if all(str(x[2].get("mode")) == "deny" for x in rules):
            ctx.denied_metrics.add(name)
            continue
        rules.sort(key=lambda x: x[0], reverse=True)
        for _, role, rule in rules:
            if str(rule.get("mode")) == "override" and rule.get("sql_expression"):
                ctx.metric_overrides[name] = {
                    "sql_expression": str(rule["sql_expression"]),
                    "formula": str(rule.get("formula") or rule["sql_expression"]),
                    "description": str(rule.get("description") or ""),
                    "by_role": role,
                }
                break

    # ⑤ 员工授权字段为权威：解除角色对「已授权字段」的列拒绝（须在列级合并之后）
    #    管理员显式授权的字段可查询，否则授权形同虚设；角色对该字段的脱敏（mask）仍保留叠加。
    if user_grants:
        for t, cols in user_grants.items():
            bare = str(t).split(".")[-1].strip().lower()
            if cols:
                wl = {str(c).strip().lower() for c in cols}
                denies = ctx.column_denies.get(bare)
                if denies:
                    ctx.column_denies[bare] = denies - wl

    # ⑤b 操作级权限：多角色并集（任一角色允许某操作 → 允许）；superuser 已在上面 return，天然全放行
    for pol in policies.values():
        actions = pol.get("actions") or {}
        for k in perm_model.ACTION_PERMS:
            if actions.get(k):
                ctx.allowed_actions.add(k)

    # ⑥ 普通员工（已登录、无角色表授权、无个人授权）→ 默认空：不展示任何表/字段
    #    guest（开放模式匿名访客）不受此限，保持原「未配置即不限制」语义。
    if ctx.allowed_tables is None and not ctx.superuser and "guest" not in roles:
        ctx.allowed_tables = set()
    return ctx


def acl_fingerprint(acl) -> str:
    """从 AclContext 计算权限指纹（缓存隔离防越权）。

    相同权限上下文的用户共享缓存；任何权限维度（角色/属性/表/行/列/指标）变化
    都会改变指纹，从而避免低权限用户命中高权限用户的缓存结果。
    """
    import hashlib
    import json
    if acl is None:
        return "anon"
    key = {
        "roles": sorted(acl.roles),
        "superuser": acl.superuser,
        "attributes": acl.attributes,
        "tenant_id": acl.tenant_id,
        "allowed_tables": sorted(acl.allowed_tables) if acl.allowed_tables is not None else None,
        "row_filters": acl.row_filters,
        "column_masks": acl.column_masks,
        "column_denies": {t: sorted(c) for t, c in acl.column_denies.items()},
        "column_whitelist": {t: sorted(c) for t, c in acl.column_whitelist.items()},
        "column_mask_agg_ok": {t: sorted(c) for t, c in acl.column_mask_agg_ok.items()},
        "metric_overrides": {k: v.get("sql_expression") for k, v in acl.metric_overrides.items()},
        "denied_metrics": sorted(acl.denied_metrics),
        "allowed_actions": sorted(acl.allowed_actions),
        "strict": acl.strict,
    }
    payload = json.dumps(key, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def build_acl_for_username(username: str) -> AclContext:
    """按用户名构建生效权限（供权限模拟器 / 生效权限查询使用）

    安全（P1 修复）：不存在用户不得回退 guest —— guest 无策略时 allowed_tables=None
    会被当作「全库可访问」，导致 effective/simulate 对任意不存在用户名返回全权 ACL。
    改为返回空白名单（allowed_tables=空集，fail-closed：任何表都无权）。
    """
    from auth import find_user
    u = find_user(username)
    if not u:
        return AclContext(username=username, roles=[], allowed_tables=set(), strict=True)
    return build_acl_context({"username": u.get("username", username),
                              "role": u.get("role", ""),
                              "roles": u.get("roles") or [],
                              "attributes": u.get("attributes") or {}})


# ── 表结构缓存（星号展开 / 列归属判断）────────────────────

_col_cache: dict[str, list[str]] = {}
_col_cache_lock = threading.Lock()
# 2026-10-03 新增：列结构查询失败的短 TTL 负缓存（避免同一 SQL 内重复放大 DB 压力）
_col_fail_cache: dict[str, float] = {}


def invalidate_column_cache() -> None:
    """清空表列结构缓存（CSV 导入/删除临时表后调用，P2 修复）。

    此前缓存不随 CSV 建/删表失效，同名临时表复用会串列结构，
    导致星号展开/列归属判断拿到过期列清单。
    """
    with _col_cache_lock:
        _col_cache.clear()


def _table_columns(table: str) -> list[str]:
    """查表的真实列名（裸表名匹配，跨 schema 取第一个命中），带进程内缓存。

    缓存 key 含当前库名（get_database_config），切库（postgres↔yans 等）后同名表
    不会命中旧库列清单，避免星号展开/列归属判定用错库的列。
    """
    bare = str(table).split(".")[-1].strip().lower()
    _db = ""
    try:
        from database import get_database_config
        _db = get_database_config().get("name") or ""
    except Exception:
        pass
    cache_key = f"{_db}:{bare}"
    with _col_cache_lock:
        if cache_key in _col_cache:
            return _col_cache[cache_key]
    cols: list[str] = []
    # 标识符白名单校验：bare 可能来自 LLM 生成的 SQL 表名（经 sqlglot 解析但未做字符集校验），
    # 若含单引号会污染 information_schema 查询；此处与 db/tools._get_real_fields 同口径校验。
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", bare):
        return cols
    try:
        from db.executor import execute_sql
        r = execute_sql(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE lower(table_name) = '{bare}' ORDER BY ordinal_position"
        )
        if r.get("success"):
            cols = [str(row.get("column_name")) for row in (r.get("rows") or [])]
    except Exception:
        cols = []
    if cols:
        with _col_cache_lock:
            _col_cache[cache_key] = cols
        return cols
    # 2026-10-03：失败结果做**短 TTL** 负缓存。原实现不缓存空结果，于是同一列的
    # 每次引用都会重新查一次 information_schema —— DB 压力大时（正是最需要权限校验
    # 生效的时刻）把压力放大 N 倍。缓存 10s 足以挡掉单条 SQL 内的重复引用，
    # 又不会像成功缓存那样把失败钉死到进程结束。
    now = time.time()
    with _col_cache_lock:
        prev_fail = _col_fail_cache.get(cache_key)
        if prev_fail and now - prev_fail < 10.0:
            return cols
        _col_fail_cache[cache_key] = now
        if len(_col_fail_cache) > 512:
            for k in [k for k, v in _col_fail_cache.items() if now - v > 10.0]:
                _col_fail_cache.pop(k, None)
    return cols


def _resolve_star_base_tables(node, cte_names: set[str] | None = None,
                              ast=None) -> list[str]:
    """解析星号所在层的**真实基表**（穿透派生表与 CTE）。

    2026-10-04 新增，配合 rewrite_sql 的星号 fail-close 使用。
    背景：星号裁剪逻辑只认「该 SELECT 层直接 FROM/JOIN 的 exp.Table」，
    遇到 `FROM (SELECT …) s`（`_frm.this` 是 exp.Subquery）或
    `FROM cte_name`（CTE 名不是受管表）时，会得出「基表未知」，
    于是 `SELECT *` 被静默放行 → deny 列不裁剪、mask 列不脱敏 → **明文泄露**
    （实测：test_orders deny unit_price / mask customer_name，
      `SELECT * FROM (SELECT * FROM test_orders) s` 两种字段都明文返回）。

    这里把 Subquery / CTE 逐层穿透，还原出最终的真实基表名，
    拿不到就返回空列表，由调用方 fail-close 拒绝。

    ⚠️ `cte_names` / `ast` 必须**由调用方显式传入**（rewrite_sql 的局部变量，
    模块级不可见）。早期版本误以为能闭包捕获，结果永远返回 []，
    变成「一律拒绝」——安全上没错但误伤合法查询，这里显式化避免该陷阱。
    """
    out: list[str] = []
    _ctes = {str(c).lower() for c in (cte_names or set())}
    _ast = ast

    def _walk(n) -> None:
        # 派生表：穿透到子查询内部
        if isinstance(n, exp.Subquery):
            for t in n.find_all(exp.Table):
                nm = (t.name or "").split(".")[-1].lower()
                if nm and nm not in _ctes and nm not in out:
                    out.append(nm)
            return
        # CTE 引用：找定义再穿透
        if isinstance(n, exp.Table):
            nm = (n.name or "").split(".")[-1].lower()
            if nm in _ctes:
                if _ast is not None:
                    for cte in _ast.find_all(exp.CTE):
                        if (cte.alias_or_name or "").lower() == nm:
                            for t2 in cte.find_all(exp.Table):
                                n2 = (t2.name or "").split(".")[-1].lower()
                                if n2 and n2 not in _ctes and n2 not in out:
                                    out.append(n2)
                return
            if nm and nm not in out:
                out.append(nm)
            return
        for ch in (getattr(n, "args", {}) or {}).values():
            if isinstance(ch, (list, tuple)):
                for x in ch:
                    if hasattr(x, "args"):
                        _walk(x)
            elif hasattr(ch, "args"):
                _walk(ch)

    try:
        _walk(node)
    except Exception:
        # 2026-10-04：原本是静默 `return []`，把「代码错误」（如缺 import 导致的 NameError）
        # 与「确实查不到基表」压成同一个结果 —— 运维看到的只是「基表无法确定」，
        # 完全猜不到是代码坏了。现在**记日志**：既保留 fail-close 的安全方向，
        # 又让这类问题在日志里说话（否则只能靠猜）。
        # 教训：本函数首版就因为 `exp` 未在模块级 import 而恒返回 []，且毫无痕迹。
        try:
            import logging as _lg
            _lg.getLogger("enforcer").warning(
                "星号基表解析失败，按 fail-close 拒绝: %s: %s",
                type(Exception).__name__, "", exc_info=True)
        except Exception:
            pass
        return []
    return out


# ── 脱敏 SQL 生成 ────────────────────────────────────────
def mask_expression(col_sql: str, mask: str, dialect: str) -> str:
    """生成脱敏后的 SQL 表达式（保持列语义，不落库、只改写查询）"""
    pg = dialect.startswith("postgres")
    m = re.match(r"^partial_(\d+)_(\d+)$", mask or "")
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if pg:
            txt = f"({col_sql})::text"
            return (f"CASE WHEN {col_sql} IS NULL THEN NULL "
                    f"WHEN length({txt}) <= {a + b} THEN '****' "
                    f"ELSE substr({txt}, 1, {a}) || '****' || right({txt}, {b}) END")
        txt = f"CAST({col_sql} AS CHAR)"
        return (f"CASE WHEN {col_sql} IS NULL THEN NULL "
                f"WHEN CHAR_LENGTH({txt}) <= {a + b} THEN '****' "
                f"ELSE CONCAT(LEFT({txt}, {a}), '****', RIGHT({txt}, {b})) END")
    if mask == "hash":
        return f"md5(({col_sql})::text)" if pg else f"MD5(CAST({col_sql} AS CHAR))"
    if mask == "null":
        return "NULL"
    if mask == "round_100":
        return f"round(({col_sql})::numeric, -2)" if pg else f"ROUND({col_sql}, -2)"
    if mask == "year_only":
        return (f"to_char(({col_sql})::date, 'YYYY')" if pg
                else f"DATE_FORMAT({col_sql}, '%Y')")
    return "'***'"  # fixed 及未知类型统一固定替换


# ── SQL 改写主入口 ───────────────────────────────────────

def _in_group_or_order(col) -> bool:
    """脱敏列是否仅用于分组/排序上下文（GROUP BY / ORDER BY）。

    分组/排序不泄露原文（输出键会被投影脱敏表达式替换），故放行；
    而 WHERE / HAVING / JOIN ON / 聚合参数等过滤计算位置仍会拒绝。
    """
    try:
        from sqlglot import exp as _e
        p = col.parent
        while p is not None:
            if isinstance(p, (_e.Group, _e.Order)):
                return True
            # 2026-10-03 修复：原实现缺了子查询边界判断（_in_agg_func 里有，
            # 这里没有）。sqlglot 的 parent 链会穿过子查询：内层 Select →
            # ScalarSubquery → 外层 Select → Group，于是子查询里的脱敏列会被误判为
            # 「仅用于分组/排序」而直接放行，明文进结果集（fail-open 数据泄露）。
            if isinstance(p, _e.Select):
                return False  # 跨过子查询边界，外层的 Group/Order 与本列无关
            p = p.parent
    except Exception:
        pass
    return False


def _in_agg_func(col) -> bool:
    """判断脱敏列是否位于聚合函数（SUM/AVG/COUNT/MIN/MAX 等）参数内。

    聚合豁免（agg_ok）依赖此判定：聚合结果不含明细原文，允许放行；
    一旦跨越子查询边界即返回 False，避免误放行。
    """
    try:
        from sqlglot import exp as _e
        p = col.parent
        while p is not None:
            if isinstance(p, _e.AggFunc):
                return True
            if isinstance(p, _e.Select):
                return False  # 进入子查询边界，不再上溯
            p = p.parent
    except Exception:
        pass
    return False


def _top_level_selects(node) -> list:
    """最外层查询链的 SELECT 节点（WITH/UNION 展开）。

    不进入子查询/派生表 —— 只有「最外层投影」是合法脱敏位置，
    子查询内的 SELECT 属于过滤/计算上下文，脱敏列出现其中应被拒绝。
    """
    try:
        from sqlglot import exp as _e
        if isinstance(node, _e.Select):
            return [node]
        if isinstance(node, _e.Union):
            out: list = []
            # Union 左右分支在 this / expression 属性（链式 UNION 时 expression 可能是 Union）
            for sub in (getattr(node, "this", None), getattr(node, "expression", None)):
                if sub is not None:
                    out.extend(_top_level_selects(sub))
            return out
        if isinstance(node, _e.With):
            return _top_level_selects(node.this)
    except Exception:
        pass
    return []


def _audit_denial(ctx: AclContext, reason: str, detail: dict | None = None) -> None:
    """越权尝试审计（fail-close 拒绝时记录），供管理员在审计日志中发现异常访问行为。

    对标观远「异常行为预警」：无权表/无权列/敏感列计算/操作权限的每次拒绝尝试都留痕，
    管理员可据此识别越权试探（如某用户反复尝试访问无权数据）。容错：审计失败不影响拒绝本身。
    """
    try:
        from auth import audit
        audit("access_denied", username=getattr(ctx, "username", ""),
              roles=getattr(ctx, "roles", []),
              tenant_id=getattr(ctx, "tenant_id", ""),
              reason=reason, detail=detail or {})
    except Exception:
        pass


# 多表同名列归属歧义哨兵（见 rewrite_sql._owner_of）——真实表名均为小写标识符，不会与之冲突
_AMBIGUOUS_OWNER = "__ambiguous_owner__"


def rewrite_sql(sql: str, ctx: AclContext | None, dialect: str | None = None) -> tuple[str, str, dict]:
    """执行前统一改写 SQL：注入行条件 / 拦截无权列 / 脱敏敏感列。

    返回 (改写后 SQL, 错误信息, 应用明细)。错误非空 = 拒绝执行（安全优先）。
    无策略时直接短路返回原 SQL（零回归、零开销）。
    """
    applied: dict = {"row_filters": [], "masked": [], "hidden": [], "denied": [], "agg_ok": []}
    if not sql or not ctx or ctx.superuser or not ctx.needs_sql_rewrite():
        return sql, "", applied

    if not dialect:
        try:
            from database import get_db_type
            dialect = "postgres" if "postgres" in (get_db_type() or "") else "mysql"
        except Exception:
            dialect = "postgres"

    try:
        import sqlglot
        from sqlglot import exp
        ast = sqlglot.parse_one(sql, read=dialect)
    except Exception as e:
        return sql, f"权限改写失败（SQL 无法解析），已拒绝执行：{e}", applied

    # ── DML/DDL 防护：权限链路只允许查询（SELECT），拒绝一切写操作 ──
    # 注意：不同 sqlglot 版本的 exp 类集合不一致（如部分版本没有 Truncate / Grant）。
    # 这里用 getattr 先把「本版本真实存在的节点类型」挑出来再逐个 find，
    # 而不是把整段防护包进 `try: ... except Exception: pass`——那种写法一旦抛异常
    # （版本差异、find 内部报错）就会静默跳过全部写操作检查，防护形同虚设，
    # 随后代码照常继续做查询改写并放行 SQL，属安全 fail-open。
    from sqlglot import exp as _exp

    _block_cls = getattr(_exp, "Block", None)
    if _block_cls is not None and isinstance(ast, _block_cls):
        return sql, "权限控制仅允许单条查询语句（SELECT），检测到多语句，已拒绝执行。", applied

    # 【2026-10-08 补 "Into"】SELECT ... INTO newtable 在 PG 里是**建表**，
    # 但它以 SELECT 开头、不含任何写操作关键词，AST 上是一个 exp.Into 节点，
    # 不在原清单里 → 权限层放行。实测 db.executor 的正则也会被 INTO TEMP 绕过，
    # 两层同时失效；且 admin 走 superuser 短路根本不进这段代码，
    # 所以这里必须补上Into 节点作为最后一道兜底。
    _danger_names = ("Insert", "Update", "Delete", "Drop", "Alter", "Create", "Merge",
                     "Command", "Grant", "Revoke", "Truncate", "Transaction",
                     "Into")
    _danger_types = tuple(
        t for t in (getattr(_exp, _n, None) for _n in _danger_names)
        if isinstance(t, type) and issubclass(t, _exp.Expression)
    )
    if not _danger_types:
        # 一个节点类型都没解析出来 = 当前 sqlglot 的 AST 结构与预期完全不符，
        # 无法完成写操作检查 → 拒绝执行（fail-close），而不是放行
        return sql, "权限校验异常（无法识别 SQL 节点类型），已拒绝执行。", applied
    for _t in _danger_types:
        if ast.find(_t) is not None:
            return sql, (f"权限控制仅允许查询语句（SELECT），检测到写操作"
                         f"（{_t.__name__}），已拒绝执行。"), applied

    try:
        # CTE 名不是物理表，参与权限判定会误伤（WITH x AS (...) SELECT * FROM x）
        cte_names: set[str] = set()
        try:
            for cte in ast.find_all(exp.CTE):
                a = cte.alias_or_name
                if a:
                    cte_names.add(a.split(".")[-1].lower())
        except Exception:
            pass

        # 别名 → 裸表名映射（全 AST）
        # 2026-10-03 修复（P0）：派生表/CTE 的列归属此前完全丢失，导致列级 deny 与
        # mask 被整段跳过（fail-open，敏感列以明文返回）。
        # 实测本项目 sqlglot 版本对 `FROM (SELECT …) s` 的 AST 形态是
        #   From(this=Subquery(Select…), alias=TableAlias(s))
        # —— 派生表**自身不是 exp.Table**，别名挂在 Subquery 上；ast.find_all(exp.Table)
        # 只会返回子查询**内部**的基表（test_orders），别名 s 在任何 Table 节点上都
        # 不存在 → alias_map 无 's' → `s.customer_name` 的 _owner_of 返回 None
        # → 862 行 `if not owner: continue` 跳过全部 deny/mask 检查。
        # 现在显式遍历 From/Join 下的 Subquery，把别名映射到其真实基表：
        # 单一基表 → 直接映射；多基表 → _AMBIGUOUS_OWNER（交调用方 fail-close 拒绝）。
        alias_map: dict[str, str] = {}
        bare_tables: set[str] = set()

        def _register(tobj) -> None:
            bare = (tobj.name or "").split(".")[-1].lower()
            if not bare or bare in cte_names:
                return
            bare_tables.add(bare)
            alias_map[(tobj.alias or tobj.name).split(".")[-1].lower()] = bare
            alias_map.setdefault(bare, bare)

        def _map_subquery(node, alias: str) -> None:
            """派生表别名 → 其子查询内用到的真实基表。"""
            key = (alias or "").split(".")[-1].lower()
            if not key:
                return
            inner: set[str] = set()
            for t in node.find_all(exp.Table):
                b = (t.name or "").split(".")[-1].lower()
                if b and b not in cte_names:
                    inner.add(b)
            if len(inner) == 1:
                alias_map[key] = next(iter(inner))
            elif len(inner) > 1:
                alias_map[key] = _AMBIGUOUS_OWNER

        for tobj in ast.find_all(exp.Table):
            _register(tobj)

        # 派生表：FROM (SELECT…) s / JOIN (SELECT…) x
        for from_node in list(ast.find_all(exp.From)) + list(ast.find_all(exp.Join)):
            src = from_node.this
            if isinstance(src, exp.Subquery):
                _map_subquery(src, src.alias or "")
        # 兜底：任何挂在 Subquery / Lateral 上的别名（形态变化时不至于漏掉）
        for sub in ast.find_all(exp.Subquery):
            if sub.alias:
                _map_subquery(sub, sub.alias)

        # 2026-10-03 修复（P0）：CTE 别名同样要能定位到真实基表。
        # `WITH x AS (SELECT * FROM test_orders) SELECT x.customer_name FROM x`
        # 中 x 命中 cte_names 被 _register 跳过，`x.customer_name` 的 owner 会是 None
        # → 整段跳过 deny/mask。这里把每个 CTE 的名字映射到它内部用到的基表。
        try:
            for cte in ast.find_all(exp.CTE):
                key = (cte.alias_or_name or "").split(".")[-1].lower()
                if not key:
                    continue
                inner: set[str] = set()
                for sub_t in cte.find_all(exp.Table):
                    b = (sub_t.name or "").split(".")[-1].lower()
                    if b and b not in cte_names:
                        inner.add(b)
                if len(inner) == 1:
                    alias_map[key] = next(iter(inner))
                elif len(inner) > 1:
                    alias_map[key] = _AMBIGUOUS_OWNER
        except Exception:
            pass

        # ── 数据集/模型级：无权表直接拒绝（含子查询/JOIN 内的表）──
        ok_tbl, denied_tbl = check_table_access(ctx, sorted(bare_tables))
        if not ok_tbl:
            applied["denied"].extend({"table": t} for t in denied_tbl)
            _audit_denial(ctx, f"无权访问表：{', '.join(denied_tbl)}")
            return sql, (f"数据集权限：当前角色无权访问表「{', '.join(denied_tbl)}」，已拒绝执行。"
                         f"如需使用，请在权限管理中为所属角色授权对应数据集。"), applied

        managed = set(ctx.column_denies) | set(ctx.column_masks)

        def _owner_of(col: "exp.Column") -> str | None:
            """判断列属于哪张受管表（带前缀用别名映射；裸列用唯一表或列归属推断）。

            安全（P0 修复）：多张受管表含同名列且其中任一表对该列配置了 deny/mask 时，
            归属不确定会给出不可预测的放行/拒绝结果（set 迭代顺序不定），存在绕过路径。
            此时返回 _AMBIGUOUS_OWNER 哨兵，由调用方 fail-close 拒绝并要求显式限定表名。
            """
            if col.table:
                return alias_map.get(col.table.lower())
            if len(bare_tables) == 1:
                return next(iter(bare_tables))
            cands = [t for t in managed & bare_tables
                     if col.name.lower() in {c.lower() for c in _table_columns(t)}]
            if len(cands) == 1:
                return cands[0]
            if len(cands) > 1:
                for t in cands:
                    if (col.name.lower() in ctx.column_denies.get(t, set())
                            or col.name.lower() in ctx.column_masks.get(t, {})):
                        return _AMBIGUOUS_OWNER
            # 2026-10-04 修复（真 bug，实测复现）：cands 为空时**不能一律判fail-close**。
            # cands 的搜索范围是 `managed & bare_tables`（只含配了 deny/mask 的表），
            # 所以「cands 为空」有两种含义截然不同的情况：
            #   (a) 该列在**任何受管表里都不存在** → 无任何 deny/mask 规则命中它
            #       → 放行是正确且安全的；
            #   (b) 该列存在于某张受管表，但我们**查不到它的列结构**
            #       （information_schema 超时/连接池忙）→ 判断不了，必须拒绝。
            # 原实现把两者都折叠成 _AMBIGUOUS_OWNER，导致 (a) 也被误拒 ——
            # 实测症状：`SELECT o.customer_name, f.city FROM test_orders o
            #   JOIN test_factories f …`（region_manager 角色，mask 只配了
            #   test_orders.customer_name）被拒，提示「字段 city 归属不明」，
            #   而报错字段甚至不在 SELECT 投影里 —— 它来自**行过滤注入的子查询**
            #   `factory_id IN (SELECT factory_id FROM test_factories WHERE city='华东')`
            #   中那个裸列 city。test_factories 并未配任何列级策略，本应放行。
            # 后果：任何「多表 + 行过滤子查询」的场景都会 fail-close 误拒，
            # 表现为「明明有权限却提示字段无权限」，且提示指向的字段与实际SQL 无关，
            # 排障方向被带偏（本次即花了几轮才定位到）。
            # 修法：cands 为空时不能一律拒绝，要看该列**归属谁**。
            # ⚠️ 第一版修法引入过 fail-open（已修正，记此以免重犯）：
            #    最初写成「列存在于任一 bare_table → 返回 None 放行」，
            #    结果 `SELECT o.city FROM test_orders o JOIN test_factories f…`
            #    （o=test_orders 不含 city，city 只在 f 上且配了 mask）被放行且
            #    **city 明文未脱敏** —— 「列存在于某张表」≠「列归属于被引用的表」。
            # 正确判据：裸列的 owner = 本次查询里**拥有该列的所有表**，
            # 由这个集合决定放行与否（而不是只看 managed 表）：
            all_owners = [t for t in bare_tables
                          if col.name.lower() in {c.lower() for c in _table_columns(t)}]
            if not all_owners:
                # 连列结构都查不到 → 判断不了 → fail-close（10-03 的安全语义，保留）
                return _AMBIGUOUS_OWNER
            # 拥有该列的表里，只要**有任何一张**配了 deny/mask → 归属不明，fail-close
            for t in all_owners:
                if (col.name.lower() in ctx.column_denies.get(t, set())
                        or col.name.lower() in ctx.column_masks.get(t, {})):
                    return _AMBIGUOUS_OWNER
            # 所有拥有该列的表都没配列级策略 → 无任何规则会命中它 → 放行正确
            return None

        # ── 行级：对直接 FROM/JOIN 了目标表的每个 SELECT 层追加条件 ──
        for table, cond in ctx.row_filters.items():
            for sel in ast.find_all(exp.Select):
                targets: list[tuple[str, str]] = []  # (bare, alias)
                frm = sel.args.get("from_") or sel.args.get("from")
                try:
                    if frm and isinstance(frm.this, exp.Table):
                        t = frm.this
                        targets.append((t.name.split(".")[-1].lower(), (t.alias or t.name).split(".")[-1]))
                    for j in sel.args.get("joins") or []:
                        jt = getattr(j, "this", None)
                        if isinstance(jt, exp.Table):
                            targets.append((jt.name.split(".")[-1].lower(), (jt.alias or jt.name).split(".")[-1]))
                except Exception:
                    continue
                for bare, alias in targets:
                    if bare != table:
                        continue
                    cond_ast = sqlglot.parse_one(cond, read=dialect)
                    # 给条件里的顶层裸列加表别名限定（多表 JOIN 防歧义）；子查询内的列不动
                    sub_ids = {id(c) for sq in cond_ast.find_all(exp.Select)
                               for c in sq.find_all(exp.Column)}
                    for c in cond_ast.find_all(exp.Column):
                        if not c.table and id(c) not in sub_ids:
                            c.set("table", exp.to_identifier(alias))
                    sel.where(cond_ast, append=True, copy=False)
                    applied["row_filters"].append({"table": table, "condition": cond,
                                                   "alias": alias})

        # ── 列级：先做全 AST 安全校验（无权列任何位置出现即拒绝）──
        # 投影位置 = 最外层查询链的投影（WITH/UNION 各分支），子查询/派生表内的
        # SELECT 不算投影 → 脱敏列出现在子查询过滤/计算中仍会被拒绝（防绕过）。
        top_selects = _top_level_selects(ast)
        outer_select = top_selects[0] if top_selects else None
        projection_col_ids: set[int] = set()
        for _sel in top_selects:
            for p in _sel.expressions:
                # 只认「投影表达式顶层就是纯列引用」（含 Alias 包装）的位置为合法脱敏展示点。
                # 用 find_all 会把 SUM(col) / CONCAT(col,..) 等表达式内部的列也算作投影，
                # 导致聚合/计算场景被误放行（如 SUM(脱敏列) 产生无意义结果）。
                inner = p.this if isinstance(p, exp.Alias) else p
                if isinstance(inner, exp.Column):
                    projection_col_ids.add(id(inner))

        mask_targets: list[tuple["exp.Expression", str, str, str]] = []  # (node, table, col, mask)
        for col in ast.find_all(exp.Column):
            owner = _owner_of(col)
            if owner == _AMBIGUOUS_OWNER:
                _audit_denial(ctx, f"字段归属歧义（多表同名列）：{col.name}")
                return sql, (f"列级权限：字段「{col.name}」在多个数据集中同名且存在敏感字段策略，"
                             f"无法确定归属，已拒绝执行。请使用「表名.列名」限定后重试。"), applied
            if not owner or owner not in managed:
                continue
            cname = col.name.lower()
            if cname in ctx.column_denies.get(owner, set()):
                applied["denied"].append({"table": owner, "column": col.name})
                _audit_denial(ctx, f"无权访问字段：{owner}.{col.name}")
                return sql, (f"列级权限：当前角色无权访问字段「{owner}.{col.name}」，已拒绝执行。"
                             f"请改用有权字段，或在权限管理中申请该字段权限。"), applied
            mask = ctx.column_masks.get(owner, {}).get(cname)
            if mask:
                if id(col) not in projection_col_ids:
                    # 仅用于分组/排序（GROUP BY / ORDER BY）→ 放行：
                    # 分组键/排序键会在投影里被脱敏表达式替换展示，不泄露原文。
                    # WHERE / HAVING / JOIN / 聚合参数等过滤计算位置仍拒绝（防推断原文）。
                    if _in_group_or_order(col):
                        continue
                    # 聚合豁免（agg_ok）：该列配置了 agg_ok 且位于聚合函数参数内 → 放行
                    # （聚合结果不含明细原文，不泄露；投影裸列 / WHERE 精确值过滤仍拒绝）
                    if (cname in ctx.column_mask_agg_ok.get(owner, set())
                            and _in_agg_func(col)):
                        applied["agg_ok"].append({"table": owner, "column": col.name})
                        continue
                    _audit_denial(ctx, f"敏感字段参与计算：{owner}.{col.name}")
                    return sql, (f"列级权限：敏感字段「{owner}.{col.name}」已配置动态脱敏，"
                                 f"仅支持在查询结果中脱敏展示，不能参与过滤/分组/聚合计算，已拒绝执行。"), applied
                mask_targets.append((col, owner, col.name, mask))

        # ── 列级：星号展开（受管表，覆盖 WITH/UNION 各顶层分支）+ 投影脱敏替换 ──
        if top_selects and managed:
            for outer_select in top_selects:
                # 只统计该 SELECT 层「直接 FROM/JOIN」的表（P2 修复）：
                # 此前用全 AST 的 bare_tables（含子查询/CTE 内表）判断，
                # 导致 `SELECT * FROM A WHERE id IN (SELECT id FROM B)` 这类
                # 合法查询被误拒（A 是受管表、B 只是子查询过滤来源）。
                #
                # 2026-10-04 自查（P0·CTE 星号泄露，**早于今日改动就存在**）：
                # `WITH x AS (SELECT * FROM test_orders) SELECT * FROM x` 里，
                # 外层 FROM 解析出的是表名 `x`（CTE 名，**不是受管表**）→
                # star_tables 为空 → 走到我今天加的「无列级策略的表放行」分支 → 明文泄露。
                #
                # 一度试过「CTE 定义里出现星号就 fail-close」，但**回归立刻抓到过度拒绝**：
                # `WITH x AS (SELECT * FROM mes_process_output) SELECT x.output_id FROM x`
                # 是完全合法的查询（外层只显式取一列），却被误拒。
                # 复盘发现该判断本身就站不住：**CTE 内部的列不会直接返回用户** ——
                # 泄露只发生在「外层也用星号」时（`SELECT * FROM x` 把 CTE 里的
                # deny/mask 列全展开出来）。所以正确做法不是禁掉 CTE 内的星号，
                # 而是让外层的基表解析**穿透 CTE 名**（下方 `_frm.this` 分支已实现）。
                layer_tables: list[str] = []
                _frm = outer_select.args.get("from_") or outer_select.args.get("from")
                if _frm and isinstance(_frm.this, exp.Table):
                    _fname = _frm.this.name.split(".")[-1].lower()
                    if _fname in cte_names:
                        # FROM 侧是 **CTE 名**（`WITH x AS (…) SELECT * FROM x`）：
                        # CTE 名不是受管表，若直接入表则 star_tables 为空 → 星号被放过 →泄露。
                        # 需穿透 CTE 定义还原真实基表。
                        _cte_base = _resolve_star_base_tables(_frm.this, cte_names, ast)
                        layer_tables.extend(_cte_base or [])
                    else:
                        layer_tables.append(_fname)
                elif _frm is not None:
                    # 2026-10-04 修复（P0·真实数据泄露，实测复现）：
                    # FROM 侧是**派生表**（`FROM (SELECT …) s`）时 `_frm.this` 是
                    # exp.Subquery 而不是 exp.Table → layer_tables 为空
                    # → star_tables 为空 → 整段星号处理被**静默跳过**。
                    # 后果是全 AST 列扫描找不到任何 exp.Column（`*` 是 exp.Star），
                    # 于是 deny 列不裁剪、mask 列不脱敏，**明文直达用户**。
                    # 实测（test_orders: deny unit_price / mask customer_name）：
                    #   SELECT * FROM (SELECT * FROM test_orders) s          → 两者明文
                    #   WITH x AS (SELECT * FROM test_orders) SELECT * FROM x→ 两者明文
                    #   SELECT x.* FROM x / 双层嵌套 / 带 LIMIT → 同样泄露
                    # 而单表 `SELECT * FROM test_orders` 正常裁剪+脱敏，
                    # 且**显式写** `SELECT s.unit_price FROM (SELECT * FROM …) s`
                    # 会被正确拒绝（走的是列扫描分支，不依赖 star_tables）——
                    # 即「星号」这条唯一不设防。
                    # 修法：解析 FROM 侧的**真实基表**（穿透 Subquery / CTE 名），
                    # 拿不到就 fail-close 拒绝，绝不静默放行。
                    layer_tables.extend(_resolve_star_base_tables(
                        _frm.this, cte_names, ast))
                for _j in outer_select.args.get("joins") or []:
                    _jt = getattr(_j, "this", None)
                    if isinstance(_jt, exp.Table):
                        layer_tables.append(_jt.name.split(".")[-1].lower())
                    elif _jt is not None:
                        layer_tables.extend(_resolve_star_base_tables(
                            _jt, cte_names, ast))
                # 去重且保持顺序
                layer_tables = list(dict.fromkeys(t for t in layer_tables if t))
                star_tables = [t for t in layer_tables if t in managed]
                new_projections = []
                changed = False
                for p in outer_select.expressions:
                    # 安全修复（P0）：sqlglot 把 `t.*` 解析为 exp.Column(this=Star())，而不是 exp.Star。
                    # 原判断只认裸 `*`，导致 `SELECT t.* FROM t` 完整绕过列级 deny/mask
                    # （已实证：`SELECT secret FROM t` 被拒，而 `SELECT t.* FROM t` 直接放行），
                    # 配了"成本价 deny / 客户名 mask"的角色只要写成 t.* 即可拿到明文全列。
                    _is_star = isinstance(p, exp.Star) or (
                        isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
                    if _is_star:
                        # 2026-10-04（P0，原修 + 当日自查修正）：
                        # 原修：把 `if _is_star and star_tables:` 改成 `if _is_star:`，
                        #   `star_tables` 为空即拒绝 —— 堵住了派生表/CTE 的星号泄露。
                        # 自查发现**过度拒绝**：`star_tables = [t for t in layer_tables if t in managed]`，
                        #   而 `managed` = 配了列级策略的表。于是**完全没配列级策略的表**
                        #   （合法且常见的配置）`star_tables` 也为空 → `SELECT * FROM test_factories`
                        #   被拒，且提示写「基表无法确定（派生表/CTE 形态）」——**与真实原因完全不符**，
                        #   会把排查的人带偏。
                        # 更糟的是策略不一致：`managed` 为空时整个 `:1064` 段直接跳过、
                        #   全部放行；`managed` 非空但表不在其中时却拒绝 —— 同样是「无策略的表」，
                        #   一个放行一个被拒。
                        # 修正为**按 layer_tables 判别**（表本身是否可确定），
                        #   与是否有列级策略分开：
                        #     · 基表可确定但无列级策略 → 放行（无需裁剪，本就无规则可违反）
                        #     · 基表可确定且属受管表→ 正常裁剪 + 脱敏
                        #     · 基表查不到（派生表/CTE 穿透失败）→ fail-close 拒绝
                        if not layer_tables:
                            return sql, ("列级权限：SELECT * 所在层的基表无法确定"
                                         "（派生表/CTE 形态），无法安全裁剪敏感字段，已拒绝执行。"
                                         "请显式列出需要的字段。"), applied
                        if not star_tables:
                            # 基表明确、但未对其配置任何列级策略 → 无规则会被违反，放行
                            changed = changed or False
                            continue
                        if len(layer_tables) != 1:
                            return sql, ("列级权限：多表查询中使用了 SELECT *，无法安全裁剪敏感字段，"
                                         "已拒绝执行。请显式列出需要的字段。"), applied
                        table = layer_tables[0]
                        cols = _table_columns(table)
                        if not cols:
                            return sql, f"列级权限：无法获取表 {table} 的列结构，已拒绝执行。", applied
                        denies = ctx.column_denies.get(table, set())
                        masks = ctx.column_masks.get(table, {})
                        for c in cols:
                            cl = c.lower()
                            if cl in denies:
                                applied["hidden"].append({"table": table, "column": c})
                                continue
                            if cl in masks:
                                expr_sql = mask_expression(_quote_col(c, dialect), masks[cl], dialect)
                                new_projections.append(
                                    exp.alias_(sqlglot.parse_one(expr_sql, read=dialect), c, quoted=True))
                                applied["masked"].append({"table": table, "column": c, "mask": masks[cl]})
                                continue
                            new_projections.append(exp.column(c))
                        changed = True
                        continue
                    new_projections.append(p)
                if changed:
                    if not new_projections:
                        return sql, "列级权限：展开后无任何可访问字段，已拒绝执行。", applied
                    outer_select.set("expressions", new_projections)
                    # 注意：不要在此处重新收集 mask_targets。
                    # star 展开生成的投影已经是「脱敏表达式 + 别名」的成品，
                    # 其内部仍含对原始列的 Column 引用；若再扫一遍会把脱敏表达式
                    # 当成待脱敏列二次套用，产生嵌套 CASE WHEN（历史 bug）。
                    # 而 star 投影本身不含 Column 节点，前面的全 AST 扫描不会把它
                    # 收进 mask_targets，因此原有 mask_targets 对未展开的显式列依然有效。

            # 显式列投影的脱敏替换（保留原列名作为别名，前端表头不变）
            for node, table, col_name, mask in mask_targets:
                col_sql = node.sql(dialect=dialect)
                expr_sql = mask_expression(col_sql, mask, dialect)
                replacement = sqlglot.parse_one(expr_sql, read=dialect)
                parent = node.parent
                if isinstance(parent, exp.Alias):
                    parent.set("this", replacement)  # 已有别名 → 只换表达式
                else:
                    node.replace(exp.alias_(replacement, col_name, quoted=True))
                applied["masked"].append({"table": table, "column": col_name, "mask": mask})

        # ── v1 白名单兼容：单表最外层投影裁剪 ──
        if ctx.column_whitelist and outer_select is not None and len(bare_tables) == 1:
            table = next(iter(bare_tables))
            wl = ctx.column_whitelist.get(table)
            if wl:
                wl_lower = {w.lower() for w in wl}
                keep: set[str] = set()
                try:
                    for og in outer_select.find_all(exp.Order, exp.Group):
                        for c in og.find_all(exp.Column):
                            # 按列名收集（忽略表前缀）：ORDER BY e.salary 也应视为引用 salary，
                            # 否则带别名前缀的列会绕过白名单（权限绕过）。
                            keep.add(c.name.lower())
                except Exception:
                    pass
                projs = []
                for p in outer_select.expressions:
                    if isinstance(p, exp.Star):
                        cols = _table_columns(table)
                        if not cols:
                            return sql, f"列级权限：无法获取表 {table} 的列结构，已拒绝执行。", applied
                        for c in cols:
                            if c.lower() in wl_lower:
                                projs.append(exp.column(c))
                            else:
                                applied["hidden"].append({"table": table, "column": c})
                        continue
                    if isinstance(p, exp.Column):
                        # 关键：按 p.name 与白名单比对，而非 `not p.table`——
                        # 否则 SELECT e.name, e.salary 这类带表别名前缀的列直接落到 projs，
                        # 白名单形同虚设（越权读白名单外列）。
                        if p.name.lower() not in wl_lower and p.name.lower() not in keep:
                            applied["hidden"].append({"table": table, "column": p.name})
                            continue
                    projs.append(p)
                if not projs:
                    return sql, f"列级权限：表 {table} 无任何可访问字段，已拒绝执行。", applied
                outer_select.set("expressions", projs)

        return ast.sql(dialect=dialect), "", applied
    except Exception as e:
        return sql, f"权限改写异常，已拒绝执行：{type(e).__name__}: {e}", applied


def _quote_col(name: str, dialect: str) -> str:
    if dialect.startswith("postgres"):
        return '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"


# ── 指标级权限（同一指标按角色不同口径）──────────────────

def apply_metric_acl(metrics: list[dict], ctx: AclContext | None = None) -> list[dict]:
    """按角色改写指标口径 / 过滤被禁用指标。

    在 metric_registry.get_effective_metrics() 一处调用即可让
    prompt 注入、确定性编译器、MQL 白盒、RAG 检索全链路口径一致。
    """
    if ctx is None:
        from security.context import get_acl
        ctx = get_acl()
    if not ctx or ctx.superuser or (not ctx.metric_overrides and not ctx.denied_metrics):
        return metrics
    out: list[dict] = []
    for m in metrics:
        name = m.get("name")
        if name in ctx.denied_metrics:
            continue
        ov = ctx.metric_overrides.get(name)
        if ov:
            m = dict(m)
            m["sql_expression"] = ov["sql_expression"]
            m["formula"] = ov.get("formula") or ov["sql_expression"]
            if ov.get("description"):
                m["description"] = ov["description"]
            m["_acl_override_by_role"] = ov.get("by_role", "")
        out.append(m)
    return out


def check_table_access(ctx: AclContext | None, tables: list[str]) -> tuple[bool, list[str]]:
    """数据集/模型级校验：返回 (是否全部有权, 被拒绝的表)"""
    if not ctx or ctx.superuser or ctx.allowed_tables is None:
        return True, []
    denied = [t for t in tables
              if str(t).split(".")[-1].lower() not in ctx.allowed_tables]
    return (not denied), denied
