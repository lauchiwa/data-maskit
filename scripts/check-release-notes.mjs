#!/usr/bin/env node
/**
 * 更新说明摘要提取的用例（frontend/src/lib/releaseNotes.ts 的 pickUserNotes）。
 *
 * 为什么必须有：更新弹窗与「关于」页的 notes 有两条来源，形态完全不同 ——
 * 桌面端读 latest.json（短摘要），Web / Docker 端读 GitHub Release 的 body
 * （整章 CHANGELOG：`### 修复`、`### Fixed` 双语各一遍，加 `---` 与扩展提示）。
 * 此前 Web 端把 body 原样塞进 `whitespace-pre-wrap` 的 `<p>`，用户看到的是带
 * `###`、`**`、`> ` 的 Markdown 与一堆术语（issue #40：「版本介绍写的是人类
 * 能读的东西吗」）。抽出摘要后如果哪天正则被改坏（比如只认中文标题、`###` 判断
 * 写成了 `##`），症状只是「弹窗里又是一坨」，不会报错 —— 所以固化成用例。
 *
 * 运行前提：Node >= 22.6（--experimental-strip-types 直接跑 .ts 源码）。
 * 用法：node --experimental-strip-types scripts/check-release-notes.mjs
 */
import assert from 'node:assert/strict'
import { pickUserNotes } from '../frontend/src/lib/releaseNotes.ts'

let pass = 0
const cases = []
function t(name, fn) { cases.push([name, fn]) }

/** 真实 Release body 的骨架：摘要在前，技术条目在后，末尾附带扩展提示。 */
const FULL_BODY = `### 更新摘要 / Summary
- 日志页现在一眼能看出每条请求是成功还是失败。
- Logs now show at a glance whether each request succeeded or failed.

### 修复
- 引擎：还原失败不再把「本机从未签发过」的占位符算进去。

### Fixed
- Engine: restore failures no longer include foreign placeholders.

---

**扩展更新提示 / Extension update notice**

> ✅ 本次发版扩展无改动。
`

t('有摘要时只取摘要段，不混入技术条目与扩展提示', () => {
  const out = pickUserNotes(FULL_BODY)
  assert.equal(
    out,
    '- 日志页现在一眼能看出每条请求是成功还是失败。\n' +
      '- Logs now show at a glance whether each request succeeded or failed.',
  )
  for (const bad of ['###', '引擎：', 'Engine:', '扩展更新提示', '---']) {
    assert.ok(!out.includes(bad), `摘要段不应包含 ${bad}`)
  }
})

t('英文标题 `### Summary` 同样识别', () => {
  const out = pickUserNotes('### Summary\n- Something changed.\n\n### Fixed\n- x\n')
  assert.equal(out, '- Something changed.')
})

t('摘要标题后紧跟 `####` 子标题时在子标题处结束', () => {
  const out = pickUserNotes('### 更新摘要 / Summary\n- 一条\n\n#### 细节\n- 不该出现\n')
  assert.equal(out, '- 一条')
})

t('老版本（无摘要）回退：剥掉 Markdown 标记后截断', () => {
  const body = '### 修复\n- 引擎：某个修复。\n\n### Fixed\n- Engine: some fix.\n\n---\n\n> ✅ 扩展无改动。\n'
  const out = pickUserNotes(body)
  assert.ok(!out.includes('###'), '回退结果不应含标题标记')
  assert.ok(!out.includes('---'), '回退结果不应含分隔线')
  assert.ok(out.includes('引擎：某个修复。'), '回退结果应保留条目正文')
})

t('超长回退截断到 300 字并补省略号', () => {
  const long = `### 修复\n- ${'很长的说明'.repeat(200)}\n`
  const out = pickUserNotes(long)
  assert.ok(out.length <= 301, `太长：${out.length}`)
  assert.ok(out.endsWith('…'), '截断后应有省略号')
})

t('有标题但没条目时退回整篇（不返回空白）', () => {
  const out = pickUserNotes('### 更新摘要 / Summary\n\n### 修复\n- 一条修复。\n')
  assert.ok(out.length > 0, '不应返回空串')
  assert.ok(out.includes('一条修复。'), '应回退到技术条目正文')
})

t('CRLF 正文按行处理', () => {
  const out = pickUserNotes('### 更新摘要 / Summary\r\n- 一条\r\n\r\n### 修复\r\n- x\r\n')
  assert.equal(out, '- 一条')
})

t('空输入返回空串', () => {
  assert.equal(pickUserNotes(''), '')
  assert.equal(pickUserNotes(undefined), '')
})

t('latest.json 的纯文本摘要原样返回（不误伤）', () => {
  const notes = 'Data Maskit v0.11.1\n- 面板上的「还原失败」不再把不属于你的占位符也算进去。'
  assert.equal(pickUserNotes(notes), notes)
})

for (const [name, fn] of cases) {
  try {
    fn()
    pass += 1
    console.log(`  ok  ${name}`)
  } catch (e) {
    console.error(`  FAIL ${name}`)
    console.error(`       ${e instanceof Error ? e.message : String(e)}`)
    process.exit(1)
  }
}
console.log(`\ncheck-release-notes: ${pass}/${cases.length} cases passed`)
