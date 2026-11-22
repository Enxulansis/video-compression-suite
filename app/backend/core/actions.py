"""Completion actions (G8): tell me it is done / shut down / recycle the source.

Everything here is opt-in and OFF by default, because two of the three can hurt:

**"成功才删源" is Recycle Bin only.** The project's hard rule is that a source is never
destroyed. IFileOperation explicitly requests RECYCLEONDELETE and fails closed;
there is no permanent-delete fallback.

The dangerous half of that call is what happens when a volume has NO Recycle Bin:
Windows then deletes permanently, and reports success either way. A user who ticked
"成功才删源" would have asked for a recoverable move and received an unrecoverable
delete, with no signal. So the volume is checked first and the action is REFUSED when
the bin is unavailable -- `recycle_bin_available`.

**Only a cancellable power action is offered.** `shutdown /s /t <n>` can be delayed and
cancelled with `shutdown /a`; sleep and hibernate cannot (there is no timer, and
`shutdown /h` has no `/t`). Offering "sleep when done" would mean a machine that drops
the moment the last job ends, with no way to take it back, so it is deliberately NOT
offered -- see `POWER_ACTIONS`.

Also honest about the notification: this build has no WinRT toast (measured: the type is
unavailable under PowerShell 7), so `notify` plays an in-process system sound. It is
called "提示音" in the UI, not "通知".
"""

from __future__ import annotations

import ctypes
import os
import stat
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .caps import subprocess_kwargs

# ---------------------------------------------------------------- notify (sound) ---

_MB_ICONASTERISK = 0x00000040


def notify() -> Dict[str, Any]:
    """Play the system notification sound, in-process.

    `MessageBeep` is used rather than a subprocess or a toast: it needs no window, no
    PowerShell and no dependency, and it cannot fail loudly in the middle of a batch.
    """
    if sys.platform != "win32":
        return {"ok": False, "method": "", "error": "只有 Windows 有系统提示音"}
    try:
        ok = bool(ctypes.windll.user32.MessageBeep(_MB_ICONASTERISK))
    except OSError as exc:
        return {"ok": False, "method": "", "error": str(exc)}
    return {"ok": ok, "method": "system-sound",
            "error": "" if ok else "MessageBeep 返回失败"}


# ------------------------------------------------------------------ recycle (safe) ---

_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_NOERRORUI = 0x0400
_FOFX_RECYCLEONDELETE = 0x00080000
_FOFX_EARLYFAILURE = 0x00100000
_RECYCLE_FLAGS = (_FOF_SILENT | _FOF_NOCONFIRMATION | _FOF_NOERRORUI |
                  _FOFX_RECYCLEONDELETE | _FOFX_EARLYFAILURE)


def file_identity(path: str) -> Dict[str, Any]:
    """Cheap file identity, not a content-quality claim; links fail closed."""
    try:
        p = Path(path)
        current = p.lstat()
        if (not stat.S_ISREG(current.st_mode) or p.is_symlink()
                or getattr(current, 'st_file_attributes', 0) & 0x400 or not current.st_ino):
            return {}
        return {'path': os.path.normcase(str(p.resolve())), 'dev': current.st_dev,
                'ino': current.st_ino, 'size': current.st_size,
                'mtime_ns': current.st_mtime_ns, 'ctime_ns': current.st_ctime_ns}
    except (OSError, ValueError):
        return {}


def identity_matches(path: str, expected: Dict[str, Any]) -> bool:
    return bool(isinstance(expected, dict) and expected and file_identity(path) == expected)


def _is_recycle_destination(destination: str, source: str) -> bool:
    try:
        result = Path(destination).resolve()
        expected = Path(Path(source).resolve().anchor) / '$Recycle.Bin'
        return result.name.startswith('$R') and os.path.normcase(str(result.parent.parent)) == os.path.normcase(str(expected))
    except (OSError, ValueError):
        return False


def recycle_bin_available(path: str) -> Tuple[bool, str]:
    """Whether `path` can actually be sent to a Recycle Bin.

    Checked because `FOF_ALLOWUNDO` silently degrades to a PERMANENT delete on a volume
    without a bin (network shares, some removable media). This feature promises a
    recoverable move, so an unavailable bin must mean "refused", never "deleted anyway".
    """
    target = Path(str(path))
    if sys.platform != "win32":
        return False, "只有 Windows 支持回收站"
    raw = str(path)
    if raw.startswith("\\\\"):
        # A UNC path never has a per-volume Recycle Bin. Checked explicitly because the
        # drive-type probe below only understands drive letters, and "unknown volume"
        # would otherwise be reported as a missing drive.
        return False, "网络位置没有回收站，删了就找不回来"
    try:
        root = Path(target.resolve().anchor)
    except (OSError, ValueError):
        return False, "无法确定该文件所在盘符"
    if not root.anchor or not root.exists():
        return False, "所在盘符不可用"
    try:
        import ctypes as _ctypes
        drive_type = _ctypes.windll.kernel32.GetDriveTypeW(str(root))
    except OSError:
        return False, "无法判断盘符类型"
    # 2 = removable, 3 = fixed, 4 = network, 5 = cdrom, 6 = ramdisk. Only fixed and
    # removable volumes keep a per-volume Recycle Bin in practice; a network path never
    # does, and that is the case most likely to surprise someone.
    if drive_type not in (2, 3):
        return False, "该位置（网络/光驱等）没有回收站，删了就找不回来"
    if not (root / "$Recycle.Bin").is_dir():
        return False, "该盘没有回收站目录，删了就找不回来"
    return True, ""


def safe_to_recycle(src: str, out_path: str, verdict: str, *, exists: bool = True) -> str:
    """'' when the source may be recycled, else the reason to refuse.

    Pure on purpose: the decision is the part worth testing, and every rule here exists
    because getting it wrong destroys something irreplaceable.
    """
    if verdict != "passed":
        return "成品未通过校验（{}），不删源".format(verdict or "未知")
    if not src:
        return "源路径为空"
    if not exists:
        return "源文件已不存在"
    if not out_path:
        return "成品路径为空"
    try:
        if Path(src).resolve() == Path(out_path).resolve():
            return "源与成品是同一个文件"
        from .paths import is_same_file
        if is_same_file(Path(src), Path(out_path)):
            return "源与成品是同一个文件"
    except (OSError, ValueError):
        return "无法比较源与成品路径"
    try:
        # A source living inside the output folder is almost certainly a mistake (the
        # user pointed the output at their library), and recycling there could cascade.
        if Path(out_path).resolve().parent in Path(src).resolve().parents:
            return "源文件位于输出目录内，不删源"
    except (OSError, ValueError):
        # FAIL CLOSED. This check exists to stop a cascade, so "I could not work out
        # whether the source is inside the output folder" has to mean "do not delete",
        # not "carry on". It used to `pass`, which turned an unreadable path (a
        # permission error on a parent directory, a disconnected drive) into permission
        # to recycle the user's source file.
        return "无法确认源文件是否位于输出目录内，谨慎起见不删源"
    ok, why = recycle_bin_available(src)
    if not ok:
        return why
    return ""


def recycle(path: str, *, recovery_path: str = '', expected_identity=None) -> Dict[str, Any]:
    """Recycle one ordinary file through IFileOperation; never fall back to deletion."""
    if sys.platform != "win32":
        return {"ok": False, "error": "只有 Windows 支持回收站", "code": -1}
    target = Path(str(path))
    if not file_identity(str(target)):
        return {"ok": False, "error": "文件不存在或不是普通文件", "code": 2}
    allowed, why = recycle_bin_available(str(target))
    if not allowed:
        return {'ok': False, 'error': why}
    # Keep an inexpensive same-volume recovery link until the shell proves the
    # destination. Some restricted profiles silently delete despite recycle flags.
    # Unsupported filesystems fail closed, never copy an entire video into RAM.
    before = file_identity(str(target))
    if expected_identity is not None and before != expected_identity:
        return {'ok': False, 'error': '源文件与本次授权的身份不一致，已保留'}
    backup = Path(recovery_path) if recovery_path else target.with_name('.vcs-recovery-' + uuid.uuid4().hex)
    if (backup.parent.resolve() != target.parent.resolve() or
            not backup.name.startswith('.vcs-recovery-') or len(backup.name) != len('.vcs-recovery-') + 32):
        return {'ok': False, 'error': '恢复副本位置无效，已保留源文件'}
    try:
        os.link(target, backup, follow_symlinks=False)
    except OSError as exc:
        return {'ok': False, 'error': '无法建立临时恢复保护，源文件已保留：{}'.format(exc)}
    result = {'ok': False, 'error': '回收站操作未完成'}
    try:
        current = file_identity(str(target))
        if not current or any(current[k] != before[k] for k in ('path', 'dev', 'ino', 'size', 'mtime_ns')):
            result = {'ok': False, 'error': '源文件在处理前发生变化'}
        else:
            result = _recycle_windows(str(target.resolve()))
        destination = result.get('recycled_path') or ''
        recoverable = bool(destination and _is_recycle_destination(destination, str(target)) and Path(destination).is_file()
                           and os.path.samefile(backup, destination))
        if result.get('ok') and recoverable and not target.exists():
            backup.unlink()
            return result
        result = {'ok': False, 'error': result.get('error') or '无法确认回收站中的可还原文件'}
    except Exception as exc:
        result = {'ok': False, 'error': str(exc)}
    # Restore only to an unoccupied original name. An unrelated replacement must
    # never be overwritten or removed during error recovery.
    try:
        if not os.path.lexists(target):
            from .paths import rename_without_replace
            rename_without_replace(backup, target)
        elif os.path.samefile(backup, target):
            backup.unlink()
        else:
            result['error'] += '；原路径已被其它文件占用，恢复副本保留在 {}'.format(backup)
    except OSError as exc:
        result['error'] += '；恢复副本保留在 {}：{}'.format(backup, exc)
    return result


def _recycle_windows(path: str) -> Dict[str, Any]:
    """Minimal COM calls, scoped to the calling worker's apartment and owned items."""
    guid_type = ctypes.c_ubyte * 16
    def guid(value):
        return guid_type.from_buffer_copy(uuid.UUID(value).bytes_le)
    ole = ctypes.OleDLL('ole32')
    shell = ctypes.OleDLL('shell32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    shell.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                                ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    initialized = False
    operation = ctypes.c_void_p()
    item = ctypes.c_void_p()
    recycled = {'path': '', 'predelete': False}
    def call(ptr, index, types=(), args=()):
        vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(vtable[index])
        result = method(ptr, *args)
        if result < 0:
            raise OSError('回收站操作失败（0x{:08X}），未尝试永久删除'.format(result & 0xffffffff))
        return result
    # IFileOperationProgressSink: veto a non-recycling operation and obtain the
    # actual shell destination, rather than enumerating unrelated recycle records.
    callbacks = []
    def callback(types, fn):
        value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(fn)
        callbacks.append(value)
        return ctypes.cast(value, ctypes.c_void_p).value
    P, D, W, H = ctypes.c_void_p, wintypes.DWORD, wintypes.LPCWSTR, ctypes.c_long
    signatures = [None, (), (), (), (H,), (D,P,W), (D,P,W,H,P),
                  (D,P,P,W), (D,P,P,W,H,P), (D,P,P,W), (D,P,P,W,H,P),
                  (D,P), (D,P,H,P), (D,P,W), (D,P,W,W,D,H,P), (D,D), (), (), ()]
    vtable = (ctypes.c_void_p * len(signatures))()
    class Sink(ctypes.Structure):
        _fields_ = [('vtable', ctypes.POINTER(ctypes.c_void_p))]
    sink = Sink(vtable)
    references = [1]
    def query(this, iid, output):
        supported = [uuid.UUID(v).bytes_le for v in ('00000000-0000-0000-C000-000000000046',
                                                     '04B0F1A7-9490-44BC-96E1-4296A31252E2')]
        if ctypes.string_at(iid, 16) not in supported:
            output[0] = None
            return -2147467262  # E_NOINTERFACE
        output[0] = ctypes.addressof(sink); references[0] += 1
        return 0
    def predelete(this, flags, source):
        recycled['predelete'] = bool(flags & 0x80)  # TSF_DELETE_RECYCLE_IF_POSSIBLE
        return 0 if recycled['predelete'] else -2147467260  # E_ABORT
    def postdelete(this, flags, source, status, destination):
        if status < 0 or not destination:
            return 0
        address = ctypes.c_void_p()
        try:
            call(destination, 5, (ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)),
                 (0x80058000, ctypes.byref(address)))  # SIGDN_FILESYSPATH
            if address.value:
                recycled['path'] = ctypes.wstring_at(address)
        except Exception:
            return -2147467260
        finally:
            if address.value:
                ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
                ole.CoTaskMemFree(address)
        return 0
    for index, types in enumerate(signatures):
        if index == 0:
            vtable[index] = callback((P, ctypes.POINTER(P)), query)
        elif index in (1, 2):
            delta = 1 if index == 1 else -1
            def reference(this, delta=delta):
                references[0] += delta
                return references[0]
            vtable[index] = callback((), reference)
        elif index == 11:
            vtable[index] = callback(types, predelete)
        elif index == 12:
            vtable[index] = callback(types, postdelete)
        else:
            vtable[index] = callback(types, lambda *args: 0)
    try:
        hr = ole.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        if hr < 0:
            raise OSError('无法安全初始化回收站操作')
        initialized = True
        clsid = guid('3AD05575-8857-4850-9277-11B85BDB8E09')
        iid = guid('947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8')
        ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(operation))
        if not operation:
            raise OSError('无法创建可恢复的回收站操作')
        call(operation, 5, (wintypes.DWORD,), (_RECYCLE_FLAGS,))
        item_iid = guid('43826D1E-E718-42EE-BC55-A1E261C37BFE')
        shell.SHCreateItemFromParsingName(path, None, ctypes.byref(item_iid), ctypes.byref(item))
        if not item:
            raise OSError('无法确认待回收的文件')
        call(operation, 18, (ctypes.c_void_p, ctypes.c_void_p), (item, ctypes.byref(sink)))
        call(operation, 21)
        aborted = wintypes.BOOL()
        call(operation, 22, (ctypes.POINTER(wintypes.BOOL),), (ctypes.byref(aborted),))
        if aborted.value:
            return {'ok': False, 'error': '回收站操作被中止', 'aborted': True}
        if Path(path).exists():
            return {'ok': False, 'error': '回收站未接收文件，源文件已保留'}
        if not recycled['predelete'] or not recycled['path']:
            return {'ok': False, 'error': '系统未提供可还原的回收站落点'}
        return {'ok': True, 'error': '', 'code': 0, 'aborted': False,
                'recycled_path': recycled['path']}
    finally:
        if item:
            call(item, 2)
        if operation:
            call(operation, 2)
        if initialized:
            ole.CoUninitialize()


# ------------------------------------------------------------------- power (G8) ---

#: ONLY cancellable actions are offered. `shutdown /s /t <n>` can be delayed and undone
#: with `shutdown /a`; sleep and hibernate have no timer (`shutdown /h` accepts no `/t`),
#: so offering them would mean a machine that drops instantly with no way back. That is
#: a decision about the user's hardware, not a feature.
POWER_ACTIONS = ("shutdown",)
DEFAULT_POWER_DELAY = 60
MIN_POWER_DELAY = 15
MAX_POWER_DELAY = 3600


def clamp_delay(seconds: Any) -> int:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return DEFAULT_POWER_DELAY
    return max(MIN_POWER_DELAY, min(MAX_POWER_DELAY, value))


def power_command(kind: str, delay: int = DEFAULT_POWER_DELAY,
                  reason: str = "") -> List[str]:
    """The argv that schedules the action. Pure, so the command can be tested without
    running it -- actually scheduling a shutdown on someone's machine to test it is not
    an acceptable thing to do to a user."""
    if kind not in POWER_ACTIONS:
        raise ValueError("不支持的完成动作: {}".format(kind))
    comment = (reason or "视频压缩套件：批量任务已完成")[:200]
    return ["shutdown", "/s", "/t", str(clamp_delay(delay)), "/c", comment]


def cancel_command() -> List[str]:
    return ["shutdown", "/a"]


def power_action(kind: str, delay: int = DEFAULT_POWER_DELAY,
                 reason: str = "") -> Dict[str, Any]:
    """Schedule the action. Windows will show its own countdown; `cancel_power` undoes
    it. Returns {ok, error, delay, command}."""
    if sys.platform != "win32":
        return {"ok": False, "error": "只有 Windows 支持自动关机"}
    try:
        argv = power_command(kind, delay, reason)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        proc = subprocess.run(argv, timeout=30, **subprocess_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": "执行失败：{}".format(exc), "command": argv}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-200:]
        return {"ok": False, "error": detail or "shutdown 返回 {}".format(proc.returncode),
                "command": argv}
    return {"ok": True, "error": "", "delay": clamp_delay(delay), "command": argv}


def cancel_power() -> Dict[str, Any]:
    """Cancel a pending shutdown (`shutdown /a`)."""
    if sys.platform != "win32":
        return {"ok": False, "error": "只有 Windows 支持取消关机"}
    try:
        proc = subprocess.run(cancel_command(), timeout=30, **subprocess_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": "取消失败：{}".format(exc)}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-200:]
        return {"ok": False, "error": detail or "没有待取消的关机"}
    return {"ok": True, "error": ""}


def describe_action(kind: str, delay: int = DEFAULT_POWER_DELAY) -> str:
    if kind not in POWER_ACTIONS:
        return ""
    return "任务全部结束后 {} 秒自动关机（可取消）".format(clamp_delay(delay))
