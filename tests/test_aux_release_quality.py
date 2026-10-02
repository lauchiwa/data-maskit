"""Release regressions for native flows, GC, decoding admission and audit coverage."""
import asyncio
import concurrent.futures
import copy
import gc
import gzip
import io
import os
import subprocess
import sys
import threading
import unittest
import weakref
from pathlib import Path
from unittest import mock

from mitmproxy import io as flow_io
from mitmproxy.test import tflow

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import transparent as tr


class AuxReleaseQualityTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(tr.aux_drain())
        self.events = []
        self.emit = mock.patch.object(tr, "_emit", lambda typ, **kw: self.events.append((typ, kw)))
        self.emit.start()
        self.addCleanup(self.emit.stop)
        self.addCleanup(lambda: self.assertTrue(tr.aux_drain()))

    def flow(self, sid):
        flow = tflow.tflow(resp=True)
        flow.request = tr.http.Request.make("POST", "https://api.openai.com/v1/chat/completions", b"{}")
        flow.response = tr.http.Response.make(200, b'{"text":"test"}', {"content-type": "application/json"})
        flow.metadata = {"session_id": sid}
        tr._new_session(sid)
        self.addCleanup(lambda: tr._aux_abandon(flow))
        return flow

    def test_real_cyclic_finalizer_reenters_admission_in_subprocess(self):
        # An actual finalizer is collected during an allocation under admission's
        # lock, not just a mocked nested release. A deadlock cannot hang the suite.
        script = '''
import gc
from types import SimpleNamespace
from unittest import mock
import transparent as tr
tr._AUX_JOBS = tr._AUX_BYTES = 0
gc.disable()
victim = SimpleNamespace(metadata={}, request=None)
victim.cycle = victim
tr._aux_reserve(victim)
del victim
factory = tr._AuxReservation
collected = []
def allocate(*args, **kwargs):
    collected.append(gc.collect())
    return factory(*args, **kwargs)
flow = SimpleNamespace(metadata={}, request=None)
with mock.patch.object(tr, "_AuxReservation", allocate):
    token = tr._aux_reserve(flow)
assert collected[0] > 0
assert tr._AUX_JOBS == 1, tr._AUX_JOBS
token.release()
token.release()
assert (tr._AUX_JOBS, tr._AUX_BYTES) == (0, 0)
'''
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(tr.__file__).parent) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run([sys.executable, "-c", script], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_serialization_copy_and_replay_have_independent_owners(self):
        flow = self.flow("aux-native")
        token = tr._aux_reserve(flow)
        state = flow.get_state()
        self.assertNotIn("shield_aux_reservation", state["metadata"])
        output = io.BytesIO()
        flow_io.FlowWriter(output).add(flow)
        output.seek(0)
        restored = next(flow_io.FlowReader(output).stream())
        for clone in (flow.copy(), restored, copy.deepcopy(flow)):
            with self.subTest(clone=clone.id):
                self.assertIsNone(tr._aux_token(clone))
                tr._aux_abandon(clone)
                self.assertFalse(token.released)
                own = tr._aux_reserve(clone)
                self.assertIsNot(own, token)
                tr._aux_abandon(clone)
        token.release()
        flow.set_state(state)
        fresh = tr._aux_reserve(flow)
        self.assertIsNot(fresh, token)
        self.assertFalse(fresh.released)
        tr._aux_abandon(flow)
        # Native in-place replay processes a legitimate upstream response again.
        tr._new_session("aux-native")
        asyncio.run(tr.response(flow))
        self.assertEqual(flow.response.status_code, 200)

    def test_replay_does_not_steal_live_submitted_owner(self):
        entered, release = threading.Event(), threading.Event()
        flow = self.flow("aux-live-replay")
        token = tr._aux_reserve(flow)
        state = flow.get_state()
        def blocked():
            entered.set()
            release.wait(3)
        try:
            tr._aux_submit(token, "aux-live-replay", token.session_ref, blocked)
            self.assertTrue(entered.wait(2))
            flow.set_state(state)
            self.assertIsNone(tr._aux_reserve(flow))
            self.assertIs(tr._aux_token(flow), token)
            self.assertFalse(token.released)
            clone = flow.copy()
            tr._aux_abandon(clone)
            self.assertFalse(token.abandoned)
        finally:
            release.set()
            self.assertTrue(tr.aux_drain())

    def test_five_tiny_gzip_responses_queue_until_actual_budget(self):
        flows = [self.flow("aux-tiny-%d" % i) for i in range(6)]
        for flow in flows:
            flow.response.headers["content-encoding"] = "gzip"
            flow.response.raw_content = gzip.compress(b'{"text":"test"}')
        entered, release = threading.Event(), threading.Event()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        worker = tr._response_offload
        baseline = tr.aux_pool_stats()
        def blocked(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("worker not released")
            return worker(*args)
        async def exercise():
            tasks = [asyncio.create_task(tr.response(flow)) for flow in flows[:5]]
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set() or tr.aux_pool_stats()["reserved_jobs"] < baseline["reserved_jobs"] + 5:
                        await asyncio.sleep(.001)
                self.assertTrue(all(flow.response.status_code == 200 for flow in flows[:5]))
                expected = baseline["reserved_bytes"] + 5 * (tr._AUX_BASE_BYTES + 2)
                self.assertEqual(tr.aux_pool_stats()["reserved_bytes"], expected)
                await tr.response(flows[5])
                self.assertEqual(flows[5].response.status_code, 503)
                self.assertEqual(flows[5].response.headers["x-should-retry"], "false")
            finally:
                release.set()
                await asyncio.gather(*tasks)
            self.assertTrue(all(flow.response.status_code == 200 for flow in flows[:5]))
        with mock.patch.object(tr, "_AUX_POOL", pool), \
             mock.patch.object(tr, "_AUX_MAX_BYTES", baseline["reserved_bytes"] + 5 * (tr._AUX_BASE_BYTES + 2)), \
             mock.patch.object(tr, "_response_offload", blocked):
            try:
                asyncio.run(exercise())
            finally:
                release.set()
                pool.shutdown(wait=True)
        self.assertEqual(tr.aux_pool_stats()["reserved_bytes"], baseline["reserved_bytes"])

    def test_actual_decoded_growth_is_atomic_and_budgeted(self):
        flow = self.flow("aux-growth")
        body = b"x" * (tr._AUX_BASE_BYTES + 100)
        flow.response.headers["content-encoding"] = "gzip"
        flow.response.raw_content = gzip.compress(body)
        token = tr._aux_reserve(flow, len(flow.response.raw_content))
        snapshot = tr._aux_snapshot(flow)
        initial = tr.aux_pool_stats()["reserved_bytes"]
        with mock.patch.object(tr, "_AUX_MAX_BYTES", initial):
            with self.assertRaises(RuntimeError):
                _ = snapshot.response.content
        self.assertIsNone(snapshot.response._content)
        self.assertEqual(tr.aux_pool_stats()["reserved_bytes"], initial)
        with mock.patch.object(tr, "decode_body", side_effect=AssertionError("decoded twice")):
            with self.assertRaises(ValueError):
                _ = snapshot.response.content
        snapshot = tr._aux_snapshot(flow)
        self.assertEqual(snapshot.response.content, body)
        self.assertEqual(token.size, len(flow.request.raw_content) + len(flow.response.raw_content) + len(body))
        tr._aux_abandon(flow)

    def test_expanded_masked_request_above_response_cap_still_audits(self):
        flow = self.flow("aux-expanded-audit")
        # Incoming admission was valid; replacing small literals with placeholders
        # can expand the already-accounted masked body beyond the response limit.
        token = tr._aux_reserve(flow)
        expanded = b" " * (tr._AUX_RESPONSE_MAX + 1)
        flow.request.raw_content = expanded
        flow.metadata["shield_request_decoded_bytes"] = len(expanded)
        self.assertIs(tr._aux_reserve(flow), token)
        with mock.patch.object(tr, "AUDIT_ENABLED", True), \
             mock.patch.object(tr, "_audit_cache_get", return_value=None), \
             mock.patch.object(tr, "_audit_scan_signals", return_value=[]) as scan:
            asyncio.run(tr.response(flow))
        scan.assert_called_once()
        self.assertEqual(flow.response.status_code, 200)
        self.assertTrue(token.released)

    def test_empty_encoding_round_trip_and_worker_decoding_limit(self):
        flow = self.flow("aux-empty-encoding")
        flow.response.headers["content-encoding"] = ""
        asyncio.run(tr.response(flow))
        self.assertEqual(flow.response.status_code, 200)
        self.assertEqual(tr.json.loads(flow.response.raw_content), {"text": "test"})
        flow = self.flow("aux-decode-limit")
        flow.response.headers["content-encoding"] = "gzip"
        flow.response.raw_content = gzip.compress(b"x" * 1000)
        with mock.patch.object(tr, "_AUX_RESPONSE_MAX", 100):
            asyncio.run(tr.response(flow))
        self.assertEqual(flow.response.status_code, 503)
        self.assertEqual(flow.response.headers["x-should-retry"], "false")
        self.assertNotIn("retry-after", flow.response.headers)

    def test_retained_sse_callback_and_future_do_not_keep_retired_session(self):
        class Session(dict):
            pass
        for fail in (False, True):
            with self.subTest(fail=fail):
                flow = self.flow("aux-sse-retention")
                owned = Session(tr.sessions["aux-sse-retention"])
                tr.sessions["aux-sse-retention"] = owned
                ref = weakref.ref(owned)
                token = tr._aux_reserve(flow)
                callback = tr._sse_stream_factory(flow, "aux-sse-retention", "api.openai.com", "POST", "/v1/test", {})
                del owned
                if fail:
                    with mock.patch.object(tr._AUX_POOL, "submit", side_effect=RuntimeError("stopped")):
                        callback(b"")
                else:
                    callback(b"")
                self.assertTrue(tr.aux_drain())
                self.assertTrue(token.released)
                self.assertIsNone(token.future)
                self.assertIsNone(token.session_ref)
                gc.collect()
                self.assertIsNone(ref())
                self.assertEqual(callback(b"already finished"), b"already finished")

    def test_stream_queue_metric_uses_measured_queue_wait(self):
        flow = self.flow("aux-stream-metric")
        before = tr.aux_pool_stats().get("wait_ms_total", 0)
        with mock.patch.object(tr.time, "perf_counter", return_value=10), \
             mock.patch.object(tr, "_audit_response"), mock.patch.object(tr, "_scan_response"):
            tr._stream_finish_offload(tr._aux_snapshot(flow), "aux-stream-metric", "host", "POST", "/v1/test", {}, "", enqueued_at=9.5)
        self.assertAlmostEqual(tr.aux_pool_stats()["wait_ms_total"] - before, 500)


if __name__ == "__main__":
    unittest.main()
