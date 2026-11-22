"""Bounded single-thread media tests; only fresh synthetic files and isolated jobs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

WORK = Path(tempfile.mkdtemp(prefix='vcs-delivery-smoke-'))
os.environ['VCS_STORE_DIR'] = str(WORK / 'store')
sys.path.insert(0,str(Path(__file__).resolve().parent.parent / 'backend'))
from core import jobs as J, repair as R
from core.caps import get_caps
import core.caps as C


def run_job(manager, job):
    pool = job.resource or 'cpu'
    manager._busy[pool] += 1
    job.status = 'running'
    job.started = time.time()
    worker = threading.Thread(target=manager._job_worker,args=(job,pool),daemon=True)
    worker.start()
    try:
        worker.join(25)
        assert not worker.is_alive(), 'job exceeded 25 seconds'
        assert not manager._procs and manager._busy[pool] == 0
        saved = json.loads(J.QUEUE_FILE.read_text(encoding='utf-8'))
        assert next(j for j in saved['jobs'] if j['id'] == job.id)['status'] == job.status
    finally:
        proc = manager._procs.get(job.id)
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        worker.join(5)


def main():
    caps = get_caps()
    subtitle = WORK / 'synthetic.srt'
    subtitle.write_text('1\n00:00:00,000 --> 00:00:03,500\nSynthetic isolation test\n',encoding='utf-8')
    source = WORK / 'source.mkv'
    subprocess.run([caps.ffmpeg,'-hide_banner','-v','error','-nostdin','-n',
        '-f','lavfi','-i','testsrc2=size=320x180:rate=24:duration=4',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=4','-i',str(subtitle),
        '-map','0:v','-map','1:a','-map','2:s','-c:v','libx264','-preset','ultrafast',
        '-threads','1','-filter_threads','1','-crf','10','-g','24',
        '-c:a','aac','-b:a','96k','-c:s','srt',str(source)],check=True,timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,capture_output=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manager = J.JobManager()
    opts = {'out_dir':str(WORK / 'out'),'preset':'ultrafast','crf':32,
            'extra_args':['-threads','1','-filter_threads','1']}
    data = manager.submit([str(source),str(source)],'P2',opts)
    first, second = [manager._jobs[j['id']] for j in data]
    assert first.out_path != second.out_path
    sentinel = Path(first.out_path)
    sentinel.parent.mkdir(parents=True,exist_ok=True)
    sentinel.write_bytes(b'existing output sentinel')
    run_job(manager, first)
    assert first.status == 'done' and first.verify['verdict'] == 'passed', first.to_dict()
    assert sentinel.read_bytes() == b'existing output sentinel'
    first_digest = hashlib.sha256(Path(first.out_path).read_bytes()).hexdigest()
    run_job(manager, second)
    assert second.status == 'done' and second.verify['verdict'] == 'passed', second.to_dict()
    assert hashlib.sha256(Path(first.out_path).read_bytes()).hexdigest() == first_digest
    print('PASS: duplicate queued outputs and late existing file never overwrite',flush=True)

    drop = manager.submit([str(source)],'P2',dict(opts,audio_mode='drop',subtitle_mode='drop'))[0]
    drop_job = manager._jobs[drop['id']]
    with patch.object(manager,'_deep_verify',return_value=True):
        run_job(manager,drop_job)
    assert drop_job.verify['verdict'] == 'passed', drop_job.to_dict()
    assert next(c for c in drop_job.verify['checks'] if c['id'] == 'V4')['status'] == 'ok'
    assert next(c for c in drop_job.verify['checks'] if c['id'] == 'V1b')['label'] == '整片解码'
    print('PASS: explicit audio and subtitle removal passes expected-track gate',flush=True)

    named = manager.submit([str(source)],'P2',dict(opts,name_template='{stem}_{filesize_bytes}'))[0]
    named_job = manager._jobs[named['id']]
    run_job(manager,named_job)
    old_final = Path(named_job.out_path)
    old_bytes = old_final.read_bytes()
    assert named_job.argv[-1] == named_job.out_path
    assert manager.retry(named_job.id)
    assert named_job.out_path != str(old_final)
    run_job(manager,named_job)
    assert named_job.status == 'done' and old_final.read_bytes() == old_bytes
    assert named_job.argv[-1] == named_job.out_path
    print('PASS: post-encode rename and real retry use the new output',flush=True)

    remux = manager.submit_repair([str(source)],'remux',{'out_dir':str(WORK/'repair'),'container':'mkv'})[0]
    remux_job = manager._jobs[remux['id']]
    run_job(manager,remux_job)
    assert remux_job.status == 'done', remux_job.to_dict()
    for ident in ('V1b','V2','V3','V4'):
        assert next(c for c in remux_job.verify['checks'] if c['id'] == ident)['status'] == 'ok'
    print('PASS: real remux retains audio/subtitles and runs all mandatory checks',flush=True)

    software_caps = SimpleNamespace(ffmpeg=caps.ffmpeg,
        has_encoder=lambda name: name == 'libx264',has_filter=caps.has_filter)
    fallback_opts = dict(opts)
    fallback_opts.pop('preset')
    with patch.object(C,'get_caps',return_value=software_caps):
        fallback = manager.submit([str(source)],'P1',fallback_opts)[0]
    fallback_job = manager._jobs[fallback['id']]
    run_job(manager,fallback_job)
    assert fallback_job.status == 'done' and fallback_job.encoder == 'libx264', fallback_job.to_dict()
    assert fallback_job.resource == 'cpu'
    print('PASS: missing hardware encoder falls back to working software parameters',flush=True)

    broken = manager.submit([str(source)],'P2',dict(opts,preset='invalid-preset',
                            name_template='failed_{filesize_bytes}'))[0]
    broken_job = manager._jobs[broken['id']]
    planned = broken_job.out_path
    run_job(manager,broken_job)
    assert broken_job.status == 'failed' and broken_job.error and broken_job.verify is None
    assert broken_job.out_path == planned
    print('PASS: FFmpeg exit 1 is a diagnosed failure and partial output is not renamed',flush=True)

    alias = manager.submit([str(source)],'P2',opts)[0]
    alias_job = manager._jobs[alias['id']]
    os.link(source,alias_job.out_path)
    run_job(manager,alias_job)
    assert alias_job.status == 'failed' and '同一文件' in alias_job.error
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert not manager._procs and all(v == 0 for v in manager._busy.values())
    manager._seek_pool.shutdown(wait=True,cancel_futures=True)
    print('PASS: late hardlink to source is refused; source unchanged, no owned process remains',flush=True)
    print(json.dumps({'work':str(WORK),'checks':8,'source_sha256':digest},ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
