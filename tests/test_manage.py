"""Maintenance UI tests: temporary data only, no model or WeChat connections."""
from __future__ import annotations

import json
import os
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import yaml

from src.config import load_config
from src.context import ContextStore
from src.memory import ProfileStore
from src.runtime import atomic_json
from src.settings_ui import SettingsEditor, SettingsConflict, patch_yaml
from web.manage import create_management_app


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text('''# My custom comment must remain.
deepseek:
  api_key: "sk-secret-private-value"
  model: "test-model"
  temperature: 1.2 # my temperature note
wechat:
  adapter: "mock"
  groups:
    - name: "测试群"
persona:
  system_prompt: |
    保留这份人设。
    包括换行和标点。
imagegen:
  api_key: "image-secret-value"
''', encoding="utf-8")
    cfg = load_config(path)
    app = create_management_app(cfg)
    app.config["TESTING"] = True
    return app, cfg, app.test_client(), {"X-Management-Token": app.extensions["maintenance_token"]}


def test_management_page_and_health_do_not_need_a_model(bundle):
    app, cfg, client, headers = bundle
    response = client.get("/manage")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "高级设置" in text and "长期记忆" in text
    assert "sk-secret-private-value" not in text
    assert "image-secret-value" not in text
    assert client.get("/manage/health").get_json()["application"] == "dsbot-management"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert client.get("/").status_code == 302


def test_access_requires_local_host_and_page_token(bundle):
    app, cfg, client, headers = bundle
    assert client.get("/api/manage/status").status_code == 403
    assert client.get("/api/manage/status", headers=headers).status_code == 200
    assert client.get("/manage", base_url="http://attacker.example").status_code == 403
    assert client.get("/manage", environ_base={"REMOTE_ADDR": "192.168.1.10"}).status_code == 403
    assert client.get("/api/manage/settings", headers={**headers, "Origin": "https://attacker.example"}).status_code == 403


def test_status_distinguishes_fresh_stale_and_stopped(bundle):
    app, cfg, client, headers = bundle
    assert client.get("/api/manage/status", headers=headers).get_json()["state"] == "unknown"
    path = Path(cfg.logging.dir) / "stats.json"
    atomic_json(path, {"updated_at": time.time(), "received": 42})
    data = client.get("/api/manage/status", headers=headers).get_json()
    assert data["state"] == "active" and data["stats"]["received"] == 42
    assert "sk-secret-private-value" not in json.dumps(data)
    atomic_json(path, {"updated_at": time.time() - 200})
    assert client.get("/api/manage/status", headers=headers).get_json()["state"] == "stale"
    atomic_json(path, {"updated_at": time.time(), "running": False})
    assert client.get("/api/manage/status", headers=headers).get_json()["state"] == "stopped"


def test_memory_edit_and_background_revision_conflict(bundle):
    app, cfg, client, headers = bundle
    store = ProfileStore(cfg.memory.dir)
    store.edit("r", ["original"], "测试群")
    response = client.get("/api/manage/memory", headers=headers).get_json()
    assert response["profiles"][0]["room_name"] == "测试群"
    record = client.get("/api/manage/memory/profile?room=r", headers=headers).get_json()
    saved = client.put("/api/manage/memory/profile", headers=headers,
                       json={"room_id": "r", "revision": record["revision"], "facts": ["corrected"]})
    assert saved.status_code == 200 and saved.get_json()["facts"] == ["corrected"]
    conflict = client.put("/api/manage/memory/profile", headers=headers,
                          json={"room_id": "r", "revision": record["revision"], "facts": ["stale"]})
    assert conflict.status_code == 409
    assert store.load("r")["facts"] == ["corrected"]
    current = saved.get_json()
    assert client.put("/api/manage/memory/profile", headers=headers,
                      json={"room_id": "r", "revision": current["revision"], "facts": []}).status_code == 200
    assert store.load("r")["facts"] == []


def test_chat_without_profile_appears_in_memory_list(bundle):
    app, cfg, client, headers = bundle
    context = ContextStore(cfg.context.persist_dir)
    room = context.get("new-room", "新群")
    context.save(room)
    assert client.get("/api/manage/memory", headers=headers).get_json()["profiles"][0]["room_name"] == "新群"


def test_cleanup_preview_is_exact_and_requires_current_token(bundle):
    app, cfg, client, headers = bundle
    root = Path(cfg.vision.media_dir)
    root.mkdir(parents=True)
    old, new = root / "old.gif", root / "new.gif"
    old.write_bytes(b"gif"); new.write_bytes(b"new")
    os.utime(old, (1, 1))
    policy = {"chat_days": 0, "memory_days": 0, "media_days": 30}
    preview = client.post("/api/manage/cleanup/preview", headers=headers, json=policy).get_json()
    assert preview["count"] == 1 and old.exists()
    newer_eligible = root / "another-old.gif"
    newer_eligible.write_bytes(b"old"); os.utime(newer_eligible, (1, 1))
    applied = client.post("/api/manage/cleanup/apply", headers=headers, json={"token": preview["token"]})
    assert applied.status_code == 200
    assert not old.exists() and new.exists() and newer_eligible.exists()
    assert client.post("/api/manage/cleanup/apply", headers=headers, json={"token": preview["token"]}).status_code == 409


def test_cleanup_rejects_changed_files_and_running_bot(bundle):
    app, cfg, client, headers = bundle
    root = Path(cfg.vision.media_dir); root.mkdir(parents=True)
    path = root / "old.gif"; path.write_bytes(b"old"); os.utime(path, (1, 1))
    policy = {"chat_days": 0, "memory_days": 0, "media_days": 1}
    preview = client.post("/api/manage/cleanup/preview", headers=headers, json=policy).get_json()
    path.write_bytes(b"changed")
    assert client.post("/api/manage/cleanup/apply", headers=headers, json={"token": preview["token"]}).status_code == 409
    os.utime(path, (1, 1))
    preview = client.post("/api/manage/cleanup/preview", headers=headers, json=policy).get_json()
    atomic_json(Path(cfg.logging.dir) / "stats.json", {"updated_at": time.time()})
    assert client.post("/api/manage/cleanup/apply", headers=headers, json={"token": preview["token"]}).status_code == 409
    assert path.exists()


def test_backup_can_be_downloaded_without_config_or_keys(bundle):
    app, cfg, client, headers = bundle
    ProfileStore(cfg.memory.dir).edit("r", ["fact"])
    result = client.post("/api/manage/backups", headers=headers, json={})
    assert result.status_code == 201
    name = result.get_json()["name"]
    assert client.get("/api/manage/backups", headers=headers).get_json()["backups"][0]["name"] == name
    download = client.get(f"/api/manage/backups/{name}/download", headers=headers)
    assert download.status_code == 200
    with zipfile.ZipFile(BytesIO(download.data)) as archive:
        assert not any("config" in filename for filename in archive.namelist())
        assert len(archive.namelist()) == 1
    assert client.get(f"/api/manage/backups/{name}/download").status_code == 403
    assert client.get("/api/manage/backups/config.yaml/download", headers=headers).status_code == 404


def test_logs_are_bounded_and_keys_are_redacted(bundle):
    app, cfg, client, headers = bundle
    path = Path(cfg.logging.dir) / "bot.log"; path.parent.mkdir(parents=True)
    path.write_text("line\n" * 250 + "key sk-secret-private-value image-secret-value Bearer abcd_1234\n", encoding="utf-8")
    data = client.get("/api/manage/logs", headers=headers).get_json()
    assert data["lines"] == 200
    assert "sk-secret-private-value" not in data["text"] and "image-secret-value" not in data["text"]
    assert "Bearer abcd_1234" not in data["text"]
    (path.parent / "manage.log").write_text("INFO 维护台记录\n", encoding="utf-8")
    assert "维护台记录" in client.get("/api/manage/logs?source=manage", headers=headers).get_json()["text"]
    assert client.get("/api/manage/logs?source=../../config.yaml", headers=headers).status_code == 400


def test_settings_save_preserves_comments_secrets_persona_and_undo(bundle):
    app, cfg, client, headers = bundle
    path = Path(cfg.source_path); original = path.read_bytes()
    data = client.get("/api/manage/settings", headers=headers).get_json()
    assert "sk-secret-private-value" not in json.dumps(data)
    assert "image-secret-value" not in json.dumps(data)
    result = client.put("/api/manage/settings", headers=headers, json={"revision": data["revision"], "changes": {
        "deepseek.temperature": 0.7, "context.window_messages": 40, "humanize.double_texts": ["甲", "乙"],
        "persona.system_prompt": "新的默认人设\n保持多行。"}})
    assert result.status_code == 200, result.get_json()
    updated = path.read_text(encoding="utf-8")
    assert "# My custom comment must remain." in updated and "# my temperature note" in updated
    parsed = yaml.safe_load(updated)
    assert parsed["deepseek"]["api_key"] == "sk-secret-private-value"
    assert parsed["imagegen"]["api_key"] == "image-secret-value"
    assert parsed["context"]["window_messages"] == 40
    assert parsed["humanize"]["double_texts"] == ["甲", "乙"]
    assert parsed["persona"]["system_prompt"] == "新的默认人设\n保持多行。"
    restored = client.post("/api/manage/settings/undo", headers=headers, json={"revision": result.get_json()["revision"]})
    assert restored.status_code == 200 and path.read_bytes() == original


@pytest.mark.parametrize("changes", [
    {"deepseek.api_key": "new-key"}, {"context.persist_dir": "C:/"},
    {"chat.max_reply_chunks": 0}, {"chat.delivery_timeout": True},
    {"humanize.split_chance": 1.1}, {"deepseek.temperature": float("nan")},
    {"proactive.active_from": "25:00"}, {"proactive.text_chance": 1.0},
    {"humanize.typing_min": 5, "humanize.typing_max": 1},
    {"deepseek.model": ""},
])
def test_invalid_settings_never_change_config(bundle, changes):
    app, cfg, client, headers = bundle
    path = Path(cfg.source_path); original = path.read_bytes()
    data = client.get("/api/manage/settings", headers=headers).get_json()
    result = client.put("/api/manage/settings", headers=headers, json={"revision": data["revision"], "changes": changes})
    assert result.status_code == 400, result.get_json()
    assert path.read_bytes() == original


def test_external_configuration_change_blocks_save_and_restore(bundle):
    app, cfg, client, headers = bundle
    path = Path(cfg.source_path)
    first = client.get("/api/manage/settings", headers=headers).get_json()
    path.write_text(path.read_text(encoding="utf-8") + "\n# External edit\n", encoding="utf-8")
    assert client.put("/api/manage/settings", headers=headers,
                      json={"revision": first["revision"], "changes": {"deepseek.temperature": 0.6}}).status_code == 409
    current = client.get("/api/manage/settings", headers=headers).get_json()
    saved = client.put("/api/manage/settings", headers=headers,
                       json={"revision": current["revision"], "changes": {"deepseek.temperature": 0.6}}).get_json()
    path.write_text(path.read_text(encoding="utf-8") + "\n# Another edit\n", encoding="utf-8")
    assert client.post("/api/manage/settings/undo", headers=headers, json={"revision": saved["revision"]}).status_code == 409
    assert "# Another edit" in path.read_text(encoding="utf-8")


def test_yaml_patch_handles_crlf_multiline_and_missing_sections():
    original = 'chat:\r\n  trigger: "all" # keep\r\npersona:\r\n  system_prompt: |\r\n    a\r\n    b\r\n  extra_rules: "x"\r\n'
    result = patch_yaml(original, {"chat.debounce_seconds": 3, "persona.system_prompt": "new\nline", "memory.enabled": False})
    parsed = yaml.safe_load(result)
    assert parsed["chat"]["trigger"] == "all" and parsed["chat"]["debounce_seconds"] == 3
    assert parsed["persona"]["extra_rules"] == "x" and parsed["persona"]["system_prompt"] == "new\nline"
    assert parsed["memory"]["enabled"] is False and "# keep" in result


def test_settings_authentication_failure_cannot_write(bundle):
    app, cfg, client, headers = bundle
    path = Path(cfg.source_path); original = path.read_bytes()
    assert client.put("/api/manage/settings", json={"changes": {"deepseek.temperature": 0.4}}).status_code == 403
    assert path.read_bytes() == original


def test_malformed_yaml_is_reported_without_echoing_secrets(bundle):
    app, cfg, client, headers = bundle
    Path(cfg.source_path).write_text('deepseek: [sk-secret-private-value', encoding="utf-8")
    result = client.get("/api/manage/settings", headers=headers)
    assert result.status_code == 400
    assert "sk-secret-private-value" not in result.get_data(as_text=True)


def test_yaml_patch_empty_section_and_no_final_newline():
    result = patch_yaml("chat:\npersona:\n  bot_name: 'fish'", {"chat.debounce_seconds": 3, "persona.extra_rules": "new"})
    parsed = yaml.safe_load(result)
    assert parsed["chat"]["debounce_seconds"] == 3
    assert parsed["persona"]["extra_rules"] == "new"
    assert parsed["persona"]["bot_name"] == "fish"
