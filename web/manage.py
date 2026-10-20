"""Loopback-only maintenance interface, standalone or alongside the demo."""
from __future__ import annotations

import ipaddress
import logging
import secrets
import threading
import yaml
from urllib.parse import urlsplit

from flask import Blueprint, Flask, jsonify, render_template, request, send_file, redirect

from src.management import MaintenanceError, MaintenanceService
from src.settings_ui import SettingsConflict

log = logging.getLogger("dsbot.manage")


def register_management(app: Flask, cfg):
    service = MaintenanceService(cfg)
    token = secrets.token_urlsafe(32)
    blueprint = Blueprint("management", __name__)
    app.extensions["maintenance"] = service
    app.extensions["maintenance_token"] = token

    @blueprint.before_request
    def local_access():
        host = urlsplit("http://" + request.host).hostname
        try:
            local = ipaddress.ip_address(request.remote_addr or "127.0.0.1").is_loopback
        except ValueError:
            local = False
        if not local or host not in ("localhost", "127.0.0.1", "::1"):
            return jsonify(error="维护台仅支持从本机访问。"), 403
        if request.path.startswith("/api/manage/"):
            if not secrets.compare_digest(request.headers.get("X-Management-Token", ""), token):
                return jsonify(error="页面凭证已失效，请按 F5 刷新浏览器后重新打开维护台。"), 403
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(error="请从维护台页面执行操作。"), 403

    @blueprint.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise MaintenanceError("请求格式不正确，请刷新页面后重试。")
        return data

    @blueprint.errorhandler(MaintenanceError)
    def operation_error(exc):
        return jsonify(error=str(exc)), exc.status

    @blueprint.errorhandler(SettingsConflict)
    def settings_conflict(exc):
        return jsonify(error=str(exc)), 409

    @blueprint.errorhandler(ValueError)
    def validation_error(exc):
        return jsonify(error=str(exc)), 400

    @blueprint.errorhandler(OSError)
    def disk_error(exc):
        log.exception("维护操作无法读写数据")
        return jsonify(error="无法读写本地文件，请检查目录权限、磁盘空间及文件是否被占用。"), 500

    @blueprint.errorhandler(yaml.YAMLError)
    def configuration_error(exc):
        return jsonify(error="配置文件格式有误，请先修正配置后再重新读取。"), 400

    @blueprint.get("/manage")
    @blueprint.get("/manage/")
    def index():
        return render_template("manage.html", management_token=token)

    @blueprint.get("/manage/health")
    def health():
        return jsonify(application="dsbot-management", version=1)

    @blueprint.get("/api/manage/status")
    def status():
        service.refresh_config()
        result = service.status()
        result["can_shutdown"] = callable(app.extensions.get("maintenance_shutdown"))
        return jsonify(result)

    @blueprint.post("/api/manage/shutdown")
    def shutdown():
        stop = app.extensions.get("maintenance_shutdown")
        if not callable(stop):
            raise MaintenanceError("当前维护页与聊天测试台共用服务，请关闭启动窗口退出。")
        timer = threading.Timer(0.25, stop)
        timer.daemon = True
        timer.start()
        return jsonify(ok=True)

    @blueprint.get("/api/manage/memory")
    def memory_list():
        return jsonify(profiles=service.profiles(), max_facts=service.cfg.memory.max_facts)

    @blueprint.get("/api/manage/memory/profile")
    def profile():
        return jsonify(service.profile(request.args.get("room", "")))

    @blueprint.put("/api/manage/memory/profile")
    def edit_profile():
        data = body()
        result = service.edit_profile(data.get("room_id"), data.get("facts"), data.get("revision"))
        log.info("维护台更新记忆：%d 条事实", len(result["facts"]))
        return jsonify(result)

    @blueprint.post("/api/manage/cleanup/preview")
    def preview():
        return jsonify(service.preview_cleanup(body()))

    @blueprint.post("/api/manage/cleanup/apply")
    def apply():
        result = service.apply_cleanup(body().get("token"))
        log.info("维护台清理完成：%d 项", result["count"])
        return jsonify(result)

    @blueprint.get("/api/manage/backups")
    def backups():
        return jsonify(backups=service.backups())

    @blueprint.post("/api/manage/backups")
    def new_backup():
        result = service.create_backup()
        log.info("维护台创建备份：%d 份数据文件", result["files"])
        return jsonify(result), 201

    @blueprint.get("/api/manage/backups/<name>/download")
    def download_backup(name):
        return send_file(service.backup_path(name), as_attachment=True, download_name=name, mimetype="application/zip")

    @blueprint.get("/api/manage/logs")
    def logs():
        return jsonify(service.logs(request.args.get("source", "bot")))

    @blueprint.get("/api/manage/settings")
    def settings():
        return jsonify(service.settings.snapshot())

    @blueprint.put("/api/manage/settings")
    def save_settings():
        data = body()
        with service.lock:
            result = service.settings.save(data.get("revision"), data.get("changes"))
            service.refresh_config()
            log.info("维护台保存高级设置：%s", ", ".join(data["changes"]))
        return jsonify(result)

    @blueprint.post("/api/manage/settings/undo")
    def undo_settings():
        with service.lock:
            result = service.settings.restore(body().get("revision"))
            service.refresh_config()
            log.info("维护台恢复上一次设置")
        return jsonify(result)

    app.register_blueprint(blueprint)


def create_management_app(cfg):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["MAX_CONTENT_LENGTH"] = 128 * 1024
    app.json.ensure_ascii = False
    register_management(app, cfg)

    @app.get("/")
    def root():
        return redirect("/manage")

    return app
