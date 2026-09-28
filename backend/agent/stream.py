"""Agent 流式 — 简化版：ThreadPoolExecutor + 队列推送 SSE

后台线程跑 LLMService.run()，事件通过 queue.Queue 推送给 SSE。

注意：本模块所有 async 生成器**禁止**使用阻塞式 `queue.get()` / 同步 for 循环，
否则会卡死 uvicorn 的 event loop，导致已 yield 的 SSE 数据无法及时 flush，
前端表现为「一直转圈，最后一次性全部输出」。统一用 get_nowait + await sleep 让出控制权。
"""
import asyncio
import json
import queue
import threading
from decimal import Decimal
from typing import AsyncGenerator

from agent.llm_service import LLMService, generate_analysis, generate_predict, cache_result
from langchain_openai import ChatOpenAI


def make_streaming_llm(label: str = "", temp: float = 0.0, max_tokens: int = 1024) -> ChatOpenAI:
    """创建 LLM 实例（复用统一工厂，含 Kimi temperature 兜底；支持流式与普通 invoke）"""
    from agent.llm_service import _make_llm
    return _make_llm(temp=temp, max_tokens=max_tokens)


class _JSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return super().default(obj)


def _sse(event: str, data: dict) -> str:
    payload = json.dumps({'type': event, **data}, ensure_ascii=False, cls=_JSONEncoder)
    return f"data:{payload}\n\n"


async def ask_stream(query: str, history: list[dict] | None = None,
                     allowed_tables: set[str] | None = None,
                     row_filters: dict[str, str] | None = None,
                     column_whitelist: dict[str, set[str]] | None = None,
                     acl=None, user: dict | None = None,
                     no_confirm: bool = False) -> AsyncGenerator[str, None]:
    """流式执行：后台线程跑 LLMService，主协程轮询队列发 SSE

    acl: security.enforcer.AclContext（权限中心 v2 统一上下文）。
    allowed_tables/row_filters/column_whitelist 为 v1 遗留参数（deprecated）：
    HTTP 入口统一传 acl，LLMService.__init__ 会把 acl 同步到内部字段；保留签名仅为兼容。
    注意 LLMService.run() 在**后台线程**里执行，ContextVar 不会从主协程继承，
    权限上下文由 run() 内部的 _bind_acl() 在线程内重新绑定。
    """
    event_queue: queue.Queue = queue.Queue()

    # ── 报告意图分流（提问即报告）：问句本身就是要一份报告（如"生产周报"）→
    # 走 periodic_report 的固定口径模板链路，不进 LLMService 的问数管道。
    # 识别失败/异常一律放行给原链路，绝不让报告识别把普通问数吞掉。
    rr = None
    try:
        from agent.report_intent import detect as _detect_report
        rr = _detect_report(query)
    except Exception:
        rr = None

    if rr is not None:
        def _bg_runner():
            try:
                from agent import periodic_report as _PR
                for ev in _PR.generate_events(query, rr, acl=acl, user=user):
                    event_queue.put(ev)
            except Exception as e:
                event_queue.put({"type": "error", "message": str(e)})
            finally:
                try:
                    from security.context import clear_acl
                    clear_acl()
                except Exception:
                    pass
                event_queue.put(None)  # 结束信号
    else:
        service = LLMService(query, history, allowed_tables=allowed_tables,
                             row_filters=row_filters, column_whitelist=column_whitelist,
                             acl=acl, no_confirm=no_confirm)

        def _bg_runner():
            try:
                for event in service.run():
                    event_queue.put(event)
            except Exception as e:
                event_queue.put({"type": "error", "message": str(e)})
            finally:
                try:
                    from security.context import clear_acl
                    clear_acl()  # 线程可能被复用，用完即清
                except Exception:
                    pass
                event_queue.put(None)  # 结束信号

    thread = threading.Thread(target=_bg_runner, daemon=True)
    thread.start()

    # 先推一个 ping，尽早建立连接并冲掉中间层缓冲
    yield ": stream-open\n\n"

    while True:
        try:
            item = event_queue.get_nowait()
        except queue.Empty:
            # 队列空：让出 event loop，让 uvicorn 把已 yield 的数据真正 flush 出去
            if not thread.is_alive() and event_queue.empty():
                break
            await asyncio.sleep(0.02)
            continue

        if item is None:
            break

        yield _sse(item["type"], {k: v for k, v in item.items() if k != "type"})
        # 关键：每条事件后让出控制权，确保逐条抵达前端而非批量堆积
        await asyncio.sleep(0)

    # 缓存结果供后续分析/预测复用（key 含 ACL 指纹，防越权复用）；
    # 报告路径没有 service（不走问数管道），跳过
    if rr is None and service.sql_result.get("rows"):
        from security.enforcer import acl_fingerprint
        cache_result(query, getattr(service, "executed_sql", "") or service.sql,
                     service.sql_result, service.schema_context,
                     acl_fp=acl_fingerprint(getattr(service, "acl", None)))

    await asyncio.to_thread(thread.join, 1)


async def _bridge(sync_gen_factory) -> AsyncGenerator[str, None]:
    """把同步生成器桥接成异步 SSE 流

    同步生成器直接在 async 函数里 for 循环会阻塞 event loop，
    这里放到后台线程执行，主协程非阻塞轮询队列，保证逐条实时下发。
    """
    q: queue.Queue = queue.Queue()

    def _runner():
        try:
            for event in sync_gen_factory():
                q.put(event)
        except Exception as exc:
            q.put({"type": "error", "content": str(exc)})
        finally:
            q.put(None)

    th = threading.Thread(target=_runner, daemon=True)
    th.start()

    yield ": stream-open\n\n"

    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            if not th.is_alive() and q.empty():
                break
            await asyncio.sleep(0.02)
            continue

        if item is None:
            break

        yield _sse(item["type"], {k: v for k, v in item.items() if k != "type"})
        await asyncio.sleep(0)

    await asyncio.to_thread(th.join, 1)


async def analysis_stream(sql: str, query: str, sql_result: dict) -> AsyncGenerator[str, None]:
    """独立分析流式端点"""
    async for chunk in _bridge(lambda: generate_analysis(query, sql, sql_result)):
        yield chunk


async def predict_stream(sql_result: dict) -> AsyncGenerator[str, None]:
    """独立预测流式端点"""
    async for chunk in _bridge(lambda: generate_predict(sql_result)):
        yield chunk
