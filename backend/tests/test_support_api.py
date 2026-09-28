# -*- coding: utf-8 -*-
"""客服中心接口冒烟测试（AI 客服 + 人工工单 + 双向未读 + 越权 + 知识反馈）

运行（必须用后端 venv，系统 python 缺依赖）：
    cd backend && .venv/Scripts/python.exe tests/test_support_api.py

设计要点：
- 走真实 ASGI 路由（httpx.AsyncClient + ASGITransport），覆盖「游客建单 → 管理员回复 → 双向未读 →
  关闭 → 越权 → 知识反馈」闭环。本仓库 starlette 0.27 与 httpx 0.28 不兼容（TestClient 报
  unexpected keyword argument 'app'，且 ASGITransport 仅支持异步），故用 AsyncClient。
- **不碰真实工单存储**：把 agent.support 的 _STORE_PATH 指向临时文件，跑完即弃。
- 第 1 步用「我要转人工客服」命中规则短路，不调用大模型，因此离线也能跑。
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx                                 # noqa: E402

import main                                  # noqa: E402
from agent import support as S               # noqa: E402
from auth import create_token                # noqa: E402

_failed: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        _failed.append(name)


async def run():
    tmp = os.path.join(tempfile.gettempdir(), "vequo_support_smoke.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    S._STORE_PATH = tmp          # 隔离存储，不污染 backend/data/support_tickets.json

    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                          base_url="http://test", timeout=60)
    H_USER = {"X-Client-Id": "smoke1"}

    print("1) AI 客服 · 明确要人工 → 规则直通转人工（不调模型）")
    r = await c.post("/api/support/ai-chat",
                     json={"messages": [{"role": "user", "content": "我要转人工客服"}],
                           "client_id": "smoke1"})
    check("ai-chat 200 且 need_human",
          r.status_code == 200 and r.json().get("need_human") is True, str(r.status_code))

    print("2) 游客建单（携带转人工摘要）")
    r = await c.post("/api/support/conversations",
                     json={"client_id": "smoke1", "user_name": "冒烟用户",
                           "summary": "【AI 客服未能解决】用户问题：如何开通权限"})
    conv = r.json().get("conversation") or {}
    cid = conv.get("id")
    check("建单成功且带摘要",
          r.status_code == 200 and bool(cid) and len(conv.get("messages") or []) == 1, str(cid))

    print("3) 用户追加一条消息")
    r = await c.post(f"/api/support/conversations/{cid}/messages",
                     json={"text": "我需要看生产表", "client_id": "smoke1"}, headers=H_USER)
    check("发消息 200", r.status_code == 200)

    print("4) 游客侧未读（应为 0：客服还没回）")
    r = await c.get("/api/support/unread", headers=H_USER)
    check("unread=0", r.json().get("unread") == 0)

    print("5) 管理员令牌")
    tok = create_token("admin", "admin")
    H_ADMIN = {"Authorization": f"Bearer {tok}"}
    check("拿到 token", bool(tok))

    print("6) 管理员看会话列表（应能看到该单，未读≥1）")
    r = await c.get("/api/support/conversations", headers=H_ADMIN)
    data = r.json()
    row = next((x for x in data.get("conversations", []) if x["id"] == cid), None)
    check("管理员可读列表",
          r.status_code == 200 and data.get("is_admin") is True and row is not None, str(r.status_code))
    check("管理员未读≥1", bool(row) and (row.get("unread") or 0) >= 1, str(row and row.get("unread")))

    print("7) 管理员以「人工客服」身份回复")
    r = await c.post(f"/api/support/conversations/{cid}/messages",
                     json={"text": "已收到，请说明需要哪些表"}, headers=H_ADMIN)
    check("sender=agent",
          r.status_code == 200 and r.json().get("message", {}).get("sender") == "agent", str(r.status_code))

    print("8) 游客侧未读（应为 1）")
    r = await c.get("/api/support/unread", headers=H_USER)
    check("unread=1", r.json().get("unread") == 1)

    print("9) 游客打开会话后未读清零")
    await c.get(f"/api/support/conversations/{cid}/messages", headers=H_USER)
    r = await c.get("/api/support/unread", headers=H_USER)
    check("unread=0", r.json().get("unread") == 0)

    print("10) 越权：其它游客访问该会话应 403")
    r = await c.get(f"/api/support/conversations/{cid}/messages", headers={"X-Client-Id": "smoke2"})
    check("403", r.status_code == 403)

    print("11) 结束会话（仅管理员）")
    r = await c.post(f"/api/support/conversations/{cid}/close", headers=H_ADMIN)
    check("close 200", r.status_code == 200)

    print("12) 知识反馈 → 人工客服待办 → 管理员处理并回复")
    r = await c.post("/api/support/knowledge-feedback",
                     json={"item_key": "met:抽检数", "item_kind": "业务指标", "item_title": "抽检数",
                           "item_scene": "质量分析", "note": "口径写错了", "client_id": "smoke1",
                           "user_name": "冒烟用户"}, headers=H_USER)
    body = r.json() if r.status_code == 200 else {}
    msg = body.get("message") or {}
    check("反馈落单且为大号待办", r.status_code == 200 and msg.get("kind") == "feedback", str(r.status_code))
    if msg.get("id"):
        r = await c.post(f"/api/support/feedback/{msg['id']}/status",
                         json={"status": "rejected", "admin_note": "核实无误",
                               "reply": "经核实该条目内容无误"}, headers=H_ADMIN)
        check("管理员标记反馈不成立并回复",
              r.status_code == 200 and bool(r.json().get("reply_message")), str(r.status_code))

    await c.aclose()
    if os.path.exists(tmp):
        os.remove(tmp)

    print()
    if _failed:
        print(f"结果: FAIL（{len(_failed)} 项未通过）→ " + "; ".join(_failed))
        return 1
    print("结果: PASS（全部通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
