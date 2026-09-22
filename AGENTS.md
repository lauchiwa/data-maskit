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

代码变更后必须通过全量门禁。**唯一清单是 `scripts/verify-all.py`**（本地与 CI 共用，15 项）：

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
> （实测 3.14），导致 python 组门禁直接 ImportError。`verify-all.py` 已内置 `py -3.13`
> 自动探测与适配；若需显式指定解释器可传 `--python` 参数或环境变量：
> ```powershell
> python scripts/verify-all.py --python "D:\path\to\Python313\python.exe"
> ```
> 或先 `$env:MASKIT_PYTHON = "D:\path\to\Python313\python.exe"` 再直接跑。

### 运行时文件约定

- `engine/config.example.json` 是随包分发的配置模板；源码态首次运行在 `engine/` 生成 `config.json`（已 gitignore），打包态生成到用户数据目录。**不要把 `engine/config.json` 提交进仓库。**
- 事件库 `shield-events.sqlite3`、`proxy_token`、`config.json.bak-*`、`model_prices_cache.json` 等全部是运行时产物，已 gitignore / dockerignore，`build.ps1` 打包前会清理。
- 测试样例中凭据形态的字符串必须一眼可见是伪造的（`sk-test-0000…`），真实上游 key 只能来自环境变量 `LLM_SHIELD_API_KEY`。
- 新增任何对外网络请求必须默认关闭并登记到 `SECURITY.md` 出站清单。

### 推送后必须回查 CI（本地绿 ≠ 推送后绿）

本地门禁只覆盖「工作区里能被扫到的文件」。**实测事故（2026-09-19）**：`AUDIT-2026-09-19.md`
带着一个 PEM 私钥头字面量被顺手纳入版本控制，本地门禁**全绿**（该文件当时还没 `git add`，
而 `audit-public-release.py` 只扫 `git ls-files` 的输出 → 它看不见），推送后 CI 的 `version` job 直接红。

- 只要动过「会被门禁扫到的文件」，**推送后必须回查 CI 结论**，不能拿本地结果收工。
- **失败步骤名可能说谎**：`ci.yml` 的 `version` job 里那个步骤，`run` 块实际跑**两个**脚本
  （`check-version.py` + `audit-public-release.py`），而步骤名只提了版本号一致性 ——
  曾据此往「版本号不一致」的方向排查。看到失败先打开那个 `run` 块确认它到底跑了几件事。
- 复核某个**已推送提交**的门禁状态，用 `git worktree add --detach <sha> <tmpdir>` 隔离复现，
  不要切分支污染工作区；在隔离副本里可以放心地临时 `git add` 伪造凭据样本做反向验证。

---

## 5. 打包与发布规范

- **发版前置授权红线（绝对铁律，严禁擅自发版）**：
  - 任何 AI 助手（包括当前 Agent、任何子代理及后续会话）**严禁在未经用户明确书面授权确认的情况下执行任何发布动作**（包括但不限于：执行 `git push origin v*`、执行发版脚本 `release.ps1`、调用 GitHub API 创建 Release、修改线上 Release状态）；
  - 发版前必须先完成所有本地全量门禁，并向用户展示最终变动清单与验证证据，**在用户明确发出“确认发版/发版吧”等指令后方可执行**。用户如果仅要求“检查/审计/看看”，本轮只输出报告，严禁顺手执行发版。
  - **模型版本前置核验（用户约定，2026-09-19）**：严禁在自动化流水线中盲目外网拉取未知模型；当用户要求发版时，AI 助手在执行打包前应先主动确认当前依赖的本地语义模型（`ner_mini_zh`）是否存在官方权威重大升级；若有升级，向用户说明并在离线跑通 `test_benchmark_matrix.py` 评测矩阵后决定是否替换；若无升级，以本地已就绪模型直接构建【全功能一体包 (All-in-One)】。
    核验结论必须**带日期锚点写进发版 commit message**（形如「模型核验 2026-09-21：`ckiplab/bert-base-chinese-ner` 上游无更新」）—— 核验是 AI 对上游发布的事实判断，仓库里没有痕迹可复核，不写日期锚点等于这项检查不可审计。

- **发版日志中英双语规范（强制）**：
  - 每次发版时，`CHANGELOG.md` 与 GitHub Release 说明必须提供**中英双语（Bilingual）对照**，方便海内外开发者理解变更细节；
  - 格式遵循 Keep a Changelog，重大修复与破坏性变动需附带中英文说明。
  - **精简强制（用户约定，2026-09-15）**：CHANGELOG 条目、Release 说明、commit message 一律**每条中英各一行**，只说「改了什么、为什么」，不展开实现细节、不罗列边界用例——细节属于代码注释与测试，不属发版日志。同类小修合并为一条。禁止长篇大论。
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

- **分支与开发流转规范（Branching Strategy）**：
  - `master`：主干稳定分支，存放已验证、可随时上线的生产级代码。正式版 Tag（`vX.Y.Z`）仅在此分支打出；
  - `dev`：开发与先行测试分支，所有新特性开发（如浏览器扩展 XHR 拦截、文件脱敏适配等）先在 `dev` 分支迭代并自测。

- **Beta / 预发布版本更新隔离规范（Pre-release Isolation）**：
  - 尚在迭代或稳定性待验证的先行特性，若需提前打包分发测试，发布为 **Beta 预发布版本**（如 `v0.2.x-beta.N`）；
  - **客户端防打扰与更新隔离底线（客户端默认不检测 Beta 更新）**：桌面端 Tauri 更新器端点固定为 `releases/latest/download/latest.json`。GitHub 官方核心机制中，`releases/latest` 永远只指向最新的正式稳定 Release，**天然排除所有标记为 Pre-release 的预发布版本**。因此任何先行 Beta 发布在 GitHub Release 上必须显式标记为 **Pre-release**，且绝不能产出/覆盖正式版的 `latest.json`。现网所有已安装正式版客户端默认**绝对不会检测到 Beta 更新**，彻底杜绝未稳定改动骚扰普通用户。
  - **Beta 发布暂不支持（2026-09-21 实测：一键与手工两条路都走不通）**：缺口全在版本号读取正则（都带闭合引号 `__version__ = '(\d+\.\d+\.\d+)'`）—— `build.ps1` 的 105/107 与 184-188、`release.ps1:101`；`bump-version.py` 的入参校验 `fullmatch(r"\d+\.\d+\.\d+")` 同样不收 prerelease 后缀，所以连「手改版本号」那一步都进不去。
    走 `build.ps1 -Version "X.Y.Z-beta.N"` 时，**先炸的是 184-188 的读回校验**（在任何构建之前，因此并不浪费打包时间），报「版本分叉，打包中止」—— **该中止路径漏调 `Restore-Version`**（build.ps1 里其余 22 个失败点都调了），于是 7 个版本文件会留在 beta 的脏状态，而报错文案会把排查方向带向「哪个文件没同步」，实际是正则读不出来。`release.ps1` 见 `build.ps1` 非零即退出，到不了它自己的 101 行。
    **需要预发布时不要走这两条路**（今天没有任何一条能通）；补齐上述正则与 `Restore-Version` 之前**一律不发 beta**。这是独立专项，不与正式发版混在一个改动里做。

- **本地开发与联调热更新底线（用户明确约定，2026-09-20）**：
  - 当修改了引擎（`engine/`）、前端（`frontend/`）或相关脱敏逻辑需要用户进行联调测试时，**AI 助手必须主动调用本地全量部署安装脚本（`.\scripts\local-dev-deploy.ps1 -Full`）完成编译、安全备份、替换本地安装目录（`<Maskit 安装目录>`，即 Tauri 默认安装位置）并重启客户端进程**；
  - 严禁仅修改本地源码文件而不重新安装客户端就让用户进行测试验证（因为扩展调用的是已安装客户端 5801 端口的旧编译引擎，未安装会导致新脱敏逻辑完全不生效）。

---

## 6. 文档与产物的入库边界

判据只有一条：**后来者 clone 下这个仓库，还需不需要它？**
需要 → **项目资产**，入库；只服务本机某次会话 → **本机工作产物**，忽略。

### 6.1 入库（项目资产）

| 文件 | 为什么 |
|---|---|
| `README.md` / `README_EN.md` | 项目门面 |
| `CONTRIBUTING.md` / `CODE_OF_CONDUCT.md` / `SECURITY.md` | 协作与安全契约 |
| `CHANGELOG.md` | 发版日志（release 流水线从它切出 Release body） |
| `scripts/*.py` / `*.mjs` / `*.ps1` | 本地与 CI 共用的门禁、发版、打包、本地部署脚本。`verify-all.py` 是唯一门禁清单；`local-dev-deploy.ps1` 是 AGENTS.md §5 要求的联调热更新入口 —— 不入库则新克隆体拿不到它，却被告知「必须调用」 |
| `AGENTS.md` / `CLAUDE.md` | **工程规范基准**。`CLAUDE.md` 只有一行 `@AGENTS.md`，是跨工具入口 —— 不入库则 red line 在克隆体上整体丢失 |
| `docs/*.md` | 长期参考文档 |

> **`DESIGN-*.md` 刻意不入库（项目决策，2026-09-19）**：设计文档确实说明「代码为什么长成
> 这样」、否决了哪些方案，但它写于改动之前，落地时几乎必然与最终实现有偏差；一旦入库就会
> 被后来者当成现状读，反而误导。**代码注释与测试才是当前事实**，设计文档留在本机做决策留档。

### 6.2 忽略（本机工作产物）

已在 `.gitignore`：`ai-coding/`（开发规格 / 调研快照 / 交接 / 代码评审 / 会话证据）、
`.claude/`、`.codex/`、`.pi/`、`.pi-subagents/`、`.workbuddy-ai/`、`.workbuddy/`、
`.mcp.json`、`PROMOTION_GUIDE.md`、`AUDIT-*.md`、`DESIGN-*.md`、`HANDOVER.md`、`docs/*-REFERENCES.md`。

**共同特征**：内容是「某次会话当下的判断」，生命周期比代码短，会随代码演进迅速过期。
留在仓库里只会让后来者读到已经失效的结论。

### 6.3 已经踩过的坑

| 文件 | 事故 |
|---|---|
| `AUDIT-2026-09-19.md` | 被 `d4edbf6` 误纳入跟踪，文中 PEM 头部字面量把 CI 的 `version` job 打红。已 `git rm --cached` + 忽略 `AUDIT-*.md` |
| `HANDOVER.md` | 会话交接文件（自带「接棒 AI 必读」「最后更新: <时间戳>」等字段）。**已 `git rm --cached` 出库**并加入忽略 |

> ⚠️ **`.gitignore` 对已跟踪文件无效。** 要让规则生效必须先 `git rm --cached <file>`
> （本地文件保留），否则 `git status` 会一直显示它、下次 `git add -A` 又原样带进仓库 ——
> `d4edbf6` 就是这么发生的。**新增忽略规则时必须同时确认该文件是否已被跟踪。**

---

## 7. 上游同步与版本血缘（本二开分支专属）

本分支（`lauchiwa/data-maskit`）从上游 `xiaYuTian11/maskit` 派生，长期需要持续合并上游更新。

### 版本号方案

**本分支版本号走独立的 `0.1xx.x` 段（`minor ≥ 100`），与上游 `0.4.x` 无任何数值关系。**

- `__version__`（`engine/panel.py`，唯一真相来源）= **本分支自己**的发布序号。`minor` 记功能批次，`patch` 记修复；
- `__upstream_base__`（同文件，紧跟 `__version__`）= 本分支所基于的**上游版本**，只读元数据，不参与任何版本比较。

为什么不把上游版本号编进 `__version__`：`X.Y.Z` 只有三个槽位，塞不进两套计数器。四段式 `0.2.12.1` 被 Cargo 硬拒（`unexpected character '.' after patch version number`），`0.2.13-fork.1` 按 semver 规范**优先级低于** `0.2.13`（会被判定比上游旧），`0.2.13+fork.1` 的 build metadata 在版本比较中被忽略。选 `minor ≥ 100` 的实际收益：上游短期到不了 `minor=100`，所以上游发任何版本都不可能在更新检查里盖过本分支构建。每次同步上游后按功能批次 `minor+1`（`0.100` → `0.101` → …），纯修复则 `patch+1`。

### 血缘在哪里看

| 位置 | 看到什么 |
|---|---|
| 设置 → 关于卡片 | 「当前版本 v0.1xx.x」下方一行「上游基线 v0.4.0」 |
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
#    绝不能被上游的 0.4.x 覆盖回去，否则更新检查会判定「有新版本」并把用户
#    降级到上游构建。

# 4. 改 __upstream_base__ 为刚合进来的上游版本，然后验证声明真实性
python scripts/check-upstream-sync.py

# 5. 全量门禁
python scripts/verify-all.py --python "<3.13 解释器>"

# 6. 合回 master，按本分支自己的序号发版（patch+1 或 minor+1）

# 7. **每次发版必须打 tag**（annotated），哪怕不推送、不走 release.yml
git tag -a v0.1xx.x -m "发布 v0.1xx.x"
```

#### 发版必打 tag：漏打会让发版说明谎报「扩展无改动」

**实测错报（v0.102.0）**：`v0.101.0`、`v0.101.1` 当时都只改了版本号、没打 tag。
`scripts/render-release-notes.py` 用 `git describe --tags --abbrev=0 HEAD^` 找「上一个版本」，
而合并上游会把上游的 tag（`v0.4.0`）一起带进本分支历史 —— 它距离 HEAD 只有 20 个提交，
比本分支上一个真 tag `v0.100.0`（31 个）更近，于是 `describe` 挑中了**上游的** tag。
接着 `git diff v0.4.0 HEAD -- extension/` 恒为空（上游那批扩展改动正是从这个 tag 合进来的），
发版说明于是印出「✅ 扩展无改动，无需重新加载」。

后果不是文案瑕疵：**扩展代码变了而用户没重载，扩展会静默失效**（网页版 AI 的请求不再进引擎，
等于不脱敏出网）。

只要每次发版都打上本分支的 tag，`describe` 就一定先挑到本分支自己的 tag，判定自然正确。
这也是为什么这条规则与「是否推送 / 是否走 CI 发版」无关 —— 它保护的是**下一次**发版的判定。

### 合并时必须守住的本分支改动

上游每次同步都可能覆盖掉这些，合完务必逐条确认：

- `src-tauri/tauri.conf.json` 的 `plugins.updater.endpoints` 与 `pubkey`（本分支自己的密钥，Key ID `8FDEF509963AB482`）。**被上游值覆盖 = 本分支构建会被上游发布覆盖掉**；
- `engine/panel.py` 的 `__upstream_base__` 及其在 `/api/status`、诊断导出里的两处透出；
- `frontend/src/components/settings/AboutUpdateCard.tsx` 的「上游基线」行与 `about.upstreamBase*` 两个 i18n key；
- `scripts/generate-latest-json.py` 的 `--repo` 默认值（空串 → 回退环境变量 → 兜底本分支仓库）。

更新签名私钥在 `~/.tauri/maskit-updater.key`，**永不入库**。丢失后已安装的客户端只认对应公钥，再也无法推送任何更新，只能让用户手工重装 —— 必须在仓库外另做备份。

### 代码托管与发布现状

| remote | 地址 | 用途 |
|---|---|---|
| `origin` | `github.com:lauchiwa/data-maskit` | 本分支代码，**公开**（2026-09 由私有转公开） |
| `upstream` | `github.com:xiaYuTian11/maskit` | 上游，公开 |

（历史：曾用 Gitee 私有仓库作 `origin`，已弃用；曾有一个 fork 上游的公开仓库 `lauchiwa/maskit`，已删除。两者均不再引用。）

#### origin 在 GitHub 后，CI 会真的跑

这是从 Gitee 迁回 GitHub 最实质的行为变化：`.github/workflows/` 之前在 Gitee 上是死文件，现在会被真实触发。

| workflow | 触发 | 作业 |
|---|---|---|
| `ci.yml` | push / PR 到 `master`、`main` | 5 个 job：python、frontend、version（ubuntu）+ rust（windows）+ rust-macos（macos） |
| `release.yml` | push `v*` tag、手动 | 打包 windows-nsis + macos-arm64，timeout 120 分钟 |
| `test-macos-build.yml` | 仅手动 | — |

**Actions 分钟数不再是约束。**`origin` 转公开后，标准 GitHub-hosted runner（Linux / Windows / macOS 全部）免费且不限量，私有仓库那套「Linux 1×、Windows 2×、macOS 10× 倍率 + 2000 分钟额度」对本仓库已不适用。推送前不需要再算额度。

两个仍然收费的例外，本仓库目前都没碰到：**larger runners** 在公开仓库也照常计费（`ci.yml` / `release.yml` 用的都是标准 runner），以及 2026-03-01 起 self-hosted runner 的 $0.002/min 平台费 —— 该费用同样豁免公开仓库。

**推 tag 会触发 `release.yml`。** 它需要两个 secret：`MASKIT_UPDATER_PRIVATE_KEY` 与 `MASKIT_UPDATER_PRIVATE_KEY_PASSWORD`（**均已于 2026-09-22 配置完成**；私钥密码为空字符串，故第二个 secret 的值是空）。未配置时的退化行为：打出未签名（无 `.sig`）的安装包，**Release 强制留在草稿**（不会自动转正）—— 这是故意的，转正会让全体现网用户的 `releases/latest/download/latest.json` 立即 404。

**推 tag 前必须看清该 tag 自己的 `release.yml`。** GitHub 用的是 **tag 指向提交里**的 workflow 文件，不是 `master` 的当前版本。实测（补打历史 tag 时）：`v0.101.0` / `v0.101.1` 指向的提交里 `release.yml` 只有 `workflow_dispatch`，所以推它们**不会**触发发版；而 `v0.100.0` 与上游的 `v0.3.2` / `v0.4.0` 那里带着 `push: tags`，推过去会真的开构建。

由此得出一条硬规矩：**永远不要 `git push --tags`**。本仓库本地有 19 个 tag，其中包含从上游合并时带进来的 `v0.2.x`–`v0.4.0`；一次 `--tags` 等于同时开出十几个发版构建，并把上游版本发成本仓库的 Release。每次只显式推要发的那一个：`git push origin v0.1xx.x`。

#### 自动更新：已于 v0.102.0 正式打通

历史上这里的结论是「必定 404，因为仓库私有」。`origin` 转公开后该阻塞消失，并已于 **2026-09-22 随 v0.102.0 完成验证**：

| 环节 | 实测结果 |
|---|---|
| 端点匿名可达 | `curl` 清空凭据拉 `releases/latest/download/latest.json` → **HTTP 200** |
| `latest.json` 内容 | `version: v0.102.0`，含 `windows-x86_64` 与 `darwin-aarch64` 两个平台条目 |
| 签名血缘 | 两个平台的 signature key id 均 = 客户端内置 pubkey 的 `8fdef509963ab482`（**MATCH**） |
| Release 状态 | `draft=false`，签名完整所以自动转正 |

因此现在每次按流程推一个 `v*` tag，已安装客户端就会在启动 8 秒后检测到并拉取更新。

两个必须继续守住的前提：

- **`latest.json` 绝不能缺**。新 Release 一旦转正即成为 `releases/latest`，没有 `latest.json` 就是全体现网用户静默断更（客户端 silent 模式不弹窗，没人会来报）。`release.yml` 已用「未签名则强制留草稿」兜着这一点，不要绕过它手动转正；
- **Beta 预发布不传 `latest.json`**（脚本已自动识别 `-beta` / `-alpha` / `-rc`），否则现网普通客户端会误检测到预发布版。

端点本身无需再改，也不需要为此重新打包。反过来说，将来若要换端点，**必须重新打包** —— 端点是编译进二进制的，改配置对已装客户端无效。

安全性不受转公开影响：投毒的包装不上，产物要过 `pubkey` 的 Ed25519 校验，而私钥不在仓库里。
