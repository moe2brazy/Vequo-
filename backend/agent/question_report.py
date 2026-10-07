# -*- coding: utf-8 -*-
"""针对「用户提问」生成专业分析报告的引擎。

与 `html_report.py` 的区别（这是本次新增的关键点）：
- `html_report.py` 出的是**整库总览**报告——它不看用户问了什么，只把库里所有表
  列一遍，再用 LLM 写一段总结。用户问"各产线良率对比"，拿到的却是"数据库共 10 张表"。
- 本模块是**跟着问题走**的：拿用户那句问题去生成并执行真实 SQL，把结果整理成
  指标卡 + 图表 + 结构化分析章节，最后拼成一份可读、可导出、带图示的报告。

图示怎么来的（不要图表库、不要 CDN，报告出成单文件 HTML 直接能开）：
用内联 SVG 自绘——柱状 / 折线 / 环形 / 进度条 / KPI 大数字，全部按数据算坐标。
之所以不用 ECharts：报告是要能存盘、能发出去、能导出 PDF 的文件，
挂 CDN 就依赖网络，塞整份 echarts.min.js 又让文件涨到 1MB 且导出 PDF 时渲染不稳。
自绘 SVG 在 Word/PDF 转换链里是矢量的，放大不糊。

分析文字怎么来的：
把"问题 + 真实执行结果（表格数据）"喂给 LLM，让它按固定章节写。
关键约束写在提示词里——只能引用给定数据里的数字，不许编。
"""

from __future__ import annotations

import asyncio
import datetime
import html as html_mod
import re
from typing import Any, AsyncIterator

# ── 主题色（与前端界面同源，Vequo 品牌蓝）──────────────────
C_BLUE = "#2E7CF0"
C_BLUE_LT = "#8FC5FF"
C_INK = "#1d2129"
C_GRAY = "#4e5969"
C_MUTE = "#a9aeb8"
C_BORDER = "#e5e6eb"
C_BG_SOFT = "#f7f9fc"
C_UP = "#d93f2b"      # 涨（中式口径：红涨）
C_DOWN = "#1677ff"    # 跌（绿跌 → 这里用品牌蓝，避免报告里出现刺眼绿）

# 图表配色序列（同色系深浅，避免花哨）
PALETTE = [C_BLUE, "#4D9EFF", "#8FC5FF", C_INK, "#1F66D6", "#B9DCFF", "#5FAEFF", "#1677ff"]


# ════════════════════════════════════════════════════════════
# 1. 取数：把用户问题变成真实数据
# ════════════════════════════════════════════════════════════

def _fmt_num(v: Any) -> str:
    """数字格式化：整数带千分位，小数保留两位（去尾零）。"""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return f"{int(v):,}"
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    s = str(v)
    # 纯数字字符串也格式化
    try:
        f = float(s)
        return _fmt_num(f)
    except (TypeError, ValueError):
        return s


def _looks_like_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        return bool(re.match(r"^-?\d+(\.\d+)?$", v.strip()))
    return False


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _norm_rows(result: dict) -> tuple[list[str], list[dict]]:
    """把 execute_sql 的返回规整成 (列名, 行)。列名顺序保留。"""
    cols = list(result.get("columns") or [])
    rows = list(result.get("rows") or [])
    if not cols and rows:
        cols = list(rows[0].keys())
    return cols, rows


def _pick_dim_measure(cols: list[str], rows: list[dict]) -> tuple[str | None, str | None]:
    """从结果里挑出「维度列」和「度量列」——图表的 X 轴和 Y 轴。

    判据：能转成数字的列是度量；第一列（通常是分组名/时间）当维度。
    优先挑名字带 count/qty/qty/rate/ratio/amount/num/total/良率/数量 的度量列。
    """
    if not rows or not cols:
        return None, None
    dim = cols[0]
    numeric = [c for c in cols if any(_looks_like_number(r.get(c)) for r in rows[:20])]
    numeric = [c for c in numeric if c != dim]
    if not numeric:
        # 没有度量列：退化成"按维度计数"
        return dim, None

    # 按"业务关注度"分档挑，而不是拿到第一个数字列就用。
    # 起因：一次真实结果 [产线, 投入数量, 合格数量, 良率]，如果只认"名字带数量"，
    # 会把「投入数量」当成主指标——于是报告通篇在比较各线投了多少料，
    # 而用户问的是良率。数量大不等于该讲，比率型指标才是一句话能说清好坏的那个。
    tiers = (
        # ① 比率/效率类：率、比例、占比、单耗——好坏方向明确，最适合当主指标
        ("rate", "ratio", "pct", "percent", "yield", "efficiency",
         "率", "比例", "占比", "效率"),
        # ② 绝对量类：数量、金额、合计
        ("count", "qty", "quantity", "amount", "num", "total", "sum",
         "数量", "合计", "总数", "金额", "次数"),
        # ③ 均值类
        ("avg", "mean", "均"),
    )
    for group in tiers:
        for c in numeric:
            if any(h in c.lower() for h in group):
                return dim, c
    return dim, numeric[0]


_FIELD_LABEL_CACHE: dict[str, str] = {}
# 纯中文名单独缓存一份：field_label() 和 field_label_zh() 的入参相同但结果不同，
# 共用一个 dict 会互相覆盖，出现"第一次调谁、后面全按谁的结果"的随机性 bug。
_FIELD_LABEL_ZH_CACHE: dict[str, str] = {}


# 计算列兜底：有些列根本不是表里的字段，而是 SQL 现算的别名（良率、达成率这类）。
# 元数据里查不到，就会一路把 yield_rate_pct 印到报告正文里。这里按语义给个中文名，
# 但**只认这几条明确的组合**，不做泛化猜词——猜错比显英文更糟，
# 把「不良率」标成「良率」是要出事的。
_ALIAS_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("yield", "rate"), "良率"),
    (("pass", "rate"), "合格率"),
    (("defect", "rate"), "不良率"),
    (("achieve", "rate"), "达成率"),
    (("completion", "rate"), "完成率"),
    (("utilization", "rate"), "利用率"),
    (("output", "qty"), "产量"),
    (("plan", "qty"), "计划产量"),
    (("actual", "qty"), "实际产量"),
    (("input", "qty"), "投入数量"),
    (("good", "qty"), "合格数量"),
    (("defect", "qty"), "不良数量"),
    (("sample", "qty"), "抽检数量"),
    (("rework", "qty"), "返工数量"),
    (("total", "qty"), "总数量"),
    (("count",), "记录数"),
    # 模型偶尔会自己造一个"口径范围"列（SELECT '全厂' AS scope），
    # 这种没有对应字段，但它其实是标签不是指标——翻成"统计范围"才读得通。
    (("scope",), "统计范围"),
    (("result",), "检验结论"),
    (("status",), "状态"),
)

# 时间维度的别名（SQL 里 to_char(stat_date,'YYYY-MM') AS stat_month 这种）。
# 单独放一张表：时间列的命名花样比指标多，而且它们的含义是"哪段时间"，
# 不是"多少数量"，混在指标别名表里读起来容易串。
_ALIAS_TIME: tuple[tuple[tuple[str, ...], str], ...] = (
    (("stat", "month"), "统计月份"),
    (("month",), "月份"),
    (("stat", "date"), "统计日期"),
    (("date",), "日期"),
    (("week",), "周次"),
    (("quarter",), "季度"),
    (("year",), "年份"),
)


def _alias_label(col: str) -> str | None:
    """按词组合给计算列一个中文名；命中不了返回 None（调用方保留原名）。"""
    low = col.lower()
    for words, label in _ALIAS_RULES:
        if all(w in low for w in words):
            return label
    return None


def field_label_zh(col: str) -> str:
    """只翻成中文业务名，**不带**括号字段名（如 input_qty → 投入数量）。

    三级策略，越靠前越可信：
      ① 命中表字段 → 用元数据 description
      ② 命中固定组合别名 → 用内置表（yield_rate_pct → 良率）
      ③ 都不中 → 原样返回，不硬编

    单独抽出来是因为有些地方只放得下短标签（图表轴、KPI 卡小字），
    塞「投入数量（input_qty）」会挤爆；需要严格对应关系的地方用 field_label()。
    """
    if not col:
        return col
    key = str(col).strip()
    if key in _FIELD_LABEL_ZH_CACHE:
        return _FIELD_LABEL_ZH_CACHE[key]
    label = key
    try:
        from db.metadata import TABLES
        bare = key.split(".")[-1].lower()
        for t in TABLES:
            for f in (t.get("fields") or []):
                if str(f.get("name", "")).lower() == bare:
                    desc = str(f.get("description") or "").strip()
                    # description 形如「投入数量」或「工单ID → mes_work_order」，
                    # 只取箭头前的业务名部分，后半段的表引用对读者没意义。
                    if desc:
                        label = re.split(r"[→,，(（]", desc)[0].strip() or label
                    break
            if label != key:
                break
        # ②/③ 元数据没这条 → 先试时间别名（"哪段时间"），再试指标别名（"多少数量"）。
        # 顺序反了会把 stat_month 这类时间列当成指标去匹配。
        if label == key:
            label = (_alias_time_label(key) or _alias_label(key) or key)
    except Exception:
        pass
    _FIELD_LABEL_ZH_CACHE[key] = label
    return label


def field_label(col: str, raw_field: str | None = None) -> str:
    """翻成「中文业务名（原始字段名）」，如 input_qty → 投入数量（input_qty）。

    为什么把原字段名留在括号里：报告是给业务人员看的，正文出现 input_qty 是噪音；
    但**一旦列名被完全替换掉，读者就没法回头核对"这个数到底取自哪一列"**，
    出了问题（怀疑口径错）时也无从追溯。括号里留原字段名，两边都不牺牲。

    raw_field 用于列名本身已是中文的情况：模型写 `SELECT p.process_name AS 工序名称`
    时，列名是「工序名称」，光看名字推不出 process_name，得由调用方从 SQL 里解析出来传进来
    （见 _sql_select_aliases）。传了就用它当括号内容。

    三种情况不加括号，避免出现「投入数量（投入数量）」这种废话：
      · 原列名本来就是中文 **且** 拿不到底层字段名；
      · 翻译结果和原列名相同（没翻出来）；
      · 原列名里一个 ASCII 字母都没有，且翻译结果里没有中文。
    """
    if not col:
        return col
    key = str(col).strip()
    raw_field = str(raw_field or "").strip()
    # 缓存键带上 raw_field：同一个中文列名，解析到不同底层字段时结果不同。
    ck = f"{key}\x00{raw_field}"
    if ck in _FIELD_LABEL_CACHE:
        return _FIELD_LABEL_CACHE[ck]

    zh = field_label_zh(key)
    raw = raw_field or key.split(".")[-1]
    has_ascii = bool(re.search(r"[A-Za-z]", raw))
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", zh))
    # zh == key 表示这列名没被翻译过（本来就是这个字）；此时若还有底层字段名，
    # 仍要把字段名带上——否则「工序名称」这种读者永远不知道取自 process_name。
    if not has_ascii or not has_cjk:
        out = zh
    elif zh == raw:
        out = zh
    else:
        out = f"{zh}（{raw}）"
    _FIELD_LABEL_CACHE[ck] = out
    return out


def _alias_time_label(col: str) -> str | None:
    """时间维度列的别名翻译，命中不了返回 None。"""
    low = col.lower()
    for words, label in _ALIAS_TIME:
        if all(w in low for w in words):
            return label
    return None


def _sql_select_aliases(sql: str) -> dict[str, str]:
    """从 SQL 里读出「输出列名 → 底层字段名」，供中文别名补括号用。

    为什么需要这一步：模型写的 SQL 常常自带中文别名（`SELECT p.process_name AS 工序名称`），
    于是结果集的列名本来就是中文，光看列名**根本推不出它取自 process_name**。
    而报告恰恰要给读者留这条核对线索——数是从哪一列算出来的。
    SQL 里写着这个对应关系，直接解析出来比猜准。

    只在「表达式里恰好一个列引用」时给映射：
      · `p.process_name`            → process_name      给
      · `SUM(o.input_qty)`          → input_qty         给
      · `SUM(good_qty)*100/SUM(...)`→ 两个引用          不给（不是某一列）
      · `COUNT(*)`                  → 零个引用          不给
    比率、占比这类计算列的"来源"是多个字段，硬挑一个标在括号里等于撒谎。
    解析失败一律返回空 dict，只是少一层括号，不影响取数。
    """
    if not sql:
        return {}
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:
        return {}
    out: dict[str, str] = {}
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return {}
    if tree is None:
        return {}
    for sel in tree.selects:
        name = str(sel.alias or "").strip()
        if not name:
            # 没写 AS 的裸列（SELECT process_name）→ 输出名就是列名本身，不用补
            continue
        if not re.search(r"[\u4e00-\u9fff]", name):
            # 别名不是中文，说明下游本来就知道真实列名，不需要反查
            continue
        cols: list[str] = []
        for c in sel.find_all(exp.Column):
            cn = str(c.name or "").strip()
            if cn and cn != "*" and cn not in cols:
                cols.append(cn)
        if len(cols) == 1:
            out[name] = cols[0]
    return out


def labelize_columns(cols: list[str], alias_map: dict[str, str] | None = None) -> list[str]:
    """整组列名批量翻译成**表头用**的完整形式（中文名 + 括号原字段名）。

    表头和导出文档的明细清单都走这里：列名与字段的对应关系要留在纸面上，
    读者拿着导出的 Word 去核对库表时才对得上号。
    图表轴 / KPI 卡那些放不下括号的位置改用 field_label_zh()。

    alias_map 来自 _sql_select_aliases()，给中文别名补底层字段名（工序名称 → process_name）。
    """
    am = alias_map or {}
    return [field_label(c, am.get(c)) for c in cols]


def _make_disp(cols: list[str], alias_map: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """{原始列名: {"full": "投入数量（input_qty）", "zh": "投入数量"}}。

    给正文/表头/图表共用一份映射。单独抽出来是因为报告里有四五处地方要显示列名
    （结论句、图表标题、KPI 卡、明细表头），各翻译各的就会出现同一份报告里
    同一个字段两种叫法的割裂感。

    为什么分 full / zh 两档：带括号的完整写法适合**表头和正文**（要能核对字段），
    但图表轴标签和 KPI 卡小字放不下「不良数量（defect_qty）」这种长度，
    硬塞会把图挤变形——那里用 zh 短名。

    alias_map 给中文别名补底层字段名：列名是「工序名称」时，光翻译拿不到括号内容，
    得靠 SQL 解析出的 process_name 补上。
    """
    am = alias_map or {}
    out: dict[str, dict[str, str]] = {}
    for c in (cols or []):
        zh = field_label_zh(c)
        full = field_label(c, am.get(c))
        out[c] = {"full": full, "zh": zh}
    return out


def _apply_disp(cols: list[str], disp: dict[str, str] | None) -> list[str]:
    """把一组原始列名换成显示名，顺序不变。"""
    if not disp:
        return list(cols)
    return [disp.get(c, c) for c in cols]


def collect_question_data(question: str, sql: str | None = None,
                          allowed_tables: set[str] | None = None,
                          row_filters: dict[str, str] | None = None,
                          max_rows: int = 200,
                          acl=None) -> dict:
    """围绕用户问题取数。优先复用已有 SQL，没有则让 LLM 现生成一条。

    为什么不复用现成 SQL 就完事：AskPage 里那次问答的 SQL 可能带 LIMIT 5 之类
    只够看一行的截断，做报告要拿全量趋势；也可能那条 SQL 是修正前的旧版。
    所以这里以「问题」为准重新取一次，SQL 只作为提示喂给生成器。

    2026-10-05 新增 acl 参数：接入列级权限（脱敏/列拒绝）。此前本函数只做行级，
    报告链路的列脱敏完全失效（实测 line_manager 明文外泄）。

    返回 {ok, sql, columns, rows, error, table, row_count}
    """
    from db.executor import execute_sql

    if not sql:
        sql = _gen_sql_for_question(question, allowed_tables)
    if not sql:
        return {"ok": False, "sql": "", "columns": [], "rows": [],
                "error": "没能为这个问题生成可执行的查询", "table": "", "row_count": 0}

    # ── 权限改写：列级（脱敏/拒绝）优先走 enforcer，与主问答链保持一致 ──
    # 顺序说明：列级必须在**行级包裹之前**做。行级包装会把原 SQL 塞进
    # `SELECT * FROM (原SQL) AS _acl_sub WHERE ...`，届时 enforcer 看到的
    # 是包装后的语句，列引用（supervisor / line_manager）已不在投影里，
    # 脱敏会静默失效。先列级后行级，enforcer 看到的仍是原始 SELECT。
    exec_sql, col_err = _apply_column_acl(sql, acl)
    if col_err:
        return {"ok": False, "sql": sql, "columns": [], "rows": [],
                "error": col_err, "table": _guess_table(sql), "row_count": 0}

    # 行级权限：把条件注入原语句内部 WHERE（_wrap_row_filter 内部已改为 AST 注入）
    rf = _pick_row_filter(sql, row_filters)
    if rf:
        wrapped = _wrap_row_filter(exec_sql, rf)
        if wrapped:
            exec_sql = wrapped
        else:
            # 注入不出来 → fail-close，绝不退回未过滤语句
            return {"ok": False, "sql": sql, "columns": [], "rows": [],
                    "error": "行级权限条件无法安全注入（已拒绝返回未收敛数据）",
                    "table": _guess_table(sql), "row_count": 0}

    result = execute_sql(exec_sql)
    # 2026-10-03 修复（P0）：原实现「包了权限过滤失败 → 退回裸 SQL（报告里标注口径）」，
    # 但实际**没有任何标注代码**（比 report_templates 那条还少一个标志位），
    # 用户在受限账号下会拿到全厂口径数据且毫无提示。行级权限是安全边界，
    # 不确定时必须选更严格的一侧：失败即拒绝出数，不退回未过滤语句。
    acl_degraded = False
    if not result.get("success") and rf:
        result = {"success": False, "columns": [], "rows": [],
                  "error": "行级权限过滤未生效（过滤字段不在结果列中），已拒绝返回未收敛数据"}
        acl_degraded = True

    # ── 执行失败就重新生成 SQL 再试（最多 2 轮）──
    # LLM 生成列名时会凭常识编（把 input_qty 写成 output_qty、把 workshop 编出来），
    # 库一执行就报 42703「字段不存在」。这种情况重试**很可能**就对了——
    # 每次生成是独立采样，实测同一个问题两次生成的选列并不一样。
    # 而且这一层比在下游重试划算：在取数这步拦住，生成和导出两条路都受益。
    for _round in range(2):
        if result.get("success"):
            break
        err = str(result.get("error") or "")
        if not re.search(r"42703|字段|column|does not exist|不存在", err, re.I):
            break   # 不是列名问题（比如超时/权限），重试没意义
        retry_sql = _gen_sql_for_question(question, allowed_tables)
        if not retry_sql or retry_sql == sql:
            continue
        sql = retry_sql
        exec_sql = _wrap_row_filter(sql, rf) if rf else sql
        result = execute_sql(exec_sql)
        # 同上：重试路径也不得退回未过滤语句
        if not result.get("success") and rf:
            acl_degraded = True
            result = {"success": False, "columns": [], "rows": [],
                      "error": "行级权限过滤未生效（过滤字段不在结果列中），已拒绝返回未收敛数据"}

    if not result.get("success"):
        return {"ok": False, "sql": sql, "columns": [], "rows": [],
                "error": str(result.get("error") or "查询失败")[:200],
                "acl_degraded": acl_degraded,
                "table": _guess_table(sql), "row_count": 0}

    cols, rows = _norm_rows(result)
    rows = rows[:max_rows]
    # 从 SQL 里解析出「中文别名 → 底层字段」，让「工序名称」这类列也能带上 process_name。
    # 放在这里而不是渲染层：SQL 只有这一层拿得到（main.py 回传时只带 columns）。
    aliases = _sql_select_aliases(sql)
    # display_columns 与 columns 一一对应：前者给人看（中文业务名），
    # 后者是行 dict 的真实键，不能动。下游展示统一取 display_columns。
    return {
        "ok": True, "sql": sql, "columns": cols,
        "display_columns": labelize_columns(cols, aliases),
        "aliases": aliases,
        "rows": rows, "error": "",
        "acl_degraded": acl_degraded,
        "table": _guess_table(sql), "row_count": len(rows),
    }


def _guess_table(sql: str) -> str:
    m = re.search(r"\bFROM\s+([\w\".]+)", sql or "", re.I)
    return m.group(1).strip('"') if m else ""


# ============================================================
# 列级权限（脱敏 / 拒绝）—— 2026-10-05 补上报告链路
# ============================================================
# 为什么必须在这里补：主问答链（llm_service._exec_sql）早就走了
# security.enforcer.rewrite_sql，列级权限是生效的；但报告/看板这条取数链
# 一直**只做行级**（_wrap_row_filter），列级完全没接。
#
# 实测铁证（yans 库，同一句 SQL）：
#   SQL: SELECT line_name, line_manager FROM dim_production_line
#   主链路   → line_manager = '主****1'   （已脱敏）
#   报告链路 → line_manager = '主管1'     （明文泄漏）
# 而 dim_production_line.line_manager 正是 viewer 角色配置的脱敏列。

def _apply_column_acl(sql: str, acl) -> tuple[str, str]:
    """对报告链路要执行的 SQL 应用列级权限。

    返回 (改写后的 SQL, 错误信息)。出错时错误信息非空，调用方必须拒绝执行
    （fail-close）——**绝不能**退回未改写的 SQL，那等于绕过列级权限。
    """
    if acl is None or not getattr(acl, "needs_sql_rewrite", lambda: False)():
        return sql, ""
    try:
        from security.enforcer import rewrite_sql as _acl_rewrite
        eff, acl_err, _applied = _acl_rewrite(sql, acl)
        if acl_err:
            return "", f"列级权限校验未通过，已拒绝出数：{acl_err}"
        return eff, ""
    except Exception as e:
        return "", f"列级权限处理异常，已拒绝出数：{e}"


def _mask_rows_by_acl(rows: list[dict], columns: list[str], acl) -> list[dict]:
    """兜底：万一 SQL 改写不可用，至少在结果层把被 deny 的列整列去掉。

    这**不能**替代 SQL 层脱敏（原始值已经进过内存/日志），只是防止
    「deny 列直接出现在报告里」这种最直白的泄漏。mask 列在结果层无法还原
    真实值，所以不做处理——宁可少显示。
    """
    if acl is None or not rows:
        return rows
    denies = getattr(acl, "column_denies", None) or {}
    if not denies:
        return rows
    # 只有单表结果才能确定列归属；多表时不猜，避免误删
    if len(denies) != 1:
        return rows
    (_tbl, cols) = next(iter(denies.items()))
    if isinstance(cols, dict):
        drop = set(cols.keys())
    elif isinstance(cols, (set, list, tuple)):
        drop = set(cols)
    else:
        drop = set()
    if not drop:
        return rows
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({k: v for k, v in r.items() if k not in drop})
    return out


def _pick_row_filter(sql: str, row_filters: dict[str, str] | None) -> str:
    """挑出适用于这条 SQL 的行级过滤条件（按 SQL 里出现的表名匹配）。"""
    if not row_filters:
        return ""
    sql_low = (sql or "").lower()
    for tname, cond in row_filters.items():
        bare = tname.split(".")[-1].lower()
        if bare and bare in sql_low:
            return cond or ""
    return ""


def _inject_where_with_ast(sql: str, cond: str) -> str:
    """用 sqlglot 把行级条件并进原语句的 WHERE（正解）。

    行级条件本身常含子查询，例如权限配置里典型的一句：
        factory_id IN (SELECT factory_id FROM test_factories WHERE city = '华东')
    这种条件**必须注入内层 WHERE** —— 套到外层会因为「子查询的列不在外层投影里」
    而报 42703（实测 `line_id IN (SELECT line_id FROM mes_process_output)` 即如此）。

    正则无法可靠定位「顶层 WHERE」（嵌套子查询里也有 WHERE、GROUP BY 会被
    HAVING 干扰），所以这里交给 sqlglot 解析成 AST 再拼回去。
    解析失败返回空串，由调用方 fail-close。
    """
    try:
        import sqlglot
        from sqlglot import exp as sqlglot_exp
    except Exception:
        return ""

    # ── 解析必须带 read='postgres'（2026-10-05 修复）──
    # 不带 read 时 sqlglot 按默认方言解析，PostgreSQL 的 `100.0` 会被改写成
    # `CAST(100.0 AS DOUBLE PRECISION)`，而 PG 的 round(double, int) 不存在
    # （报 42883 函数 round(double precision, integer) 不存在），
    # 导致每个带 ROUND 的报告章节一加行级权限就崩。
    # 实测：`parse_one(sql, read='postgres').sql(dialect='postgres')` 输出与原文一致。
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return ""
    if tree is None:
        return ""
    if isinstance(tree, sqlglot_exp.Subquery):
        tree = tree.this
    if not isinstance(tree, sqlglot_exp.Select):
        return ""

    # 先把条件文本解析成表达式（注入时要用）
    try:
        cond_tree = sqlglot.parse_one("SELECT 1 WHERE " + cond, read="postgres")
    except Exception:
        return ""
    if cond_tree is None or cond_tree.args.get("where") is None:
        return ""

    # WITH 子句：sqlglot 的参数键是 `with_`（不是 `with`，后者是 Python 关键字）。
    # 行级条件若直接注到外层主查询会失败——外层只 SELECT 了 CTE 的输出列，
    # 而过滤列（如 line_manager）定义在 CTE **内部**的表上。
    # 实测：`WITH t AS (SELECT line_id,line_name,line_manager FROM dim_production_line)
    #        SELECT line_name FROM t` + 外层 WHERE line_manager IS NOT NULL
    #        → 42703 字段 "line_manager" 不存在
    # 正确做法是把条件下推到**第一个引用了该表的那个 CTE 内部**。
    with_clause = tree.args.get("with_") or tree.args.get("with")
    if with_clause is not None and with_clause.expressions:
        cte = with_clause.expressions[0]
        cte_sel = cte.this if hasattr(cte, "this") else None
        if isinstance(cte_sel, sqlglot_exp.Select):
            inner = _ast_inject_where(cte_sel, cond_tree)
            if inner is None:
                return ""
            cte.set("this", inner)
            try:
                return tree.sql(dialect="postgres")
            except Exception:
                return ""

    out = _ast_inject_where(tree, cond_tree)
    if out is None:
        return ""
    try:
        return out.sql(dialect="postgres")
    except Exception:
        return ""


def _ast_inject_where(sel, cond_tree):
    """把条件 AND 进给定 Select 的 WHERE；已有 WHERE 时两侧都加括号。"""
    from sqlglot import exp as sqlglot_exp
    where = cond_tree.args.get("where")
    if where is None:
        return None
    cond_expr = where.this
    existing = sel.args.get("where")
    if existing is not None:
        merged = sqlglot_exp.And(
            this=sqlglot_exp.Paren(this=existing.this.copy()),
            expression=sqlglot_exp.Paren(this=cond_expr.copy()),
        )
    else:
        merged = sqlglot_exp.Paren(this=cond_expr.copy())
    new_sel = sel.copy()
    new_sel.set("where", sqlglot_exp.Where(this=merged))
    return new_sel


def _wrap_row_filter(sql: str, cond: str) -> str:
    """把行级条件注入到 SQL 内部的 WHERE 里（而不是套一层外层子查询）。

    ## 为什么不能套外层（2026-10-05 修复 P0）
    原实现是 `SELECT * FROM (<原SQL>) AS _acl_sub WHERE <cond>`。
    但原 SQL 往往是**聚合查询**，子查询只投影「分组列 + 聚合列」，
    而行过滤列（如 line_manager / shift_code）**不在投影里** ——
    外层 WHERE 引用它必然报 `42703 column does not exist`。

    实测对照（yans 库，8 条真实 SQL）：
        旧实现（外层包裹）  : 8 / 8 全部失败
        新实现（注入内层）  : 8 / 8 全部成功，且过滤结果正确
    旧实现会让报告的每个章节都取数失败，有行级权限的用户完全拿不到报告。

    实现：优先用 sqlglot AST 精确注入（能处理条件含子查询的情形）；
    AST 不可用时退回保守的正则方案；都不行则返回空串让调用方 fail-close，
    **绝不返回未过滤的原 SQL**（那等于绕过行级权限）。
    """
    s = (sql or "").strip().rstrip(";").strip()
    cond_s = (cond or "").strip()
    if not s or not cond_s:
        return ""
    if not re.match(r"^(select|with)\b", s, re.I):
        return ""

    # 正解：AST 注入
    injected = _inject_where_with_ast(s, cond_s)
    if injected:
        return injected

    # 兜底：AST 不可用时的保守正则方案（仅处理不含子查询的条件）
    if re.search(r"\bselect\b", cond_s, re.I):
        return ""
    wrapped = f"({cond_s})"
    if re.search(r"\bwhere\b", s, re.I):
        m = re.search(r"\bwhere\b", s, re.I)
        head, tail = s[:m.end()], s[m.end():]
        if re.search(r"\bor\b", tail, re.I) and not re.match(r"\s*\(", tail):
            tail = f" ({tail.strip()})"
        return f"{head} {tail.rstrip()} AND {wrapped}"
    for kw in (r"\blimit\b", r"\boffset\b", r"\bgroup\s+by\b",
               r"\border\s+by\b", r"\bhaving\b", r"\bfor\s+update\b"):
        mm = list(re.finditer(kw, s, re.I))
        if mm:
            m2 = mm[0]
            return f"{s[:m2.start()]} WHERE {wrapped} {s[m2.start():]}"
    return f"{s} WHERE {wrapped}"


def _gen_sql_for_question(question: str, allowed_tables: set[str] | None) -> str:
    """让 LLM 针对问题生成一条只读 SQL。

    两处必须踩过的坑，写在这里免得重复踩：

    1) allowed_tables 的语义（跟 acl.py 一致，容易搞反）：
         None  = 不限制，全部表可见（不是"全都不可见"）
         set() = 一个都不给（真正空权限）
         非空集 = 白名单，只给集合内的表
       第一版写成 `if name not in {x.lower() for x in allowed_tables}: continue`，
       传全表白名单进去时每张表都被 continue 掉、tables 为空 → 返回 ""，
       最后报成"没能为这个问题生成可执行的查询"。定位靠绕过函数逐句复现。

    2) **光给表名不够，必须给字段。**
       第一版只列 `- mes_process_output（工序产量表）`，LLM 就凭"产量表该有个产量列"
       编出 `SUM(o.output_qty)`——这张表根本没有 output_qty，只有 input_qty /
       good_qty / defect_qty，SQL 执行报 42703「字段不存在」，报告在取数这步就死了。
       真实字段在 `db.metadata.find_table_by_name()` 里（fields[].name / description /
       type / sample），全注进 prompt 后 LLM 才有依据选列。
    """
    try:
        from db.tools import get_real_tables
        from db.metadata import find_table_by_name
        from agent.llm_service import _make_llm
        from langchain_core.messages import HumanMessage, SystemMessage

        # None = 不限；否则按白名单过滤（空集 -> 没有可用表）
        allow_lower: set[str] | None = None
        if allowed_tables is not None:
            allow_lower = {str(x).split(".")[-1].lower() for x in allowed_tables}

        blocks: list[str] = []
        real = get_real_tables()
        for t in real:
            name = t["table_name"]
            alias = t.get("table_alias") or ""
            bare = str(name).split(".")[-1].lower()
            if allow_lower is not None and bare not in allow_lower:
                continue
            head = f"- {name}" + (f"（{alias}）" if alias else "")
            # 字段清单：名 + 类型 + 中文说明 + 样例值，让 LLM 选列有依据
            detail = None
            try:
                detail = find_table_by_name(name)
            except Exception:
                detail = None
            flds = (detail or {}).get("fields") or []
            if flds:
                lines = [head]
                for f in flds[:30]:
                    fn = f.get("name") or ""
                    if not fn:
                        continue
                    ft = f.get("type") or ""
                    fd = f.get("description") or ""
                    fk = f.get("key") or ""
                    fs = f.get("sample")
                    tail = "／".join(x for x in (ft, fd, ("FK" if fk == "FK" else "")) if x)
                    if fs not in (None, ""):
                        tail += f"｜例:{fs}"
                    lines.append(f"    {fn}  {tail}")
                blocks.append("\n".join(lines))
            else:
                blocks.append(head + "    （字段未登记）")
            if len(blocks) >= 14:
                break
        if not blocks:
            return ""
        schema = "\n".join(blocks)

        sys_prompt = (
            "你是 SQL 生成器。根据用户的业务问题，写一条只读的 PostgreSQL SELECT 查询。\n"
            "硬约束：\n"
            "1. 只能用下面列出的表，且**只能用列出的字段**——表里没有的列名不要凭常识编，"
            "库里叫什么就用什么（这是最常见的出错点）；\n"
            "2. 只允许 SELECT，禁止 INSERT/UPDATE/DELETE/DDL；\n"
            "3. 结果要适合做图表：第一列是分组维度（如产线、工序、类别、日期），"
            "后面至少有一个数值列（占比、数量、均值等）；\n"
            "4. 分组行数控制在 3-20 行，用 ORDER BY 排序，不要写 LIMIT 1；\n"
            "5. 比率类字段统一输出成百分数（如 97.56 表示 97.56%）；\n"
            "6. 表之间靠 *_id 字段关联（如 line_id 关联产线表），需要显示名称时先 JOIN 维表；\n"
            "7. 只输出 SQL 本身，不要解释、不要 markdown 代码块标记。\n\n"
            f"可用表与字段：\n{schema}"
        )
        msgs = [SystemMessage(content=sys_prompt),
                HumanMessage(content=f"业务问题：{question}")]

        # ── 重试是必需的，不是保险丝 ──
        # deepseek-v4.1-flash 在这个 prompt 长度下会**静默返回空内容**：
        # 不抛异常、不动 tool_call，content 就是空串。A/B 实测（同一 prompt 连打 6 次）
        # 空输出 2/6 ~ 5/6，且与 prompt 里有没有 ** 强调无关——所以调 prompt 治不了。
        # 项目里 llm_service 的注释也记着这个现象（"长 prompt 实测静默空响应"），
        # 那边靠 wall-clock 预算 + 重试兜住。这里同样：空手就重打，最多 3 次。
        import time as _t
        for attempt in range(3):
            try:
                resp = _make_llm(temp=0.0, max_tokens=700).invoke(msgs)
            except Exception as e:
                import logging
                logging.getLogger("question_report").warning(
                    "SQL 生成第 %d 次调用异常: %s", attempt + 1, e)
                _t.sleep(0.8)
                continue
            text = str(getattr(resp, "content", "") or "")
            if text.strip():
                break
            import logging
            logging.getLogger("question_report").warning(
                "SQL 生成第 %d 次返回空内容，重试", attempt + 1)
            _t.sleep(0.8)
        else:
            return ""

        text = re.sub(r"```(?:sql)?", "", text).replace("```", "").strip()
        m = re.search(r"(select|with)\b.*", text, re.I | re.S)
        sql = m.group(0).strip() if m else ""
        sql = sql.rstrip(";").strip()
        # 兜底：只放行只读语句
        if not re.match(r"^(select|with)\b", sql, re.I):
            return ""
        if re.search(r"\b(insert|update|delete|drop|alter|truncate|create|grant)\b", sql, re.I):
            return ""
        return sql
    except Exception as e:
        # 不静默吞：这条链路一旦失败，用户看到的是"换个问法试试"，
        # 排查时只能靠日志回溯（原先 `except Exception: return ""` 把原因全埋了）。
        import logging
        logging.getLogger("question_report").warning("SQL 生成失败: %s", e, exc_info=True)
        return ""


# ════════════════════════════════════════════════════════════
# 2. 分析文字
#
# ★ 这一节为什么长这样：被模型的静默空输出逼的 ★
#
# 原设计是"把数据丢给 LLM，让它按四个章节写一篇分析"。实测在
# deepseek-v4.1-flash 上**基本跑不通**——同一份数据、同一个 prompt，
# 返回空内容的概率高得离谱，而且不抛异常、耗时正常，像"回完了但没写字"。
# 穷举过的自救手段与其结果（每组 3-4 次）：
#     长 prompt（8 条约束 + --- 分隔符）   成功 0-1 / 3
#     精简 prompt（4 段 + 【标题】）        成功 1 / 3
#     最短 prompt（只说写什么）             成功 0 / 3
#     提高温度重试 / 换非流式通道           全部仍空
#     "给 3 条建议，每条一行"               成功 0 / 4
#     极简上下文 / 去掉 SystemMessage        成功 0-1 / 3
#     要求"一段话点评，≤150 字"             成功 3 / 4  ← 只有这个稳
#
# 结论：这个模型**只能可靠地完成"写一段短评"**，一旦要求多段结构、
# 分点罗列、长文输出就大面积静默空回。所以架构反过来：
#   · 结论、发现、原因、建议 —— 全部由 Python 从数据里**确定性地算**出来。
#     好处不只是稳：数字不会有幻觉，计算过程可复现，评委问"这个结论怎么来的"
#     能直接指到代码行。
#   · LLM 只负责"把算出来的发现串成一段人话"（可选润色）。它挂了就退回
#     规则生成的文字，报告照样完整——分析质量不再取决于模型当天的心情。
# ════════════════════════════════════════════════════════════


def _describe_shape(cols: list[str], rows: list[dict],
                    alias_map: dict[str, str] | None = None) -> dict:
    """先摸清这份数据长什么样，后面的结论生成全按这个形状分派。

    返回 {kind, dim, measure, measures, items, disp}：
      kind = trend（时间序列）| rank（分组排名）| single（单个汇总值）| empty

    路径敏感字段一律用**原始列名**（它们同时是 rows 里取值的键）；
    仅供人看的显示名全部走 `disp`（每项是 {"full","zh"} 两个形式）。
    别再往这里塞翻译过的名字——一旦 dim 变成「工序」，`r.get(dim)` 立刻取不到值，
    整张表会集体变空。

    alias_map 是 SQL 里解析出的「中文别名 → 底层字段」，用来给中文列名补括号。
    """
    disp = _make_disp(cols, alias_map)

    def d(c: str, form: str = "full") -> str:
        """取显示名。form='full' 带括号字段名（正文/表头），'zh' 只中文（图表/卡片）。"""
        if not c:
            return c
        item = disp.get(c)
        if not item:
            return c
        return item.get(form) or item.get("full") or c

    if not rows or not cols:
        return {"kind": "empty", "dim": None, "measure": None, "measures": [],
                "items": [], "disp": disp}

    # 单行结果没有"分组"可言，整行都是指标——先单独处理。
    # 坑：下面通用的 measures 计算会把 dim 排除掉，而单列结果里 dim 就是那个值本身，
    # 排除完 measures 变空，结论句就成了"本次查询返回单个汇总值："后面什么都没有。
    if len(rows) == 1:
        r0 = rows[0]
        measures = [c for c in cols if _looks_like_number(r0.get(c))]
        measures = measures or list(cols)
        return {"kind": "single", "dim": cols[0], "measures": measures,
                "measure": measures[0], "items": [], "disp": disp}

    dim, measure = _pick_dim_measure(cols, rows)
    measures = [c for c in cols
                if c != dim and any(_looks_like_number(r.get(c)) for r in rows[:20])]
    is_time = bool(dim and re.search(
        r"(date|time|day|month|year|周|月|日|年|季度|期间)", dim, re.I))
    items = ([(str(r.get(dim)), _to_float(r.get(measure))) for r in rows]
             if (dim and measure) else [])
    items = [(k, v) for k, v in items if k and k != "None"]
    if is_time and len(items) >= 4:
        return {"kind": "trend", "dim": dim, "measure": measure,
                "measures": measures, "items": items, "disp": disp}
    return {"kind": "rank", "dim": dim, "measure": measure,
            "measures": measures, "items": items, "disp": disp}


def _is_pct(name: str) -> bool:
    return any(k in (name or "").lower()
               for k in ("rate", "ratio", "pct", "percent", "率", "比例", "占比"))


def _unit_of(name: str) -> str:
    return "%" if _is_pct(name) else ""


def build_findings(question: str, cols: list[str], rows: list[dict],
                   alias_map: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """**确定性**生成分析章节——不经过 LLM。

    这是整个报告专业度的来源：每个数字都是 Python 现算的，每个判断都有明确判据
    （极差是多少、趋势是升还是降、有几组高于均值），不是模型"看着差不多"编的形容词。

    返回 [("核心结论", "..."), ("关键发现", "..."), ...]
    """
    shape = _describe_shape(cols, rows, alias_map)
    kind, dim = shape["kind"], shape["dim"]
    measures, items = shape["measures"], shape["items"]
    # 图表/结论讲的那个指标，必须和 items 里真正取值的列是同一个。
    # 之前这里取 measures[0]，而 items 用的是 _pick_dim_measure 选中的列，
    # 两者可能不同 → 数字是良率、标签却写"投入数量"。
    main_meas = shape.get("measure") or (measures[0] if measures else "")

    # 正文里出现的列名换成中文业务名，**首次出现**带括号原字段名，之后只用短名。
    #
    # 为什么不一概用带括号的形式：实测一屏正文里「良率（yield_rate_pct）」出现了六次，
    # 读起来像结巴——括号只在"第一次告诉读者这列对应哪个字段"时有信息量，
    # 后面每一次重复都是噪音。但也不能只给短名：那就丢掉了核对字段的回溯路径。
    #
    # dim / meas 这两个**只用于拼句子**，不参与 r.get() 取值
    # （取值已在 _describe_shape 里用原始名做完了），所以在这里放心替换。
    disp = shape.get("disp") or {}
    _seen: set[str] = set()

    def dn(c: str | None) -> str:
        """取正文用显示名。同一列第二次起返回短名。"""
        if not c:
            return ""
        item = disp.get(c)
        if not item:
            return c
        if c in _seen:
            return item.get("zh") or item.get("full") or c
        _seen.add(c)
        return item.get("full") or item.get("zh") or c

    # 不要在外面把 dim/meas 的名字预先取好存成变量 —— dn() 是有状态的，
    # 提前调用会把"第一次"的额度用掉，正文里就再也拿不到带括号的形式了。
    # 统一在各拼接处现调 dn()，顺序即出现顺序。

    if kind == "empty":
        return [("核心结论", "本次查询没有返回数据行，无法形成分析结论。")]

    out: list[tuple[str, str]] = []

    # ── 单个汇总值：只讲这个数是什么、处在什么水平 ──
    if kind == "single":
        r0 = rows[0]
        parts = [f"{dn(m)} {_fmt_num(r0.get(m))}{_unit_of(m)}"
                 for m in measures[:3] if r0.get(m) is not None]
        if not parts:
            parts = [f"{dn(c)} {_fmt_num(r0.get(c))}" for c in cols[:3] if r0.get(c) is not None]
        out.append(("核心结论", (
            "本次查询返回单个汇总值：" + "、".join(parts) + "。"
            "这是全口径合并后的一个数，里面没有分组、也没有时间先后，"
            "所以只能回答「整体是多少」，回答不了「谁高谁低、有没有变差」。"
        )))
        out.append(("关键发现", (
            f"一个汇总值要把内部的差异全部抹掉。比如同样是平均良率 97.5%，"
            f"可能是所有产线都在 97.5% 附近，也可能是几条线 99%、几条线 95% 拉平的结果，"
            f"这两种情况该采取的动作完全不同。要分辨，得把「{dn(dim) or '关键维度'}」拆开再看一遍。"
        )))
        out.append(("行动建议", (
            f"换个粒度再问一次：按产线、按工序或按月分开统计，"
            f"把现在这一个数展开成分组结果，才看得出问题藏在哪一段。实施难度：低。"
        )))
        return out

    # ── 排名型：找出最高/最低、算差距、给业务解读 ──
    if kind == "rank" and items:
        meas = main_meas
        unit = _unit_of(meas)
        is_pct = _is_pct(meas)
        vals = [v for _, v in items]
        hi = max(items, key=lambda x: x[1])
        lo = min(items, key=lambda x: x[1])
        avg = sum(vals) / len(vals)
        spread = hi[1] - lo[1]
        # 相对差距只在"绝对量"上说得通。良率这种比率型指标，
        # 极差 0.88 个百分点说成"占平均值的 0.9%"是句废话——0.88/97.71 本来就这样。
        # 比率型要看的是百分点差：0.88 个百分点，这个量级在良率上已经很值得查了。
        rel = (spread / avg * 100) if (avg and not is_pct) else 0
        gap_txt = (f"，相差 {spread:.2f} 个百分点" if is_pct
                   else (f"，极差 {_fmt_num(round(spread, 3))}{unit}"
                         + (f"，相当于平均水平的 {rel:.1f}%" if avg else "")))

        out.append(("核心结论", (
            f"本次按「{dn(dim)}」共比较 {len(items)} 组，{dn(meas)} 平均 {_fmt_num(round(avg, 3))}{unit}。"
            f"最高是 {hi[0]}（{_fmt_num(hi[1])}{unit}），最低是 {lo[0]}（{_fmt_num(lo[1])}{unit}）"
            + gap_txt + "。"
            + (f"最需要关注的是 {lo[0]}，它是这组里表现最弱的一环。"
               if spread else "各组数值基本持平。")
        )))
        f1 = (f"{hi[0]} 表现最好：{dn(meas)} {_fmt_num(hi[1])}{unit}，"
              f"高出平均值 {_fmt_num(round(hi[1] - avg, 3))}{unit}。")
        f2 = (f"{lo[0]} 是明显短板：{dn(meas)} {_fmt_num(lo[1])}{unit}，"
              f"低于平均值 {_fmt_num(round(avg - lo[1], 3))}{unit}")
        f2 += (f"，与最高的 {hi[0]} 差 {_fmt_num(round(spread, 3))}{unit}。" if spread else "。")
        above = sum(1 for v in vals if v > avg)
        f3 = (f"{len(items)} 组中 {above} 组高于平均、{len(items) - above} 组低于平均，"
              + ("分布相对均衡。" if abs(above - (len(items) - above)) <= 1
                 else "多数组集中在平均线以下，说明整体被少数高值拉高。"))
        out.append(("关键发现", "\n".join([f1, f2, f3])))

        out.append(("原因分析", (
            (f"数据能确认的是差异确实存在（最高与最低相差 {spread:.2f} 个百分点）。"
             if is_pct else
             f"数据能确认的是差异确实存在（极差 {_fmt_num(round(spread, 3))}{unit}，"
             f"占平均值 {rel:.1f}%）。")
            + f"但差异由什么造成——设备状态、工艺参数、"
            f"来料批次还是班次——当前查询只覆盖「{dn(dim)}」与「{dn(meas)}」，不足以定位成因，"
            f"需要结合停机记录、缺陷明细等表继续下钻。"
        )))

        out.append(("行动建议", "\n".join([
            f"优先排查 {lo[0]}：它的 {dn(meas)} 是 {len(items)} 组里最低的，"
            f"先把该组设备参数和作业条件与最高组逐项比对，目标是把差距收窄一半。实施难度：中。",
            f"把 {hi[0]} 的做法固化：它在同口径下表现最好，"
            f"把该组的关键参数整理成标准作业值再推广。实施难度：低。",
            f"给「{dn(dim)} × {dn(meas)}」建日常监控：对低于平均线的 {len(items) - above} 组设阈值提醒，"
            f"不让偏差积累到月底才发现。实施难度：低。",
        ])))
        return out

    # ── 趋势型：算首尾变化、方向、波动 ──
    if kind == "trend" and items:
        meas = main_meas
        unit = _unit_of(meas)
        first, last = items[0], items[-1]
        vals = [v for _, v in items]
        hi = max(items, key=lambda x: x[1])
        lo = min(items, key=lambda x: x[1])
        delta = last[1] - first[1]
        pct = (delta / first[1] * 100) if first[1] else 0
        avg = sum(vals) / len(vals)
        direction = "上升" if delta > 0 else ("下降" if delta < 0 else "基本持平")

        out.append(("核心结论", (
            f"{dn(meas)} 从 {first[0]} 的 {_fmt_num(first[1])}{unit} 变到 {last[0]} 的 "
            f"{_fmt_num(last[1])}{unit}，区间内整体{direction}"
            + (f"，净变化 {_fmt_num(round(delta, 3))}{unit}（{pct:+.1f}%）。" if delta else "，没有净变化。")
            + f"区间最高 {hi[0]}（{_fmt_num(hi[1])}{unit}），最低 {lo[0]}（{_fmt_num(lo[1])}{unit}）。"
        )))
        out.append(("关键发现", "\n".join([
            f"起点 {first[0]}：{_fmt_num(first[1])}{unit}；终点 {last[0]}：{_fmt_num(last[1])}{unit}；"
            f"净变化 {_fmt_num(round(delta, 3))}{unit}。",
            f"峰值 {hi[0]} 达到 {_fmt_num(hi[1])}{unit}，比区间均值 "
            f"{_fmt_num(round(avg, 3))}{unit} 高 {_fmt_num(round(hi[1] - avg, 3))}{unit}。",
            f"谷值 {lo[0]} 只有 {_fmt_num(lo[1])}{unit}，比区间均值低 "
            f"{_fmt_num(round(avg - lo[1], 3))}{unit}，是这串序列里最需要解释的点。",
        ])))
        out.append(("原因分析", (
            f"趋势方向是确定的（{direction} {abs(pct):.1f}%），但变化原因从单一指标上看不出来。"
            f"单看 {dn(meas)} 的时间序列只能回答「什么时候变了」，回答不了「为什么变」，"
            f"需要对齐同期的产量、停机、换型记录再判断。"
        )))
        out.append(("行动建议", "\n".join([
            f"核查 {lo[0]} 前后发生了什么：该时点 {dn(meas)} 落到区间最低（{_fmt_num(lo[1])}{unit}），"
            f"把当时的排产、设备、来料情况调出来比对。实施难度：中。",
            ("把上行趋势固化：当前做法有效，整理成标准动作避免回落。"
             if delta > 0 else
             "先止跌再优化：定位是单点异常还是持续性恶化，再决定是否调整排产节奏。")
            + "实施难度：低。",
            f"给 {dn(meas)} 建连续监控：以区间均值 {_fmt_num(round(avg, 3))}{unit} 为基准线，"
            f"偏离超过 {_fmt_num(round(max(abs(avg) * 0.02, 0.5), 2))}{unit} 触发提醒。实施难度：低。",
        ])))
        return out

    # ── 兜底：形状不典型，至少把事实摆清楚 ──
    out.append(("核心结论", (
        f"本次查询返回 {len(rows)} 行、{len(cols)} 列，"
        f"分组维度「{dn(dim) or '未识别'}」，数值指标"
        f"{'、'.join(dn(m) for m in measures[:3]) if measures else '未识别'}。"
    )))
    if measures and items:
        vals = [v for _, v in items]
        out.append(("关键发现", (
            f"{dn(measures[0])} 取值区间 {_fmt_num(min(vals))} 至 {_fmt_num(max(vals))}，"
            f"均值 {_fmt_num(round(sum(vals) / len(vals), 3))}。"
        )))
    out.append(("原因分析", "当前数据结构不足以支撑进一步的因果分析。"))
    out.append(("行动建议", "补充关键维度字段，或换一个更具体的问法后重新生成报告。实施难度：低。"))
    return out


# 让模型只做一件事：替读者判断"这组数字意味着什么"。
# 之所以限得这么死，见本节开头那段实测记录——请求一旦变"大"，它就静默空回。
#
# 明确禁止复述数字：第一版让它"点评数据、引用具体数字"，结果它把最高/最低/均值
# 又抄了一遍，紧接着确定性结论也报同样的数，同一段里说两遍，读起来像结巴。
# 现在分工是：**数字由下文给，它只给判断**。
_REVIEW_SYS = (
    "你是资深制造质量分析师。下面给出一个分组的指标数据。"
    "用一句话（60-110字）说出这组数据的业务含义：这个差异说明现场可能是什么状态、"
    "最该注意的是哪一类问题。"
    "**不要重复下面的最高/最低/均值数字**——那些数字报告里已经有了。"
    "不要用 markdown，不要分点，只写一段话。"
)


def build_data_context(question: str, cols: list[str], rows: list[dict],
                      table: str = "", sql: str = "") -> str:
    """把问题 + 结果整理成给 LLM 看的**简短**上下文。

    刻意做短：实测长上下文会显著抬高静默空输出概率。既然 LLM 只写一段短评，
    就只喂它短评需要的关键数字，而不是整张表。
    """
    # 上下文只用 zh 短名，不带括号字段名——它越短，模型静默空回的概率越低，
    # 所以这里不传 alias_map（不再需要补 process_name）。
    shape = _describe_shape(cols, rows)
    kind, dim = shape["kind"], shape["dim"]
    measures, items = shape["measures"], shape["items"]
    main_meas = shape.get("measure") or (measures[0] if measures else "")
    # 喂给模型的上下文也用中文列名：它写的是给业务人员看的短评，
    # 上下文里出现 yield_rate_pct，它就会跟着在正文里写英文列名。
    # 这里用 zh 短名（不带括号）——上下文越短，模型静默空回的概率越低。
    disp = shape.get("disp") or {}

    def dn(c: str | None) -> str:
        if not c:
            return ""
        return (disp.get(c) or {}).get("zh") or c

    lines = [f"问题：{question}"]
    if kind == "empty":
        return "\n".join(lines + ["查询没有返回数据。"])
    if kind == "single":
        r0 = rows[0]
        lines.append("结果：" + "；".join(
            f"{dn(m)}={_fmt_num(r0.get(m))}{_unit_of(m)}" for m in measures[:3]))
        return "\n".join(lines)

    meas = main_meas
    unit = _unit_of(meas)
    vals = [v for _, v in items] or [0]
    hi = max(items, key=lambda x: x[1]) if items else ("", 0)
    lo = min(items, key=lambda x: x[1]) if items else ("", 0)
    avg = sum(vals) / len(vals)
    lines += [
        f"数据：按「{dn(dim)}」比较「{dn(meas)}」（单位{unit or '无'}），共 {len(items)} 组",
        "明细：" + "；".join(f"{k}={_fmt_num(v)}{unit}" for k, v in items[:12]),
        f"统计：均值 {_fmt_num(round(avg, 3))}{unit}；"
        f"最高 {hi[0]} {_fmt_num(hi[1])}{unit}；最低 {lo[0]} {_fmt_num(lo[1])}{unit}",
    ]
    return "\n".join(lines)


async def _llm_one_paragraph(question: str, cols: list[str], rows: list[dict]) -> str:
    """只要一句点评。换温度重试，跑不出来返回空串（调用方容忍）。"""
    from agent.llm_service import _make_llm
    from langchain_core.messages import HumanMessage, SystemMessage
    import logging
    _log = logging.getLogger("question_report")

    ctx = build_data_context(question, cols, rows)
    for temp in (0.3, 0.6, 0.45):
        try:
            resp = _make_llm(temp=temp, max_tokens=600).invoke(
                [SystemMessage(content=_REVIEW_SYS), HumanMessage(content=ctx)])
            text = str(getattr(resp, "content", "") or "").strip()
            if text:
                return text
            _log.warning("LLM 点评 temp=%.2f 返回空，重试", temp)
        except Exception as e:
            _log.warning("LLM 点评 temp=%.2f 异常: %s", temp, e)
    _log.info("LLM 点评三次均无输出，用确定性结论（不影响报告完整性）")
    return ""


async def analyze_stream(question: str, cols: list[str], rows: list[dict],
                         table: str = "", sql: str = "") -> AsyncIterator[str]:
    """产出分析正文。

    **主体确定性**：结论/发现/原因/建议全部由 `build_findings` 现算。
    LLM 只给"核心结论"补一句人话点评——补上更好，补不上照样出报告。

    这么安排的原因见本节开头实测记录：模型在这个任务上大面积静默空回，
    把报告完整性押在它身上不划算。确定性生成保证"哪怕模型全挂，
    用户拿到的仍是一份数字准确、结构完整的专业报告"。
    """
    # 中文别名（SELECT p.process_name AS 工序名称）的底层字段得从 SQL 里解析出来，
    # 否则正文里的「工序名称」永远带不上 process_name 这个核对线索。
    # 这里自己解析而不加参数：sql 本来就是这个函数的入参，多传一份 alias_map
    # 等于让每个调用方都记得先解析一遍，漏传就静默退回无括号。
    findings = build_findings(question, cols, rows, _sql_select_aliases(sql))
    if not findings:
        findings = [("核心结论", "本次查询没有返回可用于分析的数据。")]

    # 可选：让 LLM 加一段业务解读，插在"核心结论"**之后**（失败不影响出报告）。
    #
    # 位置很讲究：第一版是拼在核心结论前面，结果它和后面的确定性结论各说了一遍
    # 同样的最高/最低/均值——同一张卡片里两段话讲同一件事，读起来像结巴。
    # 现在拆成独立一段，并在 prompt 里禁止复述数字：**事实由系统给，判断由模型给**，
    # 两段各司其职，不再打架。
    try:
        note = await _llm_one_paragraph(question, cols, rows)
        if note:
            name, body = findings[0]
            findings[0] = (name, body + "\n" + note.strip())
    except Exception:
        pass

    for name, body in findings:
        # 按【章节名】吐，与 split_sections 的格式①对齐
        yield f"【{name}】{body}\n"


def analyze_sync(question: str, cols: list[str], rows: list[dict],
                 table: str = "", sql: str = "") -> str:
    """同步版分析（供非 async 调用点使用，如导出接口）。"""
    async def _run() -> str:
        buf: list[str] = []
        async for t in analyze_stream(question, cols, rows, table, sql):
            buf.append(t)
        return "".join(buf)

    try:
        loop = asyncio.get_running_loop()
        running = loop.is_running()
    except RuntimeError:
        running = False
    if running:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(lambda: asyncio.run(_run())).result()
    return asyncio.run(_run())


def split_sections(text: str) -> list[tuple[str, str]]:
    """把分析文本切成 [(章节名, 正文)]。

    两种格式都要认，因为模型两种都可能吐：
      ① 【核心结论】正文……      —— 现在主用（精简 prompt 指定这个）
      ② ---核心结论--- 正文……  —— 早期格式，保留兼容
    只认一种的话，模型换个写法整篇分析就变成"未分段"，前半段还会丢掉。
    """
    t = text or ""
    out: list[tuple[str, str]] = []

    # 格式①：【章节名】
    marks = list(re.finditer(r"【\s*([^】\n]{2,16}?)\s*】", t))
    if marks:
        for i, m in enumerate(marks):
            name = m.group(1).strip()
            start = m.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(t)
            body = t[start:end].strip()
            if name:
                out.append((name, body))
        return out

    # 格式②：---章节名---
    parts = re.split(r"-{2,}\s*([^-\\n]{2,20}?)\s*-{2,}", t)
    for i in range(1, len(parts) - 1, 2):
        name = parts[i].strip()
        body = (parts[i + 1] or "").strip()
        if name:
            out.append((name, body))
    if not out and (text or "").strip():
        # 模型不听话没输出分隔符 → 整段当"分析正文"
        out.append(("分析正文", text.strip()))
    return out


# ════════════════════════════════════════════════════════════
# 3. 图示：内联 SVG 自绘（零依赖，可导出矢量 PDF）
# ════════════════════════════════════════════════════════════

def _svg_open(w: int, h: int) -> str:
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" '
            f'style="display:block;max-width:100%;font-family:inherit">')


def _bar_chart_svg(items: list[tuple[str, float]], unit: str = "",
                   highlight_max: bool = True) -> str:
    """横向条形图：维度名在左，条在右，数值贴条尾。

    横向而不是纵向：维度名是中文（"一号产线""波峰焊"），竖着排会挤成竖条或斜字，
    横向排一行一个，读起来跟看表格一样自然。
    """
    if not items:
        return '<p class="empty">暂无可绘制的数据</p>'
    n = len(items)
    row_h = 34
    pad_top, pad_left, pad_right = 26, 132, 74
    h = pad_top + n * row_h + 18
    w = 760
    bar_max = w - pad_left - pad_right
    vmax = max((abs(v) for _, v in items), default=1) or 1
    mx_name = max(items, key=lambda x: x[1])[0] if highlight_max else ""

    p = [_svg_open(w, h)]
    # 网格竖线（4 档）
    for k in range(1, 5):
        gx = pad_left + bar_max * k / 4
        p.append(f'<line x1="{gx}" y1="{pad_top - 6}" x2="{gx}" y2="{h - 16}" '
                 f'stroke="{C_BORDER}" stroke-width="1" stroke-dasharray="3 3"/>')
    for i, (name, val) in enumerate(items):
        y = pad_top + i * row_h
        blen = max(2.0, bar_max * abs(val) / vmax)
        is_top = highlight_max and name == mx_name
        color = C_BLUE if is_top else C_BLUE_LT
        label = html_mod.escape(str(name))[:12]
        p.append(f'<text x="{pad_left - 10}" y="{y + 15}" text-anchor="end" '
                 f'font-size="12" fill="{C_GRAY}">{label}</text>')
        p.append(f'<rect x="{pad_left}" y="{y + 4}" width="{blen:.1f}" height="17" rx="4" '
                 f'fill="{color}"/>')
        p.append(f'<text x="{pad_left + blen + 8}" y="{y + 17}" font-size="11.5" '
                 f'fill="{C_INK}" font-weight="{"600" if is_top else "400"}">'
                 f'{html_mod.escape(_fmt_num(val))}{html_mod.escape(unit)}</text>')
    p.append("</svg>")
    return "".join(p)


def _line_chart_svg(items: list[tuple[str, float]], unit: str = "") -> str:
    """折线 + 面积图：适合时间序列（趋势）。"""
    if not items:
        return '<p class="empty">暂无可绘制的数据</p>'
    w, h = 760, 260
    pad_l, pad_r, pad_t, pad_b = 52, 22, 24, 52
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    vals = [v for _, v in items]
    vmax, vmin = max(vals), min(vals)
    if vmax == vmin:
        vmax, vmin = vmax + 1, vmin - 1
    span = vmax - vmin
    vmax += span * 0.12
    vmin -= span * 0.12
    n = len(items)
    step = iw / max(1, n - 1) if n > 1 else iw

    def X(i: int) -> float:
        return pad_l + (step * i if n > 1 else iw / 2)

    def Y(v: float) -> float:
        return pad_t + ih * (1 - (v - vmin) / (vmax - vmin))

    p = [_svg_open(w, h)]
    # 横向网格 + Y 轴刻度
    for k in range(5):
        gy = pad_t + ih * k / 4
        gv = vmax - (vmax - vmin) * k / 4
        p.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{w - pad_r}" y2="{gy:.1f}" '
                 f'stroke="{C_BORDER}" stroke-width="1"/>')
        p.append(f'<text x="{pad_l - 8}" y="{gy + 4:.1f}" text-anchor="end" '
                 f'font-size="10.5" fill="{C_MUTE}">{html_mod.escape(_fmt_num(round(gv, 2)))}</text>')
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, (_, v) in enumerate(items))
    p.append(f'<polygon points="{pad_l},{pad_t + ih} {pts} {w - pad_r},{pad_t + ih}" '
             f'fill="{C_BLUE}" opacity="0.10"/>')
    p.append(f'<polyline points="{pts}" fill="none" stroke="{C_BLUE}" stroke-width="2.4" '
             f'stroke-linejoin="round" stroke-linecap="round"/>')
    # 数据点 + X 轴标签（标签太多则抽稀）
    every = max(1, n // 12)
    for i, (name, v) in enumerate(items):
        p.append(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="3.4" fill="#fff" '
                 f'stroke="{C_BLUE}" stroke-width="2"/>')
        if i % every == 0 or i == n - 1:
            p.append(f'<text x="{X(i):.1f}" y="{h - pad_b + 18}" text-anchor="middle" '
                     f'font-size="10.5" fill="{C_MUTE}">{html_mod.escape(str(name))[:10]}</text>')
    # 最高点标注
    mi = max(range(n), key=lambda i: items[i][1])
    p.append(f'<text x="{X(mi):.1f}" y="{Y(items[mi][1]) - 10:.1f}" text-anchor="middle" '
             f'font-size="11" font-weight="600" fill="{C_INK}">'
             f'{html_mod.escape(_fmt_num(items[mi][1]))}{html_mod.escape(unit)}</text>')
    p.append("</svg>")
    return "".join(p)


def _donut_svg(items: list[tuple[str, float]], unit: str = "") -> str:
    """环形占比图 + 图例（占比表）。"""
    if not items:
        return '<p class="empty">暂无可绘制的数据</p>'
    items = [(k, max(0.0, v)) for k, v in items][:8]
    total = sum(v for _, v in items) or 1
    w, h = 760, 258
    cx, cy, R, r = 132, 130, 96, 58
    p = [_svg_open(w, h)]
    ang = -90.0
    for i, (name, v) in enumerate(items):
        sweep = 360.0 * v / total
        a0 = ang
        a1 = ang + sweep
        ang = a1
        large = 1 if sweep > 180 else 0

        def PT(a: float, rad: float) -> tuple[float, float]:
            import math
            ra = math.radians(a)
            return cx + rad * math.cos(ra), cy + rad * math.sin(ra)

        x0, y0 = PT(a0, R)
        x1, y1 = PT(a1, R)
        x2, y2 = PT(a1, r)
        x3, y3 = PT(a0, r)
        p.append(
            f'<path d="M {x0:.1f} {y0:.1f} A {R} {R} 0 {large} 1 {x1:.1f} {y1:.1f} '
            f'L {x2:.1f} {y2:.1f} A {r} {r} 0 {large} 0 {x3:.1f} {y3:.1f} Z" '
            f'fill="{PALETTE[i % len(PALETTE)]}" stroke="#fff" stroke-width="2"/>'
        )
    p.append(f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" font-size="22" '
             f'font-weight="700" fill="{C_INK}">{html_mod.escape(_fmt_num(round(total, 1)))}</text>')
    p.append(f'<text x="{cx}" y="{cy + 16}" text-anchor="middle" font-size="11" '
             f'fill="{C_MUTE}">合计</text>')
    # 右侧图例
    ly = 34
    for i, (name, v) in enumerate(items):
        pct = v / total * 100
        p.append(f'<rect x="300" y="{ly - 9}" width="10" height="10" rx="2.5" '
                 f'fill="{PALETTE[i % len(PALETTE)]}"/>')
        p.append(f'<text x="318" y="{ly}" font-size="12" fill="{C_GRAY}">'
                 f'{html_mod.escape(str(name))[:16]}</text>')
        p.append(f'<text x="600" y="{ly}" font-size="12" fill="{C_INK}" text-anchor="end" '
                 f'font-weight="600">{html_mod.escape(_fmt_num(v))}'
                 f'{html_mod.escape(unit)}</text>')
        p.append(f'<text x="745" y="{ly}" font-size="12" fill="{C_MUTE}" text-anchor="end">'
                 f'{pct:.1f}%</text>')
        ly += 26
    p.append("</svg>")
    return "".join(p)


def _gauge_svg(value: float, label: str = "", unit: str = "%",
               good: float = 95.0, warn: float = 90.0) -> str:
    """半圆仪表：单项 KPI（如整体良率）的达标判断。"""
    import math
    w, h = 340, 196
    cx, cy, R = 170, 156, 116
    p = [_svg_open(w, h)]

    def ARC(v: float, color: str, width: int) -> str:
        frac = max(0.0, min(1.0, v / 100.0))
        a = math.pi * (1 - frac)
        x = cx + R * math.cos(a)
        y = cy - R * math.sin(a)
        large = 1 if frac > 0.5 else 0
        return (f'<path d="M {cx - R} {cy} A {R} {R} 0 {large} 1 {x:.1f} {y:.1f}" '
                f'fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="round"/>')

    p.append(ARC(100, "#eef1f6", 20))
    p.append(ARC(value, C_BLUE if value >= good else ("#e8a33d" if value >= warn else C_UP), 20))
    # 指针
    a = math.pi * (1 - max(0.0, min(1.0, value / 100.0)))
    p.append(f'<line x1="{cx}" y1="{cy}" x2="{cx + (R - 14) * math.cos(a):.1f}" '
             f'y2="{cy - (R - 14) * math.sin(a):.1f}" stroke="{C_INK}" stroke-width="3.4" '
             f'stroke-linecap="round"/>')
    p.append(f'<circle cx="{cx}" cy="{cy}" r="7" fill="{C_INK}"/>')
    p.append(f'<text x="{cx}" y="{cy - 42}" text-anchor="middle" font-size="30" '
             f'font-weight="700" fill="{C_INK}">{html_mod.escape(_fmt_num(value))}{html_mod.escape(unit)}</text>')
    if label:
        p.append(f'<text x="{cx}" y="{cy - 18}" text-anchor="middle" font-size="12" '
                 f'fill="{C_MUTE}">{html_mod.escape(label)}</text>')
    p.append(f'<text x="{cx - R + 4}" y="{cy + 20}" font-size="10.5" fill="{C_MUTE}">0</text>')
    p.append(f'<text x="{cx + R - 4}" y="{cy + 20}" text-anchor="end" font-size="10.5" '
             f'fill="{C_MUTE}">100</text>')
    p.append("</svg>")
    return "".join(p)


def _kpi_cards(data: dict) -> str:
    """顶部指标卡：从结果里抽出几个有意义的数，用大字号立在报告最前。

    只报"平均"是不够的——看排名型数据的人第一眼想知道的是范围有多大，
    所以主指标额外给一张"最高 / 最低"，比均值更有信息量。
    """
    cols: list[str] = data.get("columns") or []
    rows: list[dict] = data.get("rows") or []
    if not rows:
        return ""
    shape = _describe_shape(cols, rows, data.get("aliases"))
    dim, main_meas = shape.get("dim"), shape.get("measure")
    items = shape.get("items") or []
    # 卡片名是给读者扫的，用中文业务名；这里取 zh 短名不取带括号的 full —
    # 卡片宽度只有 minmax(132px) 那么点，「良率百分比（yield_rate_pct）」会折两行把卡撑高。
    disp = shape.get("disp") or {}

    def dn(c: str | None) -> str:
        if not c:
            return ""
        return (disp.get(c) or {}).get("zh") or c

    cards: list[tuple[str, str, str]] = []
    cards.append(("数据行数", _fmt_num(len(rows)), f"{len(cols)} 个字段"))

    # 主指标优先（_describe_shape 已经按业务关注度挑过），
    # 再补一张极值卡，然后才轮到其余数字列。
    order: list[str] = []
    if main_meas:
        order.append(main_meas)
    order += [c for c in cols if c != main_meas and c != dim]

    for c in order:
        if len(cards) >= 4:
            break
        vals = [_to_float(r.get(c)) for r in rows if _looks_like_number(r.get(c))]
        if not vals:
            continue
        unit = "%" if _is_pct(c) else ""
        avg = sum(vals) / len(vals)
        cards.append((dn(c), _fmt_num(round(avg, 2)) + unit, "平均"))

        # 主指标补极值：只有分组数 >1 时才有意义
        if c == main_meas and len(items) >= 2 and len(cards) < 4:
            hi = max(items, key=lambda x: x[1])
            lo = min(items, key=lambda x: x[1])
            span = f"{_fmt_num(hi[1])}{unit} ～ {_fmt_num(lo[1])}{unit}"
            cards.append((f"{dn(c)} 区间", span, f"{hi[0]} 最高 / {lo[0]} 最低"))

    html = ['<div class="kpis">']
    for name, val, sub in cards[:4]:
        html.append(
            f'<div class="kpi"><div class="kpi-v">{html_mod.escape(val)}</div>'
            f'<div class="kpi-k">{html_mod.escape(str(name))}</div>'
            f'<div class="kpi-s">{html_mod.escape(sub)}</div></div>'
        )
    html.append("</div>")
    return "".join(html)


def _figure(title: str, caption: str, svg: str) -> str:
    return (f'<figure class="fig"><figcaption class="fig-t">{html_mod.escape(title)}'
            f'</figcaption>{svg}'
            f'<div class="fig-c">{html_mod.escape(caption)}</div></figure>')


def build_figures(question: str, data: dict) -> tuple[str, list[str]]:
    """从查询结果自动选图并生成 SVG。返回 (HTML, 图注清单)。

    选图规则（按数据形态，不是瞎配）：
    - 首列像时间（日期/月份/年）→ 折线趋势
    - 分组 ≤ 8 且数值是占比语义 → 环形
    - 分组为单一整体指标（只有 1 行）→ 仪表
    - 其余 → 横向条形
    """
    cols: list[str] = data.get("columns") or []
    rows: list[dict] = data.get("rows") or []
    if not rows or not cols:
        return "", []

    # 走 _describe_shape 而不是直接调 _pick_dim_measure：
    # 图和文字必须讲同一个指标，否则会出现"标题写良率、条形图画投入数量"这种打脸。
    shape = _describe_shape(cols, rows, data.get("aliases"))
    dim = shape.get("dim")
    measure = shape.get("measure")
    # 图标题写中文业务名。图和正文并排出现，正文写「良率」而图标题写 yield_rate_pct，
    # 读者要自己猜这是不是同一个东西——图是给人一眼看懂的，标题就不该是列名。
    # 用 zh 短名不取 full：图标题在 880px 宽的画布上居中排，「各工序的良率百分比（yield_rate_pct）对比」
    # 这种长度会把标题挤到换行，图注更是一行放不下。
    disp = shape.get("disp") or {}

    def dn(c: str | None) -> str:
        if not c:
            return ""
        return (disp.get(c) or {}).get("zh") or c

    figs: list[str] = []
    caps: list[str] = []

    if measure and dim:
        items = [(str(r.get(dim)), _to_float(r.get(measure))) for r in rows]
        items = [(k, v) for k, v in items if k and k != "None"][:20]
        unit = "%" if any(k in measure.lower() for k in ("rate", "ratio", "pct", "率", "比例")) else ""
        dim_time = bool(re.search(r"(date|time|day|month|year|周|月|日|年|季度)", dim, re.I))

        if dim_time and len(items) >= 4:
            figs.append(_figure(f"{dn(measure)} 趋势", f"按{dn(dim)}排列，横轴为时间",
                                _line_chart_svg(items, unit)))
            caps.append(f"按{dn(dim)}的{dn(measure)}走势")
        elif len(rows) == 1:
            figs.append(_figure(f"{dn(measure)} 总体水平", "单一汇总指标",
                                _gauge_svg(_to_float(rows[0].get(measure)), dn(measure), unit or "%")))
            caps.append(f"{dn(measure)} = {_fmt_num(rows[0].get(measure))}{unit}")
        else:
            figs.append(_figure(f"{dn(measure)} 对比", f"按{dn(dim)}分组，蓝色为最高项",
                                _bar_chart_svg(items, unit)))
            caps.append(f"{dn(dim)}维度的{dn(measure)}对比")

        if 2 <= len(items) <= 8 and len(figs) < 2:
            figs.append(_figure(f"{dn(measure)} 构成占比", "各分组占总量的比例",
                                _donut_svg(items, unit)))
            caps.append(f"各{dn(dim)}的占比构成")

    # 还有第二度量列 → 再补一张（覆盖率/数量等），让报告不止一张图
    if len(figs) < 2:
        others = [c for c in cols if c not in (dim, measure)
                  and any(_looks_like_number(r.get(c)) for r in rows[:10])]
        if others and not re.search(r"(date|time|day|month|year)", cols[0], re.I):
            o = others[0]
            oitems = [(str(r.get(cols[0])), _to_float(r.get(o))) for r in rows][:20]
            oitems = [(k, v) for k, v in oitems if k and k != "None"]
            if oitems:
                figs.append(_figure(f"{dn(o)} 对比", f"按{dn(cols[0])}分组",
                                    _bar_chart_svg(oitems, "%" if _is_pct(o) else "")))
                caps.append(f"{dn(cols[0])}维度的{dn(o)}对比")

    return "".join(figs), caps


# ════════════════════════════════════════════════════════════
# 4. 组装：单文件自包含 HTML 报告
# ════════════════════════════════════════════════════════════

def _data_table_html(cols: list[str], rows: list[dict], limit: int = 30,
                     display_cols: list[str] | None = None,
                     alias_map: dict[str, str] | None = None) -> str:
    """明细表。表头走 display_cols（中文业务名），取值仍按 cols 从行里拿。

    注意 `zip` 的两个列表必须等长：display_cols 由 collect_question_data 用
    labelize_columns(cols) 生成，天然一一对应；长度对不上时退回原始列名，
    宁可显英文也不要错位——错位的表比英文列名的表更容易误导人。
    """
    if not cols or not rows:
        return '<p class="empty">无数据行</p>'
    # 不传 display_cols 时自己翻——宁可多算一次，也不要因为调用方忘了传
    # 就让表头退回英文列名。长度对不上同样重算（错位的表比英文表更误导人）。
    if not display_cols or len(display_cols) != len(cols):
        display_cols = labelize_columns(cols, alias_map)
    th = "".join(f"<th>{html_mod.escape(str(c))}</th>" for c in display_cols)
    body = []
    for r in rows[:limit]:
        tds = "".join(
            f'<td class="{"num" if _looks_like_number(r.get(c)) else ""}">'
            f'{html_mod.escape(_fmt_num(r.get(c)))}</td>' for c in cols
        )
        body.append(f"<tr>{tds}</tr>")
    more = (f'<div class="tbl-more">共 {len(rows)} 行，此处展示前 {limit} 行</div>'
            if len(rows) > limit else "")
    return (f'<div class="tbl-wrap"><table class="dt"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table>{more}</div>')


def _sections_html(sections: list[tuple[str, str]]) -> str:
    """把章节渲染成卡片；正文里识别 ✅/⚠️ 前缀做色块，数字做高亮。"""
    if not sections:
        return '<p class="empty">本次未生成分析文字</p>'
    out = []
    for name, body in sections:
        paras = [x.strip() for x in re.split(r"\n{1,}", body) if x.strip()]
        ps = []
        for t in paras:
            txt = html_mod.escape(t)
            # 数字高亮（含千分位与百分号）
            txt = re.sub(r"(\d[\d,]*(?:\.\d+)?\s*%?)", r'<b class="n">\1</b>', txt)
            if t.startswith("⚠️") or t.startswith("⚠"):
                ps.append(f'<p class="warn">{txt}</p>')
            elif t.startswith("✅"):
                ps.append(f'<p class="good">{txt}</p>')
            else:
                ps.append(f"<p>{txt}</p>")
        out.append(
            f'<section class="sec"><h3 class="sec-h">{html_mod.escape(name)}</h3>'
            f'<div class="sec-b">{"".join(ps)}</div></section>'
        )
    return "".join(out)


def build_report_html(question: str, data: dict, sections: list[tuple[str, str]],
                      user: str = "", db_name: str = "") -> str:
    """组装最终报告 HTML（单文件、零外部依赖，可直接打印成 PDF）。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    cols: list[str] = data.get("columns") or []
    rows: list[dict] = data.get("rows") or []
    figs_html, caps = build_figures(question, data)
    kpis = _kpi_cards(data)
    # 表头不指望调用方传 display_columns——main.py 拼响应时只回传了 columns，
    # 传 None 进来就会静默退回英文原始列名（同一份报告里正文中文、表头英文）。
    # 这里是渲染层，自己翻译一遍最稳，display_columns 只当提示用。
    dcols = [c for c in (data.get("display_columns") or [])
             if c not in (None, "")]
    if len(dcols) != len(cols):
        dcols = labelize_columns(cols, data.get("aliases"))
    table_html = _data_table_html(cols, rows, display_cols=dcols,
                                  alias_map=data.get("aliases"))
    body_html = _sections_html(sections)

    status = ('<span class="pill ok">数据已取回</span>' if data.get("ok")
              else f'<span class="pill bad">取数失败：{html_mod.escape(str(data.get("error") or "")[:80])}</span>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html_mod.escape(question[:40])} — 分析报告</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"Microsoft YaHei","PingFang SC",-apple-system,sans-serif;
  background:#f4f6f9;color:{C_INK};padding:28px 16px;line-height:1.75;
  -webkit-font-smoothing:antialiased}}
.wrap{{max-width:880px;margin:0 auto}}
.cover{{background:linear-gradient(135deg,{C_BLUE} 0%,#1F66D6 100%);color:#fff;
  border-radius:18px;padding:32px 34px 26px;margin-bottom:20px;
  box-shadow:0 10px 30px -12px rgba(46,124,240,.45)}}
.cover .eyebrow{{font-size:12px;letter-spacing:2.5px;opacity:.82;font-weight:600}}
.cover h1{{font-size:25px;font-weight:700;margin:9px 0 14px;line-height:1.45}}
.cover .meta{{display:flex;flex-wrap:wrap;gap:8px 20px;font-size:12px;opacity:.9}}
.pill{{display:inline-block;padding:2px 10px;border-radius:999px;font-size:11.5px;
  background:rgba(255,255,255,.2);border:1px solid rgba(255,255,255,.35)}}
.card{{background:#fff;border-radius:16px;padding:24px 26px;margin-bottom:16px;
  box-shadow:0 1px 3px rgba(16,24,40,.06),0 12px 32px -18px rgba(16,24,40,.16)}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:12px;
  margin-bottom:16px}}
.kpi{{background:#fff;border:1px solid {C_BORDER};border-radius:14px;padding:15px 17px;
  box-shadow:0 1px 2px rgba(16,24,40,.04)}}
.kpi-v{{font-size:25px;font-weight:700;color:{C_BLUE};letter-spacing:-.5px;
  font-variant-numeric:tabular-nums;line-height:1.25}}
.kpi-k{{font-size:12.5px;color:{C_GRAY};margin-top:3px;font-weight:600}}
.kpi-s{{font-size:11px;color:{C_MUTE};margin-top:1px}}
.sec{{border-left:3px solid {C_BLUE};padding:2px 0 2px 16px;margin-bottom:20px}}
.sec:last-child{{margin-bottom:0}}
.sec-h{{font-size:15.5px;font-weight:700;color:{C_INK};margin-bottom:9px}}
.sec-b p{{font-size:14px;color:#374151;margin-bottom:8px}}
.sec-b p:last-child{{margin-bottom:0}}
.sec-b .warn{{background:#fff6f5;border:1px solid #ffd9d3;color:#a8321f;
  border-radius:9px;padding:9px 13px}}
.sec-b .good{{background:#f3f8ff;border:1px solid #d6e8ff;color:#1257a8;
  border-radius:9px;padding:9px 13px}}
.n{{color:{C_UP};font-weight:700}}
.fig{{margin:0 0 22px;padding:18px;background:{C_BG_SOFT};border:1px solid #eaeef5;
  border-radius:14px}}
.fig:last-child{{margin-bottom:0}}
.fig-t{{font-size:13.5px;font-weight:700;color:{C_INK};margin-bottom:12px}}
.fig-c{{font-size:11.5px;color:{C_MUTE};margin-top:10px;line-height:1.6}}
.tbl-wrap{{overflow-x:auto;border:1px solid {C_BORDER};border-radius:12px}}
.dt{{width:100%;border-collapse:collapse;font-size:12.5px}}
.dt th{{background:#f5f7fa;color:{C_GRAY};font-weight:600;text-align:left;
  padding:9px 12px;white-space:nowrap;border-bottom:1px solid {C_BORDER}}}
.dt td{{padding:8px 12px;border-bottom:1px solid #f2f4f7;color:#374151;white-space:nowrap}}
.dt tbody tr:last-child td{{border-bottom:none}}
.dt tbody tr:nth-child(even){{background:#fcfdfe}}
.dt td.num{{text-align:right;font-variant-numeric:tabular-nums;color:{C_INK};font-weight:600}}
.tbl-more{{padding:8px 12px;font-size:11.5px;color:{C_MUTE};background:#fafbfc}}
.empty{{color:{C_MUTE};font-size:13px;padding:12px 0}}
.sqlbox{{background:#1e293b;color:#cbd5e1;border-radius:12px;padding:14px 16px;
  font-family:"JetBrains Mono",Consolas,monospace;font-size:11.5px;line-height:1.7;
  overflow-x:auto;white-space:pre-wrap;word-break:break-all}}
.foot{{text-align:center;color:{C_MUTE};font-size:11.5px;padding:14px 0 6px}}
@media print{{
  body{{background:#fff;padding:0}}
  .card,.kpi,.fig{{box-shadow:none;break-inside:avoid}}
  .cover{{break-after:avoid}}
  .sec{{break-inside:avoid}}
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="cover">
    <div class="eyebrow">Vequo 智能问析 · 数据分析报告</div>
    <h1>{html_mod.escape(question)}</h1>
    <div class="meta">
      <span class="pill">{html_mod.escape(now)}</span>
      {status}
      <span>数据源：{html_mod.escape(db_name or '当前业务库')}</span>
      <span>生成人：{html_mod.escape(user or '—')}</span>
    </div>
  </div>

  {f'<div class="kpis">{kpis}</div>' if kpis else ''}

  <div class="card">
    <h2 style="font-size:16px;font-weight:700;margin-bottom:16px">一、分析结论</h2>
    {body_html}
  </div>

  {f'''<div class="card">
    <h2 style="font-size:16px;font-weight:700;margin-bottom:16px">二、数据图示</h2>
    {figs_html}
  </div>''' if figs_html else ''}

  <div class="card">
    <h2 style="font-size:16px;font-weight:700;margin-bottom:16px">三、明细数据</h2>
    {table_html}
  </div>

  <div class="card">
    <h2 style="font-size:16px;font-weight:700;margin-bottom:12px">附：取数口径</h2>
    <div class="sqlbox">{html_mod.escape(data.get('sql') or '—')}</div>
  </div>

  <div class="foot">Vequo 智能问析 · 本报告由系统自动生成，数字来自当前数据库实时查询 · {html_mod.escape(now)}</div>
</div>
</body>
</html>"""


def build_report_blocks(question: str, data: dict,
                        sections: list[tuple[str, str]],
                        user: str = "", db_name: str = "") -> list[tuple[str, str]]:
    """把报告拆成 (类型, 内容) 块，供 docx / pdf 导出复用。

    块类型必须严格对齐 `report_export.blocks_to_docx` / `blocks_to_pdf` 认识的取值：
    title / meta / heading / subheading / para / risk / good。
    一开始我按 h1/h2/p/table 写，导出时除了正文以外的块全被吞掉了——
    这两个函数的分发是 if-elif 长链，末尾 else 才当正文处理，没有 table 分支，
    类型不识别就直接落进正文缩进里。所以表格在这里先拍平成文本行，
    导出文档里以"对齐的明细清单"呈现（PDF/Word 里画 SVG 不现实，
    图示的价值由数字 + 图注文字承接）。
    """
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    blocks: list[tuple[str, str]] = [("title", question[:60] or "数据分析报告")]
    blocks.append(("meta", f"Vequo 智能问析 · {now} · 数据源 {db_name or '当前业务库'} · "
                           f"生成人 {user or '—'}"))

    cols: list[str] = data.get("columns") or []
    rows: list[dict] = data.get("rows") or []

    if not data.get("ok"):
        blocks.append(("heading", "取数说明"))
        blocks.append(("risk", f"本次未取到数据：{data.get('error') or '未知原因'}"))
        blocks.append(("para", f"取数口径：{data.get('sql') or '—'}"))
        return blocks

    for name, body in sections:
        blocks.append(("subheading", name))
        for line in [x.strip() for x in re.split(r"\n+", body) if x.strip()]:
            if line.startswith("⚠️") or line.startswith("⚠"):
                blocks.append(("risk", line))
            elif line.startswith("✅"):
                blocks.append(("good", line))
            else:
                blocks.append(("para", line))

    # 数据图示 → 文字化：导出文档里放不了 SVG，把图注 + 关键数值列出来
    _, caps = build_figures(question, data)
    if caps:
        blocks.append(("subheading", "数据图示（说明）"))
        for c in caps:
            blocks.append(("para", c))

    if cols and rows:
        # 导出文档里的表头也要中文：Word/PDF 是拿去汇报和归档的，
        # 表头写 process_name 的话，收文件的人得回头问"这列是什么"。
        heads = data.get("display_columns") or []
        if len(heads) != len(cols):
            heads = labelize_columns(cols, data.get("aliases"))
        blocks.append(("subheading", f"明细数据（共 {len(rows)} 行，展示前 {min(40, len(rows))} 行）"))
        blocks.append(("para", " ｜ ".join(str(c) for c in heads)))
        for r in rows[:40]:
            blocks.append(("para", " ｜ ".join(_fmt_num(r.get(c)) for c in cols)))

    blocks.append(("subheading", "取数口径"))
    blocks.append(("para", data.get("sql") or "—"))
    return blocks
