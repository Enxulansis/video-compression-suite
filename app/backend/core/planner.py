"""Planner: (MediaInfo, Profile, Overrides) -> ffmpeg argv.

Pure function.  Same input always produces the same argv, so the command shown
in the UI is exactly the command that runs.

This module is where the project's hard rules live:

  R1  keyframe interval <= 2 s (default 1 s, or 1 keyframe per second)
  R2  closed GOP only -- AV1 must use irefresh-type=2, x265 open-gop=0
  R3  MP4/MOV always +faststart
  R4  audio is copied unless the container cannot hold the source codec
  R7  every source track is mapped (video/audio/subtitle/attachment)
  R9  the output path can never equal the input path

See specs/ENCODING_PROFILES.md for the rationale and the measured evidence.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from .probe import MediaInfo
from .crop import apply_crop_filter
from . import audio
from . import runtime

PROFILES_PATH = runtime.profiles_path()

# Container capability tables -------------------------------------------------
_MP4_FAMILY = {'mp4', 'mov', 'm4v', '3gp', '3g2', 'f4v'}
_AUDIO_OK_IN = {
    "mp4": {"aac", "mp3", "ac3", "eac3", "alac"},
    "mkv": {"aac", "mp3", "ac3", "eac3", "dts", "truehd", "flac", "opus", "vorbis", "pcm_s16le", "pcm_s24le"},
    "webm": {"opus", "vorbis"},
}
_AUDIO_TARGET = {"mp4": ("aac", "192k"), "mkv": ("libopus", "128k"), "webm": ("libopus", "128k")}
# libopus' default mapping follows the Vorbis layouts for 1..8 channels.
_OPUS_LAYOUTS = {'mono', 'stereo', '3.0', 'quad', '5.0', '5.1', '6.1', '7.1'}
_SIDE_SURROUND_LAYOUTS = {'5.0(side)': '5.0', '5.1(side)': '5.1'}
_SUB_OK_IN = {
    "mp4": {"mov_text"},
    "mkv": {"subrip", "ass", "ssa", "webvtt", "hdmv_pgs_subtitle", "dvd_subtitle"},
    "webm": {"webvtt"},
}
_TEXT_SUBS = {"subrip", "ass", "ssa", "webvtt", "mov_text", "srt", 'text'}

# Codecs that are risky to copy into MP4 without retagging
_H265_TAG = "hvc1"


@dataclass
class Overrides:
    """User-supplied deviations from the profile defaults."""

    out_dir: str = ""                # custom directory (location == "custom")
    default_out_dir: str = ""        # the app-wide default folder (location == "default")
    location: str = "beside"         # beside | default | custom -- see core/paths.py
    name_template: str = "{stem}_{profile}_{height}p"
    container: str = ""              # override profile container
    crf: Optional[int] = None
    preset: Optional[str] = None     # override the profile's speed preset
    #: A7 -- "auto" lets an extreme tier raise its own encoder effort (A5/A6 do);
    #: "off" keeps the profile's preset. An explicit `preset` always wins.
    effort: str = "auto"
    max_long_edge: int = 0           # 0 = keep original
    scale_percent: int = 0           # 0 = keep original
    gop_seconds: float = 1.0         # R1: <= 2.0 enforced
    audio_mode: Optional[str] = None # None = profile default; copy | transcode | drop
    #: Loudness target in LUFS; 0 = off. Anything else forces an audio re-encode:
    #: normalising cannot be done with `-c:a copy`, so the UI must say so (see
    #: core/audio.py and the job warnings).
    loudness: float = 0.0
    #: True-peak ceiling in dBTP. 0 = use the preset for `loudness`.
    loudness_tp: float = 0.0
    #: Loudness measured on THIS source, filled in by the caller before planning.
    #: Without it the filter degrades to the single-pass dynamic form, which alters
    #: dynamics -- the planner never measures by itself, so it stays pure.
    loudness_measured: Dict[str, Any] = field(default_factory=dict)
    subtitle_mode: str = "keep"      # keep | drop | burn
    strip_metadata: bool = False
    deinterlace: bool = False
    denoise: bool = False
    #: Detected black-bar removal, as "w:h:x:y" (see core/crop.py). Set by the caller
    #: after a detection run -- the planner itself never shells out, so it stays pure.
    crop: str = ""
    #: Ask for per-file black-bar detection. The manager resolves this into `crop`
    #: for each source, because bars differ from video to video.
    autocrop: bool = False
    hdr_to_sdr: bool = False
    ten_bit: Optional[bool] = None   # None = profile default
    #: Copy the source's file timestamps onto the output (default on). Measured gap:
    #: the container's `creation_time` is preserved by `-map_metadata 0`, but the FILE's
    #: mtime was the encode time -- so a compressed library sorted by date showed every
    #: new file as "today" instead of the shooting date.
    keep_timestamps: bool = True
    extra_args: List[str] = field(default_factory=list)
    allow_unsafe: bool = False       # expert mode: relax R1/R2 (never R3/R9)
    intent: Dict[str, Any] = field(default_factory=dict)  # simple-mode request; resolved before encoding

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Plan:
    argv: List[str]
    command: str
    out_path: str
    warnings: List[str] = field(default_factory=list)
    rules_applied: List[str] = field(default_factory=list)
    gop: int = 0
    est_output_bytes: int = 0
    encoder: str = ""
    #: Estimated peak COMMITTED memory of the encode (MB) + the basis of the number.
    #: Computed here because this is the only place that knows the encoder INPUT geometry
    #: (i.e. after the profile's own resolution cap), which is what the buffers scale with.
    est_memory_mb: int = 0
    est_memory_basis: str = ""
    profile_id: str = ""
    resolved_overrides: Dict[str, Any] = field(default_factory=dict)
    intent: Dict[str, Any] = field(default_factory=dict)
    decision: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "argv": self.argv,
            "command": self.command,
            "out_path": self.out_path,
            "warnings": self.warnings,
            "rules_applied": self.rules_applied,
            "gop": self.gop,
            "est_output_bytes": self.est_output_bytes,
            "encoder": self.encoder,
            "est_memory_mb": self.est_memory_mb,
            "est_memory_basis": self.est_memory_basis,
            "profile_id": self.profile_id,
            "resolved_overrides": self.resolved_overrides,
            "intent": self.intent,
            "decision": self.decision,
        }


# --------------------------------------------------------------------- profiles

_PROFILES_CACHE: Optional[List[Dict[str, Any]]] = None


def load_profiles(refresh: bool = False) -> List[Dict[str, Any]]:
    global _PROFILES_CACHE
    if _PROFILES_CACHE is None or refresh:
        with open(PROFILES_PATH, "r", encoding="utf-8") as fh:
            _PROFILES_CACHE = json.load(fh)["profiles"]
    return _PROFILES_CACHE


def get_profile(profile_id: str) -> Dict[str, Any]:
    for prof in load_profiles():
        if prof["id"].lower() == str(profile_id).lower():
            return prof
    raise KeyError("未知档位: {}".format(profile_id))


SIMPLE_QUALITY = {
    "h264_nvenc": (22, 26, 30), "libx264": (20, 23, 26),
    "hevc_nvenc": (24, 28, 32), "libx265": (24, 28, 32),
    "av1_nvenc": (28, 32, 36), "libsvtav1": (32, 36, 40),
}


def validate_intent(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("简单模式意图必须是对象")
    if not raw:
        return {}
    if set(raw) - {"goal", "compatibility", "slow_av1"}:
        raise ValueError("不认识的简单模式选项")
    goal = raw.get("goal", "balanced")
    compatibility = raw.get("compatibility", "modern")
    slow = raw.get("slow_av1", False)
    if goal not in ("light", "balanced", "strong"):
        raise ValueError("未知压缩目标")
    if compatibility not in ("broad", "modern", "av1"):
        raise ValueError("未知兼容偏好")
    if type(slow) is not bool:
        raise ValueError("slow_av1 必须是布尔值")
    return {"goal": goal, "compatibility": compatibility, "slow_av1": slow}


def resolve_selection(info: MediaInfo, profile: Dict[str, Any], ov: Overrides,
                      caps) -> tuple:
    """Pure per-file selection. Memory pressure never changes the chosen quality."""
    intent = validate_intent(ov.intent)
    if not intent:
        return profile, ov, []
    if not info.ok or not info.video:
        raise PlanError("该文件没有可用的视频流")
    goal, compat = intent["goal"], intent["compatibility"]
    hdr = info.is_hdr
    if hdr and compat == "broad":
        raise PlanError("待决定：广泛播放的 H.264 与保留 HDR 冲突，请改为现代设备或手动选择 HDR→SDR")
    if hdr and info.video.bit_depth > 10:
        raise PlanError("待决定：简单模式不能保留超过 10-bit 的 HDR 精度，请使用手动参数")
    paths = [("P1", "P2")] if compat == "broad" else [("P3", "P7")]
    if compat == "av1" and goal != "light":
        paths.insert(0, ("P4", "P5" if goal == "strong" and intent["slow_av1"] else ""))
    if not hdr and compat != "broad":
        paths.append(("P1", "P2"))
    chosen = None
    for pair in paths:
        for pid in pair:
            if not pid:
                continue
            candidate = get_profile(pid)
            enc = candidate["encoder"]
            if not caps.has_encoder(enc):
                continue
            if enc.endswith("_nvenc") and not getattr(caps, "hardware_probes", {}).get(enc, {}).get("ok"):
                continue  # registration alone does not prove a device is runnable
            mw, mh = encoder_min_dimensions(enc)
            if info.video.display_width < mw or info.video.display_height < mh:
                continue
            fmt = "p010le" if hdr and enc.endswith("_nvenc") else "yuv420p10le" if hdr else "yuv420p"
            if not caps.supports_pix_fmt(enc, fmt):
                continue
            chosen = dict(candidate)
            break
        if chosen:
            break
    if chosen is None:
        raise PlanError("待决定：没有满足兼容性、位深和尺寸要求的可运行编码器")
    enc = chosen["encoder"]
    quality = SIMPLE_QUALITY[enc][("light", "balanced", "strong").index(goal)]
    complex_content = (any(s.kind == "attachment" for s in info.streams)
                       or any(s.codec not in ("subrip", "srt", "mov_text", "text")
                              for s in info.subtitle_streams)
                       or any(s.codec not in ("aac", "mp3", "ac3", "eac3", "alac")
                              for s in info.audio_streams))
    container = "mkv" if complex_content else "mp4"
    # Keep only destination choices; hidden/manual controls cannot leak into simple mode.
    resolved = Overrides(out_dir=ov.out_dir, default_out_dir=ov.default_out_dir,
                         location=ov.location, name_template=ov.name_template,
                         container=container, crf=quality,
                         preset="6" if enc == "libsvtav1" else "medium" if enc in ("libx264", "libx265") else chosen["preset"],
                         effort="off", ten_bit=hdr, audio_mode="copy")
    reasons = ["自动选择 {} · {} {}（质量参数尚待样片标定）".format(enc, "CQ" if enc.endswith("_nvenc") else "CRF", quality),
               "保持原分辨率、帧率、HDR、音轨／字幕、元数据和文件时间"]
    if complex_content:
        reasons.append("复杂音轨、字幕或附件需要保留，选择 MKV")
    if enc.startswith("lib"):
        reasons.append("没有满足条件的硬件路径，使用软件编码，等待时间可能较长")
    return chosen, resolved, reasons


# ------------------------------------------------------------------ helpers

#: R1 limits, in seconds. Exposed so the API, the UI and the rules text all quote the
#: same numbers instead of each re-deriving them (which is how the front end ended up
#: printing an unclamped value while the encoder used the clamped one).
GOP_SECONDS_MIN = 0.5
GOP_SECONDS_MAX = 2.0


class PlanError(Exception):
    """A request the planner refuses to turn into a command.

    Distinct from a generic ValueError so the HTTP layer can answer 400 ("your request
    asks for something impossible") instead of 500 ("the server broke"). Every message
    is written for the user, in Chinese, and says what to change.
    """


def clamp_gop_seconds(gop_seconds: Any) -> float:
    """R1 -- the single place `gop_seconds` is clamped.

    `compute_gop` clamps too, but it only returns the frame COUNT; callers that need the
    seconds value (the `force_key_frames` expression, the SVT-AV1 `keyint` parameter, the
    rules text) were each clamping -- or forgetting to clamp -- on their own. Passing 5
    produced `keyint=5s` (a 5-second GOP) while the reported rule still said "1.0s".
    """
    try:
        value = float(gop_seconds)
    except (TypeError, ValueError):
        return 1.0
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return 1.0
    return max(GOP_SECONDS_MIN, min(GOP_SECONDS_MAX, value))


def compute_gop(fps: float, gop_seconds: float) -> int:
    """R1 -- one keyframe every `gop_seconds`, clamped to [0.5 s, 2 s]."""
    seconds = clamp_gop_seconds(gop_seconds)
    if fps and fps > 0:
        gop = int(round(fps * seconds))
    else:
        # VFR / unknown frame rate: a frame count cannot express a time limit here, so
        # `-g` is only a backstop. `_video_args` always adds the time-based
        # `-force_key_frames` expression, which holds for VFR too -- otherwise 48 frames
        # at 15 fps would be 3.2 s between keyframes and the seek rule would be broken.
        gop = 48
    return max(8, min(600, gop))


#: Output flags an expert `extra_args` entry may NOT touch: each one can silently undo a
#: hard invariant (R1 keyframe interval, R2 closed GOP, R3 faststart, R7 track mapping,
#: R9 never overwrite the source). Anything else stays allowed -- this is an escape
#: hatch, not a sandbox.
_TABOO_EXTRA_FLAGS = {
    "-g": "关键帧间隔（R1）",
    "-g:v": "关键帧间隔（R1）",
    "-keyint_min": "最小关键帧间隔（R1）",
    "-force_key_frames": "强制关键帧（R1）",
    "-sc_threshold": "场景切换阈值（会改变关键帧位置）",
    "-no-scenecut": "关闭场景切换检测（会改变关键帧位置）",
    "-c:v": "视频编码器（会绕过档位与安全约束）",
    "-codec:v": "视频编码器（会绕过档位与安全约束）",
    "-map": "轨道映射（R7）",
    "-map_metadata": "元数据映射",
    "-map_chapters": "章节映射",
    # `-movflags` is NOT listed here: `+faststart` merely restates R3 (and is allowed),
    # while `-faststart` removes it (and is refused). Only the VALUE tells them apart,
    # so it is handled explicitly below.
    "-y": "覆盖确认（R9 绝不覆盖源文件）",
    "-i": "输入文件",
    "-progress": "进度输出通道",
    "-vf": "视频滤镜（与档位滤镜链冲突）",
    "-filter:v": "视频滤镜（与档位滤镜链冲突）",
}
#: Substrings that mean the same thing when they appear inside a `*-params` value.
_TABOO_EXTRA_SUBSTRINGS = ("keyint", "irefresh", "open-gop")

# Every allowed option consumes exactly one value. Unknown arity can hide a second
# output (e.g. a zero-argument option followed by a path), so refuse it explicitly.
_EXTRA_VALUE_OPTIONS = {
    '-preset', '-crf', '-cq', '-qp', '-q', '-qscale', '-b', '-maxrate', '-minrate',
    '-bufsize', '-threads', '-filter_threads', '-pix_fmt', '-tune', '-profile',
    '-level', '-tag', '-s', '-r', '-fps_mode', '-rc', '-spatial-aq', '-temporal-aq',
    '-aq-strength', '-bf', '-refs', '-look_ahead', '-rc-lookahead',
    '-svtav1-params', '-x264-params', '-x265-params', '-aom-params',
    '-g', '-keyint_min', '-force_key_frames', '-sc_threshold', '-no-scenecut',
    '-c', '-codec', '-vcodec', '-vf', '-filter', '-movflags',
}
_EXTRA_IO_OPTIONS = {'-y', '-n', '-i', '-progress', '-map', '-map_metadata',
                     '-map_chapters', '-f', '-filter_complex', '-passlogfile', '-report'}
_EXTRA_PARAM_KEYS = {'keyint', 'min-keyint', 'irefresh-type', 'open-gop', 'scenecut',
                     'repeat-headers', 'tune', 'film-grain', 'film-grain-denoise',
                     'enable-overlays', 'scd', 'aq-mode', 'aq-strength', 'pools',
                     'frame-threads', 'lookahead-slices', 'rc-lookahead', 'bframes',
                     'ref', 'deblock', 'sao', 'strong-intra-smoothing', 'lossless'}


def option_base(flag: str) -> str:
    return flag.split(':', 1)[0].lower()


def check_extra_args(extra_args: Any, allow_unsafe: bool = False) -> List[str]:
    """Validate an expert `extra_args` list; return warnings, raise on invariant damage.

    `extra_args` is appended AFTER the invariant flags, and ffmpeg is last-one-wins, so
    `-g 9999` there silently replaced the computed keyframe interval: the plan still said
    "R1 关键帧间隔 1.0s" while the encoder wrote a 9999-frame GOP -- exactly the freeze the
    whole project exists to avoid. Refusing is the only honest answer; `allow_unsafe`
    (专家模式) unlocks it with a warning instead, because then the user has been told.

    The list is walked as flag/value PAIRS, not token by token, because the thing being
    judged is the pair: `-movflags +faststart` restates what we already do (allowed),
    while `-movflags -faststart` removes it (refused), and only the value tells them
    apart.
    """
    warnings: List[str] = []
    if not extra_args:
        return warnings
    if not isinstance(extra_args, (list, tuple)):
        raise PlanError("extra_args 必须是字符串数组")

    bad: List[str] = []
    items = [str(a) for a in extra_args]
    index = 0
    while index < len(items):
        token = items[index]
        if not token.strip() or not token.startswith('-'):
            raise PlanError("extra_args 只允许选项和值，不能包含额外输出路径：{}".format(token))
        value = ""
        if "=" in token:
            raise PlanError("额外参数请将选项和值分开填写：{}".format(token))
        else:
            head = token
        head = head.strip().lower()
        base = option_base(head)
        if base in _EXTRA_IO_OPTIONS or base not in _EXTRA_VALUE_OPTIONS:
            raise PlanError("额外参数不支持此选项或写入通道：{}".format(head))
        if index + 1 >= len(items) or not items[index + 1].strip():
            raise PlanError("额外参数缺少值：{}".format(head))
        value = items[index + 1]
        if value.startswith('-') and not re.fullmatch(r'-\d+(?:\.\d+)?', value) and head != '-movflags':
            raise PlanError("额外参数缺少有效值：{}".format(head))
        index += 1
        if base in ('-vf', '-filter'):
            raise PlanError('视频滤镜请使用缩放、裁边等专用设置，不能通过额外参数追加')
        if base.endswith('-params'):
            keys = {p.split('=', 1)[0].lower() for p in value.split(':')}
            unknown = keys - _EXTRA_PARAM_KEYS
            if unknown:
                raise PlanError('不支持的编码器参数（可能包含文件通道）：{}'.format('、'.join(sorted(unknown))))
        if ':' in head and head.split(':', 1)[1] not in ('v', 'v:0', '0'):
            raise PlanError("额外参数只支持首条视频流：{}".format(head))
        pair = "{} {}".format(head, value).strip()

        if base in ('-c', '-codec', '-vcodec'):
            bad.append('{}（视频编码器会绕过档位与安全约束）'.format(pair))
        elif base in ('-filter', '-vf'):
            bad.append('{}（与档位滤镜链冲突）'.format(pair))
        elif base in _TABOO_EXTRA_FLAGS:
            bad.append("{}（{}）".format(pair, _TABOO_EXTRA_FLAGS[base]))
        elif base == "-movflags":
            # Peek even at a token starting with `-`: `-faststart` (remove the index
            # relocation) is exactly the damaging form, and it looks like a flag.
            peek = value or (items[index + 1] if index + 1 < len(items) else "")
            if "-faststart" in peek.lower():
                raise PlanError('额外参数不能移除 R3 faststart')
        elif base.endswith("-params") and any(
                token in value.lower() for token in _TABOO_EXTRA_SUBSTRINGS):
            bad.append("{}（会改动关键帧/GOP 设置）".format(pair))
        index += 1

    if not bad:
        return warnings
    if allow_unsafe:
        warnings.append("专家模式：额外参数改动了安全约束 -- " + "、".join(bad))
        return warnings
    raise PlanError(
        "额外参数不能改动关键帧/GOP/索引等安全设置：{}。"
        "这些设置是【输出能正常拖动进度条】的前提；确实需要时请开启专家模式"
        "（allow_unsafe），届时会在任务报告里明确警告".format("、".join(bad)))


def _round_up_even(value: int) -> int:
    """Largest even number <= value (keeps a resize at or under its cap).

    Named for history, not for what it does: this FLOORS to even, deliberately, because
    a resolution cap means "not bigger than this" and rounding up would exceed it by a
    pixel. The name has already fooled one reader (who used it to raise a value to a
    minimum and got 144 for 145), hence `_ceil_even` below and this note.
    """
    return value - (value % 2)


def _ceil_even(value: float) -> int:
    """Smallest even number >= value. The opposite direction, for minimums."""
    whole = int(value)
    if whole < value:
        whole += 1
    return whole + (whole % 2)


def compute_scale(info: MediaInfo, ov: Overrides) -> Optional[tuple]:
    """Return (w, h) when a resize is required, else None.  Never upscales.

    The arithmetic uses the DISPLAY size, not the stored size. ffmpeg autorotates: the
    frames entering the filter chain are already turned the right way up, so a portrait
    phone video (stored 640x360, rotation 90, shown 360x640) must be measured as tall.
    Using the stored size produced `scale=180:100` for a portrait clip -- a 1.8:1 box
    holding a 9:16 picture, i.e. a visibly squashed video. Measured, not theorised:
    see `_temp/probe/f7` and `tests/test_metadata.py`.
    """
    video = info.video
    if not video or not video.width or not video.height:
        return None
    w, h = video.display_width, video.display_height
    if ov.scale_percent and 0 < ov.scale_percent < 100:
        w = _round_up_even(int(w * ov.scale_percent / 100))
        h = _round_up_even(int(h * ov.scale_percent / 100))
    if ov.max_long_edge and max(w, h) > ov.max_long_edge:
        factor = ov.max_long_edge / float(max(w, h))
        w = _round_up_even(int(w * factor))
        h = _round_up_even(int(h * factor))
    if (w, h) == (video.display_width, video.display_height):
        return None
    if w < 32 or h < 32:
        return None
    return (w, h)


def profile_max_edge(profile: Dict[str, Any]) -> int:
    """A profile's own resolution ceiling (`max_edge`), or 0 for "no ceiling"."""
    try:
        return max(0, int(profile.get("max_edge") or 0))
    except (TypeError, ValueError):
        return 0


def effective_max_edge(profile: Dict[str, Any], ov: Overrides) -> int:
    """Whichever ceiling is stricter.

    A profile named "720p" promises a resolution ceiling, so a user's larger value
    must not silently override it; a user's *smaller* value is still honoured. Zero
    means "not set" on both sides.
    """
    values = [v for v in (profile_max_edge(profile),
                          max(0, int(ov.max_long_edge or 0))) if v > 0]
    return min(values) if values else 0


def effective_fps(info: MediaInfo, profile: Dict[str, Any]) -> float:
    """Output frame rate: the source's, capped by the profile's `fps_cap`."""
    source = float(info.video.fps) if info.video and info.video.fps else 0.0
    try:
        cap = float(profile.get("fps_cap") or 0)
    except (TypeError, ValueError):
        cap = 0.0
    if cap > 0 and source > cap:
        return cap
    return source


def build_filters(info: MediaInfo, ov: Overrides, scale: Optional[tuple],
                  fps_cap: float = 0.0) -> List[str]:
    chain: List[str] = []
    # Crop first: scaling before cropping would resample the black bars into the
    # picture and change the aspect ratio.
    chain = apply_crop_filter(chain, ov.crop)
    if ov.deinterlace:
        chain.append("bwdif")
    # Frame-rate reduction goes before the resize: it also cuts the number of frames
    # the scaler has to touch. It is opt-in per profile because "帧率减半=体积减半"
    # is false (measured: 2.7%–4.4% average, and worse on high-motion content).
    source_fps = float(info.video.fps) if info.video and info.video.fps else 0.0
    if fps_cap and source_fps and source_fps > fps_cap + 0.01:
        chain.append("fps={:g}".format(fps_cap))
    if ov.hdr_to_sdr:
        chain.append("zscale=t=linear:npl=100,format=gbrpf32le,"
                     "zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                     "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    if ov.denoise:
        chain.append("hqdn3d=4:3:6:4.5")
    if scale:
        chain.append("scale={}:{}:flags=lanczos".format(scale[0], scale[1]))
    return chain


def _pick_encoder(profile: Dict[str, Any], caps) -> tuple:
    """Return (encoder, fell_back: bool)."""
    primary = profile["encoder"]
    if primary == "copy":
        return primary, False
    if caps.has_encoder(primary):
        return primary, False
    fb = profile.get("fallback_encoder")
    if fb and caps.has_encoder(fb):
        return fb, True
    return primary, False  # let ffmpeg fail loudly with a clear message


def estimate_output_bytes(info: MediaInfo, profile: Dict[str, Any],
                          ov: Optional[Overrides] = None,
                          scale: Optional[tuple] = None) -> int:
    """Rough output size from the source size and the profile's compression class.

    Extracted so the batch total (core/batch.py) and the per-file plan use ONE formula:
    two implementations of "expected size" would drift, and the per-file card would then
    disagree with the batch total sitting right above it.

    The size comes from `expected_fraction` -- expected output ÷ source -- a SINGLE
    canonical field. The old code read `ratio` and looked it up in a class table
    (`{1: 0.85 ... 6: 0.14}`), which worked for the P tiers (where `ratio` is a class code)
    and silently broke the whole A ladder (where `ratio` is the research's *compression
    multiple*: 3, 6, 9, 14, 21, 38, 58). Keys 3 and 6 happened to exist, everything else
    fell back to 0.40 -- so A2–A6 all predicted the SAME size, and every one of them
    predicted 40% where the balanced P3 predicted 32%, i.e. the extreme ladder looked
    BIGGER than the balanced profile. Reported by the user as exactly those two symptoms.
    """
    if not info.size_bytes:
        return 0
    est = int(info.size_bytes * expected_fraction(profile))
    if ov is not None and ov.crf is not None and profile.get("crf") is not None:
        # Uncalibrated adjustment, shared by simple/manual plans and batch estimates.
        est = int(est * 2 ** ((float(profile["crf"]) - float(ov.crf)) / 6.0))
    if scale is None and ov is not None:
        scale = compute_scale(info, ov)
    if scale and info.video:
        # The profile's fraction is calibrated AT THE PROFILE'S OWN resolution cap (the
        # ladder multiples already include the downscale), so the geometry correction must
        # be measured against that cap rather than against the source -- otherwise the
        # profile's own downscale would be counted twice.
        baseline = profile_scale(info, profile)
        base_pixels = (baseline[0] * baseline[1] if baseline
                       else info.video.display_width * info.video.display_height)
        factor = (scale[0] * scale[1]) / float(max(1, base_pixels))
        if factor < 0.999:
            est = int(est * max(0.15, factor ** 0.85))
    return est


#: Legacy class table: a P-tier `ratio` (1..6) indexes it. Kept ONLY as a fallback for
#: profiles that declare no `expected_fraction` (e.g. a hand-written user profile file).
_RATIO_CLASS_FRACTION = {1: 0.85, 2: 0.45, 3: 0.40, 4: 0.32, 5: 0.22, 6: 0.14}
#: Used when a profile declares none of the three fields. Middle of the P ladder.
_DEFAULT_FRACTION = 0.40


def expected_fraction(profile: Dict[str, Any]) -> float:
    """Expected output size ÷ source size for a profile, as a fraction in (0, 1].

    Preference order: explicit `expected_fraction`, then the legacy class code, then the
    ladder's `expected_multiple` (1 ÷ multiple), then the default.
    """
    raw = profile.get("expected_fraction")
    if raw is not None:
        try:
            return min(1.0, max(0.005, float(raw)))
        except (TypeError, ValueError):
            return _DEFAULT_FRACTION
    ratio = profile.get("ratio")
    if ratio is not None:
        try:
            code = int(ratio)
        except (TypeError, ValueError):
            code = 0
        if code in _RATIO_CLASS_FRACTION:
            return _RATIO_CLASS_FRACTION[code]
    multiple = profile.get("expected_multiple")
    if multiple:
        try:
            value = float(multiple)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return min(1.0, max(0.005, 1.0 / value))
    return _DEFAULT_FRACTION


def profile_scale(info: MediaInfo, profile: Dict[str, Any]) -> Optional[tuple]:
    """The resize this profile would apply ON ITS OWN (ignoring user overrides)."""
    edge = profile_max_edge(profile)
    if not edge:
        return None
    return compute_scale(info, Overrides(max_long_edge=edge))


def _video_args(encoder: str, profile: Dict[str, Any], overrides: Overrides,
                gop: int, pix_fmt: str, fps: float,
                gop_seconds: float = 1.0,
                out_size: Optional[tuple] = None) -> tuple:
    """Video codec arguments, plus the warnings the caller must surface.

    Returns `(args, warnings)`. It used to return bare args, which left the H.264 level
    correction below with nowhere to report itself.
    """
    warnings: List[str] = []
    args: List[str] = ["-c:v", "copy"] if encoder == "copy" else ["-c:v", encoder]
    if encoder == "copy":
        return args, warnings

    crf = overrides.crf if overrides.crf is not None else profile.get("crf")
    preset = overrides.preset if overrides.preset is not None else profile.get("preset")
    # A7 -- the extreme tiers raise the encoder's effort on their own. Measured on this
    # machine (720p CRF46, same source as reference): preset 6 -> 2 gives a **14.3%
    # smaller** file AND a slightly BETTER VMAF (93.43 -> 94.62) for 1.8x the time; the
    # gain is size and quality, the price is only time. A user who picked "极限压缩" has
    # already chosen size over speed, so the default is to take it -- announced in the
    # plan warnings, and switchable off (`effort: off`) or overridden (`preset`).
    auto_effort = 0
    if overrides.preset is None and str(overrides.effort or "auto") != "off":
        try:
            auto_effort = int(profile.get("auto_effort") or 0)
        except (TypeError, ValueError):
            auto_effort = 0
        if auto_effort and encoder == "libsvtav1":
            preset = auto_effort

    if encoder.endswith("_nvenc"):
        # NVENC: -rc vbr + -cq <n> is the CRF-equivalent constant-quality mode.
        args += ["-preset", str(preset), "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
        args += ['-forced-idr', '1']
    elif encoder == "libsvtav1":
        args += ["-preset", str(preset), "-crf", str(crf)]
    elif encoder.startswith("lib"):
        args += ["-preset", str(preset), "-crf", str(crf)]

    # R1 -- explicit, always. The frame count from `-g` AND the time expression from
    # `-force_key_frames` are both needed: `-g` alone drifts on VFR content (the count is
    # frames, the promise is seconds) and the expression is skipped entirely when the
    # frame rate is unknown. Together the gap is bounded by whichever fires first, which
    # is what "至多 2 秒一个关键帧" has to mean.
    args += ["-g", str(gop)]
    seconds = clamp_gop_seconds(gop_seconds)
    args += ["-force_key_frames", "expr:gte(t,n_forced*{:g})".format(seconds)]

    if pix_fmt:
        args += ["-pix_fmt", pix_fmt]

    extra = dict(profile.get("extra") or {})
    if encoder in ('libx265', 'hevc_nvenc'):
        extra['profile:v'] = 'main10' if pix_fmt in ('yuv420p10le', 'p010le') else 'main'
    if encoder.endswith("_nvenc"):
        # profile-specific NVENC knobs already applied above; keep tag/aq only
        extra.pop("rc", None)
        extra.pop("cq", None)
        extra.pop("b:v", None)
    if encoder == "libsvtav1":
        # R2 -- closed GOP is mandatory for seekable AV1.
        params = extra.get("svtav1-params", "")
        kv = dict(item.split("=", 1) for item in params.split(":") if "=" in item)
        kv["irefresh-type"] = "2"
        if not overrides.allow_unsafe:
            # The CLAMPED value: passing gop_seconds=5 used to write `keyint=5s` -- a
            # five-second GOP -- while the plan's rule text still claimed 1.0 s.
            kv["keyint"] = "{}s".format(max(1, int(math.ceil(seconds))))
        extra["svtav1-params"] = ":".join("{}={}".format(k, v) for k, v in kv.items())
    if encoder == "libx265":
        # R2 for HEVC: closed GOP + repeat headers (needed for MP4)
        params = extra.get("x265-params", "")
        kv = dict(item.split("=", 1) for item in params.split(":") if "=" in item)
        kv["open-gop"] = "0"
        kv["keyint"] = str(gop if not overrides.allow_unsafe else gop * 10)
        kv["min-keyint"] = str(max(1, gop // 4))
        kv["scenecut"] = "40"
        extra["x265-params"] = ":".join("{}={}".format(k, v) for k, v in kv.items())

    if "level" in extra and encoder in ("libx264", "h264_nvenc"):
        # A hardcoded level is a promise about resolution x frame rate, and P2's
        # `-level 4.0` cannot carry 1080p60 or anything above 1080p. x264 only warns and
        # exits 0, so the user got a stream labelled 4.0 that no 4.0 decoder is obliged
        # to play -- the exact opposite of that profile's "最大兼容" promise.
        width, height = (out_size or (0, 0))
        required = h264_level_for(int(width or 0), int(height or 0), fps)
        declared = str(extra.get("level") or "")
        if required and declared and _level_value(required) > _level_value(declared):
            warnings.append(
                "该档位声明 H.264 Level {}，但 {}x{}@{}fps 至少需要 Level {}；"
                "已按 Level {} 输出（否则部分设备会拒绝播放）".format(
                    declared, width, height, _fmt_fps(fps), required, required))
            extra["level"] = required

    for key, value in extra.items():
        flag = "-" + key
        args += [flag, str(value)]
    return args, warnings


def _level_value(level: str) -> float:
    """`"4.2"` -> 4.2 for comparison. An unparsable level sorts lowest."""
    try:
        return float(level)
    except (TypeError, ValueError):
        return 0.0


def _fmt_fps(fps: float) -> str:
    return "{:g}".format(fps) if fps else "?"


def _audio_args(info: MediaInfo, container: str, ov: Overrides) -> List[str]:
    if not info.audio_streams or ov.audio_mode == "drop":
        return []
    args = ["-map", "0:a?"]
    target_container = 'mp4' if container in _MP4_FAMILY else container
    codec, bitrate = _AUDIO_TARGET.get(target_container, ("aac", "192k"))
    normalise = audio.clamp_target(ov.loudness) < 0
    if ov.audio_mode == "transcode" or normalise:
        args += ["-c:a", codec, "-b:a", bitrate]
        if normalise:
            # Normalising REQUIRES decoding and re-encoding the audio; `-c:a copy`
            # would silently do nothing, which is the one outcome we cannot ship.
            args += ["-af", audio.build_filter(ov.loudness_measured, ov.loudness,
                                               ov.loudness_tp)]
    else:
        args += ['-c:a', 'copy']
    ok = _AUDIO_OK_IN.get(target_container, set())
    for index, stream in enumerate(info.audio_streams):
        selected = codec if ov.audio_mode == 'transcode' or normalise or stream.codec not in ok else 'copy'
        args += ['-c:a:{}'.format(index), selected]
        if selected != 'copy':
            args += ['-b:a:{}'.format(index), bitrate]
            layout = stream.channel_layout
            if selected in ('libopus', 'aac'):
                layout = _SIDE_SURROUND_LAYOUTS.get(layout, layout)
            if selected == 'libopus':
                if stream.channels > 8 or layout and layout not in _OPUS_LAYOUTS:
                    raise PlanError('音轨 {} 的 {} 布局（{} 声道）不能可靠转为 Opus；'
                                    '请使用 MKV 并选择音频复制，或明确丢弃音频'.format(
                                        index + 1, stream.channel_layout or '未知', stream.channels))
                # The default legacy multistream API can misplace the centre and
                # surrounds despite correct layout tags. Use the standard mapping
                # explicitly; channel-specific tone tests cover the actual samples.
                surround = stream.channels > 2 or layout and layout not in ('mono', 'stereo')
                args += ['-mapping_family:a:{}'.format(index), '1' if surround else '0']
            if layout:
                args += ['-channel_layout:a:{}'.format(index), layout]
        args += _track_metadata(stream, 'a', index, ov.strip_metadata, container,
                                any(s.default for s in info.audio_streams))
    return args


def _track_metadata(stream, kind: str, index: int, stripped: bool, container: str,
                    has_default: bool) -> List[str]:
    suffix = '{}:{}'.format(kind, index)
    disposition = dict(stream.disposition)
    if stream.default:
        disposition['default'] = 1
    if stream.forced:
        disposition['forced'] = 1
    if container in _MP4_FAMILY and index == 0 and not has_default:
        disposition['default'] = 1
    flags = '+'.join(name for name, enabled in disposition.items() if enabled)
    if not flags:
        flags = '+'.join(name for name, enabled in (('default',stream.default),('forced',stream.forced)) if enabled)
    args = ['-disposition:' + suffix, flags or '0']
    if not stripped:
        for name in ('language', 'title'):
            value = getattr(stream, name)
            if value:
                args += ['-metadata:s:' + suffix, '{}={}'.format(name, value)]
                if name == 'title' and container in _MP4_FAMILY:
                    args += ['-metadata:s:' + suffix, 'handler_name=' + value]
    return args


def _subtitle_args(info: MediaInfo, container: str, ov: Overrides,
                   burn: bool = False) -> List[str]:
    if not info.subtitle_streams or ov.subtitle_mode == "drop":
        return []
    if burn:
        # Subtitles are rendered into the video by the filter chain; copying them
        # as well would produce an empty/duplicate subtitle stream.
        return []
    target_container = 'mp4' if container in _MP4_FAMILY else container
    ok = _SUB_OK_IN.get(target_container, set())
    args = ["-map", "0:s?"]
    args += ['-c:s', 'copy']
    target = "mov_text" if target_container == "mp4" else "srt" if container == "mkv" else "webvtt"
    for index, stream in enumerate(info.subtitle_streams):
        if container == 'mov' and stream.forced:
            raise PlanError('MOV 不能可靠保留 forced 字幕标记，请使用 MP4 或 MKV')
        if stream.codec not in ok and stream.codec not in _TEXT_SUBS:
            raise PlanError('字幕轨 {}（{}）不能转换为文本字幕；请输出 MKV 或明确丢弃字幕'.format(index + 1, stream.codec))
        args += ['-c:s:{}'.format(index), 'copy' if stream.codec in ok else target]
        args += _track_metadata(stream, 's', index, ov.strip_metadata, container,
                                any(s.default for s in info.subtitle_streams))
    return args


def validate_video_container(encoder: str, source_codec: str, container: str) -> None:
    codec = source_codec if encoder == 'copy' else {'libx264':'h264','libx265':'hevc','libsvtav1':'av1'}.get(encoder, encoder.split('_')[0])
    if container == 'webm' and codec not in ('av1', 'vp8', 'vp9'):
        raise PlanError('WebM 不支持 {} 视频，请选择 MP4／MKV 或 AV1 档位'.format(codec))


def track_warnings(info: MediaInfo, container: str, ov: Overrides, burn: bool = False) -> List[str]:
    notes = []
    if ov.audio_mode != 'drop':
        target = 'mp4' if container in _MP4_FAMILY else container
        ok = _AUDIO_OK_IN.get(target, set())
        codec = _AUDIO_TARGET.get(target, ('aac', '192k'))[0]
        label = 'Opus' if codec == 'libopus' else 'AAC'
        for stream in info.audio_streams:
            if ov.audio_mode == 'transcode' or audio.clamp_target(ov.loudness) < 0 or stream.codec not in ok:
                if stream.channel_layout in _SIDE_SURROUND_LAYOUTS:
                    layout = _SIDE_SURROUND_LAYOUTS[stream.channel_layout]
                    count = '五' if layout == '5.0' else '六'
                    note = '{} 将 {} 侧环绕适配为 {} 后环绕布局，保留{}声道'.format(label, layout, layout, count)
                    if note not in notes:
                        notes.append(note)
                elif codec == 'libopus' and not stream.channel_layout and stream.channels > 2:
                    notes.append('源音轨的声道布局未知，Opus 将使用默认布局；需保留位置请使用 MKV 音频复制')
    if container in _MP4_FAMILY:
        if ov.audio_mode != 'drop' and info.audio_streams and not any(s.default for s in info.audio_streams):
            notes.append('MP4 系列容器将第一条音轨设为默认轨')
        if ov.subtitle_mode == 'keep' and info.subtitle_streams and not any(s.default for s in info.subtitle_streams):
            notes.append('MP4 系列容器将第一条字幕设为默认轨')
    if ov.subtitle_mode == 'keep' and not burn:
        native = _SUB_OK_IN.get('mp4' if container in _MP4_FAMILY else container, set())
        for index, stream in enumerate(info.subtitle_streams):
            if stream.codec in ('ass', 'ssa') and stream.codec not in native:
                notes.append('字幕轨 {} 将转为文本字幕，ASS 字体、位置和样式可能丢失；保留样式请用 MKV'.format(index + 1))
    if len(info.video_streams) > 1:
        notes.append('仅压缩第一条真实视频轨，其余 {} 条视频不保留'.format(len(info.video_streams) - 1))
    omitted = [s for s in info.streams if s.kind == 'data' or s.kind == 'attachment' and container != 'mkv']
    if omitted:
        notes.append('目标容器不保留 {} 条 data／附件轨；保留字体附件请用 MKV'.format(len(omitted)))
    return notes


def build_out_path(info: MediaInfo, profile: Dict[str, Any], ov: Overrides,
                   container: str, exists_probe=None) -> str:
    """R9 -- deterministic output path that can never collide with the source.

    The directory comes from core.paths so that compression and repair obey the
    same "save beside / to the default folder / to a custom folder" choice.
    """
    from .paths import is_same_file, render_name, resolve_out_dir, safe_filename, unique_path

    src = Path(info.path)
    # {height}p in the name means the height the viewer sees: naming a portrait phone
    # video "1080p" (its stored height) when it displays as 1920x1080 would be wrong.
    height = info.video.display_height if info.video else 0
    stem = src.stem
    template = ov.name_template or "{stem}_{profile}_{height}p"
    # Post-encode tokens ({filesize_mb} and friends) cannot be filled yet: ffmpeg
    # needs a real path now. They are blanked here and the file is renamed once the
    # values exist (see jobs._finalize_name).
    # `render_name` already reduces the result to one safe component; the fallbacks go
    # through `safe_filename` too, so a source named `CON.mp4` cannot become a device.
    name = (render_name(template, {
        "stem": stem,
        "profile": profile["id"],
        "height": height,
        "codec": profile.get("encoder", ""),
    }, provisional=True) or safe_filename(stem) or "output")

    out_dir = resolve_out_dir(str(src), ov.location, ov.out_dir,
                              ov.default_out_dir, kind="compress")
    candidate = out_dir / "{}.{}".format(name, container)

    # R9 -- refuse to write over the input, whatever the user configured.
    if is_same_file(candidate, src):
        candidate = out_dir / "{}_{}.{}".format(name, "out", container)

    if exists_probe is not None:
        candidate = unique_path(out_dir, candidate.name, exists=exists_probe)
    return str(candidate)


def encoder_min_dimensions(encoder: str) -> tuple:
    """The smallest frame an encoder accepts, as (width, height).

    Measured on this machine: `h264_nvenc` refuses anything narrower than 145 px
    (128x128, 144x144 and 100x180 all fail; 145x49 works) while the software encoders
    happily take tiny frames. Without this, a resolution cap that lands below the
    hardware floor produced ffmpeg's "Frame Dimension less than the minimum supported
    value" and a failed job with no explanation of what to change.
    """
    name = (encoder or "").lower()
    if "nvenc" in name:
        return (145, 49)
    return (32, 32)


#: H.264 level limits: (level, MaxFS in macroblocks, MaxMBPS in MB/s). Source: ITU-T
#: H.264 Table A-1. Only the two limits that a resolution/frame-rate combination can
#: plausibly break are listed; the DPB (reference-frame) limits are the encoder's job.
_H264_LEVELS = (
    ("3.0", 1620, 40500),
    ("3.1", 3600, 108000),
    ("3.2", 5120, 216000),
    ("4.0", 8192, 245760),
    ("4.1", 8192, 245760),
    ("4.2", 8704, 522240),
    ("5.0", 22080, 589824),
    ("5.1", 36864, 983040),
    ("5.2", 36864, 2073600),
    ("6.0", 139264, 4177920),
    ("6.1", 139264, 8355840),
    ("6.2", 139264, 16711680),
)


def h264_level_for(width: int, height: int, fps: float) -> str:
    """Lowest H.264 level that can carry `width`x`height` at `fps`.

    P2 hardcoded `-level 4.0`, which does not cover 1080p60 (needs 4.2) or anything
    above 1080p: x264 only *warns* ("using cpu capabilities... level 4.0 > 4.2"), exits
    0, and writes a stream whose SPS either lies about the level or is non-conformant.
    The profile's promise is "最大兼容", so the level has to be at least correct.
    """
    if not width or not height or not fps or fps <= 0:
        return ""
    mbs = ((int(width) + 15) // 16) * ((int(height) + 15) // 16)
    mbps = mbs * float(fps)
    for name, max_fs, max_mbps in _H264_LEVELS:
        if mbs <= max_fs and mbps <= max_mbps:
            return name
    return _H264_LEVELS[-1][0]


def make_plan(info: MediaInfo, profile: Dict[str, Any], ov: Overrides = None,
              caps=None, exists_probe=None) -> Plan:
    ov = ov or Overrides()
    if ov.audio_mode is None:
        ov = replace(ov, audio_mode=profile.get('audio_mode', 'copy'))
    if caps is None:
        from .caps import get_caps
        caps = get_caps()

    intent = validate_intent(ov.intent)
    profile, ov, decision = resolve_selection(info, profile, ov, caps)

    warnings: List[str] = []
    rules: List[str] = []

    if not info.ok:
        raise ValueError("源文件不可用: {}".format(info.error or info.path))

    from .paths import validate_container
    container = validate_container(ov.container or profile.get("container") or "mp4")
    encoder, fell_back = _pick_encoder(profile, caps)
    if fell_back:
        warnings.append(
            "本机未检测到 {}，已自动回退到 {}".format(profile["encoder"], encoder)
        )
        # Hardware presets (p4/p5/p6) and rate-control knobs have no software
        # meaning. Keep the profile identity, but build with the fallback's options.
        extra = dict(profile.get('extra') or {})
        for key in ('rc', 'cq', 'b:v', 'spatial-aq', 'temporal-aq', 'aq-strength'):
            extra.pop(key, None)
        profile = dict(profile, encoder=encoder, extra=extra,
                       preset='8' if encoder == 'libsvtav1' else 'medium')
    if encoder not in ("copy",) and not caps.has_encoder(encoder):
        raise ValueError("本机 ffmpeg 没有可用的 {} 编码器".format(encoder))

    # Refuse early when the SOURCE itself is below the encoder's floor: no scale setting
    # can fix that, and letting it through means a failed job and a cryptic message.
    if encoder != "copy" and info.video:
        min_w, min_h = encoder_min_dimensions(encoder)
        if info.video.display_width < min_w or info.video.display_height < min_h:
            raise ValueError(
                "源分辨率 {}x{} 低于 {} 的最小尺寸 {}x{}，该档位无法编码；"
                "请改用软件编码档位（如 P2/P5）".format(
                    info.video.display_width, info.video.display_height,
                    encoder, min_w, min_h))

    # Profile-level resolution ceiling: an A-ladder profile's whole promise is "this
    # lands at ≤720p", so it has to reach the planner, not just the profile card.
    capped_edge = effective_max_edge(profile, ov)
    if capped_edge != int(ov.max_long_edge or 0):
        ov = replace(ov, max_long_edge=capped_edge)

    # Frame rate: the source's, capped by the profile. -g is derived from the OUTPUT
    # rate, or the "1 keyframe per second" promise would be wrong by the same factor.
    fps = effective_fps(info, profile)
    if profile.get("fps_cap") and fps and info.video and info.video.fps > fps + 0.01:
        warnings.append("帧率 {} → {:g}（该档位主动降帧）".format(info.video.fps, fps))
        rules.append("R13 降帧率到 {:g}fps".format(fps))

    gop_seconds = clamp_gop_seconds(ov.gop_seconds)
    gop = compute_gop(fps, gop_seconds)
    # Refused here, not silently dropped: `extra_args` lands after the invariant flags
    # and ffmpeg is last-one-wins, so `-g 9999` used to override R1 while the report
    # still advertised "关键帧间隔 1.0s". Expert mode unlocks it with a warning instead.
    warnings += check_extra_args(ov.extra_args, ov.allow_unsafe)
    from .memory import last_option
    extra_preset = last_option(ov.extra_args, ('-preset',), '')
    if extra_preset:
        ov = replace(ov, preset=extra_preset)
    if ov.allow_unsafe:
        warnings.append("专家模式：已放宽关键帧/GOP 安全约束，输出可能无法拖动进度条")
    elif encoder != 'copy':
        rules.append("R1 关键帧间隔 {:.1f}s（-g {}）".format(gop_seconds, gop))
        rules.append("R2 禁用开放 GOP（闭合关键帧）")

    pix_fmt = profile.get("pix_fmt", "") or ""
    if ov.ten_bit is True:
        if encoder in ("libx264", "h264_nvenc"):
            # 10-bit H.264 is a non-standard extension almost nothing decodes. Saying so
            # beats silently encoding 8-bit while the UI shows "10bit" ticked.
            warnings.append("H.264 档位不支持 10bit，已按 8bit 编码（10bit 请用 HEVC/AV1 档位）")
            pix_fmt = "yuv420p"
        else:
            pix_fmt = "yuv420p10le"
    elif ov.ten_bit is False:
        pix_fmt = "yuv420p"
    elif pix_fmt == "yuv420p10le" and encoder in ("libx264", "h264_nvenc"):
        pix_fmt = "yuv420p"
    if encoder in ('hevc_nvenc', 'av1_nvenc') and pix_fmt == 'yuv420p10le':
        pix_fmt = 'p010le'
    if hasattr(caps, 'supports_pix_fmt') and not caps.supports_pix_fmt(encoder, pix_fmt):
        raise PlanError('{} 无法使用 {} 输入格式，请改用软件档位或关闭 10-bit'.format(encoder, pix_fmt))
    # MP4 codec tags have no meaning in Matroska/WebM.
    if container not in _MP4_FAMILY and 'tag:v' in (profile.get('extra') or {}):
        profile = dict(profile, extra={k:v for k,v in profile['extra'].items() if k != 'tag:v'})

    scale = compute_scale(info, ov)
    if scale and encoder != "copy":
        # A cap that lands below the encoder's floor cannot be encoded at all. Clamping
        # UP (while keeping the aspect ratio) is what the user's intent allows -- "at
        # most this big, and it has to actually work" -- and the warning says what
        # happened rather than leaving ffmpeg's cryptic message as the only clue.
        min_w, min_h = encoder_min_dimensions(encoder)
        if scale[0] < min_w or scale[1] < min_h:
            factor = max(min_w / float(scale[0]), min_h / float(scale[1]))
            # `_ceil_even`, not `_round_up_even`: the clamp must land AT OR ABOVE the
            # encoder's floor. Using the flooring helper produced 144 for a 145 minimum
            # and the encode failed anyway.
            raised = (_ceil_even(scale[0] * factor), _ceil_even(scale[1] * factor))
            if info.video and (raised[0] <= info.video.display_width
                               and raised[1] <= info.video.display_height):
                warnings.append(
                    "目标 {}x{} 低于 {} 的最小尺寸 {}x{}，已改为 {}x{}".format(
                        scale[0], scale[1], encoder, min_w, min_h,
                        raised[0], raised[1]))
                scale = raised
            else:
                warnings.append(
                    "目标 {}x{} 低于 {} 的最小尺寸 {}x{}，将不缩放（保持原分辨率）".format(
                        scale[0], scale[1], encoder, min_w, min_h))
                scale = None
    if scale:
        warnings.append("将缩放到 {}x{}".format(scale[0], scale[1]))

    # Announce the auto-effort raise where it happens, with the measured price tag: the
    # user is trading time for size, and that trade should never be silent.
    auto_effort_used = 0
    if encoder == "libsvtav1" and ov.preset is None and str(ov.effort or "auto") != "off":
        try:
            auto_effort_used = int(profile.get("auto_effort") or 0)
        except (TypeError, ValueError):
            auto_effort_used = 0
    if auto_effort_used:
        warnings.append(profile.get("auto_effort_note") or (
            "极限档已自动提高编码精度：preset {} → {}".format(
                profile.get("preset"), auto_effort_used)))
        rules.append("A7 极限档自动提精度")

    filters = build_filters(info, ov, scale, fps_cap=float(profile.get("fps_cap") or 0))
    burn_subs = ov.subtitle_mode == "burn" and bool(info.subtitle_streams)
    if burn_subs:
        if info.subtitle_streams[0].codec not in _TEXT_SUBS:
            raise PlanError('第一条字幕是图像字幕，不能用文本滤镜烧录；请保留为 MKV 或丢弃字幕')
        if caps.has_filter("subtitles"):
            # Option values are parsed inside a filter graph: both parsers must
            # receive their own escapes. Outer quotes alone break on apostrophes.
            filename = str(info.path).replace('\\', '/')
            value = ''.join('\\' + c if c in "\\':" else c for c in filename)
            escaped = ''.join('\\' + c if c in "\\'[],;" else c for c in value)
            filters.append('subtitles=filename={}:si=0'.format(escaped))
            warnings.append('烧录第一条字幕：{}（{}）；其余字幕不再内挂'.format(info.subtitle_streams[0].title or '字幕 1',info.subtitle_streams[0].language or '语言未标记'))
        else:
            warnings.append("本机 ffmpeg 缺少 subtitles 滤镜，已改为内挂字幕")
            burn_subs = False

    if info.is_hdr and not ov.hdr_to_sdr and container == "mp4" and encoder in ("libx264", "h264_nvenc"):
        warnings.append("源是 HDR，而 H.264 档位不支持 HDR；建议开启 HDR→SDR 或改用 HEVC/AV1 档位")
    if info.is_hdr and ov.hdr_to_sdr:
        warnings.append("已启用 HDR→SDR 色调映射")
        rules.append("R11 HDR→SDR 色调映射")

    # The PREVIEW cannot measure the source (that would mean a full decode on every
    # keystroke), so the command shown here carries the single-pass form while the real
    # run uses the measured linear two-pass form. Saying so is the difference between a
    # preview and a plausible-looking fiction.
    if audio.clamp_target(ov.loudness) < 0 and info.audio_streams and not ov.loudness_measured:
        warnings.append("响度标准化：此处预览是单遍形式；实际执行前会先整片测量该片响度，"
                        "改用一次性线性增益（两遍法），以免改变动态范围")

    out_path = build_out_path(info, profile, ov, container, exists_probe=exists_probe)

    # Stream copy cannot filter. "复制编码" + any filter (`-vf`) makes ffmpeg exit -22
    # with "Filtering and streamcopy cannot be used together", and the plan showed no
    # warning at all -- a guaranteed failure discovered only after the job started.
    # Refusing up front names the offending option and the way out.
    if encoder == "copy" and filters:
        raise PlanError(
            "「复制编码」不能同时使用滤镜（{}），因为流复制不重新编码画面。"
            "请改用任一编码档位，或关闭缩放/裁切/降噪/改帧率/HDR 转换/字幕烧入。".format(
                "、".join(filters)))

    argv: List[str] = [caps.ffmpeg, "-hide_banner", "-nostdin", "-n"]
    if info.is_hdr and not ov.hdr_to_sdr and encoder in ("av1_nvenc", "hevc_nvenc", "libsvtav1", "libx265"):
        # keep HDR metadata mapping intact
        pass
    argv += ["-i", info.path]

    # R7 -- explicit track mapping
    argv += ["-map", "0:{}".format(info.video.index)]
    argv += _audio_args(info, container, ov)
    argv += _subtitle_args(info, container, ov, burn=burn_subs)
    if container == 'mkv':
        argv += ['-map', '0:t?', '-c:t', 'copy']
    warnings += track_warnings(info, container, ov, burn_subs)
    if burn_subs:
        # The burned subtitle stream is rendered into the video; mark it so ffmpeg
        # does not expect an output subtitle stream.
        rules.append("R12 字幕烧入画面")
    if not ov.strip_metadata:
        argv += ["-map_metadata", "0", "-map_chapters", "0"]
    else:
        # `-map_metadata -1` clears stream/global tags but NOT chapters -- ffmpeg copies
        # those separately. The UI label says 清除元数据/章节, so chapters must actually
        # go, or "已清除" is false for every file that has any.
        argv += ["-map_metadata", "-1", "-map_chapters", "-1"]
        rules.append("R14 清除元数据与章节")

    video_args, video_warnings = _video_args(encoder, profile, ov, gop, pix_fmt, fps,
                                             gop_seconds=gop_seconds,
                                             out_size=scale or (
                                                 (info.video.display_width,
                                                  info.video.display_height)
                                                 if info.video else None))
    argv += video_args
    warnings += video_warnings

    if filters:
        argv += ["-vf", ",".join(filters)]
        rules.append("滤镜链: {}".format(",".join(filters)))

    if ov.extra_args:
        # Already validated above (`check_extra_args`): a taboo flag raised there, so what
        # reaches here is either harmless or explicitly allowed by 专家模式.
        argv += [str(a) for a in ov.extra_args]

    # R3 -- faststart for mp4/mov
    if container in _MP4_FAMILY:
        argv += ["-movflags", "+faststart"]
        rules.append("R3 MP4 faststart（索引前置，利于拖动与流式播放）")

    # Progress reporting consumed by core.runner
    argv += ["-progress", "pipe:1", "-nostats"]
    argv.append(out_path)

    est = estimate_output_bytes(info, profile, ov, scale=scale)

    # Use final options and keep original-size decode/filter buffers in the reserve.
    from types import SimpleNamespace
    from .memory import danger_note, estimate_plan_mb, encoder_geometry, is_dangerous, machine_memory, last_option
    encoder = last_option(argv, ('-c:v', '-codec:v', '-vcodec', '-c', '-codec'), encoder)
    validate_video_container(encoder, info.video.codec, container)
    if container in _MP4_FAMILY and (encoder in ('libx265','hevc_nvenc') or encoder == 'copy' and info.video.codec == 'hevc'):
        argv[-1:-1] = ['-tag:v', _H265_TAG]
    if encoder == 'copy' and last_option(argv, ('-vf', '-filter:v')):
        raise PlanError('复制编码不能同时使用视频滤镜')
    source_size = (info.video.display_width, info.video.display_height) if info.video else (0, 0)
    out_w, out_h = encoder_geometry(argv, source_size)
    est_memory_mb, memory_basis = estimate_plan_mb(SimpleNamespace(
        argv=argv, encoder=encoder, out_size=(out_w, out_h), source_size=source_size,
        source_pix_fmt=info.video.pix_fmt if info.video else ""))
    if est_memory_mb >= 512:
        warnings.append("预计峰值内存约 {:.1f} GB（编码器输入 {}x{}，{}）".format(
            est_memory_mb / 1024.0, out_w, out_h, memory_basis))
    mem = machine_memory()
    if intent:
        from . import settings
        from .memory import admission
        policy = settings.load()
        if policy.get("memory_guard", True):
            gate = admission(mem, 0, est_memory_mb, int(policy.get("memory_margin_mb") or 2500))
            if not gate["ok"]:
                warnings.append(gate["reason"] + "；可添加到队列，保持本计划等待。")
    if is_dangerous(est_memory_mb, mem):
        warnings.append(danger_note(est_memory_mb, mem) + ("保持本计划，等待内存余量。" if intent else "建议改用带分辨率上限的档位。"))

    command = " ".join(shlex.quote(a) for a in argv)
    return Plan(
        argv=argv,
        command=command,
        out_path=out_path,
        warnings=warnings,
        rules_applied=rules,
        gop=gop,
        est_output_bytes=est,
        encoder=encoder,
        est_memory_mb=est_memory_mb,
        est_memory_basis=memory_basis,
        profile_id=profile["id"],
        resolved_overrides=ov.to_dict(),
        intent=intent,
        decision=decision,
    )
