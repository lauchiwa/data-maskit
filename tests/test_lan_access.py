"""局域网访问开关（issue #45）。

分两层守：

1. 启动期绑定（`_bootstrap_lan_access`）——它决定 `app.run` 的 host，改错了就是把
   控制面暴露到整个网段，所以失败方向必须恒为「关」：文件缺失、JSON 损坏、顶层不是
   对象、值非真，全部回落 `127.0.0.1`。
2. 配置往返——`lan_access` 必须能存进去、能读回来，缺字段的老配置必须保持关闭
   （升级不能静默放开一道对外入口）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import panel


class LanAccessBootstrapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.json"
        patcher = mock.patch.object(panel, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_config_binds_loopback(self):
        self.assertFalse(panel._bootstrap_lan_access())

    def test_corrupt_json_binds_loopback(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertFalse(panel._bootstrap_lan_access())

    def test_non_object_payload_binds_loopback(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertFalse(panel._bootstrap_lan_access())

    def test_explicit_true_enables(self):
        self.path.write_text(json.dumps({"lan_access": True}), encoding="utf-8")
        self.assertTrue(panel._bootstrap_lan_access())

    def test_explicit_false_stays_loopback(self):
        self.path.write_text(json.dumps({"lan_access": False}), encoding="utf-8")
        self.assertFalse(panel._bootstrap_lan_access())

    def test_unrelated_config_does_not_enable(self):
        self.path.write_text(json.dumps({"origin_check": True, "debug": True}), encoding="utf-8")
        self.assertFalse(panel._bootstrap_lan_access())


class LanAccessConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(panel, "CONFIG_PATH", Path(tmp.name) / "config.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        panel.save_config(panel.default_config())

    def test_default_is_off(self):
        self.assertFalse(panel.load_config()["lan_access"])

    def test_roundtrip_persists(self):
        cfg = panel.load_config()
        cfg["lan_access"] = True
        panel.save_config(cfg)
        self.assertTrue(panel.load_config()["lan_access"])

    def test_patch_endpoint_updates_it(self):
        with panel.app.test_client() as client:
            resp = client.post("/api/config/patch",
                               json={"key": "lan_access", "op": "set", "path": [], "value": True},
                               headers={"X-Shield-Token": panel.API_TOKEN})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(panel.load_config()["lan_access"])


class BindHostPriorityTests(unittest.TestCase):
    """绑定地址优先级：环境变量 > 局域网开关 > 回环。"""

    def test_env_wins_over_lan_toggle(self):
        with mock.patch.dict("os.environ", {"MASKIT_PANEL_HOST": "10.0.0.5"}, clear=False):
            self.assertEqual(panel._resolve_bind_host("MASKIT_PANEL_HOST", False), "10.0.0.5")
            self.assertEqual(panel._resolve_bind_host("MASKIT_PANEL_HOST", True), "10.0.0.5")

    def test_lan_toggle_binds_all_interfaces(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(panel._resolve_bind_host("MASKIT_PANEL_HOST", True), "0.0.0.0")

    def test_default_is_loopback(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(panel._resolve_bind_host("MASKIT_PANEL_HOST", False), "127.0.0.1")

    def test_blank_env_falls_back(self):
        with mock.patch.dict("os.environ", {"MASKIT_PANEL_HOST": "   "}, clear=False):
            self.assertEqual(panel._resolve_bind_host("MASKIT_PANEL_HOST", False), "127.0.0.1")


class RemoteModeOriginTests(unittest.TestCase):
    """开局域网（REMOTE_MODE=True）后，谁还能进控制面。

    这里钉的是一个已发生过的真实回归：只给 REMOTE_MODE 写同源校验、不给 Tauri 壳
    留口子，用户一打开「局域网访问」就把自己的桌面端锁在外面（全部 /api/* 403）。
    """

    def setUp(self):
        for name, value in (("REMOTE_MODE", True), ("_origin_check_enabled", True),
                            ("_DISABLE_ORIGIN_CHECK_ENV", False)):
            patcher = mock.patch.object(panel, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _status(self, origin, host="127.0.0.1:5801"):
        headers = {"X-Shield-Token": panel.API_TOKEN, "Host": host}
        if origin:
            headers["Origin"] = origin
        with panel.app.test_client() as client:
            return client.get("/api/config", headers=headers).status_code

    def test_tauri_shell_survives_remote_mode(self):
        self.assertEqual(self._status("tauri://localhost"), 200)
        self.assertEqual(self._status("http://tauri.localhost"), 200)

    def test_lan_browser_origin_allowed(self):
        self.assertEqual(
            self._status("http://192.168.1.185:5801", host="192.168.1.185:5801"), 200)

    def test_foreign_origin_still_rejected(self):
        self.assertEqual(self._status("http://evil.example"), 403)

    def test_request_without_origin_is_allowed(self):
        # 壳层就绪探测、curl、扩展等非浏览器调用方没有 Origin
        self.assertEqual(self._status(None), 200)

    def test_tauri_shell_allowed_in_local_mode(self):
        # 本地模式行为不得因本次改动回退：壳层与面板同源白名单照旧
        with mock.patch.object(panel, "REMOTE_MODE", False):
            self.assertEqual(self._status("tauri://localhost"), 200)
            self.assertEqual(self._status("http://127.0.0.1:5801"), 200)
            self.assertEqual(self._status("http://evil.example"), 403)


class LanAddressTests(unittest.TestCase):
    def test_early_boot_monotonic_does_not_hit_empty_cache(self):
        """开机不到 60 s 就启动面板时，缓存初值不能当成“刚算过”。

        Linux 的 `time.monotonic()` 以开机为零点，早期版本把初值写成 0.0，
        `now - 0.0 < 60` 直接命中缓存返回空列表 —— 设置页的局域网地址永远不出现。
        """
        fake_addrinfo = [(panel.socket.AF_INET, panel.socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]
        with mock.patch.object(panel, "_LAN_IPS_CACHE", {"ts": None, "ips": []}), \
             mock.patch.object(panel.time, "monotonic", return_value=5.0), \
             mock.patch.object(panel.socket, "getaddrinfo", return_value=fake_addrinfo), \
             mock.patch.object(panel.socket, "socket", side_effect=OSError("no udp probe")):
            self.assertEqual(panel._lan_ipv4_addresses(), ["10.1.2.3"])

    def test_addresses_are_local_ipv4_strings(self):
        # 只校验形态：CI 容器可能没有非回环网卡，此时返回空列表是合法结果
        ips = panel._lan_ipv4_addresses()
        self.assertIsInstance(ips, list)
        for ip in ips:
            self.assertFalse(ip.startswith("127."))
            self.assertEqual(len(ip.split(".")), 4)
        self.assertEqual(ips, panel._lan_ipv4_addresses())  # 命中 60s 缓存


if __name__ == "__main__":
    unittest.main()
