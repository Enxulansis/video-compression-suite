"""Installed paths, bundled-tool isolation and predecessor identity regressions."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core import runtime as R, caps as C
import desktop as D
import server as S


class PackagingTests(unittest.TestCase):
    def test_source_paths_are_preserved(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(R, 'frozen', return_value=False):
            app = Path(__file__).resolve().parents[1]
            self.assertEqual(R.resource_root(), app)
            self.assertEqual(R.store_dir(), app / 'backend/store')
            self.assertEqual(R.profiles_path(), app / 'backend/profiles/profiles.json')

    def test_installed_data_is_outside_the_bundle(self):
        with tempfile.TemporaryDirectory() as t, patch.dict(os.environ, {'LOCALAPPDATA': t}, clear=True), \
             patch.object(R, 'frozen', return_value=True), patch.object(sys, '_MEIPASS', str(Path(t) / 'installed/_internal'), create=True):
            self.assertEqual(R.data_root(), Path(t) / R.APP_ID)
            self.assertEqual(R.store_dir(), Path(t) / R.APP_ID / 'store')
            self.assertNotEqual(R.data_root(), R.resource_root())
            self.assertEqual(R.profiles_path(), R.resource_root() / 'profiles/profiles.json')

    def test_private_data_and_explicit_store_override(self):
        with tempfile.TemporaryDirectory() as t, patch.dict(os.environ, {'VCS_DATA_DIR': t}, clear=True):
            self.assertEqual(R.data_root(), Path(t).resolve())
            self.assertEqual(R.store_dir(), Path(t).resolve() / 'store')
            with patch.dict(os.environ, {'VCS_STORE_DIR': str(Path(t) / 'owned-store')}):
                self.assertEqual(R.store_dir(), Path(t) / 'owned-store')

    def test_missing_bundle_tool_cannot_borrow_from_path(self):
        with tempfile.TemporaryDirectory() as t, patch.object(R, 'frozen', return_value=True), \
             patch.object(R, 'resource_root', return_value=Path(t)), patch.object(C.shutil, 'which', side_effect=AssertionError('must not borrow')):
            self.assertIsNone(C._find_binary('ffmpeg'))
            (Path(t) / 'bin').mkdir()
            expected = Path(t) / 'bin/ffmpeg.exe'
            expected.write_bytes(b'owned-test-placeholder')
            self.assertEqual(C._find_binary('ffmpeg'), str(expected))

    def test_source_tool_discovery_remains_available(self):
        with patch.object(R, 'frozen', return_value=False), patch.object(C.shutil, 'which', return_value='owned-ffmpeg'):
            self.assertEqual(C._find_binary('ffmpeg'), 'owned-ffmpeg')

    def test_instance_identity_changes_with_store_and_distribution(self):
        with tempfile.TemporaryDirectory() as t, patch.dict(os.environ, {'VCS_DATA_DIR': t}, clear=True), patch.object(R, 'frozen', return_value=False):
            original = R.instance_id()
            with patch.object(R, 'frozen', return_value=True):
                self.assertNotEqual(R.instance_id(), original)
            with patch.dict(os.environ, {'VCS_STORE_DIR': str(Path(t) / 'second')}):
                self.assertNotEqual(R.instance_id(), original)

    def test_installed_launcher_does_not_stop_source_predecessors(self):
        with patch.object(R, 'frozen', return_value=True):
            host = D.DesktopHost()
            self.assertEqual(host._stale_action({'started': 1, 'active_jobs': 0}), 'reuse')

    def response(self, payload):
        response = Mock(status=200)
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_installed_launcher_does_not_reuse_a_foreign_data_instance(self):
        self.foreign_instance(True)

    def test_source_launcher_does_not_reuse_an_installed_or_other_data_instance(self):
        self.foreign_instance(False)

    def foreign_instance(self, frozen):
        host = D.DesktopHost(port=12345)
        native = Mock(server_address=('127.0.0.1', 12346))
        base = {'ok': True, 'service': 'video-compression-suite', 'pid': os.getpid()}
        responses = [self.response(dict(base, instance='foreign-data')), self.response(dict(base, instance='owned-data'))]
        with patch.object(R, 'frozen', return_value=frozen), patch.object(R, 'instance_id', return_value='owned-data'), \
             patch.object(S, 'create_server', side_effect=[OSError('occupied'), native]), \
             patch('urllib.request.urlopen', side_effect=responses), patch.object(D, 'next_free_port', return_value=12346), \
             patch.object(D.threading, 'Thread'), patch.object(host, '_stop_stale', side_effect=AssertionError('foreign service')):
            self.assertTrue(host.start_backend())
            self.assertFalse(host.reused_backend)
            self.assertEqual(host.port, 12346)


if __name__ == '__main__':
    unittest.main(verbosity=2)
