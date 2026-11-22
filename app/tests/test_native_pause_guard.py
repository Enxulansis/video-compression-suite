"""Real Windows controls on owned, paced 320x180 single-thread encodes only."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

WORK=Path(tempfile.mkdtemp(prefix='vcs-native-guard-'))
os.environ['VCS_STORE_DIR']=str(WORK/'store')
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'backend'))
from core import jobs as J, memory as M, winapi
from core.caps import get_caps


def wait_for(predicate,seconds=20):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(0.05)
    raise AssertionError('bounded native wait expired')


def main():
    caps=get_caps()
    assert winapi.process_control_available() and caps.has_encoder('libx264')
    source=WORK/'synthetic.mp4'
    subprocess.run([caps.ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-y',
                    '-f','lavfi','-i','testsrc2=size=320x180:rate=24:duration=8',
                    '-c:v','libx264','-preset','ultrafast','-crf','18','-threads','1',
                    '-g','24','-pix_fmt','yuv420p',str(source)],check=True,timeout=20,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    original=hashlib.sha256(source.read_bytes()).hexdigest()
    workers=[]; owned=[]
    with patch.object(J.JobManager,'_auto_limits',return_value={'gpu':1,'cpu':1}), \
            patch.object(M,'machine_memory',return_value={
                'available':1,'total_mb':32000,'free_mb':20000}):
        manager=J.JobManager()
        def start():
            data=manager.submit([str(source)],'P2',{'out_dir':str(WORK/'out'),
                'preset':'ultrafast','extra_args':['-threads','1','-filter_threads','1'],
                'crf':32,'keep_timestamps':False})
            job=manager._jobs[data[0]['id']]
            job.argv.insert(job.argv.index('-i'),'-re')
            job.status='running'; job.started=time.time(); manager._busy['cpu']+=1
            worker=threading.Thread(target=manager._job_worker,args=(job,'cpu'),daemon=True)
            workers.append(worker); worker.start()
            wait_for(lambda: job.id in manager._procs and job.out_time>=0.5)
            proc=manager._procs[job.id]; owned.append(proc)
            assert job.pid_created>0
            return job,proc,worker
        try:
            job,proc,worker=start()
            identity=winapi.process_creation_time(handle=int(proc._handle))
            mismatch=winapi.terminate_process(proc.pid,expected_image=caps.ffmpeg,
                                             expected_created=identity+1)
            assert not mismatch['ok'] and proc.poll() is None
            print('PASS: reused/stale identity cannot terminate the owned test encoder',flush=True)
            assert manager.pause(job.id)['ok']
            time.sleep(0.6)
            size=Path(job.out_path).stat().st_size; position=job.out_time
            time.sleep(1.0)
            assert Path(job.out_path).stat().st_size==size and job.out_time==position
            assert job.status=='paused' and proc.poll() is None and manager._busy['cpu']==1
            print('PASS: real suspended encoder stops output and progress; slot retained',flush=True)
            assert manager.resume(job.id)['ok']
            worker.join(timeout=25)
            assert not worker.is_alive(),job.to_dict()
            assert job.status=='done' and job.verify['verdict']=='passed',job.to_dict()
            assert job.paused_total>=1.5 and job.elapsed < job.finished-job.started-1.0
            assert proc.poll() is not None and not manager._procs and manager._busy['cpu']==0
            print('PASS: real resume completes delivery gate and excludes paused time',flush=True)
            cancelled,proc,worker=start()
            assert manager.pause(cancelled.id)['ok']
            assert manager.cancel(cancelled.id)
            worker.join(timeout=10)
            assert not worker.is_alive() and proc.poll() is not None
            assert cancelled.status=='cancelled' and not manager._procs and manager._busy['cpu']==0
            assert hashlib.sha256(source.read_bytes()).hexdigest()==original
            print('PASS: paused cancellation reaps encoder, releases slot and preserves synthetic source',flush=True)
            data=manager.submit([str(source)],'P2',{'out_dir':str(WORK/'out'),
                'preset':'ultrafast','extra_args':['-threads','1'],'crf':32})
            verify_cancel=manager._jobs[data[0]['id']]
            verify_cancel.status='running'; verify_cancel.started=time.time(); manager._busy['cpu']+=1
            real_verify=J.verify
            def cancelling_verify(*args,**kwargs):
                assert manager.cancel(verify_cancel.id)
                return real_verify(*args,**kwargs)
            with patch.object(J,'verify',side_effect=cancelling_verify), \
                    patch.object(J.actions,'recycle') as recycle:
                worker=threading.Thread(target=manager._job_worker,args=(verify_cancel,'cpu'),daemon=True)
                workers.append(worker); worker.start(); worker.join(timeout=20)
                assert not worker.is_alive() and verify_cancel.status=='cancelled',verify_cancel.to_dict()
                recycle.assert_not_called()
            assert manager._busy['cpu']==0 and not manager._procs
            assert hashlib.sha256(source.read_bytes()).hexdigest()==original
            print('PASS: cancellation during verification cannot become done or recycle the source',flush=True)
            print('5/5 checks passed',flush=True)
        finally:
            for proc in owned:
                if proc.poll() is None:
                    winapi.terminate_process(proc.pid,resume_first=True,handle=int(proc._handle))
                    proc.kill()
                proc.wait(timeout=5)
            for worker in workers: worker.join(timeout=5)
            manager._seek_pool.shutdown(wait=True)


if __name__=='__main__': main()
