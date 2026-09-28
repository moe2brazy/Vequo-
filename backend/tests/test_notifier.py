# -*- coding: utf-8 -*-
"""多渠道 IM 交付单元测试（P2-3）：通道/订阅 CRUD、消息格式化、节流"""
import sys
sys.path.insert(0, '.')

import agent.notifier as N

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

print("═══ A. 消息格式化（各渠道 markdown）═══")
for ctype, kw in [("wecom", "markdown"), ("dingtalk", "markdown"), ("feishu", "text"), ("generic", "text")]:
    ct, body = N._format_payload(ctype, "标题", "内容")
    check(f"A-{ctype} 格式正确", kw in body and "标题" in body, "")
ct, body = N._format_payload("unknown", "t", "c")
check("A-unknown 回退 generic", ct == "application/json" and "text" in body, "")

print("═══ B. 通道 CRUD ═══")
N._channels.clear(); N._subscriptions.clear()
c = N.add_channel("企微告警", "https://qyapi.weixin.qq.com/xxx", "wecom")
check("B1 新增通道", c["type"] == "wecom" and c["id"], str(c))
check("B2 非法类型回退 generic", N.add_channel("t", "http://x", "slack")["type"] == "generic", "")
check("B3 列表返回副本", len(N.list_channels()) == 2 and N.list_channels()[0] is not N._channels[0], "")
check("B4 删除通道", N.delete_channel(c["id"]) is True and len(N.list_channels()) == 1, "")
check("B5 删除不存在的返回 False", N.delete_channel("nope") is False, "")

print("═══ C. 订阅 CRUD + 过滤 ═══")
ch = N.add_channel("测试", "http://x")
s = N.add_subscription(ch["id"], ["monitor_alert", "insight"], min_severity=1.0, throttle_min=10)
check("C1 新增订阅", s["event_types"] == ["monitor_alert", "insight"], str(s))
check("C2 非法事件类型被过滤", N.add_subscription(ch["id"], ["bogus"])["event_types"] == ["insight"], "")
check("C3 删除订阅", N.delete_subscription(s["id"]) is True, "")

print("═══ D. 节流（同通道同事件在窗口内只推一次）═══")
N._channels.clear()
N._subscriptions.clear()  # C2 的"非法类型"用例会留下一个回退订阅，此处清干净
ch2 = N.add_channel("节流测试", "http://x")
s2 = N.add_subscription(ch2["id"], ["insight"], throttle_min=10)
# 打桩：不发真实 HTTP，只统计被调用的通道
sent = {"n": 0}
N._ENABLED = True
_orig_submit = N._pool.submit
N._pool.submit = lambda fn, ch, title, content: sent.update(n=sent["n"] + 1) or None
try:
    n1 = N.push_event("insight", "异常发现", "内容A", severity=2.0, event_key="key1")
    n2 = N.push_event("insight", "异常发现", "内容B", severity=2.0, event_key="key1")
    check("D1 首次推送", n1 == 1, f"n1={n1}")
    check("D2 同事件节流内不重复推", n2 == 0, f"n2={n2}")
    n3 = N.push_event("insight", "另一事件", "内容C", severity=2.0, event_key="key2")
    check("D3 不同事件可推", n3 == 1, f"n3={n3}")
    # 严重度低于订阅阈值 → 不推（用一个高阈值的独立订阅验证）
    s_hi = N.add_subscription(ch2["id"], ["insight"], min_severity=1.5)
    n4 = N.push_event("insight", "低严重", "内容D", severity=0.5, event_key="key3")
    # 此时有两条订阅：s2(阈值0) 会推、s_hi(阈值1.5) 不推 → 只推 1 条
    check("D4 低于阈值的那条订阅不推", n4 == 1, f"n4={n4}")
    N.delete_subscription(s_hi["id"])
finally:
    N._pool.submit = _orig_submit
    N._ENABLED = False
    N._channels.clear(); N._subscriptions.clear(); N._last_sent.clear()

check("D5 未启用时不推", N.push_event("insight", "t", "c", event_key="k") == 0, "")

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
