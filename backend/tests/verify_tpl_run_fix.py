# -*- coding: utf-8 -*-
"""验证「业务知识 → 分析模板 → 一键生成报告」修复效果。
2026-10-07 实测 bug：8 个模板 100% HTTP 500，
根因是run_knowledge_template 签名缺 `request: Request`，函数体却引用了它
⇒ NameError。顺带修另两处同类问题（get_knowledge_health / export_knowledge）。
"""
import os, sys, time
BACKEND = r"D:\vequo\vequo-viqueo\backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.stdout.reconfigure(encoding="utf-8")
import httpx
import auth

B = "http://127.0.0.1:8010/api"
H = {"Authorization": "Bearer " + auth.create_token("admin", "admin")}

print("=" * 74)
print("① 8 个分析模板逐个生成报告")
print("=" * 74)
d = httpx.get(B + "/knowledge/templates", headers=H, timeout=30).json()
ts = d.get("templates") or d.get("data") or []
bad = []
for t in ts:
    tid = t.get("template_id") or t.get("id")
    t0 = time.time()
    try:
        r = httpx.post(B + "/knowledge/templates/%s/run" % tid, headers=H, timeout=90)
        el = time.time() - t0
        if r.status_code != 200:
            bad.append((tid, t.get("name"), "HTTP %s %s" % (r.status_code, r.text[:90])))
            print("  ★ %-9s %-16s HTTP %s %5.1fs | %s"
                  % (tid, t.get("name"), r.status_code, el, r.text[:90]))
            continue
        j = r.json()
        n = len(j.get("html") or "")
        steps = j.get("steps") or []
        oksteps = sum(1 for s in steps if s.get("status") in ("ok", "success"))
        # html 太短说明是「正在生成」占位页或空壳
        good = bool(j.get("success")) and n > 2000
        if not good:
            bad.append((tid, t.get("name"), "success=%s html=%d" % (j.get("success"), n)))
        print("  %s %-9s %-16s %5.1fs | html=%-6d 步骤 %d/%d 成功"
              % ("OK  " if good else "★", tid, t.get("name"), el, n, oksteps, len(steps)))
    except Exception as e:
        bad.append((tid, t.get("name"), "异常 %s" % str(e)[:80]))
        print("  ★ %-9s %-16s 异常 %s" % (tid, t.get("name"), str(e)[:80]))

print()
print("=" * 74)
print("② 另两处同类修复（原本也会 500）")
print("=" * 74)
for label, meth, url in [
    ("get_knowledge_health", "GET", B + "/knowledge/health"),
    ("export_knowledge(terms/md)", "GET", B + "/knowledge/export?scope=terms&format=md"),
]:
    try:
        r = httpx.request(meth, url, headers=H, timeout=40)
        mark = "OK  " if r.status_code == 200 else ("★403" if r.status_code == 403 else "★")
        print("  %s %-26s HTTP %s  %s" % (mark, label, r.status_code, r.text[:70].replace("\n", " ")))
    except Exception as e:
        print("  ★  %-26s 异常 %s" % (label, str(e)[:70]))

print()
print("=" * 74)
if bad:
    print("★ FAIL 仍有 %d 个模板/接口异常：" % len(bad))
    for tid, name, why in bad:
        print("   - %s %s：%s" % (tid, name, why))
    sys.exit(1)
if not ts:
    print("★ FAIL 一个模板都没测到 —— 验证无效")
    sys.exit(2)
print("结果: ALL PASS（%d 个模板 + 2 个关联接口全部正常）" % len(ts))
sys.exit(0)