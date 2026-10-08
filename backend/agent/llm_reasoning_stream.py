"""LLM 思考链（reasoning_content）流式客户端 —— 2026-10-03

## 为什么不用 langchain 的 stream()

实测 `langchain_core 1.5.3` + `langchain_openai 1.4.1` 在本项目配置的
`deepseek-v4.1-flash @ dashscope` 上，`llm.stream()` 会把 `reasoning_content`
**整段丢弃**：

    原始 HTTP（/chat/completions, stream=true）→ 328 个 SSE chunk，
        delta 里 328 次出现 reasoning_content，合计 2600 字符
    langchain 同一请求        → content 0 字符、additional_kwargs 为空 {} 

原因：`reasoning_content` 不是 OpenAI 标准字段，langchain-openai 的
`_convert_chunk_to_generation_chunk` 只映射 `delta.content` / `tool_calls` /
`function_call`，非标准字段没有承载位置，于是被静默丢掉。
`stream_usage` 之类的开关也管不到它。

所以这里**绕过 langchain**，直接用 urllib 打 OpenAI 兼容端点，
按 SSE 逐行解析，同时拿到：
  · `delta.reasoning_content` —— 模型的真实思考（token 级）
  · `delta.content`           —— 最终答案（token 级）

## 与项目既有链路的关系

本模块**只负责"把 token 推出去"**，不做任何业务决策，也不改 SQL 生成主链路：
  · `stream_reasoning()` 产出 (kind, text) 序列，kind ∈ {reasoning, content}
  · 调用方（llm_service）在 SSE 里转成 `thought` 事件
真正的 SQL 仍然走原来的 `_llm.stream()` / `.invoke()`，
两条链路各自独立 —— 思考流挂了也不影响出 SQL（fail-open）。

## 降级

任何异常（端点不支持 stream、网络错、字段名不同）都退化为「不产出 token」，
由调用方继续走原链路。绝不因展示功能影响主功能。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Generator, Iterator

# dashscope / deepseek 等 OpenAI 兼容端点的思考字段名不完全一致，
# 逐个尝试；第一个命中的就用它（实测 dashscope-v4.1-flash 用第一个）。
_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")
# 有的端点把思考放在顶层而不是 delta 里
_TOP_REASONING_KEYS = ("reasoning_content", "reasoning")


def _extract_reasoning(delta: dict, top: dict) -> str:
    """从 SSE chunk 里取出思考文本；兼容 delta 内与顶层两种位置。"""
    for k in _REASONING_KEYS:
        v = delta.get(k)
        if isinstance(v, str) and v:
            return v
    for k in _TOP_REASONING_KEYS:
        v = top.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def stream_reasoning(
    prompt: str,
    *,
    model: str,
    api_key: str,
    base_url: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    timeout: float = 120.0,
    system: str = "",
    extra_body: dict | None = None,
) -> Iterator[tuple[str, str]]:
    """流式调用 LLM，产出 (kind, text) 序列。

    kind:
      "reasoning" — 模型思考增量（可能为空字符串之外的长文本片段）
      "content"   — 最终答案增量

    与 langchain 一致地**按需产出**：调用方边收边推 SSE，用户能看到逐字效果。

    失败时静默返回（不抛异常）—— 展示层故障不能影响主链路。
    """
    if not (model and api_key and base_url and prompt):
        return

    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})

    payload: dict = {
        "model": model,
        "messages": msgs,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # 网关/模型特定参数（与 _make_llm 的 extra_body 同义）
    if extra_body:
        payload.update(extra_body)

    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            # 明确要求流式（部分网关需要）
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    deadline = time.monotonic() + max(5.0, float(timeout))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                # 墙钟熔断：与主链 deadline 对齐，避免展示流比主链活得更久
                if time.monotonic() > deadline:
                    return
                line = raw.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                # SSE：data: {...}；也兼容裸 JSON 行
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    if line == "[DONE]":
                        return
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                choices = obj.get("choices") or []
                if not choices:
                    continue
                ch = choices[0] or {}
                delta = ch.get("delta") or {}

                r = _extract_reasoning(delta, obj)
                if r:
                    yield ("reasoning", r)
                c = delta.get("content")
                if isinstance(c, str) and c:
                    yield ("content", c)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        # 端点不支持流式 / 网络故障 / 超时 —— 静默降级，调用方继续走原链路
        return
    except Exception:
        return
