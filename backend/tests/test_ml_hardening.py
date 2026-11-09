# -*- coding: utf-8 -*-
"""建模模块防御性修复回归测试（2026-11-09 全模块审查）。

审查发现的问题里，有一类最危险：不报错、不崩，但悄悄给出反的结论。
这个文件守的就是这类静默错误，外加几种会让整轮建模崩掉的解析异常。

覆盖：
1. 敏感列精确匹配 —— is_key_process（关键工序标识）不能被 "key" 子串误杀
2. 中文否定限定词 —— 「不合格数量」必须落 defect_qty，不能落 good_qty
3. 训练参数 —— NaN/inf 整项丢弃、超参透传且默认值不变
4. 数据清洗 —— 全空列 / 幻觉列 / 文本目标给中文提示，不抛 KeyError 或英文异常
5. 结果上屏 —— importance 里的 NaN 不许变成裸 NaN 非法 JSON
6. 简单推理 —— 输入 nan/inf 给中文提示，正常输入结果不变
7. 方案解析 —— Infinity / params:null / 尾部文字不穿透、非法模型 valid=False
8. 结果解释 —— 指标算不出来时要明说，不能装作正常

不连库：用 monkeypatch 注入内存行 / 假模型，纯函数全覆盖。
"""
import sys

sys.path.insert(0, '.')

import json
import pandas as pd
import numpy as np

import ml.trainer as T
from ml.trainer import (_clamp_train_params, _SENSITIVE_COLUMNS, _ident_ok,
                        _safe_metrics, predict, train_model, _MODEL_STORE, _store_key)
from ml.interpret import explain_ml_result
from agent.ml_plan import unmatched_discriminator, resolve_column
from agent.ml_intent import _parse_ml_intent, generate_ml_intent, generate_ml_intent_stream
import agent.ml_intent as MI

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


def rows(n=30, bad=None, with_ok2=False):
    out = []
    for i in range(n):
        r = {"bad": bad, "ok": float(i), "y": float(i) * 2.0}
        if with_ok2:
            r["ok2"] = float(i) * 0.5
        out.append(r)
    return out


def use_rows(r):
    T.execute_sql = lambda sql: {"success": True, "rows": r}


def expect_error(tag, fn, keyword):
    try:
        fn()
        rec(tag, "应抛 ValueError", f"含「{keyword}」", "未抛异常", False)
    except ValueError as e:
        msg = str(e)
        rec(tag, "应抛 ValueError", f"含「{keyword}」", msg[:70], keyword in msg)
    except Exception as e:
        rec(tag, "应抛 ValueError", f"含「{keyword}」", f"{type(e).__name__}: {e}"[:70], False)


print("\n=== 1. 训练参数钳制 ===")

rec("NaN 参数整项丢弃", "n_clusters=nan", "{}", _clamp_train_params({"n_clusters": float("nan")}),
    _clamp_train_params({"n_clusters": float("nan")}) == {})
rec("inf 参数整项丢弃", "contamination=inf", "{}", _clamp_train_params({"contamination": float("inf")}),
    _clamp_train_params({"contamination": float("inf")}) == {})
rec("字符串 nan 也丢弃", "n_clusters='nan'", "{}", _clamp_train_params({"n_clusters": "nan"}),
    _clamp_train_params({"n_clusters": "nan"}) == {})
cl = _clamp_train_params({"n_clusters": 3.9, "n_estimators": 5, "max_depth": 2, "max_iter": 2000})
rec("整数化 + 范围钳制", "3.9/5/2/2000", "4/10/2/2000", cl,
    cl == {"n_clusters": 4, "n_estimators": 10, "max_depth": 2, "max_iter": 2000})
rec("上限钳制", "n_estimators=5000", "500",
    _clamp_train_params({"n_estimators": 5000}).get("n_estimators"),
    _clamp_train_params({"n_estimators": 5000}).get("n_estimators") == 500)
rec("未知参数丢弃", "hack=1", "{}", _clamp_train_params({"hack": 1}),
    _clamp_train_params({"hack": 1}) == {})
rec("白名单覆盖四类模型", "len(_ALLOWED_TRAIN_PARAMS)", ">=5", len(T._ALLOWED_TRAIN_PARAMS),
    len(T._ALLOWED_TRAIN_PARAMS) >= 5)
rec("不传参数时为空", "{}", "{}", _clamp_train_params({}), _clamp_train_params({}) == {})


print("\n=== 2. 敏感列精确匹配 ===")

biz = ["is_key_process", "keyword", "monkey", "process_seq", "plan_qty"]
killed = [c for c in biz if c.lower() in _SENSITIVE_COLUMNS]
rec("业务列不被误杀", "is_key_process / keyword / monkey", "[]", killed, killed == [])
rec("真敏感列仍拒绝", "password", "['password']",
    [c for c in ["password"] if c.lower() in _SENSITIVE_COLUMNS],
    [c for c in ["password"] if c.lower() in _SENSITIVE_COLUMNS] == ["password"])


print("\n=== 3. 数据清洗与目标校验 ===")

use_rows(rows(bad=None))
expect_error("全 NULL 特征给中文提示", lambda: train_model("t", "y", ["bad", "ok"], "linear"), "全为空值")

use_rows(rows(bad=float("nan")))
expect_error("全 NaN 特征不抛 KeyError", lambda: train_model("t", "y", ["bad", "ok"], "linear"), "全为空值")

use_rows(rows(bad=1.0))
expect_error("幻觉特征列给中文提示", lambda: train_model("t", "y", ["ghost", "ok"], "linear"), "不存在")

use_rows(rows(bad=1.0))
expect_error("幻觉目标列给中文提示", lambda: train_model("t", "ghost", ["bad", "ok"], "linear"), "不在表")

use_rows([{**r, "y": None} for r in rows(bad=1.0)])
expect_error("目标全空给中文提示", lambda: train_model("t", "y", ["bad", "ok"], "linear"), "全为空值")

use_rows([{**r, "y": "A" if i % 2 else "B"} for i, r in enumerate(rows(bad=1.0))])
expect_error("文本目标+线性回归给中文提示", lambda: train_model("t", "y", ["bad", "ok"], "linear"), "数值型目标")


print("\n=== 4. 超参真正被消费，默认值不变 ===")


class _FakeRF:
    last_kwargs = None

    def __init__(self, **kw):
        _FakeRF.last_kwargs = kw

    def fit(self, X, y):
        self.feature_importances_ = np.array([float("nan"), 0.5])
        return self

    def predict(self, X):
        return np.zeros(len(X))


_real_rf = T.RandomForestRegressor
T.RandomForestRegressor = _FakeRF
use_rows(rows(bad=1.0, with_ok2=True))
try:
    r = train_model("t", "y", ["ok", "ok2"], "random_forest")
    rec("默认参数与硬编码一致", "不传 params", "100/8", _FakeRF.last_kwargs,
        _FakeRF.last_kwargs.get("n_estimators") == 100 and _FakeRF.last_kwargs.get("max_depth") == 8)
    rec("NaN importance 变 None", "feature_importances_=[nan,0.5]", "含 None",
        list(r["importance"].values()), any(v is None for v in r["importance"].values()))
    dump = json.dumps({"importance": r["importance"]}, ensure_ascii=False)
    rec("结果里没有裸 NaN", "json.dumps", "不含 NaN 字面量", "NaN" in dump, "NaN" not in dump)
except Exception as e:
    rec("默认参数与硬编码一致", "不传 params", "100/8", f"{type(e).__name__}: {e}", False)
    rec("NaN importance 变 None", "-", "含 None", f"{type(e).__name__}: {e}", False)
    rec("结果里没有裸 NaN", "-", "不含 NaN", f"{type(e).__name__}: {e}", False)
finally:
    T.RandomForestRegressor = _real_rf

use_rows(rows(bad=1.0, with_ok2=True))
T.RandomForestRegressor = _FakeRF
try:
    train_model("t", "y", ["ok", "ok2"], "random_forest", params={"n_estimators": 30, "max_depth": 6})
    rec("自定义超参生效", "30/6", "30/6", _FakeRF.last_kwargs,
        _FakeRF.last_kwargs.get("n_estimators") == 30 and _FakeRF.last_kwargs.get("max_depth") == 6)
except Exception as e:
    rec("自定义超参生效", "30/6", "30/6", f"{type(e).__name__}: {e}", False)
finally:
    T.RandomForestRegressor = _real_rf


print("\n=== 5. 推理输入有限性 ===")

_X = pd.DataFrame({"a": [1.0, 2.0, 3.0]})
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler

_sc = StandardScaler().fit(_X)
_m = LinearRegression().fit(_sc.transform(_X), [2.0, 4.0, 6.0])
_MODEL_STORE[_store_key("hardening_lin")] = {
    "model": _m, "encoders": {}, "scaler": _sc, "le_target": None,
    "features": ["a"], "model_type": "linear", "model_label": "线性回归",
}
for bad in ("nan", "inf"):
    try:
        predict("hardening_lin", {"a": bad})
        rec(f"推理拒绝 {bad}", bad, "中文 ValueError", "未抛异常", False)
    except ValueError as e:
        rec(f"推理拒绝 {bad}", bad, "中文 ValueError", str(e)[:60], "有限数值" in str(e))
    except Exception as e:
        rec(f"推理拒绝 {bad}", bad, "中文 ValueError", f"{type(e).__name__}: {e}", False)
rec("正常推理结果不变", "a=2.0", "4.0", predict("hardening_lin", {"a": 2.0})["label"],
    predict("hardening_lin", {"a": 2.0})["label"] == "4.0")


print("\n=== 6. 表名校验 ===")

rec("schema.table 合法", "factory.wo", "True", _ident_ok("factory.wo"), _ident_ok("factory.wo"))
rec("注入串仍拒绝", 'wo"; DROP', "False", _ident_ok('wo"; DROP'), not _ident_ok('wo"; DROP'))


print("\n=== 7. 中文否定限定词语义 ===")

_c = [{"name": "sample_qty", "type": "integer"}, {"name": "defect_qty", "type": "integer"},
      {"name": "good_qty", "type": "integer"}, {"name": "plan_qty", "type": "integer"}]
r1 = resolve_column("不合格数量", _c, numeric_only=True)
rec("不合格数量落 defect_qty", "不合格数量", "defect_qty", r1, r1[0] == "defect_qty")
r2 = resolve_column("合格数量", _c, numeric_only=True)
rec("合格数量仍落 good_qty", "合格数量", "good_qty", r2, r2[0] == "good_qty")
r3 = resolve_column("不良数量", _c, numeric_only=True)
rec("不良数量仍落 defect_qty", "不良数量", "defect_qty", r3, r3[0] == "defect_qty")
rec("实际产量判定未被破坏", "实际产量 / plan_qty", "实际",
    unmatched_discriminator("实际产量", "plan_qty"),
    unmatched_discriminator("实际产量", "plan_qty") == "实际")
rec("非计划停机跳过计划列", "非计划停机 / planned_downtime_minutes", "非空",
    unmatched_discriminator("非计划停机", "planned_downtime_minutes"),
    bool(unmatched_discriminator("非计划停机", "planned_downtime_minutes")))


print("\n=== 8. 方案解析异常边界 ===")

for tag, src in [("Infinity", '{"output_spec":{"max_rows":Infinity}}'),
                 ("params=null", '{"feature_engineering":[{"op":"dropna","params":null}]}')]:
    try:
        _parse_ml_intent(src)
        rec(f"{tag} 不抛异常", tag, "不抛", "不抛", True)
    except Exception as e:
        rec(f"{tag} 不抛异常", tag, "不抛", f"{type(e).__name__}: {e}", False)

_good = ('{"data_request":{"table":"t","fields":["a"]},'
         '"model_training":{"model":"LinearRegression","params":{}}}')
_i, _e = _parse_ml_intent(_good + " 注：参数见上 }")
rec("尾部文字不破坏解析", "方案 JSON + 解释", "table=t",
    _i.get("data_request", {}).get("table"),
    _i.get("data_request", {}).get("table") == "t")

_bad = ('{"data_request":{"table":"t","fields":["a"]},'
        '"model_training":{"model":"BogusModel","params":{}}}')
_i2, _e2 = _parse_ml_intent(_bad)
rec("非法模型被校验器拦下", "BogusModel", "含「不在白名单」", _e2, "不在白名单" in _e2)


class _R:
    content = _bad


class _FakeLLM:
    def stream(self, msgs):
        return iter([])

    def invoke(self, msgs):
        return _R()


_real_llm = MI._llm
MI._llm = lambda **kw: _FakeLLM()
try:
    one = generate_ml_intent("测试", "schema", "t")
    rec("一次性接口标 valid=False", "非法模型", "False", one.get("valid"), one.get("valid") is False)
    evs = [e for e in generate_ml_intent_stream("测试", "schema", "t") if e.get("type") == "intent"]
    rec("流式接口标 valid=False", "非法模型", "False", evs[-1].get("valid") if evs else None,
        bool(evs) and evs[-1].get("valid") is False)
    rec("流式接口不吐非法 intent", "非法模型", "None", evs[-1].get("intent") if evs else "?",
        bool(evs) and evs[-1].get("intent") is None)
finally:
    MI._llm = _real_llm


print("\n=== 9. 结果解释：指标缺失要明说 ===")

_base = {"model_name": "m", "model_label": "线性回归", "features": ["x"], "target": "y",
         "samples": 50, "table": "t", "importance": {"x": 0.5}}
t1 = explain_ml_result({**_base, "model_type": "linear", "metrics": {"R²": None}}, use_llm=False)["text"]
rec("回归指标缺失有提示", "R²=None", "含「算不出来」", "算不出来" in t1, "算不出来" in t1)
t2 = explain_ml_result({**_base, "model_type": "logistic", "metrics": {"准确率": None}}, use_llm=False)["text"]
rec("分类指标缺失有提示", "准确率=None", "含「无法计算」", "无法计算" in t2, "无法计算" in t2)
rec("正常结果不受影响", "R²=0.9", "不含上述提示",
    explain_ml_result({**_base, "model_type": "linear", "metrics": {"R²": 0.9}}, use_llm=False)["text"],
    "算不出来" not in explain_ml_result({**_base, "model_type": "linear", "metrics": {"R²": 0.9}}, use_llm=False)["text"])

rec("_safe_metrics 把 NaN 转 None", "{'x':nan}", "{'x':None}",
    _safe_metrics({"x": np.float64(float("nan"))}),
    _safe_metrics({"x": np.float64(float("nan"))})["x"] is None)

print(f"\n{'=' * 56}\nTOTAL pass={PASS} fail={FAIL}")
sys.exit(1 if FAIL else 0)
