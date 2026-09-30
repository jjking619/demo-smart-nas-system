#!/usr/bin/env python3
import argparse
import gc
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from local_voice_chat import (
    asr_transcribe,
    build_asr_recognizer,
    build_tts,
    check_cmd_exists,
    ensure_sensevoice_model,
    get_default_mic_resolver,
    record_audio_auto_backend,
    record_speech_until_silence,
    tts_speak,
    wav_level_dbfs,
)

try:
    from local_voice_chat.log_runtime import append_line, setup_stdout_stderr_tee
except Exception:  # noqa: BLE001
    from log_runtime import append_line, setup_stdout_stderr_tee


def load_runtime_env(env_file: str | Path | None = None) -> dict[str, str]:
    """Load repo-level .env values without overriding already-exported process env.

    This keeps the project portable across shell, systemd, and tests while still
    treating explicit process environment variables as authoritative.
    """
    candidates = []
    if env_file is not None:
        candidates.append(Path(env_file))
    repo_root = Path(__file__).resolve().parent.parent
    candidates.extend([
        repo_root / ".env",
        Path.cwd() / ".env",
        Path.home() / ".env",
    ])

    path = next((p for p in candidates if p and p.exists()), None)
    if path is None:
        return {}

    values: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            final_value = os.environ.get(key, value)
            values[key] = final_value
            os.environ[key] = final_value
    except OSError:
        return {}
    return values


load_runtime_env()
REPO_ROOT = Path(__file__).resolve().parent.parent
NAS_ROOT = Path(os.getenv("NAS_ROOT", str(Path.home() / "nas_share"))).expanduser()
DEFAULT_LOG_FILE = REPO_ROOT / "logs" / "voice_bridge.log"
DEFAULT_WAKE_WORDS = ["小远同学", "xiaoyuan", "小远", "小元同学", "小园同学", "小源同学", "小袁同学"]


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_wake_words(value: str) -> list[str]:
    parts = re.split(r"[,，;；\n]+", value or "")
    out = []
    seen = set()
    for part in parts:
        w = part.strip()
        if not w or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _normalize_for_wake(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text)
    return text


def _detect_wakeup_open_asr(recognizer, wav_path: Path, wake_words: list[str], engine: str):
    text = asr_transcribe(recognizer, wav_path, engine=engine)
    norm_text = _normalize_for_wake(text)
    for word in wake_words:
        norm_word = _normalize_for_wake(word)
        if norm_word and norm_word in norm_text:
            return True, word, text
    return False, "", text


def _is_wakeword_only_text(text: str | None, wake_words: list[str]) -> bool:
    """Return True when ASR text is effectively just the wake word itself."""
    norm_text = _normalize_for_wake(text or "")
    if not norm_text:
        return False
    for word in wake_words:
        norm_word = _normalize_for_wake(word)
        if not norm_word:
            continue
        if norm_text == norm_word:
            return True
    return False


def _resolve_sdk_root(folder_name: str) -> Path:
    env_base = os.getenv("VOICE_SDK_BASE")
    candidates = []
    if env_base:
        candidates.append(Path(env_base) / folder_name)

    candidates.append(REPO_ROOT / folder_name)
    candidates.append(Path.home() / "voice" / folder_name)

    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def parse_args():
    asr_root = _resolve_sdk_root("asr")
    tts_root = _resolve_sdk_root("tts")

    parser = argparse.ArgumentParser(
        description="Voice bridge: open-asr wake -> ASR -> OpenClaw -> TTS"
    )
    parser.add_argument("--asr-root", default=str(asr_root))
    parser.add_argument("--tts-root", default=str(tts_root))

    parser.add_argument(
        "--wake-words",
        default=os.getenv("VOICE_WAKE_WORDS", "小远同学,xiaoyuan"),
        help="Comma-separated wake words detected via open-source ASR text matching",
    )

    parser.add_argument("--wake-duration", type=float, default=4.0)
    parser.add_argument(
        "--wake-min-level-dbfs",
        type=float,
        default=float(os.getenv("VOICE_WAKE_MIN_LEVEL_DBFS", "-75.0")),
        help="忽略低于该音量阈值的唤醒片段；该值越低越灵敏。"
        "对低噪声 USB 麦克风，默认应当足够宽松，避免近距离说话也被提前过滤掉。",
    )
    parser.add_argument(
        "--wake-low-level-dbfs",
        type=float,
        default=float(os.getenv("VOICE_WAKE_LOW_LEVEL_DBFS", "-55.0")),
        help="Wake录音低于该电平且首次未命中时，触发一次增益重试",
    )
    parser.add_argument(
        "--wake-boost-db",
        type=float,
        default=float(os.getenv("VOICE_WAKE_BOOST_DB", "6.0")),
        help="低电平重试时的增益(dB)，设为0可关闭",
    )
    parser.add_argument(
        "--record-backend",
        choices=["auto", "pulse", "alsa"],
        default=os.getenv("VOICE_RECORD_BACKEND", "auto"),
    )
    parser.add_argument("--mic-input", default=os.getenv("VOICE_MIC_INPUT", "default"))

    parser.add_argument("--speech-duration", type=float, default=15.0)
    parser.add_argument("--speech-min-duration", type=float, default=1.5)
    parser.add_argument("--speech-tail-window", type=float, default=0.8)
    parser.add_argument("--speech-silence-threshold-dbfs", type=float, default=-45.0)

    parser.add_argument("--asr-language", default="")
    parser.add_argument("--asr-model", default="")
    parser.add_argument("--no-auto-download-asr", action="store_true")
    parser.add_argument(
        "--asr-engine",
        choices=["sensevoice", "conformer"],
        default="",
        help="(legacy) set both wake+command engines at once",
    )
    parser.add_argument(
        "--wake-asr-engine",
        choices=["sensevoice", "conformer"],
        default=os.getenv("VOICE_WAKE_ASR_ENGINE", "sensevoice"),
        help="ASR engine for wake-word detection",
    )
    parser.add_argument(
        "--command-asr-engine",
        choices=["sensevoice", "conformer"],
        default=os.getenv("VOICE_COMMAND_ASR_ENGINE", "sensevoice"),
        help="ASR engine for command recognition",
    )
    parser.add_argument(
        "--download-conformer-model",
        action="store_true",
        default=str(os.getenv("VOICE_DOWNLOAD_CONFORMER_MODEL", "")).strip().lower() in {"1", "true", "yes", "y", "on"},
        help="默认不预下载 Conformer；显式启用时才下载热词模型",
    )
    parser.add_argument(
        "--wake-asr-language",
        default=os.getenv("VOICE_WAKE_ASR_LANGUAGE", "zh"),
        help="ASR language hint for wake recognizer",
    )
    parser.add_argument(
        "--command-asr-language",
        default=os.getenv("VOICE_COMMAND_ASR_LANGUAGE", "auto"),
        help="ASR language hint for command recognizer",
    )
    parser.add_argument("--wake-asr-model", default="", help="Optional wake ASR model path override")
    parser.add_argument("--command-asr-model", default="", help="Optional command ASR model path override")
    parser.add_argument("--hotwords-file", default="", help="热词文件路径，默认自动生成 hotwords.txt")
    parser.add_argument("--hotwords-score", type=float, default=3.0, help="热词增益分数；唤醒词已加入热词表，3.0 左右均衡")

    parser.add_argument("--session-idle-rounds", type=int, default=3)

    parser.add_argument("--openclaw-container", default="openclaw")
    parser.add_argument("--openclaw-session-key", default="agent:main:voice-bridge-v2")
    parser.add_argument("--openclaw-timeout", type=int, default=300)
    parser.add_argument("--openclaw-dry-run", action="store_true")

    parser.add_argument("--jellyfin-url", default="http://127.0.0.1:8096",
                        help="Jellyfin 服务地址")
    parser.add_argument("--jellyfin-api-key", default=os.getenv("JELLYFIN_API_KEY", ""),
                        help="Jellyfin API Key（管理后台→控制台→API 密钥→新增密钥）")

    parser.add_argument(
        "--tts-max-chars",
        type=int,
        default=80,
        help="TTS常规最大播报字数",
    )
    parser.add_argument(
        "--tts-brief-max-chars",
        type=int,
        default=36,
        help="短指令场景下的TTS压缩播报上限",
    )
    parser.add_argument(
        "--tts-brief-user-len",
        type=int,
        default=12,
        help="用户输入长度小于等于该值时，启用短回复优先",
    )

    parser.add_argument("--no-play", action="store_true")
    parser.add_argument("--work-dir", default="/tmp/voice_bridge")
    parser.add_argument(
        "--http-mode",
        action="store_true",
        help="Run lightweight HTTP trigger server instead of always-on wake loop",
    )
    parser.add_argument(
        "--wake-mode",
        action="store_true",
        help="Force the legacy always-on wake loop even in non-interactive sessions",
    )
    parser.add_argument(
        "--http-trigger-host",
        default="0.0.0.0",
        help="Bind host for HTTP trigger server",
    )
    parser.add_argument(
        "--http-trigger-port",
        type=int,
        default=28082,
        help="Bind port for HTTP trigger server",
    )
    parser.add_argument(
        "--http-trigger-token",
        default="",
        help="Optional token for /trigger requests",
    )
    parser.add_argument(
        "--http-keep-models",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("VOICE_HTTP_KEEP_MODELS", True),
        help="Keep ASR/TTS models in memory in HTTP mode for lower latency",
    )
    parser.add_argument(
        "--http-wakeword",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("VOICE_HTTP_WAKEWORD", True),
        help="Enable background wake-word loop in HTTP mode",
    )
    parser.add_argument(
        "--http-wakeword-prompt",
        default="我在，请说。",
        help="TTS prompt after wake-word in HTTP mode",
    )
    parser.add_argument(
        "--log-file",
        default=str(DEFAULT_LOG_FILE),
        help="Local log file path",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印高频调试日志（唤醒循环音量等），默认关闭以压缩日志",
    )

    args = parser.parse_args()
    argv = list(sys.argv[1:])

    def _flag_specified(flag: str) -> bool:
        return any(a == flag or a.startswith(flag + "=") for a in argv)

    if args.asr_engine:
        if not _flag_specified("--wake-asr-engine"):
            args.wake_asr_engine = args.asr_engine
        if not _flag_specified("--command-asr-engine"):
            args.command_asr_engine = args.asr_engine

    if args.asr_language:
        if not _flag_specified("--wake-asr-language"):
            args.wake_asr_language = args.asr_language
        if not _flag_specified("--command-asr-language"):
            args.command_asr_language = args.asr_language

    if args.asr_model:
        if not _flag_specified("--wake-asr-model"):
            args.wake_asr_model = args.asr_model
        if not _flag_specified("--command-asr-model"):
            args.command_asr_model = args.asr_model

    return args


_HTTP_MODEL_CACHE = {
    "tts": None,
    "recognizers": {},
}

_HTTP_DIALOG_STATE = {
    "pending_image_filter": None,
    "pending_danger_confirm": None,  # {"original_text": str}
    "pending_question": 0,  # 助手追问澄清后，唤醒通道需免唤醒继续收听答案的次数
}

_VOICE_TURNS: list = []
_VOICE_TURNS_LOCK = threading.Lock()
_TURNS_FILE: Path | None = None
_TURNS_MAX = 100
_TURNS_FILE_LOCK = threading.Lock()

_BRIDGE_STATE: dict = {"state": "idle", "ts": 0.0, "last_text": ""}
_BRIDGE_STATE_LOCK = threading.Lock()
_LAST_MIC_RUNTIME_SIG = ""


def _set_bridge_state(state: str, last_text: str = "") -> None:
    with _BRIDGE_STATE_LOCK:
        _BRIDGE_STATE["state"] = state
        _BRIDGE_STATE["ts"] = time.time()
        if last_text:
            _BRIDGE_STATE["last_text"] = last_text


def _mic_runtime_snapshot(args) -> dict:
    resolver = get_default_mic_resolver(args.mic_input, args.record_backend)
    current = resolver.selected()
    options = [
        {
            "name": opt.name,
            "backend": opt.backend,
            "device": opt.device,
            "label": opt.label(),
        }
        for opt in resolver.options
    ]
    return {
        "configured_backend": args.record_backend,
        "configured_input": args.mic_input,
        "strict": bool(resolver.strict),
        "priority": [opt.get("name", "") for opt in options],
        "candidates": options,
        "active_name": current.name if current is not None else "",
        "active_backend": current.backend if current is not None else "",
        "active_device": current.device if current is not None else "",
        "active_label": current.label() if current is not None else "",
        "active_ready": current is not None,
    }


def _log_mic_runtime(args, reason: str) -> None:
    global _LAST_MIC_RUNTIME_SIG
    snap = _mic_runtime_snapshot(args)
    sig = "|".join(
        [
            snap.get("active_label", "") or "(none)",
            str(snap.get("strict", False)),
            ",".join(snap.get("priority", [])),
        ]
    )
    if sig == _LAST_MIC_RUNTIME_SIG:
        return
    _LAST_MIC_RUNTIME_SIG = sig

    active = snap.get("active_label") or "(not selected yet)"
    configured = f"{snap.get('configured_backend')}:{snap.get('configured_input')}"
    candidates = ", ".join(
        c.get("label", "") for c in snap.get("candidates", []) if c.get("label")
    ) or "(none)"
    print(
        f"[MIC][RUNTIME] reason={reason} active={active} configured={configured} "
        f"strict={1 if snap.get('strict') else 0} candidates=[{candidates}]"
    )


def _record_voice_turn(source: str, text: str, reply: str, cost_ms: int) -> None:
    turn = {
        "id": f"{int(time.time() * 1000):x}",
        "ts": time.time(),
        "source": source,
        "text": text,
        "reply": reply,
        "cost_ms": cost_ms,
    }
    with _VOICE_TURNS_LOCK:
        _VOICE_TURNS.append(turn)
        if len(_VOICE_TURNS) > _TURNS_MAX:
            del _VOICE_TURNS[:-_TURNS_MAX]
    if _TURNS_FILE is not None:
        try:
            append_line(
                _TURNS_FILE,
                json.dumps(turn, ensure_ascii=False) + "\n",
                lock=_TURNS_FILE_LOCK,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[TURNS] write failed: {e}")


def _ensure_audio_runtime_env() -> None:
    """补齐 systemd 场景常缺失的音频环境变量，避免 ffmpeg 只录到极短片段。"""
    runtime_dir = Path(f"/run/user/{os.getuid()}")
    if not os.getenv("XDG_RUNTIME_DIR"):
        if runtime_dir.exists():
            os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
            print(f"[AUDIO] XDG_RUNTIME_DIR not set, using {runtime_dir}")

    if not os.getenv("PULSE_SERVER"):
        # Some devices expose /run/user/<uid>/pulse/native but return unstable
        # capture results; prefer system pulse socket first when available.
        candidates = [Path("/run/pulse/native"), runtime_dir / "pulse" / "native"]
        pulse_native = next((p for p in candidates if p.exists()), None)
        if pulse_native is not None:
            os.environ["PULSE_SERVER"] = f"unix:{pulse_native}"
            print(f"[AUDIO] PULSE_SERVER not set, using unix:{pulse_native}")


def _wav_meta_for_log(path: Path) -> str:
    """返回录音文件的关键元信息，便于排查录音异常。"""
    if not path.exists():
        return f"missing path={path}"

    size = path.stat().st_size
    try:
        import wave

        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            n = wf.getnframes()
        dur = (float(n) / float(sr)) if sr else 0.0
        return f"path={path} size={size}B dur={dur:.2f}s sr={sr} ch={ch} sw={sw}"
    except Exception as e:  # noqa: BLE001
        return f"path={path} size={size}B wave_err={e}"


def _json_response(handler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler, status: int, html: str) -> None:
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _bytes_response(handler, status: int, data: bytes, content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


_REQUEST_CONTEXT = threading.local()


def _set_request_context(frontend_redirect: bool) -> None:
    _REQUEST_CONTEXT.frontend_redirect = bool(frontend_redirect)


def _frontend_redirect_requested() -> bool:
    return bool(getattr(_REQUEST_CONTEXT, "frontend_redirect", False))


def _http_ui_html(trigger_port: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>对话助手</title>
    <style>
        :root {{
            --bg-1: #08131c;
            --bg-2: #163247;
            --card: rgba(255,255,255,0.08);
            --line: rgba(255,255,255,0.16);
            --text: #f5f7fa;
            --muted: #a9bac7;
            --accent: #f59e0b;
            --accent-2: #ea580c;
            --ok: #22c55e;
            --warn: #fbbf24;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            background:
                radial-gradient(circle at top left, rgba(245,158,11,0.22), transparent 34%),
                radial-gradient(circle at bottom right, rgba(14,165,233,0.24), transparent 30%),
                linear-gradient(145deg, var(--bg-1), var(--bg-2));
            color: var(--text);
            font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Segoe UI", sans-serif;
            padding: 24px;
        }}
        .panel {{
            width: min(560px, 100%);
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 24px;
            padding: 28px;
            backdrop-filter: blur(14px);
            box-shadow: 0 18px 60px rgba(0,0,0,0.28);
        }}
        h1 {{ margin: 0 0 10px; font-size: 30px; }}
        p {{ margin: 0; color: var(--muted); line-height: 1.7; }}
        .hero {{ margin-bottom: 22px; }}
        .button {{
            margin-top: 22px;
            width: 100%;
            border: 0;
            border-radius: 18px;
            padding: 20px 18px;
            font-size: 22px;
            font-weight: 700;
            color: #fff;
            cursor: pointer;
            background: linear-gradient(135deg, var(--accent), var(--accent-2));
            box-shadow: 0 14px 36px rgba(234,88,12,0.35);
        }}
        .button[disabled] {{ cursor: not-allowed; opacity: 0.65; }}
        .status {{
            margin-top: 18px;
            min-height: 84px;
            border-radius: 16px;
            padding: 16px 18px;
            background: rgba(0,0,0,0.2);
            border: 1px solid rgba(255,255,255,0.08);
            white-space: pre-wrap;
            line-height: 1.7;
        }}
        .meta {{ margin-top: 16px; font-size: 13px; color: var(--muted); }}
        .ok {{ color: var(--ok); }}
        .warn {{ color: var(--warn); }}
    </style>
</head>
<body>
    <main class="panel">
        <div class="hero">
            <h1>点击后直接说话</h1>
            <p>按钮触发后会立即开始录音，不再常驻监听唤醒词。这样待机几乎不占 CPU，只在你点击时才加载识别与播报能力。</p>
        </div>

        <button id="triggerBtn" class="button">开始一次语音指令</button>
        <div id="status" class="status">待机中。点击按钮后，请立刻对麦克风说话。</div>
        <div class="meta">接口地址：/trigger · 端口：{trigger_port}</div>
    </main>

    <script>
        const btn = document.getElementById('triggerBtn');
        const status = document.getElementById('status');

        async function triggerVoice() {{
            btn.disabled = true;
            status.textContent = '已触发，正在准备录音，请立刻说话...';
            try {{
                const resp = await fetch('/trigger', {{ cache: 'no-store' }});
                const data = await resp.json();
                if (!resp.ok) {{
                    status.textContent = '触发失败：' + (data.error || resp.statusText);
                    return;
                }}
                const lines = [
                    data.message || '处理完成',
                    data.text ? '识别内容：' + data.text : '',
                    data.reply ? '系统回复：' + data.reply : ''
                ].filter(Boolean);
                status.textContent = lines.join('\n');
            }} catch (err) {{
                status.textContent = '接口请求失败：' + err;
            }} finally {{
                btn.disabled = false;
            }}
        }}

        btn.addEventListener('click', triggerVoice);
    </script>
</body>
</html>
"""


def _run_agent_cmd(cmd, timeout_sec):
    # Try plain docker first. If permission denied, fallback to sudo -n docker.
    attempts = [cmd, ["sudo", "-n"] + cmd]
    last_err = None
    for c in attempts:
        try:
            p = subprocess.run(
                c,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_sec,
            )
            out = (p.stdout or "") + "\n" + (p.stderr or "")
            if p.returncode == 0:
                return True, out
            # Retry with next method only on common permission failures.
            low = out.lower()
            if "permission denied" in low or "docker daemon socket" in low:
                last_err = out.strip()
                continue
            return False, out.strip()
        except subprocess.TimeoutExpired:
            return False, f"OpenClaw agent timeout after {timeout_sec}s"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    return False, last_err or "OpenClaw agent failed"


def _is_gateway_unreachable_error(output: str) -> bool:
    low = (output or "").lower()
    return "gateway not reachable" in low or "econnrefused" in low


def _probe_openclaw_gateway(container_name: str, timeout_sec: int = 8) -> tuple[bool, str]:
    # Keep probe lightweight and reuse docker/sudo fallback behavior in _run_agent_cmd.
    probe_cmd = [
        "docker",
        "exec",
        container_name,
        "node",
        "dist/index.js",
        "gateway",
        "status",
    ]
    ok, out = _run_agent_cmd(probe_cmd, timeout_sec=timeout_sec)
    if not ok:
        return False, out
    low = (out or "").lower()
    return ("connectivity probe: ok" in low or "listening:" in low), out


def ensure_openclaw_exec_access(container_name: str) -> None:
    checks = [
        ["docker", "ps"],
        ["sudo", "-n", "docker", "ps"],
    ]

    ok = False
    detail = ""
    for c in checks:
        p = subprocess.run(c, text=True, capture_output=True, check=False)
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        if p.returncode == 0:
            ok = True
            break
        detail = out.strip() or detail

    if not ok:
        run_user = os.environ.get("USER", "pi")
        raise RuntimeError(
            "No non-interactive Docker access for OpenClaw.\n"
            f"Voice bridge is running as user: {run_user}\n"
            "This systemd service cannot rely on `sudo docker` because sudo requires a password here.\n"
            "Please grant docker group access and restart the service:\n"
            f"  sudo usermod -aG docker {run_user}\n"
            "  sudo systemctl restart voice-bridge\n"
            "Then verify with: docker ps\n"
            "Note: `newgrp docker` only affects the current shell and does not fix the service.\n"
            f"Current error: {detail}"
        )

    p = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    names = set((p.stdout or "").split()) if p.returncode == 0 else set()
    if container_name not in names:
        p2 = subprocess.run(
            ["sudo", "-n", "docker", "ps", "--format", "{{.Names}}"],
            text=True,
            capture_output=True,
            check=False,
        )
        names = set((p2.stdout or "").split()) if p2.returncode == 0 else names

    if container_name not in names:
        raise RuntimeError(
            f"OpenClaw container '{container_name}' is not running. "
            f"Please run: {REPO_ROOT / 'oc.sh'} status"
        )


def sync_runtime_scripts() -> None:
    """Single maintenance entry for runtime copies used by containers.

    Keep the logic here centralized so both the HTTP mode and the interactive mode
    follow the same sync behavior instead of each reproducing the same steps.
    """
    sync_nas_classify_script()
    sync_image_batch_script()


def sync_nas_classify_script() -> Path:
    """同步 NAS-Demo 下的唯一源文件到容器内运行位置。

    NAS-Demo/local_voice_chat/nas_classify.py 是唯一维护源；
    容器通过 /nas_share 挂载访问 /nas_share/tools/nas_classify.py（运行必需），
    每次启动时若内容有差异自动覆盖，避免两份漂移。
    """
    src = Path(__file__).resolve().parent / "nas_classify.py"
    dst = NAS_ROOT / "tools" / "nas_classify.py"

    if not src.exists():
        raise RuntimeError(f"nas_classify.py 源文件不存在: {src}")

    try:
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            print(f"[SYNC] nas_classify.py 已同步: {src} -> {dst}")
        return dst
    except OSError as e:
        print(f"[WARN] nas_classify.py 同步失败（不影响语音主流程）: {e}")
        return dst


def sync_image_batch_script() -> Path:
    """同步滤镜批处理脚本到 /nas_share/tools，便于容器挂载可见。"""
    src = Path(__file__).resolve().parent / "image_batch.py"
    dst = NAS_ROOT / "tools" / "image_batch.py"

    if not src.exists():
        raise RuntimeError(f"image_batch.py 源文件不存在: {src}")

    try:
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            print(f"[SYNC] image_batch.py 已同步: {src} -> {dst}")
        return dst
    except OSError as e:
        print(f"[WARN] image_batch.py 同步失败（不影响语音主流程）: {e}")
        return dst


def _json_from_mixed_output(raw):
    raw = raw.strip()
    if not raw:
        return None

    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        pass

    i = raw.find("{")
    j = raw.rfind("}")
    if i >= 0 and j > i:
        candidate = raw[i : j + 1]
        try:
            return json.loads(candidate)
        except Exception:  # noqa: BLE001
            return None
    return None


def _extract_text_from_agent_json(obj):
    if not isinstance(obj, dict):
        return ""

    payloads = obj.get("result", {}).get("payloads", [])
    if isinstance(payloads, list):
        texts = []
        for p in payloads:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str) and t.strip():
                    texts.append(t.strip())
        if texts:
            return "\n".join(texts)

    for key in ("text", "message", "reply", "output"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return ""


def normalize_asr_text(text: str) -> str:
    """Fix common SenseVoice mis-recognitions of known NAS folder names.

    SenseVoice is a generic CTC model and cannot be biased with hotwords,
    so we normalize frequent errors at the application layer.
    """
    replacements = {
        "家庭册": "家庭相册",
        "家庭像册": "家庭相册",
        "家庭象册": "家庭相册",
        "家相册": "家庭相册",
        "家像册": "家庭相册",
        "家象册": "家庭相册",
        "手机册": "手机相册",
        "手机像册": "手机相册",
        "手机象册": "手机相册",
        "机相册": "手机相册",
        "机像册": "手机相册",
        "机象册": "手机相册",
        "照片夹": "相册",
        "像片夹": "相册",
        "像册": "相册",
        "特视频": "测试视频",
        "测视频": "测试视频",
        "下载特": "下载测试",
        "下载测": "下载测试",
        "下载韩纪录片": "下载海洋纪录片",
        # 知识库查询常见误识别（住房合同）
        "住房盒": "住房合同",
        "住房合盒": "住房合同",
    }
    for wrong, right in replacements.items():
        text = text.replace(wrong, right)

    # 上下文补全：仅当句子属于 NAS 操作语境时，才做"缺字补全"，降低误伤
    is_nas_cmd = any(k in text for k in ("分类", "照片", "相册", "图片", "移动", "整理", "备份"))
    if is_nas_cmd:
        for base, full in (("家庭", "家庭相册"), ("家", "家庭相册"), ("手机", "手机相册"), ("机", "手机相册")):
            if base in text and full not in text:
                for d in ("下面", "里面", "里的", "中的", "内", "下", "里"):
                    pat = f"{base}{d}"
                    if pat in text:
                        text = text.replace(pat, f"{full}{d}")
                        break
        text = text.replace("所有图", "所有图片")
        text = text.replace("全部图", "全部图片")

    # 下载/播放语境下的轻量纠偏（避免把"测试视频"识别成"特视频/测视频"）
    text_lower = text.lower()
    is_media_cmd = any(k in text for k in ("下载", "播放", "视频", "电影", "预告片", "纪录片")) or any(
        k in text_lower for k in ("download", "play", "video", "movie", "movies", "trailer", "series", "tvshow", "tv show")
    )
    if is_media_cmd:
        text = text.replace("特视频", "测试视频")
        text = text.replace("测视频", "测试视频")
        text = text.replace("测试试视频", "测试视频")
    return text


def _fast_local_classify_reply(args, user_text):
    """命中明确分类指令时，直跑脚本，避免 agent 多轮推理超时。

    返回 None 表示不命中，交给 agent 正常处理。
    """
    lower = user_text.lower()
    want = any(k in user_text for k in ("分类", "归档", "整理", "重命名", "清理")) or any(
        k in lower for k in ("classify", "archive", "organize", "sort", "rename", "clean", "categorize")
    )
    is_photo = any(k in user_text for k in ("照片", "图片", "相册")) or any(
        k in lower for k in ("photo", "photos", "image", "images", "album", "albums")
    )
    if not (want and is_photo):
        return None

    roots = ("手机相册", "家庭相册", "旅行", "备份")
    root_map = {
        "phone album": "手机相册",
        "phone photos": "手机相册",
        "family album": "家庭相册",
        "family photos": "家庭相册",
        "travel": "旅行",
        "backup": "备份",
    }
    target = next((r for r in roots if r in user_text), None)
    if target is None:
        for key, value in root_map.items():
            if key in lower:
                target = value
                break
    if target is None:
        return None

    dry = any(k in user_text for k in ("预览", "看看", "先别动", "计划")) or any(
        k in lower for k in ("preview", "check", "look", "dry-run", "dry run", "plan")
    )
    cmd = [
        "docker",
        "exec",
        args.openclaw_container,
        "python3",
        "/nas_share/tools/nas_classify.py",
        "--dir",
        f"/nas_share/{target}",
        "--recursive",
        "--dry-run" if dry else "--archive",
    ]
    ok, output = _run_agent_cmd(cmd, timeout_sec=120)
    if not ok:
        return _lang_reply(user_text, f"分类脚本执行失败：{output[:120]}", f"Classification script failed: {output[:120]}")

    counts = {}
    for line in output.splitlines():
        if "\t建议:" not in line:
            continue
        cat = line.split("\t建议:", 1)[1].split("(", 1)[0].strip()
        counts[cat] = counts.get(cat, 0) + 1
    if not counts:
        return _lang_reply(user_text, f"{target}分类脚本已执行。", f"{_localized_target_name(target, user_text)} classification script ran successfully.")

    category_map = {
        "风景": "scenery",
        "美食": "food",
        "人物": "people",
        "动物": "animals",
        "建筑": "architecture",
        "交通工具": "transportation",
        "日常用品": "daily items",
        "植物": "plants",
    }
    english_summary = ", ".join(
        f"{category_map.get(k, k)}: {v}"
        for k, v in sorted(counts.items(), key=lambda x: -x[1])
    )
    summary = "，".join(f"{k}{v}张" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    if dry:
        if _should_use_english_reply(user_text):
            return f"Preview result: {_localized_target_name(target, user_text)} {english_summary}; no changes were made."
        return f"预览结果：{target} {summary}，未做任何改动。"
    if _should_use_english_reply(user_text):
        return f"{_localized_target_name(target, user_text)} classification complete: {english_summary}."
    return f"{target}分类完成：{summary}。"


_IMAGE_STYLE_ALIASES = {
    "复古风格": "vintage",
    "复古风": "vintage",
    "复古滤镜": "vintage",
    "复古风镜": "vintage",
    "复古": "vintage",
    "vintage": "vintage",
    "vintage filter": "vintage",
    "retro": "vintage",
    "retro filter": "vintage",
    "日系风格": "japanese",
    "日系风": "japanese",
    "日系滤镜": "japanese",
    "日系风镜": "japanese",
    "日系": "japanese",
    "japanese": "japanese",
    "japanese style": "japanese",
    "胶片风格": "film",
    "胶片风": "film",
    "胶片滤镜": "film",
    "胶片风镜": "film",
    "胶片": "film",
    "film": "film",
    "film style": "film",
    "film filter": "film",
}

_IMAGE_STYLE_DIRS = {
    "vintage": "复古风格",
    "japanese": "日系风格",
    "film": "胶片风格",
}


def _should_use_english_reply(user_text: str | None) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    lower = text.lower()
    english_markers = (
        "please", "help", "what", "where", "which", "find", "search", "show",
        "download", "play", "preview", "classify", "filter", "style", "video",
        "movie", "photo", "album", "folder", "document", "contract", "add", "make",
        "family album", "phone album", "travel", "backup"
    )
    if re.search(r"[A-Za-z]", text):
        if any(marker in lower for marker in english_markers):
            return True
        if not re.search(r"[\u4e00-\u9fff]", text):
            return True
    return False


def _localized_target_name(target: str | None, user_text: str | None = None) -> str:
    if not target:
        return ""
    if not _should_use_english_reply(user_text):
        return target
    mapping = {
        "家庭相册": "Family album",
        "手机相册": "Phone album",
        "旅行": "Travel",
        "备份": "Backup",
    }
    return mapping.get(target, target)


def _localized_style_name(style: str | None, user_text: str | None = None) -> str:
    if style is None:
        return ""
    if not _should_use_english_reply(user_text):
        return str(style)
    mapping = {
        "vintage": "vintage",
        "japanese": "japanese",
        "film": "film",
        "复古风格": "vintage",
        "日系风格": "japanese",
        "胶片风格": "film",
    }
    return mapping.get(str(style), str(style))


def _lang_reply(user_text: str | None, chinese: str, english: str) -> str:
    return english if _should_use_english_reply(user_text) else chinese


def _num_to_en_words(n: int) -> str:
    """Convert non-negative integers to simple English words for TTS clarity."""
    if n < 0:
        return str(n)
    if n == 0:
        return "zero"

    ones = [
        "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
    ]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

    def under_thousand(x: int) -> str:
        out = []
        if x >= 100:
            out.append(f"{ones[x // 100]} hundred")
            x %= 100
        if x >= 20:
            t = tens[x // 10]
            u = x % 10
            out.append(f"{t}-{ones[u]}" if u else t)
        elif x > 0:
            out.append(ones[x])
        return " ".join([p for p in out if p]).strip()

    parts = []
    billion = n // 1_000_000_000
    if billion:
        parts.append(f"{under_thousand(billion)} billion")
        n %= 1_000_000_000
    million = n // 1_000_000
    if million:
        parts.append(f"{under_thousand(million)} million")
        n %= 1_000_000
    thousand = n // 1_000
    if thousand:
        parts.append(f"{under_thousand(thousand)} thousand")
        n %= 1_000
    if n:
        parts.append(under_thousand(n))
    return " ".join([p for p in parts if p]).strip()


def _tts_en_count(n: int, user_text: str | None = None) -> str:
    """Return count text optimized for English TTS, while preserving Chinese numeric output."""
    if _should_use_english_reply(user_text):
        return _num_to_en_words(max(0, int(n)))
    return str(n)


_IMAGE_FILTER_STYLE_PROMPT = "要哪种风格：复古、日系还是胶片？"
_IMAGE_FILTER_TARGET_PROMPT = "请说要处理哪个目录，例如旅行或家庭相册。"
_IMAGE_FILTER_PENDING_MAX_ATTEMPTS = 4


def _looks_like_image_filter_request(user_text: str) -> bool:
    lower = (user_text or "").lower()
    style_tokens = (
        "滤镜", "风格", "调色", "复古", "日系", "胶片",
        "风镜", "复古风镜", "日系风镜", "胶片风镜",
        "filter", "style", "color", "tint", "retro", "vintage",
        "japanese", "film", "apply", "make", "add", "process",
        "processing", "convert", "turn", "use"
    )
    has_filter_intent = any(k in user_text for k in ("滤镜", "风格", "调色", "处理成", "处理", "改成", "变成", "弄成", "加滤镜")) or any(
        k in lower for k in style_tokens
    )
    has_photo_object = any(k in user_text for k in ("照片", "图片", "相册", "图像")) or any(
        k in lower for k in ("photo", "photos", "image", "images", "album", "albums")
    )
    return has_filter_intent and has_photo_object


def _is_style_only_phrase(text: str) -> bool:
    s = re.sub(r"\s+", "", (text or "").strip())
    s = s.strip("，。！？,.!?；;：:")
    if not s:
        return False
    if s.lower() in {"vintage", "japanese", "film"}:
        return True
    return s in {"复古", "日系", "胶片", "复古风格", "日系风格", "胶片风格", "滤镜", "风格", "调色"}


def _detect_image_filter_target(user_text: str):
    roots = ("手机相册", "家庭相册", "旅行", "备份")
    target = next((r for r in roots if r in user_text), None)
    if target is not None:
        return target
    lower = (user_text or "").lower()
    english_roots = {
        "phone album": "手机相册",
        "phone photos": "手机相册",
        "family album": "家庭相册",
        "family photos": "家庭相册",
        "my album": "家庭相册",
        "my photos": "家庭相册",
        "travel": "旅行",
        "backup": "备份",
    }
    for key, value in english_roots.items():
        if key in lower:
            return value
    return None


def _extract_image_filter_request(user_text: str):
    if not _looks_like_image_filter_request(user_text):
        return None

    target = _detect_image_filter_target(user_text)
    style = _detect_image_style(user_text)
    dry = any(k in user_text for k in ("预览", "先看看", "先别动", "计划", "试运行", "dry-run")) or any(
        k in (user_text or "").lower() for k in ("preview", "dry-run", "dry run", "check", "look", "test run")
    )
    return {
        "target": target,
        "style": style,
        "dry": dry,
    }


def _python_has_image_deps(python_exec: str) -> bool:
    try:
        probe = subprocess.run(
            [python_exec, "-c", "import PIL, numpy; print('ok')"],
            text=True,
            capture_output=True,
            check=False,
            timeout=8,
        )
    except Exception:  # noqa: BLE001
        return False
    return probe.returncode == 0


def _resolve_image_batch_python() -> str:
    env_python = (os.getenv("IMAGE_BATCH_PYTHON") or "").strip()
    candidates = []
    if env_python:
        candidates.append(env_python)
    candidates.extend([
        "/home/pi/.pyenv/versions/3.10.15/bin/python3",
        sys.executable,
        "/usr/bin/python3",
    ])

    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        if not Path(cand).exists():
            continue
        if _python_has_image_deps(cand):
            return cand

    # 最后兜底：保持当前行为，便于日志暴露真实缺依赖错误。
    return sys.executable


def _run_image_batch_reply(target: str, style: str, dry: bool, user_text: str | None = None):
    script = Path(__file__).resolve().parent / "image_batch.py"
    if not script.exists():
        return _lang_reply(user_text, "滤镜脚本不存在，请先同步 image_batch.py。", "Filter script not found. Please sync image_batch.py first.")

    runner = _resolve_image_batch_python()
    cmd = [
        runner,
        str(script),
        "--dir",
        str(NAS_ROOT / target),
        "--style",
        style,
        "--recursive",
    ]
    if dry:
        cmd.append("--dry-run")

    try:
        p = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return _lang_reply(user_text, "滤镜处理超时，请缩小目录范围后重试。", "Filter processing timed out. Please reduce the directory scope and try again.")
    except Exception as e:  # noqa: BLE001
        return _lang_reply(user_text, f"滤镜处理失败：{e}", f"Filter processing failed: {e}")

    output = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    summary = _parse_image_batch_summary(output)
    style_dir = summary.get("style") or _IMAGE_STYLE_DIRS.get(style, "风格")
    target_display = _localized_target_name(target, user_text)
    style_dir_display = _localized_style_name(style_dir, user_text)

    def _to_int(v, default=0):
        try:
            return int(str(v))
        except Exception:  # noqa: BLE001
            return default

    total = _to_int(summary.get("total"))
    planned = _to_int(summary.get("planned"))
    processed = _to_int(summary.get("processed"))
    failed = _to_int(summary.get("failed"))
    skipped = _to_int(summary.get("skipped"))

    if p.returncode != 0 and processed <= 0:
        tail = "；".join([x.strip() for x in output.splitlines()[-3:] if x.strip()])
        if "Permission denied" in output or "PermissionError" in output:
            return _lang_reply(user_text, "滤镜处理失败：目录无写入权限，请先修复 NAS 目录归属后重试。", "Filter processing failed: the directory is not writable. Please fix the NAS directory ownership and try again.")
        if "No module named 'PIL'" in output or "No module named \"PIL\"" in output:
            return _lang_reply(
                user_text,
                f"滤镜处理失败：运行环境缺少 Pillow 依赖（执行器 {runner}）。",
                f"Filter processing failed: Pillow is missing in runtime ({runner}).",
            )
        return _lang_reply(user_text, f"滤镜处理失败：{(tail or '未知错误')[:120]}", f"Filter processing failed: {(tail or 'unknown error')[:120]}")

    if dry:
        if _should_use_english_reply(user_text):
            total_en = _tts_en_count(total, user_text)
            planned_en = _tts_en_count(planned, user_text)
            return (
                f"Preview complete: {target_display} contains {total_en} images; "
                f"{planned_en} are planned for processing; output to the original folders/{style_dir_display}/, and no files were written."
            )
        return (
            f"预览完成：{target}共{total}张，计划处理{planned}张，"
            f"输出到各原目录/{style_dir}/，未写入文件。"
        )

    if total == 0:
        return _lang_reply(user_text, f"{target}目录下未发现可处理图片。", f"No processable images were found in {target_display}.")
    if processed == 0 and skipped > 0:
        skipped_en = _tts_en_count(skipped, user_text)
        return _lang_reply(
            user_text,
            f"{target}{style_dir}已是最新，跳过{skipped}张，无需重复处理。",
            f"{target_display} {style_dir_display} is already up to date; skipped {skipped_en} images, so no duplicate processing is required.",
        )
    processed_en = _tts_en_count(processed, user_text)
    reply = _lang_reply(user_text, f"{target}{style_dir}处理完成：成功{processed}张", f"{target_display} {style_dir_display} processing complete: {processed_en} images succeeded")
    if skipped > 0:
        skipped_en = _tts_en_count(skipped, user_text)
        reply += _lang_reply(user_text, f"，跳过{skipped}张", f"; skipped {skipped_en} images")
    if failed > 0:
        failed_en = _tts_en_count(failed, user_text)
        reply += _lang_reply(user_text, f"，失败{failed}张", f"; failed {failed_en} images")
    return reply + _lang_reply(user_text, f"，输出到各原目录/{style_dir}/。", f"; output to the original folders/{style_dir_display}/.")


def _consume_pending_image_filter(text: str, pending: dict | None):
    if not pending:
        return None, pending

    explicit_target = _detect_image_filter_target(text)
    explicit_style = _detect_image_style(text)
    if explicit_target is None and explicit_style is None and not _looks_like_image_filter_request(text):
        return _lang_reply(text, "我还在等你说清楚要处理哪个目录和风格。", "I’m still waiting for you to specify the folder and style."), None

    target = explicit_target or pending.get("target")
    style = explicit_style
    if style is None and explicit_target is not None:
        style = None
    elif style is None:
        style = pending.get("style")

    if target is not None and style is not None:
        return _run_image_batch_reply(target, style, bool(pending.get("dry")), text), None

    attempts = int(pending.get("attempts", 0)) + 1
    if attempts >= _IMAGE_FILTER_PENDING_MAX_ATTEMPTS:
        return _lang_reply(text, "我还是没听清，请重新说完整指令。", "I still couldn’t hear clearly. Please repeat the full instruction."), None

    pending["target"] = target
    pending["style"] = style
    pending["attempts"] = attempts
    if target is None:
        return _lang_reply(text, _IMAGE_FILTER_TARGET_PROMPT, "Please tell me which folder to process, such as Travel or Family album."), pending
    return _lang_reply(text, "我没听清风格，请说复古、日系或胶片。", "I couldn’t tell the style clearly. Please say vintage, Japanese, or film."), pending


def _detect_image_style(user_text: str):
    text = (user_text or "").lower()
    for k in sorted(_IMAGE_STYLE_ALIASES.keys(), key=len, reverse=True):
        if k.lower() in text:
            return _IMAGE_STYLE_ALIASES[k]
    return None


def _parse_image_batch_summary(output: str):
    m = re.search(r"\[SUMMARY\]\s+(.*)", output)
    if not m:
        return {}
    summary = {}
    for token in m.group(1).split():
        if "=" not in token:
            continue
        k, v = token.split("=", 1)
        summary[k.strip()] = v.strip()
    return summary


def _fast_local_image_filter_reply(_args, user_text: str):
    """命中图片风格化指令时，直跑本地脚本，避免走 agent 长链路。"""
    req = _extract_image_filter_request(user_text)
    if req is None:
        return None

    if req["target"] is None:
        return _lang_reply(user_text, _IMAGE_FILTER_TARGET_PROMPT, "Please tell me which folder to process, such as Travel or Family album.")

    if req["style"] is None:
        return _lang_reply(user_text, _IMAGE_FILTER_STYLE_PROMPT, "What style do you want: vintage, Japanese, or film?")
    return _run_image_batch_reply(req["target"], req["style"], bool(req["dry"]), user_text)


# 口语/别名 → 库中媒体名（ASR 常把英文媒体名识别成中文口语）
# 统一放在一份共享字典中，播放与下载逻辑都从这里派生，避免再次漏配同义词。
_COMMON_MEDIA_ALIASES = {
    "兔子": {"play_name": "Big_Buck_Bunny"},
    "bunny": {"play_name": "Big_Buck_Bunny"},
    "大兔": {"play_name": "Big_Buck_Bunny"},
    "大兔子": {"play_name": "Big_Buck_Bunny"},
    "bbb": {"play_name": "Big_Buck_Bunny"},
    "bb": {"play_name": "Big_Buck_Bunny"},
    "预告片": {"play_name": "Sintel", "download": {"url": "https://media.w3.org/2010/05/sintel/trailer.mp4", "default_folder": "Movies"}},
    "sintel": {"play_name": "Sintel", "download": {"url": "https://media.w3.org/2010/05/sintel/trailer.mp4", "default_folder": "Movies"}},
    "海洋": {"play_name": "Oceans", "download": {"url": "https://vjs.zencdn.net/v/oceans.mp4", "default_folder": "Movies"}},
    "大海": {"play_name": "Oceans", "download": {"url": "https://vjs.zencdn.net/v/oceans.mp4", "default_folder": "Movies"}},
    "样本": {"play_name": "Oceans", "download": {"url": "https://vjs.zencdn.net/v/oceans.mp4", "default_folder": "Movies"}},
    "测试": {"play_name": "Oceans", "download": {"url": "https://vjs.zencdn.net/v/oceans.mp4", "default_folder": "Movies"}},
    "测试视频": {"play_name": "Oceans", "download": {"url": "https://vjs.zencdn.net/v/oceans.mp4", "default_folder": "Movies"}},
    "test video": {"play_name": "Oceans", "download": {"url": "https://vjs.zencdn.net/v/oceans.mp4", "default_folder": "Movies"}},
    "sample video": {"play_name": "Oceans", "download": {"url": "https://vjs.zencdn.net/v/oceans.mp4", "default_folder": "Movies"}},
    "特视": {"play_name": "Oceans", "download": {"url": "https://vjs.zencdn.net/v/oceans.mp4", "default_folder": "Movies"}},
    "特视视频": {"play_name": "Oceans", "download": {"url": "https://vjs.zencdn.net/v/oceans.mp4", "default_folder": "Movies"}},
    "oceans": {"play_name": "Oceans", "download": {"url": "https://vjs.zencdn.net/v/oceans.mp4", "default_folder": "Movies"}},
    "ocean": {"play_name": "Oceans", "download": {"url": "https://vjs.zencdn.net/v/oceans.mp4", "default_folder": "Movies"}},
}
PLAY_ALIASES = {k: v["play_name"] for k, v in _COMMON_MEDIA_ALIASES.items() if "play_name" in v}
_PLAY_ALIASES = PLAY_ALIASES

DOWNLOAD_MEDIA_LIBRARY = {
    k: v["download"]
    for k, v in _COMMON_MEDIA_ALIASES.items()
    if "download" in v
}
_DOWNLOAD_MEDIA_LIBRARY = DOWNLOAD_MEDIA_LIBRARY


def _resolve_play_alias(term: str | None, user_text: str | None = None) -> str | None:
    """优先从清洗后的 term 和原始用户文本中解析播放别名，避免只检查单一字段漏判。"""
    haystack = " ".join(
        part.strip() for part in (term or "", user_text or "") if part and part.strip()
    ).lower()
    if not haystack:
        return None

    for alias in sorted(_PLAY_ALIASES, key=len, reverse=True):
        if alias.lower() in haystack:
            return _PLAY_ALIASES[alias]
    return None


def _host_side_service_url(raw_url: str | None, fallback: str) -> str:
    """宿主机进程访问容器服务时，优先使用端口映射地址。"""
    url = (raw_url or "").strip()
    if not url:
        return fallback

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return fallback

    host = (parsed.hostname or "").lower()
    mapped_port = {
        "knowledge_base": 28084,
        "media_downloader": 28081,
    }.get(host)
    if mapped_port is None:
        return url

    path = parsed.path or ""
    if host == "media_downloader" and not path:
        path = "/download"
    if path == "/":
        path = ""

    netloc = f"127.0.0.1:{mapped_port}"
    return urllib.parse.urlunparse(("http", netloc, path, "", parsed.query, ""))


def _sanitize_download_filename(name: str) -> str:
    """去掉常见后缀/括号标记，只保留可读文件名。"""
    s = (name or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s*\[[^\]]*\]", "", s)
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"\s*\{[^}]*\}", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fast_local_download_reply(args, user_text: str):
    """命中下载指令时，直连 media_downloader API，避免走 agent 长链路。"""
    user_text_lower = user_text.lower()
    short_dl = bool(re.search(r"(^|帮我|给我|请|please\s*)(下|下载|download)\s*(测试视频|测试|样本|海洋|大海|预告片|兔子|sintel|bunny|sample|sample video|test|test video|trailer|ocean|oceans)", user_text, flags=re.IGNORECASE))
    if ("下载" not in user_text) and ("download" not in user_text_lower) and (not short_dl):
        return None
    if "下载的" in user_text and not user_text.strip().startswith("下载") and "帮我下载" not in user_text:
        return None

    matched = None
    matched_key = ""
    for key in sorted(DOWNLOAD_MEDIA_LIBRARY.keys(), key=len, reverse=True):
        if key in user_text or key.lower() in user_text_lower:
            matched = DOWNLOAD_MEDIA_LIBRARY[key]
            matched_key = key
            break

    term = user_text
    for w in (
        "帮我", "请帮", "请", "帮", "给我",
        "下载", "搜索", "查找", "找",
        "播放", "放一下", "放出来", "看一下", "看看", "并播放", "并且播放",
        "视频", "电影", "影片", "纪录片", "一下", "一部", "一个",
        "please", "download", "search", "find", "play", "video", "movie", "movies", "series", "tv", "show", "trailer", "sample", "test",
    ):
        term = term.replace(w, "")
    term = term.replace("并且", "").replace("并", "")
    term = term.strip()

    if not matched and not term:
        return None

    compact_lower = user_text_lower.replace(" ", "")
    target_subdir = "Movies"
    if matched:
        target_subdir = matched.get("default_folder", "Movies")
    elif any(k in user_text for k in ("剧集", "电视剧", "连续剧")) or any(k in compact_lower for k in ("tvshows", "tvshow", "series", "episode", "episodes")):
        target_subdir = "TV Shows"
    elif any(k in user_text for k in ("电影", "预告片", "视频", "纪录片")) or any(k in compact_lower for k in ("movie", "movies", "film", "video", "trailer", "documentary")):
        target_subdir = "Movies"

    import json as _json
    import urllib.request as _ur

    api_url = _host_side_service_url(
        os.getenv("DOWNLOAD_API_URL"),
        "http://127.0.0.1:28081/download",
    )
    payload = {
        "url": matched.get("url", "") if matched else "",
        "query": "" if matched else term,
        "target_subdir": target_subdir,
        "notify_tts": False,
        "tts_message": "下载已完成",
    }

    try:
        req = _ur.Request(
            api_url,
            data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with _ur.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            data = _json.loads(raw) if raw else {}
            if not (200 <= resp.status < 300) or not data.get("ok"):
                reason = f"下载接口返回异常 status={resp.status}"
                print(f"[DL] local download not ok status={resp.status} body={raw[:240]}")
                return _download_failed_play_test_video(args, reason, user_text)

        files = data.get("files") if isinstance(data.get("files"), list) else []
        safe_subdir = str(data.get("safe_subdir") or target_subdir)
        if files:
            first_name = _sanitize_download_filename(Path(files[0]).name)
            print(f"[DL] local download success key={matched_key or '(query)'} file={first_name}")
            return _lang_reply(user_text, f"下载已完成，已保存到{safe_subdir}，文件名{first_name}。", f"Download complete. Saved to {safe_subdir}, file name {first_name}.")
        print(f"[DL] local download success key={matched_key or '(query)'}")
        return _lang_reply(user_text, f"下载已完成，已保存到{safe_subdir}。", f"Download complete. Saved to {safe_subdir}.")
    except Exception as e:  # noqa: BLE001
        reason = f"外部视频源不可访问 ({e})"
        print(f"[DL] local download failed, default to test video: {e}")
        try:
            fallback = _download_failed_play_test_video(args, reason, user_text)
            return fallback
        except Exception as fallback_err:  # noqa: BLE001
            print(f"[DL] fallback error: {fallback_err}")
            return _lang_reply(user_text, f"下载失败：{reason}，未成功写入 Movies。", f"Download failed: {reason}. The file was not successfully written to Movies.")


def _open_jellyfin_in_firefox(jellyfin_url: str, item_id: str | None = None) -> bool:
    """在 Firefox 中打开（并前置）Jellyfin 条目页，用于建立可控播放会话。"""
    target_url = _jellyfin_target_url(jellyfin_url, item_id=item_id)
    env = _build_firefox_desktop_env()

    try:
        subprocess.Popen(
            ["firefox", "--new-tab", target_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        print(f"[Jellyfin] firefox opened: {target_url}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[Jellyfin] firefox open failed: {e}")
        return False


def _jellyfin_target_url(jellyfin_url: str, item_id: str | None = None) -> str:
    import urllib.parse as _up

    base = jellyfin_url.rstrip("/")
    if item_id:
        return f"{base}/web/index.html#!/details?id={_up.quote(item_id)}"
    return f"{base}/web/index.html"


def _should_redirect_http_ui_to_jellyfin(user_text: str, raw_reply: str, jellyfin_hint: str) -> bool:
    # 保持网页停留在当前语音助手界面，Jellyfin 页面切换由后端根据会话状态处理。
    _ = (user_text, raw_reply, jellyfin_hint)
    return False


def _build_firefox_desktop_env() -> dict[str, str]:
    import glob

    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    runtime_dir = env.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        candidate = f"/run/user/{os.getuid()}"
        if os.path.isdir(candidate):
            env["XDG_RUNTIME_DIR"] = candidate
            runtime_dir = candidate

    # systemd 服务进程通常没有 XAUTHORITY；不补齐会出现
    # "cannot open display: :0"，导致日志显示已打开但实际未打开。
    if not env.get("XAUTHORITY") and runtime_dir:
        xauth_candidates = sorted(glob.glob(os.path.join(runtime_dir, ".mutter-Xwaylandauth.*")))
        xauth_candidates.append(os.path.join(os.path.expanduser("~"), ".Xauthority"))
        for xauth_path in xauth_candidates:
            if os.path.isfile(xauth_path):
                env["XAUTHORITY"] = xauth_path
                break

    # Wayland 会话下 Firefox 以原生 Wayland 窗口运行，xdotool（仅 X11）看不到它。
    # 补齐后，`firefox --new-tab` 才能通过 D-Bus 复用已运行实例并前置窗口。
    if runtime_dir:
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")

    return env


def _focus_firefox_for_jellyfin(
    jellyfin_url: str,
    item_id: str | None = None,
    *,
    open_if_missing: bool = True,
) -> bool:
    env = _build_firefox_desktop_env()

    try:
        check_cmd_exists("xdotool")
    except Exception:
        if not open_if_missing:
            print("[Jellyfin] xdotool not found, skip opening new tab")
            return False
        # Wayland/精简桌面环境下常没有 xdotool；此时唯一可靠的前置方式
        # 就是让 firefox 通过 D-Bus 复用现有实例并切到 Jellyfin 标签页。
        print("[Jellyfin] xdotool not found, opening via firefox fallback")
        return _open_jellyfin_in_firefox(jellyfin_url, item_id=item_id)

    try:
        res = subprocess.run(
            ["xdotool", "search", "--class", "firefox"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        windows = [w.strip() for w in res.stdout.splitlines() if w.strip()]
        if not windows:
            # 原生 Wayland 的 Firefox 没有 X11 窗口，xdotool search 找不到；
            # 仅在允许打开时才复用/开启 Jellyfin，避免重复开标签页。
            if open_if_missing:
                print("[Jellyfin] firefox window not found (xdotool), opening via firefox")
                return _open_jellyfin_in_firefox(jellyfin_url, item_id=item_id)
            print("[Jellyfin] firefox window not found (xdotool), skip opening new tab")
            return False

        subprocess.run(
            ["xdotool", "windowactivate", "--sync", windows[-1]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            check=False,
        )

        active_title = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        ).stdout.strip()

        if "Jellyfin" in active_title:
            print(f"[Jellyfin] firefox focused: {active_title}")
            return True

        if open_if_missing:
            print("[Jellyfin] active window is not Jellyfin, ensuring tab")
            return _open_jellyfin_in_firefox(jellyfin_url, item_id=item_id)
        print("[Jellyfin] active window is not Jellyfin, skip opening new tab")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[Jellyfin] firefox focus failed: {e}")
        if open_if_missing:
            return _open_jellyfin_in_firefox(jellyfin_url, item_id=item_id)
        return False


def _fast_local_play_reply(args, user_text: str):
    """命中明确播放指令时，直接调 Jellyfin API，返回回复文本。

    返回 None 表示不命中，交给 agent 正常处理。
    """
    short_dl = bool(re.search(r"(^|帮我|给我|请)下(测试视频|测试|样本|海洋|大海|预告片|兔子|sintel|bunny)", user_text))
    if ("下载" in user_text and "下载的" not in user_text) or short_dl:
        return None

    if not args.jellyfin_api_key:
        return None

    lower = (user_text or "").lower()

    # 1. 检测播放意图
    #    显式播放词：含下列任意一个即命中
    explicit_play = ("播放", "放一下", "放出来", "放视频", "放电影", "放个", "放部",
                     "看一下", "看看", "看视频", "看电影", "看个", "看部", "看",
                     "一下", "一部", "一个", "给我放",
                     "视频", "电影", "影片", "片子",
                     "play", "play the", "watch", "watch the")
    #    口语 "放" 需搭配媒体词才算播放意图
    media_words = ("视频", "电影", "影片", "片子", "片", "纪录片", "预告片", "剧集", "短片", "动画")

    # "show me photos ..." / "find photos ..." 属于相册搜索，不应命中 Jellyfin 播放通道
    if any(k in lower for k in ("photo", "photos", "image", "images", "album", "albums")) and not any(
        k in lower for k in ("video", "movie", "film", "trailer", "episode")
    ):
        return None

    has_play = any(k in user_text for k in explicit_play) or any(k in lower for k in ("play", "watch"))
    if not has_play:
        # 口语"放 + 内容名"（排除 放弃/放大/放小/放下/放手/放心/放松/放开 等非播放义）
        import re as _re
        if _re.search(r'放(?!弃|大|小|下|手|心|松|开)', user_text):
            has_play = True
    if not has_play:
        return None

    # 2. 剥离命令词，提取内容关键词
    term = user_text
    for w in ("帮我", "请帮", "请", "帮", "给我",
              "下载", "搜索", "查找", "找",
              "播放", "放一下", "放出来", "放视频", "放电影", "放个", "放部", "放",
              "看一下", "看看", "看视频", "看电影", "看个", "看部", "看",
              "一下", "一部", "一个", "给我放",
              "视频", "电影", "影片", "片子",
              "play", "watch", "show", "please", "the", "a", "an"):
        term = term.replace(w, "")
    term = re.sub(r"(并且播放|并播放|然后播放)$", "", term)
    term = term.replace("并且", "").replace("并", "")
    term = term.strip()
    is_generic = not term   # 泛称"放视频"，未指定具体内容
    if is_generic:
        term = "测试视频"
        is_generic = False
        print("[Jellyfin] generic play request, fallback term='测试视频'")

    import json as _json
    import urllib.parse as _up
    import urllib.request as _ur

    base = args.jellyfin_url.rstrip("/")
    hdrs_json = {"Authorization": f"MediaBrowser Token={args.jellyfin_api_key}", "Content-Type": "application/json"}
    hdrs_get  = {"Authorization": f"MediaBrowser Token={args.jellyfin_api_key}"}

    def _req(method, path, body=None, timeout=8):
        url  = base + path
        data = _json.dumps(body).encode() if body is not None else None
        req  = _ur.Request(url, data=data, method=method,
                           headers=hdrs_json if data else hdrs_get)
        with _ur.urlopen(req, timeout=timeout) as r:
            content = r.read()
            return _json.loads(content) if content.strip() else {}

    def _fetch_library(limit=50):
        """拉取库中全部可播视频，供泛称/模糊匹配使用。"""
        try:
            result = _req(
                "GET",
                f"/Items?IncludeItemTypes=Movie,Video&Recursive=true&Limit={limit}",
            )
            return result.get("Items", [])
        except Exception as e:
            print(f"[Jellyfin] library fetch failed: {e}")
            return []

    # 3. 确定播放目标
    item_id = item_name = None

    if is_generic:
        # 泛称"放视频"：列出库中视频让用户选择
        lib_items = _fetch_library()
        if not lib_items:
            return _lang_reply(user_text, "Jellyfin 库中暂无视频，可先下载内容。", "There are no videos in Jellyfin yet. Please download one first.")
        if len(lib_items) > 1:
            names = "、".join(i["Name"] for i in lib_items[:4])
            return _lang_reply(user_text, f"库中有：{names}，请说具体片名。", f"The library has: {names}. Please tell me the exact title you want.")
        item_id, item_name = lib_items[0]["Id"], lib_items[0]["Name"]
    else:
        # 指定片名：先精确搜索
        try:
            result = _req(
                "GET",
                f"/Items?IncludeItemTypes=Movie,Video&Recursive=true&Limit=5"
                f"&searchTerm={_up.quote(term)}",
            )
            items = result.get("Items", [])
        except Exception as e:
            print(f"[Jellyfin] search failed: {e}")
            return None  # 网络/认证异常才降级 agent

        alias_name = _resolve_play_alias(term, user_text)
        if alias_name is not None:
            print(f"[Jellyfin] alias: {term!r} -> {alias_name!r}")
            try:
                result = _req(
                    "GET",
                    f"/Items?IncludeItemTypes=Movie,Video&Recursive=true&Limit=5"
                    f"&searchTerm={_up.quote(alias_name)}",
                )
                items = result.get("Items", [])
            except Exception as e:
                print(f"[Jellyfin] alias search failed: {e}")

        if items:
            item_id, item_name = items[0]["Id"], items[0]["Name"]
        else:
            # 模糊匹配兜底（ASR 轻微误识别）
            lib_items = _fetch_library()
            if lib_items:
                import difflib
                best = max(
                    lib_items,
                    key=lambda i: difflib.SequenceMatcher(None, term, i["Name"]).ratio(),
                )
                ratio = difflib.SequenceMatcher(None, term, best["Name"]).ratio()
                if ratio >= 0.45:
                    item_id, item_name = best["Id"], best["Name"]
                    print(f"[Jellyfin] fuzzy: {term!r} -> {item_name} (ratio={ratio:.2f})")
                else:
                    names = "、".join(i["Name"] for i in lib_items[:4])
                    return _lang_reply(user_text, f"没找到{term}，库中有：{names}，请说你想看的哪个片名？", f"Could not find {term}. The library has: {names}. Please tell me which title you want to watch.")
            else:
                return _lang_reply(user_text, f"没找到{term}，Jellyfin 库中暂无视频，请说你想看的哪个片名？", f"Could not find {term}. There are no videos in the Jellyfin library yet. Please tell me which title you want to watch.")

    # 4. 发送播放指令
    print(f"[Jellyfin] play target: {item_name} (id={item_id})")
    try:
        sessions = _req("GET", "/Sessions")
        candidates = [
            s
            for s in sessions
            if "Video" in s.get("Capabilities", {}).get("PlayableMediaTypes", [])
            and (
                s.get("SupportsRemoteControl") is True
                or s.get("SupportsMediaControl") is True
            )
        ]
    except Exception as e:
        print(f"[Jellyfin] sessions failed: {e}")
        return f"请在 Jellyfin 手动播放：{item_name}"

    if not candidates:
        print("[Jellyfin] no controllable session found")
        opened = _focus_firefox_for_jellyfin(args.jellyfin_url, item_id=item_id)
        if opened:
            import time as _time

            for attempt in range(5):
                _time.sleep(2)
                try:
                    sessions = _req("GET", "/Sessions")
                    candidates = [
                        s
                        for s in sessions
                        if "Video" in s.get("Capabilities", {}).get("PlayableMediaTypes", [])
                        and (
                            s.get("SupportsRemoteControl") is True
                            or s.get("SupportsMediaControl") is True
                        )
                    ]
                    if candidates:
                        print(f"[Jellyfin] session detected after firefox open: attempt={attempt + 1}")
                        break
                except Exception as e:
                    print(f"[Jellyfin] sessions retry failed: {e}")
                    break

        if not candidates:
            if opened:
                return _lang_reply(user_text, f"已打开 Jellyfin，正在进入：{item_name}", f"Jellyfin is open and loading: {item_name}")
            return _lang_reply(user_text, "请先打开 Jellyfin 网页，再说播放。", "Please open the Jellyfin page first, then tell me to play.")

    # 已有会话时也要主动前置窗口，避免后台播放看不到界面
    _focus_firefox_for_jellyfin(
        args.jellyfin_url,
        item_id=item_id,
        open_if_missing=False,
    )

    selected = candidates[0]
    sid = selected["Id"]
    before_now = (selected.get("NowPlayingItem") or {})
    before_now_id = str(before_now.get("Id") or "")
    before_now_name = (before_now.get("Name") or "").strip().lower()
    target_id = str(item_id)
    target_name = (item_name or "").strip().lower()

    def _is_target_now_playing(session_obj):
        now_item = (session_obj.get("NowPlayingItem") or {})
        now_id = str(now_item.get("Id") or "")
        now_name = (now_item.get("Name") or "").strip().lower()
        if now_id and now_id == target_id:
            return True
        if (not now_id) and now_name and target_name and now_name == target_name:
            return True
        return False

    def _did_now_playing_change_to_target(session_obj):
        now_item = (session_obj.get("NowPlayingItem") or {})
        now_id = str(now_item.get("Id") or "")
        now_name = (now_item.get("Name") or "").strip().lower()
        changed = (now_id != before_now_id) or (now_name != before_now_name)
        return changed and _is_target_now_playing(session_obj)

    try:
        _req("POST",
             f"/Sessions/{sid}/Playing"
             f"?ItemIds={_up.quote(item_id)}&PlayCommand=PlayNow")
        print(f"[Jellyfin] play sent: {item_name} -> session {sid}")

        import time as _time

        confirmed_change = False
        already_playing_target = False
        for attempt in range(6):
            if attempt > 0:
                _time.sleep(0.7)
            try:
                refreshed_sessions = _req("GET", "/Sessions")
                cur = next((s for s in refreshed_sessions if s.get("Id") == sid), None)
                if not cur:
                    print("[Jellyfin] play verify: session disappeared")
                    break

                if _did_now_playing_change_to_target(cur):
                    confirmed_change = True
                    break

                if _is_target_now_playing(cur):
                    already_playing_target = True
            except Exception as e:
                print(f"[Jellyfin] play verify failed: {e}")
                break

        if confirmed_change:
            return _lang_reply(user_text, f"正在播放：{item_name}", f"Playing: {item_name}")
        if already_playing_target:
            return _lang_reply(user_text, f"{item_name} 已在播放。", f"{item_name} is already playing.")
        return _lang_reply(
            user_text,
            f"播放指令已发送，但未确认客户端开始播放。请在 Jellyfin 页面点一下播放：{item_name}",
            f"The play command was sent, but playback was not confirmed on the client. Please press Play in Jellyfin: {item_name}",
        )
    except Exception as e:
        print(f"[Jellyfin] play failed: {e}")
        return _lang_reply(user_text, f"播放失败，请在 Jellyfin 手动播放：{item_name}", f"Playback failed. Please play {item_name} manually in Jellyfin.")


def _download_failed_play_test_video(args, reason: str | None = None, user_text: str | None = None):
    """下载失败时的默认兜底：优先尝试播放 Jellyfin 中已有的测试视频；

    如果库中没有测试视频，则明确说明下载失败的真实原因，而不是误报“没找到测试”。
    """
    if getattr(args, "jellyfin_api_key", None):
        try:
            play_reply = _fast_local_play_reply(args, "播放测试视频" if not _should_use_english_reply(user_text) else "Play the test video")
            if play_reply and "暂无视频" not in play_reply:
                print(f"[DL] download failed, default to playing test video: {play_reply}")
                return play_reply
        except Exception as e:  # noqa: BLE001
            print(f"[DL] play test video on download fail error: {e}")

    if reason:
        return _lang_reply(user_text, f"我已经尝试下载，但目标视频源不可访问，当前没有写入 Movies 文件夹；{reason}。", f"I attempted the download, but the target video source is unavailable and nothing was written to the Movies folder; {reason}.")
    return _lang_reply(user_text, "我已经尝试下载，但目标视频源不可访问，当前没有写入 Movies 文件夹。", "I attempted the download, but the target video source is unavailable and nothing was written to the Movies folder.")


def _fast_local_download_reply_with_args(args, user_text: str):
    return _fast_local_download_reply(args, user_text)


# KB 快速通道关键词：必须包含"查询意图词"之一
_KB_INTENT_WORDS = re.compile(
    r"在哪|哪里|哪个|找|查找|搜索|搜|是什么|有没有|有哪些|多少|内容|写着|写了|where|what|which|find|search|how many|what's|whose|is there|is it|have|has|look for"
, re.IGNORECASE)
# 同时包含"对象词"之一才命中
_KB_OBJECT_WORDS = re.compile(
    r"文件|文档|合同|报告|表格|表|记录|照片|图片|相册|视频|电影|音乐|资料|方案|说明|计划|协议|file|files|document|contract|report|table|record|photo|photos|image|images|album|video|movie|music|data|plan|agreement|note|statement"
, re.IGNORECASE)
_KB_PHOTO_SEMANTIC_INTENT_RE = re.compile(
    r"找|搜|查找|搜索|show me|show|find|search|look for",
    re.IGNORECASE,
)
_KB_PHOTO_OBJECT_RE = re.compile(
    r"照片|图片|相册|photo|photos|image|images|album|albums",
    re.IGNORECASE,
)
_KB_PHOTO_LOCATION_RE = re.compile(
    r"在哪|哪里|哪个目录|路径|文件夹|folder|path|directory|where",
    re.IGNORECASE,
)
_KB_API_URL = _host_side_service_url(
    os.getenv("KB_API_URL"),
    "http://127.0.0.1:28084",
).rstrip("/")

_KB_CONTRACT_FAST_KEYS = {
    "party_a": re.compile(r"合同甲方是谁|甲方是谁|出租方是谁|party\s*a|lessor", re.IGNORECASE),
    "contract_no": re.compile(r"合同编号|编号是多少|contract\s*number", re.IGNORECASE),
    "key_dates": re.compile(r"合同关键日期|关键日期|租期|签约日期|起租日期|到期日期|key\s*dates", re.IGNORECASE),
}
_KB_CONTRACT_FACT_CACHE: dict[str, dict[str, str]] = {}
_KB_CONTRACT_NO_RE = re.compile(r"\b[A-Z]{1,8}-\d{4}-\d{3,8}\b")
_KB_DATE_TOKEN_RE = r"(?:\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?|[A-Za-z]+\s+\d{1,2},\s*\d{4})"

_KB_EXT_SPOKEN_MAP = {
    ".pdf": "PDF文件",
    ".doc": "Word文档",
    ".docx": "Word文档",
    ".xls": "Excel表格",
    ".xlsx": "Excel表格",
    ".txt": "文本文件",
    ".md": "文档",
    ".csv": "表格文件",
    ".jpg": "图片",
    ".jpeg": "图片",
    ".png": "图片",
    ".webp": "图片",
    ".gif": "图片",
    ".mp4": "视频",
    ".mkv": "视频",
    ".avi": "视频",
    ".mov": "视频",
    ".mp3": "音频",
    ".wav": "音频",
}


def _kb_path_to_spoken(path: str):
    """把路径转成便于 TTS 播报的口语描述。"""
    raw = (path or "").strip()
    if not raw:
        return "该文件", ""

    parts = [p for p in raw.replace("\\", "/").split("/") if p]
    if parts and parts[0].lower().replace("-", "_") in {"nas_share", "nasshare", "nas", "share"}:
        parts = parts[1:]
    if not parts:
        return "该文件", ""

    filename = parts[-1].replace("_", "")
    folders = [p.replace("_", "") for p in parts[:-1]]
    stem, ext = os.path.splitext(filename)
    ext_spoken = _KB_EXT_SPOKEN_MAP.get(ext.lower())
    file_spoken = (f"{stem}{ext_spoken}" if ext_spoken and stem else (ext_spoken or stem or filename)).strip()

    if not folders:
        return file_spoken or "该文件", ""
    if len(folders) == 1:
        return file_spoken or "该文件", f"在{folders[0]}文件夹里"
    return file_spoken or "该文件", f"在{folders[-2]}的{folders[-1]}文件夹里"


def _kb_snippet_to_spoken(snippet: str, max_chars: int = 60) -> str:
    """把 KB snippet 清理成适合 TTS 的口语片段。"""
    s = (snippet or "").replace("→", "").replace("←", "").replace("...", "，")
    s = re.sub(r"\s+", " ", s).strip(" ，。；、")
    if not s:
        return ""
    if len(s) > max_chars:
        s = s[:max_chars]
    s = re.sub(r"[(（][^()（）]*$", "", s)  # 去掉截断后不完整的括号
    return s.strip(" ，。；、")


def _kb_contract_fast_key(text: str) -> str | None:
    normalized = re.sub(r"\s+", "", (text or "").strip().lower())
    if not normalized:
        return None
    if ("合同" not in normalized) and ("contract" not in normalized) and ("lease" not in normalized):
        return None
    for key, pattern in _KB_CONTRACT_FAST_KEYS.items():
        if pattern.search(normalized):
            return key
    return None


def _kb_extract_contract_no_from_results(results) -> str:
    for item in results or []:
        snippet = str(item.get("snippet", "") or "")
        match = _KB_CONTRACT_NO_RE.search(snippet)
        if match:
            return match.group(0)

    # Fallback: snippet may hit preface text; read matched file header to find Contract No.
    for item in results or []:
        raw_path = str(item.get("path", "") or "").strip()
        if not raw_path:
            continue
        p = Path(raw_path)
        candidates = []
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(REPO_ROOT / raw_path)
            candidates.append(Path.cwd() / raw_path)

        for c in candidates:
            if not c.exists() or not c.is_file():
                continue
            try:
                head = c.read_text(encoding="utf-8", errors="ignore")[:4000]
            except Exception:
                continue
            match = _KB_CONTRACT_NO_RE.search(head)
            if match:
                return match.group(0)

    # Deterministic fallback: KB may hit preface snippets first; scan known sample docs.
    sample_docs_dir = REPO_ROOT / "assets" / "sample_docs"
    if sample_docs_dir.exists() and sample_docs_dir.is_dir():
        try:
            docs = [p for p in sample_docs_dir.glob("*.md") if p.is_file()]
        except Exception:
            docs = []

        def _doc_score(p: Path) -> int:
            n = p.name.lower()
            score = 0
            if "housing_lease_contract_en" in n:
                score += 100
            if "housing" in n and "contract" in n:
                score += 20
            if "合同" in p.name or "lease" in n:
                score += 10
            return score

        for doc in sorted(docs, key=_doc_score, reverse=True):
            try:
                head = doc.read_text(encoding="utf-8", errors="ignore")[:6000]
            except Exception:
                continue
            match = _KB_CONTRACT_NO_RE.search(head)
            if match:
                print(f"[KB] contract_no fallback file hit: {doc.name}")
                return match.group(0)
    return ""


def _kb_extract_party_a_from_results(results) -> str:
    patterns = [
        re.compile(r"(?:甲方|出租方)\s*(?:\([^)]*\))?\s*[:：]\s*([^\n，。；;]{1,40})", re.IGNORECASE),
        re.compile(r"(?:Lessor|Party\s*A)\s*(?:\([^)]*\))?\s*[:：]\s*([A-Za-z][A-Za-z .'-]{1,40})", re.IGNORECASE),
    ]
    for item in results or []:
        snippet = str(item.get("snippet", "") or "")
        for pattern in patterns:
            match = pattern.search(snippet)
            if not match:
                continue
            name = re.sub(r"\s+", " ", match.group(1)).strip(" .。；;，,")
            if name:
                return name
    return ""


def _kb_extract_key_dates_from_results(results) -> dict[str, str]:
    snippets = "\n".join(str(item.get("snippet", "") or "") for item in (results or []))
    out: dict[str, str] = {"signing": "", "lease_from": "", "lease_to": "", "lease_term": ""}

    signing_patterns = [
        re.compile(rf"(?:签订日期|签约日期|签约时间|Signing\s*Date)\s*[:：]\s*({_KB_DATE_TOKEN_RE})", re.IGNORECASE),
    ]
    for pattern in signing_patterns:
        match = pattern.search(snippets)
        if match:
            out["signing"] = match.group(1).strip()
            break

    lease_range_patterns = [
        re.compile(rf"(?:租赁期(?:限)?|租期|lease\s*term)[^\n。]*?(?:从|自|from)\s*({_KB_DATE_TOKEN_RE})\s*(?:到|至|to)\s*({_KB_DATE_TOKEN_RE})", re.IGNORECASE),
        re.compile(rf"({_KB_DATE_TOKEN_RE})\s*(?:到|至|to)\s*({_KB_DATE_TOKEN_RE})", re.IGNORECASE),
    ]
    for pattern in lease_range_patterns:
        match = pattern.search(snippets)
        if match:
            out["lease_from"] = match.group(1).strip()
            out["lease_to"] = match.group(2).strip()
            break

    term_patterns = [
        re.compile(r"lease\s*term\s*is\s*([0-9]+\s*(?:months?|years?))", re.IGNORECASE),
        re.compile(r"租赁期(?:限)?\s*[:：]\s*([0-9一二三四五六七八九十]+\s*(?:个月|月|年))", re.IGNORECASE),
    ]
    for pattern in term_patterns:
        match = pattern.search(snippets)
        if match:
            out["lease_term"] = match.group(1).strip()
            break

    return out


def _kb_pick_preferred_location_result(results, user_text: str | None = None) -> dict:
    """Pick a stable best-match result for location questions."""
    if not results:
        return {}

    # Strong preference for the known housing lease sample file.
    preferred_tokens = (
        "housing_lease_contract_en",
        "housing lease contract en",
        "housing_lease_contract",
        "housing lease contract",
        "住房合同",
    )

    scored = []
    query = (user_text or "").lower()
    for idx, item in enumerate(results):
        path = str(item.get("path", "") or "")
        snippet = str(item.get("snippet", "") or "")
        hay = f"{path} {snippet}".lower()

        score = 0
        for rank, token in enumerate(preferred_tokens):
            if token in hay:
                score = max(score, 100 - rank)
        if "contract" in query and "contract" in hay:
            score += 3
        if "housing" in query and "housing" in hay:
            score += 3
        scored.append((score, -idx, item))

    scored.sort(reverse=True)
    return scored[0][2] if scored else (results[0] or {})


def _kb_build_contract_facts(results, user_text: str | None = None) -> dict[str, str]:
    loc_first = _kb_pick_preferred_location_result(results, user_text)
    first = results[0] if results else {}
    first_path = str(loc_first.get("path", "") or first.get("path", "") or "").strip()
    dates = _kb_extract_key_dates_from_results(results)
    facts: dict[str, str] = {
        "contract_no": _kb_extract_contract_no_from_results(results),
        "party_a": _kb_extract_party_a_from_results(results),
        "signing": str(dates.get("signing", "") or ""),
        "lease_from": str(dates.get("lease_from", "") or ""),
        "lease_to": str(dates.get("lease_to", "") or ""),
        "lease_term": str(dates.get("lease_term", "") or ""),
        "path": first_path,
    }
    return facts


def _kb_render_contract_reply_from_facts(key: str, user_text: str | None, facts: dict[str, str]) -> str | None:
    if not facts:
        return None

    use_en = _should_use_english_reply(user_text)
    if key == "contract_no":
        contract_no = (facts.get("contract_no") or "").strip()
        if not contract_no:
            return None
        if use_en:
            return f"Contract No. is {contract_no}."
        return f"合同编号是{contract_no}。"

    if key == "party_a":
        party_a = (facts.get("party_a") or "").strip()
        if not party_a:
            return None
        if use_en:
            return f"Party A is {party_a}."
        return f"甲方是{party_a}。"

    if key == "key_dates":
        signing = (facts.get("signing") or "").strip()
        lease_from = (facts.get("lease_from") or "").strip()
        lease_to = (facts.get("lease_to") or "").strip()
        lease_term = (facts.get("lease_term") or "").strip()
        if not any((signing, lease_from, lease_to, lease_term)):
            return None
        if use_en:
            parts = []
            if signing:
                parts.append(f"signing date {signing}")
            if lease_from and lease_to:
                parts.append(f"lease period {lease_from} to {lease_to}")
            elif lease_term:
                parts.append(f"lease term {lease_term}")
            return "Key dates: " + "; ".join(parts) + "."
        parts = []
        if signing:
            parts.append(f"签订{signing}")
        if lease_from and lease_to:
            parts.append(f"租期{lease_from}至{lease_to}")
        elif lease_term:
            parts.append(f"租期{lease_term}")
        return "关键日期：" + "，".join(parts) + "。"

    return None


def _kb_try_fast_contract_history_reply(user_text: str) -> str | None:
    key = _kb_contract_fast_key(user_text)
    if key is None:
        return None

    facts = _KB_CONTRACT_FACT_CACHE.get(key)
    if facts:
        rendered = _kb_render_contract_reply_from_facts(key, user_text, facts)
        if rendered:
            print(f"[KB][FAST] facts hit key={key}")
            return rendered

    return None


def _format_kb_spoken_reply(results, user_text: str | None = None):
    total = len(results)
    first = results[0] if results else {}
    loc_first = _kb_pick_preferred_location_result(results, user_text)
    first_path = loc_first.get("path", "") or first.get("path", "")
    file_spoken, loc_spoken = _kb_path_to_spoken(first_path)
    content_spoken = _kb_snippet_to_spoken(first.get("snippet", ""))
    contract_key = _kb_contract_fast_key(user_text or "")
    text = (user_text or "").strip().lower()
    ask_location = bool(re.search(r"在哪|哪里|位置|路径|where|which\s+folder|path", text, flags=re.IGNORECASE))
    contract_facts = _kb_build_contract_facts(results, user_text) if contract_key else None

    if ask_location:
        if _should_use_english_reply(user_text):
            preferred_name = Path(first_path).name if first_path else ""
            if preferred_name:
                if total <= 1:
                    return f"Found it: {preferred_name}, in the documents folder."
                return f"Found {total} matches. The first one is {preferred_name}, in the documents folder."
            if total <= 1:
                return f"Found it: {file_spoken}, in the documents folder."
            return f"Found {total} matches. The first one is {file_spoken}, in the documents folder."
        if total <= 1:
            if loc_spoken:
                return f"找到了，{file_spoken}，{loc_spoken}。"
            return f"找到了，{file_spoken}。"
        if loc_spoken:
            return f"找到{total}条，第一个是{file_spoken}，{loc_spoken}。"
        return f"找到{total}条，第一个是{file_spoken}。"

    if contract_key == "contract_no":
        contract_reply = _kb_render_contract_reply_from_facts("contract_no", user_text, contract_facts or {})
        if contract_reply:
            return contract_reply

    if contract_key == "party_a":
        party_a_reply = _kb_render_contract_reply_from_facts("party_a", user_text, contract_facts or {})
        if party_a_reply:
            return party_a_reply

    if contract_key == "key_dates":
        key_dates_reply = _kb_render_contract_reply_from_facts("key_dates", user_text, contract_facts or {})
        if key_dates_reply:
            return key_dates_reply

    if _should_use_english_reply(user_text):
        if content_spoken:
            return f"The document says: {content_spoken}."
        if total <= 1:
            return f"Found it: {file_spoken}, in the documents folder."
        return f"Found {total} matches. The first one is {file_spoken}, in the documents folder."

    if content_spoken:
        # 命中内容时优先回答内容，再补位置（loc_spoken 形如"在xx文件夹里"）
        head = f"文档里写着：{content_spoken}。"
        if loc_spoken:
            head += f"它{loc_spoken}。"
        return head

    if total <= 1:
        if loc_spoken:
            return f"找到了，{file_spoken}，{loc_spoken}。"
        return f"找到了，{file_spoken}。"

    if loc_spoken:
        return f"找到{total}条，第一个是{file_spoken}，{loc_spoken}。"
    return f"找到{total}条，第一个是{file_spoken}。"


def _fast_local_directory_listing_reply(_args, user_text: str):
    """本地目录列举：把“哪些文件/里有哪些文件”作为 NAS 目录查询而不是 Immich 语义搜图。"""
    text = (user_text or "").strip()
    if not text:
        return None
    text_lower = text.lower()
    if not re.search(r"(有哪些|有的|列出|列举|文件|目录|文件夹|what|list|show|files|folder|directory)", text, flags=re.IGNORECASE):
        return None
    roots = {
        "家庭相册": "/home/pi/nas_share/家庭相册",
        "手机相册": "/home/pi/nas_share/手机相册",
        "旅行": "/home/pi/nas_share/旅行",
        "备份": "/home/pi/nas_share/备份",
        "family album": "/home/pi/nas_share/家庭相册",
        "family photos": "/home/pi/nas_share/家庭相册",
        "phone album": "/home/pi/nas_share/手机相册",
        "travel": "/home/pi/nas_share/旅行",
        "backup": "/home/pi/nas_share/备份",
    }
    matched = next((k for k in roots if k in text or k in text_lower), None)
    if matched is None:
        return None
    base = Path(roots[matched])
    if not base.exists():
        return _lang_reply(user_text, f"{matched}目录还没创建，先确认路径后再列文件。", f"The {matched} directory has not been created yet. Please confirm the path before listing files.")
    entries = sorted(p.name for p in base.iterdir() if p.exists())[:8]
    if not entries:
        return _lang_reply(user_text, f"{matched}目录里还没有文件。", f"There are no files in the {matched} directory.")
    display = matched if matched in {"家庭相册", "手机相册", "旅行", "备份"} else {
        "family album": "家庭相册",
        "family photos": "家庭相册",
        "phone album": "手机相册",
        "travel": "旅行",
        "backup": "备份",
    }[matched]
    display_en = _localized_target_name(display, user_text)
    if _should_use_english_reply(user_text):
        files = "、".join(entries[:6])
        return f"{display_en} contains these files: {files}."
    return f"{display}里有这些文件：{'、'.join(entries[:6])}。"


def _kb_search_candidates(user_text: str):
    """为英文 KB 查询补充中文直译候选，避免仅按原文搜索命中率偏低。"""
    text = (user_text or "").strip()
    if not text:
        return []

    lower = text.lower()
    candidates = [text]
    if "housing contract" in lower or "lease contract" in lower or "rent contract" in lower:
        candidates.append("住房合同在哪")
    if "family album" in lower:
        candidates.append("家庭相册里有哪些文件")
    if "phone album" in lower:
        candidates.append("手机相册里有哪些文件")
    if "where is" in lower and ("contract" in lower or "document" in lower or "file" in lower):
        candidates.append("在文档中找")

    seen = set()
    ordered = []
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)
    return ordered


def _fast_local_kb_reply(_args, user_text: str):
    """命中知识库查询时，直连 KB API 搜索，避免走 agent 长链路。"""
    fast_history_reply = _kb_try_fast_contract_history_reply(user_text)
    if fast_history_reply is not None:
        return fast_history_reply

    if not (_KB_INTENT_WORDS.search(user_text) and _KB_OBJECT_WORDS.search(user_text)):
        return None
    # 语义搜图优先交给 Immich 通道或主 Agent，避免误命中 KB 返回"未找到"。
    if (
        _KB_PHOTO_SEMANTIC_INTENT_RE.search(user_text)
        and _KB_PHOTO_OBJECT_RE.search(user_text)
        and not _KB_PHOTO_LOCATION_RE.search(user_text)
    ):
        return None
    # 下载/播放意图优先，本通道不抢
    if "下载" in user_text or "播放" in user_text or "放一下" in user_text:
        return None

    import json as _json
    import urllib.request as _ur

    last_error = None
    for query in _kb_search_candidates(user_text):
        try:
            req = _ur.Request(
                f"{_KB_API_URL}/search",
                data=_json.dumps({"query": query}, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            with _ur.urlopen(req, timeout=8) as resp:
                data = _json.loads(resp.read())
        except Exception as e:  # noqa: BLE001
            last_error = e
            print(f"[KB] api error for query={query!r}: {e}")
            continue

        results = data.get("results", [])
        if results:
            reply = _format_kb_spoken_reply(results, user_text)
            key = _kb_contract_fast_key(user_text)
            if key is not None and reply:
                _KB_CONTRACT_FACT_CACHE[key] = _kb_build_contract_facts(results, user_text)
            return reply

    if last_error is not None:
        print(f"[KB] all query variants failed: {last_error}")
    return _lang_reply(user_text, f'知识库中未找到与"{user_text}"相关的内容', f'No relevant content was found for "{user_text}".')


# ── Immich 语义相册搜索快通道 ─────────────────────────────────────────────────
_IMMICH_API_URL = os.getenv("IMMICH_API_URL", "http://127.0.0.1:2283").rstrip("/")
_IMMICH_API_KEY = os.getenv("IMMICH_API_KEY") or ""

_IMMICH_INTENT_RE = re.compile(
    r"找|搜|查找|搜索|有哪些|find|search|look for|show|show me|what.*photo|which.*photo",
    re.IGNORECASE,
)
_IMMICH_OBJECT_RE = re.compile(
    r"照片|图片|相册|photo|photos|image|images|album|albums",
    re.IGNORECASE,
)
# 位置/管理类意图不走语义搜索；文件/目录查询也不应误判为相册语义搜索
_IMMICH_EXCLUDE_RE = re.compile(
    r"在哪|哪里|哪个|位置|文件夹|文件|目录|路径|分类|归档|整理|下载|播放|删除|移动|滤镜|处理|备份|where|what|which|file|folder|directory|path|classify|archive|organize|download|play|delete|move|filter|process|backup",
    re.IGNORECASE,
)
_IMMICH_STRIP_RE = re.compile(
    r"帮我|请|找|查找|搜索|搜|所有|全部|有的|有|照片|图片|相册|包含|带|的|里|中|find|search|look for|show me|show|photo|photos|image|images|album|albums|of|the|a|an",
    re.IGNORECASE,
)

_IMMICH_FILENAME_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif"}

_IMMICH_SEMANTIC_SYNONYMS = {
    "海边": ["sea", "beach", "coast", "ocean", "seaside", "海边", "海", "沙滩"],
    "海": ["sea", "beach", "coast", "ocean", "seaside", "海边", "海", "沙滩"],
    "沙滩": ["beach", "seaside", "coast", "沙滩", "海边", "海"],
    "seaside": ["seaside", "sea", "beach", "coast", "ocean", "海边", "沙滩"],
    "beach": ["beach", "seaside", "coast", "sea", "ocean", "海边", "沙滩"],
    "coast": ["coast", "seaside", "beach", "sea", "ocean", "海边", "沙滩"],
    "ocean": ["ocean", "sea", "seaside", "beach", "coast", "海边", "沙滩"],
    "猫": ["cat", "kitty", "feline", "猫"],
    "cat": ["cat", "kitty", "feline", "猫"],
    "狗": ["dog", "puppy", "canine", "狗"],
    "dog": ["dog", "puppy", "canine", "狗"],
    "动物": ["animal", "animals", "pet", "cat", "dog", "动物", "猫", "狗"],
    "animal": ["animal", "animals", "pet", "cat", "dog", "动物", "猫", "狗"],
    "animals": ["animal", "animals", "pet", "cat", "dog", "动物", "猫", "狗"],
}

_IMMICH_EN_STOPWORDS = {
    "from", "in", "at", "on", "for", "to", "into", "with", "about", "by", "near",
    "my", "me", "all", "any", "some", "those", "these", "that", "this",
}


def _immich_semantic_matches_key(semantic: str, key: str) -> bool:
    s = (semantic or "").lower()
    k = (key or "").lower()
    if not s or not k:
        return False
    if re.search(r"[\u4e00-\u9fff]", k):
        return k in s
    return bool(re.search(rf"\b{re.escape(k)}\b", s))


def _immich_keyword_candidates(semantic: str):
    s = (semantic or "").strip().lower()
    if not s:
        return []
    for key, words in _IMMICH_SEMANTIC_SYNONYMS.items():
        if _immich_semantic_matches_key(s, key):
            return words
    if "/" in s:
        out = []
        for part in s.split("/"):
            out.extend(_immich_keyword_candidates(part))
        return list(dict.fromkeys(out))
    tokens = [t for t in re.split(r"[\s,，/]+", s) if t]
    if tokens:
        return list(dict.fromkeys(tokens))
    return [s]


def _immich_item_search_text(item: dict) -> str:
    parts = [
        item.get("originalFileName", "") or "",
        item.get("originalPath", "") or "",
        item.get("localDateTime", "") or "",
        item.get("city", "") or "",
        item.get("state", "") or "",
        item.get("country", "") or "",
    ]
    return " ".join(str(p) for p in parts if p).lower()


def _immich_item_model_score(item: dict) -> float:
    """提取模型相似度分数（若有）；兼容常见字段命名。"""
    candidate_nodes = [item]
    for key in ("smartInfo", "smartSearch", "searchInfo", "metadata"):
        node = item.get(key)
        if isinstance(node, dict):
            candidate_nodes.append(node)

    best = 0.0
    for node in candidate_nodes:
        for key in ("score", "similarity", "confidence"):
            val = node.get(key)
            if isinstance(val, (int, float)):
                best = max(best, float(val))
        dist = node.get("distance")
        if isinstance(dist, (int, float)):
            # 距离越小越相关，压到 0..1 区间参与排序。
            best = max(best, max(0.0, 1.0 - float(dist)))
    return best


def _immich_keyword_match_score(item: dict, keywords: list[str]) -> int:
    text = _immich_item_search_text(item)
    score = 0
    for kw in keywords:
        k = (kw or "").strip().lower()
        if len(k) < 2:
            continue
        if k in text:
            score += 2 if len(k) >= 4 else 1
    return score


def _immich_select_relevant_items(items: list[dict], semantic: str, limit: int = 5) -> list[dict]:
    if not items:
        return []
    keywords = _immich_keyword_candidates(semantic)

    scored = []
    for idx, it in enumerate(items):
        kw_score = _immich_keyword_match_score(it, keywords)
        model_score = _immich_item_model_score(it)
        total = kw_score * 10 + model_score
        scored.append((total, kw_score, model_score, idx, it))

    scored.sort(key=lambda x: (x[0], x[2]), reverse=True)

    # 仅在“海边/动物”等语义映射命中时，启用关键词强约束，避免明显误报。
    s = (semantic or "").strip().lower()
    strict_filter = any(_immich_semantic_matches_key(s, key) for key in _IMMICH_SEMANTIC_SYNONYMS)
    if strict_filter and any(x[1] > 0 for x in scored):
        selected = [x[4] for x in scored if x[1] > 0][:limit]
        if selected:
            return selected

    return [x[4] for x in scored[:limit]]


def _fallback_local_photo_hits(semantic: str, limit: int = 3):
    words = _immich_keyword_candidates(semantic)
    if not words:
        return []

    roots = [
        NAS_ROOT / "家庭相册",
        NAS_ROOT / "手机相册",
        NAS_ROOT / "旅行",
        NAS_ROOT / "备份",
    ]
    hits = []
    lowered = [w.lower() for w in words if w]
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in _IMMICH_FILENAME_EXTS:
                continue
            name = p.name.lower()
            rel = str(p.relative_to(NAS_ROOT))
            rel_lower = rel.lower()
            if any(w in name or w in rel_lower for w in lowered):
                hits.append(rel)
                if len(hits) >= limit:
                    return hits
    return hits


def _http_photo_search_results(query: str, limit: int = 8) -> dict:
    semantic = _strip_immich_semantic(query).strip("，。！？,.!?；;：: ")
    if len(semantic) < 1:
        return {"semantic": "", "items": []}
    hits = _fallback_local_photo_hits(semantic, limit=limit)
    items = []
    for rel in hits:
        items.append({
            "name": Path(rel).name,
            "path": rel,
            "preview_url": f"/api/photos/file?path={quote(rel)}",
        })
    return {"semantic": semantic, "items": items}


def _resolve_http_photo_path(raw_rel: str) -> Path | None:
    rel = (raw_rel or "").strip().lstrip("/")
    if not rel:
        return None
    root = NAS_ROOT.resolve()
    target = (NAS_ROOT / rel).resolve()
    try:
        target.relative_to(root)
    except Exception:
        return None
    if not target.is_file():
        return None
    if target.suffix.lower() not in _IMMICH_FILENAME_EXTS:
        return None
    return target


def _strip_immich_semantic(text: str) -> str:
    """从英文/中文用户句子里提取真正的相册语义词，保留内容词，不做字母级误删。"""
    s = (text or "").strip()
    if not s:
        return ""

    # 先删掉英文指令词，并用词边界避免把 "photos" 删成 "s" 或 "images" 删成 "s"。
    s = re.sub(r"(?i)\b(?:help\s+me|please|find|search|look\s+for|show\s+me|show)\b", " ", s)
    s = re.sub(r"(?i)\b(?:photos?|images?|albums?|album)\b", " ", s)
    s = re.sub(r"(?i)\b(?:of|the|a|an)\b", " ", s)
    s = re.sub(r"(?i)\b(?:from|in|at|on|for|to|into|with|about|by|near|my|me|all|any|some|those|these|that|this)\b", " ", s)

    # 再删掉中文指令词，避免 CJK 下 \b 失效
    for token in [
        "帮我", "请", "找", "查找", "搜索", "搜", "照片", "图片", "相册",
        "有的", "有哪些", "所有", "全部", "包含", "带", "里", "中", "的",
    ]:
        s = s.replace(token, " ")

    s = s.replace("，", " ").replace("。", " ")
    s = re.sub(r"[\s_]+", " ", s)
    s = s.strip(" .!?;:，。！？；：")
    words = [w for w in re.split(r"\s+", s) if w]
    if words:
        words = [w for w in words if w.lower() not in _IMMICH_EN_STOPWORDS]
        s = " ".join(words).strip()
    return s


def _immich_item_to_spoken(item: dict, idx: int) -> str:
    """把 Immich 结果项转成口语描述：优先读真实文件名，其次回退目录位置信息。"""
    stem = os.path.splitext(item.get("originalFileName", "") or "")[0]
    stem = re.sub(r"\[[^\]]*\]", "", stem)  # 去掉 [id]
    stem = re.sub(r"^\d{8}_\d{6}_", "", stem)  # 去掉归档时间戳前缀
    stem = re.sub(r"^\S+?_(?=[一-鿿])", "", stem)  # 去掉归档类别前缀（如 风景_）
    stem_compact = re.sub(r"[_\-. ]+", "", stem).strip()
    stem_readable = re.sub(r"[_\-.]+", " ", stem).strip()

    # 中文名优先紧凑读；英文/数字文件名也直接读文件名，避免“第N张”信息量太低。
    if len(re.findall(r"[一-鿿]", stem_compact)) >= 2:
        return stem_compact[:12]
    if stem_readable:
        return stem_readable[:40]

    if len(re.findall(r"[一-鿿]", stem)) >= 2:
        return stem[:10]
    path = item.get("originalPath", "") or ""
    for p in reversed([x for x in path.split("/") if x][:-1]):
        if re.search(r"[一-鿿]", p):
            return f"{p}里的第{idx}张"
    return f"第{idx}张"


def _immich_item_to_name(item: dict, idx: int) -> str:
    """提取可播报的照片名称；无文件名时回退为 photo N。"""
    raw = (item.get("originalFileName", "") or "").strip()
    stem = os.path.splitext(raw)[0].strip()
    stem = re.sub(r"\[[^\]]*\]", "", stem)
    stem = re.sub(r"^\d{8}_\d{6}_", "", stem)
    stem = re.sub(r"^\S+?_(?=[一-鿿])", "", stem)
    stem = re.sub(r"[_\-.]+", " ", stem).strip()
    if stem:
        return stem[:40]
    return f"photo {idx}"


def _fast_local_immich_reply(_args, user_text: str):
    """语义找照片：直连 Immich smart search，秒回，不经 agent 长链路。"""
    if not (_IMMICH_INTENT_RE.search(user_text) and _IMMICH_OBJECT_RE.search(user_text)):
        return None
    if _IMMICH_EXCLUDE_RE.search(user_text):
        return None
    semantic = _strip_immich_semantic(user_text)
    semantic = semantic.strip("，。！？,.!?；;：: ")
    if len(semantic) < 2:
        return None

    import json as _json
    import urllib.request as _ur

    try:
        req = _ur.Request(
            f"{_IMMICH_API_URL}/api/search/smart",
            data=_json.dumps({"query": semantic, "size": 5}, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "x-api-key": _IMMICH_API_KEY,
            },
        )
        with _ur.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
    except Exception as e:
        print(f"[Immich] api error: {e}")
        return None

    items = (data.get("assets") or {}).get("items") or []
    items = [it for it in items if (it.get("type") or "").upper() == "IMAGE"]
    items = _immich_select_relevant_items(items, semantic, limit=5)
    if not items:
        local_hits = _fallback_local_photo_hits(semantic, limit=3)
        if local_hits:
            if _should_use_english_reply(user_text):
                count_en = _tts_en_count(len(local_hits), user_text)
                return f'Found {count_en} local photo(s) matching "{semantic}", for example: {"; ".join(local_hits)}.'
            if len(local_hits) == 1:
                return f"找到1张{semantic}照片：{local_hits[0]}"
            return f"找到{len(local_hits)}张和{semantic}相关的照片，比如：{'、'.join(local_hits)}。"
        return _lang_reply(user_text, f"相册里没有找到和{semantic}相关的照片。", f'No photos matching "{semantic}" were found in the album.')
    names = [_immich_item_to_spoken(it, i + 1) for i, it in enumerate(items[:3])]
    names_en = [_immich_item_to_name(it, i + 1) for i, it in enumerate(items[:3])]
    if _should_use_english_reply(user_text):
        count_en = _tts_en_count(len(items), user_text)
        return f"Found {count_en} photos related to {semantic}, for example: {'; '.join(names_en)}."
    return f"找到{len(items)}张和{semantic}相关的照片，比如：{'、'.join(names)}。"


# ── 危险指令拦截 ─────────────────────────────────────────────────────────────
_DANGER_WORDS_RE = re.compile(
    r'删除|清空|清除|移走|移除|抹去|格式化|覆盖|全部删|批量删|删光|删掉|删了|删除文件|删除照片'
)
_DANGER_SAFE_RE = re.compile(r'不要删|不能删|别删|防止删|禁止删')
_DANGER_CONFIRM_RE = re.compile(r'确认|执行|是的|对的|没错|好的')


def _is_dangerous_command(text: str) -> bool:
    """含破坏性词汇且无负向安全词时判定为危险指令。"""
    if _DANGER_SAFE_RE.search(text):
        return False
    return bool(_DANGER_WORDS_RE.search(text))


LOCAL_FAST_CHANNELS = (
    ("filter", _fast_local_image_filter_reply),
    ("classify", _fast_local_classify_reply),
    ("directory", _fast_local_directory_listing_reply),
    ("download", _fast_local_download_reply_with_args),
    ("play", _fast_local_play_reply),
    ("immich", _fast_local_immich_reply),
    ("kb", _fast_local_kb_reply),
)


def _run_local_fast_channels(args, user_text: str):
    for name, handler in LOCAL_FAST_CHANNELS:
        try:
            reply = handler(args, user_text)
        except Exception as e:  # noqa: BLE001
            print(f"[FAST] channel={name} error: {e}")
            continue
        if reply is not None:
            print(f"[FAST] hit channel={name}")
            return reply
    return None


def ask_openclaw(args, user_text):
    if args.openclaw_dry_run:
        return f"[dry-run] 你说的是：{user_text}"

    # ── 危险指令二次确认门 ──
    pending_danger = _HTTP_DIALOG_STATE.get("pending_danger_confirm")
    _danger_confirmed = False
    if pending_danger is not None:
        _HTTP_DIALOG_STATE["pending_danger_confirm"] = None
        if _DANGER_CONFIRM_RE.search(user_text):
            user_text = pending_danger["original_text"]
            _danger_confirmed = True  # 已确认，跳过二次拦截
            print(f"[DANGER] user confirmed, executing: {user_text!r}")
        else:
            print(f"[DANGER] user cancelled or unrelated: {user_text!r}")
            return "操作已取消。"

    fast_reply = _run_local_fast_channels(args, user_text)
    if fast_reply is not None:
        return fast_reply

    # 即将调用 agent：对危险指令加一道确认拦截（已确认的跳过）
    if not _danger_confirmed and _is_dangerous_command(user_text):
        _HTTP_DIALOG_STATE["pending_danger_confirm"] = {"original_text": user_text}
        print(f"[DANGER] intercepted for confirmation: {user_text!r}")
        return f"你说的是\"{ user_text[:24] }\"，这是危险操作，请再说\"确认\"来执行，或说\"取消\"放弃。"

    use_english_reply = _should_use_english_reply(user_text)
    reply_language = "English" if use_english_reply else "Chinese"
    bridge_prompt = (
        "你是quectel pi上的对话助手，执行用户口头指令，可调用已有工具（文件/NAS/相册等）。"
        "优先按用户指令语言回复：若用户用英文或混合英文指令，则用英文回答；否则按中文回答。\n"
        f"直接执行给结果，回复简短{reply_language}，不超过20字，一句说完，纯文字，禁止markdown（**加粗**、-列表、#标题）。\n"
        "【执行规则】\n"
        "1.下载视频/音频：必须调用 download_media 工具，禁止编造结果。关键词（默认保存位置）："
        "海洋/大海→海洋纪录片(Movies)、预告片/sintel→Sintel预告片(Movies)、兔子/bunny→Big Buck Bunny(Movies)、"
        "样本/测试→通用样本(Movies)；用户说剧集/电视剧/series/tv show 时保存到 TV Shows，其余默认 Movies；"
        "用户给URL用url参数，说搜索影视名用query参数(yt-dlp)。\n"
        "2.播放库中视频：先尝试Jellyfin播放；无法播放则回复'请在Jellyfin打开'并列出可播放列表，禁止直接说无法播放。\n"
        "3.操作NAS文件：必须用 nas_files 工具(list_directory/move_file/create_directory)，禁止猜测或编造路径。"
        "根目录(/nas_share)可用目录：备份、家庭相册、工作文档、手机相册、旅行。\n"
        "4.照片按内容分类/归档/整理：禁止仅凭文件名猜测，优先 exec 脚本一次性归档："
        "timeout 120 python3 /nas_share/tools/nas_classify.py --dir <目录> --recursive --archive。"
        "默认直接执行，只有用户说'预览/先别动'才干跑；重复前缀文件名自动清理重命名无需询问；脚本失败才回退 nas_files。\n"
        "5.图片复古/日系/胶片滤镜：优先 exec 脚本批处理："
        "timeout 600 python3 /nas_share/tools/image_batch.py --dir <目录> --style <vintage|japanese|film> --recursive。"
        "用户说预览加 --dry-run；输出到<风格名>/子目录，禁止覆盖原图。\n"
        f"用户指令：{user_text}"
    )

    cmd = [
        "docker",
        "exec",
        args.openclaw_container,
        "node",
        "dist/index.js",
        "agent",
        "--session-key",
        f"voice-turn:{int(time.time() * 1000)}",  # 每轮独立会话，防止跨轮上下文误确认
        "--thinking", "off",  # 非推理模型(deepseek 等)不支持 minimal；off 最稳且首响应最快
        "--message",
        bridge_prompt,
        "--json",
    ]

    ok, output = _run_agent_cmd(cmd, timeout_sec=args.openclaw_timeout)
    if not ok and _is_gateway_unreachable_error(output):
        print("[OPENCLAW] gateway unreachable; running health check")
        probe_ok, _ = _probe_openclaw_gateway(args.openclaw_container, timeout_sec=8)
        if probe_ok:
            retry_timeout = min(20, max(5, args.openclaw_timeout))
            print(f"[OPENCLAW] health check passed; retrying once (timeout={retry_timeout}s)")
            ok, output = _run_agent_cmd(cmd, timeout_sec=retry_timeout)

    if not ok:
        low = output.lower()
        if "sudo: a password is required" in low:
            return (
                "OpenClaw调用失败：当前进程没有免密Docker权限。"
                "请先执行 sudo usermod -aG docker pi 并重新登录后重试。"
            )
        if "permission denied while trying to connect to the docker daemon socket" in low:
            return (
                "OpenClaw调用失败：当前用户无Docker权限。"
                "请先执行 sudo usermod -aG docker pi 并重新登录后重试。"
            )
        return f"OpenClaw调用失败：{output}"

    obj = _json_from_mixed_output(output)
    if obj is None:
        return output.strip()[:200] if output.strip() else "OpenClaw未返回可解析内容"

    text = _extract_text_from_agent_json(obj)
    if text:
        return text
    return "OpenClaw返回为空"


_IMPORTANT_REPLY_HINTS = (
    "失败",
    "错误",
    "警告",
    "风险",
    "确认",
    "不可",
    "无法",
    "删除",
    "覆盖",
    "文档里写着",  # KB 内容问答的答案不可被短指令压缩截断
)

_SHORT_CMD_HINTS = (
    "打开",
    "关闭",
    "开始",
    "停止",
    "退出",
    "查询",
    "看",
    "播放",
    "暂停",
    "分类",
    "整理",
    "移动",
    "重命名",
)

_SHORT_MEDIA_PHRASES = {
    "测": "播放测试视频",
    "试": "播放测试视频",
    "测试": "播放测试视频",
    "下测": "下载测试视频",
    "下试": "下载测试视频",
    "下测试": "下载测试视频",
    "帮我下测": "帮我下载测试视频",
    "帮我下试": "帮我下载测试视频",
    "帮我下测试": "帮我下载测试视频",
    "给我下测": "给我下载测试视频",
    "给我下试": "给我下载测试视频",
    "给我下测试": "给我下载测试视频",
}


def _expand_short_media_phrase(text: str) -> str:
    """将高频短口令补全为可执行指令，避免被短句过滤误跳过。"""
    compact = re.sub(r"\s+", "", (text or "").strip())
    compact = compact.strip("，。！？,.!?；;：:")
    if compact in _SHORT_MEDIA_PHRASES:
        return _SHORT_MEDIA_PHRASES[compact]
    return text


_HTTP_INCOMPLETE_PROMPT = "我这边听到你还没说完，请继续说。"
_HTTP_INCOMPLETE_MAX_FOLLOWUPS = 2
# 助手的澄清问句（如"想下载哪个视频？"）后，唤醒通道免唤醒继续收听的次数上限
_HTTP_QUESTION_MAX_FOLLOWUPS = 2
_INCOMPLETE_ENDINGS = (
    "的", "了", "下", "把", "给", "对", "并", "然后", "进行", "处理", "操作", "一下",
)
_INCOMPLETE_ACTION_WORDS = (
    "分类", "归档", "整理", "移动", "重命名", "删除", "复制", "备份",
    "下载", "上传", "创建", "新建", "同步",
    "播放", "放", "暂停", "查询", "查找", "搜索", "查", "找", "看看",
    "处理", "滤镜", "风格", "调色", "复古", "日系", "胶片",
    "显示", "列出", "打开", "关闭",
)
_INCOMPLETE_TARGET_WORDS = (
    # 目标对象
    "照片", "图片", "相册", "文件", "目录", "文件夹",
    "视频", "音乐", "电影", "预告片", "纪录片", "剧集", "电视剧",
    "文档", "报告",
    # 知识库相关对象词（知识库查询触发需要）
    "合同", "方案", "协议", "资料", "预算", "表格", "记录", "说明", "计划",
    # NAS 路径/业务词
    "家庭", "手机", "旅行", "家庭相册", "手机相册",
    "nas_share", "NAS",
)

# 疑问句本身即完整意图（如"我的住房合同在哪"、"有哪些文档"），
# 与 KB 快速通道的意图词保持一致，不应判为"没说完"而追问用户
_COMPLETE_QUESTION_RE = re.compile(
    r"在哪|哪里|哪儿|哪个|是谁|是什么|什么意思|有没有|有哪些|有多少|多少|怎么办|怎么样|(吗|呢|什么)$"
)


def _looks_like_incomplete_command(text: str) -> bool:
    s = re.sub(r"\s+", "", (text or "").strip())
    s = s.strip("，。！？,.!?；;：:")
    if not s:
        return False
    if _is_style_only_phrase(s):
        return True
    if len(s) <= 2:
        return True
    if _COMPLETE_QUESTION_RE.search(s):
        return False

    has_target = any(k in s for k in _INCOMPLETE_TARGET_WORDS)
    has_action = any(k in s for k in _INCOMPLETE_ACTION_WORDS)
    # 同时含目标词+动作词 = 完整指令，优先放行（如"帮我把家庭相册下的照片进行分类"），
    # 避免被下面的"把字句正则"或结尾词误判为没说完而追问用户。
    if has_target and has_action:
        return False
    if s.endswith(_INCOMPLETE_ENDINGS):
        return True
    if re.match(r"^(帮我|请|给我)?把.{0,12}$", s):
        return True
    if has_target and not has_action:
        return True
    return False


# 助手回复若是澄清问句（需要用户补充信息），唤醒通道应免唤醒继续收听答案。
# 限定：以疑问结尾 + 含疑问词，避免把"好的，正在处理"这类陈述误判为问句。
_QUESTION_ENDINGS = ("？", "?", "吗", "呢")
_QUESTION_WORDS = (
    "哪个", "什么", "怎么", "哪里", "哪儿", "哪些", "哪部", "哪种",
    "还是", "是否", "能不能", "要不要", "需要吗", "多少", "几位", "哪位",
    "需要", "帮你", "帮我", "片名", "名称",
)


def _reply_is_clarifying_question(reply: str) -> bool:
    s = (reply or "").strip()
    if not s or len(s) > 24:
        return False
    if not s.endswith(_QUESTION_ENDINGS):
        return False
    return any(w in s for w in _QUESTION_WORDS)


# ── 无关语音过滤 ─────────────────────────────────────────────────────────────
# Level 1: 纯语气词 / 噪音
_IRRELEVANT_EXACT = frozenset({
    "嗯", "啊", "哦", "呢", "哈", "呀", "唉", "哎", "噢", "哟", "喔", "呵", "嘿", "哼",
    "嗯嗯", "嗯哼", "哈哈", "啊啊", "哦哦", "呀呀",
})
_IRRELEVANT_RE = re.compile(r'^[嗯啊哦呢哈呀唉哎噢哟喔呵嘿哼]{1,5}$')

# Level 2: 社交/确认/客套用语 —— 无任务意图，直接跳过
_SOCIAL_IRRELEVANT = frozenset({
    "谢谢", "谢谢你", "谢谢了", "谢谢啊", "谢谢哈", "多谢", "感谢", "不用谢",
    "好的", "好吧", "好呢", "好嘞", "行", "行吧", "行的", "可以", "没问题",
    "知道了", "知道", "明白了", "明白", "懂了", "收到", "了解",
    "对", "对的", "对对", "是的", "是", "没错", "正确",
    "不用了", "算了", "不用",
    "再见", "拜拜",
    "没有", "没", "没事", "没关系",
    "等等", "等一下", "稍等",
    "哇", "哇哦", "厉害", "厉害了", "好厉害", "太棒了", "太好了", "真棒",
})

# Level 3: 任务关键词白名单 —— 含任意一个则视为有任务意图，不过滤
_TASK_KEYWORDS = frozenset({
    # 操作动词
    "分类", "归档", "整理", "移动", "重命名", "删除", "复制", "备份",
    "下载", "上传", "创建", "新建", "同步",
    "播放", "放", "暂停", "查询", "查找", "搜索", "查", "找", "看看",
    "处理", "滤镜", "风格", "调色", "复古", "日系", "胶片",
    "显示", "列出", "打开", "关闭",
    # 目标对象
    "照片", "图片", "相册", "文件", "目录", "文件夹",
    "视频", "音乐", "电影", "预告片", "纪录片", "剧集", "电视剧",
    "文档", "报告",
    # 知识库相关对象词（知识库查询触发需要）
    "合同", "方案", "协议", "资料", "预算", "表格", "记录", "说明", "计划",
    # NAS 路径/业务词
    "家庭", "手机", "旅行", "家庭相册", "手机相册",
    "nas_share", "NAS",
})


def _is_irrelevant_speech(text: str) -> bool:
    """判断 ASR 结果是否为无关输入，需静默跳过（不调用 TTS / OpenClaw）。

    三级过滤：
      L1 - 单字或纯语气音节（嗯/啊/哦…）
      L2 - 社交/确认用语（谢谢/好的/知道了…）
      L3 - 短文本（≤8字）且不含任何任务关键词
    """
    s = text.strip()
    # L1
    if len(s) <= 1:
        return True
    if s in _IRRELEVANT_EXACT:
        return True
    if _IRRELEVANT_RE.match(s):
        return True
    # L2
    if s in _SOCIAL_IRRELEVANT:
        return True
    if _is_style_only_phrase(s):
        return True
    # L3
    if len(s) <= 8 and not any(k in s for k in _TASK_KEYWORDS):
        return True
    return False


def _irrelevant_speech_reason(text: str) -> str | None:
    """返回被无关语音过滤命中的原因；未命中返回 None。"""
    s = text.strip()
    if len(s) <= 1:
        return "L1:len<=1"
    if s in _IRRELEVANT_EXACT:
        return "L1:exact"
    if _IRRELEVANT_RE.match(s):
        return "L1:regex"
    if s in _SOCIAL_IRRELEVANT:
        return "L2:social"
    if len(s) <= 8 and not any(k in s for k in _TASK_KEYWORDS):
        return "L3:short-no-task-keyword"
    return None


def _sanitize_reply_for_tts(reply: str) -> str:
    reply = re.sub(r"/nas_share/?", "", reply)  # 共享目录路径前缀不播报
    reply = re.sub(r"\*+", "", reply)
    reply = re.sub(r"^\s*[-#]+\s*", "", reply, flags=re.MULTILINE)
    reply = re.sub(r"[^\u0000-\u007F\u4e00-\u9fff\u3000-\u303f\uff00-\uffef，。！？、：；\"'（）…—\s]", "", reply)
    reply = re.sub(r"\s+", " ", reply).strip()
    return reply


def _first_sentence(text: str) -> str:
    m = re.search(r"[。！？!?；;]", text)
    if not m:
        return text
    return text[: m.end()].strip()


def _truncate_tts_text(reply: str, max_chars: int) -> str:
    text = (reply or "").strip()
    if not text:
        return ""
    max_chars = max(12, int(max_chars))
    if len(text) <= max_chars:
        return text

    cut = max_chars
    for idx in range(max_chars, 10, -1):
        if text[idx - 1] in "，。！？；;:：、 ":
            cut = idx
            break
    truncated = text[:cut].rstrip("，、；:： .!?,?）)")
    if not truncated:
        truncated = text[:max_chars].rstrip("，、；:： .!?,?）)")
    if not truncated.endswith(("。", "！", "？")):
        truncated += "。"
    return truncated


def _adaptive_tts_reply(user_text: str, reply: str, max_chars: int, brief_max_chars: int, brief_user_len: int):
    """完整播报回复，不再按长度或短命令强制裁剪。"""
    if not reply:
        return "", ""
    return reply, ""


def _jellyfin_play_after_download(user_text: str, raw_reply: str, jellyfin_url: str, api_key: str) -> str:
    """下载完成且含播放意图时，触发 Jellyfin 扫库并在活跃 session 播放。

    返回额外 TTS 播报文本；空字符串表示未触发或无需提示。
    需要先在 Jellyfin 管理后台 → 控制台 → API 密钥 创建密钥，
    并通过 --jellyfin-api-key <key> 传入。
    """
    lower_user_text = (user_text or "").lower()
    has_play_intent = any(k in user_text for k in ("播放", "放一下", "放出来", "看一下", "看看")) or any(
        k in lower_user_text for k in ("play", "watch")
    )
    has_dl_intent = any(k in user_text for k in ("下载", "找一下", "搜一个", "要看")) or any(
        k in lower_user_text for k in ("download", "find", "search")
    )
    if not has_play_intent and not has_dl_intent:
        return ""
    # 触发条件：回复含下载完成类关键词，或用户指令本身含下载意图
    has_completion = any(k in raw_reply for k in (
        "下载完成", "已保存到", "下载已完成", "已下载",
        "已存", "存入", "保存到", "已保存",
    )) or any(
        k in (raw_reply or "").lower()
        for k in ("download complete", "downloaded", "saved to", "file name")
    )
    if not has_completion and not has_dl_intent:
        return ""

    import json as _json
    import re as _re
    import time as _time
    import urllib.parse as _up
    import urllib.request as _ur

    base = jellyfin_url.rstrip("/")
    hdrs_json = {"Authorization": f"MediaBrowser Token={api_key}", "Content-Type": "application/json"}
    hdrs_get  = {"Authorization": f"MediaBrowser Token={api_key}"}

    def _req(method, path, body=None, timeout=8):
        url = base + path
        data = _json.dumps(body).encode() if body is not None else None
        req = _ur.Request(
            url,
            data=data,
            method=method,
            headers=hdrs_json if data else hdrs_get,
        )
        try:
            with _ur.urlopen(req, timeout=timeout) as r:
                content = r.read()
                return _json.loads(content) if content.strip() else {}
        except _ur.HTTPError as err:
            if getattr(err, "code", None) == 401:
                raise PermissionError("Jellyfin API key invalid or unauthorized") from err
            raise

    # 1. 动态查找"扫描媒体库"任务并触发
    try:
        tasks = _req("GET", "/ScheduledTasks")
        scan_id = next(
            (t["Id"] for t in tasks
             if "扫描媒体库" in t.get("Name", "") or "Scan Media" in t.get("Name", "")),
            None,
        )
        if scan_id:
            _req("POST", f"/ScheduledTasks/Running/{scan_id}")
            print(f"[Jellyfin] library scan triggered (taskId={scan_id})")
        else:
            _req("POST", "/Library/Refresh")  # 旧版兜底
            print("[Jellyfin] library refresh triggered (fallback)")
    except PermissionError:
        print("[Jellyfin] unauthorized (401), skip auto scan/play")
        return ""
    except Exception as e:
        print(f"[Jellyfin] scan failed: {e}")
        return ""

    # 2. 从回复提取文件名作为搜索词；提取失败时降级到 user_text 剥离命令词
    m = _re.search(r'文件名\s*[`「]([^`「」\n]+)[`」]', raw_reply)
    if not m:
        m = _re.search(r'文件名[:：]?\s*([^，。\n]+)', raw_reply)
    if not m:
        m = _re.search(r'file name[:：]?\s*([^,.\n]+)', raw_reply, flags=_re.IGNORECASE)
    if not m:
        m = _re.search(r'([A-Za-z0-9_\-\[\] .]+\.(?:mp4|mkv|avi|mov|webm))', raw_reply, flags=_re.IGNORECASE)
    raw_name_stem = ""
    if m:
        raw_name = m.group(1)
        raw_name_stem = _re.sub(r'\.\w{2,5}$', '', raw_name).strip().lower()
        term = _re.sub(r'\s*[\[（【（][^\]）】]*[\]）】]', '', raw_name)
        term = _re.sub(r'\.\w{2,5}$', '', term).strip()
    else:
        # 降级：从用户原始指令中剥离命令词，提取核心内容词
        term = user_text
        for _w in ("帮我", "请帮", "请", "帮", "给我",
                   "下载", "搜索", "查找", "找",
                   "播放", "放一下", "放出来", "看一下", "看看",
               "视频", "电影", "影片", "一下", "一部", "进行", "下",
               "please", "download", "search", "find", "play", "watch",
               "the", "a", "an", "sample", "video", "movie", "trailer"):
            term = term.replace(_w, "")
        term = re.sub(r"(并且播放|并播放|然后播放)$", "", term)
        term = term.replace("并且", "").replace("并", "")
        term = term.strip()
    if not term:
        return ""
    if term in _PLAY_ALIASES:
        term = _PLAY_ALIASES[term]
    print(f"[Jellyfin] search: {term!r} (from={'reply' if m else 'user_text'})")

    # 3. 等扫描写入后搜索（最多 10 次，每次间隔 3 秒）
    item_id, item_name = None, term
    for i in range(10):
        _time.sleep(3)
        try:
            result = _req(
                "GET",
                f"/Items?searchTerm={_up.quote(term)}"
                "&IncludeItemTypes=Movie,Video&Limit=5&Recursive=true",
            )
            items = result.get("Items", [])
            if items:
                item_id   = items[0]["Id"]
                item_name = items[0]["Name"]
                print(f"[Jellyfin] found: {item_name} (id={item_id}) attempt={i+1}")
                break
        except PermissionError:
            print("[Jellyfin] unauthorized (401), skip auto search/play")
            return ""
        except Exception as e:
            print(f"[Jellyfin] search [{i+1}]: {e}")

    # 回退：有些媒体库会把展示名改成中文，按英文文件名搜索不到。
    # 这时按下载文件名去匹配 Item.Path，更稳定。
    if (not item_id) and raw_name_stem:
        try:
            result = _req(
                "GET",
                "/Items?IncludeItemTypes=Movie,Video&Limit=200&Recursive=true&Fields=Path",
            )
            for it in result.get("Items", []):
                p = str(it.get("Path") or "").lower()
                if raw_name_stem in p:
                    item_id = it.get("Id")
                    item_name = it.get("Name") or term
                    print(f"[Jellyfin] path fallback hit: {item_name} (id={item_id})")
                    break
        except PermissionError:
            print("[Jellyfin] unauthorized (401), skip auto path fallback")
            return ""
        except Exception as e:
            print(f"[Jellyfin] path fallback failed: {e}")

    if not item_id:
        return _lang_reply(
            user_text,
            "Jellyfin 库已刷新，视频仍在扫描中，稍后可在家庭影院查看。",
            "Jellyfin was refreshed, but the video is still being scanned. It will appear in the library shortly.",
        )

    # 4. 查找能播放 Video 的活跃 session，发送播放指令
    try:
        sessions = _req("GET", "/Sessions")
        # SupportsRemoteControl 在 Web 客户端通常为 null，改用 PlayableMediaTypes 过滤
        candidates = [
            s for s in sessions
            if "Video" in s.get("Capabilities", {}).get("PlayableMediaTypes", [])
        ]
        if candidates:
            sid = candidates[0]["Id"]
            # Jellyfin 播放接口用 query 参数，不是 body
            _req("POST",
                 f"/Sessions/{sid}/Playing"
                 f"?ItemIds={_up.quote(item_id)}&PlayCommand=PlayNow")
            print(f"[Jellyfin] play sent to session {sid}")
            return f"正在 Jellyfin 播放：{item_name}"
        print("[Jellyfin] no controllable session found")
        opened = _open_jellyfin_in_firefox(jellyfin_url, item_id=item_id)
        if opened:
            for attempt in range(5):
                _time.sleep(2)
                sessions = _req("GET", "/Sessions")
                candidates = [
                    s for s in sessions
                    if "Video" in s.get("Capabilities", {}).get("PlayableMediaTypes", [])
                ]
                if candidates:
                    sid = candidates[0]["Id"]
                    _req("POST",
                         f"/Sessions/{sid}/Playing"
                         f"?ItemIds={_up.quote(item_id)}&PlayCommand=PlayNow")
                    print(f"[Jellyfin] play sent after firefox open: session {sid}")
                    return f"正在 Jellyfin 播放：{item_name}"
        return f"已打开 Jellyfin，正在进入：{item_name}" if opened else f"请在 Jellyfin 播放：{item_name}"
    except PermissionError:
        print("[Jellyfin] unauthorized (401), skip auto play")
        return ""
    except Exception as e:
        print(f"[Jellyfin] play failed: {e}")
        return f"视频已就绪，请在 Jellyfin 播放：{item_name}"


def _resolve_asr_model_path(args, asr_root: Path, engine: str, model_path: str = ""):
    model_value = (model_path or "").strip()
    if engine == "sensevoice":
        return Path(model_value) if model_value else ensure_sensevoice_model(
            asr_root / "model",
            force_download=not args.no_auto_download_asr,
        )
    return None


def _run_single_http_turn(
    args,
    work_dir: Path,
    asr_root: Path,
    tts_root: Path,
    forced_text: str | None = None,
    audio_lock=None,
    turn_source: str = "button",
    frontend_redirect: bool = False,
) -> dict:
    _turn_start = time.monotonic()
    user_wav = work_dir / "user_http.wav"
    reply_wav = work_dir / "reply_http.wav"
    recognizer = None
    keep_models = bool(getattr(args, "http_keep_models", True))
    command_engine = args.command_asr_engine
    command_lang = args.command_asr_language
    command_model = args.command_asr_model

    if not forced_text:
        if keep_models:
            cache_key = (
                command_engine,
                command_lang,
                str(command_model or ""),
                str(args.hotwords_file or ""),
                float(args.hotwords_score),
            )
            recognizer = _HTTP_MODEL_CACHE["recognizers"].get(cache_key)
            if recognizer is None:
                asr_model_path = _resolve_asr_model_path(args, asr_root, command_engine, command_model)
                print(f"[HTTP] building ASR recognizer (engine={command_engine})...")
                recognizer = build_asr_recognizer(
                    asr_root,
                    asr_model_path,
                    command_lang,
                    engine=command_engine,
                    hotwords_file=args.hotwords_file,
                    hotwords_score=args.hotwords_score,
                    force_download=(command_engine == "conformer" and args.download_conformer_model),
                )
                _HTTP_MODEL_CACHE["recognizers"][cache_key] = recognizer
            else:
                print(f"[HTTP] reusing ASR recognizer (engine={command_engine})")
        else:
            asr_model_path = _resolve_asr_model_path(args, asr_root, command_engine, command_model)
            print(f"[HTTP] building ASR recognizer (engine={command_engine})...")
            recognizer = build_asr_recognizer(
                asr_root,
                asr_model_path,
                command_lang,
                engine=command_engine,
                hotwords_file=args.hotwords_file,
                hotwords_score=args.hotwords_score,
                force_download=(command_engine == "conformer" and args.download_conformer_model),
            )

    if keep_models:
        tts = _HTTP_MODEL_CACHE.get("tts")
        if tts is None:
            print("[HTTP] building TTS engine...")
            tts = build_tts(tts_root)
            _HTTP_MODEL_CACHE["tts"] = tts
        else:
            print("[HTTP] reusing TTS engine")
    else:
        print("[HTTP] building TTS engine...")
        tts = build_tts(tts_root)

    try:
        _set_request_context(frontend_redirect)
        listen_round = 0
        text = ""
        while True:
            if forced_text:
                text = forced_text.strip()
                print(f"[HTTP] text mode input: {text}")
            else:
                if listen_round == 0:
                    print("[HTTP] listening for one command...")
                else:
                    print(
                        f"[HTTP] listening for follow-up command... "
                        f"({listen_round}/{_HTTP_INCOMPLETE_MAX_FOLLOWUPS})"
                    )
                _set_bridge_state("listening")
                _t_mic = time.monotonic()
                if audio_lock is not None:
                    audio_lock.acquire()
                try:
                    record_speech_until_silence(
                        user_wav,
                        mic_input=args.mic_input,
                        backend=args.record_backend,
                        min_duration=args.speech_min_duration,
                        max_duration=args.speech_duration,
                        tail_window_sec=args.speech_tail_window,
                        silence_threshold_dbfs=args.speech_silence_threshold_dbfs,
                    )
                finally:
                    if audio_lock is not None:
                        audio_lock.release()
                level = wav_level_dbfs(user_wav)
                print(f"[HTTP] speech clip level: {level:.1f} dBFS")
                snap = _mic_runtime_snapshot(args)
                _log_mic_runtime(args, "http-command-record")
                print(
                    f"[HTTP][AUDIO] mic={snap.get('active_device') or args.mic_input} "
                    f"backend={snap.get('active_backend') or args.record_backend} "
                    f"source={snap.get('active_name') or 'unknown'} "
                    f"{_wav_meta_for_log(user_wav)}"
                )
                print(f"[HTTP][PHASE] record cost={int((time.monotonic() - _t_mic) * 1000)}ms")

                _set_bridge_state("asr")
                _t_asr = time.monotonic()
                raw_text = asr_transcribe(recognizer, user_wav, engine=command_engine)
                print(f"[HTTP][ASR] raw_text={raw_text!r} len={len(raw_text.strip())}")
                print(f"[HTTP][PHASE] asr cost={int((time.monotonic() - _t_asr) * 1000)}ms")
                normalized = normalize_asr_text(raw_text)
                if normalized != raw_text:
                    print(f"[HTTP] normalized: {normalized}")
                text = normalized

            expanded = _expand_short_media_phrase(text)
            if expanded != text:
                print(f"[HTTP][ASR] short phrase expanded: {text!r} -> {expanded!r}")
                text = expanded
            print(f"[HTTP] text: {text}")
            print(f"[HTTP][ASR] final_text={text!r} len={len(text.strip())}")

            if not text:
                return {
                    "ok": True,
                    "message": "未识别到有效语音，请重试。",
                    "text": "",
                    "reply": "",
                }

            pending_filter = _HTTP_DIALOG_STATE.get("pending_image_filter")
            if pending_filter is not None:
                pending_reply, new_pending = _consume_pending_image_filter(text, pending_filter)
                _HTTP_DIALOG_STATE["pending_image_filter"] = new_pending
                if pending_reply is not None:
                    print(f"[HTTP][FILTER] pending reply: {pending_reply}")
                    _set_bridge_state("speaking")
                    tts_speak(tts, pending_reply, reply_wav, play=not args.no_play)
                    _record_voice_turn(
                        source=turn_source,
                        text=text,
                        reply=pending_reply,
                        cost_ms=int((time.monotonic() - _turn_start) * 1000),
                    )
                    return {
                        "ok": True,
                        "message": "已承接上一轮滤镜参数补充。",
                        "text": text,
                        "reply": pending_reply,
                    }

            filter_req = _extract_image_filter_request(text)
            if filter_req is not None and (filter_req["target"] is None or filter_req["style"] is None):
                _HTTP_DIALOG_STATE["pending_image_filter"] = {
                    "target": filter_req["target"],
                    "style": filter_req["style"],
                    "dry": bool(filter_req["dry"]),
                    "attempts": 0,
                }
                missing = "target" if filter_req["target"] is None else "style"
                print(
                    f"[HTTP][FILTER] pending {missing} target={filter_req['target']} "
                    f"style={filter_req['style']} dry={1 if filter_req['dry'] else 0}"
                )
                if filter_req["target"] is None:
                    prompt = _lang_reply(
                        text,
                        _IMAGE_FILTER_TARGET_PROMPT,
                        "Please tell me which folder to process, such as Travel or Family album.",
                    )
                else:
                    prompt = _lang_reply(
                        text,
                        _IMAGE_FILTER_STYLE_PROMPT,
                        "What style do you want: vintage, Japanese, or film?",
                    )
                _set_bridge_state("speaking")
                tts_speak(tts, prompt, reply_wav, play=not args.no_play)
                _record_voice_turn(
                    source=turn_source,
                    text=text,
                    reply=prompt,
                    cost_ms=int((time.monotonic() - _turn_start) * 1000),
                )
                return {
                    "ok": True,
                    "message": "等待补充滤镜参数。",
                    "text": text,
                    "reply": prompt,
                }

            if (not forced_text) and _looks_like_incomplete_command(text):
                if listen_round < _HTTP_INCOMPLETE_MAX_FOLLOWUPS:
                    listen_round += 1
                    print(
                        f"[HTTP][ASR] looks incomplete, continue listening: "
                        f"text={text!r} round={listen_round}/{_HTTP_INCOMPLETE_MAX_FOLLOWUPS}"
                    )
                    tts_speak(tts, _HTTP_INCOMPLETE_PROMPT, reply_wav, play=not args.no_play)
                    continue
                print(
                    f"[HTTP][ASR] looks incomplete but max follow-ups reached, proceed: "
                    f"text={text!r}"
                )

            break

        # 危险指令待确认时，"确认/取消"等短词豁免无关语音过滤
        _skip_irrelevant = _HTTP_DIALOG_STATE.get("pending_danger_confirm") is not None
        if not _skip_irrelevant and _is_irrelevant_speech(text):
            reason = _irrelevant_speech_reason(text) or "unknown"
            hit_keywords = [k for k in _TASK_KEYWORDS if k in text][:3]
            print(
                f"[HTTP][ASR] irrelevant skipped reason={reason} "
                f"text={text!r} hit_task_keywords={hit_keywords}"
            )
            return {
                "ok": True,
                "message": "识别到的是无任务语音，已跳过。",
                "text": text,
                "reply": "",
            }

        print("[HTTP] processing...")
        _set_bridge_state("processing", text)

        _t_agent = time.monotonic()
        raw_reply = ask_openclaw(args, text)
        print(f"[HTTP][PHASE] agent cost={int((time.monotonic() - _t_agent) * 1000)}ms")
        reply = _sanitize_reply_for_tts(raw_reply)
        spoken_reply, _tail = _adaptive_tts_reply(
            text,
            reply,
            max_chars=args.tts_max_chars,
            brief_max_chars=args.tts_brief_max_chars,
            brief_user_len=args.tts_brief_user_len,
        )
        print(f"[HTTP] reply: {spoken_reply}")
        _set_bridge_state("speaking")
        _t_tts = time.monotonic()
        tts_speak(tts, spoken_reply, reply_wav, play=not args.no_play)
        print(f"[HTTP][PHASE] tts cost={int((time.monotonic() - _t_tts) * 1000)}ms")

        jf_hint = ""
        if args.jellyfin_api_key:
            jf_hint = _jellyfin_play_after_download(
                text, raw_reply, args.jellyfin_url, args.jellyfin_api_key
            )
            if jf_hint:
                jf_hint = _sanitize_reply_for_tts(jf_hint)
                print(f"[HTTP][Jellyfin] {jf_hint}")
                tts_speak(tts, jf_hint, reply_wav, play=not args.no_play)

        redirect_url = ""
        if frontend_redirect and _should_redirect_http_ui_to_jellyfin(text, raw_reply, jf_hint):
            redirect_url = _jellyfin_target_url(args.jellyfin_url)

        _record_voice_turn(
            source="text" if forced_text else turn_source,
            text=text,
            reply=spoken_reply,
            cost_ms=int((time.monotonic() - _turn_start) * 1000),
        )
        # 唤醒通道：助手若反问了澄清（如"想下载哪个视频？"），
        # 记录待回答次数，供唤醒循环免唤醒继续收听用户答案。
        if turn_source == "wake":
            _HTTP_DIALOG_STATE["pending_question"] = (
                _HTTP_QUESTION_MAX_FOLLOWUPS if _reply_is_clarifying_question(spoken_reply) else 0
            )
        return {
            "ok": True,
            "message": "文本指令处理完成。" if forced_text else "语音指令处理完成。",
            "text": text,
            "reply": spoken_reply,
            "redirect_url": redirect_url,
            "photo_results": (
                _http_photo_search_results(text, limit=8)
                if (_IMMICH_INTENT_RE.search(text) and _IMMICH_OBJECT_RE.search(text))
                else None
            ),
        }
    finally:
        _set_request_context(False)
        if recognizer is not None and not keep_models:
            del recognizer
        if not keep_models:
            del tts
            gc.collect()
        _set_bridge_state("idle")


def _http_speak_wake_prompt(args, tts_root: Path, work_dir: Path) -> None:
    prompt = (args.http_wakeword_prompt or "").strip()
    if not prompt:
        return

    reply_wav = work_dir / "reply_http_wake.wav"
    keep_models = bool(getattr(args, "http_keep_models", True))
    if keep_models:
        tts = _HTTP_MODEL_CACHE.get("tts")
        if tts is None:
            print("[HTTP][WAKE] building TTS engine...")
            tts = build_tts(tts_root)
            _HTTP_MODEL_CACHE["tts"] = tts
        else:
            print("[HTTP][WAKE] reusing TTS engine")
    else:
        print("[HTTP][WAKE] building TTS engine...")
        tts = build_tts(tts_root)

    try:
        tts_speak(tts, prompt, reply_wav, play=not args.no_play)
    finally:
        if not keep_models:
            del tts
            gc.collect()


def _run_http_wakeword_loop(
    args,
    trigger_lock: threading.Lock,
    audio_lock: threading.Lock,
    work_dir: Path,
    asr_root: Path,
    tts_root: Path,
    wake_words: list[str],
) -> None:
    keep_models = bool(getattr(args, "http_keep_models", True))
    wake_engine = args.wake_asr_engine
    wake_lang = args.wake_asr_language
    wake_model = args.wake_asr_model
    cache_key = (
        wake_engine,
        wake_lang,
        str(wake_model or ""),
        str(args.hotwords_file or ""),
        float(args.hotwords_score),
    )
    wake_recognizer = _HTTP_MODEL_CACHE["recognizers"].get(cache_key) if keep_models else None
    if wake_recognizer is None:
        asr_model_path = _resolve_asr_model_path(args, asr_root, wake_engine, wake_model)
        print(f"[HTTP][WAKE] building ASR recognizer (engine={wake_engine})...")
        wake_recognizer = build_asr_recognizer(
            asr_root,
            asr_model_path,
            wake_lang,
            engine=wake_engine,
            hotwords_file=args.hotwords_file,
            hotwords_score=args.hotwords_score,
            force_download=(wake_engine == "conformer" and args.download_conformer_model),
        )
        if keep_models:
            _HTTP_MODEL_CACHE["recognizers"][cache_key] = wake_recognizer

    # Error-path safeguards: reduce log spam and hot-loop CPU burn when recording fails continuously.
    loop_backoff_sec = max(
        0.5,
        float(os.getenv("VOICE_HTTP_WAKE_ERROR_BACKOFF_INITIAL_SEC", "1.0")),
    )
    max_backoff_sec = max(
        loop_backoff_sec,
        float(os.getenv("VOICE_HTTP_WAKE_ERROR_BACKOFF_MAX_SEC", "30.0")),
    )
    last_loop_error_text = ""
    same_loop_error_count = 0
    last_loop_error_log_ts = 0.0

    while True:
        if trigger_lock.locked():
            time.sleep(0.2)
            continue

        wake_wav = work_dir / "wake_http.wav"
        try:
            if not audio_lock.acquire(blocking=False):
                time.sleep(0.2)
                continue
            try:
                record_audio_auto_backend(
                    wake_wav,
                    duration=args.wake_duration,
                    mic_input=args.mic_input,
                    backend=args.record_backend,
                )
                _log_mic_runtime(args, "http-wake-record")
            finally:
                audio_lock.release()
            level = wav_level_dbfs(wake_wav)
            if args.verbose:
                print(f"[HTTP][WAKE] clip level: {level:.1f} dBFS")
            if level < args.wake_min_level_dbfs:
                if args.verbose:
                    print(
                        f"[HTTP][WAKE] below wake threshold: {level:.1f} dBFS < "
                        f"{args.wake_min_level_dbfs:.1f} dBFS, skipping ASR"
                    )
                continue

            hit, hit_keyword, raw_text = _detect_wakeup_open_asr(
                wake_recognizer,
                wake_wav,
                wake_words,
                wake_engine,
            )

            # HTTP 模式下也做一次低电平增益重试，避免近讲但电平偏低时漏唤醒。
            if (not hit) and (level < args.wake_low_level_dbfs) and (args.wake_boost_db > 0):
                boosted_wav = work_dir / "wake_http_boost.wav"
                if args.verbose:
                    print(
                        f"[HTTP][WAKE] low-level clip ({level:.1f} dBFS), retry with +{args.wake_boost_db:.1f} dB"
                    )
                p = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(wake_wav),
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-af",
                        f"volume={args.wake_boost_db}dB",
                        str(boosted_wav),
                        "-loglevel",
                        "error",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if p.returncode == 0 and boosted_wav.exists():
                    hit, hit_keyword, _raw = _detect_wakeup_open_asr(
                        wake_recognizer,
                        boosted_wav,
                        wake_words,
                        wake_engine,
                    )
                elif args.verbose:
                    err = (p.stderr or p.stdout or "ffmpeg boost failed").strip()
                    print(f"[HTTP][WAKE] boost retry skipped: {err}")

                boosted_wav.unlink(missing_ok=True)

            if not hit:
                if args.verbose:
                    print(
                        f"[HTTP][WAKE] no hit, level={level:.1f} dBFS, "
                        f"asr={raw_text.strip() or '(empty)'!r}"
                    )
                continue

            print(f"[HTTP][WAKE] detected: {hit_keyword or '(unknown)'} via open-asr")
            _set_bridge_state("awake")

            if not trigger_lock.acquire(blocking=False):
                print("[HTTP][WAKE] ignored because another request is running")
                _set_bridge_state("idle")  # 未抢到锁时复位，避免按钮卡在“已唤醒”
                continue

            try:
                _http_speak_wake_prompt(args, tts_root, work_dir)
                time.sleep(0.8)  # 提示音结束后留出缓冲，避免开口音节被切
                result = _run_single_http_turn(
                    args,
                    work_dir,
                    asr_root,
                    tts_root,
                    forced_text=None,
                    audio_lock=audio_lock,
                    turn_source="wake",
                )
                print(f"[HTTP][WAKE] turn done: {result.get('message', '')}")
                wakeword_only_retry_left = 0
                if (
                    result.get("message") == "识别到的是无任务语音，已跳过。"
                    and _is_wakeword_only_text(result.get("text", ""), wake_words)
                ):
                    wakeword_only_retry_left = 1
                # If a pending dialog state exists (e.g. waiting for filter style,
                # or the assistant just asked a clarifying question), keep listening
                # without requiring a new wake word.
                while (
                    wakeword_only_retry_left > 0
                    or
                    _HTTP_DIALOG_STATE.get("pending_image_filter") is not None
                    or _HTTP_DIALOG_STATE.get("pending_question", 0) > 0
                ):
                    if wakeword_only_retry_left > 0:
                        wakeword_only_retry_left -= 1
                        print("[HTTP][WAKE] wakeword repeated as first utterance, listen once more without re-wake...")
                    elif _HTTP_DIALOG_STATE.get("pending_question", 0) > 0:
                        _HTTP_DIALOG_STATE["pending_question"] -= 1
                        print("[HTTP][WAKE] pending question, listening for answer without re-wake...")
                    else:
                        print("[HTTP][WAKE] pending filter, listening for follow-up without re-wake...")
                    result = _run_single_http_turn(
                        args,
                        work_dir,
                        asr_root,
                        tts_root,
                        forced_text=None,
                        audio_lock=audio_lock,
                        turn_source="wake",
                    )
                    print(f"[HTTP][WAKE] turn done: {result.get('message', '')}")
            except Exception as e:  # noqa: BLE001
                print(f"[HTTP][WAKE] turn failed: {e}")
                _HTTP_DIALOG_STATE.pop("pending_image_filter", None)
                _HTTP_DIALOG_STATE["pending_question"] = 0
                _set_bridge_state("idle")  # 异常时复位，避免按钮卡在“已唤醒”
            finally:
                trigger_lock.release()
        except Exception as e:  # noqa: BLE001
            err_text = str(e)
            now = time.time()
            if err_text == last_loop_error_text:
                same_loop_error_count += 1
            else:
                last_loop_error_text = err_text
                same_loop_error_count = 1

            should_log = (
                same_loop_error_count <= 3
                or (now - last_loop_error_log_ts) >= 10.0
            )
            if should_log:
                if same_loop_error_count > 1:
                    print(
                        f"[HTTP][WAKE] loop error x{same_loop_error_count}: {e} "
                        f"(backoff={loop_backoff_sec:.1f}s)"
                    )
                else:
                    print(f"[HTTP][WAKE] loop error: {e}")
                last_loop_error_log_ts = now

            _set_bridge_state("idle")  # 兜底复位，覆盖 acquire/录检测阶段抛异常的路径
            time.sleep(loop_backoff_sec)
            loop_backoff_sec = min(max_backoff_sec, loop_backoff_sec * 2.0)
        else:
            # Any successful loop iteration means recording/ASR path is healthy again.
            loop_backoff_sec = max(
                0.5,
                float(os.getenv("VOICE_HTTP_WAKE_ERROR_BACKOFF_INITIAL_SEC", "1.0")),
            )
            same_loop_error_count = 0
            last_loop_error_text = ""


def _run_http_server(args) -> None:
    check_cmd_exists("ffmpeg")
    check_cmd_exists("ffplay")
    check_cmd_exists("curl")
    check_cmd_exists("docker")

    ensure_openclaw_exec_access(args.openclaw_container)
    sync_runtime_scripts()

    asr_root = Path(args.asr_root)
    tts_root = Path(args.tts_root)
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    global _TURNS_FILE
    _TURNS_FILE = (
        Path(args.log_file).parent / "voice_turns.jsonl"
        if args.log_file
        else work_dir / "voice_turns.jsonl"
    )
    print(f"[TURNS] file: {_TURNS_FILE}")

    wake_words = _parse_wake_words(args.wake_words)
    if not wake_words:
        wake_words = list(DEFAULT_WAKE_WORDS)

    trigger_lock = threading.Lock()
    audio_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *values):
            pass  # print(f"[HTTP] {self.address_string()} - {fmt % values}")

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                _html_response(self, HTTPStatus.OK, _http_ui_html(args.http_trigger_port))
                return
            if parsed.path == "/healthz":
                _json_response(self, HTTPStatus.OK, {
                    "ok": True,
                    "mode": "http-trigger",
                    "busy": trigger_lock.locked(),
                    "port": args.http_trigger_port,
                })
                return
            if parsed.path == "/api/turns":
                since = float(parse_qs(parsed.query).get("since", ["0"])[0] or "0")
                with _VOICE_TURNS_LOCK:
                    turns = [t for t in _VOICE_TURNS if t["ts"] > since]
                _json_response(self, HTTPStatus.OK, {"ok": True, "turns": turns})
                return
            if parsed.path == "/api/status":
                with _BRIDGE_STATE_LOCK:
                    state_copy = dict(_BRIDGE_STATE)
                state_copy["busy"] = trigger_lock.locked()
                state_copy["mic"] = _mic_runtime_snapshot(args)
                _json_response(self, HTTPStatus.OK, {"ok": True, **state_copy})
                return
            if parsed.path == "/api/photos/search":
                self._handle_photo_search(parsed)
                return
            if parsed.path == "/api/photos/file":
                self._handle_photo_file(parsed)
                return
            if parsed.path == "/trigger":
                self._handle_trigger(parsed)
                return
            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/trigger":
                self._handle_trigger(parsed)
                return
            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

        def _handle_trigger(self, parsed):
            req_start = time.monotonic()
            if args.http_trigger_token:
                token = parse_qs(parsed.query).get("token", [""])[0]
                if token != args.http_trigger_token:
                    print(f"[HTTP] trigger rejected: invalid token from={self.address_string()}")
                    _json_response(self, HTTPStatus.FORBIDDEN, {
                        "ok": False,
                        "error": "invalid token",
                    })
                    return

            text = parse_qs(parsed.query).get("text", [""])[0].strip()
            if not text and self.command == "POST":
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                if raw:
                    ctype = (self.headers.get("Content-Type") or "").lower()
                    if "application/json" in ctype:
                        try:
                            obj = json.loads(raw.decode("utf-8", errors="ignore"))
                            if isinstance(obj, dict):
                                text = str(obj.get("text", "")).strip()
                        except Exception:  # noqa: BLE001
                            text = ""
                    elif "application/x-www-form-urlencoded" in ctype:
                        data = parse_qs(raw.decode("utf-8", errors="ignore"))
                        text = (data.get("text", [""]) or [""])[0].strip()

            mode = "text" if text else "voice"
            print(
                f"[HTTP] trigger start method={self.command} mode={mode} "
                f"text_len={len(text)} from={self.address_string()}"
            )

            if not trigger_lock.acquire(blocking=False):
                print(f"[HTTP] trigger busy mode={mode} from={self.address_string()}")
                _json_response(self, HTTPStatus.CONFLICT, {
                    "ok": False,
                    "error": "busy",
                    "message": "上一条语音仍在处理中，请稍后再试。",
                })
                return

            try:
                result = _run_single_http_turn(
                    args,
                    work_dir,
                    asr_root,
                    tts_root,
                    forced_text=text or None,
                    audio_lock=audio_lock,
                    frontend_redirect=self.headers.get("X-Voice-Remote", "") == "1",
                )
                cost_ms = int((time.monotonic() - req_start) * 1000)
                print(f"[HTTP] trigger done mode={mode} cost_ms={cost_ms}")
                _json_response(self, HTTPStatus.OK, result)
            except Exception as e:  # noqa: BLE001
                cost_ms = int((time.monotonic() - req_start) * 1000)
                print(f"[HTTP] trigger failed mode={mode} cost_ms={cost_ms}: {e}")
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {
                    "ok": False,
                    "error": str(e),
                })
            finally:
                trigger_lock.release()

        def _handle_photo_search(self, parsed):
            q = parse_qs(parsed.query).get("q", [""])[0].strip()
            payload = _http_photo_search_results(q, limit=8) if q else {"semantic": "", "items": []}
            _json_response(self, HTTPStatus.OK, {"ok": True, "query": q, "results": payload})

        def _handle_photo_file(self, parsed):
            rel = parse_qs(parsed.query).get("path", [""])[0].strip()
            target = _resolve_http_photo_path(rel)
            if target is None:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid path"})
                return
            try:
                data = target.read_bytes()
                content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                _bytes_response(self, HTTPStatus.OK, data, content_type)
            except Exception as e:  # noqa: BLE001
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(e)})

    if args.http_wakeword:
        wake_thread = threading.Thread(
            target=_run_http_wakeword_loop,
            args=(
                args,
                trigger_lock,
                audio_lock,
                work_dir,
                asr_root,
                tts_root,
                wake_words,
            ),
            daemon=True,
            name="http-wakeword-loop",
        )
        wake_thread.start()
        print(f"[HTTP][WAKE] enabled, mode=open-asr, words={', '.join(wake_words)}")
    else:
        print("[HTTP][WAKE] disabled")

    server = ThreadingHTTPServer((args.http_trigger_host, args.http_trigger_port), Handler)
    print(
        f"[HTTP] ready: http://{args.http_trigger_host}:{args.http_trigger_port} "
        "(click page at /, trigger API at /trigger)"
    )
    server.serve_forever()


def main():
    args = parse_args()
    path = setup_stdout_stderr_tee(args.log_file)
    if path is not None:
        print(f"[LOG] file logging enabled: {path}")
    elif args.log_file:
        print("[LOG] file logging setup failed")
    _ensure_audio_runtime_env()
    _log_mic_runtime(args, "startup")

    use_http_mode = args.http_mode or (not args.wake_mode and not os.isatty(0))
    if use_http_mode:
        _run_http_server(args)
        return

    check_cmd_exists("ffmpeg")
    check_cmd_exists("ffplay")
    check_cmd_exists("curl")
    check_cmd_exists("docker")

    ensure_openclaw_exec_access(args.openclaw_container)
    sync_runtime_scripts()

    asr_root = Path(args.asr_root)
    tts_root = Path(args.tts_root)

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    wake_words = _parse_wake_words(args.wake_words)
    if not wake_words:
        wake_words = list(DEFAULT_WAKE_WORDS)

    if args.session_idle_rounds < 1:
        args.session_idle_rounds = 1

    if args.speech_min_duration < 0.5:
        args.speech_min_duration = 0.5
    if args.speech_duration < args.speech_min_duration:
        args.speech_duration = args.speech_min_duration

    wake_model_path = _resolve_asr_model_path(
        args,
        asr_root,
        args.wake_asr_engine,
        args.wake_asr_model,
    )
    command_model_path = _resolve_asr_model_path(
        args,
        asr_root,
        args.command_asr_engine,
        args.command_asr_model,
    )

    same_asr = (
        args.wake_asr_engine == args.command_asr_engine
        and args.wake_asr_language == args.command_asr_language
        and str(wake_model_path or "") == str(command_model_path or "")
    )

    print(f"[INIT] building wake ASR recognizer (engine={args.wake_asr_engine})...")
    wake_recognizer = build_asr_recognizer(
        asr_root,
        wake_model_path,
        args.wake_asr_language,
        engine=args.wake_asr_engine,
        hotwords_file=args.hotwords_file,
        hotwords_score=args.hotwords_score,
        force_download=(args.wake_asr_engine == "conformer" and args.download_conformer_model),
    )

    if same_asr:
        command_recognizer = wake_recognizer
        print("[INIT] command ASR recognizer reuses wake recognizer")
    else:
        print(f"[INIT] building command ASR recognizer (engine={args.command_asr_engine})...")
        command_recognizer = build_asr_recognizer(
            asr_root,
            command_model_path,
            args.command_asr_language,
            engine=args.command_asr_engine,
            hotwords_file=args.hotwords_file,
            hotwords_score=args.hotwords_score,
            force_download=(args.command_asr_engine == "conformer" and args.download_conformer_model),
        )
    print("[INIT] building TTS engine...")
    tts = build_tts(tts_root)

    print(f"[INIT] wake mode: open-asr ({', '.join(wake_words)})")

    print(f"[INIT] OpenClaw target: container={args.openclaw_container}, session={args.openclaw_session_key}")
    print("[INIT] dialog mode: wake once, then continuous conversation")
    print("[INIT] ready.")

    session_awake = False
    idle_rounds = 0
    pending_reply_detail = ""
    pending_image_filter = None

    while True:
        wake_wav = work_dir / "wake.wav"
        user_wav = work_dir / "user.wav"
        reply_wav = work_dir / "reply.wav"

        if not session_awake:
            print(f"\n[WAIT] say wake word: {', '.join(wake_words)}")

            record_audio_auto_backend(
                wake_wav,
                duration=args.wake_duration,
                mic_input=args.mic_input,
                backend=args.record_backend,
            )
            _log_mic_runtime(args, "cli-wake-record")
            level = wav_level_dbfs(wake_wav)
            if args.verbose:
                print(f"[MIC] wake clip level: {level:.1f} dBFS")
            if level < args.wake_min_level_dbfs:
                if args.verbose:
                    print(
                        f"[MIC] wake clip below threshold {args.wake_min_level_dbfs:.1f} dBFS, "
                        f"skipping ASR (level={level:.1f} dBFS)"
                    )
                continue
            if level < -45:
                print("[MIC] warning: volume is low, move closer or raise gain")

            hit, hit_keyword, raw_text = _detect_wakeup_open_asr(
                wake_recognizer,
                wake_wav,
                wake_words,
                args.wake_asr_engine,
            )

            # 低音量下首次未命中：做一次增益重试，降低漏唤醒
            if (not hit) and (level < args.wake_low_level_dbfs) and (args.wake_boost_db > 0):
                boosted_wav = work_dir / "wake_boost.wav"
                if args.verbose:
                    print(
                        f"[KWS] low-level clip ({level:.1f} dBFS), retry with +{args.wake_boost_db:.1f} dB"
                    )
                p = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(wake_wav),
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-af",
                        f"volume={args.wake_boost_db}dB",
                        str(boosted_wav),
                        "-loglevel",
                        "error",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if p.returncode == 0 and boosted_wav.exists():
                    boosted_level = wav_level_dbfs(boosted_wav)
                    if args.verbose:
                        print(f"[MIC] boosted wake clip level: {boosted_level:.1f} dBFS")
                    hit, hit_keyword, _raw = _detect_wakeup_open_asr(
                        wake_recognizer,
                        boosted_wav,
                        wake_words,
                        args.wake_asr_engine,
                    )
                else:
                    err = (p.stderr or p.stdout or "ffmpeg boost failed").strip()
                    print(f"[KWS] boost retry skipped: {err}")

                boosted_wav.unlink(missing_ok=True)

            if not hit:
                if args.verbose:
                    print(
                        f"[MIC] no hit, level={level:.1f} dBFS, "
                        f"asr={raw_text.strip() or '(empty)'!r}"
                    )
                continue

            print(f"[KWS] wake word detected: {hit_keyword or '(unknown)'}")
            print("[KWS] matched source: open-asr")
            print("[SESSION] wake accepted. Continuous dialog is active.")
            session_awake = True
            idle_rounds = 0

        print("[ASR] listening... (no wake word needed now)")
        print(
            f"[ASR] dynamic capture: min={args.speech_min_duration:.1f}s, "
            f"max={args.speech_duration:.1f}s, silence<{args.speech_silence_threshold_dbfs:.1f}dBFS"
        )

        try:
            record_speech_until_silence(
                user_wav,
                mic_input=args.mic_input,
                backend=args.record_backend,
                min_duration=args.speech_min_duration,
                max_duration=args.speech_duration,
                tail_window_sec=args.speech_tail_window,
                silence_threshold_dbfs=args.speech_silence_threshold_dbfs,
            )
        except Exception as e:
            print(f"[MIC] recording failed, re-listening: {e}")
            continue

        level = wav_level_dbfs(user_wav)
        print(f"[MIC] speech clip level: {level:.1f} dBFS")
        _log_mic_runtime(args, "cli-command-record")

        text = asr_transcribe(command_recognizer, user_wav, engine=args.command_asr_engine)
        normalized = normalize_asr_text(text)
        if normalized != text:
            print(f"[ASR] normalized: {normalized}")
            text = normalized
        expanded = _expand_short_media_phrase(text)
        if expanded != text:
            print(f"[ASR] short phrase expanded: {text!r} -> {expanded!r}")
            text = expanded
        print(f"[ASR] text: {text}")

        if not text:
            idle_rounds += 1
            print(f"[ASR] empty speech ({idle_rounds}/{args.session_idle_rounds})")
            if idle_rounds >= args.session_idle_rounds:
                session_awake = False
                idle_rounds = 0
                print("[SESSION] idle timeout. Wake word is required again.")
            continue

        idle_rounds = 0

        if pending_reply_detail and any(k in text for k in ("继续", "详细", "详情", "补充", "说完")):
            detail = pending_reply_detail
            pending_reply_detail = ""
            reply = f"补充说明：{detail}"
            reply = _sanitize_reply_for_tts(reply)
            spoken, tail = _adaptive_tts_reply(
                text,
                reply,
                max_chars=args.tts_max_chars,
                brief_max_chars=args.tts_brief_max_chars,
                brief_user_len=args.tts_brief_user_len,
            )
            if tail:
                pending_reply_detail = tail
            print(f"[OpenClaw] reply: {spoken}")
            tts_speak(tts, spoken, reply_wav, play=not args.no_play)
            continue

        # Local control keywords for bridge process
        if any(k in text for k in ("休眠", "停止监听", "待机")):
            session_awake = False
            reply = "好的，我先待机。需要时再叫我。"
            print(f"[TTS] reply: {reply}")
            tts_speak(tts, reply, reply_wav, play=not args.no_play)
            continue

        if any(k in text for k in ("退出程序", "关闭语音助手", "关闭对话助手")):
            reply = "好的，我现在退出。"
            print(f"[TTS] reply: {reply}")
            tts_speak(tts, reply, reply_wav, play=not args.no_play)
            print("[EXIT] done")
            break

        if pending_image_filter is not None:
            pending_reply, pending_image_filter = _consume_pending_image_filter(text, pending_image_filter)
            if pending_reply is not None:
                print(f"[FILTER] pending reply: {pending_reply}")
                tts_speak(tts, pending_reply, reply_wav, play=not args.no_play)
                continue

        filter_req = _extract_image_filter_request(text)
        if filter_req is not None and (filter_req["target"] is None or filter_req["style"] is None):
            pending_image_filter = {
                "target": filter_req["target"],
                "style": filter_req["style"],
                "dry": bool(filter_req["dry"]),
                "attempts": 0,
            }
            missing = "target" if filter_req["target"] is None else "style"
            print(
                f"[FILTER] pending {missing} target={filter_req['target']} "
                f"style={filter_req['style']} dry={1 if filter_req['dry'] else 0}"
            )
            prompt = _IMAGE_FILTER_TARGET_PROMPT if filter_req["target"] is None else _IMAGE_FILTER_STYLE_PROMPT
            tts_speak(tts, prompt, reply_wav, play=not args.no_play)
            continue

        if _is_irrelevant_speech(text):
            print(f"[ASR] irrelevant speech skipped: {text!r}")
            continue

        processing_hint = "正在处理，请稍等。"
        print(f"[OpenClaw] processing: {processing_hint}")
        tts_speak(tts, processing_hint, reply_wav, play=not args.no_play)

        reply = ask_openclaw(args, text)
        raw_reply = reply  # 保留原始回复供 Jellyfin 下载检测使用
        reply = _sanitize_reply_for_tts(reply)
        spoken_reply, tail = _adaptive_tts_reply(
            text,
            reply,
            max_chars=args.tts_max_chars,
            brief_max_chars=args.tts_brief_max_chars,
            brief_user_len=args.tts_brief_user_len,
        )
        pending_reply_detail = tail
        if tail:
            print("[TTS] long reply shortened; say '继续' to hear more")

        print(f"[OpenClaw] reply: {spoken_reply}")
        tts_speak(tts, spoken_reply, reply_wav, play=not args.no_play)

        # 下载完成+播放意图时，自动触发 Jellyfin 扫库并播放
        if args.jellyfin_api_key:
            jf_hint = _jellyfin_play_after_download(
                text, raw_reply, args.jellyfin_url, args.jellyfin_api_key
            )
            if jf_hint:
                jf_hint = _sanitize_reply_for_tts(jf_hint)
                print(f"[Jellyfin] {jf_hint}")
                tts_speak(tts, jf_hint, reply_wav, play=not args.no_play)


if __name__ == "__main__":
    main()
