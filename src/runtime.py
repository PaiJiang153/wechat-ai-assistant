"""Small shared primitives for bounded work, delivery receipts and local metrics."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
import logging
from contextlib import contextmanager
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@dataclass(frozen=True)
class DeliveryResult:
    status: str  # confirmed / failed / unknown / expired
    detail: str = ""
    queue_seconds: float = 0.0

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"


class SendReceipt(Future):
    """Resolved by the GUI thread after sending, never by merely enqueueing."""

    def __init__(self) -> None:
        super().__init__()
        self.created_at = time.monotonic()


class UsageMeter:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.data: dict = {}
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass

    def record(self, **counts: int | float) -> None:
        with self.lock:
            day = time.strftime("%Y-%m-%d")
            bucket = self.data.setdefault(day, {})
            for name, value in counts.items():
                bucket[name] = bucket.get(name, 0) + value
            self.data = dict(sorted(self.data.items())[-90:])
            if self.path:
                try:
                    atomic_json(self.path, self.data)
                except OSError:
                    logging.getLogger("dsbot.metrics").warning("无法保存用量统计", exc_info=True)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.data.get(time.strftime("%Y-%m-%d"), {}))


@contextmanager
def file_lock(path: Path):
    """Cross-process lock for profile edits and background extraction commits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
