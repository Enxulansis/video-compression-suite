"""ffprobe wrapper: turn an arbitrary media file into a normalised MediaInfo.

Read-only.  Never touches the source file.  Pure stdlib (Python 3.9+).
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from . import work
from dataclasses import dataclass, field, asdict
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .caps import get_caps, subprocess_kwargs


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _parse_fps(rate: Optional[str]) -> float:
    """'30000/1001' -> 29.97 ; '0/0' or None -> 0.0"""
    if not rate or rate in ("0/0", "N/A"):
        return 0.0
    try:
        return float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return 0.0


@dataclass
class StreamInfo:
    index: int
    kind: str                      # video | audio | subtitle | data | attachment
    codec: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    bit_depth: int = 0
    bit_rate: int = 0
    duration: float = 0.0
    channels: int = 0
    channel_layout: str = ''
    sample_rate: int = 0
    language: str = ""
    title: str = ""
    default: bool = False
    forced: bool = False
    disposition: Dict[str, int] = field(default_factory=dict)
    filename: str = ''
    mimetype: str = ''
    codec_tag: str = ''
    is_image: bool = False       # attached_pic / mjpeg cover art
    color_transfer: str = ""
    color_primaries: str = ""
    #: Display-matrix rotation in degrees (ffprobe's `side_data_list[].rotation`).
    #: A phone shot in portrait is stored landscape with one of these set, and the
    #: frames come out of ffmpeg rotated -- see `display_width`.
    rotation: int = 0

    @property
    def is_rotated(self) -> bool:
        """True when the display matrix swaps width and height."""
        return abs(int(self.rotation)) % 180 == 90

    @property
    def display_width(self) -> int:
        """Width as the viewer sees it (after the display matrix is applied)."""
        return self.height if self.is_rotated else self.width

    @property
    def display_height(self) -> int:
        return self.width if self.is_rotated else self.height

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer.lower() in ("smpte2084", "arib-std-b67")


@dataclass
class MediaInfo:
    path: str
    ok: bool = False
    error: str = ""
    size_bytes: int = 0
    #: Filesystem modification time (epoch seconds). The container's `creation_time` says
    #: when the footage was shot; this says which date a file manager sorts it under.
    mtime: float = 0.0
    duration: float = 0.0
    format_name: str = ""
    format_long_name: str = ""
    bit_rate: int = 0
    streams: List[StreamInfo] = field(default_factory=list)
    tags: Dict[str, str] = field(default_factory=dict)
    frame_count: int = 0            # video frame count (only when probed deeply)
    #: Chapters exactly as ffprobe reported them. Kept so a delivery check can prove the
    #: output still has them, instead of trusting `-map_chapters 0` blindly.
    chapters: List[Dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------- derived

    @property
    def video(self) -> Optional[StreamInfo]:
        for s in self.streams:
            if s.kind == "video" and not s.is_image:
                return s
        return None

    @property
    def video_streams(self) -> List[StreamInfo]:
        return [s for s in self.streams if s.kind == "video" and not s.is_image]

    @property
    def audio_streams(self) -> List[StreamInfo]:
        return [s for s in self.streams if s.kind == "audio"]

    @property
    def subtitle_streams(self) -> List[StreamInfo]:
        return [s for s in self.streams if s.kind == "subtitle"]

    @property
    def is_hdr(self) -> bool:
        return any(s.is_hdr for s in self.video_streams)

    def summary(self) -> Dict[str, Any]:
        v = self.video
        return {
            # DISPLAY resolution: a rotated phone video is stored 1920x1080 and shown
            # 1080x1920, and reporting the stored size is how a tool ends up squashing
            # portrait footage into a landscape box.
            "resolution": "{}x{}".format(v.display_width, v.display_height) if v else "-",
            "coded_resolution": "{}x{}".format(v.width, v.height) if v else "-",
            "rotation": int(v.rotation) if v else 0,
            "codec": (v.codec.upper() if v else "-") or "-",
            "fps": round(v.fps, 3) if v else 0,
            "duration": round(self.duration, 3),
            "bit_depth": v.bit_depth if v else 0,
            "hdr": self.is_hdr,
            "audio_tracks": len(self.audio_streams),
            "subtitle_tracks": len(self.subtitle_streams),
            "chapters": len(self.chapters),
            "creation_time": self.creation_time,
        }

    @property
    def creation_time(self) -> str:
        """The container's creation/shooting time, as recorded ('' when absent)."""
        for key in ("creation_time", "date", "com.apple.quicktime.creationdate"):
            value = self.tags.get(key)
            if value:
                return str(value)
        return ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["summary"] = self.summary()
        data["is_hdr"] = self.is_hdr
        # `video` and `audio` are computed properties, so `asdict` never included
        # them -- which meant the front-end could not see resolution or frame rate
        # and silently produced 0 for anything derived from them (the uncompressed
        # equivalent, for one). Expose the derived stream info explicitly.
        data["video"] = asdict(self.video) if self.video else None
        data["audio"] = [asdict(s) for s in self.audio_streams]
        return data


def _parse_rotation(stream: Dict[str, Any]) -> int:
    """Display-matrix rotation from ffprobe's `side_data_list`, in degrees.

    Two shapes exist in the wild: a `rotation` entry inside `side_data_list` (modern
    ffprobe) and a plain `rotate` stream tag (older files, and what `-metadata:s:v
    rotate=90` writes). Both are read; anything unparsable means "not rotated", which is
    the safe default because the alternative -- swapping width and height for a frame
    that is not actually rotated -- would distort every normal video.
    """
    for item in (stream.get("side_data_list") or []):
        if not isinstance(item, dict):
            continue
        if "rotation" in item:
            try:
                return int(round(float(item["rotation"])))
            except (TypeError, ValueError):
                continue
    tag = (stream.get("tags") or {}).get("rotate")
    try:
        return int(round(float(tag)))
    except (TypeError, ValueError):
        return 0


def _probe_json(path: str, extra: Optional[List[str]] = None) -> Dict[str, Any]:
    caps = get_caps()
    if not caps.available():
        raise RuntimeError(caps.error or "ffprobe unavailable")
    argv = [
        caps.ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters",
    ]
    if extra:
        argv.extend(extra)
    argv.append(path)
    proc = work.run(argv, timeout=120, **subprocess_kwargs())
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffprobe failed").strip()[:500])
    return json.loads(proc.stdout or "{}")


def probe(path: str) -> MediaInfo:
    """Probe one file.  Returns a MediaInfo with ok=False and an error message
    instead of raising, so a batch never dies because of one bad file."""
    p = Path(path)
    info = MediaInfo(path=str(p))
    try:
        stat = p.stat()
        info.size_bytes = stat.st_size
        # Kept so the delivery check can prove the output carries the source's file
        # timestamps (F7) instead of promising it.
        info.mtime = stat.st_mtime
    except OSError as exc:
        info.error = "无法访问文件: {}".format(exc)
        return info

    try:
        raw = _probe_json(str(p))
    except (work.Busy, work.Expired):
        raise  # transient resource failures belong to the request, not the source
    except Exception as exc:  # noqa: BLE001 - deliberately broad: batch safety
        info.error = "探测失败: {}".format(exc)
        return info

    fmt = raw.get("format") or {}
    info.format_name = fmt.get("format_name", "")
    info.format_long_name = fmt.get("format_long_name", "")
    info.duration = _to_float(fmt.get("duration"))
    info.bit_rate = _to_int(fmt.get("bit_rate"))
    tags = fmt.get("tags") or {}
    info.tags = {str(k).lower(): str(v) for k, v in tags.items()}
    info.chapters = [c for c in (raw.get("chapters") or []) if isinstance(c, dict)]

    for s in raw.get("streams") or []:
        kind = s.get("codec_type", "data")
        vid = int(s.get("index", 0))
        if kind == "video":
            disposition = s.get("disposition") or {}
            is_image = bool(disposition.get("attached_pic")) or s.get("codec_name") in (
                "mjpeg", "png", "bmp", "gif",
            ) and not s.get("width") is None and (s.get("avg_frame_rate") in (None, "0/0"))
            stream = StreamInfo(
                index=vid, kind="video",
                codec=s.get("codec_name", ""),
                profile=str(s.get("profile", "")),
                width=_to_int(s.get("width")),
                height=_to_int(s.get("height")),
                fps=_parse_fps(s.get("avg_frame_rate")) or _parse_fps(s.get("r_frame_rate")),
                pix_fmt=s.get("pix_fmt", "") or "",
                bit_depth=0,
                bit_rate=_to_int(s.get("bit_rate")),
                duration=_to_float(s.get("duration"), info.duration),
                language=(s.get("tags") or {}).get("language", ""),
                title=(s.get("tags") or {}).get("title", ""),
                default=bool(disposition.get("default")),
                is_image=bool(is_image),
                color_transfer=str(s.get("color_transfer", "") or ""),
                color_primaries=str(s.get("color_primaries", "") or ""),
                rotation=_parse_rotation(s),
            )
            bits = _bits_from_pix_fmt(stream.pix_fmt)
            stream.bit_depth = bits
            info.streams.append(stream)
        elif kind == "audio":
            disposition = s.get("disposition") or {}
            info.streams.append(StreamInfo(
                index=vid, kind="audio",
                codec=s.get("codec_name", ""),
                bit_rate=_to_int(s.get("bit_rate")),
                duration=_to_float(s.get("duration"), info.duration),
                channels=_to_int(s.get("channels")),
                channel_layout=s.get('channel_layout', ''),
                sample_rate=_to_int(s.get("sample_rate")),
                language=(s.get("tags") or {}).get("language", ""),
                title=(s.get("tags") or {}).get("title", ""),
                default=bool(disposition.get("default")),
            ))
        elif kind == "subtitle":
            disposition = s.get("disposition") or {}
            info.streams.append(StreamInfo(
                index=vid, kind="subtitle",
                codec=s.get("codec_name", ""),
                language=(s.get("tags") or {}).get("language", ""),
                title=(s.get("tags") or {}).get("title", ""),
                default=bool(disposition.get("default")),
            ))

        if info.streams and info.streams[-1].index == vid:
            stream = info.streams[-1]
            stream.forced = bool((s.get('disposition') or {}).get('forced'))
            stream.disposition = {k:int(v) for k,v in (s.get('disposition') or {}).items() if v}
            stream.codec_tag = s.get('codec_tag_string', '')
            stream.title = stream.title or (s.get('tags') or {}).get('handler_name', '')
        if kind in ('attachment', 'data'):
            tags = s.get('tags') or {}
            info.streams.append(StreamInfo(index=vid,kind=kind,codec=s.get('codec_name',''),
                                          filename=tags.get('filename',''),mimetype=tags.get('mimetype',''),
                                          title=tags.get('title','')))

    if not info.duration and info.video and info.video.duration:
        info.duration = info.video.duration
    if not info.bit_rate and info.size_bytes and info.duration > 0:
        info.bit_rate = int(info.size_bytes * 8 / info.duration)

    if not info.video_streams:
        info.error = "未找到视频流（可能只是音频/图片文件）"
        return info

    info.ok = True
    work.note_media(info)
    return info


def _bits_from_pix_fmt(pix_fmt: str) -> int:
    if not pix_fmt:
        return 0
    for token in ("16", "14", "12", "10", "9"):
        if token in pix_fmt:
            return int(token)
    return 8


# NOTE: `count_frames()` used to live here -- an ffprobe `-count_frames` pass that
# decoded the whole file just to get a number. The verifier replaced it with a single
# streaming pass (`scan_stream`, which returns the frame count AND the keyframes AND the
# decode errors without a second read), leaving it dead. It was removed during the
# 2026-10-05 review so nobody reintroduces the two-pass cost by picking up a function
# whose docstring still says "verifier only".


#: ffmpeg's `-progress` stream emits `key=value` lines.  Besides the documented
#: top-level keys it also writes one `stream_<i>_<j>_q=` line per output stream
#: (observed on ffmpeg 8.0.1 as `stream_0_0_q=-1.0`).  Missing that pattern made
#: the verifier treat ordinary progress output as an error and fail every job.
_PROGRESS_KEYS = frozenset((
    "frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms",
    "out_time", "dup_frames", "drop_frames", "speed", "progress",
))
_PROGRESS_KEY_RE = re.compile(r"^(?:[a-z][a-z0-9_]*|stream_\d+_\d+_\w+)$")

#: Anything that survives the progress filter is only treated as a *failure* if it
#: actually reads like a diagnostic.  Unknown stray text is kept as a note instead,
#: so a future ffmpeg build inventing a new progress key degrades to "noisy" rather
#: than "every output fails verification".
_ERROR_TEXT_RE = re.compile(
    r"(\[|\berror\b|\bfailed\b|\bfailure\b|could not|cannot|unable|invalid|"
    r"not found|no such|corrupt|truncat|missing|denied|unsupported|unknown)",
    re.IGNORECASE,
)


class ScanResult(NamedTuple):
    """Outcome of one streaming pass over a media file."""

    frames: int = 0
    keys: List[float] = []          # type: ignore[assignment]
    errors: List[str] = []          # type: ignore[assignment]
    notes: List[str] = []           # type: ignore[assignment]

    @property
    def ok(self) -> bool:
        return self.frames > 0 and not self.errors


def scan_stream(path: str, on_progress=None, want_keys: bool = True,
                decode: bool = False, media: Optional[MediaInfo] = None) -> ScanResult:
    """One streaming pass that yields the frame count, keyframe times AND errors.

    The verifier used to do two passes over the file: `ffprobe -count_frames` (which
    decodes every frame and throws the result away) and then a separate keyframe
    scan. On a multi-gigabyte file that is slow enough to look hung, and it showed
    no progress at all.

    ffmpeg's `-progress` stream gives `frame=` as it goes, so a single pass can
    report movement while it counts. Keyframes are recovered afterwards from the
    packet list, which is a metadata read rather than a second decode.

    `decode=False` (default) copies packets: it validates the container, the index
    and the packet structure at disk speed, but does NOT prove the entropy-coded
    video data is decodable.  `decode=True` drops `-c copy` and truly decodes every
    frame -- much slower, and the only way to catch mid-file corruption.

    Measured on the project's own 734 MB / 324 s test source: copy 1.04 s vs decode
    16.12 s (15x), and the packet-derived keyframe count matched the decoded one
    exactly (325 vs 325).

    stderr is merged into stdout so error text can be captured without a second
    reader thread; progress lines are recognised by key, so log lines cannot be
    mistaken for progress.
    """
    caps = get_caps()
    if not caps.available():
        return ScanResult(0, [], ["ffmpeg 不可用"], [])

    argv = [
        caps.ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
    ]
    if media and 'matroska' in media.format_name and any(s.codec == 'opus' for s in media.audio_streams):
        # Matroska already supplies complete packet boundaries. FFmpeg 8's Opus
        # parser reports an invalid empty packet at EOF despite 0 decoder errors.
        # Bypass that redundant parser; the decoder and diagnostic gate stay active.
        argv += ['-fflags', '+noparse+nofillin']
    argv += ['-i',path,'-map','0:v:0','-map','0:a?']
    if not decode:
        argv += ["-c", "copy"]
    else:
        # The null encoder must retain the input timestamp resolution. Remuxed
        # millisecond/VFR PTS can collide if rounded to a guessed CFR time base.
        argv += ['-fps_mode:v', 'passthrough', '-enc_time_base:v', 'demux']
    argv += [
        "-f", "null", "-",
        "-progress", "pipe:1", "-nostats",
    ]
    frames = 0
    errors: List[str] = []
    last_report = 0.0
    notes: List[str] = []
    try:
        with work.stream(
            argv,
            timeout=1800,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **({k: v for k, v in subprocess_kwargs().items()
                if k in ("creationflags", "startupinfo")}),
        ) as proc:
            assert proc.stdout is not None
            import time as _time
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                key, sep, value = line.partition("=")
                if sep and (key in _PROGRESS_KEYS or _PROGRESS_KEY_RE.match(key)):
                    if key != "frame" or value in ("N/A", ""):
                        continue
                    try:
                        frames = int(value)
                    except ValueError:
                        continue
                    now = _time.time()
                    if on_progress and now - last_report >= 0.4:
                        last_report = now
                        try:
                            on_progress(frames)
                        except Exception:
                            pass
                    continue
                bucket = errors if _ERROR_TEXT_RE.search(line) else notes
                if len(bucket) < 20:
                    bucket.append(line)
            code = proc.wait(timeout=5)
    except OSError as exc:
        return ScanResult(0, [], ["无法启动 ffmpeg：{}".format(exc)], [])
    if code != 0:
        errors.append("ffmpeg 退出码 {}".format(code))

    # Always report the final count: ffmpeg's last progress line can arrive well
    # before the stream ends, so without this the caller's frame counter can stay at
    # a stale value -- and a scan that finishes inside the caller's throttle window
    # would report no movement at all.
    if on_progress and frames > 0:
        try:
            on_progress(frames)
        except Exception:  # noqa: BLE001 - progress must never break a scan
            pass

    keys: List[float] = []
    if want_keys and frames > 0:
        keys = packet_keyframe_times(path)
    return ScanResult(frames, keys, errors, notes)


BITMAP_SUBTITLE_CODECS = frozenset(('hdmv_pgs_subtitle', 'dvd_subtitle'))


def scan_bitmap_subtitles(path: str, media: MediaInfo, on_progress=None) -> ScanResult:
    """Explicitly decode each bitmap track; AV scans may never visit its packets.

    Stream compact output instead of retaining a feature-film's frames in JSON.
    FFprobe can exit zero after discarding bad DVD packets, so diagnostics and
    packets-without-events must independently fail the gate.
    """
    tracks = [s for s in media.subtitle_streams if s.codec in BITMAP_SUBTITLE_CODECS]
    if not tracks:
        return ScanResult(0, [], [], [])
    caps = get_caps()
    if not caps.available():
        return ScanResult(0, [], ['ffprobe 不可用，图像字幕未验证'], [])
    events = 0
    errors: List[str] = []
    notes: List[str] = []
    for track in tracks:
        packets = decoded = 0
        last_report = 0.0
        argv = [caps.ffprobe, '-v', 'error', '-select_streams', str(track.index),
                '-show_packets', '-show_frames', '-show_entries',
                'packet=pts:subtitle=pts,num_rects', '-of', 'compact', path]
        try:
            with work.stream(argv, timeout=1800, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             encoding='utf-8', errors='replace', bufsize=1,
                             **({k: v for k, v in subprocess_kwargs().items()
                                 if k in ('creationflags', 'startupinfo')})) as proc:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = raw.strip()
                    if line.startswith('packet|'):
                        packets += 1
                    elif line.startswith('subtitle|'):
                        decoded += 1
                        if on_progress:
                            import time as _time
                            now = _time.monotonic()
                            if now - last_report >= 0.4:
                                on_progress(events + decoded)
                                last_report = now
                    elif line and len(errors) < 20:
                        errors.append('字幕轨 {}：{}'.format(track.index, line[:300]))
                code = proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append('字幕轨 {}：无法完成解码：{}'.format(track.index, exc))
            break
        events += decoded
        if code != 0:
            errors.append('字幕轨 {}：ffprobe 退出码 {}'.format(track.index, code))
        if packets and not decoded:
            errors.append('字幕轨 {}：有 {} 个包但没有可解码事件'.format(track.index, packets))
        elif not packets:
            notes.append('字幕轨 {}：没有字幕包，内容未能验证'.format(track.index))
        if on_progress:
            on_progress(events)
        if errors:
            break
    return ScanResult(events, [], errors, notes)


def packet_keyframe_times(path: str) -> List[float]:
    """Keyframe timestamps from the PACKET list (no decoding).

    Reading packets is dramatically cheaper than decoding frames and is enough to
    locate keyframes: the container flags each packet with a K bit.

    Uses the labelled output (`default=noprint_wrappers=1`), NOT csv -- csv orders
    columns by field name and emits `pts_time,flags`, so a `flags,pts_time`
    assumption silently yields zero keyframes.
    """
    caps = get_caps()
    if not caps.available():
        return []
    argv = [
        caps.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=flags,pts_time",
        "-of", "default=noprint_wrappers=1", path,
    ]
    try:
        proc = work.run(argv, timeout=1800, **subprocess_kwargs())
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    times: List[float] = []
    pending_key = False
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("flags="):
            pending_key = "K" in line.partition("=")[2]
        elif line.startswith("pts_time="):
            value = line.partition("=")[2].strip()
            if pending_key:
                try:
                    ts = float(value)
                    if math.isfinite(ts):
                        times.append(ts)
                except ValueError:
                    pass
            pending_key = False
    return times
