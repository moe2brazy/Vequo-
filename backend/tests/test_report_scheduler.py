# -*- coding: utf-8 -*-
"""交付闭环（洞察推送 + 定时报表订阅）测试。

只测确定性部分：推送封装、订阅生命周期、到期调度、日报生成。
实际 webhook 发送由 notifier 已有测试覆盖，此处打桩隔离。
"""
from __future__ import annotations

import sys
import os
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}  {detail}")


print("═══ A. 洞察推送封装 ═══")
from agent.insight_scan import push_insights

ins = [{"type": "outlier", "table": "t1", "metric": "m1", "severity": 2.5,
        "message": "异常"},
       {"type": "imbalance", "table": "t2", "metric": "m2", "severity": 1.0,
        "message": "失衡"}]

# notifier 默认关 → 空转（pushed=0，但结构完整）
r = push_insights(ins)
check("A1 默认空转 pushed=0", r["success"] and r["pushed"] == 0 and r["total"] == 2, str(r))

# 空列表
r2 = push_insights([])
check("A2 空列表 total=0", r2["total"] == 0 and r2["pushed"] == 0, str(r2))

# 打桩 push_event 验证参数传递正确（event_type=insight、event_key 去重、severity 透传）
calls = []
with mock.patch("agent.notifier.push_event", side_effect=lambda *a, **kw: calls.append((a, kw)) or 1):
    r3 = push_insights(ins, max_push=5)
check("A3 打桩后 pushed=2", r3["pushed"] == 2, str(r3))
check("A4 事件类型正确", all(a[0] == "insight" for a, _ in calls), str(calls))
check("A5 event_key 含表+指标", "t1:m1:outlier" in calls[0][1]["event_key"], str(calls[0]))
check("A6 severity 透传", calls[0][1]["severity"] == 2.5, str(calls[0][1]))
# 无 message 的洞察跳过
r4 = push_insights([{"type": "outlier", "message": ""}])
check("A7 空 message 跳过", r4["pushed"] == 0 and r4["skipped"] == 1, str(r4))

print("═══ B. 定时订阅生命周期 ═══")
import agent.report_scheduler as RS

RS._schedules.clear()
s = RS.add_schedule("每日洞察日报", interval_min=1440)
check("B1 带 id", bool(s.get("id")), str(s))
check("B2 interval 透传", s["interval_min"] == 1440, str(s["interval_min"]))
check("B3 默认类型 insights", s["report_type"] == "insights", "")
check("B4 next_run 已设", s["next_run"] > time.time(), "")
check("B5 list 含 1 条", len(RS.list_schedules()) == 1, "")
check("B6 get 可查", RS.get_schedule(s["id"]) is not None, "")
check("B7 删除成功", RS.delete_schedule(s["id"]) is True, "")
check("B8 删除后为空", RS.list_schedules() == [], "")
check("B9 删除不存在返回 False", RS.delete_schedule("nonexist") is False, "")
check("B10 interval 最小 1 分钟", RS.add_schedule("x", interval_min=0)["interval_min"] == 1, "")

print("═══ C. 到期调度 ═══")
RS._schedules.clear()
dispatched = []
# 打桩 _dispatch，验证到期触发 + next_run 推进
with mock.patch.object(RS, "_dispatch", side_effect=lambda s: dispatched.append(s["id"])):
    sched = RS.add_schedule("测试", interval_min=10)
    sched["next_run"] = time.time() - 1  # 已到期
    RS._schedules = [sched]
    n = RS._run_due()
    check("C1 到期触发", n == 1 and sched["id"] in dispatched, str(n))
    check("C2 next_run 推进", sched["next_run"] > time.time(), str(sched["next_run"]))
    # 未到期不触发
    sched2 = RS.add_schedule("未到期", interval_min=10)
    sched2["next_run"] = time.time() + 9999
    RS._schedules = [sched2]
    n2 = RS._run_due()
    check("C3 未到期不触发", n2 == 0, str(n2))
RS._schedules.clear()

print("═══ D. 洞察日报生成 ═══")
rpt = RS._build_insights_report(max_insights=5)
check("D1 返回字符串", isinstance(rpt, str) and len(rpt) > 0, str(rpt)[:60])
# 无洞察时也返回说明性文本（打桩 scan_top_tables 返回空）
with mock.patch("agent.insight_scan.scan_top_tables", return_value={"success": True, "insights": []}):
    rpt2 = RS._build_insights_report()
check("D2 无洞察有兜底", "未发现" in rpt2 or "洞察" in rpt2, rpt2[:60])
# 扫描失败不崩
with mock.patch("agent.insight_scan.scan_top_tables", side_effect=Exception("boom")):
    rpt3 = RS._build_insights_report()
check("D3 扫描失败不崩", isinstance(rpt3, str), rpt3[:60])

print()
print("=" * 40)
print(f"通过 {_passed} 项，失败 {_failed} 项")
print("=" * 40)
