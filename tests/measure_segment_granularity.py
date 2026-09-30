# -*- coding: utf-8 -*-
"""NER 分段粒度 / 窗口跳过的实测对照（同进程内改 MAX_TEXT_CHARS 与 _CJK_RX 做 A/B）。

为什么要 A/B 脚本而不是拍脑袋：段长与窗口跳过的收益必须用**本机实测**说话，
注释里的数字全部来自这里（跑法：python tests/measure_segment_granularity.py）。
"""
import statistics
import sys
import time

sys.path.insert(0, "engine")
import ner_engine as n  # noqa: E402


def reset_cache():
    n._CACHE.clear()
    n._CACHE_CHARS = 0


def timeit(fn, rounds=3):
    out = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    return statistics.median(out)


CN = "杭州市西湖区文三路 88 号，某某科技集团有限公司，北京市朝阳区建国路 15 号院。"
EN = ("ERROR connection refused to upstream at 12:34:56 retry=3 "
      "host=api.example.com code=ECONNREFUSED\n")

n.extract_entities("预热")                                   # 模型初始化，不计时

# ── 场景 1：纯中文长文本，新旧段长各自冷推（看总量是否退化）──
text1 = (CN * 600)[:20000]
print("场景 1：纯中文 %d 字（整段冷推，中位数/3 轮）" % len(text1))
for order in ((20000, 4000), (4000, 20000)):
    row = []
    for seg in order:
        n.MAX_TEXT_CHARS = seg
        row.append((seg, timeit(lambda: reset_cache() or n.extract_entities(text1))))
    print("   顺序 %s -> %s" % (
        order, "  ".join("段长 %d: %6.0f ms" % (s, ms) for s, ms in row)))

# ── 场景 2：长文本改一个字后的第二轮（长会话每轮重发历史的真实形态）──
text2 = (CN * 400)[:12000]
print("场景 2：%d 字，第二轮只改中间 1 个字（只计第二轮首跑）" % len(text2))
for seg in (20000, 4000):
    n.MAX_TEXT_CHARS = seg
    reset_cache()
    n.extract_entities(text2)                                # 第一轮：建缓存
    stats0 = n.cache_stats()
    edited = text2[:6000] + "改" + text2[6001:]
    t0 = time.perf_counter()
    n.extract_entities(edited)
    ms = (time.perf_counter() - t0) * 1000
    after = n.cache_stats()
    print("   段长 %5d -> 第二轮 %7.0f ms   hit %d / miss %d   %s" % (
        seg, ms, after["hit"] - stats0["hit"], after["miss"] - stats0["miss"], {k: after[k] for k in ("size",)}))

# ── 场景 3：混合正文（中文与英文的相对密度决定窗口跳过能省多少）──
print("场景 3：混合正文的窗口跳过收益（段长 4000，中位数/3 轮）")
cases = {
    "中文段落每 ~1500 字一段": (CN + EN * 18) * 20,
    "中文与英文逐句交替": (CN + EN * 3) * 30,
}
for label, text3 in cases.items():
    cjk = sum(1 for ch in text3 if "\u4e00" <= ch <= "\u9fff")
    on = None
    real = n._CJK_RX
    for mode in ("ON", "OFF"):
        n._CJK_RX = real if mode == "ON" else type(
            "_Never", (), {"search": staticmethod(lambda _s: True)})()
        n.MAX_TEXT_CHARS = 4000
        ms = timeit(lambda: reset_cache() or n.extract_entities(text3))
        if mode == "OFF":
            n._CJK_RX = real
            print("   %s（%d 字 / 汉字 %.0f%%）: 跳过 %.0f ms vs 不跳过 %.0f ms -> 省 %.0f%%"
                  % (label, len(text3), 100.0 * cjk / len(text3), on, ms, (1 - on / ms) * 100))
        else:
            on = ms
