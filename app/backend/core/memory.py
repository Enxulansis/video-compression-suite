"""Memory safety: how much a software encode will commit, and how much is free.

Why this module exists
----------------------
A batch of 8 A-tier jobs (libsvtav1) froze a 31.6 GB machine: two 1080p instances ran
concurrently, each committing several GB up front, and the user's own applications already
held ~27 GB. The screen stopped responding and the only way out was a reboot. Measured
afterwards (see `_temp/research/2026-10-06_av1_memory_investigation.md`):

  * one 1280x720 10-bit preset-4 SVT-AV1 instance: **3,443 MB private** (430 MB WS 2,328);
  * one 854x480 preset-4: ~1,314 MB; preset 2 at 480p: 1,998 MB;
  * 8-bit instead of 10-bit: x0.75;
  * **two concurrent 1080p instances: 10,063 MB private** -- the number the user saw.

So the cost is a function of the ENCODER INPUT area and it multiplies by concurrency, and
what kills the machine is **commit (private bytes)**, not working set: SVT-AV1 commits its
buffer pool at startup (5-10 GB within ~1.3 s while touching only 2-4 GB). The page file
here was 31.6 GB allocated and 0.1 GB used, i.e. the cliff is physical RAM, not swap space.

Everything here is pure arithmetic over injected numbers, so it can be tested without
allocating anything -- deliberately, because a test that has to eat 10 GB to prove the
guard works is not a test anyone can run safely.
"""

from __future__ import annotations

import os
import math
import re
from typing import Any, Dict, Optional, Tuple

# --------------------------------------------------------------------------- policy
#: Free physical memory that must remain for the rest of the machine. The encoder must
#: never take the last few GB: Windows starts paging hard long before it runs out, and
#: "mouse barely moves" is what the user experiences at that point.
SAFETY_MARGIN_MB = 2500
#: Below this free-memory level a running software encode is PAUSED (not killed): the
#: machine is about to thrash, and a suspend is reversible.
LOW_WATER_MB = 1500
#: Extra free memory required before resuming, so a job cannot flap at the threshold.
RESUME_HYSTERESIS_MB = 1000
#: Below this, a paused job is cancelled after GRACE_SECONDS instead of holding memory
#: forever.
CRITICAL_MB = 700
GRACE_SECONDS = 120
#: Estimated footprint above this fraction of physical RAM earns an explicit warning (and
#: for 4K + uncapped profiles, a confirmation).
DANGER_FRACTION = 0.25
DECODE_BUFFER_FRAMES = 32
MIN_DECODE_RESERVE_MB = 64

# --------------------------------------------------------------------- cost model
#: Committed MB per megapixel of ENCODER INPUT (not source!) at the reference preset,
#: 10-bit. Measured on this machine with 6 s `testsrc2` clips; real footage is at least
#: this heavy. `basis` says whether a number was measured here or derived.
_SVT_MB_PER_MPX = 3400.0
_REFERENCE = {
    "libsvtav1": (_SVT_MB_PER_MPX, "本机实测（720p/10bit/preset4 = 3443 MB，480p/preset2 = 1998 MB）"),
    # Software encoders that were NOT measured. Deliberately conservative (higher than the
    # smell test suggests) because being wrong upwards only costs a wait, while being wrong
    # downwards costs a frozen machine.
    "libx265": (1400.0, "未实测：按 x265 的帧池量级保守取值（libsvtav1 的 ~0.4x）"),
    "libx264": (800.0, "未实测：x264 的帧池明显小于 SVT-AV1，保守取值"),
    "libaom-av1": (3000.0, "未实测：libaom 同样吃线程×帧缓冲，按 SVT 量级保守取值"),
    "librav1e": (2000.0, "未实测：保守取值"),
}
#: Hardware encoders keep their frame buffers in VRAM, so the host-side cost is a small
#: CONSTANT -- it does not grow with resolution the way a software encoder's pool does.
#: (Scaling these by megapixels was wrong: it charged a 4K NVENC job 746 MB of system RAM
#: for buffers that live on the card.)
_HARDWARE_MB = 200.0
_HARDWARE = {
    "h264_nvenc": "硬件编码（NVENC）：帧缓冲在显存，系统内存只有固定的小额主机侧开销",
    "hevc_nvenc": "硬件编码（NVENC）：帧缓冲在显存",
    "av1_nvenc": "硬件编码（NVENC）：帧缓冲在显存，AV1 会话略大",
    "h264_qsv": "硬件编码（QSV）：帧缓冲在显存/共享内存",
    "hevc_qsv": "硬件编码（QSV）：帧缓冲在显存/共享内存",
    "av1_qsv": "硬件编码（QSV）：帧缓冲在显存/共享内存",
    "copy": "流复制：不重编码，仅 I/O 缓冲",
}
#: Preset multipliers relative to the reference above. SVT-AV1's presets are numbers
#: (lower = slower = more buffers); x264/x265 use names.
_PRESET_FACTOR = {
    "libsvtav1": {0: 1.80, 1: 1.70, 2: 1.52, 3: 1.25, 4: 1.00, 5: 0.78, 6: 0.62,
                  7: 0.55, 8: 0.50, 9: 0.46, 10: 0.42, 11: 0.38, 12: 0.35, 13: 0.32,
                  -1: 1.00, -2: 1.00},
    "libx264": {"ultrafast": 0.35, "superfast": 0.45, "veryfast": 0.55, "faster": 0.65,
                "fast": 0.80, "medium": 1.00, "slow": 1.35, "slower": 1.70,
                "veryslow": 2.10},
    "libx265": {"ultrafast": 0.40, "superfast": 0.50, "veryfast": 0.60, "faster": 0.70,
                "fast": 0.85, "medium": 1.00, "slow": 1.30, "slower": 1.60,
                "veryslow": 2.00},
}
#: 10-bit frames are 2 bytes per sample: measured x1.33 for SVT-AV1 (3443 / 2580).
TEN_BIT_FACTOR = 1.0
EIGHT_BIT_FACTOR = 0.75


def estimate_instance_mb(encoder: str, width: int, height: int,
                         ten_bit: bool = False,
                         preset: Any = None, pixel_factor: float = 1.0) -> Tuple[int, str]:
    """Committed MB for ONE encode instance, plus the basis of the number.

    `width`/`height` describe encoder input. Original-size buffers are added separately.
    """
    name = (encoder or "").lower()
    if name in _HARDWARE:
        return int(_HARDWARE_MB), _HARDWARE[name]
    per_mpx, basis = _REFERENCE.get(name, (_SVT_MB_PER_MPX,
                                           "未知编码器：按最重的软件编码器保守取值"))
    megapixels = max(0.01, (max(1, int(width or 0)) * max(1, int(height or 0))) / 1_000_000.0)
    mb = per_mpx * megapixels

    factors = []
    table = _PRESET_FACTOR.get(name)
    if table:
        key = preset
        try:
            key = int(preset) if name == "libsvtav1" else str(preset).lower()
        except (TypeError, ValueError):
            key = None
        factor = table.get(key, 1.0)
        if factor != 1.0:
            factors.append(("preset {}".format(preset), factor))
        mb *= factor
    depth = TEN_BIT_FACTOR if ten_bit else EIGHT_BIT_FACTOR
    if depth != 1.0:
        factors.append(("8bit", depth))
    if pixel_factor != 1.0:
        factors.append(('色度/像素存储', pixel_factor))
    mb *= depth * pixel_factor

    detail = "{} MB/百万像素".format(per_mpx)
    if factors:
        detail += " × " + " × ".join("{}={:g}".format(k, v) for k, v in factors)
    return int(round(mb)), "{}；{}".format(detail, basis)


def last_option(argv, flags, default=""):
    value = default
    for index, token in enumerate(argv[:-1]):
        base, _, scope = token.partition(':')
        matches = token in flags or (scope in ('v', 'v:0', '0') and
                  (base in flags or base + ':v' in flags))
        if matches:
            value = argv[index + 1]
    return value


def pixel_layout(pix_fmt: str) -> tuple[bool, float]:
    """Storage depth/chroma relative to 4:2:0. Unknown formats stay conservative."""
    name = (pix_fmt or "").lower()
    if not name:
        return True, 8 / 3  # unknown storage: reserve 16-bit RGBA-sized frames
    high = bool(re.search(r"(?:9|10|12|14|16)(?:le|be)$", name)) or name.startswith(("p010", "p012", "p016"))
    high = high or name.startswith(('rgb48', 'bgr48', 'rgba64', 'bgra64', 'gbrpf32', 'gbrapf32'))
    if name.startswith(('p210','p212','p216','nv20','y210','y212','y216')):
        return True, 4 / 3
    if name.startswith(('p410','p412','p416')):
        return True, 2.0
    if name in ('nv16','nv61'):
        return False, 4 / 3
    if name in ('nv24','nv42'):
        return False, 2.0
    if name.startswith(('rgbf32','rgb96')):
        return True, 4.0
    if name.startswith(('rgbaf32','rgba128','gbrap32')):
        return True, 16 / 3
    chroma = 2.0 if "444" in name or name.startswith(("gbr", "rgb", "bgr")) else (4 / 3 if "422" in name else 1.0)
    if name.startswith(('rgba', 'bgra', 'argb', 'abgr', 'rgb0', 'bgr0', '0rgb', '0bgr', 'gbrap')):
        chroma = 8 / 3
    elif name.startswith('yuva'):
        chroma += 2 / 3
    if 'f32' in name:
        chroma *= 2  # float storage uses four bytes instead of two
    return high, chroma


def decoder_reserve_mb(width: int, height: int, pix_fmt: str = "") -> int:
    """Unmeasured reserve for 32 decoded/filter frames, minimum 64 MB."""
    if not width or not height:
        return 0
    high, chroma = pixel_layout(pix_fmt)
    return max(MIN_DECODE_RESERVE_MB,
               math.ceil(width * height * 1.5 * (2 if high else 1) * chroma * DECODE_BUFFER_FRAMES / 1048576))


def encoder_geometry(argv, geometry=None):
    width, height = geometry or (0, 0)
    chain = last_option(argv, ("-vf", "-filter:v"))
    for part in chain.split(','):
        if re.match(r'\s*(?:crop|scale)=', part):
            match = re.match(r'\s*(?:crop|scale)=(\d+):(\d+)(?=[: ,]|$)', part)
            if not match:
                return 0, 0
            width, height = int(match[1]), int(match[2])
    size = last_option(argv, ('-s', '-s:v'))
    if size:
        match = re.fullmatch(r'(\d+)x(\d+)', size)
        if not match:
            return 0, 0
        width, height = int(match[1]), int(match[2])
    return int(width), int(height)


def filter_reserve_mb(argv, source_size, source_pix_fmt="") -> Tuple[int, str]:
    """Conservative intermediate/final frame pools, not measured filter peaks.

    Track geometry at each format/filter stage: HDR float conversion can happen
    BEFORE a later downscale. Reserve the largest intermediate pool and a distinct
    final pool, rather than multiplying every graph node by 32 frames.
    """
    width, height = source_size or (0, 0)
    if min(width,height) <= 0:
        return 0, ''
    output_fmt = last_option(argv,('-pix_fmt',),source_pix_fmt)
    current_fmt = source_pix_fmt or output_fmt
    peak = 0
    changed = False
    chain = last_option(argv,('-vf','-filter:v'))
    for part in chain.split(','):
        name,_,value = part.strip().partition('=')
        if name in ('scale','crop'):
            match = re.match(r'(\d+):(\d+)(?=[: ,]|$)',value)
            if match:
                width,height = map(int,match.groups())
        if name == 'format':
            current_fmt = value.removeprefix('pix_fmts=')
        if name in ('scale','format','zscale','tonemap','hqdn3d','bwdif','yadif','subtitles','ass','fps'):
            changed = True
            peak = max(peak,decoder_reserve_mb(width,height,current_fmt))
    final_size = encoder_geometry(argv,source_size)
    if min(final_size) <= 0:
        return 0, ''  # estimate_plan_mb rejects unresolved geometry itself
    resized = final_size != (width,height)
    converted = output_fmt and output_fmt != current_fmt
    if not changed and not resized and not converted:
        return 0, ''
    final = decoder_reserve_mb(*final_size,output_fmt or current_fmt)
    # A scale node's negotiated output format may differ from its input.
    amount = max(peak,final) if peak <= final else peak + final
    return amount, '滤镜中间／输出帧池 {} MB（{} 帧，未实测保守模型）'.format(amount,DECODE_BUFFER_FRAMES)


def estimate_plan_mb(plan: Any, ten_bit: Optional[bool] = None,
                     preset: Any = None) -> Tuple[int, str]:
    """Final argv encoder pool plus an explicit original-size decoder reserve."""
    argv = list(getattr(plan, "argv", []) or [])
    encoder = last_option(argv, ("-c:v", "-codec:v", "-vcodec", "-c", "-codec"), getattr(plan, "encoder", ""))
    pix_fmt = last_option(argv, ("-pix_fmt",), getattr(plan, "source_pix_fmt", ""))
    width, height = encoder_geometry(argv, getattr(plan, "out_size", None))
    if width <= 0 or height <= 0:
        return 0, '无法确定最终滤镜尺寸，请使用明确的 crop/scale/输出尺寸'
    high, chroma = pixel_layout(pix_fmt)
    depth = ten_bit if ten_bit is not None else high
    applied_preset = last_option(argv, ("-preset",), preset)
    encoded, basis = estimate_instance_mb(encoder, width, height, bool(depth), applied_preset, chroma)
    source = getattr(plan, "source_size", None)
    decoded = (decoder_reserve_mb(*source, getattr(plan, "source_pix_fmt", ""))
               if source and encoder != "copy" else 0)
    if decoded:
        basis += "；原尺寸解码/滤镜预留 {} MB（32 帧，未实测保守模型）".format(decoded)
    filtered, filter_basis = filter_reserve_mb(argv,source or getattr(plan,'out_size',None),
                                              getattr(plan,'source_pix_fmt','')) if encoder != 'copy' else (0,'')
    if filtered:
        basis += '；' + filter_basis
    return encoded + decoded + filtered, basis


# ------------------------------------------------------------------ machine memory

class _MemoryStatusEx(object):  # pragma: no cover - thin ctypes shim
    pass


def machine_memory() -> Dict[str, int]:
    """Physical/commit memory in MB. Returns zeros when it cannot be read.

    Uses `GlobalMemoryStatusEx` through ctypes: the project ships zero third-party
    dependencies on purpose, and this call is a single cheap syscall (no process spawn).
    """
    empty = {"total_mb": 0, "free_mb": 0, "commit_used_mb": 0, "commit_limit_mb": 0,
             "percent_used": 0, "available": 0}
    if os.name != "nt":
        return empty
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return empty
        to_mb = 1024.0 * 1024.0
        return {
            "total_mb": int(status.ullTotalPhys / to_mb),
            "free_mb": int(status.ullAvailPhys / to_mb),
            "commit_used_mb": int((status.ullTotalPageFile - status.ullAvailPageFile) / to_mb),
            "commit_limit_mb": int(status.ullTotalPageFile / to_mb),
            "percent_used": int(status.dwMemoryLoad),
            "available": 1,
        }
    except Exception:  # noqa: BLE001 - never let a memory probe break a job
        return empty


# ------------------------------------------------------------------------ decisions

def process_memory_mb(pid: int) -> Optional[Dict[str, int]]:
    """Current resident and private committed MB, or None when inaccessible."""
    if os.name != "nt" or not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class COUNTERS(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak_ws", "ws", "peak_paged", "paged", "peak_nonpaged",
                    "nonpaged", "pagefile", "peak_pagefile", "private")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        handle = kernel.OpenProcess(0x410, False, pid)
        if not handle:
            return None
        try:
            info = COUNTERS()
            info.cb = ctypes.sizeof(info)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(info), info.cb):
                return None
            return {"resident_mb": int(info.ws / 1048576),
                    "private_mb": int(info.private / 1048576)}
        finally:
            kernel.CloseHandle(handle)
    except (OSError, ValueError):
        return None


def validate_policy(guard: bool, margin_mb: int, low_water_mb: int,
                    memory: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Validate a complete policy before changing any live or persisted state."""
    if not isinstance(guard, bool):
        raise ValueError("memory_guard 必须是布尔值")
    for name, value in (("memory_margin_mb", margin_mb), ("memory_low_water_mb", low_water_mb)):
        if type(value) is not int or not 256 <= value <= 16384:
            raise ValueError("{} 必须是 256–16384 之间的整数 MB".format(name))
    if low_water_mb <= CRITICAL_MB:
        raise ValueError("memory_low_water_mb 必须高于危急水位 {} MB".format(CRITICAL_MB))
    resume = low_water_mb + RESUME_HYSTERESIS_MB
    if margin_mb < resume:
        raise ValueError("memory_margin_mb 必须不低于恢复水位 {} MB（低水位 + {}）".format(
            resume, RESUME_HYSTERESIS_MB))
    mem = memory if memory is not None else machine_memory()
    if mem.get("available") and mem.get("total_mb") and resume >= mem["total_mb"]:
        raise ValueError("恢复水位 {} MB 必须低于本机物理内存 {} MB".format(resume, mem["total_mb"]))
    return {"guard": guard, "margin_mb": margin_mb, "low_water_mb": low_water_mb}


def admission(memory: Dict[str, int], reserved_mb: int, need_mb: int,
              margin_mb: int = SAFETY_MARGIN_MB, commit_reserved_mb: int = 0) -> Dict[str, Any]:
    """May one more job start? Pure arithmetic so it is testable without a low-RAM box.

    Returns `{ok, reason, free_mb, needed_mb, headroom_mb}`. `ok=False` means "wait", not
    "refuse": the confirmed product decision is to queue the job and start it when the
    memory is there (never to silently downgrade the user's chosen quality).
    """
    free = int(memory.get("free_mb") or 0)
    if not memory.get("available"):
        return {"ok": False, "reason": "等待内存：无法读取系统内存，请稍后重试", "free_mb": 0, "needed_mb": int(need_mb),
                "headroom_mb": 0, "unknown": True}
    # Current free memory already accounts for resident processes. Only reserve the
    # positive gap between an active encoder's expected peak and its current usage.
    needed = int(need_mb) + int(reserved_mb)
    headroom = free - int(margin_mb)
    commit_limit = int(memory.get("commit_limit_mb") or 0)
    commit_free = commit_limit - int(memory.get("commit_used_mb") or 0)
    commit_needed = int(need_mb) + int(commit_reserved_mb) + int(margin_mb)
    if needed <= headroom and (not commit_limit or commit_needed <= commit_free):
        return {"ok": True, "reason": "", "free_mb": free, "needed_mb": needed,
                "headroom_mb": headroom}
    reason = ("等待内存：本任务预计需要 {:.1f} GB，已运行的软件编码还需预留 {:.1f} GB，"
              "当前可用 {:.1f} GB，安全余量 {:.1f} GB").format(
        need_mb / 1024.0, reserved_mb / 1024.0, free / 1024.0, margin_mb / 1024.0)
    if commit_limit and commit_needed > commit_free:
        reason += "；提交额度不足：剩余 {:.1f} GB，需要 {:.1f} GB（含余量）".format(
            commit_free / 1024.0, commit_needed / 1024.0)
    return {"ok": False, "reason": reason, "free_mb": free, "needed_mb": needed,
            "headroom_mb": headroom}


def safe_concurrency(memory: Dict[str, int], per_job_mb: int,
                     configured: int, margin_mb: int = SAFETY_MARGIN_MB) -> int:
    """How many jobs of this size fit right now (never more than `configured`)."""
    if not memory.get("available") or per_job_mb <= 0:
        return max(1, int(configured))
    headroom = int(memory.get("free_mb") or 0) - int(margin_mb)
    if headroom <= 0:
        return 0
    return max(0, min(int(configured), int(headroom // per_job_mb)))


def is_dangerous(need_mb: int, memory: Dict[str, int]) -> bool:
    """Would this single task claim a quarter of physical RAM (or more)?"""
    total = int(memory.get("total_mb") or 0)
    if not total:
        return False
    return need_mb >= total * DANGER_FRACTION


def danger_note(need_mb: int, memory: Dict[str, int]) -> str:
    """One sentence for the plan/UI explaining an oversized task."""
    total = int(memory.get("total_mb") or 0)
    if not total:
        return ""
    return ("预计峰值内存 {:.1f} GB，相当于本机物理内存（{:.1f} GB）的 {:.0f}%："
            "运行时可能把系统拖入换页（鼠标卡顿）。").format(
        need_mb / 1024.0, total / 1024.0, need_mb * 100.0 / total)
