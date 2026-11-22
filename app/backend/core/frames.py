"""Single-frame extraction for 1:1 comparison (V2).

The point of this module is the **order of operations**, because it is the step that
makes or breaks a visual comparison:

    scale to the COMMON resolution  ->  then crop the same region

Doing it the other way round (crop first) means the two crops are taken from
different-sized pictures, so the panels show different content and the comparison is
meaningless. Scaling both sides to the output's resolution first is also the honest
basis: it is exactly what the VMAF number measures (see quality.basis_text), and it
keeps the "same-resolution" caveat visible instead of hiding the resolution loss
inside the comparison.

Frames are returned as JPEG bytes straight from ffmpeg's stdout -- no temp files, and
nothing to clean up if the request is abandoned.
"""

from __future__ import annotations

import subprocess
from . import work
from typing import Any, Dict, List, Optional, Tuple

from .caps import get_caps, subprocess_kwargs


class FrameError(RuntimeError):
    """Raised when a frame cannot be extracted, with a message fit for the UI."""


def _binary_kwargs() -> Dict[str, Any]:
    """subprocess kwargs for BINARY stdout.

    `subprocess_kwargs()` sets encoding/errors (text mode), which would corrupt a
    JPEG; drop them but keep the Windows console-suppression flags.
    """
    kwargs = dict(subprocess_kwargs())
    kwargs.pop("encoding", None)
    kwargs.pop("errors", None)
    return kwargs


def parse_crop(text: str) -> Optional[Tuple[int, int, int, int]]:
    """`"w:h:x:y"` -> tuple, or None when absent/invalid."""
    parts = str(text or "").strip().split(":")
    if len(parts) != 4 or not all(p.strip().lstrip("-").isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def centered_crop(width: int, height: int, zoom: float) -> Optional[Tuple[int, int, int, int]]:
    """A centred crop of `width/zoom` x `height/zoom`, even-sized.

    zoom <= 1 means "no crop" (the whole frame), which is what the 1x button asks for.
    """
    if zoom <= 1.0 or width <= 0 or height <= 0:
        return None
    cw = int(width / zoom) // 2 * 2
    ch = int(height / zoom) // 2 * 2
    if cw < 64 or ch < 64:
        return None
    return (cw, ch, ((width - cw) // 2) // 2 * 2, ((height - ch) // 2) // 2 * 2)


def build_filters(scale_to: Optional[Tuple[int, int]] = None,
                  crop: Optional[Tuple[int, int, int, int]] = None) -> str:
    """`scale` first, `crop` second -- see the module docstring."""
    chain: List[str] = []
    if scale_to and scale_to[0] > 0 and scale_to[1] > 0:
        chain.append("scale={}:{}:flags=lanczos".format(scale_to[0], scale_to[1]))
    if crop and len(crop) == 4:
        chain.append("crop={}:{}:{}:{}".format(*crop))
    return ",".join(chain)


def extract(path: str, time_seconds: float, *,
            scale_to: Optional[Tuple[int, int]] = None,
            crop: Optional[Tuple[int, int, int, int]] = None,
            quality: int = 3, timeout: int = 300) -> bytes:
    """One frame at `time_seconds` as JPEG bytes.

    `quality` is ffmpeg's mjpeg `-q:v` (2 = best, 31 = worst). 3 keeps artefacts of
    the JPEG itself well below anything we are trying to show.
    """
    caps = get_caps()
    if not caps.available():
        raise FrameError("FFmpeg 不可用")
    work.ensure_media(path)
    try:
        at = max(0.0, float(time_seconds))
    except (TypeError, ValueError):
        raise FrameError("时间点无效")

    argv = [caps.ffmpeg, "-hide_banner", "-nostdin", "-v", "error"]
    # Accurate seek: -ss before -i is fast but lands on the keyframe before the
    # target, and for THIS feature the exact frame is the whole point.
    argv += ["-i", path, "-ss", "{:.3f}".format(at), "-frames:v", "1"]
    filters = build_filters(scale_to, crop)
    if filters:
        argv += ["-vf", filters]
    argv += ["-f", "image2", "-c:v", "mjpeg", "-q:v", str(int(quality)), "pipe:1"]

    try:
        proc = work.run(argv, timeout=timeout, **_binary_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        raise FrameError("取帧失败：{}".format(exc))
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-200:]
        raise FrameError(detail or "取帧失败（该时间点可能超出时长）")
    return proc.stdout


def _jpeg_size(data: bytes) -> Tuple[int, int]:
    """Width/height from a JPEG's SOF marker (so we do not need an image library)."""
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = (data[index + 5] << 8) | data[index + 6]
            width = (data[index + 7] << 8) | data[index + 8]
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment = (data[index + 2] << 8) | data[index + 3]
        index += 2 + max(2, segment)
    return 0, 0


def comparison(out_path: str, src_path: str, time_seconds: float,
               out_width: int, out_height: int, zoom: float = 2.0,
               region: str = "") -> Dict[str, Any]:
    """Both sides of the comparison at the same time point, same crop, 1:1.

    Returns JPEG bytes for each side plus the crop actually used, so the UI can label
    it ("2x 中心区域 640x360") instead of showing two anonymous pictures.
    """
    crop = parse_crop(region) or centered_crop(out_width, out_height, zoom)
    scale_to = (out_width, out_height) if out_width > 0 and out_height > 0 else None
    # The output is already at the common resolution; scaling it is a no-op but keeps
    # both sides on exactly the same code path (and guards odd source sizes).
    out_bytes = extract(out_path, time_seconds, scale_to=scale_to, crop=crop)
    src_bytes = extract(src_path, time_seconds, scale_to=scale_to, crop=crop)
    width, height = _jpeg_size(out_bytes)
    return {
        "time": round(max(0.0, float(time_seconds)), 3),
        "crop": "{}:{}:{}:{}".format(*crop) if crop else "",
        "zoom": zoom if crop else 1.0,
        "common_size": "{}x{}".format(out_width, out_height),
        "frame_size": "{}x{}".format(width, height),
        "out_jpeg": out_bytes,
        "src_jpeg": src_bytes,
    }


# ============================================================ V6 差值图（定位） ===
#
# |A − B| × N, presented as a LOCATOR only. Everything below exists to stop this
# feature from becoming a lie (measured evidence in
# `_temp/research/画质差异可视化与感知指标调研.md` §1/§4):
#
# 1. **Alignment first.** Two frames must be put in the same coordinate system before
#    any per-pixel difference means anything. Different resolutions make `blend` fail
#    hard (-22) rather than silently scale -- good, and we must NOT paper over it by
#    inserting a scale only when the other side errored. The common resolution is
#    chosen up front, explicitly, and reported.
# 2. **Downscaling beats upscaling.** Upscaling cannot restore information and would
#    hide real differences behind interpolation on both sides.
# 3. **Explicit scale flags.** The default (bicubic) has its OWN sharpness effect, and
#    with this feature that shows up as a difference that has nothing to do with
#    compression. Measured on the same pair: `bilinear` gave YAVG 0.094 where `area`
#    gave 0.634 -- the kernel alone changes the map, so the UI has to name it.
# 4. **Luma only** (`format=gray`). Differencing after a yuv420p→RGB conversion drags
#    chroma-upsampling differences into the picture; those are not compression.
# 5. **Refuse when frames cannot be paired.** `framesync` (which blend uses) does NOT
#    error on a frame-rate mismatch -- it quietly pairs by nearest earlier timestamp,
#    so the map would show "quality differences" that are really different moments.
#    A refusal with a reason beats a beautiful lie. (Measured: a 30fps source vs its
#    15fps re-encode produced a map with no error at all.)
# 6. **The gain saturates.** After ×N every difference ≥ ceil(255/N) is pure white, so
#    the brightest areas stop being distinguishable. The threshold is reported instead
#    of hidden, and there is no authoritative N: we default to a mild gain.

#: Scale filter for the difference view. NOT the `lanczos` used by the 1:1 view:
#: resampling sharpness would be indistinguishable from the compression's own.
DIFF_FLAGS = "bilinear"
DIFF_DEFAULT_GAIN = 4
DIFF_MAX_GAIN = 32


class AlignmentError(FrameError):
    """The two sides cannot be paired frame by frame, so no map is produced."""


def parse_gain(value: Any, default: int = DIFF_DEFAULT_GAIN) -> int:
    """Amplification factor, clamped to 1..DIFF_MAX_GAIN."""
    try:
        gain = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(1, min(DIFF_MAX_GAIN, gain))


def saturation_value(gain: int) -> int:
    """Smallest difference that ×`gain` clips to pure white.

    With gain 8 anything ≥ 32 is white, so the map stops distinguishing "a bit worse"
    from "much worse" above that. Stated in the UI rather than discovered by the user.
    """
    safe = max(1, int(gain))
    return -(-255 // safe)          # ceil(255 / gain)


def common_size(size_a: Tuple[int, int], size_b: Tuple[int, int]) -> Tuple[int, int]:
    """The frame both sides are put into: never upscale (see note 2 above).

    Normally this is the OUTPUT size (the source is scaled down to it), which also
    matches the basis the 1:1 view and VMAF already use. Only when the output is
    larger than the source do we take the source size instead.
    """
    width_a, height_a = max(0, int(size_a[0])), max(0, int(size_a[1]))
    width_b, height_b = max(0, int(size_b[0])), max(0, int(size_b[1]))
    if width_b <= 0 or height_b <= 0:
        return (width_a, height_a)
    if width_a <= 0 or height_a <= 0:
        return (width_b, height_b)
    if width_a * height_a < width_b * height_b:
        return (width_a, height_a)
    return (width_b, height_b)


def _side_chain(index: int, tag: str, size: Tuple[int, int], own: Tuple[int, int],
                crop: Optional[Tuple[int, int, int, int]], flags: str) -> str:
    """One input's chain: normalise PTS -> scale (only if needed) -> crop -> luma."""
    steps = ["setpts=PTS-STARTPTS"]
    if own != size and size[0] > 0 and size[1] > 0:
        steps.append("scale={}:{}:flags={}".format(size[0], size[1], flags))
    if crop and len(crop) == 4:
        steps.append("crop={}:{}:{}:{}".format(*crop))
    steps.append("format=gray")
    return "[{}:v]{}[{}]".format(index, ",".join(steps), tag)


def diff_filters(size: Tuple[int, int], crop: Optional[Tuple[int, int, int, int]] = None,
                 gain: int = DIFF_DEFAULT_GAIN, *,
                 size_a: Optional[Tuple[int, int]] = None,
                 size_b: Optional[Tuple[int, int]] = None,
                 flags: str = DIFF_FLAGS) -> str:
    """filter_complex for the difference map, ending in `[o]`.

    A side that is already at the common size is NOT rescaled: an unnecessary
    resample of either side would put its own fingerprint into the difference.
    """
    own_a = size_a or size
    own_b = size_b or size
    chain = [
        _side_chain(0, "a", size, own_a, crop, flags),
        _side_chain(1, "b", size, own_b, crop, flags),
        "[a][b]blend=all_mode=difference",
    ]
    if parse_gain(gain) > 1:
        # `clip` is spelled out: relying on the implicit clamp would make the
        # saturation threshold invisible in the command that produced the picture.
        # Both sides are already `format=gray`, so there is only a luma plane to scale.
        chain[-1] += ",lutyuv=y='clip(val*{},0,255)'".format(parse_gain(gain))
    chain[-1] += "[o]"
    return ";".join(chain)


def pairing_problem(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    """'' when the two sides pair up frame by frame, else the reason to refuse.

    Takes plain numbers (fps / frames / duration) so it can be tested without files.
    See note 5: a mismatch here does not make ffmpeg fail, it makes it lie.
    """
    fps_a, fps_b = float(a.get("fps") or 0), float(b.get("fps") or 0)
    frames_a, frames_b = int(a.get("frames") or 0), int(b.get("frames") or 0)
    dur_a, dur_b = float(a.get("duration") or 0), float(b.get("duration") or 0)

    if frames_a and frames_b and frames_a != frames_b:
        return ("两侧帧数不同（{} vs {} 帧）：逐帧差值会把不同时刻的画面拿来相减，"
                "所以不出图").format(frames_a, frames_b)
    if fps_a and fps_b and abs(fps_a - fps_b) > 0.01:
        return ("两侧帧率不同（{:.3f} vs {:.3f} fps）：配对是按时间戳取的，不会报错，"
                "只会静默配错帧，所以不出图").format(fps_a, fps_b)
    if not (fps_a and fps_b) and not (frames_a and frames_b):
        return "两侧的帧率与帧数都无法确认，无法保证帧对帧对齐，所以不出图"
    fps = fps_a or fps_b
    if fps and dur_a and dur_b and abs(dur_a - dur_b) > 1.5 / fps:
        return ("两侧时长相差超过一帧（{:.3f}s vs {:.3f}s），帧对帧对齐不可靠，"
                "所以不出图").format(dur_a, dur_b)
    return ""


def difference(path_a: str, path_b: str, time_seconds: float, *,
               size: Tuple[int, int],
               crop: Optional[Tuple[int, int, int, int]] = None,
               gain: int = DIFF_DEFAULT_GAIN,
               size_a: Optional[Tuple[int, int]] = None,
               size_b: Optional[Tuple[int, int]] = None,
               flags: str = DIFF_FLAGS,
               quality: int = 3, timeout: int = 300) -> bytes:
    """|A − B| × gain at one time point, as JPEG bytes (luma only).

    Both inputs are seeked to `time_seconds` THE SAME WAY (output-side `-ss`, which is
    the accurate seek) so they land on the same frame: measured on identical inputs the
    map comes out exactly 0 (YAVG 0, YMAX 0), which is what makes any non-zero pixel
    worth looking at.
    """
    caps = get_caps()
    if not caps.available():
        raise FrameError("FFmpeg 不可用")
    try:
        at = max(0.0, float(time_seconds))
    except (TypeError, ValueError):
        raise FrameError("时间点无效")
    if size[0] <= 0 or size[1] <= 0:
        raise AlignmentError("无法确定共同分辨率")

    chain = diff_filters(size, crop, gain, size_a=size_a, size_b=size_b, flags=flags)
    argv = [caps.ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
            "-i", path_a, "-i", path_b,
            "-ss", "{:.3f}".format(at), "-frames:v", "1",
            "-filter_complex", chain, "-map", "[o]",
            "-f", "image2", "-c:v", "mjpeg", "-q:v", str(int(quality)), "pipe:1"]
    try:
        proc = work.run(argv, timeout=timeout, **_binary_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        raise FrameError("差值图生成失败：{}".format(exc))
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-240:]
        # A resolution mismatch lands here as -22. It must never be "fixed" by
        # inserting a scale behind the user's back: the common size is decided and
        # labelled before ffmpeg is called.
        raise FrameError(detail or "差值图生成失败（该时间点可能超出时长）")
    return proc.stdout
