"""All built-in profiles and bit-depth/container variants, isolated and bounded."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from types import SimpleNamespace

WORK = Path(tempfile.mkdtemp(prefix='vcs-codec-matrix-'))
os.environ['VCS_STORE_DIR'] = str(WORK/'store')
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'backend'))
from core.caps import get_caps, subprocess_kwargs
from core import planner as P, repair as R
from core.probe import probe
from core.verifier import verify, stream_expectations
from core import work


def run(argv, seconds=15, binary=False):
    kwargs = subprocess_kwargs()
    if binary:
        kwargs.pop('encoding')
        kwargs.pop('errors')
    proc = subprocess.run(argv,timeout=seconds,**kwargs)
    if proc.returncode:
        raise AssertionError((proc.stderr or '')[-12000:])
    return proc


def make_source(complex_tracks=False, black=False):
    sub = WORK/'plain.srt'
    sub.write_text('1\n00:00:00,200 --> 00:00:03,800\nIsolated subtitle\n',encoding='utf-8')
    styled = WORK/'styled.ass'
    styled.write_text('[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 180\n'
        '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
        'Style: Default,Arial,18,&H0000FFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n'
        '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
        'Dialogue: 0,0:00:00.20,0:00:03.80,Default,,0,0,0,,Styled isolated subtitle\n',encoding='utf-8')
    metadata = WORK/'chapters.txt'
    metadata.write_text(';FFMETADATA1\ncreation_time=2026-01-01T00:00:00Z\n'
        '[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2000\ntitle=First\n'
        '[CHAPTER]\nTIMEBASE=1/1000\nSTART=2000\nEND=4000\ntitle=Second\n',encoding='utf-8')
    caps = get_caps(wait_for_all=True)
    source = WORK/('complex.mkv' if complex_tracks else 'source.mkv')
    argv = [caps.ffmpeg,'-hide_banner','-v','error','-nostdin','-n',
        '-f','lavfi','-i',('color=c=black:size=320x180:rate=24:duration=4' if black else
                          'testsrc2=size=320x180:rate=24:duration=4'),
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=4']
    if complex_tracks:
        argv += ['-f','lavfi','-i','sine=frequency=880:sample_rate=48000:duration=4',
                 '-f','lavfi','-i','anullsrc=r=48000:cl=5.1:d=4','-i',str(styled),'-i',str(sub),
                 '-i',str(metadata),'-map','0:v','-map','1:a','-map','2:a','-map','3:a',
                 '-map','4:s','-map','5:s','-map_metadata','6','-map_chapters','6',
                 '-c:a:0','aac','-c:a:1','flac','-c:a:2','ac3','-c:s:0','ass','-c:s:1','srt',
                 '-metadata:s:a:0','language=eng','-metadata:s:a:0','title=Original',
                 '-metadata:s:a:1','language=jpn','-metadata:s:a:1','title=Commentary',
                 '-metadata:s:a:2','language=chi','-metadata:s:a:2','title=Surround',
                 '-disposition:a:0','default','-disposition:a:1','0','-disposition:a:2','0',
                 '-metadata:s:s:0','language=eng','-metadata:s:s:0','title=Styled',
                 '-metadata:s:s:1','language=chi','-metadata:s:s:1','title=Forced',
                 '-disposition:s:0','default','-disposition:s:1','forced']
        font = Path('C:/Windows/Fonts/arial.ttf')
        assert font.is_file(), 'font fixture missing'
        argv += ['-attach',str(font),'-metadata:s:t','mimetype=application/x-truetype-font']
    else:
        argv += ['-i',str(sub),'-map','0:v','-map','1:a','-map','2:s','-c:a','aac','-c:s','srt',
                 '-disposition:a','default','-disposition:s','0']
    argv += ['-c:v','libx264','-preset','ultrafast','-crf','10','-g','24',
             '-threads','1','-filter_threads','1',str(source)]
    run(argv,20)
    return probe(str(source))


def low_thread_profile(profile):
    profile = dict(profile,extra=dict(profile.get('extra') or {}))
    for name, limit in (('svtav1-params','lp=2'),('x265-params','pools=1:frame-threads=1')):
        if name in profile['extra']:
            profile['extra'][name] += ':'+limit
    return profile


def check_plan(info, plan, label):
    argv = plan.argv if hasattr(plan,'argv') else plan['argv']
    output = plan.out_path if hasattr(plan,'out_path') else plan['out_path']
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    started = time.monotonic()
    def stage(name):
        print(json.dumps({'case':label,'stage':name,'elapsed':round(time.monotonic()-started,2)}),flush=True)
    stage('encode')
    run(argv,25)
    expected = stream_expectations(info,argv)
    # A small generated fixture must never inherit the production 30-minute scan
    # limit. The existing deadline wrapper kills and reaps its own child on expiry.
    manager = SimpleNamespace(auxiliary_slot=lambda *args: nullcontext())
    stage('verify')
    with work.background(manager,timeout=30):
        work.note_media(info)
        result = verify(output,info,deep=True,reencoded=expected['video']['codec'] != info.video.codec
                        or '-g' in argv,keep_timestamps=False,expected_streams=expected,
                        expected_audio=len(expected['audio']),expected_subtitles=len(expected['subtitle']),
                        on_progress=lambda p: stage('verify-'+p['stage']) if not p.get('frames') else None)
    assert result.verdict != 'failed', (label,result.to_dict())
    assert all(c.status == 'ok' for c in result.checks if c.id in ('V1','V1b','V2','V3','V4','V4a')), result.to_dict()
    caps = get_caps()
    # Independently decode every delivered video/audio stream with fatal errors.
    stage('independent-decode')
    input_flags = ['-fflags','+noparse+nofillin'] if any(s['codec']=='opus' for s in expected['audio']) and Path(output).suffix in ('.webm','.mkv') else []
    run([caps.ffmpeg,'-hide_banner','-v','error','-xerror','-threads','1']+input_flags+['-i',output,
         '-map','0:v:0','-map','0:a?','-sn','-dn','-threads','1',
         '-fps_mode:v','passthrough','-enc_time_base:v','demux','-f','null','-'],15)
    seek = []
    for position in (0.75,2.25,3.25):
        stage('seek-'+str(position))
        proc = run([caps.ffmpeg,'-hide_banner','-v','error','-xerror']+input_flags+['-ss',str(position),
                    '-threads','1','-i',output,'-map','0:v:0','-frames:v','1',
                    '-threads','1','-f','rawvideo','-pix_fmt','rgb24','-'],8,binary=True)
        assert len(proc.stdout) > 0, (label,'seek returned no frame')
        seek.append(position)
    actual = probe(output)
    row = dict(label=label,output=output,codec=actual.video.codec,bit_depth=actual.video.bit_depth,
               profile=actual.video.profile,tag=actual.video.codec_tag,
               max_gap=result.max_keyframe_gap,verdict=result.verdict,
               audio=[s.codec for s in actual.audio_streams],subtitles=[s.codec for s in actual.subtitle_streams],
               attachments=len([s for s in actual.streams if s.kind == 'attachment']),
               seek=seek,seconds=round(time.monotonic()-started,2))
    print(json.dumps(row,ensure_ascii=False),flush=True)
    return row


def main():
    caps = get_caps()
    print(json.dumps({'hardware':caps.hardware_probes},ensure_ascii=False),flush=True)
    info = make_source()
    digest = hashlib.sha256(Path(info.path).read_bytes()).hexdigest()
    rows = []
    for profile in P.load_profiles():
        plan = P.make_plan(info,low_thread_profile(profile),P.Overrides(out_dir=str(WORK/'out'),
            extra_args=['-threads','1','-filter_threads','1']),caps=caps)
        rows.append(check_plan(info,plan,profile['id']))
    for ident,container,bits in (('P3','mp4',True),('P4','mp4',True),('P7','mp4',True),
                                ('P3','mkv',False),('P5','mkv',True),('P4','webm',False),
                                ('P5','webm',True),('P2','mov',False)):
        profile=low_thread_profile(P.get_profile(ident))
        plan=P.make_plan(info,profile,P.Overrides(out_dir=str(WORK/'variants'),container=container,ten_bit=bits,
            extra_args=['-threads','1','-filter_threads','1']),caps=caps)
        rows.append(check_plan(info,plan,'{}-{}-{}'.format(ident,container,10 if bits else 8)))
    assert hashlib.sha256(Path(info.path).read_bytes()).hexdigest() == digest
    (WORK/'matrix.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'work':str(WORK),'cases':len(rows),'source_sha256':digest},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
