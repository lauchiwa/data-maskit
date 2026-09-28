"""一键自检（§16）的契约。

自检的风险不是崩溃，而是**误导**：把"没数据"说成"有问题"（用户白折腾），
或把"有问题"说成"正常"（最坏，用户以为没事）。所以每条规则都要有
「构造输入 → 必须/必须不触发」两侧断言，且取数失败必须敢标未验证。
"""
import ast
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT))

import selfcheck as sc


def ids(ctx):
    return set(sc.run_selfcheck(ctx)["fired_ids"])


HEALTHY = {
    "proxy": {"running": True, "fallback_mode": "passthrough", "restarts": 0, "last_error": "",
              "ports": [{"port": 5801, "listening": True, "holder": "mitmdump"}]},
    "env": {"cpu_count": 8, "cgroup_cpu_quota": 4.0, "nr_throttled_delta": 0, "disk_free_mb": 50000},
    "engine": {"mask_pool": {"busy_total": 0, "peak_wait_ms": 3, "queue_depth": 0, "workers": 4},
               "audit": {"truncated": 0, "parse_skipped": 0, "p95_ms": 20}},
    "ner": {"enabled": False, "available": True, "initialized": False, "failed": False},
    "events": {"window_s": 3600, "by_status": {"200": 40}, "per_minute": 2, "unresolved": 0},
    # writer 的形状必须与 event_store.writer_stats() 一致（S32 读的就是它）。
    "storage": {"writer": {"event_writer_alive": True, "audit_writer_alive": True,
                           "event_writer": {"restarts": 0, "dead_letters": 0, "drops": 0, "last_err": ""},
                           "audit_writer": {"restarts": 0, "dead_letters": 0, "drops": 0, "last_err": ""}}},
    "settings": {"fail_closed": True},
}


class HealthyBaselineTests(unittest.TestCase):
    def test_healthy_fires_nothing(self):
        self.assertEqual(ids(HEALTHY), set(), "健康输入不该有任何结论（否则用户会习惯性忽略自检）")

    def test_healthy_overall_is_ok(self):
        out = sc.run_selfcheck(HEALTHY)
        self.assertEqual(out["overall"], "ok")
        self.assertIn("未发现异常", out["summary_line"])

    def test_missing_data_does_not_fabricate_findings(self):
        """输入缺失时应"不判"，而不是靠默认值编一条结论出来。"""
        self.assertEqual(ids({}), {"S01"}, "空输入只该报「代理未运行」这一条")

    def test_never_raises_on_garbage(self):
        for bad in ({"proxy": "x"}, {"events": {"by_status": None}}, {"engine": []},
                    {"env": {"cpu_count": "many"}}, {"ner": {"skips": "x"}},
                    {"events": {"retry_storms": None}}, {"storage": {"writer": None}}):
            out = sc.run_selfcheck(bad)          # 不抛即通过
            self.assertIn("findings", out)
            self.assertIsInstance(out["input_errors"], list)

    def test_rule_exception_is_captured_not_propagated(self):
        out = sc.run_selfcheck({"proxy": {"running": True}, "events": {"window_s": {"bad": 1}}})
        self.assertIsInstance(out["findings"], list)


class RuleTests(unittest.TestCase):
    def test_s02_fallback_error(self):
        ctx = dict(HEALTHY, proxy={"running": False, "fallback_mode": "error"})
        self.assertIn("S02", ids(ctx))

    def test_s03_port_holder(self):
        ctx = dict(HEALTHY, proxy=dict(HEALTHY["proxy"], ports=[
            {"port": 5801, "listening": True, "holder": "other"}]))
        self.assertIn("S03", ids(ctx))

    def test_s04_restart_loop(self):
        self.assertIn("S04", ids(dict(HEALTHY, proxy=dict(HEALTHY["proxy"], restarts=5))))
        self.assertIn("S04", ids(dict(HEALTHY, proxy=dict(HEALTHY["proxy"], last_error="OSError: x"))))

    def test_s10_attribution_is_split_not_guessed(self):
        """47 次 503 里 45 次是上游返回：结论必须说清"不是本机拦的"，否则用户会白查本地。"""
        ctx = dict(HEALTHY, events=dict(HEALTHY["events"], by_status={"503": 47},
                                        by_block_source={"upstream": 45, "engine": 2}))
        out = sc.run_selfcheck(ctx)
        f = [x for x in out["findings"] if x["id"] == "S10"][0]
        self.assertIn("上游返回 45", f["evidence"])
        self.assertIn("本机拦截 2", f["evidence"])
        self.assertEqual(f["verified"], True)

    def test_s10_without_attribution_marks_unverified(self):
        ctx = dict(HEALTHY, events=dict(HEALTHY["events"], by_status={"503": 3}))
        f = [x for x in sc.run_selfcheck(ctx)["findings"] if x["id"] == "S10"][0]
        self.assertFalse(f["verified"], "缺来源字段时必须标未验证")

    def test_s20_only_when_ner_on_and_cpu_tight(self):
        tight = dict(HEALTHY, env={"cgroup_cpu_quota": 1.0, "nr_throttled_delta": 0},
                     ner=dict(HEALTHY["ner"], enabled=True))
        self.assertIn("S20", ids(tight))
        roomy = dict(HEALTHY, env={"cgroup_cpu_quota": 8.0},
                     ner=dict(HEALTHY["ner"], enabled=True))
        self.assertNotIn("S20", ids(roomy), "核数足够不该报")

    def test_s21_model_missing(self):
        ctx = dict(HEALTHY, ner={"enabled": True, "available": False, "initialized": False})
        self.assertIn("S21", ids(ctx))

    def test_s22_skip_reasons(self):
        ctx = dict(HEALTHY, ner=dict(HEALTHY["ner"], enabled=True, available=True,
                                     initialized=True, skips={"global_throttled": 12}))
        f = [x for x in sc.run_selfcheck(ctx)["findings"] if x["id"] == "S22"][0]
        self.assertIn("global_throttled", f["evidence"])

    def test_s23_queue_backpressure(self):
        ctx = dict(HEALTHY, engine={"mask_pool": {"busy_total": 4, "peak_wait_ms": 9000}})
        self.assertIn("S23", ids(ctx))

    def test_stale_engine_metrics_mark_findings_unverified(self):
        """引擎指标过期 → S23/S24 必须标"未验证"（0.6.0 行为断言）。

        为什么必须有行为断言：这两条规则读的是引擎写出的 `engine-runtime.json`
        （可能几小时前的快照）。拿旧快照断言"现在正在排队"就是误导，所以结论要降级成
        未验证。原实现只算不判，前端也没有未验证徽标可表达；此后只有一条源码 grep
        守着（`"stale" in panel 源码`）—— 那种断言在删掉消费方之后依然绿。
        """
        fresh = {"mask_pool": {"busy_total": 4, "peak_wait_ms": 9000}, "metrics_stale": False}
        stale = dict(fresh, metrics_stale=True)
        f_fresh = [x for x in sc.run_selfcheck(dict(HEALTHY, engine=fresh))["findings"]
                   if x["id"] == "S23"][0]
        f_stale = [x for x in sc.run_selfcheck(dict(HEALTHY, engine=stale))["findings"]
                   if x["id"] == "S23"][0]
        self.assertTrue(f_fresh["verified"], "新鲜指标上的 S23 应是已验证")
        self.assertFalse(f_stale["verified"], "过期指标上的 S23 必须标未验证")

    def test_fallback_dominated_503_is_unverified(self):
        """兜底占位参与计数时 S10 必须是"未验证"（数字只是下界）。

        兜底监听把同类事件节流到 30s 一条，10 分钟的风暴可能只记 20 条；把这个数字
        当"量级不大"的依据是错的，所以结论要么降级、要么把口径写在证据里（两者都做了）。
        """
        ctx = dict(HEALTHY, events=dict(HEALTHY["events"],
                                        by_status={"200": 10, "503": 40},
                                        by_block_source={"fallback": 40}))
        f = [x for x in sc.run_selfcheck(ctx)["findings"] if x["id"] == "S10"][0]
        self.assertFalse(f["verified"], "节流计数驱动的 503 结论不能标已验证")
        self.assertIn("节流", f["evidence"])

    def test_s26_needs_a_baseline(self):
        """只有累计值时必须标未验证：把"曾经被限流过"说成"正在被限流"是误报。"""
        no_baseline = dict(HEALTHY, env={"cgroup_cpu_quota": 1.0, "cgroup_nr_throttled": 900})
        f = [x for x in sc.run_selfcheck(no_baseline)["findings"] if x["id"] == "S26"][0]
        self.assertEqual(f["severity"], "low")
        self.assertFalse(f["verified"])
        with_delta = dict(HEALTHY, env={"cgroup_cpu_quota": 1.0, "nr_throttled_delta": 312})
        f2 = [x for x in sc.run_selfcheck(with_delta)["findings"] if x["id"] == "S26"][0]
        self.assertEqual(f2["severity"], "high")
        self.assertTrue(f2["verified"])

    def test_s30_fail_closed_off(self):
        self.assertIn("S30", ids(dict(HEALTHY, settings={"fail_closed": False})))

    def test_s31_unresolved(self):
        self.assertIn("S31", ids(dict(HEALTHY, events=dict(HEALTHY["events"], unresolved=3))))

    def test_s32_writer_drops(self):
        """S32 必须按 `event_store.writer_stats()` 的**真实形状**判定。

        旧用例把形状写成 `{"writer": {"dropped": 5}}` —— 生产端从不产生这个键
        （真实形状是嵌套一层的 `event_writer.drops`），于是“规则永远绿色”被用例
        掩盖：这是自检最坏的失败形态（用户以为日志没丢）。下面三条与最后一条
        构成一对：按真实形状读，才能“该触发时触发、不该触发时不触发”。
        """
        def storage(**writer):
            base = {"event_writer_alive": True, "audit_writer_alive": True,
                    "event_writer": {}, "audit_writer": {}}
            base.update(writer)
            return {"writer": base}

        self.assertIn("S32", ids(dict(HEALTHY, storage=storage(
            event_writer={"drops": 5, "dead_letters": 0}))), "丢弃数没被读到")
        self.assertIn("S32", ids(dict(HEALTHY, storage=storage(
            audit_writer={"drops": 0, "dead_letters": 2}))), "死信数没被读到")
        self.assertIn("S32", ids(dict(HEALTHY, storage=storage(
            event_writer_alive=False))), "写线程死亡没被读到")
        # 旧形状（生产端不存在）不再是任何异常的来源：改回旧读取方式时这条会红
        self.assertNotIn("S32", ids(dict(HEALTHY, storage={"writer": {"dropped": 5}})))

    def test_s33_disk_and_quarantine(self):
        self.assertIn("S33", ids(dict(HEALTHY, env={"disk_free_mb": 12})))
        self.assertIn("S33", ids(dict(HEALTHY, storage={"db_quarantined": True})))

    def test_s34_running_but_silent(self):
        ctx = dict(HEALTHY, events=dict(HEALTHY["events"], last_event_ts=1),
                   proxy=dict(HEALTHY["proxy"], uptime_s=7200))
        self.assertIn("S34", ids(ctx))

    def test_severity_ordering_and_overall(self):
        ctx = dict(HEALTHY, settings={"fail_closed": False},
                   events=dict(HEALTHY["events"], unresolved=2))
        out = sc.run_selfcheck(ctx)
        sev = [f["severity"] for f in out["findings"]]
        self.assertEqual(sev, sorted(sev, key=lambda x: {"high": 0, "medium": 1, "low": 2}[x]),
                         "高危必须排在最前（用户只看得到前两条）")
        self.assertEqual(out["overall"], "medium")


class ProbeTests(unittest.TestCase):
    def test_probe_environment_never_raises(self):
        env = sc.probe_environment(str(ROOT))
        self.assertIsInstance(env.get("cpu_count"), int)
        self.assertGreater(env.get("disk_free_mb", 0), 0)
        self.assertIn("platform", env)

    def test_probe_on_bad_root(self):
        env = sc.probe_environment("\x00:/nonexistent")       # 非法路径 → 只记错，不抛
        self.assertTrue("disk_error" in env or "disk_free_mb" in env)

    def test_throttle_delta_needs_two_samples(self):
        sc._LAST_THROTTLE["nr"] = None
        sc._LAST_THROTTLE["ts"] = 0.0
        first = sc.probe_environment(".")
        self.assertIsNone(first.get("nr_throttled_delta"))


class PanelWiringTests(unittest.TestCase):
    """端点是安全面的一部分：挂错前缀 = 绕过三重校验，必须静态守住。"""

    def setUp(self):
        self.src = (ROOT / "engine" / "panel.py").read_text(encoding="utf-8")

    def test_selfcheck_endpoints_live_under_api_prefix(self):
        self.assertIn('@app.get("/api/selfcheck")', self.src)
        self.assertIn('@app.get("/api/engine/metrics")', self.src)

    def test_healthz_stays_minimal(self):
        """`/healthz` 是唯一免校验的存活探针，不能变成信息出口。"""
        i = self.src.index('"/healthz"')
        body = self.src[i:i + 400]
        for leak in ("selfcheck", "mask_pool", "metrics", "skips"):
            self.assertNotIn(leak, body, "/healthz 里出现了运行细节：%s" % leak)

    def test_diagnostics_schema_bumped_and_embeds_selfcheck(self):
        self.assertIn('"schema": 2', self.src)
        self.assertIn('out["selfcheck"]', self.src)

    def test_inputs_are_scrubbed(self):
        self.assertIn("_scrub_selfcheck", self.src)
        tree = ast.parse(self.src)
        fn = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "api_selfcheck"]
        self.assertTrue(fn, "api_selfcheck 不见了")
        body = ast.get_source_segment(self.src, fn[0]) or ""
        self.assertIn("_scrub_selfcheck", body, "自检结论没打码就直接返回了")


class CrossProcessMetricsTests(unittest.TestCase):
    """引擎指标是跨进程读的（panel 读引擎写的 JSON）：格式与过期口径要锁住。"""

    def setUp(self):
        self.src = (ROOT / "engine" / "transparent.py").read_text(encoding="utf-8")
        self.psrc = (ROOT / "engine" / "panel.py").read_text(encoding="utf-8")

    def test_engine_writes_atomically(self):
        self.assertIn("os.replace", self.src, "写运行指标必须原子替换（否则面板可能读到半截 JSON）")

    def test_panel_marks_staleness(self):
        self.assertIn("stale", self.psrc)
        self.assertIn("engine-runtime.json", self.psrc)

    def test_aggregate_is_readonly_and_body_free(self):
        src = (ROOT / "engine" / "event_store.py").read_text(encoding="utf-8")
        i = src.index("def aggregate_recent")
        body = src[i:i + 6000]
        for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP"):
            self.assertNotIn(forbidden, body, "聚合里出现了写操作：%s" % forbidden)
        for leak in ("payload FROM", "content"):
            self.assertNotIn(leak, body)

    def test_runtime_metrics_file_is_ignored_by_vcs(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("engine-runtime.json", ignore,
                      "引擎指标是运行时产物，必须 gitignore（否则会被提交进仓库）")


if __name__ == "__main__":
    unittest.main()
