"""轻量 MCP（Model Context Protocol）端点 — 以 JSON-RPC 2.0 暴露语义层能力

供外部 Agent（Claude / Cursor / 自定义 Copilot 等）通过 MCP 协议调用 Vequo 维阔，
所有调用复用同一套权限链路（与人类用户同权），无第三方 MCP SDK 依赖。

暴露工具：
- ask_metric          自然语言问数（NL→SQL→结果，受权限管控）
- list_metrics        列出当前生效指标清单（名称/口径/单位）
- dry_run_permission  权限判定干跑（输入 SQL，返回 ACL 判定与改写后 SQL，不执行）

仅覆盖 initialize / notifications/initialized / tools/list / tools/call，
协议字段与官方 MCP 对齐，可被主流 MCP 客户端识别。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

router = APIRouter()

TOOLS = [
    {
        "name": "ask_metric",
        "description": "用自然语言查询指标数据（如「近30天产量按产线」「本月产量环比」），返回口径与结果",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "自然语言问数"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_metrics",
        "description": "列出当前生效的指标清单（名称/口径公式/单位）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "dry_run_permission",
        "description": "对给定 SQL 做权限判定干跑：返回是否允许、ACL 判定明细与改写后 SQL，不真正执行",
        "inputSchema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "待校验的 SQL"}},
            "required": ["sql"],
        },
    },
]


def _rpc_result(rid, result) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid, code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=200, content={
        "jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message},
    })


async def _call_tool(name: str, args: dict, authorization: str | None) -> str:
    from auth import get_current_user
    from security.enforcer import build_acl_context, rewrite_sql

    u = get_current_user(authorization)
    acl = build_acl_context(u)

    if name == "list_metrics":
        from agent.metric_registry import get_effective_metrics
        ms = get_effective_metrics()
        lines = [f"- {m['name']}：{m.get('formula') or m.get('sql_expression')}"
                 f"（{m.get('unit', '')}）" for m in ms]
        return "当前生效指标：\n" + "\n".join(lines)

    if name == "dry_run_permission":
        sql = str(args.get("sql") or "")
        if not sql:
            return json.dumps({"allowed": False, "reason": "缺少 sql 参数"}, ensure_ascii=False)
        eff, err, applied = rewrite_sql(sql, acl)
        if err:
            return json.dumps({"allowed": False, "reason": err, "applied": applied},
                              ensure_ascii=False, default=str)
        return json.dumps({"allowed": True, "rewritten_sql": eff, "applied": applied,
                           "acl": acl.as_dict()}, ensure_ascii=False, default=str)

    if name == "ask_metric":
        query = str(args.get("query") or "")
        if not query:
            return json.dumps({"error": "缺少 query 参数"}, ensure_ascii=False)
        from agent.llm_service import LLMService
        from security.context import set_acl, clear_acl
        set_acl(acl)
        try:
            service = LLMService(query, [], acl=acl)
            final: dict = {}
            for event in service.run():
                if event["type"] == "done":
                    final = event.get("response", {})
                elif event["type"] == "error":
                    return json.dumps({"error": event.get("message", "查询出错")},
                                      ensure_ascii=False)
        finally:
            clear_acl()
        answer = final.get("answer") if isinstance(final, dict) else final
        if isinstance(answer, dict):
            answer = answer.get("answer", answer)
        return json.dumps({"answer": answer, "sql": getattr(service, "sql", "")},
                          ensure_ascii=False, default=str)

    return json.dumps({"error": f"未知工具：{name}"}, ensure_ascii=False)


@router.post("/mcp")
async def mcp_endpoint(request: Request, authorization: str = Header(None)):
    try:
        body = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "请求体不是合法 JSON")
    method = body.get("method", "")
    rid = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        return _rpc_result(rid, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "Vequo 维阔 NL2SQL Agent", "version": "2.0.0"},
        })
    if method == "notifications/initialized":
        return JSONResponse(content={"jsonrpc": "2.0"})
    if method == "tools/list":
        return _rpc_result(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            text = await _call_tool(name, args, authorization)
        except Exception as e:
            # 不回传内部异常细节（P2 修复：MCP 是外部通道，路径/SQL/堆栈属信息泄露面），
            # 只给类型 + 截断消息
            _msg = str(e).strip()[:200] or type(e).__name__
            return _rpc_result(rid, {"content": [{"type": "text", "text": f"工具调用失败：{type(e).__name__}: {_msg}"}],
                                     "isError": True})
        return _rpc_result(rid, {"content": [{"type": "text", "text": text}]})
    return _rpc_error(rid, -32601, f"方法不支持：{method}")
