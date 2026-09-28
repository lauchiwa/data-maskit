"""Cache protocol fields must not exempt business data or break schema references."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import transparent as tr


class CacheFieldPathTests(unittest.TestCase):
    FIELDS = ("prompt_cache_key", "prompt_cache_retention", "user_id", "client_metadata")
    MARKER = "MASKIT_TEST_PRIVATE_MARKER"
    ROUTES = ("proxy", "bridge")

    def setUp(self):
        # Keep all fixtures in process memory: no real config, event DB, model or upstream.
        state = dict(
            FILTER_ENABLED=True, FAIL_CLOSED=True, NER_ENABLED=False, DEBUG=False,
            CAPTURE_MODE="reverse", UPSTREAMS=list(tr.DEFAULT_UPSTREAMS),
            BUILTIN_RULES=dict(tr.DEFAULT_BUILTIN_RULES),
            SENSITIVE_DISABLED=set(), SENSITIVE_WORD_DISABLED={},
            CUSTOM_WORDS={self.MARKER: "TERM"}, _CUSTOM_WORDS_SORTED=(),
        )
        for name in ("sessions", "_RECENT_FWD", "_RECENT_REV", "_RECENT_SUFFIX",
                     "_CUSTOM_WORD_FWD", "_CUSTOM_WORD_REV", "_CUSTOM_WORD_RX_CACHE"):
            state[name] = {}
        self.enterContext(mock.patch.multiple(tr, **state))
        self.enterContext(mock.patch.object(tr, "_maybe_reload"))
        self.enterContext(mock.patch.object(tr, "_emit"))
        self.counter = 0

    def _send(self, route, body):
        raw = json.dumps(body, ensure_ascii=False)
        self.counter += 1
        if route == "bridge":
            sid = f"cache-path-{self.counter}"
            tr._new_session(sid)
            sent = tr.mask_body(raw, sid)
        else:
            flow = SimpleNamespace(
                request=SimpleNamespace(
                    pretty_host="api.openai.com", host="api.openai.com", port=5802,
                    scheme="http", path="/v1/chat/completions", method="POST",
                    headers={"content-type": "application/json"}, content=raw.encode("utf-8"),
                ),
                response=None, metadata={},
                client_conn=SimpleNamespace(sockname=("127.0.0.1", 18701)),
            )
            tr.request(flow)
            self.assertIsNone(flow.response, "The fixture must reach the forwarding path")
            self.assertIn("session_id", flow.metadata)
            sent = flow.request.content.decode("utf-8")
        return sent, json.loads(sent)

    @staticmethod
    def _body(**fields):
        return {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], **fields}

    def test_protocol_key_names_survive_matching_word_list(self):
        tr.CUSTOM_WORDS.update({key: "TERM" for key in self.FIELDS})
        for route in self.ROUTES:
            for key in self.FIELDS:
                with self.subTest(route=route, key=key):
                    _, obj = self._send(route, self._body(**{key: "probe-value"}))
                    self.assertIn(key, obj)
            _, obj = self._send(route, self._body(metadata={"user_id": self.MARKER}))
            self.assertIn("user_id", obj["metadata"])
            self.assertNotIn(self.MARKER, obj["metadata"]["user_id"])

    def test_sensitive_cache_key_is_masked_and_stable_across_requests(self):
        values = []
        for route in self.ROUTES:
            for _ in range(3):
                sent, obj = self._send(route, self._body(
                    prompt_cache_key=f"session-{self.MARKER}-0001"))
                self.assertNotIn(self.MARKER, sent)
                self.assertIn("{{TERM_", obj["prompt_cache_key"])
                values.append(obj["prompt_cache_key"])
        self.assertEqual(len(set(values)), 1, "Masking must reuse the cache routing value")

    def test_clean_cache_key_is_preserved(self):
        body = self._body(prompt_cache_key="review-session-0001", prompt_cache_retention="24h")
        for route in self.ROUTES:
            sent, obj = self._send(route, body)
            self.assertEqual(obj, body)
            self.assertEqual(sent, json.dumps(body, ensure_ascii=False))

    def test_retention_exemption_requires_top_level_enum(self):
        tr.CUSTOM_WORDS.update({"24h": "TERM", "in-memory": "TERM"})
        for route in self.ROUTES:
            for retention in ("24h", "in-memory"):
                with self.subTest(route=route, retention=retention):
                    _, obj = self._send(route, self._body(
                        prompt_cache_retention=retention,
                        customer={"prompt_cache_retention": retention}))
                    self.assertEqual(obj["prompt_cache_retention"], retention)
                    self.assertNotEqual(obj["customer"]["prompt_cache_retention"], retention)
            for value in (self.MARKER, ["24h", self.MARKER], {"note": self.MARKER}):
                with self.subTest(route=route, value=value):
                    sent, obj = self._send(route, self._body(prompt_cache_retention=value))
                    self.assertNotIn(self.MARKER, sent)
                    if isinstance(value, list):
                        self.assertNotIn("24h", obj["prompt_cache_retention"])

    def test_business_values_are_scanned_at_every_nested_position(self):
        for route in self.ROUTES:
            for key in self.FIELDS:
                payload = {key: self.MARKER}
                for container in ("customer", "client_metadata", "metadata", "input", "documents"):
                    with self.subTest(route=route, key=key, container=container):
                        sent, _ = self._send(route, self._body(**{container: payload}))
                        self.assertNotIn(self.MARKER, sent)
                sent, _ = self._send(route, self._body(messages=[{
                    "role": "assistant", "content": [{
                        "type": "tool_use", "id": "review-tool", "name": "lookup", "input": payload,
                    }],
                }]))
                self.assertNotIn(self.MARKER, sent)

    def test_same_named_numeric_and_array_values_are_scanned(self):
        number = 24681012  # Synthetic numeric marker, explicitly in the local word list.
        tr.CUSTOM_WORDS[str(number)] = "TERM"
        for route in self.ROUTES:
            for key in self.FIELDS:
                for value in (number, [self.MARKER], {"note": self.MARKER}):
                    with self.subTest(route=route, key=key, value=value):
                        sent, _ = self._send(route, self._body(customer={key: value}))
                        self.assertNotIn(self.MARKER, sent)
                        self.assertNotIn(str(number), sent)

    def test_schema_literal_values_are_still_scanned(self):
        for route in self.ROUTES:
            for key in self.FIELDS:
                for keyword, value in (("const", {key: self.MARKER}),
                                       ("enum", [{key: self.MARKER}])):
                    with self.subTest(route=route, key=key, keyword=keyword):
                        schema = {"type": "object", keyword: value}
                        sent, _ = self._send(route, self._body(response_format={
                            "type": "json_schema", "json_schema": {"name": "review", "schema": schema},
                        }))
                        self.assertNotIn(self.MARKER, sent)

    def test_schema_definitions_and_required_references_stay_consistent(self):
        tr.CUSTOM_WORDS.update({key: "TERM" for key in self.FIELDS})
        for route in self.ROUTES:
            for key in self.FIELDS:
                schema = {"type": "object", "properties": {key: {"type": "string"}},
                          "required": [key], "additionalProperties": False}
                bodies = {
                    "tool": self._body(tools=[{"type": "function", "function": {
                        "name": "lookup", "strict": True, "parameters": schema}}]),
                    "response": self._body(response_format={"type": "json_schema", "json_schema": {
                        "name": "review", "strict": True, "schema": schema}}),
                }
                for kind, body in bodies.items():
                    with self.subTest(route=route, key=key, kind=kind):
                        _, obj = self._send(route, body)
                        result = (obj["tools"][0]["function"]["parameters"] if kind == "tool"
                                  else obj["response_format"]["json_schema"]["schema"])
                        self.assertEqual(set(result["required"]), set(result["properties"]))
                        self.assertNotIn(key, result["properties"], "Business names still get masked")

    def test_nested_protocol_lookalikes_do_not_protect_key_names(self):
        tr.CUSTOM_WORDS.update({key: "TERM" for key in self.FIELDS})
        for route in self.ROUTES:
            for key in self.FIELDS:
                with self.subTest(route=route, key=key):
                    _, obj = self._send(route, self._body(customer={key: "safe"}))
                    self.assertNotIn(key, obj["customer"])
            _, obj = self._send(route, self._body(customer={"metadata": {"user_id": "safe"}}))
            self.assertNotIn("user_id", obj["customer"]["metadata"])
            # Arrays are not the protocol metadata object, nor the protocol request root.
            _, obj = self._send(route, self._body(metadata=[{"user_id": "safe"}]))
            self.assertNotIn("user_id", obj["metadata"][0])
            _, obj = self._send(route, [{key: "safe" for key in self.FIELDS}])
            for key in self.FIELDS:
                self.assertNotIn(key, obj[0])

    def test_user_identifier_values_remain_masked(self):
        # Deliberately fictitious address, also registered explicitly in the word list.
        email = "maskit-review@example.invalid"
        tr.CUSTOM_WORDS[email] = "EMAIL"
        for route in self.ROUTES:
            sent, obj = self._send(route, self._body(
                metadata={"user_id": email}, client_metadata={"contact": email},
                prompt_cache_key=email))
            self.assertNotIn(email, sent)
            self.assertIn("user_id", obj["metadata"])


if __name__ == "__main__":
    unittest.main()
