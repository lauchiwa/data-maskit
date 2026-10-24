"""CHANGELOG「更新摘要」门禁：版本说明必须是用户读得懂的话（issue #40）。

为什么需要它：`CHANGELOG.md` 是**开发者变更日志**，却被直接当成用户能看到的
更新说明用 —— `render-release-notes.py` 切整章作 GitHub Release body，
`generate-latest-json.py` 把同一份内容塞进桌面更新弹窗。于是用户读到的是
「还原失败不再把『本机从未签发过』的占位符算进去」「SSE 流式接管」「upstream_idle」
这种只有作者能解码的句子（实测 issue #40：「版本介绍写的是人类能读的东西吗」）。

光靠约定不成立：AGENTS.md 早就写了「只说改了什么、为什么，不展开实现细节」，
条目里照样堆术语。所以把口径固化成机器可判的规则，在 PR 阶段就拦住：

1. 每个受检版本必须有 `### 更新摘要 / Summary`，且**排在本版本最前**
   （切章时它就落在 Release 正文与弹窗的第一屏）；
2. 中英各至少一条（CHANGELOG 双语是既有约定）；
3. 中文摘要里禁止硬技术术语、反引号、源码文件名、`文件:行号`、4xx/5xx 状态码；
4. 长度上限：中文 ≤ 200 字、英文 ≤ 400 字符（更新弹窗只有几行高）。

**不回溯历史**：`SUMMARY_REQUIRED_FROM` 之前的版本已经发过线上 Release，
补摘要也改不到已发布的页面，只会在 diff 里制造噪声。新版本自动受检。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_notes as rn  # noqa: E402  （同目录模块，见模块 docstring）

# 从哪个版本起强制摘要。改小 = 连带回溯历史章节；改大 = 放过中间版本。
# 0.10.1 起回填，与 CHANGELOG 里实际补上的三节一致。
SUMMARY_REQUIRED_FROM = (0, 10, 1)

# 中文摘要里出现即报错的硬术语。**只放「用户读不懂、也不是产品概念」的词** ——
# 「占位符」「上游」「流式」「脱敏」「还原」是 Maskit 的核心概念，界面上到处是，
# 放进来会让摘要没法写。`API`/`URL` 同理（用户天天配 API key）。
BANNED_TERMS = (
    "SSE", "NDJSON", "mitmproxy", "mitmdump", "NER", "GIL",
    "HTTP", "HTTPS", "TLS", "TCP", "UDP", "DNS", "IPC",
    "CLI", "SDK", "SQL", "JWT", "JSON", "Base64", "UTF-8",
    "gzip", "brotli", "zstd", "Content-Encoding",
    "upstream_idle", "chunked", "event-stream", "stdout", "stderr",
    "正则表达式引擎", "线程池", "事件循环",
)
_BANNED_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(re.escape(t) for t in BANNED_TERMS) + r")(?![A-Za-z0-9])",
    re.I,
)
# 反引号（内联代码）、`xxx.py` / `xxx.ts` 这类源码文件名、`文件:行号`
_BACKTICK_RE = re.compile(r"`")
_SOURCE_FILE_RE = re.compile(
    r"[\w./-]+\.(?:py|ts|tsx|js|jsx|mjs|json|md|yml|yaml|ps1|sh|rs|toml|sql)\b", re.I
)
_LINE_REF_RE = re.compile(r":\d+\b")
_HTTP_STATUS_RE = re.compile(r"(?<!\d)[45]\d{2}(?!\d)")

ZH_MAX_CHARS = 200
EN_MAX_CHARS = 400

_VERSION_NUM_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _version_tuple(label: str) -> tuple[int, int, int] | None:
    """`0.11.1` / `0.12.0-beta.1` / `v0.11.1` → (0, 11, 1)；`Unreleased` → None。"""
    m = _VERSION_NUM_RE.match(label.strip().lstrip("vV"))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _check_summary_lines(items: list[str], where: str) -> list[str]:
    """逐条检查摘要正文，返回错误信息列表（空列表=通过）。"""
    errs: list[str] = []
    zh = rn.zh_items(items)
    en = rn.en_items(items)
    if not zh:
        errs.append(f"{where}: 缺少中文摘要条目（用户读的是中文那一遍）")
    if not en:
        errs.append(f"{where}: 缺少英文摘要条目（CHANGELOG 双语约定）")

    for line in items:
        if _BACKTICK_RE.search(line):
            errs.append(f"{where}: 摘要里不能写反引号代码 —— {line!r}")
            continue
        m = _SOURCE_FILE_RE.search(line)
        if m:
            errs.append(f"{where}: 摘要里不能出现源码文件名 {m.group(0)!r} —— {line!r}")
        if _LINE_REF_RE.search(line):
            errs.append(f"{where}: 摘要里不能出现 '文件:行号' 引用 —— {line!r}")
        if _HTTP_STATUS_RE.search(line):
            errs.append(f"{where}: 摘要里不能写 HTTP 状态码（普通读者不知道 502 是什么）—— {line!r}")
        if rn.has_cjk(line):
            m = _BANNED_RE.search(line)
            if m:
                errs.append(f"{where}: 中文摘要出现硬技术术语 {m.group(0)!r} —— {line!r}")

    zh_len = sum(len(x) for x in zh)
    en_len = sum(len(x) for x in en)
    if zh_len > ZH_MAX_CHARS:
        errs.append(f"{where}: 中文摘要 {zh_len} 字，超过 {ZH_MAX_CHARS} 字上限（更新弹窗放不下）")
    if en_len > EN_MAX_CHARS:
        errs.append(f"{where}: 英文摘要 {en_len} 字符，超过 {EN_MAX_CHARS} 上限")
    return errs


def check_text(text: str) -> list[str]:
    """检查整份 CHANGELOG，返回错误列表。"""
    errs: list[str] = []
    sections = rn.split_versions(text)
    if not sections:
        return ["CHANGELOG: 没有解析到任何 `## [x.y.z]` 版本章节"]

    for label, section in sections:
        ver = _version_tuple(label)
        is_unreleased = label.strip().lower() == "unreleased"
        if is_unreleased:
            # Unreleased 只有标题、还没写条目时放行（AGENTS.md 要求发版后不留空
            # Unreleased，所以这是"刚发完版"的正常状态）。
            if not section.strip():
                continue
        elif ver is None or ver < SUMMARY_REQUIRED_FROM:
            continue

        title, items = rn.find_summary(section)
        where = f"[{label}]"
        if title is None:
            errs.append(
                f"{where}: 缺少 `### 更新摘要 / Summary`（用户能看懂的一两句话，"
                f"写在 `### 修复` 之前）"
            )
            continue
        if not rn.summary_is_first(section):
            errs.append(
                f"{where}: `### {title}` 必须是本版本第一个子节 —— "
                f"Release 正文与更新弹窗都取第一屏，放到技术条目后面等于没写"
            )
        if not items:
            errs.append(f"{where}: `### {title}` 是空的，至少写一条")
            continue
        errs.extend(_check_summary_lines(items, where))

    return errs


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="CHANGELOG 更新摘要门禁")
    ap.add_argument("--changelog", default=str(ROOT / "CHANGELOG.md"), type=Path)
    args = ap.parse_args(argv)

    try:
        text = args.changelog.read_text(encoding="utf-8")
    except OSError as e:
        print(f"check-changelog: 读不到 {args.changelog}: {e}", file=sys.stderr)
        return 1

    errs = check_text(text)
    if errs:
        print("check-changelog: 版本说明不是给用户看的，请改成人话：", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        print(
            "\n写法示例：\n"
            "  ### 更新摘要 / Summary\n"
            "  - 日志页现在一眼能看出每条请求是成功还是失败。\n"
            "  - Logs now show at a glance whether each request succeeded or failed.\n",
            file=sys.stderr,
        )
        return 1

    print(f"check-changelog: OK（{len(rn.split_versions(text))} 个版本章节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
