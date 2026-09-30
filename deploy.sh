#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)"
APP_DIR="$SCRIPT_DIR"
if [[ ! -f "$APP_DIR/docker-compose.yml" && -f "$HOME/NAS-Demo/docker-compose.yml" ]]; then
  APP_DIR="$HOME/NAS-Demo"
fi
COMPOSE_FILE="$APP_DIR/docker-compose.yml"
BOOTSTRAP_CONFIG="$APP_DIR/openclaw.bootstrap.json"
DATA_DIR="/DATA/AppData/openclaw"
TARGET_CONFIG="$DATA_DIR/openclaw.json"
ENV_FILE="$APP_DIR/.env"

# NAS 共享目录（用户名无关：默认当前用户主目录/nas_share）
NAS_ROOT="${NAS_ROOT:-$HOME/nas_share}"
export NAS_ROOT

if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

OPENCLAW_IMAGE="openclaw/openclaw:2026.9.3@sha256:6cb72e1599b3b76e2ea3dcc2f4dd7f112247367fbecadfcdee8caa7423f4989b"

# 网络：compose 模式下由 docker-compose.yml 声明；plain docker run 兜底时需手动挂载，
# 否则 openclaw 无法按容器名访问 media_downloader / knowledge_base / immich（MCP 工具全废）。
NAS_NET="${NAS_NET:-nas-demo_nas-net}"
IMMICH_NET="${IMMICH_NET:-big-bear-immich_big_bear_immich_network}"

# 内存上限：与 docker-compose.yml 的 mem_limit 保持一致（docker run 不会读取 compose 配置）
OPENCLAW_MEM_LIMIT="${OPENCLAW_MEM_LIMIT:-1024m}"

OPENCLAW_MODEL_ID="${OPENCLAW_MODEL_ID:-deepseek-chat}"
OPENCLAW_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-casaos}"
export OPENCLAW_MODEL_ID OPENCLAW_GATEWAY_TOKEN

# MCP JS 在 openclaw 容器内以非 root 用户写 /logs；默认 775 在某些 UID 映射下不可写。
# 统一放宽到 777，保证 logs/ 内文件日志可落盘（仓库内仅本地开发使用）。
mkdir -p "$APP_DIR/logs"
chmod 777 "$APP_DIR/logs" 2>/dev/null || true

if [[ -z "${OPENCLAW_MODEL_BASE_URL:-}" || -z "${OPENCLAW_MODEL_API_KEY:-}" ]]; then
  echo "WARN: OPENCLAW_MODEL_BASE_URL / OPENCLAW_MODEL_API_KEY 未完整配置，OpenClaw 可能无法调用模型。"
  echo "WARN: 请先检查 $ENV_FILE 或重新执行: bash install.sh"
fi

if [[ "${EUID}" -eq 0 ]]; then
  DOCKER_CMD=(docker)
else
  DOCKER_CMD=(sudo docker)
fi

if [[ "${EUID}" -eq 0 ]]; then
  SUDO_CMD=()
else
  SUDO_CMD=(sudo)
fi

HAS_DOCKER_COMPOSE="false"
HAS_DOCKER_COMPOSE_LEGACY="false"
FORCE_PLAIN_DOCKER="${FORCE_PLAIN_DOCKER:-0}"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Compose file not found: $COMPOSE_FILE"
  exit 1
fi

if [[ ! -f "$BOOTSTRAP_CONFIG" ]]; then
  echo "Bootstrap config not found: $BOOTSTRAP_CONFIG"
  exit 1
fi

if [[ "$FORCE_PLAIN_DOCKER" != "1" ]]; then
  if "${DOCKER_CMD[@]}" compose version >/dev/null 2>&1; then
    HAS_DOCKER_COMPOSE="true"
  elif command -v docker-compose >/dev/null 2>&1; then
    HAS_DOCKER_COMPOSE_LEGACY="true"
  fi
fi

echo "[1/4] Stop and remove broken CasaOS OpenClaw container if exists"
"${DOCKER_CMD[@]}" rm -f openclaw >/dev/null 2>&1 || true

echo "[2/4] Remove incompatible image tag if exists"
"${DOCKER_CMD[@]}" rmi icewhaletech/openclaw:2026.5.7 >/dev/null 2>&1 || true

echo "[3/4] Pull arm64-capable upstream image"
"${DOCKER_CMD[@]}" pull --platform linux/arm64 "$OPENCLAW_IMAGE"

echo "[3.5/4] Seed initial OpenClaw gateway config if missing"
"${SUDO_CMD[@]}" mkdir -p "$DATA_DIR"
if [[ ! -f "$TARGET_CONFIG" ]]; then
  "${SUDO_CMD[@]}" cp "$BOOTSTRAP_CONFIG" "$TARGET_CONFIG"
  echo "Created initial config: $TARGET_CONFIG"
else
  echo "Keeping existing config: $TARGET_CONFIG"
fi

echo "[4/4] Start OpenClaw"

# Parse IMMICH vars from docker-compose.yml for the docker run fallback
_parse_compose_env() {
  local key="$1"
  grep -E "^\s+${key}:" "$COMPOSE_FILE" | head -1 | sed 's/.*: *//' | tr -d '"'\''[:space:]'
}
IMMICH_URL_VAL="$(_parse_compose_env IMMICH_URL)"
IMMICH_API_KEY_VAL="$(_parse_compose_env IMMICH_API_KEY)"

if [[ "$HAS_DOCKER_COMPOSE" == "true" ]]; then
  echo "Using: docker compose"
  "${DOCKER_CMD[@]}" compose -f "$COMPOSE_FILE" up -d
elif [[ "$HAS_DOCKER_COMPOSE_LEGACY" == "true" ]]; then
  echo "Using: docker-compose"
  "${SUDO_CMD[@]}" docker-compose -f "$COMPOSE_FILE" up -d
else
  echo "Compose command not found, fallback to plain docker run"

  # 1) 确保主网络存在（与 docker-compose.yml 的 nas-net 等价）
  if ! "${DOCKER_CMD[@]}" network inspect "$NAS_NET" >/dev/null 2>&1; then
    "${DOCKER_CMD[@]}" network create "$NAS_NET" >/dev/null 2>&1 || true
  fi

  "${DOCKER_CMD[@]}" run -d \
    --name openclaw \
    --restart unless-stopped \
    --init \
    --platform linux/arm64 \
    --no-healthcheck \
    --network "$NAS_NET" \
    --memory "$OPENCLAW_MEM_LIMIT" \
    --add-host host.docker.internal:host-gateway \
    -e HOME=/home/node \
    -e OPENCLAW_HOME=/home/node \
    -e TERM=xterm-256color \
    -e OPENCLAW_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN}" \
    -e TZ=Asia/Shanghai \
    -e OPENCLAW_MODEL_BASE_URL="${OPENCLAW_MODEL_BASE_URL:-}" \
    -e OPENCLAW_MODEL_API_KEY="${OPENCLAW_MODEL_API_KEY:-}" \
    -e OPENCLAW_MODEL_ID="${OPENCLAW_MODEL_ID}" \
    -e IMMICH_URL="${IMMICH_URL_VAL:-http://10.55.84.133:2283}" \
    -e IMMICH_API_KEY="${IMMICH_API_KEY_VAL:-}" \
    -p 24190:18789 \
    -p 18790:18790 \
    -v /DATA/AppData/openclaw:/home/node/.openclaw \
    -v "$APP_DIR/logs":/logs \
    -v "$NAS_ROOT":/nas_share \
    "$OPENCLAW_IMAGE" \
    /bin/bash -lc 'node dist/index.js gateway --bind lan --allow-unconfigured --port 18789'

  # 2) 追加加入 Immich 网络（docker run 不支持多 --network，需事后 connect）
  if "${DOCKER_CMD[@]}" network inspect "$IMMICH_NET" >/dev/null 2>&1; then
    if ! "${DOCKER_CMD[@]}" inspect openclaw --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null | tr ' ' '\n' | grep -qx "$IMMICH_NET"; then
      "${DOCKER_CMD[@]}" network connect "$IMMICH_NET" openclaw >/dev/null 2>&1 \
        && echo "Connected openclaw to $IMMICH_NET" \
        || echo "WARN: 无法将 openclaw 接入 $IMMICH_NET（Immich/知识库访问可能受限）"
    fi
  fi
fi

echo "Done. Check status with: sudo docker ps | grep -i openclaw"
echo "Open URL: https://<your-host-ip>:24190/#token=casaos"
