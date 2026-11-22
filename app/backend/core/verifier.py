"""Verifier -- the quality gate that makes "you can drag the seek bar" a test.

Runs after every encode.  V2 is the check that matters most: it is the automated
guard against the AV1 seek failure reproduced in _temp/research/AV1_SEEK_ROOTCAUSE.md
(an SVT-AV1 stream with open GOP and a long -g ended up with a single keyframe).

Verifier verdicts:
    passed        all mandatory checks OK
    risky         playable but outside our compatibility budget (e.g. keyframe gap too big)
    failed        undecodable / wrong duration / lost tracks
"""

from __future__ import annotations

import json
import math
import subprocess
import time
from . import work
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .caps import get_caps, subprocess_kwargs
from .probe import MediaInfo, probe
from . import audio


def stream_expectations(source: MediaInfo, argv: List[str]) -> Dict[str, Any]:
    """Expected delivery streams from the exact output mappings and codec options."""
    from .memory import last_option
    codec_names = {'libx264':'h264','h264_nvenc':'h264','libx265':'hevc','hevc_nvenc':'hevc',
                   'libsvtav1':'av1','av1_nvenc':'av1','libopus':'opus','libmp3lame':'mp3','srt':'subrip'}
    maps = [argv[i+1] for i,token in enumerate(argv[:-1]) if token == '-map']
    expected = {}
    for kind, selector, streams in (('audio','a',source.audio_streams),
                                    ('subtitle','s',source.subtitle_streams),
                                    ('attachment','t',[s for s in source.streams if s.kind == 'attachment'])):
        rows = []
        selected = streams if '0:{}?'.format(selector) in maps else [s for s in streams if '0:{}'.format(s.index) in maps]
        for index, stream in enumerate(selected):
            codec = 'copy'
            disposition = ''
            for i,token in enumerate(argv[:-1]):
                if token in ('-c','-codec','-c:'+selector,'-codec:'+selector,
                             '-c:{}:{}'.format(selector,index),'-codec:{}:{}'.format(selector,index)):
                    codec = argv[i+1]
                if token == '-disposition:{}:{}'.format(selector,index):
                    disposition = argv[i+1]
            rows.append(dict(codec=stream.codec if codec == 'copy' else codec_names.get(codec,codec),
                             language=stream.language,title=stream.title,
                             default='default' in disposition.split('+') if disposition else stream.default,
                             forced='forced' in disposition.split('+') if disposition else stream.forced,
                             channels=stream.channels,
                             channel_layout=last_option(argv,('-channel_layout','-channel_layout:'+selector,
                                            '-channel_layout:{}:{}'.format(selector,index)),stream.channel_layout)
                                            if kind == 'audio' else '',
                             filename=stream.filename,mimetype=stream.mimetype))
        expected[kind] = rows
    encoder = last_option(argv, ('-c:v','-codec:v','-vcodec','-c','-codec'), 'copy')
    pix_fmt = last_option(argv, ('-pix_fmt',), '')
    expected['video'] = {'codec':source.video.codec if encoder == 'copy' else codec_names.get(encoder,encoder),
                         'bit_depth':source.video.bit_depth if encoder == 'copy' else 10 if '10' in pix_fmt else 8,
                         'tag':last_option(argv, ('-tag:v',), '')}
    return expected


def _stream_check(result, info, expected, keep_metadata):
    problems = []
    video = expected['video']
    if len(info.video_streams) != 1 or info.video is None:
        problems.append('真实视频轨数量不为 1')
    elif info.video.codec != video['codec'] or video['bit_depth'] and info.video.bit_depth != video['bit_depth']:
        problems.append('视频编码／位深与计划不一致')
    elif video['tag'] and info.video.codec_tag != video['tag']:
        problems.append('视频 codec 标签与计划不一致')
    for kind, label in (('audio','音轨'),('subtitle','字幕'),('attachment','附件')):
        actual = [s for s in info.streams if s.kind == kind]
        wanted = expected[kind]
        if len(actual) != len(wanted):
            problems.append('{}数量 {}→{}'.format(label,len(wanted),len(actual)))
            continue
        for index,(got,row) in enumerate(zip(actual,wanted)):
            fields = ['codec']
            if kind != 'attachment':
                fields += ['default','forced']
            if kind == 'audio' and row['channels']:
                fields += ['channels']
            if kind == 'audio' and row['channel_layout']:
                fields += ['channel_layout']
            if keep_metadata:
                fields += ['filename','mimetype'] if kind == 'attachment' else ['language','title']
            for field in fields:
                # Absent source language/title impose no invented metadata requirement.
                if field in ('language','title') and not row[field]:
                    continue
                if getattr(got,field) != row[field]:
                    problems.append('{} {} 的 {} 与计划不一致'.format(label,index+1,field))
    result.checks.append(Check('V4a','编码与轨道身份','fail' if problems else 'ok',
                               '；'.join(problems) or '编码／位深、轨道顺序、语言、标题、标记及附件符合计划'))
    if problems:
        result.verdict = 'failed'

# Hard budget from specs/ENCODING_PROFILES.md §4.
MAX_KEYFRAME_GAP_S = 2.0
DURATION_TOLERANCE_S = 1.0
DURATION_TOLERANCE_PCT = 0.5


@dataclass
class Check:
    id: str
    label: str
    status: str            # ok | warn | fail | skipped
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "status": self.status, "detail": self.detail}


@dataclass
class VerifyResult:
    verdict: str = "passed"          # passed | risky | failed
    checks: List[Check] = field(default_factory=list)
    max_keyframe_gap: float = 0.0
    keyframe_count: int = 0
    duration: float = 0.0
    size_bytes: int = 0
    #: Delivered loudness, when normalisation was requested (G1): measured with ebur128
    #: on the output, so the report can state what the file actually is.
    loudness: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "checks": [c.to_dict() for c in self.checks],
            "max_keyframe_gap": self.max_keyframe_gap,
            "keyframe_count": self.keyframe_count,
            "duration": self.duration,
            "size_bytes": self.size_bytes,
            "loudness": self.loudness,
        }


def keyframe_times(path: str) -> Tuple[List[float], List[float]]:
    """Return (keyframe_timestamps, []) for the first video stream.

    `-skip_frame nokey` makes decoders emit only keyframes, so the scan costs
    milliseconds on multi-GB files instead of decoding every frame.
    """
    caps = get_caps()
    if not caps.available():
        return [], []
    argv = [
        caps.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-show_entries", "frame=key_frame,best_effort_timestamp_time",
        "-of", "json", path,
    ]
    try:
        proc = work.run(argv, timeout=1800, **subprocess_kwargs())
        if proc.returncode != 0:
            return [], []
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], []

    keys: List[float] = []
    for frame in data.get("frames") or []:
        try:
            ts = float(frame.get("best_effort_timestamp_time"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(ts) and int(frame.get("key_frame", 0) or 0) == 1:
            keys.append(ts)
    return keys, []


def keyframe_times_retry(path: str, attempts: int = 6,
                         delay: float = 0.4) -> Tuple[List[float], str]:
    """keyframe_times() with retries.

    A freshly written MP4 is not readable until its `moov` atom exists, which
    `-movflags +faststart` only writes at the very end of the encode.  Probing
    such a file too early fails with "moov atom not found" instead of blocking,
    so the caller retries rather than trusting the first empty result.
    """
    last_err = ""
    for attempt in range(attempts):
        keys, _ = keyframe_times(path)
        if keys:
            return keys, ""
        last_err = "第 {} 次扫描未取到关键帧".format(attempt + 1)
        if attempt < attempts - 1:
            time.sleep(delay)
    return [], last_err


def _max_gap(times: List[float], duration: float) -> float:
    """Largest interval between consecutive keyframes, including the tail."""
    if not math.isfinite(duration) or duration <= 0 or any(not math.isfinite(t) for t in times):
        return float('inf')
    if not times:
        return duration if duration > 0 else float("inf")
    marks = sorted(times)
    gap = marks[0]                       # from 0 s to the first keyframe
    for prev, cur in zip(marks, marks[1:]):
        gap = max(gap, cur - prev)
    if duration > 0:
        gap = max(gap, duration - marks[-1])
    return gap


def _metadata_check(result: "VerifyResult", out: Path, source: Optional[MediaInfo],
                    info: MediaInfo, keep_timestamps: bool, keep_metadata: bool = True) -> None:
    """V4b -- did the container metadata and file timestamps survive? (F7)

    Remux and re-encode use the same checks: both can lose chapters and
    `creation_time`. Explicitly stripped metadata is excluded from the expectation.
    """
    if source is None:
        return
    notes: List[str] = []
    lost = False
    if keep_metadata and source.creation_time:
        from datetime import datetime
        def normalised(value):
            try:
                return datetime.fromisoformat(value.replace('Z','+00:00'))
            except ValueError:
                return value
        if info.creation_time and normalised(info.creation_time) == normalised(source.creation_time):
            notes.append("拍摄时间 {}".format(info.creation_time[:19]))
        else:
            notes.append("拍摄时间丢失或改变（源为 {}）".format(source.creation_time[:19]))
            lost = True
    if keep_metadata and source.chapters:
        got, wanted = len(info.chapters), len(source.chapters)
        matches = got == wanted and all(
            (a.get('tags') or {}).get('title','') == (b.get('tags') or {}).get('title','')
            # Muxer time-base rounding and AAC priming can shift boundaries ~21 ms.
            and abs(float(a.get('start_time',0)) - float(b.get('start_time',0))) <= 0.05
            and abs(float(a.get('end_time',0)) - float(b.get('end_time',0))) <= 0.05
            for a,b in zip(info.chapters,source.chapters))
        if matches:
            notes.append("章节 {} 个".format(got))
        else:
            notes.append("章节数量、标题或时间改变（{} → {}）".format(wanted, got))
            lost = True
    if keep_timestamps:
        try:
            delta = abs(out.stat().st_mtime - (source.mtime or 0.0))
        except OSError:
            delta = 1e9
        if source.mtime and delta <= 2.0:
            notes.append("文件时间已保留")
        else:
            notes.append("文件时间未保留")
            lost = True
    result.checks.append(Check(
        "V4b", "元数据与时间戳", "warn" if lost else ("ok" if notes else "skipped"),
        "；".join(notes) or "源没有可对比的元数据"))
    if lost and result.verdict == "passed":
        result.verdict = "risky"


def verify(output_path: str, source: Optional[MediaInfo] = None,
           skip_decode: bool = False, on_progress=None,
           deep: bool = False,
           loudness: Optional[float] = None,
           loudness_tp: Optional[float] = None,
           keep_timestamps: bool = False,
           reencoded: bool = True,
           expected_audio: Optional[int] = None,
           expected_subtitles: Optional[int] = None,
           keep_metadata: bool = True,
           expected_streams: Optional[Dict[str, Any]] = None) -> VerifyResult:
    """Run the delivery gate.

    `on_progress(payload)` receives {"stage", "frames", "percent"} while the slow
    parts run, so a multi-gigabyte file no longer looks frozen during verification.

    `deep=False` (default) scans packets instead of decoding every frame: it still
    checks the container, the packet structure and every keyframe, at disk speed.
    `deep=True` decodes the whole video as well, which is the only way to catch
    corruption *inside* the entropy-coded stream -- at roughly real decode cost.
    The V1b check label always states which of the two actually ran.

    `loudness` (a LUFS target) asks for one extra check: the DELIVERED file is measured
    with `ebur128` and compared against the target. It is deliberately our own meter and
    not `loudnorm`'s self-report, which was measured 1.4 dB optimistic on a high-crest
    clip -- a normalisation claim nobody re-measured is exactly the kind of number this
    project does not ship.

    `reencoded` says whether the video stream was actually re-encoded. It gates the ONE
    relaxation V2 allows: a remux inherits the source's keyframe layout, so its gap is not
    the tool's fault. A re-encode has no such excuse -- see the V2 block.
    """
    def progress(stage: str, frames: int = 0, percent: Optional[float] = None) -> None:
        if not on_progress:
            return
        try:
            on_progress({"stage": stage, "frames": frames, "percent": percent})
        except Exception:  # noqa: BLE001 - reporting must never break verification
            pass

    result = VerifyResult()
    out = Path(output_path)
    if not out.is_file():
        result.checks.append(Check("V0", "输出文件存在", "fail", "文件未生成"))
        result.verdict = "failed"
        return result
    result.size_bytes = out.stat().st_size

    progress("probe")
    info = probe(str(out))
    result.duration = info.duration

    # V1 -- decodable & has a video stream
    if not info.ok or not math.isfinite(info.duration) or info.duration <= 0:
        result.checks.append(Check("V1", "输出可解析", "fail", info.error or "无法确定有效输出时长"))
        result.verdict = "failed"
        return result
    result.checks.append(Check(
        "V1", "输出可解析",
        "ok",
        "{} {}x{} {:.2f}s".format(
            (info.video.codec if info.video else "-").upper(),
            info.video.width if info.video else 0,
            info.video.height if info.video else 0,
            info.duration,
        ),
    ))

    expected_frames = 0
    if info.video and math.isfinite(info.video.fps) and info.video.fps > 0:
        expected_frames = int(info.duration * info.video.fps)

    frames = 0
    keys: List[float] = []
    if not skip_decode:
        # ONE pass: read the whole stream while reporting progress. The old code
        # ran a blocking -count_frames pass that printed nothing, followed by a
        # separate keyframe scan -- on a large file that looked like a hang.
        from .probe import scan_stream

        scan_stage = "decode" if deep else "scan"

        def on_scan(seen: int) -> None:
            percent = (seen / expected_frames * 100.0) if expected_frames else None
            progress(scan_stage, seen, percent)

        progress(scan_stage, 0, 0.0 if expected_frames else None)
        scan = scan_stream(str(out), on_progress=on_scan, decode=deep, media=info)
        frames, keys, scan_errors = scan.frames, scan.keys, scan.errors
        label = "整片解码" if deep else "整片读取"
        if scan_errors:
            result.checks.append(Check("V1b", label, "fail",
                                       "；".join(scan_errors[:3])[:300]))
            result.verdict = "failed"
            return result
        if frames <= 0:
            result.checks.append(Check("V1b", label, "fail", "读取到 0 帧"))
            result.verdict = "failed"
            return result
        if deep:
            detail = "{} 帧全部解码成功".format(frames)
        else:
            detail = "{} 帧全部读取成功（包级扫描，未逐帧解码）".format(frames)
        if scan.notes:
            detail += "；忽略未知输出 {}".format(scan.notes[0][:60])
        result.checks.append(Check("V1b", label, "ok", detail))
        if deep and any(s.codec in ('hdmv_pgs_subtitle', 'dvd_subtitle') for s in info.subtitle_streams):
            from .probe import scan_bitmap_subtitles
            progress('subtitle_decode', 0)
            subtitles = scan_bitmap_subtitles(str(out), info,
                on_progress=lambda count: progress('subtitle_decode', count))
            if subtitles.errors:
                result.checks.append(Check('V1s', '图像字幕解码', 'fail',
                                           '；'.join(subtitles.errors[:3])[:500]))
                result.verdict = 'failed'
                return result
            detail = '{} 个字幕显示／清屏事件已解码'.format(subtitles.frames)
            if subtitles.notes:
                detail += '；' + '；'.join(subtitles.notes)
                if result.verdict == 'passed':
                    result.verdict = 'risky'
            result.checks.append(Check('V1s', '图像字幕解码',
                                       'warn' if subtitles.notes else 'ok', detail))
    else:
        keys, _ = keyframe_times(str(out))

    # V2 -- THE check: keyframe spacing.
    # The budget is the 2 s compatibility line, but never stricter than what the
    # source itself offers: a remux/no-op pass cannot invent keyframes, so for
    # those the source's own spacing is the effective gate and we say so.
    progress("keyframes")
    result.keyframe_count = len(keys)
    gap = _max_gap(keys, info.duration)
    result.max_keyframe_gap = round(gap, 3) if gap != float("inf") else 9999.0

    source_gap = None
    if not reencoded and source is not None and source.ok:
        # Packet read first (0.8 s on a 734 MB source vs 10.4 s to decode it); the
        # decode-based scan stays as a fallback for containers whose packets carry
        # no keyframe flag.
        from .probe import packet_keyframe_times
        source_keys = packet_keyframe_times(source.path)
        if not source_keys:
            source_keys, _ = keyframe_times(source.path)
        if source_keys:
            source_gap = _max_gap(source_keys, source.duration)

    # Only timestamp rounding may exceed the reported hard limit.
    tolerance = 1e-6  # floating-point timestamp rounding, never a whole late frame
    budget = MAX_KEYFRAME_GAP_S
    note = ""
    # The relaxation exists for REMUX only: a copied stream keeps the source's keyframe
    # layout, so blaming the tool for the source's own 20 s GOP would be wrong. It must
    # never apply to a RE-ENCODE -- there, a long gap means something bypassed the -g /
    # force_key_frames this project always sets, and that is precisely the defect the
    # whole tool exists to prevent. The previous condition inferred "not re-encoded" from
    # gap arithmetic alone and asserted it in the report.
    if not reencoded and source_gap is not None and source_gap > MAX_KEYFRAME_GAP_S:
        budget = source_gap
        note = "（仅重封装、未重编码，关键帧结构继承自源：{:.2f}s）".format(source_gap)

    if not keys:
        result.checks.append(Check("V2", "关键帧间隔", "fail",
                                   "未检测到任何关键帧，播放器无法定位，拖动进度条会卡死"))
        result.verdict = "failed"
    elif math.isfinite(gap) and gap <= budget + tolerance:
        shown = MAX_KEYFRAME_GAP_S if not note else round(budget, 2)
        detail = "共 {} 个关键帧，最大间隔 {:.2f}s（上限 {:.1f}s）".format(
            len(keys), gap, shown)
        if note:
            detail += note
        result.checks.append(Check("V2", "关键帧间隔",
                                   "warn" if note else "ok", detail))
        if note and result.verdict == 'passed':
            result.verdict = 'risky'
    else:
        result.checks.append(Check("V2", "关键帧间隔", "fail",
                                   "最大间隔 {:.2f}s，超过 {:.1f}s 上限：拖动进度条可能卡死".format(
                                       gap, MAX_KEYFRAME_GAP_S)))
        result.verdict = "failed"

    # V3 -- duration preserved
    if source and source.ok and math.isfinite(source.duration) and source.duration > 0:
        delta = abs(info.duration - source.duration)
        tol = max(DURATION_TOLERANCE_S, source.duration * DURATION_TOLERANCE_PCT / 100.0)
        if delta <= tol:
            result.checks.append(Check("V3", "时长一致", "ok",
                                       "源 {:.2f}s → 输出 {:.2f}s（差 {:.2f}s）".format(
                                           source.duration, info.duration, delta)))
        else:
            result.checks.append(Check("V3", "时长一致", "fail",
                                       "源 {:.2f}s vs 输出 {:.2f}s，差 {:.2f}s 超出容差 {:.2f}s".format(
                                           source.duration, info.duration, delta, tol)))
            result.verdict = "failed"
    else:
        result.checks.append(Check("V3", "时长一致", "warn", "缺少有效源时长，无法确认完整交付"))
        if result.verdict == 'passed':
            result.verdict = 'risky'

    # V4 -- tracks preserved
    if source:
        wanted_audio = len(source.audio_streams) if expected_audio is None else expected_audio
        wanted_subs = len(source.subtitle_streams) if expected_subtitles is None else expected_subtitles
        if wanted_audio == len(info.audio_streams) and wanted_subs == len(info.subtitle_streams):
            result.checks.append(Check("V4", "音轨/字幕轨", "ok",
                                       "音轨 {}→{}，字幕轨 {}→{}".format(
                                           wanted_audio, len(info.audio_streams),
                                           wanted_subs, len(info.subtitle_streams))))
        else:
            result.checks.append(Check("V4", "音轨/字幕轨", "fail",
                                       "预期音轨/字幕 {} / {}，实际 {} / {}".format(
                                           wanted_audio, wanted_subs,
                                           len(info.audio_streams), len(info.subtitle_streams))))
            result.verdict = 'failed'
    else:
        result.checks.append(Check("V4", "音轨/字幕轨", "skipped", "缺少源信息"))

    if expected_streams is not None:
        _stream_check(result, info, expected_streams, keep_metadata)

    # V5 -- size benefit (informational, never a failure)
    if source and source.size_bytes and result.size_bytes:
        ratio = result.size_bytes / float(source.size_bytes)
        if ratio <= 0.98:
            result.checks.append(Check("V5", "体积收益", "ok",
                                       "{:.1f} MB → {:.1f} MB（压缩到 {:.0f}%）".format(
                                           source.size_bytes / 1048576.0,
                                           result.size_bytes / 1048576.0, ratio * 100)))
        else:
            result.checks.append(Check("V5", "体积收益", "warn",
                                       "输出未变小（{:.0f}%）：该片源已足够紧凑或需换更弱档位".format(ratio * 100)))
            if result.verdict == "passed":
                result.verdict = "risky"
    else:
        result.checks.append(Check("V5", "体积收益", "skipped", "缺少体积信息"))

    # V4b -- metadata and file timestamps (F7). The container claims are easy to make and
    # easy to lose silently: `-map_metadata 0` preserves `creation_time` and chapters
    # TODAY (measured), and this check is what keeps a future flag change from quietly
    # dropping them. File timestamps are compared only when they were requested.
    _metadata_check(result, out, source, info, keep_timestamps, keep_metadata)

    # L1 -- loudness normalisation, measured on the DELIVERED file (G1). Only runs when
    # the user asked for normalisation, and it uses our own `ebur128` measurement rather
    # than `loudnorm`'s self-report -- which was measured 1.4 dB optimistic on the true
    # peak of a high-crest clip. A normalisation claim nobody re-measured is exactly the
    # kind of number this project does not ship.
    if loudness is not None and float(loudness) < 0:
        progress("loudness")
        try:
            delivered = audio.measure_output(str(out))
            status, detail = audio.verdict(delivered, float(loudness), loudness_tp)
            result.checks.append(Check("L1", "响度标准化", status, detail))
            if status == "warn" and result.verdict == "passed":
                result.verdict = "risky"
            result.loudness = delivered.to_dict()
        except Exception as exc:  # noqa: BLE001 - preserve a usable file but expose missing evidence
            result.checks.append(Check("L1", "响度标准化", "warn",
                                       "成品响度未能测量：{}".format(exc)))
            if result.verdict == 'passed':
                result.verdict = 'risky'

    if result.verdict == "passed":
        for check in result.checks:
            if check.status == "warn":
                result.verdict = "risky"
                break
    return result
