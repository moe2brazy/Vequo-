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

import calendar
import datetime
import logging
import re
from dataclasses import dataclass, field

_log = logging.getLogger("report_intent")

# 数据区间缓存：key = kind|库名|日期，value = (取数时刻, lo, hi)
_RANGE_CACHE: dict[str, tuple[float, str, str]] = {}

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

# ── 显式时间区间：用户自己点名了时间，不能再按"默认本月"猜 ────────────
# 2026-11-07 修复（P0）：这三类问法原来一条都不认，全部掉进 `_detect_period`
# 的兜底 `return "month"`，再被数据水位钳到数据末日所在的那几天。实测：
#   「生成所有时间的生产报告」   → 2026-09-09 ~ 2026-09-15
#   「生成 2026 年 8 月的生产报告」→ 2026-09-09 ~ 2026-09-15（年份月份全丢）
#   「2026 年 8 月 1 日到 8 月 31 日」→ 还是 2026-09-09 ~ 2026-09-15
# 三条问法算出同一个区间，用户看到的就是"只出 9 月"。识别顺序必须是
# 全量 > 显式区间 > 周期词，否则「生成所有时间的月报」会被"月报"抢走。
_ALL_TIME_RE = re.compile(
    r"(所有时间|全部时间|所有日期|全部日期|所有月份|全部月份|所有时段|全时段|"
    r"全周期|全量|全部数据|所有数据|历史全部|全部历史|历史上全|有史以来|"
    r"不限时间|不限日期|不限时段|时间不限|日期不限|从头到现在)"
)

_CN_MONTH = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}

# 「日」级日期：2026年8月1日 / 2026-08-01 / 8月1日（无年，待推）
_YMD_CN_RE = re.compile(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_YMD_ISO_RE = re.compile(r"((?:19|20)\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)")
_MD_RE = re.compile(r"(?<![\d年])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")

# 「月」级：2026年8月 / 2026年八月份 / 2026-08 / 8月份（无年，待推）
_YM_CN_RE = re.compile(
    r"((?:19|20)\d{2})\s*年\s*(十一|十二|\d{1,2}|[一二三四五六七八九十])\s*月(?:份)?")
_YM_ISO_RE = re.compile(r"((?:19|20)\d{2})\s*[-/.]\s*(\d{1,2})(?!\s*[-/.]\s*\d)")
_M_ONLY_RE = re.compile(
    r"(?<![\d年])(十一|十二|\d{1,2}|[一二三四五六七八九十])\s*月份?(?!\s*[\d日号])")

# 各报告类型对应的「数据实际覆盖区间」查询。全量报告要的就是这个区间，
# 显式年月也靠它推断该用哪个年份（库里只有 2026 年数据时，「8 月」就是 2026-08）。
_RANGE_SQL = {
    "production": "SELECT MIN(stat_date)::text AS lo, MAX(stat_date)::text AS hi "
                  "FROM mes_process_output",
    "quality": "SELECT MIN(inspection_date)::text AS lo, MAX(inspection_date)::text AS hi "
               "FROM qms_inspection",
    "equipment_defect": "SELECT MIN(start_time)::date::text AS lo, "
                        "MAX(start_time)::date::text AS hi FROM eqp_downtime_record",
}

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
    window_note: str = ""           # 区间本身的额外说明（如「该区间库内无数据」）

    def asof_note(self) -> str:
        """区间被调整 / 有异常时给用户的一句说明。没有则返回空串。"""
        if self.window_note:
            return self.window_note
        if not self.adjusted:
            return ""
        return (f"库内数据截至 {self.asof}，本报告已按数据水位对齐统计区间"
                f"（原请求区间 {self.original}）")

    @property
    def period_label(self) -> str:
        if self.period == "none":
            return "全部时间"
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
    if period == "none":
        # 全量语义的区间由调用方按库内真实数据范围填（见 detect），
        # 这里返回空串而不是"本月"——否则「所有时间」又会被悄悄缩成一个星期。
        return "", ""
    # month / none 默认按本月算
    if period == "custom":
        # 交给调用方覆盖（这里给一个安全默认：近 30 天）
        return (ref - datetime.timedelta(days=29)).isoformat(), ref.isoformat()
    return ref.replace(day=1).isoformat(), ref.isoformat()


def _cn_month(tok: str) -> int:
    """把「8 / 08 / 八 / 十一」统一成 1-12，认不出返回 0。"""
    tok = (tok or "").strip()
    if tok in _CN_MONTH:
        return _CN_MONTH[tok]
    try:
        v = int(tok)
    except ValueError:
        return 0
    return v if 1 <= v <= 12 else 0


def _collect_dates(q: str) -> list[tuple[int, int, int, int]]:
    """抽出问句里所有「日」级日期，返回 [(位置, 年|0, 月, 日)]，按出现顺序。

    先认带年份的写法，再认「8月1日」——用已占用区间过滤，避免
    「2026年8月1日」被两条正则各算一遍。
    """
    found: list[tuple[int, int, int, int]] = []
    spans: list[tuple[int, int]] = []
    for rx, has_year in ((_YMD_CN_RE, True), (_YMD_ISO_RE, True)):
        for m in rx.finditer(q):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if not (1 <= mo <= 12):
                continue
            try:
                datetime.date(y, mo, d)      # 挡掉「8月32日」这类
            except ValueError:
                continue
            found.append((m.start(), y, mo, d))
            spans.append((m.start(), m.end()))
    for m in _MD_RE.finditer(q):
        if any(a <= m.start() < b for a, b in spans):
            continue
        mo, d = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12):
            continue
        found.append((m.start(), 0, mo, d))
    found.sort(key=lambda x: x[0])
    return found


def _data_range(kind: str) -> tuple[str, str]:
    """取该报告类型下业务事实表的真实数据区间（结果缓存 5 分钟，按库隔离）。"""
    import time as _t
    key = f"{kind}|{_db_key()}|{_today()}"
    hit = _RANGE_CACHE.get(key)
    if hit and _t.time() - hit[0] < 300:
        return hit[1], hit[2]
    sql = _RANGE_SQL.get(kind) or _RANGE_SQL["production"]
    lo = hi = ""
    try:
        from db.executor import execute_sql
        r = execute_sql(sql)
        if r.get("success") and r.get("rows"):
            row = r["rows"][0]
            lo = str(row.get("lo") or "")[:10]
            hi = str(row.get("hi") or "")[:10]
    except Exception as e:
        _log.warning("数据区间探测失败（%s）：%s", kind, e)
    for v in (lo, hi):
        if v and len(v) != 10:
            lo = hi = ""          # 只认 YYYY-MM-DD，半截字符串比没有更危险
            break
    _RANGE_CACHE[key] = (_t.time(), lo, hi)
    return lo, hi


def _db_key() -> str:
    try:
        from database import get_database_config
        cfg = get_database_config() or {}
        return str(cfg.get("database") or cfg.get("db") or "")
    except Exception:
        return ""


def _resolve_year(month: int, lo: str, hi: str) -> int:
    """没写年份的「8 月」该算哪一年。

    优先当年；当年完全落在数据区间之外时，在数据覆盖的年份里找确实含这个月的
    那一年（库内只有 2026 年数据，问「8 月」就该给 2026-08，而不是今年的 8 月）。
    """
    y = _today().year
    if not lo or not hi:
        return y
    try:
        ly, hy = int(lo[:4]), int(hi[:4])
    except ValueError:
        return y
    if ly <= y <= hy:
        return y
    for yy in range(hy, ly - 1, -1):
        last = calendar.monthrange(yy, month)[1]
        if f"{yy:04d}-{month:02d}-01" <= hi and f"{yy:04d}-{month:02d}-{last:02d}" >= lo:
            return yy
    return y


def _month_bounds(y: int, month: int) -> tuple[str, str]:
    last = calendar.monthrange(y, month)[1]
    return f"{y:04d}-{month:02d}-01", f"{y:04d}-{month:02d}-{last:02d}"


def _explicit_range(q: str, kind: str) -> tuple[str, str] | None:
    """解析问句里点明的具体时间区间，没有则 None。

    优先级：日级区间 > 单日 > 年月 > 无年月份。
    """
    lo = hi = ""

    def _range() -> tuple[str, str]:
        """只有推年份时才查库，避免每句问话都多打一次数据库。"""
        nonlocal lo, hi
        if not lo and not hi:
            lo, hi = _data_range(kind)
        return lo, hi

    dates = _collect_dates(q)
    if dates:
        def _abs(item):
            _, y, mo, d = item
            yy = y or _resolve_year(mo, *_range())
            try:
                return datetime.date(yy, mo, d)
            except ValueError:
                return None

        ds = [x for x in (_abs(it) for it in dates) if x]
        if ds:
            # 两个以上取首尾（「8月1日到8月31日」）；只有一个就是那一天
            return ds[0].isoformat(), ds[-1].isoformat()

    m = _YM_CN_RE.search(q)
    if m:
        month = _cn_month(m.group(2))
        if month:
            return _month_bounds(int(m.group(1)), month)
    m = _YM_ISO_RE.search(q)
    if m:
        month = _cn_month(m.group(2))
        if month:
            return _month_bounds(int(m.group(1)), month)
    m = _M_ONLY_RE.search(q)
    if m:
        month = _cn_month(m.group(1))
        if month:
            return _month_bounds(_resolve_year(month, *_range()), month)
    return None


def _looks_explicit(q: str) -> bool:
    """问句里是否点了具体时间（不查库的快速判断）。"""
    return bool(_collect_dates(q) or _YM_CN_RE.search(q)
                or _YM_ISO_RE.search(q) or _M_ONLY_RE.search(q))


def _detect_period(q: str) -> tuple[str, bool]:
    """返回 (period, prev)。

    顺序不能换：全量 > 显式区间 > 周期词 > 默认本月。
    「生成所有时间的月报」里同时有"所有时间"和"月报"，必须判成全量。
    """
    prev = bool(_PREV_RE.search(q))
    if _ALL_TIME_RE.search(q):
        return "none", False
    if _looks_explicit(q):
        return "custom", False
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


def _is_full_month(start: str, end: str) -> bool:
    """start/end 是否正好是一个自然整月。"""
    try:
        s = datetime.date.fromisoformat(start)
        e = datetime.date.fromisoformat(end)
    except ValueError:
        return False
    return (s.day == 1 and s.year == e.year and s.month == e.month
            and e.day == calendar.monthrange(e.year, e.month)[1])


def _build_title(kind: str, period: str, prev: bool,
                 start: str = "", end: str = "") -> str:
    """拼报告标题：生产+周报 → 生产周报；指定区间则把区间写进标题。

    标题必须和内容对得上：用户要「2026 年 8 月」，标题就不能还写"生产月报"，
    否则同一份报告隔一周再生成，看起来像同一期、数字却不同。
    """
    label = _KIND_LABEL.get(kind, "经营")
    if kind == "inventory" and period in ("custom", "none"):
        # 库存是时点快照，模板固定取最新一期（SUM 全历史会放大几十倍）。
        # 用户点了月份时标题必须说清这一点，不能挂着「2026 年 8 月」的名头
        # 展示 9 月的库存水位。
        return f"{label}报告（最新快照）"
    if period == "none":
        return f"{label}报告（全部时间）"
    if period == "custom" and start and end:
        if _is_full_month(start, end):
            return f"{label}月报（{int(start[:4])}年{int(start[5:7])}月）"
        if start == end:
            return f"{label}日报（{start}）"
        return f"{label}报告（{start} ~ {end}）"
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

    kind = _detect_kind(q)
    period, prev = _detect_period(q)
    ref = _today()
    window_note = ""

    if period == "none":
        # 全量：区间就是库内真实数据覆盖范围，不猜、不缩
        lo, hi = _data_range(kind)
        start, end = (lo, hi) if lo and hi else _span("month", ref, prev)
        if not (lo and hi):
            window_note = "未能读到库内数据的时间范围，已退回本月区间。"
    elif period == "custom":
        rng = _explicit_range(q, kind)
        if rng:
            start, end = rng
        else:
            period = "month"
            start, end = _span(period, ref, prev)
    else:
        start, end = _span(period, ref, prev)

    # ── 数据水位钳制 ──────────────────────────────────────────
    # 2026-10-05 修复（P0）：区间原本一律以「今天」收尾，但演示/私有库的
    # 数据往往滞后于今天 —— 实测 yans 库 mes_process_output 只有
    # 2026-08-01 ~ 2026-09-15，而「生产周报」算出的区间是 10-05~10-05，
    # 于是**所有章节都返回 0 行**，报告却显示「取数成功 6/6」，
    # 用户看到的是一份全空报告（去掉时间条件立刻有数据：投入 318 万、良率 97.56%）。
    #
    # 2026-11-07 二次修复（P0）：原实现把"整段超出数据"的区间按**等长平移**回退，
    # 于是「上个月」（数据只到 09-15 时）被算成 08-16~09-15 —— 既不是"上个月"，
    # 也不是任何一个自然月，标题却仍写"上月生产月报"。改成按**自然周期**回退到
    # 数据末日所在的那一期，并在标题里写明原请求区间。
    data_max = _data_max_date()
    if data_max and end > data_max and period not in ("none", "custom"):
        req_span = f"{start} ~ {end}"
        if start > data_max:
            asof_d = datetime.date.fromisoformat(data_max)
            start, end = _span(period, asof_d, False)
            # 已经回退到"数据末日所在那一期"，标题里的「上月/上周」就不成立了，
            # 否则会出现"上月生产月报"配着 09-01~09-15 这种自相矛盾的封面。
            prev = False
            window_note = (f"库内数据截至 {data_max}，原请求区间 {req_span} 无数据，"
                           f"已改为最近可用区间 {start} ~ {end}。")
        else:
            end = data_max
            window_note = f"库内数据截至 {data_max}，统计区间已收窄至 {start} ~ {end}。"

    title = _build_title(kind, period, prev, start, end)

    return ReportRequest(
        kind=kind, label=_KIND_LABEL.get(kind, "经营"),
        period=period, title=title, start=start, end=end,
        query=query, matched_by=matched_by, prev=prev,
        window_note=window_note,
    )


def _data_max_date() -> str:
    """业务事实表里最新的日期（复用 _data_range 的 5 分钟缓存）。

    这是全局钳制用的水位，固定取生产事实表；按报告类型精确取区间请用 _data_range。
    """
    return _data_range("production")[1]


def is_report_request(query: str) -> bool:
    return detect(query) is not None
