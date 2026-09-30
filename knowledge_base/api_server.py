#!/usr/bin/env python3
"""
NAS Knowledge Base API Server
- Indexes file names/paths + text content from /nas_share via SQLite FTS5
- Exposes:  GET  /healthz
            POST /search  {"query": "..."}
            POST /rescan
- Periodic re-scan every SCAN_INTERVAL seconds (no extra packages needed)
"""
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

NAS_ROOT = Path(os.getenv("NAS_ROOT", "/nas_share")).resolve()
PORT = int(os.getenv("PORT", "8084"))
DB_PATH = Path(os.getenv("DB_PATH", "/data/kb.db"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "10"))
LOG_FILE = os.getenv("LOG_FILE", "/logs/knowledge_base.log").strip()
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUPS = int(os.getenv("LOG_BACKUPS", "3"))

# Directories to skip (inside nas_share)
# knowledge_base_data 是索引库自身所在目录（挂到 /data），排除可避免自索引抖动
EXCLUDE_DIRS = frozenset(["tools", "Immich上传", "knowledge_base_data", ".git", "__pycache__"])
# File suffixes to skip（含 SQLite WAL/SHM，避免被当成待索引文件反复入库）
SKIP_EXTS = frozenset([
    ".immich", ".pyc", ".db", ".db-wal", ".db-shm",
    ".js", ".sh", ".service", ".bin", ".so",
])
# Extensions whose text content can be read directly
TEXT_EXTS = frozenset([".txt", ".md", ".csv", ".log"])
# PDF 内容提取：用系统 pdftotext（poppler-utils），避免引入额外 Python 依赖
PDF_EXTRACT_TIMEOUT_SEC = int(os.getenv("PDF_EXTRACT_TIMEOUT_SEC", "30"))
# 入库内容截断长度（trigram 索引按字符计，够覆盖常见问答片段）
CONTENT_MAX_CHARS = int(os.getenv("CONTENT_MAX_CHARS", "8000"))

_db_lock = threading.Lock()
_log_lock = threading.Lock()


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
        with _log_lock:
            _rotate_log_file(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _rotate_log_file(path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

# 索引内容格式版本：变更提取逻辑（如新增 PDF 提取）时递增，触发全量重建
_CONTENT_SCHEMA_VERSION = 2


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS files USING fts5(
            path,
            name,
            ext,
            category,
            content,
            tokenize='unicode61'
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            path TEXT PRIMARY KEY,
            mtime REAL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        )
    """)
    row = db.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    if (row[0] if row else 0) != _CONTENT_SCHEMA_VERSION:
        # 清空 meta 强制全量重扫（文件本身不删，仅重建索引内容）
        db.execute("DELETE FROM meta")
        db.execute(
            "INSERT OR REPLACE INTO schema_version(id, version) VALUES (1, ?)",
            (_CONTENT_SCHEMA_VERSION,),
        )
        db.commit()
    return db


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _should_skip(rel: Path) -> bool:
    for part in rel.parts:
        if part in EXCLUDE_DIRS or part.startswith("."):
            return True
    return rel.suffix.lower() in SKIP_EXTS


def _read_text(fp: Path) -> str:
    try:
        return fp.read_text(encoding="utf-8", errors="replace")[:CONTENT_MAX_CHARS]
    except Exception:
        return ""


def _extract_pdf_text(fp: Path) -> str:
    """用系统 pdftotext 提取前 20 页文本（stdout 直出，不落盘）。失败返回空串。"""
    try:
        p = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-l", "20", str(fp), "-"],
            capture_output=True,
            timeout=PDF_EXTRACT_TIMEOUT_SEC,
        )
        if p.returncode != 0:
            return ""
        return p.stdout.decode("utf-8", errors="replace")[:CONTENT_MAX_CHARS]
    except Exception:
        return ""


def _extract_content(fp: Path) -> str:
    ext = fp.suffix.lower()
    if ext in TEXT_EXTS:
        return _read_text(fp)
    if ext == ".pdf":
        return _extract_pdf_text(fp)
    return ""


def _iter_indexable_files():
    """遍历可索引文件，并就地剪枝被排除目录。

    原先用 NAS_ROOT.rglob("*")：它会先进入被排除目录、再逐个文件跳过。
    Immich上传 是 Immich 资产库的挂载点，会随照片增长到数万条，每 SCAN_INTERVAL
    秒全量遍历一次纯属浪费。改为 os.walk + dirnames 剪枝后不再进入这些目录。
    """
    for dirpath, dirnames, filenames in os.walk(NAS_ROOT):
        dirnames[:] = [
            d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")
        ]
        for name in filenames:
            fp = Path(dirpath) / name
            rel = fp.relative_to(NAS_ROOT)
            if _should_skip(rel):
                continue
            yield fp, rel


def scan(db: sqlite3.Connection) -> int:
    """Incremental scan: insert new/changed, remove deleted files."""
    current: dict[str, float] = {}
    for fp, rel in _iter_indexable_files():
        try:
            current[str(rel)] = fp.stat().st_mtime
        except OSError:
            pass

    with _db_lock:
        existing = dict(db.execute("SELECT path, mtime FROM meta").fetchall())

        # Remove deleted
        for p in set(existing) - set(current):
            db.execute("DELETE FROM files WHERE path = ?", (p,))
            db.execute("DELETE FROM meta WHERE path = ?", (p,))

        # Insert / update changed
        changed = 0
        for rel_str, mtime in current.items():
            if rel_str in existing and existing[rel_str] == mtime:
                continue
            fp = NAS_ROOT / rel_str
            rel = Path(rel_str)
            parts = rel.parts
            category = parts[0] if parts else ""
            content = _extract_content(fp)

            db.execute("DELETE FROM files WHERE path = ?", (rel_str,))
            db.execute(
                "INSERT INTO files(path, name, ext, category, content) VALUES (?,?,?,?,?)",
                (rel_str, rel.stem, rel.suffix.lower(), category, content),
            )
            db.execute(
                "INSERT OR REPLACE INTO meta(path, mtime) VALUES (?,?)",
                (rel_str, mtime),
            )
            changed += 1

        db.commit()
        total = db.execute("SELECT COUNT(*) FROM meta").fetchone()[0]

    _log(f"[kb] scan done: total={total} changed={changed}")
    return total


def _scan_loop(db: sqlite3.Connection):
    while True:
        time.sleep(SCAN_INTERVAL)
        try:
            scan(db)
        except Exception as e:
            _log(f"[kb] scan error: {e}")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_FTS_UNSAFE = re.compile(r'["\*\(\)\[\]\{\}:^~]')

# 中文/ASCII 连续片段，用于 content LIKE 兜底（unicode61 不切中文词）
_CJK_RUN = re.compile(r"[一-鿿]{2,}")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]{2,}")
# 常见虚词/疑问词，用于把连续中文切成内容词片段
_STOP_SPLIT = re.compile(
    r"我的|你的|他的|她的|里的|的是|是在|在哪|哪里|哪儿|哪个|是什么|什么|怎么|为什么|如何|多少|几|谁|"
    r"的|了|里|在|是|有|和|与|或|吗|呢|吧|啊|请|帮|帮我|给|到|去|来|这|那|就|都|也|还|又|再|才|把|被|让|对|从|向|于|以|等|中|之|其|该|本|每|各|某"
)


def _sanitize_fts(q: str) -> str:
    return _FTS_UNSAFE.sub(" ", q).strip()


def _query_fragments(q: str) -> list:
    frags = []
    for run in _CJK_RUN.findall(q):
        frags += [p for p in _STOP_SPLIT.split(run) if len(p) >= 2]
    frags += _ASCII_WORD.findall(q)
    seen = set()
    out = []
    for f in frags:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out[:4]


def _manual_snippet(content: str, frag: str, width: int = 90) -> str:
    """截取包含 frag 的句子（按行/句号切），比固定窗口更适合播报。"""
    if not content:
        return ""
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if frag not in line:
            continue
        combined = line
        # 行太短（如标题行"第四条 租金及支付方式"）则拼接后续行，保证信息完整
        while len(combined.strip()) < 16 and i + 1 < len(lines):
            i += 1
            combined += " " + lines[i]
        rel = combined.find(frag)
        for sep in ("。", "；", ";"):
            ls = combined.rfind(sep, 0, rel)
            le = combined.find(sep, rel + len(frag))
            if le - ls <= width:
                combined = combined[ls + 1: le if le >= 0 else None]
                break
        snip = " ".join(combined.split()).strip()
        if len(snip) > width:
            snip = snip[:width] + "..."
        return snip
    return ""


def search(db: sqlite3.Connection, query: str, limit: int = MAX_RESULTS) -> list[dict]:
    q = _sanitize_fts(query)
    if not q:
        return []

    results = []
    with _db_lock:
        try:
            rows = db.execute(
                """SELECT path, name, ext, category,
                          snippet(files, 4, '→', '←', '...', 20) AS snip
                   FROM files
                   WHERE files MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (q, limit),
            ).fetchall()
            for path, name, ext, category, snip in rows:
                results.append({
                    "path": f"/nas_share/{path}",
                    "name": name,
                    "ext": ext,
                    "category": category,
                    "snippet": snip,
                })
        except sqlite3.OperationalError:
            # FTS syntax error → fallback to LIKE on name
            try:
                rows = db.execute(
                    "SELECT path, name, ext, category, '' FROM files WHERE name LIKE ? LIMIT ?",
                    (f"%{q}%", limit),
                ).fetchall()
                for path, name, ext, category, _ in rows:
                    results.append({
                        "path": f"/nas_share/{path}",
                        "name": name,
                        "ext": ext,
                        "category": category,
                        "snippet": "",
                    })
            except Exception:
                pass

        # 中文兜底：unicode61 不切中文词，FTS 对中文基本无效（且默认 AND 语义），
        # 用查询中的内容词片段做 name/content LIKE，并手工截取 snippet
        if not results:
            frags = _query_fragments(query)
            if frags:
                try:
                    conds = " OR ".join("(name LIKE ? OR content LIKE ?)" for _ in frags)
                    params = []
                    for f in frags:
                        params += [f"%{f}%", f"%{f}%"]
                    params += [limit]
                    rows = db.execute(
                        f"SELECT path, name, ext, category, content "
                        f"FROM files WHERE {conds} LIMIT ?",
                        params,
                    ).fetchall()
                    for path, name, ext, category, content in rows:
                        snip = ""
                        # 靠后的内容词通常是问题焦点（如"租金"），优先用它截 snippet
                        for f in reversed(frags):
                            snip = _manual_snippet(content, f)
                            if snip:
                                break
                        results.append({
                            "path": f"/nas_share/{path}",
                            "name": name,
                            "ext": ext,
                            "category": category,
                            "snippet": snip,
                        })
                except Exception:
                    pass

    return results


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    db: sqlite3.Connection | None = None

    def log_message(self, fmt, *args):
        pass  # silence access log

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            with _db_lock:
                count = self.db.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
            _json(self, 200, {"ok": True, "indexed": count})
        elif path == "/search":
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            _json(self, 200, {"ok": True, "query": q, "results": search(self.db, q)})
        else:
            _json(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        if path == "/search":
            q = body.get("query", body.get("q", ""))
            results = search(self.db, q)
            _log(f"[kb] search query_len={len(str(q))} result_count={len(results)}")
            _json(self, 200, {"ok": True, "query": q, "results": results})
        elif path == "/rescan":
            _log("[kb] rescan triggered")
            threading.Thread(target=scan, args=(self.db,), daemon=True).start()
            _json(self, 200, {"ok": True, "message": "rescan triggered"})
        else:
            _json(self, 404, {"ok": False, "error": "not found"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    db = open_db()
    Handler.db = db

    _log(f"[kb] log file: {LOG_FILE or '(disabled)'}")
    _log(f"[kb] starting - NAS_ROOT={NAS_ROOT} port={PORT}")
    scan(db)

    threading.Thread(target=_scan_loop, args=(db,), daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    _log(f"[kb] ready on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
