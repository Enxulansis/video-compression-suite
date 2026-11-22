"""Serial small synthetic encodes for the new quality goals, with real memory gates."""
import copy
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

WORK = Path(tempfile.mkdtemp(prefix='vcs-simple-smoke-'))
os.environ['VCS_STORE_DIR'] = str(WORK / 'store')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))
from core import caps as C, memory as M, planner as P
from core.probe import probe
from core.verifier import verify, stream_expectations
from core import work
from contextlib import nullcontext
from types import SimpleNamespace

observations = []


def admit(need):
    memory = M.machine_memory()
    gate = M.admission(memory, 0, need + 64)
    if not gate['ok'] or not memory.get('commit_limit_mb'):
        (WORK / 'deferred.json').write_text(json.dumps({'memory': memory, 'gate': gate}, indent=2), encoding='utf-8')
        print(json.dumps({'deferred': True, 'work': str(WORK), 'reason': gate['reason'] or 'unknown commit'}), flush=True)
        raise SystemExit(77)


def run(argv, timeout=20, need=512):
    admit(need)
    destination = WORK / ('process-' + str(len(observations)) + '.log')
    minimum = 999999
    with destination.open('w', encoding='utf-8') as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + timeout
        try:
            while proc.poll() is None:
                memory = M.machine_memory(); minimum = min(minimum, memory.get('free_mb', 0))
                if not memory.get('available') or memory.get('free_mb', 0) < M.SAFETY_MARGIN_MB or not memory.get('commit_limit_mb') or memory['commit_limit_mb'] - memory['commit_used_mb'] < M.SAFETY_MARGIN_MB:
                    raise RuntimeError('live safety margin reached; stopping own isolated child')
                if time.monotonic() >= deadline: raise TimeoutError('isolated process deadline')
                time.sleep(.05)
            if proc.returncode: raise AssertionError(destination.read_text(encoding='utf-8')[-4000:])
        finally:
            if proc.poll() is None: proc.kill()
            proc.wait(timeout=5)
    observations.append({'minimum_free_mb': minimum, 'timeout': timeout, 'argv': argv})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--extra-only', action='store_true')
    args = parser.parse_args()
    # Admission also covers the bounded capability probe's synthetic frames.
    admit(1024)
    caps = C.get_caps()
    assert caps.available()
    source = WORK / 'generated.mkv'
    subtitles = WORK / 'fixture.srt'
    subtitles.write_text('1\n00:00:00,100 --> 00:00:03,800\nIsolated text subtitle\n', encoding='utf-8')
    run([caps.ffmpeg, '-hide_banner', '-v', 'error', '-nostdin', '-n',
         '-f', 'lavfi', '-i', 'testsrc2=size=432x240:rate=24:duration=4',
         '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4',
         '-i', str(subtitles), '-map', '0:v', '-map', '1:a', '-map', '2:s',
         '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '10', '-g', '24',
         '-threads', '1', '-filter_threads', '1', '-c:a', 'aac', '-c:s', 'srt', str(source)])
    info = probe(str(source))
    assert info.ok, info.error
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    rows = []
    # Each mode exercises all three goals through the real resolver.
    variants = [('broad-hardware', 'broad', False, False),
                ('modern-hardware', 'modern', False, False),
                ('av1-hardware', 'av1', False, False),
                ('broad-software', 'broad', False, True),
                ('modern-software', 'modern', False, True)]
    if args.extra_only: variants = [('av1-software', 'av1', True, True)]
    for label, compatibility, slow, software in variants:
        selected_caps = copy.deepcopy(caps)
        if software:
            selected_caps.encoders = [n for n in selected_caps.encoders if not n.endswith(('_nvenc', '_qsv', '_amf'))]
        group = []
        for goal in (('strong',) if args.extra_only else ('light', 'balanced', 'strong')):
            intent = {'goal': goal, 'compatibility': compatibility, 'slow_av1': slow}
            plan = P.make_plan(info, P.get_profile('P3'), P.Overrides(intent=intent, location='custom',
                               out_dir=str(WORK / (label + '-' + goal))), caps=selected_caps)
            # Keep the selected quality/preset, limit only test worker thread counts.
            argv = list(plan.argv)
            if plan.encoder == 'libx265':
                index = argv.index('-x265-params') + 1
                argv[index] += ':pools=1:frame-threads=1'
            if plan.encoder == 'libsvtav1':
                index = argv.index('-svtav1-params') + 1
                argv[index] += ':lp=2'
            argv[-1:-1] = ['-threads', '1', '-filter_threads', '1']
            Path(plan.out_path).parent.mkdir(parents=True, exist_ok=True)
            print(json.dumps({'case': label + '-' + goal, 'stage': 'encode', 'encoder': plan.encoder}), flush=True)
            run(argv, timeout=25, need=plan.est_memory_mb)
            expected = stream_expectations(info, argv)
            manager = SimpleNamespace(auxiliary_slot=lambda *a: nullcontext())
            admit(max(512, plan.est_memory_mb))
            with work.background(manager, timeout=20):
                work.note_media(info)
                result = verify(plan.out_path, info, deep=True, keep_timestamps=False,
                                expected_streams=expected, expected_audio=len(expected['audio']),
                                expected_subtitles=len(expected['subtitle']))
            assert result.verdict != 'failed', result.to_dict()
            assert all(c.status == 'ok' for c in result.checks if c.id in ('V1', 'V1b', 'V2', 'V3', 'V4', 'V4a')), result.to_dict()
            output = probe(plan.out_path)
            assert output.video.width == info.video.width and output.video.height == info.video.height
            assert len(output.audio_streams) == len(info.audio_streams)
            assert len(output.subtitle_streams) == len(info.subtitle_streams)
            assert result.max_keyframe_gap <= 2
            row = {'case': label + '-' + goal, 'encoder': plan.encoder, 'bytes': Path(plan.out_path).stat().st_size,
                   'quality': plan.resolved_overrides['crf'], 'gap': result.max_keyframe_gap, 'verdict': result.verdict}
            rows.append(row); group.append(row)
            print(json.dumps(row), flush=True)
        if len(group) == 3 and len({r['encoder'] for r in group}) == 1:
            assert group[0]['bytes'] >= group[1]['bytes'] >= group[2]['bytes'], group
    if args.extra_only:
        for label, hdr in (('styled-ass', False), ('hdr-ten-bit', True)):
            special = WORK / (label + '.mkv')
            argv = [caps.ffmpeg, '-hide_banner', '-v', 'error', '-nostdin', '-n', '-i', str(source),
                    '-map', '0:v', '-map', '0:a', '-map', '0:s', '-c:a', 'copy', '-c:s', 'ass' if not hdr else 'copy']
            if hdr:
                argv += ['-c:v', 'libx265', '-preset', 'ultrafast', '-crf', '18', '-pix_fmt', 'yuv420p10le',
                         '-x265-params', 'pools=1:frame-threads=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc', '-color_trc', 'smpte2084',
                         '-color_primaries', 'bt2020', '-colorspace', 'bt2020nc']
            else: argv += ['-c:v', 'copy']
            run(argv + ['-threads', '1', '-filter_threads', '1', str(special)])
            special_info = probe(str(special)); assert special_info.ok
            if hdr: assert special_info.is_hdr and special_info.video.bit_depth == 10, special_info.to_dict()
            before = hashlib.sha256(special.read_bytes()).hexdigest()
            plan = P.make_plan(special_info, P.get_profile('P3'), P.Overrides(
                intent={'goal': 'balanced'}, location='custom', out_dir=str(WORK / (label + '-out'))), caps=caps)
            Path(plan.out_path).parent.mkdir(parents=True, exist_ok=True)
            run(plan.argv, need=plan.est_memory_mb)
            actual = probe(plan.out_path)
            assert actual.ok and actual.video.width == special_info.video.width
            if hdr:
                assert actual.is_hdr and actual.video.bit_depth == 10, actual.to_dict()
                assert actual.video.color_transfer == special_info.video.color_transfer
                assert actual.video.color_primaries == special_info.video.color_primaries
            else:
                assert Path(plan.out_path).suffix == '.mkv'
                assert actual.subtitle_streams[0].codec == 'ass'
            expected = stream_expectations(special_info, plan.argv)
            admit(max(512, plan.est_memory_mb))
            with work.background(SimpleNamespace(auxiliary_slot=lambda *a: nullcontext()), timeout=20):
                work.note_media(special_info)
                verified = verify(plan.out_path, special_info, deep=True, keep_timestamps=False,
                                  expected_streams=expected, expected_audio=len(expected['audio']),
                                  expected_subtitles=len(expected['subtitle']))
            assert verified.verdict != 'failed', verified.to_dict()
            assert hashlib.sha256(special.read_bytes()).hexdigest() == before
            rows.append({'case': label, 'encoder': plan.encoder, 'container': Path(plan.out_path).suffix,
                         'hdr': actual.is_hdr, 'bit_depth': actual.video.bit_depth, 'verdict': verified.verdict})
            print(json.dumps(rows[-1]), flush=True)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    result = {'cases': rows, 'memory_observations': observations, 'source_sha256': digest,
              'note': 'Small SDR fixture only; no full-resolution quality or HDR calibration', 'work': str(WORK)}
    (WORK / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': True, 'cases': len(rows), 'work': str(WORK)}), flush=True)


if __name__ == '__main__': main()
