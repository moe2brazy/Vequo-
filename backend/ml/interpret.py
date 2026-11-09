"""ML 结果解释 —— 把训练指标翻译成业务能看懂的话

为什么单独成模块：

    结果卡上原来只有指标标签、特征重要性条和两张图。业务用户看到
    「R²: 0.8954」和「equipment_downtime_minutes: 0.31」，得自己在脑子里翻译一遍。
    命题把「模型结果解释」和训练、评估、推理并列为四项能力 —— 展示不等于解释。

两条路，先确定性后 LLM：

    1. 规则路径：把指标分档、算驱动因素占比、核对样本量，拼成
       【这个模型能回答什么】【主要驱动因素】【用之前要知道】三段。
       零延迟，数字全部取自 result，不会错。
    2. LLM 路径（默认关闭，ML_EXPLAIN_LLM=1 才开）：把算出的事实喂给模型换措辞。
       输出要通过数字白名单校验 —— 出现事实里没有的数字一律判不合格，退回规则文案。
       默认关的原因见 _llm_enabled()：实测更慢，且会把 RMSE/MAE 这类术语原样搬上屏。

约定：本模块纯函数 + 一次可选 LLM 调用，不查库、不写库，可单测（use_llm=False）。
"""

from __future__ import annotations

import logging
import math
import os
import re

logger = logging.getLogger(__name__)


# ====== 指标分档 ============================================================
# 阈值不是学术标准，是给业务方看的话术标尺：同一档的说法固定，避免同样 0.8
# 这次叫"较好"下次叫"良好"，用户对不上。

_R2_BANDS = (
    (0.90, "很强", "可以直接拿来做量级预估"),
    (0.80, "较强", "适合排产前的量级预估"),
    (0.65, "中等", "看趋势可以，别拿绝对值当定论"),
    (0.50, "偏弱", "只能当方向参考"),
    (0.00, "很弱", "不建议直接用于决策"),
)
_ACC_BANDS = (
    (0.95, "很高", "多数场景可以直接用"),
    (0.85, "较高", "常规判别够用"),
    (0.70, "中等", "要求严格的场景建议人工复核"),
    (0.60, "偏低", "误判集中在少数类别，用前先看样本分布"),
)
_SIL_BANDS = (
    (0.70, "清晰", "分组边界明显，可以按组归因"),
    (0.50, "较好", "分组基本可用"),
    (0.35, "一般", "组间有重叠，解读时要留余地"),
)


def _num(v):
    """取有限浮点数；None/NaN/inf/非数值一律返回 None（不要让它渗进文案）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _fmt(v, digits: int = 2) -> str:
    """数值格式化：大整数带千分位，小数按需截断，尾部零去掉。

    注意 digits=0 时不能 rstrip("0") —— 那会把 "10" 啃成 "1"，
    占比 9.64% 显示成 1% 就是这么来的。
    """
    f = _num(v)
    if f is None:
        return "-"
    if abs(f - round(f)) < 1e-9 and abs(f) >= 100:
        return f"{int(round(f)):,}"
    s = f"{f:.{digits}f}"
    if digits > 0:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _band(value, bands) -> tuple[str, str]:
    """按阈值表取 (档位名, 一句话建议)；取不到返回 ("", "")。"""
    f = _num(value)
    if f is None:
        return "", ""
    for th, label, note in bands:
        if f >= th:
            return label, note
    return "偏低", "结果不稳定，建议补特征或换算法"


def _cn(table: str, col: str) -> str:
    """列名中文化：能翻成中文的写成「中文（english）」，翻不出的保留原名。

    先按表名查（同一字段在不同表叫法可能不同），查不到再用通用语义查一次 ——
    跨表建模时目标列来自另一张表，只拿基表名去查必然落空。
    与数据问答侧 _col_display 同一口径（都走 field_semantics.field_label），不另造映射。
    翻不出来时返回英文原名，绝不返回空串。
    """
    c = str(col or "").strip()
    if not c:
        return ""
    cn = ""
    for tb in (table or "", ""):
        try:
            from agent.field_semantics import field_label
            cn = field_label(tb, c) or ""
        except Exception:
            cn = ""
        if cn and cn != c:
            break
    if cn and cn != c:
        return f"{cn}（{c}）"
    return c


def _plain(label: str) -> str:
    """剥掉「中文（english）」里的英文括号，正文只留中文。

    结果卡上字段清单已经列过一次英文列名，解读正文再重复三遍
    「投入数量（input_qty）」只会把句子撑得读不动。第一处对照保留括号，
    后面叙述一律用纯中文名。
    """
    return re.sub(r"（[A-Za-z0-9_.\-]+）", "", label or "")


def _task_of(result: dict) -> str:
    mt = str(result.get("model_type") or "").lower()
    if mt in ("kmeans",):
        return "clustering"
    if mt in ("isolation",):
        return "anomaly"
    if mt in ("logistic",):
        return "classification"
    # 树模型/随机森林可能是分类也可能是回归：看指标键名，比猜类型可靠
    metrics = result.get("metrics") or {}
    if "准确率" in metrics or "F1" in metrics:
        return "classification"
    return "regression"


# ====== 事实抽取 ============================================================

def build_facts(result: dict) -> dict:
    """从 train_model 的返回值里抽出解读要用的事实。纯计算，不抛异常。"""
    result = result or {}
    table = str(result.get("table") or "")
    task = _task_of(result)
    metrics = result.get("metrics") or {}
    importance = result.get("importance") or {}
    samples = int(_num(result.get("samples")) or 0)

    facts = {
        "task": task,
        "model": str(result.get("model_label") or result.get("model_type") or "模型"),
        "model_type": str(result.get("model_type") or ""),
        "table": table,
        "target": _cn(table, result.get("target") or ""),
        "features": [_cn(table, f) for f in (result.get("features") or [])],
        "samples": samples,
        # sklearn 的 test_size=0.2 是向上取整（ceil），写 275 会比真实测试集少一行
        "test_rows": math.ceil(samples * 0.2) if task in ("regression", "classification") else 0,
        "metric_label": "",
        "metric_value": None,
        "grade": "",
        "grade_note": "",
        "rmse": _num(metrics.get("RMSE")),
        "mae": _num(metrics.get("MAE")),
        "drivers": [],
        "driver_coverage": None,
        "data_note": str(result.get("data_note") or ""),
        "join_note": str(result.get("join_note") or ""),
        "caveats": [],
    }

    # ── 主指标与档位 ──
    if task == "regression":
        facts["metric_label"] = "R²"
        # 不能写 metrics.get("R²") or metrics.get("R2")：R² 恰好为 0.0 时会被 or 吞成 None
        _mv = metrics.get("R²")
        if _mv is None:
            _mv = metrics.get("R2")
        facts["metric_value"] = _num(_mv)
        facts["grade"], facts["grade_note"] = _band(facts["metric_value"], _R2_BANDS)
        # R² 为负不是"偏弱"，是比拿平均值硬猜还差 —— 这两件事的处置办法完全不同
        if facts["metric_value"] is not None and facts["metric_value"] < 0:
            facts["grade"], facts["grade_note"] = "失效", ""
    elif task == "classification":
        facts["metric_label"] = "准确率"
        facts["metric_value"] = _num(metrics.get("准确率"))
        facts["grade"], facts["grade_note"] = _band(facts["metric_value"], _ACC_BANDS)
        facts["f1"] = _num(metrics.get("F1"))
    elif task == "clustering":
        facts["metric_label"] = "轮廓系数"
        facts["metric_value"] = _num(metrics.get("轮廓系数"))
        facts["grade"], facts["grade_note"] = _band(facts["metric_value"], _SIL_BANDS)
        facts["n_clusters"] = int(_num(metrics.get("聚类数")) or 0)
    else:  # anomaly
        facts["metric_label"] = "异常比例"
        facts["metric_value"] = _num(metrics.get("异常比例"))
        facts["n_outliers"] = int(_num(metrics.get("异常数")) or 0)
        facts["panel"] = int(_num(metrics.get("总样本")) or samples)
        facts["grade"] = "已标注"

    # ── 账面恒等式 / 特征泄漏判定 ──
    # 回归拟合度顶到 0.999 以上，通常不是模型强，而是目标列本来就能由输入列
    # 算出来（实测 mes_process_output：合格数 = 投入数 − 不良数 − 返工数 − 小损耗，
    # R²=1.0、RMSE=0.28）。这时「解释力很强，可以直接拿来做量级预估」是误导，
    # 必须反过来讲。阈值取 0.999 是为了不误伤计划量→实际产出那类真关系（R²≈0.998）。
    _r2 = facts.get("metric_value")
    facts["suspect_leak"] = bool(task == "regression" and _r2 is not None and _r2 >= 0.999)

    # ── 驱动因素：按重要性绝对值排，给前三项和它们的合计占比 ──
    pairs = []
    for k, v in importance.items():
        fv = _num(v)
        if fv is None:
            continue
        pairs.append((str(k), fv))
    if pairs:
        total = sum(abs(v) for _, v in pairs) or 1.0
        pairs.sort(key=lambda x: -abs(x[1]))
        top = pairs[:3]
        facts["n_importance"] = len(pairs)
        facts["drivers"] = [
            {"name": _cn(table, k), "value": round(v, 4), "share": round(abs(v) / total, 4)}
            for k, v in top
        ]
        facts["driver_coverage"] = round(
            sum(round(abs(v) / total * 100) for _, v in top) / 100, 2)

    # ── 使用限制：把容易被追问的点提前说明白 ──
    cav = facts["caveats"]
    if task == "clustering":
        cav.append("聚类只给出分组，不告诉你每组是好是坏 —— 好坏要靠业务标准另外判断。")
    if task == "anomaly":
        cav.append("异常比例是训练时设定的上限（contamination），不代表真实异常率；"
                   "调高它，标出来的记录会跟着变多。")
    if task == "classification":
        cav.append("准确率会被类别不均衡放大：某一类占九成时，全判成这一类也有 90% 的准确率。"
                   "看结论要结合各类样本量。")
    if task in ("regression", "classification"):
        mv = facts["metric_value"]
        # 阈值定在 0.999：计划量→实际产出这种真关系能到 0.998，不该被误报；
        # 而"用返工数预测不良数"这类近亲特征会顶到 0.9999，必须提醒自查。
        # 这条排在样本量前面：它影响的是"这个结果能不能信"，比样本多少更急。
        if task == "regression" and mv is not None and mv >= 0.999:
            if facts.get("suspect_leak"):
                cav.append("想让它真的能预测，得换一类问题：用不受目标影响的因素"
                           "（设备状态、班次、停机时长）当输入，别用账目的组成部分。")
            else:
                cav.append("指标接近满分，先确认目标列的等价字段有没有混进特征。")
        if task == "classification" and mv is not None and mv >= 0.999:
            cav.append("准确率接近满分，先确认有没有把结果类字段当成特征。")
        if samples >= 5000:
            cav.append("样本已顶到 5000 行加载上限，超出部分没参与训练。")
        if samples and samples < 200:
            cav.append(f"有效样本只有 {samples} 行，量级偏小，结论更像是这批数据里的规律。")
        elif samples and samples < 1000:
            cav.append(f"有效样本 {samples} 行，属于中小样本，换一批数据先重训一次再下结论。")
        if facts["drivers"] and not facts.get("suspect_leak"):
            cav.append("特征重要性只说明这个字段在模型里被用得多，不等于改它就能改结果。")
    facts["caveats"] = cav[:4]
    return facts


# ====== 规则文案（兜底，也是基准）==========================================

def rule_text(facts: dict) -> str:
    """按事实拼解读文案。格式与数据问答的分析结论一致：【段首】+ · 条目 + **加粗**。"""
    facts = facts or {}
    task = facts.get("task") or "regression"
    target = facts.get("target") or "目标值"
    feats = facts.get("features") or []
    plain_target = _plain(target)
    plain_feats = [_plain(f) for f in feats]
    samples = facts.get("samples") or 0
    mv = facts.get("metric_value")
    grade = facts.get("grade") or ""
    note = facts.get("grade_note") or ""
    lines: list[str] = []

    if task in ("regression", "classification"):
        head = "【这个模型能回答什么】"
        lines.append(head)
        feat_txt = "、".join(feats[:4]) if feats else "所选特征"
        if len(feats) > 4:
            feat_txt += " 等 %d 项" % len(feats)
        if task == "regression":
            if facts.get("suspect_leak"):
                pf = "、".join(plain_feats[:4]) if plain_feats else "所选特征"
                lines.append(f"这次用 {samples:,} 条记录，让模型学「{pf}」和"
                             f"「{plain_target}」之间的关系，其中留出 "
                             f"{facts.get('test_rows', 0):,} 条做检验，没参与训练。")
            else:
                body = f"用「{feat_txt}」预测「{target}」，样本 {samples:,} 行"
                if facts.get("test_rows"):
                    body += f"，留出 {facts['test_rows']:,} 行做检验，这部分没参与训练"
                lines.append(body + "。")
            if mv is not None and mv < 0:
                lines.append(f"模型的表现比直接用平均值猜还差（R² **{_fmt(mv, 2)}**），"
                             f"这套特征解释不了「{target}」的波动，建议换特征或换算法。")
            elif facts.get("suspect_leak"):
                # 拟合度顶到满分时，最忌讳还夸「解释力很强，可以直接拿来做量级预估」。
                # 实测 mes_process_output 的合格数就是投入−不良−返工，R²=1.0 是账本，
                # 不是模型能力。这里把结论翻过来讲，并说清为什么不能拿来预测未来。
                lines.append(f"**这个结果不能当预测用**：拟合度 **{_fmt(mv * 100, 1)}%**、"
                             f"平均只差 **±{_fmt(facts.get('rmse'), 1)}**，"
                             "等于把账本原样抄了一遍。")
                lines.append(f"这几个输入字段几乎能直接算出「{plain_target}」——"
                             "这种满分通常说明输入里就含着目标的组成部分，"
                             "模型没有学到新规律，只是把这笔账复述了一次。")
                lines.append("它能用来核对有没有录错，回答不了「下个月能产多少」："
                             "下个月投入多少还不知道，模型没有可以代入的数。")
            elif mv is not None:
                s = f"模型能解释 **{_fmt(mv * 100, 1)}%** 的「{target}」波动，解释力{grade}"
                if note:
                    s += f"，{note}"
                lines.append(s + "。")
                if facts.get("rmse") is not None:
                    lines.append(f"预测值与真实值平均偏离 **±{_fmt(facts['rmse'], 1)}** 左右"
                                 f"（RMSE），不是每个点都对得上。")
            else:
                # _safe_metrics 会把 NaN 指标转成 None（小样本 / 常数列常见）。
                # 不给一句明示，用户会以为模型表现正常，下面的驱动因素仍照常输出。
                lines.append("本次评估指标算不出来（目标可能接近常数，或有效样本太少），"
                             "下面关于驱动因素的判断只作参考，先别拿它做决策。")
        else:
            body = f"按「{feat_txt}」判断「{target}」属于哪一类，样本 {samples:,} 行"
            if facts.get("test_rows"):
                body += f"，留出 {facts['test_rows']:,} 行做检验"
            lines.append(body + "。")
            if mv is not None:
                s = f"准确率 **{_fmt(mv * 100, 1)}%**，判断水平{grade}"
                if facts.get("f1") is not None:
                    s += f"，加权 F1 **{_fmt(facts['f1'], 2)}**"
                if note:
                    s += f"，{note}"
                lines.append(s + "。")
            else:
                lines.append("本次准确率无法计算（有效样本太少或目标类别不足），"
                             "下面关于驱动因素的判断只作参考。")

    elif task == "clustering":
        lines.append("【这次聚类做了什么】")
        k = facts.get("n_clusters") or 0
        lines.append(f"把 {samples:,} 条记录按 {len(feats)} 个特征分成 **{k}** 组，"
                     f"没给标准答案，靠数据自身的相似度归堆。")
        if mv is not None:
            # note 为空时不能直接拼句号，否则会出现「分组结构一般。。」
            tail = f"分组结构{grade}" + (f"，{note}" if note else "")
            lines.append(f"轮廓系数 **{_fmt(mv, 2)}**，{tail}。")
    else:
        lines.append("【这次异常检测做了什么】")
        n_out = facts.get("n_outliers") or 0
        panel = facts.get("panel") or samples
        lines.append(f"在 {panel:,} 条记录里标出 **{n_out:,}** 条异常。")
        if mv is not None:
            lines.append(f"占比 **{_fmt(mv * 100, 1)}%**，这是训练前设定的上限，不是数据里的真实异常率。")

    drivers = facts.get("drivers") or []
    n_total = facts.get("n_importance") or len(drivers)
    if facts.get("suspect_leak") and drivers:
        # 账面恒等式下，系数大小只反映各列的量纲，排名是假信息。
        # 与其摆三个读不懂的数字，不如直接说清它们的业务关系。
        lines.append("【这几个字段是什么关系】")
        lines.append("· 这几个字段是同一笔账的收支项，少一个都算不出"
                     f"「{plain_target}」，所以不必再比谁更重要，"
                     "权重排名在这里没有意义。")
    elif drivers and n_total == 1:
        # 只有一个特征时，"占全部特征的 100%" 是句废话，直接说清模型靠什么判断
        lines.append("【主要驱动因素】")
        lines.append(f"· 「{drivers[0]['name']}」是唯一的输入特征，模型完全靠它判断，"
                     f"重要性 **{_fmt(drivers[0]['value'], 2)}**。")
    elif drivers:
        lines.append("【主要驱动因素】")
        for i, d in enumerate(drivers):
            rank = "排第一" if i == 0 else ("排第二" if i == 1 else "排第三")
            lines.append(f"· 「{d['name']}」{rank}，重要性 **{_fmt(d['value'], 2)}**，"
                         f"占全部特征的 {_fmt((d.get('share') or 0) * 100, 0)}%。")
        cov = facts.get("driver_coverage")
        if cov is not None and len(drivers) >= 2 and n_total > len(drivers):
            num = {2: "前两项", 3: "前三项"}.get(len(drivers), "前几项")
            lines.append(f"· {num}合计 **{_fmt(cov * 100, 0)}%**，其余字段对结果的影响很小。")
        elif n_total == len(drivers) and len(drivers) >= 2:
            num = {2: "两个", 3: "三个"}.get(len(drivers), "这几个")
            lines.append(f"· 参与判断的字段就{num}，没有别的特征分摊权重。")

    cav = facts.get("caveats") or []
    # 技术口径的说明放最前（数据从哪来、砍了多少行），业务限制跟在后面
    extra = []
    if facts.get("join_note"):
        extra.append(str(facts["join_note"]).replace(" ⋈ ", " 关联 "))
    if facts.get("data_note"):
        extra.append(str(facts["data_note"]))
    if cav or extra:
        lines.append("【用之前要知道】")
        for c in extra + cav:
            lines.append(f"· {c}")

    return "\n".join(lines)


# ====== LLM 改写（带数字白名单校验）========================================

_SECTION_HEADS = ("【这个模型能回答什么】", "【这次聚类做了什么】",
                  "【这次异常检测做了什么】", "【主要驱动因素】", "【用之前要知道】")


def _fact_dump(facts: dict) -> str:
    """把事实压成给 LLM 看的键值块。数字只在这里出现，是校验的唯一依据。"""
    drv = "；".join(f"{d['name']} {d['value']}（占比 {d['share']}）"
                    for d in (facts.get("drivers") or [])) or "无"
    parts = [
        f"模型：{facts.get('model')}",
        f"任务：{facts.get('task')}",
        f"预测目标：{facts.get('target') or '无'}",
        f"特征：{'、'.join(facts.get('features') or []) or '无'}",
        f"样本：{facts.get('samples')} 行，其中留出 {facts.get('test_rows')} 行做检验",
        f"主指标：{facts.get('metric_label')} = {facts.get('metric_value')}，档位：{facts.get('grade')}",
    ]
    if facts.get("rmse") is not None:
        parts.append(f"RMSE = {facts.get('rmse')}，MAE = {facts.get('mae')}")
    if facts.get("f1") is not None:
        parts.append(f"加权 F1 = {facts.get('f1')}")
    if facts.get("n_clusters"):
        parts.append(f"聚类数 = {facts.get('n_clusters')}")
    if facts.get("n_outliers"):
        parts.append(f"异常数 = {facts.get('n_outliers')}，总记录 = {facts.get('panel')}")
    parts.append(f"驱动因素：{drv}")
    if facts.get("data_note"):
        parts.append(f"数据说明：{facts.get('data_note')}")
    if facts.get("join_note"):
        parts.append(f"跨表说明：{facts.get('join_note')}")
    if facts.get("caveats"):
        parts.append(f"已知限制：{'；'.join(facts.get('caveats'))}")
    return "\n".join(parts)


def _num_tokens(text: str) -> set[str]:
    """抽文本里的数字（先去掉千分位逗号，避免 1,376 被拆成 1 和 376）。"""
    return set(re.findall(r"\d+(?:\.\d+)?", (text or "").replace(",", "")))


def _numbers_ok(candidate: str, allowed_text: str, base_text: str) -> bool:
    """候选文案里的每个数字，必须在事实或基准文案里出现过。

    个位数（0-9）放行：它们是"三项""两段"这类量词，写错也不影响判断。
    """
    allowed = _num_tokens(allowed_text) | _num_tokens(base_text)
    for tok in _num_tokens(candidate):
        if len(tok) == 1:
            continue
        if tok not in allowed:
            logger.info("ML 解读被数字校验拦下：%s 不在事实里", tok)
            return False
    return True


def _llm_rewrite(facts: dict, base: str, question: str, timeout: float = 25.0) -> str:
    """让 LLM 改写一段措辞。任何异常/校验失败都返回空串，由调用方退回规则文案。"""
    try:
        from agent.llm_service import _make_llm
        from langchain_core.messages import HumanMessage, SystemMessage
    except Exception:
        return ""
    dump = _fact_dump(facts)
    sys = (
        "你在给工厂的业务人员解释一次已经跑完的机器学习建模结果。\n"
        "下面的数字是系统算出来的唯一事实来源，你只能使用它们，一个都不许改、不许补。\n\n"
        f"{dump}\n\n"
        "把上面的结果改写成一段好读的中文说明，遵守这些要求：\n"
        "1. 段首用方括号中文标题，每个标题单独占一行。回归和分类用"
        "【这个模型能回答什么】【主要驱动因素】【用之前要知道】；"
        "聚类用【这次聚类做了什么】【主要驱动因素】【用之前要知道】；"
        "异常检测用【这次异常检测做了什么】【主要驱动因素】【用之前要知道】。\n"
        "2. 第二个标题起的条目用「· 」开头，每条一句话。\n"
        "3. 关键数字用 **数字** 包起来。\n"
        "4. 不解释算法原理，不复述指标定义，直接给判断。\n"
        "5. 全文 300 字以内，不要开场白，不要结尾总结。\n"
    )
    try:
        llm = _make_llm(temp=0.3, max_tokens=800, timeout=timeout, max_retries=1)
        resp = llm.invoke([SystemMessage(content=sys), HumanMessage(content=question or "解释这次建模结果")])
        text = (getattr(resp, "content", "") or "").strip()
    except Exception as e:
        logger.info("ML 解读 LLM 调用失败，退回规则文案：%s", e)
        return ""
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    if not text or not any(h in text for h in _SECTION_HEADS):
        return ""
    if len(text) > 900:
        return ""
    if not _numbers_ok(text, dump, base):
        return ""
    return text


def _llm_enabled() -> bool:
    """LLM 改写默认关闭。

    实测（2026-11-09，qwen3.8-flash）：改写一轮 23.2s，产出的文案把
    「RMSE 150.48、MAE 116.61」原样摆上屏 —— 比规则文案更难懂，还丢了
    「平均偏离 ±150」这种业务能接住的说法。指标解读是确定性计算，
    交给 LLM 只会更慢更容易跑偏，这跟系统里 _build_deterministic_insight
    的原则一致：能确定性算清楚的，不绕 LLM。
    需要更口语的措辞时用 ML_EXPLAIN_LLM=1 打开，输出仍要过数字白名单。
    """
    return str(os.environ.get("ML_EXPLAIN_LLM", "")).strip().lower() in ("1", "true", "on", "yes")


def explain_ml_result(result: dict, question: str = "", use_llm=None) -> dict:
    """生成结果解读。

    返回 {"text": 文案, "source": "llm"|"rule"|"none"}。
    use_llm=None 时看环境变量 ML_EXPLAIN_LLM（默认关）。
    永不抛异常：解读只是锦上添花，不能因为它挂掉整次建模。
    """
    if use_llm is None:
        use_llm = _llm_enabled()
    try:
        facts = build_facts(result)
        # 没有主指标也没有驱动因素 → 这不是一次正常的训练结果（多半是空壳），
        # 硬拼出来只会得到「样本 0 行」这种废话，不如让结果卡保持原样。
        if facts.get("metric_value") is None and not facts.get("drivers"):
            return {"text": "", "source": "none", "facts": facts}
        base = rule_text(facts)
    except Exception as e:  # 理论上不会走到，但不能让它把训练结果一起带走
        logger.warning("ML 解读规则路径失败：%s", e)
        return {"text": "", "source": "none", "facts": {}}
    if use_llm and base:
        text = _llm_rewrite(facts, base, question)
        if text:
            return {"text": text, "source": "llm", "facts": facts}
    return {"text": base, "source": "rule", "facts": facts}
