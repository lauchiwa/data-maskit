"""测试 Token 缓存采集与统计聚合入库 (C1/C2)。

验证：
1. record_event 处理带有 cache_read_tokens / cache_write_tokens 的 RESTORE 事件；
2. daily_tokens 和 daily_models 能够正确记录并累加 cache_read 和 cache_write；
3. today_stats, stats_range, stats_trend, stats_models 均能正确提取缓存指标；
4. 数据库升级迁移：在缺失新列的旧表中启动时能自动平滑补充列。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

import event_store


class TestCacheTokenStorage(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test-shield.sqlite3")
        self._orig_db_path = event_store.DB_PATH
        event_store.DB_PATH = type(self._orig_db_path)(self.db_path)
        event_store._DB_INITIALIZED = False
        event_store.init_db()

    def tearDown(self):
        event_store._DB_INITIALIZED = False
        event_store.DB_PATH = self._orig_db_path
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_daily_tokens_and_models_aggregation_with_cache(self):
        """验证带缓存 token 的 RESTORE 事件入库并正确累加。"""
        now = time.time()
        # 记录第一条带缓存的请求（类似 OpenAI / 中转站形式）
        event_store.append_event({
            "type": "RESTORE",
            "ts": now,
            "model": "gpt-4o",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "cache_read_tokens": 800,
            },
            "status": "restored",
            "count": 1,
            "restored": 1,
        })

        # 记录第二条带缓存写入的请求（类似 Anthropic 形式）
        event_store.append_event({
            "type": "RESTORE",
            "ts": now,
            "model": "gpt-4o",
            "usage": {
                "prompt_tokens": 2000,
                "completion_tokens": 500,
                "cache_read_tokens": 500,
                "cache_write_tokens": 1000,
            },
            "status": "restored",
            "count": 1,
            "restored": 1,
        })

        # 1. 验证 today_stats
        ts = event_store.today_stats(now=now)
        self.assertTrue(ts.get("ok"))
        tokens = ts.get("tokens", {})
        self.assertEqual(tokens.get("prompt"), 3000)
        self.assertEqual(tokens.get("completion"), 700)
        self.assertEqual(tokens.get("cache_read"), 1300)
        self.assertEqual(tokens.get("cache_write"), 1000)

        # 2. 验证 stats_range
        sr = event_store.stats_range(days=1, now=now)
        self.assertTrue(sr.get("ok"))
        r_tokens = sr.get("tokens", {})
        self.assertEqual(r_tokens.get("prompt"), 3000)
        self.assertEqual(r_tokens.get("completion"), 700)
        self.assertEqual(r_tokens.get("cache_read"), 1300)
        self.assertEqual(r_tokens.get("cache_write"), 1000)

        # 3. 验证 stats_models
        models = event_store.stats_models(days=1, now=now)
        self.assertEqual(len(models), 1)
        m = models[0]
        self.assertEqual(m.get("model"), "gpt-4o")
        self.assertEqual(m.get("requests"), 2)
        self.assertEqual(m.get("prompt"), 3000)
        self.assertEqual(m.get("completion"), 700)
        self.assertEqual(m.get("cache_read"), 1300)
        self.assertEqual(m.get("cache_write"), 1000)

        # 4. 验证 stats_history
        trend = event_store.stats_history(days=1, granularity="day")
        self.assertTrue(trend.get("ok"))
        self.assertGreater(len(trend.get("data", [])), 0)
        day_bucket = trend["data"][-1]
        self.assertEqual(day_bucket.get("tokens_prompt"), 3000)
        self.assertEqual(day_bucket.get("tokens_completion"), 700)
        self.assertEqual(day_bucket.get("tokens_cache_read"), 1300)
        self.assertEqual(day_bucket.get("tokens_cache_write"), 1000)

    def test_legacy_db_migration(self):
        """验证旧表结构缺少 cache_read/cache_write 时平滑迁移且不报错。"""
        legacy_db_path = os.path.join(self.tmp_dir, "legacy-shield.sqlite3")
        # 预先建立旧版无 cache 列的 daily_tokens 和 daily_models 表
        with sqlite3.connect(legacy_db_path) as conn:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, type TEXT, sid TEXT, host TEXT, method TEXT, path TEXT, count INTEGER, restored INTEGER, status TEXT, http_status INTEGER, ingress TEXT, payload TEXT)")
            conn.execute("CREATE TABLE daily_tokens (day TEXT PRIMARY KEY, prompt INTEGER, completion INTEGER)")
            conn.execute("CREATE TABLE daily_models (day TEXT, model TEXT, requests INTEGER, prompt INTEGER, completion INTEGER, errors INTEGER DEFAULT 0, PRIMARY KEY (day, model))")
            conn.execute("INSERT INTO daily_tokens VALUES ('2026-10-10', 100, 50)")
            conn.execute("INSERT INTO daily_models VALUES ('2026-10-10', 'old-model', 1, 100, 50, 0)")
            conn.commit()

        # 切换到 legacy 数据库并触发自动迁移
        event_store.DB_PATH = type(self._orig_db_path)(legacy_db_path)
        event_store._DB_INITIALIZED = False
        event_store._ensure_db()

        # 验证新列已成功添加
        with sqlite3.connect(legacy_db_path) as conn:
            t_cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_tokens)").fetchall()]
            self.assertIn("cache_read", t_cols)
            self.assertIn("cache_write", t_cols)
            m_cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_models)").fetchall()]
            self.assertIn("cache_read", m_cols)
            self.assertIn("cache_write", m_cols)

        # 验证读取旧数据依然正常（无缓存数据时，不返回 cache 列以保持干净和向前兼容）
        models = event_store.stats_models(days=7, now=time.mktime(time.strptime("2026-10-10", "%Y-%m-%d")) + 3600)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].get("model"), "old-model")
        self.assertNotIn("cache_read", models[0])
        self.assertNotIn("cache_write", models[0])


if __name__ == "__main__":
    unittest.main()
