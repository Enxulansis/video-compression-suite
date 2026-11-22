"""Frozen desktop entry point. This file has no dependence on a source checkout."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as W
import os
import sys
from pathlib import Path

# The supported Windows baseline provides .NET Framework 4.8. Do not accidentally
# select a caller's CoreCLR environment and introduce an unbundled .NET dependency.
os.environ['PYTHONNET_RUNTIME'] = 'netfx'
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from core import runtime
import desktop


def _activate_existing_window() -> None:
    """Restore only a visible window owned by this exact installed executable."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    user.EnumWindows.argtypes = [callback_type, W.LPARAM]
    user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    user.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [W.HWND]

    @callback_type
    def inspect(hwnd, _):
        title = ctypes.create_unicode_buffer(512)
        user.GetWindowTextW(hwnd, title, len(title))
        if title.value != desktop.WINDOW_TITLE:
            return True
        pid = W.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        image = desktop.winapi.process_image_path(pid.value)
        if image and os.path.normcase(image) == os.path.normcase(sys.executable):
            user.ShowWindow(hwnd, 9)
            user.SetForegroundWindow(hwnd)
            return False
        return True

    user.EnumWindows(inspect, 0)


def main() -> int:
    desktop._attach_log_when_windowless()
    if '--self-test' in sys.argv or '--test-window' in sys.argv:
        import package_checks
        flag = '--test-window' if '--test-window' in sys.argv else '--self-test'
        report = Path(sys.argv[sys.argv.index(flag) + 1])
        return package_checks.run(report, window=flag == '--test-window')

    # Inno Setup checks this mutex before an upgrade/uninstall. It must never
    # forcibly close an application that may still be encoding a video.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    mutex = kernel.CreateMutexW(None, False, runtime.APP_MUTEX)
    existing = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    if not mutex:
        raise ctypes.WinError()
    try:
        if existing:
            _activate_existing_window()
            return 0
        kernel.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
        kernel.OpenMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        installing = kernel.OpenMutexW(0x00100000, False, 'VideoCompressionSuite.Setup')
        if installing:
            kernel.CloseHandle(installing)
            desktop._notify_error('视频压缩套件', '程序正在安装、更新或卸载，请完成后再打开。')
            return 2
        return desktop.main()
    finally:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(mutex)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        desktop._notify_error('视频压缩套件：启动失败',
                              '启动失败：{}\n\n日志：{}'.format(exc, runtime.data_root() / 'logs' / 'desktop.log'))
        raise SystemExit(1)
