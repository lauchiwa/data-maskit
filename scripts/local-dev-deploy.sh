#!/usr/bin/env bash
# ==============================================================================
# scripts/local-dev-deploy.sh
# 本地极速部署与回滚脚本 (Local Dev Hot Update & Rollback for Linux)
#
# 适用场景：本地开发、调试脱敏/还原/NER/扩展逻辑，免去发版等待与重复安装
# 用法：
#   ./scripts/local-dev-deploy.sh             # 默认快速更新引擎 (约15~20秒，只打包并替换 Python 引擎)
#   ./scripts/local-dev-deploy.sh --full      # 全量更新 (前端 + 引擎 + 重新编译 Tauri 壳)
#   ./scripts/local-dev-deploy.sh --restore   # 一键回滚到上一个备份版本
#   ./scripts/local-dev-deploy.sh --target <dir> # 指定安装目录
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

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

TARGET_DIR=""
PYTHON_BIN=""
FULL_UPDATE=false
RESTORE_BACKUP=false
NO_RESTART=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)
      FULL_UPDATE=true
      shift
      ;;
    --restore)
      RESTORE_BACKUP=true
      shift
      ;;
    --no-restart)
      NO_RESTART=true
      shift
      ;;
    --target)
      TARGET_DIR="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    -h|--help)
      echo "用法: ./scripts/local-dev-deploy.sh [选项]"
      echo "选项:"
      echo "  --full            全量更新（前端 + 引擎 + 壳）"
      echo "  --restore         回滚至上一次部署备份"
      echo "  --no-restart      更新后不自动重启客户端"
      echo "  --target <dir>    指定安装目录"
      echo "  --python <path>   指定 Python 解释器"
      exit 0
      ;;
    *)
      error "未知参数: $1"
      exit 1
      ;;
  esac
done

# 1. 自动定位 Python 解释器
if [ -z "$PYTHON_BIN" ]; then
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
fi

if [ -z "$PYTHON_BIN" ]; then
  error "未找到具备 PyInstaller 的 Python 解释器"
  exit 1
fi

# 2. 定位安装目录
if [ -z "$TARGET_DIR" ]; then
  if [ -n "${MASKIT_INSTALL_DIR:-}" ] && [ -d "$MASKIT_INSTALL_DIR" ]; then
    TARGET_DIR="$MASKIT_INSTALL_DIR"
  elif [ -d "$HOME/.local/share/maskit" ]; then
    TARGET_DIR="$HOME/.local/share/maskit"
  elif [ -d "/usr/lib/maskit" ] && [ -w "/usr/lib/maskit" ]; then
    TARGET_DIR="/usr/lib/maskit"
  elif [ -d "$ROOT_DIR/src-tauri/target/release" ]; then
    TARGET_DIR="$ROOT_DIR/src-tauri/target/release"
  else
    TARGET_DIR="$HOME/.local/share/maskit"
    mkdir -p "$TARGET_DIR"
  fi
fi

TARGET_DIR="$(cd "$TARGET_DIR" 2>/dev/null && pwd || echo "$TARGET_DIR")"
BACKUP_DIR="${TARGET_DIR}_backup"
info "目标目录: $TARGET_DIR"

stop_running_app() {
  info "正在停止运行中的 Maskit 进程..."
  pkill -x Maskit 2>/dev/null || true
  pkill -x MaskitEngine 2>/dev/null || true
  pkill -x LLMShield 2>/dev/null || true
  pkill -x LLMShieldEngine 2>/dev/null || true
  pkill -f "mitmdump.*transparent.py" 2>/dev/null || true
  sleep 1
}

# 3. 回滚模式
if [ "$RESTORE_BACKUP" = true ]; then
  if [ ! -d "$BACKUP_DIR" ]; then
    error "未找到备份目录: $BACKUP_DIR"
    exit 1
  fi
  stop_running_app
  info "正在从备份恢复..."
  rm -rf "$TARGET_DIR"
  cp -a "$BACKUP_DIR" "$TARGET_DIR"
  success "回滚完成！"
  if [ "$NO_RESTART" = false ] && [ -x "$TARGET_DIR/Maskit" ]; then
    nohup "$TARGET_DIR/Maskit" >/dev/null 2>&1 &
    success "已重启客户端"
  fi
  exit 0
fi

# 4. 构建环节
if [ "$FULL_UPDATE" = true ]; then
  info "执行全量构建（前端 + 引擎 + 壳）..."
  ./build.sh --bundles deb --no-gates
else
  info "快速构建：打包 Python 引擎 sidecar..."
  rm -rf dist_engine build_engine
  $PYTHON_BIN -m PyInstaller engine/maskit-engine.spec --noconfirm --distpath dist_engine --workpath build_engine
  if [ ! -f "dist_engine/MaskitEngine/MaskitEngine" ]; then
    error "引擎打包失败"
    exit 1
  fi
fi

# 5. 替换到目标目录
stop_running_app

# 备份旧版本
if [ -d "$TARGET_DIR" ]; then
  rm -rf "$BACKUP_DIR"
  cp -a "$TARGET_DIR" "$BACKUP_DIR"
fi

mkdir -p "$TARGET_DIR/resources/engine"

if [ "$FULL_UPDATE" = true ]; then
  if [ -f "src-tauri/target/release/Maskit" ]; then
    cp -f "src-tauri/target/release/Maskit" "$TARGET_DIR/Maskit"
    chmod +x "$TARGET_DIR/Maskit"
  fi
fi

# 拷贝引擎
ENGINE_SRC="dist_engine/MaskitEngine"
rm -rf "$TARGET_DIR/resources/engine/_internal"
cp -a "$ENGINE_SRC/_internal" "$TARGET_DIR/resources/engine/_internal"
cp -f "$ENGINE_SRC/MaskitEngine" "$TARGET_DIR/resources/engine/MaskitEngine"
chmod +x "$TARGET_DIR/resources/engine/MaskitEngine"

success "部署更新完成: $TARGET_DIR"

# 6. 重启应用
if [ "$NO_RESTART" = false ] && [ -x "$TARGET_DIR/Maskit" ]; then
  info "启动客户端..."
  nohup "$TARGET_DIR/Maskit" >/dev/null 2>&1 &
  success "Maskit 客户端已在后台启动"
fi
