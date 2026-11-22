"""Autocrop: detect and remove baked-in black bars.

Why this matters for a compression tool
---------------------------------------
A 2.39:1 film stored in a 16:9 frame is ~25% black pixels, and the encoder still has
to code them -- they are cheap but not free, and they also inflate the "pixels" term
in every bitrate/bpp calculation we show the user. Removing the bars is one of the few
size wins that costs nothing perceptually.

How it works
------------
`cropdetect` cannot crop in the same pass that encodes (it reports, it does not act),
so this is a two-step: run it over a few short windows, take the consensus rectangle,
*validate it hard*, and hand a plain `crop=w:h:x:y` string to the planner as a filter.
The planner stays pure -- it never runs ffmpeg itself.

The validation is the point. A false positive here silently throws away picture, so
detection is only accepted when:
  * the rectangle is even-sized (yuv420p requires it) and at least 320x180;
  * the bars are substantial (>= 2% of a dimension) -- otherwise the "crop" is noise;
  * the crop is not greedy (<= 30% of width, <= 30% of height removed);
  * the samples agree (the same rectangle, within tolerance, in most windows).

Anything else returns "no crop" with the reason, because doing nothing is always
safer than cropping a real edge off someone's video.
"""

from __future__ import annotations

import os
import re
import subprocess
from . import work
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .caps import get_caps, subprocess_kwargs
from .probe import MediaInfo

# `cropdetect` prints e.g. "x1:y1:w1:h1" followed by "crop=1920:800:0:140".
_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")

#: Sample windows (fractions of duration) -- bars are constant, but a window can land
#: on a fade or a full-frame scene, so more than one opinion is needed.
SAMPLE_FRACTIONS = (0.15, 0.4, 0.65, 0.9)
SAMPLE_SECONDS = 6

MIN_KEEP_WIDTH = 320
MIN_KEEP_HEIGHT = 180
MIN_BAR_FRACTION = 0.02      # a "bar" thinner than 2% is noise, not letterboxing
MAX_CROP_FRACTION = 0.30     # never remove more than 30% of either dimension
AGREEMENT_TOLERANCE = 8      # pixels; detection jitters by a few even at round=2


@dataclass
class CropResult:
    ok: bool = False
    crop: str = ""                     # "w:h:x:y" -- ready for the crop filter
    width: int = 0
    height: int = 0
    x: int = 0
    y: int = 0
    src_width: int = 0
    src_height: int = 0
    samples: List[Dict[str, Any]] = field(default_factory=list)
    agreement: float = 0.0
    saved_fraction: float = 0.0        # share of pixels removed
    message: str = ""
    reason: str = ""                   # why it was rejected, when ok is False

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def parse_cropdetect(text: str) -> Optional[Tuple[int, int, int, int]]:
    """Last `crop=w:h:x:y` in cropdetect's log, as a tuple. None when absent.

    The LAST one is used because cropdetect converges as it sees more frames; the
    first values are from its startup guess.
    """
    matches = _CROP_RE.findall(str(text or ""))
    if not matches:
        return None
    w, h, x, y = matches[-1]
    return int(w), int(h), int(x), int(y)


def _even(value: int) -> int:
    return int(value) - (int(value) % 2)


def validate(w: int, h: int, x: int, y: int,
             src_w: int, src_h: int) -> Tuple[bool, str, Tuple[int, int, int, int]]:
    """Clamp to even values and reject anything risky.

    Returns (accepted, reason, (w, h, x, y)). The returned rectangle is always even
    and inside the frame, even when accepted is False, so callers can log what was
    proposed.
    """
    if src_w <= 0 or src_h <= 0:
        return False, "无法得知源分辨率", (0, 0, 0, 0)

    w, h = _even(min(w, src_w)), _even(min(h, src_h))
    x, y = _even(max(0, min(x, src_w - w))), _even(max(0, min(y, src_h - h)))

    if w < MIN_KEEP_WIDTH or h < MIN_KEEP_HEIGHT:
        return False, "裁剪后只剩 {}x{}，太小".format(w, h), (w, h, x, y)

    bar_x = (src_w - w) / float(src_w)
    bar_y = (src_h - h) / float(src_h)
    if bar_x < MIN_BAR_FRACTION and bar_y < MIN_BAR_FRACTION:
        return False, "没有检出明显的黑边（不足 {:.0%}）".format(MIN_BAR_FRACTION), (w, h, x, y)
    if bar_x > MAX_CROP_FRACTION or bar_y > MAX_CROP_FRACTION:
        return False, ("要裁掉 {:.0%}×{:.0%} 的画面，超过 "
                       "{:.0%} 安全上限，已放弃自动裁边").format(
                           bar_x, bar_y, MAX_CROP_FRACTION), (w, h, x, y)

    # A centered crop is what letterboxing/pillarboxing actually looks like; a wildly
    # off-centre rectangle usually means the detector locked onto a dark object.
    # Compare the gap on BOTH sides -- checking only `x > 0` missed the case of a
    # rectangle pinned to the left edge with all the black on the right, which is not
    # letterboxing at all (pinned by a test).
    gap_x = src_w - w
    if gap_x > 0 and abs(gap_x - 2 * x) > max(16, 0.1 * gap_x):
        return False, "检出的裁剪框不居中，可能是把暗色物体当成黑边了", (w, h, x, y)
    gap_y = src_h - h
    if gap_y > 0 and abs(gap_y - 2 * y) > max(16, 0.1 * gap_y):
        return False, "检出的裁剪框不居中，可能是把暗色物体当成黑边了", (w, h, x, y)

    return True, "", (w, h, x, y)


def consensus(boxes: List[Tuple[int, int, int, int]]) -> Tuple[Optional[Tuple[int, int, int, int]],
                                                               float]:
    """Most-agreed rectangle (median of each field) plus the share that agrees.

    Median rather than mode: cropdetect jitters by a pixel or two, so an exact-match
    vote would report far less agreement than there really is.
    """
    usable = [b for b in boxes if b[0] > 0 and b[1] > 0]
    if not usable:
        return None, 0.0
    fields = []
    for index in range(4):
        values = sorted(b[index] for b in usable)
        fields.append(values[len(values) // 2])
    median = tuple(fields)
    agree = sum(1 for b in usable
                if all(abs(b[i] - median[i]) <= AGREEMENT_TOLERANCE for i in range(4)))
    return median, agree / float(len(usable))


def detect(path: str, info: Optional[MediaInfo] = None,
           seconds: int = SAMPLE_SECONDS) -> CropResult:
    """Run cropdetect over several windows and decide whether to crop.

    Never raises: a detection failure means "no crop", which is always safe.
    """
    caps = get_caps()
    result = CropResult()
    if not caps.available():
        result.reason = "FFmpeg 不可用"
        return result

    probe_info = info
    if probe_info is None or not probe_info.ok:
        from .probe import probe as _probe
        probe_info = _probe(path)
    if not probe_info.ok or not probe_info.video:
        result.reason = probe_info.error or "无法探测该文件"
        return result

    work.note_media(probe_info)
    video = probe_info.video
    # Display size: cropdetect runs on the AUTOROTATED frames, so its box is in
    # display coordinates. Validating against the stored size rejected valid crops
    # (and could accept invalid ones) on every rotated phone video.
    result.src_width, result.src_height = video.display_width, video.display_height
    duration = float(probe_info.duration or 0)
    if video.display_width <= 0 or video.display_height <= 0:
        result.reason = "无法得知分辨率"
        return result

    boxes: List[Tuple[int, int, int, int]] = []
    for fraction in SAMPLE_FRACTIONS:
        start = max(0.0, duration * fraction - seconds / 2.0)
        if duration and start + seconds > duration:
            start = max(0.0, duration - seconds)
        argv = [
            caps.ffmpeg, "-hide_banner", "-nostdin", "-v", "info",
            "-ss", "{:.2f}".format(start), "-t", str(int(seconds)),
            "-i", path,
            "-map", "0:v:0", "-vf", "cropdetect=limit=24:round=2:reset=0",
            "-f", "null", "-",
        ]
        try:
            # subprocess_kwargs() already carries stdout/stderr/encoding/errors --
            # passing them again is a TypeError, not a merge.
            proc = work.run(argv, text=True, timeout=600, **subprocess_kwargs())
        except (OSError, subprocess.SubprocessError) as exc:
            result.samples.append({"at": round(start, 2), "error": str(exc)})
            continue
        found = parse_cropdetect((proc.stderr or "") + (proc.stdout or ""))
        if found:
            boxes.append(found)
            result.samples.append({"at": round(start, 2), "crop": list(found)})
        else:
            result.samples.append({"at": round(start, 2), "error": "未检出"})

    median, agreement = consensus(boxes)
    result.agreement = round(agreement, 3)
    if median is None:
        result.reason = "未检出黑边"
        result.message = "这段视频没有明显的黑边，不需要裁剪。"
        return result

    accepted, reason, box = validate(median[0], median[1], median[2], median[3],
                                     video.display_width, video.display_height)
    result.width, result.height, result.x, result.y = box
    result.crop = "{}:{}:{}:{}".format(*box)
    result.saved_fraction = round(
        1.0 - (box[0] * box[1]) / float(video.display_width * video.display_height), 4)

    if agreement < 0.5:
        result.ok = False
        result.reason = "各采样点结果不一致（仅 {:.0%} 一致），可能不是固定黑边".format(agreement)
        result.message = result.reason + "，已放弃自动裁边。"
        return result
    if not accepted:
        result.ok = False
        result.reason = reason
        result.message = reason
        return result

    result.ok = True
    result.message = "检出黑边：{}x{} → {}x{}（去掉 {:.1%} 的像素），4 个采样点 {:.0%} 一致".format(
        video.display_width, video.display_height, box[0], box[1],
        result.saved_fraction, agreement)
    return result


_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.RLock()


def detect_cached(path: str, info: Optional[MediaInfo] = None) -> CropResult:
    """`detect` with a cache keyed on the file's mtime and size.

    Detection costs a few seconds of decoding, and the UI asks for it again on every
    plan refresh -- so cache it, but never across a file change.
    """
    try:
        stat = os.stat(path)
        key = "{}:{}:{}".format(path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return detect(path, info)

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return CropResult(**cached)

    result = detect(path, info)
    with _CACHE_LOCK:
        # Keep the map small: this only exists to survive a UI refresh.
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = result.to_dict()
    return result


def apply_crop_filter(chain: List[str], crop: str) -> List[str]:
    """Insert the crop FIRST, ahead of every other filter.

    Cropping before scaling is what makes the result correct: scaling first would
    resample the bars into the picture and change the aspect ratio.
    """
    text = str(crop or "").strip()
    if not text:
        return chain
    parts = text.split(":")
    if len(parts) != 4 or not all(p.strip().isdigit() for p in parts):
        return chain
    return ["crop={}".format(text)] + list(chain)
