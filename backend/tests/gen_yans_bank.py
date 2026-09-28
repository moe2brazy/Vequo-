# -*- coding: utf-8 -*-
"""yans 大题库生成器：指标 × 维度 × 问法模板 自动组合 + 手工特殊问法。

用法: python -c "from gen_yans_bank import build_bank; qs = build_bank()"
生成的题库包含: 编译命中期望题(expect=group/total/rank/top/trend) + LLM兜底题(expect=llm)。
"""
import random
random.seed(20260901)

# 表 → {指标名: 该表可用维度列表}（yans 基准）
# 维度列表来自 _FACT_META dims + 时间列
TABLE_METRIC_DIMS = {
    "mes_process_output": {
        "产量": ["工序", "产线", "产品", "班次", "工单", "车间"],
        "良率": ["工序", "产线", "产品", "班次", "车间"],
        "不良数": ["工序", "产线", "产品", "班次", "车间"],
        "投入量": ["工序", "产线", "产品", "班次"],
        "返工数量": ["工序", "产线", "产品", "班次", "车间"],
        "返工率": ["工序", "产线", "产品", "班次", "车间"],
    },
    "qms_defect_detail": {
        "缺陷数": ["缺陷类型", "不良类型", "严重度", "严重程度", "工序", "检验"],
        "缺陷件数": ["缺陷类型", "严重度", "工序"],
        "严重缺陷数": ["缺陷类型", "严重度", "工序"],
    },
    "qms_inspection": {
        "抽检数": ["产品", "工序", "结果"],
        "质检合格率": ["产品", "工序", "结果"],
        "抽检不良数": ["产品", "工序"],
        "检验次数": ["产品", "工序", "结果"],
    },
    "mes_work_order": {
        "工单数": ["状态", "工单状态", "产品", "产线", "车间"],
        "计划数量": ["产品", "产线", "车间", "状态"],
        "在制工单数": ["产品", "产线", "车间"],
        "已完成工单数": ["产品", "产线", "车间"],
        "已关闭工单数": ["产品", "产线", "车间"],
    },
    "eqp_downtime_record": {
        "停机时长": ["停机原因", "原因", "设备", "设备类型", "产线", "车间", "计划类型"],
        "停机次数": ["停机原因", "设备", "设备类型", "产线", "车间", "计划类型"],
        "停机原因种类数": [],
        "平均停机时长": ["设备", "设备类型", "产线", "车间"],
        "非计划停机时长": ["设备", "产线", "车间"],
        "计划内停机次数": ["设备", "产线", "车间"],
        "计划内停机时长": ["设备", "产线", "车间"],
        "非计划停机占比": ["设备", "产线", "车间"],
    },
    "inv_inventory_snapshot": {
        "库存量": ["产品", "仓库"],
        "总库存": ["产品", "仓库"],
        "安全库存": ["产品", "仓库"],
        "缺货量": ["产品", "仓库"],
        "库存预警数": ["产品", "仓库"],
        "冻结库存": ["产品", "仓库"],
    },
    "dim_equipment": {
        "设备数": ["设备状态", "设备类型", "产线", "车间"],
        "运行设备数": ["设备类型", "产线", "车间"],
        "维护设备数": ["设备类型", "产线", "车间"],
        "空闲设备数": ["设备类型", "产线", "车间"],
    },
    "dim_product": {
        "产品数": ["产品类别", "类别"],
        "活跃产品数": ["产品类别"],
    },
    "dim_process": {
        "工序数": ["工序"],
        "标准良率": ["工序"],
        "关键工序数": [],
    },
    "dim_production_line": {
        "产线数": ["车间"],
    },
}

# 问法模板: (模板, expect)  {m}=指标名 {d}=维度 {N}=数字
TEMPLATES = [
    ("各{d}的{m}", "group"),
    ("按{d}统计{m}", "group"),
    ("各{d}的{m}排行", "rank"),
    ("各{d}的{m}排名", "rank"),
    ("{m}排行", "rank"),
    ("{m}最高的{d}是哪个", "top"),
    ("{m}最多的{d}是哪个", "top"),
    ("各{d}的{m}TOP{N}", "top"),
    ("总{m}", "total"),
    ("{m}是多少", "total"),
    ("近{N}天各{d}的{m}", "group"),
    ("本月各{d}的{m}", "group"),
    ("各{d}的{m}趋势", "trend"),
]

# 每指标各维度的主问法（避免模板爆炸，控制总量）
# 先手工保证每个指标×每个维度至少 1 题主问法，再补模板变体

def build_bank() -> list[dict]:
    bank = []
    seen = set()

    def add(q, expect):
        q = q.strip()
        if not q or q in seen:
            return
        seen.add(q)
        bank.append({"q": q, "expect": expect})

    # 1) 每个指标×每个维度：主问法「各{d}的{m}」
    for table, metrics in TABLE_METRIC_DIMS.items():
        for metric, dims in metrics.items():
            if not dims:
                continue
            for d in dims:
                add(f"各{d}的{metric}", "group")
            # 每指标 TOP-N 与 Top-1（用第一个维度）
            d0 = dims[0]
            add(f"各{d0}的{metric}排行", "rank")
            add(f"{metric}最高的{d0}是哪个", "top")
            add(f"{metric}最多的{d0}是哪个", "top")
            add(f"各{d0}的{metric}TOP10", "top")
            add(f"总{metric}", "total")
            add(f"{metric}是多少", "total")

    # 2) 时间变体（有时间列的表）
    for metric, dims in (("产量", ["工序", "产线"]), ("良率", ["产线"]),
                         ("停机时长", ["设备", "产线"]), ("停机次数", ["产线"]),
                         ("库存量", ["产品"]), ("工单数", ["产品"]),
                         ("抽检数", ["产品"])):
        for d in dims:
            add(f"近7天各{d}的{metric}", "group")
            add(f"近30天各{d}的{metric}", "group")
            add(f"本月各{d}的{metric}", "group")
            add(f"每日{metric}趋势", "trend")
            add(f"近30天{metric}", "total")

    # 3) 累计/对比/关系类（走专用编译或 LLM）
    add("本年累计产量", "llm")
    add("本月累计停机时长", "llm")
    add("产量与良率的对比", "group")
    add("各产线的产量与停机时长对比", "group")
    add("停机时长和停机次数的关系", "group")
    add("上月产量", "total")
    add("今年产量", "total")
    add("产量同比", "llm")
    add("良率环比", "llm")

    # 3.5) 计划类型/状态维度专属问法（新增维度）
    add("各计划类型的停机次数", "group")
    add("各计划类型的停机时长", "group")
    add("各计划类型的平均停机时长", "group")
    add("各车间的设备数", "group")
    add("各车间的运行设备数", "group")
    add("各车间的维护设备数", "group")

    # 4) 值过滤类（已确定性化的状态/枚举过滤指标 → total；未注册的走 LLM）
    add("已完成状态的工单数", "total")
    add("运行中的设备数", "total")
    add("维护中的设备数", "total")
    add("关键工序有多少个", "total")
    add("严重缺陷（critical）的数量", "total")
    add("检验结果为pass的抽检数", "llm")

    # 5) 模糊/防误伤问法（应回退 LLM 或编译总量，不能报错）
    add("产量", "total")
    add("停机", "total")
    add("良率趋势", "trend")
    add("各产线的产量趋势", "trend")

    # 6) 二维分组（仅直接分组维度；含 JOIN 维度组合回退 LLM）
    add("各状态各产品的工单数", "llm")
    add("各工序各班次的产量", "llm")

    return bank

if __name__ == "__main__":
    bank = build_bank()
    from collections import Counter
    c = Counter(b["expect"] for b in bank)
    print(f"生成 {len(bank)} 题: {dict(c)}")
