"""Panel connection policy compatibility and public evidence projection."""
import copy
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import panel


class ConnectionConfigTests(unittest.TestCase):
    def config(self):
        cfg = copy.deepcopy(panel.default_config())
        cfg["upstreams"] = [{"name": "test", "port": 18701,
                             "target": "https://example.test", "paths": ["/v1"]}]
        return cfg

    def test_absent_policy_stays_absent(self):
        self.assertNotIn("connection_policy", panel.normalize_config(self.config())["upstreams"][0])

    def test_http2_default_is_consistent_and_explicit_true_survives(self):
        template = json.loads((Path(__file__).resolve().parents[1] / "engine/config.example.json").read_text())
        self.assertIs(template["http2"], False)
        self.assertIs(panel.default_config()["http2"], False)
        self.assertIs(panel.normalize_config({})["http2"], False)
        self.assertIs(panel.normalize_config({"http2": True})["http2"], True)

    def test_real_unsupported_control_is_rejected(self):
        cfg = self.config()
        cfg["upstreams"][0]["connection_policy"] = {"reuse": "never"}
        with self.assertRaises(ValueError):
            panel.normalize_config(cfg)

    def test_engine_parser_preserves_explicit_policy_for_request_rejection(self):
        import transparent
        cfg = self.config()
        cfg["upstreams"][0]["connection_policy"] = {"reuse": "never"}
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "config.json").write_text(json.dumps(cfg))
            with mock.patch.object(transparent, "_DATA_ROOT", data):
                settings = transparent._read_settings()
        self.assertEqual(settings["upstreams"][0]["connection_policy"], {"reuse": "never"})

    def test_shared_validator_receives_http2_and_policy(self):
        cfg = self.config()
        policy = {"reuse": "never"}
        cfg["upstreams"][0]["connection_policy"] = policy
        validator = mock.Mock(return_value=policy)
        module = types.SimpleNamespace(validate_connection_policy=validator)
        with mock.patch.dict(sys.modules, {"connection_policy": module}):
            self.assertEqual(panel.normalize_config(cfg)["upstreams"][0]["connection_policy"], policy)
        validator.assert_called_once_with(policy, http2=cfg["http2"])

    def test_unsupported_policy_rejected_before_save(self):
        cfg = self.config()
        cfg["upstreams"][0]["connection_policy"] = {"reuse": "never"}
        module = types.SimpleNamespace(validate_connection_policy=mock.Mock(side_effect=ValueError("unsupported")))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            original = json.dumps(self.config())
            path.write_text(original)
            with mock.patch.dict(sys.modules, {"connection_policy": module}), mock.patch.object(panel, "CONFIG_PATH", path):
                with self.assertRaises(ValueError):
                    panel.save_config(cfg)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_old_form_rename_preserves_policy_and_null_resets(self):
        cfg = self.config()
        cfg["upstreams"][0]["connection_policy"] = {"reuse": "never"}
        value = dict(cfg["upstreams"][0], name="renamed")
        del value["connection_policy"]
        panel._apply_config_patch(cfg, "upstreams", "list_upsert", [], value, match="test")
        self.assertEqual(cfg["upstreams"][0]["connection_policy"], {"reuse": "never"})
        panel._apply_config_patch(cfg, "upstreams", "list_upsert", [], dict(value, connection_policy=None))
        self.assertNotIn("connection_policy", panel.normalize_config(cfg)["upstreams"][0])

    def test_invalid_policy_not_silently_ignored_when_module_missing(self):
        cfg = self.config()
        cfg["upstreams"][0]["connection_policy"] = "bad"
        with mock.patch.dict(sys.modules, {"connection_policy": None}):
            with self.assertRaises(ValueError):
                panel.normalize_config(cfg)


class ConnectionDiskConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.json"
        patcher = mock.patch.object(panel, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(panel._sync_runtime_config, panel.default_config())
        # Small enough that the structural-shrink guard would NOT save these
        # words/upstreams from an accidental fallback to the defaults.
        self.original = {
            "upstreams": [
                {"name": "custom", "base_path": "/custom", "port": 18741,
                 "target": "https://custom.example.test", "paths": ["/custom/v1"],
                 "extra_headers": {"X-Test-Mode": "custom"}, "connection_policy": {}},
                {"name": "other", "port": 18742, "target": "https://other.example.test"},
            ],
            "sensitive": {"公司": ["本地业务词", "另一个业务词"]},
            "target_domains": ["custom.example.test"],
            "auto_start_proxy": False, "record_plaintext_words": False,
            "stop_mode": "block", "response_scan": False, "stream_response": False,
            "audit": {"enabled": False}, "http2": True,
        }
        self.write_original()

    def write_original(self):
        self.original_text = json.dumps(self.original, ensure_ascii=False)
        self.path.write_text(self.original_text, encoding="utf-8")

    def assert_untouched(self):
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original_text)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def post(self, endpoint, payload):
        with panel.app.test_client() as client:
            return client.post(endpoint, json=payload, headers={"X-Shield-Token": panel.API_TOKEN})

    def test_load_preserves_unrelated_settings_and_unsupported_raw_policy(self):
        warnings = []
        cfg = panel.load_config(warnings)
        self.assertEqual(cfg["sensitive"], self.original["sensitive"])
        self.assertEqual(cfg["target_domains"], self.original["target_domains"])
        self.assertEqual([u["target"] for u in cfg["upstreams"]],
                         [u["target"] for u in self.original["upstreams"]])
        self.assertEqual(cfg["upstreams"][0]["connection_policy"], {})
        self.assertNotIn("connection_policy", cfg["upstreams"][1])
        for key in ("http2", "stop_mode", "auto_start_proxy", "record_plaintext_words",
                    "response_scan", "stream_response"):
            self.assertEqual(cfg[key], self.original[key], key)
        self.assertIs(cfg["audit"]["enabled"], False)
        self.assertEqual(cfg["meta"], {})  # No pretend migration of an unwritable config.
        self.assertEqual(warnings, [panel._CONNECTION_POLICY_LOAD_WARNING])
        self.assert_untouched()

    def test_bad_policy_types_stay_visible_with_bounded_metadata(self):
        for policy in ({}, [], False, 42, "bad", {"private": "raw-private-data" * 10000}):
            with self.subTest(policy_type=type(policy).__name__):
                self.original["upstreams"][0]["connection_policy"] = policy
                self.write_original()
                with panel.app.test_client() as client:
                    response = client.get("/api/config", headers={"X-Shield-Token": panel.API_TOKEN})
                self.assertEqual(response.status_code, 200)
                cfg = response.get_json()
                # Config readback intentionally retains the raw value for repair;
                # diagnostics must not copy it into public capability/warning data.
                self.assertEqual(cfg["upstreams"][0]["connection_policy"], policy)
                self.assertEqual(cfg["_meta"]["warnings"], [panel._CONNECTION_POLICY_LOAD_WARNING])
                self.assertLess(len(json.dumps(cfg["_meta"]["warnings"])), 500)
                self.assertNotIn("raw-private-data", json.dumps(cfg["_meta"]))
                self.assertIs(cfg["_meta"]["transport_capabilities"]["supported"], False)
                self.assert_untouched()

    def test_unrelated_post_and_patch_reject_without_backup_or_write(self):
        for endpoint, payload in (
            ("/api/config", {"record_plaintext_words": True}),
            ("/api/config/patch", {"key": "sensitive", "op": "list_add",
                                   "path": ["公司"], "value": ["新业务词"]}),
        ):
            with self.subTest(endpoint=endpoint):
                response = self.post(endpoint, payload)
                self.assertEqual(response.status_code, 400)
                self.assertIs(response.get_json()["ok"], False)
                self.assert_untouched()

    def test_enable_attempt_rejected_before_legacy_read_migrations(self):
        del self.original["upstreams"][0]["connection_policy"]
        self.write_original()
        proposed = dict(self.original["upstreams"][0], connection_policy={})
        for endpoint, payload in (
            ("/api/config", {"upstreams": [proposed]}),
            ("/api/config/patch", {"key": "upstreams", "op": "list_upsert", "value": proposed}),
            ("/api/config/patch", {"key": "upstreams", "op": "set", "value": [proposed]}),
        ):
            with self.subTest(endpoint=endpoint, op=payload.get("op")):
                response = self.post(endpoint, payload)
                self.assertEqual(response.status_code, 400)
                self.assert_untouched()

    def test_policy_only_null_reset_preserves_rest_and_backs_up_original(self):
        before = panel.load_config()
        response = self.post("/api/config/patch", {
            "key": "upstreams", "op": "list_upsert", "match": "custom",
            "value": {"connection_policy": None},
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        expected = copy.deepcopy(before)
        del expected["upstreams"][0]["connection_policy"]
        self.assertEqual(response.get_json()["config"], expected)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), expected)
        backups = list(self.path.parent.glob("config.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), self.original_text)

    def test_shrink_guard_cannot_restore_an_unvalidated_disk_policy(self):
        for i in range(2):
            self.original["upstreams"].append({"name": f"extra{i}", "port": 18743 + i,
                                               "target": "https://extra.example.test"})
        self.write_original()
        reduced = copy.deepcopy(self.original)
        reduced["upstreams"] = [self.original["upstreams"][1]]
        with self.assertRaises(ValueError):
            panel.save_config(reduced)
        self.assert_untouched()

    def test_missing_validator_preserves_disk_policy_but_still_rejects_write(self):
        with mock.patch.dict(sys.modules, {"connection_policy": None}):
            warnings = []
            cfg = panel.load_config(warnings)
            self.assertEqual(cfg["sensitive"], self.original["sensitive"])
            self.assertEqual(cfg["upstreams"][0]["connection_policy"], {})
            self.assertEqual(warnings, [panel._CONNECTION_POLICY_LOAD_WARNING])
            with self.assertRaises(ValueError):
                panel.save_config(cfg)
        self.assert_untouched()

    def test_unparseable_json_keeps_existing_default_fallback(self):
        self.original_text = '{"sensitive":'
        self.path.write_text(self.original_text, encoding="utf-8")
        warnings = []
        self.assertEqual(panel.load_config(warnings), panel.default_config())
        self.assertEqual(warnings, [])
        self.assert_untouched()


class TransportProjectionTests(unittest.TestCase):
    def evidence(self):
        return {"phase": "tls_handshake", "reused": None, "idle_s": None,
                "connect_ms": 0, "tls_ms": 1.5, "via_proxy": False,
                "evidence_complete": False, "server_conn_id": "local-test-id",
                "actual_endpoint": "https://user:password@example.test/path",
                "headers": {"Authorization": "sk-test-000000000000"},
                "raw_connection": {"private": "never export"}}

    def test_nested_whitelist_preserves_unknown_and_false(self):
        result = panel._project_transport(self.evidence())
        self.assertIsNone(result["reused"])
        self.assertIsNone(result["idle_s"])
        self.assertEqual(result["connect_ms"], 0)
        self.assertIs(result["via_proxy"], False)
        for key in ("actual_endpoint", "headers", "raw_connection"):
            self.assertNotIn(key, result)
        self.assertNotIn("connect_ms", panel._project_transport({"connect_ms": float("nan")}))

    def test_tail_uses_nested_whitelist(self):
        line = "SHIELD\tERR\t" + json.dumps({"transport": self.evidence(), "dialog": "private"})
        result = json.loads(panel._tail_line_sanitize(line).split("\t", 2)[2])
        self.assertEqual(result, {"transport": panel._project_transport(self.evidence())})

    def test_metrics_keep_observation_without_inventing_missing_values(self):
        result = panel._project_engine_metrics({
            "transport": {"inflight": 0, "oldest_request_age_s": None, "raw_connection": "private",
                          "capabilities": {"deadlines": False, "http1_reuse_policy": False, "private": "secret"}},
            "heartbeat": {"generated_at": 123, "loop_lag_ms": 0, "private": "secret"}})
        self.assertEqual(result["heartbeat"], {"generated_at": 123, "loop_lag_ms": 0})
        self.assertIsNone(result["transport"]["oldest_request_age_s"])
        self.assertNotIn("connections", result["transport"])
        self.assertNotIn("raw_connection", result["transport"])
        self.assertNotIn("private", result["transport"]["capabilities"])

    def test_capabilities_share_bounded_observation_projection(self):
        caps = {"supported": False, "deadlines": False, "http1_reuse_policy": False,
                "observation": False, "observation_reason": "unsupported observation " * 1000,
                "stream_cancellation": False, "stream_cancellation_reason": "unknown version",
                "version": "unknown", "reason": "controls unavailable", "private": {"raw": "never export"}}
        module = types.SimpleNamespace(transport_capabilities=lambda: caps)
        with mock.patch.dict(sys.modules, {"connection_policy": module}):
            meta_caps = panel._connection_capabilities()
        metrics_caps = panel._project_engine_metrics({"transport": {"capabilities": caps}})["transport"]["capabilities"]
        self.assertEqual(meta_caps, metrics_caps)
        self.assertIs(meta_caps["observation"], False)
        self.assertIs(meta_caps["stream_cancellation"], False)
        self.assertEqual(meta_caps["stream_cancellation_reason"], "unknown version")
        self.assertLessEqual(len(meta_caps["observation_reason"]), 160)
        self.assertNotIn("private", meta_caps)
        self.assertEqual(panel._project_connection_capabilities({
            "supported": {"raw": "private"}, "observation": "yes", "version": [],
            "reason": {"raw": "private"}, "stream_cancellation_reason": ["private"],
        }), {})

    def test_capability_import_failure_does_not_claim_observation(self):
        with mock.patch.dict(sys.modules, {"connection_policy": None}):
            caps = panel._connection_capabilities()
        for key in ("supported", "deadlines", "http1_reuse_policy", "observation", "stream_cancellation"):
            self.assertIs(caps[key], False)
        self.assertEqual(caps["observation_reason"], "connection_policy_unavailable")
        self.assertEqual(caps["stream_cancellation_reason"], "connection_policy_unavailable")


class TransportI18nTests(unittest.TestCase):
    def test_known_phase_and_reason_codes_have_both_labels(self):
        source = (Path(__file__).resolve().parents[1] / "frontend/src/lib/i18n.tsx").read_text(encoding="utf-8")
        phases = ("unknown", "connecting", "tcp_connected", "tls_handshake", "tls_established",
                  "awaiting_response", "response_stream", "complete", "local_response")
        reasons = ("unknown", "connect_failed", "tls_failed", "connection_selection_failed",
                   "server_disconnected", "request_failed", "client_cancelled", "client_disconnected",
                   "client_protocol_error", "response_offload_timeout", "response_offload_failed",
                   "response_offload_wait", "stream_finish_wait", "stream_finish_failed",
                   "unsupported_connection_policy")
        for kind, codes in (("phase", phases), ("reason", reasons)):
            for code in codes:
                with self.subTest(kind=kind, code=code):
                    labels = re.findall(r"'transport\." + kind + r"\." + code + r"': '([^']+)'", source)
                    self.assertEqual(len(labels), 2, "Must have Chinese and English labels")
                    self.assertRegex(labels[0], r"[一-鿿]")
                    self.assertNotRegex(labels[1], r"[一-鿿]")
                    self.assertNotEqual(labels[1], code)

    def test_event_details_translate_all_enum_surfaces_and_preserve_unknown(self):
        source = (Path(__file__).resolve().parents[1] /
                  "frontend/src/components/events/EventDetailDialog.tsx").read_text(encoding="utf-8")
        for expression in ("transportLabel('phase', event.transport?.phase)",
                           "transportLabel('reason', event.transport?.reason)",
                           "transportLabel('phase', event.failure_phase)",
                           "transportLabel('reason', event.reason)"):
            self.assertIn(expression, source)
        self.assertIn("if (!value) return t('transport.unknown')", source)
        self.assertIn("return label === key ? value : label", source)


if __name__ == "__main__":
    unittest.main()
