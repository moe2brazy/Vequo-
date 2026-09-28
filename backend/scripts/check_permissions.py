"""权限 CI 检查：一次性跑全部权限测试，任一失败即非零退出。

供 CI / pre-commit / 上线前检查使用：
    python backend/scripts/check_permissions.py

覆盖：
  - tests/test_permission.py  （引擎单元测试 T1-T11）
  - tests/test_auth_flows.py  （多角色授权流程 F1-F6）
"""
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PY = sys.executable

TESTS = [
    BACKEND / "tests" / "test_permission.py",
    BACKEND / "tests" / "test_auth_flows.py",
]


def main() -> int:
    failed: list[str] = []
    for t in TESTS:
        print(f"\n{'=' * 60}\n>>> {t.name}\n{'=' * 60}")
        r = subprocess.run([PY, str(t)], cwd=str(BACKEND), capture_output=True, text=True)
        tail = (r.stdout or "")[-800:]
        if r.returncode != 0:
            tail += (r.stderr or "")[-800:]
            failed.append(t.name)
        print(tail)
    print(f"\n{'=' * 60}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("ALL PERMISSION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
