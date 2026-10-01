"""Deterministic local-only ownership, cancellation and bounded-input contracts."""
import asyncio
import concurrent.futures
import copy
import gc
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import transparent as tr


class AuxLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(tr.aux_drain())
        self.events = []
        self.emit = mock.patch.object(tr, "_emit", lambda typ, **kw: self.events.append((typ, kw)))
        self.emit.start()
        self.addCleanup(self.emit.stop)
        self.addCleanup(lambda: self.assertTrue(tr.aux_drain()))

    def flow(self, sid="aux-owned"):
        tr._new_session(sid)
        tr.sessions[sid]["rev"]["{{NAME_bcdfgh}}"] = "synthetic-original"
        return SimpleNamespace(
            request=tr.http.Request.make("POST", "https://api.openai.com/v1/chat/completions", b"{}"),
            response=tr.http.Response.make(200, b'{"text":"{{NAME_bcdfgh}}"}',
                                           {"content-type": "application/json"}),
            metadata={"session_id": sid})

    async def started(self, event):
        async with asyncio.timeout(2):
            while not event.is_set():
                await asyncio.sleep(.001)

    def test_running_timeout_owns_snapshot_and_quota_until_actual_completion(self):
        entered, release = threading.Event(), threading.Event()
        flow = self.flow()
        owned = tr.sessions["aux-owned"]
        observed = []
        worker = tr._response_offload

        def blocked(snapshot, *args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release worker")
            observed.append((snapshot.response.status_code, snapshot.request.content,
                             tr._session_get("aux-owned") is owned))
            return worker(snapshot, *args)

        async def exercise():
            task = asyncio.create_task(tr.response(flow))
            await self.started(entered)
            await task
            token = flow.metadata["shield_aux_reservation"]
            self.assertFalse(token.released)
            self.assertEqual(flow.response.status_code, 503)
            self.assertNotIn("retry-after", flow.response.headers)
            flow.request.content = b'{"changed":true}'
            tr._new_session("aux-owned")
            replacement = tr.sessions["aux-owned"]
            release.set()
            while not token.released:
                await asyncio.sleep(.001)
            self.assertIs(tr.sessions["aux-owned"], replacement)

        with mock.patch.object(tr, "_response_offload", blocked), \
             mock.patch.object(tr, "_AUX_WAIT_HARD_S", .03), \
             mock.patch.object(tr, "_audit_response") as audit, \
             mock.patch.object(tr, "_scan_response") as scan:
            audit.return_value = None
            try:
                asyncio.run(exercise())
            finally:
                release.set()
                tr.aux_drain()
            self.assertEqual(observed, [(200, b"{}", True)])
            self.assertEqual(audit.call_count, 1)
            self.assertEqual(scan.call_count, 1)
        self.assertFalse(any(kind == "RESTORE" for kind, _ in self.events))

    def test_cancel_running_job_keeps_capacity_and_suppresses_success(self):
        entered, release = threading.Event(), threading.Event()
        flow = self.flow("aux-cancel")

        def blocked(*args):
            entered.set()
            release.wait(3)
            return (b"{}", True, None, None, "{}")

        async def exercise():
            task = asyncio.create_task(tr.response(flow))
            await self.started(entered)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(flow.metadata["shield_aux_reservation"].released)
            release.set()

        with mock.patch.object(tr, "_response_offload", blocked):
            try:
                asyncio.run(exercise())
            finally:
                release.set()
                self.assertTrue(tr.aux_drain())
        self.assertTrue(flow.metadata["shield_aux_reservation"].released)
        self.assertFalse(any(kind == "RESTORE" for kind, _ in self.events))

    def test_cancelled_queue_entries_keep_budget_until_dequeued(self):
        entered, release = threading.Event(), threading.Event()
        first, second, third = self.flow("aux-q1"), self.flow("aux-q2"), self.flow("aux-q3")
        baseline = tr.aux_pool_stats()["reserved_jobs"]
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        ran = []

        def blocked():
            entered.set()
            release.wait(3)

        with mock.patch.object(tr, "_AUX_POOL", pool), \
             mock.patch.object(tr, "_AUX_MAX_JOBS", baseline + 2):
            try:
                one = tr._aux_reserve(first)
                tr._aux_submit(one, "aux-q1", tr.sessions["aux-q1"], blocked)
                self.assertTrue(entered.wait(2))
                two = tr._aux_reserve(second)
                tr._aux_submit(two, "aux-q2", tr.sessions["aux-q2"], lambda: ran.append(True))
                tr._aux_abandon(second)
                self.assertFalse(two.released)
                self.assertIsNone(tr._aux_reserve(third))
                release.set()
                self.assertTrue(tr.aux_drain())
                self.assertTrue(two.released)
                self.assertEqual(ran, [])
            finally:
                release.set()
                pool.shutdown(wait=True)
        self.assertEqual(tr.aux_pool_stats()["reserved_jobs"], baseline)

    def test_byte_resize_rejection_and_idempotent_release(self):
        flow = self.flow("aux-bytes")
        before = tr.aux_pool_stats()
        token = tr._aux_reserve(flow)
        reserved = tr.aux_pool_stats()["reserved_bytes"]
        with mock.patch.object(tr, "_AUX_MAX_BYTES", reserved):
            self.assertIsNone(tr._aux_reserve(flow, tr._AUX_BASE_BYTES + 1))
        self.assertEqual(tr.aux_pool_stats()["reserved_bytes"], reserved)
        tr._aux_abandon(flow)
        tr._aux_abandon(flow)
        self.assertTrue(token.released)
        self.assertEqual(tr.aux_pool_stats()["reserved_bytes"], before["reserved_bytes"])

    def test_submit_failure_is_explicit_and_does_not_run_inline(self):
        flow = self.flow("aux-submit")
        callback = tr._sse_stream_factory(flow, "aux-submit", "api.openai.com", "POST", "/v1/test", {})
        with mock.patch.object(tr._AUX_POOL, "submit", side_effect=RuntimeError("stopped")), \
             mock.patch.object(tr, "_stream_finish_offload") as worker:
            callback(b"")
        worker.assert_not_called()
        self.assertTrue(flow.metadata["shield_aux_reservation"].released)
        self.assertTrue(any(kw.get("reason") == "stream_finish_failed" for _, kw in self.events))
        self.assertTrue(tr.aux_drain())

    def test_discarded_unsubmitted_flow_and_copies_have_one_owner(self):
        before = tr.aux_pool_stats()["reserved_jobs"]
        flow = self.flow("aux-gc")
        token = tr._aux_reserve(flow)
        self.assertIs(copy.deepcopy(token), token)
        del token, flow
        gc.collect()
        self.assertEqual(tr.aux_pool_stats()["reserved_jobs"], before)

    def test_request_rejection_precedes_mask_submission(self):
        # Full request setup is shared with the existing suite; this contract also
        # guards against accidentally moving aux admission after upstream return.
        import inspect
        source = inspect.getsource(tr._request_impl)
        self.assertLess(source.index("_aux_reserve(flow)"), source.index("_mask_admit("))
        self.assertLess(source.index("_aux_reserve(flow)"), source.index("run_in_executor("))

    def test_local_request_rejection_is_not_reprocessed_as_upstream_response(self):
        flow = self.flow("aux-local-block")
        async def reject(f):
            tr._aux_reserve(f)
            f.response = tr.http.Response.make(503, b'{"error":{"code":"shield_busy"}}',
                                               {"retry-after": "5"})
        async def exercise():
            await tr.request(flow)
            before = flow.response.content
            tr.responseheaders(flow)
            await tr.response(flow)
            self.assertEqual(flow.response.content, before)
            self.assertEqual(flow.response.headers["retry-after"], "5")
        with mock.patch.object(tr, "_request_impl", reject), \
             mock.patch.object(tr, "_response_offload") as worker:
            asyncio.run(exercise())
            worker.assert_not_called()

    def test_completed_token_does_not_retain_future_or_session(self):
        flow = self.flow("aux-no-retain")
        asyncio.run(tr.response(flow))
        token = flow.metadata["shield_aux_reservation"]
        self.assertTrue(token.released)
        self.assertIsNone(token.future)
        self.assertIsNone(token.session_ref)

    def test_gzip_decodes_once_and_summary_is_prepared_off_loop(self):
        from mitmproxy.net import encoding
        flow = self.flow("aux-gzip")
        original = flow.response.content
        flow.response.headers["content-encoding"] = "gzip"
        flow.response.content = original
        loop_thread = threading.get_ident()
        calls, preparations = [], []
        decode = encoding.decode
        prepare = tr._emit_restore_summary
        def checked_decode(raw, kind, *args, **kwargs):
            if kind == "gzip":
                calls.append(threading.get_ident())
                self.assertNotEqual(calls[-1], loop_thread)
            return decode(raw, kind, *args, **kwargs)
        def checked_prepare(*args, **kwargs):
            preparations.append(threading.get_ident())
            self.assertTrue(kwargs.get("prepare_only"))
            self.assertNotEqual(preparations[-1], loop_thread)
            return prepare(*args, **kwargs)
        with mock.patch.object(encoding, "decode", checked_decode), \
             mock.patch.object(tr, "_emit_restore_summary", checked_prepare):
            asyncio.run(tr.response(flow))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(preparations), 1)
        self.assertIn(b"synthetic-original", flow.response.content)

    def test_worker_width_is_conservative_and_clamped(self):
        for requested, expected in [("1", 1), ("2", 2), ("99", 4), ("-4", 1)]:
            with mock.patch.dict(tr.os.environ, {"MASKIT_AUX_WORKERS": requested}):
                self.assertEqual(tr._aux_pool_width(), expected)


if __name__ == "__main__":
    unittest.main()
