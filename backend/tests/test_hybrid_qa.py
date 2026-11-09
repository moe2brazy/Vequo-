# -*- coding: utf-8 -*-
"""混合问答（非结构化+数据融合，对标 Spotter 3）测试。

只测确定性部分：文档检索、引用标注组装、惰性检索、无文档兜底。
LLM 融合引用已在端到端手动验证（分析中出现 [1] 标注），此处不依赖 LLM。

2026-11-09 修复（P0）：这个文件以前往**共享的持久化知识库**写测试文档，
但清理时调用 `delete_memory(id, kind="documentation")` —— 实际签名只有
`memory_id`，多传的 kind 会抛 TypeError，又被 `except Exception: pass` 吞掉。
结果「混合问答测试：良率 = …」永久留在库里，被真实问答当成业务口径引用成
[1][2][3]，用户看到一段引着测试文档、说「无法回答」的分析结论。

现在：① 用唯一标记，只删本测试自己写的内容，绝不误删用户文档；
② 删掉错误的 kind= 参数；③ try/finally 保证异常/中断也清理；
④ 顺带把历史残留的同批测试文档一起清掉。
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_passed = 0
_failed = 0

_TEST_TAG = "__HYBRID_QA_UNIT_TEST__"
# 历史残留（旧版本写进去、没删掉的），跑一次就清一次
_LEGACY_MARK = "混合问答测试"


def check(name: str, cond: bool, detail: str = ""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}  {detail}")


def cleanup(m):
    """只删测试文档。list_memories 默认 limit=50，这里放大避免漏掉早期的。"""
    try:
        for mem in m.list_memories(limit=10000).get("documentations", []):
            content = str(mem.get("content", ""))
            if _TEST_TAG in content or _LEGACY_MARK in content:
                # delete_memory(memory_id) —— 不要传 kind=，会 TypeError
                m.delete_memory(mem["id"])
    except Exception as e:
        print(f"  ⚠️ 清理测试文档失败: {e}")


print("═══ A. 文档检索与引用组装 ═══")
from agent.memory import get_memory

m = get_memory()
cleanup(m)
try:
    # 文档里带上 query 的原话：_hybrid_score 用 bigram 分词打分，只写「良率 = …」
    # 时「各产线的良率对比」的命中率不足 0.1 阈值（实测 0.078），A 组会假红。
    # 这正是「引用错误文档比不引用更糟」的阈值在起作用，测试文档要贴合真实问法。
    m.add_documentation(f"{_TEST_TAG}：各产线的良率对比口径。良率 = 良品数 / 投入数 × 100%。", source="test")
    m.add_documentation(f"{_TEST_TAG}：安全库存是补货预警的阈值。", source="test")

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
    s4 = LLMService("各产线的良率对比")
    s4._retrieve_docs(k=2)
    # 原来只有 for 里一条断言，检索不到时整组 0 断言也显示「通过」——补一条兜底
    check("D0 至少命中 1 条文档", len(s4._cited_docs) >= 1, str(s4._cited_docs))
    for d in s4._cited_docs:
        check("D 引用结构完整 [index+content]", "index" in d and "content" in d and d["content"], str(d))
        break

    print("═══ E. 测试文档不污染共享库 ═══")
    leftover = [x for x in m.list_memories(limit=10000).get("documentations", [])
                if _TEST_TAG in str(x.get("content", "")) or _LEGACY_MARK in str(x.get("content", ""))]
    # 断言放在 finally 之前（此时文档还在，finally 之后才删）
    check("E1 测试期间文档确实写入", len(leftover) == 2, str(len(leftover)))
finally:
    cleanup(m)

leftover_after = [x for x in m.list_memories(limit=10000).get("documentations", [])
                  if _TEST_TAG in str(x.get("content", "")) or _LEGACY_MARK in str(x.get("content", ""))]
check("E2 清理后零残留", len(leftover_after) == 0, str(len(leftover_after)))

print()
print("=" * 40)
print(f"通过 {_passed} 项，失败 {_failed} 项")
print("=" * 40)
sys.exit(1 if _failed else 0)
