"""命令行入口。

    python -m src.main check          检查配置 / Key / 依赖
    python -m src.main mock           控制台模拟群聊（不需要微信）
    python -m src.main web            启动本地网页测试台（不需要微信）
    python -m src.main run            正式运行：连接微信（默认 wechatauto）
"""
from __future__ import annotations

import argparse
import logging
import sys
import json
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .bot import Bot
from .config import AppConfig, load_config
from .context import ContextStore
from .engine import ReplyEngine
from .llm import DeepSeekClient, LLMError, UnconfiguredLLM

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("dsbot")


def setup_logging(cfg: AppConfig, quiet_console: bool = False, filename: str = "bot.log") -> None:
    """配置日志。

    注意：不要用 root logger。wechatauto（以及其他第三方库）在 import 时会重配
    root logging，把挂在 root 上的 FileHandler 删掉，导致 bot.log 里没有运行日志。
    这里把 handler 挂在项目自己的 "dsbot" logger 上并关闭向上传播，不受第三方影响。
    """
    level = getattr(logging, cfg.logging.level.upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%m-%d %H:%M:%S")

    bot_logger = logging.getLogger("dsbot")
    bot_logger.setLevel(level)
    bot_logger.propagate = False
    for h in list(bot_logger.handlers):
        bot_logger.removeHandler(h)

    if not quiet_console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        bot_logger.addHandler(sh)
    try:
        Path(cfg.logging.dir).mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            Path(cfg.logging.dir) / filename,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        bot_logger.addHandler(fh)
    except OSError as exc:  # pragma: no cover
        print(f"[warn] 无法写日志文件：{exc}")
    # 第三方库统一降噪（它们自己的 handler 我们控制不了，但级别可以调）
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "wechatauto"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_engine(cfg: AppConfig, llm, search=None, memory=None, imagegen=None) -> ReplyEngine:
    if isinstance(llm, DeepSeekClient):
        from .runtime import UsageMeter
        llm.meter = UsageMeter(Path(cfg.logging.dir) / "usage.json")
    store = ContextStore(cfg.context.persist_dir)
    return ReplyEngine(
        cfg, llm, store, self_wxid=cfg.wechat.self_wxid, search=search, memory=memory, imagegen=imagegen
    )


def _require_llm(cfg: AppConfig) -> DeepSeekClient:
    try:
        return DeepSeekClient(cfg.deepseek)
    except LLMError as exc:
        print(f"\n[配置错误] {exc}\n")
        raise SystemExit(2)


# ---------------------------------------------------------------------- 子命令
def cmd_check(cfg: AppConfig) -> int:
    print("配置检查")
    print("-" * 50)
    print(f"配置文件      : {cfg.source_path}")
    print(f"API Base URL  : {cfg.deepseek.base_url}")
    print(f"模型          : {cfg.deepseek.model}")
    key = cfg.deepseek.resolve_api_key()
    print(f"API Key       : {'已配置 ' + key[:6] + '***' + key[-4:] if key else '❌ 未配置（请填 config.yaml 的 deepseek.api_key）'}")
    print(f"触发规则      : {cfg.trigger_label}")
    print(f"群白名单      : {cfg.wechat.allowed_groups or '（空 = 所有群）'}")
    print(f"上下文目录    : {cfg.context.persist_dir}")
    print(f"识图/表情包   : {'✅ 开启（detail=' + cfg.vision.detail + '）' if cfg.vision.enabled else '❌ 关闭'}")
    print(f"语音转文字    : {'✅ 开启（模型 ' + cfg.asr.model + '，本地）' if cfg.asr.enabled else '❌ 关闭'}")
    print(f"文件解析      : {'✅ 开启' if cfg.files.enabled else '❌ 关闭'}")
    print(f"联网搜索      : {'✅ 开启（DeepSeek Search，模型自主决定）' if cfg.search.enabled else '❌ 关闭'}")
    if cfg.imagegen.enabled:
        print(f"生图          : ✅ 开启（{cfg.imagegen.provider} / {cfg.imagegen.model}，模型自主决定）")
    else:
        print("生图          : ❌ 关闭")
    print(f"长期记忆      : {'✅ 开启（每 ' + str(cfg.memory.every_n_messages) + ' 条消息更新档案）' if cfg.memory.enabled else '❌ 关闭'}")
    print(f"表情库        : {'✅ 开启（复用对方常发表情）' if cfg.stickers.enabled else '❌ 关闭'}")
    if cfg.proactive.enabled:
        print(
            f"主动开场      : ✅ 开启（随机 {cfg.proactive.min_gap_minutes:.0f}~{cfg.proactive.max_gap_minutes:.0f} 分钟，"
            f"{cfg.proactive.active_from}~{cfg.proactive.active_to}）"
        )
    else:
        print("主动开场      : ❌ 关闭")
    print(f"微信适配器    : {cfg.wechat.adapter}")
    if cfg.wechat.adapter in ("pyweixin", "wechatauto"):
        groups = cfg.group_list()
        privates = cfg.private_chat_list()
        print(f"监听目标      : {len(groups)} 个群 + {len(privates)} 个私聊")
        for g in groups:
            persona = cfg.persona_for(g)
            rule = g.trigger or cfg.chat.trigger
            print(f"   - [群] {g.name}  [人设:{g.persona or '默认'} / 触发:{rule} / 称呼:{persona.bot_name}]")
        for p in privates:
            persona = cfg.persona_for(p)
            rule = p.trigger or "all"
            print(f"   - [私聊] {p.name}  [人设:{p.persona or '默认'} / 触发:{rule} / 称呼:{persona.bot_name}]")
        print(f"轮询间隔      : {cfg.wechat.poll_seconds}s")
    elif cfg.wechat.adapter == "wcf":
        print(f"WCF 端口      : {cfg.wechat.wcf_port}")

    sys.stdout.flush()  # 下面要 import wechatauto，它会替换 stdout，先把手头输出冲出去

    ok = bool(key)
    try:
        import openai  # noqa: F401
        print("依赖 openai   : ✅")
    except ImportError:
        print("依赖 openai   : ❌ 请 pip install -r requirements.txt")
        ok = False
    try:
        import yaml  # noqa: F401
        print("依赖 PyYAML   : ✅")
    except ImportError:
        print("依赖 PyYAML   : ❌")
        ok = False
    try:
        import flask  # noqa: F401
        print("依赖 flask    : ✅")
    except ImportError:
        print("依赖 flask    : ⚠️  网页测试台不可用")
    if cfg.wechat.adapter == "wcf":
        try:
            import wcferry  # noqa: F401
            print("依赖 wcferry  : ✅")
        except Exception as exc:  # noqa: BLE001
            print(f"依赖 wcferry  : ❌ {exc}")
            ok = False
    elif cfg.wechat.adapter == "wechatauto":
        try:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import wechatauto  # noqa: F401
            print("依赖 wechatauto: ✅")
        except Exception as exc:  # noqa: BLE001
            print(f"依赖 wechatauto: ❌ {exc}（pip install wechatauto-replica）")
            ok = False
    elif cfg.wechat.adapter == "pyweixin":
        try:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import pyweixin  # noqa: F401
            print("依赖 pyweixin : ✅")
        except Exception as exc:  # noqa: BLE001
            print(f"依赖 pyweixin : ❌ {exc}（pip install pywechat127）")
            ok = False
    print("-" * 50)
    print("结论：" + ("✅ 可以启动" if ok else "❌ 请先解决上面的问题"))
    return 0 if ok else 1


def cmd_mock(cfg: AppConfig) -> int:
    from .mock_adapter import ConsoleAdapter

    llm = _require_llm(cfg)
    engine = build_engine(cfg, llm)
    adapter = ConsoleAdapter()
    engine.set_self_wxid(adapter.self_wxid)
    adapter.on_reset = lambda room_id: engine.reset_room(room_id)  # type: ignore[attr-defined]
    bot = Bot(cfg, adapter, engine)
    try:
        bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        llm.close()
    return 0


def cmd_web(cfg: AppConfig, host: str, port: int) -> int:
    from web.app import create_app

    # 网页测试台允许"还没填 Key"先打开看界面，发消息时再提示错误
    try:
        llm = DeepSeekClient(cfg.deepseek)
    except LLMError as exc:
        print(f"\n[提示] {exc}")
        print("[提示] 网页仍会打开，可以先看界面；填好 Key 后重启即可正常对话。\n")
        llm = UnconfiguredLLM(str(exc))
    engine = build_engine(cfg, llm)
    app = create_app(cfg, engine)
    print(f"\n  本地网页测试台： http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False)
    return 0


def cmd_manage(cfg: AppConfig, port: int, open_browser: bool = False) -> int:
    from web.manage import create_management_app
    from werkzeug.serving import make_server
    app = create_management_app(cfg)
    try:
        server = make_server("127.0.0.1", port, app, threaded=True)
    except (OSError, SystemExit):
        print(f"维护台未能启动：端口 {port} 可能被占用，请换一个 --port。")
        return 2
    app.extensions["maintenance_shutdown"] = server.shutdown
    print(f"本地维护台：http://127.0.0.1:{port}/manage")
    if open_browser:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}/manage")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _wechat_39_running() -> bool:
    """检查有没有 WeChat.exe（3.9 客户端）在跑。4.x 的进程名是 Weixin.exe，不算。"""
    try:
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WeChat.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="ignore",
        ).stdout
        return "WeChat.exe" in out
    except Exception:  # noqa: BLE001
        return True  # 检测不了就不拦，交给 WCF 自己报错


def _weixin_4_running() -> bool:
    """检查微信 4.x（Weixin.exe）是否在运行。"""
    try:
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="ignore",
        ).stdout
        return "Weixin.exe" in out
    except Exception:  # noqa: BLE001
        return True


def _acquire_single_instance_lock() -> bool:
    """同一时间只允许一个机器人实例（防止重复回复/抢微信窗口）。"""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\DeepSeekWeChatBot")
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        # 故意把句柄保存在模块级，进程结束自动释放
        globals()["_SINGLE_INSTANCE_HANDLE"] = handle
        return True
    except Exception:  # noqa: BLE001
        return True  # 非 Windows/异常时不阻止启动


def _preflight_run(cfg: AppConfig, force: bool) -> bool:
    """启动真实微信前的体检：不做的话 wcferry 会 os._exit，用户只看到「一闪而过」。"""
    problems: list[str] = []
    if not _is_admin():
        problems.append(
            "当前不是管理员权限。\n"
            "   → 请用「以管理员身份运行」打开 PowerShell，或直接运行：\n"
            "     powershell -ExecutionPolicy Bypass -File .\\scripts\\run_bot.ps1"
        )
    if not _wechat_39_running():
        problems.append(
            "没有检测到微信 3.9 客户端（WeChat.exe）在运行。\n"
            "   → 先启动并登录小号的那个 3.9 微信，再启动机器人。\n"
            "   → 版本/安装步骤见 docs/WECHAT_SETUP.md（必须是 3.9.12.51 64 位）"
        )
    if not problems:
        return True
    print("\n启动前检查没通过：\n")
    for i, p in enumerate(problems, 1):
        print(f"{i}. {p}\n")
    if force:
        print("（--force 已指定，继续尝试启动……）\n")
        return True
    print(r"排查完再运行；环境诊断：.\.venv\Scripts\python.exe scripts\check_wcf.py")
    print("如果确认环境没问题仍想硬启动，加参数：python -m src.main run --force\n")
    return False


def _preflight_pyweixin(cfg: AppConfig, force: bool) -> bool:
    problems: list[str] = []
    if not _weixin_4_running():
        problems.append(
            "没有检测到微信 4.x（Weixin.exe）在运行。\n"
            "   → 先启动微信、登录机器人小号，再运行本程序。"
        )
    if not cfg.wechat.group_name.strip():
        problems.append(
            "config.yaml 里没填 wechat.group_name（要挂机器人的群名）。\n"
            "   → 填成你们三人群的群名，比如「我 + 朋友 + DeepSeek」"
        )
    if not problems:
        return True
    print("\n启动前检查没通过：\n")
    for i, p in enumerate(problems, 1):
        print(f"{i}. {p}\n")
    if force:
        print("（--force 已指定，继续尝试启动……）\n")
        return True
    print("说明：pyweixin 方案不需要管理员权限，但要求微信窗口保持打开、电脑不锁屏。\n")
    return False


def cmd_run_pyweixin(cfg: AppConfig, force: bool = False) -> int:
    from .pyweixin_adapter import PyweixinAdapter

    if not _preflight_pyweixin(cfg, force):
        return 2
    llm = _require_llm(cfg)
    engine = build_engine(cfg, llm)
    adapter = PyweixinAdapter(
        group_name=cfg.wechat.group_name,
        group_members=cfg.wechat.group_members,
        poll_seconds=cfg.wechat.poll_seconds,
        window_minimize=cfg.wechat.window_minimize,
        self_name=cfg.wechat.self_name,
    )
    bot = Bot(cfg, adapter, engine)
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出…")
    finally:
        llm.close()
    return 0


def cmd_run_wechatauto(cfg: AppConfig, force: bool = False) -> int:
    if not _preflight_wechatauto(cfg, force):
        return 2
    from .wechatauto_adapter import WechatautoAdapter

    llm = _require_llm(cfg)
    search = None
    if cfg.search.enabled:
        try:
            from .websearch import SearchClient

            search = SearchClient(cfg.deepseek, cfg.search)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 联网搜索初始化失败，将不联网：{exc}")
    engine = build_engine(cfg, llm, search=search)
    imagegen = None
    if cfg.imagegen.enabled:
        try:
            from .imagegen import ImageGenerator

            imagegen = ImageGenerator(cfg.imagegen, llm)
            engine.imagegen = imagegen
            if cfg.imagegen.provider in ("gemini", "openai") and not cfg.imagegen.resolve_api_key():
                msg = (
                    "[提示] 生图已启用，但没有配置 Key：请填 config.yaml 的 imagegen.api_key"
                    "（Gemini 去 https://aistudio.google.com/apikey 领）后重启"
                )
                print(msg)
                log.warning(msg)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 生图功能初始化失败：{exc}")
    memory = None
    if cfg.memory.enabled:
        try:
            from .memory import MemoryManager

            memory = MemoryManager(cfg.memory, llm)
            engine.memory = memory
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 长期记忆初始化失败，将不记录档案：{exc}")
    vision = None
    if cfg.vision.enabled:
        try:
            from .vision import VisionClient

            vision = VisionClient(cfg.deepseek, cfg.vision)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 识图功能初始化失败，将只发文字：{exc}")
    asr = None
    if cfg.asr.enabled:
        try:
            from .asr import SpeechToText

            asr = SpeechToText(cfg.asr)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 语音转文字初始化失败：{exc}")
    file_summarizer = None
    if cfg.files.enabled:
        try:
            from .files import FileSummarizer

            file_summarizer = FileSummarizer(cfg.files)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 文件解析初始化失败：{exc}")
    sticker_store = None
    if cfg.stickers.enabled:
        try:
            from .stickers import StickerStore

            sticker_store = StickerStore(cfg.stickers.dir)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 表情库初始化失败：{exc}")
    adapter = WechatautoAdapter(
        group_names=[g.name for g in cfg.group_list()],
        private_names=[p.name for p in cfg.private_chat_list()],
        poll_seconds=cfg.wechat.poll_seconds,
        reply_with_at=cfg.chat.reply_with_at,
        expected_self_name=cfg.wechat.self_name,
        vision=vision,
        asr=asr,
        file_summarizer=file_summarizer,
        media_dir=cfg.vision.media_dir,
        files_dir=cfg.files.dir,
        sticker_store=sticker_store,
        queue_size=cfg.wechat.queue_size,
        media_workers=cfg.wechat.media_workers,
        task_ttl_seconds=cfg.wechat.task_ttl_seconds,
        state_dir=cfg.wechat.state_dir,
    )
    bot = Bot(cfg, adapter, engine)
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出…")
    finally:
        llm.close()
    return 0


def _preflight_wechatauto(cfg: AppConfig, force: bool) -> bool:
    problems: list[str] = []
    if not _weixin_4_running():
        problems.append(
            "没有检测到微信 4.x（Weixin.exe）在运行。\n"
            "   → 先启动微信并登录机器人小号，再运行本程序。"
        )
    if not cfg.group_list() and not cfg.private_chat_list():
        problems.append("config.yaml 里没有配置任何群或私聊（wechat.groups / wechat.private_chats / group_name）。")
    try:
        import wechatauto  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        problems.append(f"缺少依赖 wechatauto-replica：{exc}\n   → pip install wechatauto-replica")
    if not problems:
        return True
    print("\n启动前检查没通过：\n")
    for i, p in enumerate(problems, 1):
        print(f"{i}. {p}\n")
    if force:
        print("（--force 已指定，继续尝试启动……）\n")
        return True
    return False


def cmd_run(cfg: AppConfig, force: bool = False) -> int:
    if not _acquire_single_instance_lock():
        print(
            "\n机器人已经在运行了（同一时间只允许一个实例，避免重复回复）。\n"
            "如果你确定没在运行，请在任务管理器里结束 python.exe 后再启动。\n"
        )
        return 3
    if cfg.wechat.adapter == "wechatauto":
        return cmd_run_wechatauto(cfg, force)
    if cfg.wechat.adapter == "pyweixin":
        return cmd_run_pyweixin(cfg, force)

    from .wcf_adapter import WcfAdapter

    if not _preflight_run(cfg, force):
        return 2
    llm = _require_llm(cfg)
    engine = build_engine(cfg, llm)
    adapter = WcfAdapter(port=cfg.wechat.wcf_port, debug=cfg.wechat.wcf_debug)
    bot = Bot(cfg, adapter, engine)
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出…")
    finally:
        llm.close()
    return 0


def _force_utf8_stdio() -> None:
    """Windows 控制台/管道默认 GBK，直接 print emoji 会 UnicodeEncodeError。

    注意：wechatauto 等第三方库在 import 时会替换 sys.stdout，
    之前缓冲在旧对象里的输出会丢失（表现为 check 只剩最后几行）。
    所以这里开 line_buffering，每次 print 立即落盘，避免丢行。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="dsbot",
        description="微信群聊 DeepSeek 机器人：群里每个人发消息，DeepSeek 自动接话",
    )
    parser.add_argument("-c", "--config", help="配置文件路径（默认 config.yaml）")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("check", help="检查配置与依赖")
    sub.add_parser("mock", help="控制台模拟群聊（不用微信）")
    sub.add_parser("status", help="查看最近运行指标及今日 Token 用量")
    p_cleanup = sub.add_parser("cleanup", help="预览数据清理；--apply 才执行")
    p_cleanup.add_argument("--apply", action="store_true")
    p_backup = sub.add_parser("backup", help="备份聊天、记忆和运行状态，不包含 Key")
    p_backup.add_argument("--output", required=True)
    p_memory = sub.add_parser("memory", help="查看、修订或清空长期记忆")
    p_memory.add_argument("action", choices=("list", "show", "set", "clear"))
    p_memory.add_argument("--room", help="list 显示的 room_id")
    p_memory.add_argument("--fact", action="append", default=[], help="set 时使用，可重复；完整替换该会话档案")
    p_web = sub.add_parser("web", help="启动本地网页测试台（不用微信）")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8000)
    p_manage = sub.add_parser("manage", help="启动本地可视化维护台，无需模型 Key")
    p_manage.add_argument("--port", type=int, default=8010)
    p_manage.add_argument("--open", action="store_true", help="启动后打开浏览器")
    p_run = sub.add_parser("run", help="正式运行：连接微信")
    p_run.add_argument("--force", action="store_true", help="跳过管理员/微信进程预检，强行启动")

    args = parser.parse_args(argv)
    cmd = args.cmd or "check"

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[配置错误] {exc}\n提示：复制 config.example.yaml 为 config.yaml 再填写。")
        return 2

    if cmd in ("cleanup", "backup", "memory", "status"):
        try:
            if cmd == "cleanup":
                from .maintenance import cleanup
                result = cleanup(cfg, apply=args.apply)
            elif cmd == "backup":
                from .maintenance import backup
                result = backup(cfg, args.output)
            elif cmd == "memory":
                from .memory import ProfileStore
                store = ProfileStore(cfg.memory.dir)
                if args.action == "list":
                    result = [{"room_id": r.get("room_id"), "room_name": r.get("room_name"),
                               "facts": len(r.get("facts", []))} for r in store.list_profiles()]
                else:
                    if not args.room:
                        raise ValueError("该操作需要 --room 指定会话 ID")
                    if args.action == "show":
                        result = store.load(args.room)
                    else:
                        if args.action == "set" and not args.fact:
                            raise ValueError("set 至少需要一个 --fact；清空请用 clear")
                        result = store.edit(args.room, args.fact if args.action == "set" else [])
            else:
                from .runtime import UsageMeter
                stats = Path(cfg.logging.dir) / "stats.json"
                result = {"usage_today": UsageMeter(Path(cfg.logging.dir) / "usage.json").snapshot(),
                          "delivery_today": UsageMeter(Path(cfg.logging.dir) / "delivery-metrics.json").snapshot(),
                          "last_snapshot": json.loads(stats.read_text(encoding="utf-8")) if stats.exists() else {}}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except (ValueError, OSError) as exc:
            print(f"[操作失败] {exc}")
            return 2
    if cmd == "check":
        setup_logging(cfg, quiet_console=True)
        from . import net

        net.configure(cfg.network.use_system_proxy)
        return cmd_check(cfg)
    setup_logging(cfg, filename="manage.log" if cmd == "manage" else "bot.log")
    # 网络出口：默认直连，避免系统代理（Clash 等）没启动时调用 API 直接 Connection error
    from . import net

    net.configure(cfg.network.use_system_proxy)
    if cmd == "mock":
        return cmd_mock(cfg)
    if cmd == "web":
        return cmd_web(cfg, args.host, args.port)
    if cmd == "manage":
        return cmd_manage(cfg, args.port, args.open)
    if cmd == "run":
        return cmd_run(cfg, force=getattr(args, "force", False))
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
