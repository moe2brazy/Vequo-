# -*- coding: utf-8 -*-
"""_rows_effectively_empty 自身的边界自测（先验函数，再接主链）。
覆盖：零行/None/一行全None/多行全None/部分有值/空串/0值/False值/字符串数字/
     非 dict 行/生成器/空 dict 行/嵌套 None 容器。
"""
import os, sys
BACKEND = r"D:\vequo\vequo-viqueo\backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.stdout.reconfigure(encoding="utf-8")
from agent.llm_service import _rows_effectively_empty as f

CASES = [
    # (输入, 期望, 说明)
    ([], True, "零行"),
    (None, True, "None"),
    ([{}], True, "单行空 dict"),
    ([{"r": None}], True, "★一行全 None（根因场景）"),
    ([{"r": None, "x": None}], True, "一行两列全 None"),
    ([{"a": None}, {"a": None}], True, "多行全 None"),
    ([{"a": None}, {"a": 1}], False, "部分行有值"),
    ([{"a": 0}], False, "★值 0 是合法答案，不能当空"),
    ([{"a": 0.0}], False, "值 0.0 合法"),
    ([{"a": False}], False, "值 False 合法（布尔）"),
    ([{"a": ""}], True, "空串算空"),
    ([{"a": "  "}], True, "纯空白算空"),
    ([{"a": "0"}], False, "字符串'0' 是值"),
    ([{"a": 1}], False, "普通有值"),
    ([(None,)], True, "tuple 行全 None"),
    ([(None, 5)], False, "tuple 行部分有值"),
    ([None], True, "行本身是 None"),
    (iter([{"a": None}]), True, "生成器输入"),
    (iter([]), True, "空生成器"),
    ([{"a": "N/A"}], False, "字符串 N/A 算值"),
]
fail = 0
for inp, exp, why in CASES:
    try:
        got = f(inp)
    except Exception as e:
        got = "EXC:%s" % type(e).__name__
        why += " ←抛异常"
    ok = got is exp or got == exp
    if not ok:
        fail += 1
    print("%s %-26s exp=%-5s got=%-8s %s" % ("OK  " if ok else "FAIL",
          str(inp)[:26], exp, got, why))

# 幂等/重复调用
r = [{"a": None}]
if not (f(r) and f(r) and f(r)):
    print("FAIL 重复调用结果不一致")
    fail += 1
else:
    print("OK   重复调用一致（无副作用）")

# 不改原对象
rows = [{"a": None}]
_ = f(rows)
if rows != [{"a": None}]:
    print("FAIL 改动了入参")
    fail += 1
else:
    print("OK   未改动入参")

print("\n结果: %s" % ("ALL PASS" if not fail else "FAIL=%d" % fail))
sys.exit(1 if fail else 0)