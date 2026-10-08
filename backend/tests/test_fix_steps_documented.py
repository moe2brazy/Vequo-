# -*- coding: utf-8 -*-
"""改写链与守卫的注册完整性（第 17 轮二轮审计固化）。
查四类"注册漏项"——它们都不会报错，只会静默少做事：
 ① 每个在改写链里的 _fix_* 都必须在 _FIX_STEP_MSG 有说明（否则用户看不到口径调整）
 ② 每个在 _SUB_GUARDS 里的守卫都必须在主链被调用（否则守卫形同虚设）
 ③ 单位词表三张表必须同步（守卫有、修复无 = 拦得住修不出）
 ④ 缺源三表必须对齐（规则/类别/源字段，错位会让守卫拿错特征比对）
"""
import ast, os, re, sys
BACKEND = r"D:\vequo\vequo-viqueo\backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.stdout.reconfigure(encoding="utf-8")
import agent.llm_service as M

SRC = os.path.join(BACKEND, "agent", "llm_service.py")
src = open(SRC, encoding="utf-8").read()
tree = ast.parse(src)
fail = 0

print("=" * 74)
print("① 改写链步骤必须在 _FIX_STEP_MSG 有说明")
print("=" * 74)
steps = sorted(set(re.findall(r'\("(_\w+)",\s*\n?\s*lambda', src)))
print("  改写链步骤 %d 个" % len(steps))
miss = [s for s in steps if s not in (M._FIX_STEP_MSG or {})]
if miss:
    fail += 1
    print("  ★ 缺说明: %s" % miss)
    print("    ⇒ 触发后用户看不到「口径已调整」提示，只看到数字变了")
else:
    print("  OK 全部有说明")

print()
print("=" * 74)
print("② _SUB_GUARDS 必须在主链被调用")
print("=" * 74)
sub = None
for node in tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if getattr(t, "id", None) == "_SUB_GUARDS":
                sub = [e.value for e in node.value.elts]
calls = {x.func.id for x in ast.walk(tree)
         if isinstance(x, ast.Call) and getattr(x.func, "id", "") in (sub or [])}
missing = [s for s in (sub or []) if s not in calls]
print("  清单 %d / 主链 %d" % (len(sub or []), len(calls)))
if missing:
    fail += 1
    print("  ★ 清单有但主链没调: %s" % missing)
else:
    print("  OK 一致")

print()
print("=" * 74)
print("③ 单位词表三表同步")
print("=" * 74)
g = {w for w, _ in M._UNIT_PER}
f = {w for w, _, _, _ in M._UNIT_FIX_RULES}
s_ = set(M._UNIT_STACKABLE)
if g - f:
    fail += 1
    print("  ★ 守卫有、修复无（拦得住修不出）: %s" % sorted(g - f))
else:
    print("  OK 守卫 ⊆ 修复")
if f - g:
    fail += 1
    print("  ★ 修复有、守卫无（修得动拦不住）: %s" % sorted(f - g))
else:
    print("  OK 修复 ⊆ 守卫")
if s_ - f:
    fail += 1
    print("  ★ 可叠加表有、主规则表无: %s" % sorted(s_ - f))
else:
    print("  OK 可叠加 ⊆ 主规则")

print()
print("=" * 74)
print("④ 缺源三表对齐")
print("=" * 74)
n_r, n_c = len(M._UNDERIVABLE_METRIC_RULES), len(M._UNREG_CAT_ORDER)
if n_r != n_c:
    fail += 1
    print("  ★ 长度不等: 规则 %d ≠ 类别 %d ⇒ 「命中第N条→取第N类」错位" % (n_r, n_c))
else:
    print("  OK 长度一致（%d）" % n_r)
for i in range(min(n_r, n_c)):
    cat = M._UNREG_CAT_ORDER[i]
    if cat not in M._UNREG_NEED_SOURCE:
        fail += 1
        print("  ★ 规则[%d] 类别 %s 在 _UNREG_NEED_SOURCE 里缺键" % (i, cat))
else:
    print("  OK 各类别均有源字段特征")

print()
print("=" * 74)
print("⑤ 死代码：今日新增符号必须被引用")
print("=" * 74)
for sym in ("_rows_effectively_empty", "_unit_conversion_reason",
            "_fix_unit_conversion", "_ratio_denominator_has_defect_reason",
            "_time_condition_missing_reason", "_scale_factor",
            "_ROLL_RATIO_ASK_RE_NARROW", "_UNIT_FIX_RULES"):
    n = len(re.findall(r"\b%s\b" % re.escape(sym), src))
    if n <= 1:
        fail += 1
        print("  ★ %-36s 死代码（%d 次）" % (sym, n))
    else:
        print("  OK %-36s %d 次" % (sym, n))

print()
print("结果: %s" % ("ALL PASS" if not fail else "FAIL=%d" % fail))
sys.exit(1 if fail else 0)