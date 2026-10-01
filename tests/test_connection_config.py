"""Panel connection policy compatibility and public evidence projection."""
import copy
import json
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


if __name__ == "__main__":
    unittest.main()
