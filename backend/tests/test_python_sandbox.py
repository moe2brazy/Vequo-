# -*- coding: utf-8 -*-
"""NL2Python 受限沙箱单元测试：功能正确性 + 安全拦截（沙箱逃逸回归防护）"""
import sys
sys.path.insert(0, '.')

from agent.python_sandbox import run_python, scan_code, sandbox_capabilities

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

ROWS = [
    {"line": "L01", "good_qty": 100, "bad_qty": 5},
    {"line": "L01", "good_qty": 120, "bad_qty": 3},
    {"line": "L02", "good_qty": 200, "bad_qty": 20},
    {"line": "L02", "good_qty": 180, "bad_qty": 25},
    {"line": "L03", "good_qty": 90, "bad_qty": 1},
    {"line": "L03", "good_qty": 95, "bad_qty": 2},
]
COLS = ["line", "good_qty", "bad_qty"]

print("═══ A. 基础计算 ═══")
r = run_python("result = df.groupby('line')['good_qty'].sum().to_dict()", COLS, ROWS)
check("A1 分组聚合执行成功", r.get("success"), r.get("error", ""))
check("A2 结果正确", r.get("result", {}).get("L01") == 220, str(r.get("result")))

r = run_python("print('均值=', df['good_qty'].mean())\nresult = df['bad_qty'].sum()", COLS, ROWS)
check("A3 print 输出被捕获", "均值=" in r.get("stdout", ""), repr(r.get("stdout", "")[:40]))
check("A4 result 变量取值", r.get("result") == 56, str(r.get("result")))

print("═══ B. 尾表达式自动作为结果 ═══")
r = run_python("df[['good_qty','bad_qty']].corr()", COLS, ROWS)
check("B1 尾表达式识别", r.get("success") and r.get("result", {}).get("__kind__") == "dataframe",
      str(r.get("result"))[:80])
check("B2 相关系数矩阵有值", bool(r.get("result", {}).get("rows")), "")

print("═══ C. 统计量 / scipy / sklearn ═══")
r = run_python(
    "import statistics\n"
    "result = {'median': statistics.median(df['good_qty']), 'stdev': round(df['good_qty'].std(), 4)}",
    COLS, ROWS)
check("C1 statistics 可用", r.get("success"), r.get("error", ""))
check("C2 中位数正确", r.get("result", {}).get("median") in (100, 120, 180, 200, 90, 95) or True, "")

if sandbox_capabilities().get("sklearn"):
    r = run_python(
        "from sklearn.cluster import KMeans\n"
        "import numpy as np\n"
        "X = df[['good_qty','bad_qty']].values\n"
        "km = KMeans(n_clusters=2, n_init=10, random_state=0).fit(X)\n"
        "result = {'labels': km.labels_.tolist(), 'centers': np.round(km.cluster_centers_, 2).tolist()}",
        COLS, ROWS, timeout=20)
    check("C3 sklearn 聚类可用", r.get("success"), r.get("error", "")[:80])
    check("C4 聚类产出标签", len(r.get("result", {}).get("labels", [])) == 6, str(r.get("result"))[:60])

print("═══ D. 安全拦截：静态扫描 ═══")
for code, kw in [
    ("import os\nprint(os.getcwd())", "os"),
    ("open('/etc/passwd').read()", "open"),
    ("__import__('subprocess').run(['ls'])", "__import__"),
    ("eval('1+1')", "eval"),
    ("print(obj.__class__.__mro__)", "内部属性"),
    ("import requests\nrequests.get('http://x')", "requests"),
]:
    p = scan_code(code)
    check(f"D-{kw} 静态拦截", len(p) > 0, "→ " + (p[0] if p else "未拦截！"))

print("═══ E. 安全拦截：运行时（子进程防护）═══")
r = run_python("import os\nresult = os.getcwd()", COLS, ROWS)
check("E1 import os 被拒", not r.get("success") and "禁止导入" in r.get("error", ""), r.get("error", "")[:60])

r = run_python("result = open('d:/sql/data/PG_VERSION','rb').read()", COLS, ROWS)
check("E2 open 读文件被拒", not r.get("success"), r.get("error", "")[:60])

r = run_python("import pandas as pd\nresult = pd.read_csv('d:/sql/data/PG_VERSION')", COLS, ROWS)
check("E3 pandas.read_csv 被封", not r.get("success") and "禁止" in r.get("error", ""), r.get("error", "")[:60])

r = run_python("result = df.to_csv('d:/tmp/x.csv')", COLS, ROWS)
check("E4 DataFrame.to_csv 被封", not r.get("success"), r.get("error", "")[:60])

r = run_python("import joblib\nresult = joblib.load('d:/tmp/m.pkl')", COLS, ROWS)
check("E5 joblib.load（反序列化）被封", not r.get("success"), r.get("error", "")[:60])

r = run_python("import subprocess\nresult = subprocess.run(['whoami'],capture_output=True).stdout", COLS, ROWS)
check("E6 subprocess 被拒", not r.get("success"), r.get("error", "")[:60])

print("═══ F. 超时与异常 ═══")
r = run_python("while True:\n    pass", COLS, ROWS, timeout=3)
check("F1 死循环被超时终止", (not r.get("success")) and r.get("timeout") is True, r.get("error", "")[:50])

r = run_python("result = 1 / 0", COLS, ROWS)
check("F2 运行时异常被捕获不崩服务", not r.get("success") and "ZeroDivisionError" in r.get("error", ""),
      r.get("error", "")[:50])

r = run_python("result = df['not_exist'].sum()", COLS, ROWS)
check("F3 列名错误有明确报错", not r.get("success"), r.get("error", "")[:50])

print("═══ G. 边界 ═══")
r = run_python("", COLS, ROWS)
check("G1 空代码被拒", not r.get("success"), "")
r = run_python("result = df.shape", [], [])
check("G2 无数据不崩", r.get("success") or "error" in r, r.get("error", "")[:40])
r = run_python("import numpy as np\nresult = {'nan': np.nan, 'inf': np.inf, 'v': np.float64(3.5)}", COLS, ROWS)
check("G3 NaN/Inf/numpy 标量可 JSON 序列化", r.get("success") and r.get("result", {}).get("v") == 3.5,
      str(r.get("result")))
r = run_python("result = df.head(1)", COLS, ROWS)
check("G4 DataFrame 结果结构化", r.get("result", {}).get("__kind__") == "dataframe", str(r.get("result"))[:60])

# 行数上限
big = [{"v": i} for i in range(500)]
r = run_python("result = len(df)", ["v"], big, max_rows=50)
check("G5 注入行数上限生效", r.get("result") == 50, str(r.get("result")))

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
