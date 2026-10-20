"""Explicit, previewable maintenance of bot-owned data."""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path


def cleanup_candidates(cfg, now: float | None = None) -> list[Path]:
    now = time.time() if now is None else now
    policies = [(cfg.context.persist_dir, cfg.retention.chat_days, "*.json"),
                (cfg.memory.dir, cfg.retention.memory_days, "*.json"),
                (cfg.vision.media_dir, cfg.retention.media_days, "**/*"),
                (cfg.files.dir, cfg.retention.media_days, "**/*"),
                (cfg.imagegen.out_dir, cfg.retention.media_days, "gen_*")]
    candidates = set()
    for directory, days, pattern in policies:
        root = Path(directory).resolve()
        if days <= 0:
            continue
        if root == Path(root.anchor) or root == Path.cwd().resolve():
            raise ValueError(f"拒绝清理根目录或项目目录：{root}")
        for path in root.glob(pattern):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            if path.name in ("quota.json",) or path.suffix in (".lock", ".tmp"):
                continue
            if path.stat().st_mtime < now - days * 86400:
                candidates.add(path.resolve())
    generated = Path(cfg.imagegen.out_dir).resolve()
    if cfg.imagegen.max_keep > 0:
        images = sorted((p for p in generated.glob("gen_*") if p.is_file() and not p.is_symlink()),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        # Never remove recent attachments which may still be in a send queue.
        grace = max(cfg.chat.delivery_timeout, cfg.wechat.task_ttl_seconds) + 60
        for path in images[cfg.imagegen.max_keep:]:
            if path.resolve().is_relative_to(generated) and now - path.stat().st_mtime > grace:
                candidates.add(path.resolve())
    return sorted(candidates)


def cleanup(cfg, *, apply: bool = False) -> dict:
    paths = cleanup_candidates(cfg)
    total = sum(path.stat().st_size for path in paths)
    if apply:
        for path in paths:
            path.unlink(missing_ok=True)
    return {"applied": apply, "files": [str(p) for p in paths], "bytes": total}


def backup(cfg, output: str) -> dict:
    destination = Path(output).resolve()
    if destination.exists():
        raise ValueError("备份文件已存在，请选择新的文件名")
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    # Keys, raw media and unrelated project files are deliberately excluded.
    sources = {"rooms": cfg.context.persist_dir, "memory": cfg.memory.dir,
               "stickers": cfg.stickers.dir, "state": cfg.wechat.state_dir}
    try:
        with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
            for label, directory in sources.items():
                root = Path(directory).resolve()
                for path in root.glob("*.json"):
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        continue
                    # Read each atomic JSON snapshot completely before archiving.
                    content = path.read_text(encoding="utf-8")
                    json.loads(content)
                    archive.writestr(f"{label}/{path.name}", content)
                    count += 1
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {"path": str(destination), "files": count}
