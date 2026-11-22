"""Project license files must ship with the release and appear in its manifest."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    'payload_under_test', Path(__file__).resolve().parents[1] / 'packaging/prepare_payload.py',
)
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)


class LicensePackagingTests(unittest.TestCase):
    license_contents = {
        'LICENSE': b'MIT license fixture\r\n',
        'THIRD_PARTY_NOTICES.md': '# 第三方组件\n'.encode('utf-8'),
    }

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / 'ffmpeg/bin'
        self.media.mkdir(parents=True)
        for filename in payload.MEDIA_FILES:
            (self.media / filename).write_bytes(b'media fixture')
        self.downloads = self.root / 'downloads'

    def prepare(self, project_root, output):
        argv = ['prepare', '--output', str(output), '--downloads', str(self.downloads),
                '--media-dir', str(self.media)]
        script = project_root / 'app/packaging/prepare_payload.py'
        with patch.object(payload, '__file__', str(script)), patch.object(sys, 'argv', argv):
            payload.main()

    def test_each_missing_project_license_fails_before_creating_payload(self):
        for omitted in self.license_contents:
            with self.subTest(omitted=omitted):
                project = self.root / f'missing-{omitted}'
                project.mkdir()
                for filename, contents in self.license_contents.items():
                    if filename != omitted:
                        (project / filename).write_bytes(contents)
                output = self.root / f'payload-{omitted}'
                with patch.object(payload.subprocess, 'run', side_effect=AssertionError('must not start FFmpeg')), \
                     self.assertRaises(FileNotFoundError) as error:
                    self.prepare(project, output)
                self.assertIn(str(project / omitted), str(error.exception))
                self.assertFalse(output.exists())

    def test_project_licenses_are_copied_and_hashed_in_release_manifest(self):
        project = self.root / 'project'
        project.mkdir()
        for filename, contents in self.license_contents.items():
            (project / filename).write_bytes(contents)
        for filename in ('web/index.html', 'web/app.js', 'web/style.css', 'backend/profiles/profiles.json',
                         'packaging/video_compression.spec', 'packaging/installer.iss',
                         'packaging/version_info.txt', 'packaging/build_release.ps1'):
            source = project / 'app' / filename
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b'source fixture')
        for filename in ('LICENSE', 'README.txt'):
            (self.media.parent / filename).write_bytes(b'FFmpeg fixture')
        self.downloads.mkdir()
        for filename in ('ffmpeg-894da5ca7d-source.tar.gz', 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe'):
            (self.downloads / filename).write_bytes(b'download fixture')
        runtime = self.root / 'python'
        runtime.mkdir()
        (runtime / 'LICENSE_PYTHON.txt').write_bytes(b'Python license fixture')

        output = self.root / 'payload'
        version = Mock(returncode=0, stdout='ffmpeg version 8.0.1-full_build-www.gyan.dev\n'
                                            'configuration: --enable-shared\n')
        distribution = Mock(version='0.0.0', files=[])
        distribution.read_text.return_value = 'Name: fixture\n'
        with patch.object(sys, 'prefix', str(runtime)), \
             patch.object(payload.subprocess, 'run', return_value=version), \
             patch.object(payload.md, 'distribution', return_value=distribution):
            self.prepare(project, output)

        manifest = json.loads((output / 'release-info.json').read_text(encoding='utf-8'))
        for filename, contents in self.license_contents.items():
            with self.subTest(filename=filename):
                entry = f'licenses/application/{filename}'
                self.assertEqual((output / entry).read_bytes(), contents)
                self.assertEqual(manifest['files'][entry], hashlib.sha256(contents).hexdigest())


if __name__ == '__main__':
    unittest.main(verbosity=2)
