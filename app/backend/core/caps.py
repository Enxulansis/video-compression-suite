"""Runtime capability detection for the media toolchain.

Capabilities are read from the local FFmpeg build, and exposed hardware encoders
are runtime-tested. Registration alone never proves hardware availability.
Unused hardware encoders do not delay startup or become selectable by default.

Uses only the Python standard library (Python 3.9+).
"""

from __future__ import annotations

import json
import copy
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import runtime

# Windows: never pop a console window for our child processes.
_CREATE_NO_WINDOW = 0x08000000
_STARTUPINFO = None
if sys.platform == "win32":  # pragma: no cover - platform specific
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _STARTUPINFO.wShowWindow = 0


def subprocess_kwargs() -> Dict[str, Any]:
    """Keyword arguments shared by every ffmpeg/ffprobe invocation."""
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
        kwargs["startupinfo"] = _STARTUPINFO
    return kwargs


# Candidate locations, in priority order, if the binary is not on PATH.
# Kept as a fallback only; PATH always wins.
_WINDOWS_HINTS = [
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
    os.path.expandvars(r"%USERPROFILE%\scoop\shims"),
    os.path.expandvars(r"%ProgramData%\chocolatey\bin"),
]

# Only capabilities exposed by this application need a startup runtime probe.
# Keep this list shared with the API so unused registered encoders cannot delay it.
DISPLAY_ENCODERS = (
    "libsvtav1", "libaom-av1", "librav1e", "av1_nvenc", "av1_qsv", "av1_amf",
    "hevc_nvenc", "hevc_qsv", "libx265", "h264_nvenc", "h264_qsv", "libx264",
    "libvvenc", "libvpx-vp9", "aac", "libopus", "libmp3lame", "flac",
)
HARDWARE_SUFFIXES = ('_nvenc', '_qsv', '_amf')


def _primary_hardware() -> set:
    # Repair can use these encoders independently of the chosen compression profile.
    required = {'h264_nvenc', 'hevc_nvenc', 'av1_nvenc'}
    path = runtime.profiles_path()
    profiles = json.loads(path.read_text(encoding='utf-8'))['profiles']
    for profile in profiles:
        for key in ('encoder', 'fallback_encoder'):
            name = profile.get(key) or ''
            if name.endswith(HARDWARE_SUFFIXES):
                required.add(name)
    return required


def _find_binary(name: str) -> Optional[str]:
    if runtime.frozen():
        bundled = runtime.resource_root() / "bin" / (name + ".exe")
        return str(bundled) if bundled.is_file() else None
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if sys.platform == "win32" else "")
    for hint in _WINDOWS_HINTS:
        candidate = Path(hint) / exe
        if candidate.is_file():
            return str(candidate)
    return None


def _run(argv: List[str], timeout: int = 30) -> str:
    from .work import startup_probe_slot
    try:
        with startup_probe_slot(timeout) as remaining:
            proc = subprocess.run(argv, timeout=remaining, **subprocess_kwargs())
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


class Capabilities:
    """Snapshot of what the local FFmpeg can actually do."""

    def __init__(self) -> None:
        self.ffmpeg: Optional[str] = _find_binary("ffmpeg")
        self.ffprobe: Optional[str] = _find_binary("ffprobe")
        self.version: str = ""
        self.encoders: List[str] = []
        self.hwaccels: List[str] = []
        self.filters: List[str] = []
        self.error: str = ""
        self.hardware_probes: Dict[str, Dict[str, Any]] = {}
        self.pixel_formats: Dict[str, List[str]] = {}
        self.hardware_pending: List[str] = []
        self.supplemental_error: str = ''
        self._on_primary = None

    def snapshot(self) -> "Capabilities":
        result = copy.copy(self)
        for name in ('encoders', 'filters', 'hwaccels', 'hardware_pending', 'hardware_probes', 'pixel_formats'):
            setattr(result, name, copy.deepcopy(getattr(self, name)))
        result._on_primary = None
        return result

    # ---------------------------------------------------------------- probing

    def probe(self) -> "Capabilities":
        if not self.ffmpeg or not self.ffprobe:
            self.error = (
                "未找到 ffmpeg/ffprobe。请安装 FFmpeg 并确保其在 PATH 中，"
                "或把 ffmpeg.exe 所在目录加入 PATH 后重启本程序。"
            )
            return self
        banner = _run([self.ffmpeg, "-hide_banner", "-version"])
        match = re.search(r"ffmpeg version (\S+)", banner)
        self.version = match.group(1) if match else "unknown"

        self.encoders = self._list(
            [self.ffmpeg, "-hide_banner", "-encoders"], r"^\s*[VASD.]{6}\s+(\S+)"
        )
        # The filter flag column is up to three characters (T/S/C or '.'), and ffmpeg
        # left-pads it -- `libvmaf` prints as " .. libvmaf", i.e. only two dots.
        # Requiring exactly three therefore hid every such filter, and `has_filter`
        # confidently reported that libvmaf was missing while it worked fine. Match
        # 1-3 flags and reject the two legend lines ("T.. = Timeline support").
        self.filters = self._list(
            [self.ffmpeg, "-hide_banner", "-filters"],
            r"^\s*[TSC.]{1,3}\s+([A-Za-z0-9_]+)\s+\S",
        )
        hw = _run([self.ffmpeg, "-hide_banner", "-hwaccels"])
        self.hwaccels = [
            line.strip()
            for line in hw.splitlines()
            if line.strip() and not line.lower().startswith("hardware")
        ]
        # A registered hardware encoder can still lack a usable device or driver.
        # Two synthetic frames, serially. Publish current profile requirements first;
        # keep secondary device validation running without holding up the whole page.
        deadline = time.monotonic() + 12
        primary = _primary_hardware()
        names = sorted((n for n in self.encoders if (n in DISPLAY_ENCODERS or n in primary) and n.endswith(HARDWARE_SUFFIXES)),
                       key=lambda n: (n not in primary, 0 if n.endswith('_nvenc') else 1 if n.endswith('_qsv') else 2,n))
        self.hardware_pending = list(names)
        published = False
        def publish_primary():
            nonlocal published
            if not published and not primary.intersection(self.hardware_pending):
                published = True
                if self._on_primary:
                    self._on_primary(self)
        publish_primary()
        for name in names:
            remaining = min(1.5, deadline - time.monotonic())
            if remaining <= 0:
                self.hardware_probes[name] = {'ok':False,'error':'硬件运行探测总时限已到'}
                self.hardware_pending.remove(name)
                publish_primary()
                continue
            help_text = _run([self.ffmpeg, '-hide_banner', '-h', 'encoder=' + name], timeout=remaining)
            match = re.search(r'Supported pixel formats:\s*([^\r\n]+)', help_text)
            self.pixel_formats[name] = match.group(1).split() if match else []
            result = self._test_hardware(name, 'yuv420p', deadline)
            self.hardware_probes[name] = result
            if result['ok'] and name in ('hevc_nvenc', 'av1_nvenc'):
                result['ten_bit'] = self._test_hardware(name, 'p010le', deadline)
            self.hardware_pending.remove(name)
            publish_primary()
        return self

    def _test_hardware(self, name: str, pix_fmt: str, deadline: float) -> Dict[str, Any]:
        remaining = min(1.5, deadline - time.monotonic())
        if remaining <= 0:
            return {'ok': False, 'error': '硬件运行探测总时限已到'}
        argv = [self.ffmpeg, '-hide_banner', '-v', 'error', '-nostdin', '-f', 'lavfi',
                '-i', 'color=size=320x180:rate=24:duration=0.1', '-frames:v', '2',
                '-an', '-threads', '1', '-filter_threads', '1', '-c:v', name,
                '-pix_fmt', pix_fmt]
        if name == 'hevc_nvenc' and pix_fmt == 'p010le':
            argv += ['-profile:v', 'main10']
        argv += ['-f', 'null', '-']
        from .work import startup_probe_slot
        try:
            with startup_probe_slot(remaining) as child_timeout:
                proc = subprocess.run(argv, timeout=child_timeout, **subprocess_kwargs())
            return {'ok': proc.returncode == 0, 'error': (proc.stderr or '')[-600:] if proc.returncode else ''}
        except (OSError, subprocess.SubprocessError) as exc:
            return {'ok': False, 'error': str(exc)}

    @staticmethod
    def _list(argv: List[str], pattern: str) -> List[str]:
        out = _run(argv)
        rx = re.compile(pattern)
        names: List[str] = []
        for line in out.splitlines():
            m = rx.match(line)
            if m:
                names.append(m.group(1))
        return names

    # -------------------------------------------------------------- accessors

    def has_encoder(self, name: str) -> bool:
        if name not in self.encoders:
            return False
        if name.endswith(HARDWARE_SUFFIXES):
            return bool(self.hardware_probes.get(name, {}).get('ok'))
        return True

    def supports_pix_fmt(self, name: str, pix_fmt: str) -> bool:
        result = self.hardware_probes.get(name)
        if name == 'copy':
            return True
        if result is None:
            return not name.endswith(HARDWARE_SUFFIXES)
        if pix_fmt == 'p010le':
            return bool(result.get('ten_bit', {}).get('ok'))
        return bool(result['ok'] and (not pix_fmt or pix_fmt in self.pixel_formats.get(name, [])))

    def has_filter(self, name: str) -> bool:
        return name in self.filters

    def has_hwaccel(self, name: str) -> bool:
        return name in self.hwaccels

    def available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe and not self.error)

    def to_dict(self) -> Dict[str, Any]:
        """Compact payload for the /api/caps endpoint."""
        interesting_filters = [
            "scale_cuda", "scale_qsv", "zscale", "tonemap", "libplacebo",
            "bwdif", "yadif", "hqdn3d", "nlmeans", "cropdetect", "libvmaf",
            "subtitles", "ass",
        ]
        return {
            "available": self.available(),
            "error": self.error,
            "ffmpeg": self.ffmpeg,
            "ffprobe": self.ffprobe,
            "version": self.version,
            "encoders": {n: self.has_encoder(n) for n in DISPLAY_ENCODERS},
            "filters": {n: self.has_filter(n) for n in interesting_filters},
            "hwaccels": sorted(self.hwaccels),
            'hardware_probes': self.hardware_probes,
            'registered_encoders': [n for n in DISPLAY_ENCODERS if n in self.encoders],
            'hardware_pending': list(self.hardware_pending),
            'hardware_complete': not self.hardware_pending,
            'supplemental_error': self.supplemental_error,
        }


_CAPS: Optional[Capabilities] = None
_CAPS_LOCK = threading.RLock()
_CAPS_CONDITION = threading.Condition(_CAPS_LOCK)
_CAPS_LOADING = False
_CAPS_ERROR: Optional[Exception] = None
STARTUP_PROBE_RESERVE_MB = 512


def startup_probe_reserve_mb() -> int:
    """Conservative outstanding peak for the one serial startup probe."""
    with _CAPS_CONDITION:
        return STARTUP_PROBE_RESERVE_MB if _CAPS_LOADING else 0


def _probe_caps() -> None:
    global _CAPS, _CAPS_LOADING, _CAPS_ERROR
    def publish(candidate):
        global _CAPS
        snapshot = candidate.snapshot()
        with _CAPS_CONDITION:
            _CAPS = snapshot
            _CAPS_CONDITION.notify_all()
    try:
        candidate = Capabilities()
        candidate._on_primary = publish
        candidate.probe()
        publish(candidate)
    except Exception as exc:
        with _CAPS_CONDITION:
            _CAPS_ERROR = exc
            if _CAPS is not None:
                failed = _CAPS.snapshot()
                failed.supplemental_error = str(exc)
                for name in failed.hardware_pending:
                    failed.hardware_probes[name] = {'ok': False, 'error': '补充检测失败：' + str(exc)}
                failed.hardware_pending = []
                _CAPS = failed
    finally:
        with _CAPS_CONDITION:
            _CAPS_LOADING = False
            _CAPS_CONDITION.notify_all()


def get_caps(refresh: bool = False, *, wait_for_all: bool = False) -> Capabilities:
    """Return verified profile capabilities; optional full detection stays serial."""
    global _CAPS, _CAPS_LOADING, _CAPS_ERROR
    with _CAPS_CONDITION:
        if refresh:
            while _CAPS_LOADING:
                _CAPS_CONDITION.wait()
            _CAPS = None
        if _CAPS is None and not _CAPS_LOADING:
            _CAPS_ERROR = None
            _CAPS_LOADING = True
            try:
                # Let bounded subprocess.run finish/timeout and reap its child even
                # if the user closes the window during supplemental detection.
                threading.Thread(target=_probe_caps, name='vcs-hardware-probes', daemon=False).start()
            except Exception:
                _CAPS_LOADING = False
                _CAPS_CONDITION.notify_all()
                raise
        while _CAPS is None or ((wait_for_all or refresh) and _CAPS_LOADING):
            if not _CAPS_LOADING and _CAPS_ERROR is not None:
                raise _CAPS_ERROR
            _CAPS_CONDITION.wait()
        return _CAPS


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    # Run standalone diagnostics through the same package/shared slots as the app.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core.caps import get_caps as diagnostic_caps
    caps = diagnostic_caps(refresh=True)
    print(json.dumps(caps.to_dict(), ensure_ascii=False, indent=2))
