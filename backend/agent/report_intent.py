"""报告意图识别 —— 让用户「问一句」就能拿到报告，而不是先查数再点按钮。

设计取舍：
- 只在**问句本身是要一份报告**时才命中，不能把普通问数吞掉。
  「上个月的生产报告有多少条」是在数报告条数，不是要报告，必须放行给问数链路。
- 周期由问句决定（周报=本周、月报=本月…），但**不解释相对时间**给下游，
  统一折算成 [start, end] 两个具体日期再走 SQL，避免模型自己算日期算错。
- 识别不出来具体类型（只说「生成报告」）时给 general，由通用链路兜底，
  不硬猜一个业务类型，免得给用户一份方向跑偏的报告。

对外只有两个入口：
    detect(query) -> ReportRequest | None
    ReportRequest.start / .end / .period_days 供模板取数用
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

# ── 词表 ──────────────────────────────────────────────────────────
# 报告类名词：命中任一才进入候选。顺序无关，后面单独判类型。
_REPORT_NOUN_RE = re.compile(
    r"(周报|月报|日报|年报|季报|半年报|季度报|报表|简报|总结报告|分析报告|盘点报告|报告)"
)

# 动作词：明确「要一份」的意图。没有动作词也不一定否——见 _looks_like_bare_noun。
_REPORT_VERB_RE = re.compile(
    r"(生成|出具|输出|导出|做一?份|来一?份|写一?份|整理一?份|出个|出份|给我一?份|帮我出一?份|"
    r"我要|给我来|整一份|弄一份|出一个|来个)"
)

# 「报告」当动词用的情况：报告一下 / 报告给我 / 报告情况。这些不是要文档。
_REPORT_AS_VERB_RE = re.compile(r"报告\s*(一下|下|给我|情况|结果|进度|给)")

# 问数据的信号词：出现这些，即使含报告名词也更可能是想「数一数报告」
_DATA_ASK_RE = re.compile(r"(多少|几条|几行|几份|数量|条数|记录数|计数|有几|统计一下有几)")

# 时间范围词（用于判周期，也用于决定是否要用默认窗）
_PERIOD_WORDS = [
    ("year",    r"(年报|今年|本年|全年|年度)"),
    ("half",    r"(半年报|半年|上半年|下半年)"),
    ("quarter", r"(季报|季度报|本季度|这个季度|季度)"),
    ("month",   r"(月报|本月|这个月|当月|月度)"),
    ("week",    r"(周报|本周|这周|当周|一周|每?周)"),
    ("day",     r"(日报|今日|今天|当日|每天|日度)"),
]

# 上一周期
_PREV_RE = re.compile(r"(上|去|前一)(周|个月|月|年|季度|个季度)")

# ── 报告类型：按优先级匹配，先命中先定 ──
_KIND_RULES: list[tuple[str, re.Pattern]] = [
    # 销售（当前 yans 库无销售表，识别出来是为了给出明确提示而不是假装有）
    ("sales",              re.compile(r"(销售|销售额|营收|营业额|出货|发货|订单金额|回款)")),
    # 设备不良 / 停机
    ("equipment_defect",   re.compile(r"(不良设备|设备不良|故障设备|设备故障|设备停机|停机|设备.*(不良|异常|维修)|维修)")),
    # 质量
    ("quality",            re.compile(r"(质量|品质|不良|缺陷|合格|直通|检验|抽检|返工|返修|报废)")),
    # 库存
    ("inventory",          re.compile(r"(库存|存货|仓储|呆滞|周转|安全库存|备料)")),
    # 生产
    ("production",         re.compile(r"(生产|产量|产出|制造|工单|产能|交付|良率|达成)")),
]

_KIND_LABEL = {
    "sales": "销售",
    "equipment_defect": "设备不良",
    "quality": "质量",
    "inventory": "库存",
    "production": "生产",
    "general": "经营",
}


@dataclass
class ReportRequest:
    """一次报告请求的解析结果。"""

    kind: str                       # production / quality / equipment_defect / inventory / sales / general
    label: str                      # 业务名，如「设备不良」
    period: str                     # week / month / day / quarter / half / year / custom / none
    title: str                      # 报告标题，如「生产周报」
    start: str                      # YYYY-MM-DD
    end: str                        # YYYY-MM-DD
    query: str = ""                 # 原始问句
    matched_by: str = ""            # 触发原因（审计/调试用）
    prev: bool = False              # 是否指上一个周期
    adjusted: bool = False          # 区间是否被按数据水位重算过
    asof: str = ""                  # 库内数据最新日期（YYYY-MM-DD）
    original: str = ""              # 调整前的区间文案，供报告里说明

    def asof_note(self) -> str:
        """区间被调整过时给用户的一句说明。没调整返回空串。"""
        if not self.adjusted:
            return ""
        return (f"库内数据截至 {self.asof}，本报告已按数据水位对齐统计区间"
                f"（原请求区间 {self.original}）")

    @property
    def period_label(self) -> str:
        return {
            "day": "当日", "week": "本周", "month": "本月",
            "quarter": "本季度", "half": "本半年", "year": "本年",
        }.get(self.period, "指定区间")

    def window_text(self) -> str:
        """给人看的统计区间文案，写进报告封面。"""
        return f"{self.start} ~ {self.end}"


def _today() -> datetime.date:
    return datetime.datetime.now().date()


def _span(period: str, ref: datetime.date, prev: bool = False) -> tuple[str, str]:
    """把周期名折算成具体的 [start, end]。

    prev=True 取上一个完整周期（上周/上月/上年）。
    当期一律算到「今天」，因为库里当天的数据已经写进来了，截到昨天会少一天。
    """
    if prev:
        if period == "week":
            this_monday = ref - datetime.timedelta(days=ref.weekday())
            s = this_monday - datetime.timedelta(days=7)
            return s.isoformat(), (s + datetime.timedelta(days=6)).isoformat()
        if period == "month":
            first = ref.replace(day=1)
            last_month_end = first - datetime.timedelta(days=1)
            return last_month_end.replace(day=1).isoformat(), last_month_end.isoformat()
        if period == "quarter":
            q = (ref.month - 1) // 3
            first_this = ref.replace(month=q * 3 + 1, day=1)
            last_end = first_this - datetime.timedelta(days=1)
            q2 = (last_end.month - 1) // 3
            return last_end.replace(month=q2 * 3 + 1, day=1).isoformat(), last_end.isoformat()
        if period == "half":
            # 下半年 → 上一个完整半年是上半年（1/1-6/30）；上半年 → 去年下半年
            if ref.month >= 7:
                return ref.replace(month=1, day=1).isoformat(), ref.replace(month=6, day=30).isoformat()
            y = ref.year - 1
            return ref.replace(year=y, month=7, day=1).isoformat(), \
                   ref.replace(year=y, month=12, day=31).isoformat()
        if period == "year":
            return ref.replace(year=ref.year - 1, month=1, day=1).isoformat(), \
                   ref.replace(year=ref.year - 1, month=12, day=31).isoformat()

    if period == "day":
        return ref.isoformat(), ref.isoformat()
    if period == "week":
        monday = ref - datetime.timedelta(days=ref.weekday())
        return monday.isoformat(), ref.isoformat()
    if period == "quarter":
        q = (ref.month - 1) // 3
        return ref.replace(month=q * 3 + 1, day=1).isoformat(), ref.isoformat()
    if period == "half":
        if ref.month >= 7:
            return ref.replace(month=7, day=1).isoformat(), ref.isoformat()
        return ref.replace(month=1, day=1).isoformat(), ref.isoformat()
    if period == "year":
        return ref.replace(month=1, day=1).isoformat(), ref.isoformat()
    # month / none 默认按本月算
    if period == "custom":
        # 交给调用方覆盖（这里给一个安全默认：近 30 天）
        return (ref - datetime.timedelta(days=29)).isoformat(), ref.isoformat()
    return ref.replace(day=1).isoformat(), ref.isoformat()


def _detect_period(q: str) -> tuple[str, bool]:
    """返回 (period, prev)。没命中任何时间词时给 month —— 月报是最常见的默认口径。"""
    prev = bool(_PREV_RE.search(q))
    for name, pat in _PERIOD_WORDS:
        if re.search(pat, q):
            return name, prev
    return "month", prev


def _detect_kind(q: str) -> str:
    for kind, pat in _KIND_RULES:
        if pat.search(q):
            return kind
    return "general"


_PERIOD_SUFFIX = {"day": "日报", "week": "周报", "month": "月报",
                  "quarter": "季报", "half": "半年报", "year": "年报"}
_PREV_PREFIX = {"day": "昨日", "week": "上周", "month": "上月",
                "quarter": "上季度", "half": "上期", "year": "上年"}


def _build_title(kind: str, period: str, prev: bool) -> str:
    """拼报告标题：生产+周报 → 生产周报；设备不良+月报 → 设备不良月报。"""
    label = _KIND_LABEL.get(kind, "经营")
    suffix = _PERIOD_SUFFIX.get(period)
    if not suffix:
        return f"{label}报告"
    core = f"{label}{suffix}"
    return f"{_PREV_PREFIX.get(period, '上期')}{core}" if prev else core


def detect(query: str) -> ReportRequest | None:
    """判断问句是不是「要一份报告」。是则返回解析结果，否则 None。

    三条命中路径（任一成立即可）：
      a) 报告名词 + 动作词         ——「生成生产周报」「给我做一份不良设备报告」
      b) 极短问句且以报告名词收尾    ——「生产周报」「设备不良报告」
      c) 报告名词 + 周期词起头       ——「本周生产情况报告」

    显式排除：
      - 「报告一下…」这类动词用法
      - 含「多少/几条/几份」的问数问句（没带动作词时）
    """
    q = (query or "").strip().strip("？?。.！!，,")
    if not q or len(q) > 60:
        return None

    m_noun = _REPORT_NOUN_RE.search(q)
    if not m_noun:
        return None

    # 「报告一下」「报告给我」→ 当动词用，不是要文档
    if _REPORT_AS_VERB_RE.search(q):
        return None

    has_verb = bool(_REPORT_VERB_RE.search(q))
    tail_noun = bool(re.search(r"(周报|月报|日报|年报|季报|半年报|报表|简报|报告)$", q))
    short_noun = tail_noun and len(q) <= 14
    data_ask = bool(_DATA_ASK_RE.search(q))

    if has_verb:
        matched_by = "verb+noun"
    elif short_noun:
        matched_by = "bare_noun"
    else:
        matched_by = ""

    # 含问数词又没有动作词 → 判为「在数报告」，放行给问数链路
    if data_ask and not has_verb:
        return None
    if not matched_by:
        return None

    period, prev = _detect_period(q)
    kind = _detect_kind(q)
    ref = _today()
    start, end = _span(period, ref, prev)
    # 2026-10-05 修复（P0）：区间原本一律以「今天」收尾，但演示/私有库的
    # 数据往往滞后于今天 —— 实测 yans 库 mes_process_output 只有
    # 2026-08-01 ~ 2026-09-15，而「生产周报」算出的区间是 10-05~10-05，
    # 于是**所有章节都返回 0 行**，报告却显示「取数成功 6/6」，
    # 用户看到的是一份全空报告（去掉时间条件立刻有数据：投入 318 万、良率 97.56%）。
    # 这里把区间末端钳制到库内实际数据末日，避免凭空造出空区间。
    data_max = _data_max_date()
    clamped_note = ""
    if data_max and end > data_max:
        clamped_note = (f"（数据仅到 {data_max}，统计区间已自动收窄）")
        if start > data_max:
            # 整个区间都在数据之外 → 退回「以数据末日为终点」的等长区间
            try:
                span = datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)
                end = data_max
                start = (datetime.date.fromisoformat(data_max) - span).isoformat()
            except Exception:
                start = data_max
        else:
            end = data_max
    title = _build_title(kind, period, prev) + clamped_note

    return ReportRequest(
        kind=kind, label=_KIND_LABEL.get(kind, "经营"),
        period=period, title=title, start=start, end=end,
        query=query, matched_by=matched_by, prev=prev,
    )


_DATA_MAX_CACHE: dict[str, str] = {}


def _data_max_date() -> str:
    """取业务事实表里最新的日期（结果缓存 5 分钟）。

    找不到任何带日期列的表时返回空串（不做钳制，保持原有行为）。
    """
    import time as _t
    key = str(_today())
    hit = _DATA_MAX_CACHE.get("k")
    if hit and _t.time() - _DATA_MAX_CACHE.get("ts", 0) < 300:
        return _DATA_MAX_CACHE.get("v") or ""
    val = ""
    try:
        from db.executor import execute_sql
        r = execute_sql(
            "SELECT MAX(stat_date)::text AS d FROM mes_process_output")
        if r.get("success") and r.get("rows"):
            v = str(r["rows"][0].get("d") or "")[:10]
            if len(v) == 10:
                val = v
    except Exception:
        val = ""
    _DATA_MAX_CACHE["k"] = key
    _DATA_MAX_CACHE["v"] = val
    _DATA_MAX_CACHE["ts"] = _t.time()
    return val


def is_report_request(query: str) -> bool:
    return detect(query) is not None
