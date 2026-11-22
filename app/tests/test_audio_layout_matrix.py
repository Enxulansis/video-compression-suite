"""Opt-in isolated audio layout/tone matrix, each step bounded to 15 seconds."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'backend'))
from core import memory as M, planner as P, verifier as V
import importlib
B=importlib.import_module('core.probe')
AUDIO_CASE_RESERVE_MB=256


class ResourceDeferred(RuntimeError):
    pass


def run(argv,binary=False):
    gate=M.admission(M.machine_memory(),0,AUDIO_CASE_RESERVE_MB)
    if not gate['ok']:
        raise ResourceDeferred(gate['reason'])
    proc=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    deadline=time.monotonic()+15
    try:
        while True:
            try:
                stdout,stderr=proc.communicate(timeout=min(0.1,max(0.001,deadline-time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic()>=deadline:
                    raise TimeoutError('合成音频步骤超过 15 秒')
                mem=M.machine_memory()
                commit_free=mem.get('commit_limit_mb',0)-mem.get('commit_used_mb',0)
                usage=M.process_memory_mb(proc.pid)
                if (not mem.get('available') or mem['free_mb']<M.SAFETY_MARGIN_MB or
                    commit_free<M.SAFETY_MARGIN_MB or usage and max(usage.values())>AUDIO_CASE_RESERVE_MB):
                    raise ResourceDeferred('运行中余量不足、读数不可用或测试进程超过 256 MB')
        if proc.returncode:
            raise AssertionError(stderr.decode('utf-8',errors='replace')[-3000:])
        return stdout if binary else stdout.decode('utf-8')
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)


def probe(path,ffprobe):
    raw=json.loads(run([ffprobe,'-v','error','-show_streams','-show_format','-show_chapters',
                        '-of','json',str(path)]))
    with patch.object(B,'_probe_json',return_value=raw):
        info=B.probe(str(path))
    assert info.ok,info.error
    return info


def tones(channels,layout):
    # Keep LFE below its bandwidth and give every other channel a distinct tone.
    frequencies=[300+120*i for i in range(channels)]
    if 'LFE' in layout or layout.startswith(('5.1','6.1','7.1')):
        frequencies[3]=50
    return frequencies


def main():
    if not os.environ.get('VCS_TEST_GATE') or not os.environ.get('VCS_STORE_DIR'):
        raise SystemExit('必须通过隔离审查 runner 执行')
    ffmpeg,ffprobe=shutil.which('ffmpeg'),shutil.which('ffprobe')
    assert ffmpeg and ffprobe
    root=Path(os.environ['TEMP'])/'audio_layout_matrix'
    root.mkdir(exist_ok=False)
    specs=[('opus',n,layout) for n,layout in ((1,'mono'),(2,'stereo'),(3,'3.0'),(4,'quad'),
           (5,'5.0'),(6,'5.1'),(7,'6.1'),(8,'7.1'),(5,'5.0(side)'),(6,'5.1(side)'))]
    specs += [('aac',6,'5.1(side)'),('aac',8,'7.1'),('copy',8,'7.1(wide)')]
    rows=[]
    for index,(codec,channels,layout) in enumerate(specs):
        gate=M.admission(M.machine_memory(),0,AUDIO_CASE_RESERVE_MB)
        label=f'{codec}-{layout}'
        if not gate['ok']:
            rows.append({'case':label,'status':'skipped','reason':gate['reason']})
            print(json.dumps(rows[-1],ensure_ascii=False),flush=True)
            continue
        # MOV's channel atom preserves PCM layouts; native Matroska PCM does not.
        source=root/f'source_{index}.mov'
        freq=tones(channels,layout)
        expressions='|'.join(f'0.05*sin(2*PI*{f}*t)' for f in freq)
        run([ffmpeg,'-hide_banner','-v','error','-nostdin','-n','-filter_threads','1',
             '-f','lavfi','-i','testsrc2=size=160x90:rate=12:duration=1',
             '-f','lavfi','-i',f'aevalsrc=exprs={expressions}:sample_rate=48000:duration=1:channel_layout={layout}',
             '-map','0:v','-map','1:a','-c:v','libx264','-preset','ultrafast',
             '-threads','1','-pix_fmt','yuv420p','-g','12','-c:a','pcm_s16le',str(source)])
        digest=hashlib.sha256(source.read_bytes()).hexdigest()
        info=probe(source,ffprobe)
        assert info.audio_streams[0].channel_layout==layout,(label,info.audio_streams[0])
        if codec=='copy':
            container='mkv'
            overrides=P.Overrides(audio_mode='copy')
        else:
            container='mp4' if codec=='aac' else 'mkv'
            overrides=P.Overrides(audio_mode='transcode')
        output=root/f'out_{index}.{container}'
        argv=[ffmpeg,'-hide_banner','-v','error','-nostdin','-n','-i',str(source),
              '-map','0:0','-c:v','copy']+P._audio_args(info,container,overrides)+[str(output)]
        run(argv)
        actual=probe(output,ffprobe)
        expected=V.stream_expectations(info,argv)
        verification=V.VerifyResult()
        V._stream_check(verification,actual,expected,False)
        if codec=='copy':
            # Native Matroska PCM loses the source's nonstandard speaker mask.
            # The delivery gate must refuse that loss, even with eight channels.
            assert not actual.audio_streams[0].channel_layout,actual.audio_streams[0]
            assert verification.verdict=='failed',verification.checks
            assert any('channel_layout' in c.detail for c in verification.checks)
            assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
            rows.append({'case':label,'status':'rejected_at_delivery','channels':channels,
                         'source_layout':layout,'output_layout':actual.audio_streams[0].channel_layout,
                         'reason':'Matroska PCM 未保留布局，交付校验拒绝','source_sha256':digest})
            (root/'matrix.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(rows[-1],ensure_ascii=False),flush=True)
            continue
        assert verification.verdict=='passed',(label,verification.checks)
        decode=[ffmpeg,'-hide_banner','-v','error','-nostdin']
        if container=='mkv' and codec=='opus':
            decode+=['-fflags','+noparse+nofillin']
        decode+=['-i',str(output),'-map','0:a:0','-vn','-c:a','pcm_f32le','-ar','48000','-f','f32le','-']
        pcm=run(decode,True)
        samples=[v[0] for v in struct.iter_unpack('<f',pcm)]
        measured=[]
        for channel,f in enumerate(freq):
            signal=samples[channel::channels][int(0.2*48000):int(0.8*48000)]
            assert len(signal)>28000 and all(math.isfinite(v) for v in signal),label
            crossings=sum(a<=0<b for a,b in zip(signal,signal[1:]))
            found=crossings*48000/(len(signal)-1)
            assert abs(found-f)<8,(label,channel,f,found)
            measured.append(round(found,2))
        assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
        rows.append({'case':label,'status':'passed','channels':channels,
                     'source_layout':layout,'output_layout':actual.audio_streams[0].channel_layout,
                     'expected_frequencies':freq,'observed_frequencies':measured,'source_sha256':digest})
        (root/'matrix.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(rows[-1],ensure_ascii=False),flush=True)
    (root/'matrix.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    # A partially executed matrix cannot be advertised as a complete pass.
    return 77 if any(row['status']=='skipped' for row in rows) else 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except ResourceDeferred as exc:
        destination=Path(os.environ['TEMP'])/'audio_layout_matrix'/'deferred.json'
        destination.write_text(json.dumps({'status':'deferred','reason':str(exc)},ensure_ascii=False),encoding='utf-8')
        print('NOT_FULLY_VERIFIED: '+str(exc),flush=True)
        raise SystemExit(77)
