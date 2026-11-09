# -*- coding: utf-8 -*-
"""演示问法验收：逐条走真实 _respond_ml 链路（含 AI 方案生成），打印摘要。

用途：赛前把要上台输入的问题先跑一遍，确认「AI 方案真的解析成了预期字段、
指标可信、解读能讲」，而不是凭空写问法清单。依赖 LLM 配置与 yans 库。

跑法（cwd=backend）：.venv/Scripts/python.exe -I -X utf8 tests/verify_demo_questions.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.llm_service import LLMService  # noqa: E402

QS = [
    "用工序产量表按投入数量预测合格数量",
    "用工序产量表按投入数量随机森林预测合格数量",
    "投入数量=1200 能出多少",
    "用工序产量表按投入数量、不合格数量、返工数量预测合格数量",
    "用工序产量表做异常检测",
    "对工序产量表做 KMeans 聚类",
]

for q in QS:
    t0 = time.time()
    done = None
    try:
        for ev in LLMService(q)._respond_ml():
            if ev.get("type") == "done":
                done = ev
    except Exception as e:
        print(f"\n### {q}\n   EXCEPTION: {e}")
        continue
    r = (done or {}).get("response", {})
    mr = r.get("ml_result") or {}
    print(f"\n### {q}   ({time.time() - t0:.1f}s)")
    print("   type:", r.get("type"),
          "model:", mr.get("model_label") or (mr.get("model") or {}).get("label"))
    print("   features:", mr.get("features"), "target:", mr.get("target"),
          "samples:", mr.get("samples"))
    print("   metrics:", mr.get("metrics"))
    print("   pred:", mr.get("pred_samples"))
    ex = (mr.get("explain") or "").replace("\n", " | ")
    print("   explain:", ex[:300])
    print("   next:", mr.get("next_prompt"))
    if r.get("error"):
        print("   ERROR:", r.get("error"))
print("\nDONE")
