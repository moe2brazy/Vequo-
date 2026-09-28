"""多角色授权流程测试（端到端，以「身份」为视角）。

与 test_permission.py（引擎单元测试）互补，本脚本模拟不同身份的完整授权链路：
ACL 构建 → 表级/行级/列级/指标级 → SQL 改写 → 各数据入口过滤，
并回归两个已修复的安全漏洞：
  - F2  per-user 授权（user_grants）必须继承角色级的行过滤与列脱敏（纵深防御）
  - F3  acl_fingerprint 必须包含字段白名单 / 聚合豁免维度（防越权缓存复用）

运行：cd backend && python tests/test_auth_flows.py
只读真实配置，不改动任何配置文件。
"""

import os
import sys

os.environ.setdefault("PERMISSION_VCS", "0")  # 测试环境关闭权限自动提交，避免污染 git 历史

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from security import enforcer
from security.enforcer import build_acl_context, rewrite_sql, acl_fingerprint, AclContext
from agent.metric_registry import get_effective_metrics

# 备份/恢复（F2 用隔离角色会改配置，结束后恢复原文件，避免污染 updated_at 等元数据）
_PERM_FILE = os.path.join(BACKEND, "auth_permissions.json")


def _backup():
    if os.path.exists(_PERM_FILE):
        return open(_PERM_FILE, encoding="utf-8").read()
    return None


def _restore(content):
    if content is not None:
        with open(_PERM_FILE, "w", encoding="utf-8") as fh:
            fh.write(content)
    enforcer.perm_model.load_model(refresh=True)

_PASS = 0
_FAIL = 0


def check(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✔ {name}")
    else:
        _FAIL += 1
        print(f"  ✘ {name}  {detail}")


def section(title):
    print("\n" + "=" * 66)
    print(title)
    print("=" * 66)


# 六种身份（真实配置驱动）
IDENTITIES = [
    ("admin",          {"username": "admin", "role": "admin", "roles": ["admin"], "attributes": {}}),
    ("analyst",        {"username": "analyst", "role": "analyst", "roles": ["analyst"], "attributes": {}}),
    ("viewer",         {"username": "viewer", "role": "viewer", "roles": ["viewer"], "attributes": {}}),
    ("region_manager", {"username": "rm", "role": "region_manager", "roles": ["region_manager"], "attributes": {"region": "华东"}}),
    ("demo_rm",        {"username": "demo_rm", "role": "region_manager", "roles": ["region_manager"], "attributes": {"region": "上海"}}),
    ("guest",          {"username": "guest", "role": "guest", "roles": ["guest"], "attributes": {}}),
]


def F1_role_acl_matrix():
    section("F1 角色 ACL 构建矩阵")
    for name, u in IDENTITIES:
        ctx = build_acl_context(u)
        d = ctx.as_dict()
        print(f"  [{name}] superuser={ctx.superuser} "
              f"tables={len(d['allowed_tables']) if d['allowed_tables'] else '∞'} "
              f"rows={len(d['row_filters'])} masks={len(d['column_masks'])} "
              f"metrics={list(d['metric_overrides'].keys())}")

    admin = build_acl_context(IDENTITIES[0][1])
    check("admin = superuser 无限制", admin.superuser and admin.allowed_tables is None)

    rm = build_acl_context(IDENTITIES[3][1])
    check("region_manager 行过滤按 region=华东 渲染", rm.row_filters.get("test_factories") == "city = '华东'",
          rm.row_filters)
    check("region_manager 客户名脱敏", rm.column_masks.get("test_orders", {}).get("customer_name") == "partial_1_1")
    check("region_manager 无权 test_materials", "test_materials" not in (rm.allowed_tables or set()))

    demo = build_acl_context(IDENTITIES[4][1])
    check("demo_rm 行过滤按 region=上海 渲染", demo.row_filters.get("test_factories") == "city = '上海'",
          demo.row_filters)

    guest = build_acl_context(IDENTITIES[5][1])
    check("guest 开放模式不受限", guest.allowed_tables is None and not guest.superuser)


def F2_per_user_inherits_role_defense():
    section("F2 per-user 授权继承角色行/列防线（回归：越权裸奔）")
    # 用隔离角色 + per-user 授权模拟，避免依赖 viewer 真实配置漂移
    enforcer.perm_model.upsert_role({"key": "t_rm2", "name": "临时区域经理", "priority": 55}, actor="unit")
    try:
        enforcer.perm_model.set_role_section("t_rm2", "datasets", ["sales"], actor="unit")
        enforcer.perm_model.set_role_section("t_rm2", "rows",
            {"test_orders": {"rule": {"op": "in_subquery", "ref_table": "test_factories",
                                      "on": {"left": "factory_id", "right": "factory_id"},
                                      "where": {"field": "city", "op": "eq", "source": "user.region"}},
                             "enabled": True}}, actor="unit")
        enforcer.perm_model.set_role_section("t_rm2", "columns",
            {"test_orders": {"customer_name": {"mode": "mask", "mask": "partial_1_1"}}}, actor="unit")
        # 员工 emp_x 属于 t_rm2 角色，且被单独授权 test_orders 整表
        enforcer.perm_model.set_user_grants("emp_x", {"test_orders": []}, actor="unit")
        try:
            ctx = build_acl_context({"username": "emp_x", "role": "t_rm2",
                                     "roles": ["t_rm2"], "attributes": {"region": "华东"}})
            check("per-user 授权表保留角色行过滤",
                  "test_orders" in ctx.row_filters and "华东" in ctx.row_filters["test_orders"],
                  ctx.row_filters)
            check("per-user 授权表保留角色列脱敏",
                  ctx.column_masks.get("test_orders", {}).get("customer_name") == "partial_1_1",
                  ctx.column_masks)
            sql, err, _ = rewrite_sql("SELECT customer_name FROM test_orders", ctx, "postgres")
            check("改写后注入行过滤 + 脱敏", not err and "SUBSTRING" in sql and "华东" in sql, sql)
        finally:
            enforcer.perm_model.set_user_grants("emp_x", {}, actor="unit")
    finally:
        enforcer.perm_model.delete_role("t_rm2", actor="unit")


def F3_fingerprint_dimensions():
    section("F3 acl_fingerprint 维度完整性（回归：越权缓存复用）")
    a = AclContext(username="a", roles=["viewer"]); a.allowed_tables = {"test_orders"}
    a.column_whitelist = {"test_orders": {"order_id", "customer_name"}}
    b = AclContext(username="b", roles=["viewer"]); b.allowed_tables = {"test_orders"}
    b.column_whitelist = {"test_orders": {"order_id"}}
    check("字段白名单不同 → 指纹不同", acl_fingerprint(a) != acl_fingerprint(b))

    c = AclContext(username="c", roles=["viewer"]); c.allowed_tables = {"test_materials"}
    c.column_masks = {"test_materials": {"unit_cost": "round_100"}}
    c.column_mask_agg_ok = {"test_materials": {"unit_cost"}}
    d = AclContext(username="d", roles=["viewer"]); d.allowed_tables = {"test_materials"}
    d.column_masks = {"test_materials": {"unit_cost": "round_100"}}
    check("聚合豁免不同 → 指纹不同", acl_fingerprint(c) != acl_fingerprint(d))


def F4_entry_filters():
    section("F4 各数据入口 ACL 过滤")
    from routers.tables import (_acl_filter_tables, _acl_filter_overview,
                                _acl_filter_relationships, _acl_filter_topics)
    from routers.knowledge import _acl_filter_terms
    from unittest import mock

    allowed = {"dim_equipment", "eqp_downtime_record"}
    fake_ctx = AclContext(username="t", roles=["viewer"])
    fake_ctx.allowed_tables = allowed
    # 统一 mock 受限用户：各过滤函数经 get_current_user + build_acl_context 得到受限上下文
    with mock.patch("routers.tables.get_current_user",
                    return_value={"username": "t", "role": "viewer", "roles": ["viewer"], "attributes": {}}), \
         mock.patch("routers.tables.enforcer.build_acl_context", return_value=fake_ctx):

        tbl = {"tables": [
            {"table_name": "dim_equipment", "columns": [{"name": "a"}, {"name": "b"}]},
            {"table_name": "mes_process_output", "columns": [{"name": "x"}]},
        ]}
        r = _acl_filter_tables(tbl, "x")
        check("表列表过滤无权表", [t["table_name"] for t in r["tables"]] == ["dim_equipment"], r)

        ov = _acl_filter_overview({"data": {
            "table_count": 3, "total_rows": 300, "total_columns": 30,
            "table_row_counts": {"dim_equipment": 10, "eqp_downtime_record": 20, "mes_process_output": 270},
            "table_column_counts": {"dim_equipment": 10, "eqp_downtime_record": 10, "mes_process_output": 10},
        }}, "x")
        check("overview 表数/行数/字段数过滤",
              ov["data"]["table_count"] == 2 and ov["data"]["total_rows"] == 30 and ov["data"]["total_columns"] == 20,
              ov["data"])

        rel = _acl_filter_relationships({"nodes": [{"id": "dim_equipment"}, {"id": "mes_process_output"}],
                                          "relationships": [
                                              {"source_table": "dim_equipment", "target_table": "mes_process_output"},
                                              {"source_table": "dim_equipment", "target_table": "eqp_downtime_record"},
                                          ]}, "x")
        check("关系过滤（节点+关系只留可见表）",
              [n["id"] for n in rel["nodes"]] == ["dim_equipment"]
              and len(rel["relationships"]) == 1
              and rel["relationships"][0]["target_table"] == "eqp_downtime_record", rel)

        tp = _acl_filter_topics({"topics": [
            {"id": "eq", "related_tables": ["dim_equipment"]},
            {"id": "prod", "related_tables": ["mes_process_output"]},
        ]}, "x")
        check("主题过滤（只留相关表全可见）", [t["id"] for t in tp["topics"]] == ["eq"], tp)

        term = _acl_filter_terms({"terms": [
            {"term": "停机", "mapped_table": "dim_equipment"},
            {"term": "产量", "mapped_table": "mes_process_output"},
        ]}, "x")
        check("术语过滤（按映射表）", [t["term"] for t in term["terms"]] == ["停机"], term)

    # ── 知识页三接口（scenes/relations/graph）──
    from routers.knowledge import _acl_filter_scenes, _acl_filter_relations, _acl_filter_graph
    allowed2 = {"dim_equipment", "eqp_downtime_record"}
    fake_ctx2 = AclContext(username="t", roles=["viewer"])
    fake_ctx2.allowed_tables = allowed2
    with mock.patch("routers.tables.get_current_user",
                    return_value={"username": "t", "role": "viewer", "roles": ["viewer"], "attributes": {}}), \
         mock.patch("routers.tables.enforcer.build_acl_context", return_value=fake_ctx2):

        sc = _acl_filter_scenes({"scenes": {
            "eq": {"objects": [{"table": "dim_equipment"}]},
            "prod": {"objects": [{"table": "mes_process_output"}]},
            "mixed": {"objects": [{"table": "dim_equipment"}, {"table": "mes_process_output"}]},
        }}, "x")
        check("场景过滤（只留涉及表全可见）", list(sc["scenes"].keys()) == ["eq"], sc)

        kr = _acl_filter_relations({"relations": [
            {"source_table": "dim_equipment", "target_table": "eqp_downtime_record"},
            {"source_table": "dim_equipment", "target_table": "mes_process_output"},
        ]}, "x")
        check("知识关系过滤（两端可见）",
              len(kr["relations"]) == 1 and kr["relations"][0]["target_table"] == "eqp_downtime_record", kr)

        kg = _acl_filter_graph({"nodes": [{"id": "dim_equipment"}, {"id": "eqp_downtime_record"}, {"id": "mes_process_output"}],
                                "edges": [
                                    {"source": "dim_equipment", "target": "mes_process_output"},
                                    {"source": "dim_equipment", "target": "eqp_downtime_record"},
                                ]}, "x")
        check("图谱过滤（节点+边只留可见）",
              sorted(n["id"] for n in kg["nodes"]) == ["dim_equipment", "eqp_downtime_record"]
              and len(kg["edges"]) == 1 and kg["edges"][0]["target"] == "eqp_downtime_record", kg)


def F5_fail_close():
    section("F5 fail-close 安全底线")
    rm = build_acl_context(IDENTITIES[3][1])
    # 无权表拒绝
    _, err, _ = rewrite_sql("SELECT unit_cost FROM test_materials", rm, "postgres")
    check("无权表拒绝", bool(err), err)
    # 脱敏列参与 WHERE → 拒绝
    _, err2, _ = rewrite_sql("SELECT order_id FROM test_orders WHERE customer_name = '张伟'", rm, "postgres")
    check("脱敏列 WHERE 拒绝", bool(err2) and "脱敏" in err2, err2)
    # 行策略缺失属性 → fail-close 1=0（region_manager 角色 + 无 region 属性的用户）
    an = build_acl_context({"username": "noregion", "role": "region_manager",
                            "roles": ["region_manager"], "attributes": {}})
    check("缺失属性 fail-close 1=0", an.row_filters.get("test_orders") == "1=0", an.row_filters)
    check("unresolved_vars 记录缺失 region", "test_orders:region" in an.unresolved_vars, an.unresolved_vars)
    # DML 拒绝
    _, err3, _ = rewrite_sql("DELETE FROM test_orders", rm, "postgres")
    check("DML 拒绝", bool(err3) and "仅允许" in err3, err3)


def F6_metric_acl():
    section("F6 指标口径覆盖")
    rm = build_acl_context(IDENTITIES[3][1])
    filtered = enforcer.apply_metric_acl(get_effective_metrics(), rm)
    prod = next((m for m in filtered if m["name"] == "产量"), None)
    check("region_manager 产量按总产出口径", prod and "good_qty + defect_qty" in prod["sql_expression"],
          prod and prod["sql_expression"])
    admin = build_acl_context(IDENTITIES[0][1])
    raw = enforcer.apply_metric_acl(get_effective_metrics(), admin)
    prod2 = next((m for m in raw if m["name"] == "产量"), None)
    check("admin 产量不受覆盖（原口径）", prod2 and "_acl_override_by_role" not in prod2,
          prod2 and prod2.get("_acl_override_by_role"))


if __name__ == "__main__":
    bak = _backup()
    try:
        # 安装演示 seed 权限模型快照：本测试按 seed 的 5 角色/销售域脱敏/region 行过滤
        # 口径编写断言；真实 auth_permissions.json 已生产化为其他角色集时直接跑会误报
        # （2026-09-13 检查实锤 7 项）。结束后 finally 中 _restore(bak) 恢复真实配置。
        from tests._fixture_seed import install_seed_perm_model
        install_seed_perm_model()
        F1_role_acl_matrix()
        F2_per_user_inherits_role_defense()
        F3_fingerprint_dimensions()
        F4_entry_filters()
        F5_fail_close()
        F6_metric_acl()
        print("\n" + "=" * 66)
        print(f"结果：{_PASS} 通过 / {_FAIL} 失败")
        print("=" * 66)
        sys.exit(1 if _FAIL else 0)
    finally:
        _restore(bak)
