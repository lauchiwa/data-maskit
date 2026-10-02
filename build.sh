#!/usr/bin/env bash
# ==============================================================================
# Data Maskit Linux 一键打包脚本（Tauri 版）：
# 前端构建 -> 引擎 sidecar (PyInstaller) -> Tauri bundle (.deb / .AppImage)
#
# 用法：
#   ./build.sh                     # 打包 deb 与 appimage
#   ./build.sh --bundles deb       # 仅打 deb 包
#   ./build.sh --bundles appimage  # 仅打 appimage
#   ./build.sh --version 0.6.2     # 指定版本打包
#   ./build.sh --no-gates          # 跳过门禁验证直接构建
# ==============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info() { echo -e "${CYAN}==>${NC} ${BOLD}$1${NC}"; }
success() { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}警告:${NC} $1"; }
error() { echo -e "${RED}错误:${NC} $1" >&2; }

RELEASE_ONLY=false
SKIP_GATES=false
TARGET_VERSION=""
BUNDLES="deb,appimage"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-only)
      RELEASE_ONLY=true
      shift
      ;;
    --no-gates)
      SKIP_GATES=true
      shift
      ;;
    --version)
      TARGET_VERSION="$2"
      shift 2
      ;;
    --bundles)
      BUNDLES="$2"
      shift 2
      ;;
    -h|--help)
      echo "用法: ./build.sh [选项]"
      echo "选项:"
      echo "  --bundles <list>   打包格式，默认 deb,appimage"
      echo "  --version <ver>    指定版本号（如 0.6.2）"
      echo "  --release-only     独立暂存目录构建，不替换源码态或已安装的引擎"
      echo "  --no-gates         跳过全量门禁检查"
      exit 0
      ;;
    *)
      error "未知参数: $1"
      exit 1
      ;;
  esac
done

info "开始 Data Maskit Linux 打包流水线..."

# 1. 探测 Python 解释器（需装齐 flask, mitmproxy, PyInstaller）
info "检查 Python 打包环境..."
PYTHON_BIN=""
CANDIDATES=(
  "${MASKIT_PYTHON:-}"
  "$HOME/.venvs/maskit/bin/python"
  "$ROOT_DIR/.venv/bin/python"
  "$ROOT_DIR/venv/bin/python"
  "$(which python3.13 2>/dev/null || true)"
  "$(which python3 2>/dev/null || true)"
)

for cand in "${CANDIDATES[@]}"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then
    if "$cand" -c "import flask, mitmproxy, PyInstaller" 2>/dev/null; then
      PYTHON_BIN="$cand"
      break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  error "未找到装齐依赖的 Python 解释器（需 flask + mitmproxy + pyinstaller）。"
  echo "提示: 请创建虚拟环境并安装依赖:"
  echo "  python3 -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  pip install -r requirements.txt -r requirements-dev.txt"
  echo "或设置环境变量 MASKIT_PYTHON=/path/to/python"
  exit 1
fi
success "使用 Python 解释器: $PYTHON_BIN ($($PYTHON_BIN --version))"

# 2. 检查 Node.js 与 npm
info "检查前端构建环境..."
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  error "未安装 Node.js 或 npm，请先安装 Node.js (>= 20)"
  exit 1
fi
success "Node.js: $(node --version), npm: $(npm --version)"

# 3. 检查 Rust 与 Cargo
info "检查 Rust 编译环境..."
if ! command -v cargo >/dev/null 2>&1 || ! command -v rustc >/dev/null 2>&1; then
  error "未安装 Rust / Cargo。请访问 https://rustup.rs 安装 Rust 工具链。"
  exit 1
fi
success "Rust: $(rustc --version)"

# 4. 检查 Linux 系统打包依赖 (WebKitGTK, AppIndicator, etc.)
info "检查 Linux 系统打包依赖..."
MISSING_PKGS=()
check_pkg() {
  local pkg="$1"
  if ! pkg-config --exists "$pkg" 2>/dev/null; then
    MISSING_PKGS+=("$pkg")
  fi
}

check_pkg "dbus-1" || true
check_pkg "webkit2gtk-4.1" || check_pkg "webkit2gtk-4.0" || true
check_pkg "ayatana-appindicator3-0.1" || check_pkg "appindicator3-0.1" || true
check_pkg "librsvg-2.0" || true

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
  warn "检测到部分系统依赖库可能缺失: ${MISSING_PKGS[*]}"
  echo "若打包过程报错，请在 Ubuntu/Debian 上执行:"
  echo "  sudo apt update && sudo apt install -y \\"
  echo "    pkg-config libdbus-1-dev libwebkit2gtk-4.1-dev librsvg2-dev patchelf libssl-dev libayatana-appindicator3-dev"
fi

# 5. 版本设置（若指定）
if [ -n "$TARGET_VERSION" ]; then
  info "同步指定版本号: $TARGET_VERSION..."
  "$PYTHON_BIN" scripts/bump-version.py "$TARGET_VERSION"
fi

# 6. 门禁前准备依赖；npm ci 失败必须中止，不回退到改写锁文件的 install。
if [ ! -d "frontend/node_modules" ]; then
  npm --prefix frontend ci
fi
mkdir -p src-tauri/resources/engine

# 全量门禁校验
if [ "$SKIP_GATES" = false ]; then
  info "执行全量门禁检查 (scripts/verify-all.py)..."
  if ! "$PYTHON_BIN" scripts/verify-all.py --python "$PYTHON_BIN"; then
    error "全量门禁未通过，终止打包。若确需跳过可用 --no-gates 参数。"
    exit 1
  fi
  success "全量门禁验证通过！"
else
  warn "已跳过全量门禁检查 (--no-gates)"
fi

# 7. 构建只按 spec 白名单收集资源，绝不删除源码态配置、凭据或事件库。
NER_MODEL_DIR="engine/models/ner_mini_zh"
NER_READY=true
for model_file in model_quantized.onnx tokenizer.json config.json; do
  if [ ! -s "$NER_MODEL_DIR/$model_file" ]; then NER_READY=false; fi
done
if [ "$NER_READY" = true ]; then
  success "NER 本地语义模型已就绪，将构建【全功能一体包】"
else
  warn "本地语义模型不完整；spec 会拒绝半套模型，完全缺失时才构建轻量规则包"
fi

# 8. 前端构建
info "构建前端静态资源..."
npm --prefix frontend run build
success "前端构建产物就绪 (frontend/dist)"

# 9. --release-only 使用新目录，不覆盖可能被源码态客户端使用的旧引擎。
ENGINE_DIST="$ROOT_DIR/dist_engine"
ENGINE_WORK="$ROOT_DIR/build_engine"
if [ "$RELEASE_ONLY" = true ]; then
  BUILD_STAGE=$(mktemp -d "${TMPDIR:-/tmp}/maskit-build.XXXXXX")
  ENGINE_DIST="$BUILD_STAGE/dist_engine"
  ENGINE_WORK="$BUILD_STAGE/build_engine"
  export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$BUILD_STAGE/tauri-target}"
  info "独立打包目录: $BUILD_STAGE"
fi
info "使用 PyInstaller 打包 Python 引擎 sidecar..."
"$PYTHON_BIN" -m PyInstaller engine/maskit-engine.spec --noconfirm --distpath "$ENGINE_DIST" --workpath "$ENGINE_WORK"

SRC_ENGINE="$ENGINE_DIST/MaskitEngine"
if [ ! -x "$SRC_ENGINE/MaskitEngine" ]; then
  error "PyInstaller 引擎产物缺失: $SRC_ENGINE/MaskitEngine"
  exit 1
fi

# 10. 验证实际打包产物；不安装、不调用生产面板、不依赖源码 PYTHONPATH。
SMOKE_ARGS=(--engine "$SRC_ENGINE/MaskitEngine")
if [ "$NER_READY" = true ]; then SMOKE_ARGS+=(--ner); fi
"$PYTHON_BIN" tests/smoke_transport.py "${SMOKE_ARGS[@]}"

# 11. 通过资源映射打包，不替换源码态或已安装的 resources/engine。
info "执行 Tauri 构建 (bundles: $BUNDLES)..."
if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ] && [ -n "${MASKIT_UPDATER_PRIVATE_KEY:-}" ]; then
  export TAURI_SIGNING_PRIVATE_KEY="$MASKIT_UPDATER_PRIVATE_KEY"
fi
UNSIGNED=false
if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
  info "未检测到更新签名密钥，以未签名模式 (unsigned) 构建..."
  UNSIGNED=true
fi
CONFIG_JSON=$("$PYTHON_BIN" - "$SRC_ENGINE" "$UNSIGNED" <<'PY'
import json, pathlib, sys
bundle = {"resources": {str(pathlib.Path(sys.argv[1]).resolve()) + "/": "resources/engine/"}}
if sys.argv[2] == "true":
    bundle["createUpdaterArtifacts"] = False
print(json.dumps({"build": {"beforeBuildCommand": None}, "bundle": bundle}))
PY
)
node frontend/node_modules/@tauri-apps/cli/tauri.js build --bundles "$BUNDLES" --config "$CONFIG_JSON" -- --locked

# 12. 产物校验与总结
info "校验打包产物..."
BUNDLE_DIR="${CARGO_TARGET_DIR:-$ROOT_DIR/src-tauri/target}/release/bundle"
OUTPUTS=()

if [[ ",$BUNDLES," == *,deb,* ]] && [ -d "$BUNDLE_DIR/deb" ]; then
  while IFS= read -r f; do
    OUTPUTS+=("$f")
  done < <(find "$BUNDLE_DIR/deb" -type f -name "*.deb")
fi

if [[ ",$BUNDLES," == *,appimage,* ]] && [ -d "$BUNDLE_DIR/appimage" ]; then
  while IFS= read -r f; do
    OUTPUTS+=("$f")
  done < <(find "$BUNDLE_DIR/appimage" -type f -name "*.AppImage")
fi

if [ ${#OUTPUTS[@]} -eq 0 ]; then
  error "未找到任何生成的打包产物 (.deb / .AppImage)！"
  exit 1
fi

echo ""
echo -e "${GREEN}================================================================${NC}"
echo -e "${GREEN}${BOLD}✓ Data Maskit Linux 打包成功！${NC}"
echo -e "${GREEN}================================================================${NC}"
echo "构建产物清单:"
for out in "${OUTPUTS[@]}"; do
  SIZE=$(du -h "$out" | cut -f1)
  echo -e "  - ${CYAN}$out${NC} (${SIZE})"
done
echo ""
echo "安装使用建议:"
echo "  • Debian/Ubuntu: 对上面列出的 .deb 文件执行 sudo dpkg -i <文件>"
echo "  • AppImage: 对上面列出的文件赋予执行权限后启动；本脚本不会自动安装或启动"
echo ""
