"""当前请求的 ACL 上下文（ContextVar）

用途：指标注册表（metric_registry）、编译器（metric_compiler）等模块是「无参函数链」，
无法逐层透传用户身份。这里用 ContextVar 挂当前 ACL，使「指标级权限（同一指标按角色不同口径）」
能在 NL→MQL→SQL 全链路一致生效。

注意：SSE 流式查询在后台线程执行（threading.Thread 不继承 ContextVar），
因此 LLMService.run() 内部会再次 set_acl(self.acl)，保证任何执行线程都拿到正确上下文。
"""

from __future__ import annotations

from contextvars import ContextVar

_current_acl: ContextVar = ContextVar("current_acl", default=None)


def set_acl(acl) -> None:
    _current_acl.set(acl)


def get_acl():
    try:
        return _current_acl.get()
    except LookupError:
        return None


def clear_acl() -> None:
    _current_acl.set(None)
