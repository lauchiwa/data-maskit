#!/usr/bin/env node
/**
 * 版本号归一化的用例门禁（frontend/src/lib/utils.ts 的 normalizeVersion）。
 *
 * 为什么必须有：更新检查有三条来源，形态不一致 —— Tauri Updater 的 `update.version`
 * （来自 latest.json）是纯 semver `0.11.0`，而 GitHub API 的 `tag_name` 是 `v0.11.0`。
 * 界面模板统一写成 `v{v}`，任何一条来源忘了归一化就会渲染出「更新到 vv0.11.0」
 * （实测踩过：桌面端正常，Web / Docker 端双 v）。
 *
 * 三条不变量：
 *  1. 无论来源带不带 `v`，归一化后都不带 —— 模板 `v{v}` 才能稳定渲染成 `v0.11.0`；
 *  2. 空值/空白归一化为 undefined，不能返回空串（否则模板渲染出孤零零一个 `v`）；
 *  3. 只剥前导 `v`，不碰预发布后缀（`v0.11.0-beta.1` → `0.11.0-beta.1`）。
 *
 * 运行前提：Node >= 22.6（--experimental-strip-types 直接跑 .ts 源码）。
 * 用法：node --experimental-strip-types scripts/check-update-version.mjs
 *       （在仓库任意目录执行均可；退出码 0 通过 / 1 失败）
 */
import assert from 'node:assert/strict'
import { normalizeVersion } from '../frontend/src/lib/utils.ts'

let pass = 0
const cases = []
function t(name, fn) {
  cases.push([name, fn])
}

// ---------------------------------------------------------------- 形态归一

t('GitHub 的 tag_name 形态：v0.11.0 -> 0.11.0', () => {
  assert.equal(normalizeVersion('v0.11.0'), '0.11.0')
})

t('Tauri 的纯 semver 形态保持不变：0.11.0 -> 0.11.0', () => {
  assert.equal(normalizeVersion('0.11.0'), '0.11.0')
})

t('大写 V 前缀也剥掉', () => {
  assert.equal(normalizeVersion('V1.2.3'), '1.2.3')
})

t('两侧空白先 trim', () => {
  assert.equal(normalizeVersion('  v0.11.0  '), '0.11.0')
})

// ---------------------------------------------------------------- 空值语义

t('空串 -> undefined（不是空串，避免模板渲染出孤零零的 v）', () => {
  assert.equal(normalizeVersion(''), undefined)
})

t('纯空白 -> undefined', () => {
  assert.equal(normalizeVersion('   '), undefined)
})

t('null / undefined -> undefined', () => {
  assert.equal(normalizeVersion(null), undefined)
  assert.equal(normalizeVersion(undefined), undefined)
})

// ---------------------------------------------------------------- 边界

t('预发布后缀不被破坏：v0.11.0-beta.1 -> 0.11.0-beta.1', () => {
  assert.equal(normalizeVersion('v0.11.0-beta.1'), '0.11.0-beta.1')
})

t('只剥一个前导 v（GitHub tag 不会有第二个）', () => {
  assert.equal(normalizeVersion('vv0.11.0'), 'v0.11.0')
})

t('幂等：归一化两次结果相同', () => {
  assert.equal(normalizeVersion(normalizeVersion('v1.2.3')), '1.2.3')
})

// ------------------------------------------- 核心不变量：与 i18n 模板/Release 链接联调

t('模板 `v{v}` + 三条来源，都必须渲染成 v0.11.0（不得出现 vv）', () => {
  // 与 i18n 的 'layout.updateTo': '更新到 v{v}' 同形
  const render = (v) => `更新到 v${normalizeVersion(v) ?? ''}`
  assert.equal(render('0.11.0'), '更新到 v0.11.0') // Tauri latest.json
  assert.equal(render('v0.11.0'), '更新到 v0.11.0') // GitHub tag_name
  assert.equal(render('v0.11.0\n'), '更新到 v0.11.0') // 带尾随空白
})

t('Release 链接需要 tag 形态：显式拼 v 后为 /tag/v0.11.0', () => {
  const url = (v) =>
    `https://github.com/xiaYuTian11/maskit/releases/tag/v${normalizeVersion(v) ?? ''}`
  assert.equal(url('v0.11.0'), 'https://github.com/xiaYuTian11/maskit/releases/tag/v0.11.0')
  assert.equal(url('0.11.0'), 'https://github.com/xiaYuTian11/maskit/releases/tag/v0.11.0')
})

let failed = 0
for (const [name, fn] of cases) {
  try {
    await fn()
    pass += 1
    console.log(`  ok   ${name}`)
  } catch (err) {
    failed += 1
    console.error(`  FAIL ${name}\n       ${err && err.message ? err.message : err}`)
  }
}
console.log(`check-update-version: ${pass}/${cases.length} passed`)
process.exit(failed ? 1 : 0)
