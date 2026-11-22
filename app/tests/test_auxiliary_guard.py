"""Isolated arithmetic, bounded auxiliary work and cancellation regressions."""
from __future__ import annotations
import importlib
import argparse
import hashlib
import http.client
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_resource_guard import GuardRegression, J, M, server
from core.planner import make_plan, Overrides
from core.probe import MediaInfo, StreamInfo
from server import get_profile
from core.caps import get_caps


class AuxiliaryRegression(GuardRegression):
    # Inherited first-round regression methods are deliberately omitted from this suite.
    def setUp(self):
        super().setUp()
        p = patch.object(M, 'machine_memory', return_value={
            'available': 1, 'free_mb': 32000, 'total_mb': 32000})
        p.start()
        self.addCleanup(p.stop)

    def info(self, width=7680, height=4320, pix_fmt='yuv420p10le'):
        return MediaInfo(path=str(Path(self.tmp.name) / 'synthetic.mp4'), ok=True,
                         duration=60, size_bytes=100000,
                         streams=[StreamInfo(0, 'video', codec='hevc', width=width, height=height,
                                             fps=30, pix_fmt=pix_fmt, bit_depth=10)])

    def test_auto_effort_estimate_uses_actual_preset(self):
        caps = SimpleNamespace(ffmpeg='ffmpeg', has_encoder=lambda name: True,
                               has_filter=lambda name: True)
        plan = make_plan(self.info(3840, 2160), get_profile('A6'), Overrides(), caps=caps)
        actual_preset = plan.argv[plan.argv.index('-preset') + 1]
        self.assertEqual(actual_preset, '2')
        encoded, _ = M.estimate_instance_mb('libsvtav1', 640, 360, True, 2)
        self.assertGreaterEqual(plan.est_memory_mb, encoded)

    def test_8k_downscale_keeps_source_buffer_reservation(self):
        plan = SimpleNamespace(encoder='libsvtav1', argv=['ffmpeg','-pix_fmt','yuv420p10le',
                               '-vf','scale=1280:720', '-preset','4'], out_size=(1280,720),
                               source_size=(7680,4320), source_pix_fmt='yuv420p10le')
        encoded = M.estimate_instance_mb('libsvtav1',1280,720,True,4)[0]
        self.assertGreater(M.estimate_plan_mb(plan)[0], encoded + 2000)

    def test_final_pixel_format_and_preset_win(self):
        base = SimpleNamespace(encoder='libsvtav1', out_size=(3840,2160),
                               argv=['ffmpeg','-pix_fmt','yuv420p', '-preset','6',
                                     '-pix_fmt','yuv420p12le','-preset','2'])
        self.assertGreaterEqual(M.estimate_plan_mb(base)[0],
                                M.estimate_instance_mb('libsvtav1',3840,2160,True,2)[0])

    def test_crop_only_uses_encoder_geometry(self):
        plan = SimpleNamespace(encoder='libsvtav1', out_size=(3840,2160),
                               argv=['ffmpeg','-pix_fmt','yuv420p10le','-preset','4',
                                     '-vf','crop=2688:1512:0:0'])
        self.assertEqual(M.estimate_plan_mb(plan)[0],
                         M.estimate_instance_mb('libsvtav1',2688,1512,True,4)[0])

    def test_unresolved_resize_cannot_use_tiny_estimate(self):
        manager=self.manager()
        job=self.add(manager,est_memory_mb=50,encoder='libsvtav1',
                     media={'video':{'width':320,'height':180,'pix_fmt':'yuv420p'}},
                     argv=['ffmpeg','-c:v','libsvtav1','-vf','scale=iw*20:ih*20','out.mp4'])
        self.assertEqual(manager._job_memory_mb(job),0)
        self.assertFalse(manager.memory_gate(job)['ok'])
        self.assertEqual(M.encoder_geometry(['ffmpeg','-vf','scale=640:360',
                                            '-s','1920x1080'],(320,180)),(1920,1080))

    def test_unknown_memory_and_unknown_job_wait(self):
        self.assertFalse(M.admission({'available':0},0,5000)['ok'])
        manager = self.manager()
        job = self.add(manager)
        self.assertFalse(manager.memory_gate(job)['ok'])

    def test_saved_positive_estimate_is_refreshed_from_final_options(self):
        manager=self.manager()
        job=self.add(manager,est_memory_mb=486,encoder='libsvtav1',
                     media={'video':{'width':3840,'height':2160,'pix_fmt':'yuv420p10le'}},
                     argv=['ffmpeg','-c:v','libsvtav1','-preset','2','-pix_fmt','yuv420p10le',
                           '-vf','scale=640:360','out.mp4'])
        self.assertGreater(manager._job_memory_mb(job),1191)
        self.assertEqual(job.est_memory_mb,manager._job_memory_mb(job))

    def test_final_encoder_drives_resource_pool(self):
        manager=self.manager()
        job=self.add(manager,resource='gpu',est_memory_mb=2000,
                     argv=['ffmpeg','-c:v','hevc_nvenc','-c:v','libsvtav1','out.mp4'])
        manager._busy['cpu']=manager._limits['cpu']
        self.assertIsNone(manager._next_queued())
        self.assertEqual(job.resource,'cpu')
        self.assertEqual(manager._argv_encoder(job.argv),'libsvtav1')

    def test_preview_timeout_reaps_encoder(self):
        manager = self.manager()
        info = self.info(320,180)
        Path(info.path).write_bytes(b'mocked media')
        out = Path(self.tmp.name) / 'preview.mp4'
        plan = SimpleNamespace(argv=['ffmpeg','-i',info.path,'-c:v','libx264',str(out)],
                               out_path=str(out))
        class FakeProcess:
            pid = 12345
            returncode = None
            killed = False
            calls = 0
            def __init__(self, argv, **kwargs):
                Path(argv[-1]).write_bytes(b'mocked output')
            def poll(self): return self.returncode
            def communicate(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired('mocked encoder',1)
                return ('', 'mock timeout')
            def kill(self):
                self.killed = True
                self.returncode = 1
            def wait(self, *args, **kwargs): return 1
            def __enter__(self): return self
            def __exit__(self,*args): return False
        spawned=[]
        def spawn(argv, **kwargs):
            proc=FakeProcess(argv,**kwargs); spawned.append(proc); return proc
        with patch.object(server,'probe',return_value=info), \
                patch.object(server,'make_plan',return_value=plan), \
                patch.object(subprocess,'Popen',side_effect=spawn):
            status,data=self.request(manager, {'path':info.path,'profile':'P2'}, '/api/preview')
        self.assertTrue(spawned and spawned[0].killed, data)
        self.assertEqual(status,504,data)

    def test_auxiliary_scope_rejects_overlap_and_releases(self):
        W = importlib.import_module('core.work')
        manager=self.manager()
        entered=threading.Event(); release=threading.Event(); errors=[]
        def held():
            try:
                with W.request(manager, timeout=2):
                    entered.set(); release.wait(1)
            except Exception as exc: errors.append(exc)
        thread=threading.Thread(target=held,daemon=True); thread.start()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(W.Busy):
                with W.request(manager,timeout=1): pass
        finally:
            release.set(); thread.join(timeout=2)
        self.assertEqual(errors,[])
        with W.request(manager,timeout=1): W.check()

    def test_multistage_deadline_does_not_restart(self):
        W=importlib.import_module('core.work')
        manager=self.manager()
        def fake_run(argv,**kwargs):
            time.sleep(0.06); return subprocess.CompletedProcess(argv,0,'','')
        with patch.object(subprocess,'run',side_effect=fake_run) as run:
            with self.assertRaises(W.Expired):
                with W.request(manager,timeout=0.04):
                    W.run(['ffprobe','dummy'],timeout=300)
                    W.run(['ffprobe','dummy'],timeout=300)
            self.assertEqual(run.call_count,1)
            self.assertLessEqual(run.call_args.kwargs['timeout'],0.04)

    def test_frame_wait_is_bounded_and_shares_deadline(self):
        W=importlib.import_module('core.work')
        manager=self.manager(); entered=threading.Event(); release=threading.Event(); errors=[]
        def held():
            try:
                with W.request(manager,timeout=2):
                    entered.set(); release.wait(1)
            except Exception as exc: errors.append(exc)
        thread=threading.Thread(target=held,daemon=True); thread.start()
        self.assertTrue(entered.wait(1))
        try:
            started=time.monotonic()
            with self.assertRaises(W.Expired):
                with W.request(manager,timeout=0.05,wait=True): pass
            self.assertLess(time.monotonic()-started,0.5)
        finally:
            release.set(); thread.join(timeout=2)
        self.assertEqual(errors,[])
        with W.request(manager,timeout=1,wait=True): W.check()

    def test_auxiliary_reserves_queue_slot_and_memory(self):
        manager=self.manager()
        job=self.add(manager,est_memory_mb=2000)
        with patch.object(M,'machine_memory',return_value={'available':1,'free_mb':6000}):
            with manager.auxiliary_slot(2000,'cpu'):
                self.assertEqual(manager._busy['cpu'],1)
                self.assertFalse(manager.memory_gate(job)['ok'])
        self.assertEqual(manager._busy['cpu'],0)
        self.assertTrue(manager.memory_gate(job)['ok'])

    def test_cancel_failure_is_not_success(self):
        manager=self.manager(); job,proc=self.live(manager)
        job.status='paused'; job.pause_reason='user'
        with patch.object(J.winapi,'terminate_process',return_value={'ok':False,'error':'fake'}):
            self.assertFalse(manager.cancel(job.id))
        self.assertEqual(job.status,'paused')

    def test_silent_stream_timeout_reaps_owned_child(self):
        W=importlib.import_module('core.work')
        manager=self.manager(); started=time.monotonic(); child=None
        with self.assertRaises(W.Expired):
            with W.request(manager,timeout=0.2):
                with W.stream([sys.executable,'-c','import time; time.sleep(60)'],timeout=60,
                              stdout=subprocess.PIPE,stderr=subprocess.STDOUT) as child:
                    self.assertEqual(list(child.stdout),[])
        self.assertLess(time.monotonic()-started,5)
        self.assertIsNotNone(child.returncode)
        self.assertEqual(manager._aux_reserved_mb,0)
        with W.request(manager,timeout=1): W.check()

    def test_crop_and_scale_chain_and_chroma(self):
        for chain, expected in [('crop=1800:1000:0:0,scale=854:480:flags=bicubic',(854,480)),
                                ('scale=854:480,crop=640:360:0:0',(640,360))]:
            with self.subTest(chain=chain):
                self.assertEqual(M.encoder_geometry(['ffmpeg','-vf',chain],(1920,1080)),expected)
        self.assertEqual(M.pixel_layout('yuv444p12le'),(True,2.0))
        self.assertEqual(M.pixel_layout('yuv422p10le'),(True,4/3))
        for size in [(3840,2160),(7680,4320)]:
            with self.subTest(size=size):
                a=M.decoder_reserve_mb(*size,'yuv420p')
                b=M.decoder_reserve_mb(*size,'yuv420p10le')
                c=M.decoder_reserve_mb(*size,'yuv444p12le')
                self.assertGreaterEqual(b,2*a-1)
                self.assertGreaterEqual(c,2*b-1)

    def test_cancel_before_spawn_never_launches(self):
        manager=self.manager(); job=self.add(manager,status='running')
        self.assertTrue(manager.cancel(job.id))
        with patch.object(subprocess,'Popen') as spawn, patch.object(J,'get_caps') as caps:
            manager._run_job(job)
            spawn.assert_not_called()
            caps.assert_not_called()
        self.assertEqual(job.status,'cancelled')

    def test_seek_pool_is_fixed_deduplicated_and_bounded(self):
        manager=self.manager()
        with patch.object(manager._seek_pool,'submit') as submit:
            manager.scan_seek_async([f'fake-{i}.mp4' for i in range(50)])
            manager.scan_seek_async([f'fake-{i}.mp4' for i in range(10)])
            self.assertEqual(submit.call_count,32)
            self.assertEqual(len(manager._seek_scanning),32)
            self.assertEqual(manager._seek_pool._max_workers,2)
            self.assertIsNone(manager.seek_results['fake-49.mp4']['seek_ok'])
        manager._seek_pool.shutdown(wait=True)

    def test_auxiliary_upscale_reserves_output_buffers(self):
        W=importlib.import_module('core.work')
        source=self.info(320,180,'yuv420p')
        with W.request(self.manager(),timeout=1):
            W.note_media(source)
            budget=W._budget.get()
            base=W._need(['ffmpeg','-i',source.path,'-c:v','mjpeg','pipe:1'],budget)[0]
            large=W._need(['ffmpeg','-i',source.path,'-vf','scale=7680:4320',
                           '-c:v','mjpeg','pipe:1'],budget)[0]
            self.assertGreater(large,base+1000)
        self.assertEqual(M.pixel_layout('rgba64le'),(True,8/3))
        self.assertEqual(M.pixel_layout('rgb48be'),(True,2))

    def test_keyframe_probe_reserves_decoder_and_cpu_slot(self):
        W=importlib.import_module('core.work')
        source=self.info()
        with W.request(self.manager(),timeout=1):
            W.note_media(source)
            need,pool=W._need(['ffprobe','-skip_frame','nokey','-show_entries',
                              'frame=key_frame',source.path],W._budget.get())
            self.assertEqual(pool,'cpu')
            self.assertGreater(need,3000)
            self.assertEqual(W._need(['ffprobe','-show_streams',source.path],W._budget.get()),(64,None))

    def test_real_auxiliary_success_paths_preserve_source(self):
        caps=get_caps(); manager=self.manager()
        source=Path(self.tmp.name)/'synthetic.mp4'
        subprocess.run([caps.ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-y',
            '-f','lavfi','-i','testsrc2=size=320x180:rate=24:duration=3',
            '-c:v','libx264','-preset','ultrafast','-threads','1','-g','24',
            '-pix_fmt','yuv420p',str(source)],check=True,timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW)
        before=hashlib.sha256(source.read_bytes()).hexdigest()
        body={'path':str(source),'profile':'P2','seconds':6,
              'overrides':{'preset':'ultrafast','extra_args':['-threads','1','-filter_threads','1'],'crf':32,
                           'location':'custom','out_dir':str(Path(self.tmp.name)/'out')}}
        for route in ('/api/preview','/api/measure','/api/crop'):
            with self.subTest(route=route):
                status,data=self.request(manager,body,route)
                self.assertEqual(status,200,data)
                self.assertTrue(data['ok'],data)
                if route=='/api/preview': self.assertEqual(data['start'],0)
        httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True)
        with patch.object(server,'get_manager',return_value=manager):
            thread.start()
            conn=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=5)
            try:
                from urllib.parse import quote
                conn.request('GET','/api/frame?path='+quote(str(source))+'&t=0.5')
                response=conn.getresponse(); blob=response.read()
                self.assertEqual(response.status,200,blob[:200])
                self.assertTrue(blob.startswith(b'\xff\xd8'),blob[:100])
            finally:
                conn.close(); httpd.shutdown(); httpd.server_close(); thread.join(timeout=2)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
        self.assertEqual(manager._busy,{'gpu':0,'cpu':0})
        self.assertEqual(manager._aux_reserved_mb,0)
        self.assertEqual(manager.list_jobs(),[])


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--unit-only',action='store_true',help='omit real synthetic media encoding')
    args=parser.parse_args()
    names=[name for name in AuxiliaryRegression.__dict__ if name.startswith('test_')]
    if args.unit_only:
        names.remove('test_real_auxiliary_success_paths_preserve_source')
        print('Explicitly omitted: real synthetic media encoding (--unit-only)',flush=True)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(AuxiliaryRegression(n) for n in names))
    raise SystemExit(not result.wasSuccessful())
