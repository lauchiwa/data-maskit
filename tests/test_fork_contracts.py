"""Fork invariants that must survive upstream merges (no network or installed client)."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
import panel
import transparent as tr
import test_cache_field_paths as cache_paths


class ForkContractTests(unittest.TestCase):
    def test_all_default_update_sources_use_fork_repository(self):
        repo = "lauchiwa/data-maskit"
        api = f"https://api.github.com/repos/{repo}/releases/latest"
        static = f"https://github.com/{repo}/releases/latest/download/latest.json"
        self.assertEqual(panel.DEFAULT_UPDATE_API_URL, api)
        self.assertEqual(panel.DEFAULT_UPDATE_STATIC_URL, static)
        config = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
        self.assertEqual(config["plugins"]["updater"]["endpoints"], [static])
        for name in ("frontend/src/lib/tauri.ts", "frontend/src/pages/Settings.tsx"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(api, text, name)
            self.assertNotIn("repos/xiaYuTian11/maskit/releases/latest", text, name)

    def test_update_public_key_keeps_fork_identity(self):
        config = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
        pubkey = config["plugins"]["updater"]["pubkey"]
        lines = base64.b64decode(pubkey).decode("ascii").splitlines()
        packet = base64.b64decode(lines[1])
        self.assertEqual(int.from_bytes(packet[2:10], "little"), 0x8FDEF509963AB482)

    def test_fork_release_link_is_allowed_without_relaxing_boundaries(self):
        self.assertTrue(panel._is_allowed_external_url("https://github.com/lauchiwa/data-maskit/releases"))
        self.assertTrue(panel._is_allowed_external_url("https://github.com/xiaYuTian11/maskit"))
        for url in (
            "https://github.com/lauchiwa/data-maskit.evil/releases",
            "https://github.com/other/data-maskit/releases",
            "https://github.com/lauchiwa/data-maskit/releases?token=fake",
            "http://github.com/lauchiwa/data-maskit/releases",
        ):
            self.assertFalse(panel._is_allowed_external_url(url), url)

    def test_engineering_instructions_remain_tracked_and_not_ignored(self):
        tracked = subprocess.check_output(
            ["git", "ls-files", "--", "AGENTS.md", "CLAUDE.md"], cwd=ROOT, text=True)
        self.assertEqual(set(tracked.splitlines()), {"AGENTS.md", "CLAUDE.md"})
        self.assertEqual((ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip(), "@AGENTS.md")
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "--", "AGENTS.md", "CLAUDE.md"],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(ignored.returncode, 1, ignored.stdout + ignored.stderr)

    def test_model_rules_run_after_masking_in_worker(self):
        # Reuse the isolated dual-path fixture without inheriting/re-running its test methods.
        case = cache_paths.CacheFieldPathTests()
        self.addCleanup(case.doCleanups)
        case.setUp()
        rules = [{"match": "gpt-4o", "headers": {"originator": "fork-test"},
                  "body": {"system": case.MARKER, "prompt_cache_key": "fixed-by-rule"}}]
        tr.UPSTREAMS = [{**u, "model_rules": rules} for u in tr.UPSTREAMS]
        loop_thread = threading.get_ident()
        worker_threads = []
        original = tr._apply_model_rule_body

        def inject(body, rule, upstream):
            worker_threads.append(threading.get_ident())
            self.assertNotIn(case.MARKER, body["messages"][0]["content"])
            return original(body, rule, upstream)

        body = case._body(messages=[{"role": "user", "content": case.MARKER}])
        with mock.patch.object(tr, "_apply_model_rule_body", side_effect=inject), \
                mock.patch.object(tr, "_device_id", return_value="0" * 64):
            _, sent = case._send("proxy", body)
        self.assertEqual(sent["system"], case.MARKER, "Configured fingerprints must not be masked")
        self.assertEqual(sent["prompt_cache_key"], "fixed-by-rule")
        self.assertNotIn(case.MARKER, sent["messages"][0]["content"])
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], loop_thread, "Injection must stay off the event loop")


if __name__ == "__main__":
    unittest.main()
