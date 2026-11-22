"""Live telemetry/resource recovery: synthetic bytes and bounded loopback HTTP only."""
import http.client
import json
import os
import sys
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from contextlib import contextmanager
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_resource_guard import GuardRegression, server, J, M, S
from core import probe as P, work, caps


class LiveMemory(unittest.TestCase):
    setUp = GuardRegression.setUp
    manager = GuardRegression.manager

    def source(self):
        p = Path(self.tmp.name) / 'owned-synthetic.mp4'
        p.write_bytes(b'synthetic-fixture-not-a-real-video')
        return str(p)

    def raw(self):
        return {'format': {'duration': '10', 'format_name': 'mp4'}, 'streams': [
            {'index': 0, 'codec_type': 'video', 'codec_name': 'h264', 'width': 320,
             'height': 180, 'pix_fmt': 'yuv420p', 'avg_frame_rate': '24/1'}]}

    def memory(self, free=6000, commit=20000):
        return {'available': 1, 'free_mb': free, 'total_mb': 32000,
                'commit_limit_mb': 84000, 'commit_used_mb': 84000 - commit}

    @contextmanager
    def connection(self, manager):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        with patch.object(server, 'get_manager', return_value=manager):
            t.start()
            c = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
            try: yield c
            finally:
                c.close(); httpd.shutdown(); httpd.server_close(); t.join(timeout=2)
                self.assertFalse(t.is_alive())

    def call(self, c, path, body=None):
        c.request('GET' if body is None else 'POST', path,
                  None if body is None else json.dumps(body), {'Content-Type': 'application/json'})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    def cached(self, manager, src):
        with patch.object(P, '_probe_json', return_value=self.raw()):
            info = P.probe(src)
        self.assertTrue(info.ok)
        manager.remember_probe(info)
        return info

    def test_transient_exceptions_remain_retryable_bad_source_remains_bad(self):
        src = self.source()
        for failure in (work.Busy('waiting'), work.Expired('deadline')):
            with patch.object(P, '_probe_json', side_effect=failure), self.assertRaises(type(failure)):
                P.probe(src)
        with patch.object(P, '_probe_json', side_effect=RuntimeError('invalid container')):
            info = P.probe(src)
        self.assertFalse(info.ok); self.assertIn('invalid container', info.error)
        manager = self.manager(); manager.remember_probe(info)
        self.assertIsNone(manager.cached_probe(src))

    def test_failed_probe_single_response_then_live_recovery_on_same_connection(self):
        manager = self.manager(); src = self.source()
        fake = caps.Capabilities(); fake.ffprobe = 'ffprobe'
        mem = self.memory(2300)
        with patch.object(M, 'machine_memory', side_effect=lambda: dict(mem)), \
             patch.object(P, 'get_caps', return_value=fake), self.connection(manager) as c:
            status, data = self.call(c, '/api/probe', {'paths': [src], 'check_seek': False})
            self.assertEqual(status, 429, data)
            self.assertIn('等待内存', data['error'])
            status, data = self.call(c, '/api/memory')
            self.assertEqual(status, 200, data) # no second HTTP error left in stream
            self.assertEqual(data['memory']['free_mb'], 2300)
            self.assertFalse(data['probe_gate']['ok'])
            mem['free_mb'] = 4700
            status, data = self.call(c, '/api/memory')
            self.assertEqual(status, 200); self.assertEqual(data['memory']['free_mb'], 4700)
            self.assertTrue(data['probe_gate']['ok'])
            with patch.object(P, '_probe_json', return_value=self.raw()), \
                 patch.object(manager, 'scan_seek_async', side_effect=AssertionError('no keyframe scan')):
                status, data = self.call(c, '/api/probe', {'paths': [src], 'check_seek': False})
            self.assertEqual(status, 200, data); self.assertTrue(data['items'][0]['ok'])
            self.assertEqual(data['items'][0]['seek']['verdict'], 'unmeasured')
            self.assertEqual(manager.list_jobs(), [])

    def test_expired_probe_http_then_usable_connection(self):
        manager = self.manager(); src = self.source()
        with self.connection(manager) as c, patch.object(P, '_probe_json', side_effect=work.Expired('deadline')):
            self.assertEqual(self.call(c, '/api/probe', {'paths': [src], 'check_seek': False})[0], 504)
            self.assertEqual(self.call(c, '/api/memory')[0], 200)

    def test_cached_plan_and_enqueue_during_memory_pressure_and_auxiliary_busy(self):
        manager = self.manager(); src = self.source(); self.cached(manager, src)
        self.assertTrue(work._requests.acquire(blocking=False))
        try:
            with patch.object(M, 'machine_memory', return_value=self.memory(900)), \
                 patch.object(P, '_probe_json', side_effect=AssertionError('cached source must not probe')), \
                 self.connection(manager) as c:
                for route, body in [('/api/plan', {'path': src, 'profile': 'P1'}),
                                    ('/api/estimate/batch', {'paths': [src], 'profile': 'P1'})]:
                    status, data = self.call(c, route, body)
                    self.assertEqual(status, 200, data)
                status, data = self.call(c, '/api/jobs', {'paths': [src], 'profile': 'P1'})
                self.assertEqual(status, 201, data)
                self.assertEqual(data['jobs'][0]['status'], 'queued')
                self.assertFalse(manager.memory_gate(manager._jobs[data['jobs'][0]['id']])['ok'])
        finally: work._requests.release()

    def test_uncached_request_does_not_bypass_busy_guard(self):
        manager = self.manager(); src = self.source()
        self.assertTrue(work._requests.acquire(blocking=False))
        try:
            with self.connection(manager) as c:
                self.assertEqual(self.call(c, '/api/plan', {'path': src, 'profile': 'P1'})[0], 429)
        finally: work._requests.release()

    def test_cache_changed_same_size_within_same_second_invalidates(self):
        manager = self.manager(); src = self.source()
        st = os.stat(src)
        base = (st.st_mtime_ns // 1000000000) * 1000000000 + 300000000
        os.utime(src, ns=(st.st_atime_ns, base))
        self.cached(manager, src)
        os.utime(src, ns=(st.st_atime_ns, base + 1000000))
        self.assertIsNone(manager.cached_probe(src))

    def test_snapshot_commit_pressure_stricter_policy_and_no_store_mutation(self):
        S.save({'memory_guard': True, 'memory_margin_mb': 3000})
        manager = self.manager()
        before = S.SETTINGS_FILE.read_bytes()
        with patch.object(M, 'machine_memory', return_value=self.memory(6000, 2900)):
            data = manager.memory_snapshot()
        self.assertEqual(data['policy']['margin_mb'], 3000)
        self.assertFalse(data['probe_gate']['ok']); self.assertIn('提交', data['probe_gate']['reason'])
        self.assertGreater(data['sampled_at'], 0)
        self.assertEqual(S.SETTINGS_FILE.read_bytes(), before)
        self.assertFalse(J.QUEUE_FILE.exists())

    def test_changed_after_route_cache_check_keeps_child_memory_guard(self):
        manager = self.manager(); src = self.source(); info = self.cached(manager, src)
        fake = caps.Capabilities(); fake.ffprobe = 'ffprobe'
        # First cache read passes the fast-route check, second detects source change.
        with patch.object(manager, 'cached_probe', side_effect=[info, None]), \
             patch.object(M, 'machine_memory', return_value=self.memory(900)), \
             patch.object(P, 'get_caps', return_value=fake), self.connection(manager) as c:
            status, data = self.call(c, '/api/plan', {'path': src, 'profile': 'P1'})
        self.assertEqual(status, 429, data)


if __name__ == '__main__':
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LiveMemory)).wasSuccessful())
