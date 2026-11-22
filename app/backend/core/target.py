"""Target-size search: "get this under 200 MB".

The market gap this fills: consumer tools only offer a size slider, and CLI tools
(ab-av1, Av1an) only search for quality. Nothing does both directions. We already
have the hard half -- a real sample-encode estimator -- so this adds the inversion:
solve for the CRF that lands on a requested size.

How it works (no two-pass encode, so it stays valid for NVENC and SVT-AV1 alike):

    size(crf) = pixels * fps * duration * bpp(crf) / 8

`bpp(crf)` is the bits-per-pixel-per-frame curve, which is smooth and concave. We
measure it at three CRF points with short sample encodes, fit
`log(bpp) = a + b * crf`, then solve that line for the CRF the target needs. Three
measurements plus a closed-form solve is far cheaper than a second full pass, and
it degrades gracefully: if the target is unreachable we say so instead of silently
producing something else.

Accuracy depends on the samples matching the whole file, so we spread them across
the runtime (content complexity varies enormously -- measured 232-2972 kbps across
sections of one real file) and report a predicted range rather than a single number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .probe import MediaInfo

# CRF probes: spread wide enough to fit a clean line, but all inside the sane range
# for the codec so the fit is not dragged by extreme quality settings.
PROBE_CRFS: Dict[str, Tuple[int, ...]] = {
    "av1": (28, 34, 40),
    "hevc": (20, 25, 30),
    "h264": (19, 23, 27),
}
DEFAULT_PROBE = (23, 28, 33)

# Where the search is allowed to land, per codec family.
CRF_BOUNDS: Dict[str, Tuple[int, int]] = {
    "av1": (18, 52),
    "hevc": (14, 40),
    "h264": (12, 34),
}
DEFAULT_BOUNDS = (14, 40)

# Sample windows per probe (positions as fractions of the duration).
SAMPLE_FRACTIONS = (0.2, 0.5, 0.8)
SAMPLE_SECONDS = 6


def codec_family(encoder: str) -> str:
    name = (encoder or "").lower()
    if "av1" in name or "svt" in name or "aom" in name or "rav1e" in name:
        return "av1"
    if "265" in name or "hevc" in name:
        return "hevc"
    if "264" in name or "h264" in name or "avc" in name:
        return "h264"
    return "hevc"


def _parse_kbps(value: str) -> int:
    """`192k` / `192000` / `1.5M` -> bits per second. 0 when unparsable."""
    text = str(value or "").strip().lower()
    if not text:
        return 0
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


def audio_bytes(info: MediaInfo, argv: List[str], fallback_kbps: int = 128) -> int:
    """How many bytes the audio will occupy under this plan.

    Sample encodes run with `-an`, so the measured curve is VIDEO ONLY. Comparing a
    whole-file target against it overshoots by exactly the audio, which is invisible
    on a film with a short soundtrack and enormous on one with a long one (measured:
    a 123 s clip whose 317 kbps track is 4.9 MB against an 11 MB target -- a 45%
    overshoot). "Under 25 MB for WeChat" has to mean the file, so the audio is
    subtracted from the budget before the CRF is solved.
    """
    low = [str(a).lower() for a in (argv or [])]
    if "-an" in low:
        return 0
    if not info.audio_streams:
        return 0

    duration = float(info.duration or 0)
    if duration <= 0:
        return 0

    # An explicit -b:a means a re-encode at that rate; otherwise the streams are
    # copied and the source bitrates are what will land in the file.
    target_bps = 0
    for index, token in enumerate(low):
        if token in ("-b:a", "-ab", "-audio_bitrate") and index + 1 < len(low):
            target_bps = _parse_kbps(low[index + 1])
            break
        if token.startswith("-b:a="):
            target_bps = _parse_kbps(token.split("=", 1)[1])
            break

    streams = len(info.audio_streams)
    if target_bps > 0:
        per_stream = target_bps
    else:
        known = [s.bit_rate for s in info.audio_streams if s.bit_rate]
        if not known:
            per_stream = fallback_kbps * 1000
        else:
            # Copy mode: total of what the source actually carries.
            return int(sum(known) * duration / 8.0)
    return int(per_stream * streams * duration / 8.0)


def container_overhead_bytes(target_bytes: int) -> int:
    """Allowance for the container's own structures (moov, indexes, metadata).

    One percent, floored at 256 KB: small next to the audio term but enough that a
    deliberately tight target does not come in a few hundred kilobytes over.
    """
    return max(256 * 1024, int(max(0, target_bytes) * 0.01))


# --------------------------------------------------------------------- bpp guard
#
# The honest way to answer "can I get 100x smaller?" is not to talk about ratios at
# all -- it is to convert the request into bits per pixel per frame and compare it
# with what codecs can actually do. Ratios depend entirely on what you divide by
# (the same 3 Mbps output is 6x against one source and 249x against uncompressed),
# while bpp is a property of the picture.
#
# Bands below come from the measured curve in
# _temp/research/超级压缩倍率方法研究.md and the industry ABR ladders cited there.

#: (minimum bpp, tier, message) -- checked from best to worst.
BPP_TIERS = (
    (0.100, "comfortable",
     "每像素 {bpp:.3f} bit，属于「安全区」：任何现代编码器都能做得好看"),
    (0.040, "normal",
     "每像素 {bpp:.3f} bit，属于主流流媒体阶梯的常规区间"),
    (0.020, "degraded",
     "每像素 {bpp:.3f} bit，已进入「能明显看出劣化」的区间，建议同时降低分辨率"),
    (0.005, "extreme",
     "每像素 {bpp:.3f} bit，低于所有主流阶梯的最低档，只能靠降分辨率/抽帧/去音频维持"),
    (0.0, "impossible",
     "每像素 {bpp:.3f} bit：对 30fps 内容，任何编码器都无法保留可辨认的细节，"
     "这个目标不是「更努力的编码」能解决的"),
)

#: Uncompressed 8-bit 4:2:0 is 1.5 bytes per pixel; that is the only denominator for
#: which a three-digit "compression ratio" is honest, and it is always labelled.
UNCOMPRESSED_BYTES_PER_PIXEL = 1.5


def uncompressed_bytes(info: MediaInfo) -> int:
    """Size of this video as uncompressed 8-bit 4:2:0 -- the honest big denominator."""
    video = getattr(info, "video", None)
    duration = float(getattr(info, "duration", 0) or 0)
    if not video or not getattr(video, "fps", 0) or not duration:
        return 0
    pixels_per_second = float(video.width) * float(video.height) * float(video.fps)
    return int(pixels_per_second * duration * UNCOMPRESSED_BYTES_PER_PIXEL)


def bits_per_pixel(size_bytes: int, info: MediaInfo, pixels_override: int = 0) -> float:
    """bits per pixel per frame for a file of `size_bytes`.

    Tolerant of a partially-populated info object: returning 0 for "cannot compute"
    keeps these helpers usable from tests and from callers that only have a duration.
    """
    video = getattr(info, "video", None)
    duration = float(getattr(info, "duration", 0) or 0)
    if not video or not duration:
        return 0.0
    fps = float(getattr(video, "fps", 0) or 0)
    if fps <= 0:
        return 0.0
    frames = fps * duration
    pixels = (float(pixels_override) if pixels_override
              else float(video.width) * float(video.height))
    if frames <= 0 or pixels <= 0:
        return 0.0
    return (float(size_bytes) * 8.0) / (frames * pixels)


def reachability(target_bytes: int, info: MediaInfo,
                 overhead_bytes: int = 0) -> Dict[str, Any]:
    """Classify what a requested file size means in bpp terms.

    Returns the tier, the bpp, and a message phrased so the UI can print it verbatim.
    `videos_bpp` is computed on the video budget (target minus audio/container), since
    that is what the encoder actually gets to spend.
    """
    budget = max(0, int(target_bytes) - max(0, int(overhead_bytes)))
    value = bits_per_pixel(budget, info)
    result: Dict[str, Any] = {
        "bpp": round(value, 5),
        "video_budget_bytes": budget,
        "tier": "unknown",
        "message": "",
        "uncompressed_bytes": uncompressed_bytes(info),
    }
    if value <= 0:
        result["message"] = "无法计算每像素比特（缺少帧率或时长）"
        return result
    for minimum, tier, template in BPP_TIERS:
        if value >= minimum:
            result["tier"] = tier
            result["message"] = template.format(bpp=value)
            return result
    return result


def reachability_is_blocking(tier: str) -> bool:
    """Only the physically impossible tier should refuse the request.

    Everything above it is a decision for the user, phrased as a warning -- refusing
    to try because a target is aggressive would be paternalistic, and "aggressive"
    depends on content we have not measured yet.
    """
    return tier == "impossible"


@dataclass
class TargetResult:
    ok: bool = False
    crf: int = 0
    target_bytes: int = 0
    predicted_bytes: int = 0
    low: int = 0
    high: int = 0
    video_budget_bytes: int = 0
    overhead_bytes: int = 0
    audio_bytes: int = 0
    reachability: Dict[str, Any] = field(default_factory=dict)
    strict: bool = False
    samples: List[Dict[str, Any]] = field(default_factory=list)
    fit: Dict[str, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _measure_point(measure_one, crf: int) -> Optional[float]:
    """Measure size in bytes for the whole file at one CRF. None = failed."""
    try:
        return measure_one(crf)
    except Exception:  # noqa: BLE001
        return None


def solve_for_target(measure_one, info: MediaInfo, height: int,
                     target_bytes: int, encoder: str,
                     tolerance: float = 0.06,
                     overhead_bytes: int = 0,
                     audio_bytes_value: int = 0,
                     strict: bool = False) -> TargetResult:
    """Find the CRF whose predicted size is closest to `target_bytes`.

    `measure_one(crf) -> int` must encode a few short samples at that CRF and
    return the extrapolated full-file size in bytes. Supplied by the caller so this
    module stays free of subprocess concerns (and is unit-testable with a fake).

    `overhead_bytes` is everything in the finished file that is NOT the video stream
    (audio plus container). Sample encodes are video-only, so the search targets
    `target_bytes - overhead_bytes`; without that the answer is systematically over
    the requested size by exactly the overhead.

    `strict=True` aims at the BOTTOM of the prediction band instead of its middle, so
    the result is very likely to land under the target rather than around it. That is
    what "must fit in 25 MB" needs; it costs a little quality, so it is opt-in.
    """
    result = TargetResult(target_bytes=target_bytes,
                          overhead_bytes=max(0, int(overhead_bytes)),
                          audio_bytes=max(0, int(audio_bytes_value)))
    if target_bytes <= 0:
        result.warnings.append("目标体积无效")
        return result

    budget = target_bytes - result.overhead_bytes
    if budget <= 0:
        result.warnings.append(
            "目标体积太小：仅音轨与封装就需要约 {}，已超过目标 {}.{}".format(
                format_bytes(result.overhead_bytes), format_bytes(target_bytes),
                " 请去掉音轨、降低音频码率或提高目标体积" if result.audio_bytes else "")
        )
        result.note = ("目标 {} 里没有给视频留出空间（音频约 {}、封装约 {}）").format(
            format_bytes(target_bytes), format_bytes(result.audio_bytes),
            format_bytes(result.overhead_bytes - result.audio_bytes))
        return result

    result.video_budget_bytes = budget
    if result.overhead_bytes and result.overhead_bytes / target_bytes > 0.05:
        result.warnings.append(
            "音频与封装约占目标的 {:.0%}，已从视频预算中扣除".format(
                result.overhead_bytes / target_bytes))

    # Reachability in bpp terms (A2). Ratio talk is useless here -- the same file is
    # "6x" or "249x" depending on the denominator -- while bpp says whether the
    # request is physically answerable. Only the impossible tier refuses.
    reach = reachability(target_bytes, info, result.overhead_bytes)
    result.reachability = reach
    if reach["message"]:
        result.warnings.append(reach["message"])
    if reachability_is_blocking(reach["tier"]):
        result.warnings.append("已拒绝执行：请提高目标体积、降低分辨率或缩短片长")
        result.note = reach["message"]
        return result

    target_for_video = budget

    family = codec_family(encoder)
    probes = PROBE_CRFS.get(family, DEFAULT_PROBE)
    bounds = CRF_BOUNDS.get(family, DEFAULT_BOUNDS)

    points: List[Tuple[int, int]] = []
    for crf in probes:
        size = _measure_point(measure_one, crf)
        if not size or size <= 0:
            continue
        points.append((crf, size))
        result.samples.append({"crf": crf, "bytes": size})

    if len(points) < 2:
        result.warnings.append("采样不足，无法拟合体积曲线")
        result.note = "需要至少 2 个可用的 CRF 采样点"
        return result

    # Fit log(size) = a + b * crf  (b is negative: bigger CRF -> smaller file).
    n = len(points)
    xs = [p[0] for p in points]
    ys = [math.log(p[1]) for p in points]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom <= 1e-9:
        result.warnings.append("采样点过于集中，无法拟合")
        return result
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x
    result.fit = {"slope": round(slope, 6), "intercept": round(intercept, 6),
                  "points": n}

    def predict(crf: float) -> float:
        return math.exp(intercept + slope * crf)

    def solve_crf(budget: float) -> int:
        """The CRF whose prediction lands on `budget` (video bytes), with a nudge."""
        ideal_crf = int(round(max(bounds[0], min(bounds[1],
                            (math.log(budget) - intercept) / slope if slope else bounds[0]))))
        chosen, chosen_err = ideal_crf, abs(predict(ideal_crf) - budget)
        for candidate in (ideal_crf - 1, ideal_crf, ideal_crf + 1):
            if bounds[0] <= candidate <= bounds[1]:
                err = abs(predict(candidate) - budget)
                if err < chosen_err:
                    chosen, chosen_err = candidate, err
        return chosen

    # Strict mode: aim at the bottom of the band rather than its middle, so the
    # finished file very likely lands UNDER the target instead of around it. One
    # extra pass over the same closed form -- no second encode needed.
    if strict:
        middle = solve_crf(float(target_for_video))
        half_width = max(0.0, predict(middle - 1) - predict(middle))
        if half_width > 0:
            target_for_video = max(1.0, target_for_video - half_width)
            result.strict = True

    ideal = ((math.log(target_for_video) - intercept) / slope) if slope else bounds[0]
    best = solve_crf(float(target_for_video))

    result.crf = best
    video_predicted = int(predict(best))
    # Report what the FILE will weigh, not just the video stream: that is the number
    # the user asked about.
    result.predicted_bytes = video_predicted + result.overhead_bytes
    result.low = int(predict(best + 1)) + result.overhead_bytes   # coarser -> smaller
    result.high = int(predict(best - 1)) + result.overhead_bytes  # finer -> bigger
    result.ok = True

    if result.strict and result.high > target_bytes:
        result.warnings.append(
            "已按「不超过目标」求解，但预测上限 {:.1f} MB 仍略高于目标 {:.1f} MB"
            "（内容差异所致）".format(result.high / 1048576.0, target_bytes / 1048576.0))

    # Honest reporting: if the clamp pinned us at a bound, the target is not
    # reachable at this quality range and the user needs to know.
    if ideal < bounds[0]:
        result.warnings.append(
            "目标体积偏大：即使最省码率的设置（CRF {}）也会更小，"
            "可降低目标或去掉体积限制".format(bounds[0]))
    elif ideal > bounds[1]:
        result.warnings.append(
            "目标体积偏小：即使最省的 CRF {} 也难以达到，"
            "画质会明显下降，建议放宽目标或降低分辨率".format(bounds[1]))

    error = abs(result.predicted_bytes - target_bytes) / target_bytes
    guaranteed = result.strict and result.high <= target_bytes
    if guaranteed:
        # Aiming under the target is the point of strict mode; reporting that as an
        # "error" would read like the solver missed.
        result.warnings.append(
            "严格模式：预测区间 {:.1f}–{:.1f} MB 全部在目标 {:.1f} MB 以下".format(
                result.low / 1048576.0, result.high / 1048576.0,
                target_bytes / 1048576.0))
    elif error > tolerance:
        result.warnings.append(
            "预测误差约 {:.0%}，成片可能偏离目标；可先用「精确测算」再跑".format(error))
    split = ""
    if result.overhead_bytes:
        split = "（视频 {:.1f} + 音频/封装 {:.1f}）".format(
            result.predicted_bytes / 1048576.0 - result.overhead_bytes / 1048576.0,
            result.overhead_bytes / 1048576.0)
    head = "已按「不超过目标」求解" if guaranteed else "在 CRF {} 处预测".format(best)
    result.note = (
        "{}{} 整片 {:.1f} MB{}（目标 {:.1f} MB，{}）；区间 {:.1f}–{:.1f} MB".format(
            head,
            "" if guaranteed else "",
            result.predicted_bytes / 1048576.0, split,
            target_bytes / 1048576.0,
            "落在目标以下" if guaranteed else "误差 {:.0%}".format(error),
            result.low / 1048576.0, result.high / 1048576.0))
    return result


def parse_target(text: str, source_bytes: int = 0) -> int:
    """Accept '200MB', '1.5 GB', '800m', '300000000' -> bytes. 0 when unparsable.

    Also accepts the two RELATIVE forms, because "compress this to 20%" is how people
    actually think about it, and a ratio is only meaningful against a known source:

        '20%'  -> 20% of the source size
        '5x'   -> one fifth of the source size (5x smaller)
    """
    value = str(text or "").strip().lower().replace(" ", "")
    if not value:
        return 0

    if value.endswith("%"):
        try:
            percent = float(value[:-1])
        except ValueError:
            return 0
        if percent <= 0 or not source_bytes:
            return 0
        return int(source_bytes * percent / 100.0)

    if value.endswith("x"):
        try:
            factor = float(value[:-1])
        except ValueError:
            return 0
        if factor <= 1 or not source_bytes:
            return 0
        return int(source_bytes / factor)

    multipliers = {
        "tb": 1024 ** 4, "t": 1024 ** 4,
        "gb": 1024 ** 3, "g": 1024 ** 3,
        "mb": 1024 ** 2, "m": 1024 ** 2,
        "kb": 1024, "k": 1024,
        "b": 1,
    }
    for suffix in ("tb", "gb", "mb", "kb", "t", "g", "m", "k", "b"):
        if value.endswith(suffix):
            number = value[: -len(suffix)]
            try:
                return int(float(number) * multipliers[suffix])
            except ValueError:
                return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def describe_target(text: str, source_bytes: int = 0) -> str:
    """Echo how a target string was interpreted, for the UI to show back."""
    raw = str(text or "").strip()
    is_percent = raw.endswith("%")
    is_factor = raw.lower().endswith("x")
    parsed = parse_target(raw, source_bytes)
    if parsed <= 0:
        if not raw:
            return "无法识别：(空)"
        if is_percent or is_factor:
            number = raw[:-1].strip()
            try:
                value = float(number)
            except ValueError:
                return "无法识别：{}".format(raw)
            # Distinguish "nonsense number" from "valid but we lack the denominator",
            # because the fix is different for each.
            if is_percent and value <= 0:
                return "百分比必须大于 0：{}".format(raw)
            if is_factor and value <= 1:
                return "倍率必须大于 1（1x 就是原大小）：{}".format(raw)
            if not source_bytes:
                return "{}（需要先知道源文件大小才能换算）".format(raw)
        return "无法识别：{}".format(raw)
    if is_percent or is_factor:
        ratio = parsed / float(source_bytes) * 100 if source_bytes else 0
        return "{} → {}（源的 {:.0f}%）".format(raw, format_bytes(parsed), ratio)
    return "{}{}".format(raw, "" if raw.lower().endswith(("b", "t", "g", "m", "k")) else
                         "（按字节解释）")


def format_bytes(num: int) -> str:
    value = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return "{:.1f} {}".format(value, unit)
        value /= 1024
    return "{:.1f} TB".format(value)
