"""测试凭据前缀脱敏规则及误伤防护。

验证：
1. DEFAULT_SECRET_PREFIXES 中的安全前缀能正常识别；
2. 常见的代码/常量/SHA/UUID/Base64 等不会被误伤（特别防范 ASIA_EAST_1 等枚举误报）；
3. 值形态均使用片段拼接构造，避免触发敏感字面量扫描。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

from shield_defaults import DEFAULT_SECRET_PREFIXES
import transparent


class TestSecretPrefixes(unittest.TestCase):
    def setUp(self):
        # 备份并替换 transparent 模块的 SECRET_PREFIXES
        self._orig_prefixes = list(transparent.SECRET_PREFIXES)
        transparent.SECRET_PREFIXES = list(DEFAULT_SECRET_PREFIXES)
        transparent._prefix_rx_cache = None
        transparent._prefix_rx_key = None

    def tearDown(self):
        transparent.SECRET_PREFIXES = self._orig_prefixes
        transparent._prefix_rx_cache = None
        transparent._prefix_rx_key = None

    def test_default_prefixes_hit(self):
        """测试 DEFAULT_SECRET_PREFIXES 中的各前缀均能成功命中。"""
        rx = transparent._prefix_secret_regex()
        self.assertIsNotNone(rx)

        # 构造各前缀的伪造凭据（片段拼接，不使用真实/明文字面量）
        sample_tail = "a1b2c3d4e5f6"
        for prefix in DEFAULT_SECRET_PREFIXES:
            sample = prefix + sample_tail
            match = rx.search("my_token = \"" + sample + "\"")
            self.assertIsNotNone(match, f"前缀 {prefix} 应能命中构造的凭据")
            self.assertEqual(match.group(0), sample)

    def test_no_false_positives_on_common_code_tokens(self):
        """测试代码中常见的区域常量、UUID、Git SHA、Base64 等不会被误伤。"""
        rx = transparent._prefix_secret_regex()
        self.assertIsNotNone(rx)

        non_secrets = [
            # 常见的 AWS 区域常量（绝不能误伤 ASIA_* 枚举）
            'AWS_REGION = "ASIA_EAST_1"',
            'region = "ASIA_PACIFIC_SOUTH"',
            'const REGION = "ASIA_NORTHEAST_1";',
            # Git commit SHA
            'commit = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"',
            # UUID
            'uuid = "123e4567-e89b-12d3-a456-426614174000"',
            # Base64 资产片段
            'img = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA"',
            # 普通标识符或文件名
            'path = "/var/log/app_trace_dump_2026.log"',
            'class AsiaPacificController:',
        ]

        for code in non_secrets:
            match = rx.search(code)
            self.assertIsNone(match, f"代码中的正常标识符不应被误判为凭据: {code}")


if __name__ == "__main__":
    unittest.main()
