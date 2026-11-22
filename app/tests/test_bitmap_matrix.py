"""Real synthetic PGS/DVD packets, bounded media steps in an isolated review store."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_audio_layout_matrix import ResourceDeferred
from core import planner as P, verifier as V, memory as M
import importlib
B=importlib.import_module('core.probe')
RESERVE_MB=256


def check_resources(proc=None):
    mem=M.machine_memory()
    usage=M.process_memory_mb(proc.pid) if proc else None
    if (not mem.get('available') or mem['free_mb']<M.SAFETY_MARGIN_MB or
        mem.get('commit_limit_mb',0)-mem.get('commit_used_mb',0)<M.SAFETY_MARGIN_MB or
        usage and max(usage.values())>RESERVE_MB):
        raise ResourceDeferred('物理／提交余量不足或测试进程超过 256 MB')
    return mem


def safe_run(argv,*,timeout=15,**kwargs):
    gate=M.admission(check_resources(),0,RESERVE_MB)
    if not gate['ok']:
        raise ResourceDeferred(gate['reason'])
    proc=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    deadline=time.monotonic()+min(timeout,15)
    try:
        while True:
            try:
                stdout,stderr=proc.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic()>=deadline:
                    raise TimeoutError('字幕测试步骤超过 15 秒')
                check_resources(proc)
        if kwargs.get('text') or kwargs.get('encoding') or kwargs.get('universal_newlines'):
            stdout,stderr=stdout.decode('utf-8',errors='replace'),stderr.decode('utf-8',errors='replace')
        return subprocess.CompletedProcess(argv,proc.returncode,stdout,stderr)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)


def run(argv,binary=False):
    result=safe_run(argv)
    assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')[-3000:]
    assert not result.stderr.strip(),result.stderr.decode('utf-8',errors='replace')[-3000:]
    return result.stdout if binary else result.stdout.decode('utf-8')


ORIGINAL_STREAM=B.work.stream


@contextmanager
def safe_stream(argv,*,timeout,**kwargs):
    gate=M.admission(check_resources(),0,RESERVE_MB)
    if not gate['ok']:
        raise ResourceDeferred(gate['reason'])
    with ORIGINAL_STREAM(argv,timeout=min(timeout,15),**kwargs) as proc:
        stopped=threading.Event()
        failure=[]
        def monitor():
            while not stopped.wait(0.1):
                try:
                    check_resources(proc)
                except ResourceDeferred as exc:
                    failure.append(exc)
                    if proc.poll() is None:
                        proc.kill()
                    return
        watcher=threading.Thread(target=monitor,daemon=True)
        watcher.start()
        try:
            yield proc
        finally:
            stopped.set()
            watcher.join(timeout=2)
        if failure:
            raise failure[0]


def pgs_blob():
    # FFmpeg supdec.c/pgssubdec.c: seven real segments, one yellow bitmap and clear.
    def segment(pts,kind,payload):
        return b'PG'+struct.pack('>II',pts,0)+bytes([kind])+struct.pack('>H',len(payload))+payload
    show=bytes.fromhex('00 A0 00 5A 00 00 01 80 00 00 01 00 01 00 00 00 4C 00 29')
    window=bytes.fromhex('01 00 00 00 00 00 00 A0 00 5A')
    palette=bytes.fromhex('00 00 01 D2 92 10 FF')
    bitmap=bytes.fromhex('00 01 00 C0 00 00 2C 00 08 00 08')+bytes.fromhex('00 88 01 00 00')*8
    clear=bytes.fromhex('00 A0 00 5A 00 00 02 40 00 00 00')
    return b''.join(segment(pts,kind,payload) for pts,kind,payload in
        ((0,0x16,show),(0,0x17,window),(0,0x14,palette),(0,0x15,bitmap),
         (0,0x80,b''),(90000,0x16,clear),(90000,0x80,b'')))


def main():
    if not os.environ.get('VCS_TEST_GATE') or not os.environ.get('VCS_STORE_DIR'):
        raise SystemExit('必须通过隔离审查 runner 执行')
    ffmpeg,ffprobe=shutil.which('ffmpeg'),shutil.which('ffprobe')
    assert ffmpeg and ffprobe
    root=Path(os.environ['TEMP'])/'bitmap_matrix'
    root.mkdir(exist_ok=False)
    sup=root/'yellow.sup'
    sup.write_bytes(pgs_blob())
    digest=hashlib.sha256(sup.read_bytes()).hexdigest()
    log=[]
    def step(label,argv,binary=False):
        print(label,flush=True)
        value=run(argv,binary)
        if not binary:
            suffix='.json' if argv[0]==ffprobe and 'json' in argv else '.log'
            (root/(label+suffix)).write_text(value,encoding='utf-8')
        return value
    base=[ffmpeg,'-hide_banner','-v','error','-nostdin','-n','-filter_threads','1','-threads','1']
    step('pgs_frames',[ffprobe,'-v','error','-select_streams','s','-show_frames','-of','json',str(sup)])
    display=json.loads((root/'pgs_frames.json').read_text(encoding='utf-8'))['frames']
    assert [(f['pts_time'],f['num_rects']) for f in display]==[('0.000000',1),('1.000000',0)]
    dvd=root/'dvd.mkv'
    step('dvd_encode',base+['-fix_sub_duration','-i',str(sup),'-map','0:s','-c:s','dvdsub',str(dvd)])
    step('dvd_frames',[ffprobe,'-v','error','-select_streams','s','-show_frames','-of','json',str(dvd)])
    display=json.loads((root/'dvd_frames.json').read_text(encoding='utf-8'))['frames']
    assert len(display)==1 and display[0]['num_rects']==1
    assert abs(display[0]['end_display_time']-1000)<20,display
    source=root/'source.mkv'
    step('source_mux',base+['-f','lavfi','-i','color=c=black:size=160x90:rate=12:duration=2',
         '-i',str(sup),'-i',str(dvd),'-map','0:v','-map','1:s','-map','2:s',
         '-c:v','libx264','-preset','ultrafast','-threads','1','-g','12','-pix_fmt','yuv420p',
         '-c:s','copy','-metadata:s:s:0','language=zho','-metadata:s:s:0','title=PGS yellow',
         '-metadata:s:s:1','language=eng','-metadata:s:s:1','title=DVD yellow',
         '-disposition:s:0','default','-disposition:s:1','forced',str(source)])
    caps=SimpleNamespace(ffmpeg=ffmpeg,ffprobe=ffprobe,available=lambda:True,
                         has_encoder=lambda name:name=='libx264',has_filter=lambda name:True)
    with patch.object(B,'get_caps',return_value=caps), patch.object(B.work,'run',side_effect=safe_run), \
            patch.object(B.work,'stream',side_effect=safe_stream):
        # Raw fixture parser is bounded and uses the same normalisation as production.
        raw=json.loads(step('source_probe',[ffprobe,'-v','error','-show_streams','-show_format','-of','json',str(source)]))
        with patch.object(B,'_probe_json',return_value=raw):
            info=B.probe(str(source))
        assert info.ok,info.error
        source_digest=hashlib.sha256(source.read_bytes()).hexdigest()
        assert [s.codec for s in info.subtitle_streams]==['hdmv_pgs_subtitle','dvd_subtitle']
        output=root/'copy.mkv'
        argv=base+['-i',str(source),'-map','0:v:0','-c:v','copy']+P._subtitle_args(info,'mkv',P.Overrides())+[str(output)]
        step('copy',argv)
        raw=json.loads(step('copy_probe',[ffprobe,'-v','error','-show_streams','-show_format','-of','json',str(output)]))
        with patch.object(B,'_probe_json',return_value=raw):
            actual=B.probe(str(output))
        result=V.VerifyResult()
        V._stream_check(result,actual,V.stream_expectations(info,argv),True)
        assert result.verdict=='passed',result.checks
        verification=V.verify(str(output),info,deep=True,expected_streams=V.stream_expectations(info,argv))
        assert all(c.status!='fail' for c in verification.checks),verification.checks
        bitmap_check=next(c for c in verification.checks if c.id=='V1s')
        assert bitmap_check.status=='ok',bitmap_check
        log.append({'case':'deep_delivery','status':'passed','verdict':verification.verdict,
                    'bitmap_detail':bitmap_check.detail})
        for container in ('mp4','webm'):
            try:
                P._subtitle_args(info,container,P.Overrides())
            except P.PlanError as exc:
                assert '不能转换为文本字幕' in str(exc),exc
                log.append({'case':container,'status':'rejected_at_planning'})
            else:
                raise AssertionError(container+' 没有拒绝图像转文本')
        try:
            P.make_plan(info,P.get_profile('P2'),P.Overrides(subtitle_mode='burn',container='mkv',
                        out_dir=str(root)),caps=caps)
        except P.PlanError as exc:
            assert '图像' in str(exc),exc
            log.append({'case':'bitmap_burn','status':'rejected_at_planning'})
        else:
            raise AssertionError('图像字幕不应进入文本烧录')
        output_digest=hashlib.sha256(output.read_bytes()).hexdigest()
        for index in range(2):
            pixels=step(f'render_{index}',base+['-i',str(output),'-filter_complex_threads','1',
                 '-filter_complex',f'[0:v:0][0:s:{index}]overlay',
                 '-ss','0.5','-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','-'],True)
            assert len(pixels)==160*90*3,len(pixels)
            yellow=sum(r>180 and g>180 and b<80 for r,g,b in struct.iter_unpack('BBB',pixels))
            assert yellow>=40,(index,yellow)
            cleared=step(f'clear_{index}',base+['-i',str(output),'-filter_complex_threads','1',
                 '-filter_complex',f'[0:v:0][0:s:{index}]overlay',
                 '-ss','1.5','-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','-'],True)
            assert len(cleared)==160*90*3 and max(cleared)==0,(index,len(cleared),max(cleared))
            log.append({'track':index,'codec':actual.subtitle_streams[index].codec,'yellow_pixels':yellow,'status':'passed'})
        for index in range(2):
            packets=json.loads(step(f'packets_{index}',[ffprobe,'-v','error','-select_streams',f's:{index}',
                '-show_packets','-show_data','-of','json',str(output)]))['packets']
            def packet_bytes(row):
                return bytes.fromhex(''.join(line.partition(':')[2].split('  ')[0].replace(' ','')
                    for line in row['data'].splitlines() if ':' in line))
            packet=next(packet_bytes(row) for row in packets if int(row['size'])>4)
            if index==0:
                needle=bytes.fromhex('00 88 01 00 00')*8
                offset=packet.index(needle)
                damaged=packet[:offset]+bytes.fromhex('00 BF 01 00 00')+packet[offset+5:]
            else:
                damaged=packet[:2]+b'\xff\xff'+packet[4:]  # SPU control offset exceeds packet size.
            data=output.read_bytes()
            assert data.count(packet)==1,(index,len(packet))
            bad=root/f'bad_{index}.mkv'
            bad.write_bytes(data.replace(packet,damaged,1))
            decoded=safe_run([ffprobe,'-v','error','-select_streams',f's:{index}',
                 '-show_frames','-show_packets','-show_entries','subtitle=num_rects:packet=pts',
                 '-of','compact',str(bad)],text=True)
            (root/f'bad_{index}_decoder.log').write_text(decoded.stdout+decoded.stderr,encoding='utf-8')
            bitmap_scan=B.scan_bitmap_subtitles(str(bad),actual)
            assert bitmap_scan.errors,(index,bitmap_scan)
            verification=V.verify(str(bad),info,deep=True,expected_streams=V.stream_expectations(info,argv))
            assert verification.verdict=='failed',verification.checks
            if index==1:
                assert next(c for c in verification.checks if c.id=='V1s').status=='fail'
            log.append({'case':f'corrupt_{index}','decoder_stderr':decoded.stderr,
                        'explicit_bitmap_errors':bitmap_scan.errors,
                        'verdict':verification.verdict,'checks':[c.__dict__ for c in verification.checks]})
        assert hashlib.sha256(output.read_bytes()).hexdigest()==output_digest
        assert hashlib.sha256(source.read_bytes()).hexdigest()==source_digest
    assert hashlib.sha256(sup.read_bytes()).hexdigest()==digest
    (root/'matrix.json').write_text(json.dumps(log,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(log,ensure_ascii=False),flush=True)
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except ResourceDeferred as exc:
        root=Path(os.environ['TEMP'])/'bitmap_matrix'
        root.mkdir(exist_ok=True)
        (root/'deferred.json').write_text(json.dumps({'status':'deferred','reason':str(exc)},ensure_ascii=False),encoding='utf-8')
        print('NOT_FULLY_VERIFIED: '+str(exc),flush=True)
        raise SystemExit(77)
