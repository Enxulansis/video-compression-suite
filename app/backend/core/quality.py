"""Quality scoring: a VMAF number, plus what it actually means.

Two things make a quality score useful rather than decorative:

1. **The basis is stated.** A VMAF computed against the original 4K frame and one
   computed against the source *scaled to the output's resolution* are different
   numbers, and only the second answers "did the encode hurt, given the size I chose?"
   (scaling to 720p and back costs ~20 VMAF points on its own -- measured). Every
   result carries a human-readable `basis` string, and the UI must show it.

2. **It is translated.** "VMAF 93.03" means nothing to most people. The bands below
   come from the usual VMAF reading: >=95 is visually indistinguishable in practice,
   90-95 is very good, and below ~80 artefacts are easy to see on a normal screen.

Measurement is sampled, not whole-file: three short windows are enough to place a
file in a band, and a full VMAF pass on a two-hour film is not something to run after
every encode.
"""

from __future__ import annotations

import re
import subprocess
from . import work
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .caps import get_caps, subprocess_kwargs
from .probe import MediaInfo

#: (minimum score, label, explanation) -- checked in order.
PLAIN_BANDS = (
    (95.0, "肉眼无损", "和原片并排也几乎看不出差别"),
    (92.0, "很好", "正常观看看不出差别，放大对比才有细微差异"),
    (88.0, "良好", "细看能发现轻微柔化，日常观看没问题"),
    (82.0, "一般", "仔细看能看出细节损失，暗部/纹理先丢"),
    (72.0, "明显劣化", "普通屏幕就能看出糊、块、涂抹"),
    (0.0, "很差", "画质损失很显眼，建议提高画质档或降低分辨率"),
)

#: Sample windows as fractions of the duration.
SAMPLE_FRACTIONS = (0.2, 0.5, 0.8)
SAMPLE_SECONDS = 6

_SCORE_RE = re.compile(r"VMAF score:\s*([\d.]+)")


@dataclass
class QualityResult:
    ok: bool = False
    score: float = 0.0
    plain: str = ""
    explanation: str = ""
    basis: str = ""
    samples: List[Dict[str, Any]] = field(default_factory=list)
    reference_size: str = ""
    note: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def plain_language(score: float) -> Dict[str, str]:
    """Map a VMAF score onto a band a non-expert can act on."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return {"plain": "未知", "explanation": "无法识别该分数"}
    for threshold, label, explanation in PLAIN_BANDS:
        if value >= threshold:
            return {"plain": label, "explanation": explanation}
    return {"plain": PLAIN_BANDS[-1][1], "explanation": PLAIN_BANDS[-1][2]}


def parse_score(text: str) -> Optional[float]:
    """The VMAF score from ffmpeg's log.

    Deliberately the LAST occurrence: ffmpeg prints a per-frame score when asked, and
    the summary line comes after those.
    """
    matches = _SCORE_RE.findall(str(text or ""))
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def basis_text(src: MediaInfo, out_w: int, out_h: int) -> str:
    """Say exactly what the score is measured against."""
    if not src.video:
        return "对源比较（基准未知）"
    same = (src.video.width == out_w and src.video.height == out_h)
    if same:
        return "对源原分辨率比较（{}x{}，同分辨率同质量口径）".format(out_w, out_h)
    return ("对源缩放到成品分辨率后比较（{}x{} → {}x{}；"
            "这是「同分辨率」口径，不包含降分辨率本身的损失）").format(
                src.video.width, src.video.height, out_w, out_h)


def measure_sample_at(src_path: str, plan, crf: int, seconds: int = SAMPLE_SECONDS,
                      position_fraction: float = 0.5) -> QualityResult:
    """Predict the quality a full encode at `crf` would have, from ONE sample window.

    This is the other half of the size/quality trade: the target-size solver can say
    "CRF 34 lands on 25 MB", and this says "and that will look like VMAF 94 (很好)".
    Without it the user is choosing a number blind.

    The sample is encoded with the same encoder arguments the real job uses, so the
    prediction is about the settings, not about a stand-in.
    """
    import subprocess
    from . import work
    import tempfile
    from pathlib import Path as _Path

    caps = get_caps()
    result = QualityResult()
    if not caps.available() or not caps.has_filter("libvmaf"):
        result.error = "当前 FFmpeg 不含 libvmaf，无法预估画质"
        return result

    from .probe import probe as _probe
    info = _probe(src_path)
    if not info.ok or not info.video:
        result.error = info.error or "无法探测源文件"
        return result

    argv = list(plan.argv)
    try:
        progress_at = argv.index("-progress")
    except ValueError:
        progress_at = len(argv)
    encoder_args = argv[:progress_at]
    input_at = encoder_args.index("-i")
    encoder_args = encoder_args[input_at + 2:]
    drop_with_value = {"-map", "-map_metadata", "-map_chapters", "-c:a", "-b:a",
                       "-c:s", "-movflags", "-maxrate", "-bufsize"}
    cleaned: List[str] = []
    skip_next = False
    for token in encoder_args:
        if skip_next:
            skip_next = False
            continue
        if token in drop_with_value:
            skip_next = True
            continue
        cleaned.append(token)
    # Force the CRF under test and keep the plan's video filters (scale/crop matter:
    # comparing at the wrong resolution would measure the wrong thing).
    cleaned = _override_crf(cleaned, crf)

    duration = float(info.duration or 0)
    start = max(0.0, duration * position_fraction - seconds / 2.0)
    if duration and start + seconds > duration:
        start = max(0.0, duration - seconds)

    tmpdir = _Path(tempfile.mkdtemp(prefix="vcs_quality_"))
    try:
        sample = tmpdir / "sample.mp4"
        encode = ([caps.ffmpeg, "-hide_banner", "-nostdin", "-y"]
                  + ["-ss", "{:.2f}".format(start), "-t", str(int(seconds)), "-i", src_path]
                  + cleaned + ["-an", str(sample)])
        try:
            proc = work.run(encode, text=True, timeout=1800, **subprocess_kwargs())
        except (OSError, subprocess.SubprocessError) as exc:
            result.error = "样片编码失败：{}".format(exc)
            return result
        if proc.returncode != 0 or not sample.is_file():
            result.error = "样片编码失败：{}".format((proc.stderr or "")[-200:])
            return result

        out_info = _probe(str(sample))
        if not out_info.ok or not out_info.video:
            result.error = "无法探测样片"
            return result
        out_w, out_h = out_info.video.width, out_info.video.height

        compare = [
            caps.ffmpeg, "-hide_banner", "-nostats", "-v", "info",
            # The sample starts at its own t=0 (it was cut out of the source), so it
            # is NOT seeked -- seeking it to the source's timestamp found no frames at
            # all and every prediction failed with "no score".
            "-t", str(int(seconds)), "-i", str(sample),
            "-ss", "{:.2f}".format(start), "-t", str(int(seconds)), "-i", src_path,
            "-lavfi",
            "[1:v]scale={}:{}:flags=bicubic,format=yuv420p[ref];"
            "[0:v]format=yuv420p[dist];[dist][ref]libvmaf=n_threads=4".format(out_w, out_h),
            "-f", "null", "-",
        ]
        try:
            proc = work.run(compare, text=True, timeout=1800, **subprocess_kwargs())
        except (OSError, subprocess.SubprocessError) as exc:
            result.error = "样片评分失败：{}".format(exc)
            return result
        score = parse_score((proc.stderr or "") + (proc.stdout or ""))
        if score is None:
            result.error = "样片未能算出分数"
            return result

        result.score = round(score, 2)
        band = plain_language(result.score)
        result.plain = band["plain"]
        result.explanation = band["explanation"]
        result.reference_size = "{}x{}".format(out_w, out_h)
        result.basis = basis_text(info, out_w, out_h)
        result.samples = [{"at": round(start, 2), "score": round(score, 3)}]
        result.note = ("在 CRF {} 上抽测 1 段 {}s 样片，预计画质 VMAF {:.1f}（{}）").format(
            crf, int(seconds), result.score, result.plain)
        result.ok = True
        return result
    finally:
        try:
            import shutil as _shutil
            _shutil.rmtree(str(tmpdir), ignore_errors=True)
        except OSError:
            pass


def _override_crf(args: List[str], crf: int) -> List[str]:
    """Replace the CRF/CQ value in an ffmpeg video-argument list."""
    out: List[str] = []
    replaced = False
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-crf", "-cq") and index + 1 < len(args):
            out += [token, str(int(crf))]
            replaced = True
            index += 2
            continue
        out.append(token)
        index += 1
    if not replaced:
        out += ["-crf", str(int(crf))]
    return out


def measure(out_path: str, src_path: str, src_info: Optional[MediaInfo] = None,
            seconds: int = SAMPLE_SECONDS) -> QualityResult:
    """Sampled VMAF of `out_path` against `src_path`. Never raises."""
    caps = get_caps()
    result = QualityResult()
    if not caps.available() or not caps.has_filter("libvmaf"):
        result.error = "当前 FFmpeg 不含 libvmaf，无法评分"
        return result

    from .probe import probe as _probe
    out_info = _probe(out_path)
    if not out_info.ok or not out_info.video:
        result.error = out_info.error or "无法探测成品"
        return result
    info = src_info if (src_info is not None and src_info.ok) else _probe(src_path)
    work.note_media(info)
    if not info.ok:
        result.error = info.error or "无法探测源文件"

    out_w, out_h = out_info.video.width, out_info.video.height
    if out_w <= 0 or out_h <= 0:
        result.error = "无法得知成品分辨率"
        return result
    result.reference_size = "{}x{}".format(out_w, out_h)
    result.basis = basis_text(info, out_w, out_h)

    duration = float(min(info.duration or 0, out_info.duration or 0))
    scores: List[float] = []
    for fraction in SAMPLE_FRACTIONS:
        start = max(0.0, duration * fraction - seconds / 2.0)
        if duration and start + seconds > duration:
            start = max(0.0, duration - seconds)
        argv = [
            caps.ffmpeg, "-hide_banner", "-nostats", "-v", "info",
            "-ss", "{:.2f}".format(start), "-t", str(int(seconds)), "-i", out_path,
            "-ss", "{:.2f}".format(start), "-t", str(int(seconds)), "-i", src_path,
            "-lavfi",
            "[1:v]scale={}:{}:flags=bicubic,format=yuv420p[ref];"
            "[0:v]format=yuv420p[dist];[dist][ref]libvmaf=n_threads=4".format(out_w, out_h),
            "-f", "null", "-",
        ]
        try:
            # subprocess_kwargs() already carries stdout/stderr/encoding.
            proc = work.run(argv, text=True, timeout=1800, **subprocess_kwargs())
        except (OSError, subprocess.SubprocessError) as exc:
            result.samples.append({"at": round(start, 2), "error": str(exc)})
            continue
        score = parse_score((proc.stderr or "") + (proc.stdout or ""))
        if score is None:
            result.samples.append({"at": round(start, 2), "error": "未取得分数"})
            continue
        scores.append(score)
        result.samples.append({"at": round(start, 2), "score": round(score, 3)})

    if not scores:
        result.error = "所有采样点都没能算出分数"
        return result

    result.score = round(sum(scores) / len(scores), 2)
    band = plain_language(result.score)
    result.plain = band["plain"]
    result.explanation = band["explanation"]
    spread = max(scores) - min(scores)
    result.note = "{} 个采样点平均 {:.1f}（{:.1f}–{:.1f}，极差 {:.1f}）".format(
        len(scores), result.score, min(scores), max(scores), spread)
    result.ok = True
    return result


# --------------------------------------------------------------- V1: per-frame
#
# A single average says "this file is 96" and nothing about WHERE it is not. The
# per-frame curve answers the user's actual question -- "码率这些参数看不出影响在哪"
# -- by naming the timestamps that carry the damage, and the VIF/ADM sub-scales say
# what KIND of damage it is (high frequencies go first).

#: Frames are ranked but spread out: ten frames from the same 0.2 s are one problem,
#: not ten, and a list of near-identical timestamps is useless to act on.
WORST_MIN_GAP_S = 1.5
WORST_COUNT = 8

#: CAMBI at or above this counts as visible banding (the CAMBI paper's line). Used
#: both for the verdict and to decide which frames deserve to be listed at all.
BANDING_VISIBLE = 5.0

#: CAMBI (banding) verdicts. The <5 line comes from the CAMBI paper's reading of the
#: scale; the rest is a monotone extension of it, and the UI always shows the number
#: alongside the words so the band never stands in for the measurement.
BANDING_BANDS = (
    (20.0, "严重色带", "平坦渐变处会出现明显的条纹/色阶，建议提高画质档"),
    (10.0, "明显色带", "渐变区域能看出条带，暗场尤其明显"),
    (BANDING_VISIBLE, "轻微色带", "细看渐变能发现轻微条带（论文判据 <5 为无色带）"),
    (0.0, "无色带", "渐变区没有可察觉的色带"),
)


def plain_banding(value: float) -> Dict[str, str]:
    """CAMBI score -> words. Mirrors plain_language() for VMAF."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return {"plain": "未知", "explanation": "无法识别该分数"}
    for threshold, label, explanation in BANDING_BANDS:
        if number >= threshold:
            return {"plain": label, "explanation": explanation}
    return {"plain": BANDING_BANDS[-1][1], "explanation": BANDING_BANDS[-1][2]}


@dataclass
class FrameScore:
    time: float = 0.0
    vmaf: float = 0.0
    vif_scale0: float = 0.0
    vif_scale3: float = 0.0
    adm2: float = 0.0
    motion: float = 0.0
    cambi: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class AnalysisResult:
    ok: bool = False
    basis: str = ""
    pooled: Dict[str, float] = field(default_factory=dict)
    vif_scales: List[float] = field(default_factory=list)
    adm_scales: List[float] = field(default_factory=list)
    worst: List[Dict[str, Any]] = field(default_factory=list)
    frame_count: int = 0
    windows: List[Dict[str, Any]] = field(default_factory=list)
    banding: Dict[str, Any] = field(default_factory=dict)
    note: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class BandingResult:
    """CAMBI scan of a single file -- no reference needed, so it works on any output."""

    ok: bool = False
    pooled: Dict[str, float] = field(default_factory=dict)
    worst: List[Dict[str, Any]] = field(default_factory=list)
    #: The MAX, not the mean: banding is local, and the UI picks its colour from this.
    #: It was silently missing at first, which made every scan render as "no banding"
    #: however bad it was.
    worst_score: float = 0.0
    frame_count: int = 0
    plain: str = ""
    explanation: str = ""
    note: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def select_worst(frames: List[FrameScore], count: int = WORST_COUNT,
                 min_gap: float = WORST_MIN_GAP_S) -> List[FrameScore]:
    """The `count` lowest-scoring frames, no two closer than `min_gap` seconds.

    Greedy: walk upward from the worst score and keep a frame only when it is far
    enough from everything already kept.
    """
    ordered = sorted(frames, key=lambda f: f.vmaf)
    kept: List[FrameScore] = []
    for frame in ordered:
        if all(abs(frame.time - other.time) >= min_gap for other in kept):
            kept.append(frame)
            if len(kept) >= count:
                break
    return sorted(kept, key=lambda f: f.time)


def _parse_vmaf_json(path: str, offset: float, fps: float) -> List[FrameScore]:
    """Per-frame scores from one libvmaf JSON log, with absolute timestamps."""
    import json
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    out: List[FrameScore] = []
    for item in data.get("frames") or []:
        metrics = item.get("metrics") or {}
        try:
            number = float(item.get("frameNum") or 0)
        except (TypeError, ValueError):
            number = 0.0
        out.append(FrameScore(
            time=round(offset + (number / fps if fps else 0.0), 3),
            vmaf=float(metrics.get("vmaf") or 0.0),
            vif_scale0=float(metrics.get("integer_vif_scale0") or 0.0),
            vif_scale3=float(metrics.get("integer_vif_scale3") or 0.0),
            adm2=float(metrics.get("integer_adm2") or 0.0),
            motion=float(metrics.get("integer_motion2") or 0.0),
            cambi=float(metrics.get("cambi") or 0.0),
        ))
    return out


def _pool(values: List[float]) -> Dict[str, float]:
    """min/max/mean/harmonic mean -- the same four libvmaf reports."""
    usable = [v for v in values if v > 0]
    if not usable:
        return {}
    count = float(len(usable))
    return {
        "min": round(min(usable), 3),
        "max": round(max(usable), 3),
        "mean": round(sum(usable) / count, 3),
        "harmonic_mean": round(count / sum(1.0 / v for v in usable), 3),
    }


def _pool_cambi(values: List[float]) -> Dict[str, float]:
    """CAMBI's floor is 0, so unlike VMAF a zero is a real reading, not a gap."""
    if not values:
        return {}
    count = float(len(values))
    return {
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "mean": round(sum(values) / count, 3),
    }


def summarise_banding(scores: List[float],
                      frames: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Pooled CAMBI plus the worst banding frames and a plain-language verdict.

    Uses the MAX for the verdict, not the mean: banding is a local defect, and a
    single badly banded gradient is what a viewer notices. Averaging it away over a
    whole film is exactly how "VMAF 92 but it looks striped" happens.
    """
    usable = [s for s in scores if s is not None]
    if not usable:
        return {}
    worst_score = max(usable)
    band = plain_banding(worst_score)
    out: Dict[str, Any] = {
        "pooled": _pool_cambi(usable),
        "worst_score": round(worst_score, 3),
        "plain": band["plain"],
        "explanation": band["explanation"],
    }
    if frames:
        ranked = sorted(frames, key=lambda f: -(f.get("cambi") or 0.0))
        kept: List[Dict[str, Any]] = []
        for frame in ranked:
            # Only genuinely banded frames belong in a "banding hot-spot" list. The
            # spread-out rule alone happily listed a CAMBI 0.2 frame next to a 24.0
            # one just because it sat 11 s away -- that is noise dressed as a finding.
            # 5.0 is the paper's "visible banding" line, the same one the verdict uses.
            if (frame.get("cambi") or 0.0) < BANDING_VISIBLE:
                continue
            if all(abs((frame.get("time") or 0) - (other.get("time") or 0)) >= WORST_MIN_GAP_S
                   for other in kept):
                kept.append({"time": frame.get("time"), "cambi": frame.get("cambi"),
                             "vmaf": frame.get("vmaf")})
                if len(kept) >= 5:
                    break
        out["worst"] = sorted(kept, key=lambda f: f["time"])
    return out


def scan_banding(path: str, seconds: int = 8, full: bool = False,
                 windows: int = 4) -> BandingResult:
    """CAMBI scan of ONE file -- no reference required.

    Because CAMBI is no-reference, the same stream is fed to both of libvmaf's inputs
    (verified on this build: the values are bit-identical to feeding the real pair).
    That makes banding scannable for any output, including files compressed long ago
    whose source is gone.
    """
    import json
    import subprocess
    from . import work
    import tempfile
    from pathlib import Path as _Path

    caps = get_caps()
    result = BandingResult()
    if not caps.available() or not caps.has_filter("libvmaf"):
        result.error = "当前 FFmpeg 不含 libvmaf，无法扫描色带"
        return result

    from .probe import probe as _probe
    info = _probe(path)
    if not info.ok or not info.video:
        result.error = info.error or "无法探测该文件"
        return result
    duration = float(info.duration or 0)
    fps = float(info.video.fps or 0.0)

    if full:
        plan = [(0.0, duration)]
    else:
        plan = []
        for index in range(max(1, windows)):
            fraction = (index + 0.5) / max(1, windows)
            start = max(0.0, duration * fraction - seconds / 2.0)
            if duration and start + seconds > duration:
                start = max(0.0, duration - seconds)
            plan.append((round(start, 2), float(seconds)))

    frames: List[FrameScore] = []
    tmpdir = _Path(tempfile.mkdtemp(prefix="vcs_cambi_"))
    try:
        for index, (start, length) in enumerate(plan):
            name = "c{}.json".format(index)
            argv = [
                caps.ffmpeg, "-hide_banner", "-nostats", "-v", "error",
                "-ss", "{:.2f}".format(start), "-t", "{:g}".format(length), "-i", path,
                "-lavfi",
                # One input, split: the "reference" is the same stream, which is
                # legitimate precisely because CAMBI never compares the two.
                "[0:v]split=2[a][b];[a]format=yuv420p[dist];[b]format=yuv420p[ref];"
                "[dist][ref]libvmaf=log_path={}:log_fmt=json:"
                "feature=name=cambi:n_threads=4".format(name),
                "-f", "null", "-",
            ]
            try:
                work.run(argv, text=True, timeout=7200, cwd=str(tmpdir),
                               **subprocess_kwargs())
            except (OSError, subprocess.SubprocessError) as exc:
                result.error = str(exc)
                continue
            frames.extend(_parse_vmaf_json(str(tmpdir / name), start, fps))
    finally:
        import shutil as _shutil
        _shutil.rmtree(str(tmpdir), ignore_errors=True)

    if not frames:
        result.error = result.error or "所有窗口都没能取到 CAMBI 数据"
        return result

    frames.sort(key=lambda f: f.time)
    result.frame_count = len(frames)
    summary = summarise_banding([f.cambi for f in frames],
                               [f.to_dict() for f in frames])
    result.pooled = summary.get("pooled", {})
    result.worst = summary.get("worst", [])
    result.worst_score = float(summary.get("worst_score") or 0.0)
    result.plain = summary.get("plain", "")
    result.explanation = summary.get("explanation", "")
    result.note = "{} 帧（{} 个窗口）：CAMBI 均值 {:.2f}，最差 {:.2f} —— {}".format(
        len(frames), len(plan), result.pooled.get("mean", 0.0),
        summary.get("worst_score", 0.0), result.plain)
    result.ok = True
    return result


def analyze(out_path: str, src_path: str, src_info: Optional[MediaInfo] = None,
            seconds: int = 6, full: bool = False,
            worst_count: int = WORST_COUNT) -> AnalysisResult:
    """Per-frame VMAF so the user can see WHERE the quality went.

    Default is the same three sampled windows the score uses; `full=True` walks the
    whole file, which is far slower but catches damage between the windows.

    Never raises: a failure returns `ok=False` with a reason.
    """
    import json
    import subprocess
    from . import work
    import tempfile
    from pathlib import Path as _Path

    caps = get_caps()
    result = AnalysisResult()
    if not caps.available() or not caps.has_filter("libvmaf"):
        result.error = "当前 FFmpeg 不含 libvmaf，无法分析"
        return result

    from .probe import probe as _probe
    out_info = _probe(out_path)
    if not out_info.ok or not out_info.video:
        result.error = out_info.error or "无法探测成品"
        return result
    info = src_info if (src_info is not None and src_info.ok) else _probe(src_path)
    work.note_media(info)
    if not info.ok or not info.video:
        result.error = info.error or "无法探测源文件"
        return result

    out_w, out_h = out_info.video.width, out_info.video.height
    fps = float(out_info.video.fps or 0.0)
    result.basis = basis_text(info, out_w, out_h)
    duration = float(min(info.duration or 0, out_info.duration or 0))

    if full:
        windows = [(0.0, duration)]
    else:
        windows = []
        for fraction in SAMPLE_FRACTIONS:
            start = max(0.0, duration * fraction - seconds / 2.0)
            if duration and start + seconds > duration:
                start = max(0.0, duration - seconds)
            windows.append((round(start, 2), float(seconds)))

    frames: List[FrameScore] = []
    tmpdir = _Path(tempfile.mkdtemp(prefix="vcs_vmaf_"))
    try:
        for index, (start, length) in enumerate(windows):
            name = "w{}.json".format(index)
            # A drive-letter colon cannot appear in a filter option, so the log is a
            # bare filename resolved against the process CWD. (Escaping it as `\:`
            # does not survive the filter parser.)
            argv = [
                caps.ffmpeg, "-hide_banner", "-nostats", "-v", "error",
                "-ss", "{:.2f}".format(start), "-t", "{:g}".format(length), "-i", out_path,
                "-ss", "{:.2f}".format(start), "-t", "{:g}".format(length), "-i", src_path,
                "-lavfi",
                "[1:v]scale={}:{}:flags=bicubic,format=yuv420p[ref];"
                "[0:v]format=yuv420p[dist];"
                # CAMBI rides along in the same pass: it costs nothing extra here and
                # it catches the one failure VMAF is nearly blind to (banding in flat
                # gradients -- the CAMBI paper measured VMAF/MOS correlation at 0.088
                # on a banding dataset where CAMBI reached -0.923).
                "[dist][ref]libvmaf=log_path={}:log_fmt=json:"
                "feature=name=cambi:n_threads=4".format(out_w, out_h, name),
                "-f", "null", "-",
            ]
            try:
                proc = work.run(argv, text=True, timeout=7200,
                                      cwd=str(tmpdir), **subprocess_kwargs())
            except (OSError, subprocess.SubprocessError) as exc:
                result.windows.append({"start": start, "error": str(exc)})
                continue
            got = _parse_vmaf_json(str(tmpdir / name), start, fps)
            if not got:
                result.windows.append({"start": start, "error": "未取到逐帧数据",
                                       "stderr": (proc.stderr or "")[-160:]})
                continue
            frames.extend(got)
            result.windows.append({"start": start, "seconds": length,
                                   "frames": len(got),
                                   "mean": round(sum(f.vmaf for f in got) / len(got), 3)})
    finally:
        import shutil as _shutil
        _shutil.rmtree(str(tmpdir), ignore_errors=True)

    if not frames:
        result.error = "所有窗口都没能取到逐帧分数"
        return result

    frames.sort(key=lambda f: f.time)
    result.frame_count = len(frames)
    result.pooled = _pool([f.vmaf for f in frames])
    result.vif_scales = [
        round(sum(f.vif_scale0 for f in frames) / len(frames), 4),
        round(sum(f.vif_scale3 for f in frames) / len(frames), 4),
    ]
    result.worst = [f.to_dict() for f in select_worst(frames, worst_count)]
    # Banding summary comes free from the same pass.
    result.banding = summarise_banding([f.cambi for f in frames],
                                       [f.to_dict() for f in frames])
    low = result.pooled.get("min", 0.0)
    result.note = (
        "{} 帧（{} 个窗口）：均值 {:.1f}，最差 {:.1f}，调和平均 {:.1f}".format(
            len(frames), len(windows), result.pooled.get("mean", 0.0), low,
            result.pooled.get("harmonic_mean", 0.0)))
    result.ok = True
    return result
