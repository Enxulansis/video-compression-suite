"""HTTP transport regression tests for the keep-alive body handling.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_keepalive.py

Why this file exists
--------------------
The backend is a stdlib `ThreadingHTTPServer` with `protocol_version = "HTTP/1.1"`,
so a connection is reused for many requests and ONE `Handler` instance serves all of
them. Two bugs of exactly that shape shipped and were only ever caught by hand:

* an unread request body stayed in the socket, so the next request line became
  `{}GET /api/jobs` and the server answered 501 Unsupported method;
* the body memo was created once per connection instead of once per request, so the
  second request on a pooled connection was handed the FIRST request's body while
  its own body stayed in the socket -- a settings POST that "succeeded" but changed
  nothing, followed by a 501 for whatever came next.

Both are invisible to single-request tests, and invisible to a browser because
connection pooling makes them intermittent. So this file drives a real server over a
raw socket and sends several requests down ONE connection, asserting that each body
reaches its own request and that nothing is left behind.
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

# Isolate the store BEFORE importing the app: STORE_DIR is resolved at import time,
# and a test must never write into the user's real archive.
_ISOLATED_STORE = Path(__file__).resolve().parent / "_work" / "store_keepalive"
_ISOLATED_STORE.mkdir(parents=True, exist_ok=True)
os.environ["VCS_STORE_DIR"] = str(_ISOLATED_STORE)

import server as server_mod  # noqa: E402
from core.settings import load as load_settings  # noqa: E402
from core.settings import save as save_settings  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(label)
        print("  ok   {}".format(label))
    else:
        FAILED.append(label)
        print("  FAIL {} {}".format(label, detail))


class Conn:
    """One keep-alive connection driven by hand, so pooling cannot hide anything."""

    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.create_connection((host, port), timeout=30)
        self.buf = b""

    def request(self, method: str, path: str, body: bytes | None = None,
                host: str = "127.0.0.1") -> tuple[int, bytes]:
        head = "{} {} HTTP/1.1\r\nHost: {}\r\n".format(method, path, host)
        if body is not None:
            head += "Content-Type: application/json\r\nContent-Length: {}\r\n".format(len(body))
        head += "Connection: keep-alive\r\n\r\n"
        self.sock.sendall(head.encode("ascii") + (body or b""))
        return self.read_response()

    def read_response(self) -> tuple[int, bytes]:
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError("连接在收到完整响应头之前被关闭")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or 0)
        while len(self.buf) < length:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError("连接在收到完整响应体之前被关闭")
            self.buf += chunk
        body, self.buf = self.buf[:length], self.buf[length:]
        return status, body

    def leftover(self) -> bytes:
        """Bytes still queued after the last response -- non-empty means a lost body."""
        self.sock.settimeout(0.35)
        try:
            extra = self.sock.recv(65536)
        except (socket.timeout, TimeoutError):
            return b""
        finally:
            self.sock.settimeout(30)
        self.buf += extra
        return self.buf

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def main() -> int:
    httpd = server_mod.create_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print("测试服务器: 127.0.0.1:{}".format(port))

    original = load_settings()
    try:
        try:
            session(port)
        except Exception as exc:  # noqa: BLE001
            # A regressed transport kills the connection rather than returning a bad
            # status, so report it as one readable failure instead of a traceback --
            # the steps after a broken connection would be meaningless anyway.
            check("连接全程保持可用（后续步骤才能执行）", False,
                  "{}: {}".format(type(exc).__name__, exc))
    finally:
        try:
            save_settings(original)
        except Exception as exc:  # noqa: BLE001
            print("  warn 还原 settings 失败: {}".format(exc))
        httpd.shutdown()
        httpd.server_close()

    print()
    print("通过 {} / 失败 {}".format(len(PASSED), len(FAILED)))
    for label in FAILED:
        print("  FAILED: {}".format(label))
    return 1 if FAILED else 0


def session(port: int) -> None:
    conn = Conn("127.0.0.1", port)

    # ---- 1. body-ignoring route followed by a GET on the SAME connection -----
    # This is the literal 501 reproduction: /api/jobs/clear ignores its body.
    status, body = conn.request("POST", "/api/jobs/clear", b"{}")
    check("POST 忽略 body 的路由返回 200", status == 200, "status={}".format(status))

    status, body = conn.request("GET", "/api/jobs")
    check("同一连接上的后续 GET 未被残留 body 污染",
          status == 200 and json.loads(body).get("ok") is True,
          "status={} body={!r}".format(status, body[:120]))

    # ---- 2. the settings body must actually be parsed -----------------------
    # Distinct values per request: if the memo leaked, limits stay at the first
    # value (or the request 501s) instead of tracking the latest body.
    want = {"limits": {"gpu": 3, "cpu": 2}}
    status, body = conn.request("POST", "/api/settings", json.dumps(want).encode())
    check("POST /api/settings 返回 200", status == 200, "status={}".format(status))

    status, body = conn.request("GET", "/api/jobs")
    stats = json.loads(body).get("stats", {}) if status == 200 else {}
    check("首个 settings body 生效", stats.get("limits") == {"gpu": 3, "cpu": 2},
          "limits={}".format(stats.get("limits")))

    # Second POST on the SAME connection with a DIFFERENT body.
    want2 = {"limits": {"gpu": 2, "cpu": 4}}
    status, body = conn.request("POST", "/api/settings", json.dumps(want2).encode())
    check("同连接第二次 POST /api/settings 返回 200", status == 200,
          "status={}".format(status))

    status, body = conn.request("GET", "/api/jobs")
    stats = json.loads(body).get("stats", {}) if status == 200 else {}
    check("同连接第二次 POST 的 body 才是生效的那个（无粘包/无旧 body 复用）",
          stats.get("limits") == {"gpu": 2, "cpu": 4},
          "limits={} status={}".format(stats.get("limits"), status))

    # ---- 3. PATCH (F12a per-job edit) must not be 501 -----------------------
    status, body = conn.request("PATCH", "/api/jobs/none-such-job",
                                b'{"profile_id": "P1"}')
    check("PATCH 有 do_PATCH 而不是 501", status != 501,
          "status={} body={!r}".format(status, body[:120]))

    # ---- 4. nothing left in the socket after all of that --------------------
    leftover = conn.leftover()
    check("连接内没有未被读取的请求字节", leftover == b"",
          "residual={!r}".format(leftover[:80]))

    conn.close()

    # ---- 5. a second connection still works (pool reused after close) -------
    conn2 = Conn("127.0.0.1", port)
    status, body = conn2.request("GET", "/api/caps")
    check("新连接 GET /api/caps 正常", status == 200 and json.loads(body).get("ok") is True,
          "status={}".format(status))
    conn2.close()


if __name__ == "__main__":
    raise SystemExit(main())
