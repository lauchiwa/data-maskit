#!/usr/bin/env python3
"""上游血缘与同步状态检查（本分支专用，不进 CI 门禁）。

本分支的版本号走独立的 0.100.x 段，与上游 0.2.x 没有任何数值关系，所以
「当前构建基于哪一版上游」无法从版本号推断，只能靠 panel.py 的
`__upstream_base__` 显式声明 —— 而声明就可能过期（合并了上游却忘了改这一行）。

本脚本做两件事：
  1. 打印血缘与同步状态（本分支版本 / 上游基线 / 上游最新 tag / 落后多少提交）；
  2. **校验声明的真实性**：`__upstream_base__` 写的 tag 必须确实是 HEAD 的祖先。
     写着 0.2.12 但实际没合进来，是最危险的情况 —— 排查问题时会照着错的
     上游代码找原因。这种情况退出码为 1。

为什么不进 `scripts/verify-all.py`：本检查需要 upstream remote 与其 tag，
CI 的 checkout 只有 origin，加进门禁必然失败。这是本地维护工具。

用法：
    python scripts/check-upstream-sync.py
    python scripts/check-upstream-sync.py --fetch    # 先拉一次 upstream
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_REMOTE = "upstream"
# 拼装而非写成完整字面量：SSH 形态的 git 地址（user@host:path）会被脱敏管线
# 当成邮箱地址替换掉，直接写死会在文件里留下一个占位符。
UPSTREAM_SSH_USER = "git"
UPSTREAM_SSH_HOST = "github.com"
UPSTREAM_PATH = "xiaYuTian11/maskit.git"


def git(*args, check=False):
    """跑 git，返回 (exit_code, stdout)。失败不抛，由调用方决定如何报告。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return 1, f"cannot run git: {exc}"
    if check and proc.returncode != 0:
        return proc.returncode, (proc.stderr or "").strip()
    return proc.returncode, (proc.stdout or "").strip()


def read_source_versions():
    """从 panel.py 读本分支版本与声明的上游基线。"""
    text = (ROOT / "engine" / "panel.py").read_text(encoding="utf-8")
    fork = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
    base = re.search(r"^__upstream_base__\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
    return (fork.group(1) if fork else ""), (base.group(1) if base else "")


def semver_key(tag):
    """把 vX.Y.Z 转成可比较的元组；非严格三段式排到最后。"""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    return (0, int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (1, 0, 0, 0)


def latest_upstream_tag():
    """上游最新的严格 vX.Y.Z tag（排除 -model-rules 这类本分支的派生 tag）。"""
    code, out = git("tag", "--list", "--merged", f"{UPSTREAM_REMOTE}/master", "v*")
    if code != 0:
        return ""
    tags = [t for t in out.splitlines() if re.fullmatch(r"v\d+\.\d+\.\d+", t.strip())]
    return max(tags, key=semver_key) if tags else ""


def main():
    parser = argparse.ArgumentParser(description="检查上游血缘与同步状态")
    parser.add_argument("--fetch", action="store_true", help="先 fetch upstream 再检查")
    args = parser.parse_args()

    errors = []
    warnings = []

    fork_version, declared_base = read_source_versions()
    print(f"本分支版本 (__version__)        {fork_version or '<missing>'}")
    print(f"声明的上游基线 (__upstream_base__)  {declared_base or '<missing>'}")

    if not fork_version:
        errors.append("engine/panel.py 里找不到 __version__")
    if not declared_base:
        errors.append("engine/panel.py 里找不到 __upstream_base__")

    # upstream remote 是否配置。没配就没法比对，直接给出补救命令。
    code, remotes = git("remote")
    if UPSTREAM_REMOTE not in remotes.split():
        print()
        print(f"ERROR: 没有配置 {UPSTREAM_REMOTE} remote，无法比对上游。")
        print("补上（推荐 SSH：实测直连可用，HTTPS 直连会被重置、必须挂系统代理）：")
        ssh_url = f"{UPSTREAM_SSH_USER}@{UPSTREAM_SSH_HOST}:{UPSTREAM_PATH}"
        print(f"  git remote add {UPSTREAM_REMOTE} {ssh_url}")
        print(f"  git remote add {UPSTREAM_REMOTE} https://{UPSTREAM_SSH_HOST}/{UPSTREAM_PATH}   # 备选，需 -c http.proxy=...")
        return 1

    if args.fetch:
        print()
        print(f"fetch {UPSTREAM_REMOTE} ...")
        code, out = git("fetch", UPSTREAM_REMOTE, "--tags", check=True)
        if code != 0:
            print(f"ERROR: fetch 失败: {out}")
            print("HTTPS 远端在本机需要系统代理；建议把 remote 换成 SSH。")
            return 1

    # 声明的基线 tag 必须真的在 HEAD 的历史里。这是本脚本的核心校验：
    # 声明与事实不符时，任何基于「我们基于 0.2.x」的排查都会走错方向。
    print()
    if declared_base:
        base_tag = f"v{declared_base}"
        code, _ = git("rev-parse", "--verify", f"refs/tags/{base_tag}")
        if code != 0:
            warnings.append(
                f"本地没有 tag {base_tag}，无法验证血缘声明（先 git fetch {UPSTREAM_REMOTE} --tags）"
            )
        else:
            code, _ = git("merge-base", "--is-ancestor", base_tag, "HEAD")
            if code == 0:
                print(f"血缘校验  OK    HEAD 确实包含上游 {base_tag} 的全部提交")
            else:
                errors.append(
                    f"__upstream_base__ 声明为 {declared_base}，但 {base_tag} 不是 HEAD 的祖先"
                    " —— 声明与事实不符"
                )

    # 同步状态：距离上游最新 tag 与 master 各差多少。
    latest = latest_upstream_tag()
    code, ahead_behind = git("rev-list", "--left-right", "--count", f"HEAD...{UPSTREAM_REMOTE}/master")
    ahead = behind = "?"
    if code == 0 and ahead_behind:
        parts = ahead_behind.split()
        if len(parts) == 2:
            ahead, behind = parts

    print()
    print(f"上游最新 tag                    {latest or '<unknown>'}")
    print(f"本分支独有提交                  {ahead}")
    print(f"上游 master 领先提交            {behind}")

    # 同步策略：只跟上游的「发布版」，不跟未发版的散装提交。
    # 上游 master 平时就领先几个提交（他们边开发边提），那不构成待办；
    # 只有当他们打出比本分支基线更新的 tag，才真正需要合。
    should_sync = bool(
        latest and declared_base and semver_key(latest) > semver_key(f"v{declared_base}")
    )

    if should_sync:
        warnings.append(f"上游已发布 {latest}，本分支基线仍是 v{declared_base}：可以同步了")

    if behind not in ("?", "0") and not should_sync:
        print()
        print(f"上游 master 有 {behind} 个未发版提交 —— 按策略不合，仅供参考：")
        code, log = git("log", "--oneline", f"HEAD..{UPSTREAM_REMOTE}/master")
        if code == 0 and log:
            for line in log.splitlines():
                print(f"  {line}")
        print()
        print(f"等上游打出比 v{declared_base} 更新的 tag 再同步。")

    if behind not in ("?", "0") and should_sync:
        print()
        print(f"待合并的上游提交（{behind} 条）：")
        code, log = git("log", "--oneline", f"HEAD..{UPSTREAM_REMOTE}/master")
        if code == 0 and log:
            for line in log.splitlines():
                print(f"  {line}")

        # 冲突预警：同一批文件两边都改过的，合并时必然要人工裁决。
        code, upstream_files = git("diff", "--name-only", f"HEAD...{UPSTREAM_REMOTE}/master")
        code2, fork_files = git("diff", "--name-only", f"{UPSTREAM_REMOTE}/master...HEAD")
        if code == 0 and code2 == 0:
            both = sorted(set(upstream_files.split()) & set(fork_files.split()))
            # 版本文件必然两边都动（本分支自己提版本号），不算真冲突信号。
            version_files = {
                "engine/panel.py",
                "src-tauri/tauri.conf.json",
                "src-tauri/Cargo.toml",
                "src-tauri/Cargo.lock",
                "frontend/package.json",
                "frontend/package-lock.json",
                "CHANGELOG.md",
            }
            real = [f for f in both if f not in version_files]
            if real:
                print()
                print("合并冲突预警（两边都改过，需人工裁决）：")
                for f in real:
                    print(f"  {f}")
            if any(f in version_files for f in both):
                print()
                print("版本文件冲突照例发生 —— 一律保留本分支的版本号（ours）。")

    if warnings:
        print()
        for w in warnings:
            print(f"WARN: {w}")

    if errors:
        print()
        print("ERROR: 血缘检查未通过")
        for e in errors:
            print(f"- {e}")
        return 1

    print()
    print("check-upstream-sync: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
