"""Usage snapshots from supported SSE protocols must merge without double counting."""
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import shield_defaults as defaults
import panel
import transparent as tr


def event(data):
    return "data: " + json.dumps(data) + "\n\n"


class StreamingUsageTests(unittest.TestCase):
    def setUp(self):
        """把 PT 还原映射钉成**空** —— `forward_response` 断言的是「逐字节原样透传」。

        `_pt_restore_map()` 未设 `LLM_SHIELD_DATA_DIR` 时会读 `%APPDATA%\\Maskit` 的
        **真实**事件库；开发机上只要跑过一次真实代理，映射就非空，PT 于是启用还原链路
        并重写文本帧（实测把 `\\r\\n` 归一成 `\\n`），用例就以「透传改坏了字节」的假象失败。
        门禁必须确定性——不能取决于开发者今天有没有跑过代理。
        """
        patcher = mock.patch.object(panel, "_pt_restore_map", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_anthropic_snapshots_merge_without_summing_cumulative_counts(self):
        start = {"type": "message_start", "message": {"usage": {"input_tokens": 25, "output_tokens": 1}}}
        updates = [{"type": "message_delta", "usage": {"output_tokens": n}} for n in (10, 15)]
        text = "".join(event(d) for d in [start, *updates])
        expected = {"prompt_tokens": 25, "completion_tokens": 15}
        self.assertEqual(defaults.extract_usage(text), expected)
        usage = {}
        for data in [start, *updates]:
            usage = defaults.extract_usage(event(data), previous=usage)
        self.assertEqual(usage, expected)

    def test_response_terminal_events_and_existing_json_shapes(self):
        expected = {"prompt_tokens": 37, "completion_tokens": 11}
        for kind in ("response.completed", "response.incomplete", "response.failed"):
            with self.subTest(kind=kind):
                data = {"type": kind, "response": {"usage": {"input_tokens": 37, "output_tokens": 11}}}
                self.assertEqual(defaults.extract_usage(event(data)), expected)
        for data in ({"usage": expected}, {"usage": {"input_tokens": 37, "output_tokens": 11}},
                     {"meta": {"tokens": expected}}, {"usage": {}, "meta": {"tokens": expected}}):
            with self.subTest(data=data):
                self.assertEqual(defaults.extract_usage(json.dumps(data)), expected)
        self.assertEqual(defaults.extract_usage('{"usage":{"total_tokens":128}}'),
                         {"prompt_tokens": 128, "completion_tokens": 0})

    def test_invalid_fields_do_not_erase_usage_and_explicit_zero_is_valid(self):
        previous = {"prompt_tokens": 25, "completion_tokens": 15}
        for bad in (None, "not-a-count", -1, float("inf")):
            with self.subTest(bad=bad):
                data = {"usage": {"input_tokens": bad, "output_tokens": 0}}
                self.assertEqual(defaults.extract_usage(event(data), previous),
                                 {"prompt_tokens": 25, "completion_tokens": 0})
        self.assertEqual(previous, {"prompt_tokens": 25, "completion_tokens": 15})
        self.assertEqual(defaults.extract_usage("data: [DONE]\n\n", previous), previous)

    def test_stream_callback_keeps_usage_after_text_truncation_and_network_splits(self):
        sid = "usage-test"
        tr._new_session(sid)
        self.addCleanup(tr.sessions.pop, sid, None)
        flow = SimpleNamespace(metadata={})
        with mock.patch.object(tr, "_SSE_KEEP_MAX", 1), \
             mock.patch.object(tr, "_emit_restore_summary") as summary, \
             mock.patch.object(tr, "_audit_response"), \
             mock.patch.object(tr, "_scan_response"), \
             mock.patch.object(tr, "_emit") as emit:
            stream = tr._sse_stream_factory(flow, sid, "example.invalid", "POST", "/v1/messages", {})
            data = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 25, "output_tokens": 1}}},
                {"type": "content_block_delta", "index": 0, "delta": {"text": "test" * 100}},
                {"type": "message_delta", "usage": {"output_tokens": 15}},
                {"type": "message_stop"},
            ]
            raw = "".join(event(d) for d in data).encode()
            for offset in range(0, len(raw), 17):
                stream(raw[offset:offset + 17])
            stream(b"")
            tr.aux_drain()  # 收尾已投递 aux 池：等它落库再断言
        emit.assert_not_called()
        summary.assert_called_once()
        self.assertEqual(summary.call_args.kwargs["stream_usage"],
                         {"prompt_tokens": 25, "completion_tokens": 15})

    def test_total_fallback_with_zero_fields_remains_compatible(self):
        for fields in ({"completion_tokens": 0}, {"prompt_tokens": 0},
                       {"input_tokens": 0, "output_tokens": 0}):
            with self.subTest(fields=fields):
                data = {"usage": {"total_tokens": 128, **fields}}
                self.assertEqual(defaults.extract_usage(json.dumps(data)),
                                 {"prompt_tokens": 128, "completion_tokens": 0})
        self.assertEqual(defaults.extract_usage(event({"usage": {
            "total_tokens": 128, "prompt_tokens": 100, "completion_tokens": 28}})),
            {"prompt_tokens": 100, "completion_tokens": 28})

    def forward_response(self, raw, content_type, chunk_size):
        handler_type = panel._make_passthrough_handler("https://example.invalid")
        handler = object.__new__(handler_type)
        handler.headers = {}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/v1/messages"
        handler.command = "POST"
        for name in ("send_response", "send_header", "end_headers", "send_error"):
            setattr(handler, name, mock.Mock())
        headers = {"Content-Type": content_type, "Content-Length": str(len(raw))}
        response = mock.Mock(status=200)
        response.getheader.side_effect = lambda name, default=None: headers.get(name, default)
        response.getheaders.return_value = list(headers.items())
        response.read1.side_effect = [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)] + [b""]
        with mock.patch.object(panel.http.client, "HTTPSConnection") as connection, \
             mock.patch.object(panel, "enqueue_event") as enqueue:
            connection.return_value.getresponse.return_value = response
            handler._do_forward()
        handler.send_error.assert_not_called()
        connection.return_value.close.assert_called_once()
        self.assertEqual(handler.wfile.getvalue(), raw)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0]["type"], "PASS")
        return enqueue.call_args.args[0]["usage"]

    def test_passthrough_keeps_initial_usage_in_long_split_stream(self):
        start = {"type": "message_start", "message": {"usage": {"input_tokens": 25, "output_tokens": 1}}}
        content = {"type": "content_block_delta", "index": 0, "delta": {"text": "test" * 300}}
        end = {"type": "message_delta", "usage": {"output_tokens": 15}}
        raw = (event(start) + event(content) * 100 + event(end)).encode()
        self.assertGreater(len(raw), 65536)
        for size in (17, 65536):
            with self.subTest(chunk_size=size):
                self.assertEqual(self.forward_response(raw, "text/event-stream", size),
                                 {"prompt_tokens": 25, "completion_tokens": 15})

    def test_passthrough_skips_oversized_lines_and_recovers_usage_at_eof(self):
        start = event({"type": "message_start", "message": {"usage": {"input_tokens": 25}}})
        huge = event({"type": "content_block_delta", "delta": {"text": "x" * 100000}})
        end = event({"type": "message_delta", "usage": {"output_tokens": 15}}).rstrip()
        raw = (start + huge + end).replace("\n", "\r\n").encode()
        self.assertEqual(self.forward_response(raw, "text/event-stream", 4096),
                         {"prompt_tokens": 25, "completion_tokens": 15})

    def test_passthrough_keeps_non_streaming_json_usage(self):
        raw = json.dumps({"usage": {"prompt_tokens": 25, "completion_tokens": 15}}).encode()
        self.assertEqual(self.forward_response(raw, "application/json", 17),
                         {"prompt_tokens": 25, "completion_tokens": 15})


class WholeBodyRestoreUsageTests(unittest.TestCase):
    """非流式（整包）响应的 usage 必须仍从 body 里抽出来（0.6.0 回归守卫）。

    判据曾是 `streamed_text is not None`，而整包路径同样会传还原后的正文 →
    整包永远落进“流式”分支，`_extract_usage(body)` 成为死代码 → 非流式响应的
    token 用量恒为空，面板的日 token/费用统计整体塔掉。
    """

    def _flow(self, body: bytes):
        return SimpleNamespace(
            response=SimpleNamespace(status_code=200, content=body,
                                     headers={"content-type": "application/json"}),
            request=SimpleNamespace(headers={}),
            metadata={},
        )

    def _capture(self, body, **kw):
        sid = "usage-whole-body"
        tr._new_session(sid)
        self.addCleanup(lambda: tr.sessions.pop(sid, None))
        captured = {}
        with mock.patch.object(tr, "_emit",
                               side_effect=lambda kind, **fields: captured.update(fields)):
            tr._emit_restore_summary(self._flow(body), sid, "api.openai.com", "POST",
                                     "/v1/chat/completions", {}, ok=True, **kw)
        return captured

    def test_whole_body_usage_is_extracted_from_response_body(self):
        body = json.dumps({"choices": [{"message": {"content": "hi"}}],
                           "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                                     "total_tokens": 18}}).encode()
        # 忠实复现整包调用点：传 streamed_text=还原后正文，且不传 stream_usage
        captured = self._capture(body, streamed_text=body.decode("utf-8"))
        usage = captured.get("usage") or {}
        self.assertEqual(usage.get("prompt_tokens"), 11,
                         "整包响应丢了 usage（日 token/费用统计会塔）")
        self.assertEqual(usage.get("completion_tokens"), 7)
        self.assertEqual(captured.get("stream_actual"), "whole")

    def test_stream_path_never_reparses_whole_body(self):
        """互补红线：流式路径不得回退到“整段文本重解析”（性能）。"""
        body = json.dumps({"usage": {"prompt_tokens": 999}}).encode()
        with mock.patch.object(tr, "_extract_usage",
                               side_effect=AssertionError("流式路径不得重解析整段文本")):
            captured = self._capture(body, streamed_text="x", stream_actual="stream",
                                     stream_usage=None)
        self.assertIsNone(captured.get("usage"))

    def test_stream_error_path_keeps_collected_usage(self):
        """stream_error 也是流式：采集器采到了就用它的，没采到也不读 body。"""
        body = json.dumps({"usage": {"prompt_tokens": 999}}).encode()
        with mock.patch.object(tr, "_extract_usage",
                               side_effect=AssertionError("stream_error 不得重解析整段文本")):
            captured = self._capture(body, streamed_text="y", stream_actual="stream_error",
                                     stream_usage={"prompt_tokens": 3})
        self.assertEqual((captured.get("usage") or {}).get("prompt_tokens"), 3)

    def test_cache_and_reasoning_tokens_extraction(self):
        """测试各大厂商的缓存与思考 token 采集。"""
        # 1. OpenAI 格式（含 prompt_tokens_details.cached_tokens 与 reasoning_tokens）
        openai_payload = {
            "usage": {
                "prompt_tokens": 79741,
                "completion_tokens": 25,
                "total_tokens": 79766,
                "prompt_tokens_details": {
                    "audio_tokens": 0,
                    "cached_tokens": 77254,
                },
                "completion_tokens_details": {
                    "reasoning_tokens": 15,
                },
            }
        }
        res = defaults.extract_usage(json.dumps(openai_payload))
        self.assertEqual(res.get("prompt_tokens"), 79741)
        self.assertEqual(res.get("completion_tokens"), 25)
        self.assertEqual(res.get("cache_read_tokens"), 77254)
        self.assertEqual(res.get("reasoning_tokens"), 15)
        self.assertNotIn("cache_write_tokens", res)

        # 2. Anthropic 格式（含 cache_read_input_tokens 与 cache_creation_input_tokens）
        anthropic_payload = {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 2095,
                    "output_tokens": 503,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 1000,
                }
            }
        }
        res_anthropic = defaults.extract_usage(event(anthropic_payload))
        self.assertEqual(res_anthropic.get("prompt_tokens"), 2095)
        self.assertEqual(res_anthropic.get("completion_tokens"), 503)
        self.assertEqual(res_anthropic.get("cache_read_tokens"), 2000)
        self.assertEqual(res_anthropic.get("cache_write_tokens"), 1000)

        # 3. DeepSeek 格式（含 prompt_cache_hit_tokens）
        deepseek_payload = {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "prompt_cache_hit_tokens": 800,
                "prompt_cache_miss_tokens": 200,
            }
        }
        res_deepseek = defaults.extract_usage(json.dumps(deepseek_payload))
        self.assertEqual(res_deepseek.get("prompt_tokens"), 1000)
        self.assertEqual(res_deepseek.get("completion_tokens"), 200)
        self.assertEqual(res_deepseek.get("cache_read_tokens"), 800)

