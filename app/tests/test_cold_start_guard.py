"""Cold-start correctness and predecessor safety, without native UI or media."""
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))
import desktop as D
import server as S
from core import caps as C


class ColdStartGuard(unittest.TestCase):
    def response(self, value):
        result = Mock()
        result.status = 200
        result.read.return_value = value if isinstance(value, bytes) else json.dumps(value).encode()
        result.__enter__ = Mock(return_value=result)
        result.__exit__ = Mock(return_value=False)
        return result

    def health(self, pid=None):
        return self.response({'ok': True, 'service': 'video-compression-suite',
                              'pid': pid or os.getpid(), 'instance': D.runtime.instance_id()})

    def fake_server(self, port=49123):
        server = Mock()
        server.server_address = ('127.0.0.1', port)
        return server

    def test_cold_bind_precedes_health_and_never_checks_a_predecessor(self):
        host = D.DesktopHost(port=0)
        events = []
        def bind(*args):
            events.append('bind')
            return self.fake_server()
        def health(*args, **kwargs):
            events.append('health')
            return self.health()
        with patch.object(S, 'create_server', side_effect=bind), patch('urllib.request.urlopen', side_effect=health), \
             patch.object(D.threading, 'Thread') as threads, patch.object(host, '_stale_action', side_effect=AssertionError('cold path must not inspect predecessor')):
            self.assertTrue(host.start_backend())
        self.assertEqual(events, ['bind', 'health'])
        self.assertEqual(host.port, 49123)
        self.assertIn(':49123/', host.base_url)
        self.assertEqual(threads.call_count, 2)
        self.assertEqual(threads.call_args.kwargs['target'], host._warm_capabilities)
        self.assertTrue(threads.call_args.kwargs['daemon'])

    def reused(self, active=None, stopped=False, action='reuse'):
        host = D.DesktopHost()
        info = {'started': host._code_mtime() - 3600, 'pid': 4242}
        if active is not None:
            info['active_jobs'] = active
        with patch.object(S, 'create_server', side_effect=OSError('occupied')) as create, \
             patch('urllib.request.urlopen', side_effect=[self.health(4242), self.response(info)]), \
             patch.object(host, '_stop_stale', return_value=stopped) as stop:
            self.assertTrue(host.start_backend())
            self.assertTrue(host.reused_backend)
            self.assertEqual(create.call_count, 1)
            if action == 'replace':
                stop.assert_called_once_with(4242)
            else:
                stop.assert_not_called()

    def test_busy_predecessor_is_kept(self):
        self.reused(active=2)

    def test_unknown_predecessor_is_kept(self):
        self.reused()

    def test_stop_refusal_never_starts_another_instance(self):
        self.reused(active=0, action='replace')

    def test_idle_predecessor_replacement_waits_for_port_release(self):
        host = D.DesktopHost()
        info = {'started': host._code_mtime() - 3600, 'active_jobs': 0, 'pid': 4242}
        new = self.fake_server(8787)
        responses = [self.health(4242), self.response(info), OSError('gone'), OSError('gone'), self.health()]
        with patch.object(S, 'create_server', side_effect=[OSError('occupied'), new]) as create, \
             patch('urllib.request.urlopen', side_effect=responses), patch.object(host, '_stop_stale', return_value=True), \
             patch.object(D.threading, 'Thread'):
            self.assertTrue(host.start_backend())
        self.assertFalse(host.reused_backend)
        self.assertEqual(create.call_count, 2)

    def test_old_port_still_bound_after_stop_does_not_fall_back(self):
        host = D.DesktopHost()
        info = {'started': host._code_mtime() - 3600, 'active_jobs': 0, 'pid': 4242}
        old = [self.health(4242), self.response(info), OSError('gone'), OSError('gone')]
        with patch.object(S, 'create_server', side_effect=OSError('still bound')) as create, \
             patch('urllib.request.urlopen', side_effect=old), patch.object(host, '_stop_stale', return_value=True), \
             patch.object(D, 'next_free_port', side_effect=AssertionError('do not start another instance')):
            self.assertFalse(host.start_backend())
        self.assertEqual(create.call_count, 2)
        self.assertIn('尚未释放', host.start_error)

    def test_foreign_http_200_is_not_reused(self):
        host = D.DesktopHost()
        with patch.object(S, 'create_server', side_effect=[OSError('occupied'), self.fake_server(49124)]), \
             patch('urllib.request.urlopen', side_effect=[self.response(b'<html>foreign</html>'), self.health()]), \
             patch.object(D, 'next_free_port', return_value=49124), patch.object(D.threading, 'Thread'), \
             patch.object(host, '_stop_stale', side_effect=AssertionError('foreign process')):
            self.assertTrue(host.start_backend())
        self.assertFalse(host.reused_backend)
        self.assertEqual(host.port, 49124)

    def test_wrong_pid_cannot_confirm_new_instance(self):
        host = D.DesktopHost()
        new = self.fake_server()
        with patch.object(S, 'create_server', return_value=new), \
             patch('urllib.request.urlopen', side_effect=lambda *a, **k: self.health(4242)), \
             patch.object(D.threading, 'Thread'), patch.object(D.time, 'sleep'):
            self.assertFalse(host.start_backend())
        new.shutdown.assert_called_once()
        new.server_close.assert_called_once()

    def test_failed_bind_does_not_start_inbox_watcher(self):
        with patch.object(S, 'ThreadingHTTPServer', side_effect=OSError('occupied')), patch.object(S._INBOX, 'start') as watcher:
            with self.assertRaises(OSError):
                S.create_server(port=49123)
        watcher.assert_not_called()

    def test_stop_requires_project_path_and_successful_exit(self):
        host = D.DesktopHost()
        with patch.object(D.winapi, 'process_image_path', return_value=r'D:\python.exe'), \
             patch.object(D.winapi, 'process_command_line', return_value=r'python E:\unrelated\desktop.py'), \
             patch.object(D.subprocess, 'run', side_effect=AssertionError('wrong project')):
            self.assertFalse(host._stop_stale(4242))
        command = 'python "' + str(Path(D.__file__).resolve()) + '"'
        with patch.object(D.winapi, 'process_image_path', return_value=r'D:\python.exe'), \
             patch.object(D.winapi, 'process_command_line', return_value=command), \
             patch.object(D.subprocess, 'run', side_effect=[Mock(stdout='python.exe'), Mock(returncode=1)]):
            self.assertFalse(host._stop_stale(4242))

    def test_only_reported_hardware_is_probed_and_ten_bit_is_retained(self):
        caps = C.Capabilities()
        caps.ffmpeg = caps.ffprobe = 'owned-fixture'
        offered = [n for n in C.DISPLAY_ENCODERS if n.endswith(C.HARDWARE_SUFFIXES)]
        registered = offered + ['mjpeg_qsv', 'mpeg2_qsv', 'vp9_qsv', 'hevc_amf']
        def read(argv, **kwargs):
            if '-encoders' in argv:
                return '\n'.join(' V..... ' + n + ' fixture' for n in registered)
            if '-h' in argv:
                return 'Supported pixel formats: yuv420p p010le'
            return 'ffmpeg version fixture'
        tested = []
        def test(name, fmt, deadline):
            tested.append((name, fmt))
            return {'ok': True, 'error': ''}
        with patch.object(C, '_run', side_effect=read), patch.object(caps, '_test_hardware', side_effect=test):
            caps.probe()
        self.assertEqual({n for n, fmt in tested}, set(offered))
        self.assertIn(('hevc_nvenc', 'p010le'), tested)
        self.assertIn(('av1_nvenc', 'p010le'), tested)
        for unused in registered[len(offered):]:
            self.assertFalse(caps.has_encoder(unused))
            self.assertFalse(caps.supports_pix_fmt(unused, 'yuv420p'))
        self.assertTrue(all(caps.has_encoder(n) for n in offered))

    def test_registered_unprobed_hardware_cannot_be_selected(self):
        caps = C.Capabilities()
        caps.encoders = ['hevc_nvenc', 'libx265']
        self.assertFalse(caps.has_encoder('hevc_nvenc'))
        self.assertFalse(caps.supports_pix_fmt('hevc_nvenc', 'p010le'))
        self.assertTrue(caps.has_encoder('libx265'))

    def test_failed_warmup_does_not_publish_a_successful_snapshot(self):
        with patch.object(C, 'get_caps', side_effect=RuntimeError('fixture failure')) as probe:
            D.DesktopHost._warm_capabilities()
        probe.assert_called_once_with()


if __name__ == '__main__':
    with patch('sys.stdout', new=io.StringIO()):
        outcome = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ColdStartGuard))
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
