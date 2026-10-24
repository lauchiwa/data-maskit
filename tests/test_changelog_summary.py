"""版本说明「人话摘要」的回归测试（issue #40）。

为什么单独立一个文件锁它：`CHANGELOG.md` 同时被两个发布路径消费 ——

  - `scripts/render-release-notes.py` 原样切整章作 GitHub Release body；
  - `scripts/generate-latest-json.py` 取「更新摘要」作桌面更新弹窗的 notes。

两者都不会因为文案变难读而报错，用户只会默默读不懂。而门禁本身写错的方式很隐蔽：
正则漏了 `re.M`（摘要条目一条都读不到）、只认中文标题、把历史版本也纳入检查（用户
一 clone 就红）。所以这里既锁「真实 CHANGELOG 能过」，也锁「门禁真的有牙」——
把术语、位置、长度任一项写坏，都必须报错。
"""
import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release_notes as rn  # noqa: E402


def _load_script(name: str):
    """文件名带连字符不能直接 import，按路径加载（与 test_release_metadata.py 同法）。"""
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check_changelog = _load_script("check-changelog")
generate_latest_json = _load_script("generate-latest-json")


def _changelog(body: str) -> str:
    return "# Changelog\n\n" + body


GOOD = """## [0.11.1] - 2026-10-16

### 更新摘要 / Summary
- 日志页现在一眼能看出每条请求是成功还是失败。
- Logs now show at a glance whether each request succeeded or failed.

### 修复
- 引擎：某个技术修复。
"""


class ParseTests(unittest.TestCase):
    def test_split_versions_keeps_body_without_heading(self):
        sections = dict(rn.split_versions(_changelog(GOOD + "\n## [0.10.1] - 2026-10-13\n\n### 修复\n- x\n")))
        self.assertEqual(set(sections), {"0.11.1", "0.10.1"})
        self.assertNotIn("## [0.11.1]", sections["0.11.1"])
        self.assertNotIn("## [0.10.1]", sections["0.11.1"])

    def test_find_summary_reads_items_with_multiline_regex(self):
        # 回归：`_ITEM_RE` 曾漏 `re.M`，`^` 只匹配整段开头，条目一条都读不到 ——
        # 门禁因此把「明明写了摘要」判成空，属于最难自查的一类错。
        title, items = rn.find_summary(GOOD)
        self.assertEqual(len(items), 2)
        self.assertIn("日志页现在一眼能看出每条请求是成功还是失败。", items[0])

    def test_summary_must_be_first_subsection(self):
        self.assertTrue(rn.summary_is_first(GOOD))
        moved = GOOD.replace("### 更新摘要 / Summary", "### 修复\n- x\n\n### 更新摘要 / Summary")
        self.assertFalse(rn.summary_is_first(moved))

    def test_zh_and_en_split(self):
        _, items = rn.find_summary(GOOD)
        self.assertEqual(len(rn.zh_items(items)), 1)
        self.assertEqual(len(rn.en_items(items)), 1)

    def test_user_notes_respects_item_and_char_caps(self):
        items = [f"- 第{i}条说得比较长的一句话说明" for i in range(6)]
        text = "### 更新摘要 / Summary\n" + "\n".join(items) + "\n"
        out = rn.user_notes(text, max_items=2, max_chars=1000)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("- ")]), 2)

        tiny = rn.user_notes(text, max_items=3, max_chars=30)
        self.assertLessEqual(len(tiny.replace("\n", "")), 40)
        self.assertTrue(tiny.endswith("…"))


class GateTests(unittest.TestCase):
    def test_good_changelog_passes(self):
        self.assertEqual(check_changelog.check_text(_changelog(GOOD)), [])

    def test_missing_summary_is_an_error(self):
        text = _changelog("## [0.11.1] - 2026-10-16\n\n### 修复\n- 引擎：x\n")
        errs = check_changelog.check_text(text)
        self.assertTrue(any("缺少" in e for e in errs), errs)

    def test_summary_not_first_is_an_error(self):
        bad = """## [0.11.1] - 2026-10-16

### 修复
- 引擎：x

### 更新摘要 / Summary
- 日志页现在一眼能看出每条请求是成功还是失败。
- Logs now show at a glance whether each request succeeded or failed.
"""
        errs = check_changelog.check_text(_changelog(bad))
        self.assertTrue(any("第一个子节" in e for e in errs), errs)

    def test_technical_terms_are_rejected(self):
        bad = GOOD.replace(
            "- 日志页现在一眼能看出每条请求是成功还是失败。",
            "- 修复了 SSE 流式接管时的 upstream_idle 判定。")
        errs = check_changelog.check_text(_changelog(bad))
        self.assertTrue(any("硬技术术语" in e for e in errs), errs)

    def test_http_status_and_source_file_are_rejected(self):
        bad = GOOD.replace(
            "- 日志页现在一眼能看出每条请求是成功还是失败。",
            "- panel.py 现在会返回 502。")
        errs = check_changelog.check_text(_changelog(bad))
        self.assertTrue(any("源码文件名" in e for e in errs), errs)
        self.assertTrue(any("状态码" in e for e in errs), errs)

    def test_backtick_is_rejected(self):
        bad = GOOD.replace(
            "- 日志页现在一眼能看出每条请求是成功还是失败。",
            "- 日志页现在能看出 `status` 字段。")
        errs = check_changelog.check_text(_changelog(bad))
        self.assertTrue(any("反引号" in e for e in errs), errs)

    def test_chinese_length_cap(self):
        long_line = "- " + "很长的说明" * 60
        bad = GOOD.replace("- 日志页现在一眼能看出每条请求是成功还是失败。", long_line)
        errs = check_changelog.check_text(_changelog(bad))
        self.assertTrue(any("字上限" in e for e in errs), errs)

    def test_english_only_summary_is_an_error(self):
        bad = GOOD.replace("- 日志页现在一眼能看出每条请求是成功还是失败。\n", "")
        errs = check_changelog.check_text(_changelog(bad))
        self.assertTrue(any("缺少中文摘要" in e for e in errs), errs)

    def test_old_versions_are_exempt(self):
        # 0.10.1 之前的历史章节不回溯（已发布，补了也改不到线上页面）
        old = "## [0.9.0] - 2026-10-10\n\n### 修复\n- 引擎：x\n"
        self.assertEqual(check_changelog.check_text(_changelog(old)), [])

    def test_unreleased_empty_is_ok_but_with_entries_needs_summary(self):
        empty = _changelog("## [Unreleased]\n\n## [0.11.1] - 2026-10-16\n\n### 更新摘要 / Summary\n- 日志页现在一眼能看出成败。\n- Logs now show whether requests succeeded.\n")
        self.assertEqual(check_changelog.check_text(empty), [])

        filled = _changelog("## [Unreleased]\n\n### 修复\n- 引擎：x\n")
        errs = check_changelog.check_text(filled)
        self.assertTrue(any("[Unreleased]" in e for e in errs), errs)

    def test_real_changelog_passes(self):
        """真实文件必须过门禁 —— 否则下次有人手改条目就红在 CI 而不是本地。"""
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(check_changelog.check_text(text), [])


class LatestJsonNotesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)

    def _file(self, text: str) -> pathlib.Path:
        p = self.dir / "CHANGELOG.md"
        p.write_text(text, encoding="utf-8")
        return p

    def test_prefers_user_summary(self):
        notes = generate_latest_json._release_notes("v0.11.1", self._file(_changelog(GOOD)))
        self.assertIn("日志页现在一眼能看出每条请求是成功还是失败。", notes)
        self.assertIn("Logs now show at a glance", notes)
        self.assertNotIn("修复 1 项", notes)

    def test_falls_back_to_counts_for_older_versions(self):
        old = """## [0.9.0] - 2026-10-10

### 修复
- 引擎：x
- 面板：y
"""
        notes = generate_latest_json._release_notes("v0.9.0", self._file(_changelog(old)))
        self.assertIn("修复 2 项", notes)

    def test_unknown_version_falls_back_without_raising(self):
        notes = generate_latest_json._release_notes("v9.9.9", self._file(_changelog(GOOD)))
        self.assertIn("v9.9.9", notes)

    def test_missing_file_falls_back_without_raising(self):
        notes = generate_latest_json._release_notes("v0.11.1", self.dir / "no-such-changelog.md")
        self.assertIn("v0.11.1", notes)


if __name__ == "__main__":
    unittest.main()
