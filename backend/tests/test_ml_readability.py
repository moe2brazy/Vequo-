# -*- coding: utf-8 -*-
"""建模结果「看得懂」回归测试：恒等式别夸、系数别带量纲、外推别硬答。

背景（2026-11-09 真库实测）：用户问「用线性回归预测下个月产量」，系统返回
「模型能解释 100% 的波动，解释力很强，可以直接拿来做量级预估」，
驱动因素写「投入数量 重要性 451.93，占全部特征的 95%」。三个问题：

1. 答非所问：要的是未来时间点产量，给的是同一行记录的横截面回归；
   库内数据只到 2026-09-15，建模模块没有按日期外推的能力。
2. R²=1.0 是账面恒等式（good_qty = input_qty − defect_qty − rework_qty − 小损耗），
   不是模型能力，实测 2752 行 RMSE 仅 0.28 件。
3. 451.93 是带量纲的回归系数（≈ 该列标准差 450.63），不是「重要性」；
   量纲不同的系数不能排名，更不能算占比。

本文件守住修复后的三条线。不调 LLM；连库用例拿不到库时跳过而非判红。
"""
import inspect
import sys

sys.path.insert(0, '.')

from ml.interpret import build_facts, explain_ml_result
from agent.llm_service import LLMService

PASS, FAIL = 0, 0


def rec(name, inp, expect, actual, ok, note=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}\n       输入: {inp}\n       预期: {expect}\n       实际: {actual}")


def text_of(result):
    return explain_ml_result(result, use_llm=False).get("text", "")


# 真实训练返回的形状（数值取自 2026-11-09 本地 yans 库）
LEAK = {
    "model_name": "linear_mes_process_output_good_qty",
    "model_type": "linear",
    "model_label": "线性回归",
    "metrics": {"R²": 1.0, "RMSE": 0.28, "MAE": 0.25},
    "importance": {"input_qty": 0.9454, "defect_qty": 0.046, "rework_qty": 0.0085},
    "features": ["input_qty", "defect_qty", "rework_qty"],
    "target": "good_qty",
    "samples": 2752,
    "table": "mes_process_output",
    "data_note": "",
    "join_note": "",
}

print("=== 一、账面恒等式：结论必须翻过来讲 ===")
f_leak = build_facts(LEAK)
t_leak = text_of(LEAK)
rec("满分判定为疑似泄漏", "R²=1.0", "suspect_leak=True",
    f_leak["suspect_leak"], f_leak["suspect_leak"] is True)
rec("不再夸解释力", "R²=1.0", "不含「解释力很强」与「可以直接拿来做量级预估」",
    ("解释力很强" in t_leak, "可以直接拿来做量级预估" in t_leak),
    "解释力很强" not in t_leak and "可以直接拿来做量级预估" not in t_leak)
rec("明说不能当预测用", "R²=1.0", "含「不能当预测用」",
    "不能当预测用" in t_leak, "不能当预测用" in t_leak)
rec("说清为什么不能预测未来", "R²=1.0", "含「下个月」与「没有可以代入的数」",
    ("下个月" in t_leak, "没有可以代入的数" in t_leak),
    "下个月" in t_leak and "没有可以代入的数" in t_leak)
rec("不再给带量纲的重要性排名", "三列恒等式", "不含「占全部特征的」",
    "占全部特征的" in t_leak, "占全部特征的" not in t_leak)
rec("字段名不重复三遍", "三列恒等式", "「投入数量」出现不超过 1 次",
    t_leak.count("投入数量"), t_leak.count("投入数量") <= 1)
rec("正文不带英文列名括号", "三列恒等式", "不含「（input_qty）」",
    "（input_qty）" in t_leak, "（input_qty）" not in t_leak)
rec("驱动段改成业务关系说明", "三列恒等式", "含【这几个字段是什么关系】",
    "【这几个字段是什么关系】" in t_leak,
    "【这几个字段是什么关系】" in t_leak)
rec("给出可操作的换问法建议", "三列恒等式", "含「换一类问题」",
    "换一类问题" in t_leak, "换一类问题" in t_leak)

print("=== 二、正常结果不受影响，阈值不误伤 ===")
normal = dict(LEAK)
normal["metrics"] = {"R²": 0.8954, "RMSE": 12.34, "MAE": 8.1}
f_normal, t_normal = build_facts(normal), text_of(normal)
rec("0.8954 不判泄漏", "R²=0.8954", "suspect_leak=False",
    f_normal["suspect_leak"], f_normal["suspect_leak"] is False)
rec("正常结果仍给解释力结论", "R²=0.8954", "含「解释力」",
    "解释力" in t_normal, "解释力" in t_normal)
rec("正常结果仍列主要驱动因素", "R²=0.8954", "含【主要驱动因素】",
    "【主要驱动因素】" in t_normal, "【主要驱动因素】" in t_normal)
near = dict(LEAK)
near["metrics"] = {"R²": 0.9981, "RMSE": 150.48, "MAE": 116.61}
rec("计划量→实际产出 0.9981 不误判", "R²=0.9981", "suspect_leak=False",
    build_facts(near)["suspect_leak"], build_facts(near)["suspect_leak"] is False)
neg = dict(LEAK)
neg["metrics"] = {"R²": -0.12, "RMSE": 88.0, "MAE": 70.0}
t_neg = text_of(neg)
rec("负 R² 仍走失效分支", "R²=-0.12", "含「比直接用平均值猜还差」",
    "比直接用平均值猜还差" in t_neg,
    "比直接用平均值猜还差" in t_neg and "不能当预测用" not in t_neg)

print("=== 三、未来时间外推：拦住并给替代问法 ===")
should_block = ["用线性回归预测下个月产量", "预测未来一个月的产量",
                "下季度的良率会是多少", "明年产能预计多少"]
should_pass = ["用工序产量表按投入数量预测合格数量", "根据计划产量预测实际产量",
               "用抽检表建模预测不良数量", "用设备停机时长预测良率"]
for q in should_block:
    rec("拦下外推问法", q, "True",
        LLMService(q)._looks_like_ml_forecast(),
        LLMService(q)._looks_like_ml_forecast() is True)
for q in should_pass:
    rec("正常建模不误拦", q, "False",
        LLMService(q)._looks_like_ml_forecast(),
        LLMService(q)._looks_like_ml_forecast() is False)

svc = LLMService("用线性回归预测下个月产量")
evs = list(svc._respond_ml())
dones = [e for e in evs if e.get("type") == "done"]
steps = [e.get("name") for e in evs if e.get("type") == "step"]
err = (dones[0]["response"].get("error", "") if dones else "")
rec("拦截只出一个 done", "外推问法", "1 个 done",
    len(dones), len(dones) == 1)
rec("响应是能力说明而非假结果", "外推问法", "type=ml_error",
    dones[0]["response"].get("type") if dones else "无",
    bool(dones) and dones[0]["response"].get("type") == "ml_error")
rec("说明里交代数据边界", "外推问法", "含「2026-09-15」",
    "2026-09-15" in err, "2026-09-15" in err)
rec("说明里给可替代问法", "外推问法", "含「投入数量=1200」",
    "投入数量=1200" in err, "投入数量=1200" in err)
rec("没进 LLM 方案设计", "外推问法", "steps 只有 ML建模",
    steps, steps == ["ML建模"])

src = inspect.getsource(LLMService._respond_ml)
i_pred = src.find("_looks_like_ml_predict()")
i_fcst = src.find("_looks_like_ml_forecast()")
rec("续问推理判定在外推判定之前", "源码顺序", "predict 下标 < forecast 下标",
    (i_pred, i_fcst), 0 <= i_pred < i_fcst)

print("=== 四、线性模型重要性归一化（连库用例，拿不到库则跳过）===")
try:
    from ml.trainer import train_model
    r = train_model("mes_process_output", "good_qty",
                    ["input_qty", "defect_qty", "rework_qty"], "linear")
    imp = {k: v for k, v in r["importance"].items() if v is not None}
    total = sum(imp.values())
    rec("相对权重合计为 1", "linear + 三列", "≈1.0",
        round(total, 4), abs(total - 1.0) < 0.01)
    rec("不再出现带量纲的 451.93", "linear + 三列", "全部 <=1",
        round(max(imp.values()), 4), all(0 <= v <= 1 for v in imp.values()))
except Exception as e:
    print(f"  SKIP 连库用例（{type(e).__name__}: {str(e)[:60]}）")

print(f"\n{'=' * 56}\nTOTAL pass={PASS} fail={FAIL}")
sys.exit(1 if FAIL else 0)
