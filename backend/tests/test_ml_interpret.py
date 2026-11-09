# -*- coding: utf-8 -*-
"""建模结果解读测试：指标必须被翻成人话，且数字一个都不许错。

背景：结果卡原来只有指标标签和特征重要性条，业务用户看到「R²: 0.8954」
得自己在脑子里翻译。命题把「模型结果解释」写进能力清单，展示不等于解释。

这个文件守住的线：
1. 回归 / 分类 / 聚类 / 异常检测四类结果都能出三段式解读，段首对得上
2. 关键数字（R²、准确率、RMSE、样本量）必须出现，且格式是业务能读的样子
3. 指标分档边界要稳：0.80 不能叫「很强」，负数不能说成「偏弱」以下还给出建议
4. 容易误读的地方必须主动写出来：类别不均衡、异常比例是设定上限、小样本
5. LLM 改写路径的数字白名单能拦下编造的数字（这条守的是上屏正确性）
6. 空壳 / NaN / 全空 importance 不许把异常抛出去，也不许把 nan 写进文案

不连库、不调 LLM：全部走 use_llm=False 的规则路径。
"""
import sys

sys.path.insert(0, '.')

from ml.interpret import (build_facts, rule_text, explain_ml_result,
                          _band, _R2_BANDS, _ACC_BANDS, _numbers_ok, _num_tokens)

PASS, FAIL = 0, 0


def rec(name, inp, expect, actual, ok, note=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"{'✅' if ok else '❌'} {name}\n     输入: {inp!r}\n     预期: {expect}\n"
          f"     实际: {actual}{(' | ' + note) if note else ''}")


def text_of(result):
    return explain_ml_result(result, use_llm=False).get("text", "")


def plain(text):
    """去掉加粗标记，方便断言连续中文（**3** 组 里，"3 组" 是断开的）。"""
    return (text or "").replace("**", "")


def sections(text):
    """把文案切成 {段首: [条目...]}，模拟前端的 renderAnalysis 解析方式。"""
    out, cur = {}, None
    for line in (text or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("【") and s.endswith("】"):
            cur = s
            out[cur] = []
        elif cur:
            out[cur].append(s)
    return out


# ── 固件：四类真实形状的训练结果（取自 yans 库的列与量级）──────────────
REG = {
    "model_name": "random_forest_qms_inspection_defect_qty",
    "model_type": "random_forest", "model_label": "随机森林",
    "metrics": {"R²": 0.8954, "RMSE": 12.34, "MAE": 8.1},
    "importance": {"downtime_minutes": 0.42, "process_name": 0.28,
                   "equipment_name": 0.08, "shift": 0.05},
    "features": ["sample_qty", "inspection_result"],
    "target": "defect_qty", "samples": 1376, "table": "qms_inspection",
    "data_note": "原始 1502 行，剔除含缺失值记录后剩余 1376 行（缺失最多：order_status）",
    "join_note": "",
}

CLS = {
    "model_name": "random_forest_qms_defect_detail_defect_type",
    "model_type": "random_forest", "model_label": "随机森林",
    "metrics": {"准确率": 0.8723, "F1": 0.8611},
    "importance": {"process_name": 0.61, "severity": 0.39},
    "features": ["process_name", "severity"],
    "target": "defect_type", "samples": 2115, "table": "qms_defect_detail",
}

KM = {
    "model_name": "kmeans_mes_process_output_nosup",
    "model_type": "kmeans", "model_label": "KMeans聚类",
    "metrics": {"轮廓系数": 0.42, "聚类数": 3},
    "importance": {},
    "features": ["input_qty", "good_qty", "defect_qty"],
    "target": "", "samples": 2752, "table": "mes_process_output",
}

ISO = {
    "model_name": "isolation_mes_process_output_nosup",
    "model_type": "isolation", "model_label": "孤立森林",
    "metrics": {"异常比例": 0.1, "异常数": 275, "总样本": 2752},
    "importance": {},
    "features": ["input_qty", "good_qty"], "target": "",
    "samples": 2752, "table": "mes_process_output",
}

print("═══ A. 回归：指标要能读成一句话 ═══\n")

t_reg = text_of(REG)
sec = sections(t_reg)
rec("回归段首", REG["model_label"], "三段齐全",
    list(sec.keys()),
    all(h in sec for h in ("【这个模型能回答什么】", "【主要驱动因素】", "【用之前要知道】")))
rec("解释力百分比", "R²=0.8954", "文案里出现 89.5%", "89.5%" in t_reg, "89.5%" in t_reg)
rec("档位话术", "R²=0.8954", "较强", "较强" in t_reg, "较强" in t_reg)
rec("误差也要给", "RMSE=12.34", "出现 ±12.3", "±12.3" in t_reg, "±12.3" in t_reg)
rec("样本数带千分位", "samples=1376", "1,376", "1,376" in t_reg, "1,376" in t_reg)
rec("留出检验行数", "samples=1376", "276（sklearn 的 ceil(0.2×1376)）",
    "276" in t_reg, "276" in t_reg)
rec("目标列中文化", "target=defect_qty", "不良数量（defect_qty）",
    "不良数量（defect_qty）" in t_reg, "不良数量（defect_qty）" in t_reg)
rec("驱动因素排名", "importance 首位 downtime_minutes", "排第一 + 字段名上屏",
    "排第一" in t_reg and "downtime_minutes" in t_reg,
    "排第一" in t_reg and "downtime_minutes" in t_reg)
rec("占比不许被截成 1%", "share=9.64%", "显示 10%",
    "占全部特征的 10%" in t_reg, "占全部特征的 10%" in t_reg)
rec("合计占比与逐项对得上", "51+34+10", "合计 95%",
    "前三项合计 **95%**" in t_reg, "前三项合计 **95%**" in t_reg)
rec("数据折损写进限制", REG["data_note"], "含「剔除」与 1502",
    "剔除" in t_reg and "1502" in t_reg, "剔除" in t_reg and "1502" in t_reg)
rec("不出现 nan", "正常结果", "无 nan/None", "nan" not in t_reg.lower() and "None" not in t_reg,
    "nan" not in t_reg.lower())

print("\n═══ B. 分类：说准确率，别说 R²，并且要点破不均衡 ═══\n")

t_cls = text_of(CLS)
rec("分类用准确率", CLS["model_label"], "含 87.2% 与加权 F1",
    "87.2%" in t_cls and "F1" in t_cls, "87.2%" in t_cls and "F1" in t_cls)
rec("分类不提 R²", CLS["model_label"], "文案不含 R²", "R²" not in t_cls, "R²" not in t_cls)
rec("不均衡提醒", "class imbalance", "含「类别不均衡」",
    "类别不均衡" in t_cls, "类别不均衡" in t_cls)
rec("分类段首", CLS["model_label"], "【这个模型能回答什么】",
    "【这个模型能回答什么】" in text_of(CLS), "【这个模型能回答什么】" in text_of(CLS))

print("\n═══ C. 聚类 / 异常检测：各说各的，别混用回归话术 ═══\n")

t_km = text_of(KM)
rec("聚类段首", "kmeans", "【这次聚类做了什么】",
    "【这次聚类做了什么】" in t_km, "【这次聚类做了什么】" in t_km)
rec("聚类话术", "轮廓系数 0.42 + k=3", "含 3 组与 0.42",
    "3 组" in plain(t_km) and "0.42" in t_km, "3 组" in plain(t_km) and "0.42" in t_km)
rec("聚类不提 R²", "kmeans", "文案不含 R²", "R²" not in t_km, "R²" not in t_km)
rec("聚类说明无标准答案", "kmeans", "含「没给标准答案」",
    "没给标准答案" in t_km, "没给标准答案" in t_km)

t_iso = text_of(ISO)
rec("异常段首", "isolation", "【这次异常检测做了什么】",
    "【这次异常检测做了什么】" in t_iso, "【这次异常检测做了什么】" in t_iso)
rec("异常数量", "275 / 2752", "含 275 与 2,752",
    "275" in t_iso and "2,752" in t_iso, "275" in t_iso and "2,752" in t_iso)
rec("异常比例是设定值", "contamination=0.1", "含「设定的上限」",
    "设定的上限" in t_iso and "不代表" in t_iso, "设定的上限" in t_iso)

print("\n═══ D. 分档边界：0.80 不能叫「很强」，负数不许给正面建议 ═══\n")

for val, expect in ((0.95, "很强"), (0.90, "很强"), (0.80, "较强"), (0.79, "中等"),
                    (0.65, "中等"), (0.50, "偏弱"), (0.10, "很弱")):
    rec("R² 分档", val, expect, _band(val, _R2_BANDS)[0], _band(val, _R2_BANDS)[0] == expect)

for val, expect in ((0.99, "很高"), (0.95, "很高"), (0.86, "较高"), (0.70, "中等"), (0.61, "偏低")):
    rec("准确率分档", val, expect, _band(val, _ACC_BANDS)[0], _band(val, _ACC_BANDS)[0] == expect)

neg = dict(REG)
neg["metrics"] = {"R²": -0.12, "RMSE": 88.0, "MAE": 70.0}
t_neg = text_of(neg)
rec("负 R² 判失效", "R²=-0.12", "grade=失效（不是「偏弱」）",
    build_facts(neg)["grade"], build_facts(neg)["grade"] == "失效")
rec("负 R² 给处置建议", "R²=-0.12", "点明比取平均还差、建议换特征",
    "平均值猜还差" in t_neg and "换特征" in t_neg,
    "平均值猜还差" in t_neg and "换特征" in t_neg)
rec("负 R² 不写负数百分比", "R²=-0.12", "文案不含「-12.0%」",
    "-12.0%" in t_neg, "-12.0%" not in t_neg)

print("\n═══ E. 使用限制：容易被追问的点必须提前说明 ═══\n")

small = dict(REG); small["samples"] = 120
rec("小样本提示", "samples=120", "含「量级偏小」",
    "量级偏小" in text_of(small), "量级偏小" in text_of(small))

mid = dict(REG); mid["samples"] = 640
rec("中小样本提示", "samples=640", "含「中小样本」",
    "中小样本" in text_of(mid), "中小样本" in text_of(mid))

big = dict(REG); big["samples"] = 5000
rec("顶到加载上限", "samples=5000", "含「5000 行加载上限」",
    "5000 行加载上限" in text_of(big), "5000 行加载上限" in text_of(big))

perfect = dict(REG); perfect["metrics"] = {"R²": 0.9992, "RMSE": 0.1, "MAE": 0.1}
rec("接近满分要自查", "R²=0.9992", "含「接近满分」",
    "接近满分" in text_of(perfect), "接近满分" in text_of(perfect))

joined = dict(REG); joined["join_note"] = "「实际产量」不在 mes_work_order 里，已按 work_order_id 从 mes_process_output 汇总 good_qty（SUM）后作为预测目标"
rec("跨表来源写进限制", "join_note", "含「跨表」或「汇总」",
    "汇总" in text_of(joined), "汇总" in text_of(joined))

print("\n═══ F. 脏数据：不许抛异常，也不许把 nan 写到屏幕上 ═══\n")

for name, bad in (("空 dict", {}),
                  ("空壳（有 samples 无指标）", {"model_label": "随机森林", "samples": 0}),
                  ("importance 全 None", {**REG, "importance": {"a": None, "b": None}}),
                  ("metrics 全 nan", {**REG, "metrics": {"R²": float("nan"), "RMSE": float("nan")}})):
    try:
        r = explain_ml_result(bad, use_llm=False)
        txt = r.get("text", "")
        ok = ("nan" not in txt.lower()) and isinstance(r.get("source"), str)
        note = f"source={r.get('source')}"
    except Exception as e:
        ok, txt, note = False, f"抛异常: {e}", ""
    rec("脏数据不崩", name, "返回 dict 且文案不含 nan", txt[:40] or "(空)", ok, note)

rec("空壳不出废话", "有 samples 无指标", "文案为空（不给「样本 0 行」）",
    repr(text_of({"model_label": "随机森林", "samples": 0})),
    text_of({"model_label": "随机森林", "samples": 0}) == "")

nofeat = {**REG, "features": []}
rec("无特征不崩", "features=[]", "文案非空且不出现「」空引号",
    text_of(nofeat)[:30], bool(text_of(nofeat)) and "「」" not in text_of(nofeat))

print("\n═══ G. 数字白名单：编造的数字不许上屏 ═══\n")

dump = "R² = 0.8954，RMSE = 12.34，样本 1376 行，留出 275 行"
base = rule_text(build_facts(REG))
rec("合规改写放行", "只用了已有数字", "True",
    _numbers_ok("解释力 89.5%，样本 1,376 行，前三项合计 95%", dump, base),
    _numbers_ok("解释力 89.5%，样本 1,376 行，前三项合计 95%", dump, base))
rec("折算成百分比也放行", "0.42 → 42%", "True",
    _numbers_ok("重要性 0.42，占 51%", dump, base),
    _numbers_ok("重要性 0.42，占 51%", dump, base))
rec("千分位不算新数字", "2,752", "True（去逗号后与 2752 同值）",
    _numbers_ok("共 2,752 条", "总样本 2752", base),
    _numbers_ok("共 2,752 条", "总样本 2752", base))
rec("编造的数字被拦", "凭空写 96.8%", "False",
    _numbers_ok("解释力 96.8%", dump, base), not _numbers_ok("解释力 96.8%", dump, base))
rec("个位数量词放行", "三项 / 2 段", "True",
    _numbers_ok("三项里有 2 项", dump, base), _numbers_ok("三项里有 2 项", dump, base))
rec("数字抽取不受逗号干扰", "1,376", "{1376}",
    _num_tokens("1,376"), _num_tokens("1,376") == {"1376"})

print("\n═══ H. 事实层：分档与任务判定要能对上 ═══\n")

f_reg = build_facts(REG)
rec("任务判定-回归", "random_forest + R²", "regression", f_reg["task"], f_reg["task"] == "regression")
rec("任务判定-分类", "random_forest + 准确率", "classification",
    build_facts(CLS)["task"], build_facts(CLS)["task"] == "classification")
rec("留出集计算", "samples=1376", "276（向上取整，与 sklearn 一致）",
    f_reg["test_rows"], f_reg["test_rows"] == 276)
rec("驱动合计占比", "前四项 0.42/0.28/0.08/0.05", "0.95（逐项四舍五入后相加）",
    f_reg["driver_coverage"], abs((f_reg["driver_coverage"] or 0) - 0.95) < 0.001)
rec("重要性字段总数", "4 项", "n_importance=4",
    f_reg["n_importance"], f_reg["n_importance"] == 4)

two = dict(REG); two["importance"] = {"sample_qty": 0.62, "shift": 0.38}
t_two = text_of(two)
rec("只有两个字段时不说「前三项」", "importance 两项", "含「就两个」，不含「前三项」",
    ("就两个" in t_two, "前三项" in t_two),
    "就两个" in t_two and "前三项" not in t_two)

one = dict(REG); one["importance"] = {"sample_qty": 1.0}
t_one = text_of(one)
rec("单特征不说「占 100%」", "importance 一项", "含「唯一的输入特征」，不含「占全部特征的」",
    ("唯一的输入特征" in t_one, "占全部特征的" in t_one),
    "唯一的输入特征" in t_one and "占全部特征的" not in t_one)

print(f"\n{'=' * 56}\nTOTAL pass={PASS} fail={FAIL}")
sys.exit(1 if FAIL else 0)
