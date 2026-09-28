# -*- coding: utf-8 -*-
"""权限/指标测试共享 fixture。

背景（2026-09-13 深度检查实锤）：test_auth_flows / test_permission 按「演示 seed 权限模型」
编写断言（5 角色 / 销售域脱敏 / region 行过滤），但真实 auth_permissions.json 已被生产化
为其他角色集（如仅 超级管理员/普通员工），直接跑会产生 7+ 项误报失败。

用法：调用方先备份 auth_permissions.json（各自的 _backup()），再调用本函数安装 seed
快照；测试结束后按原样恢复（_restore()）+ load_model(refresh=True)。
"""

import sys
import os

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def install_seed_perm_model():
    """安装演示 seed 权限模型快照（补充 analyst 角色），刷新内存缓存。"""
    from security import model as perm_model

    model = perm_model._seed_model()
    have = {r.get("key") for r in model["roles"]}
    if "analyst" not in have:
        model["roles"].append({"key": "analyst", "name": "分析师",
                               "description": "数据分析岗（测试期望角色）",
                               "priority": 40, "system": False})
    perm_model.save_model(model, actor="unit-fixture")
    perm_model.load_model(refresh=True)
