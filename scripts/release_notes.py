"""CHANGELOG「更新摘要 / Summary」的唯一解析实现（stdlib only）。

为什么单独成模块：同一套判据要服务三处 ——

1. `scripts/check-changelog.py`：门禁（摘要存在、必须排在本版本最前、不含术语、长度上限）；
2. `scripts/generate-latest-json.py`：桌面更新弹窗的 `notes`（取 1~3 条人话，取代
   "修复 2 项"这种没有任何内容的统计）；
3. 人工排查：`python -c "import release_notes ..."` 直接看某个版本抽出来的是什么。

三处各写一份正则，只会在某一处改口径时静默漂移（`panel._EXT_MAX_BODY` 与
`transparent._MAX_REQUEST_BODY` 那对常量就是先例：两边必须同步，靠
`ConstantParityTests` 钉住）。所以收进一个模块，谁改都只有一处。

摘要的形态（写在 `## [x.y.z]` 之下、任何 `### 修复/Added` 之前）：

    ### 更新摘要 / Summary
    - 日志页现在一眼能看出每条请求是成功还是失败。
    - Logs now show at a glance whether each request succeeded or failed.
"""
from __future__ import annotations

import re

# 摘要标题的识别词。中文优先（本项目 CHANGELOG 是中文在前、英文在后），
# 英文只认整词 `summary`，避免把 `### Summary of fixes` 之外的分节误认成摘要。
# 大小写不敏感（`### SUMMARY` 也该认）。
SUMMARY_KEY_ZH = "更新摘要"
SUMMARY_KEY_EN = "summary"

# `## [0.11.1] - 2026-10-16` / `## [Unreleased]`
_VERSION_HEAD_RE = re.compile(r"^##\s*\[([^\]]+)\][^\n]*$", re.M)
# 任意三级及以下标题（`### 修复` / `#### xxx`）。摘要段遇到它就结束。
_SUB_HEAD_RE = re.compile(r"^(#{3,6})\s*(\S[^\n]*)$", re.M)
_ITEM_RE = re.compile(r"^\s*[-*]\s+(.*\S)\s*$", re.M)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def split_versions(text: str) -> list[tuple[str, str]]:
    """把 CHANGELOG 正文切成 `[(版本号, 章节正文)]`。

    章节正文**不含** `## [x]` 标题行本身，也不含下一节标题 —— 这样"本版本第一个
    `###` 子节"的判断与正文索引一致，不用反复 ±1。
    `version` 可能是 `Unreleased`（调用方自行决定要不要处理）。
    """
    heads = list(_VERSION_HEAD_RE.finditer(text))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(heads):
        start = m.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((m.group(1).strip(), text[start:end]))
    return out


def _is_summary_title(title: str) -> bool:
    low = title.strip().lower()
    if SUMMARY_KEY_ZH in title:
        return True
    # 整词匹配：`summary` 必须作为独立词出现（`### Summary` / `### Summary / 摘要`）。
    return re.search(rf"\b{SUMMARY_KEY_EN}\b", low) is not None


def find_summary(section: str) -> tuple[str | None, list[str]]:
    """返回 `(摘要标题, 条目列表)`；没有摘要子节时返回 `(None, [])`。

    标题保留原文（去掉 `###` 与首尾空白），条目去掉 `- `/`* ` 前缀。
    """
    for m in _SUB_HEAD_RE.finditer(section):
        title = m.group(2).strip()
        if not _is_summary_title(title):
            continue
        # 摘要段 = 本标题之后到下一个任意 `###`/`####` 之前
        tail = section[m.end():]
        nxt = _SUB_HEAD_RE.search(tail)
        body = tail[: nxt.start()] if nxt else tail
        items = [im.group(1) for im in _ITEM_RE.finditer(body)]
        return title, items
    return None, []


def summary_is_first(section: str) -> bool:
    """摘要是不是本版本的**第一个** `###` 子节。

    必须最前，因为 `render-release-notes.py` 原样切整章作为 Release body，
    `generate-latest-json.py` 取摘要作为桌面弹窗 —— 位置一乱，用户先读到的
    又是技术条目。
    """
    for m in _SUB_HEAD_RE.finditer(section):
        return _is_summary_title(m.group(2).strip())
    return False


def has_cjk(line: str) -> bool:
    """至少 4 个汉字才算中文条目 —— 只出现 `KEY`、`v2` 之类的行不算。"""
    return len(_CJK_RE.findall(line)) >= 4


def zh_items(items: list[str]) -> list[str]:
    """中文条目（按原顺序）。"""
    return [x for x in items if has_cjk(x)]


def en_items(items: list[str]) -> list[str]:
    """英文条目（不含汉字的行；纯符号行已被 `_ITEM_RE` 的 `\\S` 排除）。"""
    return [x for x in items if not _CJK_RE.search(x)]


def user_notes(
    section: str,
    max_items: int = 3,
    max_chars: int = 900,
) -> str:
    """把摘要段拼成给用户看的纯文本（中文在前、英文在后）。

    `max_chars` 是硬上限：`latest.json` 的 `notes` 会显示在更新卡片里
    （`whitespace-pre-wrap` 纯文本），过长会把卡片撑成一屏。超限时按整行截断
    并补 `…`，不切断半句话的字符（宁可少一条，也不要断字）。
    """
    _, items = find_summary(section)
    if not items:
        return ""
    picked = (zh_items(items) or [])[:max_items] + (en_items(items) or [])[:max_items]
    lines: list[str] = []
    used = 0
    for line in picked:
        cost = len(line) + 3  # "- " + 换行
        if used + cost > max_chars:
            break
        lines.append(f"- {line}")
        used += cost
    if not lines:
        return ""
    if len(lines) < len(picked):
        lines.append("…")
    return "\n".join(lines)
