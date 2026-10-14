/**
 * 普通事件日志的共享合并与排序逻辑（Dashboard 最近日志 与 Logs 拦截日志共用）。
 *
 * 需求背景：一次 LLM 请求会产生 MASK（请求脱敏）+ 终态（RESTORE 回复还原 / CANCEL 取消 /
 * ERR 失败）多条事件，若原样展示会在一张表里占多行、且顺序按接口返回倒序不稳定，用户
 * 要求「一次请求对应一条日志」。因此按 sid 把它们合成一行「往返链路」。
 *
 * ## 折叠语义（2026-10-13 起）
 *
 * - MASK 提供：ts/host/path/count(脱敏数)/upstream(客户端名)/mask_ms
 * - RESTORE 提供：restored/http_status/upstream_ms/total_ms/cost_usd/status/model
 * - CANCEL / ERR 提供：reason/failure_owner/error_type/upstream_idle_s（折叠行的**终态归因**）
 * - 折叠结果写 `terminal` 字段（RESTORE / CANCEL / ERR），**列表的「结论」列靠它区分
 *   「成功 / 已取消 / 失败」** —— 折叠行的 `type` 仍是 MASK，只看 type 会把三种结果
 *   渲染成一模一样的「脱敏」。
 *
 * 为什么 CANCEL/ERR 也要折进来（此前它们独立成行、且默认被当噪声隐藏）：实测本机事件库
 * 最近 24h 的同 sid 组合是 `MASK+RESTORE 1437` / `MASK+CANCEL 960` / `MASK+ERR 7`，
 * **完全 1:1 无歧义**。独立成行的后果是：CANCEL 被噪声开关藏起来后，列表里只剩一条
 * 「孤儿 MASK 行」（状态「—」、耗时是脱敏耗时的几百毫秒），用户看到的是**半条链路**，
 * 无从判断这次请求到底成没成。
 *
 * - unresolved/degraded 只在 RESTORE 事件上产生，必须带过来，否则合并行永远显示不出
 *   「未还原 / 兜底还原」。
 * - 详情回源优先终态事件的 seq（RESTORE → 失败归因事件 → MASK），弹窗才能落到最相关的那条。
 * - **绝不吞日志**：同 sid 的第三条同类事件、以及没有 sid 的事件（SCAN_WARN/DNS_ERROR 等）
 *   一律独立成行；没有配对 MASK 的孤立 CANCEL 仍按噪声处理（保持既有默认行为）。
 *
 * 审计行（SCAN_WARN 审计信号）由调用方在合并结果之上再并入，本模块不负责。
 */
import type { ShieldEvent } from '@/types/api'
// 必须带 `.ts` 后缀：`scripts/check-log-outcome.mjs` 用 node --experimental-strip-types
// 直接跑本文件（与 lib/env-import.ts 同一约定），Node 的 ESM 解析不做扩展名补全。
import { deriveOutcome, outcomeFromFilter } from './log-outcome.ts'

/**
 * 合并结果里的普通事件行。
 *
 * `_audit=false` 与审计行区分；`_detailSeq` 供详情回源；`terminal` 是本行折叠到的终态
 * 事件类型（缺省 = 只有请求侧记录，即「进行中 / 未完成」）。
 */
export type MergedEvent = ShieldEvent & {
  _audit: boolean
  _detailSeq?: number
  terminal?: 'RESTORE' | 'CANCEL' | 'ERR'
}

export interface MergeOptions {
  /** 事件类型筛选。传 "全部" 哨兵（FILTER_ALL）时不过滤，与 Logs 页常量对齐。 */
  filterType?: string
  /** 隐藏噪声（SKIP/PASS/BYPASS/CANCEL/DNS_ERROR），默认 false。 */
  hideNoise?: boolean
  /**
   * 当前时间（epoch 秒）。结论里的「进行中 / 未完成」需要它；缺省取系统时间。
   * 显式传入是为了让用例能固定时间轴（纯函数约定）。
   */
  nowSec?: number
}

/** Logs 页「全部类型」筛选项的值（哨兵，不会与事件类型撞）。 */
export const FILTER_ALL = '__ALL__'

/** 「放行与透传」合并筛选项：PASS/BYPASS/SKIP 三种原语合看。 */
export const PASS_COMBINED = 'PASS_COMBINED'

/** 只看浏览器扩展入口的筛选项。 */
export const EXT_ALL = 'EXT_ALL'

/** 不参与精确类型过滤的哨兵值。 */
const SENTINEL_FILTERS = new Set([FILTER_ALL, PASS_COMBINED, EXT_ALL])

/** 可参与「一次请求一行」折叠的事件类型：请求侧 + 三种终态。 */
const FOLD_TYPES = new Set(['MASK', 'RESTORE', 'CANCEL', 'ERR'])

/** 折叠前就能判定的噪声（不参与折叠，可以提前丢，省掉分组开销）。
 *
 * 注意这里**不含 CANCEL**：CANCEL 的语义已经变了 —— 它会被折进配对的 MASK 行
 * （折叠行 type=MASK，不受噪声影响），只有**没有配对 MASK 的孤立 CANCEL** 才算噪声，
 * 那一条判在折叠之后（见 mergeMaskRestore 第 3 步）。
 */
const NOISE_PRE_TYPES = new Set(['SKIP', 'PASS', 'BYPASS', 'DNS_ERROR'])

/** 独立成行的普通事件（不参与折叠）。 */
function plainRow(e: ShieldEvent): MergedEvent {
  return { ...e, _audit: false, _detailSeq: undefined }
}

/**
 * 把普通事件列表合并为「一次请求一行」并按 ts 最新在前排序。
 *
 * @param events 原始事件列表（ShieldEvent[]）
 * @param opts 筛选选项；缺省时全部保留、不隐藏噪声
 * @returns 合并后的行，最新 ts 在前
 */
export function mergeMaskRestore(events: readonly ShieldEvent[], opts: MergeOptions = {}): MergedEvent[] {
  const { filterType, hideNoise } = opts
  const nowSec = opts.nowSec ?? Date.now() / 1000
  const outcomeFilter = filterType ? outcomeFromFilter(filterType) : null
  // 「全部」之外的任何筛选都是用户的**显式**意图，优先于「隐藏噪声」这个便捷默认值：
  // 否则在下拉里选「用户取消」「透传放行」只会得到空列表 —— 这些类型本就在噪声集合里
  // （旧实现就是这个表现：筛选器里选得出、列表里永远为空）。
  const explicitFilter = filterType !== undefined && filterType !== FILTER_ALL
  const applyNoise = Boolean(hideNoise) && !explicitFilter
  const exactType =
    filterType && !SENTINEL_FILTERS.has(filterType) && !outcomeFilter ? filterType : null

  // 1) 预筛：只处理「折叠前就能判定」的条件；结论与孤立 CANCEL 的噪声判定留到折叠之后
  //    （CANCEL 必须先参与折叠，否则配对信息就丢了）。
  const pre = events.filter((e) => {
    if (applyNoise && NOISE_PRE_TYPES.has(e.type)) return false
    if (filterType === EXT_ALL) return e.ingress === 'ext'
    if (filterType === PASS_COMBINED) return e.type === 'PASS' || e.type === 'BYPASS' || e.type === 'SKIP'
    if (exactType) return e.type === exactType
    return true
  })

  // 2) 按 sid 分组折叠
  interface Group {
    mask?: ShieldEvent
    restore?: ShieldEvent
    cancel?: ShieldEvent
    err?: ShieldEvent
    /** 同 sid 的第三条同类事件：独立成行，绝不静默覆盖已折叠的行 */
    extra: ShieldEvent[]
  }
  const groups = new Map<string, Group>()
  const independent: MergedEvent[] = []
  for (const e of pre) {
    const sid = e.sid || ''
    if (!sid || !FOLD_TYPES.has(e.type)) {
      independent.push(plainRow(e))
      continue
    }
    let g = groups.get(sid)
    if (!g) {
      g = { extra: [] }
      groups.set(sid, g)
    }
    if (e.type === 'MASK' && !g.mask) g.mask = e
    else if (e.type === 'RESTORE' && !g.restore) g.restore = e
    else if (e.type === 'CANCEL' && !g.cancel) g.cancel = e
    else if (e.type === 'ERR' && !g.err) g.err = e
    else g.extra.push(e)
  }

  // 3) 每个 sid 出一行
  const rows: MergedEvent[] = []
  for (const g of groups.values()) {
    for (const x of g.extra) rows.push(plainRow(x))
    const base = g.mask ?? g.restore ?? g.err ?? g.cancel
    if (!base) continue
    // 没有配对 MASK 的孤立 CANCEL 仍是噪声（保持既有默认行为；有 MASK 的折叠行不受影响）
    if (applyNoise && !g.mask && !g.restore && !g.err && g.cancel) continue
    // 终态优先、请求侧兜底；缺的那侧用 base 兜底。
    // **表达式必须保持 `r.x ?? m.x` 的形状**：tests/test_attribution.py 的守卫用例按这个
    // 形状检查「引擎的每个 NER 指标都真的被带进了前端合并行」。改成可选链（r?.x ?? m?.x）
    // 会让那条守卫失配 —— 它守的是「字段有没有被带过来」，不是语法，没必要为改写法去动它。
    const r = g.restore ?? base
    const m = g.mask ?? base
    // 终态优先级：RESTORE（响应跑完）> ERR > CANCEL。实测同 sid 不会同时出现多个终态，
    // 这里只是把「真出现时选哪个」写死，避免行为随 Map 顺序漂移。
    const termEvent = g.restore ?? g.err ?? g.cancel
    const terminal: MergedEvent['terminal'] = g.restore ? 'RESTORE' : g.err ? 'ERR' : g.cancel ? 'CANCEL' : undefined
    rows.push({
      // 请求侧是「这次请求」的锚：ts/host/path/count/upstream 取 MASK（无 MASK 时才退回终态）
      ...base,
      _audit: false,
      terminal,
      // 详情回源：优先响应侧终态（弹窗含请求+回复完整链路），其次是失败归因事件
      // （CANCEL/ERR 自带 reason/failure_owner，点开就能看到为什么失败），最后才是 MASK。
      _detailSeq: (g.restore ?? g.err ?? g.cancel ?? g.mask ?? base).seq,
      http_status: r.http_status ?? m.http_status,
      upstream_ms: r.upstream_ms ?? m.upstream_ms,
      total_ms: r.total_ms ?? m.total_ms,
      status: r.status ?? m.status,
      restored: r.restored ?? m.restored,
      unresolved: r.unresolved ?? m.unresolved,
      degraded: r.degraded ?? m.degraded,
      stream_actual: r.stream_actual ?? m.stream_actual,
      // A terminal snapshot owns the evidence, including explicit unknowns.
      transport: r.transport ?? m.transport,
      failure_phase: r.failure_phase ?? m.failure_phase,
      upstream_may_have_executed: r.upstream_may_have_executed ?? m.upstream_may_have_executed,
      ner_init_ms: r.ner_init_ms ?? m.ner_init_ms,
      ner_infer_ms: r.ner_infer_ms ?? m.ner_infer_ms,
      ner_budget_wait_ms: r.ner_budget_wait_ms ?? m.ner_budget_wait_ms,
      ner_calls: r.ner_calls ?? m.ner_calls,
      ner_windows: r.ner_windows ?? m.ner_windows,
      ner_global_throttled: r.ner_global_throttled ?? m.ner_global_throttled,
      ner_sem_wait_ms: r.ner_sem_wait_ms ?? m.ner_sem_wait_ms,
      ner_cache_hits: r.ner_cache_hits ?? m.ner_cache_hits,
      ner_cache_misses: r.ner_cache_misses ?? m.ner_cache_misses,
      cost_usd: r.cost_usd ?? m.cost_usd,
      model: r.model || m.model || '',
      upstream: r.upstream || m.upstream || '',
      // 终态归因：状态列据此显示「客户端断开 / 上游静默 106s / 域名解析失败」
      reason: termEvent?.reason,
      failure_owner: termEvent?.failure_owner,
      error_type: termEvent?.error_type,
      upstream_idle_s: termEvent?.upstream_idle_s,
    })
  }

  let out = [...rows, ...independent]
  // 结论是**派生量**（后端按 type 精确过滤，没有 outcome 维度），所以这一层在客户端筛。
  // 代价与既有的 PASS_COMBINED 相同：只作用于已拉取到的窗口。
  if (outcomeFilter) out = out.filter((row) => deriveOutcome(row, nowSec) === outcomeFilter)
  // 按 ts 降序（最新在前）
  return out.sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0))
}
