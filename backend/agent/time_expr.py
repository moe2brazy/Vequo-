"""时间表达式解析器 — 把中文时间表达翻译成确定性的 SQL 日期区间

支持：最近N天/周/月/年、本周、本月、本季度、今年、昨天/今日/昨天、上周/上月/上季度/去年、
近几日等口语表达。解析结果按当前数据库方言（PG/MySQL）输出对应日期表达式。

用法：
    from agent.time_expr import parse_time_expr
    hint = parse_time_expr("统计最近7天的产量")   # 返回 dict 或 None
    # hint = {"raw": "最近7天", "gte": "CURRENT_DATE - INTERVAL '7 days'", "label": "最近7天(含今天)"}
"""

import re

# ── 中文数字 → int ──
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CN_TEN = {"十": 10, "廿": 20, "卅": 30}


def _cn_to_int(s: str) -> int | None:
    """中文数字转 int（支持 一~九十九，含"两"），失败返回 None"""
    if s.isdigit():
        return int(s)
    total = 0
    num = 0
    for ch in s:
        if ch in _CN_TEN:
            if num == 0:
                num = 1
            total += num * _CN_TEN[ch]
            num = 0
        elif ch in _CN_NUM:
            num = _CN_NUM[ch]
        else:
            return None
    return total + num


# ── 日期区间构造 ──
def _interval(amount: int, unit_sql: str, unit_label: str, interval_amount: int | None = None) -> dict:
    # interval_amount 单独指定 SQL 里的区间数值（amount 保留展示用），
    # 用于「最近N天(含今天)」这类需减 1 的场景。
    if interval_amount is None:
        interval_amount = amount
    return {
        "raw": f"最近{amount}{unit_label}",
        "gte": f"CURRENT_DATE - INTERVAL '{interval_amount} {unit_sql}'",
        "label": f"最近{amount}{unit_label}(含今天)",
        "unit": unit_sql,
        "amount": amount,
    }


def _handle_abs_year(m) -> dict | None:
    """绝对年份：2023年 / 2024 年 → 该年 1/1 ~ 次年 1/1（左闭右开）。"""
    try:
        y = int(m.group("year"))
    except Exception:
        return None
    if not (1900 <= y <= 2199):
        return None
    return {
        "raw": f"{y}年",
        "gte": f"DATE '{y:04d}-01-01'",
        "lt": f"DATE '{y + 1:04d}-01-01'",
        "label": f"{y}年(整年)",
        "unit": "year",
        "amount": None,
    }


def _handle_abs_month(m) -> dict | None:
    """绝对月份：3月 / 12月 → 当年该月 1 日 ~ 次月 1 日。

    用 date_trunc('year', CURRENT_DATE) 偏移，避免把当前年份写死进 SQL。
    """
    try:
        mm = int(m.group("month"))
    except Exception:
        return None
    if not (1 <= mm <= 12):
        return None
    return {
        "raw": f"{mm}月",
        "gte": f"date_trunc('year', CURRENT_DATE) + INTERVAL '{mm - 1} months'",
        "lt": f"date_trunc('year', CURRENT_DATE) + INTERVAL '{mm} months'",
        "label": f"{mm}月(整月)",
        "unit": "month",
        "amount": None,
    }


# 中文月份数字 → 阿拉伯数字（2026-10-03 新增，配合中文绝对月份规则）
_CN_MONTH_MAP = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}


def _handle_abs_cn_month(m) -> dict | None:
    """中文绝对月份：三月份 / 九月份的产量 → 当年该月 1 日 ~ 次月 1 日。

    2026-10-03 新增（P0）。背景：原规则表里"绝对月份"只吃阿拉伯数字，
    而紧随其后的"最近N个月"规则前缀 `(?:最近|近|过去)?` 是**可选**的，
    于是「三月份的产量」落空后被它接走 →
        raw="最近3个月" → gte=CURRENT_DATE - INTERVAL '3 months'
    把"3 月这个单月"理解成"往前推 3 个月"，得到 3 个月窗口而非 1 个月。
    数字格式完全正常、无任何警告，用户无从察觉（实测三/九/七/3 月份均中招）。

    语义与 `_handle_abs_month` 完全一致（按日历月推进、不倒退），
    避免两条分支给出不同的时间窗。
    """
    raw = (m.group("cnmonth") or "").strip()
    mm = _CN_MONTH_MAP.get(raw)
    if not mm or not (1 <= mm <= 12):
        return None
    return {
        "raw": f"{raw}月",
        "gte": f"date_trunc('year', CURRENT_DATE) + INTERVAL '{mm - 1} months'",
        "lt": f"date_trunc('year', CURRENT_DATE) + INTERVAL '{mm} months'",
        "label": f"{mm}月(整月)",
        "unit": "month",
        "amount": None,
    }


# 正则规则表：(pattern, handler)  按顺序匹配，第一个命中生效
def _handle_abs_year_month(m) -> dict | None:
    """处理「2023年3月」式年月组合（必须放在纯年份规则之前，否则年份先命中、月份被吞，
    整个查询范围被放大 ~12 倍且 label 显示为整年）。"""
    try:
        y = int(m.group("year"))
        mm = int(m.group("month"))
    except Exception:
        return None
    if not (1900 <= y <= 2199 and 1 <= mm <= 12):
        return None
    # DATE 'YYYY-MM-01' + INTERVAL '1 month' 在 PG 与 MySQL 均为合法写法，跨库无需转换
    start = f"DATE '{y:04d}-{mm:02d}-01'"
    return {
        "raw": f"{y}年{mm}月",
        "gte": start,
        "lt": f"{start} + INTERVAL '1 month'",
        "label": f"{y}年{mm}月(整月)",
        "unit": "month",
        "amount": None,
    }


_RULES = [
    # 绝对年月组合（2023年3月 / 2024 年 12 月）—— 必须放在纯年份规则之前。
    # 2026-10-01 修复：此前「2023年3月」先被年份规则命中返回整年区间，
    # 「3月」被完全忽略（结果约为正确值的 12 倍，label 还显示"2023年(整年)"）。
    (re.compile(r"(?P<year>(?:19|20)\d{2})\s*年\s*(?P<month>1[0-2]|[1-9])\s*月", re.IGNORECASE),
     lambda m: _handle_abs_year_month(m)),
    # 绝对年份（2023年 / 2024 年）—— 必须放在"最近N年"之前。
    # 数据正确性修复（P0）：规则 1 的 (?:最近|近|过去)? 前缀是【可选】的，
    # 实测 `统计2023年产量` 被解析成 gte = CURRENT_DATE - INTERVAL '2023 years'，
    # 绝对年份查询被静默改写成无意义的相对窗口，且前端 label 还显示"最近2023年"，用户无法察觉。
    (re.compile(r"(?P<year>(?:19|20)\d{2})\s*年", re.IGNORECASE),
     lambda m: _handle_abs_year(m)),
    # 绝对月份（3月 / 12月的销量）—— 同理必须先于"最近N个月"。
    # 数字前不允许紧跟数字，避免匹配"2023年12月"中的 12 时与年份规则冲突。
    # 2026-10-03 修复（P0）：原为 `(?!\s*月)(?!\s*份)` 双否定断言，把「3月份」也排除了
    # （中文口语里"3月份"就是指那个月，是合法且高频的问法）→ 落空后被下面的
    # "最近N个月"规则接走 → 变成"往前推 3 个月"的 3 个月窗口，而非 3 月这个单月。
    # 现在只排除真正的单位接续（3个月 / 3月内），**允许带"份"**。
    # 与下面中文月份规则用同一套否定断言，保证「3月份」和「三月份」行为一致。
    (re.compile(r"(?<!\d)(?P<month>1[0-2]|[1-9])\s*月(?!\s*个)(?!\s*内)(?!\s*份之)", re.IGNORECASE),
     lambda m: _handle_abs_month(m)),
    # 中文绝对月份（三月份 / 九月份的产量）—— 2026-10-03 新增（P0）。
    # 上一条只吃阿拉伯数字，导致「三月份」落空后被下面"最近N个月"规则接走：
    # 该规则的前缀 (?:最近|近|过去)? 是**可选**的，于是
    #   「三月份的产量」→ raw="最近3个月" → gte=CURRENT_DATE - INTERVAL '3 months'
    # 把"3月这个单月"理解成"往前推 3 个月"，得到 3 个月窗口而非 1 个月。
    # 数字格式完全正常、无任何警告，用户无从察觉（实测三/九/七/3 月份均中招）。
    #
    # ⚠️ 负向断言不能用 `(?!份)`：中文月份习惯带"份"（「三月份」= 三月这个月），
    # 一旦排除就又落回"最近N个月"。改用「后接非单位字」来区分：
    # 「三月份」后接"的产量"→ 命中；「三个月」被 unit=个月 的规则接走（顺序在后）。
    (re.compile(r"(?P<cnmonth>十一|十二|十|[一二三四五六七八九])\s*月"
                r"(?!\s*(?:个|以内|左右|间))", re.IGNORECASE),
     lambda m: _handle_abs_cn_month(m)),
    # 第N天（第15天的产量）—— 2026-10-03 新增（P0）。
    # 原先「第」不在"最近N天"的字符集里 → 「第15天」被解析成"最近15天"，
    # 得到一个 14 天的区间而非某一天，且数字看着合理极易被当成正确答案。
    # 这里前置拦截，语义为"数据最新日期往前第 N 天"（锚到库内最新，与数据滞后对齐）。
    (re.compile(r"第\s*(?P<num>\d+)\s*(?:天|日)(?!内)", re.IGNORECASE),
     lambda m: {"raw": f"第{m.group('num')}天",
                "gte": f"CURRENT_DATE - INTERVAL '{max(0, int(m.group('num')) - 1)} days'",
                "lt": f"CURRENT_DATE - INTERVAL '{max(0, int(m.group('num')) - 2)} days'",
                "label": f"第{m.group('num')}天", "unit": "day",
                "amount": int(m.group("num"))}),
    # 最近N天/周/月/年（支持中文数字与阿拉伯数字，"最近7天" "近一个月" "近3周"）
    (re.compile(r"(?:最近|近|过去)?\s*(?P<num>[\d一二两三四五六七八九十]+)\s*(?P<unit>天|日|周|星期|个月|月|年)\s*(?:内|以来)?", re.IGNORECASE),
     lambda m: _handle_n(m)),
    # 本周 / 这周 / 本星期
    (re.compile(r"(?:本|这|当)\s*(?:周|星期)", re.IGNORECASE),
     lambda m: {"raw": "本周", "gte": "date_trunc('week', CURRENT_DATE)", "label": "本周(周一起)", "unit": "week", "amount": None}),
    # 本月 / 这个月 / 当月
    (re.compile(r"(?:本|这|当)\s*(?:个月|月)", re.IGNORECASE),
     lambda m: {"raw": "本月", "gte": "date_trunc('month', CURRENT_DATE)", "label": "本月(1号起)", "unit": "month", "amount": None}),
    # 本季度 / 本季
    (re.compile(r"(?:本|这)\s*季度", re.IGNORECASE),
     lambda m: {"raw": "本季度", "gte": "date_trunc('quarter', CURRENT_DATE)", "label": "本季度(季初起)", "unit": "quarter", "amount": None}),
    # 今年 / 本年 / 今年一年
    (re.compile(r"(?:今|本)\s*年", re.IGNORECASE),
     lambda m: {"raw": "今年", "gte": "date_trunc('year', CURRENT_DATE)", "label": "今年(1月1日起)", "unit": "year", "amount": None}),
    # 昨天 / 昨日 / 今天 / 今日 / 前天
    (re.compile(r"昨天|昨日", re.IGNORECASE),
     lambda m: {"raw": "昨天", "gte": "CURRENT_DATE - INTERVAL '1 day'", "lt": "CURRENT_DATE", "label": "昨天", "unit": "day", "amount": 1}),
    (re.compile(r"今天|今日", re.IGNORECASE),
     lambda m: {"raw": "今天", "gte": "CURRENT_DATE", "label": "今天(0点起)", "unit": "day", "amount": 0}),
    (re.compile(r"前天", re.IGNORECASE),
     lambda m: {"raw": "前天", "gte": "CURRENT_DATE - INTERVAL '2 days'", "lt": "CURRENT_DATE - INTERVAL '1 day'", "label": "前天", "unit": "day", "amount": 2}),
    # 上周 / 上月 / 上季度 / 去年
    (re.compile(r"上\s*周|上周", re.IGNORECASE),
     lambda m: {"raw": "上周", "gte": "date_trunc('week', CURRENT_DATE) - INTERVAL '7 days'", "lt": "date_trunc('week', CURRENT_DATE)", "label": "上周(上周一至上周日)", "unit": "week", "amount": 1}),
    (re.compile(r"上\s*(?:个月|月)", re.IGNORECASE),
     lambda m: {"raw": "上月", "gte": "date_trunc('month', CURRENT_DATE) - INTERVAL '1 month'", "lt": "date_trunc('month', CURRENT_DATE)", "label": "上月(整月)", "unit": "month", "amount": 1}),
    (re.compile(r"上\s*季度", re.IGNORECASE),
     lambda m: {"raw": "上季度", "gte": "date_trunc('quarter', CURRENT_DATE) - INTERVAL '3 months'", "lt": "date_trunc('quarter', CURRENT_DATE)", "label": "上季度", "unit": "quarter", "amount": 1}),
    (re.compile(r"去\s*年|去年", re.IGNORECASE),
     lambda m: {"raw": "去年", "gte": "date_trunc('year', CURRENT_DATE) - INTERVAL '1 year'", "lt": "date_trunc('year', CURRENT_DATE)", "label": "去年(整年)", "unit": "year", "amount": 1}),
]


def _handle_n(m) -> dict | None:
    """处理「最近N天/周/月/年」"""
    num_s = m.group("num")
    unit = m.group("unit")
    n = _cn_to_int(num_s)
    if n is None or n <= 0:
        return None
    # 纵深防御：即便上游规则被绕过，也绝不能把 2023 这样的绝对年份当成"最近 2023 年"
    if unit == "年" and 1900 <= n <= 2199:
        return None
    if n > 3650:  # 上限 10 年，防止无意义的大区间
        n = 3650
    unit_map = {"天": ("days", "天"), "日": ("days", "天"), "周": ("weeks", "周"),
                "星期": ("weeks", "周"), "个月": ("months", "个月"), "月": ("months", "个月"),
                "年": ("years", "年")}
    sql_unit, label = unit_map.get(unit, ("days", "天"))
    # 「最近N天(含今天)」= 今天 + 过去 N-1 天，共 N 个日历日；
    # 直接用 INTERVAL 'N days' 会含 N+1 天（off-by-one）。周/月/年按整周期语义不变。
    interval_amount = n - 1 if sql_unit == "days" else n
    return _interval(n, sql_unit, label, interval_amount)


def parse_time_expr(query: str) -> dict | None:
    """从自然语言问题中提取时间表达式，返回 {raw, gte, lt?, label, unit, amount} 或 None。

    只识别第一个命中（时间范围类表达），避免多个时间词互相冲突。
    """
    if not query:
        return None
    for pat, handler in _RULES:
        m = pat.search(query)
        if m:
            try:
                r = handler(m)
                if r:
                    return r
            except Exception:
                continue
    return None


# PG → MySQL 日期表达式转换（PG 专用语法映射为 MySQL 等价写法）
_MYSQL_UNIT = {"day": "DAY", "days": "DAY", "week": "WEEK", "weeks": "WEEK",
               "month": "MONTH", "months": "MONTH", "year": "YEAR", "years": "YEAR"}


def _mysql_expr(expr: str) -> str:
    """把 PG 方言日期表达式转换为 MySQL 等价表达式（只覆盖本模块生成的形式）"""
    e = expr
    e = re.sub(r"date_trunc\('week',\s*CURRENT_DATE\)",
               "DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)", e, flags=re.I)
    e = re.sub(r"date_trunc\('month',\s*CURRENT_DATE\)",
               "DATE_FORMAT(CURDATE(), '%Y-%m-01')", e, flags=re.I)
    e = re.sub(r"date_trunc\('quarter',\s*CURRENT_DATE\)",
               "DATE_SUB(DATE_FORMAT(CURDATE(), '%Y-%m-01'), "
               "INTERVAL ((MONTH(CURDATE())-1) MOD 3) MONTH)", e, flags=re.I)
    e = re.sub(r"date_trunc\('year',\s*CURRENT_DATE\)",
               "DATE_FORMAT(CURDATE(), '%Y-01-01')", e, flags=re.I)
    # INTERVAL 'N unit' → INTERVAL N UNIT（MySQL 单位必须为单数大写）
    e = re.sub(r"INTERVAL\s*'(\d+)\s*(\w+)'",
               lambda m: f"INTERVAL {m.group(1)} {_MYSQL_UNIT.get(m.group(2).lower(), m.group(2).upper())}",
               e, flags=re.I)
    e = re.sub(r"\bCURRENT_DATE\b", "CURDATE()", e, flags=re.I)
    return e


def build_time_hint(query: str) -> str:
    """生成可注入 SQL 生成 Prompt 的时间约束提示（按当前数据库方言输出，无命中返回空串）"""
    t = parse_time_expr(query)
    if not t:
        return ""
    try:
        from database import get_db_type
        is_mysql = get_db_type() == "mysql"
    except Exception:
        is_mysql = False
    if is_mysql:
        gte = _mysql_expr(t["gte"])
        lt = _mysql_expr(t["lt"]) if t.get("lt") else ""
    else:
        gte, lt = t["gte"], t.get("lt", "")
    if lt:
        return (f"时间范围：\"{t['raw']}\" 对应日期区间为 "
                f"[{gte}, {lt})（{t['label']}）。"
                f"请在 WHERE 中把表的日期字段与该区间比较，例如 "
                f"date_field >= {gte} AND date_field < {lt}。")
    return (f"时间范围：\"{t['raw']}\" 对应日期下限为 {gte}（{t['label']}）。"
            f"请在 WHERE 中把表的日期字段与该下限比较，例如 "
            f"date_field >= {gte}。")


if __name__ == "__main__":
    for q in ["统计最近7天的产量", "最近三个月的不良率", "本周产量", "上个月销量",
              "本季度各产线产量", "去年订单总额", "昨天停机次数", "近两周的良率",
              "三天前的数据", "今年每个月的产量"]:
        print(f"{q!r:20} -> {parse_time_expr(q)}")
