"""部署体检：云服务器上装好后先跑这个，逐项确认环境是否可用。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\doctor.py
    .\\.venv\\Scripts\\python.exe scripts\\doctor.py --network   # 额外测试 API / 搜索连通性
"""
from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

OK = "✅"
BAD = "❌"
WARN = "⚠️ "


def check(name: str, ok: bool, detail: str = "", critical: bool = True) -> bool:
    print(f"  {OK if ok else (BAD if critical else WARN)} {name:<22} {detail}")
    return ok or not critical


def weixin_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="ignore",
        ).stdout
        return "Weixin.exe" in out
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--network", action="store_true", help="测试 DeepSeek 模型与搜索接口")
    args = parser.parse_args()

    problems = 0
    print("=" * 62)
    print(" 微信群聊 DeepSeek 机器人 · 部署体检")
    print("=" * 62)

    # ---------------- 系统 ----------------
    print("\n[1] 系统环境")
    problems += not check("操作系统", platform.system() == "Windows", f"{platform.system()} {platform.release()}")
    bits = 64 if sys.maxsize > 2**32 else 32
    problems += not check("Python", sys.version_info >= (3, 10), f"{platform.python_version()} ({bits} 位)")
    problems += not check("项目目录", (ROOT / "src").is_dir(), str(ROOT))

    # ---------------- 依赖 ----------------
    print("\n[2] Python 依赖")
    modules = [
        ("openai", "openai"),
        ("yaml", "PyYAML"),
        ("flask", "flask"),
        ("wechatauto", "wechatauto-replica"),
        ("faster_whisper", "faster-whisper"),
        ("pilk", "pilk"),
        ("docx", "python-docx"),
        ("openpyxl", "openpyxl"),
        ("pypdf", "pypdf"),
        ("requests", "requests"),
        ("numpy", "numpy"),
    ]
    for mod, label in modules:
        try:
            __import__(mod)
            check(label, True, "已安装")
        except Exception as exc:  # noqa: BLE001
            problems += not check(label, False, f"缺失：{exc}")

    # ---------------- 配置 ----------------
    print("\n[3] 配置")
    cfg = None
    try:
        from src.config import load_config

        cfg = load_config()
        check("config.yaml", True, cfg.source_path)
    except Exception as exc:  # noqa: BLE001
        problems += not check("config.yaml", False, f"读取失败：{exc}")
        print("\n结论：配置有问题，先修好再部署。")
        return 1

    key = cfg.deepseek.resolve_api_key()
    problems += not check("API Key", bool(key), "已配置" if key else "未配置（config.yaml 或 DEEPSEEK_API_KEY）")
    check("模型", True, f"{cfg.deepseek.base_url} / {cfg.deepseek.model}")
    check("接入方式", True, f"{cfg.wechat.adapter} / 群「{cfg.wechat.group_name}」")
    check("多媒体", True, f"识图={cfg.vision.enabled} 语音={cfg.asr.enabled} 文件={cfg.files.enabled} 联网={cfg.search.enabled}")

    # ---------------- 微信 ----------------
    print("\n[4] 微信客户端")
    running = weixin_running()
    problems += not check("微信进程", running, "Weixin.exe 在运行" if running else "未运行，请先启动微信并登录小号")
    if running:
        try:
            from wechatauto import WeChatDB

            db = WeChatDB()
            info = db.get_self_info() or {}
            nick = info.get("nick_name") or "?"
            check("登录账号", True, f"{nick} / {info.get('username')}")
            if cfg.wechat.self_name:
                ok = nick == cfg.wechat.self_name
                problems += not check("账号匹配", ok, f"配置要求「{cfg.wechat.self_name}」，当前「{nick}」")
            try:
                groups = db.get_groups() or []
                names = [str(g.get("name", "")) for g in groups]
                found = cfg.wechat.group_name in names
                problems += not check("目标群", found, f"「{cfg.wechat.group_name}」" + ("" if found else " 没找到"))
            except Exception as exc:  # noqa: BLE001
                check("目标群", False, f"读取失败：{exc}", critical=False)
        except Exception as exc:  # noqa: BLE001
            problems += not check("微信数据库", False, f"连接失败：{exc}")

    # ---------------- 网络 ----------------
    if args.network:
        print("\n[5] 网络与接口")
        try:
            from src.llm import DeepSeekClient

            client = DeepSeekClient(cfg.deepseek)
            reply = client.chat([{"role": "user", "content": "回复两个字：在线"}])
            client.close()
            check("DeepSeek 模型", True, f"返回：{reply[:20]}")
        except Exception as exc:  # noqa: BLE001
            problems += not check("DeepSeek 模型", False, f"{exc}")
        try:
            from src.websearch import SearchClient

            sc = SearchClient(cfg.deepseek, cfg.search)
            results = sc.search("DeepSeek", count=2)
            check("联网搜索", bool(results), f"{len(results)} 条结果")
        except Exception as exc:  # noqa: BLE001
            check("联网搜索", False, f"{exc}", critical=False)

    print("\n" + "=" * 62)
    if problems:
        print(f"{BAD} 有 {problems} 项需要处理，修好后重新运行本脚本。")
        return 1
    print(f"{OK} 环境体检通过，可以启动机器人：start_bot.bat 或 python -m src.main run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
