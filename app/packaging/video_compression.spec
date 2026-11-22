# Build with the dedicated vcs Python and PyInstaller from the build-tools folder.
import os
import json
import sys
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

app = Path(SPECPATH).parent
payload = Path(os.environ['VCS_BUILD_PAYLOAD'])
prefix = Path(sys.prefix)
vc_names = ('vcruntime140.dll', 'vcruntime140_1.dll', 'vcruntime140_threads.dll',
            'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
            'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll')
binaries = [(str(prefix / name), '.') for name in vc_names]
binaries += [(str(prefix / 'Library/bin/libmpdec-4.dll'), '.')]
release = json.loads((payload / 'release-info.json').read_text(encoding='utf-8'))
binaries += [(str(payload / 'bin' / name), 'bin') for name in release['media_runtime_files']]

a = Analysis(
    [str(app / 'packaging/launcher.py')],
    pathex=[str(app / 'backend'), str(app / 'packaging')],
    binaries=binaries,
    datas=[(str(app / 'web/index.html'), 'web'),
           (str(app / 'web/app.js'), 'web'),
           (str(app / 'web/style.css'), 'web'),
           (str(app / 'backend/profiles/profiles.json'), 'profiles'),
           (str(payload / 'licenses'), 'licenses'),
           (str(payload / 'release-info.json'), '.'),
           (str(payload / '使用说明.txt'), '.')] + copy_metadata('pywebview'),
    hiddenimports=['webview.platforms.winforms', 'webview.platforms.edgechromium',
                   'clr', 'pythonnet', 'clr_loader'],
    hookspath=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'qtpy', 'gtk', 'gi',
              'cefpython3', 'webview.platforms.cef', 'webview.platforms.qt',
              'webview.platforms.gtk', 'webview.platforms.cocoa', 'psutil'],
    noarchive=False,
)
# Dependency scanning also adds these DLLs at the root. Keep their explicit
# bin/ copies beside both media executables so they are shared exactly once.
shared_dlls = {name.lower() for name in release['media_runtime_files'] if name.lower().endswith('.dll')}
a.binaries = [entry for entry in a.binaries if entry[0].lower() not in shared_dlls]
collected = {entry[0].replace('\\', '/') for entry in a.binaries}
assert all('bin/' + name in collected for name in release['media_runtime_files'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name='VideoCompressionSuite', console=False, debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False,
          uac_admin=False, version=str(app / 'packaging/version_info.txt'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name='VideoCompressionSuite')
