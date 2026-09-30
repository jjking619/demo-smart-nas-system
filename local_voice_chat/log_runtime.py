#!/usr/bin/env python3
from __future__ import annotations

import sys
import threading
from pathlib import Path

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3


def rotate_file(path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS) -> None:
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        for i in range(backups - 1, 0, -1):
            src = Path(f"{path}.{i}")
            if src.exists():
                src.replace(Path(f"{path}.{i + 1}"))
        path.replace(Path(f"{path}.1"))
    except Exception:
        pass


def append_line(
    path: Path,
    line: str,
    *,
    max_bytes: int = LOG_MAX_BYTES,
    backups: int = LOG_BACKUPS,
    lock: threading.Lock | None = None,
) -> None:
    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_file(path, max_bytes=max_bytes, backups=backups)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        rotate_file(path, max_bytes=max_bytes, backups=backups)

    try:
        if lock is None:
            _write()
        else:
            with lock:
                _write()
    except Exception:
        pass


class TeeStream:
    """Append writes to a file while preserving terminal output."""

    def __init__(
        self,
        stream,
        log_path: Path,
        *,
        max_bytes: int = LOG_MAX_BYTES,
        backups: int = LOG_BACKUPS,
        check_every_writes: int = 200,
    ):
        self._stream = stream
        self._log_path = log_path
        self._write_count = 0
        self._max_bytes = max_bytes
        self._backups = backups
        self._check_every_writes = max(1, check_every_writes)

    def write(self, data):
        self._stream.write(data)
        append_line(
            self._log_path,
            data,
            max_bytes=self._max_bytes,
            backups=self._backups,
        )
        self._write_count += 1
        if self._write_count % self._check_every_writes == 0:
            rotate_file(self._log_path, max_bytes=self._max_bytes, backups=self._backups)

    def flush(self):
        self._stream.flush()

    def isatty(self):
        return self._stream.isatty()


def setup_stdout_stderr_tee(
    log_file: str | Path,
    *,
    max_bytes: int = LOG_MAX_BYTES,
    backups: int = LOG_BACKUPS,
) -> Path | None:
    if not log_file:
        return None
    try:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_file(path, max_bytes=max_bytes, backups=backups)
        sys.stdout = TeeStream(sys.stdout, path, max_bytes=max_bytes, backups=backups)
        sys.stderr = TeeStream(sys.stderr, path, max_bytes=max_bytes, backups=backups)
        return path
    except Exception:
        return None
