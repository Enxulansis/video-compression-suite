"""Bounded, single-thread 320x180 synthetic encode through recovered queue + memory gate."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

WORK = Path(tempfile.mkdtemp(prefix='vcs-guard-smoke-'))
os.environ['VCS_STORE_DIR'] = str(WORK / 'store')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))
from core import jobs as J, memory as M
from core.caps import get_caps


def wait_for(predicate, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError('bounded wait expired')


def main():
    caps = get_caps()
    assert caps.available() and caps.has_encoder('libx264')
    source = WORK / 'synthetic.mp4'
    subprocess.run([caps.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=3',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1', '-g', '24',
                    '-pix_fmt', 'yuv420p', str(source)], check=True, timeout=20,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    before = source.read_bytes()
    mem = {'available': 1, 'total_mb': 32000, 'free_mb': 900}
    with patch.object(J.JobManager, '_auto_limits', return_value={'gpu': 1, 'cpu': 1}), \
            patch.object(M, 'machine_memory', side_effect=lambda: dict(mem)):
        original = J.JobManager()
        jobs = original.submit([str(source)], 'P2', {'out_dir': str(WORK / 'out'),
                                                      'preset': 'ultrafast', 'extra_args': ['-threads', '1'], 'crf': 32})
        assert len(jobs) == 1 and jobs[0]['status'] == 'queued', jobs
        restored = J.JobManager()
        task = restored._jobs[jobs[0]['id']]
        threading.Thread(target=restored.run_forever, daemon=True).start()
        time.sleep(0.3)
        assert task.status == 'queued' and not restored._procs
        print('PASS: recovered queue starts no encoder before release', flush=True)
        restored.release_restored()
        wait_for(lambda: bool(task.waiting_reason))
        assert task.status == 'queued' and not restored._procs
        print('PASS: low-memory admission leaves released work queued', flush=True)
        mem['free_mb'] = 20000
        restored._kick()
        wait_for(lambda: task.status in J.TERMINAL)
        wait_for(lambda: restored._busy['cpu'] == 0)
        assert task.status == 'done', task.to_dict()
        assert task.verify['verdict'] == 'passed', task.verify
        assert task.verify['max_keyframe_gap'] <= 2.0, task.verify
        assert Path(task.out_path).is_file() and not restored._procs
        assert source.read_bytes() == before
        payload = json.loads(J.QUEUE_FILE.read_text(encoding='utf-8'))
        assert payload['jobs'][0]['status'] == 'done'
        print('PASS: real 320x180 single-thread encode and delivery gate passed', flush=True)
        print('PASS: output persisted, original synthetic source unchanged, slot/process released', flush=True)
        print(json.dumps({'work': str(WORK), 'gap_seconds': task.verify['max_keyframe_gap'],
                          'elapsed_seconds': task.elapsed}, ensure_ascii=False), flush=True)
    print('4/4 checks passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
