"""Real mitmproxy stream errors wake the actual Maskit hooks, without cancelling them."""
import asyncio
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

from mitmproxy import io as flow_io
from mitmproxy.proxy import events
from mitmproxy.proxy.layers.http import HttpErrorHook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import transparent as tr
from tests.test_stream_cancellation import HTTPDriver


class NativeWorkCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        for handle in (
            patch.object(tr, "_maybe_reload", lambda *a, **k: None),
            patch.object(tr, "write_runtime_metrics", lambda *a, **k: None),
            patch.object(tr, "_emit", lambda typ, **kw: self.events.append((typ, kw))),
            patch.object(tr, "CAPTURE_MODE", "explicit"),
            patch.object(tr, "FILTER_ENABLED", True),
            patch.object(tr, "NER_ENABLED", False),
            patch.object(tr, "is_target", lambda *a: True),
        ):
            handle.start()
            self.addCleanup(handle.stop)
        self.assertTrue(tr._CONNECTIONS.running())
        self.addCleanup(tr._CONNECTIONS.done)
        self.mask_baseline = tr.mask_pool_stats()["inflight"]
        self.aux_baseline = tr.aux_pool_stats()["reserved_jobs"]

    async def until(self, predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(.001)

    async def test_h2_reset_returns_from_real_request_hook_before_worker_exits(self):
        driver = HTTPDriver(use_h2=True)
        hook = driver.request()
        flow = hook.flow
        flow.request.path = "/v1/chat/completions"
        flow.request.headers["content-type"] = "application/json"
        flow.request.content = json.dumps({"model": "local-test", "messages": [
            {"role": "user", "content": "synthetic cancellation"}]}).encode()
        entered, release = threading.Event(), threading.Event()
        def blocked(value, *args, **kwargs):
            entered.set()
            release.wait(4)
            return value
        with patch.object(tr, "_mask_tree", blocked):
            task = asyncio.create_task(tr.request(flow))
            try:
                await self.until(entered.is_set)
                # Runtime cancellation state must not poison native saving or copying.
                output = io.BytesIO()
                flow_io.FlowWriter(output).add(flow)
                self.assertTrue(output.getvalue())
                copied = flow.copy()
                self.assertFalse(hasattr(copied, "_shield_mask_cancel"))
                self.assertIsNone(tr._aux_token(copied))
                driver.disconnect()
                await asyncio.wait_for(task, 1)
                self.assertFalse(task.cancelled())
                self.assertIn(b"shield_request_cancelled", flow.response.content)
                self.assertEqual(tr.mask_pool_stats()["inflight"], self.mask_baseline + 1)
                errors = driver.dispatch(events.HookCompleted(hook))
                error_hook = driver.hook(errors, HttpErrorHook)
                tr.error(error_hook.flow)
                driver.dispatch(events.HookCompleted(error_hook))
                self.assertFalse(driver.layer.streams)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await self.until(lambda: tr.mask_pool_stats()["inflight"] == self.mask_baseline)
        self.assertEqual(sum(kind == "CANCEL" for kind, _ in self.events), 1)
        self.assertFalse(any(kind in ("MASK", "RESTORE") for kind, _ in self.events))
        self.assertEqual(tr.aux_pool_stats()["reserved_jobs"], self.aux_baseline)

    async def test_h1_eof_returns_from_response_hook_without_false_restore(self):
        driver = HTTPDriver()
        request = driver.request()
        flow = request.flow
        sid = "native-response-cancel"
        tr._new_session(sid)
        flow.metadata["session_id"] = sid
        tr._CONNECTIONS.request_started(flow)
        self.assertIsNotNone(tr._aux_reserve(flow))
        response = driver.response_hook(request)
        tr._CONNECTIONS.responseheaders(flow)
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(4)
            return (b"{}", True, None, None, None)
        with patch.object(tr, "_response_offload", blocked):
            task = asyncio.create_task(tr.response(flow))
            try:
                await self.until(entered.is_set)
                token = tr._aux_token(flow)
                driver.disconnect()
                await asyncio.wait_for(task, 1)
                self.assertFalse(task.cancelled())
                self.assertFalse(token.released)
                self.assertEqual(tr.aux_pool_stats()["reserved_jobs"], self.aux_baseline + 1)
                driver.dispatch(events.HookCompleted(response))
                self.assertFalse(any(isinstance(c, HttpErrorHook) for c in driver.commands))
                self.assertFalse(driver.layer.streams)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await self.until(lambda: tr.aux_pool_stats()["reserved_jobs"] == self.aux_baseline)
        self.assertNotIn(sid, tr.sessions)
        self.assertEqual(sum(kind == "CANCEL" for kind, _ in self.events), 1)
        self.assertFalse(any(kind == "RESTORE" for kind, _ in self.events))


if __name__ == "__main__":
    unittest.main()
