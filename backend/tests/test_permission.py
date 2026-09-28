"""权限系统（语义层 + MQL→SQL 权限控制）单元测试。

覆盖：
- T1  模型层：load_model 结构 / 角色 CRUD / 数据集 CRUD / 策略节 / 敏感标记
- T2  引擎层：build_acl_context 多角色合并（admin→superuser；行 OR 合并；列优先级）
- T3  引擎层：行策略变量渲染 + 缺失变量 fail-close(1=0) + SQL 注入防护
- T4  引擎层：rewrite_sql 行注入 / 列脱敏 / 列拒绝 / 无权表拒绝 / star 展开 / join 别名限定
- T5  指标层：apply_metric_acl override / deny / 默认放行
- T6  审批流：submit_change 非敏感直接落地 / 敏感+非admin→pending / 敏感+admin→auto_approved / 驳回
- T7  auth 多角色：get_user_roles / set_user_roles / set_user_attributes / get_current_user 透传

运行：cd backend && python tests/test_permission.py
注意：会临时改写 auth_permissions.json / auth_users.json（结束后自动恢复）。
"""

import os
import sys
import json
import shutil

os.environ.setdefault("PERMISSION_VCS", "0")  # 测试环境关闭权限自动提交，避免污染 git 历史

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from security import model as perm_model
from security import enforcer, approval
from security.context import get_acl, set_acl, clear_acl
from agent.metric_registry import get_effective_metrics


# ── 备份 / 恢复（不污染真实配置）────────────────────────
_PERM_FILE = os.path.join(BACKEND, "auth_permissions.json")
_USERS_FILE = os.path.join(BACKEND, "auth_users.json")
_APPROVALS_FILE = os.path.join(BACKEND, "auth_approvals.json")
_BAK = {}


def _backup():
    for f in (_PERM_FILE, _USERS_FILE, _APPROVALS_FILE):
        if os.path.exists(f):
            _BAK[f] = open(f, encoding="utf-8").read()


def _restore():
    for f, content in _BAK.items():
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(content)
    perm_model.load_model(refresh=True)
    enforcer.invalidate_column_cache()


# ═══════════ T1 模型层 ═══════════

def test_model_layer():
    m = perm_model.load_model()
    assert int(m.get("version") or 0) >= 2, m.get("version")
    roles = {r["key"] for r in m["roles"]}
    assert {"admin", "analyst", "viewer"}.issubset(roles), roles
    assert "region_manager" in roles, roles
    dss = {d["key"] for d in m["datasets"]}
    assert {"prod_core", "quality", "equipment", "inventory", "sales"}.issubset(dss), dss
    pol = perm_model.get_policy("region_manager")
    assert "test_orders" in pol["rows"], pol
    assert "customer_name" in pol["columns"]["test_orders"], pol
    assert "产量" in pol["metrics"], pol
    assert "test_materials.unit_cost" in perm_model.sensitive_columns()
    assert perm_model.is_sensitive_change("column", "test_materials.unit_cost")
    print("T1 模型层 OK")

    # 角色 CRUD（自定义角色）
    perm_model.upsert_role({"key": "t_tester", "name": "测试角色", "priority": 35}, actor="unit")
    assert perm_model.get_role("t_tester")["priority"] == 35
    # 策略节
    perm_model.set_role_section("t_tester", "rows", {"test_orders": {"expr": "1=1", "enabled": True}}, actor="unit")
    assert perm_model.get_policy("t_tester")["rows"]["test_orders"]["expr"] == "1=1"
    # 数据集 CRUD
    perm_model.upsert_dataset({"key": "t_ds", "name": "测试集", "tables": ["test_orders"], "sensitive": True}, actor="unit")
    assert "t_ds" in perm_model.sensitive_datasets()
    assert perm_model.dataset_tables(["t_ds"]) == {"test_orders"}
    # 敏感标记
    perm_model.set_sensitive("columns", ["test_orders.customer_name"], actor="unit")
    assert perm_model.sensitive_columns() == {"test_orders.customer_name"}
    # 删除
    perm_model.delete_dataset("t_ds", actor="unit")
    perm_model.delete_role("t_tester", actor="unit")
    assert perm_model.get_role("t_tester") is None
    print("T1 写操作 OK（角色/策略/数据集/敏感，已清理）")


# ═══════════ T2 引擎层：ACL 合并 ═══════════

def test_acl_merge():
    # admin → superuser 无限制
    ctx = enforcer.build_acl_context({"username": "root", "role": "admin",
                                      "roles": ["admin"], "attributes": {}})
    assert ctx.superuser is True and ctx.allowed_tables is None
    # 区域销售经理：行策略注入 + 客户名脱敏；test_materials 不在授权数据集 → 表级拒绝
    ctx = enforcer.build_acl_context({"username": "rm", "role": "region_manager",
                                      "roles": ["region_manager"], "attributes": {"region": "华东"}})
    d = ctx.as_dict()
    assert ctx.superuser is False
    assert "test_orders" in d["row_filters"], d["row_filters"]
    assert "city = '华东'" in d["row_filters"]["test_factories"]
    assert d["column_masks"]["test_orders"]["customer_name"] == "partial_1_1"
    assert "test_materials" not in (d["allowed_tables"] or []), d["allowed_tables"]
    # 指标覆盖
    ov = ctx.metric_overrides.get("产量")
    assert ov and "good_qty + defect_qty" in ov["sql_expression"], ov
    # viewer：存在表级白名单限制（受限角色），无权访问销售/库存域的表
    ctx2 = enforcer.build_acl_context({"username": "v", "role": "viewer",
                                       "roles": ["viewer"], "attributes": {}})
    d2 = ctx2.as_dict()
    assert d2["allowed_tables"] is not None, d2["allowed_tables"]
    assert "test_orders" not in d2["allowed_tables"], d2["allowed_tables"]
    assert "test_materials" not in d2["allowed_tables"], d2["allowed_tables"]
    # 列拒绝路径：给 analyst 临时配置 test_materials.unit_cost deny（analyst 有该表访问权）
    perm_model.set_role_section("analyst", "columns",
                                {"test_materials": {"unit_cost": {"mode": "deny", "note": "测试"}}},
                                actor="unit")
    ctx3 = enforcer.build_acl_context({"username": "a", "role": "analyst",
                                       "roles": ["analyst"], "attributes": {}})
    d3 = ctx3.as_dict()
    assert "test_materials" in d3["column_denies"] and "unit_cost" in d3["column_denies"]["test_materials"], d3["column_denies"]
    # 普通员工（无角色表授权、无个人授权）→ 默认空（allowed_tables 为空集合）
    # 用隔离的无授权角色测试，不依赖 viewer 的真实配置（viewer 可能已被快捷向导配置了 datasets）
    perm_model.upsert_role({"key": "t_empty", "name": "空授权测试", "priority": 10}, actor="unit")
    try:
        ctx4 = enforcer.build_acl_context({"username": "v", "role": "t_empty",
                                           "roles": ["t_empty"], "attributes": {}})
        d4 = ctx4.as_dict()
        assert d4["allowed_tables"] is not None and len(d4["allowed_tables"]) == 0, d4["allowed_tables"]
        assert d4["has_user_grants"] is False
    finally:
        perm_model.delete_role("t_empty", actor="unit")
    print("T2 ACL 合并 OK")


def test_acl_row_render():
    # 变量渲染
    expr = enforcer.render_row_expr("city = ${user.region} AND dept = ${user.dept}",
                                    "u1", {"region": "华东", "dept": "销售二部"})
    assert expr[0] == "city = '华东' AND dept = '销售二部'", expr
    # 缺失变量 → fail-close 1=0
    expr = enforcer.render_row_expr("city = ${user.region}", "u1", {})
    assert "1=0" in expr[0], expr
    # SQL 注入：属性含非法字符（引号等）→ 抛 ValueError，调用方 fail-close 为 1=0
    try:
        enforcer.render_row_expr("city = ${user.region}", "u1", {"region": "华东' OR '1'='1"})
        assert False, "含非法字符的属性应被拒绝渲染"
    except ValueError:
        pass
    print("T3 行策略渲染 OK")


# ═══════════ T3 引擎层：SQL 改写 ═══════════

def _rm_ctx():
    return enforcer.build_acl_context({"username": "rm", "role": "region_manager",
                                       "roles": ["region_manager"], "attributes": {"region": "华东"}})


def test_rewrite_row_and_mask():
    ctx = _rm_ctx()
    sql, err, info = enforcer.rewrite_sql(
        "SELECT customer_name, unit_price FROM test_orders LIMIT 5", ctx, dialect="postgres")
    assert not err, err
    assert "SUBSTRING" in sql and "'****'" in sql, sql  # 客户名脱敏
    assert "test_orders.factory_id IN" in sql, sql       # 行过滤注入
    assert "WHERE" in sql, sql
    print("T4a 行注入 + 列脱敏 OK:", sql[:120], "...")


def test_rewrite_deny():
    ctx = _rm_ctx()
    # region_manager 无权访问 test_materials → 拒绝
    sql, err, info = enforcer.rewrite_sql("SELECT unit_cost FROM test_materials", ctx, dialect="postgres")
    assert err and ("拒绝" in err or "无权" in err), (sql, err)
    print("T4b 无权表拒绝 OK:", err)


def test_rewrite_join_alias():
    ctx = _rm_ctx()
    sql, err, info = enforcer.rewrite_sql(
        "SELECT o.customer_name, f.city FROM test_orders o JOIN test_factories f ON o.factory_id = f.factory_id",
        ctx, dialect="postgres")
    assert not err, err
    # 脱敏：表达式内部引用原列（正常），但输出别名不泄漏表前缀
    assert "SUBSTRING" in sql and 'AS "customer_name"' in sql, sql
    # 行过滤按表别名限定注入（f.city 限定 test_factories；o.factory_id 限定 test_orders）
    assert "f.city = '华东'" in sql, sql
    assert "o.factory_id IN" in sql, sql
    print("T4c join 别名行限定 OK")


def test_rewrite_star():
    ctx = _rm_ctx()
    sql, err, info = enforcer.rewrite_sql("SELECT * FROM test_orders", ctx, dialect="postgres")
    assert not err, err
    assert "customer_name" in sql and "SUBSTRING" in sql, sql
    assert "SELECT *" not in sql, sql  # 星号已展开（脱敏串里的 **** 除外）
    print("T4d star 展开 + 脱敏 OK")


def test_rewrite_cte():
    ctx = _rm_ctx()
    # 脱敏列出现在 CTE 定义（非外层投影）→ fail-close 拒绝（安全优先，宁可拒绝不错放）
    sql, err, info = enforcer.rewrite_sql(
        "WITH x AS (SELECT customer_name FROM test_orders) SELECT * FROM x", ctx, dialect="postgres")
    assert err and "脱敏" in err, (sql, err)
    # 非敏感列的 CTE 正常放行（CTE 名不误伤为表）
    sql2, err2, info2 = enforcer.rewrite_sql(
        "WITH x AS (SELECT order_id FROM test_orders) SELECT order_id FROM x", ctx, dialect="postgres")
    assert not err2, err2
    print("T4e CTE 处理 OK（敏感列 fail-close / 普通列放行）")


def test_rewrite_table_only_acl():
    """未授权普通员工（无个人授权、无角色表授权）→ 全部表拒绝（默认空）
    用隔离的无授权角色测试，不依赖 viewer 真实配置。"""
    perm_model.upsert_role({"key": "t_empty2", "name": "空授权测试2", "priority": 10}, actor="unit")
    try:
        ctx = enforcer.build_acl_context({"username": "v", "role": "t_empty2",
                                          "roles": ["t_empty2"], "attributes": {}})
        sql, err, info = enforcer.rewrite_sql("SELECT * FROM test_orders", ctx, dialect="postgres")
        assert err and "无权访问表" in err, (sql, err)
        sql2, err2, info2 = enforcer.rewrite_sql("SELECT * FROM dim_product LIMIT 2", ctx, dialect="postgres")
        assert err2 and "无权访问表" in err2, (sql2, err2)
    finally:
        perm_model.delete_role("t_empty2", actor="unit")
    print("T4f 未授权员工默认空（全部表拒绝）OK")


def test_rewrite_dml_guard():
    """DML/DDL 与多语句防护：权限链路只允许 SELECT"""
    ctx = _rm_ctx()
    for bad in ("DELETE FROM test_orders WHERE order_id=1",
                "UPDATE test_orders SET status='x'",
                "DROP TABLE test_orders",
                "INSERT INTO test_orders(order_id) VALUES(1)",
                "SELECT * FROM test_orders; DROP TABLE test_orders"):
        sql, err, info = enforcer.rewrite_sql(bad, ctx, dialect="postgres")
        assert err and "仅允许" in err, (bad, err)
    print("T4g DML/多语句防护 OK")


def test_rewrite_mask_position():
    """脱敏列位置规则：
    - 拒绝：WHERE / HAVING / JOIN ON / 聚合参数 / 表达式拼接（防推断原文）
    - 放行：纯投影（脱敏展示）、GROUP BY / ORDER BY（分组/排序键经投影脱敏）
    """
    ctx = _rm_ctx()
    for bad in ("SELECT COUNT(*) FROM test_orders WHERE customer_name LIKE %s",
                "SELECT order_id FROM test_orders WHERE customer_name = %s",
                "SELECT SUM(customer_name) FROM test_orders",
                "SELECT customer_name || 'X' FROM test_orders",
                "SELECT a.order_id FROM test_orders a JOIN test_orders b ON a.customer_name = b.customer_name"):
        sql, err, info = enforcer.rewrite_sql(bad, ctx, dialect="postgres")
        assert err and "脱敏" in err, (bad, err)
    # 纯投影 / 别名投影：放行并脱敏
    for good in ("SELECT customer_name FROM test_orders",
                 "SELECT customer_name AS c FROM test_orders"):
        sql, err, info = enforcer.rewrite_sql(good, ctx, dialect="postgres")
        assert not err and "SUBSTRING" in sql, (good, sql)
    # 分组 / 排序：放行（投影键脱敏展示）
    sql, err, info = enforcer.rewrite_sql(
        "SELECT customer_name, SUM(quantity*unit_price) AS amt FROM test_orders GROUP BY customer_name",
        ctx, dialect="postgres")
    assert not err and "SUBSTRING" in sql and "GROUP BY customer_name" in sql, (sql, err)
    sql2, err2, info2 = enforcer.rewrite_sql(
        "SELECT order_id FROM test_orders ORDER BY customer_name", ctx, dialect="postgres")
    assert not err2, (sql2, err2)
    print("T4h 脱敏列位置校验 OK")


def test_rewrite_union():
    """UNION 各分支都要脱敏（显式列与 star）"""
    ctx = _rm_ctx()
    sql, err, info = enforcer.rewrite_sql(
        "SELECT customer_name FROM test_orders UNION ALL SELECT customer_name FROM test_orders",
        ctx, dialect="postgres")
    assert not err, err
    assert sql.count("SUBSTRING") == 2, sql
    sql2, err2, info2 = enforcer.rewrite_sql(
        "SELECT * FROM test_orders UNION ALL SELECT * FROM test_orders",
        ctx, dialect="postgres")
    assert not err2, err2
    assert sql2.count("SUBSTRING") == 2 and "SELECT *" not in sql2, sql2
    print("T4i UNION 双分支脱敏 OK")


# ═══════════ T4 指标层 ═══════════

def test_metric_acl():
    ctx = _rm_ctx()
    metrics = get_effective_metrics()
    assert metrics, "指标列表为空"
    filtered = enforcer.apply_metric_acl(metrics, ctx)
    names = {m["name"] for m in filtered}
    assert "产量" in names
    prod = next(m for m in filtered if m["name"] == "产量")
    assert "good_qty + defect_qty" in prod["sql_expression"], prod
    print("T5 指标口径覆盖 OK: 产量 →", prod["sql_expression"])


# ═══════════ T5 审批流 ═══════════

def test_approval_flow():
    # 非敏感变更 → 直接落地
    res = approval.submit_change({"action": "set_role_section", "role": "viewer",
                                  "section": "rows", "value": {"dim_product": {"expr": "1=1", "enabled": True}}},
                                 actor="admin", actor_roles=["admin"], reason="unit-test")
    assert res["pending"] is False, res
    # 敏感变更 + 非 admin → 待审批单
    res = approval.submit_change({"action": "set_role_section", "role": "viewer",
                                  "section": "columns",
                                  "value": {"test_orders": {"customer_name": {"mode": "mask", "mask": "partial_3_4"}}}},
                                 actor="analyst", actor_roles=["analyst"], reason="需要客户名")
    assert res["pending"] is True and res["request"]["status"] == "pending", res
    req_id = res["request"]["id"]
    # 敏感变更 + admin → 自动通过
    res = approval.submit_change({"action": "set_sensitive", "kind": "metrics", "items": ["产量"]},
                                 actor="admin", actor_roles=["admin"], reason="unit")
    assert res["pending"] is False, res
    # 审批通过 → 落地；再次提交的申请单
    res = approval.submit_change({"action": "set_sensitive", "kind": "columns",
                                  "items": ["test_orders.unit_price"]},
                                 actor="analyst", actor_roles=["analyst"], reason="x")
    rid = res["request"]["id"]
    item = approval.review_request(rid, True, reviewer="admin", comment="同意")
    assert item["status"] == "approved", item
    assert "test_orders.unit_price" in perm_model.sensitive_columns()
    # 审批驳回
    res = approval.submit_change({"action": "set_sensitive", "kind": "columns",
                                  "items": ["test_orders.customer_name"]},
                                 actor="analyst", actor_roles=["analyst"], reason="x")
    rid = res["request"]["id"]
    item = approval.review_request(rid, False, reviewer="admin", comment="不同意")
    assert item["status"] == "rejected", item
    # 变更历史已记录
    hist = approval.get_change_history(limit=50)
    actions = {h["action"] for h in hist}
    assert "set_sensitive" in actions
    print("T6 审批流 OK（落地/待审批/自动通过/驳回/历史）")


# ═══════════ T6 auth 多角色 ═══════════

def test_auth_multi_role():
    from auth import (get_user_roles, set_user_roles, set_user_attributes,
                      get_current_user, create_token)
    # 先备份 admin 的现有角色，测试后恢复
    u = perm_model  # noqa
    import auth as auth_mod
    users = auth_mod.load_users()
    admin_bak = None
    for x in users:
        if x["username"] == "admin":
            admin_bak = {"roles": x.get("roles"), "role": x["role"], "attributes": x.get("attributes")}
            break
    try:
        set_user_roles("admin", ["admin", "region_manager"])
        u_doc = auth_mod.find_user("admin")
        assert get_user_roles(u_doc) == ["admin", "region_manager"], get_user_roles(u_doc)
        set_user_attributes("admin", {"region": "华东"})
        # get_current_user 透传 roles / attributes
        token = create_token("admin", "admin")
        cu = get_current_user("Bearer " + token)
        assert "admin" in cu["roles"] and "region_manager" in cu["roles"], cu
        assert cu["attributes"].get("region") == "华东", cu
        # 兼容旧数据：无 roles 数组时回退单 role
        legacy = {"username": "viewer", "role": "viewer"}
        assert get_user_roles(legacy) == ["viewer"]
        print("T7 auth 多角色 OK")
    finally:
        if admin_bak is not None:
            set_user_roles("admin", admin_bak["roles"] or [admin_bak["role"]])
            set_user_attributes("admin", admin_bak["attributes"] or {})


# ═══════════ ContextVar 上下文 ═══════════

def test_context_var():
    ctx = _rm_ctx()
    clear_acl()
    assert get_acl() is None
    set_acl(ctx)
    assert get_acl() is ctx
    clear_acl()
    assert get_acl() is None
    print("T8 ContextVar OK")


# ═══════════ T9 员工级 per-user 数据授权 ═══════════

def test_user_grants():
    """per-user 表/字段白名单：授权表与角色授权表「并集」（例外是补充，不覆盖角色）；
    字段白名单生效；未授权字段被拒；管理员显式授权字段可覆盖角色级列拒绝（授权为权威）。
    用隔离的空授权角色测试，不依赖 viewer 真实配置。"""
    perm_model.upsert_role({"key": "t_grants", "name": "授权测试", "priority": 10}, actor="unit")
    try:
        perm_model.set_user_grants("emp1",
            {"test_orders": ["order_id", "customer_name", "quantity"]}, actor="unit")
        ctx = enforcer.build_acl_context({"username": "emp1", "role": "t_grants",
                                          "roles": ["t_grants"], "attributes": {}})
        d = ctx.as_dict()
        assert d["has_user_grants"] is True
        # 隔离角色 datasets 空 → 并集后仅 test_orders
        assert d["allowed_tables"] == ["test_orders"], d["allowed_tables"]
        assert "test_orders" in ctx.column_whitelist, ctx.column_whitelist
        wl = {c.lower() for c in ctx.column_whitelist["test_orders"]}
        assert wl == {"order_id", "customer_name", "quantity"}, wl
        sql, err, info = enforcer.rewrite_sql(
            "SELECT order_id, customer_name FROM test_orders", ctx, dialect="postgres")
        assert not err, (sql, err)
        sql2, err2, info2 = enforcer.rewrite_sql(
            "SELECT unit_price FROM test_orders", ctx, dialect="postgres")
        assert err2 and ("无权" in err2 or "拒绝" in err2), (sql2, err2)
        sql3, err3, info3 = enforcer.rewrite_sql(
            "SELECT * FROM test_materials", ctx, dialect="postgres")
        assert err3 and "无权访问表" in err3, (sql3, err3)
        print("T9 per-user 授权 OK")

        # ── per-user 显式授权字段 覆盖 角色级列拒绝（管理员授权为权威）──
        perm_model.set_role_section("t_grants", "columns",
                                    {"test_materials": {"unit_cost": {"mode": "deny", "note": "测试"}}},
                                    actor="unit")
        perm_model.set_user_grants("emp1", {"test_materials": ["unit_cost"]}, actor="unit")
        ctx2 = enforcer.build_acl_context({"username": "emp1", "role": "t_grants",
                                           "roles": ["t_grants"], "attributes": {}})
        assert ctx2.column_whitelist.get("test_materials") == {"unit_cost"}, ctx2.column_whitelist
        assert "unit_cost" not in ctx2.column_denies.get("test_materials", set()), \
            ctx2.column_denies  # 角色 deny 已被解除
        sql4, err4, info4 = enforcer.rewrite_sql(
            "SELECT unit_cost FROM test_materials", ctx2, dialect="postgres")
        assert not err4, (sql4, err4)  # 授权字段可查询
        # 未授权的其他字段仍被拒绝（test_materials 的其他列不在白名单 → 白名单裁剪）
        sql5, err5, info5 = enforcer.rewrite_sql(
            "SELECT material_name FROM test_materials", ctx2, dialect="postgres")
        assert err5, (sql5, err5)
        print("T9 授权字段覆盖角色 deny OK")

        # 撤销 per-user 授权后回归角色态（t_grants 空授权，不再有 test_orders）
        perm_model.set_user_grants("emp1", {}, actor="unit")
        ctx3 = enforcer.build_acl_context({"username": "emp1", "role": "t_grants",
                                           "roles": ["t_grants"], "attributes": {}})
        assert ctx3.has_user_grants is False
        assert "test_orders" not in (ctx3.allowed_tables or []), ctx3.allowed_tables
        print("T9 撤销授权 → 回归角色态 OK")
    finally:
        perm_model.set_user_grants("emp1", {}, actor="unit")  # 撤销
        perm_model.delete_role("t_grants", actor="unit")  # 清理隔离角色


# ═══════════ T10 轻量模式开关（P4）═══════════

def test_light_mode():
    """AUTH_FULL_MODE=0 时：敏感变更 + 非 admin → 直接落地（pending=False）+ auto_approved 留痕"""
    saved = approval.AUTH_FULL_MODE
    try:
        approval.AUTH_FULL_MODE = False
        res = approval.submit_change(
            {"action": "set_role_section", "role": "viewer", "section": "columns",
             "value": {"test_orders": {"unit_price": {"mode": "mask", "mask": "partial_3_4"}}}},
            actor="analyst", actor_roles=["analyst"], reason="轻量模式测试")
        assert res["pending"] is False, res
        # 落地后策略已生效
        pol = perm_model.get_policy("viewer")
        assert pol["columns"]["test_orders"]["unit_price"]["mode"] == "mask", pol
        # 且审批单以 auto_approved 留痕（不留 pending）
        reqs = approval.list_requests(limit=50)
        auto = [r for r in reqs if r.get("reason") == "轻量模式测试"]
        assert auto and all(r.get("status") == "auto_approved" for r in auto), reqs
        print("T10 轻量模式：敏感变更直接落地 + auto_approved 留痕 OK")
    finally:
        approval.AUTH_FULL_MODE = saved
        perm_model.set_role_section("viewer", "columns", {}, actor="unit")


# ═══════════ T11 声明式行规则（P2）═══════════

def test_declarative_row_rule():
    """rule 编译（简单比较 / 跨表子查询 / 固定值防注入）与 ACL 接入（rule 优先、expr 兼容）"""
    expr = enforcer.compile_row_rule({"field": "city", "op": "eq", "source": "user.region"})
    assert expr == "city = ${user.region}", expr
    expr2 = enforcer.compile_row_rule(
        {"op": "in_subquery", "ref_table": "test_factories",
         "on": {"left": "factory_id", "right": "factory_id"},
         "where": {"field": "city", "op": "eq", "source": "user.region"}})
    assert expr2 == "factory_id IN (SELECT factory_id FROM test_factories WHERE city = ${user.region})", expr2
    expr3 = enforcer.compile_row_rule({"field": "city", "op": "eq", "source": "上海"})
    assert expr3 == "city = '上海'", expr3
    # 固定值注入防护
    try:
        enforcer.compile_row_rule({"field": "city", "op": "eq", "source": "x' OR '1'='1"})
        assert False, "含非法字符的固定值应被拒绝"
    except ValueError:
        pass
    # 接入 ACL：rule 配置 → build_acl_context 生成行过滤；编译失败且无 expr → 视为无限制
    perm_model.set_role_section("region_manager", "rows",
                                {"test_factories": {"rule": {"field": "city", "op": "eq", "source": "user.region"},
                                                    "enabled": True}}, actor="unit")
    try:
        ctx = enforcer.build_acl_context({"username": "rm2", "role": "region_manager",
                                          "roles": ["region_manager"], "attributes": {"region": "华东"}})
        assert "test_factories" in ctx.row_filters and "city = '华东'" in ctx.row_filters["test_factories"], \
            ctx.row_filters
        perm_model.set_role_section("region_manager", "rows",
                                    {"test_factories": {"rule": {"field": "city", "op": "bad_op", "source": "user.region"},
                                                        "enabled": True}}, actor="unit")
        ctx2 = enforcer.build_acl_context({"username": "rm2", "role": "region_manager",
                                           "roles": ["region_manager"], "attributes": {"region": "华东"}})
        assert "test_factories" not in ctx2.row_filters, ctx2.row_filters
        print("T11 声明式行规则 OK")
    finally:
        perm_model.set_role_section("region_manager", "rows", {}, actor="unit")


if __name__ == "__main__":
    _backup()
    # 固定测试基准库（postgres 演示库，rewrite 测试需要 test_orders/test_materials 的
    # 真实列结构），不依赖 .env 当前连接（仅进程内切换，不写 .env）。
    import database
    database.switch_database({"db_type": "postgresql", "host": "localhost", "port": 5432,
                              "name": "postgres", "user": "postgres", "password": "123456"})
    # 安装演示 seed 权限模型快照：断言按 seed 的 5 角色/销售域口径编写，真实配置
    # 已生产化为其他角色集时直接跑会误报（2026-09-13 检查实锤）。finally 中 _restore() 恢复。
    from tests._fixture_seed import install_seed_perm_model
    install_seed_perm_model()
    try:
        test_model_layer()
        test_acl_merge()
        test_acl_row_render()
        test_rewrite_row_and_mask()
        test_rewrite_deny()
        test_rewrite_join_alias()
        test_rewrite_star()
        test_rewrite_cte()
        test_rewrite_table_only_acl()
        test_rewrite_dml_guard()
        test_rewrite_mask_position()
        test_rewrite_union()
        test_metric_acl()
        test_approval_flow()
        test_auth_multi_role()
        test_context_var()
        test_user_grants()
        test_light_mode()
        test_declarative_row_rule()
        print("\n全部权限单测通过 ✔")
    finally:
        _restore()
