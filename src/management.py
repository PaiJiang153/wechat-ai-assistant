"""Local maintenance operations. No model calls or WeChat actions."""
from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path

from .config import AppConfig, load_config
from .maintenance import backup, cleanup_candidates
from .memory import ProfileStore
from .settings_ui import SettingsEditor


class MaintenanceError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return fallback


class MaintenanceService:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.settings = SettingsEditor(cfg)
        self.previews = {}
        self.base = Path(cfg.source_path).resolve().parent if cfg.source_path else Path(cfg.context.persist_dir).resolve().parent
        self.backup_dir = self.base / "data" / "backups"

    def refresh_config(self):
        if self.cfg.source_path:
            self.cfg = load_config(self.cfg.source_path)

    def status(self):
        cfg = self.cfg
        snapshot = read_json(Path(cfg.logging.dir) / "stats.json", {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        updated = snapshot.get("updated_at", 0)
        updated = updated if isinstance(updated, (int, float)) else 0
        age = max(0, time.time() - updated) if updated else None
        state = "stopped" if snapshot.get("running") is False else "active" if age is not None and age < 100 else "stale" if age is not None else "unknown"
        today = time.strftime("%Y-%m-%d")
        usage = read_json(Path(cfg.logging.dir) / "usage.json", {})
        delivery = read_json(Path(cfg.logging.dir) / "delivery-metrics.json", {})
        usage = usage if isinstance(usage, dict) else {}
        delivery = delivery if isinstance(delivery, dict) else {}
        daily = delivery.get(today) or {}
        attempts = max(0, daily.get("send_attempts", 0))
        targets = [dict(name=g.name, kind="群聊", persona=g.persona or "默认人设", trigger=g.trigger or cfg.chat.trigger)
                   for g in cfg.group_list()]
        targets += [dict(name=p.name, kind="私聊", persona=p.persona or "默认人设", trigger=p.trigger or cfg.chat.trigger)
                    for p in cfg.private_chat_list()]
        return dict(state=state, updated_at=updated, heartbeat_age=age, server_time=time.time(),
                    stats={k: snapshot.get(k, 0) for k in ("received", "replied", "dropped", "errors", "expired", "uptime_seconds")},
                    usage_today=usage.get(today) or {}, delivery_today=daily,
                    average_queue_seconds=round(daily.get("queue_seconds", 0) / attempts, 2) if attempts else 0,
                    usage_history=[dict(day=day, tokens=(value or {}).get("total_tokens", 0)) for day, value in sorted(usage.items())[-7:]],
                    targets=targets, model=cfg.deepseek.model, bot_name=cfg.persona.bot_name,
                    key_configured=bool(cfg.deepseek.resolve_api_key()),
                    retention=dict(chat_days=cfg.retention.chat_days, memory_days=cfg.retention.memory_days, media_days=cfg.retention.media_days),
                    features={section: getattr(cfg, section).enabled for section in ("memory", "proactive", "vision", "asr", "files", "search", "imagegen")})

    def profiles(self):
        cfg = self.cfg
        records = {}
        for directory in (cfg.context.persist_dir, cfg.memory.dir):
            for path in Path(directory).glob("*.json"):
                record = read_json(path, {})
                if not isinstance(record, dict) or not record.get("room_id"):
                    continue
                room = str(record["room_id"])
                item = records.setdefault(room, dict(room_id=room, room_name=record.get("room_name") or room,
                                                    count=0, updated_at=0))
                if record.get("room_name"):
                    item["room_name"] = str(record["room_name"])
                if isinstance(record.get("facts"), list):
                    item.update(count=len(record["facts"]), updated_at=record.get("updated_at", 0))
        return sorted(records.values(), key=lambda item: item["room_name"])

    def profile(self, room):
        if not isinstance(room, str) or not room or len(room) > 500:
            raise MaintenanceError("请选择一个有效的会话。")
        record = ProfileStore(self.cfg.memory.dir).load(room)
        if not record.get("room_name"):
            record["room_name"] = next((r["room_name"] for r in self.profiles() if r["room_id"] == room), room)
        return {key: record.get(key, default) for key, default in
                (("room_id", room), ("room_name", room), ("facts", []), ("updated_at", 0), ("revision", 0), ("evidence", {}))}

    def edit_profile(self, room, facts, revision):
        if type(revision) is not int or revision < 0:
            raise MaintenanceError("缺少档案版本，请重新打开会话。")
        if not isinstance(facts, list) or len(facts) > self.cfg.memory.max_facts:
            raise MaintenanceError(f"最多保留 {self.cfg.memory.max_facts} 条记忆。")
        if any(not isinstance(f, str) or not f.strip() or len(f.strip()) > 80 for f in facts):
            raise MaintenanceError("每条记忆需为 1 到 80 个字符。")
        current = self.profile(room)
        try:
            return ProfileStore(self.cfg.memory.dir).edit(room, facts, current["room_name"], expected_revision=revision)
        except ValueError as exc:
            raise MaintenanceError(str(exc), 409) from exc

    @staticmethod
    def fingerprint(path):
        stat = path.stat()
        return (stat.st_size, stat.st_mtime_ns, stat.st_ino)

    def preview_cleanup(self, policy):
        if not isinstance(policy, dict) or set(policy) != {"chat_days", "memory_days", "media_days"}:
            raise MaintenanceError("请填写三类数据的保留天数。")
        if any(type(v) is not int or not 0 <= v <= 36500 for v in policy.values()):
            raise MaintenanceError("保留天数需为 0 到 36500 之间的整数。")
        cfg = copy.deepcopy(self.cfg)
        for name, value in policy.items():
            setattr(cfg.retention, name, value)
        paths = cleanup_candidates(cfg)
        if len(paths) > 5000:
            raise MaintenanceError("待清理文件超过 5000 项，请缩小保留策略后再预览。")
        items = []
        fingerprints = {}
        for path in paths:
            fingerprints[path] = self.fingerprint(path)
            try:
                label = str(path.relative_to(self.base))
            except ValueError:
                label = path.name
            items.append(dict(name=label, bytes=fingerprints[path][0]))
        token = uuid.uuid4().hex
        with self.lock:
            self.previews = {k: v for k, v in self.previews.items() if v["expires"] > time.time()}
            if len(self.previews) >= 20:
                self.previews.pop(next(iter(self.previews)))
            self.previews[token] = dict(expires=time.time() + 300, fingerprints=fingerprints, cfg=cfg)
        return dict(token=token, files=items, count=len(items), bytes=sum(i["bytes"] for i in items), expires_in=300)

    def apply_cleanup(self, token):
        with self.lock:
            preview = self.previews.pop(token, None) if isinstance(token, str) else None
            if not preview or preview["expires"] < time.time():
                raise MaintenanceError("预览已过期，请重新预览。", 409)
            if self.status()["state"] == "active":
                raise MaintenanceError("机器人最近仍有运行心跳。请先停止机器人，再执行清理。", 409)
            paths = preview["fingerprints"]
            current = set(cleanup_candidates(preview["cfg"]))
            try:
                if any(p not in current or p.is_symlink() or self.fingerprint(p) != fingerprint for p, fingerprint in paths.items()):
                    raise MaintenanceError("待清理文件已发生变化，请重新预览。", 409)
            except FileNotFoundError as exc:
                raise MaintenanceError("待清理文件已发生变化，请重新预览。", 409) from exc
            deleted = 0
            try:
                for path in paths:
                    path.unlink()
                    deleted += 1
            except OSError as exc:
                raise MaintenanceError(f"已清理 {deleted} 项，其余文件未完成，请重新预览。", 409) from exc
            return dict(count=deleted, bytes=sum(v[0] for v in paths.values()))

    def backups(self):
        return [dict(name=p.name, bytes=p.stat().st_size, created_at=p.stat().st_mtime)
                for p in sorted(self.backup_dir.glob("chat-backup-*.zip"), reverse=True)
                if not p.is_symlink() and p.is_file() and self.valid_backup_name(p.name)]

    @staticmethod
    def valid_backup_name(name):
        return isinstance(name, str) and re.fullmatch(r"chat-backup-\d{8}-\d{6}-[a-f0-9]{8}\.zip", name) is not None

    def create_backup(self):
        with self.lock:
            name = f"chat-backup-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.zip"
            result = backup(self.cfg, str(self.backup_dir / name))
            return dict(name=name, files=result["files"], bytes=(self.backup_dir / name).stat().st_size)

    def backup_path(self, name):
        if not self.valid_backup_name(name):
            raise MaintenanceError("备份不存在。", 404)
        path = self.backup_dir / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(self.backup_dir.resolve()):
            raise MaintenanceError("备份不存在。", 404)
        return path

    def logs(self, source="bot"):
        if source not in ("bot", "manage"):
            raise MaintenanceError("请选择机器人或维护台日志。")
        path = Path(self.cfg.logging.dir) / ("bot.log" if source == "bot" else "manage.log")
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - 256 * 1024))
                text = stream.read().decode("utf-8", errors="replace")
            lines = text.splitlines()
            if size > 256 * 1024 and lines:
                lines = lines[1:]
            text = "\n".join(lines[-200:])
        except FileNotFoundError:
            text = ""
        for key in (self.cfg.deepseek.resolve_api_key(), self.cfg.imagegen.resolve_api_key(),
                    self.cfg.deepseek.api_key, self.cfg.imagegen.api_key):
            if key:
                text = text.replace(key, "[已隐藏密钥]")
        text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._-]+", "Bearer [已隐藏]", text)
        text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[已隐藏密钥]", text)
        return dict(text=text, lines=len(text.splitlines()), limit=200)
