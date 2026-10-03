# -*- coding: utf-8 -*-
"""NL2Python 受限执行沙箱（对标 Microsoft Fabric Code Interpreter / Vanna Python 沙箱 /
Smartbi 库外 Python 引擎 / ThoughtSpot 可检查 Python）。

竞品共识（本模块据此设计）：
  - 代码**可见可审**：生成的代码先给用户看，确认后才执行（Vanna / ThoughtSpot 的
    "可检查 Python"）；本产品把这道闸门做成 API 级二次确认，与「未注册口径 →
    human-in-loop 二次确认」的既有约定保持一致。
  - 库外计算：SQL 算不出的（相关性矩阵、分位数、聚类、假设检验、异常值检测、
    分布拟合…）在库外用 pandas / numpy / scipy / sklearn 算，不塞进 SQL。
  - 沙箱隔离：独立进程 + 超时 + 导入白名单 + 内建裁剪 + IO 封禁（Fabric / Vanna）。

定位边界（重要）：本模块**不替代 SQL**。确定性编译仍是主路径，Python 只承接
「SQL 表达不了或表达起来极不自然」的统计计算，且全程只读、无写回、无网络。

对外 API：
  - scan_code(code)            → 静态安全预检，返回违规说明列表（空=通过）
  - run_python(code, cols, rows) → 在子进程执行，返回 {success, stdout, result, error}
  - sandbox_capabilities()     → 可用包清单（供代码生成 prompt 声明能力边界）
"""

import json
import os
import re
import subprocess
import sys

_RUNNER = os.path.join(os.path.dirname(__file__), "_sandbox_runner.py")

# ── 静态安全预检：快速失败，不浪费一次子进程 ──────────────────
# 说明：真正的权威防线是子进程里的受控 __import__ 与内建裁剪；这里只做
# 低成本的提前拦截，并把拒绝原因用中文讲清楚，方便用户改代码后重试。
_DANGER_PATTERNS: list[tuple[str, str]] = [
    (r"__import__", "禁止使用 __import__"),
    (r"\bopen\s*\(", "禁止文件读写（open）"),
    (r"\beval\s*\(", "禁止 eval"),
    (r"\bexec\s*\(", "禁止 exec"),
    (r"\bcompile\s*\(", "禁止 compile"),
    (r"\binput\s*\(", "禁止交互式 input"),
    (r"\bbreakpoint\s*\(", "禁止 breakpoint"),
    (r"\bglobals\s*\(\s*\)", "禁止访问 globals()"),
    # 安全修复（P0）：动态属性访问是"字符串拼接绕过 dunder 正则"的跳板，提前给出清晰中文提示
    # （子进程侧已把 getattr/setattr/vars/globals/locals 换成拒绝函数，这里只是改善报错体验）
    (r"\bgetattr\s*\(", "禁止使用 getattr（沙箱逃逸风险）"),
    (r"\bsetattr\s*\(", "禁止使用 setattr（沙箱逃逸风险）"),
    (r"\bvars\s*\(", "禁止使用 vars()（沙箱逃逸风险）"),
    (r"\bsubprocess\b", "禁止启动子进程"),
    (r"\bsocket\b", "禁止网络访问"),
    (r"\brequests\b", "禁止网络请求"),
    (r"\burllib\b", "禁止网络请求"),
    (r"\bhttp\b", "禁止网络访问"),
    (r"\bshutil\b", "禁止文件系统操作"),
    (r"\bpathlib\b", "禁止路径操作"),
    (r"\bpickle\b", "禁止反序列化"),
    (r"\bjoblib\b", "禁止反序列化"),
    (r"\bmarshal\b", "禁止反序列化"),
    (r"\bctypes\b", "禁止调用原生库"),
    (r"\bsqlite3\b", "禁止数据库直连"),
    (r"\bmultiprocessing\b", "禁止多进程"),
    (r"\bthreading\b", "禁止多线程"),
    (r"\bsignal\b", "禁止信号操作"),
    (r"__(class__|bases__|subclasses__|globals__|mro__|dict__|code__|builtins__|loader__|getattribute__|reduce__|init__)",
     "禁止访问对象内部属性（沙箱逃逸风险）"),
]

# import / from ... import 的模块级黑名单（白名单之外的模块在子进程里也会被拒，
# 这里只是提前给出更清晰的提示）
_BLOCKED_IMPORTS = (
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "pickle", "joblib",
    "marshal", "ctypes", "sqlite3", "multiprocessing", "threading", "signal",
    "pty", "platform", "getpass", "webbrowser", "requests", "urllib", "http",
    "ftplib", "smtplib", "telnetlib", "xmlrpc", "ssl", "select", "selectors",
    "asyncio", "importlib", "builtins", "code", "codeop", "mmap", "resource",
    "termios", "pwd", "grp", "tempfile", "glob", "fileinput", "linecache",
)

# 白名单包内的危险子模块（`import scipy.io` / `from scipy import io` 两种写法都要拦）
_BLOCKED_SUBMODULES = (
    "scipy.io", "sklearn.datasets", "numpy.ctypeslib", "numpy.f2py",
    "numpy.distutils", "numpy.lib.format", "numpy.lib.npyio",
    "pandas.io", "pandas.util", "pandas.testing", "pandas.compat",
)
_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+([A-Za-z_][\w.]*)|from\s+([A-Za-z_][\w.]*)\s+import\b)",
    re.MULTILINE,
)


def _is_blocked_module(full: str) -> bool:
    """完整模块路径是否命中黑名单（root 命中或子模块前缀命中）。"""
    root = (full or "").split(".")[0]
    if root in _BLOCKED_IMPORTS:
        return True
    return any(full == b or full.startswith(b + ".") for b in _BLOCKED_SUBMODULES)


def scan_code(code: str) -> list[str]:
    """静态安全预检。返回违规说明列表，空列表表示通过。"""
    if not (code or "").strip():
        return ["代码为空"]
    problems: list[str] = []
    for pat, reason in _DANGER_PATTERNS:
        if re.search(pat, code):
            problems.append(reason)
    for m in _IMPORT_RE.finditer(code):
        mod = (m.group(1) or m.group(2) or "").split(".")[0]
        if mod in _BLOCKED_IMPORTS:
            problems.append("禁止导入模块 %s" % mod)
        full = (m.group(1) or m.group(2) or "")
        if _is_blocked_module(full):
            problems.append("禁止导入模块 %s" % full)
    # 去重保序
    seen, out = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def sandbox_capabilities() -> dict:
    """探测沙箱内可用的计算包（供代码生成 prompt 声明能力边界）。"""
    caps = {}
    for mod in ("pandas", "numpy", "scipy", "sklearn", "statistics"):
        try:
            __import__(mod)
            caps[mod] = True
        except Exception:
            caps[mod] = False
    return caps


def _kill_tree(p: subprocess.Popen) -> None:
    """尽力终止进程树（Windows 下 kill() 只杀直接子进程，必要时 taskkill /T）。

    2026-10-03 修复（P0）：原实现里 `p.wait(timeout=3)` 成功后紧跟一个 `return`，
    而 `p.kill()` 发出后子进程数秒内必然退出 → wait **正常返回** → 下面的
    `taskkill /F /T` 是**永远执行不到的死代码**，「杀进程树」实际只杀直接子进程，
    孙进程（模型自己 spawn 的）会存活并继续占用 CPU/内存。
    现在：无论 wait 成功与否，只要进程还活着就走 taskkill /T。
    """
    try:
        p.kill()
    except Exception:
        pass
    try:
        p.wait(timeout=3)
    except Exception:
        pass
    # 复查：只有确实还活着（或退出码拿不到）才需要 taskkill /T
    try:
        if p.poll() is None and os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, timeout=5)
    except Exception:
        pass


def run_python(code: str, columns: list, rows: list,
               timeout: float = None, max_rows: int = None) -> dict:
    """在受限子进程中执行 Python 代码。

    columns/rows 为只读注入的数据（来自已执行的 SQL 结果），代码只能读取，
    没有任何数据库连接或写回通道。返回 {success, stdout, result, error, elapsed_ms}。
    """
    try:
        from config import SANDBOX_TIMEOUT_SEC, SANDBOX_MAX_ROWS
    except Exception:
        SANDBOX_TIMEOUT_SEC, SANDBOX_MAX_ROWS = 8.0, 20000
    timeout = float(timeout or SANDBOX_TIMEOUT_SEC)
    max_rows = int(max_rows or SANDBOX_MAX_ROWS)

    problems = scan_code(code)
    if problems:
        return {"success": False, "stdout": "", "result": None,
                "error": "静态安全检查未通过：" + "；".join(problems),
                "blocked": True, "elapsed_ms": 0}

    rows = list(rows or [])
    if len(rows) > max_rows:
        rows = rows[:max_rows]

    import time
    t0 = time.time()
    payload = json.dumps({"code": code, "columns": list(columns or []), "rows": rows},
                         ensure_ascii=False, default=str)
    # 安全修复（P0）：禁止把父进程的完整环境变量继承给不可信子进程。
    # 原实现 `env = dict(os.environ)` 会把 LLM_API_KEY / DB_PASSWORD 等全部密钥灌入沙箱，
    # 配合沙箱逃逸（见 _sandbox_runner 的 builtins 裁剪）即可被用户代码直接读走。
    # 这里改为最小白名单：只保留解释器与编码所需变量。
    _ALLOW_ENV = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
                  "LANG", "LC_ALL", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
    env = {k: os.environ[k] for k in _ALLOW_ENV if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"   # 避免写 __pycache__（本机策略可能拦截）
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        p = subprocess.Popen(
            [sys.executable, _RUNNER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=env,
        )
    except Exception as e:
        return {"success": False, "stdout": "", "result": None,
                "error": "沙箱启动失败: %s" % e, "elapsed_ms": int((time.time() - t0) * 1000)}

    try:
        out, err = p.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        # 2026-10-03 修复（P0）：超时路径不回收 PIPE —— 实测每次超时泄漏 2 个
        # 管道句柄（stdout/stderr），反复触发「执行超时」会单调增长。
        for _s in (p.stdin, p.stdout, p.stderr):
            try:
                if _s is not None:
                    _s.close()
            except Exception:
                pass
        return {"success": False, "stdout": "", "result": None,
                "error": "执行超时（超过 %.0f 秒），已强制终止。请简化计算或减少数据量。" % timeout,
                "timeout": True, "elapsed_ms": int((time.time() - t0) * 1000)}

    elapsed = int((time.time() - t0) * 1000)
    out = (out or "").strip()
    try:
        data = json.loads(out)
        data["elapsed_ms"] = elapsed
        return data
    except Exception:
        # 子进程崩了 / 输出不是 JSON：把 stderr 尾巴回传，便于排查
        tail = ((err or "").strip().splitlines() or [""])[-1][:300]
        return {"success": False, "stdout": "", "result": None,
                "error": "沙箱执行异常：%s" % (tail or "无输出"),
                "elapsed_ms": elapsed}
