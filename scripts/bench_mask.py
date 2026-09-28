"""脱敏与审计性能基线：改造前后各跑一次，用来证明"变快了"而不是"感觉快了"。

**不进门禁**（跑几十秒、结果依赖本机 CPU 与核数），手动/发版前跑：

    py -3.13 scripts/bench_mask.py --save ai-coding/bench-before.json
    # …… 改造 ……
    py -3.13 scripts/bench_mask.py --save ai-coding/bench-after.json
    py -3.13 scripts/bench_mask.py --compare ai-coding/bench-before.json

为什么必须先抓基线（2026-09-26 教训）：并发改造的收益项原先写成
「p95 ≤ 改造前 60%」「审计 CPU 占比下降 ≥70%」，但当时**根本没有基线脚本**，
这两个数字既不可复现也不可验收 —— 改完之后谁都能宣称达标。

测五组指标，每组都直接锚在真实热路径上（不是另写一套模拟代码）：

  mask_single      单请求脱敏 `tr.mask()`           → 改造后不许劣化（±10%）
  mask_pool        经 `_MASK_POOL` 并发的逐请求延迟 → 队头阻塞的直接证据
  restore_whole    整包还原 `json.loads + _restore_tree + dumps`（8MB）
                                                   → 事件循环占用（A-3 的目标）
  audit_scan       审计三信号扫描（16/128/512KB）   → 0.46ms/KB 的口径来源
  audit_full       `_parse_response_payload` 全量解析 + 请求体全量 json.loads
                                                   → A-1 补上的那部分成本

CPU 口径：`time.process_time()` 是**进程内所有线程**的 CPU 时间，除以墙钟即
「这段时间里平均有几个核在忙」。放在审计组上用，就能回答"审计到底吃掉多少 CPU"。

产物只写用户显式 `--save` 的路径；默认不落任何文件（本机工作产物不入库，
`ai-coding/` 已在 .gitignore 里）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT))

# 敏感样本一律在运行时拼出来：`-----BEGIN...PRIVATE KEY-----` 的完整块与
# `AKIA...` 形态若以字面量进仓库，会被 scripts/audit-public-release.py 拦下
# （它就是干扫描这件事的）。这里要的是"能被审计信号识别"的输入，不是凭据本身。
FAKE_PEM = ("-----BEGIN " + "PRIVATE" + " KEY-----\n"
            + "MIIBOgIBAAJBAK" + "x" * 64 + "\n"
            + "-----END " + "PRIVATE" + " KEY-----")
FAKE_AWS = "AKIA" + "Z" * 16
# 显然伪造的样例：`.invalid` 是 RFC 2606 保留域，电话号码段用全 0，
# 任何人一眼就能看出「这是压测数据、不是真凭据」。
FAKE_EMAIL = "bench-user" + "@" + "example" + ".invalid"   # RFC 2606 保留域
FAKE_PHONE = "+86-100-" + "0000-0000"

FILLER = "这是用于压测的中文正文，包含一些正常业务描述与编号 %d。客户名称与项目代号需要脱敏。"


def _pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[idx]


def _stats(values):
    return {
        "n": len(values),
        "p50": round(_pct(values, 50), 3),
        "p95": round(_pct(values, 95), 3),
        "mean": round(statistics.fmean(values), 3) if values else 0.0,
        "max": round(max(values), 3) if values else 0.0,
    }


def _make_response_body(kb):
    """造一个 LLM 风格响应体，体积约 kb 千字节（内容可被三信号扫出东西）。"""
    target = max(1, kb) * 1024
    parts = []
    i = 0
    overhead = 0
    while overhead < target:
        chunk = FILLER % i
        if i == 3:
            chunk += " 联系邮箱 %s 电话 %s" % (FAKE_EMAIL, FAKE_PHONE)
        if i == 7:
            chunk += " 临时密钥 %s 与云凭据 %s" % (FAKE_PEM, FAKE_AWS)
        if i == 11:
            chunk += " 请执行 rm -rf /tmp/bench-cache 清理缓存"
        parts.append(chunk)
        overhead += len(chunk.encode("utf-8"))
        i += 1
    body = {"id": "bench", "model": "bench-model", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": "".join(parts)}}]}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _setup_env(tmp):
    import transparent as tr
    tr._DATA_ROOT = tmp
    (tmp / "config.json").write_text(json.dumps({
        "sensitive": {"甲类": ["压测敏感词%02d" % i for i in range(20)]},
        "fail_closed": True,
    }, ensure_ascii=False), encoding="utf-8")
    tr._maybe_reload(force=True)
    tr._emit = lambda *a, **k: None          # 事件落库不参与计时
    tr.sessions.clear()
    return tr


def bench_mask_single(tr, iters):
    """单请求脱敏：最接近用户感知的一段（规则扫描 + 签发 + 拼接）。"""
    text = ("客户压测敏感词01 与 02，编号 %d，" % iters) + FILLER % 1
    samples = []
    for i in range(iters):
        sid = "bench-single-%d" % (i % 8)
        t0 = time.perf_counter()
        tr.mask(text, sid)
        samples.append((time.perf_counter() - t0) * 1000)
    return _stats(samples)


def bench_mask_pool(tr, concurrency, per_thread):
    """经 `_MASK_POOL` 提交并发脱敏，记录**逐请求**的排队 + 执行延迟。

    这条是队头阻塞的直接证据：worker 宽度为 1 时，第 N 个请求的延迟包含
    前面 N-1 个请求的全部执行时间，p95 随并发线性上升。
    """
    total = concurrency * per_thread
    lat = []
    pool = getattr(tr, "_MASK_POOL", None)
    if pool is None:
        return {"n": 0, "note": "_MASK_POOL 不存在，跳过"}
    # 单条文本要够大，否则每条只花 0.02ms，量的是调度噪声而不是吞吐。
    # 20KB 文本（不触发 NER）单条约 2~4ms，队列效应才看得见。
    workload = ("编号 客户压测敏感词01 " + (FILLER % 1) * 60)

    async def run():
        loop = asyncio.get_running_loop()

        async def one(i):
            sid = "bench-pool-%d" % (i % 4)
            t0 = time.perf_counter()
            # ⚠️ 每条自己记账、并发提交（gather）：要把"排队 + 执行"都算进该条的
            # 延迟里，才能看出队头阻塞。写成"先提交一堆再顺序 await"会把
            # await 之前的等待时间漏掉，宽池反而显得更慢（实测踩过）。
            await loop.run_in_executor(pool, tr.mask, workload, sid)
            lat.append((time.perf_counter() - t0) * 1000)

        await asyncio.gather(*(one(i) for i in range(total)))

    t0 = time.perf_counter()
    asyncio.run(run())
    wall = time.perf_counter() - t0
    out = _stats(lat)
    out["wall_s"] = round(wall, 3)
    out["throughput_per_s"] = round(total / wall, 2) if wall else 0.0
    depth = getattr(tr, "_mask_queue_stats", None)
    if callable(depth):                     # A-6 之后才有
        out["queue"] = depth()
    return out


def bench_restore_whole(tr, body_kb, iters):
    """整包还原（事件循环上的 O(body) 活）耗时。"""
    raw = _make_response_body(body_kb)
    samples = []
    sizes = []
    for _ in range(iters):
        t0 = time.perf_counter()
        body = json.loads(raw)
        body = tr._restore_tree(body, "bench-restore")
        out = json.dumps(body, ensure_ascii=False).encode("utf-8")
        samples.append((time.perf_counter() - t0) * 1000)
        sizes.append(len(out))
    st = _stats(samples)
    st["body_kb"] = round(body_kb, 1)
    return st


def bench_audit(tr, sizes_kb, iters):
    """审计扫描成本，按体积给出口径；并顺带量全量解析那两块（A-1 的补丁目标）。"""
    import audit_signals as audit
    out = {}
    for kb in sizes_kb:
        raw = _make_response_body(kb)
        text = raw.decode("utf-8", errors="replace")
        headers = "content-type:application/json"
        scan_samples, parse_samples = [], []
        cpu0 = time.process_time()
        wall0 = time.perf_counter()
        for _ in range(iters):
            scan_text = text[:128 * 1024]
            t0 = time.perf_counter()
            audit.scan_error_leak(503, scan_text, headers)
            audit.scan_response_poison(scan_text, "")
            audit.scan_dangerous_action(scan_text, "")
            scan_samples.append((time.perf_counter() - t0) * 1000)

            t0 = time.perf_counter()
            tr._parse_response_payload(text, "application/json")
            try:
                json.loads(raw)
            except Exception:
                pass
            parse_samples.append((time.perf_counter() - t0) * 1000)
        cpu = (time.process_time() - cpu0) * 1000
        wall = (time.perf_counter() - wall0) * 1000
        out["%dKB" % kb] = {
            "scan_128k_window": _stats(scan_samples),
            "full_parse": _stats(parse_samples),
            "cpu_ms_total": round(cpu, 1),
            "cpu_ratio": round(cpu / wall, 3) if wall else 0.0,
        }
    return out


def _diff(before, after):
    """对比两组结果，只对"延迟型"指标给结论（越大越差）。"""
    rows = []

    def walk(path, b, a):
        if isinstance(b, dict) and isinstance(a, dict):
            for k in b:
                if k in a:
                    walk("%s.%s" % (path, k) if path else k, b[k], a[k])
            return
        if isinstance(b, (int, float)) and isinstance(a, (int, float)) and b:
            ratio = a / b
            rows.append((path, b, a, ratio))

    walk("", before, after)
    for path, b, a, ratio in rows:
        flag = ""
        if any(path.endswith(s) for s in (".p50", ".p95", ".mean", ".max", ".cpu_ms_total")):
            flag = "  <== 变慢" if ratio > 1.1 else ("  ok" if ratio <= 1.0 else "")
        print("  %-52s %10.3f -> %10.3f  x%.2f%s" % (path, b, a, ratio, flag))


def main():
    ap = argparse.ArgumentParser(description="脱敏/审计性能基线")
    ap.add_argument("--iters", type=int, default=30, help="mask_single 迭代次数")
    ap.add_argument("--restore-kb", type=float, default=8192, help="整包还原的 body 体积（KB）")
    ap.add_argument("--restore-iters", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=16, help="mask_pool 并发数")
    ap.add_argument("--per-thread", type=int, default=4, help="mask_pool 每并发请求数")
    ap.add_argument("--audit-sizes", default="16,128,512", help="审计体积点（KB，逗号分隔）")
    ap.add_argument("--audit-iters", type=int, default=5)
    ap.add_argument("--json", action="store_true", help="只输出 JSON（供脚本消费）")
    ap.add_argument("--save", default="", help="把本次结果写到该路径")
    ap.add_argument("--compare", default="", help="与该基线文件对比")
    ap.add_argument("--skip-restore", action="store_true", help="跳过 8MB 整包还原（省时间）")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp())
    tr = _setup_env(tmp)

    result = {
        "generated_at": int(time.time()),
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "mask_workers": int(getattr(tr, "_MASK_WORKER_COUNT", 1)),
        "ner_threads": int(getattr(tr, "_NER_INTRA_THREADS", 0) or 0),
        "body_kb": args.restore_kb,
    }
    result["mask_single"] = bench_mask_single(tr, args.iters)
    result["mask_pool"] = bench_mask_pool(tr, args.concurrency, args.per_thread)
    result["audit_scan"] = bench_audit(
        tr, [int(x) for x in args.audit_sizes.split(",") if x.strip()], args.audit_iters)
    if not args.skip_restore:
        result["restore_whole"] = bench_restore_whole(tr, args.restore_kb, args.restore_iters)

    if args.save:
        p = Path(args.save)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("bench_mask：python=%s cpu=%s workers=%s"
              % (result["python"], result["cpu_count"], result["mask_workers"]))
        for group in ("mask_single", "mask_pool", "restore_whole"):
            if group in result:
                print("  %-16s %s" % (group, json.dumps(result[group], ensure_ascii=False)))
        for kb, st in result["audit_scan"].items():
            print("  audit %-8s scan(p50/p95)=%.1f/%.1f ms  full_parse(p50)=%.1f ms  cpu_ratio=%.2f"
                  % (kb, st["scan_128k_window"]["p50"], st["scan_128k_window"]["p95"],
                     st["full_parse"]["p50"], st["cpu_ratio"]))
        if args.save:
            print("已保存：%s" % args.save)

    if args.compare:
        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print("与基线对比（%s，生成于 %s）：" % (args.compare, base.get("generated_at")))
        _diff(base, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
