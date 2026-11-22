"""Native desktop window host (PyWebView), used when pywebview is installed.

Why this exists: a plain browser page cannot learn the absolute path of a file the
user drops onto it -- the browser engine discards it. PyWebView's WebView2 host can,
and exposes it to Python as `pywebviewFullPath`. That gives the one thing every other
approach could not: **drag files straight into the window and get real paths**.

PyWebView on Windows needs `pythonnet` (it is listed as a hard dependency of
pywebview for sys_platform == "win32"), so this module is optional: if the import
fails, the caller falls back to the browser --app window in start-app.ps1.

Install (run once, in the project venv):
    python -m venv .venv
    .venv\\Scripts\\python.exe -m pip install pywebview
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import winapi, runtime

#: Keep helper processes (tasklist/taskkill) from flashing a console window.  Defined
#: here rather than imported from core.caps because this module is also run directly
#: by pythonw with only its own directory on sys.path.
_CREATE_NO_WINDOW = 0x08000000


def _no_window() -> Dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"creationflags": _CREATE_NO_WINDOW, "startupinfo": startupinfo}

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
WINDOW_TITLE = "视频压缩套件 · Video Compression Suite"
WINDOW_SIZE = (1500, 980)

INSTALL_HINT = """未安装 pywebview，无法使用原生拖拽窗口。

本机 Python 3.9 太旧（pythonnet 新版要求 3.11+），所以用 uv 建一个 3.13 环境：
  cd E:\\视频压缩\\app
  uv venv --python 3.13 .venv
  uv pip install --python .venv\\Scripts\\python.exe -i https://pypi.tuna.tsinghua.edu.cn/simple pywebview

装完运行 check-desktop.ps1 自检，然后 start-desktop.bat 启动。
"""


def available() -> bool:
    """True when pywebview can actually be imported."""
    try:
        import webview  # noqa: F401
    except Exception:  # noqa: BLE001 - import errors vary by platform
        return False
    return True


def version() -> str:
    """Installed pywebview version.

    `webview.__version__` does NOT exist in pywebview 6.x, so read the package
    metadata instead -- the first attempt printed "unknown" for that reason.
    """
    try:
        import importlib.metadata as metadata
        return metadata.version("pywebview")
    except Exception:  # noqa: BLE001
        return "unknown"


def _import_webview():
    import webview
    return webview


class DesktopHost:
    """Runs the HTTP backend and wraps it in a native window."""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self.window: Optional[Any] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._dropped: List[str] = []
        self.bound_drop_events: List[str] = []
        self.reused_backend = False
        #: Why start-up failed, in words fit for a message box (pythonw has no console).
        self.start_error = ""

    @property
    def base_url(self) -> str:
        """Derived from `self.port`, NOT cached: the port can change when it is occupied.

        A cached URL is how a fallback port would silently open the window on the WRONG
        address (the old, occupied one), which is worse than failing loudly.
        """
        return "http://{}:{}/?app=1&desktop=1".format(HOST, self.port)

    @staticmethod
    def extract_paths(event: Any) -> List[str]:
        """Pull absolute paths out of a pywebview drop event, defensively.

        The payload shape has changed across pywebview versions and a wrong guess
        means "drag does nothing" with no error, so try every known location and
        never raise.
        """
        if not isinstance(event, dict):
            return []
        candidates: List[Any] = []
        data_transfer = event.get("dataTransfer")
        if isinstance(data_transfer, dict):
            files = data_transfer.get("files")
            if isinstance(files, list):
                candidates.extend(files)
        files = event.get("files")
        if isinstance(files, list):
            candidates.extend(files)

        paths: List[str] = []
        for item in candidates:
            if isinstance(item, str):
                paths.append(item)
                continue
            if isinstance(item, dict):
                # Only real path keys -- a bare `name` is just a filename and would
                # be rejected by the page anyway; using it would mask a real problem.
                for key in ("pywebviewFullPath", "path", "fullPath"):
                    value = item.get(key)
                    if value and isinstance(value, str):
                        paths.append(value)
                        break
        return paths

    # ------------------------------------------------------------------ backend

    @staticmethod
    def _code_mtime() -> float:
        """Newest mtime across backend sources + front-end assets.

        Mirrors `Get-CodeMtime` in start.ps1 so both launchers agree on what "the
        code on disk changed" means.
        """
        if runtime.frozen():
            return Path(sys.executable).stat().st_mtime
        root = runtime.resource_root()
        newest = 0.0
        targets = [
            root / "backend", root / "backend" / "core", root / "web",
            root / "backend" / "profiles",
        ]
        for folder in targets:
            if not folder.is_dir():
                continue
            for item in folder.iterdir():
                if not item.is_file():
                    continue
                if item.suffix not in (".py", ".js", ".css", ".html", ".json"):
                    continue
                try:
                    newest = max(newest, item.stat().st_mtime)
                except OSError:
                    continue
        return newest

    def _is_stale(self, info: Dict[str, Any]) -> bool:
        """True when the running server started before the newest code change.

        Comparing the server's reported mtime against the file's mtime can never
        differ (both read the same file -- that mistake made start.ps1's first
        attempt useless); the server's START TIME is the meaningful reference.
        """
        # Installed files cannot be replaced while the application mutex is held.
        # Never apply source-checkout process replacement to a packaged executable.
        if runtime.frozen():
            return False
        try:
            started = float(info.get("started") or 0)
        except (TypeError, ValueError):
            return False
        if started <= 0:
            return False
        try:
            return self._code_mtime() > started + 1
        except OSError:
            return False

    def _stale_action(self, info: Dict[str, Any]) -> str:
        """Decide what to do about an already-running backend.

        Returns one of:
          ``reuse``   -- it is current, or we cannot tell: just use it
          ``busy``    -- it is stale but still has work in flight: warn, never kill
          ``replace`` -- it is stale and idle: safe to stop and start fresh

        Kept as a pure function of the reported info so the one rule that really
        matters -- never kill an in-flight encode -- is directly testable.
        """
        if not info or not self._is_stale(info):
            return "reuse"
        try:
            if 'active_jobs' not in info or info['active_jobs'] is None:
                return 'reuse'
            active = int(info['active_jobs'])
        except (TypeError, ValueError):
            return 'reuse'
        if active < 0:
            return 'reuse'
        return "busy" if active > 0 else "replace"

    def _stop_stale(self, pid: Any) -> bool:
        """Stop a predecessor we started ourselves -- and only if it is OURS.

        The pid arrives over HTTP from whatever is listening on the port, so "it is a
        python process" is not enough: a fake server could name any python.exe on the
        machine and this method would then kill it (`/api/version` is trivially
        forgeable -- it is plain JSON from an unauthenticated local endpoint). The
        identity check therefore uses things the response cannot invent: the FULL image
        path of the process, and its command line, both of which must point at this
        project. When either cannot be read, we refuse and tell the user to use
        stop.ps1 -- a stale backend is an inconvenience, killing an innocent process is
        not.

        (`tasklist`/`taskkill` rather than `wmic`, which Microsoft is removing.)
        """
        try:
            target = int(pid)
        except (TypeError, ValueError):
            return False
        if target <= 0 or target == os.getpid():
            return False

        image = winapi.process_image_path(target)
        if not image or "python" not in Path(image).name.lower():
            return False
        command = winapi.process_command_line(target)
        if not command:
            return False
        try:
            backend_dir = str(Path(__file__).resolve().parent).lower()
        except OSError:
            return False
        # The command line must identify this checkout's complete backend path.
        # A bare desktop.py from another project is insufficient evidence.
        haystack = command.replace("/", "\\").lower()
        if backend_dir not in haystack:
            return False

        try:
            probe = subprocess.run(
                ["tasklist", "/FI", "PID eq {}".format(target), "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15, **_no_window())
            listing = (probe.stdout or "").lower()
        except (OSError, subprocess.SubprocessError):
            return False
        if "python" not in listing:
            return False
        try:
            result = subprocess.run(["taskkill", "/PID", str(target), "/F"],
                                    capture_output=True, timeout=15, **_no_window())
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def start_backend(self) -> bool:
        """Start the same server the browser version uses, in a daemon thread.

        If the port is already taken by OUR server (a background instance started by
        start.ps1 earlier, or another desktop window), reuse it instead of failing:
        otherwise the user double-clicks the launcher and gets nothing but a message,
        which is exactly the confusing failure we are trying to eliminate.

        Reuse is conditional, though: `start.ps1` has always replaced an instance
        that predates the code on disk, but the desktop path had no such check, so
        double-clicking the shortcut kept serving the OLD backend while newer code
        sat unused on disk -- the "I don't see the update" report.  The same policy
        is applied here, with one hard safety rule: a backend that still has jobs in
        flight is NEVER killed, only reported.
        """
        backend_dir = Path(__file__).resolve().parent
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))

        import urllib.request

        def healthy(expected_pid: Optional[int] = None) -> bool:
            try:
                with urllib.request.urlopen(
                        "http://{}:{}/api/health".format(HOST, self.port), timeout=1) as resp:
                    if resp.status != 200:
                        return False
                    data = json.loads(resp.read(4097).decode('utf-8'))
                return (isinstance(data, dict) and data.get('ok') is True
                        and data.get('service') == 'video-compression-suite'
                        and data.get('instance') == runtime.instance_id()
                        and type(data.get('pid')) is int and data['pid'] > 0
                        and (expected_pid is None or data['pid'] == expected_pid))
            except Exception:  # noqa: BLE001
                return False

        def running_info() -> Optional[Dict[str, Any]]:
            try:
                with urllib.request.urlopen(
                        "http://{}:{}/api/version".format(HOST, self.port), timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                return data if isinstance(data, dict) else None
            except Exception:  # noqa: BLE001
                return None

        from server import create_server

        try:
            # A free port can bind immediately; a failed HTTP connection to it costs
            # about one second on Windows. Inspect predecessors only after bind fails.
            self._server = create_server(HOST, self.port)
        except OSError as exc:
            if healthy():
                info = running_info()
                action = self._stale_action(info)
                if action == 'busy':
                    print('  [!] 旧后端还有任务在运行，本次保留并复用；任务空闲后再重启。')
                if action != 'replace' or not self._stop_stale((info or {}).get('pid')):
                    print('  [i] 复用端口 {} 的现有服务，没有启动第二实例。'.format(self.port))
                    self.reused_backend = True
                    return True
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and healthy():
                    time.sleep(.05)
                if healthy():
                    self.start_error = '旧服务尚未退出，请等待后重试；没有启动第二实例。'
                    return False
                try:
                    self._server = create_server(HOST, self.port)
                except OSError as exc2:
                    self.start_error = '旧端口尚未释放：{}；没有启动第二实例。'.format(exc2)
                    return False
            else:
                print("  [!] 无法监听 {}:{} —— {}".format(HOST, self.port, exc))
                fallback = next_free_port(self.port + 1)
                if not fallback:
                    self.start_error = '端口 {} 被其它程序占用，且找不到可用的替代端口。'.format(self.port)
                    return False
                print("  [i] 端口 {} 被其它程序占用，改用 {}".format(self.port, fallback))
                try:
                    self.port = fallback
                    self._server = create_server(HOST, self.port)
                except OSError as exc2:
                    self.start_error = '替代端口 {} 无法监听：{}'.format(fallback, exc2)
                    return False
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.4}, daemon=True)
        self._thread.start()

        for _ in range(50):
            if healthy(expected_pid=os.getpid()):
                # Overlap the same single capability probe with native GUI setup,
                # instead of leaving it idle until the page starts its first fetch.
                threading.Thread(target=self._warm_capabilities, name='vcs-capabilities', daemon=True).start()
                return True
            time.sleep(0.2)
        self.start_error = '新服务没有在限定时间内就绪。'
        self._server.shutdown()
        self._server.server_close()
        return False

    @staticmethod
    def _warm_capabilities() -> None:
        from core.caps import get_caps
        try:
            get_caps()
        except Exception as exc:  # page fetching keeps its normal error/retry path
            print('  [!] 编码能力预检测失败：{}'.format(exc))

    # ------------------------------------------------------------- window API

    def _js_api(self):
        """Object handed to JS as window.pywebview.api."""
        host = self

        class Api:
            def ping(self) -> str:
                return "pong"

            def take_dropped(self) -> List[str]:
                dropped, host._dropped = host._dropped, []
                return dropped

            def pick_paths(self, kind: str = "files", initial: str = "") -> Dict[str, Any]:
                """Native dialog owned by this window.

                Using pywebview's own dialog matters for two reasons the page cannot
                fix itself: the dialog is parented to the app window (so it cannot
                open *behind* it) and it is DPI-aware (a shelled-out WinForms dialog
                renders blurry on a HiDPI screen).
                """
                return host.pick(kind, initial)

            def reveal(self, target: str) -> bool:
                return winapi.open_in_explorer(target)

        return Api()

    def pick(self, kind: str = "files", initial: str = "") -> Dict[str, Any]:
        """Show a native dialog through pywebview."""
        if self.window is None:
            return {"ok": False, "cancelled": False, "error": "窗口未就绪"}
        webview = _import_webview()
        # pywebview 6 renamed the dialog constants: the module-level FOLDER_DIALOG /
        # OPEN_DIALOG still work but emit a DeprecationWarning. Prefer FileDialog.
        dialog = getattr(webview, "FileDialog", None)
        folder_id = getattr(dialog, "FOLDER", None) if dialog else None
        open_id = getattr(dialog, "OPEN", None) if dialog else None
        if folder_id is None:
            folder_id = getattr(webview, "FOLDER_DIALOG", 20)
        if open_id is None:
            open_id = getattr(webview, "OPEN_DIALOG", 10)
        try:
            if kind == "folder":
                result = self.window.create_file_dialog(folder_id, directory=initial or "")
                if not result:
                    return {"ok": True, "cancelled": True, "path": ""}
                # FOLDER_DIALOG returns a tuple/list of paths in some versions.
                chosen = result[0] if isinstance(result, (list, tuple)) else result
                return {"ok": True, "cancelled": False, "path": str(chosen)}
            result = self.window.create_file_dialog(
                open_id, allow_multiple=True, directory=initial or "")
            if not result:
                return {"ok": True, "cancelled": True, "paths": []}
            paths = [str(p) for p in (result if isinstance(result, (list, tuple)) else [result])]
            return {"ok": True, "cancelled": False, "paths": paths}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "cancelled": False,
                    "error": "无法打开对话框: {}".format(exc)}

    def _on_loaded(self) -> None:
        """Wire up file drag-and-drop -- the only way that actually works.

        pywebview does NOT expose a window-level drop event (window.events only has
        closed/loaded/shown/... ). File drops are enabled by registering a `drop`
        listener on a DOM element: that increments its internal `_dnd_state
        ['num_listeners']`, which tells the native host to accept files, and only
        then does the host fill in `pywebviewFullPath` on each dropped File. With no
        listener the drop is simply ignored -- "drag does nothing, no error", which
        is exactly the symptom reported.

        So we register the listener on the document body from the Python side and
        let the handler run here, where the real paths land.
        """
        if self.window is None:
            return
        try:
            self.window.evaluate_js(
                "window.__vcsDesktopShell = true;"
                "if (window.__vcsOnDesktopShell) window.__vcsOnDesktopShell();")
        except Exception:  # noqa: BLE001
            pass

        def on_drop(event: Any) -> None:
            paths = self.extract_paths(event)
            if paths:
                self._dropped.extend(paths)
                if self.window is not None:
                    try:
                        self.window.evaluate_js(
                            "window.__vcsDrainDropped && window.__vcsDrainDropped();")
                    except Exception:  # noqa: BLE001
                        pass

        # Registering on <body> is what switches the native file-drop support on.
        try:
            self.window.dom.body.events.drop += on_drop
            self.bound_drop_events = ["body.drop"]
        except Exception as exc:  # noqa: BLE001
            self.bound_drop_events = []
            print("  [!] 未能注册拖拽监听: {}".format(exc))
            print("      拖拽可能不可用；请用「选择文件 / 选择文件夹」或收件箱。")

    # -------------------------------------------------------------------- run

    def run(self) -> int:
        if not available():
            _notify_error("无法启动桌面窗口", INSTALL_HINT)
            print(INSTALL_HINT)
            return 3
        webview = _import_webview()

        print("  启动后端…")
        if not self.start_backend():
            reason = self.start_error or "后端启动失败"
            print("  [X] {}".format(reason))
            # Under pythonw.exe there is NO console: a printed error is invisible and the
            # user just sees "double-click does nothing" (reported from real use, with the
            # evidence sitting in app/logs/desktop.log). Anything that stops the window
            # from opening must therefore also be SAID out loud.
            _notify_error("视频压缩套件：无法启动", reason)
            return 2
        print("  [OK] 后端就绪 {}".format(self.base_url))

        self.window = webview.create_window(
            WINDOW_TITLE,
            self.base_url,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=(1000, 700),
            js_api=self._js_api(),
        )
        # Drag-drop is registered in _on_loaded (it needs window.dom, which only
        # exists once the window is live). There is no window-level drop event.
        self.window.events.loaded += self._on_loaded
        self.window.events.shown += lambda: print('  原生窗口已显示（拖拽文件到窗口即可加入）')

        # gui='edgechromium' is REQUIRED for real drag-drop paths on Windows:
        # pywebviewFullPath only exists on the edgechromium/winui3 backends
        # (mshtml is deprecated and CEF does not implement it). Do not switch this
        # to another renderer "for compatibility" -- the path support disappears.
        options = {}
        if runtime.frozen():
            options['storage_path'] = str(runtime.data_root() / 'webview')
        webview.start(gui="edgechromium", debug=False, **options)
        return 0


def _notify_error(title: str, message: str) -> None:
    """Show a message box -- the only way to be heard when running without a console.

    `pythonw.exe` has no console, so every `print()` in this file is invisible to the user
    (it goes to `app/logs/desktop.log`). A start-up failure must therefore be SHOWN, or the
    symptom is "I double-click and nothing happens", which is exactly what was reported.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        # MB_OK | MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST
        ctypes.windll.user32.MessageBoxW(None, str(message), str(title),
                                         0x00000000 | 0x00000010 | 0x00010000 | 0x00040000)
    except Exception:  # noqa: BLE001 - never let the notification kill the failure path
        pass


def next_free_port(start: int, tries: int = 12) -> Optional[int]:
    """First bindable port at or after `start`. Returns None when none is free.

    Binding is the only honest test: a port can be free to connect but reserved, and
    Windows reports that as `WinError 10013` (which is what the log showed).
    """
    import socket
    for port in range(int(start), int(start) + max(1, int(tries))):
        if port > 65535:
            break
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((HOST, port))
        except OSError:
            continue
        finally:
            probe.close()
        return port
    return None


def _attach_log_when_windowless() -> None:
    """Redirect stdout/stderr to a log file when running under pythonw.exe.

    pythonw.exe gives the process NO console, so sys.stdout/sys.stderr can be None
    or a dead handle. Any print() then raises and kills the process instantly -- a
    silent "double-click does nothing", which is the worst possible failure. Called
    as the very first thing in main(), before anything prints.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir = runtime.data_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = open(log_dir / "desktop.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = handle
        if sys.stderr is None:
            sys.stderr = handle
    except Exception:  # noqa: BLE001 - never let logging setup kill startup
        try:
            devnull = open(os.devnull, "w")
            sys.stdout = sys.stdout or devnull
            sys.stderr = sys.stderr or devnull
        except Exception:  # noqa: BLE001
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _attach_log_when_windowless()
    argv = list(sys.argv[1:] if argv is None else argv)
    port = DEFAULT_PORT
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (IndexError, ValueError):
            pass
    if "--check" in argv:
        ok = available()
        print("pywebview available:", ok)
        if ok:
            print("pywebview version :", version())
        else:
            print(INSTALL_HINT)
        return 0 if ok else 3
    return DesktopHost(port=port).run()


if __name__ == "__main__":
    raise SystemExit(main())
