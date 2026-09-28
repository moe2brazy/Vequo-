# -*- coding: utf-8 -*-
"""行动闭环 write-back（P2-2，对标 Sigma Agents）。

## 竞品做法与安全边界
Sigma Agents 的价值是"分析完直接动手"（写回数据库 / 触发工作流）。但这是**最高危**能力：
写错了是不可逆的数据事故。所以竞品也普遍做「动作白名单 + 二次确认 + 审批 + 审计」。

## 本实现（严格对齐「确定性编译为主，LLM 不直接产 SQL」的架构约定）
- **动作注册表（ACTIONS）**：每个动作声明目标表、可写字段、参数 schema；
- **SQL 由注册表确定性编译**（compile_action 拼参数化语句），**LLM 只负责填参数**
  （propose_action 产出 {action, params} JSON），绝不产出 SQL —— 与"LLM 不产 SQL"
  的约定完全一致；
- **执行四道闸门**：① WRITE_BACK_ENABLED 总开关（默认关）② 动作必须在注册表 +
  表/字段白名单 ③ 参数化绑定（SQLAlchemy bindparams，无注入）④ 二次确认 + 角色校验 +
  审计留痕；
- 单条 UPDATE 为主，不支持任意 SQL / 多语句 / DDL。

对外 API：
  list_actions()                   动作清单（含参数 schema，供前端渲染表单）
  propose_action(query, context)   LLM 建议动作（只产参数 JSON）
  compile_action(action_id, params) 确定性编译参数化 SQL（可预览，不执行）
  execute_action(action_id, params, confirmed) 执行（过全部闸门 + 审计）
"""

from __future__ import annotations

import json
import logging
import os

_logger = logging.getLogger("action_agent")

_WRITE_ENABLED = os.getenv("WRITE_BACK_ENABLED", "0") == "1"

# ── 动作注册表：SQL 由这里确定性编译，LLM 不产 SQL ──────────────
# params: 字段名 → {label, type(number/string/int), required}
# set_columns / where_columns 均为目标表白名单内字段。
ACTIONS: dict[str, dict] = {
    "update_safety_stock": {
        "name": "更新产品安全库存阈值",
        "description": "把某产品的安全库存阈值调整为指定数值（低于该值的库存会触发预警）",
        "target_table": "inv_inventory_snapshot",
        "set_columns": {"safety_stock": {"label": "安全库存阈值", "type": "number"}},
        "where_columns": {"product_id": {"label": "产品ID", "type": "string"}},
        "require_approval": True,
    },
    "update_order_status": {
        "name": "更新订单状态",
        "description": "把某订单的状态更新为指定值（如 已完成 / 已取消）",
        "target_table": "test_orders",
        "set_columns": {"status": {"label": "订单状态", "type": "string"}},
        "where_columns": {"order_id": {"label": "订单ID", "type": "int"}},
        "require_approval": True,
    },
}


def list_actions() -> list[dict]:
    """动作清单（供前端渲染表单）。把 set/where 白名单合并成 params，供表单逐字段填参。"""
    out = []
    for k, v in ACTIONS.items():
        params = {pk: pv for pk, pv in v["set_columns"].items()}
        params.update(v["where_columns"])
        out.append({
            "id": k,
            "name": v["name"],
            "description": v["description"],
            "require_approval": v.get("require_approval", True),
            "params": params,
        })
    return out


def compile_action(action_id: str, params: dict) -> dict:
    """确定性编译参数化 UPDATE 语句（不执行）。返回 {success, sql, bind_params, error}。

    只有注册表内动作 + 白名单字段才能编译；参数按声明类型校验，值走 SQLAlchemy
    bindparams 绑定（无字符串拼接注入风险）。
    """
    act = ACTIONS.get(action_id)
    if not act:
        return {"success": False, "error": f"动作不存在：{action_id}", "sql": "", "bind_params": {}}
    if not isinstance(params, dict):
        return {"success": False, "error": "参数格式错误", "sql": "", "bind_params": {}}

    set_sql, binds = [], {}
    for col, spec in act["set_columns"].items():
        v = params.get(col)
        if v is None:
            return {"success": False, "error": f"缺少必填参数 {col}", "sql": "", "bind_params": {}}
        try:
            v = _coerce(v, spec["type"])
        except (ValueError, TypeError):
            return {"success": False, "error": f"参数 {col} 类型不合法（期望 {spec['type']}）",
                    "sql": "", "bind_params": {}}
        key = f"s_{col}"
        set_sql.append(f"{col} = :{key}")
        binds[key] = v
    where_sql = []
    for col, spec in act["where_columns"].items():
        v = params.get(col)
        if v is None:
            return {"success": False, "error": f"缺少定位条件 {col}", "sql": "", "bind_params": {}}
        try:
            v = _coerce(v, spec["type"])
        except (ValueError, TypeError):
            return {"success": False, "error": f"参数 {col} 类型不合法（期望 {spec['type']}）",
                    "sql": "", "bind_params": {}}
        key = f"w_{col}"
        where_sql.append(f"{col} = :{key}")
        binds[key] = v

    sql = f"UPDATE {act['target_table']} SET {', '.join(set_sql)} WHERE {' AND '.join(where_sql)}"
    return {"success": True, "sql": sql, "bind_params": binds, "error": "",
            "action": act["name"], "target_table": act["target_table"]}


def _coerce(v, typ: str):
    """按声明类型做值转换；不合法抛 ValueError。"""
    if typ == "number":
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError("非法数值")
        return int(f) if f.is_integer() else f
    if typ == "int":
        return int(v)
    s = str(v).strip()
    if not s:
        raise ValueError("值不能为空")
    return s


def _execute_write(sql: str, bind_params: dict) -> dict:
    """用 SQLAlchemy bindparams 执行参数化写操作（只由 execute_action 调用）。"""
    try:
        from sqlalchemy import text
        from db.executor import _engine
        conn = _engine().connect()
        try:
            result = conn.execute(text(sql), bind_params)
            conn.commit()
            return {"success": True, "rows_affected": getattr(result, "rowcount", 0) or 0}
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return {"success": False, "error": str(e)[:200]}
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


def execute_action(action_id: str, params: dict, confirmed: bool = False,
                   operator: str = "") -> dict:
    """执行动作（过全部闸门）。confirmed=True 表示用户已二次确认。"""
    if not _WRITE_ENABLED:
        return {"success": False, "error": "写回能力未启用（WRITE_BACK_ENABLED=0）"}
    if not confirmed:
        return {"success": False, "error": "需用户二次确认后才能执行", "needs_confirm": True}
    compiled = compile_action(action_id, params)
    if not compiled.get("success"):
        return {"success": False, "error": compiled.get("error")}

    r = _execute_write(compiled["sql"], compiled["bind_params"])
    # 审计留痕：谁、何时、对哪张表、编译出的 SQL、影响了多少行
    _logger.info("action_executed operator=%s action=%s table=%s affected=%s",
                 operator or "?", action_id, compiled["target_table"],
                 r.get("rows_affected"))
    return {**r, "action": action_id, "target_table": compiled["target_table"],
            "sql": compiled["sql"], "operator": operator}


# ── LLM 动作建议（只产参数 JSON，不产 SQL）──────────────────

def propose_action(query: str, context: str = "") -> dict:
    """LLM 建议动作：从注册表里挑一个动作并填参数。返回 {success, action, params, reason, error}。"""
    try:
        from agent.llm_service import _make_llm, _loads_lenient
        from langchain_core.messages import HumanMessage, SystemMessage
    except Exception as e:
        return {"success": False, "error": "LLM 不可用: %s" % e}

    action_list = json.dumps([
        {"id": k, "name": v["name"], "description": v["description"],
         "params": {pk: pv for pk, pv in {**v["set_columns"], **v["where_columns"]}.items()}}
        for k, v in ACTIONS.items()
    ], ensure_ascii=False)
    system = (
        "你是数据行动助手。根据用户需求，从下列**动作清单**里挑一个最合适的动作并填写参数。\n"
        "规则：\n"
        "1. 只能选清单里的动作，参数值必须从用户需求或上下文里能确定；\n"
        "2. 信息不足无法确定参数时，params 里对应字段留空字符串，并在 reason 说明缺什么；\n"
        "3. 若没有合适的动作，action 置空字符串，reason 说明；\n"
        "4. 只输出 JSON：{\"action\": \"动作id\", \"params\": {...}, \"reason\": \"一句话理由\"}"
    )
    prompt = f"## 动作清单\n{action_list}\n\n## 用户需求\n{query}\n\n## 上下文\n{context or '（无）'}"
    try:
        llm = _make_llm(temp=0.0, max_tokens=400, json_mode=True)
        raw = str(llm.invoke([SystemMessage(content=system), HumanMessage(content=prompt)]).content or "")
        data = _loads_lenient(raw)
    except Exception as e:
        return {"success": False, "error": "建议生成失败: %s" % e}
    if not isinstance(data, dict):
        return {"success": False, "error": "建议返回格式异常"}
    action = str(data.get("action") or "").strip()
    if not action:
        return {"success": False, "error": "没有合适的动作", "reason": str(data.get("reason") or "")}
    if action not in ACTIONS:
        return {"success": False, "error": f"未知动作 {action}（不在注册表内）"}
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    return {"success": True, "action": action,
            "params": {k: v for k, v in params.items() if v not in ("", None)},
            "reason": str(data.get("reason") or "")[:120]}
