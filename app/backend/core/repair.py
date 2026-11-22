"""Repair: make an already-compressed-but-unseekable file playable again.

Diagnosis first, then the cheapest repair that can actually work.

The failure mode this exists for (confirmed on a real user file):

    keyframe count = 1 over 167 s   ->  every seek must decode from 0 s
    ffmpeg -ss 80 -i file -frames:v 1  ->  fails outright

Such a file carries good pixels but a broken GOP structure.  There is no way to
insert keyframes into an existing AV1/HEVC bitstream without re-encoding, so
"repair" means one of:

  remux     lossless, instant -- only helps when the pixels are already fine and
            merely the container index is missing (rare, but free to check)
  same-codec  re-encode with the seek-safe rules, keeping the original codec
            (best size/quality trade-off for an AV1 source)
  compat      re-encode to 8-bit HEVC (hvc1) for maximum player compatibility

Rebuilding an inadequate GOP requires re-encoding and may increase size and
reduce quality. Remux only rebuilds the container and preserves the source GOP.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .caps import get_caps
from .planner import (clamp_gop_seconds, compute_gop, Overrides, _audio_args,
                      _subtitle_args, track_warnings, validate_video_container)
from .probe import MediaInfo, StreamInfo, probe
from .verifier import MAX_KEYFRAME_GAP_S, _max_gap, keyframe_times

# Codecs that support 10-bit and are worth keeping when the source is 10-bit.
_TEN_BIT_OK = {"av1", "hevc", "vp9", "vvc"}
# Containers whose subtitle/audio handling differs from Matroska.
_MP4_FAMILY = {"mp4", "mov", "m4v", "3gp", "3g2", "f4v"}

# ---------------------------------------------------------------------------
# film-grain synthesis is DISABLED on purpose.
#
# Measured on a real user file (AV1 10-bit, 1280x720, 167 s): with
# `film-grain=8` the repaired output came out at ~1920 kbps, i.e. ~4x the source
# (347 kbps).  With `film-grain=0` the same settings produced ~340-420 kbps --
# smaller than the source at comparable quality.  The synthesised grain is noise
# that the encoder then has to spend bits on, so for *repair* (where the goal is
# seekability without ballooning the file) it is strictly harmful.
#
# CRF 38 keeps a repaired file at roughly the source's own bitrate.  Choose lower
# values for higher quality, at a larger size.
# ---------------------------------------------------------------------------
DEFAULT_REPAIR_CRF = 38
DEFAULT_REPAIR_PRESET = 6


def _svt_params(options: Dict[str, Any], gop_seconds: float) -> str:
    """SVT-AV1 parameters for a repair encode: closed GOP, no grain synthesis."""
    kv = {
        "irefresh-type": "2",          # R2 -- closed GOP is mandatory
        "tune": "0",                   # subjective quality
        "enable-overlays": "1",
        "scd": "1",                    # scene-cut detection
        "film-grain": str(int(options.get("film_grain") or 0)),
        "aq-mode": "2",
        # Clamped by the planner's single helper, or a caller passing 5 would keyint=5s
        # while the report still claimed the R1 limit.
        "keyint": "{}s".format(max(1, int(math.ceil(clamp_gop_seconds(gop_seconds))))),
    }
    return ":".join("{}={}".format(k, v) for k, v in kv.items())


@dataclass
class Diagnosis:
    path: str
    ok: bool = False
    error: str = ""
    codec: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    size_bytes: int = 0
    pix_fmt: str = ""
    bit_depth: int = 0
    audio_codec: str = ""
    container: str = ""
    keyframes: int = 0
    max_keyframe_gap: float = 0.0
    first_keyframe: float = 0.0
    tail_gap: float = 0.0
    frames: int = 0
    verdict: str = "unknown"        # seekable | coarse | broken | unknown
    seek_ok: bool = True
    reasons: List[str] = field(default_factory=list)
    streams: List[StreamInfo] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["summary"] = self.summary()
        return data

    def summary(self) -> str:
        if self.verdict == "seekable":
            return "关键帧结构正常，可以自由拖动进度条"
        if self.verdict == "coarse":
            return "关键帧间隔偏大，拖动定位会跳、可能卡顿"
        if self.verdict == "broken":
            return "几乎没有关键帧，播放器无法定位 —— 拖动进度条必然卡死"
        return self.error or "无法判定"


def diagnose(path: str) -> Diagnosis:
    """Inspect seekability.  Read-only; never modifies the source."""
    info = probe(path)
    diag = Diagnosis(path=str(path))
    if not info.ok:
        diag.error = info.error or "无法探测该文件"
        return diag

    video = info.video
    assert video is not None
    diag.ok = True
    diag.codec = video.codec
    diag.profile = video.profile
    diag.width = video.width
    diag.height = video.height
    diag.fps = video.fps
    diag.duration = info.duration
    diag.size_bytes = info.size_bytes
    diag.pix_fmt = video.pix_fmt
    diag.bit_depth = video.bit_depth
    diag.container = (info.format_name.split(",")[0] or "").strip()
    diag.streams = list(info.streams)
    if info.audio_streams:
        diag.audio_codec = info.audio_streams[0].codec

    keys, _ = keyframe_times(str(path))
    diag.keyframes = len(keys)
    if keys:
        diag.first_keyframe = round(keys[0], 3)
        diag.tail_gap = round(max(0.0, diag.duration - keys[-1]), 3)
        gap = _max_gap(keys, diag.duration)
        diag.max_keyframe_gap = round(gap, 3) if gap != float("inf") else 9999.0
    else:
        diag.max_keyframe_gap = diag.duration or 9999.0
        diag.tail_gap = diag.duration

    # Classify.  "broken" is the case measured on the real file: a single
    # keyframe at t=0, or a gap covering most of the runtime.
    expected = max(1, int(math.floor(diag.duration)))  # one keyframe per second
    if diag.keyframes <= 1:
        diag.verdict = "broken"
        diag.seek_ok = False
        diag.reasons.append(
            "整片只有 {} 个关键帧（{}），播放器 seek 时找不到落点".format(
                diag.keyframes, "位于 0 秒" if diag.keyframes == 1 else "无")
        )
    elif gap > MAX_KEYFRAME_GAP_S + 1e-6:
        diag.verdict = "coarse"
        diag.seek_ok = False
        diag.reasons.append(
            "最大关键帧间隔 {:.2f}s，超过 {:.1f}s 的兼容上限".format(
                diag.max_keyframe_gap, MAX_KEYFRAME_GAP_S)
        )
    else:
        diag.verdict = "seekable"
        diag.reasons.append(
            "共 {} 个关键帧，最大间隔 {:.2f}s".format(diag.keyframes, diag.max_keyframe_gap)
        )

    if diag.verdict != "seekable":
        diag.reasons.append(
            "需要重编码视频轨才能插入关键帧（编码时才能写关键帧，事后无法补插）"
        )
        if diag.keyframes < expected * 0.5:
            diag.reasons.append(
                "关键帧密度仅 {:.2f}/秒，正常应为 1/秒".format(
                    diag.keyframes / diag.duration if diag.duration else 0)
            )
    if diag.bit_depth >= 10:
        diag.reasons.append(
            "{}-bit 源：部分老播放器/电视不支持，若目标设备较老建议改 8-bit".format(diag.bit_depth)
        )
    return diag


# ------------------------------------------------------------------- strategies

@dataclass
class RepairStrategy:
    id: str
    name: str
    summary: str
    video_ok: bool = True          # False => cannot help, only re-encode
    lossless: bool = False
    output_scale: float = 2.2      # expected output size / input size
    quality_note: str = ""


def available_strategies(diag: Diagnosis) -> List[RepairStrategy]:
    strategies: List[RepairStrategy] = []
    caps = get_caps()

    if diag.keyframes > 1:
        strategies.append(RepairStrategy(
            id="remux",
            name="无损重封装",
            summary="只重建容器索引，不重编码。源本身有足够关键帧时才有效。",
            lossless=True,
            output_scale=1.0,
            quality_note="画质零损失；若源关键帧不足则无效",
        ))

    same_codec_note = {
        "av1": "保持 AV1（10-bit），压缩率与源同级，体积会因关键帧变多而增大",
        "hevc": "保持 HEVC，体积会因关键帧变多而增大",
        "h264": "保持 H.264",
        "vp9": "保持 VP9",
    }.get(diag.codec, "保持原编码 {}".format(diag.codec.upper()))

    preferred = diag.codec if diag.codec in ("av1", "hevc", "h264", "vp9") else "hevc"
    if diag.codec == "av1" and caps.has_encoder("libsvtav1"):
        strategies.append(RepairStrategy(
            id="same",
            name="同编码修复（推荐）",
            summary="用原编码重压一遍，但强制每秒一个闭合关键帧。体积与画质最接近原片。",
            output_scale=2.4 if diag.bit_depth >= 10 else 2.0,
            quality_note=same_codec_note + "；属二次编码，画质有极小损失",
        ))
        strategies.append(RepairStrategy(
            id="same8",
            name="同编码 · 改 8-bit",
            summary="保持 AV1 但降到 8-bit，兼容老播放器，压缩率略降。",
            output_scale=2.1,
            quality_note="兼容性优先；10-bit→8-bit 有轻微色带风险",
        ))
    elif caps.has_encoder("libsvtav1"):
        strategies.append(RepairStrategy(
            id="av1",
            name="重压为 AV1（体积最小）",
            summary="改成 AV1 10-bit，压缩率最高，但要求播放器支持 AV1。",
            output_scale=1.6,
            quality_note="体积最优；播放器需支持 AV1",
        ))

    if caps.has_encoder("libx265") or caps.has_encoder("hevc_nvenc"):
        strategies.append(RepairStrategy(
            id="compat",
            name="转为 HEVC 8-bit（兼容最好）",
            summary="换成 8-bit H.265（hvc1 标记），几乎所有现代播放器/电视/手机都能播。",
            output_scale=2.6,
            quality_note="兼容性优先；已是 10-bit 源转 8-bit 会有轻微色带",
        ))
    strategies.append(RepairStrategy(
        id="compat264",
        name="转为 H.264（万能兜底）",
        summary="最保守的选择：任何设备都能播，代价是体积明显变大。",
        output_scale=4.5,
        quality_note="兼容性最高、体积最大",
    ))
    return strategies


def pick_default_strategy(diag: Diagnosis, strategies: List[RepairStrategy]) -> str:
    """Prefer keeping the source codec, else HEVC; never the lossless remux for a
    file we already know has too few keyframes."""
    ids = {s.id for s in strategies}
    if diag.verdict == "seekable" and "remux" in ids:
        return "remux"
    for candidate in ("same", "same8", "compat", "av1", "compat264", "remux"):
        if candidate in ids:
            return candidate
    return strategies[0].id if strategies else "compat"


# ------------------------------------------------------------------ plan build

def _gop_for(diag: Diagnosis, gop_seconds: float = 1.0) -> int:
    return compute_gop(diag.fps, gop_seconds)


def _human(num_bytes: int) -> str:
    value = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "{:.1f} {}".format(value, unit)
        value /= 1024.0
    return "{:.1f} GB".format(value)


# NOTE: a public `gop_for()` wrapper around `_gop_for` used to sit here and nothing
# called it (every caller -- including `build_repair_plan` -- uses `_gop_for` directly).
# Two names for one function is how a future reader ends up "fixing" the unused one.
# Removed in the 2026-10-05 review.


def _sample_positions(duration: float, sample_seconds: int) -> List[float]:
    if duration <= sample_seconds:
        return [0.0]
    return [max(0.0, min(duration - sample_seconds, duration * f)) for f in (0.2, 0.5, 0.8)]


def _run_sample_encodes(diag_path: str, duration: float, duration_for_gop: float,
                        head: List[str], encoder_args: List[str],
                        sample_seconds: int) -> Dict[str, Any]:
    """Encode a few short windows and report measured bitrates.

    `head` is the ffmpeg prefix (binary + banner flags) and `encoder_args` are the
    output-side options WITHOUT the output path.  Shared by the repair and the
    compression estimators so both report numbers from real encodes.
    """
    import subprocess
    from . import work
    import tempfile
    import time as _time

    starts = _sample_positions(duration, sample_seconds)
    measured: List[float] = []
    elapsed = 0.0
    tmpdir = Path(tempfile.mkdtemp(prefix="vcs_measure_"))
    try:
        for start in starts:
            out = tmpdir / "probe_{:.0f}.mp4".format(start)
            argv = (list(head)
                    + ["-ss", "{:.2f}".format(start), "-t", str(sample_seconds), "-i", diag_path]
                    + list(encoder_args) + [str(out)])
            t0 = _time.time()
            proc = work.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  encoding="utf-8", errors="replace", timeout=3600)
            elapsed += _time.time() - t0
            if proc.returncode != 0 or not out.is_file():
                return {"ok": False,
                        "error": "测算编码失败: {}".format((proc.stderr or "")[-300:])}
            info = probe(str(out))
            if info.duration > 0 and info.size_bytes > 0:
                measured.append(info.size_bytes * 8 / info.duration / 1000.0)
    finally:
        try:
            for child in tmpdir.iterdir():
                child.unlink()
            tmpdir.rmdir()
        except OSError:
            pass

    if not measured:
        return {"ok": False, "error": "未能取得有效测量结果"}
    avg = sum(measured) / len(measured)
    # Per-run fixed cost (process start, seek, muxer flush) makes short samples
    # look slower than the full encode really is; discount it before scaling.
    effective = max(0.5, sample_seconds - 1.6) * len(starts)
    speed = effective / elapsed if elapsed > 0 else 0.0
    return {
        "ok": True,
        "samples_kbps": [round(k, 1) for k in measured],
        "avg_kbps": round(avg, 1),
        "est_bytes": int(avg * 1000 / 8 * duration) if duration else 0,
        "sample_seconds": sample_seconds,
        "sample_positions": [round(s, 1) for s in starts],
        "measure_elapsed": round(elapsed, 1),
        "encode_speed": round(speed, 2),
        "est_full_seconds": int(duration / speed) if speed > 0 and duration else 0,
        "note": "体积按 {} 段真实样片外推，误差约 ±20%；用时误差较大，仅供参考".format(len(measured)),
    }


def measure_compress_cost(info: MediaInfo, plan: Plan,
                          sample_seconds: int = 8,
                          plan_override: Optional[Plan] = None) -> Dict[str, Any]:
    """Honest size/time estimate for a normal compression job.

    `plan_override` lets a caller measure a DIFFERENT plan (for example the same
    profile at a specific CRF while searching for a target size) while still
    reporting the result against the original source file.
    """
    caps = get_caps()
    if not caps.available() or not info.ok or not info.video:
        return {"ok": False, "error": "无法测算：FFmpeg 或源文件不可用"}

    video = info.video
    argv = list((plan_override or plan).argv)
    progress_at = argv.index("-progress")
    encoder_args = argv[:progress_at]
    # Strip the input side and container/video-only flags that must not be repeated
    # for a bare sample encode.
    input_at = encoder_args.index("-i")
    encoder_args = encoder_args[input_at + 2:]
    drop_with_value = {"-map", "-map_metadata", "-map_chapters", "-c:a", "-b:a", "-af",
                       "-c:s", "-c:t", "-movflags"}
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

    measured = _run_sample_encodes(
        info.path, info.duration, info.duration,
        [caps.ffmpeg, "-hide_banner", "-nostdin", "-y"], cleaned + ["-an"],
        sample_seconds)
    if not measured.get("ok"):
        return measured

    est = measured["est_bytes"]
    source_kbps = round(info.size_bytes * 8 / info.duration / 1000.0, 1) if info.duration else 0.0
    measured.update({
        "source_bytes": info.size_bytes,
        "source_kbps": source_kbps,
        "est_low": int(est * 0.8),
        "est_high": int(est * 1.3),
        "ratio": round(est / info.size_bytes, 3) if info.size_bytes else 0.0,
        "encoder": plan.encoder,
    })
    return measured


def _estimate_output(diag: Diagnosis, strategy: RepairStrategy,
                     crf: Optional[int]) -> Dict[str, int]:
    """Rough size prediction before the real thing runs.

    Deliberately pessimistic: the source was compressed by someone else with an
    unknown (usually aggressive) setting, and a seekable re-encode cannot beat an
    ultra-low-bitrate single-keyframe source on size.  The API exposes
    `measure_repair_cost()` which runs a short real encode when the user wants a
    trustworthy number instead of this estimate.
    """
    # kbps per megapixel-second, calibrated with real AV1 8-bit encodes of
    # 1280x720 animation content (see CHANGELOG for the measurement run).
    table = {
        "av1":  {34: 0.55, 38: 0.42, 42: 0.30, 46: 0.22},
        "hevc": {20: 0.70, 22: 0.90, 24: 1.20, 26: 1.60},
        "h264": {20: 1.10, 23: 1.70, 26: 2.60},
    }
    codec = {"same": "av1", "same8": "av1", "av1": "av1",
             "compat": "hevc", "compat264": "h264"}.get(strategy.id, diag.codec)
    quality = crf if crf is not None else DEFAULT_REPAIR_CRF
    curve = table.get(codec, table["av1"])
    nearest = min(curve, key=lambda k: abs(k - quality))
    kbps_per_mps = curve[nearest]

    megapixels = (diag.width or 1280) * (diag.height or 720) / 1_000_000.0
    fps = diag.fps or 30.0
    video_kbps = kbps_per_mps * megapixels * (fps / 30.0) * 1000.0
    audio_kbps = 134.0 if diag.audio_codec else 0.0
    total_bytes = (video_kbps + audio_kbps) * diag.duration / 8.0

    # A repair must never promise shrinkage.  Measured reality on a real file:
    # source 9.72 MB / 347 kbps -> repaired 11.48 MB (1.18x) at CRF 38 with 1 s
    # keyframes.  The extra keyframes cost roughly 15-40%, so the fallback estimate
    # is anchored on the source size, not on an optimistic bitrate guess.  For a
    # trustworthy number the API runs `measure_repair_cost()` on real samples.
    scaled = max(diag.size_bytes * (1.0 if strategy.lossless else 1.18), total_bytes * 0.9)
    mid = int(scaled)
    return {"low": int(mid * 0.85), "high": int(mid * 1.45), "mid": mid}


def measure_repair_cost(diag: Diagnosis, strategy_id: str,
                        options: Optional[Dict[str, Any]] = None,
                        sample_seconds: int = 8) -> Dict[str, Any]:
    """Run a SHORT real encode to get an honest size and speed for this repair.

    Samples a few positions across the file because content complexity varies
    enormously (measured spread on one real file: 227-2972 kbps depending on the
    section), then extrapolates.  This is what the UI shows as the real estimate.
    """
    import subprocess
    from . import work
    import tempfile
    import time as _time

    caps = get_caps()
    options = dict(options or {})
    if not caps.available() or not diag.ok:
        return {"ok": False, "error": "无法测量：FFmpeg 或源文件不可用"}

    full = build_repair_plan(diag, strategy_id, options)
    strategies = {s.id: s for s in available_strategies(diag)}
    strategy = strategies.get(full["strategy"]["id"], strategies.get(strategy_id))
    if strategy is None or strategy.lossless:
        return {"ok": False, "error": "无损重封装无需计时估算", "estimate": full.get("estimate")}

    duration = diag.duration or 0.0
    # Take the encoder side of the real plan instead of index-patching the whole
    # argv: the plan ends with `-progress pipe:1 -nostats <out>`, so strip that and
    # swap in a short input window.  Index arithmetic on the full list is brittle
    # (it silently overwrote `-nostats` during development).
    full_argv = list(full["argv"])
    encoder_args = full_argv[:full_argv.index("-progress")]
    input_at = encoder_args.index("-i")
    encoder_args = encoder_args[input_at + 2:]
    drop_value_flags = {"-map", "-map_metadata", "-map_chapters", "-c:a", "-b:a", "-af",
                        "-c:s", "-c:t", "-movflags"}
    cleaned: List[str] = []
    skip_next = False
    for token in encoder_args:
        if skip_next:
            skip_next = False
            continue
        if token in drop_value_flags:
            skip_next = True
            continue
        cleaned.append(token)

    measured = _run_sample_encodes(
        diag.path, duration, duration,
        [caps.ffmpeg, "-hide_banner", "-nostdin", "-y"], cleaned + ["-an"],
        sample_seconds)
    if not measured.get("ok"):
        measured["estimate"] = full.get("estimate")
        return measured

    est = measured["est_bytes"]
    source_video_kbps = (diag.size_bytes * 8 / duration / 1000.0) if duration else 0.0
    measured.update({
        "source_kbps": round(source_video_kbps, 1),
        "est_low": int(est * 0.85),
        "est_high": int(est * 1.25),
        "ratio": round(est / diag.size_bytes, 2) if diag.size_bytes else 0.0,
        "based_on_gop_seconds": clamp_gop_seconds(options.get("gop_seconds", 1.0)),
        "strategy": strategy.id,
    })
    return measured


def build_repair_plan(diag: Diagnosis, strategy_id: str, options: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Any]:
    """Return {argv, command, out_path, warnings, rules, estimate}.

    The caller (jobs.py) runs it through the same runner/verifier pipeline as a
    normal job, so a repaired file is held to the same keyframe gate.
    """
    options = dict(options or {})
    caps = get_caps()
    src = Path(diag.path)
    gop_seconds = clamp_gop_seconds(options.get("gop_seconds", 1.0))
    gop = _gop_for(diag, gop_seconds)
    crf = options.get("crf")
    crf = int(crf) if crf not in (None, "") else None
    preset = options.get('preset')
    preset = DEFAULT_REPAIR_PRESET if preset in (None, '') else preset

    strategies = {s.id: s for s in available_strategies(diag)}
    strategy = strategies.get(strategy_id)
    if strategy is None:
        raise ValueError('不可用的修复策略：{}'.format(strategy_id))

    warnings: List[str] = []
    rules: List[str] = [] if strategy.id == 'remux' else [
        "R1 关键帧间隔 {:.1f}s（-g {}）".format(gop_seconds, gop),
        "R2 闭合 GOP（禁止开放 GOP）",
    ]

    from .paths import is_same_file, resolve_out_dir, unique_path, validate_container, safe_filename

    container_out = validate_container(options.get("container") or ("mkv" if diag.container == "matroska" else "mp4"))
    suffix = {"same": "fixed", "same8": "fixed8", "av1": "av1", "compat": "hevc",
              "compat264": "h264", "remux": "remux"}.get(strategy.id, "fixed")
    # Same location policy as compression (core/paths.py): beside the source,
    # a shared default folder, or a folder chosen for this run.
    out_dir = resolve_out_dir(
        diag.path,
        str(options.get("location") or "beside"),
        str(options.get("out_dir") or ""),
        str(options.get("default_out_dir") or ""),
        kind="repair",
    )
    filename = "{}_seekfixed_{}.{}".format(safe_filename(src.stem) or 'output', suffix, container_out)
    if is_same_file(out_dir / filename, src):
        filename = "{}_seekfixed_{}_out.{}".format(src.stem, suffix, container_out)
    # Deterministic: the path shown in the command preview is the path that runs.
    # Re-running a repair of the same file lands on _2, _3, ... rather than
    # overwriting the previous result.
    out_path = unique_path(out_dir, filename)
    source = MediaInfo(diag.path, ok=diag.ok, streams=diag.streams)
    if not source.video:
        source.streams = list(source.streams) + [StreamInfo(0,'video',codec=diag.codec)]
    ov = Overrides(audio_mode=str(options.get('audio_mode') or 'copy'),
                   subtitle_mode=str(options.get('subtitle_mode') or 'keep'),
                   strip_metadata=bool(options.get('strip_metadata')))
    if ov.subtitle_mode == 'burn':
        raise ValueError('修复模式请保留或丢弃字幕；烧录请使用压缩模式')
    argv: List[str] = [caps.ffmpeg, "-hide_banner", "-nostdin", "-n", "-i", diag.path]
    argv += ["-map", "0:{}".format(source.video.index)]
    argv += _audio_args(source, container_out, ov)
    if not options.get("strip_metadata"):
        argv += ["-map_metadata", "0", "-map_chapters", "0"]
    else:
        argv += ['-map_metadata', '-1', '-map_chapters', '-1']
    argv += _subtitle_args(source, container_out, ov)
    if container_out == 'mkv':
        argv += ['-map', '0:t?', '-c:t', 'copy']

    # ---- video
    if strategy.id == "remux":
        argv += ["-c:v", "copy"]
        rules.append("R-remux 视频轨直接拷贝（无损）")
    elif strategy.id in ("same", "same8"):
        if caps.has_encoder("libsvtav1"):
            argv += ["-c:v", "libsvtav1", "-preset", str(preset)]
            argv += ["-crf", str(crf if crf is not None else DEFAULT_REPAIR_CRF)]
            argv += ["-pix_fmt", "yuv420p" if strategy.id == "same8" else "yuv420p10le"]
            argv += ["-svtav1-params", _svt_params(options, gop_seconds)]
        else:
            strategy = strategies["compat"]
            warnings.append("本机缺少 libsvtav1，已改用 HEVC")
            argv += ["-c:v", "hevc_nvenc" if caps.has_encoder("hevc_nvenc") else "libx265",
                     "-preset", "p5" if caps.has_encoder("hevc_nvenc") else "medium"]
            argv += ["-rc", "vbr", "-cq", str(crf if crf is not None else 22), "-b:v", "0"] \
                if caps.has_encoder("hevc_nvenc") else ["-crf", str(crf if crf is not None else 22)]
            argv += ["-pix_fmt", "yuv420p", "-profile:v", "main", "-tag:v", "hvc1"]
    elif strategy.id in ("av1", "compat", "compat264"):
        if strategy.id == "av1" and caps.has_encoder("libsvtav1"):
            argv += ["-c:v", "libsvtav1", "-preset", str(preset)]
            argv += ["-crf", str(crf if crf is not None else DEFAULT_REPAIR_CRF), "-pix_fmt", "yuv420p10le"]
            argv += ["-svtav1-params", _svt_params(options, gop_seconds)]
        elif strategy.id == "compat264":
            if caps.has_encoder("h264_nvenc"):
                argv += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                         "-cq", str(crf if crf is not None else 21), "-b:v", "0"]
            else:
                argv += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf if crf is not None else 21)]
            argv += ["-pix_fmt", "yuv420p", "-profile:v", "high"]
        else:
            if caps.has_encoder("hevc_nvenc"):
                argv += ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr",
                         "-cq", str(crf if crf is not None else 22), "-b:v", "0"]
            else:
                argv += ["-c:v", "libx265", "-preset", "medium", "-crf", str(crf if crf is not None else 22),
                         "-x265-params", "open-gop=0:repeat-headers=1:keyint={}".format(gop)]
            argv += ["-pix_fmt", "yuv420p", "-profile:v", "main", "-tag:v", "hvc1"]

    if strategy.id == "remux":
        pass
    else:
        argv += ["-g", str(gop)]
        argv += ["-force_key_frames", "expr:gte(t,n_forced*{:g})".format(gop_seconds)]

    encoder = next((argv[i+1] for i, token in enumerate(argv[:-1]) if token == '-c:v'), '')
    validate_video_container(encoder, diag.codec, container_out)
    if encoder.endswith('_nvenc'):
        argv += ['-forced-idr', '1']
    if container_out not in _MP4_FAMILY:
        argv = [token for i,token in enumerate(argv) if token != '-tag:v' and (i == 0 or argv[i-1] != '-tag:v')]
    if source.audio_streams:
        rules.append('R4／R5 音轨逐条按容器适配')
    warnings += track_warnings(source, container_out, ov)

    if container_out in _MP4_FAMILY:
        argv += ["-movflags", "+faststart"]
        rules.append("R3 MP4 faststart")
    if options.get("maxrate_kbps"):
        kbps = int(options["maxrate_kbps"])
        argv += ["-maxrate", "{}k".format(kbps), "-bufsize", "{}k".format(kbps * 2)]
        rules.append("R13 码率上限 {} kbps（抑制关键帧增多带来的体积膨胀）".format(kbps))

    argv += ["-progress", "pipe:1", "-nostats", str(out_path)]

    estimate = _estimate_output(diag, strategy, crf)
    if strategy.id != 'remux':
        warnings.append(
        "修复需要重编码视频轨，体积通常会比原片增加约 15–40%（关键帧变多的代价）；"
        "预计 {}–{} —— 点「精确测算」可跑 3 段真实样片给出准确数字".format(
            _human(estimate["low"]), _human(estimate["high"])))
    if diag.bit_depth >= 10 and strategy.id in ("compat", "compat264", "same8"):
        warnings.append("{}-bit 转 8-bit 可能出现轻微色带".format(diag.bit_depth))

    import shlex
    return {
        "argv": argv,
        "command": " ".join(shlex.quote(a) for a in argv),
        "out_path": str(out_path),
        "warnings": warnings,
        "rules_applied": rules,
        "gop": gop,
        "encoder": next((argv[i + 1] for i, flag in enumerate(argv[:-1]) if flag == '-c:v'), ''),
        "strategy": asdict(strategy),
        "estimate": estimate,
    }
