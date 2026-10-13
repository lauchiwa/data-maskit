#!/usr/bin/env node
/**
 * 复制工具失败语义的用例门禁（frontend/src/lib/utils.ts 的 copyText）。
 *
 * 为什么必须有：copyText 的回退路径 document.execCommand('copy') 会返回 false
 * 表示「复制没有生效」（焦点被抢、权限被拒等）。旧实现丢弃返回值、函数正常返回，
 * 调用方按「没异常 = 成功」判定并提示「已复制」——用户以为剪贴板里是新正文，
 * 实际还是上一次的内容（issue #38）。这是纯函数 + DOM 桩的用例；前端没有单测
 * 框架，所以固化成 Node 用例跑在 frontend job 里（与 .env 导入解析器用例同法）。
 *
 * 运行前提：Node >= 22.6（--experimental-strip-types 直接跑 .ts 源码）。
 * 用法：node --experimental-strip-types scripts/check-copy-text.mjs
 *       （在仓库任意目录执行均可；退出码 0 通过 / 1 失败）
 */
import assert from 'node:assert/strict'
import { copyText } from '../frontend/src/lib/utils.ts'

let pass = 0
const cases = []
function t(name, fn) { cases.push([name, fn]) }

/** 装 DOM / 剪贴板桩，返回调用计数；每个用例前重装，避免用例间串味。 */
function installEnv({ clipboard = false, secure = false, writeThrows = false, execResult = false } = {}) {
  const calls = { write: 0, exec: 0, created: 0, removed: 0 }
  // Node 自带只读的 navigator/window 访问器（26.x 实测），只能 defineProperty 覆盖。
  const stub = (key, value) =>
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true })
  stub('window', { isSecureContext: secure })
  stub(
    'navigator',
    clipboard
      ? {
          clipboard: {
            writeText: async () => {
              calls.write += 1
              if (writeThrows) throw new Error('denied')
            },
          },
        }
      : {},
  )
  stub('document', {
    createElement: () => {
      calls.created += 1
      return { style: {}, value: '', focus() {}, select() {} }
    },
    body: {
      appendChild() {},
      removeChild() {
        calls.removed += 1
      },
    },
    execCommand: (cmd) => {
      assert.equal(cmd, 'copy', '只应调用 execCommand("copy")')
      calls.exec += 1
      return execResult
    },
  })
  return calls
}

t('安全上下文 + clipboard 可用：走 Clipboard API，不触碰回退', async () => {
  const calls = installEnv({ clipboard: true, secure: true })
  await copyText('demo text')
  assert.equal(calls.write, 1)
  assert.equal(calls.exec, 0)
})

t('clipboard 可用但 writeText 抛错：回退成功时不报错', async () => {
  const calls = installEnv({ clipboard: true, secure: true, writeThrows: true, execResult: true })
  await copyText('demo text')
  assert.equal(calls.write, 1)
  assert.equal(calls.exec, 1)
})

t('clipboard 不可用 + execCommand 返回 true：正常返回', async () => {
  const calls = installEnv({ execResult: true })
  await copyText('demo text')
  assert.equal(calls.exec, 1)
})

t('clipboard 不可用 + execCommand 返回 false：必须抛错（issue #38）', async () => {
  const calls = installEnv({ execResult: false })
  await assert.rejects(() => copyText('demo text'), /copy_failed/)
  assert.equal(calls.exec, 1)
})

t('writeText 抛错 + 回退失败：同样必须抛错', async () => {
  const calls = installEnv({ clipboard: true, secure: true, writeThrows: true, execResult: false })
  await assert.rejects(() => copyText('demo text'), /copy_failed/)
  assert.equal(calls.exec, 1)
})

t('回退路径成败都必须移除临时 textarea', async () => {
  const ok = installEnv({ execResult: true })
  await copyText('demo text')
  assert.equal(ok.created, 1)
  assert.equal(ok.removed, 1)

  const fail = installEnv({ execResult: false })
  await assert.rejects(() => copyText('demo text'))
  assert.equal(fail.created, 1)
  assert.equal(fail.removed, 1)
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
console.log(`check-copy-text: ${pass}/${cases.length} passed`)
process.exit(failed ? 1 : 0)
