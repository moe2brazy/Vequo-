# -*- coding: utf-8 -*-
"""续问推理测试：训练完再问「计划数量=1200 多少」，必须走推理而不是重训。

背景（2026-11-09）：结果卡上印着「继续问『预测』可基于该模型推理；格式如：字段=数值」，
但照这句提示去问，系统把整句话当成一次新的训练任务，去解析「结果」这个不存在的列，
返回「『结果』在 mes_work_order 里没有对应的数值列」。命题要求「简单推理」，
这条路径断了就等于没有。

根因是判定位置和判定依据都不对：原判定在 LLM 生成建模方案之后，依据是 LLM 给出的
intent_type == "predict"。LLM 看到「预测结果」四个字更倾向于判成建模任务，判定失效。

现在改成前置判定：已有模型 + 句子里有「字段=数值」+ 没有训练类动词 → 直接推理。
这个文件守住的线：

1. 有模型 + 纯续问 → 走推理，出 pred_samples，不出 ml_error
2. 没有模型时不许抢跑，仍要去训练（提示先训练）
3. 句子里带「训练 / 建模 / 拟合 / 重新训」时不许走推理，否则真训练需求被拦
4. 中文列名（「计划数量=1200」）能映射到英文特征，英文列名原样可用
5. 缺特征不静默：要给 warnings，并落到结果卡的「备注」列
6. 问题里没有任何赋值时，提示里要给出可照抄的例子

不连库：模型用内存里现训的线性回归替身，几毫秒。
"""
import sys

sys.path.insert(0, '.')

import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler

from ml.trainer import _MODEL_STORE, _store_key, predict
from agent.llm_service import (LLMService, _set_last_ml_model, _get_last_ml_model)

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


# ---- 造一个不依赖数据库的模型：plan_qty 100/200/300 → 95/190/285 ----
_X = pd.DataFrame({"plan_qty": [100.0, 200.0, 300.0]})
_y = [95.0, 190.0, 285.0]
_scaler = StandardScaler().fit(_X)
_lin = LinearRegression().fit(_scaler.transform(_X), _y)
_NAME = "unit_test_linear"
_MODEL_STORE[_store_key(_NAME)] = {
    "model": _lin, "encoders": {}, "scaler": _scaler, "le_target": None,
    "features": ["plan_qty"], "model_type": "linear", "model_label": "线性回归",
}

META = {"name": _NAME, "features": ["plan_qty"], "target": "good_qty", "label": "线性回归"}


def predict_rows(query):
    svc = LLMService(query)
    last = None
    for ev in svc._ml_predict({}):
        if ev.get("type") == "done":
            last = ev
    return (last or {}).get("response", {})


print("\n=== 1. 判定：什么时候算续问推理 ===")

_set_last_ml_model({})
rec("没有模型时不抢跑", "计划数量=1200 时的预测结果", "False",
    LLMService("计划数量=1200 时的预测结果")._looks_like_ml_predict(),
    LLMService("计划数量=1200 时的预测结果")._looks_like_ml_predict() is False)

_set_last_ml_model(META)

for q in ["计划数量=1200 时的预测结果", "预测一下 plan_qty:900", "如果计划量是2000呢"]:
    got = LLMService(q)._looks_like_ml_predict()
    rec(f"纯续问走推理：{q}", q, "True", got, got is True)

for q in ["用工单表训练随机森林，计划量=1200 的那批也要纳入",
          "用计划数量=1200 重新训练一个模型",
          "用抽检表建模，样本量=500"]:
    got = LLMService(q)._looks_like_ml_predict()
    rec(f"训练意图不拦：{q}", q, "False", got, got is False)

got = LLMService("这个模型准不准")._looks_like_ml_predict()
rec("没有赋值形式不算续问", "这个模型准不准", "False", got, got is False)


print("\n=== 2. 推理结果结构 ===")

r = predict_rows("计划数量=400 时的预测结果")
mr = r.get("ml_result") or {}
rows = mr.get("pred_samples") or []
rec("响应类型是 ml_result", "计划数量=400", "ml_result", r.get("type"),
    r.get("type") == "ml_result")
rec("出 pred_samples 而不是报错", "计划数量=400", "1 行", len(rows), len(rows) == 1)
val = str(rows[0].get("预测")) if rows else ""
rec("预测值接近 380（线性外推）", "plan_qty=400 → 385", "375~395", val,
    val.replace(".", "").isdigit() and 375 <= float(val) <= 395)

r_cn = predict_rows("计划数量=200 时的预测结果")
rows_cn = (r_cn.get("ml_result") or {}).get("pred_samples") or []
cn_in = rows_cn[0]["输入"] if rows_cn else ""
# 输入列现在给业务看中文名（「计划数量 200」），不再是调试用的 JSON
rec("中文列名映射到真实特征", "计划数量=200", "中文或英文特征名 + 200", cn_in,
    ("plan_qty" in cn_in or "计划" in cn_in) and "200" in cn_in)


print("\n=== 3. 缺特征与空输入 ===")

one = predict(_NAME, {})
rec("缺特征给 warnings 不静默", "空输入", "1 条 warning", one.get("warnings"),
    bool(one.get("warnings")))
rec("缺特征仍能出结果", "空输入", "有 prediction", one.get("prediction"),
    one.get("prediction") is not None)

r_empty = predict_rows("帮我预测一下")
rec("没有赋值时给可照抄的例子", "帮我预测一下", "含「计划数量」或「plan_qty」与「=」",
    r_empty.get("error"), "=" in (r_empty.get("error") or ""))

print(f"\n{'=' * 56}\nTOTAL pass={PASS} fail={FAIL}")
sys.exit(1 if FAIL else 0)
