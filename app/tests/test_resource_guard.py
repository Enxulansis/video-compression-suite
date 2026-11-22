"""Queue recovery and memory integration regressions, using fake processes only."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

if 'VCS_STORE_DIR' not in os.environ:
    os.environ['VCS_STORE_DIR'] = tempfile.mkdtemp(prefix='vcs-guard-import-')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))
from core import jobs as J, memory as M, settings as S
import server
from core import verifier as V
from core.probe import MediaInfo, StreamInfo


class GuardRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='vcs-guard-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for obj, name, value in ((J, 'STORE_DIR', root), (J, 'JOBS_DIR', root / 'jobs'),
                                 (J, 'QUEUE_FILE', root / 'queue.json'),
                                 (S, 'STORE_DIR', root), (S, 'SETTINGS_FILE', root / 'settings.json'),
                                 (S, '_CACHE', {})):
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        auto = patch.object(J.JobManager, '_auto_limits', return_value={'gpu': 1, 'cpu': 2})
        auto.start()
        self.addCleanup(auto.stop)
        memory = patch.object(M, 'machine_memory', return_value={
            'available': 1, 'free_mb': 32000, 'total_mb': 32000})
        memory.start()
        self.addCleanup(memory.stop)
        for name in ('suspend_process', 'resume_process', 'terminate_process'):
            p = patch.object(J.winapi, name, return_value={'ok': True})
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(J.JobManager, '_pid_is_tracked_ffmpeg', return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def manager(self):
        return J.JobManager()

    def add(self, manager, ident='test0001', **kwargs):
        job = J.Job(ident, 'synthetic.mp4', 'P5', **kwargs)
        manager._jobs[ident] = job
        manager._order.append(ident)
        return job

    def request(self, manager, body, path='/api/settings'):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        with patch.object(server, 'get_manager', return_value=manager):
            thread.start()
            conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
            try:
                conn.request('POST', path, json.dumps(body), {'Content-Type': 'application/json'})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_memory_settings_endpoint(self):
        manager = self.manager()
        status, data = self.request(manager, {'memory_margin_mb': 3000, 'memory_low_water_mb': 1600})
        self.assertEqual(status, 200, data)
        self.assertEqual(manager.memory_policy()['margin_mb'], 3000)
        self.assertEqual(manager.memory_policy()['low_water_mb'], 1600)

    def test_memory_settings_survive_restart(self):
        S.save({'memory_guard': False, 'memory_margin_mb': 4000, 'memory_low_water_mb': 1800})
        manager = self.manager()
        self.assertEqual(manager.memory_policy(), {'guard': False, 'margin_mb': 4000, 'low_water_mb': 1800})

    def test_pool_limits_survive_restart(self):
        manager = self.manager()
        status, data = self.request(manager, {'limits': {'gpu': 2, 'cpu': 3}})
        self.assertEqual(status, 200, data)
        S._CACHE.clear()
        self.assertEqual(self.manager().limits(), {'gpu': 2, 'cpu': 3})

    def test_malformed_json_shapes_do_not_prevent_startup(self):
        for payload in ([], None, 'valid json string', {'jobs': None}, {'jobs': 4}):
            with self.subTest(payload=payload):
                J.QUEUE_FILE.write_text(json.dumps(payload), encoding='utf-8')
                manager = self.manager()
                self.assertEqual(manager.list_jobs(), [])
                self.assertTrue(manager._load_error)

    def test_cancelling_restored_queue_does_not_hold_new_jobs(self):
        manager = self.manager()
        self.add(manager)
        manager._persist()
        restored = self.manager()
        self.assertTrue(restored.restore_state()['held'])
        restored.cancel_all()
        self.assertFalse(restored.restore_state()['held'])
        new = self.add(restored, 'test0002', est_memory_mb=2000)
        self.assertIs(restored._next_queued(), new)

    def test_release_counts_only_restored_jobs(self):
        manager = self.manager()
        self.add(manager)
        manager._persist()
        restored = self.manager()
        self.add(restored, 'test0002')
        self.assertEqual(restored.restore_state()['queued'], 1)
        self.assertEqual(restored.release_restored()['released'], 1)

    def test_estimate_updates_are_not_hidden_by_cache(self):
        manager = self.manager()
        job = self.add(manager, est_memory_mb=1000)
        self.assertEqual(manager._job_memory_mb(job), 1000)
        job.est_memory_mb = 8000
        self.assertEqual(manager._job_memory_mb(job), 8000)

    def test_invalid_memory_policy_is_atomic(self):
        manager = self.manager()
        old = manager.memory_policy()
        status, data = self.request(manager, {'memory_guard': False, 'memory_low_water_mb': 256})
        self.assertEqual(status, 400, data)
        self.assertEqual(manager.memory_policy(), old)
        self.assertEqual(S.load()['memory_guard'], True)

    def live(self, manager):
        job = self.add(manager, status='running', resource='cpu', est_memory_mb=2000)
        proc = SimpleNamespace(pid=12345, poll=lambda: None)
        manager._procs[job.id] = proc
        manager._busy['cpu'] = 1
        return job, proc

    def tick(self, manager, job, proc, free, now=100):
        with patch.object(M, 'machine_memory', return_value={
                'available': 1, 'free_mb': free, 'total_mb': 32000}):
            return manager._memory_watchdog_tick(job, proc, now)

    def test_current_usage_is_not_charged_twice(self):
        manager = self.manager()
        self.live(manager)
        candidate = self.add(manager, 'test0002', est_memory_mb=2000)
        with patch.object(M, 'machine_memory', return_value={'available': 1, 'free_mb': 6000}), \
                patch.object(M, 'process_memory_mb', return_value={'resident_mb': 2000, 'private_mb': 2000}):
            self.assertTrue(manager.memory_gate(candidate)['ok'])
            self.assertIs(manager._next_queued(), candidate)

    def test_undispatched_peak_is_reserved(self):
        manager = self.manager()
        running = self.add(manager, status='running', est_memory_mb=2000)
        manager._busy['cpu'] = 1
        candidate = self.add(manager, 'test0002', est_memory_mb=2000)
        with patch.object(M, 'machine_memory', return_value={'available': 1, 'free_mb': 6000}):
            self.assertFalse(manager.memory_gate(candidate)['ok'])
        self.assertNotIn(running.id, manager._procs)

    def test_unknown_process_usage_reserves_full_peak(self):
        manager = self.manager()
        self.live(manager)
        candidate = self.add(manager, 'test0002', est_memory_mb=2000)
        with patch.object(M, 'machine_memory', return_value={'available': 1, 'free_mb': 6000}), \
                patch.object(M, 'process_memory_mb', return_value=None):
            self.assertFalse(manager.memory_gate(candidate)['ok'])

    def test_committed_memory_exhaustion_blocks_start(self):
        decision = M.admission({'available': 1, 'free_mb': 20000, 'commit_limit_mb': 40000,
                                'commit_used_mb': 39000}, 0, 2000)
        self.assertFalse(decision['ok'])
        self.assertIn('提交额度', decision['reason'])

    def test_user_pause_is_never_automatically_resumed(self):
        manager = self.manager()
        job, proc = self.live(manager)
        self.assertTrue(manager.pause(job.id)['ok'])
        self.tick(manager, job, proc, 1000)
        self.tick(manager, job, proc, 3000)
        self.assertEqual(job.status, 'paused')
        self.assertEqual(job.pause_reason, 'user')
        J.winapi.resume_process.assert_not_called()

    def test_automatic_pause_recovers_only_at_resume_watermark(self):
        manager = self.manager()
        job, proc = self.live(manager)
        self.tick(manager, job, proc, 1000)
        self.assertEqual((job.status, job.pause_reason), ('paused', 'memory'))
        with patch.object(M, 'machine_memory', return_value={'available': 1, 'free_mb': 2000}):
            self.assertFalse(manager.resume(job.id)['ok'])
        self.tick(manager, job, proc, 2499)
        self.assertEqual(job.status, 'paused')
        self.tick(manager, job, proc, 2500)
        self.assertEqual((job.status, job.pause_reason), ('running', ''))

    def test_user_can_take_over_automatic_pause(self):
        manager = self.manager()
        job, proc = self.live(manager)
        self.tick(manager, job, proc, 1000)
        self.assertTrue(manager.pause(job.id)['ok'])
        self.tick(manager, job, proc, 3000)
        self.assertEqual((job.status, job.pause_reason), ('paused', 'user'))

    def test_disabling_guard_releases_only_memory_pause(self):
        manager = self.manager()
        job, proc = self.live(manager)
        self.tick(manager, job, proc, 1000)
        manager.set_memory_policy(guard=False)
        self.tick(manager, job, proc, 1000)
        self.assertEqual(job.status, 'running')
        manager.pause(job.id)
        self.tick(manager, job, proc, 3000)
        self.assertEqual((job.status, job.pause_reason), ('paused', 'user'))

    def test_failed_resume_retains_automatic_pause(self):
        manager = self.manager()
        job, proc = self.live(manager)
        self.tick(manager, job, proc, 1000)
        with patch.object(J.winapi, 'resume_process', return_value={'ok': False, 'error': 'fake failure'}):
            self.tick(manager, job, proc, 3000)
        self.assertEqual((job.status, job.pause_reason), ('paused', 'memory'))
        self.tick(manager, job, proc, 3000)
        self.assertEqual(job.status, 'running')

    def test_failed_resume_ends_old_critical_interval(self):
        manager = self.manager()
        job, proc = self.live(manager)
        self.tick(manager, job, proc, 600, 100)
        with patch.object(J.winapi, 'resume_process', return_value={'ok': False}):
            self.tick(manager, job, proc, 3000, 200)
        self.assertFalse(self.tick(manager, job, proc, 600, 201))
        self.assertFalse(self.tick(manager, job, proc, 600, 320))
        self.assertTrue(self.tick(manager, job, proc, 600, 321))

    def test_critical_interval_restarts_after_noncritical_sample(self):
        manager = self.manager()
        job, proc = self.live(manager)
        self.tick(manager, job, proc, 600, 100)
        self.tick(manager, job, proc, 800, 200)
        self.tick(manager, job, proc, 600, 201)
        self.assertFalse(self.tick(manager, job, proc, 600, 320))
        self.assertTrue(self.tick(manager, job, proc, 600, 321))
        J.winapi.terminate_process.assert_called_once_with(proc.pid, resume_first=True)
        self.assertEqual(job.status, 'failed')

    def test_zero_free_memory_is_a_valid_critical_sample(self):
        manager = self.manager()
        job, proc = self.live(manager)
        self.tick(manager, job, proc, 0)
        self.assertEqual(job.pause_reason, 'memory')

    def test_unknown_memory_does_not_cancel_paused_job(self):
        manager = self.manager()
        job, proc = self.live(manager)
        self.tick(manager, job, proc, 600)
        with patch.object(M, 'machine_memory', return_value={'available': 0, 'free_mb': 0}):
            self.assertFalse(manager._memory_watchdog_tick(job, proc, 500))
        J.winapi.terminate_process.assert_not_called()

    def test_pauses_do_not_disable_later_stall_detection(self):
        job = J.Job('test0001', 'synthetic.mp4', 'P5', status='paused', paused_total=3000)
        self.assertEqual(J.JobManager._stall_seconds(job, 100, 0, 4000), 0)
        job.status = 'running'
        self.assertEqual(J.JobManager._stall_seconds(job, 3100, 3000, 4010), 910)

    def test_impossible_thresholds_are_rejected(self):
        with self.assertRaises(ValueError):
            M.validate_policy(True, 10000, 8192, {'available': 1, 'total_mb': 8192})
        with self.assertRaises(ValueError):
            M.validate_policy(True, 1500, 1500, {'available': 1, 'total_mb': 32000})

    def test_invalid_types_cannot_disable_guard(self):
        manager = self.manager()
        for body in ({'memory_guard': 'false'}, {'memory_margin_mb': 3000.5},
                     {'memory_low_water_mb': True}):
            with self.subTest(body=body):
                self.assertEqual(self.request(manager, body)[0], 400)
        self.assertTrue(manager.memory_policy()['guard'])

    def test_discard_restored_is_scoped_and_does_not_release(self):
        manager = self.manager()
        self.add(manager)
        self.add(manager, 'done0002', status='done')
        manager._persist()
        restored = self.manager()
        new = self.add(restored, 'test0003', est_memory_mb=2000)
        with patch.object(restored, 'release_restored') as release:
            self.assertEqual(self.request(restored, {}, '/api/jobs/discard-restored')[0], 400)
            status, data = self.request(restored, {'confirm': True}, '/api/jobs/discard-restored')
            self.assertEqual((status, data['removed']), (200, 1))
            release.assert_not_called()
        self.assertEqual(set(restored._jobs), {'done0002', 'test0003'})
        self.assertFalse(restored.restore_state()['held'])
        self.assertIs(restored._next_queued(), new)

    def test_repair_and_older_records_derive_missing_estimate(self):
        manager = self.manager()
        job = self.add(manager, encoder='same', media={'diagnosis': {'width': 1920, 'height': 1080}},
                       argv=['ffmpeg', '-c:v', 'libsvtav1', '-preset', '4', '-pix_fmt',
                             'yuv420p10le', '-vf', 'crop=1800:1000:0:0,scale=854:480:flags=bicubic', 'out.mp4'])
        self.assertEqual(manager._job_memory_mb(job),
                         M.estimate_instance_mb('libsvtav1', 854, 480, True, 4)[0]
                         + M.decoder_reserve_mb(1920, 1080, '')
                         + M.decoder_reserve_mb(854, 480, 'yuv420p10le'))

    def test_constructor_respects_explicit_concurrency(self):
        manager = J.JobManager(concurrency=4)
        self.assertEqual(manager.limits(), {'gpu': 2, 'cpu': 2})

    def test_invalid_saved_limits_do_not_prevent_startup(self):
        S.save({'limits': ['invalid']})
        manager = self.manager()
        self.assertEqual(manager.limits(), {'gpu': 1, 'cpu': 2})
        self.assertTrue(manager._load_error)

    def test_sse_snapshot_contains_restored_hold(self):
        manager = self.manager()
        self.add(manager)
        manager._persist()
        restored = self.manager()
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        with patch.object(server, 'get_manager', return_value=restored):
            thread.start()
            conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
            try:
                conn.request('GET', '/api/events')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                first = response.readline().decode('utf-8')
                data = json.loads(first.removeprefix('data: '))
                self.assertEqual(data['restore'], {'held': True, 'queued': 1})
            finally:
                conn.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_watchdog_and_queue_writers_complete_together(self):
        manager = self.manager()
        job, proc = self.live(manager)
        start = threading.Barrier(3)
        errors = []
        def write():
            try:
                start.wait(timeout=2)
                for _ in range(40):
                    manager._persist()
            except Exception as exc:
                errors.append(exc)
        def protect():
            try:
                start.wait(timeout=2)
                for _ in range(20):
                    self.tick(manager, job, proc, 1000)
                    self.tick(manager, job, proc, 3000)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=fn, daemon=True) for fn in (write, protect)]
        for thread in threads:
            thread.start()
        start.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads), 'queue writer/guard deadlock')
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(J.QUEUE_FILE.read_text(encoding='utf-8'))['jobs'][0]['status'], 'running')

    def test_failed_worker_start_releases_slot(self):
        manager = self.manager()
        job = self.add(manager)
        with patch.object(manager, '_next_queued', side_effect=[job, ValueError('stop test loop')]), \
                patch.object(J.threading.Thread, 'start', side_effect=RuntimeError('fake exhaustion')):
            with self.assertRaisesRegex(ValueError, 'stop test loop'):
                manager.run_forever()
        self.assertEqual(job.status, 'failed')
        self.assertEqual(manager._busy['cpu'], 0)

    def test_live_process_memory_is_readable(self):
        usage = M.process_memory_mb(os.getpid())
        self.assertIsNotNone(usage)
        self.assertGreater(usage['resident_mb'], 0)
        self.assertGreater(usage['private_mb'], 0)

    def test_bad_records_do_not_drop_valid_neighbours(self):
        items = [{'id': ['bad'], 'src': 'bad.mp4', 'profile_id': 'P5'},
                 {'id': 'test0001', 'src': 'bad.mp4', 'profile_id': 'P5', 'status': []},
                 {'id': 'test0002', 'src': 'bad.mp4', 'profile_id': 'P5', 'elapsed': 'bad'},
                 {'id': 'test0003', 'src': 'good.mp4', 'profile_id': 'P5'}]
        J.QUEUE_FILE.write_text(json.dumps({'jobs': items}), encoding='utf-8')
        manager = self.manager()
        self.assertEqual([j['id'] for j in manager.list_jobs()], ['test0003'])
        self.assertTrue(manager.restore_state()['held'])

    def test_same_size_reencode_still_runs_keyframe_gate(self):
        out = Path(self.tmp.name) / 'synthetic.mp4'
        out.write_bytes(b'fake media for mocked probe')
        info = MediaInfo(path=str(out), ok=True, duration=10, size_bytes=out.stat().st_size,
                         streams=[StreamInfo(0, 'video', codec='h264', width=320, height=180, fps=30)])
        scan = SimpleNamespace(frames=300, keys=[0.0], errors=[], notes=[])
        with patch.object(V, 'probe', return_value=info), \
                patch('core.probe.scan_stream', return_value=scan), \
                patch.object(V, 'keyframe_times', return_value=([0.0], [])):
            result = V.verify(str(out), info, reencoded=True)
        self.assertEqual(result.verdict, 'failed')
        key_check = next(c for c in result.checks if c.id == 'V2')
        self.assertEqual(key_check.status, 'fail')


if __name__ == '__main__':
    unittest.main(verbosity=2)
