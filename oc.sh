#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)"
APP_DIR="$SCRIPT_DIR"
if [[ ! -f "$APP_DIR/deploy.sh" && -f "$HOME/NAS-Demo/deploy.sh" ]]; then
  APP_DIR="$HOME/NAS-Demo"
fi

if [[ -f "$APP_DIR/.env" ]]; then
  set -a
  source "$APP_DIR/.env"
  set +a
fi

FILEBROWSER_IMAGE="filebrowser/filebrowser:2.63.23@sha256:a469ea076d4a1b4b1d86a41d130f2f536cd9da996a2b1fb39c0d7635f9d89b9a"

# NAS 共享目录：可从 .env / 环境变量覆盖，默认当前用户主目录（用户名无关）
NAS_ROOT="${NAS_ROOT:-$HOME/nas_share}"
export NAS_ROOT

docker_cmd() {
  if [[ "${EUID}" -eq 0 ]]; then
    docker "$@"
    return
  fi
  if docker info >/dev/null 2>&1; then
    docker "$@"
  else
    sudo docker "$@"
  fi
}

ensure_repo_logs_dir_writable() {
  mkdir -p "$APP_DIR/logs"
  chmod 777 "$APP_DIR/logs" 2>/dev/null || sudo chmod 777 "$APP_DIR/logs" 2>/dev/null || true
}

usage() {
  cat <<'EOF'
Usage:
  ./oc.sh deploy
  ./oc.sh ui-fix            Re-apply CasaOS Legacy card filtering only
  ./oc.sh reset
  ./oc.sh status
  ./oc.sh logs [SERVICE|all] [N] [--follow] [--level=info|warn|error]
  ./oc.sh health
  ./oc.sh url
  ./oc.sh casaos-url
  ./oc.sh model
  ./oc.sh model-apply     Apply .env model config (base URL/API key/model id) without full install
  ./oc.sh tools-nas-setup
  ./oc.sh tools-nas-show
  ./oc.sh tools-media-setup
  ./oc.sh tools-media-show
  ./oc.sh tools-immich-setup
  ./oc.sh tools-immich-show
  ./oc.sh tools-kb-setup
  ./oc.sh tools-kb-show
  ./oc.sh tools-sync         Sync NAS-Demo sources -> nas_share/tools (runtime copy)
  ./oc.sh tools-photos-setup  Sync sample photos -> 家庭相册/测试样例 + Family album/Test Samples + Travel/Test Samples，并自动导入 Immich
  ./oc.sh pair-list
  ./oc.sh pair-approve <request_id>
  ./oc.sh openclaw-app-deploy  Install OpenClaw launcher (CasaOS web app)
  ./oc.sh immich-apply       Apply immich-compose.yml to CasaOS
  ./oc.sh immich-show        Show current CasaOS Immich config
  ./oc.sh immich-sync-jobs   Trigger Immich ML jobs (faceDetection + smartSearch)
  ./oc.sh jellyfin-deploy    Start Jellyfin (jellyfin-compose.yml)
  ./oc.sh jellyfin-show      Show Jellyfin container status
  ./oc.sh jellyfin-apply [URL]  Apply Jellyfin .env key/url to voice-bridge and verify
  ./oc.sh jellyfin-key-check [URL]  Verify Jellyfin API key in .env and runtime state
  ./oc.sh mic-mode [status|usb|onboard]
  ./oc.sh voice-assistant-deploy  Install Voice Assistant (CasaOS web app)
  ./oc.sh voice-assistant-show    Show Voice Assistant container status
  ./oc.sh nas-files-deploy        Install NAS file browser (read-only nas_share)
  ./oc.sh nas-files-show          Show NAS file browser container status
  ./oc.sh docker-mirror       Configure Docker registry mirrors (fix image pull resets/failures)
EOF
}

clean_request_id() {
  printf '%s' "$1" | tr -d '[:space:]' | sed 's/[。．，,；;：:]$//'
}

log_level_pattern() {
  case "${1:-info}" in
    error)
      echo 'error|err|failed|exception|traceback|panic'
      ;;
    warn)
      echo 'warn|warning|error|err|failed|exception|traceback|panic'
      ;;
    info|*)
      echo ''
      ;;
  esac
}

show_one_service_logs() {
  local service="$1"
  local tail_n="$2"
  local follow_flag="$3"
  local level="$4"
  local pattern
  local cmd=(logs --tail "$tail_n")

  if [[ "$follow_flag" == "1" ]]; then
    cmd+=(--follow)
  fi
  cmd+=("$service")

  pattern="$(log_level_pattern "$level")"
  if [[ -n "$pattern" ]]; then
    docker_cmd "${cmd[@]}" 2>&1 | grep -Ei "$pattern" || true
  else
    docker_cmd "${cmd[@]}" 2>&1 || true
  fi
}

has_casaos_cli() {
  command -v casaos-cli >/dev/null 2>&1
}

casaos_app_exists() {
  local appid="$1"
  casaos-cli app-management show local "$appid" --yaml >/dev/null 2>&1
}

first_existing_casaos_appid() {
  local id
  for id in "$@"; do
    if casaos_app_exists "$id"; then
      echo "$id"
      return 0
    fi
  done
  return 1
}

remove_containers_if_exist() {
  local name
  for name in "$@"; do
    docker_cmd rm -f "$name" >/dev/null 2>&1 || true
  done
}

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[\/&]/\\&/g'
}

short_fingerprint() {
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum | awk '{print substr($1,1,10)}'
  elif command -v shasum >/dev/null 2>&1; then
    printf '%s' "$1" | shasum -a 256 | awk '{print substr($1,1,10)}'
  else
    printf '%s' "na"
  fi
}

env_get_raw_value() {
  local key="$1" env_file="$APP_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    printf '%s' ""
    return 0
  fi
  grep -E "^[[:space:]]*${key}=" "$env_file" | tail -1 | cut -d= -f2- | xargs || true
}

env_set_value() {
  local key="$1" value="$2" env_file="$APP_DIR/.env"
  local line
  line="${key}=${value}"
  touch "$env_file"

  awk -v k="$key" -v nl="$line" '
    {
      if ($0 ~ "^[[:space:]]*" k "=" || $0 ~ "^[[:space:]]*#[[:space:]]*" k "=") {
        if (!done) { print nl; done = 1 }
        next
      }
      print
    }
    END { if (!done) print nl }
  ' "$env_file" > "$env_file.tmp" && mv "$env_file.tmp" "$env_file"
}

env_ensure_value() {
  local key="$1" fallback="$2" cur
  cur="$(env_get_raw_value "$key")"
  if [[ -n "$cur" ]]; then
    env_set_value "$key" "$cur"
  else
    env_set_value "$key" "$fallback"
  fi
}

mic_mode_status() {
  local env_file="$APP_DIR/.env"
  echo "Microphone mode (.env): $env_file"
  for k in \
    VOICE_MIC_PRIORITY \
    VOICE_MIC_USB_BACKEND \
    VOICE_MIC_USB_INPUT \
    VOICE_MIC_ONBOARD_BACKEND \
    VOICE_MIC_ONBOARD_INPUT \
    VOICE_MIC_RECHECK_SEC \
    VOICE_MIC_STRICT \
    VOICE_MIC_MIN_LEVEL_DBFS; do
    echo "  $k=$(env_get_raw_value "$k")"
  done

  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet voice-bridge 2>/dev/null; then
    local pid
    pid="$(systemctl show -p MainPID --value voice-bridge 2>/dev/null || true)"
    if [[ -n "$pid" && "$pid" != "0" && -r "/proc/$pid/environ" ]]; then
      echo "Microphone mode (voice-bridge runtime):"
      for k in \
        VOICE_MIC_PRIORITY \
        VOICE_MIC_USB_BACKEND \
        VOICE_MIC_USB_INPUT \
        VOICE_MIC_ONBOARD_BACKEND \
        VOICE_MIC_ONBOARD_INPUT \
        VOICE_MIC_RECHECK_SEC \
        VOICE_MIC_STRICT \
        VOICE_MIC_MIN_LEVEL_DBFS; do
        rv="$(tr '\0' '\n' < "/proc/$pid/environ" | grep -E "^${k}=" | tail -1 | cut -d= -f2- || true)"
        echo "  $k=${rv}"
      done
    fi
  else
    echo "voice-bridge: inactive"
  fi
}

mic_mode_apply() {
  local mode="$1"

  # Backward-compatible aliases (hidden from help to keep UX simple).
  case "$mode" in
    usb-first|usb-only)
      mode="usb"
      ;;
    onboard-first|onboard-only)
      mode="onboard"
      ;;
  esac

  env_ensure_value VOICE_MIC_USB_BACKEND "alsa"
  env_ensure_value VOICE_MIC_USB_INPUT "plughw:Audio,0"
  env_ensure_value VOICE_MIC_ONBOARD_BACKEND "pulse"
  env_ensure_value VOICE_MIC_ONBOARD_INPUT "regular0"
  env_ensure_value VOICE_MIC_RECHECK_SEC "60"

  case "$mode" in
    usb)
      env_set_value VOICE_MIC_PRIORITY "usb,onboard"
      env_set_value VOICE_MIC_STRICT "0"
      ;;
    onboard)
      env_set_value VOICE_MIC_PRIORITY "onboard"
      env_set_value VOICE_MIC_STRICT "1"
      ;;
    *)
      echo "ERROR: unknown mic mode: $mode"
      echo "Usage: ./oc.sh mic-mode [status|usb|onboard]"
      return 1
      ;;
  esac

  # 保留旧键位兼容。
  env_ensure_value VOICE_RECORD_BACKEND "auto"
  env_ensure_value VOICE_MIC_INPUT "plughw:Audio,0"

  if command -v systemctl >/dev/null 2>&1; then
    if [[ "${EUID}" -eq 0 ]]; then
      systemctl restart voice-bridge
    else
      sudo systemctl restart voice-bridge
    fi
  fi

  echo "Applied mic mode: $mode"
  mic_mode_status
}

jellyfin_api_key_from_env() {
  local env_file="$APP_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    printf '%s' ""
    return 0
  fi
  grep -E '^JELLYFIN_API_KEY=' "$env_file" | tail -1 | sed 's/^JELLYFIN_API_KEY=//' | tr -d '"' | xargs
}

check_jellyfin_api_key() {
  local jellyfin_url api_key code code2 body_file pid runtime_key
  local env_fp run_fp

  jellyfin_url="${2:-${JELLYFIN_URL:-http://127.0.0.1:8096}}"
  jellyfin_url="${jellyfin_url%/}"
  api_key="$(jellyfin_api_key_from_env)"

  if [[ -z "$api_key" ]]; then
    echo "ERROR: .env 未配置 JELLYFIN_API_KEY"
    echo "请先在 Jellyfin 后台创建 API 密钥并写入 $APP_DIR/.env"
    return 1
  fi

  env_fp="$(short_fingerprint "$api_key")"
  echo "Jellyfin key source: $APP_DIR/.env"
  echo "  key length=${#api_key}, fp=${env_fp}"

  if ! curl -fsS --max-time 5 "${jellyfin_url}/System/Info/Public" >/dev/null 2>&1; then
    echo "ERROR: Jellyfin 不可达: ${jellyfin_url}"
    return 1
  fi

  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet voice-bridge 2>/dev/null; then
    pid="$(systemctl show -p MainPID --value voice-bridge 2>/dev/null || true)"
    if [[ -n "$pid" && "$pid" != "0" && -r "/proc/$pid/environ" ]]; then
      runtime_key="$(tr '\0' '\n' < "/proc/$pid/environ" | grep -E '^JELLYFIN_API_KEY=' | tail -1 | cut -d= -f2- || true)"
      if [[ -n "$runtime_key" ]]; then
        run_fp="$(short_fingerprint "$runtime_key")"
        if [[ "$runtime_key" == "$api_key" ]]; then
          echo "voice-bridge runtime key: 已加载当前 .env（fp=${run_fp}）"
        else
          echo "WARN: voice-bridge 仍在使用旧 key（runtime fp=${run_fp}, env fp=${env_fp}）"
          echo "      请执行: sudo systemctl restart voice-bridge"
        fi
      else
        echo "WARN: 未在 voice-bridge 进程环境中读取到 JELLYFIN_API_KEY（请确认 service 的 EnvironmentFile 配置）"
      fi
    fi
  else
    echo "WARN: voice-bridge 未运行，建议先执行: sudo systemctl start voice-bridge"
  fi

  body_file="$(mktemp)"
  code="$(curl -sS -o "$body_file" -w '%{http_code}' -H "Authorization: MediaBrowser Token=${api_key}" "${jellyfin_url}/ScheduledTasks" || true)"
  case "$code" in
    200)
      echo "OK: Jellyfin API key 校验通过（/ScheduledTasks -> 200）"
      ;;
    401)
      echo "ERROR: Jellyfin API key 无效或未授权（/ScheduledTasks -> 401）"
      rm -f "$body_file"
      return 2
      ;;
    403)
      echo "WARN: key 可用但权限不足（/ScheduledTasks -> 403），自动扫库/自动播放可能失败"
      ;;
    000)
      echo "ERROR: 请求 Jellyfin 失败（网络或服务不可达）"
      rm -f "$body_file"
      return 1
      ;;
    *)
      echo "WARN: /ScheduledTasks 返回 HTTP $code"
      head -c 200 "$body_file" | tr '\n' ' '
      echo
      ;;
  esac

  code2="$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: MediaBrowser Token=${api_key}" "${jellyfin_url}/Sessions" || true)"
  if [[ "$code2" == "200" ]]; then
    echo "OK: 会话接口可访问（/Sessions -> 200）"
  elif [[ "$code2" == "401" ]]; then
    echo "WARN: 会话接口未授权（/Sessions -> 401），远程播放可能不可用"
  else
    echo "INFO: /Sessions -> HTTP $code2"
  fi

  if command -v journalctl >/dev/null 2>&1 && systemctl is-active --quiet voice-bridge 2>/dev/null; then
    if journalctl -u voice-bridge -n 200 --no-pager 2>/dev/null | grep -Eqi 'Jellyfin.*(401|unauthorized)|401 Unauthorized'; then
      echo "WARN: 最近日志出现 Jellyfin 401/unauthorized，建议重启 voice-bridge 后复测"
    else
      echo "OK: 最近 200 行 voice-bridge 日志未发现 Jellyfin 401"
    fi
  fi

  rm -f "$body_file"
}

apply_model_from_env() {
  local env_file="$APP_DIR/.env"
  local base_url api_key model_id provider_json
  local run data_dir cfg backup_file

  if [[ ! -f "$env_file" ]]; then
    echo "ERROR: 缺少 $env_file（请先执行 bash install.sh 完成初始化）" >&2
    return 1
  fi

  # 显式重读 .env，避免沿用过期/未导出的环境变量
  base_url="$(grep -E '^OPENCLAW_MODEL_BASE_URL=' "$env_file" | tail -1 | sed 's/^OPENCLAW_MODEL_BASE_URL=//' | tr -d '"' | xargs)"
  api_key="$(grep -E '^OPENCLAW_MODEL_API_KEY=' "$env_file" | tail -1 | sed 's/^OPENCLAW_MODEL_API_KEY=//' | tr -d '"' | xargs)"
  model_id="$(grep -E '^OPENCLAW_MODEL_ID=' "$env_file" | tail -1 | sed 's/^OPENCLAW_MODEL_ID=//' | tr -d '"' | xargs)"
  model_id="${model_id:-deepseek-chat}"

  if [[ -z "$base_url" ]]; then
    echo "ERROR: .env 未配置 OPENCLAW_MODEL_BASE_URL" >&2
    return 1
  fi
  if [[ -z "$api_key" ]]; then
    echo "ERROR: .env 未配置 OPENCLAW_MODEL_API_KEY" >&2
    return 1
  fi
  # 终端粘贴 API Key 常见污染（重复粘贴/ESC 序列混入），提前告警便于自检
  if ! printf '%s' "$api_key" | grep -Eq '^[A-Za-z0-9._-]+$'; then
    echo "WARN: OPENCLAW_MODEL_API_KEY 含非常见字符（可能粘贴污染）: $api_key" >&2
  fi

  if ! docker_cmd ps --format '{{.Names}}' | grep -qx openclaw; then
    echo "ERROR: openclaw 容器未运行，请先执行 bash install.sh 部署" >&2
    return 1
  fi

  echo "将从 .env 应用模型配置:"
  echo "  baseUrl = $base_url"
  echo "  modelId = $model_id"
  echo "  apiKey  = ${api_key:0:6}... (共 ${#api_key} 字符)"

  provider_json="$(python3 - "$base_url" "$api_key" "$model_id" <<'PY'
import json, sys
base_url, api_key, model_id = sys.argv[1], sys.argv[2], sys.argv[3]
payload = {
    "baseUrl": base_url,
    "apiKey": api_key,
    "api": "openai-completions",
    "models": [{
        "id": model_id,
        "name": model_id,
        "input": ["text", "image"],
        "contextWindow": 64000,
        "maxTokens": 8192,
    }],
}
print(json.dumps(payload, ensure_ascii=False))
PY
)"

  if docker_cmd exec openclaw node dist/index.js config set models.providers.custom "$provider_json" --strict-json \
     && docker_cmd exec openclaw node dist/index.js config set agents.defaults.model.primary "custom/${model_id}"; then
    echo "已通过 OpenClaw config set 更新模型配置。"
    return 0
  fi

  # config set 在旧配置残留 ${VAR} 环境引用时会被保护性拦截；回退直接编辑 openclaw.json
  echo "WARN: config set 被拦截（通常因旧配置残留环境引用），回退直接写入 openclaw.json ..."
  data_dir="/DATA/AppData/openclaw"
  cfg="$data_dir/openclaw.json"
  if [[ ! -f "$cfg" ]]; then
    echo "ERROR: 找不到 $cfg" >&2
    return 1
  fi
  backup_file="$cfg.bak-$(date +%Y%m%d%H%M%S)"
  run=""
  if [[ "${EUID}" -ne 0 ]]; then
    run="sudo"
  fi
  $run cp "$cfg" "$backup_file"
  $run python3 - "$cfg" "$base_url" "$api_key" "$model_id" <<'PY'
import json, sys
path, base_url, api_key, model_id = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
data = json.load(open(path, encoding="utf-8"))
custom = {
    "baseUrl": base_url,
    "apiKey": api_key,
    "api": "openai-completions",
    "models": [{
        "id": model_id,
        "name": model_id,
        "input": ["text", "image"],
        "contextWindow": 64000,
        "maxTokens": 8192,
    }],
}
data.setdefault("models", {}).setdefault("providers", {})["custom"] = custom
data.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})["primary"] = "custom/%s" % model_id
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
print("openclaw.json 已更新")
PY
  $run chown -R 1000:1000 "$data_dir" 2>/dev/null || true
  echo "已直接写入 openclaw.json（备份: $backup_file）"
  return 0
}

render_template_file() {
  local src="$1"
  local dst app_dir_escaped nas_root_escaped nas_puid nas_pgid
  dst="$(mktemp "/tmp/$(basename "$src").XXXXXX")"
  app_dir_escaped="$(escape_sed_replacement "$APP_DIR")"
  nas_root_escaped="$(escape_sed_replacement "$NAS_ROOT")"
  nas_puid="${NAS_PUID:-$(id -u)}"
  nas_pgid="${NAS_PGID:-$(id -g)}"

  sed \
    -e "s#__APP_DIR__#${app_dir_escaped}#g" \
    -e "s#__NAS_ROOT__#${nas_root_escaped}#g" \
    -e "s#__NAS_PUID__#${nas_puid}#g" \
    -e "s#__NAS_PGID__#${nas_pgid}#g" \
    "$src" > "$dst"

  printf '%s\n' "$dst"
}

host_primary_ip() {
  local ip
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="src"){print $(i+1); exit}}}')"
  if [[ -z "$ip" ]]; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  echo "$ip"
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

openclaw_base_url() {
  local ip host scheme
  ip="$(host_primary_ip)"
  host="${ip:-<your-host-ip>}"
  scheme="http"
  if curl -k -I --max-time 3 https://127.0.0.1:24190/healthz >/dev/null 2>&1; then
    scheme="https"
  fi
  echo "${scheme}://${host}:24190"
}

ensure_openclaw_on_immich_network() {
  local network_name="big-bear-immich_big_bear_immich_network"
  if ! docker_cmd inspect openclaw >/dev/null 2>&1; then
    return 0
  fi
  if ! docker_cmd network inspect "$network_name" >/dev/null 2>&1; then
    return 0
  fi
  if docker_cmd inspect openclaw --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null | tr ' ' '\n' | grep -qx "$network_name"; then
    return 0
  fi
  docker_cmd network connect "$network_name" openclaw >/dev/null 2>&1 || true
}

# Immich API Key：优先宿主 .env（oc.sh 启动时已 source，用户唯一配置源），
# 缺失时回退运行中容器注入值。这样修改 .env 后直接重跑 tools-*-setup 即可生效，
# 无需重建 openclaw 容器。
immich_api_key() {
  if [[ -n "${IMMICH_API_KEY:-}" ]]; then
    printf '%s' "$IMMICH_API_KEY"
    return 0
  fi
  # 容器 env 兜底；历史部署曾写入坏值（如 "-}"），仅接受不含 '{'/'}' 的干净值
  local raw
  raw="$(docker_cmd exec openclaw printenv IMMICH_API_KEY 2>/dev/null || true)"
  if [[ -n "$raw" ]] && ! printf '%s' "$raw" | grep -qE '[{}]'; then
    printf '%s' "$raw"
  fi
}

# Immich 服务地址：优先宿主 .env（IMMICH_URL），回退容器 env，最后用默认容器名。
# 历史部署曾把容器 env 写成裸端口 "2283"（无协议前缀），必须校验 http(s):// 后才可用。
immich_server_url() {
  if [[ -n "${IMMICH_URL:-}" ]] && [[ "$IMMICH_URL" == http://* || "$IMMICH_URL" == https://* ]]; then
    printf '%s' "$IMMICH_URL"
    return 0
  fi
  local raw
  raw="$(docker_cmd exec openclaw printenv IMMICH_URL 2>/dev/null || true)"
  if [[ "$raw" == http://* || "$raw" == https://* ]]; then
    printf '%s' "$raw"
    return 0
  fi
  printf '%s' "http://immich-server:2283"
}

# 宿主机侧访问 Immich API 统一走本机映射端口；.env 里的 IMMICH_URL 主要给容器内 MCP 使用。
immich_host_url() {
  printf '%s' "http://127.0.0.1:2283"
}

import_sample_photos_to_immich() {
  local src_dir="$APP_DIR/assets/sample_photos"
  local api_base api_key src name ts resp

  IMMICH_SAMPLE_CREATED=0
  IMMICH_SAMPLE_DUPLICATE=0
  IMMICH_SAMPLE_FAILED=0

  if [[ ! -d "$src_dir" ]]; then
    echo "SKIP  sample_photos 源目录不存在: $src_dir"
    return 0
  fi

  api_base="$(immich_host_url)/api"
  api_key="$(immich_api_key)"

  if [[ -z "$api_key" ]]; then
    echo "SKIP  Immich 样例照片导入：IMMICH_API_KEY 未配置"
    return 0
  fi

  if ! curl -fsS --max-time 5 "${api_base}/server/ping" >/dev/null 2>&1; then
    echo "SKIP  Immich 样例照片导入：服务未就绪 ($(immich_host_url))"
    return 0
  fi

  for src in "$src_dir"/*.jpg "$src_dir"/*.jpeg; do
    [[ -f "$src" ]] || continue
    name="$(basename "$src")"
    ts="$(date -u -r "$src" '+%Y-%m-%dT%H:%M:%S.000Z' 2>/dev/null || date -u '+%Y-%m-%dT%H:%M:%S.000Z')"
    if ! resp="$(curl -fsS -X POST "${api_base}/assets" \
      -H "x-api-key: ${api_key}" \
      -F "assetData=@${src};type=image/jpeg" \
      -F "fileCreatedAt=${ts}" \
      -F "fileModifiedAt=${ts}" \
      -F "deviceId=nas-demo-samples" \
      -F "deviceAssetId=sample-${name}" 2>/dev/null)"; then
      echo "WARN  Immich 导入失败: $name"
      IMMICH_SAMPLE_FAILED=$((IMMICH_SAMPLE_FAILED + 1))
      continue
    fi

    if printf '%s' "$resp" | grep -qE '"status"[[:space:]]*:[[:space:]]*"duplicate"'; then
      echo "SAME  $name (Immich)"
      IMMICH_SAMPLE_DUPLICATE=$((IMMICH_SAMPLE_DUPLICATE + 1))
    elif printf '%s' "$resp" | grep -qE '"status"[[:space:]]*:[[:space:]]*"created"|"id"[[:space:]]*:[[:space:]]*"'; then
      echo "IMPORTED  $name -> Immich"
      IMMICH_SAMPLE_CREATED=$((IMMICH_SAMPLE_CREATED + 1))
    else
      echo "WARN  Immich 返回异常: $name"
      IMMICH_SAMPLE_FAILED=$((IMMICH_SAMPLE_FAILED + 1))
    fi
  done

  echo "Immich sample import: created=$IMMICH_SAMPLE_CREATED duplicate=$IMMICH_SAMPLE_DUPLICATE failed=$IMMICH_SAMPLE_FAILED"
}

sync_sample_docs() {
  local src_dir="$APP_DIR/assets/sample_docs"
  local doc_root="$NAS_ROOT/文档"
  local src name dst

  SAMPLE_DOCS_CHANGED=0
  SAMPLE_DOCS_TOTAL=0

  if [[ ! -d "$src_dir" ]]; then
    echo "SKIP  sample_docs 源目录不存在: $src_dir"
    return 0
  fi

  mkdir -p "$doc_root"
  for src in "$src_dir"/*.md "$src_dir"/*.txt; do
    [[ -f "$src" ]] || continue
    SAMPLE_DOCS_TOTAL=$((SAMPLE_DOCS_TOTAL + 1))
    name="$(basename "$src")"
    dst="$doc_root/$name"
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
      echo "SAME  $name"
      continue
    fi
    cp "$src" "$dst"
    echo "SYNC  $name -> 文档/"
    SAMPLE_DOCS_CHANGED=$((SAMPLE_DOCS_CHANGED + 1))
  done

  if [[ "${EUID}" -eq 0 ]]; then
    chown -R "$(id -u):$(id -g)" "$doc_root" 2>/dev/null || true
  else
    sudo chown -R "$(id -u):$(id -g)" "$doc_root" 2>/dev/null || true
  fi

  echo "Sample docs sync: total=$SAMPLE_DOCS_TOTAL changed=$SAMPLE_DOCS_CHANGED"
}

trigger_immich_jobs() {
  local api key job resp summary http_code body_file

  api="$(immich_host_url)/api"
  key="$(immich_api_key)"

  if [[ -z "$key" ]]; then
    echo "SKIP  Immich jobs：IMMICH_API_KEY 未配置"
    return 0
  fi

  if ! curl -fsS --max-time 5 "${api}/server/ping" >/dev/null 2>&1; then
    echo "SKIP  Immich jobs：服务未就绪 ($(immich_host_url))"
    return 0
  fi

  body_file="$(mktemp)"
  for job in faceDetection smartSearch; do
    http_code="$(curl -sS -o "$body_file" -w '%{http_code}' -X PUT "${api}/jobs/${job}" \
      -H "x-api-key: ${key}" \
      -H "Content-Type: application/json" \
      -d '{"command":"start","force":false}' 2>/dev/null || true)"
    resp="$(cat "$body_file" 2>/dev/null || true)"

    if [[ "$http_code" == "400" ]] && printf '%s' "$resp" | grep -q 'Job is already running'; then
      echo "$job: already running"
      continue
    fi

    if [[ "$http_code" != "200" ]]; then
      echo "WARN  触发 Immich job 失败: $job"
      continue
    fi

    summary="$(printf '%s' "$resp" | python3 -c 'import json,sys; d=json.load(sys.stdin); q=d.get("queueStatus", {}); c=d.get("jobCounts", {}); print(f"isActive={q.get('"'"'isActive'"'"')} active={c.get('"'"'active'"'"')} waiting={c.get('"'"'waiting'"'"')} failed={c.get('"'"'failed'"'"')}")' 2>/dev/null || true)"
    if [[ -n "$summary" ]]; then
      echo "$job: $summary"
    else
      echo "$job: $resp"
    fi
  done
  rm -f "$body_file"
}

case "${1:-}" in
  deploy)
    "$APP_DIR/deploy.sh"
    ensure_openclaw_on_immich_network
    # 重新部署后也自动重应用 Legacy 卡片过滤，避免 CasaOS 资源更新后回退。
    bash "$APP_DIR/install.sh" ui-fix || true
    ;;
  ui-fix)
    bash "$APP_DIR/install.sh" ui-fix
    ;;
  reset)
    bash "$APP_DIR/install.sh" reset
    ;;
  status)
    docker_cmd ps -a | grep -i openclaw || true
    ;;
  logs)
    log_service="openclaw"
    log_tail="120"
    log_follow="0"
    log_level="info"
    args=("${@:2}")

    if [[ ${#args[@]} -gt 0 ]]; then
      if [[ "${args[0]}" =~ ^[0-9]+$ ]]; then
        log_tail="${args[0]}"
        args=("${args[@]:1}")
      elif [[ "${args[0]}" != -* ]]; then
        log_service="${args[0]}"
        args=("${args[@]:1}")
      fi
    fi

    if [[ ${#args[@]} -gt 0 && "${args[0]}" =~ ^[0-9]+$ ]]; then
      log_tail="${args[0]}"
      args=("${args[@]:1}")
    fi

    for arg in "${args[@]}"; do
      case "$arg" in
        -f|--follow)
          log_follow="1"
          ;;
        --level=*)
          log_level="${arg#*=}"
          ;;
      esac
    done

    if [[ "$log_service" == "all" ]]; then
      if [[ "$log_follow" == "1" ]]; then
        echo "ERROR: logs all 暂不支持 --follow，请改为指定单个服务"
        exit 1
      fi
      for svc in openclaw voice_assistant media_downloader knowledge_base immich-server immich-machine-learning jellyfin filebrowser openclaw_portal; do
        echo "===== ${svc} (tail=${log_tail}, level=${log_level}) ====="
        show_one_service_logs "$svc" "$log_tail" "0" "$log_level"
      done
    else
      show_one_service_logs "$log_service" "$log_tail" "$log_follow" "$log_level"
    fi
    ;;
  health)
    if curl -k -I --max-time 5 https://127.0.0.1:24190/healthz >/dev/null 2>&1; then
      curl -k -I --max-time 5 https://127.0.0.1:24190/healthz
    else
      curl -I --max-time 5 http://127.0.0.1:24190/healthz
    fi
    ;;
  url)
    echo "$(openclaw_base_url)/#token=${OPENCLAW_GATEWAY_TOKEN:-casaos}"
    ;;
  casaos-url)
    ip="$(host_primary_ip)"
    casaos_scheme="$(detect_casaos_scheme)"
    if [[ -n "$casaos_scheme" ]]; then
      echo "${casaos_scheme}://${ip:-<your-host-ip>}"
    else
      echo "WARN: 未检测到 CasaOS Web 服务（127.0.0.1:80/443）。" >&2
      echo "WARN: 可先执行安装/修复命令: curl -fsSL https://get.casaos.io | sudo bash" >&2
      echo "http://${ip:-<your-host-ip>}"
    fi
    ;;
  model)
    docker_cmd exec -it -e TERM=xterm-256color openclaw node dist/index.js config --section model
    ;;
  model-apply)
    apply_model_from_env
    docker_cmd restart openclaw
    echo "openclaw 已重启，模型配置已生效（可执行 ./oc.sh model 核对）。"
    ;;
  tools-nas-setup)
    # Restrict filesystem tools to NAS mount only.
    docker_cmd exec openclaw node dist/index.js mcp set nas_files '{"enabled":true,"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/nas_share"],"toolFilter":{"include":["move_file","list_directory","create_directory","search_files","get_file_info","read_file","write_file","edit_file"]}}'
    docker_cmd exec openclaw node dist/index.js mcp reload
    docker_cmd restart openclaw
    echo "NAS tools configured. Available file root: /nas_share"
    ;;
  tools-nas-show)
    docker_cmd exec openclaw node dist/index.js mcp show nas_files --json
    ;;
  tools-media-setup)
    ensure_repo_logs_dir_writable
    mkdir -p "$NAS_ROOT/downloads/Movies" "$NAS_ROOT/downloads/TV Shows"
    mkdir -p "$NAS_ROOT/tools"
    cp "$APP_DIR/local_voice_chat/download_media_mcp.js" "$NAS_ROOT/tools/download_media_mcp.js"

    if ! docker_cmd ps --format '{{.Names}}' | grep -qx media_downloader; then
      if docker_cmd ps -a --format '{{.Names}}' | grep -qx media_downloader; then
        docker_cmd start media_downloader >/dev/null
      else
        docker_cmd build -t nas-media-downloader:local "$APP_DIR/media_downloader"
        docker_cmd run -d \
          --name media_downloader \
          --restart unless-stopped \
          -e DOWNLOAD_ROOT=/downloads \
          -e PORT=8081 \
          -e YTDLP_TIMEOUT_SEC=1800 \
          -e DOWNLOAD_TTS_TEXT="下载已完成" \
          -e LOG_FILE=/logs/media_downloader.log \
          -e LOG_MAX_BYTES=5242880 \
          -e LOG_BACKUPS=3 \
          -v $NAS_ROOT/downloads:/downloads \
          -v $APP_DIR/logs:/logs \
          -p 28081:8081 \
          nas-media-downloader:local >/dev/null
      fi
    fi
    # 让 openclaw 能按容器名直接访问下载服务（host.docker.internal 在本机不可达）
    if docker_cmd network inspect big-bear-immich_big_bear-immich_network >/dev/null 2>&1; then
      if ! docker_cmd network inspect big-bear-immich_big_bear-immich_network \
          --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null | grep -q ' media_downloader'; then
        docker_cmd network connect big-bear-immich_big_bear-immich_network media_downloader
      fi
    fi

    docker_cmd exec openclaw node dist/index.js mcp set download_media '{"enabled":true,"command":"node","args":["/nas_share/tools/download_media_mcp.js"],"env":{"DOWNLOAD_API_URL":"http://media_downloader:8081/download","DOWNLOAD_ROOT_LABEL":"'"$NAS_ROOT"'/downloads","DOWNLOAD_NOTIFY_TEXT":"下载已完成","MCP_LOG_FILE":"/logs/mcp_download_media.log","LOG_MAX_BYTES":"5242880","LOG_BACKUPS":"3"}}'
    docker_cmd exec openclaw node dist/index.js mcp reload
    docker_cmd restart openclaw
    echo "Media download tool configured."
    echo "Download root: $NAS_ROOT/downloads"
    ;;
  tools-media-show)
    docker_cmd exec openclaw node dist/index.js mcp show download_media --json
    ;;
  tools-immich-setup)
    ensure_openclaw_on_immich_network
    # Read env vars: prefer host .env, fallback to container (set in docker-compose.yml)
    IMMICH_URL_VAL="$(immich_server_url)"
    IMMICH_KEY_VAL="$(immich_api_key)"
    if [[ -z "${IMMICH_KEY_VAL}" ]]; then
      echo "ERROR: IMMICH_API_KEY 未配置。"
      echo "请在 Immich 后台创建 API Key 后，写入 $APP_DIR/.env 的 IMMICH_API_KEY，再重试本命令。"
      exit 1
    fi
    # immich-mcp requires IMMICH_BASE_URL in the form http://<host>:<port>/api
    docker_cmd exec openclaw node dist/index.js mcp set immich \
      "{\"enabled\":true,\"command\":\"npx\",\"args\":[\"-y\",\"immich-mcp\"],\"env\":{\"IMMICH_BASE_URL\":\"${IMMICH_URL_VAL}/api\",\"IMMICH_API_KEY\":\"${IMMICH_KEY_VAL}\"}}"
    docker_cmd exec openclaw node dist/index.js mcp reload
    docker_cmd restart openclaw
    echo "Immich MCP tools configured."
    echo "  IMMICH_BASE_URL: ${IMMICH_URL_VAL}/api"
    echo "Seeding built-in sample photos into Immich..."
    import_sample_photos_to_immich
    if [[ "${IMMICH_SAMPLE_CREATED:-0}" -gt 0 || "${IMMICH_SAMPLE_DUPLICATE:-0}" -gt 0 ]]; then
      echo "Triggering Immich indexing jobs..."
      trigger_immich_jobs
    fi
    echo "Completed: Immich Finished"
    ;;
  tools-immich-show)
    docker_cmd exec openclaw node dist/index.js mcp show immich --json
    ;;
  tools-kb-setup)
    ensure_repo_logs_dir_writable
    mkdir -p $NAS_ROOT/tools
    mkdir -p $NAS_ROOT/knowledge_base_data
    sync_sample_docs
    cp "$APP_DIR/knowledge_base/kb_mcp.js" $NAS_ROOT/tools/kb_mcp.js

    if ! docker_cmd ps --format '{{.Names}}' | grep -qx knowledge_base; then
      if docker_cmd ps -a --format '{{.Names}}' | grep -qx knowledge_base; then
        docker_cmd start knowledge_base >/dev/null
      else
        docker_cmd build -t nas-knowledge-base:local "$APP_DIR/knowledge_base"
        docker_cmd run -d \
          --name knowledge_base \
          --restart unless-stopped \
          -e NAS_ROOT=/nas_share \
          -e PORT=8084 \
          -e SCAN_INTERVAL=300 \
          -e LOG_FILE=/logs/knowledge_base.log \
          -e LOG_MAX_BYTES=5242880 \
          -e LOG_BACKUPS=3 \
          -v $NAS_ROOT:/nas_share:ro \
          -v $NAS_ROOT/knowledge_base_data:/data \
          -v $APP_DIR/logs:/logs \
          -p 28084:8084 \
          nas-knowledge-base:local >/dev/null
      fi
    fi

    # 让 openclaw 能按容器名直接访问 KB 服务（host.docker.internal 在本机不可达）
    if docker_cmd network inspect big-bear-immich_big_bear-immich_network >/dev/null 2>&1; then
      if ! docker_cmd network inspect big-bear-immich_big_bear-immich_network \
          --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null | grep -q ' knowledge_base'; then
        docker_cmd network connect big-bear-immich_big_bear-immich_network knowledge_base
      fi
    fi

    IMMICH_URL_VAL="$(immich_server_url)"
    IMMICH_KEY_VAL="$(immich_api_key)"

    docker_cmd exec openclaw node dist/index.js mcp set kb_search \
      "{\"enabled\":true,\"command\":\"node\",\"args\":[\"/nas_share/tools/kb_mcp.js\"],\"env\":{\"KB_API_URL\":\"http://knowledge_base:8084\",\"IMMICH_BASE_URL\":\"${IMMICH_URL_VAL}\",\"IMMICH_API_KEY\":\"${IMMICH_KEY_VAL}\",\"MCP_LOG_FILE\":\"/logs/mcp_kb_search.log\",\"LOG_MAX_BYTES\":\"5242880\",\"LOG_BACKUPS\":\"3\"}}"
    docker_cmd exec openclaw node dist/index.js mcp reload
    docker_cmd restart openclaw
    if [[ "${SAMPLE_DOCS_CHANGED:-0}" -gt 0 ]]; then
      curl -fsS -X POST http://127.0.0.1:28084/rescan >/dev/null 2>&1 || true
    fi
    echo "Knowledge base configured."
    echo "  KB API:   http://knowledge_base:8084"
    echo "  Indexed:  /nas_share (excludes tools/ Immich上传/)"
    echo "  Photos:   Immich CLIP (${IMMICH_URL_VAL})"
    echo "  Sample docs: /nas_share/文档"
    ;;
  tools-kb-show)
    docker_cmd exec openclaw node dist/index.js mcp show kb_search --json
    ;;
  tools-sync)
    # 统一同步：NAS-Demo（git 唯一源码）→ nas_share/tools（容器运行副本）
    # 并清理遗留副本；MCP 脚本有变更时重启 openclaw 使配置生效。
    mkdir -p $NAS_ROOT/tools

    synced=0
    changed=0
    for entry in \
      "local_voice_chat/download_media_mcp.js" \
      "local_voice_chat/image_batch.py" \
      "local_voice_chat/nas_classify.py" \
      "knowledge_base/kb_mcp.js"; do
      src="$APP_DIR/$entry"
      dst="$NAS_ROOT/tools/$(basename "$entry")"
      if [[ ! -f "$src" ]]; then
        echo "SKIP  $entry (源不存在)"
        continue
      fi
      if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        echo "SAME  $entry"
        continue
      fi
      cp "$src" "$dst"
      echo "SYNC  $entry -> $dst"
      synced=$((synced + 1))
      case "$entry" in
        *download_media_mcp.js|*kb_mcp.js) changed=1 ;;
      esac
    done

    # 清理遗留副本（实际运行在 NAS-Demo，nas_share/tools 内无人使用）
    for legacy in hotwords.txt voice_bridge.py; do
      if [[ -f "$NAS_ROOT/tools/$legacy" ]]; then
        rm -f "$NAS_ROOT/tools/$legacy"
        echo "RM    $legacy (遗留副本，已清理)"
      fi
    done

    if [[ "$changed" -eq 1 ]]; then
      docker_cmd exec openclaw node dist/index.js mcp reload || true
      docker_cmd restart openclaw
      echo "MCP 脚本有变更，已重启 openclaw"
    else
      echo "MCP 脚本无变更，无需重启 openclaw"
    fi
    echo "tools-sync done: $synced file(s) synced"
    ;;
  tools-photos-setup)
    # 同步仓库内置样例照片到 NAS 相册，并尽量自动导入 Immich，方便直接试照片语义能力。
    # 目标：同时写入中文和英文测试目录，便于中英文语音指令联调。
    #   - $NAS_ROOT/家庭相册/测试样例
    #   - $NAS_ROOT/Family album/Test Samples
    #   - $NAS_ROOT/Travel/Test Samples
    src_dir="$APP_DIR/assets/sample_photos"
    dest_dirs=(
      "$NAS_ROOT/家庭相册/测试样例"
      "$NAS_ROOT/Family album/Test Samples"
      "$NAS_ROOT/Travel/Test Samples"
    )
    album_roots=(
      "$NAS_ROOT/家庭相册"
      "$NAS_ROOT/Family album"
      "$NAS_ROOT/Travel"
    )
    if [[ ! -d "$src_dir" ]]; then
      echo "SKIP  sample_photos 源目录不存在: $src_dir"
      exit 0
    fi
    for dest_dir in "${dest_dirs[@]}"; do
      mkdir -p "$dest_dir"
    done
    photos_copied=0
    for src in "$src_dir"/*.jpg; do
      [[ -f "$src" ]] || continue
      name="$(basename "$src")"
      for dest_dir in "${dest_dirs[@]}"; do
        dst="$dest_dir/$name"
        rel_dir="${dest_dir#"$NAS_ROOT/"}"
        if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
          echo "SAME  $name -> ${rel_dir}/"
          continue
        fi
        cp "$src" "$dst"
        echo "SYNC  $name -> ${rel_dir}/"
        photos_copied=$((photos_copied + 1))
      done
    done
    # 目录权限对齐宿主用户（避免容器以其它 uid 创建后无法写入）
    if [[ "${EUID}" -eq 0 ]]; then
      for album_root in "${album_roots[@]}"; do
        chown -R "$(id -u):$(id -g)" "$album_root" 2>/dev/null || true
      done
    else
      for album_root in "${album_roots[@]}"; do
        sudo chown -R "$(id -u):$(id -g)" "$album_root" 2>/dev/null || true
      done
    fi
    echo "Importing sample photos into Immich..."
    import_sample_photos_to_immich
    if [[ "${IMMICH_SAMPLE_CREATED:-0}" -gt 0 || "${IMMICH_SAMPLE_DUPLICATE:-0}" -gt 0 ]]; then
      echo "Triggering Immich indexing jobs..."
      trigger_immich_jobs
    fi
    echo "tools-photos-setup done: $photos_copied file sync action(s) across Chinese/English sample folders"
    ;;
  pair-list)
    docker_cmd exec -it openclaw node dist/index.js devices list
    ;;
  pair-approve)
    if [[ -z "${2:-}" ]]; then
      echo "Missing request_id"
      usage
      exit 1
    fi
    req="$(clean_request_id "$2")"
    docker_cmd exec -it openclaw node dist/index.js devices approve "$req"
    ;;
  openclaw-app-deploy)
    OPENCLAW_APP_COMPOSE="${APP_DIR}/openclaw-compose.yml"
    if [[ ! -f "${OPENCLAW_APP_COMPOSE}" && -f "${APP_DIR}/openclaw-app-compose.yml" ]]; then
      OPENCLAW_APP_COMPOSE="${APP_DIR}/openclaw-app-compose.yml"
    fi
    if [[ ! -f "${OPENCLAW_APP_COMPOSE}" ]]; then
      echo "ERROR: ${OPENCLAW_APP_COMPOSE} not found"
      exit 1
    fi
    portal_token="${OPENCLAW_GATEWAY_TOKEN:-casaos}"
    portal_token_escaped="$(printf '%s' "$portal_token" | sed 's/[\/&]/\\&/g')"
    if [[ "${EUID}" -eq 0 ]]; then
      mkdir -p /DATA/AppData/openclaw-portal
      sed "s/__OPENCLAW_TOKEN__/${portal_token_escaped}/g" "${APP_DIR}/redirect/index.html" > /DATA/AppData/openclaw-portal/index.html
    else
      sudo mkdir -p /DATA/AppData/openclaw-portal
      sed "s/__OPENCLAW_TOKEN__/${portal_token_escaped}/g" "${APP_DIR}/redirect/index.html" | sudo tee /DATA/AppData/openclaw-portal/index.html >/dev/null
    fi
    if has_casaos_cli; then
      existing_appid="$(first_existing_casaos_appid openclaw-app org.local.openclaw.portal openclaw openclaw-portal || true)"
      if [[ -n "$existing_appid" ]]; then
        if casaos-cli app-management apply "$existing_appid" -f "${OPENCLAW_APP_COMPOSE}"; then
          msg="updated in CasaOS"
        else
          echo "WARN: apply failed, reinstall OpenClaw launcher app..."
          casaos-cli app-management uninstall "$existing_appid" --no-remove-config || true
          remove_containers_if_exist openclaw_portal
          casaos-cli app-management install -f "${OPENCLAW_APP_COMPOSE}"
          msg="reinstalled in CasaOS"
        fi
      else
        if casaos-cli app-management install -f "${OPENCLAW_APP_COMPOSE}"; then
          msg="installed to CasaOS"
        else
          echo "WARN: install failed, retry after removing old openclaw_portal container..."
          remove_containers_if_exist openclaw_portal
          casaos-cli app-management install -f "${OPENCLAW_APP_COMPOSE}"
          msg="installed to CasaOS (after migration)"
        fi
      fi
    else
      echo "WARN: casaos-cli not found, fallback to docker compose"
      docker_cmd compose -f "${OPENCLAW_APP_COMPOSE}" up -d
      msg="deployed by docker compose"
    fi
    ip="$(host_primary_ip)"
    echo "OpenClaw launcher ${msg}. Open http://${ip:-<your-host-ip>}:28086"
    ;;
  immich-apply)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)"
    IMMICH_COMPOSE="${SCRIPT_DIR}/immich-compose.yml"
    IMMICH_COMPOSE_RENDERED="$(render_template_file "${IMMICH_COMPOSE}")"
    if [[ ! -f "${IMMICH_COMPOSE}" ]]; then
      echo "ERROR: ${IMMICH_COMPOSE} not found"
      exit 1
    fi
    if has_casaos_cli; then
      existing_appid="$(first_existing_casaos_appid big-bear-immich com.bigbeartechworld.immich || true)"
      if [[ -n "$existing_appid" ]]; then
        echo "Applying ${IMMICH_COMPOSE} to CasaOS (${existing_appid})..."
        casaos-cli app-management apply "$existing_appid" -f "${IMMICH_COMPOSE_RENDERED}"
        echo "Done. CasaOS Immich config updated (changes applied asynchronously)."
      else
        echo "Installing Immich app to CasaOS..."
        if casaos-cli app-management install -f "${IMMICH_COMPOSE_RENDERED}"; then
          echo "Done. CasaOS Immich installed (asynchronous)."
        else
          echo "WARN: install failed, retry after migrating existing immich containers..."
          remove_containers_if_exist immich-server immich-machine-learning immich-postgres immich-redis
          casaos-cli app-management install -f "${IMMICH_COMPOSE_RENDERED}"
          echo "Done. CasaOS Immich installed (after migration)."
        fi
      fi
    else
      echo "WARN: casaos-cli not found, fallback to docker compose"
      docker_cmd compose -f "${IMMICH_COMPOSE_RENDERED}" up -d
      echo "Immich deployed by docker compose."
    fi
    rm -f "${IMMICH_COMPOSE_RENDERED}"
    ;;
  immich-show)
    casaos-cli app-management show local big-bear-immich --yaml 2>&1
    ;;
  immich-sync-jobs)
    echo "Triggering Immich indexing jobs..."
    trigger_immich_jobs
    echo "Jobs triggered. New photos will be indexed shortly."
    ;;
  jellyfin-deploy)
    JELLYFIN_COMPOSE="${APP_DIR}/jellyfin-compose.yml"
    JELLYFIN_COMPOSE_RENDERED="$(render_template_file "${JELLYFIN_COMPOSE}")"
    if [[ ! -f "${JELLYFIN_COMPOSE}" ]]; then
      echo "ERROR: ${JELLYFIN_COMPOSE} not found"
      exit 1
    fi
    if has_casaos_cli; then
      existing_appid="$(first_existing_casaos_appid jellyfin org.jellyfin.server || true)"
      if [[ -n "$existing_appid" ]]; then
        casaos-cli app-management apply "$existing_appid" -f "${JELLYFIN_COMPOSE_RENDERED}"
        msg="updated in CasaOS (asynchronous)"
      else
        if casaos-cli app-management install -f "${JELLYFIN_COMPOSE_RENDERED}"; then
          msg="installed to CasaOS (asynchronous)"
        else
          echo "WARN: install failed, retry after migrating existing jellyfin container..."
          remove_containers_if_exist jellyfin
          casaos-cli app-management install -f "${JELLYFIN_COMPOSE_RENDERED}"
          msg="installed to CasaOS (after migration)"
        fi
      fi
    else
      echo "WARN: casaos-cli not found, fallback to docker compose"
      docker_cmd compose -f "${JELLYFIN_COMPOSE_RENDERED}" up -d
      msg="deployed by docker compose"
    fi
    rm -f "${JELLYFIN_COMPOSE_RENDERED}"
    ip="$(host_primary_ip)"
    echo "Jellyfin ${msg}. Open http://${ip:-<your-host-ip>}:8096"
    echo "First run: 建库时选 /media 下的子目录，建议关闭 Admin > Playback > Transcoding"
    ;;
  jellyfin-show)
    docker_cmd ps -a --filter name=jellyfin --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    ;;
  jellyfin-key-check)
    check_jellyfin_api_key "${2:-}"
    ;;
  jellyfin-apply)
    echo "Applying Jellyfin config from .env to voice-bridge..."
    if ! command -v systemctl >/dev/null 2>&1; then
      echo "ERROR: systemctl not found, please restart voice-bridge manually"
      exit 1
    fi
    if [[ "${EUID}" -eq 0 ]]; then
      systemctl restart voice-bridge
    else
      sudo systemctl restart voice-bridge
    fi
    check_jellyfin_api_key "${2:-}"
    ;;
  mic-mode)
    mode="${2:-status}"
    if [[ "$mode" == "status" ]]; then
      mic_mode_status
    else
      mic_mode_apply "$mode"
    fi
    ;;
  voice-assistant-deploy)
    VOICE_COMPOSE="${APP_DIR}/voice-assistant-compose.yml"
    if [[ ! -f "${VOICE_COMPOSE}" ]]; then
      echo "ERROR: ${VOICE_COMPOSE} not found"
      exit 1
    fi
    VOICE_COMPOSE_RENDERED="$(render_template_file "${VOICE_COMPOSE}")"
    if has_casaos_cli; then
      existing_appid="$(first_existing_casaos_appid voice-assistant org.local.voice.assistant || true)"
      if [[ -n "$existing_appid" ]]; then
        if casaos-cli app-management apply "$existing_appid" -f "${VOICE_COMPOSE_RENDERED}"; then
          msg="updated in CasaOS (asynchronous)"
        else
          echo "WARN: apply failed, retry after migrating existing voice_assistant container..."
          remove_containers_if_exist voice_assistant
          casaos-cli app-management apply "$existing_appid" -f "${VOICE_COMPOSE_RENDERED}" || \
            casaos-cli app-management install -f "${VOICE_COMPOSE_RENDERED}"
          msg="updated in CasaOS (after migration)"
        fi
      else
        if casaos-cli app-management install -f "${VOICE_COMPOSE_RENDERED}"; then
          msg="installed to CasaOS (asynchronous)"
        else
          echo "WARN: install failed, retry after migrating existing voice_assistant container..."
          remove_containers_if_exist voice_assistant
          casaos-cli app-management install -f "${VOICE_COMPOSE_RENDERED}"
          msg="installed to CasaOS (after migration)"
        fi
      fi
    else
      echo "WARN: casaos-cli not found, fallback to docker compose"
      docker_cmd compose -f "${VOICE_COMPOSE_RENDERED}" up -d
      msg="deployed by docker compose"
    fi
    rm -f "${VOICE_COMPOSE_RENDERED}"
    ip="$(host_primary_ip)"
    echo "Voice Assistant ${msg}. Open http://${ip:-<your-host-ip>}:28083"
    ;;
  voice-assistant-show)
    docker_cmd ps -a --filter name=voice_assistant --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    ;;
  nas-files-deploy)
    FB_COMPOSE="${APP_DIR}/filebrowser-compose.yml"
    FB_COMPOSE_RENDERED="$(render_template_file "${FB_COMPOSE}")"
    fb_msg=""
    if [[ ! -f "${FB_COMPOSE}" ]]; then
      echo "ERROR: ${FB_COMPOSE} not found"
      exit 1
    fi
    # 预先创建 config/database 目录（容器以 uid 1001 运行，目录需可写）
    mkdir -p /DATA/AppData/filebrowser/config /DATA/AppData/filebrowser/database
    chmod 777 /DATA/AppData/filebrowser/config /DATA/AppData/filebrowser/database
    if has_casaos_cli; then
      existing_appid="$(first_existing_casaos_appid nas-filebrowser org.local.nas.filebrowser || true)"
      if [[ -n "$existing_appid" ]]; then
        if casaos-cli app-management apply "$existing_appid" -f "${FB_COMPOSE_RENDERED}"; then
          fb_msg="updated in CasaOS (asynchronous)"
        else
          echo "WARN: apply failed, retry after migrating existing filebrowser container..."
          remove_containers_if_exist filebrowser
          casaos-cli app-management apply "$existing_appid" -f "${FB_COMPOSE_RENDERED}" || \
            casaos-cli app-management install -f "${FB_COMPOSE_RENDERED}"
          fb_msg="updated in CasaOS (after migration)"
        fi
      else
        if casaos-cli app-management install -f "${FB_COMPOSE_RENDERED}"; then
          fb_msg="installing via CasaOS (asynchronous)"
        else
          echo "WARN: install failed, retry after migrating existing filebrowser container..."
          remove_containers_if_exist filebrowser
          casaos-cli app-management install -f "${FB_COMPOSE_RENDERED}"
          fb_msg="installed to CasaOS (after migration)"
        fi
      fi

      # CasaOS occasionally stores /srv source as /tmp/casaos-compose-app-*/... (invalid after temp cleanup).
      # If detected, force uninstall+install to self-heal.
      for _ in $(seq 1 20); do
        srv_source="$(docker_cmd inspect filebrowser --format '{{range .Mounts}}{{if eq .Destination "/srv"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"
        if [[ -n "$srv_source" ]]; then
          break
        fi
        sleep 1
      done
      if [[ "$srv_source" == /tmp/casaos-compose-app-* ]]; then
        echo "WARN: detected invalid /srv mount ($srv_source), reinstalling nas-filebrowser..."
        fb_appid="$(first_existing_casaos_appid nas-filebrowser org.local.nas.filebrowser || true)"
        if [[ -n "$fb_appid" ]]; then
          casaos-cli app-management uninstall "$fb_appid" --no-remove-config || true
        fi
        remove_containers_if_exist filebrowser
        casaos-cli app-management install -f "${FB_COMPOSE_RENDERED}"
        fb_msg="reinstalled in CasaOS (mount self-healed)"
      fi
    else
      echo "WARN: casaos-cli not found, fallback to docker compose"
      docker_cmd compose -f "${FB_COMPOSE_RENDERED}" up -d
      fb_msg="deployed by docker compose"
    fi
    rm -f "${FB_COMPOSE_RENDERED}"
    ip="$(host_primary_ip)"
    # 免登录：等数据库初始化后，停止容器 -> 写入 noauth -> 再启动（读写由 uid 1001 与可写挂载保证）
    for _ in $(seq 1 40); do
      if [ -f /DATA/AppData/filebrowser/database/filebrowser.db ]; then
        docker_cmd stop filebrowser >/dev/null 2>&1 || true
        docker_cmd run --rm --user 1001:1001 --entrypoint /bin/filebrowser \
          -v /DATA/AppData/filebrowser/database:/database \
          "$FILEBROWSER_IMAGE" \
          -d /database/filebrowser.db config set --auth.method=noauth >/dev/null 2>&1 || true
        docker_cmd start filebrowser >/dev/null 2>&1 || true
        echo "Auth disabled (no login)."
        break
      fi
      sleep 2
    done
    [[ -n "$fb_msg" ]] && echo "NAS file browser ${fb_msg}."
    echo "Open http://${ip:-<your-host-ip>}:28085"
    ;;
  nas-files-show)
    docker_cmd ps -a --filter name=filebrowser --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    ;;
  docker-mirror)
    # 配置 Docker 镜像加速（幂等）：解决直连 Docker Hub 拉取大镜像被重置导致的
    # 「小镜像能装、大镜像反复失败」问题。脚本内部会备份配置、校验 JSON 后重启 docker。
    MIRROR_SCRIPT="${APP_DIR}/setup_docker_mirror.sh"
    if [[ ! -f "$MIRROR_SCRIPT" ]]; then
      echo "ERROR: 未找到 ${MIRROR_SCRIPT}"
      exit 1
    fi
    if [[ "${EUID}" -eq 0 ]]; then
      bash "$MIRROR_SCRIPT"
    else
      sudo bash "$MIRROR_SCRIPT"
    fi
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "Unknown command: $1"
    usage
    exit 1
    ;;
esac