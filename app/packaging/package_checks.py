"""Isolated release diagnostics, run inside the frozen executable itself."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import traceback
import urllib.request

from core import runtime


def run(report: Path, window: bool = False) -> int:
    result = {'ok': False, 'checks': [], 'frozen': runtime.frozen()}
    root = os.environ.get('VCS_DATA_DIR')
    # Refuse diagnostics against an actual user's settings or queue.
    if not root or os.environ.get('VCS_STORE_DIR'):
        result['error'] = 'Diagnostics require a private VCS_DATA_DIR and no VCS_STORE_DIR'
        report.write_text(json.dumps(result), encoding='utf-8')
        return 2
    root = Path(root).resolve()
    if runtime.store_dir().exists():
        result['error'] = 'Diagnostics require a fresh data directory'
        report.write_text(json.dumps(result), encoding='utf-8')
        return 2

    def check(name, value, detail=None):
        result['checks'].append({'name': name, 'ok': bool(value), 'detail': detail})
        if not value:
            raise AssertionError(name + ': ' + str(detail))

    try:
        if window:
            _window(check)
        else:
            _backend(root, check, result)
        result['ok'] = True
    except Exception:
        result['error'] = traceback.format_exc()
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['ok'] else 1


def _backend(root, check, result):
    import decimal
    check('native decimal runtime', decimal.Decimal('0.1') + decimal.Decimal('0.2') == decimal.Decimal('0.3'))
    from core import caps, planner, probe, verifier, settings
    import server
    import desktop

    check('desktop version metadata', desktop.version() == '6.2.1', desktop.version())

    for name in ('index.html', 'app.js', 'style.css'):
        check('bundled web ' + name, (server.WEB_DIR / name).is_file())
    check('bundled profiles', len(planner.load_profiles()) >= 8)
    manifest = json.loads((runtime.resource_root() / 'release-info.json').read_text(encoding='utf-8'))
    import hashlib
    media_names = manifest['media_runtime_files']
    check('full shared runtime file list', manifest['ffmpeg_linkage'] == 'shared' and len(media_names) == 9)
    for name in media_names:
        path = runtime.resource_root() / 'bin' / name
        with path.open('rb') as handle:
            checksum = hashlib.file_digest(handle, 'sha256').hexdigest()
        check('media file integrity ' + name, checksum == manifest['files']['bin/' + name])
    check('isolated inbox', server.INBOX_DIR.parent == root)
    check('isolated settings', settings.STORE_DIR == root / 'store')

    # Loading the actual renderer verifies the pythonnet/CLR and WebView2 SDK DLLs
    # without showing a window or opening any native dialog.
    from webview.platforms import edgechromium
    from webview.platforms import winforms
    check('native .NET renderer imports', edgechromium is not None and winforms is not None)
    loader = runtime.resource_root() / 'webview/lib/runtimes/win-x64/native/WebView2Loader.dll'
    check('WebView2 loader', loader.is_file(), str(loader))
    dll = ctypes.WinDLL(str(loader))
    get_version = dll.GetAvailableCoreWebView2BrowserVersionString
    get_version.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    get_version.restype = ctypes.c_long
    value = ctypes.c_void_p()
    status = get_version(None, ctypes.byref(value))
    try:
        version = ctypes.wstring_at(value.value) if value.value else ''
        check('installed WebView2 runtime', status == 0 and bool(version), version)
    finally:
        if value.value:
            ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ctypes.windll.ole32.CoTaskMemFree(value)

    for name in ('ffmpeg', 'ffprobe'):
        binary = caps._find_binary(name)
        check('bundled ' + name, binary and Path(binary).parent == runtime.resource_root() / 'bin', binary)
        proc = subprocess.run([binary, '-version'], timeout=10, **caps.subprocess_kwargs())
        check(name + ' execution', proc.returncode == 0, proc.stdout.splitlines()[0])

    httpd = server.create_server(port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(httpd.server_address[1])
    try:
        for path in ('/api/health', '/', '/app.js', '/style.css'):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                payload = r.read()
                check('HTTP ' + path, r.status == 200 and len(payload) > 0)
                if path == '/api/health':
                    check('data instance identity', json.loads(payload)['instance'] == runtime.instance_id())
    finally:
        server._INBOX.stop()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)

    # Real, tiny synthetic video; single software encode, no user media or GPU.
    source = root / '合成测试 source.mp4'
    source.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = caps._find_binary('ffmpeg')
    args = [ffmpeg, '-hide_banner', '-nostdin', '-n', '-f', 'lavfi', '-i',
            'testsrc2=size=160x96:rate=10:duration=2', '-c:v', 'libx264', '-threads', '1',
            '-preset', 'ultrafast', '-crf', '12', '-g', '10', '-pix_fmt', 'yuv420p', str(source)]
    proc = subprocess.run(args, timeout=20, **caps.subprocess_kwargs())
    check('synthetic source', proc.returncode == 0, proc.stderr[-1000:])
    info = probe.probe(str(source))
    check('real source probe', info.ok, info.error)
    c = caps.Capabilities()
    c.encoders = ['libx264', 'aac']
    # The real frozen tools are used; only hardware selection is excluded from this
    # small deterministic software-encoding diagnostic.
    plan = planner.make_plan(info, planner.get_profile('P1'),
                             planner.Overrides(location='custom', out_dir=str(root),
                                               name_template='压缩结果 output', crf=32,
                                               preset='veryfast', keep_timestamps=False,
                                               extra_args=['-threads', '1']), caps=c)
    output = Path(plan.out_path)
    check('software fallback selected', 'libx264' in plan.argv, plan.argv)
    proc = subprocess.run(plan.argv, timeout=30, **caps.subprocess_kwargs())
    check('real planned encode', proc.returncode == 0 and output.is_file(), proc.stderr[-1000:])
    verified = verifier.verify(str(output), source=info, deep=True)
    result['verification'] = verified.to_dict()
    check('delivery verification', verified.verdict == 'passed', result['verification'])
    settings.save({'show_advanced': True})
    check('per-user settings persistence', (root / 'store/settings.json').is_file())
    import package_media_checks
    package_media_checks.run(root, source, output, info, check, result)


def _window(check):
    import desktop

    class TestHost(desktop.DesktopHost):
        error = None
        loaded = False

        def _on_loaded(self):
            try:
                super()._on_loaded()
                self.loaded = True
                self.window.evaluate_js('window.pywebview.api.ping().then(v => window.__packagePing = v)')
                end = time.monotonic() + 25
                while time.monotonic() < end:
                    ready = self.window.evaluate_js('({shell: window.__vcsDesktopShell === true, ping: window.__packagePing, script: typeof window.__vcsDrainDropped === "function", text: document.body.innerText.length})')
                    if ready.get('ping') == 'pong' and ready.get('script'):
                        break
                    time.sleep(.1)
                check('real desktop document', ready.get('shell') and ready.get('text', 0) > 100, ready)
                check('native JS bridge round trip', ready.get('ping') == 'pong', ready)
                check('desktop drop handler binding', self.bound_drop_events == ['body.drop'], self.bound_drop_events)
                check('actual application script executed', ready.get('script'), ready)
                check('private ephemeral backend', not self.reused_backend and self.port > 0)
            except Exception:
                self.error = traceback.format_exc()
            finally:
                self.window.destroy()

    host = TestHost(port=0)
    timer = threading.Timer(40, lambda: host.window.destroy() if host.window else None)
    timer.daemon = True
    timer.start()
    try:
        code = host.run()
        check('native window lifecycle', code == 0 and host.loaded and not host.error, host.error)
    finally:
        timer.cancel()
        if host._server:
            host._server.shutdown()
            host._server.server_close()
