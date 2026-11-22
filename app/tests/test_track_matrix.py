"""Real complex-track matrix: only generated media, bounded child processes."""
import hashlib
import argparse
import json
import shutil
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_codec_matrix import WORK, P, R, run, make_source, low_thread_profile, check_plan, get_caps, probe
from test_codec_matrix import work, nullcontext, SimpleNamespace


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--group',choices=('all','mp4','mkv','copy','repair','burn'),default='all')
    group=parser.parse_args().group
    caps = get_caps()
    info = make_source(complex_tracks=True,black=group=='burn')
    digest = hashlib.sha256(Path(info.path).read_bytes()).hexdigest()
    assert len(info.audio_streams) == 3 and len(info.subtitle_streams) == 2
    assert len([s for s in info.streams if s.kind == 'attachment']) == 1 and len(info.chapters) == 2
    rows = []
    for ident,container in (('P2','mp4'),('P2','mov'),('P3','mp4'),('P5','mp4'),
                            ('P2','mkv'),('P3','mkv'),('P5','mkv'),('P5','webm'),('P8','mkv')):
        case_group = 'copy' if ident == 'P8' else 'mp4' if container in ('mp4','mov') else 'mkv'
        if group not in ('all',case_group):
            continue
        if container == 'mov':
            try:
                P.make_plan(info,P.get_profile(ident),P.Overrides(container=container),caps=caps)
            except P.PlanError as exc:
                assert 'forced' in str(exc)
                rows.append({'label':'complex-P2-mov','status':'rejected','reason':str(exc)})
                print(json.dumps(rows[-1],ensure_ascii=False),flush=True)
                continue
            raise AssertionError('MOV forced subtitle was accepted')
        plan=P.make_plan(info,low_thread_profile(P.get_profile(ident)),
                         P.Overrides(container=container,out_dir=str(WORK/'out'),
                         extra_args=['-threads','1','-filter_threads','1']),caps=caps)
        row=check_plan(info,plan,'complex-{}-{}'.format(ident,container))
        if container in ('mp4','mov'):
            assert row['audio'] == ['aac','aac','ac3'], row
            assert row['subtitles'] == ['mov_text','mov_text']
        if container == 'mkv':
            assert row['audio'] == ['aac','flac','ac3'] and row['attachments'] == 1, row
            assert row['subtitles'] == ['ass','subrip'], row
        rows.append(row)
    repair_cases=(('remux','mp4'),('remux','mkv'),('compat','mp4'),('compat264','mkv')) if group in ('all','repair') else ()
    if repair_cases:
        print(json.dumps({'stage':'diagnose'}),flush=True)
        with work.background(SimpleNamespace(auxiliary_slot=lambda *args: nullcontext()),timeout=30):
            work.note_media(info)
            diag=R.diagnose(info.path)
    for strategy,container in repair_cases:
        plan=R.build_repair_plan(diag,strategy,{'container':container,'out_dir':str(WORK/'repair')})
        # Test-only thread limit; hardware defaults and GOP remain exactly as planned.
        plan['argv'][-1:-1]=['-threads','1','-filter_threads','1']
        rows.append(check_plan(info,plan,'repair-{}-{}'.format(strategy,container)))
    if group in ('all','burn'):
        base=[caps.ffmpeg,'-hide_banner','-v','error','-i']
        tail=['-frames:v','1','-an','-threads','1','-f','rawvideo','-pix_fmt','rgb24','-']
        def pixels(path,position):
            return run(base+[path,'-ss',str(position)]+tail,8,binary=True).stdout
        original=pixels(info.path,1)
        quoted=WORK/"字幕 '样片', [1]; case.mkv"
        shutil.copy2(info.path,quoted)
        for burn_info,label in ((info,'burn-first-ass'),(probe(str(quoted)),'burn-special-path')):
            burn = P.make_plan(burn_info,low_thread_profile(P.get_profile('P2')),
                P.Overrides(out_dir=str(WORK/'burn'),container='mkv',subtitle_mode='burn',
                            extra_args=['-threads','1','-filter_threads','1']),caps=caps)
            rows.append(check_plan(burn_info,burn,label))
            assert not rows[-1]['subtitles'] and rows[-1]['attachments']==1
            rendered=pixels(burn.out_path,1)
            assert len(original)==len(rendered)==320*180*3
            if group=='burn':
                def yellow(data):
                    return sum(data[i]>150 and data[i+1]>150 and data[i+2]<100
                               for i in range(130*320*3,len(data),3))
                before=pixels(burn.out_path,0.05)
                assert yellow(original)==yellow(before)==0
                assert yellow(rendered)>50, 'ASS yellow text not actually rendered'
                rows[-1]['burn_pixels']={'before':yellow(before),'source':yellow(original),'during':yellow(rendered)}
                print(json.dumps({'burn_pixels':rows[-1]['burn_pixels']},ensure_ascii=False),flush=True)
            else:
                assert sum(a!=b for a,b in zip(original[130*320*3:],rendered[130*320*3:]))>500
        assert hashlib.sha256(quoted.read_bytes()).hexdigest()==digest
    assert hashlib.sha256(Path(info.path).read_bytes()).hexdigest()==digest
    (WORK/'matrix.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'work':str(WORK),'group':group,'cases':len(rows),'source_sha256':digest},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
