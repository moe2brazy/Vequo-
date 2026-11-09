# -*- coding: utf-8 -*-
"""真库验收：训练一次 → 按结果卡提示续问，确认不再走 SUM 查询。

对应 2026-11-09 报障的两段对话：
  ① 用工序产量表按投入数量预测合格数量
  ② 投入数量=1200 能出多少   ← 以前被判成 data，答 SUM(input_qty)=3,187,009

不调 LLM：续问判定在方案生成之前，整条链路是确定性的。
跑法（cwd=backend）：.venv/Scripts/python.exe -I -X utf8 tests/verify_ml_followup_real.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.trainer import train_model
from agent.llm_service import LLMService, _set_last_ml_model, _classify_intent

TABLE = "mes_process_output"
res = train_model(TABLE, "good_qty", ["input_qty"], "linear", {})
print("训练:", res["model_label"], res["metrics"], "features=", res["features"], "samples=", res.get("samples"))

_set_last_ml_model({
    "name": res["model_name"], "features": res["features"], "target": res["target"],
    "label": res["model_label"], "metrics": res["metrics"], "table": TABLE,
    "model_type": res.get("model_type", ""),
})

q = "投入数量=1200 能出多少"
intent = _classify_intent(q)
print("意图:", intent)
assert intent == "ml", f"续问没判成 ml，仍会走 SQL 查询：{intent}"

svc = LLMService(q)
done = None
for ev in svc._respond_ml():
    if ev.get("type") == "done":
        done = ev
r = (done or {}).get("response", {})
print("响应类型:", r.get("type"))
assert r.get("type") == "ml_result", f"不是推理结果：{r}"

mr = r.get("ml_result") or {}
print("预测行:", mr.get("pred_samples"))
print("解读:\n" + (mr.get("explain") or ""))
assert mr.get("pred_samples"), "没有预测行"
assert "量级估计" in (mr.get("explain") or ""), "缺人话结论"
print("VERIFY_OK")
