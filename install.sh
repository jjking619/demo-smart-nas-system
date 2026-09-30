#!/usr/bin/env bash
set -euo pipefail

# Install entrypoint for NAS-Demo.

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
export PATH

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)"
APP_DIR="$SCRIPT_DIR"
ENV_FILE="$APP_DIR/.env"
ENV_EXAMPLE="$APP_DIR/.env.example"

API_KEY_ARG=""
BASE_URL_ARG=""
MODEL_ID_ARG=""
CHECK_ONLY=0
RESET=0
SKIP_FIREWALL=0
SKIP_VOICE="${SKIP_VOICE_BRIDGE:-0}"
ARG_FIREWALL_ONLY=0
ARG_UI_FIX_ONLY=0
ARG_MIRROR_ONLY=0

FIREWALL_PORTS="${FIREWALL_PORTS:-80 2283 8096 28081 28082 28083 28084 28085 28086 24190 24192}"
INSTALL_REPO="${INSTALL_REPO:-https://github.com/Quectel-Pi/demo-smart-nas-system.git}"
OPENCLAW_DATA_DIR="/DATA/AppData/openclaw"
CASAOS_INSTALL_SCRIPT_URL="${CASAOS_INSTALL_SCRIPT_URL:-https://get.casaos.io}"

usage() {
  cat <<'EOF'
用法:
  bash install.sh
  bash install.sh --api-key=sk-xxx --model-base-url=https://api.deepseek.com/v1 --model-id=deepseek-chat
  bash install.sh --check
  bash install.sh reset            # 重置 OpenClaw 配置为 bootstrap 并重启容器
  bash install.sh firewall         # 仅放行 NAS-Demo 所需端口（幂等 + 持久化）
  bash install.sh ui-fix           # 仅修复 CasaOS Legacy 卡片显示（幂等）
  bash install.sh docker-mirror    # 配置 Docker 镜像加速（解决大镜像拉取失败，幂等）
  bash install.sh --skip-voice     # 安装时跳过语音桥（宿主机 systemd 服务）

说明:
  - 交互模式下只会询问 OpenClaw API 相关配置
  - 非交互可通过参数传入，便于远程或自动化
  - --check 仅做环境检查，不会改动系统
  - --skip-firewall 在正常安装时跳过自动放行端口
  - 默认自动安装语音桥（Voice Assistant 后端，端口 28082），--skip-voice 可跳过
  - 若在“还没有 NAS-Demo 代码”的目录运行，会自动 git clone（可用 --repo 换地址）
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-key=*)
      API_KEY_ARG="${1#*=}"
      if [[ -z "$API_KEY_ARG" && $# -ge 2 && "$2" != -* ]]; then
        API_KEY_ARG="$2"
        shift
      fi
      ;;
    --api-key)
      [[ $# -ge 2 ]] || { echo "参数 --api-key 缺少值" >&2; exit 1; }
      [[ "$2" != -* ]] || { echo "参数 --api-key 缺少值" >&2; exit 1; }
      API_KEY_ARG="$2"
      shift
      ;;
    --model-base-url=*)
      BASE_URL_ARG="${1#*=}"
      if [[ -z "$BASE_URL_ARG" && $# -ge 2 && "$2" != -* ]]; then
        BASE_URL_ARG="$2"
        shift
      fi
      ;;
    --model-base-url)
      [[ $# -ge 2 ]] || { echo "参数 --model-base-url 缺少值" >&2; exit 1; }
      [[ "$2" != -* ]] || { echo "参数 --model-base-url 缺少值" >&2; exit 1; }
      BASE_URL_ARG="$2"
      shift
      ;;
    --model-id=*)
      MODEL_ID_ARG="${1#*=}"
      if [[ -z "$MODEL_ID_ARG" && $# -ge 2 && "$2" != -* ]]; then
        MODEL_ID_ARG="$2"
        shift
      fi
      ;;
    --model-id)
      [[ $# -ge 2 ]] || { echo "参数 --model-id 缺少值" >&2; exit 1; }
      [[ "$2" != -* ]] || { echo "参数 --model-id 缺少值" >&2; exit 1; }
      MODEL_ID_ARG="$2"
      shift
      ;;
    --check) CHECK_ONLY=1 ;;
    --skip-firewall) SKIP_FIREWALL=1 ;;
    --skip-voice) SKIP_VOICE=1 ;;
    --repo=*)
      INSTALL_REPO="${1#*=}"
      if [[ -z "$INSTALL_REPO" && $# -ge 2 && "$2" != -* ]]; then
        INSTALL_REPO="$2"
        shift
      fi
      ;;
    --repo)
      [[ $# -ge 2 ]] || { echo "参数 --repo 缺少值" >&2; exit 1; }
      [[ "$2" != -* ]] || { echo "参数 --repo 缺少值" >&2; exit 1; }
      INSTALL_REPO="$2"
      shift
      ;;
    reset) RESET=1 ;;
    firewall) RESET=0; CHECK_ONLY=0; ARG_FIREWALL_ONLY=1 ;;
    ui-fix) RESET=0; CHECK_ONLY=0; ARG_FIREWALL_ONLY=0; ARG_UI_FIX_ONLY=1 ;;
    docker-mirror) RESET=0; CHECK_ONLY=0; ARG_FIREWALL_ONLY=0; ARG_UI_FIX_ONLY=0; ARG_MIRROR_ONLY=1 ;;
    -h|--help|help) usage; exit 0 ;;
    *)
      echo "未知参数: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

log() { echo "[install] $*"; }
warn() { echo "[install][WARN] $*" >&2; }
die() { echo "[install][ERROR] $*" >&2; exit 1; }

docker_cmd() {
  if [[ "${EUID}" -eq 0 ]]; then
    docker "$@"
  else
    sudo docker "$@"
  fi
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

ensure_wget_for_casaos() {
  if command -v wget >/dev/null 2>&1 && wget --help 2>&1 | grep -q -- '--show-progress'; then
    return 0
  fi

  warn "检测到 wget 不支持 --show-progress（常见于 BusyBox），尝试安装 GNU wget..."
  if [[ "${EUID}" -eq 0 ]]; then
    apt install wget -y || apt-get install wget -y || die "安装 GNU wget 失败，请手动执行: sudo apt install wget -y"
  else
    sudo apt install wget -y || sudo apt-get install wget -y || die "安装 GNU wget 失败，请手动执行: sudo apt install wget -y"
  fi

  command -v wget >/dev/null 2>&1 || die "安装后仍未找到 wget，请检查 PATH"
  wget --help 2>&1 | grep -q -- '--show-progress' || die "当前 wget 仍不支持 --show-progress，请确认 /usr/bin/wget 为 GNU 版本"
}

install_casaos() {
  log "开始安装 CasaOS（该过程会提示输入 sudo 密码）..."

  # CasaOS 安装脚本内部会调用 wget --show-progress；BusyBox wget 会失败。
  ensure_wget_for_casaos

  if [[ "${EUID}" -eq 0 ]]; then
    if curl -fsSL "$CASAOS_INSTALL_SCRIPT_URL" | bash; then
      return 0
    fi
    warn "curl 安装 CasaOS 失败，尝试安装 wget 后重试..."
    wget -qO- "$CASAOS_INSTALL_SCRIPT_URL" | bash
  else
    if curl -fsSL "$CASAOS_INSTALL_SCRIPT_URL" | sudo bash; then
      return 0
    fi
    warn "curl 安装 CasaOS 失败，尝试安装 wget 后重试..."
    wget -qO- "$CASAOS_INSTALL_SCRIPT_URL" | sudo bash
  fi
}

ensure_docker_via_casaos() {
  if command -v docker >/dev/null 2>&1; then
    return 0
  fi

  warn "未检测到 docker，推荐按 CasaOS 路线安装（会自动安装 docker）"
  if [[ "$CHECK_ONLY" -eq 1 ]]; then
    die "缺少命令: docker。请先执行: curl -fsSL ${CASAOS_INSTALL_SCRIPT_URL} | sudo bash"
  fi

  if [[ ! -t 0 ]]; then
    die "缺少命令: docker。当前是非交互环境，请先执行: curl -fsSL ${CASAOS_INSTALL_SCRIPT_URL} | sudo bash"
  fi

  local ans
  read -r -p "是否现在安装 CasaOS（推荐）？[Y/n]: " ans
  if [[ -n "$ans" && ! "$ans" =~ ^[Yy]$ ]]; then
    die "已取消。你可手动执行: curl -fsSL ${CASAOS_INSTALL_SCRIPT_URL} | sudo bash"
  fi

  install_casaos

  command -v docker >/dev/null 2>&1 || die "CasaOS 安装后仍未检测到 docker，请重新登录后重试"
}

detect_casaos_scheme() {
  if curl -k -I --max-time 3 https://127.0.0.1:443 >/dev/null 2>&1; then
    echo "https"
  elif curl -I --max-time 3 http://127.0.0.1:80 >/dev/null 2>&1; then
    echo "http"
  else
    echo ""
  fi
}

ensure_casaos_web_if_missing() {
  if [[ -n "$(detect_casaos_scheme)" ]]; then
    return 0
  fi

  warn "未检测到 CasaOS 管理入口（127.0.0.1:80/443）"

  if [[ "$CHECK_ONLY" -eq 1 ]]; then
    warn "可执行安装命令: curl -fsSL ${CASAOS_INSTALL_SCRIPT_URL} | sudo bash"
    return 0
  fi

  if [[ ! -t 0 ]]; then
    warn "当前是非交互环境，已跳过自动安装 CasaOS；可手动执行: curl -fsSL ${CASAOS_INSTALL_SCRIPT_URL} | sudo bash"
    return 0
  fi

  local ans
  read -r -p "是否现在安装 CasaOS 管理服务？[Y/n]: " ans
  if [[ -n "$ans" && ! "$ans" =~ ^[Yy]$ ]]; then
    warn "已跳过 CasaOS 安装；后续可手动执行: curl -fsSL ${CASAOS_INSTALL_SCRIPT_URL} | sudo bash"
    return 0
  fi

  install_casaos

  if [[ -z "$(detect_casaos_scheme)" ]]; then
    warn "CasaOS 安装后仍未检测到 80/443 监听，可稍后检查: systemctl status casaos"
  fi
}

container_exists() {
  docker_cmd ps -a --format '{{.Names}}' | grep -qx "$1"
}

casaos_app_exists_any() {
  local id
  for id in "$@"; do
    if casaos-cli app-management show local "$id" --yaml >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

cleanup_legacy_nas_demo_app() {
  if ! casaos-cli app-management show local nas-demo --yaml >/dev/null 2>&1; then
    return 0
  fi

  if ! casaos-cli app-management show local nas-demo --yaml 2>/dev/null | grep -q '^x-casaos:'; then
    warn "检测到 legacy 应用 nas-demo，尝试清理以避免 CasaOS 首页出现不可点击条目..."
    casaos-cli app-management uninstall nas-demo --no-remove-config || warn "清理 legacy nas-demo 失败，可稍后手动处理"
  fi
}

restart_casaos_app_services() {
  # CasaOS 应用安装卡在 "Installing" 时，重启相关服务可清理僵死任务状态。
  if [[ "${EUID}" -eq 0 ]]; then
    systemctl restart casaos-message-bus casaos-app-management casaos-gateway >/dev/null 2>&1 || true
  else
    sudo systemctl restart casaos-message-bus casaos-app-management casaos-gateway >/dev/null 2>&1 || true
  fi
}

run_oc_cmd_with_casaos_retry() {
  local subcmd="$1"
  local label="$2"
  local max_retry="${3:-5}"
  local wait_sec="${4:-10}"

  local i out rc
  for i in $(seq 1 "$max_retry"); do
    set +e
    out="$(bash "$APP_DIR/oc.sh" "$subcmd" 2>&1)"
    rc=$?
    set -e

    if [[ "$rc" -eq 0 ]]; then
      return 0
    fi

    if printf '%s' "$out" | grep -qiE '409 Conflict|already being installed|is already being installed'; then
      warn "${label} 遇到 CasaOS 安装冲突（409），第 ${i}/${max_retry} 次重试前先恢复 CasaOS 服务并等待 ${wait_sec}s"
      restart_casaos_app_services
      sleep "$wait_sec"
      continue
    fi

    warn "${label} 失败（exit=${rc}）：$(printf '%s' "$out" | tail -n 2 | tr '\n' ' ')"
    return "$rc"
  done

  warn "${label} 多次重试后仍失败（可能仍卡在 CasaOS Installing）"
  return 1
}

pull_image_with_retry() {
  local image="$1"
  local attempts="${2:-4}"
  local wait_sec="${3:-8}"
  local i

  for i in $(seq 1 "$attempts"); do
    if docker_cmd pull "$image" >/dev/null 2>&1; then
      log "镜像预拉取成功: $image"
      return 0
    fi
    warn "镜像预拉取失败: $image（第 ${i}/${attempts} 次，${wait_sec}s 后重试）"
    sleep "$wait_sec"
  done

  warn "镜像预拉取多次失败: $image（后续将继续尝试安装，若失败请重试 install.sh）"
  return 1
}

pre_pull_casaos_images() {
  # CasaOS 应用安装是异步流程，网络抖动（connection reset by peer）时不易即时感知。
  # 先在宿主机预拉关键镜像并重试，可显著降低 install 后“只剩一个应用成功”的概率。
  local images=(
    # openclaw portal 与 voice assistant 统一使用 slim，减少网络抖动下镜像拉取失败概率。
    "python:3.11-slim"
    "jellyfin/jellyfin:12.0@sha256:baba630419915985442f315f08b0cf46d9f4c8a0cc4bd38e94a6d35751dd5ef5"
    "ghcr.io/immich-app/postgres:14-vectorchord0.3.0-pgvectors0.2.0@sha256:c570d9e1c2494f65d2a0a379a7f6df66e8441964254a30aa62cc58e8ebf1dee0"
    "ghcr.io/immich-app/immich-server:v3.1.0@sha256:b434cb9287eea1471c9974845914d4dd328c9c2d652e446ed4930f99944f0ceb"
    "ghcr.io/immich-app/immich-machine-learning:v3.1.0@sha256:5a0839dc5303cd7215bcd2180a26aed3af41675aefb3e75e5157e9f10ad16e6e"
    "redis:6.2.20-alpine@sha256:2185e741f4c1e7b0ea9ca1e163a3767c4270a73086b6bbea2049a7203212fb7f"
  )
  local image
  for image in "${images[@]}"; do
    pull_image_with_retry "$image" 4 8 || true
  done
}

render_compose_template_file() {
  local src="$1"
  local dst app_dir_escaped nas_root_escaped nas_puid nas_pgid nas_root
  dst="$(mktemp "/tmp/$(basename "$src").XXXXXX")"

  nas_root="$(env_get NAS_ROOT)"
  nas_root="${nas_root:-$HOME/nas_share}"
  nas_puid="$(env_get NAS_PUID)"
  nas_puid="${nas_puid:-$(id -u)}"
  nas_pgid="$(env_get NAS_PGID)"
  nas_pgid="${nas_pgid:-$(id -g)}"

  app_dir_escaped="$(printf '%s' "$APP_DIR" | sed 's/[\/&]/\\&/g')"
  nas_root_escaped="$(printf '%s' "$nas_root" | sed 's/[\/&]/\\&/g')"

  sed \
    -e "s#__APP_DIR__#${app_dir_escaped}#g" \
    -e "s#__NAS_ROOT__#${nas_root_escaped}#g" \
    -e "s#__NAS_PUID__#${nas_puid}#g" \
    -e "s#__NAS_PGID__#${nas_pgid}#g" \
    "$src" > "$dst"

  printf '%s\n' "$dst"
}

fallback_compose_deploy() {
  local compose_src="$1"
  local label="$2"
  local rendered=""

  if [[ ! -f "$compose_src" ]]; then
    warn "${label} 回退 compose 失败：文件不存在 $compose_src"
    return 1
  fi

  rendered="$(render_compose_template_file "$compose_src")"
  if docker_cmd compose -f "$rendered" up -d >/dev/null 2>&1; then
    log "${label} 已通过 compose 回退部署成功"
    rm -f "$rendered"
    return 0
  fi

  warn "${label} compose 回退部署失败"
  rm -f "$rendered"
  return 1
}

run_oc_cmd_with_retry_and_fallback() {
  local subcmd="$1"
  local label="$2"
  local fallback_compose="${3:-}"
  local max_retry="${4:-5}"
  local wait_sec="${5:-10}"

  if run_oc_cmd_with_casaos_retry "$subcmd" "$label" "$max_retry" "$wait_sec"; then
    return 0
  fi

  if [[ -n "$fallback_compose" ]]; then
    warn "${label} 进入 compose 回退部署路径"
    fallback_compose_deploy "$fallback_compose" "$label" && return 0
  fi

  return 1
}

env_get() {
  local key="$1"
  grep -E "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' || true
}

env_set() {
  local key="$1"
  local value="$2"
  if grep -qE "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null; then
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' "$ENV_FILE" > "$ENV_FILE.tmp"
    mv "$ENV_FILE.tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

mask_key() {
  local s="$1"
  if [[ -z "$s" ]]; then
    echo "(空)"
  else
    echo "${s:0:6}..."
  fi
}

# =============================================================================
# 资源前置检查
#
# 背景：NAS-Demo 全家桶常驻内存约 3GB（实测 immich-server 首次启动导入 geodata
# 时峰值近 1GB，openclaw 约 650MB，immich-machine-learning 约 430MB），
# 首次安装镜像约 10GB。内存/磁盘不足时若不在开始时提示，往往装到一半才 OOM
# 或写满，排查成本高。
#
# 环境变量 NAS_SKIP_RESOURCE_CHECK=1 可跳过本检查（自动化/受控环境用）。
# =============================================================================
_meminfo_kb() {
  local key="$1" default="${2:-0}" v
  v="$(awk -v k="$key" '$1==k{print $2; exit}' /proc/meminfo 2>/dev/null)"
  [[ "$v" =~ ^[0-9]+$ ]] || v="$default"
  echo "$v"
}

check_resources() {
  if [[ "${NAS_SKIP_RESOURCE_CHECK:-0}" == "1" ]]; then
    warn "已跳过资源检查（NAS_SKIP_RESOURCE_CHECK=1）"
    return 0
  fi

  local mem_total_mb mem_avail_mb swap_total_mb swap_free_mb
  local disk_avail_gb docker_root
  mem_total_mb=$(( $(_meminfo_kb MemTotal:) / 1024 ))
  mem_avail_mb=$(( $(_meminfo_kb MemAvailable: "$(_meminfo_kb MemFree:)") / 1024 ))
  swap_total_mb=$(( $(_meminfo_kb SwapTotal:) / 1024 ))
  swap_free_mb=$(( $(_meminfo_kb SwapFree:) / 1024 ))

  docker_root="$(docker_cmd info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
  disk_avail_gb="$(df -Pk "$docker_root" 2>/dev/null | awk 'NR==2{printf "%d", $4/1024/1024}')"
  [[ "$disk_avail_gb" =~ ^[0-9]+$ ]] || disk_avail_gb=0

  log "资源检查：总内存 ${mem_total_mb}MB / 可用 ${mem_avail_mb}MB / Swap 可用 ${swap_free_mb}MB；Docker 所在盘剩余 ${disk_avail_gb}GB"

  # ── 磁盘：镜像约 10GB，加数据/日志留余量 ──
  if [[ "$disk_avail_gb" -lt 8 ]]; then
    die "磁盘空间不足：Docker 目录（$docker_root）仅剩 ${disk_avail_gb}GB，至少需要 8GB。可先执行: docker system prune -af"
  elif [[ "$disk_avail_gb" -lt 15 ]]; then
    warn "磁盘偏紧：仅剩 ${disk_avail_gb}GB（推荐 ≥15GB）。可执行 docker system prune -af 回收未使用镜像后重试"
  fi

  # ── 内存：全家桶峰值约 2.5~3GB ──
  if [[ "$mem_total_mb" -lt 4096 ]]; then
    warn "总内存仅 ${mem_total_mb}MB（推荐 ≥4GB）。Immich 机器学习可能被 OOM Killer 终止，"
    warn "  表现：照片分类卡死 / 语义搜索无结果。若出现可执行: sudo docker start immich-machine-learning"
  fi

  if [[ "$mem_avail_mb" -lt 1200 ]]; then
    if [[ "$swap_free_mb" -ge 2048 ]]; then
      warn "可用内存仅 ${mem_avail_mb}MB，将依赖 Swap（可用 ${swap_free_mb}MB），首次安装会明显变慢"
    else
      die "可用内存不足：仅 ${mem_avail_mb}MB，且 Swap 可用不足 2GB。请先释放内存或扩容 Swap 后重试；确需继续可执行: NAS_SKIP_RESOURCE_CHECK=1 bash install.sh"
    fi
  elif [[ "$mem_avail_mb" -lt 2000 ]]; then
    warn "可用内存偏低（${mem_avail_mb}MB），安装期间请避免其它重负载任务"
  fi

  # ── 交换空间：内存受限设备的兜底，缺失时明确提示 ──
  if [[ "$swap_total_mb" -eq 0 && "$mem_total_mb" -lt 8192 ]]; then
    warn "未启用 Swap 且总内存 <8GB，建议配置 2~4GB Swap 以降低 OOM 风险："
    warn "  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile"
  fi

  log "资源检查通过"
}

# =============================================================================
# Docker 镜像加速检查
#
# 背景：部分网络环境（企业网络/弱网/国内直连 Docker Hub）在从 CloudFront 拉取
# 大镜像 blob 阶段会被重置（read: connection reset by peer），表现为
# 「小镜像能装、大镜像反复失败」，进而导致 CasaOS 异步安装回滚，
# 最终只留下体积最小的应用（例如 NAS Files）。
#
# 本检查只做提示，不改动系统；确需配置时执行:
#   sudo bash setup_docker_mirror.sh    （或 bash install.sh docker-mirror）
# =============================================================================
check_docker_registry_mirror() {
  if docker_cmd info 2>/dev/null \
      | awk '/Registry Mirrors:/{flag=1; next} /^[[:space:]]*$/{flag=0} flag' \
      | grep -qE 'https?://'; then
    log "Docker 镜像加速已配置"
    return 0
  fi

  warn "未检测到 Docker 镜像加速（registry-mirrors）"
  warn "  直连 Docker Hub 拉取大镜像可能被重置，导致部分应用安装失败后回滚"
  warn "  建议执行: bash $APP_DIR/setup_docker_mirror.sh"
}

check_env() {
  log "环境检查中..."
  require_cmd bash
  require_cmd curl
  require_cmd openssl
  require_cmd python3
  ensure_docker_via_casaos
  check_resources
  check_docker_registry_mirror
  ensure_casaos_web_if_missing
  if ! docker_cmd ps >/dev/null 2>&1; then
    die "docker 无法访问，请确认当前用户有 sudo docker 权限"
  fi
  if [[ -f "$APP_DIR/oc.sh" ]]; then
    chmod +x "$APP_DIR/oc.sh" || true
  fi
  log "环境检查通过"
}

ensure_openclaw_data_permissions() {
  if [[ "${EUID}" -eq 0 ]]; then
    mkdir -p "$OPENCLAW_DATA_DIR"
    chown -R 1000:1000 "$OPENCLAW_DATA_DIR"
    chmod 775 "$OPENCLAW_DATA_DIR" || true
  else
    sudo mkdir -p "$OPENCLAW_DATA_DIR"
    sudo chown -R 1000:1000 "$OPENCLAW_DATA_DIR"
    sudo chmod 775 "$OPENCLAW_DATA_DIR" || true
  fi
}

ensure_runtime_paths_permissions() {
  local nas_root nas_uid nas_gid
  nas_root="$(env_get NAS_ROOT)"
  nas_uid="$(env_get NAS_PUID)"
  nas_gid="$(env_get NAS_PGID)"
  nas_root="${nas_root:-$HOME/nas_share}"
  nas_uid="${nas_uid:-$(id -u)}"
  nas_gid="${nas_gid:-$(id -g)}"

  if [[ "${EUID}" -eq 0 ]]; then
    mkdir -p "$nas_root" "$nas_root/downloads/Movies" "$nas_root/downloads/TV Shows" "$nas_root/tools" "$nas_root/knowledge_base_data"
    mkdir -p /DATA/AppData/filebrowser/config /DATA/AppData/filebrowser/database
    chown "$nas_uid:$nas_gid" "$nas_root" "$nas_root/downloads" "$nas_root/downloads/Movies" "$nas_root/downloads/TV Shows" "$nas_root/tools" "$nas_root/knowledge_base_data" || true
    chown -R "$nas_uid:$nas_gid" /DATA/AppData/filebrowser || true
    chmod 775 "$nas_root" "$nas_root/downloads" "$nas_root/downloads/Movies" "$nas_root/downloads/TV Shows" "$nas_root/tools" "$nas_root/knowledge_base_data" || true
    chmod 775 /DATA/AppData/filebrowser /DATA/AppData/filebrowser/config /DATA/AppData/filebrowser/database || true
  else
    sudo mkdir -p "$nas_root" "$nas_root/downloads/Movies" "$nas_root/downloads/TV Shows" "$nas_root/tools" "$nas_root/knowledge_base_data"
    sudo mkdir -p /DATA/AppData/filebrowser/config /DATA/AppData/filebrowser/database
    sudo chown "$nas_uid:$nas_gid" "$nas_root" "$nas_root/downloads" "$nas_root/downloads/Movies" "$nas_root/downloads/TV Shows" "$nas_root/tools" "$nas_root/knowledge_base_data" || true
    sudo chown -R "$nas_uid:$nas_gid" /DATA/AppData/filebrowser || true
    sudo chmod 775 "$nas_root" "$nas_root/downloads" "$nas_root/downloads/Movies" "$nas_root/downloads/TV Shows" "$nas_root/tools" "$nas_root/knowledge_base_data" || true
    sudo chmod 775 /DATA/AppData/filebrowser /DATA/AppData/filebrowser/config /DATA/AppData/filebrowser/database || true
  fi
}

detect_openclaw_scheme() {
  if curl -k -I --max-time 3 https://127.0.0.1:24190/healthz >/dev/null 2>&1; then
    echo "https"
  else
    echo "http"
  fi
}

get_primary_ip() {
  local ip
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="src"){print $(i+1); exit}}}')"
  if [[ -z "$ip" ]]; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  echo "$ip"
}

prepare_env() {
  [[ -f "$ENV_EXAMPLE" ]] || die "缺少 $ENV_EXAMPLE"
  if [[ ! -f "$ENV_FILE" ]]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    log "已创建 .env"
  fi

  local base_url api_key model_id token
  base_url="$(env_get OPENCLAW_MODEL_BASE_URL)"
  api_key="$(env_get OPENCLAW_MODEL_API_KEY)"
  model_id="$(env_get OPENCLAW_MODEL_ID)"
  token="$(env_get OPENCLAW_GATEWAY_TOKEN)"

  [[ -n "$BASE_URL_ARG" ]] && base_url="$BASE_URL_ARG"
  [[ -n "$API_KEY_ARG" ]] && api_key="$API_KEY_ARG"
  [[ -n "$MODEL_ID_ARG" ]] && model_id="$MODEL_ID_ARG"

  if [[ -z "$base_url" ]]; then
    if [[ -t 0 ]]; then
      read -r -p "请输入 OpenClaw 模型 Base URL [默认 https://api.deepseek.com/v1]: " base_url
      base_url="${base_url:-https://api.deepseek.com/v1}"
    else
      die "未提供 OPENCLAW_MODEL_BASE_URL，请使用 --model-base-url=..."
    fi
  fi

  if [[ -z "$api_key" ]]; then
    if [[ -t 0 ]]; then
      read -r -s -p "请输入 OpenClaw 模型 API Key: " api_key
      echo
    else
      die "未提供 OPENCLAW_MODEL_API_KEY，请使用 --api-key=..."
    fi
  fi

  [[ -n "$api_key" ]] || die "API Key 不能为空"
  model_id="${model_id:-deepseek-chat}"
  token="${token:-$(openssl rand -hex 12)}"

  env_set OPENCLAW_MODEL_BASE_URL "$base_url"
  env_set OPENCLAW_MODEL_API_KEY "$api_key"
  env_set OPENCLAW_MODEL_ID "$model_id"
  env_set OPENCLAW_GATEWAY_TOKEN "$token"

  # 用户名无关路径：首次运行自动写入当前用户实际值。
  # 兼容历史模板中的 /home/pi 默认值：非 pi 用户首次运行时自动迁移。
  local current_nas_root current_voice_sdk_base
  current_nas_root="$(env_get NAS_ROOT)"
  if [[ -z "$current_nas_root" || ( "$current_nas_root" == "/home/pi/nas_share" && "$HOME" != "/home/pi" ) ]]; then
    env_set NAS_ROOT "$HOME/nas_share"
  fi

  # VOICE_SDK_BASE 也按当前用户 home 自动写入（兼容历史 /home/pi 默认）。
  current_voice_sdk_base="$(env_get VOICE_SDK_BASE)"
  if [[ -z "$current_voice_sdk_base" || ( "$current_voice_sdk_base" == "/home/pi/voice" && "$HOME" != "/home/pi" ) ]]; then
    env_set VOICE_SDK_BASE "$HOME/voice"
  fi

  env_set APP_DIR "$APP_DIR"
  env_set NAS_PUID "$(id -u)"
  env_set NAS_PGID "$(id -g)"

  log "已写入 .env:"
  log "  OPENCLAW_MODEL_BASE_URL=$base_url"
  log "  OPENCLAW_MODEL_API_KEY=$(mask_key "$api_key")"
  log "  OPENCLAW_MODEL_ID=$model_id"
  log "  NAS_ROOT=$(env_get NAS_ROOT)"
  log "  APP_DIR=$APP_DIR"
  log "  NAS_PUID/NAS_PGID=$(env_get NAS_PUID)/$(env_get NAS_PGID)"
}

wait_openclaw() {
  log "等待 OpenClaw 就绪..."
  for i in $(seq 1 120); do
    if docker_cmd exec openclaw node dist/index.js status >/dev/null 2>&1; then
      log "OpenClaw 已就绪"
      return 0
    fi
    sleep 2
  done
  die "OpenClaw 长时间未就绪，请执行: ./oc.sh logs"
}

start_pairing_auto_approve_window() {
  local token
  token="$(env_get OPENCLAW_GATEWAY_TOKEN)"
  token="${token:-casaos}"

  # 安装后开启短时自动批准窗口，覆盖首次打开 Control UI 产生的 pending 请求。
  (
    for _ in $(seq 1 300); do
      if docker_cmd exec openclaw node dist/index.js devices approve --latest --json \
        --url ws://127.0.0.1:18789 --token "$token" >/dev/null 2>&1; then
        exit 0
      fi
      sleep 2
    done
    exit 0
  ) >/dev/null 2>&1 &

  log "已开启 10 分钟自动配对批准窗口（首次打开 Control UI 将自动通过）"
}

sync_immich_key_to_env_if_exists() {
  local key_line
  key_line="$(grep -E '^IMMICH_API_KEY=' /DATA/AppData/openclaw/.env 2>/dev/null | tail -1 || true)"
  if [[ -n "$key_line" ]]; then
    env_set IMMICH_API_KEY "${key_line#IMMICH_API_KEY=}"
  fi
}

# =============================================================================
# 语音桥（宿主机 systemd 服务，端口 28082）
#
# Voice Assistant 网页（28083）只是前端，对话由语音桥处理后端转发，因此语音桥
# 属于「必装」组件；这里在部署流程末尾自动完成，避免用户漏装导致 28083 报 502。
#
# 注：
#   - 需要 root 写 /etc/systemd/system/voice-bridge.service 并启用服务；非交互
#     环境且无免密 sudo 时安全跳过，不阻塞自动化安装。
#   - 默认使用 SenseVoice 作为 wake/command 共用引擎
#   - Conformer 仅作为手动高级回退，不在默认安装流程中作为推荐路径
#   - 如需手动启用，可在 .env 中设置 VOICE_WAKE_ASR_ENGINE=conformer
#   - 首次安装会预下载 wake 模型 / command 模型 / TTS 模型（数百 MB），视网络需数分钟。
#   - 幂等：已在运行则跳过；需要重装时手动执行安装脚本。
# =============================================================================
setup_voice_bridge() {
  local installer="$APP_DIR/local_voice_chat/install_voice_bridge_service.sh"
  local run_user

  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    run_user="$SUDO_USER"
  else
    run_user="$(id -un)"
  fi

  if [[ "${SKIP_VOICE:-0}" -eq 1 ]]; then
    log "已按 --skip-voice 跳过语音桥安装"
    log "  需要时手动执行: cd $APP_DIR/local_voice_chat && ./install_voice_bridge_service.sh"
    return 0
  fi

  if [[ ! -f "$installer" ]]; then
    warn "未找到语音桥安装脚本: $installer，跳过"
    return 0
  fi

  if ! command -v systemctl >/dev/null 2>&1; then
    warn "未检测到 systemd，跳过语音桥安装（可前台调试: python3 local_voice_chat/voice_bridge.py）"
    return 0
  fi

  if systemctl is-active --quiet voice-bridge 2>/dev/null; then
    if id -nG "$run_user" | tr ' ' '\n' | grep -qx docker; then
      log "语音桥已在运行，跳过安装（重装: cd $APP_DIR/local_voice_chat && ./install_voice_bridge_service.sh）"
      return 0
    fi
    warn "语音桥已在运行，但用户 $run_user 不在 docker 组，继续执行修复安装"
  fi

  # 离线 ASR/TTS 依赖：numpy + sherpa_onnx（用户级 pip）
  if ! python3 -c 'import numpy, sherpa_onnx' >/dev/null 2>&1; then
    log "安装语音桥依赖（numpy / sherpa-onnx，首次较慢）..."
    if ! python3 -m pip install --user numpy sherpa-onnx >/dev/null 2>&1; then
      # Debian 12+ / Ubuntu 24+ 标记为 externally-managed，需显式放行
      python3 -m pip install --user --break-system-packages numpy sherpa-onnx >/dev/null 2>&1 || true
    fi
  fi
  if ! python3 -c 'import numpy, sherpa_onnx' >/dev/null 2>&1; then
    warn "仍缺少 numpy/sherpa_onnx，跳过语音桥安装"
    warn "  手动安装依赖: python3 -m pip install --user sherpa-onnx numpy"
    return 0
  fi

  # 安装 systemd 服务需要 root：非交互环境不弹密码提示，直接给出手动命令
  if [[ "${EUID}" -ne 0 ]] && ! sudo -n true >/dev/null 2>&1; then
    if [[ ! -t 0 ]]; then
      warn "非交互环境且无免密 sudo，跳过语音桥安装"
      warn "  手动安装: cd $APP_DIR/local_voice_chat && ./install_voice_bridge_service.sh"
      return 0
    fi
    log "安装语音桥需要 sudo 权限，稍后会提示输入密码"
  fi

  log "安装并启动语音桥（首次会预下载 wake 模型 / command 模型 / TTS 模型，视网络需数分钟）..."
  if bash "$installer"; then
    log "语音桥已就绪（端口 28082）；已准备 wake 模型 / command 模型 / TTS 模型"
  else
    warn "语音桥安装失败，可稍后手动执行:"
    warn "  cd $APP_DIR/local_voice_chat && ./install_voice_bridge_service.sh"
  fi
}

deploy_all() {
  log "开始执行一键安装流程..."
  local has_casaos_cli=0

  if command -v casaos-cli >/dev/null 2>&1; then
    has_casaos_cli=1
  fi

  ensure_openclaw_data_permissions
  ensure_runtime_paths_permissions

  # 1) 优先走 compose，全流程仍由 install.sh 统一调度
  if docker_cmd compose version >/dev/null 2>&1; then
    if [[ "$has_casaos_cli" -eq 1 ]]; then
      log "检测到 casaos-cli，采用 CasaOS 应用模式部署（OpenClaw/Immich/Jellyfin）"
      cleanup_legacy_nas_demo_app || true
      pre_pull_casaos_images || true
      FORCE_PLAIN_DOCKER=1 bash "$APP_DIR/deploy.sh" || warn "openclaw 部署失败，可稍后重试"
      run_oc_cmd_with_retry_and_fallback openclaw-app-deploy "openclaw-app-deploy" "$APP_DIR/openclaw-compose.yml" || warn "openclaw-app-deploy 失败"
      run_oc_cmd_with_retry_and_fallback immich-apply "immich-apply" "$APP_DIR/immich-compose.yml" || warn "immich-apply 失败"
      run_oc_cmd_with_retry_and_fallback jellyfin-deploy "jellyfin-deploy" "$APP_DIR/jellyfin-compose.yml" || warn "jellyfin-deploy 失败"

      if ! casaos_app_exists_any openclaw-app org.local.openclaw.portal openclaw openclaw-portal; then
        warn "OpenClaw 入口未注册成功，重试一次..."
        run_oc_cmd_with_retry_and_fallback openclaw-app-deploy "openclaw-app-deploy" "$APP_DIR/openclaw-compose.yml" 3 12 || warn "openclaw-app-deploy 重试失败"
      fi
      if ! casaos_app_exists_any big-bear-immich com.bigbeartechworld.immich; then
        warn "Immich 应用未注册成功，重试一次..."
        run_oc_cmd_with_retry_and_fallback immich-apply "immich-apply" "$APP_DIR/immich-compose.yml" 3 12 || warn "immich-apply 重试失败"
      fi
      if ! casaos_app_exists_any jellyfin org.jellyfin.server; then
        warn "Jellyfin 应用未注册成功，重试一次..."
        run_oc_cmd_with_retry_and_fallback jellyfin-deploy "jellyfin-deploy" "$APP_DIR/jellyfin-compose.yml" 3 12 || warn "jellyfin-deploy 重试失败"
      fi
    else
      if container_exists openclaw; then
        warn "检测到已存在 openclaw，跳过重建 openclaw，尝试拉起其余核心服务"
        docker_cmd compose up -d --build \
          media_downloader knowledge_base immich-postgres immich-redis \
          immich-server immich-machine-learning jellyfin || warn "compose 拉起其余服务失败，可稍后重试"
      else
        log "检测到 docker compose，执行全家桶部署"
        docker_cmd compose up -d --build
      fi
    fi
  else
    # 2) compose 不可用时，走精简兜底路径（仅依赖现有 oc.sh 和初始化脚本）
    warn "未检测到 docker compose，切换到精简兜底安装路径"
    bash "$APP_DIR/oc.sh" deploy
  fi

  wait_openclaw

  # 直接配置模型（避免不同部署模式下环境占位差异）
  local base_url api_key model_id provider_json
  base_url="$(env_get OPENCLAW_MODEL_BASE_URL)"
  api_key="$(env_get OPENCLAW_MODEL_API_KEY)"
  model_id="$(env_get OPENCLAW_MODEL_ID)"

  provider_json=$(cat <<EOF
{
  "baseUrl": "${base_url}",
  "apiKey": "${api_key}",
  "api": "openai-completions",
  "models": [
    {
      "id": "${model_id}",
      "name": "${model_id}",
      "input": ["text", "image"],
      "contextWindow": 64000,
      "maxTokens": 8192
    }
  ]
}
EOF
)

  docker_cmd exec openclaw node dist/index.js config set models.providers.custom "$provider_json" --strict-json
  docker_cmd exec openclaw node dist/index.js config set agents.defaults.model.primary "custom/${model_id}"
  docker_cmd exec openclaw node dist/index.js config set gateway.controlUi.allowedOrigins '["*"]' --strict-json || true

  # MCP + 相关能力
  bash "$APP_DIR/oc.sh" tools-nas-setup || warn "tools-nas-setup 失败"
  bash "$APP_DIR/oc.sh" tools-media-setup || warn "tools-media-setup 失败"
  bash "$APP_DIR/oc.sh" tools-kb-setup || warn "tools-kb-setup 失败"
  bash "$APP_DIR/oc.sh" tools-photos-setup || warn "tools-photos-setup 失败"

  # Immich MCP：key 已存在（.env 或 /DATA/AppData/openclaw/.env）则由 oc.sh 注册
  sync_immich_key_to_env_if_exists
  if [[ -n "$(env_get IMMICH_API_KEY)" ]]; then
    bash "$APP_DIR/oc.sh" tools-immich-setup || warn "tools-immich-setup 失败，可稍后手动运行 ./oc.sh tools-immich-setup"
  else
    warn "未检测到 IMMICH_API_KEY，跳过 immich MCP。可稍后在 Immich 生成 key 后运行 ./oc.sh tools-immich-setup"
  fi

  # CasaOS 附加组件（失败不阻断主流程）
  if [[ "$has_casaos_cli" -eq 1 ]]; then
    run_oc_cmd_with_retry_and_fallback nas-files-deploy "nas-files-deploy" "$APP_DIR/filebrowser-compose.yml" || warn "nas-files-deploy 失败"
    run_oc_cmd_with_retry_and_fallback voice-assistant-deploy "voice-assistant-deploy" "$APP_DIR/voice-assistant-compose.yml" || warn "voice-assistant-deploy 失败"
  else
    if ! container_exists jellyfin; then
      run_oc_cmd_with_retry_and_fallback jellyfin-deploy "jellyfin-deploy" "$APP_DIR/jellyfin-compose.yml" || warn "jellyfin-deploy 失败"
    fi
    if ! container_exists filebrowser; then
      run_oc_cmd_with_retry_and_fallback nas-files-deploy "nas-files-deploy" "$APP_DIR/filebrowser-compose.yml" || warn "nas-files-deploy 失败"
    fi
    if ! container_exists voice_assistant; then
      run_oc_cmd_with_retry_and_fallback voice-assistant-deploy "voice-assistant-deploy" "$APP_DIR/voice-assistant-compose.yml" || warn "voice-assistant-deploy 失败"
    fi
  fi

  # 自动安装语音桥（Voice Assistant 的后端依赖），失败不阻断主流程
  setup_voice_bridge

  docker_cmd restart openclaw >/dev/null 2>&1 || true
}

summary() {
  local ip token scheme casaos_scheme casaos_url openclaw_url
  ip="$(get_primary_ip)"
  token="$(env_get OPENCLAW_GATEWAY_TOKEN)"
  scheme="$(detect_openclaw_scheme)"
  casaos_scheme="$(detect_casaos_scheme)"
  casaos_url=""
  if [[ -n "$casaos_scheme" ]]; then
    casaos_url="${casaos_scheme}://${ip:-<IP>}"
  fi
  openclaw_url="${scheme}://${ip:-<IP>}:24190/#token=${token:-casaos}"
  echo
  echo "============================================================"
  echo "安装完成"
  echo
  echo "下一步（推荐顺序）："
  if [[ -n "$casaos_url" ]]; then
    echo "1) 先打开 CasaOS 管理入口"
    echo "   ${casaos_url}"
    echo "2) 在 CasaOS 中确认/配置其它应用（Immich / Jellyfin / NAS Files / Voice Assistant）"
    echo "3) 再打开 OpenClaw 控制台"
  else
    echo "1) 先安装/修复 CasaOS 管理服务"
    echo "   curl -fsSL ${CASAOS_INSTALL_SCRIPT_URL} | sudo bash"
    echo "2) 安装完成后执行: bash ./oc.sh casaos-url"
    echo "3) 再打开 OpenClaw 控制台"
  fi
  echo "   ${openclaw_url}"
  echo
  if [[ -z "$casaos_url" ]]; then
    echo "提示: 当前未检测到 CasaOS Web 服务（80/443），上面的 CasaOS URL 可能无法访问。"
    echo
  fi

  echo "服务地址（按需使用）："
  if [[ -n "$casaos_url" ]]; then
    echo "CasaOS   : ${casaos_url}"
  else
    echo "CasaOS   : (未检测到 80/443 监听，请先安装/启动 CasaOS)"
  fi
  echo "OpenClaw : ${openclaw_url}"
  echo "Immich   : http://${ip:-<IP>}:2283"
  echo "Jellyfin : http://${ip:-<IP>}:8096"
  echo "NAS Files: http://${ip:-<IP>}:28085"
  echo "Voice Assistant: http://${ip:-<IP>}:28083"
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet voice-bridge 2>/dev/null; then
    local run_user
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
      run_user="$SUDO_USER"
    else
      run_user="$(id -un)"
    fi
    if id -nG "$run_user" | tr ' ' '\n' | grep -qx docker; then
      echo "语音桥   : http://${ip:-<IP>}:28082 (active)"
    else
      echo "语音桥   : 运行中但用户 $run_user 缺少 docker 组权限（请重跑: cd $APP_DIR/local_voice_chat && ./install_voice_bridge_service.sh）"
    fi
  else
    echo "语音桥   : 未运行（Voice Assistant 依赖它；安装: cd $APP_DIR/local_voice_chat && ./install_voice_bridge_service.sh）"
  fi
  echo "============================================================"
  echo "常用维护命令:"
  echo "  ./oc.sh status"
  echo "  ./oc.sh logs"
  echo "  ./oc.sh tools-sync"
}

# =============================================================================
# firewall — 幂等放行 NAS-Demo 所需 TCP 端口并持久化到 /etc/iptables/rules.v4
#
# 说明：
#   - 宿主机 INPUT 链默认 DROP（仅 22/3389/5900 等白名单放行），
#     本机服务（CasaOS 80、Immich 2283、Jellyfin 8096、OpenClaw 24190、
#     filebrowser 28085、对话助手 28083 等）需要显式放行才能局域网访问。
#   - 幂等：已存在的规则自动跳过，重复执行安全。
#   - 持久化：先备份 /etc/iptables/rules.v4 再写入，重启后仍生效。
#   - 需要 root；脚本直接调用宿主机 iptables，避免创建临时 alpine 容器
#     造成 CasaOS Legacy 区出现随机条目。
# =============================================================================
_ipt_in_host() {
  if [[ "${EUID}" -eq 0 ]]; then
    iptables "$@"
  else
    sudo iptables "$@"
  fi
}

_rule_exists() {
  # iptables -C 存在则返回 0；不存在返回 1（规则不存在不是错误）
  local port="$1"
  if _ipt_in_host -C INPUT -p tcp --dport "$port" -j ACCEPT >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

cmd_firewall() {
  local changed=0 p
  log "放行 NAS-Demo 所需 TCP 端口：$FIREWALL_PORTS"
  for p in $FIREWALL_PORTS; do
    if _rule_exists "$p"; then
      log "  端口 $p 已放行，跳过"
    else
      _ipt_in_host -A INPUT -p tcp --dport "$p" -j ACCEPT
      log "  端口 $p 已放行"
      changed=1
    fi
  done
  if [[ "$changed" -eq 1 ]]; then
    log "持久化规则到 /etc/iptables/rules.v4（先备份）..."
    local ts rules_file backup_file
    ts="$(date +%Y%m%d_%H%M%S)"
    rules_file="/etc/iptables/rules.v4"
    backup_file="${rules_file}.bak.${ts}"
    if [[ "${EUID}" -eq 0 ]]; then
      mkdir -p /etc/iptables
      [[ -f "$rules_file" ]] && cp "$rules_file" "$backup_file" || true
      iptables-save > "$rules_file"
    else
      sudo mkdir -p /etc/iptables
      sudo test -f "$rules_file" && sudo cp "$rules_file" "$backup_file" || true
      sudo sh -c "iptables-save > '$rules_file'"
    fi
    log "已写入 rules.v4（备份 ${backup_file}）"
  else
    log "无新增规则，跳过持久化"
  fi
  log "防火墙放行完成"
}

cleanup_firewall_helper_containers() {
  # 仅清理旧版本 install.sh 产生的临时 iptables helper 容器。
  local ids id cmd name
  ids="$(docker_cmd ps -a --filter ancestor=alpine:3.20 --format '{{.ID}}' 2>/dev/null || true)"
  [[ -n "$ids" ]] || return 0

  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    cmd="$(docker_cmd inspect "$id" --format '{{.Config.Cmd}}' 2>/dev/null || true)"
    if [[ "$cmd" != *"nsenter -t 1 -n iptables"* ]]; then
      continue
    fi
    name="$(docker_cmd inspect "$id" --format '{{.Name}}' 2>/dev/null | sed 's#^/##' || true)"
    docker_cmd rm -f "$id" >/dev/null 2>&1 || true
    log "已清理遗留临时容器: ${name:-$id}"
  done <<< "$ids"
}

apply_casaos_legacy_hide_patch() {
  # 按 README 约定：Legacy 容器条目按 title(en_us/en_US) 黑名单过滤。
  local home_bundle tmp_file backup_file
  local replacement

  home_bundle="$(ls /var/lib/casaos/www/src_views_Home_vue.*.js 2>/dev/null | head -1 || true)"
  if [[ -z "$home_bundle" ]]; then
    warn "未找到 CasaOS Home 前端 bundle，跳过 Legacy 卡片过滤补丁"
    return 0
  fi

  backup_file="${home_bundle}.nasdemo.bak"

  # 历史版本缺陷：替换串里未转义的 "&" 会被 sed 展开成整个匹配内容，
  # 注入出 `(item this.oldAppList = orgOldAppList;...)` 这类非法代码，
  # 导致 Home 路由编译失败（登录后白屏）。命中该特征时先用备份还原再重打补丁。
  if grep -q "const titleObj=(item this.oldAppList" "$home_bundle" 2>/dev/null; then
    if [[ ! -f "$backup_file" ]]; then
      warn "CasaOS Home bundle 已被旧补丁破坏且无备份，请重装 CasaOS 资源后重试"
      return 0
    fi
    if [[ "${EUID}" -eq 0 ]]; then
      cp "$backup_file" "$home_bundle"
    else
      sudo cp "$backup_file" "$home_bundle"
    fi
    warn "检测到 CasaOS Home bundle 曾被旧补丁破坏，已用备份还原"
  fi

  tmp_file="$(mktemp /tmp/casaos-home.XXXXXX.js)"
  cp "$home_bundle" "$tmp_file"

  # 补丁版本标记：规则变更后需能升级覆盖旧补丁
  local marker="nasDemoLegacyHideAppsV2"
  if grep -q "$marker" "$tmp_file"; then
    rm -f "$tmp_file"
    log "CasaOS Legacy 卡片过滤补丁已是最新版本，跳过"
    return 0
  fi

  # 命中旧版补丁时，先用 pristine 备份还原，避免在已打补丁的 bundle 上二次注入。
  if grep -qE "nasDemoLegacyHideBlacklist|nasDemoLegacyHideApps" "$tmp_file"; then
    if [[ -f "$backup_file" ]]; then
      cp "$backup_file" "$tmp_file"
      log "检测到旧版过滤补丁，已用备份还原后重新应用新版规则"
    else
      warn "检测到旧版过滤补丁但缺少备份，跳过首页 bundle 补丁（custom.js 层仍会生效）"
      rm -f "$tmp_file"
      return 0
    fi
  fi

  # 规则与 casaos/casaos-legacy-hide.custom.js 保持一致：
  #   container 型 -> 按容器名(title)过滤；v2app 型 -> 按应用 ID(name)过滤 + 幽灵条目过滤。
  # sed 替换串中的字面 "&" 必须转义为 "\&"，否则会被展开成整个匹配内容（历史上曾致首页白屏）。
  replacement="const nasDemoLegacyHideContainer=['openclaw','knowledge_base','media_downloader','immich-machine-learning','immich-postgres','immich-redis'];const ${marker}=['ai-nas','demo-smart-nas-system','nas-demo'];this.oldAppList = orgOldAppList.filter(item => { if (!item) return false; const t=(item.title) || {}; const title=((t.en_us || t.en_US || '') + '').trim().toLowerCase(); const nm=((item.name || '') + '').trim().toLowerCase(); if (item.app_type === 'container') return nasDemoLegacyHideContainer.indexOf(title) === -1; if (item.app_type === 'v2app') { if (${marker}.indexOf(nm) !== -1) return false; if (!item.status \&\& !item.port \&\& !item.store_app_id) return false; } return true; });"
  sed -i "0,/this.oldAppList = orgOldAppList;/s#this.oldAppList = orgOldAppList;#${replacement}#" "$tmp_file"

  if ! grep -q "$marker" "$tmp_file"; then
    rm -f "$tmp_file"
    warn "Legacy 卡片过滤补丁注入失败，保持原状"
    return 0
  fi

  if cmp -s "$tmp_file" "$home_bundle"; then
    rm -f "$tmp_file"
    log "CasaOS Home bundle 无需变更"
    return 0
  fi

  if [[ "${EUID}" -eq 0 ]]; then
    [[ -f "$backup_file" ]] || cp "$home_bundle" "$backup_file"
    cp "$tmp_file" "$home_bundle"
    systemctl restart casaos-gateway || warn "重启 casaos-gateway 失败，请手动执行: sudo systemctl restart casaos-gateway"
  else
    sudo test -f "$backup_file" || sudo cp "$home_bundle" "$backup_file"
    sudo cp "$tmp_file" "$home_bundle"
    sudo systemctl restart casaos-gateway || warn "重启 casaos-gateway 失败，请手动执行: sudo systemctl restart casaos-gateway"
  fi
  rm -f "$tmp_file"
  log "已应用 CasaOS Legacy 卡片过滤补丁并重启网关"
}

ensure_casaos_custom_js_legacy_filter() {
  # 更稳妥的兜底：在 custom.js 请求层过滤 appgrid 返回的 Legacy 容器卡片。
  # 该方式不依赖 hash bundle 文件名，CasaOS 升级后仍更容易保持生效。
  local custom_js template_js tmp_in tmp_out
  local begin_mark end_mark

  custom_js="/var/lib/casaos/www/js/custom.js"
  template_js="$APP_DIR/casaos/casaos-legacy-hide.custom.js"
  begin_mark="NAS_DEMO_LEGACY_HIDE_BEGIN"
  end_mark="NAS_DEMO_LEGACY_HIDE_END"
  tmp_in="$(mktemp /tmp/casaos-customjs.in.XXXXXX.js)"
  tmp_out="$(mktemp /tmp/casaos-customjs.out.XXXXXX.js)"

  if [[ -f "$custom_js" ]]; then
    cat "$custom_js" > "$tmp_in"
  else
    printf '%s\n' "// Add your custom scripts here" > "$tmp_in"
  fi

  # 先删除旧块，确保幂等更新。
  sed "/${begin_mark}/,/${end_mark}/d" "$tmp_in" > "$tmp_out"

  if [[ -f "$template_js" ]]; then
    cat "$template_js" >> "$tmp_out"
  else
    warn "未找到模板文件: $template_js，跳过 custom.js Legacy 过滤规则写入"
    rm -f "$tmp_in" "$tmp_out"
    return 0
  fi

  if cmp -s "$tmp_in" "$tmp_out"; then
    rm -f "$tmp_in" "$tmp_out"
    log "CasaOS custom.js Legacy 过滤规则已是最新"
    return 0
  fi

  if [[ "${EUID}" -eq 0 ]]; then
    mkdir -p /var/lib/casaos/www/js
    cp "$tmp_out" "$custom_js"
  else
    sudo mkdir -p /var/lib/casaos/www/js
    sudo cp "$tmp_out" "$custom_js"
  fi

  rm -f "$tmp_in" "$tmp_out"
  log "已写入 CasaOS custom.js Legacy 过滤规则"
}

# =============================================================================
# reset — 把 OpenClaw 配置重置为 bootstrap 模板并重启容器
# =============================================================================
cmd_reset() {
  local bootstrap="$APP_DIR/openclaw.bootstrap.json"
  local data_dir="/DATA/AppData/openclaw"
  local target="$data_dir/openclaw.json"

  [[ -f "$bootstrap" ]] || die "缺少 bootstrap 配置: $bootstrap"
  docker_cmd ps -a --format '{{.Names}}' | grep -qx openclaw || die "未找到 openclaw 容器"

  log "重置 OpenClaw 配置为 bootstrap 模板..."
  if [[ "$EUID" -eq 0 ]]; then
    mkdir -p "$data_dir"
    cp "$bootstrap" "$target"
  else
    sudo mkdir -p "$data_dir"
    sudo cp "$bootstrap" "$target"
  fi
  log "已重置: $target"
  docker_cmd restart openclaw
  log "openclaw 已重启，可用 ./oc.sh deploy 重新按需部署"
}

# =============================================================================
# bootstrap — 若当前目录还没有 NAS-Demo 代码，自动 clone 后重新执行 install.sh
# =============================================================================
bootstrap_if_needed() {
  if [[ -f "$APP_DIR/oc.sh" && -f "$APP_DIR/docker-compose.yml" ]]; then
    return 0
  fi
  log "当前目录不是 NAS-Demo 仓库，自动拉取代码: $INSTALL_REPO"
  require_cmd git
  local target="${PWD}/NAS-Demo"
  if [[ -d "$target/.git" ]]; then
    log "已存在 $target，直接更新"
    git -C "$target" pull --ff-only || warn "git pull 失败，使用现有代码继续"
  else
    git clone "$INSTALL_REPO" "$target" || die "git clone 失败，请检查仓库地址/网络，或先手动 clone 后进入目录运行"
  fi
  exec bash "$target/install.sh" "$@"
}

main() {
  bootstrap_if_needed "$@"
  if [[ "$ARG_MIRROR_ONLY" -eq 1 ]]; then
    local mirror_script="$APP_DIR/setup_docker_mirror.sh"
    [[ -f "$mirror_script" ]] || die "缺少 $mirror_script"
    if [[ "${EUID}" -eq 0 ]]; then
      bash "$mirror_script"
    else
      sudo bash "$mirror_script"
    fi
    exit 0
  fi
  if [[ "$ARG_UI_FIX_ONLY" -eq 1 ]]; then
    cleanup_firewall_helper_containers || warn "清理遗留临时容器失败，可稍后手动执行 docker rm -f <container>"
    ensure_casaos_custom_js_legacy_filter || warn "写入 CasaOS custom.js 过滤规则失败，可稍后手动处理"
    apply_casaos_legacy_hide_patch || warn "应用 CasaOS Legacy 卡片过滤补丁失败，可稍后手动按 README 0.2.1 处理"
    log "ui-fix 完成"
    exit 0
  fi
  if [[ "$RESET" -eq 1 ]]; then
    cmd_reset
    exit 0
  fi
  check_env
  if [[ "$CHECK_ONLY" -eq 1 ]]; then
    log "--check 完成"
    exit 0
  fi
  if [[ "$ARG_FIREWALL_ONLY" -eq 1 ]]; then
    cmd_firewall
    exit 0
  fi
  prepare_env
  if [[ "$SKIP_FIREWALL" -eq 0 ]]; then
    cmd_firewall || warn "防火墙放行失败，可稍后手动执行: bash install.sh firewall"
  else
    log "已按 --skip-firewall 跳过防火墙放行"
  fi
  deploy_all
  cleanup_firewall_helper_containers || warn "清理遗留临时容器失败，可稍后手动执行 docker rm -f <container>"
  ensure_casaos_custom_js_legacy_filter || warn "写入 CasaOS custom.js 过滤规则失败，可稍后手动处理"
  apply_casaos_legacy_hide_patch || warn "应用 CasaOS Legacy 卡片过滤补丁失败，可稍后手动按 README 0.2.1 处理"
  start_pairing_auto_approve_window
  summary
}

main "$@"
