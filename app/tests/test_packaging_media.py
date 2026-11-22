"""Shared runtime packaging must reject missing libraries and exclude stray files."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('payload_under_test', Path(__file__).resolve().parents[1] / 'packaging/prepare_payload.py')
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)


class MediaPackagingTests(unittest.TestCase):
    def fixture(self, root):
        media = root / 'bin'
        media.mkdir()
        for name in payload.MEDIA_FILES:
            (media / name).write_bytes(b'private fixture')
        return media

    def test_stray_executables_and_user_data_are_not_collected(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            media = self.fixture(root)
            (media / 'ffplay.exe').write_bytes(b'extra executable')
            (media / 'queue.json').write_text('{}', encoding='utf-8')
            selected = payload.media_runtime_files(media)
            self.assertEqual(len(selected), 9)
            self.assertFalse(any(p.name in ('ffplay.exe', 'queue.json') for p in selected))

    def test_missing_shared_library_fails_before_creating_payload(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            media = self.fixture(root)
            # Build a separate incomplete input rather than removing any fixture.
            incomplete = root / 'incomplete'
            incomplete.mkdir()
            for name in payload.MEDIA_FILES:
                if name != 'avcodec-62.dll':
                    (incomplete / name).write_bytes(b'private fixture')
            output = root / 'must-not-exist'
            with patch.object(sys, 'argv', ['prepare', '--output', str(output), '--downloads', str(root), '--media-dir', str(incomplete)]), \
                 patch.object(payload.subprocess, 'run', side_effect=AssertionError('must not execute incomplete input')):
                with self.assertRaisesRegex(FileNotFoundError, 'avcodec-62.dll'):
                    payload.main()
            self.assertFalse(output.exists())

    def test_unpinned_or_static_build_is_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            media = self.fixture(root)
            output = root / 'must-not-exist'
            with patch.object(sys, 'argv', ['prepare', '--output', str(output), '--downloads', str(root), '--media-dir', str(media)]), \
                 patch.object(payload.subprocess, 'run', return_value=Mock(returncode=0, stdout='ffmpeg version 8.0.1-full_build-www.gyan.dev\n')):
                with self.assertRaisesRegex(RuntimeError, 'pinned'):
                    payload.main()
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
