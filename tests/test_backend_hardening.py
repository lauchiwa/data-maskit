"""后端跨平台与安全边界回归测试。

这些用例不启动真实代理或修改系统信任库；外部进程和平台能力均通过 mock
验证，避免测试套件误杀宿主进程或打开浏览器。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT))

import audit_engine  # noqa: E402
import panel  # noqa: E402


class UrlBoundaryTests(unittest.TestCase):
    def test_external_url_requires_exact_host_and_path_boundary(self):
        allowed = (
            "https://github.com/xiaYuTian11/maskit",
            "https://github.com/xiaYuTian11/maskit/releases/latest",
            "https://linux.do/t/123",
        )
        denied = (
            "https://github.com/xiaYuTian11/maskit.evil",
            "https://github.com/xiaYuTian11/maskit@evil.example/",
            "https://github.com.evil/xiaYuTian11/maskit",
            "https://github.com/xiaYuTian11/maskit:443/",
            "https://user:pass@linux.do/",
            "http://linux.do/",
            "https://linux.do.evil/",
            "https://github.com/xiaYuTian11/maskit?next=https://evil.example",
            "https://github.com/xiaYuTian11/maskit#fragment",
        )
        for url in allowed:
            self.assertTrue(panel._is_allowed_external_url(url), url)
        for url in denied:
            self.assertFalse(panel._is_allowed_external_url(url), url)

    def test_safe_target_removes_all_url_credentials_and_query(self):
        self.assertEqual(
            panel._safe_target("https://alice:secret@example.com:8443/api?token=sk-live-secret#x"),
            "https://example.com:8443/api",
        )
        self.assertTrue(panel._safe_target("javascript:alert(1)").startswith("<redacted"))

    def test_forwarded_origin_is_used_only_when_explicitly_trusted(self):
        old_remote = panel.REMOTE_MODE
        try:
            panel.REMOTE_MODE = True
            with mock.patch.dict(panel.os.environ, {panel.TRUST_PROXY_ENV: "1"}, clear=False):
                with panel.app.test_request_context(
                    "/",
                    base_url="http://internal:5801",
                    headers={
                        "Origin": "https://public.example",
                        "X-Forwarded-Proto": "https",
                        "X-Forwarded-Host": "public.example",
                    },
                ):
                    self.assertTrue(panel._origin_ok())
            # 多值头不是单跳代理语义，必须拒绝而不能取第一个值。
            with mock.patch.dict(panel.os.environ, {panel.TRUST_PROXY_ENV: "1"}, clear=False):
                with panel.app.test_request_context(
                    "/",
                    base_url="http://internal:5801",
                    headers={
                        "Origin": "https://public.example",
                        "X-Forwarded-Proto": "https",
                        "X-Forwarded-Host": "public.example, evil.example",
                    },
                ):
                    self.assertFalse(panel._origin_ok())
            # 默认关闭信任时，合法公网 Origin 也不能伪造内部 scheme/host。
            with mock.patch.dict(panel.os.environ, {panel.TRUST_PROXY_ENV: "0"}, clear=False):
                with panel.app.test_request_context(
                    "/",
                    base_url="http://internal:5801",
                    headers={"Origin": "https://public.example"},
                ):
                    self.assertFalse(panel._origin_ok())
        finally:
            panel.REMOTE_MODE = old_remote


class PosixProcessTests(unittest.TestCase):
    def test_process_exists_uses_kill_zero_on_posix(self):
        with mock.patch.object(panel.sys, "platform", "linux"), \
             mock.patch.object(panel.os, "kill") as kill:
            self.assertTrue(panel._process_exists(os.getpid()))
            kill.assert_called_once_with(os.getpid(), 0)

    def test_taskkill_refuses_current_process(self):
        with mock.patch.object(panel.os, "kill") as kill:
            ok, detail = panel._taskkill_pid(os.getpid())
        self.assertFalse(ok)
        self.assertIn("current", detail)
        kill.assert_not_called()

    def test_posix_port_fallback_does_not_fabricate_pid(self):
        # 若只能通过 TCP connect 判断端口存在，返回空 PID 集合，不能伪造 PID 1。
        old_cache = panel._netstat_cache.copy()
        # 必须连子进程探测一起打桩：本机（开发者机器）往往**真的**有进程监听
        # 18701，此时 lsof/ss 会返回真实 PID，断言就变成了「本机没装 Maskit 才成立」
        # —— 实测在开发机上恒红（与改动无关，基线同样失败），而 CI 上恒绿。
        with mock.patch.object(panel.sys, "platform", "linux"), \
             mock.patch.object(panel.Path, "is_dir", return_value=False), \
             mock.patch.object(panel, "_run_console", return_value=(1, "")), \
             mock.patch.object(panel.socket, "socket") as sock_cls:
            sock = sock_cls.return_value.__enter__.return_value
            sock.connect_ex.return_value = 0
            out = panel._listening_port_pids({18701}, fresh=True)
        panel._netstat_cache = old_cache
        self.assertEqual(out, {18701: set()})


class WindowsPortScanTests(unittest.TestCase):
    """Windows 也必须有连接探测兜底（issue #41）。

    Windows 侧取监听端口只有 `netstat -ano -p tcp` 一条路：netstat 被安全软件 /
    组策略拦截、或连接数大导致 5s 超时时，`_run_console` 落到 except 返回空串，
    netstat 结果恒为空。此前兜底被 `if sys.platform != "win32"` 挡住，于是三条
    路径一起失灵：启动就绪判定恒判「端口未监听」→ 等满 _START_READY_TIMEOUT（60s）
    报启动失败（用户侧就是「点了启动起不来」）；端口占用预检永远查不出占用；
    watchdog 每 10s 就把正在正常转发的 mitmdump 杀掉重启。
    """

    def setUp(self):
        old_cache = dict(panel._netstat_cache)
        self.addCleanup(panel._netstat_cache.update, old_cache)
        panel._netstat_cache["ts"] = 0.0
        panel._netstat_cache["data"] = {}

    def test_netstat_empty_still_reports_listening_port(self):
        with mock.patch.object(panel.sys, "platform", "win32"), \
             mock.patch.object(panel, "_run_console", return_value=(0, "")), \
             mock.patch.object(panel.socket, "socket") as sock_cls:
            sock = sock_cls.return_value.__enter__.return_value
            sock.connect_ex.return_value = 0
            # 只能证明「端口在监听」，拿不到 PID：返回空集合，不能伪造 PID。
            self.assertEqual(panel._listening_port_pids({18701}, fresh=True),
                             {18701: set()})

    def test_netstat_empty_and_port_closed_reports_nothing(self):
        # 端口真实空闲时必须返回空，否则占用预检会把空闲端口误判成被占用、启动被自己挡住。
        with mock.patch.object(panel.sys, "platform", "win32"), \
             mock.patch.object(panel, "_run_console", return_value=(0, "")), \
             mock.patch.object(panel.socket, "socket") as sock_cls:
            sock = sock_cls.return_value.__enter__.return_value
            sock.connect_ex.return_value = 10061  # WSAECONNREFUSED
            self.assertEqual(panel._listening_port_pids({18701}, fresh=True), {})

    def test_netstat_pid_is_never_overwritten_by_probe(self):
        # netstat 命中时不能再补探测：空 PID 集合会盖掉真实 PID，占用预检和
        # _free_upstream_ports 都靠 PID 判断「是不是自己的 mitmdump」，一旦被盖掉，
        # 崩溃残留进程就永远清不掉。
        out = "  TCP    127.0.0.1:18701   0.0.0.0:0   LISTENING   4321\n"
        with mock.patch.object(panel.sys, "platform", "win32"), \
             mock.patch.object(panel, "_run_console", return_value=(0, out)), \
             mock.patch.object(panel.socket, "socket") as sock_cls:
            sock = sock_cls.return_value.__enter__.return_value
            sock.connect_ex.side_effect = AssertionError("netstat 已命中，不应再做连接探测")
            self.assertEqual(panel._listening_port_pids({18701}, fresh=True),
                             {18701: {4321}})


class DiagLogPersistTests(unittest.TestCase):
    """诊断行落盘（issue #41 的可观测性修复）。

    引擎是被壳层 `taskkill /F /T` 强杀的（stdout 块缓冲也来不及 flush），内存里那
    800 行诊断缓存会随进程一起消失。这些用例守两条硬边界：该落的必须落盘；
    带明文的 SHIELD 事件行绝不能落盘。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for name, value in (("DATA_ROOT", self.root), ("_DIAG_LOG_ENABLED", True)):
            p = mock.patch.object(panel, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _log(self):
        return self.root / "engine.log"

    def test_diag_prefix_is_persisted(self):
        panel._emit_log("[panel] mitmdump 60s 内未监听端口 [18701]，判定启动失败")
        self.assertIn("未监听端口", self._log().read_text(encoding="utf-8"))

    def test_event_line_with_plaintext_is_not_persisted(self):
        # 隐私红线：SHIELD 事件行携带 req_preview / original 明文，而诊断包会打包
        # 数据目录。只允许 [panel]/[engine]/[proxy] 诊断前缀落盘。
        panel._emit_log('SHIELD\tMASK\t{"req_preview":"原文不该落盘"}')
        self.assertFalse(self._log().exists())

    def test_disabled_by_default_in_source_mode(self):
        # 源码态/测试默认不落盘：否则跑一次单测就往用户 %APPDATA%\Maskit 写诊断行。
        with mock.patch.object(panel, "_DIAG_LOG_ENABLED", False):
            panel._emit_log("[panel] 不该落盘")
        self.assertFalse(self._log().exists())

    def test_oversized_file_rotates_once_and_keeps_single_history(self):
        self._log().write_text("x" * (panel._DIAG_LOG_MAX + 1), encoding="utf-8")
        panel._emit_log("[panel] 轮转后这一行")
        self.assertTrue((self.root / "engine.log.1").exists())
        self.assertFalse((self.root / "engine.log.2").exists(), "只保留一份历史")
        self.assertIn("轮转后这一行", self._log().read_text(encoding="utf-8"))

    def test_write_failure_never_raises(self):
        # 磁盘满/目录不可写时绝不能影响脱敏主链路
        with mock.patch.object(panel, "DATA_ROOT", Path("/proc/nonexistent-dir")):
            panel._emit_log("[panel] 写不进去也不许抛")


class SidecarAndReportTests(unittest.TestCase):
    def test_sidecar_uses_engine_entry_prefix_and_posix_session(self):
        fake = mock.Mock()
        old_argv = panel._mitmdump_argv0
        old_popen = panel.subprocess.Popen
        try:
            panel._mitmdump_argv0 = lambda: ["maskit-engine", "--mitmdump"]
            panel.subprocess.Popen = mock.Mock(return_value=fake)
            with mock.patch.object(panel.sys, "platform", "linux"):
                panel._spawn_mitmdump_sidecar(["-p", "0"])
            args, kwargs = panel.subprocess.Popen.call_args
        finally:
            panel._mitmdump_argv0 = old_argv
            panel.subprocess.Popen = old_popen
        self.assertEqual(args[0], ["maskit-engine", "--mitmdump", "-p", "0"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertNotIn("creationflags", kwargs)

    def test_cert_endpoint_accepts_json_confirmation_and_does_not_call_certutil_on_posix(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cert = panel.CA_CERT
            panel.CA_CERT = Path(tmp) / "mitmproxy-ca-cert.cer"
            panel.CA_CERT.write_text("dummy", encoding="ascii")
            try:
                with mock.patch.object(panel.sys, "platform", "linux"), \
                     panel.app.test_client() as client:
                    response = client.post(
                        "/api/cert",
                        json={"confirm": True},
                        headers={"X-Shield-Token": panel.API_TOKEN},
                    )
            finally:
                panel.CA_CERT = old_cert
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["installed"])
        self.assertEqual(payload["scope"], "manual")
        self.assertNotIn("certutil", str(payload).lower())

    def test_cert_endpoint_rejects_string_confirmation(self):
        with panel.app.test_client() as client:
            response = client.post(
                "/api/cert",
                json={"confirm": "false"},
                headers={"X-Shield-Token": panel.API_TOKEN},
            )
        self.assertEqual(response.status_code, 400)

    def test_report_scrubs_target_and_evidence_before_writing(self):
        matrix = audit_engine.aggregate_matrix({step: [] for step in audit_engine._ALL_STEPS})
        findings = {step: [] for step in audit_engine._ALL_STEPS}
        findings["step9_error"] = [{
            "severity": "HIGH",
            "signal": "error_leak",
            "evidence": "Authorization: Bearer abcdefghijklmnop12345; sk-live-abcdefghijklmnop",
        }]
        text = audit_engine.render_markdown_report(
            "https://user:password@relay.example/v1?token=sk-live-secret",
            "model-x",
            matrix,
            findings,
        )
        self.assertNotIn("password", text)
        self.assertNotIn("sk-live-abcdefghijklmnop", text)
        self.assertNotIn("abcdefghijklmnop12345", text)
        self.assertIn("https://relay.example/v1", text)

        with tempfile.TemporaryDirectory() as tmp:
            path = audit_engine.save_report(text, tmp, generated_at="20260101-000000")
            saved = Path(path).read_text(encoding="utf-8")
            self.assertNotIn("sk-live", saved)


if __name__ == "__main__":
    unittest.main()
