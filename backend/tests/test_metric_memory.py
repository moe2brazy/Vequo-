"""第三层（指标语义层向量检索）单元测试。

覆盖：
- rebuild 重建数量 = 当前生效指标数
- BM25 降级路径对同义/换说法查询的召回（无 embedding 环境）
- 无词重叠查询返回空（不污染 prompt）
- 关键词 + RAG 统一入口 _retrieve_for_query 一致性
- 相关性下限：高阈值只保留最强项（防弱相关指标注入）
- 向量路径（mock embedding）混合检索不崩溃且能召回

运行：cd backend && python tests/test_metric_memory.py
"""

import os
import sys
import tempfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from agent.metric_memory import MetricMemory
from agent.metric_registry import get_effective_metrics, _retrieve_for_query


def _new_mm():
    return MetricMemory(db_path=tempfile.mktemp(suffix=".db"))


def test_rebuild_count():
    mm = _new_mm()
    info = mm.rebuild()
    eff = get_effective_metrics()
    assert info["rebuilt"] == len(eff), (info, len(eff))
    print("T1 rebuild OK:", info)
    return mm


def test_bm25_fuzzy_recall():
    mm = _new_mm()
    mm.rebuild()
    cases = [
        ("东西合格的比例", "良率"),
        ("仓库里还有多少货", "库存量"),
        # "设备坏了多少次" 语义等价于「设备故障/停机次数」——「坏了」≈故障停机，
        # 正确召回为 停机次数 / 非计划停机次数（别名"故障停机次数"）。
        # 早期期望"维护次数"依赖字符 unigram 噪声信号（"次"字巧合命中），
        # 修复向量去噪后按语义正确召回"停机"类指标。
        ("设备坏了多少次", "停机次数"),
    ]
    for q, expect in cases:
        hits = mm.retrieve(q, top_k=3)
        names = [m["name"] for m in hits]
        assert names, f"fuzzy query {q!r} returned empty"
        assert any(expect[:2] in n for n in names), (q, names)
        print(f"T2 {q!r} -> {names}")


def test_no_overlap_empty():
    mm = _new_mm()
    mm.rebuild()
    assert mm.retrieve("qwezxc随机串无意义", top_k=3) == []
    print("T3 no-overlap returns [] OK")


def test_keyword_unified():
    hits = _retrieve_for_query("各产线的产量")
    assert hits and "产量" in hits[0]["name"], [m["name"] for m in hits]
    print("T4 keyword unified OK:", [m["name"] for m in hits])


def test_threshold_floor():
    mm = _new_mm()
    mm.rebuild()
    # 高阈值（0.999）下只保留归一化后最强的一项，验证相关性下限过滤生效
    low = mm.retrieve("东西合格的比例", top_k=5)
    high = mm.retrieve("东西合格的比例", top_k=5, threshold=0.999)
    assert len(high) <= 1, (len(low), len(high))
    if low:
        # 高阈值结果应是低阈值结果的前缀子集
        low_names = [m["name"] for m in low]
        high_names = [m["name"] for m in high]
        assert set(high_names).issubset(set(low_names))
    print(f"T5 threshold floor OK: low={len(low)} high={len(high)}")


def test_vector_hybrid_mock():
    mm = _new_mm()
    # mock 一个确定性 embedding（字符计数向量），验证向量路径 + 混合不崩溃且能召回
    def fake_embed(text):
        vec = [0.0] * 16
        for ch in text:
            vec[ord(ch) % 16] += 1.0
        return vec

    mm._embed_fn = fake_embed
    mm._embed_tried = True
    mm.rebuild()
    info = mm.rebuild()
    assert info["embedded"] is True
    hits = mm.retrieve("良率是多少", top_k=3)
    assert hits, "vector path should return hits"
    names = [m["name"] for m in hits]
    assert any("良率" in n for n in names), names
    print("T6 vector hybrid OK:", names)


def test_with_scores_shape():
    mm = _new_mm()
    mm.rebuild()
    res = mm.retrieve("东西合格的比例", top_k=3, with_scores=True)
    assert all(set(x.keys()) == {"metric", "score"} for x in res), res
    if res:
        assert 0.0 <= res[0]["score"] <= 1.0
    print("T7 with_scores OK:", [(x["metric"]["name"], x["score"]) for x in res])


if __name__ == "__main__":
    test_rebuild_count()
    test_bm25_fuzzy_recall()
    test_no_overlap_empty()
    test_keyword_unified()
    test_threshold_floor()
    test_vector_hybrid_mock()
    test_with_scores_shape()
    print("\nALL THIRD-LAYER TESTS PASSED ✅")
