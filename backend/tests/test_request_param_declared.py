# -*- coding: utf-8 -*-
"""扫「函数体引用了 request，但签名没声明 request」这类 bug。
2026-10-07 实测：`/knowledge/templates/{id}/run` 因为漏声明 `request: Request`，
函数体里 `_username_of(authorization, request)` 触发
`NameError: name 'request' is not defined` ⇒ 8 个模板 100% 报 500。
Python **不会在定义处报错**，只在运行时炸 ⇒ 静态扫不出来，必须 AST 扫。
"""
import os as _os  # 路径基准：以本文件所在目录为准，换机器/换目录后仍有效
_BASE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import ast, os, sys

TARGETS = [
    _os.path.join(_BASE, "routers/knowledge.py"),
    _os.path.join(_BASE, "main.py"),
    _os.path.join(_BASE, "routers/tables.py"),
    _os.path.join(_BASE, "routers/config.py"),
    _os.path.join(_BASE, "routers/permission.py"),
]
sys.stdout.reconfigure(encoding="utf-8")

fail = 0
for path in TARGETS:
    if not os.path.exists(path):
        print("目标文件不存在（路径基准失效，本测试将失去意义）: %s" % path)
        fail += 1
        continue
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # 签名里声明的参数名
        a = node.args
        declared = {p.arg for p in (a.posonlyargs + a.args + a.kwonlyargs)}
        if a.vararg:
            declared.add(a.vararg.arg)
        if a.kwarg:
            declared.add(a.kwarg.arg)
        # 声明了 request 就算过
        if "request" in declared:
            continue
        # 函数体（不含嵌套函数体）里是否引用了名为 request 的名字
        found = []
        for sub in ast.walk(node):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not node:
                continue
            if isinstance(sub, ast.Name) and sub.id == "request":
                found.append(sub.lineno)
            elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                    and sub.value.id == "request":
                found.append(sub.lineno)
        if found:
            bad.append((node.name, node.lineno, sorted(set(found))))
    name = os.path.basename(path)
    if bad:
        for fn, ln, refs in bad:
            fail += 1
            print("★ %s: def %s (L%d) 未声明 request，却在 L%s 引用"
                  % (name, fn, ln, ",".join(map(str, refs[:5]))))
    else:
        print("OK %s（%d 个函数全部合规）" % (name, sum(
            1 for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))))

print()
print("结果: %s" % ("ALL PASS" if not fail else "FAIL=%d" % fail))
sys.exit(1 if fail else 0)