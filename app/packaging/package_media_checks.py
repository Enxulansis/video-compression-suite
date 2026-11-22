"""Small real-media regression for the bundled shared FFmpeg, private fixtures only."""
from __future__ import annotations

import math
from pathlib import Path
import subprocess


def run(root, source, initial_output, source_info, check, result):
    from core.caps import get_caps, subprocess_kwargs
    from core.planner import get_profile, make_plan, Overrides
    from core.probe import probe
    from core.verifier import verify
    from core import repair, quality, frames

    caps = get_caps(wait_for_all=True)
    result['hardware_runtime_probes'] = caps.hardware_probes
    outputs = {}

    def encode(argv, label):
        proc = subprocess.run(argv, timeout=25, **subprocess_kwargs())
        check(label + ' encode', proc.returncode == 0, proc.stderr[-1200:])

    def delivery(path, info, label, **options):
        verified = verify(str(path), source=info, deep=True, **options)
        check(label + ' delivery', verified.verdict == 'passed', verified.to_dict())
        actual = probe(str(path))
        check(label + ' seek frame', frames.extract(str(path), .6, timeout=8).startswith(b'\xff\xd8'))
        return actual

    for profile_id in ('P7', 'P5', 'P1', 'P3', 'P4'):
        profile = get_profile(profile_id)
        encoder = profile['encoder']
        if encoder.endswith('_nvenc') and not caps.has_encoder(encoder):
            result.setdefault('not_tested_hardware', []).append(encoder)
            continue
        profile = dict(profile, extra=dict(profile.get('extra') or {}))
        for key, limit in (('svtav1-params', 'lp=2'), ('x265-params', 'pools=1:frame-threads=1')):
            if key in profile['extra']:
                profile['extra'][key] += ':' + limit
        preset = '12' if encoder == 'libsvtav1' else 'ultrafast' if encoder == 'libx265' else None
        plan = make_plan(source_info, profile, Overrides(location='custom', out_dir=str(root),
                         name_template='shared_' + profile_id, preset=preset,
                         keep_timestamps=False, extra_args=['-threads', '1']), caps=caps)
        encode(plan.argv, profile_id)
        actual = delivery(plan.out_path, source_info, profile_id)
        expected = 'av1' if 'av1' in encoder else 'hevc' if encoder in ('libx265', 'hevc_nvenc') else 'h264'
        check(profile_id + ' actual codec', actual.video.codec == expected, actual.video.codec)
        outputs[profile_id] = plan.out_path

    # A/V/subtitle mapping exercises demux, AAC and text-subtitle conversion DLLs.
    subtitle = root / 'synthetic subtitles.srt'
    subtitle.write_text('1\n00:00:00,200 --> 00:00:01,500\nOffline shared runtime\n', encoding='utf-8')
    multiplexed = root / '合成 音轨字幕.mkv'
    encode([caps.ffmpeg, '-hide_banner', '-nostdin', '-n', '-i', str(source),
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
            '-i', str(subtitle), '-map', '0:v:0', '-map', '1:a:0', '-map', '2:s:0',
            '-c:v', 'copy', '-c:a', 'aac', '-b:a', '64k', '-c:s', 'srt',
            '-t', '2', '-threads', '1', str(multiplexed)], 'A/V/subtitle source')
    info = probe(str(multiplexed))
    plan = make_plan(info, get_profile('P2'), Overrides(location='custom', out_dir=str(root),
                     name_template='shared_tracks', container='mp4', preset='veryfast',
                     crf=30, keep_timestamps=False, extra_args=['-threads', '1']), caps=caps)
    encode(plan.argv, 'A/V/subtitle conversion')
    actual = delivery(plan.out_path, info, 'A/V/subtitle conversion')
    check('AAC audio retained', [s.codec for s in actual.audio_streams] == ['aac'])
    check('SRT to MP4 subtitles', [s.codec for s in actual.subtitle_streams] == ['mov_text'])

    # A deliberately poor GOP is a generated diagnostic input, never a user file.
    broken = root / '合成 长GOP.mp4'
    encode([caps.ffmpeg, '-hide_banner', '-nostdin', '-n', '-f', 'lavfi', '-i',
            'testsrc2=size=160x96:rate=10:duration=4', '-c:v', 'libx264', '-preset',
            'ultrafast', '-crf', '12', '-g', '1000', '-keyint_min', '1000', '-sc_threshold',
            '0', '-threads', '1', str(broken)], 'long-GOP fixture')
    diag = repair.diagnose(str(broken))
    check('repair diagnoses long GOP', diag.ok and diag.verdict == 'broken', diag.to_dict())
    repair_plan = repair.build_repair_plan(diag, 'compat264',
                   {'location': 'custom', 'out_dir': str(root), 'crf': 30, 'gop_seconds': 1})
    encode(repair_plan['argv'], 'repair')
    delivery(repair_plan['out_path'], probe(str(broken)), 'repair')
    fixed = repair.diagnose(repair_plan['out_path'])
    check('repair produces seekable output', fixed.verdict == 'seekable', fixed.to_dict())

    measured = quality.measure(str(initial_output), str(source), source_info, seconds=1)
    check('VMAF shared filter execution', measured.ok and math.isfinite(measured.score)
          and 0 <= measured.score <= 100, measured.to_dict())
    diff = frames.difference(str(source), str(initial_output), .6, size=(160, 96),
                             size_a=(160, 96), size_b=(160, 96), timeout=8)
    check('shared difference-frame filter', diff.startswith(b'\xff\xd8'))
    result['media_outputs'] = outputs
