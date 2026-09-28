"""报告生成 Agent — 流式 + 结构化输出 + 数据分析专家

行业模板参数化：不再硬编码制造业口径（波峰焊/SMT 标杆等），
改为根据用户传入的行业/数据描述生成分析，避免切库后编造行业数据。
"""

from typing import AsyncIterator
from langchain_core.messages import HumanMessage, SystemMessage, AIMessageChunk
from config import LLM_CONFIG

# 行业/数据口径由调用方传入；默认通用制造业，但明确提示"只依据给定数据，不确定的不要编"
_REPORT_ANALYST_TPL = """你是一位拥有20年经验、供职于头部咨询公司的数据分析专家，输出风格专业、严谨、克制。

请按以下固定格式输出分析报告（严格遵循分隔符，使用纯文字，不要任何 markdown 标记符号）：

---执行摘要---
用2-3句话概括：整体状况概述、最关键的1个亮点、1个最大风险、核心建议方向。
要求：关键数字直接写在句中，每个结论必须带具体数据支撑（如"产量合计 1,234 件，环比下降 8.6%"），
不得使用无数据的空泛表述。用⚠️标记风险，✅标记亮点。

---数据整体分析---
基于给定数据描述整体状况与关键指标，指出明显异常或突出的部分，所有判断必须引用具体数值。
若数据中不含某类信息，明确说明"当前数据未覆盖"，不要编造、不要臆测。

---重点维度分析---
结合数据中的维度（如产线/工序/类别/时间等，以实际数据为准）分析差异与趋势。
要求：先摆数值对比（如"A 线 1,020 件 vs B 线 780 件，差距 23.5%"，或"连续 3 个周期下滑"），再给出业务解读，
不输出没有数据支撑的观点。纯文字叙述，不用列表符号。

---总体结论---
用2-3句话收束：当前数据反映的整体水平如何、最需要管理层关注的 1-2 个事项、数据质量或覆盖度上有何局限。

---改进建议---
按优先级给出3-5条可执行建议。每条包含：问题、措施、预期效果、实施难度（低/中/高）。
优先给出投入少见效快的方案；每条建议都要回扣上面分析中提到的具体数据。

禁止事项：
- 不要编造数据，不要引用数据中不存在的数字；所有数值必须来自给定数据
- 不要套用未提供的行业标杆值（如"行业标杆≥98.5%"这类具体数字，除非数据中明确给出）
- 若数据不足，可以直接说"当前数据不足以支撑该分析"，这比编造更好
- 禁止使用任何 markdown 格式符号（包括 **加粗**、- 列表、# 标题、> 引用等）
- 数字直接写在句子中，如"良率为97.19%"
- 不要输出分隔符以外的内容
"""

# 兼容旧调用方：未传行业时的默认系统提示词（保留原结构，但去掉编造行业标杆的硬编码）
REPORT_SYSTEM_PROMPT = _REPORT_ANALYST_TPL


def _llm(temp: float = 0.3):
    # 复用统一 LLM 工厂（含默认 60s 超时 + provider 温度规则），
    # 避免报告生成链路因缺少 timeout 在 LLM 抖动时永久挂死 worker。
    from agent.llm_service import _make_llm
    return _make_llm(temp=temp, max_tokens=LLM_CONFIG["max_tokens"])


def build_report_prompt(context: str, industry: str = "") -> str:
    """构造报告生成提示词。

    Args:
        context: 数据上下文（表格/摘要）
        industry: 行业说明（如"制造业：电子元器件"），可为空；
                  非空时追加到系统提示词，帮助 LLM 使用恰当的术语但不下结论性标杆。
    """
    prompt = _REPORT_ANALYST_TPL
    if industry and industry.strip():
        prompt += f"\n\n## 行业背景（仅用于术语参考，不得据此编造数据或标杆值）\n{industry.strip()}\n"
    return prompt


async def generate_report_stream(context: str, industry: str = "") -> AsyncIterator[str]:
    """流式生成专家分析报告

    Args:
        context: 数据上下文
        industry: 行业说明（可选，参数化后不再硬编码制造业口径）
    """
    llm = _llm(temp=0.3)
    full_prompt = build_report_prompt(context, industry)
    async for chunk in llm.astream([
        SystemMessage(content=full_prompt),
        HumanMessage(content=f"请基于以下数据生成分析报告：\n\n{context}")
    ]):
        if isinstance(chunk, AIMessageChunk) and chunk.content:
            yield chunk.content
