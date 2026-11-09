# -*- coding: utf-8 -*-
"""建模续问推理的**意图路由**回归测试（2026-11-09 报障）。

用户照结果卡提示走完两步：
  ①「用工序产量表按投入数量预测合格数量」 → 训练出线性回归
  ②「投入数量=1200 能出多少」 → 期望：基于刚训的模型推一条，答「约 1180 件」

实际②被判成普通数据查询：生成 SUM(input_qty)=3,187,009，再配一段引着
「混合问答测试」文档、说「无法回答」的分析。根因不在推理函数，而在意图分类：
这句里没有任何建模词，规则只看到 weak_data 的「多少」→ data → LLM 复核也判 data，
_respond_ml() 里的续问判定根本没机会执行。

本文件守住：
1. 有模型 + 中文特征名/口语问法 → 规则高置信判 ml，跳过 LLM 复核
2. 没有模型时不许抢跑（仍按原规则分类）
3. 普通查询、训练需求、未来时间外推都不许被吞成 ml
4. 意图分类与 _respond_ml 的执行判定必须是同一套判据
5. 端到端推理给出人话结论（含预测值、误差量级）与 features

不连库：模型用内存里现训的线性回归替身，几毫秒。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler

from ml.trainer import _MODEL_STORE, _store_key
from agent.llm_service import (
    LLMService, _set_last_ml_model, _classify_intent, _classify_intent_rule,
    _looks_like_ml_followup_predict, _ml_next_prompt,
)

PASS, FAIL = 0, 0


def rec(name, inp, expect, actual, ok):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")
        print(f"         输入: {inp}")
        print(f"         期望: {expect}")
        print(f"         实际: {actual}")


# ---- 替身模型：input_qty 100/200/300 → 98/196/294（约 0.98 倍）----
_X = pd.DataFrame({"input_qty": [100.0, 200.0, 300.0]})
_y = [98.0, 196.0, 294.0]
_scaler = StandardScaler().fit(_X)
_lin = LinearRegression().fit(_scaler.transform(_X), _y)
_NAME = "unit_test_followup"
_MODEL_STORE[_store_key(_NAME)] = {
    "model": _lin, "encoders": {}, "scaler": _scaler, "le_target": None,
    "features": ["input_qty"], "model_type": "linear", "model_label": "线性回归",
}

META = {
    "name": _NAME, "features": ["input_qty"], "target": "good_qty",
    "label": "线性回归", "table": "mes_process_output",
    "metrics": {"R²": 0.9987, "RMSE": 15.89, "MAE": 13.14},
}


def rule(q):
    return _classify_intent_rule(q)


print("\n=== 1. 没有模型时不抢跑 ===")
_set_last_ml_model({})
# 只挑不含建模词的口语续问：带「预测」二字的句子本来就会走低置信 ml（LLM 复核），
# 那是既有行为，不是本次要守的线。
for q in ["投入数量=1200 能出多少", "input_qty=1200 能出多少"]:
    got = rule(q)
    rec(f"无模型不判 ml：{q}", q, "非 ml", got, got[0] != "ml")
rec("无模型：口语续问按原规则判 data", "投入数量=1200 能出多少", "data",
    rule("投入数量=1200 能出多少"), rule("投入数量=1200 能出多少")[0] == "data")
rec("无模型：英文列名续问按原规则分类", "input_qty=1200 能出多少", "非 ml",
    rule("input_qty=1200 能出多少"), rule("input_qty=1200 能出多少")[0] != "ml")


print("\n=== 2. 有模型：续问必须高置信判 ml ===")
_set_last_ml_model(META)
for q in ["投入数量=1200 能出多少", "投入数量=1200 大概能出多少合格品",
          "input_qty=1200 能出多少", "如果投入量是1200呢",
          "投入数量：1200，合格数量大概多少"]:
    got = rule(q)
    rec(f"判 ml 且高置信：{q}", q, ("ml", True), got, got == ("ml", True))
    rec(f"分类入口一致：{q}", q, "ml", _classify_intent(q), _classify_intent(q) == "ml")
    rec(f"执行判定一致：{q}", q, True,
        LLMService(q)._looks_like_ml_predict(),
        LLMService(q)._looks_like_ml_predict() is True)


print("\n=== 3. 不许误吞其它问句 ===")
for q in ["投入数量=1200 的记录有哪些", "统计投入数量=1200 的工单",
          "查询投入数量=1200 的明细", "列出投入数量=1200 的排行",
          "用工序产量表训练随机森林，投入量=1200 那批也纳入",
          "重新训练一个模型，投入数量=1200",
          "用工序产量表建模预测合格数量",
          "用线性回归预测下个月产量",
          "这个模型准不准"]:
    got = rule(q)
    rec(f"不抢跑：{q}", q, "非 ml 高置信续问", got,
        not (got[0] == "ml" and got[1] is True and _looks_like_ml_followup_predict(q)))

rec("无赋值不算续问", "这个模型能回答什么", False,
    _looks_like_ml_followup_predict("这个模型能回答什么"),
    _looks_like_ml_followup_predict("这个模型能回答什么") is False)
rec("无关字段不算续问", "设备编号=1200 能出多少", False,
    _looks_like_ml_followup_predict("设备编号=1200 能出多少"),
    _looks_like_ml_followup_predict("设备编号=1200 能出多少") is False)


print("\n=== 4. 端到端：推理要给人话结论 ===")
svc = LLMService("投入数量=1200 能出多少")
last = None
for ev in svc._ml_predict({}):
    if ev.get("type") == "done":
        last = ev
r = (last or {}).get("response", {})
mr = r.get("ml_result") or {}
rows = mr.get("pred_samples") or []
rec("响应 ml_result", "投入数量=1200 能出多少", "ml_result", r.get("type"),
    r.get("type") == "ml_result")
rec("出 pred_samples", "", "1 行", len(rows), len(rows) == 1)
if rows:
    v = float(rows[0]["预测"])
    rec("预测值落在 1100~1300", "", "1100~1300", v, 1100 <= v <= 1300)
    rec("输入用英文特征名", "", "input_qty", rows[0]["输入"],
        "input_qty" in rows[0]["输入"] and "1200" in rows[0]["输入"])
else:
    rec("预测值落在 1100~1300", "", "1100~1300", "无行", False)
ex = mr.get("explain") or ""
rec("有人话结论", "", "非空", ex[:60], bool(ex))
rec("结论带模型名", "", "线性回归", ex, "线性回归" in ex)
rec("结论带误差量级", "", "15.89", ex, "15.89" in ex)
rec("结论不承诺实际产出", "", "不是实际产出承诺", ex, "不是实际产出承诺" in ex)
rec("结果带 features 供前端拼提示", "", "input_qty", mr.get("features"),
    "input_qty" in (mr.get("features") or []))
rec("推理结果不带训练指标", "", "{}", mr.get("metrics"), mr.get("metrics") == {})


print("\n=== 5. 续问提示按任务类型分发 ===")
cases = [
    ("回归", {"model_type": "linear", "features": ["input_qty"], "metrics": {"R²": 0.9988}},
     "input_qty=1200 能出多少"),
    ("聚类", {"model_type": "kmeans", "features": ["input_qty", "good_qty"],
             "metrics": {"轮廓系数": 0.4532}}, "input_qty=1200 归到哪一组"),
    ("异常检测", {"model_type": "isolation", "features": ["input_qty"],
               "metrics": {"异常比例": 0.1}}, "input_qty=1200 是不是异常"),
    ("分类", {"model_type": "logistic", "features": ["sample_qty"],
             "metrics": {"准确率": 0.9}}, "sample_qty=1200 会判成哪一类"),
]
for name, res, expect in cases:
    got = _ml_next_prompt(res)
    rec(f"{name}提示：{expect}", name, expect, got, got == expect)
rec("无特征时不给提示", "", "", _ml_next_prompt({"features": []}),
    _ml_next_prompt({"features": []}) == "")


print("\n=== 6. 聚类模型的续问不写「约 簇 2」 ===")


class _FakeKMeans:
    def predict(self, X):
        return [2]


_km_cols = ["input_qty", "good_qty", "defect_qty", "rework_qty"]
_km_X = pd.DataFrame({c: [100.0, 200.0, 300.0] for c in _km_cols})
_NAME_KM = "unit_test_kmeans"
_MODEL_STORE[_store_key(_NAME_KM)] = {
    "model": _FakeKMeans(), "encoders": {}, "scaler": StandardScaler().fit(_km_X),
    "le_target": None, "features": _km_cols, "model_type": "kmeans",
    "model_label": "KMeans聚类",
}
_set_last_ml_model({
    "name": _NAME_KM, "features": _km_cols, "target": "", "label": "KMeans聚类",
    "model_type": "kmeans", "table": "mes_process_output",
    "metrics": {"轮廓系数": 0.4532, "聚类数": 3},
})
last_km = None
for ev in LLMService("input_qty=1200 能出多少")._ml_predict({}):
    if ev.get("type") == "done":
        last_km = ev
ex_km = ((last_km or {}).get("response", {}).get("ml_result") or {}).get("explain") or ""
rec("聚类续问出结论", "", "非空", ex_km[:60], bool(ex_km))
rec("结论说归到某组", "", "归到", ex_km, "归到" in ex_km)
rec("不写「约 簇」", "", "不含", ex_km, "约 簇" not in ex_km)
rec("不写产出承诺", "", "不含", ex_km, "产出承诺" not in ex_km)

print(f"\n{'=' * 56}\nTOTAL pass={PASS} fail={FAIL}")
sys.exit(1 if FAIL else 0)
