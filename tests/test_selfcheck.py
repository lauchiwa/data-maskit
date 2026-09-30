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


# 多问题样本：用来验证「英文模式不残留中文」这种行为测试能真的跑出若干条结论
# （只要 fired_ids 非空即可；具体触发哪几条不影响断言的有效性）。
MESSY = json.loads(json.dumps(HEALTHY))
MESSY["proxy"].update({"running": False, "stop_mode": "error", "restarts": 3,
                       "last_error": "模型文件缺失（应为 /data/engine/models/ner_mini_zh 下的 model_quantized.onnx）"})
MESSY["proxy"]["ports"] = [{"port": 5801, "listening": True, "holder": "nginx"}]
MESSY["env"].update({"cgroup_cpu_quota": 1.0, "nr_throttled_delta": 5,
                     "disk_free_mb": 10, "disk_path": "/data"})
MESSY["engine"]["mask_pool"] = {"busy_total": 3, "peak_wait_ms": 1200,
                                 "queue_depth": 2, "workers": 4}
MESSY["engine"]["audit"] = {"truncated": 2, "parse_skipped": 1, "p95_ms": 40}
MESSY["ner"] = {"enabled": True, "available": True, "initialized": False, "failed": True,
                "last_error": "模型文件缺失（应为 /data/engine/models/ner_mini_zh 下的 model_quantized.onnx）",
                "skips": {"global_throttled": 3}}
MESSY["events"] = {"window_s": 600, "by_status": {"200": 10, "503": 7}, "per_minute": 90,
                   "unresolved": 3,
                   "by_block_source": {"upstream": 5, "engine": 2, "fallback": 0}}
MESSY["settings"]["fail_closed"] = False


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

    def test_s11_requires_upstream_samples(self):
        """零样本不得下“上游秒拒”的判断。

        `aggregate_recent` 的默认值是 {"p50": 0.0, "n": 0} —— p50=0 会让
        “p50 ≤ 300ms”成立，于是**没有样本**也会被读成“上游返回得很快”，
        并给出“核对限流/配额”这种指向错误的建议。合法判据必须在 n > 0 之后才谈 p50。
        """
        base = dict(HEALTHY["events"])
        self.assertNotIn("S11", ids(dict(HEALTHY, events=dict(
            base, by_status={"5xx": 3}, upstream_ms={"p50": 0.0, "p95": 0.0, "n": 0}))),
            "零样本时凭默认 p50=0 断言了“上游秒拒”")
        self.assertIn("S11", ids(dict(HEALTHY, events=dict(
            base, by_status={"5xx": 3}, upstream_ms={"p50": 12.0, "p95": 20.0, "n": 40}))),
            "有样本且很快时必须判（别把规则废掉）")
        self.assertNotIn("S11", ids(dict(HEALTHY, events=dict(
            base, by_status={"5xx": 3}, upstream_ms={"p50": 5000.0, "p95": 9000.0, "n": 40}))),
            "上游很慢是超时，不是秒拒")

    def test_ok_items_never_contradict_fired_findings(self):
        """「检查通过」只能来自“那条规则确实没触发”。

        修复前它是一个与规则无关的静态列表（有 findings 时固定显示第 1 条），
        S01「代理未运行」会与「代理运行中且端口/兜底层正常」同屏出现。
        """
        out = sc.run_selfcheck(dict(HEALTHY, proxy=dict(HEALTHY["proxy"], running=False),
                                    settings={"fail_closed": True}))
        self.assertIn("S01", out["fired_ids"])
        notes = " ".join(o["note"] for o in out["ok_items"])
        self.assertNotIn("代理运行中", notes, "已在报「代理未运行」却仍显示「代理运行中」")
        # 反向：完全健康时 A–E 应全部在列
        healthy = sc.run_selfcheck(HEALTHY)
        self.assertEqual({o["id"] for o in healthy["ok_items"]}, {"A", "B", "C", "D", "E", "F"})

    def test_s35_reports_words_that_did_not_take_effect(self):
        """词表问题必须出现在结论里（2026-09-30 事故：一个 re: 词让整表静默失效）。"""
        ctx = dict(HEALTHY, words={"configured": 5, "engine_count": 2, "engine_stale": False,
                                   "issues": {"re:(?i)(Beijing)": "正则无效，已跳过该词：..."}})
        out = sc.run_selfcheck(ctx)
        self.assertIn("S35", out["fired_ids"])
        self.assertNotIn("F", {o["id"] for o in out["ok_items"]},
                         "已经报了词表问题，就不能同时显示「词表全部生效」")
        clean = sc.run_selfcheck(dict(HEALTHY, words={"configured": 5, "engine_count": 5,
                                                     "engine_stale": False, "issues": {}}))
        self.assertNotIn("S35", clean["fired_ids"])
        self.assertIn("F", {o["id"] for o in clean["ok_items"]})
        stale = sc.run_selfcheck(dict(HEALTHY, words={"configured": 5, "engine_count": None,
                                                     "engine_stale": True, "issues": {}}))
        self.assertNotIn("S35", stale["fired_ids"], "引擎指标过期时不许下结论")

    def test_s36_reports_event_db_dead_space(self):
        """删行不等于文件变小：死空间占比高时要能看见（实测线上 249MB 里 84MB 是空页）。"""
        big = dict(HEALTHY, storage={"db": {"ok": True, "bytes": 261_000_000,
                                            "free_bytes": 88_000_000, "rows": 19000,
                                            "free_ratio": 0.337}})
        out = sc.run_selfcheck(big)
        self.assertIn("S36", out["fired_ids"])
        self.assertNotIn("E", {o["id"] for o in out["ok_items"]},
                         "已经报了库体积问题，就不能同时显示「事件库写入正常」")
        small = dict(HEALTHY, storage={"db": {"ok": True, "bytes": 5_000_000, "free_bytes": 100,
                                              "rows": 30, "free_ratio": 0.00002}})
        self.assertNotIn("S36", sc.run_selfcheck(small)["fired_ids"])
        self.assertIn("E", {o["id"] for o in sc.run_selfcheck(small)["ok_items"]})
        broken = dict(HEALTHY, storage={"db": {"ok": False, "error": "OperationalError: locked"}})
        self.assertNotIn("S36", sc.run_selfcheck(broken)["fired_ids"], "取不到体积时不许下结论")

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

    def _projection_keys(self, fn_name):
        """取 panel 里某个函数的字符串键集合（dict 字面量 + out[...] 赋值）。"""
        tree = ast.parse(self.src)
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name == fn_name]:
            keys = {k.value for n in ast.walk(fn) if isinstance(n, ast.Dict)
                    for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            keys |= {s.slice.value for s in ast.walk(fn) if isinstance(s, ast.Subscript)
                     and isinstance(s.value, ast.Name)
                     and isinstance(s.slice, ast.Constant) and isinstance(s.slice.value, str)}
            return keys
        self.fail("找不到 %s（被改名/删除了？）" % fn_name)

    def test_engine_metrics_projection_carries_governor(self):
        """`/api/engine/metrics` 必须透出 governor —— 它是 `budget_waited` 的**唯一**出口。

        这条守卫的由来：CHANGELOG 两次把这个指标的出口写错（先写「面板可见」、又写
        「诊断包可见」，两次都不对），而两次都没有任何测试盯着「出口到底存不存在」。
        口径写错不是功能缺陷，但它会让人去够一个根本拿不到的数。
        """
        self.assertIn("governor", self._projection_keys("_project_engine_metrics"),
                      "/api/engine/metrics 不再透出 governor（budget_waited 就没有出口了）")

    def test_diagnostics_bundle_does_not_carry_ner(self):
        """诊断包**不**带 ner / governor / engine —— 别再把两个出口写混。

        实测过的错法：在 panel.py 里看到 `"governor": ner.get("governor")` 就以为诊断包
        含它。那一行其实属于 `_project_engine_metrics`（喂 `/api/engine/metrics`），而
        `_diagnostics_payload` 的键里没有 ner / governor / engine。
        """
        keys = self._projection_keys("_diagnostics_payload")
        for leak in ("ner", "governor", "engine"):
            self.assertNotIn(leak, keys,
                             "诊断包出现了 %s：出口口径变了就同步改 CHANGELOG" % leak)
        self.assertIn("selfcheck", keys, "诊断包应内嵌自检结论")

    def test_language_param_reaches_both_exports(self):
        """自检与诊断包都要按 `?lang=` 出结论（英文界面不该出现中文结论）。

        实测过的失效形态：端点读了 lang、但传入处忘了带（或新增端点时漏抄）——
        测试全绿而英文界面冒出中文，所以两边都得钉住。
        """
        self.assertIn('request.args.get("lang")', self.src,
                      "端点没读 ?lang=，结论永远中文")
        self.assertIn("run_selfcheck(ctx, lang=lang)", self.src,
                      "/api/selfcheck 读了 lang 却没传给 run_selfcheck")
        self.assertRegex(self.src, r"run_selfcheck\(_selfcheck_inputs\(\), lang=",
                         "诊断包读了 lang 却没传给 run_selfcheck")
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


class BilingualTests(unittest.TestCase):
    """自检结论必须跟语言走：英文界面出现中文结论 = 回归。

    分两层：① 行为——英文模式跑一遍，断言结论里没有任何汉字；
    ② 结构——逐条规则检查源码里带英文分支（新增规则漏译时立即报错，
    不必等到那条规则恰好在测试环境里被触发）。
    """

    @staticmethod
    def _text(out):
        parts = [str(out.get("summary_line") or "")]
        for f in out.get("findings", []):
            parts += [str(f.get("title") or ""), str(f.get("evidence") or ""),
                      str(f.get("action") or "")]
        for o in out.get("ok_items", []):
            parts.append(str(o.get("note") or ""))
        return " ".join(parts)

    def test_every_rule_has_an_english_branch(self):
        """结构层：每条规则都必须给英文分支（漏译不再靠运气发现）。"""
        import inspect
        for rule in sc.RULES:
            fn = getattr(rule, "__wrapped__", rule)
            self.assertIn("_is_en()", inspect.getsource(fn),
                          "%s 缺英文分支：英文界面会露出中文结论"
                          % getattr(fn, "__name__", rule))

    def test_english_mode_contains_no_chinese(self):
        out = sc.run_selfcheck(json.loads(json.dumps(MESSY)), lang="en")
        self.assertTrue(out["fired_ids"], "用例本身没触发任何规则，测试失去意义")
        self.assertNotRegex(self._text(out), r"[\u4e00-\u9fff]",
                            "英文模式残留中文结论：%s" % self._text(out)[:200])

    def test_chinese_mode_stays_chinese(self):
        out = sc.run_selfcheck(json.loads(json.dumps(MESSY)), lang="zh")
        self.assertRegex(self._text(out), r"[\u4e00-\u9fff]")

    def test_unknown_language_falls_back_to_chinese(self):
        a = self._text(sc.run_selfcheck(json.loads(json.dumps(MESSY)), lang="xx"))
        b = self._text(sc.run_selfcheck(json.loads(json.dumps(MESSY)), lang="zh"))
        self.assertEqual(a, b, "非法语言必须回退中文，不能漏出半截英文")

    def test_healthy_english_mode_has_no_chinese(self):
        """全好路径也要英文：ok_items 的 note 与 summary_line 同样要跟语言走。"""
        out = sc.run_selfcheck(json.loads(json.dumps(HEALTHY)), lang="en")
        self.assertNotRegex(self._text(out), r"[\u4e00-\u9fff]",
                            "健康路径残留中文：%s" % self._text(out)[:200])


if __name__ == "__main__":
    unittest.main()
