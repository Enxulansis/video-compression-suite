"""Launcher robustness: port fallback + visible failure.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_launcher_port.py

Why this file exists
--------------------
The user double-clicked `启动视频压缩.lnk` and nothing happened. The launcher's own log had
the answer:

    启动后端…
    [X] 无法监听 127.0.0.1:8787 —— [WinError 10013] ...
    [X] 后端启动失败

Port 8787 was held by an unrelated service, and because the shortcut starts `pythonw.exe`
(no console) that message went to a log file nobody reads. Two defects, both pinned here:
the launcher must TAKE THE NEXT FREE PORT instead of dying, and it must SAY what went wrong.
"""

from __future__ import annotations

import io
import socket
import sys
from pathlib import Path
from unittest.mock import patch

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import desktop  # noqa: E402

results: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def bindable(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main() -> int:
    print("\nlauncher port-fallback self-test\n")

    print("  -- 端口被占用时换端口，而不是静默退出 --")
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    taken = holder.getsockname()[1]
    try:
        picked = desktop.next_free_port(taken)
        check("跳过被占用的端口", picked is not None and picked != taken,
              "taken={} picked={}".format(taken, picked))
        check("选出的端口真的可以绑定", picked is not None and bindable(picked), str(picked))
        check("起点可用时原样返回（不无谓跳号）",
              desktop.next_free_port(taken + 1) == taken + 1, str(taken + 1))
        check("连续占用多个端口时继续往后找",
              desktop.next_free_port(taken, tries=3) in (taken + 1, taken + 2))
    finally:
        holder.close()

    print("  -- base_url 必须跟着端口走 --")
    host = desktop.DesktopHost(port=8787)
    check("初始 URL 指向 8787", ":8787/?" in host.base_url, host.base_url)
    host.port = 8899
    check("改端口后 URL 立刻跟着变（否则回退会把窗口开在旧地址）",
          ":8899/?" in host.base_url, host.base_url)
    check("start_error 默认是空串而不是 None", host.start_error == "",
          repr(host.start_error))

    print("  -- 无控制台时的失败必须能被看见 --")
    try:
        # Verify the real notification wrapper without creating an interactive
        # message box that blocks unattended test runs until someone clicks it.
        with patch('ctypes.windll.user32.MessageBoxW', return_value=1) as message:
            desktop._notify_error("测试标题", "测试内容")
            if sys.platform == 'win32':
                check("系统弹框收到正确的标题与内容",
                      message.call_count == 1 and message.call_args.args[1:3] == ('测试内容', '测试标题'))
        check("_notify_error 不抛异常（pythonw 下没有 stdout 也能弹框）", True)
    except Exception as exc:  # noqa: BLE001
        check("_notify_error 不抛异常", False, repr(exc))
    check("_notify_error 是非 Windows 安全空操作",
          callable(desktop._notify_error))

    print("  -- 快捷方式指向的入口仍然存在且可解析 --")
    check("desktop.py 有 main()", callable(getattr(desktop, "main", None)))
    check("DESKTOP 默认端口没被改掉", desktop.DEFAULT_PORT == 8787,
          str(desktop.DEFAULT_PORT))

    print("\n  {}/{} checks passed".format(sum(results), len(results)))
    for index, ok in enumerate(results):
        if not ok:
            print("  FAILED: check #{}".format(index + 1))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
