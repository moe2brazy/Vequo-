# -*- coding: utf-8 -*-
"""NL2Python 沙箱 —— 子进程执行入口（内部模块，请勿直接调用）。

由 agent/python_sandbox.py 通过 `python -m agent._sandbox_runner` 拉起：
stdin 读 JSON 载荷 {code, columns, rows}，stdout 输出 JSON 结果。

为什么必须在子进程里跑：用户代码可能死循环、爆内存、崩解释器，
放在主服务进程里会拖垮整个服务。子进程 + 硬超时 + 强制 kill 是最后一道防线。

四层防护（纵深，任一单层被绕过仍有下一层）：
  1. 受控 __import__：用户代码的所有 import 走白名单守卫，os/sys/socket 等直接拒绝。
     模块内部的依赖导入仍走真实 __import__（用的是真实 builtins），因此 pandas 等
     复杂包的内部 import 链不受影响 —— 这是"只拦用户、不拦依赖"的关键。
  2. 受限 builtins：exec 时注入裁剪过的 builtins，移除 open/exec/eval/compile/
     input/breakpoint，并把 open 替换成抛异常。
  3. IO 入口封禁：pandas 读写文件/数据库、numpy 文件 IO、joblib 反序列化入口
     在子进程内被替换为拒绝函数（子进程一次性，可安全地全局打补丁）。
  4. 输出与体积限制：stdout 截断、结果需可 JSON 序列化、注入行数上限。
"""

import sys
import io
import ast
import json
import builtins as _bi

# ── 导入白名单：只放行科学计算与纯计算相关模块 ────────────────
ALLOWED_MODULES = {
    "pandas", "numpy", "scipy", "sklearn",
    "math", "statistics", "cmath", "numbers", "decimal", "fractions", "random",
    "itertools", "functools", "operator", "collections", "heapq", "bisect",
    "datetime", "calendar", "time", "json", "re", "string", "textwrap",
    "unicodedata", "difflib", "array", "copy", "pprint", "dataclasses",
    "typing", "enum", "abc", "warnings", "contextlib",
}

# ── 白名单包内的危险子模块（沙箱逃逸路径）：
#    scipy.io / sklearn.datasets 可读写磁盘文件；
#    numpy.ctypeslib 可加载原生 DLL；numpy.f2py/distutils 可编译原生码；
#    pandas.io 是 read_sql/read_parquet 等底层 IO 入口。
#    检查对象是「完整模块路径」，覆盖 `import scipy.io` 与 `from scipy import io` 两种写法。
_BANNED_SUBMODULES = {
    "scipy.io", "sklearn.datasets",
    "numpy.ctypeslib", "numpy.f2py", "numpy.distutils", "numpy.lib.format",
    "numpy.lib.npyio", "numpy.ma.mrecords", "pandas.io", "pandas.util",
    "pandas.testing", "pandas.compat", "pandas._libs",
}

_real_import = _bi.__import__


def _deny(*_a, **_k):
    raise PermissionError("沙箱禁止文件 / 网络 / 反序列化操作")


def _deny_attr(*_a, **_k):
    """安全修复（P0）：动态属性/命名空间访问是沙箱逃逸的跳板，一律拒绝。"""
    raise PermissionError("沙箱禁止使用 getattr/setattr/vars/globals/locals（沙箱逃逸风险）")


def _is_banned_module(full: str) -> bool:
    """完整模块路径是否命中黑名单（前缀匹配，覆盖子包内层）。"""
    return any(full == b or full.startswith(b + ".") for b in _BANNED_SUBMODULES)


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    """用户代码可见的 __import__：白名单之外一律拒绝；白名单内的危险子模块同样拒绝。

    `from scipy import io` 场景下 name 只有 "scipy"，真正的目标是 fromlist 里的 "io"，
    因此必须同时检查 name 与 name + fromlist 组合出的完整路径。
    """
    root = (name or "").split(".")[0]
    if root not in ALLOWED_MODULES:
        raise ImportError(
            "沙箱禁止导入模块：%s（仅允许 %s）" % (name, "、".join(sorted(ALLOWED_MODULES)))
        )
    if _is_banned_module(name):
        raise ImportError("沙箱禁止导入模块：%s（文件/网络/反序列化逃逸风险）" % name)
    for f in (fromlist or ()):
        if isinstance(f, str):
            sub = f"{name}.{f}"
            if _is_banned_module(sub):
                raise ImportError("沙箱禁止导入模块：%s（文件/网络/反序列化逃逸风险）" % sub)
    return _real_import(name, globals, locals, fromlist, level)


def _safe_builtins() -> dict:
    """裁剪内置命名空间：移除逃逸/IO 相关内建，open 换成拒绝函数。

    安全修复（P0）：原先只删了 open/exec/eval 等，保留了 getattr/setattr/vars/globals/locals，
    而静态预检是靠"正则匹配 __builtins__ 等 dunder 文本"实现的 —— 字符串拼接即可绕过：
        getattr(pd, "__bui" "ltins__")["__im" "port__"]("o" "s")
    实测可拿到 os 模块并执行任意命令。
    动态构造属性名只有这几条路：getattr/setattr/delattr/vars/globals/locals/__dict__。
    字面量的 __dict__ / __builtins__ / __class__ 等已被 _DANGER_PATTERNS 拦截，
    因此移除上述动态入口即可闭合"拼接绕过"这条链路。
    """
    safe = dict(vars(_bi))
    for n in ("open", "exec", "eval", "compile", "input", "breakpoint",
              "exit", "quit", "help", "memoryview", "__import__",
              # 动态属性/命名空间访问入口（沙箱逃逸跳板）
              # 说明：dir() 保留 —— 它只能列出属性名，无法访问属性值，不构成逃逸路径，
              # 而 LLM 生成的数据分析代码常用 dir()/df.columns 做探查，去掉会误伤。
              "getattr", "setattr", "delattr", "vars", "globals", "locals",
              "super", "__build_class__", "__loader__", "__spec__",
              "classmethod", "staticmethod", "property"):
        safe.pop(n, None)
    safe["__import__"] = _guarded_import
    safe["open"] = _deny
    safe["getattr"] = _deny_attr
    safe["setattr"] = _deny_attr
    safe["delattr"] = _deny_attr
    safe["vars"] = _deny_attr
    safe["globals"] = _deny_attr
    safe["locals"] = _deny_attr
    return safe


def _patch_module_io(mod, func_names) -> None:
    """把模块内指定的 IO/反序列化函数替换为拒绝函数（防绕过 import 黑名单的路径）。"""
    for fn in func_names:
        try:
            if hasattr(mod, fn):
                setattr(mod, fn, _deny)
        except Exception:
            pass


def _harden_io() -> None:
    """封禁 pandas / numpy / joblib / scipy / sklearn 的文件与反序列化入口。

    子进程是一次性的，这里对类做全局打补丁不会影响主服务。
    """
    try:
        import pandas as pd
        for fn in ("read_csv", "read_table", "read_excel", "read_json", "read_html",
                   "read_parquet", "read_feather", "read_orc", "read_pickle",
                   "read_spss", "read_stata", "read_sas", "read_hdf", "read_xml",
                   "read_sql", "read_sql_query", "read_sql_table", "read_clipboard",
                   "read_fwf", "read_gbq"):
            if hasattr(pd, fn):
                setattr(pd, fn, _deny)
        for fn in ("to_csv", "to_excel", "to_json", "to_parquet", "to_pickle",
                   "to_sql", "to_html", "to_clipboard", "to_feather", "to_orc",
                   "to_stata", "to_hdf", "to_gbq", "to_markdown"):
            if hasattr(pd.DataFrame, fn):
                setattr(pd.DataFrame, fn, _deny)
            if hasattr(pd.Series, fn):
                setattr(pd.Series, fn, _deny)
        # pandas.io 子包（read_parquet 等底层 IO 的真正入口）
        try:
            for sub in ("parquet", "feather", "orc", "sql", "pickle", "excel",
                        "clipboard", "html", "json", "stata", "spss", "sas", "gbq"):
                try:
                    m = __import__(f"pandas.io.{sub}", fromlist=["x"])
                    _patch_module_io(m, [n for n in dir(m) if n.startswith(("read_", "to_", "write_"))])
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass

    try:
        import numpy as np
        for fn in ("load", "loadtxt", "genfromtxt", "fromfile", "frombuffer",
                   "save", "savez", "savetxt", "fromregex", "memmap", "DataSource"):
            if hasattr(np, fn):
                setattr(np, fn, _deny)
        if hasattr(np.ndarray, "tofile"):
            np.ndarray.tofile = _deny
        # numpy.ctypeslib 可加载任意原生 DLL → 整模块替换为拒绝函数
        try:
            np.ctypeslib = _deny
        except Exception:
            pass
        try:
            import numpy.lib.format as _npf
            _patch_module_io(_npf, ["read_array", "write_array", "open_memmap"])
        except Exception:
            pass
        try:
            import numpy.lib.npyio as _npn
            _patch_module_io(_npn, [n for n in dir(_npn) if n.startswith(("load", "save", "read_", "write_"))])
        except Exception:
            pass
    except Exception:
        pass

    # joblib.load 会反序列化任意对象 → 等价于任意代码执行，必须封
    try:
        import joblib
        joblib.load = _deny
        joblib.dump = _deny
    except Exception:
        pass

    # scipy.io / sklearn.datasets 可读写磁盘文件
    try:
        import scipy.io as _sio
        _patch_module_io(_sio, ["loadmat", "savemat", "wavfile", "netcdf_file",
                                "mmread", "mmwrite", "harwell_boeing", "idlave",
                                "readsav", "hdf5", "fortran", "matlab"])
        for sub in ("wavfile", "netcdf", "hdf5", "idl", "harwell_boeing"):
            try:
                m = __import__(f"scipy.io.{sub}", fromlist=["x"])
                _patch_module_io(m, [n for n in dir(m) if n.startswith(("read", "write", "load", "save"))])
            except Exception:
                pass
    except Exception:
        pass

    try:
        import sklearn.datasets as _skd
        _patch_module_io(_skd, [n for n in dir(_skd)
                                if n.startswith(("load_", "fetch_", "download_", "read_"))])
    except Exception:
        pass

    # time.sleep 只会被用来拖时间，封掉；其他 time 功能保留
    try:
        import time as _t
        _t.sleep = _deny
    except Exception:
        pass


def _jsonable(v, depth: int = 0):
    """把任意执行结果转成 JSON 可序列化结构（含 NaN/Inf 与 numpy 标量处理）。"""
    if depth > 6:
        return str(v)[:500]
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int,)):
        return v if abs(v) < 10 ** 15 else str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):  # NaN / Inf 不是合法 JSON
            return None
        return round(v, 10)
    if isinstance(v, str):
        return v[:4000]
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x, depth + 1) for x in list(v)[:2000]]
    if isinstance(v, dict):
        return {str(k)[:200]: _jsonable(x, depth + 1) for k, x in list(v.items())[:2000]}

    try:
        import numpy as np
        if isinstance(v, np.generic):
            return _jsonable(v.item(), depth + 1)
        if isinstance(v, np.ndarray):
            return _jsonable(v.tolist(), depth + 1)
    except Exception:
        pass

    try:
        import pandas as pd
        if isinstance(v, pd.DataFrame):
            return {
                "__kind__": "dataframe",
                "columns": [str(c) for c in v.columns][:200],
                "rows": _jsonable(v.head(200).to_dict("records"), depth + 1),
                "row_count": int(len(v)),
            }
        if isinstance(v, pd.Series):
            return {
                "__kind__": "series",
                "name": str(v.name or ""),
                "data": _jsonable(v.head(200).to_dict(), depth + 1),
                "length": int(len(v)),
            }
        if isinstance(v, pd.Index):
            return _jsonable(list(v)[:200], depth + 1)
    except Exception:
        pass

    try:
        import datetime as _dt
        if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
            return v.isoformat()
    except Exception:
        pass

    return str(v)[:4000]


def _build_frame(columns, rows):
    """把注入的行数据转成 DataFrame；pandas 不可用时降级为 None（代码仍可用 rows 列表）。"""
    try:
        import pandas as pd
        return pd.DataFrame(rows, columns=columns)
    except Exception:
        return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as e:
        print(json.dumps({"success": False, "error": "载荷解析失败: %s" % e}))
        return 0

    code = payload.get("code") or ""
    columns = payload.get("columns") or []
    rows = payload.get("rows") or []

    _harden_io()

    df = _build_frame(columns, rows)
    ns = {
        "__builtins__": _safe_builtins(),
        "__name__": "__sandbox__",
        "df": df,
        "rows": [dict(r) for r in rows],
        "columns": list(columns),
    }
    # 预注入常用包，省去用户代码 import（同时也避免 import 守卫的误伤）
    try:
        import pandas as pd
        ns["pd"] = pd
    except Exception:
        pass
    try:
        import numpy as np
        ns["np"] = np
    except Exception:
        pass

    buf = io.StringIO()
    _old_stdout, sys.stdout = sys.stdout, buf
    error = ""
    result = None
    try:
        tree = ast.parse(code)
        # 尾表达式自动作为结果（LLM 常把 df.corr() 写在最后一行）
        tail = tree.body[-1] if (tree.body and isinstance(tree.body[-1], ast.Expr)) else None
        head = ast.Module(body=(tree.body[:-1] if tail else tree.body), type_ignores=[])
        exec(compile(head, "<sandbox>", "exec"), ns)
        if tail is not None:
            if ns.get("result") is not None:
                # 用户已显式赋值 result：尾表达式仍要执行（保留 print 等副作用），
                # 但不能再用它的返回值覆盖 result（print() 返回 None 会把结果冲掉）。
                exec(compile(ast.Module(body=[tail], type_ignores=[]), "<sandbox>", "exec"), ns)
            else:
                ns["result"] = eval(compile(ast.Expression(tail.value), "<sandbox>", "eval"), ns)
        result = ns.get("result")
    except Exception as e:
        error = "%s: %s" % (type(e).__name__, e)
    finally:
        sys.stdout = _old_stdout

    out = buf.getvalue()
    print(json.dumps({
        "success": not error,
        "stdout": out[:8000],
        "result": _jsonable(result),
        "error": error,
    }, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    main()
