# 数据面具 Maskit 项目工程规范与架构指南

> 本文件是 Data Maskit 开源项目的开发总控指南与唯一工程规范基准。

---

## 1. 架构总览

Data Maskit 是一款专为大模型打造的**100% 本地隐私脱敏与还原网关**。拦截开发终端（Cursor、Claude Code、Codex 等）发往 LLM 的 API 请求，在本地自动打码成业务占位符后转发上游，并在模型回答返回时毫秒级流式无感还原。

### 架构分层
```text
┌─────────────────────────────────────────────────────────────┐
│ 桌面壳层 (src-tauri/)                                       │
│   - 基于 Tauri 2 (Rust) 构建，系统托盘常驻、单实例控制       │
│   - 引擎进程生命周期管理、端口探活、开机自启自愈             │
└──────────────────────────────┬──────────────────────────────┘
                               │ (IPC / HTTP 本地通信)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 引擎核心层 (Python 3.13 + mitmproxy)                        │
│   - engine_entry.py: Sidecar 入口与自启编排                 │
│   - panel.py: Flask API 控制面服务 + WebUI 静态文件托管      │
│   - transparent.py: 核心脱敏/还原流式代理 Addon             │
│   - event_store.py: SQLite 本地事件库与统计维护              │
│   - audit_engine.py / audit_signals.py: 安全审计与风险引擎   │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 前端展示层 (frontend/)                                      │
│   - React 19 + TypeScript + Vite + Tailwind CSS + shadcn/ui │
│   - 顶栏一键中英双语快捷切换、深浅主题切换、自适应仪表盘     │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 核心文件与模块职责

| 路径 | 核心职责 | 维护与改动红线 |
|---|---|---|
| `transparent.py` | 核心脱敏代理引擎（mitmproxy addon） | 负责正则特征扫描、占位符生成、跨 chunk SSE 流式还原；**改动必须跑全量单测 + 流式冒烟**。 |
| `panel.py` | Flask 控制面服务（5801 端口） | 提供配置读写、代理启停、日志查询、统计分析与 WebUI 静态托管；`__version__` 为版本唯一真相来源。 |
| `engine_entry.py` | 引擎 Sidecar 运行入口 | 负责环境就绪探测、ACL 权限收紧、代理自启与透明直连兜底。 |
| `event_store.py` | SQLite 本地日志与统计引擎 | 负责事件写入批量事务、崩溃自愈、日统计增量维护；凭据类恒只存哈希摘要。 |
| `audit_signals.py` | 纯函数安全审计信号检测 | 严禁引入重依赖，保持纯 Python stdlib 实现，永不抛异常。 |
| `audit_engine.py` | 主动探针与安全风险矩阵评估 | 负责跨请求隔离性测试、提示词注入与投毒检测。 |
| `shield_defaults.py` | 默认脱敏规则与用量解析器 | 包含标准内置规则定义及各大模型 Token 用量解析提取。 |
| `frontend/` | React 19 现代化前端源码 | 新增 UI 必须遵守 Tailwind 类 + 语义组件，严格支持中文/英文双语国际化。 |
| `src-tauri/` | Tauri 2 跨平台桌面壳 | 系统托盘、开机自启自愈、引擎崩溃守护、进程隔离。 |
| `build.ps1` | 一键打包发版流水线 | 自动编排单测、前端构建、PyInstaller 引擎、Tauri NSIS 安装包。 |
| `Dockerfile` | 容器化部署配置 | 适用于 Linux / NAS / 团队私有网关运行，内嵌 Web 控制台。 |

---

## 3. 关键工程约束与安全红线

1. **100% 本地运算与零外传（绝对安全红线）**：
   - 脱敏与还原全部在本地进程内运行，严禁添加任何形式的用户追踪、云端日志收集或遥测上报代码。
2. **Fail-Closed 严格安全熔断**：
   - 脱敏管线发生未捕获异常或请求体超过 32MB 时，必须立即回退 503 阻断，**绝不放行未脱敏明文请求出网**。
3. **原生透明直连兜底（Passthrough，绝不断网）**：
   - 当用户停止代理或未启动脱敏代理时，本地端口必须由轻量兜底层维持监听，并执行**明文透明直连转发（Passthrough）**，确保用户的日常开发网络调用绝对不会因中间件原因中断。
4. **毫秒级 SSE 流式打字机接管**：
   - 针对 `text/event-stream` 响应，逐事件增量还原下发。跨 chunk 占位符必须智能缓冲拼接，中途无事件时返回空列表 `[]`（严禁返回 `b""` 导致 chunked 语法提前断连）。
5. **占位符规范（`{{LABEL_后缀}}`）**：
   - 后缀必须采用 6 位纯辅音随机串，彻底消除大模型对十六进制数做变异算术的诱因；
   - 跨请求维持滑动窗口复用，保证多轮对话上下文逻辑一致。
6. **凭据安全红线**：
   - API_KEY、TOKEN、SECRET、JWT、ACCESS_KEY、PRIVATE_KEY 类日志，在事件库中恒只存 preview + sha256 摘要，导出时恒剔除原文。
7. **控制面接口命名空间红线（`/api/` 前缀）**：
   - `panel.py` 的 `api_guard` 只对 `/api/*` 执行 Host / Origin / `X-Shield-Token` 三重校验；非 `/api/` 路径（SPA HTML、`/assets/*`、favicon 等由 `serve_spa` 托管）一律免检。这是为了让反向代理 / CDN（Nginx、EdgeOne）HTTPS 终止环境下，浏览器加载 `crossorigin` 模块脚本时携带的 Origin 不被误判 403 导致整页白屏（实测事故）。
   - 因此**任何新增的控制面端点必须挂在 `/api/` 前缀下**，否则会自动绕过全部安全防线；根空间只允许放纯静态资源。
   - 关闭 Origin 校验（`config.origin_check=false` 或环境变量 `MASKIT_DISABLE_ORIGIN_CHECK=1`）只应用于受信任反代 / CDN 场景，且必须在 `SECURITY.md` 中同步说明。

---

## 4. 验证与测试流程

代码变更后必须通过全量门禁。**唯一清单是 `scripts/verify-all.py`**（本地与 CI 共用，13 项）：

```powershell
python scripts/verify-all.py                 # 全跑
python scripts/verify-all.py --only python,version
python scripts/verify-all.py --list          # 打印清单（供漂移比对）
```

分组与 `.github/workflows/ci.yml` 的 job 一一对应：

| 组 | 对应 ci.yml job | 内容 |
|---|---|---|
| `python` | `python` | `py_compile engine/*.py`、`unittest discover -s tests`、`smoke_stream.py`、`smoke_egress.py` |
| `frontend` | `frontend` | `npm run build`、`npm run lint`、`check-i18n.mjs`、`check-env-import.mjs`（工作目录 `frontend/`） |
| `rust` | `rust` + `rust-macos` | `cargo check`、`cargo test --lib`（工作目录 `src-tauri/`） |
| `version` | `version` | `check-version.py`、`audit-public-release.py`、`check-workflows.py` |

> **为什么要有统一入口**：门禁原先散在 `build.ps1`（只跑「py_compile + 单测 + 前端构建」三样）
> 与 `ci.yml`（全量）两处，本地「过了 build.ps1 却被 CI 拦下」时 tag 已经推到远端了。
> 现在 `build.ps1` 直接调本脚本，`scripts/check-workflows.py` 会**双向比对**
> `verify-all.py` 与 `ci.yml`，任一侧漏加/多加都会在 PR 阶段报错。
> **新增门禁只需改 `verify-all.py` 的 `GATES` 与 `ci.yml` 两处**，不要再往文档里抄命令清单。

> `check-workflows.py` 是唯一需要额外依赖的校验脚本（`pyyaml`）：CI 不 lint workflow，
> YAML 或 `run` 块写坏只会在「推送后 Actions 页报错」才暴露，最坏拖到打 tag 发版时才炸。
> 只校验显式 `shell: bash` 的步骤（`shell: pwsh` 拿 bash 语法验必然误报）。

> Windows 本地若 `bash` 被解析成 WSL 垫片，用 `MASKIT_BASH=<PortableGit>\usr\bin\bash.exe` 覆盖；
> 找不到时脚本会跳过 shell 校验并告警（CI 在 ubuntu 上必跑）。

> **Windows 本地 Python 解释器**：裸 `python` 可能解析到未装 flask/mitmproxy 的版本
> （实测 3.14），导致 python 组门禁直接 ImportError。跑本地门禁时显式指定 3.13
> 解释器（已装齐 flask + mitmproxy + pyinstaller）：
> ```powershell
> python scripts/verify-all.py --python "C:\Python313\python.exe"
> ```
> 或先 `$env:MASKIT_PYTHON = "C:\Python313\python.exe"` 再直接跑；
> 解释器路径变化时以 `py -3.13 -c "import sys; print(sys.executable)"` 的实际输出为准。

### 运行时文件约定

- `engine/config.example.json` 是随包分发的配置模板；源码态首次运行在 `engine/` 生成 `config.json`（已 gitignore），打包态生成到用户数据目录。**不要把 `engine/config.json` 提交进仓库。**
- 事件库 `shield-events.sqlite3`、`proxy_token`、`config.json.bak-*`、`model_prices_cache.json` 等全部是运行时产物，已 gitignore / dockerignore，`build.ps1` 打包前会清理。
- 测试样例中凭据形态的字符串必须一眼可见是伪造的（`sk-test-0000…`），真实上游 key 只能来自环境变量 `LLM_SHIELD_API_KEY`。
- 新增任何对外网络请求必须默认关闭并登记到 `SECURITY.md` 出站清单。

---

## 5. 打包与发布规范

- **发版前置授权红线（绝对铁律，严禁擅自发版）**：
  - 任何 AI 助手（包括当前 Agent、任何子代理及后续会话）**严禁在未经用户明确书面授权确认的情况下执行任何发布动作**（包括但不限于：执行 `git push origin v*`、执行发版脚本 `release.ps1`、调用 GitHub API 创建 Release、修改线上 Release 状态）；
  - 发版前必须先完成所有本地全量门禁，并向用户展示最终变动清单与验证证据，**在用户明确发出“确认发版/发版吧”等指令后方可执行**。用户如果仅要求“检查/审计/看看”，本轮只输出报告，严禁顺手执行发版。

- **发版日志中英双语规范（强制）**：
  - 每次发版时，`CHANGELOG.md` 与 GitHub Release 说明必须提供**中英双语（Bilingual）对照**，方便海内外开发者理解变更细节；
  - 格式遵循 Keep a Changelog，重大修复与破坏性变动需附带中英文说明。
  - **CHANGELOG 维护工作流**：
    - **开发期间**：把变更条目（中英成对）追加到 `## [Unreleased]` 下方，按已有「新增 / 修复 / 优化」分节；
    - **发版时**：把 `## [Unreleased]` 改名为 `## [<version>] - <日期>`（或新建一节并把条目移过去）；**不要留下空的 Unreleased**，否则下一次发版起点会乱；
    - **发版前**请确认 `## [<version>]` 章节已存在 —— release-draft job 通过 `scripts/render-release-notes.py` 从 CHANGELOG 切出该章节作为双语 Release body；找不到该章节脚本会 `SystemExit(1)`，发版 job 直接失败（不会生成空 body 静默上线）。

- **多平台构建矩阵**：
  - **Windows 桌面端**：`x86_64` NSIS 安装包；
  - **macOS 桌面端**：`arm64`（Apple Silicon）DMG 安装包，由 GitHub Actions `macos-latest` 原生编译；
  - **Docker 容器**：`linux/amd64` 与 `linux/arm64` 双架构镜像（推送到 `ghcr.io`）。

- **一键全自动发版（推荐）**：使用 `release.ps1`，自动编排「前置 git pull 对齐 -> build.ps1 打包与门禁验证 -> git commit -> git tag -> 推送主分支与 Tag」：
  ```powershell
  # 自动读取并按当前/指定版本号完成构建、提交流水线与推送
  .\release.ps1 -Version "0.2.6"

  # 仅打包测试，不执行 git commit/push
  .\release.ps1 -BuildOnly
  ```

- **底层打包流水线**：`build.ps1`（纯构建编排，不含 Git 提交命令）：
  ```powershell
  # 发布模式（不杀本机运行中的生产实例）
  .\build.ps1 -ReleaseOnly

  # 或指定正式版本号打包
  .\build.ps1 -ReleaseOnly -Version "1.0.0"
  ```
- 打包产物位于：
  - Windows: `src-tauri\target\release\bundle\nsis\Maskit_<版本>_x64-setup.exe`；
  - macOS: `src-tauri/target/release/bundle/dmg/Maskit_<版本>_aarch64.dmg`。

---

## 6. 上游同步与版本血缘（本二开分支专属）

本分支（`lauchiwa/data-maskit`）从上游 `xiaYuTian11/maskit` 派生，长期需要持续合并上游更新。

### 版本号方案

**本分支版本号走独立的 `0.100.x` 段，与上游 `0.2.x` 无任何数值关系。**

- `__version__`（`engine/panel.py`，唯一真相来源）= **本分支自己**的发布序号。`minor` 记功能批次，`patch` 记修复；
- `__upstream_base__`（同文件，紧跟 `__version__`）= 本分支所基于的**上游版本**，只读元数据，不参与任何版本比较。

为什么不把上游版本号编进 `__version__`：`X.Y.Z` 只有三个槽位，塞不进两套计数器。四段式 `0.2.12.1` 被 Cargo 硬拒（`unexpected character '.' after patch version number`），`0.2.13-fork.1` 按 semver 规范**优先级低于** `0.2.13`（会被判定比上游旧），`0.2.13+fork.1` 的 build metadata 在版本比较中被忽略。选 `0.100.x` 段的实际收益：上游短期到不了 `minor=100`，所以上游发任何版本都不可能在更新检查里盖过本分支构建。

### 血缘在哪里看

| 位置 | 看到什么 |
|---|---|
| 设置 → 关于卡片 | 「当前版本 v0.100.x」下方一行「上游基线 v0.2.12」 |
| `GET /api/status` | `version` + `upstream_base` 两个字段 |
| 诊断导出 | `app.version` + `app.upstream_base` |
| `python scripts/check-upstream-sync.py` | 完整同步状态 + **校验声明真实性** |
| `CHANGELOG.md` | 每个版本章节开头注明「基于上游 vX.Y.Z」 |

`scripts/check-upstream-sync.py` 是唯一会**校验**而非仅展示的入口：它验证 `__upstream_base__` 声明的 tag 确实是 HEAD 的祖先。声明过期（合并了上游却忘改这一行，或写了个没合进来的版本）会导致排查问题时照着错误的上游代码找原因，所以这种情况退出码为 1。它还会预报下次合并的冲突文件（两边都改过的那些）。

该脚本**不进** `scripts/verify-all.py` 门禁：它依赖 `upstream` remote 及其 tag，而 CI 的 checkout 只有 `origin`，加进门禁必然失败。这是本地维护工具。

### 同步时机：只跟上游的「发布版」

**只有当上游打出比 `__upstream_base__` 更新的 tag 时才合。** 上游 `master` 平时领先几个未发版提交是常态，那些散装提交不合 —— 它们没经过上游自己的发版验证，合进来等于替上游做集成测试，而本分支的 `model_rules` 等改动跟它们叠在一起会放大冲突面。

`scripts/check-upstream-sync.py` 按这条策略区分两种输出：上游只是 `master` 领先时标注「按策略不合，仅供参考」；只有出现更新的 tag 才提示「可以同步了」。两种情况退出码都是 0，唯一报错的情形仍是血缘声明造假。

### 分叉面控制

本分支的原则是**尽量贴近上游**，二开只做必要的。新增改动前先掂量它会不会落进冲突集 —— 判断办法：

```powershell
# 两边都相对合并基线改过的文件 = 真正的冲突集
$mb = git merge-base master upstream/master
git diff --name-only $mb master | Sort-Object > ours.txt
git diff --name-only $mb upstream/master | Sort-Object > theirs.txt
# 取交集
```

新建文件不会冲突，改上游高频文件（`engine/transparent.py`、`frontend/src/lib/i18n.tsx`、`frontend/src/pages/Settings.tsx`、`engine/panel.py`）必然反复冲突。能放进新文件的就别往上游文件里塞。

### 同步上游的标准流程

```powershell
# 0. upstream remote（用 SSH：实测 HTTPS 直连会被 Connection reset，必须挂系统代理）
git remote add upstream git@github.com:xiaYuTian11/maskit.git

# 1. 看清差距（--fetch 会先拉一次上游）
python scripts/check-upstream-sync.py --fetch

# 2. 在分支上合并，不要直接在 master 上合
git switch -c merge/<上游版本>
git merge upstream/master

# 3. 解冲突。版本文件（panel.py / tauri.conf.json / Cargo.toml / Cargo.lock /
#    package.json / package-lock.json）的版本号一律保留本分支的值（ours）——
#    绝不能被上游的 0.2.x 覆盖回去，否则更新检查会判定「有新版本」并把用户
#    降级到上游构建。

# 4. 改 __upstream_base__ 为刚合进来的上游版本，然后验证声明真实性
python scripts/check-upstream-sync.py

# 5. 全量门禁
python scripts/verify-all.py --python "<3.13 解释器>"

# 6. 合回 master，按本分支自己的序号发版（patch+1 或 minor+1）
```

### 合并时必须守住的本分支改动

上游每次同步都可能覆盖掉这些，合完务必逐条确认：

- `src-tauri/tauri.conf.json` 的 `plugins.updater.endpoints` 与 `pubkey`（本分支自己的密钥，Key ID `8FDEF509963AB482`）。**被上游值覆盖 = 本分支构建会被上游发布覆盖掉**。注意 `endpoints` 当前值是历史遗留的失效地址（详见下节）；
- `engine/panel.py` 的 `__upstream_base__` 及其在 `/api/status`、诊断导出里的两处透出；
- `frontend/src/components/settings/AboutUpdateCard.tsx` 的「上游基线」行与 `about.upstreamBase*` 两个 i18n key；
- `scripts/generate-latest-json.py` 的 `--repo` 默认值（空串 → 回退环境变量 → 兜底本分支仓库）。

更新签名私钥在 `~/.tauri/maskit-updater.key`，**永不入库**。丢失后已安装的客户端只认对应公钥，再也无法推送任何更新，只能让用户手工重装 —— 必须在仓库外另做备份。

### 代码托管与发布现状

| remote | 地址 | 用途 |
|---|---|---|
| `origin` | `github.com:lauchiwa/data-maskit` | 本分支代码，**私有** |
| `upstream` | `github.com:xiaYuTian11/maskit` | 上游，公开 |

（历史：曾用 Gitee 私有仓库作 `origin`，已弃用；曾有一个 fork 上游的公开仓库 `lauchiwa/maskit`，已删除。两者均不再引用。）

#### origin 在 GitHub 后，CI 会真的跑

这是从 Gitee 迁回 GitHub 最实质的行为变化：`.github/workflows/` 之前在 Gitee 上是死文件，现在会被真实触发。

| workflow | 触发 | 作业 |
|---|---|---|
| `ci.yml` | push / PR 到 `master`、`main` | 5 个 job：python、frontend、version（ubuntu）+ rust（windows）+ rust-macos（macos） |
| `release.yml` | push `v*` tag、手动 | 打包 windows-nsis + macos-arm64，timeout 120 分钟 |
| `test-macos-build.yml` | 仅手动 | — |

**私有仓库的 Actions 分钟数计费，且按平台倍率扣：**Linux 1×、Windows 2×、**macOS 10×**。每推一次 `master` 就跑全部 5 个 job，其中 macOS 那个按 40 分钟超时上限算相当于扣 400 分钟额度。频繁推送前先算额度，或在仓库设置里禁用不需要的 job。

**推 tag 会触发 `release.yml`。** 它需要两个 secret：`MASKIT_UPDATER_PRIVATE_KEY` 与 `MASKIT_UPDATER_PRIVATE_KEY_PASSWORD`。**未配置就推 tag 的后果**：打出未签名（无 `.sig`）的安装包并留下草稿 Release，而本地那份已验签的产物可能被覆盖。要么先在 Settings → Secrets 配好密钥，要么用 `gh release create` 发本地产物（不推 tag，不触发 CI）。

#### 自动更新仍不可用

`plugins.updater.endpoints` 已指向 `github.com/lauchiwa/data-maskit`（即 `origin`），但**仓库是私有的，`tauri-plugin-updater` 匿名拉 `latest.json` 必定 404**。已安装客户端启动 8 秒后静默检查一次并静默失败 —— silent 模式不弹窗，不影响脱敏功能。分发靠手工发安装包。

与之前不同的是失败原因：旧端点指向一个**不存在**的仓库，现在指向一个**存在但私有**的仓库。行为上都是 404，但现在这个地址归你控制 —— 不再存在外人注册同名仓库、向你的客户端投馀 `latest.json` 的可能。（即便投也装不上：包要过 `pubkey` 的 Ed25519 校验，私钥不在仓库里。）

要真正启用自动更新，需要一个**允许匿名下载**的端点托管 `latest.json` 与安装包（代价是产物公开，但代码可以不公开）。改端点后**必须重新打包**：端点是编译进二进制的，改配置对已装客户端无效。
