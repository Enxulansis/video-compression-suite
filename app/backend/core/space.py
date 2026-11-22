"""Disk-space safety: never let a long batch fill the disk.

Why this exists
---------------
A 4K batch can write tens of gigabytes. Filling the volume does not merely fail the
job -- it leaves a truncated file AND can take the whole machine down with it.
ffmpeg's own ENOSPC handling is too late to be useful, and the error it surfaces
("No space left on device") says nothing about how much was needed.

Three layers, cheapest first:

1. **Before queueing (advisory)** -- `evaluate()` reports headroom so the UI can show
   it and warn, the way Shutter Encoder turns red under 10% free.
2. **Before starting a job (hard floor)** -- refuse to start with less than
   `MIN_FREE_BYTES` free: there is no point beginning a multi-gigabyte write there.
3. **While encoding (the real protection)** -- once enough progress exists to project
   the final size, abort *before* the disk is full and say how much was needed.

None of this estimates the output size from the source alone: profile "ratios" are
too coarse to risk a batch on. The projection uses what is already measured -- bytes
written so far against progress -- which needs no model at all.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

#: Refuse to start a job below this much free space. Big enough to hold a short
#: encode plus container overhead; small enough not to block a legitimately tight
#: disk when the user is compressing something small.
MIN_FREE_BYTES = 256 * 1024 * 1024

#: Headroom kept above the projected remainder so the write does not race the floor.
RESERVE_BYTES = 128 * 1024 * 1024

#: Below this fraction of the volume free, the UI should look alarmed.
LOW_FREE_FRACTION = 0.10

#: Don't trust a size projection before this much progress has been made.
MIN_PROGRESS_FOR_PROJECTION = 10.0


def _existing_ancestor(path: str) -> Optional[Path]:
    """Nearest existing directory at or above `path`.

    `shutil.disk_usage` needs a path that exists; the output directory may not have
    been created yet, so walk up rather than reporting "unknown".
    """
    try:
        candidate = Path(path)
    except (TypeError, ValueError):
        return None
    for _ in range(64):
        try:
            if candidate.is_dir():
                return candidate
            if candidate.parent == candidate:
                return None
            candidate = candidate.parent
        except OSError:
            return None
    return None


def free_bytes(path: str) -> int:
    """Free bytes on the volume holding `path`; 0 when it cannot be determined."""
    target = _existing_ancestor(path)
    if target is None:
        return 0
    try:
        return int(shutil.disk_usage(str(target)).free)
    except OSError:
        return 0


def format_bytes(num: int) -> str:
    value = float(max(0, int(num)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return "{:.1f} {}".format(value, unit)
        value /= 1024
    return "{:.1f} TB".format(value)


def evaluate(path: str, needed_bytes: int = 0) -> Dict[str, Any]:
    """Advisory check for the UI: how much room is there, and is it enough?

    `needed_bytes` is only ever the *source* size or another deliberately coarse
    upper bound -- never a guess at the output size. It is used to phrase a warning,
    not to block.
    """
    target = _existing_ancestor(path)
    if target is None:
        return {"ok": False, "known": False, "path": str(path),
                "error": "路径不存在，无法读取磁盘信息", "message": "无法读取磁盘信息"}
    try:
        usage = shutil.disk_usage(str(target))
    except OSError as exc:
        return {"ok": False, "known": False, "path": str(target),
                "error": str(exc), "message": "无法读取磁盘信息"}

    total = int(usage.total) or 1
    free = int(usage.free)
    fraction = free / total
    needed = max(0, int(needed_bytes or 0))

    if free < MIN_FREE_BYTES:
        level = "error"
        message = "磁盘仅剩 {}，不足以开始编码（至少需要 {}）".format(
            format_bytes(free), format_bytes(MIN_FREE_BYTES))
    elif fraction < LOW_FREE_FRACTION:
        level = "warn"
        message = "磁盘仅剩 {}（{:.0f}%），建议先清理".format(format_bytes(free), fraction * 100)
    elif needed and free < needed:
        level = "warn"
        message = "可用 {} 小于源文件 {}，压完可能放不下".format(
            format_bytes(free), format_bytes(needed))
    else:
        level = "ok"
        message = "可用 {}（{:.0f}%）".format(format_bytes(free), fraction * 100)

    return {
        "ok": level != "error",
        "known": True,
        "level": level,
        "path": str(target),
        "free_bytes": free,
        "total_bytes": int(usage.total),
        "free_fraction": round(fraction, 4),
        "min_free_bytes": MIN_FREE_BYTES,
        "needed_bytes": needed,
        "message": message,
    }


def preflight(out_dir: str, src_path: str = "", src_size: int = 0) -> Dict[str, Any]:
    """Everything worth knowing before writing into `out_dir` (drives G4 warnings)."""
    problems: list = []
    warnings: list = []

    target = Path(out_dir) if out_dir else None
    writable = False
    if target is None:
        problems.append("输出目录未确定")
    else:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append("无法创建输出目录 {}：{}".format(target, exc))
        else:
            if not os.access(str(target), os.W_OK):
                problems.append("输出目录不可写：{}".format(target))
            else:
                writable = True

    space = evaluate(out_dir or "", src_size)
    if space.get("known"):
        if space.get("level") == "error":
            problems.append(space["message"])
        elif space.get("level") == "warn":
            warnings.append(space["message"])
    else:
        warnings.append(space.get("message") or "无法读取磁盘信息")

    if src_path and not os.path.isfile(src_path):
        problems.append("源文件不存在：{}".format(src_path))

    return {
        "ok": not problems,
        "out_dir": str(target) if target else "",
        "writable": writable,
        "space": space,
        "problems": problems,
        "warnings": warnings,
    }


def project_shortfall(free: int, written: int, progress: float,
                      reserve: int = RESERVE_BYTES) -> Optional[Dict[str, Any]]:
    """Will the remaining write fit? None when it is too early to say.

    Deliberately model-free: extrapolate the bytes already written to the whole file
    using ffmpeg's own progress percentage. Returns a dict describing the shortfall
    (for a diagnosable error message) or None when there is still room.
    """
    if progress < MIN_PROGRESS_FOR_PROJECTION or written <= 0:
        return None
    projected_total = int(written / (progress / 100.0))
    remaining = max(0, projected_total - written)
    if free >= remaining + reserve:
        return None
    return {
        "projected_total": projected_total,
        "written": written,
        "remaining": remaining,
        "free": free,
        "reserve": reserve,
        "shortfall": remaining + reserve - free,
        "message": (
            "磁盘空间不足：已写入 {}（约 {:.0f}%），按此推算还需 {}，"
            "但可用空间只有 {}（另留 {} 余量）。已停止以免写满磁盘。".format(
                format_bytes(written), progress,
                format_bytes(remaining), format_bytes(free), format_bytes(reserve))
        ),
    }
