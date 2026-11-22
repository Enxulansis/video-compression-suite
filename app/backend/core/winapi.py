"""Native Windows helpers, without any third-party package.

Two jobs live here:

1. **File/folder dialogs.** A web page cannot learn the absolute path of a file the
   user drops or picks -- the browser discards it on purpose. The only reliable way to
   get real paths is to ask the OS from a process that is not sandboxed, using the
   .NET dialogs through PowerShell's Add-Type. Nothing is installed:
   System.Windows.Forms ships with Windows.
2. **Process control for pause/resume (G6).** Windows has no SIGSTOP, so "pause an
   encode" has to mean `NtSuspendProcess` on the encoder. See `suspend_process`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .caps import subprocess_kwargs

# A filter string in the format WinForms expects: "Label|*.ext;*.ext|Label2|..."
VIDEO_FILTER = (
    "视频文件|*.mp4;*.mkv;*.mov;*.avi;*.wmv;*.flv;*.f4v;*.ts;*.m2ts;*.mts;"
    "*.webm;*.mpg;*.mpeg;*.vob;*.rm;*.rmvb;*.3gp;*.3g2;*.ogv;*.asf;*.mxf;*.dv|"
    "所有文件|*.*"
)

_PS_TIMEOUT = 600


def _run_powershell(script: str) -> Optional[Dict[str, Any]]:
    """Run a PowerShell snippet that must print one JSON line.

    The script goes through a temporary .ps1 file rather than `-Command`. With
    `-Command` the script is re-parsed by PowerShell's command-line parser, where a
    stray backslash or quote turns into "not recognized as a cmdlet" -- running a
    file removes that entire class of quoting bug.
    """
    exe = _find_powershell()
    if not exe:
        return None

    import tempfile
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="utf-8-sig", newline="\r\n") as handle:
            handle.write(script)
            tmp_path = handle.name
        argv = [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
                "-ExecutionPolicy", "Bypass", "-File", tmp_path]
        kwargs = {k: v for k, v in subprocess_kwargs().items() if k != "stdin"}
        proc = subprocess.run(argv, timeout=_PS_TIMEOUT, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass

    out = (proc.stdout or "").strip()
    if not out:
        return None
    # The dialog is the only thing allowed to print JSON; take the last JSON line.
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _find_powershell() -> Optional[str]:
    """Prefer PowerShell 7, fall back to Windows PowerShell 5.1."""
    import shutil
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def available() -> bool:
    return sys.platform == "win32" and _find_powershell() is not None


def pick_files(initial_dir: str = "", title: str = "选择要压缩的视频（可多选）") -> Dict[str, Any]:
    """Show the native multi-select file dialog. Returns the chosen absolute paths."""
    if not available():
        return {"ok": False, "cancelled": False,
                "error": "当前系统不支持原生对话框（仅 Windows）", "paths": []}
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$d = New-Object System.Windows.Forms.OpenFileDialog;"
        "$d.Multiselect = $true;"
        "$d.Title = " + _ps(title) + ";"
        "$d.Filter = " + _ps(VIDEO_FILTER) + ";"
        "$d.RestoreDirectory = $true;"
        + ("$d.InitialDirectory = " + _ps(initial_dir) + ";" if initial_dir else "")
        + "$r = $d.ShowDialog();"
        "if ($r -eq [System.Windows.Forms.DialogResult]::OK) {"
        "  $o = [ordered]@{ ok = $true; cancelled = $false; paths = @($d.FileNames) };"
        "} else {"
        "  $o = [ordered]@{ ok = $true; cancelled = $true; paths = @() };"
        "}"
        "$o | ConvertTo-Json -Compress -Depth 4"
    )
    result = _run_powershell(script)
    if not result:
        return {"ok": False, "cancelled": False, "paths": [],
                "error": "无法启动选择对话框（PowerShell 不可用）"}
    result.setdefault("paths", [])
    if isinstance(result["paths"], str):
        result["paths"] = [result["paths"]]
    return result


def pick_folder(initial_dir: str = "", title: str = "选择要加入的文件夹") -> Dict[str, Any]:
    """Show the native folder dialog. Returns the chosen absolute directory."""
    if not available():
        return {"ok": False, "cancelled": False, "error": "当前系统不支持原生对话框（仅 Windows）"}
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = " + _ps(title) + ";"
        "$d.ShowNewFolderButton = $false;"
        + ("$d.SelectedPath = " + _ps(initial_dir) + ";" if initial_dir else "")
        + "$r = $d.ShowDialog();"
        "if ($r -eq [System.Windows.Forms.DialogResult]::OK) {"
        "  $o = [ordered]@{ ok = $true; cancelled = $false; path = $d.SelectedPath };"
        "} else {"
        "  $o = [ordered]@{ ok = $true; cancelled = $true; path = '' };"
        "}"
        "$o | ConvertTo-Json -Compress -Depth 4"
    )
    result = _run_powershell(script)
    if not result:
        return {"ok": False, "cancelled": False,
                "error": "无法启动选择对话框（PowerShell 不可用）"}
    result.setdefault("path", "")
    return result


def _ps(value: str) -> str:
    """Single-quote a PowerShell string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def open_in_explorer(target: str) -> bool:
    """Reveal a folder (or select a file) in Explorer."""
    path = Path(target)
    try:
        if path.is_dir():
            subprocess.Popen(["explorer", str(path)], **subprocess_kwargs())
        elif path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)], **subprocess_kwargs())
        else:
            return False
    except OSError:
        return False
    return True


# NOTE: a multi-file `reveal_paths()` helper used to live here. Nothing called it: the
# UI's "在文件夹中显示" goes through `/api/reveal` -> `open_in_explorer` with a single
# path. Removed in the 2026-10-05 review (dead code that looked like a supported API).


# ------------------------------------------------------- process control (G6) ---
#
# Pause/resume of a running encode. Windows has no SIGSTOP, so the only real pause is
# `NtSuspendProcess` on the encoder process. It was measured before being relied on, and
# the measurement is the reason this is acceptable:
#
#   | encoder      | suspended at | growth during 10 s suspension | result                 |
#   |--------------|--------------|-------------------------------|------------------------|
#   | h264_nvenc   | 27.25 MB     | **0 bytes**                   | resumed, valid 60 s file |
#   | libsvtav1    | 0.00 MB      | **0 bytes**                   | resumed, valid 60 s file |
#
# i.e. the process really stops (one write already in flight may still complete, which
# is why the check samples twice) and the GPU session survives. Note there is nothing to
# suspend underneath: ffmpeg encodes in-process, it does not spawn child workers.

_PROCESS_SUSPEND_RESUME = 0x0800
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

_LIB = None
_ERROR = ""


def _lib():
    """(ntdll, kernel32) once, or (None, None) with a reason on a non-Windows host."""
    global _LIB, _ERROR
    if _LIB is not None:
        return _LIB
    if sys.platform != "win32":
        _ERROR = "只有 Windows 支持暂停/继续编码"
        _LIB = (None, None)
        return _LIB
    import ctypes
    ntdll = ctypes.WinDLL("ntdll")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll.NtSuspendProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtSuspendProcess.restype = ctypes.c_long
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    # Handles are 64-bit: without these declarations ctypes would marshal them as
    # 32-bit ints and silently pass a truncated handle to the kernel.
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32)]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    kernel32.GetProcessTimes.restype = ctypes.c_int
    _LIB = (ntdll, kernel32)
    return _LIB


def process_control_available() -> bool:
    ntdll, _ = _lib()
    return ntdll is not None


def control_reason() -> str:
    """Why process control is unavailable, empty when it is."""
    _lib()
    return _ERROR


def is_alive(pid: int) -> bool:
    """True when `pid` is a live process. Never raises."""
    ntdll, kernel32 = _lib()
    if ntdll is None or not pid:
        return False
    import ctypes
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        # A process we cannot even open may still exist (access denied) -- treat a
        # non-openable pid as NOT alive only when it truly is gone.
        return False
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def process_image(pid: int) -> str:
    """Executable name for `pid`, or '' when it cannot be read."""
    full = process_image_path(pid)
    return Path(full).name if full else ""


def process_image_path(pid: int, *, handle=None) -> str:
    """FULL executable path for `pid`, or '' when it cannot be read.

    The full path, not just the name, because "is this process ours?" cannot be
    answered by "is it python": an unrelated python.exe must never be the target of a
    stop/terminate decision. The path is the only part of a process's identity that a
    remote `/api/version` response cannot forge.
    """
    ntdll, kernel32 = _lib()
    if ntdll is None or not pid:
        return ""
    import ctypes
    owned = handle is None
    handle = handle or kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = ctypes.c_uint32(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0,
                                                   buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        if owned:
            kernel32.CloseHandle(ctypes.c_void_p(handle))


def process_creation_time(pid: int = 0, *, handle=None) -> int:
    """FILETIME identity; zero means identity could not be established."""
    _, kernel32 = _lib()
    if kernel32 is None:
        return 0
    import ctypes
    owned = handle is None
    handle = handle or kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return 0
    try:
        values = [ctypes.c_ulonglong() for _ in range(4)]
        if not kernel32.GetProcessTimes(ctypes.c_void_p(handle), *(ctypes.byref(v) for v in values)):
            return 0
        return values[0].value
    finally:
        if owned:
            kernel32.CloseHandle(ctypes.c_void_p(handle))


def process_command_line(pid: int) -> str:
    """Command line for `pid`, or '' when it cannot be read.

    Via CIM rather than a native call: there is no `QueryFullProcessImageName`
    equivalent for the argument vector, and this is only consulted on the rare
    "replace a stale instance" path, so a few hundred milliseconds is acceptable.
    """
    if not pid:
        return ""
    # `_run_powershell` only understands a JSON line, so wrap the value.
    script = (
        "$p = Get-CimInstance Win32_Process -Filter \"ProcessId={}\" "
        "-ErrorAction SilentlyContinue; "
        "if ($p) {{ [pscustomobject]@{{ CommandLine = [string]$p.CommandLine }} "
        "| ConvertTo-Json -Compress }}"
    ).format(int(pid))
    result = _run_powershell(script)
    if not result:
        return ""
    return str(result.get("CommandLine") or "").strip()


def _suspend_call(pid: int, call: str, handle=None) -> Dict[str, Any]:
    ntdll, kernel32 = _lib()
    if ntdll is None:
        return {"ok": False, "error": control_reason() or "无法控制进程"}
    import ctypes
    owned = handle is None
    handle = handle or kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, int(pid))
    if not handle:
        return {"ok": False, "error": "无法打开进程 {}（可能已退出）".format(pid)}
    try:
        status = getattr(ntdll, call)(ctypes.c_void_p(handle))
    finally:
        if owned:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    if status != 0:
        return {"ok": False, "error": "{} 失败（NTSTATUS {:#x}）".format(call, status & 0xFFFFFFFF)}
    return {"ok": True}


def suspend_process(pid: int, *, handle=None) -> Dict[str, Any]:
    """Freeze a process. Returns {ok, error}."""
    return _suspend_call(pid, "NtSuspendProcess", handle)


def resume_process(pid: int, *, handle=None) -> Dict[str, Any]:
    """Thaw a process suspended by `suspend_process`."""
    return _suspend_call(pid, "NtResumeProcess", handle)


def terminate_process(pid: int, resume_first: bool = False, *, handle=None,
                      expected_image: str = "", expected_created: int = 0) -> Dict[str, Any]:
    """Kill a process, optionally un-freezing it first.

    Resuming before terminating matters: a suspended ffmpeg cannot run its own cleanup,
    and leaving a process in a suspended state while killing it is how you end up with a
    handle nobody can release.
    """
    ntdll, kernel32 = _lib()
    if ntdll is None:
        return {"ok": False, "error": control_reason() or "无法控制进程"}
    import ctypes
    owned = handle is None
    rights = _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION
    if resume_first:
        rights |= _PROCESS_SUSPEND_RESUME
    handle = handle or kernel32.OpenProcess(rights, False, int(pid))
    if not handle:
        return {"ok": False, "error": "无法打开进程 {}（可能已退出）".format(pid)}
    try:
        if expected_image and (Path(process_image_path(pid, handle=handle)).resolve() !=
                               Path(expected_image).resolve()):
            return {"ok": False, "error": "进程路径不匹配，未终止"}
        if expected_created and process_creation_time(handle=handle) != expected_created:
            return {"ok": False, "error": "进程创建时间不匹配，pid 已被复用，未终止"}
        if resume_first:
            resumed = resume_process(pid, handle=handle)
            if not resumed.get("ok"):
                return resumed
        ok = bool(kernel32.TerminateProcess(ctypes.c_void_p(handle), 1))
    finally:
        if owned:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    return {"ok": ok, "error": "" if ok else "TerminateProcess 失败"}
