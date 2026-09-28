# -*- coding: utf-8 -*-
"""第三轮：FastAPI 端点级集成测试（execute_confirm / metrics 保存）"""
import sys
sys.path.insert(0, '.')

# 固定测试基准库（postgres 演示库），不依赖 .env 当前连接
import database
database.switch_database({"db_type": "postgresql", "host": "localhost", "port": 5432,
                          "name": "postgres", "user": "postgres", "password": "123456"})

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

# ── TestClient + 认证 mock（httpx ASGI transport，兼容旧 starlette）──
import httpx
import main
from security.enforcer import AclContext, build_acl_context, acl_fingerprint

# mock 认证：模块级 get_current_user（main 端点直接引用）+ security.enforcer 内部符号
main.get_current_user = lambda authorization=None: {"username": "tester", "role": "admin", "roles": ["admin"]}

CURRENT_ACL = {"acl": None}
def _fake_build(u):
    return CURRENT_ACL["acl"] or AclContext(username="tester", superuser=True)
def _fake_fp(a):
    return "fp_test"
import security.enforcer as enforcer
enforcer.build_acl_context = _fake_build
enforcer.acl_fingerprint = _fake_fp


def _cleanup_test_metric():
    """清理测试指标（直接操作运行时 JSON，避免依赖不存在的删除接口）"""
    import json, io
    p = 'metrics_registry.json'
    try:
        d = json.load(io.open(p, encoding='utf-8'))
    except Exception:
        return
    items = d if isinstance(d, list) else d.get('metrics', d.get('items', []))
    kept = [m for m in items if m.get('name') != '集成测试指标ZZ']
    if isinstance(d, list):
        json.dump(kept, io.open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    else:
        d['metrics'] = kept
        json.dump(d, io.open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)


async def main_test():
    _cleanup_test_metric()  # 先清残留（上次异常退出可能遗留）
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await run_cases(client)
    _cleanup_test_metric()  # 跑完再清
    # 恢复 enforcer 符号
    enforcer.build_acl_context = build_acl_context
    enforcer.acl_fingerprint = acl_fingerprint


async def run_cases(client):
    # ═══ H. execute_confirm 端点 ═══
    print("═══ H. execute_confirm 端点 ═══")

    # H1 正常 SELECT
    r = await client.post("/api/agent/execute_confirm", json={
        "query": "各产线产量", "sql": "SELECT line_id, SUM(good_qty) qty FROM mes_process_output GROUP BY line_id LIMIT 5"})
    check("H1 正常执行", r.status_code == 200 and r.json().get("type") == "data_query",
          f"status={r.status_code} rows={r.json().get('result', {}).get('row_count')}")

    # H2 只读拦截：DELETE
    r = await client.post("/api/agent/execute_confirm", json={
        "query": "q", "sql": "DELETE FROM mes_process_output WHERE line_id='L01'"})
    check("H2 DELETE 拒绝(400)", r.status_code == 400 and "仅允许" in r.json().get("detail", ""),
          f"status={r.status_code} detail={r.json().get('detail','')[:40]}")

    # H3 0 行 → answer 带提示
    r = await client.post("/api/agent/execute_confirm", json={
        "query": "q", "sql": "SELECT * FROM mes_process_output WHERE line_id = 'NOT_EXIST' LIMIT 5"})
    body = r.json()
    check("H3 0行提示", r.status_code == 200 and "0 行" in body.get("answer", "") and "JOIN" in body.get("answer", ""),
          f"answer={body.get('answer','')[:60]}")

    # H4 表存在性：不存在表 → 400
    r = await client.post("/api/agent/execute_confirm", json={
        "query": "q", "sql": "SELECT * FROM no_such_table LIMIT 1"})
    check("H4 不存在表拒绝(400)", r.status_code == 400 and "不存在" in r.json().get("detail", ""))

    # H5 表权限：allowed_tables 限制 → 403
    CURRENT_ACL["acl"] = AclContext(username="viewer", allowed_tables={"mes_process_output"})
    r = await client.post("/api/agent/execute_confirm", json={
        "query": "q", "sql": "SELECT * FROM test_orders LIMIT 1"})
    check("H5 无权表 403", r.status_code == 403, f"status={r.status_code}")

    # H6 RLS 行级：allowed + row_filters → 执行成功且只返回过滤行
    CURRENT_ACL["acl"] = AclContext(username="rls_user", allowed_tables={"mes_process_output"},
                                    row_filters={"mes_process_output": "line_id = 'L01'"})
    r = await client.post("/api/agent/execute_confirm", json={
        "query": "q", "sql": "SELECT DISTINCT line_id FROM mes_process_output LIMIT 10"})
    body = r.json()
    rows = body.get("result", {}).get("rows", []) or []
    lines = {row["line_id"] for row in rows}
    check("H6 RLS 行级生效", r.status_code == 200 and lines == {"L01"}, f"lines={lines}")

    # H7 列级拒绝 → 403
    CURRENT_ACL["acl"] = AclContext(username="col_user", allowed_tables={"test_orders"},
                                    column_denies={"test_orders": {"customer_name"}})
    r = await client.post("/api/agent/execute_confirm", json={
        "query": "q", "sql": "SELECT customer_name FROM test_orders LIMIT 1"})
    check("H7 列级拒绝 403", r.status_code == 403, f"status={r.status_code}")

    # H8 超时/行数上限护栏由 executor 兜底（SELECT 正常不受影响）
    CURRENT_ACL["acl"] = AclContext(username="tester", superuser=True)
    r = await client.post("/api/agent/execute_confirm", json={
        "query": "q", "sql": "SELECT factory_id FROM test_factories LIMIT 3"})
    check("H8 正常查询不受护栏误伤", r.status_code == 200)

    # ═══ I. /api/metrics 定义口径 ═══
    print("═══ I. /api/metrics 定义口径 ═══")

    # I1 tables 空 → 400（BE-2 修复验证）
    r = await client.post("/api/metrics", json={
        "name": "测试指标空表", "formula": "SUM(x)", "sql_expression": "SUM(x)",
        "tables": []})
    check("I1 空表拒绝(400)", r.status_code == 400 and "适用表" in r.json().get("detail", ""),
          f"status={r.status_code} detail={r.json().get('detail','')[:40]}")

    # I2 正常保存
    r = await client.post("/api/metrics", json={
        "name": "集成测试指标ZZ", "formula": "SUM(good_qty)", "sql_expression": "SUM(good_qty)",
        "tables": ["mes_process_output"], "unit": "件", "aliases": []})
    check("I2 正常保存", r.status_code == 200 and r.json().get("success"), f"status={r.status_code}")

    # I3 名称冲突 → 400
    r = await client.post("/api/metrics", json={
        "name": "集成测试指标ZZ", "formula": "x", "tables": ["mes_process_output"]})
    check("I3 名称冲突 400", r.status_code == 400, f"status={r.status_code}")

    # I4 非法 SQL 表达式 → 400
    r = await client.post("/api/metrics", json={
        "name": "坏SQL指标", "formula": "x", "sql_expression": "SELECT * FROM x",
        "tables": ["mes_process_output"]})
    check("I4 非法SQL拒绝(400)", r.status_code == 400, f"status={r.status_code} detail={r.json().get('detail','')[:40]}")


import asyncio
asyncio.run(main_test())

print(f"\n═══ 结果: PASS={PASS} FAIL={FAIL} ═══")
sys.exit(1 if FAIL else 0)
