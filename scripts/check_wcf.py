"""WeChatFerry 环境诊断脚本。

用途：在真正启动机器人（``python -m src.main run``）之前，一条命令把
「微信进程 / 微信版本 / 是否 64 位 / 管理员权限 / wcferry 依赖 / 能否连上微信 /
当前登录账号」全部检查一遍，并用中文给出结论和修复建议。

用法（Windows，建议 **以管理员身份** 打开 PowerShell）::

    # 1) 静态检查：不注入微信，随时可跑
    .\\.venv\\Scripts\\python.exe scripts\\check_wcf.py

    # 2) 实际连接一次微信（会注入 sdk.dll，需要微信已登录 + 管理员权限）
    .\\.venv\\Scripts\\python.exe scripts\\check_wcf.py --connect

    # 3) 指定端口（默认读 config.yaml 的 wechat.wcf_port，否则 10086）
    .\\.venv\\Scripts\\python.exe scripts\\check_wcf.py --port 10086

退出码：0 = 全部通过；1 = 有项目需要处理；2 = 连不上微信。

说明：``wcferry`` 初始化失败时其内部可能直接 ``os._exit()`` 杀掉进程，
所以 ``--connect`` 会把「真正的连接」放到子进程里做，父进程负责输出结论，
避免诊断脚本自己突然消失、看不到原因。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Windows 下管道/重定向时 Python 默认按 GBK 输出，中文与符号会乱码甚至抛异常
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

OK = "[OK]  "
WARN = "[!!]  "
FAIL = "[FAIL]"

# 已知的 wcferry 版本 -> 配套微信版本
# （真实判断以包内 spy.dll 的 ProductVersion 为准，这里只作为查不到文件时的参考）
KNOWN_WCFERRY_TO_WECHAT = {
    "39.2.0.0": "3.9.10.27",
    "39.3.0.0": "3.9.11.25",
    "39.4.0.0": "3.9.12.17",
    "39.5.0.0": "3.9.12.51",
    "39.5.1.0": "3.9.12.51",
    "39.5.2.0": "3.9.12.51",
    "39.6.0.0": "3.9.12.56",
}


# ---------------------------------------------------------------------------
# Windows 文件/注册表小工具
# ---------------------------------------------------------------------------
def _read_pe_machine(path: str) -> int:
    """读取 PE 文件头里的 Machine 字段：0x8664=x64，0x14C=x86。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x400)
        if len(head) < 0x40:
            return 0
        pe_offset = struct.unpack_from("<I", head, 0x3C)[0]
        if head[pe_offset : pe_offset + 4] != b"PE\0\0":
            return 0
        return struct.unpack_from("<H", head, pe_offset + 4)[0]
    except OSError:
        return 0


def _file_version(path: str) -> str:
    """读取 Windows 可执行文件/ DLL 的 ProductVersion（失败返回空字符串）。

    用 ctypes 调 version.dll，不依赖 PowerShell / wmic。
    """
    if not path or not os.path.exists(path):
        return ""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # pragma: no cover - 非 Windows
        return ""
    try:
        version_dll = ctypes.windll.version
        size = version_dll.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(str(path), 0, size, buffer):
            return ""
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version_dll.VerQueryValueW(buffer, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return ""
        for lang in ("040904b0", "080404b0", "040904e4", "000004b0"):
            sub = f"\\StringFileInfo\\{lang}\\ProductVersion"
            if version_dll.VerQueryValueW(buffer, sub, ctypes.byref(ptr), ctypes.byref(length)):
                if length.value:
                    return ctypes.wstring_at(ptr, length.value).strip().strip("\x00")
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _find_wechat_install() -> tuple[str, str]:
    """从注册表找微信安装路径和版本号，返回 (安装目录, 版本)。"""
    try:
        import winreg
    except Exception:  # pragma: no cover - 非 Windows
        return "", ""

    candidates = (
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\WeChat"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\WeChat"),
    )
    for hive, key in candidates:
        try:
            with winreg.OpenKey(hive, key) as handle:
                install, _ = winreg.QueryValueEx(handle, "InstallPath")
                version, _ = winreg.QueryValueEx(handle, "Version")
                return str(install), str(version)
        except OSError:
            continue
    return "", ""


def _wechat_processes() -> list[str]:
    """列出正在运行的 WeChat.exe 进程行（用 tasklist，避免额外依赖）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WeChat.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    text = (out.stdout or b"").decode("mbcs", errors="ignore")
    return [line.strip() for line in text.splitlines() if "WeChat.exe" in line]


def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - 非 Windows
        return False


def _port_in_use(port: int) -> bool:
    """本机 127.0.0.1:port 是否已被占用（可能已经有一个 wcf.exe 在跑）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _norm_version(text: str) -> tuple[int, ...]:
    """把 "3.9.12.56" / "39.6.0.0" 这类字符串归一成整数元组用于比较。"""
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:4])


def _version_eq(a: str, b: str) -> bool:
    na, nb = _norm_version(a), _norm_version(b)
    if not na or not nb:
        return False
    # 补齐到同长度再比，避免 3.9.12 与 3.9.12.0 误判
    length = max(len(na), len(nb))
    return na + (0,) * (length - len(na)) == nb + (0,) * (length - len(nb))


# ---------------------------------------------------------------------------
# wcferry 包信息
# ---------------------------------------------------------------------------
def _wcferry_info() -> dict:
    info: dict = {
        "installed": False,
        "version": "",
        "package_dir": "",
        "required_wechat": "",
        "wcf_exe": "",
        "sdk_dll": "",
        "spy_dll": "",
        "spy_machine": 0,
    }
    try:
        import importlib.metadata
        import importlib.util

        spec = importlib.util.find_spec("wcferry")
        if spec is None or not spec.submodule_search_locations:
            return info
        info["installed"] = True
        info["package_dir"] = list(spec.submodule_search_locations)[0]
        try:
            info["version"] = importlib.metadata.version("wcferry")
        except importlib.metadata.PackageNotFoundError:
            info["version"] = ""
    except Exception:  # noqa: BLE001
        return info

    pkg = Path(info["package_dir"])
    info["wcf_exe"] = str(pkg / "wcf.exe")
    info["sdk_dll"] = str(pkg / "sdk.dll")
    info["spy_dll"] = str(pkg / "spy.dll")

    spy_version = _file_version(info["spy_dll"])
    if spy_version:
        info["required_wechat"] = spy_version
    elif info["version"]:
        info["required_wechat"] = KNOWN_WCFERRY_TO_WECHAT.get(info["version"], "")
    info["spy_machine"] = _read_pe_machine(info["spy_dll"])
    return info


# ---------------------------------------------------------------------------
# 静态检查
# ---------------------------------------------------------------------------
def _print_header(title: str) -> None:
    print()
    print("=" * 66)
    print(f" {title}")
    print("=" * 66)


def _print_item(state: str, label: str, detail: str = "") -> None:
    print(f"{state} {label:<22}{detail}")


def run_static_checks(port: int) -> dict:
    """跑所有不需要注入微信的检查，返回结论字典。"""
    result: dict = {
        "essential_ok": True,
        "warnings": [],
        "hints": [],
        "wechat_version": "",
        "required_wechat": "",
        "port": port,
    }

    # ---------------- 1. 系统环境 ----------------
    _print_header("1. 系统环境")
    is_windows = platform.system() == "Windows"
    _print_item(OK if is_windows else FAIL, "操作系统", f"{platform.system()} {platform.release()}")
    if not is_windows:
        result["essential_ok"] = False
        result["hints"].append("WeChatFerry 只支持 Windows（64 位）；当前系统无法运行。")
        return result

    py_bits = 64 if sys.maxsize > 2**32 else 32
    py_version = platform.python_version()
    py_ok = sys.version_info >= (3, 9)
    _print_item(OK if py_ok else FAIL, "Python", f"{py_version} ({py_bits} 位)")
    if not py_ok:
        result["essential_ok"] = False
        result["hints"].append("Python 版本过低，请用 Python 3.9+（本项目在 3.13 上验证过）。")

    if py_bits == 32:
        result["warnings"].append(
            "当前 Python 是 32 位。wcferry 的 sdk.dll 是 64 位，建议换 64 位 Python（微信也必须 64 位）。"
        )

    admin = _is_admin()
    _print_item(OK if admin else WARN, "管理员权限", "已提权" if admin else "未提权")
    if not admin:
        result["warnings"].append(
            "当前终端没有管理员权限。注入 DLL（sdk.dll 把 spy.dll 写进微信进程）需要管理员，"
            "请右键 PowerShell / 终端「以管理员身份运行」。"
        )

    _print_item(OK, "端口", f"{port}（消息接收占用 {port + 1}）")
    if _port_in_use(port):
        _print_item(WARN, "端口占用", f"127.0.0.1:{port} 已被占用（可能已有机器人在跑）")
        result["warnings"].append(
            f"端口 {port} 已被占用：如果之前运行过 WCF/机器人，先关掉旧进程；"
            "否则新实例可能连到旧实例上。"
        )

    # ---------------- 2. wcferry 依赖 ----------------
    _print_header("2. wcferry 依赖")
    wcf_info = _wcferry_info()
    result["wcferry"] = wcf_info
    result["required_wechat"] = wcf_info["required_wechat"]

    if not wcf_info["installed"]:
        _print_item(FAIL, "wcferry", "未安装")
        _print_item("", "安装命令", r".\.venv\Scripts\python.exe -m pip install wcferry")
        result["essential_ok"] = False
        result["hints"].append("wcferry 未安装：先执行上面那条 pip 命令，再重新运行本脚本。")
    else:
        _print_item(OK, "wcferry", f"{wcf_info['version'] or '已安装'}  ({wcf_info['package_dir']})")
        # sdk.dll + spy.dll 是必须的；wcf.exe 只有 39.6+ 才有（39.5.x 直接由 sdk.dll 注入）
        for key, label, needed in (
            ("sdk_dll", "sdk.dll", True),
            ("spy_dll", "spy.dll", True),
            ("wcf_exe", "wcf.exe", False),
        ):
            exists = os.path.exists(wcf_info[key])
            if needed:
                _print_item(OK if exists else FAIL, label, "存在" if exists else "缺失（包不完整，请重装 wcferry）")
                if not exists:
                    result["essential_ok"] = False
                    result["hints"].append(f"wcferry 包内缺少 {label}，可能是被杀软隔离，请加入白名单后重装。")
            elif exists:
                _print_item(OK, label, "存在")

        required = wcf_info["required_wechat"]
        if required:
            _print_item(OK, "配套微信版本", required + "（读自 spy.dll 的 ProductVersion）")
        else:
            _print_item(WARN, "配套微信版本", "读取失败，请以 WeChatFerry 官方发布页为准")
            result["warnings"].append(
                "读不到 spy.dll 版本号。请到 https://github.com/lich0821/WeChatFerry/releases "
                "查看当前 wcferry 版本对应的微信版本。"
            )

        arch_name = {0x8664: "x64", 0x14C: "x86"}.get(wcf_info["spy_machine"], "无法识别")
        _print_item(OK if arch_name != "无法识别" else WARN, "注入 DLL 架构", arch_name)
        if arch_name != "无法识别":
            result["warnings"].append(
                f"wcferry 自带的 spy.dll 是 {arch_name} 架构：微信安装包必须同为 {arch_name}，"
                "装错架构会出现「注入成功但收不到消息」或 Python 进程直接消失。"
            )

    # ---------------- 3. 微信客户端 ----------------
    _print_header("3. 微信客户端")
    procs = _wechat_processes()
    running = bool(procs)
    _print_item(OK if running else FAIL, "微信进程", f"运行中（{len(procs)} 个）" if running else "未运行")
    if not running:
        result["essential_ok"] = False
        result["hints"].append("微信没有运行：请先启动微信并登录机器人小号，再运行机器人。")

    install_dir, reg_version = _find_wechat_install()
    if install_dir:
        _print_item(OK, "安装目录", install_dir)
        exe_path = os.path.join(install_dir, "WeChat.exe")
        exe_version = _file_version(exe_path)
        if not exe_version:
            exe_version = reg_version
        result["wechat_version"] = exe_version
        if exe_version:
            _print_item(OK, "微信版本", exe_version)
        else:
            _print_item(WARN, "微信版本", "读取失败（注册表/文件都没有版本号）")
        machine = _read_pe_machine(exe_path) if os.path.exists(exe_path) else 0
        arch_name = {0x8664: "x64", 0x14C: "x86"}.get(machine, "无法识别")
        _print_item(OK if arch_name != "无法识别" else WARN, "微信架构", arch_name)
        spy_machine = wcf_info.get("spy_machine", 0)
        if machine and spy_machine:
            if machine == spy_machine:
                _print_item(OK, "架构匹配", f"微信与 wcferry 的 spy.dll 同为 {arch_name}")
            else:
                spy_arch = {0x8664: "x64", 0x14C: "x86"}.get(spy_machine, str(hex(spy_machine)))
                _print_item(FAIL, "架构不匹配", f"微信是 {arch_name}，wcferry 的 spy.dll 是 {spy_arch}")
                result["essential_ok"] = False
                result["hints"].append(
                    f"微信位数不对：当前微信是 {arch_name}，但 wcferry 自带的注入 DLL 是 {spy_arch}。"
                    f"请安装 {spy_arch} 版本的微信（见 docs/WECHAT_SETUP.md 的下载说明）。"
                )
    else:
        _print_item(WARN, "安装目录", "注册表里没找到（绿色版/注册表被清理？）")
        result["hints"].append(
            "没读到微信安装目录。如果微信确实装了并能登录，可忽略这一项，继续看版本核对。"
        )

    required = wcf_info.get("required_wechat", "")
    current = result["wechat_version"]
    if required and current:
        if _version_eq(required, current):
            _print_item(OK, "版本匹配", f"微信 {current} = wcferry 需要的 {required}")
        else:
            _print_item(FAIL, "版本不匹配", f"微信 {current} ≠ wcferry 需要的 {required}")
            result["essential_ok"] = False
            result["hints"].append(
                f"微信版本不对：请安装 {required}（这是 wcferry {wcf_info['version']} 对应的版本）。"
                "务必关闭微信自动更新，否则会升级到不兼容的版本。"
            )

    # ---------------- 4. 配置文件（可选） ----------------
    _print_header("4. 项目配置（可选）")
    config_path = PROJECT_ROOT / "config.yaml"
    if config_path.exists():
        try:
            import yaml

            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            wechat_cfg = data.get("wechat") or {}
            _print_item(OK, "config.yaml", str(config_path))
            _print_item("", "adapter", str(wechat_cfg.get("adapter", "（未配置）")))
            _print_item("", "wcf_port", str(wechat_cfg.get("wcf_port", "（未配置）")))
            _print_item("", "self_wxid", str(wechat_cfg.get("self_wxid") or "（空，自动获取）"))
            if wechat_cfg.get("adapter") and wechat_cfg.get("adapter") != "wcf":
                result["warnings"].append("config.yaml 里 wechat.adapter 不是 wcf，run 不会连真实微信。")
        except Exception as exc:  # noqa: BLE001
            _print_item(WARN, "config.yaml", f"解析失败：{exc}")
    else:
        _print_item(WARN, "config.yaml", "不存在（复制 config.example.yaml 为 config.yaml 并填 Key）")
        result["hints"].append("还没有 config.yaml：先复制 config.example.yaml，填入 DeepSeek API Key。")

    return result


# ---------------------------------------------------------------------------
# 实际连接（子进程）
# ---------------------------------------------------------------------------
def run_live_child(port: int) -> int:
    """子进程模式：真正初始化 Wcf 并读取登录账号；结果以 LIVE_JSON 行输出。"""
    def emit(payload: dict) -> None:
        print("LIVE_JSON " + json.dumps(payload, ensure_ascii=False))
        sys.stdout.flush()

    try:
        from wcferry import Wcf
    except Exception as exc:  # noqa: BLE001
        emit({"ok": False, "error": f"import wcferry 失败：{exc}"})
        return 2

    wcf = None
    try:
        # block=False：先建立连接，再自己等登录，避免卡死在 while not is_login()
        wcf = Wcf(port=port, debug=False, block=False)
        deadline = time.time() + 20
        logged_in = False
        while time.time() < deadline:
            if wcf.is_login():
                logged_in = True
                break
            time.sleep(1)

        payload: dict = {"ok": logged_in, "login": logged_in, "self_wxid": "", "user_info": {}}
        if logged_in:
            payload["self_wxid"] = wcf.get_self_wxid()
            try:
                user_info = wcf.get_user_info() or {}
                # 手机号脱敏，避免诊断输出里带完整隐私
                mobile = str(user_info.get("mobile") or "")
                if len(mobile) >= 7:
                    user_info["mobile"] = mobile[:3] + "****" + mobile[-2:]
                payload["user_info"] = user_info
            except Exception as exc:  # noqa: BLE001
                payload["user_info_error"] = str(exc)
        else:
            payload["error"] = "20 秒内没有检测到已登录的微信账号"
        emit(payload)
        return 0 if logged_in else 1
    except BaseException as exc:  # noqa: BLE001  包含 os._exit 走不到的情况
        emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 3
    finally:
        if wcf is not None:
            try:
                wcf.cleanup()
            except BaseException:  # noqa: BLE001
                pass


def run_live_parent(port: int) -> dict:
    """父进程模式：起子进程做真实连接，解析结果（子进程被 os._exit 也不会拖死父进程）。"""
    cmd = [sys.executable, os.path.abspath(__file__), "--connect-child", "--port", str(port)]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "连接微信超时（120 秒），请检查微信是否登录/是否卡在 UAC"}
    except OSError as exc:
        return {"ok": False, "error": f"无法启动子进程：{exc}"}

    stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    for line in stdout.splitlines():
        if line.startswith("LIVE_JSON "):
            try:
                payload = json.loads(line[len("LIVE_JSON ") :])
                payload["_returncode"] = proc.returncode
                payload["_stderr_tail"] = stderr.strip().splitlines()[-5:] if stderr.strip() else []
                return payload
            except json.JSONDecodeError:
                continue
    return {
        "ok": False,
        "error": f"连接子进程没有返回结果（退出码 {proc.returncode}）",
        "_returncode": proc.returncode,
        "_stderr_tail": stderr.strip().splitlines()[-10:] if stderr.strip() else [],
        "_stdout_tail": stdout.strip().splitlines()[-10:] if stdout.strip() else [],
    }


def _print_live_result(payload: dict) -> bool:
    _print_header("5. 实际连接微信（--connect）")
    if payload.get("ok"):
        _print_item(OK, "RPC 连接", f"已连上 127.0.0.1 端口")
        _print_item(OK, "登录状态", "已登录")
        _print_item(OK, "机器人 wxid", payload.get("self_wxid") or "（未知）")
        info = payload.get("user_info") or {}
        if info:
            _print_item("", "昵称", str(info.get("name") or info.get("nickName") or "（未知）"))
            _print_item("", "手机号", str(info.get("mobile") or ""))
        if payload.get("user_info_error"):
            _print_item(WARN, "账号信息", f"读取失败：{payload['user_info_error']}")
        return True

    _print_item(FAIL, "RPC 连接", str(payload.get("error") or "失败"))
    rc = payload.get("_returncode")
    if rc is not None:
        _print_item("", "子进程退出码", str(rc))
    for line in payload.get("_stderr_tail") or []:
        _print_item("", "stderr", line)
    _print_item("", "常见原因", "微信未启动 / 微信版本不匹配 / 未用管理员运行 / 杀软拦截 sdk.dll、spy.dll")
    return False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _read_config_port() -> int | None:
    config_path = PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        return None
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        port = (data.get("wechat") or {}).get("wcf_port")
        return int(port) if port else None
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="WeChatFerry 环境诊断：微信进程/版本/wcferry 连接/登录账号",
    )
    parser.add_argument("--port", type=int, default=None, help="WCF RPC 端口（默认读 config.yaml 或 10086）")
    parser.add_argument("--connect", action="store_true", help="实际连接一次微信（会注入 sdk.dll，需要管理员）")
    parser.add_argument("--connect-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    port = args.port or _read_config_port() or 10086

    if args.connect_child:
        return run_live_child(port)

    print()
    print("WeChatFerry 环境诊断")
    print(f"项目目录：{PROJECT_ROOT}")
    print(f"Python  ：{sys.executable}")

    result = run_static_checks(port)

    live_ok = True
    if args.connect:
        payload = run_live_parent(port)
        live_ok = _print_live_result(payload)
    else:
        _print_header("5. 实际连接微信（未检查）")
        print(f"{WARN} 加 --connect 参数可以实际连接一次微信，确认 RPC 和登录账号。")

    # ---------------- 结论 ----------------
    _print_header("结论")
    if result["essential_ok"] and live_ok:
        print(f"{OK} 环境检查通过，可以启动机器人：")
        print(r"       .\.venv\Scripts\python.exe -m src.main run")
        if not args.connect:
            print("      （建议先加 --connect 跑一次，确认能连上微信并拿到 wxid）")
    else:
        print(f"{FAIL} 还有问题需要处理：")
        if result["hints"]:
            for i, hint in enumerate(result["hints"], 1):
                print(f"  {i}. {hint}")
        if not live_ok:
            print("  * 实际连接失败：先确认微信已登录、版本匹配、终端是管理员。")

    if result["warnings"]:
        print()
        print("提醒（不阻塞启动，但建议处理）：")
        for warning in result["warnings"]:
            print(f"  - {warning}")

    print()
    print("版本核对方法：官方发布页 https://github.com/lich0821/WeChatFerry/releases ")
    print("每个 release 正文都会写「配套微信版本」，并附带 WeChatSetup-x.y.z.exe。")
    print("历史微信安装包归档：https://github.com/tom-snow/wechat-windows-versions/releases")

    if args.connect and not live_ok:
        return 2
    return 0 if result["essential_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
