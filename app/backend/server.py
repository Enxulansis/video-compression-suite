"""HTTP layer: JSON API, SSE progress stream and static file serving.

Zero external dependencies -- everything here is the Python standard library
(http.server + socketserver + threading), so the app runs on a bare Python
install with no pip step at all.
"""

from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import queue as queue_mod
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.caps import get_caps
from core.jobs import get_manager
from core import jobs as jobs_mod
from core.planner import Overrides, PlanError, get_profile, load_profiles, make_plan
from core.probe import probe
from core import inbox as inbox_mod
from core import history as history_mod
from core import paths, settings, sizing, space
from core import crop as crop_mod
from core import artifacts
from core import frames as frames_mod
from core import logs as logs_mod
from core import quality
from core import target as target_mod
from core import winapi
from core import work
from core import runtime
from core.repair import (available_strategies, build_repair_plan, diagnose,
                         measure_compress_cost, measure_repair_cost,
                         pick_default_strategy)
from core.verifier import keyframe_times_retry, _max_gap, verify

WEB_DIR = runtime.resource_root() / "web"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

#: Ids come from `uuid.uuid4().hex[:12]`, but the history index lists whatever
#: directory exists under store/jobs -- so the rule here must accept anything the
#: index can legitimately surface, or a user sees a record they can never delete.
#: Safety comes from shape, not from hex-ness: a single path component, no dots and
#: no separators, so it can never traverse.  (`history._drop_record` independently
#: re-checks that the resolved directory is a direct child of JOBS_DIR.)
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
#: The same shape without anchors, for embedding in a route regex. The job routes used
#: to hardcode `[0-9a-f]+`, so a record whose id was not hexadecimal (the history index
#: legitimately holds directory names like `probe00000002`) showed up in /api/history
#: yet could not be cancelled, retried, or have its log opened -- visible forever,
#: impossible to clean up, which is exactly what the lax `_JOB_ID_RE` was widened for.
_JOB_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{3,63}"

APP_VERSION = "0.7.1"

#: Upper bound for a single extracted frame. The comparison view never asks for more
#: than a couple of thousand pixels; the ceiling exists so one request cannot make
#: ffmpeg allocate an arbitrarily large frame.
MAX_FRAME_EDGE = 4096
#: Largest request body accepted. Every endpoint here takes a small JSON object; the
#: cap turns "declare a huge Content-Length and never send it" from an unbounded
#: thread-block into an immediate 413.
MAX_BODY_BYTES = 8 * 1024 * 1024
#: Seconds a handler may sit idle before the connection is dropped. A client that
#: announces a body and never sends it would otherwise hold its thread forever.
CLIENT_TIMEOUT_S = 30


def asset_signature() -> str:
    """Fingerprint of the served front-end, computed from file mtime+size.

    Used for two things: cache-busting the asset URLs, and letting the launcher
    detect that the running process is older than the code on disk.
    """
    parts = []
    for name in ("index.html", "app.js", "style.css"):
        path = WEB_DIR / name
        try:
            stat = path.stat()
            parts.append("{}:{}:{}".format(name, int(stat.st_mtime), stat.st_size))
        except OSError:
            parts.append("{}:missing".format(name))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


ASSET_SIG = asset_signature()
SERVER_STARTED = time.time()

# Drop-in folder: anything copied in here is picked up automatically. This is the
# path-free way to add files, so it is created next to the app.
INBOX_DIR = runtime.data_root() / "收件箱"


def _inbox_notify(new_paths: List[str]) -> None:
    """Called by the watcher for each batch of newly settled files."""
    manager = get_manager()
    manager.broadcast({"event": "inbox", "paths": new_paths})


_INBOX = inbox_mod.Inbox(str(INBOX_DIR), _inbox_notify)

# Native dialog results, keyed by a token the page generates.
#
# The dialog MUST NOT run inside the HTTP request thread: if the window ends up
# behind something (or the user simply walks away) the request hangs forever and
# the page sits on a spinner -- observed during testing. We run it in a worker
# thread and hand the answer back through this slot + an SSE event.
_PICK_RESULTS: Dict[str, Dict[str, Any]] = {}
_PICK_LOCK = threading.Lock()
PICK_TTL = 600.0


def _store_pick(token: str, payload: Dict[str, Any]) -> None:
    now = time.time()
    with _PICK_LOCK:
        _PICK_RESULTS[token] = {"at": now, "payload": payload}
        # drop stale entries so this dict cannot grow forever
        for key in [k for k, v in _PICK_RESULTS.items() if now - v["at"] > PICK_TTL]:
            _PICK_RESULTS.pop(key, None)
    try:
        get_manager().broadcast({"event": "picked", "token": token, "payload": payload})
    except Exception:  # noqa: BLE001
        pass


def _start_picker(kind: str, token: str, initial_dir: str = "") -> None:
    def worker() -> None:
        try:
            if kind == "folder":
                result = winapi.pick_folder(initial_dir)
            else:
                result = winapi.pick_files(initial_dir)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "cancelled": False,
                      "error": "选择对话框失败: {}".format(exc)}
        _store_pick(token, result)

    threading.Thread(target=worker, daemon=True).start()



def backend_mtime() -> float:
    """Newest mtime across the backend sources -- i.e. when the code last changed."""
    if runtime.frozen():
        return Path(sys.executable).stat().st_mtime
    newest = 0.0
    backend_dir = Path(__file__).resolve().parent
    for pattern in ("*.py", "core/*.py", "profiles/*.json"):
        for path in backend_dir.glob(pattern):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                pass
    return newest

# Only files under these roots may be read/browsed from the API.
def _allowed_roots() -> List[Path]:
    roots: List[Path] = []
    env = os.environ.get("VCS_ALLOWED_ROOTS", "")
    for chunk in env.split(os.pathsep):
        chunk = chunk.strip()
        if chunk:
            roots.append(Path(chunk).resolve())
    if runtime.frozen() and os.name == 'nt':
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        drives = [chr(65 + n) + ':\\' for n in range(26) if mask & (1 << n)]
    else:
        drives = ("D:\\", "E:\\", "F:\\")
    for drive in drives:
        candidate = Path(drive)
        if candidate.exists():
            roots.append(candidate)
    roots.append(Path.home().resolve())
    return roots


_ROOTS = _allowed_roots()


def _is_allowed(path: str) -> bool:
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for root in _ROOTS:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


#: Top-level directories the app will not WRITE into, even when the drive itself is an
#: allowed root. The read whitelist is deliberately broad ("your videos are on D:/E:/F:"),
#: but "we may read from this drive" is not "we may drop new files into the System32
#: directory or the Startup folder". A media file landing in Startup is a persistence
#: primitive, and a wrong `name_template` should never be able to reach one.
_WRITE_DENY_FIRST = {
    "windows", "program files", "program files (x86)", "$recycle.bin",
    "system volume information", "recovery", "perflogs",
}


def _is_allowed_write_dir(path: str) -> bool:
    """May the app create/write files under this directory?

    Applied to the RESOLVED path, so `E:\\out\\..\\..\\Windows\\Temp` is judged where it
    actually lands rather than where it was spelled.
    """
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    if not _is_allowed(str(resolved)):
        return False
    parts = [p.lower() for p in resolved.parts if p not in ("\\", "/")]
    # parts[0] is the drive ("e:\\"); the first real component is parts[1].
    if len(parts) > 1 and parts[1] in _WRITE_DENY_FIRST:
        return False
    lowered = str(resolved).lower()
    if "start menu\\programs\\startup" in lowered or "start menu/programs/startup" in lowered:
        return False
    return True


def require_writable_dir(path: str, what: str = "输出目录") -> str:
    """Validate a user-supplied output directory; raise ApiError(400) with the reason."""
    text = str(path or "").strip()
    if not text:
        raise ApiError(400, "{}不能为空".format(what))
    if not _is_allowed_write_dir(text):
        raise ApiError(400, "{}不在允许范围内（或位于系统目录中）：{}".format(what, text))
    return text


def require_confirm(body: Dict[str, Any], what: str, detail: str = "") -> None:
    """Destructive endpoints need an explicit, per-request confirmation flag.

    The Origin/Host check stops *other sites*, but it happily trusts any other page on
    this machine (any port on 127.0.0.1/localhost is an allowed origin). So a stray
    local script -- or a UI bug -- could `POST /api/history/clear` and wipe the archive
    with no evidence that a human ever agreed. `confirm: true` is not a security
    boundary; it is a deliberate-act marker, which is exactly what these calls lacked.
    """
    if body.get("confirm") is not True:
        raise ApiError(400, "该操作不可撤销（{}）{}；请在请求中显式带 confirm: true".format(
            what, detail))


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoCompressSuite/0.1"
    protocol_version = "HTTP/1.1"
    # Idle-socket timeout. A client that announces Content-Length and then sends
    # nothing used to hold its thread forever (measured: a declared 2 GB body blocked
    # with no response and no release); `SocketServer` closes the connection when a
    # read times out.
    timeout = CLIENT_TIMEOUT_S

    # Per-request body memo. A class-level default keeps `_read_body` safe even if it
    # is somehow reached before `_dispatch` runs `_reset_body`; the real reset happens
    # per request because one Handler instance serves every keep-alive request on a
    # connection (see _read_body).
    _body_cache: Optional[bytes] = None

    # -------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if os.environ.get("VCS_VERBOSE"):
            sys.stderr.write("[http] %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        if status < 400:
            work.check()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def _read_body(self) -> bytes:
        """Read this request's body ONCE and remember it for the rest of the request.

        Two separate hazards, both caused by HTTP/1.1 keep-alive reusing one Handler
        instance for every request on a connection:

        1. Every handler must consume the body even when it ignores it. A body left
           in the socket is parsed as the start of the NEXT request, so the following
           request line becomes `{}GET /api/jobs` and the server answers
           501 "Unsupported method". Reading centrally in `_dispatch` fixes that.
        2. The memo must NOT outlive the request. `_body_cache` used to be cleared
           only when the Handler was constructed, i.e. once per connection, so the
           second request on a reused connection was handed the FIRST request's body
           while its own body stayed in the socket. Symptom: a settings POST appeared
           to succeed but changed nothing, and the request after it got 501 --
           intermittent, dependent on client connection pooling. Hence the reset in
           `_dispatch` via `_reset_body`.
        """
        if self._body_cache is not None:
            return self._body_cache
        # Chunked bodies were silently read as EMPTY (no branch for Transfer-Encoding),
        # which both answered 400 "missing parameter" to a request that did send one and
        # left the chunk bytes in the socket to be parsed as the next request line. This
        # server does not implement chunked decoding, and pretending otherwise is worse
        # than refusing, so say so with the status code that means exactly that.
        encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if encoding and encoding != "identity":
            raise ApiError(411, "不支持分块请求体（Transfer-Encoding: {}）；请带 Content-Length".format(
                self.headers.get("Transfer-Encoding")))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            raise ApiError(400, "Content-Length 无效")
        if length < 0:
            raise ApiError(400, "Content-Length 无效")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "请求体过大（上限 {} MB）".format(MAX_BODY_BYTES // (1024 * 1024)))
        raw = self.rfile.read(length) if length > 0 else b""
        self._body_cache = raw
        return raw

    def _reset_body(self) -> None:
        """Forget the previous request's body (called once per request, see above)."""
        self._body_cache = None

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "请求体不是合法 JSON")

    @staticmethod
    def _client_overrides(manager: Any, body: Dict[str, Any]) -> Dict[str, Any]:
        """Overrides from a request body, with unknown keys rejected.

        `_make_overrides` drops keys it does not recognise, so a typo (`max_edge` for
        `max_long_edge`) was accepted, reported success, and produced a command with no
        scaling at all -- the page then showed one thing while the encoder did another.
        Rejecting the request is the only way the user finds out.
        """
        raw = body.get("overrides")
        if raw is not None and not isinstance(raw, dict):
            raise ApiError(400, "overrides 必须是对象")
        overrides = dict(raw or {})
        unknown = manager.unknown_override_keys(overrides)
        if unknown:
            # Say WHICH kind of wrong it is: a key only the server may fill is a
            # different mistake from a misspelt one, and the fix is different too.
            internal = [k for k in unknown if k in jobs_mod.INTERNAL_OVERRIDE_KEYS]
            if internal:
                raise ApiError(400, "这些参数由服务端测量后填写，不接受客户端指定：{}".format(
                    "、".join(internal)))
            raise ApiError(400, "不认识的参数：{}".format("、".join(unknown)))
        # Directory VALUES were never checked -- only key names. That left the read
        # whitelist with a hole on the write side: `{"out_dir": "C:\\Windows\\Temp"}`
        # was accepted and the job created the directory. Same reason the location
        # values are checked here rather than trusted downstream.
        for key, label in (("out_dir", "输出目录"), ("default_out_dir", "默认输出目录")):
            value = overrides.get(key)
            if value not in (None, ""):
                require_writable_dir(str(value), label)
        location = overrides.get("location")
        if location not in (None, "", "beside", "default", "custom"):
            raise ApiError(400, "不认识的保存位置：{}".format(location))
        # Type-check the VALUES too, and answer 400 rather than letting a string where a
        # number belongs become a 500 inside the planner (or, worse, reach the page and
        # be interpolated into an HTML attribute).
        try:
            return manager.coerce_overrides(overrides)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, str(exc))

    def _check_origin(self) -> None:
        """Block drive-by requests from other local pages (DNS-rebinding / CSRF)."""
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        if host and host not in ALLOWED_HOSTS and host not in ("0.0.0.0",):
            raise ApiError(403, "非法 Host: {}".format(host))
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlparse(origin)
            if parsed.hostname not in ALLOWED_HOSTS:
                raise ApiError(403, "跨站请求被拒绝")

    def _check_same_site(self) -> None:
        """Guard for RESOURCE endpoints that pages request without an Origin header.

        `Origin` is only sent for CORS/POST-style requests, so a cross-site
        `<img src="http://127.0.0.1:8787/media?...">` or `<video>` arrives with no
        Origin at all and sailed past `_check_origin` (measured: 200 for /media with a
        foreign Origin, while /api/jobs correctly answered 403). `Sec-Fetch-Site` IS
        sent for every subresource request by every current browser, and `same-site`
        is not good enough here: the trust boundary is "this exact origin", not "some
        page on 127.0.0.1" -- 127.0.0.1:4956 and 127.0.0.1:8787 are the same site.

        Clients that do not send the header (curl, the test harness, a local program)
        fall back to the ordinary Origin/Host check. That is the right trade-off: the
        thing being defended against is a *web page*, and a local program can already
        read these files directly.
        """
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            raise ApiError(403, "跨站资源请求被拒绝")
        self._check_origin()

    # ------------------------------------------------------------ HTTP verbs

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        # Used by the per-job settings edit (F12a). Without this the base handler
        # answers 501 "Unsupported method" and the edit silently never happens.
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        # Without this, BaseHTTPRequestHandler answers 501 in text/html, which breaks
        # the "every error is JSON" contract the front end and the tests rely on. There
        # is deliberately no CORS preflight support: this server is same-origin only.
        self._dispatch("OPTIONS")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        # Drain the body for EVERY request up front, including routes that ignore it.
        # Otherwise the unread bytes are parsed as the next request on a keep-alive
        # connection (see _read_body).
        self._reset_body()
        try:
            self._read_body()
        except OSError:
            pass
        except ApiError as exc:
            # A body we refuse to read stays in the socket, so this connection can no
            # longer be reused for another request -- say so instead of letting the
            # leftover bytes become the next request line.
            self.close_connection = True
            self._error(exc.status, exc.message)
            return
        try:
            if path == "/api/events":
                self._check_origin()
                self._sse()
                return
            if path.startswith("/api/"):
                self._check_origin()
                if method not in ("GET", "HEAD", "POST", "PATCH", "DELETE"):
                    # Answer 405 in JSON (rather than letting the route table fall
                    # through to a confusing 404 "未知接口") and name what is allowed.
                    self._error(405, "不支持的方法: {}（允许 GET/HEAD/POST/PATCH/DELETE）".format(method))
                    return
                auxiliary = (path in ("/api/preview", "/api/measure", "/api/target", "/api/crop",
                                      "/api/probe", "/api/repair/diagnose", "/api/repair/measure",
                                      "/api/repair/plan", "/api/plan", "/api/estimate/batch",
                                      "/api/group", "/api/budget", "/api/verify", "/api/preflight")
                             or path.startswith("/api/quality/") or path.startswith("/api/frame"))
                if method == 'POST' and path in ('/api/plan', '/api/estimate/batch', '/api/group', '/api/budget'):
                    body = self._read_json()
                    manager = get_manager()
                    selected = [body.get('path')] if path == '/api/plan' else body.get('paths')
                    if (isinstance(selected, list) and selected and len(selected) <= 200 and
                        all(isinstance(p, str) and _is_allowed(p) and manager.cached_probe(p) is not None
                            for p in selected)):
                        auxiliary = False  # cached arithmetic launches no media child
                if auxiliary:
                    frame = path.startswith('/api/frame')
                    with work.request(get_manager(), work.FRAME_SECONDS if frame else work.REQUEST_SECONDS, wait=frame):
                        self._route_api(method, path, query)
                elif method == 'POST' and path in ('/api/plan', '/api/estimate/batch', '/api/group', '/api/budget', '/api/jobs/batch'):
                    # Keep deadlines and child admission if a file changes between
                    # the cache check and planning, without locking out pure arithmetic.
                    with work.background(get_manager()):
                        self._route_api(method, path, query)
                else:
                    self._route_api(method, path, query)
                return
            self._serve_static(path)
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except (work.Busy, work.Expired) as exc:
            self._error(exc.status, str(exc))
        except PlanError as exc:
            self._error(400, str(exc))
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - never let one request kill the server
            self._error(500, "服务器内部错误: {}".format(exc))

    # ----------------------------------------------------------------- api

    def _route_api(self, method: str, path: str, query: Dict[str, List[str]]) -> None:
        if method in ('GET', 'HEAD') and path == '/api/health':
            # Window readiness is independent of encoder probes and queue restoration.
            self._json({'ok':True, 'time':time.time(), 'service':'video-compression-suite',
                        'pid':os.getpid(), 'instance':runtime.instance_id()})
            return

        if method in ("GET", "HEAD") and path == "/api/caps":
            self._json({"ok": True, "caps": get_caps().to_dict()})
            return

        if method in ("GET", "HEAD") and path == "/api/profiles":
            caps = get_caps()
            profiles = []
            for prof in load_profiles():
                item = dict(prof)
                enc = prof.get("encoder", "")
                fb = prof.get("fallback_encoder")
                item["available"] = (enc == "copy") or caps.has_encoder(enc) or bool(fb and caps.has_encoder(fb))
                item["effective_encoder"] = enc if caps.has_encoder(enc) else (fb if fb else enc)
                profiles.append(item)
            self._json({"ok": True, "profiles": profiles,
                        "simple_mode": True,
                        "queue_handoff": True,
                        "default": next((p["id"] for p in profiles if p.get("default")), "P3")})
            return

        manager = get_manager()
        if method in ("GET", "HEAD") and path == "/api/memory":
            self._json(dict(ok=True, **manager.memory_snapshot()))
            return

        if method in ("GET", "HEAD") and path == "/api/jobs":
            self._json({"ok": True, "jobs": manager.list_jobs(), "stats": manager.stats(),
                        # The completion policy travels with the queue so the panel can
                        # show whether a shutdown is armed without a second request.
                        "on_success": manager.on_success_policy(),
                        "memory": manager.memory_policy(),
                        "restore": manager.restore_state(),
                        "power": manager.power_state()})
            return

        if method == "POST" and path == "/api/on-success":
            body = self._read_json()
            result = manager.set_on_success(body.get("on_success") or {})
            if not result.get("ok"):
                raise ApiError(400, result.get("error") or "无法设置完成动作")
            self._json(result)
            return

        if method == "POST" and path == "/api/power/cancel":
            # Cancelling is always allowed and never fails the request on "nothing to
            # cancel": the user pressing it expects the machine to stay on, and that is
            # true either way.
            self._json(manager.cancel_power())
            return

        if method in ("GET", "HEAD") and path == "/api/version":
            # The launcher compares this against the code on disk: a running
            # process older than the files means "restart me", which is what
            # makes double-clicking start.bat always serve the latest build.
            # `pid` + `active_jobs` exist so the desktop launcher can apply the same
            # policy without ever killing an encode that is still in flight.
            # The environment fields (规范化) are what the 设置 / 关于 panel shows, so
            # "which build am I running, and with which FFmpeg" stops requiring a
            # terminal -- and a mismatch between the page and the server is visible.
            caps = get_caps()
            self._json({
                "ok": True,
                "version": APP_VERSION,
                "asset_signature": ASSET_SIG,
                "backend_mtime": backend_mtime(),
                "started": SERVER_STARTED,
                "uptime": max(0.0, time.time() - SERVER_STARTED),
                "pid": os.getpid(),
                "active_jobs": manager.active_count(),
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "ffmpeg": {
                    "path": caps.ffmpeg,
                    "version": caps.version,
                    "available": caps.available(),
                },
                "store_dir": str(jobs_mod.STORE_DIR),
                "inbox_dir": str(INBOX_DIR),
                "limits": manager.limits(),
            })
            return

        if method in ("GET", "HEAD") and path == "/api/capabilities":
            # What this build can do beyond a plain web page. The UI uses it to
            # show the native-dialog and inbox entry points, and to display the
            # measured hardware sizing behind the concurrency defaults.
            self._json({
                "ok": True,
                "native_dialogs": winapi.available(),
                "inbox_dir": str(INBOX_DIR),
                "platform": sys.platform,
                "hardware": sizing.recommend(),
                "limits": manager.limits(),
            })
            return

        if method == "POST" and path in ("/api/pick/files", "/api/pick/folder"):
            body = self._read_json()
            kind = "folder" if path.endswith("folder") else "files"
            token = str(body.get("token") or "")
            if not token:
                token = "t{}".format(int(time.time() * 1000))
            # Fire the dialog off-thread and answer immediately; the result arrives
            # on the SSE stream as a "picked" event carrying this token.
            _start_picker(kind, token, str(body.get("initial_dir") or ""))
            self._json({"ok": True, "async": True, "token": token})
            return

        if method == "POST" and path == "/api/pick/result":
            body = self._read_json()
            token = str(body.get("token") or "")
            with _PICK_LOCK:
                entry = _PICK_RESULTS.pop(token, None)
            if entry is None:
                self._json({"ok": True, "pending": True})
            else:
                self._json({"ok": True, "pending": False, "result": entry["payload"]})
            return

        if method == "POST" and path == "/api/reveal":
            body = self._read_json()
            target = str(body.get("path") or "")
            if not target or not _is_allowed(target):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            self._json({"ok": winapi.open_in_explorer(target)})
            return

        if method in ("GET", "HEAD") and path == "/api/inbox":
            self._json({"ok": True, "dir": str(INBOX_DIR)})
            return

        if method == "POST" and path == "/api/inbox/scan":
            # Explicitly pick up whatever is in the folder right now, including
            # files dropped while the app was closed (the watcher normally ignores
            # a pre-existing backlog to avoid re-queuing it on every launch).
            found = _INBOX.scan_once(announce_existing=True)
            self._json({"ok": True, "found": found, "dir": str(INBOX_DIR)})
            return

        if method == "POST" and path == "/api/inbox/clear":
            # Remove the files that were copied into the inbox (they are copies made
            # for convenience; the originals the user dragged from are untouched).
            body = self._read_json()
            try:
                pending = [i for i in INBOX_DIR.iterdir()
                           if i.is_file() and not i.name.startswith(".")]
            except OSError:
                pending = []
            require_confirm(body, "清空收件箱会删掉其中的 {} 个文件".format(len(pending)))
            removed = 0
            try:
                for item in INBOX_DIR.iterdir():
                    if item.is_file() and not item.name.startswith("."):
                        try:
                            item.unlink()
                            removed += 1
                        except OSError:
                            pass
            except OSError:
                pass
            self._json({"ok": True, "removed": removed})
            return

        if method in ("GET", "HEAD") and path == "/api/roots":
            self._json({"ok": True, "roots": [str(r) for r in _ROOTS]})
            return

        if method == "POST" and path == "/api/preview":
            # Encode a short sample clip so the user can eyeball the result -- and
            # drag the seek bar -- before committing a multi-hour batch.
            body = self._read_json()
            src = body.get("path") or ""
            if not src or not _is_allowed(src):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            if not os.path.isfile(src):
                raise ApiError(404, "文件不存在: {}".format(src))
            seconds = max(1, min(15, int(body.get("seconds") or 6)))
            info = probe(src)
            if not info.ok:
                raise ApiError(400, info.error or "无法探测该文件")
            work.note_media(info)
            profile = get_profile(body.get("profile") or "P3")
            ov = manager._make_overrides(self._client_overrides(manager, body))

            plan = make_plan(info, profile, ov)
            argv = list(plan.argv)

            # Stable, predictable preview path (no collision suffixing, and it must
            # never be mistaken for a real job output).
            preview_out = str(Path(plan.out_path).with_name(
                "{}_preview_{}s.{}".format(
                    Path(plan.out_path).stem, seconds, Path(plan.out_path).suffix.lstrip("."))))

            # `-progress pipe:1 -nostats <out>` closes the planner's argv, so the
            # output path is the last element.  Only that one gets rewritten.
            argv[-1] = preview_out
            assert argv.count(preview_out) == 1, "preview argv built incorrectly"

            # Take the sample from ~25% in, where the content is usually more
            # representative than the opening titles.  `-ss` must precede `-i`
            # (input seek) -- otherwise it is treated as an output option and the
            # whole file before the seek point still gets decoded.
            # NB: inserting `-ss` shifts every later index, so the output slot must
            # be recomputed *after* that insert, never cached beforehand.
            start = 0.0
            if info.duration > seconds:
                start = max(0.0, min(info.duration * 0.25, info.duration - seconds))
                input_at = argv.index("-i")
                argv[input_at:input_at] = ["-ss", "{:.2f}".format(start)]
                argv.insert(len(argv) - 1, "-t")
                argv.insert(len(argv) - 1, str(seconds))
            assert argv[-1] == preview_out, "preview output path must stay last"

            Path(preview_out).parent.mkdir(parents=True, exist_ok=True)
            try:
                os.remove(preview_out)
            except OSError:
                pass

            if os.environ.get("VCS_VERBOSE"):
                sys.stderr.write("[preview] " + " ".join(argv) + "\n")
            # The encode must finish before the keyframe scan: with +faststart the
            # moov atom only lands at the end, so scanning earlier fails outright
            # with "moov atom not found" (an empty result, not a blocking wait).
            proc = work.run(argv, timeout=1800, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
            stderr = proc.stderr
            if proc.returncode != 0 or not os.path.isfile(preview_out):
                raise ApiError(500, "预览生成失败: {}".format((stderr or "")[-400:]))

            keys, probe_err = keyframe_times_retry(preview_out)
            if os.environ.get("VCS_VERBOSE"):
                sys.stderr.write("[preview] keys={} probe_err={!r}\n".format(
                    len(keys), probe_err[:160]))

            gap = _max_gap(keys, float(seconds))
            preview_ok = bool(keys) and gap <= 2.35
            self._json({
                "ok": True,
                "path": preview_out,
                "url": "/media?path=" + urllib.parse.quote(preview_out),
                "start": round(start, 2),
                "seconds": seconds,
                "size_bytes": os.path.getsize(preview_out),
                "keyframes": len(keys),
                "max_keyframe_gap": round(gap, 3) if keys else None,
                "seek_ok": preview_ok,
                "command": " ".join(argv),
            })
            return

        if method == "POST" and path == "/api/probe":
            body = self._read_json()
            selected = body.get("paths") or ([body["path"]] if body.get("path") else [])
            if not isinstance(selected, list) or not selected:
                raise ApiError(400, "缺少 paths")
            check_seek = bool(body.get("check_seek", True))
            wanted = [str(p) for p in selected[:500]]

            # Skip files we are already tracking: re-adding a huge set should not
            # re-scan everything. Only completed scans are reused -- an in-flight
            # one must keep reporting "checking" rather than a stale answer.
            known = dict(manager.seek_results)
            in_flight = set(manager._seek_scanning)

            fresh = []
            results: List[Optional[Dict[str, Any]]] = [None] * len(wanted)
            for index, p in enumerate(wanted):
                if not _is_allowed(p):
                    results[index] = {"path": p, "ok": False, "error": "路径不在允许范围内"}
                else:
                    cached = manager.cached_probe(p)
                    if cached is not None:
                        results[index] = cached.to_dict()
                    else:
                        fresh.append((index, p))

            # ffprobe calls are IO-bound: run them concurrently instead of one by
            # one, which is what made adding a big folder feel slow.
            probed: Dict[int, Any] = {}
            if fresh:
                with ThreadPoolExecutor(max_workers=min(2, max(1, len(fresh)))) as pool:
                    futures = {pool.submit(copy_context().run, probe, p): (i, p) for i, p in fresh}
                    for future in as_completed(futures):
                        index, p = futures[future]
                        try:
                            info = future.result()
                            probed[index] = info
                            results[index] = info.to_dict()
                        except (work.Busy, work.Expired):
                            raise
                        except Exception as exc:  # noqa: BLE001
                            results[index] = {"path": p, "ok": False,
                                              "error": "探测失败: {}".format(exc)}
            # Keep the metadata for the batch endpoints (totals / grouping / budget):
            # they are computed from these rather than re-probing the whole selection.
            for info in probed.values():
                manager.remember_probe(info)

            # Seekability needs a full keyframe scan, which is far slower than the
            # metadata probe. Do it in the background and stream the answer over
            # SSE so the file list appears instantly.
            scan: List[str] = []
            for item in results:
                if not item or not item.get("ok"):
                    continue
                cached = known.get(item["path"])
                if cached and item["path"] not in in_flight:
                    item["seek"] = cached
                    continue
                if not check_seek:
                    item['seek'] = {'verdict': 'unmeasured', 'summary': '尚未扫描关键帧'}
                    continue
                item["seek"] = {"verdict": "checking", "seek_ok": True,
                                "summary": "正在检测关键帧…", "reasons": [],
                                "keyframes": 0, "max_keyframe_gap": 0}
                if check_seek:
                    scan.append(item["path"])
            if scan:
                manager.scan_seek_async(scan)

            self._json({"ok": True, "items": [r for r in results if r]})
            return

        # -- batch arithmetic (A1 totals / C3 grouping / C1 budget) --------------
        if method == "POST" and path in ("/api/estimate/batch", "/api/group", "/api/budget"):
            from core import batch as batch_mod

            body = self._read_json()
            selected = [str(p) for p in (body.get("paths") or [])]
            if not selected:
                raise ApiError(400, "缺少 paths")
            bad = [p for p in selected if not _is_allowed(p)]
            if bad:
                raise ApiError(400, "路径不在允许范围内：{}".format(bad[0]))
            profile = get_profile(body.get("profile") or "P3")
            ov = manager._make_overrides(self._client_overrides(manager, body))
            # Per-file / per-group settings: the batch panel drives those very features, so
            # a panel computed from the main profile alone would describe a batch that will
            # never be encoded. Unknown profile ids are refused rather than ignored.
            per_file: Dict[str, Dict[str, Any]] = {}
            raw_per_file = body.get("per_file")
            if raw_per_file is not None:
                if not isinstance(raw_per_file, dict):
                    raise ApiError(400, "per_file 必须是对象（路径 → 设置）")
                for raw_path, own in raw_per_file.items():
                    if not isinstance(own, dict):
                        raise ApiError(400, "per_file 的每一项都必须是小对象")
                    entry = dict(own)
                    wanted = entry.get("profile")
                    if wanted:
                        try:
                            get_profile(str(wanted))
                        except KeyError:
                            raise ApiError(400, "未知档位: {}".format(wanted))
                    per_file[str(raw_path)] = entry
            profiles_by_id = {p["id"]: p for p in load_profiles()}
            infos = manager.batch_probes(selected)
            usable = [i for i in infos if i.ok]
            if not usable:
                raise ApiError(400, "这些文件都无法探测：{}".format(
                    (infos[0].error if infos else "") or "未知原因"))

            if path == "/api/estimate/batch":
                self._json(batch_mod.estimate_batch(usable, profile, ov, per_file,
                                                    profiles_by_id))
                return
            if path == "/api/group":
                self._json(batch_mod.group_files(usable, load_profiles()))
                return
            try:
                total = int(body.get("total_bytes") or 0)
            except (TypeError, ValueError):
                raise ApiError(400, "total_bytes 必须是字节数")
            if total <= 0:
                raise ApiError(400, "缺少 total_bytes（总预算，单位字节）")
            self._json(batch_mod.allocate_budget(usable, total, profile, ov, per_file,
                                                 profiles_by_id))
            return

        if method == "POST" and path == "/api/target":
            # "Get this under 200 MB": measure a few CRF points with real sample
            # encodes, fit the size curve and solve it for the requested size.
            # No two-pass encode, so it works for NVENC and SVT-AV1 alike.
            body = self._read_json()
            src = body.get("path") or ""
            if not src or not _is_allowed(src):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            info = probe(src)
            if not info.ok:
                raise ApiError(400, info.error or "无法探测该文件")
            profile = get_profile(body.get("profile") or "P3")
            overrides = self._client_overrides(manager, body)
            # The shared default folder lives in settings, not in the page, so the
            # preview names the same destination the submit below would use.
            overrides.setdefault("default_out_dir", settings.default_out_dir())
            target_text = str(body.get("target") or "")
            # Ratio forms ('20%', '5x') need the source size to mean anything.
            target_bytes = target_mod.parse_target(target_text, info.size_bytes)
            if target_bytes <= 0:
                raise ApiError(400, "目标体积无法识别：{}。可用 200MB / 1.5GB / 20% / 5x".format(
                    target_mod.describe_target(target_text, info.size_bytes)))

            base_ov = manager._make_overrides(overrides)
            base_plan = make_plan(info, profile, base_ov, exists_probe=os.path.isfile)
            seconds = max(4, min(15, int(body.get("seconds") or 6)))

            def measure_one(crf: int) -> int:
                ov = manager._make_overrides(dict(overrides, crf=int(crf)))
                plan = make_plan(info, profile, ov, exists_probe=os.path.isfile)
                measured = measure_compress_cost(info, base_plan, sample_seconds=seconds,
                                                 plan_override=plan)
                if not measured.get("ok"):
                    raise RuntimeError(measured.get("error") or "测算失败")
                return int(measured["est_bytes"])

            solved = target_mod.solve_for_target(
                measure_one, info, info.video.height, target_bytes, base_plan.encoder,
                overhead_bytes=(target_mod.audio_bytes(info, base_plan.argv)
                                + target_mod.container_overhead_bytes(target_bytes)),
                audio_bytes_value=target_mod.audio_bytes(info, base_plan.argv),
                strict=bool(body.get("strict")))
            payload = solved.to_dict()
            payload["target_text"] = target_text
            payload["target_explained"] = target_mod.describe_target(target_text, info.size_bytes)

            # The other half of the trade: what will CRF N actually look like? One
            # sample encode plus one sampled VMAF -- a few seconds, and it turns
            # "CRF 34" into "VMAF 94 (很好)" before the user commits to a full run.
            if solved.ok and body.get("predict_quality", True):
                try:
                    sample_plan = make_plan(
                        info, profile,
                        manager._make_overrides(dict(overrides, crf=int(solved.crf))),
                        exists_probe=os.path.isfile)
                    predicted = quality.measure_sample_at(
                        src, sample_plan, int(solved.crf),
                        seconds=max(4, min(10, int(body.get("seconds") or 6))))
                    payload["quality"] = predicted.to_dict()
                except Exception as exc:  # noqa: BLE001 - a prediction must not fail the solve
                    payload["quality"] = {"ok": False, "error": str(exc)}
            if solved.ok:
                # Hand back the plan the user would actually run.
                chosen = manager._make_overrides(dict(overrides, crf=solved.crf))
                plan = make_plan(info, profile, chosen, exists_probe=os.path.isfile)
                payload["plan"] = {k: v for k, v in plan.to_dict().items() if k != "argv"}
                payload["plan"]["out_location"] = paths.describe(
                    src, str(overrides.get("location") or "beside"),
                    str(overrides.get("out_dir") or ""),
                    str(overrides.get("default_out_dir") or ""), kind="compress")
            self._json({"ok": True, "target": payload,
                        "source_bytes": info.size_bytes,
                        "target_bytes": target_bytes})
            return

        if method in ("GET", "HEAD") and path == "/api/frame":
            # One frame as JPEG, for the 1:1 comparison view (V2). Served as an image
            # so the browser can cache and re-use it, and so the UI can point an <img>
            # straight at a URL instead of round-tripping base64.
            # An <img> carries no Origin, so without this guard any page could drive
            # ffmpeg through this endpoint (the cross-site amplifier in the review).
            self._check_same_site()
            target = (query.get("path") or [""])[0]
            if not target or not _is_allowed(target):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            try:
                at = float((query.get("t") or ["0"])[0])
            except (TypeError, ValueError):
                raise ApiError(400, "时间点无效")

            common = None
            try:
                width = int((query.get("w") or ["0"])[0])
                height = int((query.get("h") or ["0"])[0])
                if width > 0 and height > 0:
                    # Uncapped, `?w=100000&h=100000` went straight into ffmpeg's scale
                    # filter -- one request could ask for an arbitrarily large frame.
                    common = (min(width, MAX_FRAME_EDGE), min(height, MAX_FRAME_EDGE))
            except (TypeError, ValueError):
                common = None

            crop = frames_mod.parse_crop((query.get("crop") or [""])[0])
            if crop is None and common:
                try:
                    zoom = float((query.get("zoom") or ["1"])[0])
                except (TypeError, ValueError):
                    zoom = 1.0
                crop = frames_mod.centered_crop(common[0], common[1], zoom)
            # NB: do not name this `quality` -- it would shadow the core.quality
            # module for this entire handler (a local name anywhere in a function
            # makes that name local everywhere in it), and `quality.analyze(...)`
            # further down would then raise UnboundLocalError at runtime.
            try:
                jpeg_quality = max(2, min(31, int((query.get("q") or ["3"])[0])))
            except (TypeError, ValueError):
                jpeg_quality = 3

            try:
                blob = frames_mod.extract(target, at, scale_to=common, crop=crop,
                                          quality=jpeg_quality)
            except frames_mod.FrameError as exc:
                raise ApiError(400, str(exc))
            # Routed through `_send`, which skips the body for HEAD. Writing it here
            # directly put 9 KB of JPEG into the socket after a HEAD response and
            # desynchronised the next request on that keep-alive connection
            # (measured: `BadStatusLine: 'ÿØÿà JFIF ...'`).
            self._send(HTTPStatus.OK, blob, "image/jpeg",
                       extra={"Cache-Control": "no-store"})
            return

        if method in ("GET", "HEAD") and path == "/api/frame/diff":
            # V6 locator: |源 − 成品| × gain, luma only. This is a MAP, not a verdict:
            # it amplifies noise and alignment error along with real damage, and there
            # is no authoritative gain. Two things are therefore non-negotiable here:
            # the common resolution is decided explicitly and reported, and the request
            # is REFUSED when the two sides cannot be paired frame by frame -- ffmpeg
            # would not fail on a frame-rate mismatch, it would quietly compare
            # different moments and hand back a confident-looking lie.
            # same guard as /api/frame: no Origin on an <img>, so a cross-site page
            # could otherwise use the diff endpoint as a CPU amplifier.
            self._check_same_site()
            src_path = (query.get("src") or [""])[0]
            out_path = (query.get("out") or [""])[0]
            for label, value in (("src", src_path), ("out", out_path)):
                if not value or not _is_allowed(value):
                    raise ApiError(400, "缺少或不在允许范围内的 {}".format(label))
            try:
                at = float((query.get("t") or ["0"])[0])
            except (TypeError, ValueError):
                raise ApiError(400, "时间点无效")

            info_a, info_b = probe(src_path), probe(out_path)
            if not info_a.ok:
                raise ApiError(400, info_a.error or "无法探测源文件")
            if not info_b.ok:
                raise ApiError(400, info_b.error or "无法探测成品文件")
            video_a, video_b = info_a.video, info_b.video
            if not video_a or not video_b:
                raise ApiError(400, "两侧都必须有视频流")

            problem = frames_mod.pairing_problem(
                {"fps": video_a.fps, "frames": info_a.frame_count,
                 "duration": info_a.duration or video_a.duration},
                {"fps": video_b.fps, "frames": info_b.frame_count,
                 "duration": info_b.duration or video_b.duration})
            if problem:
                self._json({"ok": False, "error": problem}, 409)
                return

            # Display sizes: the two inputs are autorotated by ffmpeg before the
            # blend, so the geometry that matters is the size the viewer sees.
            size = frames_mod.common_size(
                (video_a.display_width, video_a.display_height),
                (video_b.display_width, video_b.display_height))
            crop = frames_mod.parse_crop((query.get("crop") or [""])[0])
            if crop is None:
                try:
                    zoom = float((query.get("zoom") or ["1"])[0])
                except (TypeError, ValueError):
                    zoom = 1.0
                crop = frames_mod.centered_crop(size[0], size[1], zoom)
            gain = frames_mod.parse_gain((query.get("gain") or [""])[0])

            try:
                blob = frames_mod.difference(
                    src_path, out_path, at, size=size, crop=crop, gain=gain,
                    size_a=(video_a.display_width, video_a.display_height),
                    size_b=(video_b.display_width, video_b.display_height))
            except frames_mod.FrameError as exc:
                raise ApiError(400, str(exc))

            # The basis travels with the picture instead of being re-derived (and
            # possibly re-guessed) by the page. ASCII-only so it is safe in a header.
            self._send(HTTPStatus.OK, blob, "image/jpeg", {
                "X-VCS-Common": "{}x{}".format(size[0], size[1]),
                "X-VCS-Flags": frames_mod.DIFF_FLAGS,
                "X-VCS-Gain": str(gain),
                "X-VCS-Saturate": str(frames_mod.saturation_value(gain)),
                "X-VCS-Fps": "{:.3f}/{:.3f}".format(video_a.fps, video_b.fps),
                "X-VCS-Crop": "{}:{}:{}:{}".format(*crop) if crop else "full",
            })
            return

        if method == "POST" and path == "/api/quality/artifacts":
            # Blockiness / blur against the source (V5). Both filters are no-reference
            # and single-input, so this runs each side once -- at a COMMON resolution,
            # because these numbers are meaningless across different frame sizes.
            body = self._read_json()
            out_path = str(body.get("out_path") or "")
            src_path = str(body.get("src_path") or "")
            if not out_path or not _is_allowed(out_path):
                raise ApiError(400, "缺少或不在允许范围内的 out_path")
            if not src_path or not _is_allowed(src_path):
                raise ApiError(400, "缺少或不在允许范围内的 src_path")
            if not os.path.isfile(out_path):
                raise ApiError(400, "成品文件已不在原位置")
            if not os.path.isfile(src_path):
                raise ApiError(400, "源文件已不在原位置")
            try:
                seconds = max(3, min(20, int(body.get("seconds") or 6)))
            except (TypeError, ValueError):
                seconds = 6
            result = artifacts.compare(src_path, out_path, seconds=seconds)
            if not result.get("ok"):
                raise ApiError(400, result.get("error") or "测量失败")
            self._json({"ok": True, "artifacts": result})
            return

        if method == "POST" and path == "/api/quality/banding":
            # CAMBI banding scan of ONE file (V4). No reference needed, so this works
            # on any output -- including files compressed long ago whose source is
            # gone. It is also the check that catches what VMAF is nearly blind to:
            # measured here, a flat gradient at CRF 30-48 reads VMAF 96-97.5 while
            # CAMBI reports 14-22 (明显到严重色带).
            body = self._read_json()
            target = str(body.get("path") or "")
            if not target or not _is_allowed(target):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            if not os.path.isfile(target):
                raise ApiError(400, "文件已不在原位置")
            try:
                seconds = max(3, min(30, int(body.get("seconds") or 8)))
            except (TypeError, ValueError):
                seconds = 8
            result = quality.scan_banding(target, seconds=seconds,
                                          full=bool(body.get("full")))
            self._json({"ok": True, "banding": result.to_dict()})
            return

        if method == "POST" and path == "/api/quality/analyze":
            # Per-frame VMAF so the user can see WHERE the quality went (V1). This is
            # the slowest endpoint in the app (it decodes both sides), so `full` is
            # opt-in and the default walks only the sampled windows.
            body = self._read_json()
            out_path = str(body.get("out_path") or "")
            src_path = str(body.get("src_path") or "")
            if not out_path or not _is_allowed(out_path):
                raise ApiError(400, "缺少或不在允许范围内的 out_path")
            if not src_path or not _is_allowed(src_path):
                raise ApiError(400, "缺少或不在允许范围内的 src_path")
            if not os.path.isfile(out_path):
                raise ApiError(400, "成品文件已不在原位置")
            try:
                seconds = max(3, min(20, int(body.get("seconds") or 6)))
            except (TypeError, ValueError):
                seconds = 6
            result = quality.analyze(out_path, src_path, seconds=seconds,
                                     full=bool(body.get("full")))
            payload = result.to_dict()
            # Hand the UI everything it needs to build crop URLs without repeating the
            # basis arithmetic client-side.
            try:
                info = probe(out_path)
                if info.ok and info.video:
                    # BOTH sides of the pair must come from the same coordinate system.
                    # This used to report display_width with the STORED height, so a
                    # 640x360 phone clip with rotation 90 produced {width: 360,
                    # height: 360} -- a square -- and the 1:1 / diff view then asked for
                    # the wrong crop region on exactly the files that are hardest to
                    # eyeball. Same root cause as the earlier "portrait squashed" defect,
                    # which is why the display pair is now stated explicitly.
                    payload["frame"] = {
                        "width": info.video.display_width,
                        "height": info.video.display_height,
                        "coded_width": info.video.width,
                        "coded_height": info.video.height,
                        "rotation": info.video.rotation,
                        "fps": info.video.fps}
            except Exception:  # noqa: BLE001 - the analysis is still useful
                pass
            self._json({"ok": True, "analysis": payload})
            return

        if method == "POST" and path == "/api/crop":
            # Preview black-bar detection for one file (G2). The actual crop applied
            # per job is resolved in the manager, because bars differ per video.
            body = self._read_json()
            src = body.get("path") or ""
            if not src or not _is_allowed(src):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            info = probe(src)
            if not info.ok:
                raise ApiError(400, info.error or "无法探测该文件")
            result = crop_mod.detect_cached(src, info)
            # `crop` is expressed in DISPLAY coordinates (cropdetect runs on the
            # auto-rotated frames), so the geometry handed to the UI must be the display
            # one -- otherwise a portrait source gets a landscape crop box drawn on it.
            # The coded size rides along because it is what the encoder actually stores.
            self._json({"ok": True, "crop": result.to_dict(),
                        "source": {"width": info.video.display_width if info.video else 0,
                                   "height": info.video.display_height if info.video else 0,
                                   "coded_width": info.video.width if info.video else 0,
                                   "coded_height": info.video.height if info.video else 0,
                                   "rotation": info.video.rotation if info.video else 0}})
            return

        if method == "POST" and path == "/api/preflight":
            # Pre-encode warnings (G4): can we actually write this batch, and is there
            # room for it? Advisory -- the user decides, but nothing should start
            # silently into a full or unwritable volume.
            body = self._read_json()
            selected = body.get("paths") or ([body["path"]] if body.get("path") else [])
            if not isinstance(selected, list) or not selected:
                raise ApiError(400, "缺少 paths")
            location = str(body.get("location") or "beside")
            if location not in paths.LOCATIONS:
                raise ApiError(400, "未知的保存位置: {}".format(location))
            # This endpoint CREATES the directory it checks (`space.preflight` calls
            # mkdir), so an unchecked `out_dir` here was a write-primitive outside the
            # whitelist -- measured: `Q:\vcs-probe\out` reached the mkdir and failed only
            # because the drive did not exist.
            for key, label in (("out_dir", "输出目录"), ("default_out_dir", "默认输出目录")):
                value = body.get(key)
                if value not in (None, ""):
                    require_writable_dir(str(value), label)
            kind = "repair" if str(body.get("mode") or "compress") == "repair" else "compress"

            items = []
            seen_dirs: Dict[str, Dict[str, Any]] = {}
            for raw in [str(p) for p in selected[:500]]:
                if not _is_allowed(raw):
                    items.append({"path": raw, "ok": False,
                                  "problems": ["路径不在允许范围内"], "warnings": []})
                    continue
                info = probe(raw)
                if not info.ok:
                    items.append({"path": raw, "ok": False,
                                  "problems": [info.error or "无法探测该文件"],
                                  "warnings": []})
                    continue
                out_dir = str(paths.resolve_out_dir(
                    raw, location, str(body.get("out_dir") or ""),
                    str(body.get("default_out_dir") or settings.default_out_dir()),
                    kind=kind))
                # One disk probe per directory, not per file: a 200-file batch should
                # not stat the same volume 200 times.
                if out_dir not in seen_dirs:
                    seen_dirs[out_dir] = space.preflight(out_dir, raw, info.size_bytes)
                base = seen_dirs[out_dir]
                payload = dict(base)
                payload["path"] = raw
                payload["src_size"] = info.size_bytes
                # Per-file problems the directory check cannot see.
                problems = list(base.get("problems") or [])
                if info.video and not info.video.width:
                    problems.append("该文件没有可用的视频流")
                payload["problems"] = problems
                payload["ok"] = not problems
                items.append(payload)

            worst = "ok"
            for item in items:
                if not item["ok"]:
                    worst = "error"
                    break
                if item.get("warnings"):
                    worst = worst if worst == "warn" else "warn"
            self._json({"ok": True, "level": worst, "items": items,
                        "checked": len(items)})
            return

        if method == "POST" and path == "/api/measure":
            # Honest compression size/time estimate from real sample encodes.
            body = self._read_json()
            src = body.get("path") or ""
            if not src or not _is_allowed(src):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            info = probe(src)
            if not info.ok:
                raise ApiError(400, info.error or "无法探测该文件")
            profile = get_profile(body.get("profile") or "P3")
            ov = manager._make_overrides(self._client_overrides(manager, body))
            plan = make_plan(info, profile, ov)
            seconds = max(4, min(15, int(body.get("seconds") or 8)))
            result = measure_compress_cost(info, plan, sample_seconds=seconds)
            if not result.get("ok"):
                raise ApiError(500, result.get("error") or "测算失败")
            self._json({"ok": True, "estimate": {
                "low": result["est_low"],
                "high": result["est_high"],
                "mid": result["est_bytes"],
                "ratio": result["ratio"],
                "source_bytes": result["source_bytes"],
                "source_kbps": result["source_kbps"],
                "avg_kbps": result["avg_kbps"],
                "samples": result["samples_kbps"],
                "est_seconds": result["est_full_seconds"],
                "sample_seconds": result["sample_seconds"],
                "sample_positions": result["sample_positions"],
                "encoder": result.get("encoder", plan.encoder),
                "basis": "按 {} 段真实样片（{}）用档位 {} 实际编码外推".format(
                    len(result["sample_positions"]),
                    "、".join("{}s".format(p) for p in result["sample_positions"]),
                    plan.encoder),
                "note": result["note"],
            }})
            return

        if method == "POST" and path == "/api/repair/diagnose":
            body = self._read_json()
            src = body.get("path") or ""
            if not src or not _is_allowed(src):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            if not os.path.isfile(src):
                raise ApiError(404, "文件不存在: {}".format(src))
            diag = diagnose(src)
            if not diag.ok:
                raise ApiError(400, diag.error or "无法探测该文件")
            strategies = [s.__dict__ for s in available_strategies(diag)]
            self._json({
                "ok": True,
                "diagnosis": diag.to_dict(),
                "strategies": strategies,
                "default_strategy": pick_default_strategy(
                    diag, available_strategies(diag)),
            })
            return

        if method == "POST" and path == "/api/repair/plan":
            body = self._read_json()
            src = body.get("path") or ""
            if not src or not _is_allowed(src):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            diag = diagnose(src)
            if not diag.ok:
                raise ApiError(400, diag.error or "无法探测该文件")
            strategy = body.get("strategy") or pick_default_strategy(
                diag, available_strategies(diag))
            options = dict(body.get("options") or {})
            options.setdefault("default_out_dir", settings.default_out_dir())
            plan = build_repair_plan(diag, strategy, options)
            plan["out_location"] = paths.describe(
                src, str(options.get("location") or "beside"),
                str(options.get("out_dir") or ""),
                str(options.get("default_out_dir") or ""), kind="repair")
            self._json({"ok": True, "plan": {k: v for k, v in plan.items() if k != "argv"},
                        "diagnosis": diag.to_dict()})
            return

        if method == "POST" and path == "/api/repair/measure":
            # Honest size/time estimate: runs a few short real encodes.
            body = self._read_json()
            src = body.get("path") or ""
            if not src or not _is_allowed(src):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            diag = diagnose(src)
            if not diag.ok:
                raise ApiError(400, diag.error or "无法探测该文件")
            strategy = body.get("strategy") or pick_default_strategy(
                diag, available_strategies(diag))
            options = dict(body.get("options") or {})
            options.setdefault("default_out_dir", settings.default_out_dir())
            seconds = max(4, min(15, int(body.get("seconds") or 8)))
            self._json({"ok": True, "measure": measure_repair_cost(
                diag, strategy, options, sample_seconds=seconds)})
            return

        if method == "POST" and path == "/api/repair/jobs":
            body = self._read_json()
            selected = body.get("paths") or ([body["path"]] if body.get("path") else [])
            if not isinstance(selected, list) or not selected:
                raise ApiError(400, "缺少 paths")
            bad = [p for p in selected if not _is_allowed(str(p))]
            if bad:
                raise ApiError(403, "以下路径不在允许范围内: {}".format(", ".join(map(str, bad[:3]))))
            options = dict(body.get("options") or {})
            options.setdefault("default_out_dir", settings.default_out_dir())
            jobs = manager.submit_repair(
                [str(p) for p in selected],
                body.get("strategy") or "",
                options,
            )
            self._json({"ok": True, "jobs": jobs}, 201)
            return

        if method == "POST" and path == "/api/plan":
            body = self._read_json()
            src = body.get("path") or ""
            if not src or not _is_allowed(src):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            info = manager.cached_probe(src) or probe(src)
            manager.remember_probe(info)
            if not info.ok:
                raise ApiError(400, info.error or "无法探测该文件")
            profile = get_profile(body.get("profile") or "P3")
            overrides = self._client_overrides(manager, body)
            # The shared default folder lives in settings, not in the page, so a
            # batch keeps landing in the same place across sessions.
            overrides.setdefault("default_out_dir", settings.default_out_dir())
            ov = manager._make_overrides(overrides)
            plan = make_plan(info, profile, ov, exists_probe=os.path.isfile)
            payload = plan.to_dict()
            payload["out_location"] = paths.describe(
                src, str(overrides.get("location") or "beside"),
                str(overrides.get("out_dir") or ""),
                str(overrides.get("default_out_dir") or ""), kind="compress")
            self._json({"ok": True, "plan": payload, "media": info.to_dict()})
            return

        if method == "POST" and path == "/api/jobs/batch":
            body = self._read_json()
            unknown = set(body) - {'paths', 'per_file', 'skip_completed', 'recycle_sources', 'confirm_recycle', 'expected_count'}
            if unknown:
                raise ApiError(400, '不认识的批次参数：{}'.format('、'.join(sorted(unknown))))
            if type(body.get('skip_completed', False)) is not bool or type(body.get('confirm_recycle', False)) is not bool:
                raise ApiError(400, '跳过与二次确认选项必须为布尔值')
            selected = body.get('paths')
            if (not isinstance(selected, list) or not 1 <= len(selected) <= 200 or
                    any(not isinstance(p, str) or not p for p in selected) or
                    len(set(selected)) != len(selected)):
                raise ApiError(400, 'paths 必须为 1–200 个不重复的文件路径')
            if any(not _is_allowed(p) for p in selected):
                raise ApiError(403, '源文件路径不在允许范围内')
            normalized, identities = set(), set()
            for source in selected:
                try:
                    key = os.path.normcase(str(Path(source).resolve()))
                except (OSError, ValueError):
                    raise ApiError(400, '无法确认源文件路径：{}'.format(source))
                identity = jobs_mod.actions.file_identity(source)
                file_id = (identity['dev'], identity['ino']) if identity else None
                if key in normalized or (file_id is not None and file_id in identities):
                    raise ApiError(400, '同一批次包含重复的源文件，请移除重复项')
                normalized.add(key)
                if file_id is not None:
                    identities.add(file_id)
            recycle = body.get('recycle_sources', False)
            if type(recycle) is not bool:
                raise ApiError(400, 'recycle_sources 必须为布尔值')
            if recycle and (body.get('confirm_recycle') is not True or
                            type(body.get('expected_count')) is not int or
                            body['expected_count'] != len(selected)):
                raise ApiError(400, '源文件处理必须二次确认，且确认数量须与本次文件数量一致')
            selections = body.get('per_file')
            if not isinstance(selections, dict) or set(selections) != set(selected):
                raise ApiError(400, '每个提交文件都必须有对应的压缩参数')
            entries = []
            for source in selected:
                selection = selections[source]
                if not isinstance(selection, dict):
                    raise ApiError(400, '逐文件压缩参数必须为对象')
                if set(selection) - {'profile', 'overrides'}:
                    raise ApiError(400, '逐文件选择只允许 profile 与 overrides')
                profile = selection.get('profile') or 'P3'
                try:
                    get_profile(profile)
                except (KeyError, TypeError):
                    raise ApiError(400, '未知档位：{}'.format(profile))
                overrides = self._client_overrides(manager, selection)
                overrides.setdefault('default_out_dir', settings.default_out_dir())
                entries.append({'path': source, 'profile': profile, 'overrides': overrides})
            skipped, keep = [], []
            for entry in entries:
                previous = history_mod.latest_for_source(entry['path']) if body.get('skip_completed') else None
                if previous:
                    skipped.append({'path': entry['path'], 'out_path': previous['out_path']})
                else:
                    keep.append(entry)
            result = manager.submit_batch(keep, recycle_sources=recycle,
                                          confirm_recycle=body.get('confirm_recycle') is True)
            self._json(dict(result, ok=True, skipped=skipped, requested=len(selected)), 201)
            return

        if method == "POST" and path == "/api/jobs":
            body = self._read_json()
            if any(key in body for key in ('recycle_source', 'recycle_sources', 'confirm_recycle')):
                raise ApiError(400, '本次源文件处理请使用批次提交接口')
            # NB: do not name this `paths` -- it would shadow the core.paths module.
            selected = body.get("paths") or []
            if not isinstance(selected, list) or not selected:
                raise ApiError(400, "缺少 paths")
            bad = [p for p in selected if not _is_allowed(str(p))]
            if bad:
                raise ApiError(403, "以下路径不在允许范围内: {}".format(", ".join(map(str, bad[:3]))))
            overrides = self._client_overrides(manager, body)
            overrides.setdefault("default_out_dir", settings.default_out_dir())

            # G6b -- "跳过已完成": re-running a folder should not re-encode what is
            # already compressed. Only skipped when the previous output still exists,
            # and always reported back so the count is never silently different from
            # what the user queued.
            queued = [str(p) for p in selected]
            skipped: List[Dict[str, Any]] = []
            if body.get("skip_completed"):
                keep = []
                for p in queued:
                    prev = history_mod.latest_for_source(p)
                    if prev:
                        skipped.append({"path": p, "out_path": prev["out_path"],
                                        "size": prev["total_size"],
                                        "finished": prev["finished"]})
                    else:
                        keep.append(p)
                queued = keep

            jobs = manager.submit(queued, body.get("profile") or "P3", overrides)
            self._json({"ok": True, "jobs": jobs, "skipped": skipped,
                        "requested": len(selected)}, 201)
            return

        if method == "POST" and path == "/api/browse":
            body = self._read_json()
            target = body.get("path") or ""
            if not target:
                entries = [{"name": str(r), "path": str(r), "dir": True} for r in _ROOTS if r.exists()]
                self._json({"ok": True, "path": "", "entries": entries})
                return
            if not _is_allowed(target):
                raise ApiError(403, "路径不在允许范围内")
            directory = Path(target)
            if not directory.is_dir():
                raise ApiError(400, "不是目录: {}".format(target))
            entries = []
            try:
                for child in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    if child.name.startswith("$"):
                        continue
                    entries.append({
                        "name": child.name,
                        "path": str(child),
                        "dir": child.is_dir(),
                        "size": 0 if child.is_dir() else child.stat().st_size,
                    })
            except PermissionError:
                raise ApiError(403, "无权限读取该目录")
            self._json({"ok": True, "path": str(directory), "entries": entries[:2000]})
            return

        match = re.match(r"^/api/jobs/({})/log$".format(_JOB_ID_PATTERN), path)
        if match and method in ("GET", "HEAD"):
            # F8: the structured record of one job. Everything a diagnosis needs travels
            # in ONE response (timeline + checks + command + raw tail + environment),
            # because "which of the five endpoints do I need?" is how a user gives up.
            job_id = match.group(1)
            item = next((j for j in manager.list_jobs() if j["id"] == job_id), None)
            if item is None:
                raise ApiError(404, "任务不存在")
            try:
                limit = max(4096, min(1024 * 1024, int((query.get("tail") or ["0"])[0])))
            except (TypeError, ValueError):
                limit = logs_mod.LOG_TAIL_BYTES
            if not limit:
                limit = logs_mod.LOG_TAIL_BYTES
            log_path = jobs_mod.JOBS_DIR / job_id / "ffmpeg.log"
            tail, total, truncated = logs_mod.read_tail(log_path, limit)
            caps = get_caps()
            environment = {
                "ffmpeg_version": caps.version,
                "ffmpeg_path": caps.ffmpeg or "",
                # The encoders that matter, not all 569: a report needs "was libsvtav1
                # even available", not a dump.
                "encoders": [name for name in caps.encoders
                             if name in ("libsvtav1", "libaom-av1", "librav1e",
                                         "av1_nvenc", "hevc_nvenc", "h264_nvenc",
                                         "libx265", "libx264", "libvvenc")],
            }
            self._json({
                "ok": True,
                "job": item,
                "events": [
                    dict(e, label=logs_mod.kind_label(e.get("kind") or ""))
                    for e in (item.get("events") or [])
                ],
                "log_tail": tail,
                "log_bytes": total,
                "log_truncated": truncated,
                "log_url": "/api/jobs/{}/log/raw".format(job_id),
                "environment": environment,
                "diagnostics": logs_mod.build_diagnostics(
                    item, environment, tail, item.get("events") or [], APP_VERSION),
            })
            return

        match = re.match(r"^/api/jobs/({})/log/raw$".format(_JOB_ID_PATTERN), path)
        if match and method in ("GET", "HEAD"):
            # The whole stored log, as a plain-text download (for attaching to a report).
            job_id = match.group(1)
            if not any(j["id"] == job_id for j in manager.list_jobs()):
                raise ApiError(404, "任务不存在")
            log_path = jobs_mod.JOBS_DIR / job_id / "ffmpeg.log"
            try:
                blob = log_path.read_bytes()
            except OSError:
                blob = b"(no log for this job)\n"
            self._send(HTTPStatus.OK, blob, "text/plain; charset=utf-8",
                       {"Content-Disposition":
                        'attachment; filename="job-{}.log"'.format(job_id)})
            return

        match = re.match(r"^/api/jobs/({})(?:/(cancel|retry|move|pause|resume))?$".format(_JOB_ID_PATTERN), path)
        if match:
            job_id, action = match.group(1), match.group(2)
            if method == "POST" and action == "cancel":
                self._json({"ok": manager.cancel(job_id)})
                return
            if method == "POST" and action == "retry":
                self._json({"ok": manager.retry(job_id)})
                return
            if method == "POST" and action in ("pause", "resume"):
                # Pause/resume (G6). Both report the reason on refusal: "can't pause"
                # without saying why is the same as the button not working.
                result = (manager.pause(job_id) if action == "pause"
                          else manager.resume(job_id))
                if not result.get("ok"):
                    raise ApiError(400, result.get("error") or "操作失败")
                self._json({"ok": True, "job": result.get("job")})
                return
            if method == "POST" and action == "move":
                # Queue reordering (F12b): only queued jobs move.
                body = self._read_json()
                before = str(body.get("before") or "")
                if before and not _JOB_ID_RE.match(before):
                    raise ApiError(400, "before 的 id 非法")
                if not manager.move(job_id, before):
                    raise ApiError(400, "只有排队中的任务可以调整顺序")
                self._json({"ok": True, "jobs": manager.list_jobs()})
                return
            if method == "PATCH":
                # Per-job parameter edit (F12a). Refused once the job has started.
                body = self._read_json()
                overrides_patch = body.get("overrides")
                if overrides_patch is not None and not isinstance(overrides_patch, dict):
                    raise ApiError(400, "overrides 必须是对象")
                result = manager.update_job(job_id, overrides_patch or {},
                                            str(body.get("profile") or ""))
                if not result.get("ok"):
                    raise ApiError(400, result.get("error") or "无法修改该任务")
                self._json(result)
                return
            if method == "DELETE":
                self._json({"ok": manager.remove(job_id)})
                return
            if method in ("GET", "HEAD"):
                for item in manager.list_jobs():
                    if item["id"] == job_id:
                        self._json({"ok": True, "job": item})
                        return
                raise ApiError(404, "任务不存在")

        if method == "POST" and path == "/api/jobs/release":
            # "Start the queue I had before" -- the explicit act that a restored queue now
            # requires. Opening the app is not consent to start encoding: reported from real
            # use, where launching the app immediately began two A-tier encodes and made the
            # machine unusable before the user had looked at anything.
            self._read_json()
            self._json(manager.release_restored())
            return

        if method == "POST" and path == "/api/jobs/discard-restored":
            body = self._read_json()
            require_confirm(body, "移除上次恢复的排队记录（视频文件保留）")
            self._json(manager.discard_restored())
            return

        if method == "POST" and path == "/api/jobs/pause-all":
            # Reversible, so no confirmation: the complaint that produced this pair of
            # endpoints was the opposite one -- "I paused everything and there was no way
            # to start again", which is what /api/jobs/resume-all answers.
            self._read_json()
            result = manager.pause_all()
            self._json({"ok": True, **result})
            return

        if method == "POST" and path == "/api/jobs/resume-all":
            self._read_json()
            result = manager.resume_all()
            self._json({"ok": True, **result})
            return

        if method == "POST" and path == "/api/jobs/cancel-all":
            # The "stop everything" half of stop-current vs stop-all (G5). It also
            # cancels QUEUED work, not just what is in flight -- leaving a queue to
            # start later is the surprise this button exists to prevent. Cancelling is
            # recoverable (nothing is deleted), but abandoning a long queue is not
            # something to do by accident, so it carries the same explicit-act marker.
            body = self._read_json()
            require_confirm(body, "停止全部会同时取消排队中的任务")
            self._json({"ok": True, "stopped": manager.cancel_all()})
            return

        if method == "POST" and path == "/api/jobs/clear":
            # Deliberately NOT confirmed: this only drops finished rows from the list.
            # Reports and output files stay on disk (see JobManager.clear_finished), so
            # adding a dialog here would train the user to click through the one that
            # actually matters (history/remove with delete_output).
            self._read_json()
            removed = manager.clear_successful()
            self._json({"ok": True, "removed": len(removed), "removed_ids": removed})
            return

        # -- history -----------------------------------------------------------
        # The archive already existed on disk (store/jobs/*/report.json); these
        # routes only index it.  Ids are strictly validated before they are ever
        # turned into a path, so no request can reach outside store/jobs/.
        if method in ("GET", "HEAD") and path == "/api/history":
            try:
                limit = int((query.get("limit") or ["200"])[0])
            except (TypeError, ValueError):
                limit = 200
            limit = max(0, min(limit, 2000))
            needle = (query.get("query") or [""])[0]
            only_existing = (query.get("only_existing") or ["0"])[0] in ("1", "true", "yes")
            self._json({
                "ok": True,
                "entries": history_mod.index(limit=limit, query=needle,
                                             only_existing=only_existing),
                "stats": history_mod.stats(),
            })
            return

        if method == "POST" and path == "/api/history/remove":
            body = self._read_json()
            delete_output = bool(body.get("delete_output"))
            if delete_output and not body.get("confirm_delete_output"):
                raise ApiError(400, "删除成品文件需要二次确认")

            # Batch form: {"ids": [...]} or {"all_matching": true, "query": "..."}.
            # `all_matching` is resolved server-side so a client page limit cannot
            # leave part of the filtered set behind and make the count a lie.
            raw_ids = body.get("ids")
            all_matching = bool(body.get("all_matching"))
            if all_matching:
                # `all_matching` + `delete_output` was one flag away from deleting the
                # output files of EVERY record: the scope came from the server-side
                # filter, so the confirmation the user saw (a list they had selected)
                # and the set actually destroyed could be different sizes. Binding the
                # two makes "delete everything" a thing the client has to state.
                expected = body.get("expected_count")
                if delete_output and not isinstance(expected, int):
                    raise ApiError(400,
                                   "按条件批量删除成品需要同时给出 expected_count"
                                   "（= 你确认过的条数），以免删除范围与确认的不一致")
            if isinstance(raw_ids, list) or all_matching:
                ids = [str(i) for i in (raw_ids or [])]
                for job_id in ids:
                    if not _JOB_ID_RE.match(job_id):
                        raise ApiError(400, "任务 id 非法: {}".format(job_id))
                if not ids and not all_matching:
                    raise ApiError(400, "未选择任何记录")
                result = history_mod.remove_many(
                    ids=ids,
                    query=str(body.get("query") or ""),
                    only_existing=bool(body.get("only_existing")),
                    all_matching=all_matching,
                    delete_output=delete_output,
                    expected_count=(body.get("expected_count")
                                    if isinstance(body.get("expected_count"), int) else None),
                )
                result["stats"] = history_mod.stats()
                self._json(result)
                return

            job_id = str(body.get("id") or "")
            if not _JOB_ID_RE.match(job_id):
                raise ApiError(400, "任务 id 非法")
            result = history_mod.remove(job_id, delete_output=delete_output)
            result["stats"] = history_mod.stats()
            self._json(result)
            return

        if method == "POST" and path == "/api/history/clear":
            body = self._read_json()
            # `only_missing` only forgets records whose file is already gone (harmless);
            # the default form removes EVERY record, so it needs the act marker.
            if not body.get("only_missing"):
                stats = history_mod.stats()
                require_confirm(body, "清空档案会删除全部记录（成品文件保留）",
                                "，当前 {} 条".format(stats.get("total", "?")))
            result = history_mod.clear(only_missing=bool(body.get("only_missing")))
            result["stats"] = history_mod.stats()
            self._json(result)
            return

        if method in ("GET", "HEAD") and path == "/api/settings":
            current = settings.load()
            policy = manager.memory_policy()
            current.update(memory_guard=policy["guard"], memory_margin_mb=policy["margin_mb"],
                           memory_low_water_mb=policy["low_water_mb"], limits=manager.limits())
            self._json({
                "ok": True,
                "settings": current,
                "suggested_default_out_dir": settings.resolve_default_candidate(),
                "locations": [
                    {"id": "beside", "name": "源文件旁边",
                     "description": paths.DESCRIPTIONS["beside"]},
                    {"id": "default", "name": "固定默认目录",
                     "description": paths.DESCRIPTIONS["default"]},
                    {"id": "custom", "name": "本次指定目录",
                     "description": paths.DESCRIPTIONS["custom"]},
                ],
            })
            return

        if method == "POST" and path == "/api/settings":
            body = self._read_json()
            ui_patch = {}
            for key in ("show_advanced", "show_extreme", "slow_av1"):
                if key in body:
                    if type(body[key]) is not bool:
                        raise ApiError(400, key + " 必须是布尔值")
                    ui_patch[key] = body[key]
            if "compatibility" in body:
                if body["compatibility"] not in ("broad", "modern", "av1"):
                    raise ApiError(400, "未知兼容偏好")
                ui_patch["compatibility"] = body["compatibility"]
            memory_keys = {"memory_guard": "guard", "memory_margin_mb": "margin_mb",
                           "memory_low_water_mb": "low_water_mb"}
            changed: Dict[str, Any] = dict(ui_patch)
            changed.update({key:body[key] for key in memory_keys if key in body})
            if "limits" in body:
                changed["limits"] = body["limits"]
            if "reset_limits" in body:
                if body['reset_limits'] is not True or 'limits' in body:
                    raise ApiError(400, '恢复推荐值不能同时指定并行数量')
                recommended = sizing.recommend()
                changed["limits"] = {key:recommended[key] for key in ('gpu', 'cpu')}
            target = None
            if "default_out_dir" in body:
                candidate = str(body["default_out_dir"] or "").strip()
                if candidate:
                    # Validate eagerly: a typo should fail here, not mid-batch.
                    # Order matters -- the whitelist check runs BEFORE mkdir, or a
                    # rejected path still creates its directory (measured: a 403 on
                    # `C:\Windows\Temp\x` left `x` behind).
                    target = Path(candidate)
                    if not _is_allowed_write_dir(str(target)):
                        raise ApiError(403, "该目录不在允许范围内（或位于系统目录中）: {}".format(candidate))
                    if target.exists() and not target.is_dir():
                        raise ApiError(400, "该路径是一个文件，不是目录: {}".format(candidate))
                changed["default_out_dir"] = candidate
            if "location" in body:
                value = str(body["location"] or "beside").lower()
                if value not in paths.LOCATIONS:
                    raise ApiError(400, "未知的保存位置: {}".format(value))
                changed["location"] = value
            for key in ('deep_verify', 'vmaf_check'):
                if key in body:
                    if type(body[key]) is not bool:
                        raise ApiError(400, key + ' 必须是布尔值')
                    changed[key] = body[key]
            unknown = set(body) - set(settings.DEFAULTS) - {'reset_limits'}
            if unknown or 'concurrency' in body:
                raise ApiError(400, '不支持的设置字段')
            try:
                saved = manager.save_settings(changed, target)
            except ValueError as exc:
                raise ApiError(400, str(exc))
            except OSError as exc:
                raise ApiError(503, '设置未保存，运行参数保持原值：{}'.format(exc))
            self._json({"ok": True, "settings": saved, "limits": saved["limits"],
                        "memory": saved["memory"],
                        "suggested_default_out_dir": settings.resolve_default_candidate()})
            return

        if method in ("GET", "HEAD") and path == "/api/verify":
            target = (query.get("path") or [""])[0]
            if not target or not _is_allowed(target):
                raise ApiError(400, "缺少或不在允许范围内的 path")
            src = (query.get("source") or [""])[0]
            if src and not _is_allowed(src):
                raise ApiError(400, '源文件不在允许范围内')
            source = probe(src) if src and _is_allowed(src) else None
            if source is not None and not source.ok:
                raise ApiError(400, source.error or '源文件不可用')
            deep = (query.get("deep") or ["0"])[0] in ("1", "true", "yes")
            result = verify(target, source, deep=deep).to_dict()
            self._json({"ok": result['verdict'] == 'passed', "verify": result})
            return

        raise ApiError(404, "未知接口: {}".format(path))

    # ------------------------------------------------------------------ SSE

    def _sse(self) -> None:
        manager = get_manager()
        events: "queue_mod.Queue[Dict[str, Any]]" = queue_mod.Queue(maxsize=2000)

        def listener(payload: Dict[str, Any]) -> None:
            try:
                events.put_nowait(payload)
            except queue_mod.Full:
                pass

        manager.add_listener(listener)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def write(payload: Dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False)
                self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
                self.wfile.flush()

            write({"event": "snapshot", "jobs": manager.list_jobs(), "stats": manager.stats(),
                   "restore": manager.restore_state()})
            last_beat = time.time()
            while True:
                try:
                    payload = events.get(timeout=1.0)
                    write(payload)
                except queue_mod.Empty:
                    now = time.time()
                    if now - last_beat > 15:
                        last_beat = now
                        write({"event": "ping", "stats": manager.stats()})
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            manager.remove_listener(listener)

    # --------------------------------------------------------------- static

    def _serve_static(self, path: str) -> None:
        if path == "/media":
            # `/media` is a FILE READER over the allowed roots, so it needs a cross-site
            # guard of its own; `_check_same_site` covers the no-Origin case that
            # `_check_origin` cannot see.
            self._check_same_site()
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            target = (query.get("path") or [""])[0]
            if not target or not _is_allowed(target) or not os.path.isfile(target):
                raise ApiError(404, "媒体文件不存在")
            # Only real media may render inline; anything else downloads, so a dropped
            # `.html` cannot become a page in this origin.
            guessed = (mimetypes.guess_type(target)[0] or "")
            inline = guessed.startswith(("video/", "audio/", "image/"))
            self._send_file(Path(target), download=not inline,
                            content_type=guessed if inline else "application/octet-stream")
            return

        rel = path.lstrip("/") or "index.html"
        if ".." in rel.split("/"):
            raise ApiError(403, "非法路径")
        candidate = (WEB_DIR / rel).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            raise ApiError(403, "非法路径")
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            raise ApiError(404, "未找到资源: {}".format(rel))

        # Stamp the asset fingerprint into the HTML so a changed bundle gets a new
        # URL. Belt and braces with the no-store headers.
        if candidate.suffix.lower() in (".html", ".htm"):
            try:
                html = candidate.read_text(encoding="utf-8")
            except OSError:
                raise ApiError(500, "无法读取页面文件")
            body = html.replace("__ASSET_SIG__", ASSET_SIG).encode("utf-8")
            self._send(status=200, body=body, content_type="text/html; charset=utf-8",
                       extra={"Cache-Control": "no-store, no-cache, must-revalidate",
                              "Pragma": "no-cache", "Expires": "0"})
            return

        self._send_file(candidate, download=rel.endswith(".txt"))

    def _send_file(self, file_path: Path, download: bool = False,
                   content_type: Optional[str] = None,
                   no_store: bool = True) -> None:
        size = file_path.stat().st_size
        ctype = content_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

        # Range support so the built-in preview player can seek.
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            if size == 0:
                # `end = size - 1 = -1` used to be clamped into "bytes 0-0/0" with
                # Content-Length: 1 and no body at all -- a response that lies about its
                # own length and desynchronises keep-alive. RFC 7233: no satisfiable
                # range exists for an empty representation.
                self.send_response(416)
                self.send_header("Content-Range", "bytes */0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            spec = range_header[len("bytes="):].split(",")[0]
            first, _, last = spec.partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:
                    start = max(0, size - int(last))
            except ValueError:
                start, end = 0, size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        # Without nosniff a browser may sniff a `.txt`/unknown file as HTML and render it
        # in this app's origin, which is a script-execution primitive for any file the
        # user can be talked into dropping in an allowed folder.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        # Front-end assets must never be served from cache: a stale app.js was a
        # real "I don't see the update" report, and it is indistinguishable from a
        # broken build. Media files still cache normally (they are content).
        if no_store:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        else:
            self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
        if download:
            self.send_header("Content-Disposition",
                             'attachment; filename="{}"'.format(file_path.name))
        self.end_headers()
        if self.command == "HEAD":
            return
        remaining = length
        with open(file_path, "rb") as fh:
            fh.seek(start)
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


def create_server(host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    if host not in ALLOWED_HOSTS:
        raise SystemExit("出于安全考虑，只允许绑定 127.0.0.1（收到: {}）".format(host))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    _INBOX.start()
    return server
