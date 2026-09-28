"""执行 Windows 发版校验和步骤，防止 PowerShell/Python 双层转义生成字面量 \\n。

只在临时 bundle 中生成假产物，不构建、不签名、不发布。Windows 上实际经过
PowerShell；其他平台直接执行原样提取的 Python payload（PowerShell 不转义反斜杠），
使 Linux CI 也能拦住同一回归。不要用 shlex 的 POSIX 规则解析 PowerShell 命令。
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseChecksumTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="maskit-checksums-")
        self.addCleanup(tmp.cleanup)
        self.workdir = Path(tmp.name)
        self.bundle = self.workdir / "src-tauri/target/release/bundle"
        self.files = {
            "nsis/Maskit test-setup.exe": b"FAKE-INSTALLER\x00\xff",
            "nsis/Maskit test-setup.exe.sig": b"FAKE-SIGNATURE",
        }
        for name, content in self.files.items():
            self._write(name, content)

    def _write(self, name, content):
        path = self.bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _run_step(self, mode="signed"):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        step = re.search(
            r"^      - name: Generate SHA-256 checksums \(Windows\)\n"
            r"(.*?)(?=^      - name:|\Z)", workflow, re.M | re.S,
        )
        self.assertIsNotNone(step, "Windows checksum step must exist")
        self.assertIn("shell: pwsh", step.group(1))
        command = re.search(r'^        run: python -c "([^\n]+)"$', step.group(1), re.M)
        self.assertIsNotNone(command, "Update this executor if the workflow command shape changes")
        payload = command.group(1)
        # These would add PowerShell interpolation/quoting semantics that the
        # non-Windows fallback cannot reproduce. Fail rather than silently drift.
        self.assertFalse(any(char in payload for char in ('$', '`', '"')))
        env = dict(os.environ, CHECKSUM_PLATFORM="windows-x64", CHECKSUM_MODE=mode)
        argv = [sys.executable, "-c", payload]
        if os.name == "nt":
            shell = shutil.which("pwsh") or shutil.which("powershell")
            self.assertIsNotNone(shell, "Windows regression must run through PowerShell")
            env["CHECKSUM_TEST_PYTHON"] = sys.executable
            script = self.workdir / "checksums.ps1"
            # Substitute only the interpreter, retaining the workflow's exact
            # quoted payload. Never resolve an unrelated global Python install.
            script.write_text('& $env:CHECKSUM_TEST_PYTHON -c "' + payload
                              + '"\nexit $LASTEXITCODE\n', encoding="utf-8")
            argv = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(script)]
        result = subprocess.run(argv, cwd=self.workdir, env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        return (self.bundle / f"SHA256SUMS-windows-x64-{mode}.txt").read_bytes()

    def _assert_manifest(self, data):
        expected = [
            (hashlib.sha256(content).hexdigest() + "  " + name).encode("utf-8")
            for name, content in sorted(self.files.items(), key=lambda item: Path(item[0]))
        ]
        self.assertEqual(data.splitlines(), expected)
        self.assertTrue(data.endswith(b"\n"), "Last checksum must also end with a real newline")
        self.assertNotIn(b"\\n", data)

    def test_signed_artifacts_have_separate_lines_and_correct_hashes(self):
        """v0.103.0 两条摘要被字面量 \\n 粘成一行；空格路径和二进制内容也必须正确。"""
        self._assert_manifest(self._run_step())

    def test_rerun_excludes_existing_checksum_manifests(self):
        self._write("SHA256SUMS-old.txt", b"OLD-CHECKSUMS")
        self._write("nsis/SHA256SUMS-nested.txt", b"OLD-NESTED-CHECKSUMS")
        first = self._run_step()
        self._assert_manifest(first)
        self.assertEqual(self._run_step(), first)

    def test_unsigned_manifest_includes_notice(self):
        self.files.pop("nsis/Maskit test-setup.exe.sig")
        (self.bundle / "nsis/Maskit test-setup.exe.sig").unlink()
        self.files["UNSIGNED.txt"] = b"UNSIGNED: fake test artifact only.\n"
        self._write("UNSIGNED.txt", self.files["UNSIGNED.txt"])
        self._assert_manifest(self._run_step("unsigned"))


if __name__ == "__main__":
    unittest.main()
