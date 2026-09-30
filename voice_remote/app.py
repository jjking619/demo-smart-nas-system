#!/usr/bin/env python3
import json
import html
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0").strip() or "0.0.0.0"
PORT = int(os.getenv("PORT", "8082"))
UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "http://host.docker.internal:28082").rstrip("/")
TRIGGER_TOKEN = os.getenv("TRIGGER_TOKEN", "").strip()
TRIGGER_TIMEOUT_SEC = int(os.getenv("TRIGGER_TIMEOUT_SEC", "300"))
APP_TITLE = os.getenv("APP_TITLE", "Voice Assistant").strip() or "Voice Assistant"
UI_VERSION = os.getenv("UI_VERSION", str(int(time.time())))
LOG_FILE = os.getenv("LOG_FILE", "/logs/voice_remote.log").strip()
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUPS = int(os.getenv("LOG_BACKUPS", "3"))
MAX_TASKS = int(os.getenv("MAX_TASKS", "120"))
STATIC_DIR = Path(__file__).with_name("static")
APP_JS_FILE = STATIC_DIR / "app.js"
TURNS_FILE = Path(os.getenv("TURNS_FILE", "/logs/voice_turns.jsonl"))
NAS_CONTENT_FILE = Path(os.getenv("NAS_CONTENT_FILE", "/logs/nas_content.json"))
NAS_SHARE_ROOT = Path(os.getenv("NAS_SHARE_ROOT", str(Path.home() / "nas_share"))).expanduser()
MANIFEST_FALLBACK_SCAN_TTL_SEC = int(os.getenv("MANIFEST_FALLBACK_SCAN_TTL_SEC", "45"))

_TASKS = {}
_TASK_ORDER = []
_TASK_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()
_FALLBACK_SCAN_CACHE = {"ts": 0.0, "counts": {"documents": 0, "photos": 0, "videos": 0}}

_PHOTO_PRESET_PRIORITY = ["找海边的照片", "找猫/动物的照片"]
_PHOTO_PRESET_MAP = {
    "seaside": {
        "patterns": [r"海边", r"沙滩", r"beach", r"seaside", r"coast", r"ocean", r"sea"],
        "keywords": ["海边", "沙滩", "beach", "seaside", "coast", "ocean", "sea"],
        "label": "海边",
    },
    "animals": {
        "patterns": [r"猫", r"动物", r"cat", r"dog", r"animal", r"pet"],
        "keywords": ["猫", "狗", "动物", "cat", "dog", "animal", "animals", "pet"],
        "label": "猫/动物",
    },
    "vintage": {
        "patterns": [r"复古", r"滤镜", r"vintage", r"filter"],
        "keywords": [],
        "label": "复古滤镜预览",
    },
}
_PHOTO_SCAN_ROOTS = ["家庭相册", "手机相册", "旅行", "backup", "Family album", "Travel"]


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _rotate_log_file(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < LOG_MAX_BYTES:
            return
        for i in range(LOG_BACKUPS - 1, 0, -1):
            src = Path(f"{path}.{i}")
            if src.exists():
                src.replace(Path(f"{path}.{i + 1}"))
        path.replace(Path(f"{path}.1"))
    except Exception:
        pass


def _log(msg: str) -> None:
    line = f"[{_now_str()}] {msg}"
    print(line, flush=True)
    if not LOG_FILE:
        return
    try:
        path = Path(LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            _rotate_log_file(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _rotate_log_file(path)
    except Exception:
        pass


def _json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler, status, html):
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(handler, status, text, content_type):
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _bytes_response(handler, status, data, content_type):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _load_static_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# 启动自检结果，供 /api/healthz 暴露。目录型 bind mount 的 inode 一旦被替换
# （宿主机 rm -rf + 重建同名目录），容器内会看到一个空目录，前端随即整体失效。
_STATIC_STATUS = {"ok": True, "missing": []}


def _check_static_assets() -> bool:
    required = ("app.js", "i18n.js")
    missing = [name for name in required if not (STATIC_DIR / name).is_file()]
    try:
        entries = len(list(STATIC_DIR.iterdir())) if STATIC_DIR.is_dir() else -1
    except OSError:
        entries = -1
    _STATIC_STATUS["ok"] = not missing
    _STATIC_STATUS["missing"] = missing
    if missing:
        _log(
            "[voice-remote] 前端静态资源缺失: "
            + ", ".join(missing)
            + f"（{STATIC_DIR} 条目数={entries}）。"
            "若宿主机同名目录内确实存在这些文件，说明 bind mount 仍指向"
            "已被替换的旧目录 inode，重启容器即可恢复: docker restart voice_assistant"
        )
    else:
        _log(f"[voice-remote] 静态资源自检通过: {STATIC_DIR}（{entries} 个条目）")
    return not missing


def _upstream_url(path, extra_query=None):
    query = {}
    if TRIGGER_TOKEN:
        query["token"] = TRIGGER_TOKEN
    if extra_query:
        query.update(extra_query)
    q = urllib.parse.urlencode(query)
    return f"{UPSTREAM_BASE_URL}{path}" + (f"?{q}" if q else "")


def _call_upstream_trigger(text=""):
    payload = {"text": text} if text else {}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _upstream_url("/trigger"),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Voice-Remote": "1",
        },
    )
    with urllib.request.urlopen(req, timeout=TRIGGER_TIMEOUT_SEC) as resp:
        raw = resp.read()
        return resp.status, raw.decode("utf-8", errors="ignore")


def _call_upstream_healthz():
    req = urllib.request.Request(_upstream_url("/healthz"), method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return resp.status, raw


def _call_upstream_status():
    req = urllib.request.Request(_upstream_url("/api/status"), method="GET")
    with urllib.request.urlopen(req, timeout=3) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return resp.status, raw


def _new_task(mode: str, text: str):
    task_id = uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)
    task = {
        "id": task_id,
        "mode": mode,
        "text": text,
        "status": "queued",
        "created_ms": now,
        "updated_ms": now,
        "started_ms": None,
        "finished_ms": None,
        "cost_ms": None,
        "result": None,
        "error": "",
        "http_status": None,
    }
    with _TASK_LOCK:
        _TASKS[task_id] = task
        _TASK_ORDER.append(task_id)
        if len(_TASK_ORDER) > MAX_TASKS:
            old_id = _TASK_ORDER.pop(0)
            _TASKS.pop(old_id, None)
    return task


def _set_task(task_id: str, **kwargs):
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return
        task.update(kwargs)
        task["updated_ms"] = int(time.time() * 1000)


def _get_task(task_id: str):
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return None
        return dict(task)


def _run_task(task_id: str, text: str):
    mode = "text" if text else "voice"
    started = time.monotonic()
    _set_task(task_id, status="running", started_ms=int(time.time() * 1000))
    _log(f"[voice-remote] task start id={task_id} mode={mode} text_len={len(text)}")
    try:
        code, raw = _call_upstream_trigger(text=text)
        payload = json.loads(raw) if raw else {"ok": code < 400}
        if text:
            photo_results = _build_photo_results_for_text(text)
            if photo_results:
                payload["photo_results"] = photo_results
        ok = bool(payload.get("ok", code < 400)) and code < 400
        _set_task(
            task_id,
            status="done" if ok else "error",
            result=payload,
            error="" if ok else str(payload.get("error", "upstream failed")),
            http_status=code,
            finished_ms=int(time.time() * 1000),
            cost_ms=int((time.monotonic() - started) * 1000),
        )
        _log(f"[voice-remote] task done id={task_id} mode={mode} status={code} cost_ms={int((time.monotonic() - started) * 1000)}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        try:
            payload = json.loads(body) if body else {"ok": False, "error": e.reason}
        except Exception:
            payload = {"ok": False, "error": body or e.reason}
        _set_task(
            task_id,
            status="error",
            result=payload,
            error=str(payload.get("error", e.reason)),
            http_status=e.code,
            finished_ms=int(time.time() * 1000),
            cost_ms=int((time.monotonic() - started) * 1000),
        )
        _log(f"[voice-remote] task http_error id={task_id} mode={mode} status={e.code} cost_ms={int((time.monotonic() - started) * 1000)} err={e}")
    except Exception as e:
        _set_task(
            task_id,
            status="error",
            result={"ok": False, "error": str(e)},
            error=str(e),
            http_status=502,
            finished_ms=int(time.time() * 1000),
            cost_ms=int((time.monotonic() - started) * 1000),
        )
        _log(f"[voice-remote] task failed id={task_id} mode={mode} cost_ms={int((time.monotonic() - started) * 1000)} err={e}")


def _submit_task(text: str):
    mode = "text" if text else "voice"
    task = _new_task(mode, text)
    th = threading.Thread(target=_run_task, args=(task["id"], text), daemon=True)
    th.start()
    return task


_FALLBACK_ACTIONS = {
    "docs": [
        {"zh": "住房合同在哪", "en": "Where is my housing contract?", "lzh": "住房合同在哪", "len": "Find the contract"},
        {"zh": "住房合同的甲方是谁", "en": "Who signed the housing contract?", "lzh": "合同甲方是谁", "len": "Who signed it"},
        {"zh": "合同编号是多少", "en": "What is the contract number?", "lzh": "合同编号是多少", "len": "Contract number"},
        {"zh": "住房合同里的关键日期", "en": "Key dates in the housing contract", "lzh": "合同关键日期", "len": "Key dates"},
    ],
    "photos": [
        {"zh": "帮我把家庭相册的照片分类，先预览", "en": "Organize my family album, preview first", "lzh": "相册自动分类", "len": "Organize album"},
        {"zh": "把家庭相册的照片加复古滤镜，先预览", "en": "Add a vintage filter to my album, preview first", "lzh": "加复古滤镜", "len": "Vintage filter"},
        {"zh": "找海边的照片", "en": "Show me photos from the seaside", "lzh": "找海边的照片", "len": "Seaside photos"},
        {"zh": "找猫/动物的照片", "en": "Show me photos of animals", "lzh": "找猫/动物的照片", "len": "Animal photos"},
    ],
    "videos": [
        {"zh": "下载测试视频", "en": "Download the sample video", "lzh": "下载测试视频", "len": "Download a video"},
        {"zh": "播放oceans", "en": "Play oceans", "lzh": "播放oceans", "len": "Play oceans"},
    ],
    "ask": [
        {"zh": "简单介绍下你能帮我做什么", "en": "Briefly, what can you help me with?", "lzh": "你能做什么", "len": "What can you do"},
        {"zh": "这台 NAS 上都有什么内容", "en": "What is on this NAS?", "lzh": "NAS 里有什么", "len": "What's on it"},
    ],
}


def _safe_int(value, default=0):
    try:
        return max(0, int(value))
    except Exception:
        return default


def _safe_text(value, default=""):
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


_DOC_EXTS = {
    ".txt", ".md", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".rtf", ".odt"
}
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif", ".tif", ".tiff"}
_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".ts", ".m2ts", ".wmv", ".flv"}


def _fallback_count_from_nas_share():
    now = time.time()
    cached_ts = float(_FALLBACK_SCAN_CACHE.get("ts", 0.0))
    if now - cached_ts <= MANIFEST_FALLBACK_SCAN_TTL_SEC:
        return dict(_FALLBACK_SCAN_CACHE.get("counts", {}))

    counts = {"documents": 0, "photos": 0, "videos": 0}
    root = NAS_SHARE_ROOT
    if not root.exists() or not root.is_dir():
        _FALLBACK_SCAN_CACHE["ts"] = now
        _FALLBACK_SCAN_CACHE["counts"] = counts
        return counts

    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            # 跳过隐藏目录，减少扫描成本
            if "/." in dirpath:
                continue
            for name in filenames:
                ext = Path(name).suffix.lower()
                if ext in _PHOTO_EXTS:
                    counts["photos"] += 1
                elif ext in _VIDEO_EXTS:
                    counts["videos"] += 1
                elif ext in _DOC_EXTS:
                    counts["documents"] += 1
    except Exception as e:  # noqa: BLE001
        _log(f"[voice-remote] fallback scan failed: {e}")

    _FALLBACK_SCAN_CACHE["ts"] = now
    _FALLBACK_SCAN_CACHE["counts"] = counts
    return counts


def _photo_action_sort_key(item: dict) -> tuple[int, str]:
    zh = _safe_text((item or {}).get("zh"))
    try:
        idx = _PHOTO_PRESET_PRIORITY.index(zh)
    except ValueError:
        idx = len(_PHOTO_PRESET_PRIORITY)
    return (idx, zh)


def _reorder_photo_actions(items: list[dict]) -> list[dict]:
    if not items:
        return items
    return sorted(items, key=_photo_action_sort_key)


def _detect_photo_preset(user_text: str) -> str:
    text = (user_text or "").strip().lower()
    if not text:
        return ""
    if not re.search(r"照片|图片|相册|photo|photos|image|images|album", text, flags=re.IGNORECASE):
        return ""
    for key, cfg in _PHOTO_PRESET_MAP.items():
        for pattern in cfg["patterns"]:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return key
    return ""


def _iter_photo_roots() -> list[Path]:
    roots = []
    for name in _PHOTO_SCAN_ROOTS:
        p = NAS_SHARE_ROOT / name
        if p.exists() and p.is_dir():
            roots.append(p)
    if not roots and NAS_SHARE_ROOT.exists() and NAS_SHARE_ROOT.is_dir():
        roots.append(NAS_SHARE_ROOT)
    return roots


def _safe_rel_path(path: Path, root: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except Exception:
        return ""
    return rel.as_posix()


def _search_local_photos_by_preset(preset_key: str, limit: int = 8) -> list[dict]:
    cfg = _PHOTO_PRESET_MAP.get(preset_key)
    if not cfg:
        return []
    keywords = [k.lower() for k in cfg.get("keywords", []) if k]
    if not keywords and preset_key == "vintage":
        return _list_recent_photos_for_preview(limit=limit)
    roots = _iter_photo_roots()
    scored = []
    seq = 0
    for root in roots:
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    ext = Path(name).suffix.lower()
                    if ext not in _PHOTO_EXTS:
                        continue
                    full = Path(dirpath) / name
                    rel = _safe_rel_path(full, NAS_SHARE_ROOT)
                    if not rel:
                        continue
                    rel_l = rel.lower()
                    name_l = name.lower()
                    score = 0
                    for kw in keywords:
                        if kw in name_l:
                            score += 6
                        elif kw in rel_l:
                            score += 2
                    if score <= 0:
                        continue
                    try:
                        mtime = int(full.stat().st_mtime)
                    except Exception:
                        mtime = 0
                    scored.append((score, mtime, -seq, rel, name))
                    seq += 1
        except Exception as e:  # noqa: BLE001
            _log(f"[voice-remote] photo scan failed root={root}: {e}")
            continue

    scored.sort(reverse=True)
    out = []
    seen = set()
    for _score, _mtime, _seq, rel, name in scored:
        if rel in seen:
            continue
        seen.add(rel)
        out.append({
            "name": name,
            "path": rel,
            "preview_url": f"/api/photos/file?path={urllib.parse.quote(rel)}",
        })
        if len(out) >= limit:
            break
    return out


def _list_recent_photos_for_preview(limit: int = 8) -> list[dict]:
    preferred = ["家庭相册", "Family album", "手机相册", "旅行", "Travel", "backup"]
    roots = []
    for name in preferred:
        p = NAS_SHARE_ROOT / name
        if p.exists() and p.is_dir():
            roots.append(p)
    if not roots:
        roots = _iter_photo_roots()

    candidates = []
    for root in roots:
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    ext = Path(name).suffix.lower()
                    if ext not in _PHOTO_EXTS:
                        continue
                    full = Path(dirpath) / name
                    rel = _safe_rel_path(full, NAS_SHARE_ROOT)
                    if not rel:
                        continue
                    try:
                        mtime = int(full.stat().st_mtime)
                    except Exception:
                        mtime = 0
                    candidates.append((mtime, rel, name))
        except Exception as e:  # noqa: BLE001
            _log(f"[voice-remote] vintage preview scan failed root={root}: {e}")

    candidates.sort(reverse=True)
    out = []
    seen = set()
    for _mtime, rel, name in candidates:
        if rel in seen:
            continue
        seen.add(rel)
        out.append({
            "name": name,
            "path": rel,
            "preview_url": f"/api/photos/file?path={urllib.parse.quote(rel)}",
        })
        if len(out) >= limit:
            break
    return out


def _build_photo_results_for_text(user_text: str) -> dict | None:
    preset_key = _detect_photo_preset(user_text)
    if not preset_key:
        return None
    items = _search_local_photos_by_preset(preset_key, limit=8)
    if not items:
        upstream_items = _search_upstream_photos(user_text)
        if upstream_items:
            items = upstream_items
    cfg = _PHOTO_PRESET_MAP.get(preset_key, {})
    return {
        "preset": preset_key,
        "semantic": cfg.get("label") or preset_key,
        "items": items,
    }


def _resolve_photo_path(raw_rel_path: str) -> Path | None:
    rel = (raw_rel_path or "").strip().lstrip("/")
    if not rel:
        return None
    root = NAS_SHARE_ROOT.resolve()
    target = (NAS_SHARE_ROOT / rel).resolve()
    try:
        target.relative_to(root)
    except Exception:
        return None
    if not target.is_file():
        return None
    if target.suffix.lower() not in _PHOTO_EXTS:
        return None
    return target


def _search_upstream_photos(query: str) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    try:
        url = _upstream_url("/api/photos/search", {"q": q})
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        data = json.loads(raw) if raw else {}
    except Exception as e:  # noqa: BLE001
        _log(f"[voice-remote] upstream photo search failed: {e}")
        return []

    src_items = (((data or {}).get("results") or {}).get("items") or [])
    out = []
    for item in src_items:
        rel = _safe_text((item or {}).get("path"))
        if not rel:
            continue
        out.append({
            "name": _safe_text((item or {}).get("name"), Path(rel).name),
            "path": rel,
            "preview_url": f"/api/photos/file?path={urllib.parse.quote(rel)}",
        })
    return out


def _fetch_upstream_photo_file(raw_rel_path: str) -> tuple[bytes, str] | None:
    rel = (raw_rel_path or "").strip().lstrip("/")
    if not rel:
        return None
    try:
        url = _upstream_url("/api/photos/file", {"path": rel})
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            return data, ctype
    except Exception as e:  # noqa: BLE001
        _log(f"[voice-remote] upstream photo file fetch failed path={rel}: {e}")
        return None


def _safe_prompt_item(item):
    if not isinstance(item, dict):
        return None
    zh = _safe_text(item.get("zh"))
    en = _safe_text(item.get("en"), zh)
    lzh = _safe_text(item.get("lzh"), zh)
    len_ = _safe_text(item.get("len"), en)
    if not zh and not en:
        return None
    if not zh:
        zh = en
    if not en:
        en = zh
    if not lzh:
        lzh = zh
    if not len_:
        len_ = en
    return {
        "zh": zh[:180],
        "en": en[:180],
        "lzh": lzh[:90],
        "len": len_[:90],
    }


def _load_nas_manifest():
    raw = {}
    has_manifest = False
    try:
        if NAS_CONTENT_FILE.is_file():
            raw = json.loads(NAS_CONTENT_FILE.read_text(encoding="utf-8"))
            has_manifest = True
    except Exception as e:  # noqa: BLE001
        _log(f"[voice-remote] failed to read nas manifest: {e}")

    counts = raw.get("counts") if isinstance(raw, dict) else {}
    actions = {}
    for key in ("docs", "photos", "videos", "ask"):
        src = raw.get(key) if isinstance(raw, dict) else None
        items = []
        if isinstance(src, list):
            for item in src:
                cleaned = _safe_prompt_item(item)
                if cleaned:
                    items.append(cleaned)
                if len(items) >= 8:
                    break
        if not items:
            items = list(_FALLBACK_ACTIONS[key])
        if key == "photos":
            items = _reorder_photo_actions(items)
        actions[key] = items

    # 计数优先使用 NAS 实时扫描结果（带 TTL 缓存），避免 manifest 过期导致删/增文件后不更新。
    # 若 NAS 根目录不可用，再回退到 manifest 内计数。
    live_counts = _fallback_count_from_nas_share()
    has_live_root = NAS_SHARE_ROOT.exists() and NAS_SHARE_ROOT.is_dir()
    fallback_counts = live_counts if has_live_root else {
        "documents": _safe_int((counts or {}).get("documents", 0)),
        "photos": _safe_int((counts or {}).get("photos", 0)),
        "videos": _safe_int((counts or {}).get("videos", 0)),
    }

    return {
        "generated_at": _safe_text((raw or {}).get("generated_at"), ""),
        "counts": {
            "documents": fallback_counts["documents"],
            "photos": fallback_counts["photos"],
            "videos": fallback_counts["videos"],
        },
        "actions": actions,
    }


def _render_count(doc_count, photo_count, video_count):
    return {
        "docs": f'<span class="zh">{doc_count} 份文档</span><span class="en">{doc_count} document(s)</span>',
        "photos": f'<span class="zh">{photo_count} 张照片</span><span class="en">{photo_count} photo(s)</span>',
        "videos": f'<span class="zh">{video_count} 个视频</span><span class="en">{video_count} video(s)</span>',
        "ask": "&nbsp;",
    }


def _render_chips(items):
    out = []
    for item in items:
        out.append(
            "<button type=\"button\" class=\"qa\" "
            f"data-prompt-zh=\"{html.escape(item['zh'], quote=True)}\" "
            f"data-prompt-en=\"{html.escape(item['en'], quote=True)}\" "
            f"data-label-zh=\"{html.escape(item['lzh'], quote=True)}\" "
            f"data-label-en=\"{html.escape(item['len'], quote=True)}\">"
            f"{html.escape(item['lzh'])}</button>"
        )
    return "\n            ".join(out)


def _task_card(icon, title_key, hint_key, count_html, actions):
    chips = _render_chips(actions)
    return f"""<article class=\"task-card\">\n        <div class=\"tc-head\">\n          <span class=\"tc-icon\">{icon}</span>\n          <div class=\"tc-title\">\n            <h3 data-i18n=\"{title_key}\">{title_key}</h3>\n            <p class=\"tc-sub\" data-i18n=\"{hint_key}\">{hint_key}</p>\n          </div>\n        </div>\n        <div class=\"tc-count\">{count_html}</div>\n        <div class=\"chips\">\n            {chips}\n        </div>\n      </article>"""


def _index_html():
    manifest = _load_nas_manifest()
    doc_count = manifest["counts"]["documents"]
    photo_count = manifest["counts"]["photos"]
    video_count = manifest["counts"]["videos"]
    count_text = _render_count(doc_count, photo_count, video_count)
    generated_at = html.escape(manifest.get("generated_at") or _now_str())

    docs_card = _task_card("📄", "catDocs", "catDocsHint", count_text["docs"], manifest["actions"]["docs"])
    photos_card = _task_card("🖼", "catPhotos", "catPhotosHint", count_text["photos"], manifest["actions"]["photos"])
    videos_card = _task_card("🎬", "catVideos", "catVideosHint", count_text["videos"], manifest["actions"]["videos"])
    ask_card = _task_card("✨", "catAsk", "catAskHint", count_text["ask"], manifest["actions"]["ask"])

    return f"""<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{APP_TITLE}</title>
  <style>
    :root {{
      --bg-1: #09121a;
      --bg-2: #143247;
      --card: rgba(255, 255, 255, 0.08);
      --line: rgba(255, 255, 255, 0.15);
      --text: #f6f8fb;
      --muted: #a5b7c6;
      --accent: #f59e0b;
      --accent2: #ea580c;
      --ok: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      min-height: 100dvh;
      font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(245,158,11,0.2), transparent 36%),
        radial-gradient(circle at bottom right, rgba(14,165,233,0.2), transparent 32%),
        linear-gradient(135deg, var(--bg-1), var(--bg-2));
      display: flex;
      flex-direction: column;
      padding: 0;
    }}
    .app {{
      flex: 1;
      width: 100%;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 20px;
      padding: clamp(20px, 4vw, 52px);
    }}
    h1 {{ margin: 0; font-size: clamp(28px, 4vw, 44px); }}
    .lead {{ margin: 0; color: var(--muted); line-height: 1.7; font-size: clamp(15px, 1.6vw, 18px); }}
    .grid {{ display: grid; gap: 20px; grid-template-columns: 1fr 1fr; }}
    .card {{
      background: rgba(0,0,0,0.2);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 16px;
      padding: 16px;
    }}
    .card h2 {{ margin: 0 0 10px; font-size: 20px; }}
    .hint {{ margin: 0 0 12px; color: var(--muted); line-height: 1.65; font-size: 14px; }}
    .btn {{
      width: 100%;
      border: 0;
      border-radius: 16px;
      padding: 18px 16px;
      font-size: 19px;
      font-weight: 700;
      color: #fff;
      cursor: pointer;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      box-shadow: 0 10px 25px rgba(234,88,12,0.33);
    }}
    .btn:disabled {{ opacity: 0.62; cursor: not-allowed; }}
    .row {{ display: flex; gap: 10px; }}
    input[type=text] {{
      flex: 1;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.2);
      background: rgba(0,0,0,0.25);
      color: var(--text);
      padding: 11px 12px;
      outline: none;
      font-size: 15px;
    }}
    .meta {{ margin-top: 10px; color: var(--muted); font-size: 13px; }}
    @media (max-width: 880px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
        .turns-wrap {{
            flex: 1 1 auto;
            min-height: 0;
            display: flex;
            flex-direction: column;
            background: rgba(0,0,0,0.22);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px;
            padding: 18px;
        }}
        .turns-hdr {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }}
    .turns-hdr h2 {{ margin: 0; font-size: 18px; }}
    .clear-btn {{ background: none; border: 1px solid rgba(255,255,255,0.2); color: var(--muted); border-radius: 8px; padding: 4px 10px; font-size: 13px; cursor: pointer; }}
    .turns {{ flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 10px; overflow-y: auto; }}
    .turn {{ background: rgba(0,0,0,0.22); border: 1px solid rgba(255,255,255,0.1); border-radius: 12px; padding: 10px 14px; font-size: 14px; line-height: 1.65; }}
        .turn-live {{
            border-style: dashed;
            position: sticky;
            top: 0;
            z-index: 1;
            background: rgba(3,12,19,0.92);
        }}
    .turn-src {{ color: var(--accent); font-size: 12px; margin-bottom: 4px; }}
    .turn-txt {{ color: var(--text); }}
    .turn-rep {{ color: var(--ok); margin-top: 4px; }}
    .turn-meta {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}

        html[lang^="en"] .zh {{ display: none !important; }}
        html:not([lang^="en"]) .en {{ display: none !important; }}

        .topbar {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }}
        .ask-title {{ margin: 4px 0 0; font-size: clamp(20px, 2.2vw, 26px); }}
        .tasks {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); }}
        .task-card {{
            background: rgba(0,0,0,0.22);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 18px;
            padding: 18px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}
        .tc-head {{ display: flex; align-items: center; gap: 12px; }}
        .tc-icon {{ font-size: 28px; line-height: 1; }}
        .tc-title h3 {{ margin: 0; font-size: 20px; }}
        .tc-sub {{ margin: 2px 0 0; color: var(--muted); font-size: 13px; }}
        .tc-count {{ color: var(--accent); font-size: 13px; font-weight: 700; }}
        .chips {{ display: flex; flex-wrap: wrap; gap: 8px; }}
        .qa {{
            border: 1px solid rgba(255,255,255,0.22);
            background: rgba(255,255,255,0.06);
            color: var(--text);
            border-radius: 999px;
            padding: 8px 14px;
            font-size: 14px;
            cursor: pointer;
        }}
        .qa:hover {{ background: rgba(245,158,11,0.18); border-color: var(--accent); }}
        .qa:disabled {{ opacity: 0.5; cursor: not-allowed; }}
        .manifest-note {{ grid-column: 1 / -1; color: var(--muted); font-size: 12px; }}

        .entry {{
            display: flex;
            flex-direction: column;
            gap: 10px;
            background: rgba(0,0,0,0.18);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px;
            padding: 14px 16px;
        }}
        .entry-label {{ color: var(--muted); font-size: 13px; }}
        .btn-ghost {{ width: auto; align-self: flex-start; padding: 12px 20px; font-size: 16px; }}
        .btn-send {{ width: 120px; padding: 12px 16px; font-size: 16px; }}
        .jump-link {{ color: #93c5fd; font-size: 13px; margin-top: 4px; display: none; }}
        .jump-link a {{ color: #bfdbfe; }}
        .photo-modal {{
            position: fixed;
            inset: 0;
            display: none;
            align-items: center;
            justify-content: center;
            background: rgba(0, 0, 0, 0.55);
            z-index: 999;
            padding: 16px;
        }}
        .photo-modal.show {{ display: flex; }}
        .photo-panel {{
            width: min(1080px, 96vw);
            max-height: 92vh;
            overflow: auto;
            background: rgba(9, 18, 26, 0.96);
            border: 1px solid rgba(255,255,255,0.18);
            border-radius: 16px;
            padding: 16px;
        }}
        .photo-panel-head {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }}
        .photo-title {{ margin: 0; font-size: 18px; }}
        .photo-close {{
            border: 1px solid rgba(255,255,255,0.2);
            background: transparent;
            color: var(--text);
            border-radius: 10px;
            cursor: pointer;
            padding: 6px 10px;
        }}
        .photo-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
            gap: 12px;
        }}
        .photo-item {{
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 12px;
            overflow: hidden;
        }}
        .photo-item img {{
            width: 100%;
            aspect-ratio: 1 / 1;
            object-fit: cover;
            display: block;
            background: rgba(255,255,255,0.08);
        }}
        .photo-meta {{ padding: 8px 10px 10px; }}
        .photo-name {{ font-size: 12px; color: var(--text); line-height: 1.4; word-break: break-all; margin-bottom: 6px; }}
        .photo-open {{ color: #93c5fd; font-size: 12px; text-decoration: none; }}
        .photo-empty {{ color: var(--muted); font-size: 14px; }}
  </style>
</head>
<body>
  <main class="app" data-ui-version="{UI_VERSION}">
        <div class="topbar">
            <div>
                <h1 data-i18n="appTitle">智能 NAS</h1>
                <p class="lead" data-i18n="leadText">你的文件，一句话就够。</p>
            </div>
      <button id="langToggle" type="button" style="padding:8px 12px; border-radius:999px; border:1px solid rgba(255,255,255,0.2); background:rgba(255,255,255,0.06); color:var(--text); cursor:pointer; font-size:13px;">English</button>
    </div>

        <h2 class="ask-title" data-i18n="askTitle">你想做什么？</h2>

        <section class="tasks">
            {docs_card}
            {photos_card}
            {videos_card}
            {ask_card}
            <div class="manifest-note"><span class="zh">清单时间 {generated_at}</span><span class="en">Indexed {generated_at}</span></div>
    </section>

        <section class="entry">
            <div class="entry-label" data-i18n="entryLabel">点击“说一句”按钮后说话，或呼叫“小远同学”唤醒；也可以打字直接输入。</div>
            <div class="row">
                <button id="voiceBtn" class="btn btn-ghost" data-i18n="voiceBtnStart">🎙 说一句</button>
                <input id="textInput" type="text" data-i18n-placeholder="textInputPlaceholder" placeholder="也可以直接打字，一句话就行" />
                <button id="textBtn" class="btn btn-send" data-i18n="textBtn">发送</button>
            </div>
        </section>

        <div id="status" style="display:none;" data-i18n="statusIdle">待机中。点上面的卡片，或说话/打字。</div>
        <div id="redirectHint" class="jump-link"></div>
        <div id="photoModal" class="photo-modal" aria-hidden="true">
            <section class="photo-panel">
                <div class="photo-panel-head">
                    <h3 id="photoModalTitle" class="photo-title">找到的照片</h3>
                    <button id="photoModalClose" type="button" class="photo-close">关闭</button>
                </div>
                <div id="photoGrid" class="photo-grid"></div>
                <div id="photoEmpty" class="photo-empty" style="display:none;">没有找到可展示的图片。</div>
            </section>
        </div>
        <div class="meta"><span class="zh">上游服务：{UPSTREAM_BASE_URL} · 版本：{UI_VERSION}</span><span class="en">Upstream: {UPSTREAM_BASE_URL} · build {UI_VERSION}</span></div>
    <section class="turns-wrap">
      <div class="turns-hdr">
                <h2 data-i18n="turnsTitle">最近活动</h2>
        <button class="clear-btn" id="clearTurns" data-i18n="clearTurns">清空</button>
      </div>
      <div id="turns" class="turns"></div>
    </section>
  </main>

  <script>
    window.__voiceUiLoaded = false;
        function _reportUiStatus(message) {{
            if (typeof window.__voiceSetStatus === 'function') {{
                window.__voiceSetStatus(message);
                return;
            }}
            const turnsEl = document.getElementById('turns');
            if (!turnsEl) return;
            let live = document.getElementById('liveStatusTurn');
            if (!live) {{
                live = document.createElement('div');
                live.id = 'liveStatusTurn';
                live.className = 'turn turn-live';
                live.innerHTML = '<div class="turn-src">🧾 实时状态</div><div class="turn-txt"></div>';
                turnsEl.prepend(live);
            }}
            const txt = live.querySelector('.turn-txt');
            if (txt) txt.textContent = message;
        }}
    window.addEventListener('error', function (event) {{
      const detail = event && event.message ? '：' + event.message : '';
        _reportUiStatus('页面脚本异常' + detail);
    }});
    window.addEventListener('unhandledrejection', function (event) {{
      const detail = event && event.reason ? '：' + String(event.reason) : '';
        _reportUiStatus('页面脚本异常' + detail);
    }});
    window.addEventListener('load', function () {{
      window.setTimeout(function () {{
        if (window.__voiceUiLoaded) return;
            _reportUiStatus('页面脚本异常：脚本未完成加载');
            fetch('/static/app.js', {{ cache: 'no-store' }}).then(function (resp) {{
                if (resp.ok) return;
                _reportUiStatus(
                    '页面脚本异常：app.js 返回 HTTP ' + resp.status +
                    '（服务端静态资源缺失，请重启 voice_assistant 容器后刷新）'
                );
            }}).catch(function () {{}});
      }}, 1200);
    }});
  </script>
  <script src=\"/static/i18n.js?v={UI_VERSION}\"></script>
  <script src=\"/static/app.js?v={UI_VERSION}\"></script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *values):
        line = fmt % values
        # 前端页面每秒轮询 /api/status 等会产生大量 200 访问噪音且无诊断价值，
        # 关键事件（trigger/task 结果/错误）均已单独 _log，故跳过所有成功响应。
        if " 200 " in line:
            return
        _log(f"[voice-remote] {self.address_string()} - {line}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            _html_response(self, HTTPStatus.OK, _index_html())
            return
        if parsed.path in {"/static/app.js", "/static/i18n.js"}:
            target = APP_JS_FILE if parsed.path.endswith("app.js") else STATIC_DIR / "i18n.js"
            try:
                _text_response(
                    self,
                    HTTPStatus.OK,
                    _load_static_text(target),
                    "application/javascript",
                )
            except FileNotFoundError:
                _log(f"[voice-remote] static file missing: {target}")
                _text_response(
                    self,
                    HTTPStatus.NOT_FOUND,
                    f"console.error('{target.name} not found');",
                    "application/javascript",
                )
            except Exception as e:  # noqa: BLE001
                _log(f"[voice-remote] static file load failed: {e}")
                _text_response(
                    self,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"console.error('{target.name} load failed');",
                    "application/javascript",
                )
            return
        if parsed.path == "/api/healthz":
            try:
                code, raw = _call_upstream_healthz()
                payload = json.loads(raw) if raw else {}
                payload["proxy_ok"] = True
                payload["ui_version"] = UI_VERSION
                payload["ui_static_ok"] = _STATIC_STATUS["ok"]
                if _STATIC_STATUS["missing"]:
                    payload["ui_static_missing"] = _STATIC_STATUS["missing"]
                _json_response(self, code, payload)
            except Exception as e:  # noqa: BLE001
                _json_response(self, HTTPStatus.BAD_GATEWAY, {
                    "ok": False,
                    "proxy_ok": False,
                    "error": str(e),
                })
            return
        if parsed.path == "/api/status":
            try:
                code, raw = _call_upstream_status()
                payload = json.loads(raw) if raw else {}
                _json_response(self, code, payload)
            except Exception:  # noqa: BLE001
                _json_response(self, HTTPStatus.OK, {"ok": True, "state": "idle", "busy": False})
            return
        if parsed.path == "/api/photos/search":
            self._handle_photo_search(parsed)
            return
        if parsed.path == "/api/photos/file":
            self._handle_photo_file(parsed)
            return
        if parsed.path.startswith("/api/task/"):
            task_id = parsed.path.rsplit("/", 1)[-1].strip()
            self._handle_task_get(task_id)
            return
        if parsed.path == "/api/task":
            task_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0].strip()
            self._handle_task_get(task_id)
            return
        if parsed.path == "/api/turns":
            self._handle_turns(parsed)
            return
        if parsed.path == "/api/trigger":
            self._handle_trigger(parsed)
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/trigger":
            self._handle_trigger(parsed)
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def _parse_text(self, parsed):
        text = urllib.parse.parse_qs(parsed.query).get("text", [""])[0].strip()
        if text:
            return text
        if self.command != "POST":
            return ""
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return ""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                obj = json.loads(raw.decode("utf-8", errors="ignore"))
                if isinstance(obj, dict):
                    return str(obj.get("text", "")).strip()
            except Exception:
                return ""
        elif "application/x-www-form-urlencoded" in ctype:
            data = urllib.parse.parse_qs(raw.decode("utf-8", errors="ignore"))
            return (data.get("text", [""]) or [""])[0].strip()
        return ""

    def _handle_trigger(self, parsed):
        text = self._parse_text(parsed)
        mode = "text" if text else "voice"
        task = _submit_task(text)
        _log(
            f"[voice-remote] trigger accepted id={task['id']} "
            f"method={self.command} mode={mode} text_len={len(text)} from={self.client_address[0]}"
        )
        _json_response(self, HTTPStatus.ACCEPTED, {
            "ok": True,
            "task_id": task["id"],
            "status": task["status"],
            "poll": f"/api/task/{task['id']}",
        })

    def _handle_photo_search(self, parsed):
        q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
        results = _build_photo_results_for_text(q) if q else None
        _json_response(self, HTTPStatus.OK, {"ok": True, "query": q, "results": results or {"items": []}})

    def _handle_photo_file(self, parsed):
        rel = urllib.parse.parse_qs(parsed.query).get("path", [""])[0].strip()
        target = _resolve_photo_path(rel)
        if target is not None:
            try:
                data = target.read_bytes()
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                _bytes_response(self, HTTPStatus.OK, data, ctype)
                return
            except Exception as e:  # noqa: BLE001
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(e)})
                return

        upstream = _fetch_upstream_photo_file(rel)
        if upstream is None:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid path"})
            return
        data, ctype = upstream
        _bytes_response(self, HTTPStatus.OK, data, ctype)

    def _handle_task_get(self, task_id: str):
        if not task_id:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing task id"})
            return
        task = _get_task(task_id)
        if not task:
            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "task not found"})
            return
        _json_response(self, HTTPStatus.OK, {"ok": True, "task": task})

    def _handle_turns(self, parsed):
        since = float(urllib.parse.parse_qs(parsed.query).get("since", ["0"])[0] or "0")
        turns = []
        try:
            if TURNS_FILE.exists():
                with TURNS_FILE.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            t = json.loads(line)
                            if float(t.get("ts", 0)) > since:
                                turns.append(t)
                        except Exception:
                            pass
        except Exception as e:
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(e)})
            return
        _json_response(self, HTTPStatus.OK, {"ok": True, "turns": turns})


def main():
    _log(f"[voice-remote] log file: {LOG_FILE or '(disabled)'}")
    _check_static_assets()
    server = ThreadingHTTPServer((LISTEN_HOST, PORT), Handler)
    _log(f"[voice-remote] listening at http://{LISTEN_HOST}:{PORT}, upstream={UPSTREAM_BASE_URL}, ui={UI_VERSION}")
    server.serve_forever()


if __name__ == "__main__":
    main()
