"""响应还原遇到 `type` 值为对象/数组时不得抛异常（issue #44）。

JSON Schema 里 `type` 有两种非字符串形态，二者都不可 hash：

- 属性名形态：`{"properties": {"type": {"enum": [...], "type": "string"}}}`
- 保留字数组形态：`{"type": ["string", "null"]}`

`_restore_tree` 曾直接拿它做 set 成员判断，抛 `TypeError: unhashable type: 'dict'`。
流式下 `_stream_owned` 的兜底会丢掉**已出队的事件块**——OpenAI Responses API 的
`response.completed` 恰好回显整份 tools schema，于是终止事件整块蒸发，客户端只收到
无 `event:` / `data:` 前缀的残帧，判定「流异常中断」并反复重试。

这里同时钉住三条：根因判据、整包路径、流式分片不变性。
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import transparent as tr

# 占位符字面量按片段拼接：本机在网关之后，直接写完整 token 会被落盘还原
TOKEN = "{{NAME_" + "bcdfgh}}"
ORIGINAL = "Bob Builder"


def schema_with_non_string_type():
    return {
        "properties": {
            "type": {"enum": ["file", "snippet", "context"], "type": "string"},
            "tags": {"type": ["string", "null"]},
            "name": {"type": "string", "description": TOKEN},
        }
    }


def build_responses_stream(completed_pad=60000):
    """8 个小事件 + 1 个回显 tools 的 response.completed（与上游同形态的紧凑 JSON）。"""
    events = [
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "message", "id": "msg_1", "role": "assistant", "content": []}},
    ]
    events += [{"type": "response.output_text.delta", "output_index": 0,
                "content_index": 0, "delta": word} for word in ("O", "K")]
    events.append({"type": "response.output_text.done", "output_index": 0,
                   "content_index": 0, "text": "OK"})
    completed = {
        "type": "response.completed",
        "response": {
            "id": "resp_1",
            "status": "completed",
            "model": "test-model",
            "tools": [{"type": "function", "name": "probe", "description": "probe",
                       "parameters": {"type": "object", "properties": schema_with_non_string_type()}}],
            "extra_padding": "x" * completed_pad,
            "usage": {"input_tokens": 800, "output_tokens": 86,
                      "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 886},
        },
    }
    events.append(completed)
    return "".join(
        "event: %s\ndata: %s\n\n" % (e["type"], json.dumps(e, ensure_ascii=False, separators=(",", ":")))
        for e in events
    )


class RestoreTypeCollisionTests(unittest.TestCase):
    def setUp(self):
        for name in ("sessions", "_RECENT_FWD", "_RECENT_REV"):
            patcher = mock.patch.object(tr, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sid = "type-collision"
        tr._new_session(self.sid)
        tr.sessions[self.sid]["rev"][TOKEN] = ORIGINAL

    def test_restore_tree_keeps_schema_and_restores_siblings(self):
        out = tr._restore_tree({"tools": [{"parameters": schema_with_non_string_type()}]}, self.sid)
        props = out["tools"][0]["parameters"]["properties"]
        self.assertEqual(props["type"], {"enum": ["file", "snippet", "context"], "type": "string"})
        self.assertEqual(props["tags"], {"type": ["string", "null"]})
        # 同层的普通字符串叶子必须照常还原：判据收紧不能把整块划进豁免
        self.assertEqual(props["name"]["description"], ORIGINAL)

    def test_restore_sse_data_handles_object_type(self):
        data = {"type": {"x": 1}, "delta": TOKEN}
        tr._restore_sse_data(data, self.sid, final=True)
        self.assertEqual(data["delta"], ORIGINAL)

    def test_restore_json_body_does_not_raise(self):
        body = json.dumps({"tools": [schema_with_non_string_type()]}).encode()
        out = tr._restore_json_body(body, self.sid, "example.invalid", "POST", "/v1/responses")
        self.assertIsNotNone(out)
        self.assertIn(ORIGINAL, out.decode("utf-8"))


class ResponsesStreamChunkingTests(unittest.TestCase):
    """`response.completed` 回显 schema 时，任意分片都要完整下发。"""

    def setUp(self):
        for name in ("sessions", "_RECENT_FWD", "_RECENT_REV"):
            patcher = mock.patch.object(tr, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sid = "responses-stream"
        tr._new_session(self.sid)
        tr.sessions[self.sid]["rev"][TOKEN] = ORIGINAL
        self.raw = build_responses_stream()

    def _feed(self, chunks):
        # 每条流结束都会把会话交给 aux 池 drop；同一用例里连跑两条流必须重建
        tr._new_session(self.sid)
        tr.sessions[self.sid]["rev"][TOKEN] = ORIGINAL
        flow = SimpleNamespace(metadata={})
        with mock.patch.object(tr, "_scan_response"), \
             mock.patch.object(tr, "_audit_response"), \
             mock.patch.object(tr, "_emit_restore_summary"):
            stream = tr._sse_stream_factory(flow, self.sid, "example.invalid", "POST", "/v1/responses", {})
            out = [stream(chunk) or b"" for chunk in chunks]
            out.append(stream(b"") or b"")
            tr.aux_drain()
        return b"".join(out).decode("utf-8", "replace")

    def _terminals(self, text):
        hits = 0
        for frame in text.split("\n\n"):
            for line in frame.split("\n"):
                if line.startswith("data:"):
                    try:
                        if json.loads(line[5:].strip()).get("type") == "response.completed":
                            hits += 1
                    except ValueError:
                        pass
        return hits

    def test_single_chunk_and_tcp_sized_chunks_are_equivalent(self):
        data = self.raw.encode("utf-8")
        whole = self._feed([data])
        split = self._feed([data[i:i + 65536] for i in range(0, len(data), 65536)])
        self.assertEqual(whole, split)
        self.assertEqual(self._terminals(whole), 1)
        # 整条流不得出现无 event:/data: 前缀的残片
        for frame in whole.split("\n\n"):
            if frame.strip():
                self.assertTrue(frame.startswith(("event:", "data:")), frame[:80])

    def test_completed_event_split_mid_json_keeps_terminal(self):
        data = self.raw.encode("utf-8")
        cut = data.index(b"event: response.completed")
        chunks = [data[:cut]] + [data[i:i + 1024] for i in range(cut, len(data), 1024)]
        out = self._feed(chunks)
        self.assertEqual(self._terminals(out), 1)
        # 重序列化后是 Python 风格带空格的 JSON；关键字段必须在，证明尾部没被切
        self.assertIn('"total_tokens": 886', out)
        self.assertEqual(out.count("event: response.completed"), 1)


if __name__ == "__main__":
    unittest.main()
