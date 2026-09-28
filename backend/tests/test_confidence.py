# -*- coding: utf-8 -*-
"""置信度 + 不确定性来源（P0-A，对标 Smartbi 白泽）确定性逻辑测试。

只测 _build_confidence 的规则分层（编译/缓存=high，LLM 路径按风险分档），
不依赖 LLM —— 置信度全部由确定性信号计算。
"""
from __future__ import annotations

import sys
import os

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


print("═══ A. 编译命中 → high ═══")
import agent.llm_service as L
from agent.llm_service import LLMService

s = LLMService.__new__(LLMService)  # 不跑 run()，只测 _build_confidence
s.compiled_mql = {"metric": "x"}
s._semantic_hit = False
s.matched_tables = [{"table_name": "a"}]
s.sql_result = {"rows": [{"a": 1}], "row_count": 1}
s._result_warning = ""
s._eval_score = 0
c = s._build_confidence()
check("A1 level=high", c["level"] == "high", str(c))
check("A2 score=95", c["score"] == 95, str(c["score"]))
check("A3 依据含编译器保证", any("编译器" in b for b in c["basis"]), str(c["basis"]))
check("A4 无风险", c["risks"] == [], str(c["risks"]))

print("═══ B. 语义缓存命中 → high(90) ═══")
s2 = LLMService.__new__(LLMService)
s2.compiled_mql = None
s2._semantic_hit = True
s2.matched_tables = [{"table_name": "a"}]
s2.sql_result = {"rows": [{"a": 1}], "row_count": 1}
s2._result_warning = ""
s2._eval_score = 0
c2 = s2._build_confidence()
check("B1 level=high", c2["level"] == "high", str(c2))
check("B2 score=90", c2["score"] == 90, str(c2["score"]))

print("═══ C. LLM 路径 + 无风险 → medium ═══")
s3 = LLMService.__new__(LLMService)
s3.compiled_mql = None
s3._semantic_hit = False
s3.matched_tables = [{"table_name": "a"}, {"table_name": "b"}]
s3.sql_result = {"rows": [{"a": 1, "b": "x"}], "row_count": 1}
s3._result_warning = ""
s3._eval_score = 80
c3 = s3._build_confidence()
check("C1 level=medium", c3["level"] == "medium", str(c3))
check("C2 score=75", c3["score"] == 75, str(c3["score"]))
check("C3 无重大风险", c3["risks"] == [], str(c3["risks"]))

print("═══ D. LLM 路径 + 表匹配歧义 → medium 带提示 ═══")
s4 = LLMService.__new__(LLMService)
s4.compiled_mql = None
s4._semantic_hit = False
s4.matched_tables = [{"table_name": "a"}, {"table_name": "b"}, {"table_name": "c"}, {"table_name": "d"}]
s4.sql_result = {"rows": [{"a": 1}], "row_count": 1}
s4._result_warning = ""
s4._eval_score = 80
c4 = s4._build_confidence()
check("D1 仍是 medium（仅歧义不降级）", c4["level"] == "medium", str(c4))
check("D2 风险含表匹配歧义", any("歧义" in r for r in c4["risks"]), str(c4["risks"]))

print("═══ E. LLM 路径 + 空值 → low ═══")
s5 = LLMService.__new__(LLMService)
s5.compiled_mql = None
s5._semantic_hit = False
s5.matched_tables = [{"table_name": "a"}]
s5.sql_result = {"rows": [{"a": 1}, {"a": None}, {"a": ""}], "row_count": 3}
s5._result_warning = ""
s5._eval_score = 80
c5 = s5._build_confidence()
check("E1 level=low", c5["level"] == "low", str(c5))
check("E2 score=55", c5["score"] == 55, str(c5["score"]))
check("E3 风险含空值", any("空值" in r for r in c5["risks"]), str(c5["risks"]))

print("═══ F. LLM 路径 + 复核告警 → low ═══")
s6 = LLMService.__new__(LLMService)
s6.compiled_mql = None
s6._semantic_hit = False
s6.matched_tables = [{"table_name": "a"}]
s6.sql_result = {"rows": [{"a": 1}, {"a": 2}], "row_count": 2}
s6._result_warning = "结果疑似答非所问"
s6._eval_score = 80
c6 = s6._build_confidence()
check("F1 告警 → low", c6["level"] == "low", str(c6))
check("F2 风险含告警", any("答非所问" in r for r in c6["risks"]), str(c6["risks"]))

print("═══ G. LLM 路径 + 评价低分 → low ═══")
s7 = LLMService.__new__(LLMService)
s7.compiled_mql = None
s7._semantic_hit = False
s7.matched_tables = [{"table_name": "a"}]
s7.sql_result = {"rows": [{"a": 1}, {"a": 2}], "row_count": 2}
s7._result_warning = ""
s7._eval_score = 45
c7 = s7._build_confidence()
check("G1 低分 → low", c7["level"] == "low", str(c7))
check("G2 风险含评价分", any("偏低" in r for r in c7["risks"]), str(c7["risks"]))

print()
print("=" * 40)
print(f"通过 {_passed} 项，失败 {_failed} 项")
print("=" * 40)
