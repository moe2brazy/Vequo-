# -*- coding: utf-8 -*-
"""混合问答（非结构化+数据融合，对标 Spotter 3）测试。

只测确定性部分：文档检索、引用标注组装、惰性检索、无文档兜底。
LLM 融合引用已在端到端手动验证（分析中出现 [1] 标注），此处不依赖 LLM。
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


print("═══ A. 文档检索与引用组装 ═══")
from agent.memory import get_memory

m = get_memory()
# 清理测试文档，避免跨运行累积干扰
try:
    for mem in m.list_memories().get("documentations", []):
        if "混合问答测试" in str(mem.get("content", "")):
            m.delete_memory(mem.get("id"), kind="documentation")
except Exception:
    pass

m.add_documentation("混合问答测试：良率 = 良品数 / 投入数 × 100%。", source="test")
m.add_documentation("混合问答测试：安全库存是补货预警的阈值。", source="test")

from agent.llm_service import LLMService

s = LLMService("各产线的良率对比")
docs = s._retrieve_docs(k=3)
check("A1 检索命中良率文档", len(docs) >= 1, str(docs))
check("A2 片段含关键口径", docs and "良率" in docs[0], str(docs[0] if docs else ""))
check("A3 cited_docs 带 index", s._cited_docs and s._cited_docs[0]["index"] == 1, str(s._cited_docs))
check("A4 标记已检索", s._docs_retrieved is True, "")

print("═══ B. 惰性检索 ═══")
s2 = LLMService("各产品的库存情况")
check("B1 未检索前标记为 False", s2._docs_retrieved is False, "")
cd = s2._get_cited_docs()
check("B2 惰性检索触发", s2._docs_retrieved is True, "")
check("B3 返回 list", isinstance(cd, list), str(cd))

print("═══ C. 无文档兜底 ═══")
s3 = LLMService("完全无关的查询xyz")
cd3 = s3._get_cited_docs()
check("C1 无命中返回空", cd3 == [], str(cd3))
check("C2 空结果不崩", s3._retrieved_docs == [], "")

print("═══ D. 引用标注结构 ═══")
s4 = LLMService("良率的定义")
s4._retrieve_docs(k=2)
for d in s4._cited_docs:
    check(f"D 引用结构完整 [index+content]", "index" in d and "content" in d and d["content"], str(d))
    break

# 清理测试文档
try:
    for mem in m.list_memories().get("documentations", []):
        if "混合问答测试" in str(mem.get("content", "")):
            m.delete_memory(mem.get("id"), kind="documentation")
except Exception:
    pass

print()
print("=" * 40)
print(f"通过 {_passed} 项，失败 {_failed} 项")
print("=" * 40)
