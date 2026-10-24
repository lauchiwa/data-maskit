/**
 * 更新说明的「人话摘要」提取。
 *
 * 为什么需要：`checkUpdate()` 的 notes 有两条来源，但只有一条是短的 ——
 *  - 桌面端读 `latest.json`，`scripts/generate-latest-json.py` 已经只放摘要；
 *  - Web / Docker 端读 GitHub `releases/latest` 的 `body`，那是**整章 CHANGELOG**
 *    （`### 修复`、`### Fixed` 双语各一遍，还有 `---` 与扩展提示），原先被直接塞进
 *    `whitespace-pre-wrap` 的 `<p>` 里 —— 用户看到的是带 `###`、`**`、`> ` 的原始
 *    Markdown 与一堆术语（issue #40：「版本介绍写的是人类能读的东西吗」）。
 *
 * 约定：Release body 的第一屏必须是 `### 更新摘要 / Summary`，由
 * `scripts/check-changelog.py` 在 CI 里强制。这里只做**取段**，不解析 Markdown ——
 * 引一个渲染库来显示几行更新说明是过度设计（摘要自身被约定为纯文本）。
 */

/** 摘要标题：`### 更新摘要 / Summary`、`### Summary`。英文侧要求整词。 */
const SUMMARY_HEAD = /^#{2,6}\s*(?:更新摘要|summary(?![a-z]))/i
/** 任何二~六级标题：摘要段遇到它就结束。 */
const ANY_HEAD = /^#{2,6}\s/
/** 回退时丢弃的 Markdown 前缀（标题、引用、分隔线、加粗整行）。 */
const MD_PREFIX = [/^#{1,6}\s+/, /^>\s?/, /^-{3,}\s*$/, /^\*\*(.+)\*\*$/]

/** 回退展示的最大字符数：更新卡片只有几行高，超出的部分本来也没人读。 */
const FALLBACK_MAX = 300

function stripMarkdown(text: string): string {
  return text
    .split('\n')
    .map((line) => {
      let out = line
      for (const re of MD_PREFIX) out = out.replace(re, '$1')
      return out.trimEnd()
    })
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

function truncate(text: string, max = FALLBACK_MAX): string {
  return text.length > max ? `${text.slice(0, max).trimEnd()}…` : text
}

/**
 * 从 Release 正文里取出要给用户看的那一段。
 *
 * - 有 `### 更新摘要 / Summary`：返回该段（去掉标题行，保留条目）；
 * - 没有（老版本 Release）：剥掉 Markdown 标记后截断到 300 字，至少别再显示 `###`；
 * - 空输入：返回空串（调用方按「无说明」处理）。
 */
export function pickUserNotes(body: string): string {
  const text = String(body || '')
    .replace(/\r\n?/g, '\n')
    .trim()
  if (!text) return ''

  const lines = text.split('\n')
  const start = lines.findIndex((line) => SUMMARY_HEAD.test(line))
  if (start === -1) return truncate(stripMarkdown(text))

  const section: string[] = []
  for (let i = start + 1; i < lines.length; i++) {
    if (ANY_HEAD.test(lines[i])) break
    section.push(lines[i])
  }
  const picked = section.join('\n').trim()
  // 有标题但没条目（写漏了）：退回整篇剥标记，总比空白强
  return picked ? picked : truncate(stripMarkdown(text))
}
