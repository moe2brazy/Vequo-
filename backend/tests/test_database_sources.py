# -*- coding: utf-8 -*-
"""多数据源注册表测试（P1-1 对标 Spotter automatic model selection 渐进式落地）。

覆盖：默认源兜底 / 注册（连接测试闸门）/ 同名更新 / 删除保护 / 激活 / 跨源搜索。
用备份-恢复保护真实源文件，测试不污染运行数据。
"""

import json
import os
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as DB


class MultiSourceTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 备份真实源文件（若存在），测试后恢复
        cls._backup = None
        if DB.DATABASE_SOURCES_PATH.exists():
            cls._backup = DB.DATABASE_SOURCES_PATH.read_text(encoding="utf-8")
        cls._env_pwd = os.getenv("DB_PASSWORD", "")
        # P0 修复：activate_database_source 会经 save_db_config_to_env 改写生产 .env，
        # 导致「跑一遍测试基线库就被切走」（评测回退事件的实锤根因）。
        # 此处备份 .env，测试后原样恢复，保证测试对磁盘配置零副作用。
        cls._env_backup = DB.ENV_PATH.read_text(encoding="utf-8") if DB.ENV_PATH.exists() else None

    @classmethod
    def tearDownClass(cls):
        if cls._backup is not None:
            DB.DATABASE_SOURCES_PATH.write_text(cls._backup, encoding="utf-8")
        else:
            DB.DATABASE_SOURCES_PATH.unlink(missing_ok=True)
        # 恢复 .env（ activate / save 配置路径的持久化副作用全部回滚）
        if cls._env_backup is not None:
            DB.ENV_PATH.write_text(cls._env_backup, encoding="utf-8")
        elif DB.ENV_PATH.exists():
            DB.ENV_PATH.unlink()

    def setUp(self):
        # 每个用例从默认源开始
        DB._save_sources([])

    def _default_source(self) -> dict:
        return {
            "db_type": "postgresql", "host": "localhost", "port": 5432,
            "database": "postgres", "user": "postgres", "password": self._env_pwd,
        }

    def test_default_source_fallback(self):
        """A. 空注册表 → 默认源兜底（当前连接配置）"""
        sources = DB.get_database_sources()
        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0]["is_active"])
        self.assertNotIn("password", sources[0])  # 列表不含密码

    def test_add_and_list(self):
        """B. 注册新源（连接测试通过才保存）+ 列表不含密码"""
        r = DB.add_database_source({"name": "生产库", **self._default_source()})
        self.assertTrue(r["success"], r.get("error"))
        self.assertNotIn("password", r["source"])
        lst = DB.get_database_sources()
        names = [s["name"] for s in lst]
        self.assertIn("生产库", names)
        # 只有默认源是活动源
        active = [s for s in lst if s["is_active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["name"], "默认数据源")

    def test_add_invalid_connection(self):
        """C. 连接测试失败 → 不保存"""
        bad = {**self._default_source(), "password": "wrong_password_xxx"}
        r = DB.add_database_source({"name": "坏源", **bad})
        self.assertFalse(r["success"])
        self.assertIn("连接测试失败", r["error"])
        self.assertEqual(len(DB.get_database_sources()), 1)  # 只有默认源

    def test_add_duplicate_updates(self):
        """D. 同名源 → 更新而非重复添加"""
        r1 = DB.add_database_source({"name": "生产库", **self._default_source()})
        r2 = DB.add_database_source({"name": "生产库", **self._default_source()})
        self.assertTrue(r1["success"] and r2["success"])
        self.assertEqual(len(DB.get_database_sources()), 2)  # 默认 + 生产库

    def test_remove_protection(self):
        """E. 活动源不可删；非活动源可删"""
        r = DB.add_database_source({"name": "可删源", **self._default_source()})
        sid = r["source"]["id"]
        # 活动源（default）删除被拒
        self.assertFalse(DB.remove_database_source("default"))
        # 非活动源删除成功
        self.assertTrue(DB.remove_database_source(sid))
        self.assertEqual(len(DB.get_database_sources()), 1)

    def test_activate(self):
        """F. 激活非活动源 → 标记翻转 + 全局连接切换"""
        r = DB.add_database_source({"name": "新主源", **self._default_source()})
        sid = r["source"]["id"]
        res = DB.activate_database_source(sid)
        self.assertTrue(res["success"], res.get("error"))
        lst = DB.get_database_sources()
        self.assertTrue(next(s for s in lst if s["id"] == sid)["is_active"])
        self.assertFalse(next(s for s in lst if s["id"] == "default")["is_active"])
        # 激活不存在的源 → 失败
        self.assertFalse(DB.activate_database_source("no_such_id")["success"])

    def test_search_across_sources(self):
        """G. 跨源搜索：英文表名子串/单词前缀命中；跳过活动源"""
        DB.add_database_source({"name": "副库", **self._default_source()})
        res = DB.search_across_sources("inventory", limit=5)
        self.assertTrue(any(x["table_name"] == "inv_inventory_snapshot" for x in res),
                        f"inventory 未命中: {res}")
        self.assertTrue(all(x["source_name"] == "副库" for x in res))  # 只搜非活动源
        # 无匹配关键词 → 空
        self.assertEqual(DB.search_across_sources("zzz_no_such_table_zzz"), [])

    def test_search_empty_keyword(self):
        """H. 空关键词 → 空结果"""
        self.assertEqual(DB.search_across_sources("  "), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
