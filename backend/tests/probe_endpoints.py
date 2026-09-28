# -*- coding: utf-8 -*-
"""全量 API 端点进程内探测（FastAPI TestClient，不依赖真实 HTTP/沙箱网络）。
逐一调用 main.py 注册的所有 GET 端点（登录态），定位 5xx 真异常。"""
import sys, os, re, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
import main as appmod
app = appmod.app
client = TestClient(app)

# 登录拿 token
r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
token = (r.json() or {}).get("token") or (r.json() or {}).get("access_token")
h = {"Authorization": f"Bearer {token}"} if token else {}
print("登录:", bool(token), flush=True)

# 提取 GET 端点
eps = set()
for src in ["main.py"] + [f"routers/{f}" for f in os.listdir("routers") if f.endswith(".py")]:
    try:
        s = open(src, encoding="utf-8").read()
    except Exception:
        continue
    for m in re.finditer(r"@(?:app|router)\.(get)\(\"([^\"]+)\"", s):
        eps.add(m.group(2))

SAMPLES = {"table_name": "mes_work_order", "username": "admin", "rule_id": "1", "metric": "产量",
           "memory_id": "m1", "nid": "1", "cid": "1", "sid": "s1", "rid": "r1", "source_id": "s1",
           "term:path": "test", "key:path": "k", "key": "k", "req_id": "1", "fid": "f1", "model": "gpt",
           "table": "mes_work_order", "id": "1", "name": "产量"}

ok = bad = err = other = skipped = 0
bad_list = []
for path in sorted(eps):
    filled = path
    skip = False
    for ph in re.findall(r"\{([^}]+)\}", path):
        if ph in SAMPLES:
            filled = filled.replace("{" + ph + "}", SAMPLES[ph])
        else:
            skip = True
    if skip:
        skipped += 1
        continue
    try:
        resp = client.get(filled, headers=h, timeout=30)
        st = resp.status_code
        body = resp.text[:120]
    except Exception as e:
        st = -1
        body = f"{type(e).__name__}: {str(e)[:80]}"
    if 200 <= st < 300:
        ok += 1
    elif st >= 500:
        bad += 1
        bad_list.append((filled, st, body))
        print(f"  🔴5xx {filled} → {st} {body}", flush=True)
    elif st == -1:
        err += 1
        print(f"  💥异常 {filled} → {body}", flush=True)
    else:
        other += 1
print(f"\n═══ GET 端点探测: 共 {len(eps)} 端点 ═══", flush=True)
print(f"✅ 2xx={ok}  🔴 5xx={len(bad_list)}  💥 异常={err}  ⚠️ 4xx={other}  ⏭️ 跳过={skipped}", flush=True)
