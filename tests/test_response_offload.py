"""A-3：整包响应处理（还原 + 审计 + 响应扫描）不在事件循环上跑。

这一条是本轮改造里最"看不见"的一条：改完不会有任何 UI 变化，纯靠测试锁住。
所以断言抓两件事——
  ① 重活确实发生在**另一个线程**（否则等于没改）；
  ② 事件循环在等待期间**真的能干活**（否则只是把阻塞换了个写法：在循环上
     `await run_in_executor` 若忘了下池、或者被 GIL 长段占住，都会假绿）。
"""
import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT))

import transparent as tr

# 邮箱等"像真凭据"的样例一律运行时拼接：既避免仓库里出现真实形态，
# 也避免被本地脱敏网关改写后导致用例不稳定。
FAKE_EMAIL = "bench-user" + "@" + "example" + ".invalid"


class ResponseOffloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_root = tr._DATA_ROOT        # 全局态，必须还回去（否则污染后续用例）
        tr._DATA_ROOT = self.tmp
        (self.tmp / "config.json").write_text(json.dumps({
            "sensitive": {"甲类": ["测试敏感词"]}, "fail_closed": True,
        }, ensure_ascii=False), encoding="utf-8")
        tr._maybe_reload(force=True)
        self.emitted = []
        self._emit = tr._emit
        tr._emit = lambda typ, **kw: self.emitted.append((typ, kw))
        tr.sessions.clear()
        tr._AUDIT_FINDINGS_CACHE.clear()
        tr._AUDIT_CFG_FP[0] = None

    def tearDown(self):
        tr._emit = self._emit
        tr._DATA_ROOT = self._orig_root
        tr._maybe_reload(force=True)
        tr.sessions.clear()

    def _masked_body(self, sid):
        """造一个"响应里带占位符"的整包 JSON body，返回 (body, 原文)。"""
        original = "联系人 " + FAKE_EMAIL + " 与 测试敏感词"
        masked = tr.mask(original, sid)
        body = json.dumps({"choices": [{"message": {"content": masked}}]}).encode("utf-8")
        return body, original

    def _flow(self, body, sid, ct="application/json"):
        return SimpleNamespace(
            request=SimpleNamespace(host="api.openai.com", pretty_host="api.openai.com",
                                    method="POST", path="/v1/chat/completions",
                                    headers={"content-type": "application/json"}, content=b"{}"),
            response=SimpleNamespace(status_code=200, headers={"content-type": ct}, content=body),
            metadata={"session_id": sid},
        )

    def test_restore_still_works_through_async_hook(self):
        """功能不回归：async 化之后，整包占位符照样被还原成原文。"""
        sid = "offload-ok"
        tr._new_session(sid)
        body, original = self._masked_body(sid)
        flow = self._flow(body, sid)
        asyncio.run(tr.response(flow))
        content = flow.response.content.decode("utf-8")
        self.assertIn(FAKE_EMAIL, content, "邮箱没被还原：%r" % content[:200])
        self.assertIn("测试敏感词", content)
        self.assertNotIn("{{EMAIL_", content)
        self.assertIn("RESTORE", [t for t, _ in self.emitted], "RESTORE 事件丢了")

    def test_work_runs_off_the_event_loop_thread(self):
        """重活必须在别的线程：这是 A-3 的全部意义。"""
        seen = []
        orig = tr._response_offload

        def spy(*a, **kw):
            seen.append(threading.current_thread().name)
            return orig(*a, **kw)

        sid = "offload-thread"
        tr._new_session(sid)
        body, _ = self._masked_body(sid)
        with mock.patch.object(tr, "_response_offload", spy):
            asyncio.run(tr.response(self._flow(body, sid)))
        self.assertTrue(seen, "没走到 offload")
        self.assertTrue(all("maskit-aux" in n for n in seen),
                        "重活没下池，实际线程：%r" % seen)

    def test_event_loop_stays_responsive_during_offload(self):
        """事件循环在等待期间能继续跑别的协程（而不是被长段 CPU 占住）。"""
        ticks = {"n": 0}
        orig = tr._restore_json_body

        def slow(*a, **kw):
            time.sleep(0.15)          # 模拟一条大响应的解析/还原耗时
            return orig(*a, **kw)

        async def main(flow):
            stop = time.perf_counter() + 0.12

            async def ticker():
                while time.perf_counter() < stop:
                    ticks["n"] += 1
                    await asyncio.sleep(0.005)

            await asyncio.gather(ticker(), tr.response(flow))

        sid = "offload-loop"
        tr._new_session(sid)
        body, _ = self._masked_body(sid)
        with mock.patch.object(tr, "_restore_json_body", slow):
            asyncio.run(main(self._flow(body, sid)))
        self.assertGreater(ticks["n"], 5,
                           "等待期间事件循环没能推进其它协程（ticks=%d）" % ticks["n"])

    def test_audit_block_is_applied_by_the_loop(self):
        """审计熔断：aux 线程只构造响应，由事件循环回写（不跨线程写 flow）。"""
        sid = "offload-audit"
        tr._new_session(sid)
        body, _ = self._masked_body(sid)
        flow = self._flow(body, sid)
        fake_block = object()
        with mock.patch.object(tr, "_audit_response", lambda *a, **k: fake_block):
            asyncio.run(tr.response(flow))
        # fake_block 替换了 flow.response（本用例只关心"回写发生在循环侧"这条契约）
        self.assertIs(flow.response, fake_block)

    def test_offload_reports_apply_block_false(self):
        """`_response_offload` 调审计时必须 apply_block=False（否则会跨线程写 flow）。"""
        calls = []
        orig = tr._audit_response

        def spy(*a, **kw):
            calls.append(kw.get("apply_block"))
            return orig(*a, **kw)

        sid = "offload-apply"
        tr._new_session(sid)
        body, _ = self._masked_body(sid)
        with mock.patch.object(tr, "_audit_response", spy):
            asyncio.run(tr.response(self._flow(body, sid)))
        self.assertEqual(calls, [False],
                         "audit 必须由事件循环回写，实际 apply_block=%r" % calls)

    def test_streamed_response_is_untouched_by_response_hook(self):
        """流式响应已在 stream 回调里收尾：response 钩子必须立刻返回（不重复处理）。"""
        sid = "offload-stream"
        tr._new_session(sid)
        body, _ = self._masked_body(sid)
        flow = self._flow(body, sid, ct="text/event-stream")
        flow.metadata["shield_streamed"] = True
        asyncio.run(tr.response(flow))
        self.assertEqual(flow.response.content, body, "流式路径被整包钩子改写了")

    def test_restore_error_is_reported_and_does_not_raise(self):
        """还原抛异常时：记 ERR、不改 body、不把异常抛进 mitmproxy。"""
        sid = "offload-err"
        tr._new_session(sid)
        flow = self._flow(b"{not json", sid)
        asyncio.run(tr.response(flow))
        self.assertEqual(flow.response.content, b"{not json")
        self.assertIn("ERR", [t for t, _ in self.emitted])

    def test_restore_failure_still_runs_audit_and_scan(self):
        """还原失败**不得**跳过审计与响应扫描（0.6.0 修 fail-open 回归）。

        0.5.0 的实现里，还原异常只置 ok=False，紧接着的 `_audit_response` 与
        `_scan_response` 无条件执行；A-3 重构时在 except 里加了 return，等于
        「body 形态能诱发还原异常」⇒「这条响应不审计、不落响应扫描」——
        输入可控地关掉一层安全检测。旧用例只断言"不抛异常、不改 body"，
        所以它当时是绿的（断言打在错误的边界上）。
        """
        sid = "offload-failopen"
        tr._new_session(sid)
        flow = self._flow(b"{not json", sid)
        audit_calls, scan_calls = [], []
        real_audit, real_scan = tr._audit_response, tr._scan_response
        try:
            tr._audit_response = lambda *a, **k: audit_calls.append(1)
            tr._scan_response = lambda *a, **k: scan_calls.append(1)
            asyncio.run(tr.response(flow))
        finally:
            tr._audit_response, tr._scan_response = real_audit, real_scan
        self.assertTrue(audit_calls, "还原失败后审计被跳过了（fail-open 回归）")
        self.assertTrue(scan_calls, "还原失败后响应扫描被跳过了（fail-open 回归）")

    def test_stream_finish_audit_runs_off_the_event_loop(self):
        """流式收尾的审计与响应扫描必须**不在事件循环线程**上执行。

        收尾回调（`_sse_stream_factory` 的 `_finish`）是 mitmproxy 的**同步** stream
        回调，过去它把审计 + 响应扫描（实测 256KB 留存文本 ≈ 35ms）直接跑在事件循环上，
        每个流式响应都会卡住所有并发请求。现在投递到 `_AUX_POOL`。
        若哪天又被改回内联执行，本用例的线程断言会变红。
        """
        sid = "offload-stream-thread"
        tr._new_session(sid)
        original = "联系人 " + FAKE_EMAIL
        masked = tr.mask(original, sid)
        flow = self._flow(b"", sid, ct="text/event-stream")
        called = []
        real_audit, real_scan = tr._audit_response, tr._scan_response
        loop_thread = threading.current_thread().name

        def spy(kind):
            def _f(*a, **k):
                called.append((kind, threading.current_thread().name))
            return _f

        try:
            tr._audit_response = spy("audit")
            tr._scan_response = spy("scan")
            stream = tr._sse_stream_factory(flow, sid, "api.openai.com", "POST",
                                            "/v1/chat/completions", {})
            stream(('data: %s\n\n' % json.dumps(
                {"choices": [{"delta": {"content": masked}}]}, ensure_ascii=False)).encode())
            self.assertEqual(called, [], "收尾前不该已经跑过审计")
            stream(b"")                      # 触发 _finish（同步返回）
            self.assertTrue(tr.aux_drain(5.0), "收尾任务没在 5s 内跑完")
        finally:
            tr._audit_response, tr._scan_response = real_audit, real_scan
        self.assertTrue(called, "收尾没有执行审计/响应扫描")
        for kind, thread_name in called:
            self.assertNotEqual(thread_name, loop_thread,
                                "%s 跑在调用线程上（又改回内联执行了）" % kind)

    def test_deferred_drop_does_not_wipe_a_recreated_session(self):
        """延后 drop 不得抹掉"同一个 sid 的新会话"（0.6.0 回归守卫）。

        场景：流式收尾投递到 aux 池后**不等待**，而 `_drop(sid)` 也随之延后。若此时
        同一个 sid 上已经有了新会话（同一 sid 的连续两次流 —— 测试里就是这么写的，
        生产里换会话复用同一 sid 也一样），无条件 `sessions.pop(sid)` 会把新会话连同
        rev 表一起抹掉 → 占位符还原不回来。
        这条用例把收尾**故意拖慢**（sleep 60ms），让延后 drop 必定落在重建之后 ——
        修复前必红（不是概率红），修复后必绿。
        """
        sid = "offload-recreate"
        tr._new_session(sid)
        real_audit, real_scan = tr._audit_response, tr._scan_response
        tr._audit_response = lambda *a, **k: time.sleep(0.06)
        tr._scan_response = lambda *a, **k: None
        try:
            flow = self._flow(b"", sid, ct="text/event-stream")
            stream = tr._sse_stream_factory(flow, sid, "api.openai.com", "POST",
                                            "/v1/chat/completions", {})
            stream(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
            stream(b"")                       # 投递收尾（同步返回，不等待）
            tr._new_session(sid)              # 同 sid 重建会话
            tr.sessions[sid]["rev"]["{{NAME_abcdfg}}"] = "Carol"
            self.assertTrue(tr.aux_drain(5.0), "收尾任务没在 5s 内跑完")
        finally:
            tr._audit_response, tr._scan_response = real_audit, real_scan
        self.assertIn("{{NAME_abcdfg}}", (tr.sessions.get(sid) or {}).get("rev", {}),
                      "延后 drop 抹掉了同一个 sid 的新会话（还原表被清空）")

    def test_slow_aux_wait_is_traced_with_a_timestamped_event(self):
        """响应侧等 aux 池超过阈值必须留痕（0.6.0 质量修正）。

        背景：这条 await **刻意不设硬超时** —— 超时以后要么把未还原的响应交给客户端
        （占位符泄漏，产品的核心承诺就没了），要么改回 503（把一条已成功的上游响应判死），
        两种"修法"都比等待更糟。但不留痕的等待不可诊断：弱机 + 多智能体并发时，
        用户只看到"响应很慢"，日志里什么都没有。
        所以阈值一过就发一条带 `aux_wait_ms` 的事件。这里把阈值压到 1ms、让 offload
        睡 60ms，断言真的发了（而不是只断言"代码里有这个分支"）。
        """
        sid = "aux-wait-trace"
        tr._new_session(sid)
        body, _ = self._masked_body(sid)
        real_offload = tr._response_offload
        real_threshold = tr._AUX_WAIT_TRACE_S
        real_max = tr._AUX_WAIT_TRACE_MS[0]

        def slow_offload(*a, **k):
            time.sleep(0.06)
            return real_offload(*a, **k)

        tr._response_offload = slow_offload
        tr._AUX_WAIT_TRACE_S = 0.001
        tr._AUX_WAIT_TRACE_MS[0] = 0.0
        try:
            asyncio.run(tr.response(self._flow(body, sid)))
        finally:
            tr._response_offload = real_offload
            tr._AUX_WAIT_TRACE_S = real_threshold

        traces = [(t, k) for t, k in self.emitted
                  if k.get("reason") == "response_offload_wait"]
        self.assertEqual(len(traces), 1, "慢等待没有留痕（用户只能看到'响应很慢'）")
        _, kw = traces[0]
        self.assertGreaterEqual(kw.get("aux_wait_ms", 0), 50.0, "留痕里没有可信的耗时")
        self.assertIn("等待脱敏线程池", kw.get("msg", ""))
        # 指标侧也要能看到"本进程最长等过多久"
        self.assertGreaterEqual(tr.aux_pool_stats()["max_wait_ms"], 50.0)
        tr._AUX_WAIT_TRACE_MS[0] = real_max

    def test_aux_pool_stats_shape(self):
        """C-1 的取数接口：面板要读得到这些键。"""
        st = tr.aux_pool_stats()
        for key in ("submitted", "completed", "failed", "queue_depth"):
            self.assertIn(key, st)


if __name__ == "__main__":
    unittest.main()
