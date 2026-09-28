# -*- coding: utf-8 -*-
"""NL2Python 代码生成 Agent —— 只生成代码，绝不执行。

执行由 agent/python_sandbox.py 在「用户二次确认 + 静态安全检查」之后完成。
这个「生成 / 执行分离」的设计与竞品一致：ThoughtSpot 称之为 inspectable Python，
Vanna 是 generate → confirm → run，Fabric Code Interpreter 同样是先展示代码。

同时也符合本产品的架构约定：LLM 产出的是「待确认的产物 + 说明」，与未注册口径
走 human-in-loop 二次确认是同一条原则，只是把 SQL 换成了 Python 代码。

对外 API：
  - describe_data(columns, rows)      → 供 prompt 使用的数据画像文本
  - generate_python_plan(query, ...)  → {success, code, explanation, error}
"""

import json
import re

from langchain_core.messages import SystemMessage

from agent.python_sandbox import sandbox_capabilities, scan_code

_SYSTEM = """你是资深数据分析工程师，负责为「SQL 算不出来」的分析需求编写 Python 计算代码。

## 运行环境的严格限制（违反会导致代码被拒绝执行）
1. 只能使用：pandas、numpy、scipy、sklearn、math、statistics、itertools、
   collections、json、datetime、re、random、functools、operator 等计算类模块。
   **禁止**：os、sys、subprocess、socket、requests、urllib、shutil、pathlib、
   pickle、joblib、open()、eval()、exec() 等任何文件 / 网络 / 系统调用。
2. 数据已注入为 DataFrame 变量 `df`，只读。你不能也不需要连接数据库。
3. 代码在同一个进程内一次性执行，不要写 while True 之类的死循环。

## 输出要求
1. 把最终结论赋值给变量 `result`（DataFrame / dict / list / 标量均可）；
   也允许把最后一个表达式直接写在末尾，系统会自动把它当作结果。
2. 用 print() 输出 2-4 条关键中文结论（结论先行，带具体数字，不要复述代码）。
3. 注意数据类型：数值列可能是字符串，必要时先 pd.to_numeric(errors='coerce')；
   注意空值 dropna / fillna。
4. 只输出下列 JSON，不要 Markdown 代码块，不要注释以外的多余文字：
   {{"explanation": "一句话说明这段代码算什么、怎么读结果（40 字以内）",
     "code": "完整的 Python 代码"}}

## 可用能力
{capabilities}
"""

_PROMPT = """## 用户需求
{query}

## 数据（变量名 df）
行数：{row_count}
列与类型：
{columns}

前 3 行样例：
{sample}

请编写 Python 代码完成用户需求。只输出 JSON。"""


def describe_data(columns: list, rows: list) -> str:
    """生成数据画像文本（列名 + 推断类型 + 空值率），供 LLM 写正确的代码。"""
    cols = list(columns or [])
    if not cols and rows:
        cols = list((rows[0] or {}).keys())
    lines = []
    for c in cols:
        vals = [r.get(c) for r in (rows or [])[:200]]
        non_null = [v for v in vals if v is not None and str(v).strip() != ""]
        kind = "未知"
        if non_null:
            sample = non_null[0]
            if isinstance(sample, bool):
                kind = "布尔"
            elif isinstance(sample, (int, float)):
                kind = "数值"
            elif re.match(r"^\d{4}-\d{2}-\d{2}", str(sample)):
                kind = "日期"
            else:
                kind = "文本"
        null_rate = "%.0f%%" % (100 * (1 - len(non_null) / max(len(vals), 1)))
        uniq = len({str(v) for v in non_null})
        lines.append("- %s（%s，空值 %s，%d 个不同值）" % (c, kind, null_rate, uniq))
    return "\n".join(lines) or "（无列信息）"


def generate_python_plan(query: str, columns: list, rows: list,
                         max_rows: int = 20000) -> dict:
    """生成 Python 分析代码（不执行）。返回 {success, code, explanation, error}。"""
    try:
        from agent.llm_service import _make_llm, _loads_lenient
    except Exception as e:
        return {"success": False, "code": "", "explanation": "", "error": "LLM 不可用: %s" % e}

    rows = list(rows or [])[:max_rows]
    caps = sandbox_capabilities()
    cap_text = "、".join([k for k, v in caps.items() if v]) or "仅 Python 标准库"
    if caps.get("sklearn"):
        cap_text += "（可用 sklearn 做聚类 / 回归 / 异常检测）"

    try:
        sample = json.dumps(rows[:3], ensure_ascii=False, default=str)[:1200]
    except Exception:
        sample = "[]"

    prompt = _PROMPT.format(
        query=(query or "")[:400],
        row_count=len(rows),
        columns=describe_data(columns, rows),
        sample=sample,
    )
    try:
        llm = _make_llm(temp=0.0, max_tokens=1400, json_mode=True)
        raw = str(llm.invoke([SystemMessage(content=_SYSTEM.format(capabilities=cap_text)),
                              SystemMessage(content=prompt)]).content or "")
    except Exception as e:
        return {"success": False, "code": "", "explanation": "", "error": "代码生成失败: %s" % e}

    data = _loads_lenient(raw)
    if not isinstance(data, dict):
        return {"success": False, "code": "", "explanation": "", "error": "代码生成返回格式异常"}

    code = str(data.get("code") or "").strip()
    code = re.sub(r"^```(?:python)?\s*|\s*```$", "", code, flags=re.MULTILINE).strip()
    if not code:
        return {"success": False, "code": "", "explanation": "", "error": "未生成有效代码"}

    # 生成后立即做静态安全检查：带病代码不返回给用户，直接把原因讲清楚
    problems = scan_code(code)
    return {
        "success": not problems,
        "code": code,
        "explanation": str(data.get("explanation") or "")[:200],
        "error": ("生成的代码未通过安全检査：" + "；".join(problems)) if problems else "",
        "warnings": problems,
    }
