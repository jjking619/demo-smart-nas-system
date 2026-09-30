#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_SRC="$SCRIPT_DIR/voice-bridge.service"
SERVICE_DST="/etc/systemd/system/voice-bridge.service"

if [[ -f "$APP_DIR/.env" ]]; then
  set -a
  source "$APP_DIR/.env"
  set +a
fi

if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  RUN_USER="$SUDO_USER"
else
  RUN_USER="$(id -un)"
fi

if ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx audio; then
  if sudo usermod -aG audio "$RUN_USER" 2>/dev/null; then
    echo "[install] $RUN_USER 已加入 audio 组（可访问 USB 麦克风）"
  else
    echo "[install][WARN] 无法将 $RUN_USER 加入 audio 组，USB 麦克风录音可能不可用" >&2
  fi
fi

# voice-bridge 以 $RUN_USER 身份运行，需要非交互调用 docker（与 openclaw 容器交互）。
# systemd 服务没有 tty，`sudo docker` 会因需要密码而必然失败，因此必须依赖 docker 组。
if ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx docker; then
  if sudo usermod -aG docker "$RUN_USER" 2>/dev/null; then
    echo "[install] $RUN_USER 已加入 docker 组（语音桥可非交互调用 Docker）"
  else
    echo "[install][WARN] 无法将 $RUN_USER 加入 docker 组，语音桥可能报 'No non-interactive Docker access'" >&2
  fi
fi

env_ensure() {
  local key="$1" value="$2"
  local cur new_line

  cur="$(grep -E "^[[:space:]]*${key}=" "$APP_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"

  if [[ -n "$cur" && "$cur" != "auto" && "$cur" != "default" ]]; then
    new_line="${key}=${cur}"
  else
    new_line="${key}=${value}"
  fi

  awk -v k="$key" -v nl="$new_line" '
    {
      if ($0 ~ "^[[:space:]]*" k "=" || $0 ~ "^[[:space:]]*#[[:space:]]*" k "=") {
        if (!done) { print nl; done = 1 }
        next
      }
      print
    }
    END { if (!done) print nl }
  ' "$APP_DIR/.env" > "$APP_DIR/.env.tmp" && mv "$APP_DIR/.env.tmp" "$APP_DIR/.env"
}
# 录音设备策略：优先 USB；仅在 USB 不可用时回退到板载麦。
env_ensure VOICE_MIC_PRIORITY "usb,onboard"
env_ensure VOICE_MIC_USB_BACKEND "alsa"
env_ensure VOICE_MIC_USB_INPUT "plughw:Audio,0"
env_ensure VOICE_MIC_ONBOARD_BACKEND "pulse"
env_ensure VOICE_MIC_ONBOARD_INPUT "regular0"
env_ensure VOICE_MIC_RECHECK_SEC "60"
env_ensure VOICE_MIC_STRICT "0"

# 兼容旧版本配置项（运行时可被 VOICE_MIC_* 优先策略覆盖）。
env_ensure VOICE_RECORD_BACKEND "auto"
env_ensure VOICE_MIC_INPUT "default"
env_ensure VOICE_HTTP_WAKE_ERROR_BACKOFF_INITIAL_SEC "1.0"
env_ensure VOICE_HTTP_WAKE_ERROR_BACKOFF_MAX_SEC "30.0"
env_ensure VOICE_HTTP_WAKEWORD "0"
env_ensure VOICE_HTTP_KEEP_MODELS "0"

# install 脚本常由 sudo 调用，避免 .env 被 root 接管导致后续无法编辑。
if [[ -f "$APP_DIR/.env" ]]; then
  chown "$RUN_USER":"$RUN_USER" "$APP_DIR/.env" 2>/dev/null || true
fi

RUN_USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

detect_python() {
  local cand py user_site
  cand="$(command -v python3 || true)"
  user_site="$(find "$RUN_USER_HOME/.local/lib" -maxdepth 2 -name site-packages -type d 2>/dev/null | head -1)"
  for py in "$cand" "$RUN_USER_HOME/.pyenv/versions/"*/bin/python3 \
            /usr/bin/python3 /usr/local/bin/python3; do
    [[ -n "$py" && -x "$py" ]] || continue
    if [[ -n "$user_site" ]]; then
      if PYTHONPATH="$user_site" "$py" -c "import numpy, sherpa_onnx" >/dev/null 2>&1; then
        echo "$py"
        return 0
      fi
    elif "$py" -c "import numpy, sherpa_onnx" >/dev/null 2>&1; then
      echo "$py"
      return 0
    fi
  done
  return 1
}
PYTHON_BIN="$(detect_python)" || { echo "[install][ERROR] 找不到装有 numpy/sherpa_onnx 的 python3，请先安装依赖" >&2; exit 1; }
VOICE_SDK_BASE="${VOICE_SDK_BASE:-$RUN_USER_HOME/voice}"

MODEL_ASR_ROOT="${VOICE_SDK_BASE}/asr"
MODEL_TTS_ROOT="${VOICE_SDK_BASE}/tts"
echo "[install] 检查语音模型（SenseVoice / TTS，缺失将自动预下载，首次视网络需数分钟）..."
if ( cd "$SCRIPT_DIR" && PYTHONPATH="${APP_DIR}:${SCRIPT_DIR}" "$PYTHON_BIN" - "$MODEL_ASR_ROOT" "$MODEL_TTS_ROOT" <<'PY'
import sys
from pathlib import Path

asr_root = Path(sys.argv[1])
tts_root = Path(sys.argv[2])
asr_root.mkdir(parents=True, exist_ok=True)
tts_root.mkdir(parents=True, exist_ok=True)

from local_voice_chat import (
    ensure_sensevoice_model,
    ensure_official_matcha_tts,
)

# 默认使用 SenseVoice：中文/英文命令与唤醒都走同一模型，降低双引擎路径复杂度。
cmd_model = ensure_sensevoice_model(asr_root / "model", force_download=True)
print(f"[install][模型] wake 模型就绪: {Path(cmd_model).name}")
print(f"[install][模型] command 模型就绪: {Path(cmd_model).name}")

model_dir, vocoder = ensure_official_matcha_tts(tts_root, force_download=True)
print(f"[install][模型] TTS 模型就绪: {model_dir.name} + {Path(vocoder).name}")
PY
); then
  echo "[install] 已准备 wake 模型 / command 模型 / TTS 模型"
  echo "[install] 语音模型目录：$MODEL_ASR_ROOT + $MODEL_TTS_ROOT"
else
  echo "[install][WARN] 语音模型预下载失败（可稍后重跑本脚本补齐）；" >&2
  echo "[install][WARN] 首次语音唤醒时可能需要现场下载，期间暂无法唤醒。" >&2
fi

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[\/&]/\\&/g'
}

if [[ ! -f "$SERVICE_SRC" ]]; then
  echo "service file not found: $SERVICE_SRC"
  exit 1
fi

run_user_escaped="$(escape_sed_replacement "$RUN_USER")"
app_dir_escaped="$(escape_sed_replacement "$APP_DIR")"
python_bin_escaped="$(escape_sed_replacement "$PYTHON_BIN")"
voice_sdk_base_escaped="$(escape_sed_replacement "$VOICE_SDK_BASE")"

tmp_service="$(mktemp /tmp/voice-bridge.service.XXXXXX)"
sed \
  -e "s#__RUN_USER__#${run_user_escaped}#g" \
  -e "s#__APP_DIR__#${app_dir_escaped}#g" \
  -e "s#__PYTHON_BIN__#${python_bin_escaped}#g" \
  -e "s#__VOICE_SDK_BASE__#${voice_sdk_base_escaped}#g" \
  "$SERVICE_SRC" > "$tmp_service"

sudo cp "$tmp_service" "$SERVICE_DST"
rm -f "$tmp_service"
sudo systemctl daemon-reload
sudo systemctl enable --now voice-bridge
# enable --now 不会重启已运行实例；显式 restart 让 audio 组与 .env 新配置立即生效
sudo systemctl restart voice-bridge

echo "voice-bridge service installed and started"
sudo systemctl status --no-pager --lines=20 voice-bridge || true
