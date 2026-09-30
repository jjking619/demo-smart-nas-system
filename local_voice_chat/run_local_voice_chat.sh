#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! python3 - <<'PY' >/dev/null 2>&1
import sherpa_onnx  # noqa: F401
import numpy  # noqa: F401
PY
then
  echo "[SETUP] Installing Python deps: sherpa-onnx, numpy"
  python3 -m pip install --user sherpa-onnx numpy
fi

exec python3 "$SCRIPT_DIR/local_voice_chat.py" "$@"
