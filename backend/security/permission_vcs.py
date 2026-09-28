"""权限即代码（Permission-as-Code）：把权限策略变更自动纳入 Git 版本控制。

在审批流落地（approval.apply_change）后调用 commit_permission_change()，
把 auth_permissions.json 的本次变更提交到 Git，实现权限变更：
  - 可追溯（git log 谁在何时改了什么）
  - 可 diff（git show <commit> 查看前后差异）
  - 可回滚（git revert <commit> 一键撤销）

安全与容错（绝不阻断业务）：
  - 非 git 仓库 / git 不可用 / 无变更 → 静默返回 None
  - 仅精确提交 auth_permissions.json 一个文件，绝不 `git add -A`
  - commit 身份用内联 -c 指定，不依赖全局 git config
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent          # backend/
_PERM_FILE = _BASE / "auth_permissions.json"            # 仅此一个文件纳入版本控制
_GIT = "git"


def _enabled() -> bool:
    """测试/CI 环境可通过 PERMISSION_VCS=0 关闭自动提交，避免测试污染 git 历史"""
    return os.environ.get("PERMISSION_VCS", "1").lower() not in ("0", "false", "off")


def _run(args: list[str], timeout: int = 8) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            [_GIT, *args], cwd=str(_BASE),
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def is_available() -> bool:
    """当前目录是否为可用的 git 仓库"""
    r = _run(["rev-parse", "--is-inside-work-tree"])
    return bool(r and r.returncode == 0 and r.stdout.strip() == "true")


def commit_permission_change(actor: str, action: str, target: str) -> str | None:
    """提交 auth_permissions.json 的当前变更；返回短 commit hash，失败/无变更返回 None。"""
    if not _enabled() or not is_available():
        return None
    # 仅当 auth_permissions.json 有变更时才提交
    st = _run(["status", "--porcelain", "--", "auth_permissions.json"])
    if not st or not st.stdout.strip():
        return None
    add = _run(["add", "--", "auth_permissions.json"])
    if not add or add.returncode != 0:
        return None
    msg = f"permission[{action}]: {target} by {actor}"
    commit = _run(["-c", "user.name=permission-bot",
                   "-c", "user.email=permission-bot@local",
                   "commit", "-m", msg, "--", "auth_permissions.json"], timeout=15)
    if not commit or commit.returncode != 0:
        return None
    rev = _run(["rev-parse", "--short", "HEAD"])
    return (rev.stdout.strip() or None) if rev and rev.returncode == 0 else None


def last_commits(limit: int = 20) -> list[dict]:
    """读取 auth_permissions.json 的最近提交历史（供审计/前端展示）"""
    if not is_available():
        return []
    r = _run(["log", "--oneline", f"-{limit}", "--", "auth_permissions.json"])
    if not r or r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        out.append({"hash": parts[0], "message": parts[1] if len(parts) > 1 else ""})
    return out
