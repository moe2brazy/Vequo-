# -*- coding: utf-8 -*-
"""报告时间窗口测试：用户点名的时间必须被真正用上。

2026-11-07 的线上故障：无论问「生成所有时间的报告」「2026 年 8 月的报告」
还是「上个月」，出来的都是 2026-09-09 ~ 2026-09-15 那几天。
根因是 report_intent._detect_period 不认年月、不认「所有时间」，
全部落到兜底的「本月」，再被数据水位钳到数据末日所在的那一周。

这个文件守住的线：
1. 「所有时间/全部/全量/历史」→ 区间就是库内真实覆盖范围，不许缩小
2. 点名年月 / 日期区间 → 就用那个区间，一个字都不许改
3. 区间整段落在数据之外 → 按自然周期回退（不是等长平移），且标题不再谎称"上月"
4. _align_window 对用户点名的区间只能收窄，不能替换成"近 30 天"
5. 普通问数不能被报告意图吞掉
"""
import sys

sys.path.insert(0, '.')

import datetime

from agent.report_intent import detect, _data_range, _span, _is_full_month

PASS, FAIL = 0, 0


def rec(name, inp, expect, actual, ok, note=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"{'✅' if ok else '❌'} {name}\n     输入: {inp!r}\n     预期: {expect}\n"
          f"     实际: {actual}{(' | ' + note) if note else ''}")


DATA_LO, DATA_HI = _data_range("production")
HAS_DB = bool(DATA_LO and DATA_HI)
print(f"库内数据区间: {DATA_LO or '(不可用)'} ~ {DATA_HI or '(不可用)'}\n")

print("═══ A. 全量语义：所有时间不许被缩成一个周期 ═══\n")

for q in ("生成所有时间的生产报告", "生成全量生产报告", "生成历史全部生产报告",
          "生成全部时间的生产报告"):
    r = detect(q)
    ok = bool(r) and r.period == "none"
    if ok and HAS_DB:
        ok = (r.start, r.end) == (DATA_LO, DATA_HI)
    rec("全量", q, f"period=none 且区间={DATA_LO}~{DATA_HI}",
        f"period={getattr(r, 'period', None)} 区间={getattr(r, 'start', '')}~{getattr(r, 'end', '')}",
        ok)

# 「所有时间的月报」里同时有"所有时间"和"月报"两个信号，
# 识别顺序若把周期词放前面，就会退化成"本月月报" —— 这是最容易回归的一条。
r = detect("生成所有时间的生产月报")
rec("全量", "生成所有时间的生产月报（全量 vs 周期词冲突）",
    "period=none（全量优先）", f"period={getattr(r, 'period', None)}", bool(r) and r.period == "none")

print("\n═══ B. 点名年月：必须精确到那个月 ═══\n")

for q in ("生成2026年8月的生产报告", "生成2026年8月生产月报", "8月份的生产报告",
          "生成2026-08的生产报告"):
    r = detect(q)
    ok = bool(r) and r.period == "custom" and (r.start, r.end) == ("2026-08-01", "2026-08-31")
    rec("年月", q, "2026-08-01 ~ 2026-08-31",
        f"{getattr(r, 'start', '')} ~ {getattr(r, 'end', '')}", ok)

r = detect("生成2026年2月的生产报告")
rec("年月", "2026年2月（闰年边界改 2026 非闰年）", "2026-02-01 ~ 2026-02-28",
    f"{getattr(r, 'start', '')} ~ {getattr(r, 'end', '')}",
    bool(r) and (r.start, r.end) == ("2026-02-01", "2026-02-28"))

print("\n═══ C. 点名日期区间 ═══\n")

for q, exp in (
    ("生成2026年8月1日到8月31日的生产报告", ("2026-08-01", "2026-08-31")),
    ("生成2026-08-01至2026-08-31的生产报告", ("2026-08-01", "2026-08-31")),
    ("生成2026年8月1日到9月15日的生产报告", ("2026-08-01", "2026-09-15")),
    ("生成2026年8月15日的生产报告", ("2026-08-15", "2026-08-15")),
):
    r = detect(q)
    ok = bool(r) and (r.start, r.end) == exp
    rec("日期区间", q, f"{exp[0]} ~ {exp[1]}",
        f"{getattr(r, 'start', '')} ~ {getattr(r, 'end', '')}", ok)

print("\n═══ D. 数据水位回退：不许等长平移 ═══\n")

r = detect("生成上个月的生产报告")
ok = bool(r)
if ok:
    # 关键回归：曾经算出 08-16 ~ 09-15 这种"既不是上月也不是自然月"的区间
    ok = r.start.endswith("-01") and r.end == DATA_HI
    ok = ok and r.period == "month"
rec("水位回退", "生成上个月的生产报告（数据停在 09-15）",
    f"按自然月回退：某月 01 日 ~ {DATA_HI}，不是 08-16~09-15",
    f"{getattr(r, 'start', '')} ~ {getattr(r, 'end', '')}", ok,
    note=f"note={getattr(r, 'window_note', '')[:50]}")

if ok:
    bad = "上月" in r.title
    rec("水位回退", "回退后标题不许再写「上月」", "标题不含「上月」", r.title, not bad)

r = detect("生成2026年10月的生产报告")
rec("水位回退", "点名 10 月但数据没有 → 区间保持原样，不偷偷换掉",
    "2026-10-01 ~ 2026-10-31",
    f"{getattr(r, 'start', '')} ~ {getattr(r, 'end', '')}",
    bool(r) and (r.start, r.end) == ("2026-10-01", "2026-10-31"))

print("\n═══ E. 标题与区间必须一致 ═══\n")

r = detect("生成2026年8月的生产报告")
rec("标题", "点名 8 月的标题要带年月", "标题含 2026年8月 且含 月报",
    getattr(r, "title", ""),
    bool(r) and "2026年8月" in r.title and "月报" in r.title)

r = detect("生成所有时间的生产报告")
rec("标题", "全量报告标题", "标题含「全部时间」", getattr(r, "title", ""),
    bool(r) and "全部时间" in r.title)

r = detect("生成2026年8月的库存报告")
rec("标题", "库存是快照，点名月份也不能挂月份的名头",
    "标题含「最新快照」且不含「2026年8月」", getattr(r, "title", ""),
    bool(r) and "最新快照" in r.title and "2026年8月" not in r.title)

print("\n═══ F. 周期区间长度仍然正确 ═══\n")

today = datetime.date.today()
for period, max_days, label in (("day", 0, "日报"), ("week", 6, "周报")):
    s, e = _span(period, today, False)
    span = (datetime.date.fromisoformat(e) - datetime.date.fromisoformat(s)).days
    rec("周期", f"{label}区间长度", f"{max_days} 天内", f"{span} 天（{s}~{e}）",
        0 <= span <= max_days)

s, e = _span("month", datetime.date(2026, 8, 15), False)
rec("周期", "月报区间是自然月", "2026-08-01 ~ 2026-08-15（当期算到今天/参考日）",
    f"{s} ~ {e}", (s, e) == ("2026-08-01", "2026-08-15"))
rec("周期", "整月判定 helper", "2026-08-01~2026-08-31 是整月",
    str(_is_full_month("2026-08-01", "2026-08-31")), _is_full_month("2026-08-01", "2026-08-31"))

print("\n═══ G. _align_window 不许覆盖用户点名的区间 ═══\n")

from agent.periodic_report import _align_window
from agent.report_intent import ReportRequest


def mk(period, start, end):
    return ReportRequest(kind="production", label="生产", period=period,
                         title="t", start=start, end=end)


rr = mk("custom", "2026-08-01", "2026-08-31")
_align_window(rr)
rec("对齐", "点名 8 月（整段在数据内）", "区间不变",
    f"{rr.start} ~ {rr.end}", (rr.start, rr.end) == ("2026-08-01", "2026-08-31"))

rr = mk("custom", "2026-10-01", "2026-10-31")
_align_window(rr)
rec("对齐", "点名 10 月（整段在数据之后）", "区间不变 + 有「无数据」说明",
    f"{rr.start} ~ {rr.end} note={bool(rr.window_note)}",
    (rr.start, rr.end) == ("2026-10-01", "2026-10-31") and bool(rr.window_note))

rr = mk("none", DATA_LO or "2026-08-01", DATA_HI or "2026-09-15")
_align_window(rr)
rec("对齐", "全量区间", "区间不变（水位对齐对全量不生效）",
    f"{rr.start} ~ {rr.end}",
    (rr.start, rr.end) == (DATA_LO or "2026-08-01", DATA_HI or "2026-09-15"))

print("\n═══ H. 普通问数不能被报告意图吞掉 ═══\n")

for q in ("全厂良率是多少", "各产线良率", "上个月的生产报告有多少条", "报告一下昨天的产量"):
    rec("不误吞", q, "detect 返回 None", f"detect={detect(q)}", detect(q) is None)

print(f"\n{'=' * 56}\nTOTAL pass={PASS} fail={FAIL}")
sys.exit(1 if FAIL else 0)
