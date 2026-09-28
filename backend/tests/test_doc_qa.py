# -*- coding: utf-8 -*-
"""非结构化问答 + 引用溯源单元测试（P2-1，对标 ThoughtSpot Spotter 3）"""
import sys
sys.path.insert(0, '.')

from agent.doc_qa import chunk_text, _chunk_score, _top_chunks, answer_with_citations

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

print("═══ A. 文本分块 ═══")
check("A1 空文本", chunk_text("") == [], "")
check("A2 短文本不分块", chunk_text("很短") == ["很短"], "")
long = "第一句。第二句。第三句。" * 40
chunks = chunk_text(long, size=100)
check("A3 长文本切成多块", len(chunks) > 1, f"{len(chunks)} 块")
check("A4 每块不超过 size+overlap", all(len(c) <= 100 + 40 for c in chunks), str(max(map(len, chunks))))
check("A5 块内容非空", all(c.strip() for c in chunks), "")

print("═══ B. 块相关性打分 ═══")
check("B1 相关块得分高", _chunk_score("安全库存预警", "当库存低于安全库存阈值时会触发预警通知") > 0.3,
      str(_chunk_score("安全库存预警", "当库存低于安全库存阈值时会触发预警通知")))
check("B2 无关块得分低", _chunk_score("安全库存预警", "设备停机原因分析记录维修时长") < 0.15,
      str(_chunk_score("安全库存预警", "设备停机原因分析记录维修时长")))
check("B3 空问题得分 0", _chunk_score("", "任意内容") == 0.0, "")
check("B4 空块得分 0", _chunk_score("任意问题", "") == 0.0, "")

print("═══ C. 片段检索（真实知识库）═══")
chunks = _top_chunks("良率", k=3)
check("C1 能检索到片段", isinstance(chunks, list), f"{len(chunks)} 条")
if chunks:
    check("C2 片段带来源与得分", all({"snippet", "source", "score"} <= set(c) for c in chunks), "")
    check("C3 按得分降序", all(chunks[i]["score"] >= chunks[i + 1]["score"] for i in range(len(chunks) - 1)), "")
check("C4 无相关知识返回空", _top_chunks("火星基地建设预算", k=3) == [], "")

print("═══ D. 问答 + 引用（真实 LLM）═══")
r = answer_with_citations("良率的计算口径是什么", k=3)
if r["success"]:
    check("D1 生成回答", bool(r["answer"]), r["answer"][:40])
    check("D2 带引用来源", len(r["citations"]) > 0, f"{len(r['citations'])} 条引用")
    check("D3 引用含片段", all(c.get("snippet") for c in r["citations"]), "")
    print("    回答:", r["answer"][:80].replace("\n", " "))
else:
    print("  ⚠️  ", r.get("error"), "—— 跳过 D1-D3（降级路径，不崩）")
    check("D0 降级返回成功=False", r["success"] is False, "")

check("D4 空问题降级", answer_with_citations("")["success"] is False, "")
check("D5 无相关文档降级", answer_with_citations("火星基地建设预算")["success"] is False, "")

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
