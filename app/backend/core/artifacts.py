"""Blockiness and blur measurements (V5) -- the two defects VMAF under-reports.

VMAF is a *fidelity* score: it answers "how different is this from the source", and it
weighs that difference by a model of visibility. Two artefacts that viewers name
immediately -- blocking and softness -- are poorly served by that framing, because
they are structural rather than additive. These two no-reference filters measure them
directly, which is also why they can be run on a finished file alone.

What can and cannot be claimed
------------------------------
* **Relative is solid.** Comparing output against source at the SAME resolution is
  well defined and is the primary signal here: "40% more blockiness than the source".
* **Absolute is not.** `blockdetect` has no accepted threshold in the literature (the
  original paper proposes the measure, not a pass/fail line). So the absolute numbers
  are SHOWN but not turned into verdicts. `blurdetect` has empirical anchors only
  (a sharp test frame measured ~4.6 here, and gblur sigma=3 pushed it to ~11.2), which
  is content-dependent -- so blur is treated the same way: the source is the baseline.

Both filters are single-input, so comparing means running each side once. The source is
scaled to the output's resolution first, for the same reason the frame comparison does
it: two different-sized pictures are not comparable.
"""

from __future__ import annotations

import subprocess
from . import work
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .caps import get_caps, subprocess_kwargs

#: Sample windows as fractions of the duration.
SAMPLE_FRACTIONS = (0.2, 0.5, 0.8)
SAMPLE_SECONDS = 6

#: OUR thresholds for a relative change against the source. They are empirical
#: judgement calls, not published limits -- `blockdetect` has no agreed absolute
#: scale, so a relative rule is the only defensible one, and it is labelled as ours.
BLOCK_NOTABLE = 0.15      # +15% blockiness
BLOCK_ALARMING = 0.50     # +50%
BLUR_NOTABLE = 0.10       # +10% edge width = softer
BLUR_ALARMING = 0.30

#: Empirical blur anchors measured on this machine, quoted as context only.
BLUR_REFERENCE_SHARP = 4.6
BLUR_REFERENCE_BLURRED = 11.2

#: The metadata keys the filters emit.
BLOCK_KEY = "lavfi.block"
BLUR_KEY = "lavfi.blur"


@dataclass
class FrameArtifacts:
    index: int = 0
    time: float = 0.0
    block: float = 0.0
    blur: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ArtifactScan:
    ok: bool = False
    frames: List[FrameArtifacts] = field(default_factory=list)
    pooled: Dict[str, Dict[str, float]] = field(default_factory=dict)
    windows: List[Dict[str, Any]] = field(default_factory=list)
    note: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "pooled": self.pooled,
            "windows": self.windows,
            "frame_count": len(self.frames),
            "note": self.note,
            "error": self.error,
            "worst_block": self.worst(BLOCK_KEY.replace("lavfi.", "")),
            "worst_blur": self.worst("blur"),
        }

    def worst(self, which: str, count: int = 5) -> List[Dict[str, Any]]:
        """The frames with the most blockiness / most blur, spread out in time."""
        pick = (lambda f: f.block) if which == "block" else (lambda f: f.blur)
        ranked = sorted(self.frames, key=pick, reverse=True)
        kept: List[FrameArtifacts] = []
        for frame in ranked:
            if all(abs(frame.time - other.time) >= 1.0 for other in kept):
                kept.append(frame)
                if len(kept) >= count:
                    break
        return [f.to_dict() for f in sorted(kept, key=lambda f: f.time)]


def parse_metadata(text: str, fps: float = 0.0) -> List[FrameArtifacts]:
    """Parse `metadata=mode=print` output into per-frame values.

    The format is a `frame:N pts:.. pts_time:T` header followed by one `key=value`
    line per metadata entry, so the parser has to carry the current frame across
    lines rather than treating each line as a record.
    """
    frames: List[FrameArtifacts] = []
    current: Optional[FrameArtifacts] = None
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("frame:"):
            if current is not None:
                frames.append(current)
            current = FrameArtifacts()
            parts = line.split()
            for token in parts:
                if token.startswith("frame:"):
                    try:
                        current.index = int(token.split(":", 1)[1])
                    except (IndexError, ValueError):
                        pass
                elif token.startswith("pts_time:"):
                    try:
                        current.time = round(float(token.split(":", 1)[1]), 3)
                    except (IndexError, ValueError):
                        pass
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        try:
            number = float(value.strip())
        except ValueError:
            continue
        if key == BLOCK_KEY:
            current.block = number
        elif key == BLUR_KEY:
            current.blur = number
    if current is not None:
        frames.append(current)
    if fps:
        for position, frame in enumerate(frames):
            if not frame.time:
                frame.time = round(position / fps, 3)
    return frames


def _pool(values: List[float]) -> Dict[str, float]:
    """Mean/max for one metric. These are not VMAF: higher is always worse."""
    usable = [v for v in values if v is not None]
    if not usable:
        return {}
    return {
        "mean": round(sum(usable) / len(usable), 3),
        "max": round(max(usable), 3),
    }


def scan(path: str, seconds: int = SAMPLE_SECONDS,
         windows: int = 3, scale_to: Optional[Tuple[int, int]] = None,
         window_list: Optional[List[Tuple[float, float]]] = None) -> ArtifactScan:
    """Measure blocking and blur over sampled windows of one file."""
    caps = get_caps()
    result = ArtifactScan()
    if not caps.available() or not caps.has_filter("blockdetect") \
            or not caps.has_filter("blurdetect"):
        result.error = "当前 FFmpeg 缺少 blockdetect / blurdetect 滤镜"
        return result

    from .probe import probe as _probe
    info = _probe(path)
    if not info.ok or not info.video:
        result.error = info.error or "无法探测该文件"
        return result
    duration = float(info.duration or 0)
    fps = float(info.video.fps or 0.0)

    if window_list is None:
        window_list = []
        for index in range(max(1, windows)):
            fraction = (index + 0.5) / max(1, windows)
            start = max(0.0, duration * fraction - seconds / 2.0)
            if duration and start + seconds > duration:
                start = max(0.0, duration - seconds)
            window_list.append((round(start, 2), float(seconds)))

    chain = "blockdetect=period_min=3:period_max=24,blurdetect=low=0.1:high=0.5"
    if scale_to and scale_to[0] > 0 and scale_to[1] > 0:
        chain = "scale={}:{}:flags=lanczos,".format(scale_to[0], scale_to[1]) + chain

    frames: List[FrameArtifacts] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="vcs_artifacts_"))
    try:
        for index, (start, length) in enumerate(window_list):
            name = "a{}.txt".format(index)
            argv = [
                caps.ffmpeg, "-hide_banner", "-nostats", "-v", "error",
                "-ss", "{:.2f}".format(start), "-t", "{:g}".format(length), "-i", path,
                # A relative filename resolved against the process CWD: a drive-letter
                # colon cannot appear inside a filter option.
                "-vf", "{},metadata=mode=print:file={}".format(chain, name),
                "-an", "-f", "null", "-",
            ]
            try:
                work.run(argv, text=True, timeout=3600, cwd=str(tmpdir),
                               **subprocess_kwargs())
            except (OSError, subprocess.SubprocessError) as exc:
                result.windows.append({"start": start, "error": str(exc)})
                continue
            try:
                text = (tmpdir / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                result.windows.append({"start": start, "error": "未生成测量文件"})
                continue
            got = parse_metadata(text, fps)
            if not got:
                result.windows.append({"start": start, "error": "未取到测量值"})
                continue
            for frame in got:
                frame.time = round(start + frame.time, 3)
            frames.extend(got)
            result.windows.append({"start": start, "seconds": length, "frames": len(got)})
    finally:
        import shutil
        shutil.rmtree(str(tmpdir), ignore_errors=True)

    if not frames:
        result.error = result.error or "所有窗口都没能取到测量值"
        return result
    frames.sort(key=lambda f: f.time)
    result.frames = frames
    result.pooled = {
        "block": _pool([f.block for f in frames]),
        "blur": _pool([f.blur for f in frames]),
    }
    result.note = "{} 帧（{} 个窗口）：块效应均值 {:.2f}，模糊均值 {:.2f}".format(
        len(frames), len(window_list),
        result.pooled.get("block", {}).get("mean", 0.0),
        result.pooled.get("blur", {}).get("mean", 0.0))
    result.ok = True
    return result


def relative_verdict(label: str, src: float, out: float,
                     notable: float, alarming: float,
                     higher_is_worse: str) -> Dict[str, Any]:
    """Compare one metric against the source and phrase the change.

    Purely relative on purpose: absolute thresholds for these filters are not
    established, so the source IS the reference and the change is what gets reported.
    """
    if not src or src <= 0:
        return {"label": label, "level": "unknown", "delta": 0.0,
                "detail": "{}：无法与源比较（源无有效读数）".format(label)}
    delta = (out - src) / src
    if delta >= alarming:
        level = "bad"
    elif delta >= notable:
        level = "warn"
    elif delta <= -notable:
        level = "better"
    else:
        level = "ok"
    sign = "+" if delta >= 0 else ""
    detail = "{}：源 {:.2f} → 成品 {:.2f}（{}{:.0%}，{}）".format(
        label, src, out, sign, delta, higher_is_worse)
    return {"label": label, "level": level, "delta": round(delta, 4), "detail": detail}


def compare(src_path: str, out_path: str, seconds: int = SAMPLE_SECONDS,
            windows: int = 3) -> Dict[str, Any]:
    """Source vs output blocking/blur, at the output's resolution."""
    from .probe import probe as _probe
    out_info = _probe(out_path)
    if not out_info.ok or not out_info.video:
        return {"ok": False, "error": out_info.error or "无法探测成品"}
    src_info = _probe(src_path)
    if not src_info.ok:
        return {"ok": False, "error": src_info.error or "无法探测源文件"}

    common = (out_info.video.width, out_info.video.height)
    # Use the SAME windows for both sides, so the two numbers describe the same
    # content -- sampling each side independently would compare different scenes.
    duration = float(min(src_info.duration or 0, out_info.duration or 0))
    window_list = []
    for index in range(max(1, windows)):
        fraction = (index + 0.5) / max(1, windows)
        start = max(0.0, duration * fraction - seconds / 2.0)
        if duration and start + seconds > duration:
            start = max(0.0, duration - seconds)
        window_list.append((round(start, 2), float(seconds)))

    out_scan = scan(out_path, window_list=window_list)
    src_scan = scan(src_path, scale_to=common, window_list=window_list)
    if not out_scan.ok or not src_scan.ok:
        return {"ok": False,
                "error": out_scan.error or src_scan.error or "测量失败"}

    src_block = src_scan.pooled.get("block", {}).get("mean", 0.0)
    out_block = out_scan.pooled.get("block", {}).get("mean", 0.0)
    src_blur = src_scan.pooled.get("blur", {}).get("mean", 0.0)
    out_blur = out_scan.pooled.get("blur", {}).get("mean", 0.0)

    verdicts = [
        relative_verdict("块效应", src_block, out_block, BLOCK_NOTABLE, BLOCK_ALARMING,
                         "越大越明显"),
        relative_verdict("模糊度", src_blur, out_blur, BLUR_NOTABLE, BLUR_ALARMING,
                         "越大越糊"),
    ]
    worst = out_scan.pooled.get("block", {}).get("max", 0.0)
    # State the basis, for the same reason the VMAF panel does: measured at the
    # output's resolution, so a downscaled encode shows no resolution loss HERE. The
    # blur number would otherwise look reassuring on a 360p output.
    native = (src_info.video.width, src_info.video.height) if src_info.video else (0, 0)
    if native == common:
        basis = "对源原分辨率比较（{}x{}）".format(*common)
    else:
        basis = ("两侧都按成品分辨率 {}x{} 比较（源 {}x{} 已缩放）"
                 "—— 该口径不含降分辨率本身的损失").format(common[0], common[1],
                                                          native[0], native[1])
    return {
        "ok": True,
        "common_size": "{}x{}".format(*common),
        "source_native": "{}x{}".format(*native) if native[0] else "",
        "basis": basis,
        "source": {"block": round(src_block, 3), "blur": round(src_blur, 3)},
        "output": {"block": round(out_block, 3), "blur": round(out_blur, 3)},
        "max_block": round(worst, 3),
        "verdicts": verdicts,
        "worst_block_frames": out_scan.worst("block", 4),
        "worst_blur_frames": out_scan.worst("blur", 4),
        "thresholds": {"block_notable": BLOCK_NOTABLE, "block_alarming": BLOCK_ALARMING,
                       "blur_notable": BLUR_NOTABLE, "blur_alarming": BLUR_ALARMING},
        "note": ("均为无参考指标（只需被测文件本身），且只在**同一分辨率**下可比"
                 "——所以两侧都按成品分辨率测量。判据是相对源的变化幅度："
                 "blockdetect 没有公认的绝对阈值，blurdetect 只有经验锚点"
                 "（本机清晰样片约 {:.1f}、加 gblur σ=3 后约 {:.1f}，且随内容变化），"
                 "因此这里把源当作基线，只报变化。").format(
                     BLUR_REFERENCE_SHARP, BLUR_REFERENCE_BLURRED),
    }
