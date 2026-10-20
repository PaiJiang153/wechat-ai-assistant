"""微信 UI 树探测脚本：判断当前微信版本能不能被 UI 自动化（pyweixin）识别。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\probe_uia.py
    .\\.venv\\Scripts\\python.exe scripts\\probe_uia.py --with-narrator   # 临时挂讲述人再测

输出会告诉你：
  * 微信版本、主窗口的 UIA 类名
  * ControlView / RawView 下有多少个控件（>20 说明 UI 树正常暴露，pyweixin 大概率可用）
  * 是否存在 pyweixin 期望的 mmui::MainWindow
"""
from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import time
from collections import Counter

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def weixin_version() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin") as key:
            raw = winreg.QueryValueEx(key, "Version")[0]
        # 0xf254186b 之类编码，这里只做粗略展示
        return f"registry={raw} (0x{int(raw) & 0xFFFFFFFF:X})"
    except Exception as exc:  # noqa: BLE001
        return f"读取失败：{exc}"


def weixin_pids() -> list[int]:
    import psutil

    pids = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info["name"] or "").lower() == "weixin.exe":
                pids.append(p.info["pid"])
        except Exception:  # noqa: BLE001
            pass
    return pids


def find_windows():
    import win32gui
    import win32process

    pids = set(weixin_pids())
    rows = []

    def cb(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in pids:
                return
            title = win32gui.GetWindowText(hwnd)
            if title in ("微信", "Weixin"):
                rows.append((hwnd, pid, title))
        except Exception:  # noqa: BLE001
            pass

    win32gui.EnumWindows(cb, None)
    return rows


def walk_count(root, walker, depth=0, max_depth=8):
    if depth > max_depth:
        return 0
    total = 0
    child = walker.GetFirstChildElement(root)
    while child:
        total += 1 + walk_count(child, walker, depth + 1, max_depth)
        child = walker.GetNextSiblingElement(child)
    return total


def probe(with_narrator: bool) -> int:
    import comtypes.client
    from pywinauto import Desktop

    print("=" * 60)
    print("微信版本：", weixin_version())
    pids = weixin_pids()
    print("微信进程：", f"{len(pids)} 个" if pids else "未运行 ❌")
    if not pids:
        print("请先启动微信并登录后再运行本脚本。")
        return 1

    narrator = None
    if with_narrator:
        print("正在启动讲述人（临时）…")
        narrator = subprocess.Popen("C:\\Windows\\System32\\Narrator.exe")
        time.sleep(8)

    try:
        wins = find_windows()
        if not wins:
            print("没找到微信主窗口（可能最小化到托盘），请先点开微信主界面。")
            return 1

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)

        best = 0
        for hwnd, pid, title in wins:
            try:
                root = uia.ElementFromHandle(hwnd)
                ctl = walk_count(root, uia.ControlViewWalker)
                raw = walk_count(root, uia.RawViewWalker)
                best = max(best, ctl, raw)
                print(f"\n窗口 hwnd={hwnd} pid={pid} 标题={title!r}")
                print(f"  UIA 类名: {root.CurrentClassName!r}")
                print(f"  ControlView 控件数: {ctl}")
                print(f"  RawView 控件数: {raw}")
                if ctl + raw <= 3:
                    print("  ⚠️ UI 树被隐藏（微信主动不暴露控件）")
                else:
                    print("  ✅ UI 树可见")
            except Exception as exc:  # noqa: BLE001
                print(f"窗口 {hwnd} 探测失败：{type(exc).__name__}: {exc}")

        print("\npyweixin 期望的主窗口类名: 'mmui::MainWindow'")
        try:
            from pywinauto import Desktop as _D

            for hwnd, _, _ in wins:
                w = _D(backend="uia").window(handle=hwnd)
                print(f"  hwnd={hwnd} pywinauto class_name() = {w.class_name()!r}, children={len(w.children())}")
        except Exception as exc:  # noqa: BLE001
            print("  pywinauto 复检失败：", exc)

        print("\n结论：", "✅ 这个微信版本可以做 UI 自动化" if best > 20 else "❌ 这个微信版本隐藏了 UI 树，pyweixin 无法工作")
        return 0 if best > 20 else 2
    finally:
        if narrator:
            narrator.terminate()
            print("讲述人已关闭")


def main() -> int:
    parser = argparse.ArgumentParser(description="探测微信 UI 树是否可被自动化")
    parser.add_argument("--with-narrator", action="store_true", help="先临时启动讲述人再探测")
    args = parser.parse_args()
    return probe(args.with_narrator)


if __name__ == "__main__":
    raise SystemExit(main())
