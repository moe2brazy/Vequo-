# -*- coding: utf-8 -*-
"""编译链真库回归（2026-10-03 新增）。

背景：连续两轮审查都发现「一个 fix 只覆盖同型问题的一部分」，而此前项目**没有任何
一条编译链的回归护栏** —— 静态断言全绿不代表真库跑得通，本轮补上这一层。

覆盖：
  1. 全量题库编译 + 执行（崩溃数、执行失败数必须为 0）
  2. 与备份版改写函数的零回归基线对比（SQL/执行结果必须一致）
  3. 关键修复的真实场景验证（数据感知重锚、多 days 区间不塌陷、权限派生表/CTE 不绕过）
  4. 第二轮补充：绝对月份 0 行提示、insight_scan worker 线程继承 ACL、
     get_foreign_keys 失败不钉缓存、库存量最新快照日口径 + 表达式红线注入回归

运行：
    python tests/regression_compile_chain.py            # 全部
    python tests/regression_compile_chain.py --quick    # 只跑 1+3+4（跳过基线对比）

依赖真实 PostgreSQL（读 backend/.env 的 DB_* 配置）。库不可用时全部 SKIP 而非 FAIL，
避免在无库环境（CI/新机器）产生假红。
"""
from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

QUICK = "--quick" in sys.argv

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(cond), detail))


def _load_bank() -> list:
    """题库：优先 gen_yans_bank_v3，失败则用内置最小集（保证脚本总能跑）。"""
    try:
        from gen_yans_bank_v3 import build_bank_v3
        bank = build_bank_v3()
        if bank:
            return bank
    except Exception:
        pass
    return [
        {"q": "最近30天各产线的投入数量", "expect": "group"},
        {"q": "本月各产线产量", "expect": "group"},
        {"q": "各产线的合格数量", "expect": "group"},
        {"q": "各产线的不良数排行", "expect": "rank"},
        {"q": "各产线的平均不良数", "expect": "group"},
        {"q": "最近7天各产线的产量", "expect": "group"},
        {"q": "不良类型排行", "expect": "rank"},
        {"q": "各产线的总投入数量", "expect": "total"},
    ]


def _shape_ok(expect: str, n: int) -> bool:
    if expect == "top":
        return n >= 1
    if expect in ("group", "rank", "trend"):
        return n > 1
    if expect == "total":
        return n == 1
    return True


# ── 1. 全量编译 + 执行 ────────────────────────────────────────────────
def test_compile_and_execute() -> None:
    from agent.metric_compiler import try_compile_metric
    from db.executor import execute_sql

    bank = _load_bank()
    crashes: list[str] = []
    exec_fail: list[tuple[str, str]] = []
    compiled = 0
    ok_rows = 0
    t0 = time.time()

    for item in bank:
        q = item["q"]
        expect = item.get("expect", "group")
        try:
            r = try_compile_metric(q)
        except Exception as e:
            crashes.append("%s -> 编译器崩溃 %s: %s" % (q, type(e).__name__, e))
            continue
        if not r:
            continue
        compiled += 1
        try:
            res = execute_sql(r["sql"])
        except Exception as e:
            crashes.append("%s -> 执行器崩溃 %s: %s" % (q, type(e).__name__, e))
            continue
        if not res.get("success"):
            exec_fail.append((q, str(res.get("error", ""))[:120]))
            continue
        if _shape_ok(expect, int(res.get("row_count") or 0)):
            ok_rows += 1

    el = time.time() - t0
    print("[1] 全量编译+执行：题 %d / 编译命中 %d / 形态正确 %d / 耗时 %.1fs"
          % (len(bank), compiled, ok_rows, el))
    # 崩溃与执行失败必须为 0 —— 这是最硬的两条底线
    check("编译链零崩溃", not crashes, "; ".join(crashes[:3]))
    check("编译链零执行失败", not exec_fail, "; ".join("%s: %s" % x for x in exec_fail[:3]))


# ── 2. 零回归基线对比（与备份版改写函数）────────────────────────────
def test_baseline_no_regression() -> None:
    """把两个改写函数换回备份实现，比对 SQL 与执行结果。"""
    from agent.metric_compiler import try_compile_metric
    from db.executor import execute_sql
    from agent import llm_service as ls

    # 基线来源：优先用 git 历史里的改写前版本（`git show <commit>:<path>`），
    # 回退到旧的 .bak_ 备份文件。
    # 2026-10-04：备份文件已被清理（移出到 D:\vequo\_archive），改用 git 取基线 ——
    # 这样基线对比不再依赖任何临时文件，只要历史在就永远可跑。
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bak_src = None
    src_rel = "backend/agent/llm_service.py"
    for commit in ("5139972^", "5139972~1"):
        try:
            import subprocess
            bak_src = subprocess.run(
                ["git", "-C", os.path.dirname(ROOT), "show", "%s:%s" % (commit, src_rel)],
                capture_output=True, text=True, encoding="utf-8", timeout=30
            ).stdout
            if bak_src and "def _fix_relative_window_days" in bak_src:
                print("[2] 基线来源：git %s（改写前版本）" % commit)
                break
            bak_src = None
        except Exception:
            bak_src = None
    if not bak_src:
        old = os.path.join(ROOT, "agent", "llm_service.py.bak_r19pre_20261003")
        if os.path.exists(old):
            bak_src = open(old, encoding="utf-8").read()
            print("[2] 基线来源：.bak_ 备份文件")
    if not bak_src:
        print("[2] 找不到基线来源（git 历史不可达且无 .bak_），跳过基线对比")
        return

    def _extract(name: str) -> str:
        m = re.search(r"^def %s\(.*?(?=^def )" % re.escape(name), bak_src, re.S | re.M)
        return m.group(0) if m else ""

    ns = dict(ls.__dict__)
    try:
        exec(_extract("_fix_relative_window_days"), ns)
        exec(_extract("_fix_relative_month_literal"), ns)
    except Exception as e:
        check("基线对比可执行", False, repr(e)[:100])
        return
    old_window = ns["_fix_relative_window_days"]
    old_month = ns["_fix_relative_month_literal"]

    # 与 apply_output_fixes 相同的 steps 顺序（改写器版本在此可控）
    chain = [
        ("_fix_dim_listing_limit", "q2"), ("_fix_period_compare_branches", "q2"),
        ("_fix_period_compare_merged", "q2"), ("_fix_degenerate_self_compare", "q2"),
        ("_fix_relative_date_anchor", "q2"), ("_fix_relative_month_literal", "MONTH"),
        ("_fix_relative_window_days", "WINDOW"), ("_fix_window_drop_time_groupby", "q2"),
        ("_fix_window_diff_coalesce", "q2"), ("_fix_topn_stable_tiebreak", "q2"),
        ("_fix_value_column_mismatch", "q1"), ("_fix_having_agg_select", "q2"),
        ("_fix_count_from_owner", "q2"), ("_fix_explicit_col_select", "q2"),
        ("_fix_group_key_superset", "q2"), ("_fix_detail_row_superset", "q2"),
    ]

    def run(sql: str, q: str, month_fn, window_fn) -> str:
        out = sql
        for name, kind in chain:
            if kind == "MONTH":
                fn = month_fn
            elif kind == "WINDOW":
                fn = window_fn
            elif kind == "q1":
                out = getattr(ls, name)(out)
                continue
            else:
                fn = getattr(ls, name)
            try:
                out = fn(q, out)
            except Exception:
                pass
        return out

    def probe(s: str):
        try:
            r = execute_sql(s)
            return bool(r.get("success")), int(r.get("row_count") or 0)
        except Exception:
            return False, -1

    bank = _load_bank()
    diff_sql = 0
    diff_res: list[str] = []
    for item in bank:
        r = try_compile_metric(item["q"])
        if not r:
            continue
        new = run(r["sql"], item["q"], ls._fix_relative_month_literal, ls._fix_relative_window_days)
        old = run(r["sql"], item["q"], old_month, old_window)
        if new.strip() == old.strip():
            continue
        diff_sql += 1
        if probe(new) != probe(old):
            diff_res.append(item["q"])

    print("[2] 基线对比：SQL 有差异 %d 题，其中执行结果不同 %d 题" % (diff_sql, len(diff_res)))
    check("基线对比零结果差异", not diff_res, "; ".join(diff_res[:3]))


# ── 3. 关键修复的真实场景验证 ─────────────────────────────────────────
def test_data_range_reanchor() -> None:
    """LLM 写死错误年份 → 必须被重锚，且返回数据、不被链内后续规则撤销。"""
    from db.executor import execute_sql
    from agent import llm_service as ls

    r = execute_sql("SELECT MIN(stat_date) AS mn, MAX(stat_date) AS mx FROM mes_process_output")
    if not r.get("success") or not r.get("rows"):
        check("数据范围探测", False, "查询失败")
        return
    # 按列名取（不同方言/驱动返回的 dict 键不一致，位置索引不可靠）
    row = r["rows"][0]
    mx = str(row.get("mx") or (list(row.values())[1] if len(row) > 1 else ""))[:10]
    sql_bad = ("SELECT line_id, SUM(input_qty) FROM mes_process_output "
               "WHERE stat_date >= '2024-05-01' AND stat_date <= '2024-05-31' GROUP BY line_id")
    notes, fired = [], []
    out = ls.apply_output_fixes("本月的投入数量", sql_bad,
                                sqlexec=lambda s: execute_sql(s),
                                notes=notes, fired=fired)
    print("[3] 重锚：fired=%s sql=%s" % (fired, out[:110]))
    check("硬编码错误年份触发重锚", "_fix_data_aware_time_anchor" in fired, str(fired))
    check("重锚后不再是错误年份", "'2024-05" not in out, out[:120])
    # 本条是 P0「重锚被 month_literal 撤销」的回归护栏
    check("重锚未被改回 CURRENT_DATE", "CURRENT_DATE" not in out, out[:120])
    res = execute_sql(out)
    check("重锚后能取到数据", bool(res.get("success")) and int(res.get("row_count") or 0) > 0,
          "rows=%s (库最大日期 %s)" % (res.get("row_count"), mx))
    check("重锚有面向用户的说明", bool(notes), str(notes)[:100])


def test_window_days_no_collapse() -> None:
    """多 days 区间不得被压成同一区间（区间塌陷 → 必然 0 行）。"""
    from agent import llm_service as ls
    q = "最近30天各产线的投入数量"
    sql = ("SELECT a FROM t WHERE d >= CURRENT_DATE - INTERVAL '60 days' "
           "AND d < CURRENT_DATE - INTERVAL '30 days'")
    out = ls._fix_relative_window_days(q, sql)
    print("[4] 多区间：%s" % out[:130])
    check("多 days 区间不塌陷(下界保留60)", "INTERVAL '60 days'" in out, out[:130])
    # 单区间仍应归一（原能力）
    out1 = ls._fix_relative_window_days("最近30天的产量",
                                        "SELECT a FROM t WHERE d >= CURRENT_DATE - INTERVAL '29 days'")
    check("单 days 区间仍归一(原能力)", "INTERVAL '30 days'" in out1, out1)


def test_acl_no_bypass() -> None:
    """派生表 / CTE / 表别名 / JOIN 不得绕过列级 deny 与 mask（P0）。"""
    from security.enforcer import AclContext, rewrite_sql
    from db.executor import execute_sql

    r = execute_sql("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='mes_process_output' ORDER BY ordinal_position")
    if not r.get("success") or not r.get("rows"):
        check("权限测试取列", False, "information_schema 查询失败")
        return
    cols = [x["column_name"] for x in r["rows"]]
    if len(cols) < 2:
        check("权限测试取列", False, "列数不足")
        return
    mask_col, deny_col = cols[0], cols[1]

    acl = AclContext(
        username="regression_viewer", roles=["viewer"], superuser=False,
        allowed_tables={"mes_process_output"},
        column_denies={"mes_process_output": {deny_col}},
        column_masks={"mes_process_output": {mask_col: "partial_1_1"}},
    )

    def masked(sql: str) -> tuple:
        try:
            s2, err, _ap = rewrite_sql(sql, acl)
        except Exception as e:
            return ("EXC", repr(e)[:80])
        is_masked = ("CASE WHEN" in s2) and ("SUBSTRING" in s2 or "****" in s2 or "MD5" in s2.upper())
        return ("DENIED" if err else "ALLOW", is_masked, s2[:130], err[:90] if err else "")

    cases = [
        ("派生表", f"SELECT s.{mask_col} FROM (SELECT * FROM mes_process_output) s", "mask"),
        ("CTE", f"WITH x AS (SELECT * FROM mes_process_output) "
                f"SELECT x.{mask_col} FROM x", "mask"),
        ("表别名", f"SELECT f.{mask_col} FROM mes_process_output AS f", "mask"),
        ("两表JOIN含派生表",
         f"SELECT f.{mask_col} FROM mes_process_output f "
         f"JOIN (SELECT * FROM mes_process_output LIMIT 5) d ON f.line_id=d.line_id", "mask"),
        ("派生表取deny列",
         f"SELECT s.{deny_col} FROM (SELECT * FROM mes_process_output) s", "deny"),
        ("直接取deny列", f"SELECT {deny_col} FROM mes_process_output", "deny"),
    ]
    for label, sql, expect in cases:
        res = masked(sql)
        print("[5] %-18s %s" % (label, res[3] or res[0]))
        if expect == "mask":
            check("掩码不被%s绕过" % label, res[0] == "ALLOW" and res[1] is True, str(res)[:150])
        else:
            check("deny不被%s绕过" % label, res[0] == "DENIED", str(res)[:150])


# ── 4. 第二轮补充修复的回归护栏（2026-10-03 晚）──────────────────────
def test_abs_month_hint() -> None:
    """绝对月份问句 0 行时必须给出「库里没有这个月」的提示。

    背景：编译器对「各产线6月的产量」产出
      `stat_date >= (SELECT date_trunc('year', MAX(stat_date)) FROM t) + INTERVAL '5 months'`
    这本身就是数据驱动锚点，所以 0 行是**正确行为**（数据只到 8~9 月）。
    原实现 `_zero_row_data_range_hint` 只认相对时间词 → 绝对月份问句拿不到任何提示，
    用户只看到「0 行」不知道原因。本条锁住「只补提示、不改 SQL」这个决定。
    """
    from db.executor import execute_sql
    from agent import llm_service as ls

    R = ls._ABS_MONTH_RE
    for q, want in [("各产线6月的产量", True), ("2026年8月的库存", True), ("第3季度产量", True),
                    ("库存量是多少", False), ("产量大于5000的产线", False)]:
        check("绝对月份正则[%s]" % q, bool(R.search(q)) == want)
    check("相对时间问句仍命中(原能力)", bool(ls._ANY_TIME_ASK_RE.search("最近30天产量")))
    check("纯非时间问句不触发(原能力)", not ls._ANY_TIME_ASK_RE.search("产量排名前三"))

    hint = ls._zero_row_data_range_hint(execute_sql, ["mes_process_output"], "各产线6月的产量")
    print("[6] 绝对月份提示: %s" % (hint[:100] if hint else "(空)"))
    check("绝对月份 0 行有提示", bool(hint) and "6月" in hint, (hint or "(空)")[:120])
    check("非时间问句无提示(原行为)",
          ls._zero_row_data_range_hint(execute_sql, ["mes_process_output"], "库存量是多少") == "")


def test_insight_scan_acl_context() -> None:
    """insight_scan 的 worker 线程必须继承 ACL ContextVar（fail-open 修复）。"""
    import inspect
    from contextvars import ContextVar, copy_context
    from concurrent.futures import ThreadPoolExecutor
    from agent import insight_scan as isc

    src = inspect.getsource(isc.scan_table)
    check("insight_scan 用 copy_context 传播 ACL", "copy_context" in src)
    check("insight_scan 不再用 ex.map(会丢 ContextVar)", "ex.map" not in src)
    check("单任务异常不中断整批", "f.result()" in src)

    cv = ContextVar("regression_cv", default=None)
    cv.set("ACL_OBJ")
    with ThreadPoolExecutor(max_workers=1) as ex:
        without = ex.submit(lambda: cv.get()).result()
        _c = copy_context()
        with_ctx = ex.submit(lambda: _c.run(cv.get)).result()
    check("实测无 context 时 ACL 丢失", without is None, str(without))
    check("实测 copy_context 后 ACL 保留", with_ctx == "ACL_OBJ", str(with_ctx))


def test_fk_cache_not_pinned() -> None:
    """get_foreign_keys 查询失败时不得把空结果钉进缓存（对齐 get_real_tables 的 _trusted）。"""
    import inspect
    from db import tools as dtools

    s = inspect.getsource(dtools.get_foreign_keys)
    check("get_foreign_keys 有 trusted 分支", "trusted" in s and "not trusted" in s)
    check("失败路径不刷 TTL", s.count('_fk_cache["ts"] = time.time()') == 1, str(s.count('_fk_cache["ts"] = time.time()')))
    s2 = inspect.getsource(dtools._load_foreign_keys_uncached)
    check("_load 返回(结果, 可信) 二元组", "tuple[dict[str, list[dict]], bool]" in s2)
    check("异常返回不可信", "return {}, False" in s2)
    res, trusted = dtools._load_foreign_keys_uncached()
    check("真库调用返回二元组", isinstance(res, dict) and isinstance(trusted, bool),
          "表数=%d trusted=%s" % (len(res), trusted))
    check("真库真空键图判为可信", trusted is True, str(trusted))


def test_inventory_snapshot_caliber() -> None:
    """库存量口径必须是「最新快照日」，不是全历史累加（差约 46 倍）。

    真库实测（yans，45 个快照日）：全历史 SUM=3406851，最新快照日 SUM=73781。
    顺带锁住红线：表达快照日用的聚合标量子查询必须能过 _safe_metric_expr，
    且不能因此削弱注入防护。
    """
    from agent.metric_registry import get_all_metrics
    from agent.metric_compiler import _safe_metric_expr
    from db.executor import execute_sql

    ms = [m for m in get_all_metrics() if str(m.get("name")) == "库存量"]
    if not ms:
        check("注册表存在库存量", False, "未找到")
        return
    expr = ms[0].get("sql_expression") or ""
    print("[7] 库存量口径: %s" % expr[:110])
    check("库存量口径不是全历史裸 SUM", expr.strip() != "SUM(available_qty)", expr[:90])
    check("库存量口径含快照日过滤", "MAX(snapshot_date)" in expr, expr[:110])
    check("库存量口径能过红线", _safe_metric_expr(expr), expr[:110])

    r = execute_sql("SELECT " + expr + " AS v FROM inv_inventory_snapshot")
    row = (r.get("rows") or [{}])[0]
    v = row.get("v") if isinstance(row, dict) else None
    r_all = execute_sql("SELECT SUM(available_qty + COALESCE(frozen_qty,0)) AS v FROM inv_inventory_snapshot")
    row_all = (r_all.get("rows") or [{}])[0]
    v_all = row_all.get("v") if isinstance(row_all, dict) else None
    print("    最新快照日=%s 全历史=%s" % (v, v_all))
    check("库存量真库值小于全历史(证明已收敛)", isinstance(v, (int, float)) and v < (v_all or 0) * 0.5,
          "v=%s v_all=%s" % (v, v_all))

    # 红线安全回归：本轮为放行聚合标量子查询而调整过判据，注入防护不得削弱
    BLOCK = ["COUNT(*) WHERE result='fail'", "SUM(a) GROUP BY b", "SUM(a) ORDER BY b",
             "SUM(a) LIMIT 10", "SUM(a); DROP TABLE t", "SUM(a)--x", "SUM(a)/*x*/",
             "SUM((SELECT x FROM secrets))", "(SELECT password FROM users)",
             "SUM(a) UNION SELECT b FROM t", "SUM(a) JOIN t ON 1=1", "TRUNCATE TABLE t"]
    for e in BLOCK:
        check("红线拦[%s]" % e[:26], _safe_metric_expr(e) is False, "竟然放行")
    ALLOW = ["SUM(good_qty)", "SUM(COALESCE(good_qty,0))", "SUM(CASE WHEN a THEN 1 ELSE 0 END)",
             "SUM(a)/NULLIF(COUNT(b),0)", "AVG(x)", "COUNT(*)",
             "(SELECT MAX(d) FROM t)"]
    for e in ALLOW:
        check("红线放[%s]" % e[:26], _safe_metric_expr(e) is True, "被误拒")


# ── 主流程 ────────────────────────────────────────────────────────────
def main() -> int:
    # 数据库配置由 database 模块自行从 .env 加载（config.py 只导出 LLM 相关），
    # 这里直接用它的当前连接即可，不需要手动 switch_database。
    import database  # noqa: F401  确保 .env 被加载

    # 连通性预检：无库时 SKIP 而非 FAIL（避免 CI/新机器假红）
    from db.executor import execute_sql
    try:
        probe = execute_sql("SELECT 1 AS ok")
        if not probe.get("success"):
            raise RuntimeError(str(probe.get("error")))
    except Exception as e:
        print("!! 数据库不可用（%s）→ 本测试 SKIP，不计入失败" % e)
        print("   需要真实 PostgreSQL（读 backend/.env 的 DB_* 配置）。")
        return 0
    try:
        from database import get_db_type
        print("数据库: %s（数据来自 backend/.env）" % get_db_type())
    except Exception:
        pass

    tests = [test_compile_and_execute]
    if not QUICK:
        tests.insert(1, test_baseline_no_regression)
    tests += [test_data_range_reanchor, test_window_days_no_collapse, test_acl_no_bypass,
              test_abs_month_hint, test_insight_scan_acl_context,
              test_fk_cache_not_pinned, test_inventory_snapshot_caliber]

    for t in tests:
        try:
            t()
        except Exception as e:
            check("%s 未抛异常" % t.__name__, False, "%s: %s" % (type(e).__name__, str(e)[:120]))

    passed = sum(1 for _n, c, _d in _RESULTS if c)
    failed = [(n, d) for n, c, d in _RESULTS if not c]
    print("\n" + "=" * 72)
    print("断言 PASS %d / FAIL %d" % (passed, len(failed)))
    for n, d in failed:
        print("  FAIL %s | %s" % (n, d))
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
