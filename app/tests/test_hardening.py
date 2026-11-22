"""Regression tests for the hardening batch (security review follow-up).

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_hardening.py

Why this file exists
--------------------
A read-only review of the server and API found a set of defects that all share one
shape: *the app trusted something it should have checked*. None of them were visible
from the happy path, and the happy path is all the other test files exercise, so each
one is pinned here with the exact request or input that used to slip through:

* `out_dir` / `name_template` were never validated -- arbitrary directory creation and
  an output name that could climb out of the chosen folder;
* `/media` had no cross-site guard (`Origin` is not sent for `<img>`/`<video>`), so any
  local page could probe for files or have a local `.html` executed in this origin;
* a chunked request body was read as EMPTY and left the chunk bytes to be parsed as the
  next request line; a declared-but-unsent Content-Length blocked a thread forever;
* `HEAD /api/frame` wrote a JPEG body anyway, desynchronising keep-alive;
* a `Range` request on a 0-byte file answered `206 … bytes 0-0/0` with
  `Content-Length: 1` and no body;
* `extra_args` could override `-g`, silently breaking the project's one hard invariant;
* `gop_seconds > 2` was clamped for `-g` but NOT for the SVT-AV1 `keyint` parameter or
  the `force_key_frames` expression;
* destructive endpoints (`history/clear`, `inbox/clear`, bulk delete) acted on a scope
  the client had not confirmed.

Everything here is mechanical (no encoding), so it runs in a few seconds.
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import urllib.parse
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

# Isolate the store BEFORE importing the app: STORE_DIR is resolved at import time and
# these requests touch history/settings.
_ISOLATED_STORE = Path(__file__).resolve().parent / "_work" / "store_hardening"
_ISOLATED_STORE.mkdir(parents=True, exist_ok=True)
os.environ["VCS_STORE_DIR"] = str(_ISOLATED_STORE)

import server as server_mod  # noqa: E402
from core import actions as actions_mod  # noqa: E402
from core import audio as audio_mod  # noqa: E402
from core import history as history_mod  # noqa: E402
from core import paths as paths_mod  # noqa: E402
from core import planner as planner_mod  # noqa: E402
from core.probe import MediaInfo, StreamInfo  # noqa: E402
from core.settings import load as load_settings  # noqa: E402
from core.settings import save as save_settings  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
#: A real, playable clip in the test work dir -- used where the request has to reach
#: the point of actually opening a file rather than being refused earlier.
SRC = Path(__file__).resolve().parent / "_work" / "e2e_src.mp4"


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(label)
        print("  ok   {}".format(label))
    else:
        FAILED.append(label)
        print("  FAIL {} -- {}".format(label, detail))


class Client:
    """Minimal HTTP client that can send a raw header block (for the odd cases)."""

    def __init__(self, port: int) -> None:
        self.port = port

    def raw(self, request: bytes, timeout: float = 10.0,
            read: bool = True) -> tuple[int, dict, bytes]:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        try:
            sock.sendall(request)
            if not read:
                return 0, {}, b""
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("iso-8859-1").split("\r\n")
            status = int(lines[0].split(" ")[1]) if lines and " " in lines[0] else 0
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)
            body = rest
            while len(body) < length:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                body += chunk
            return status, headers, body[:length]
        finally:
            sock.close()

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None) -> tuple[int, dict, bytes]:
        head = "{} {} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n".format(method, path)
        for key, value in (headers or {}).items():
            head += "{}: {}\r\n".format(key, value)
        if body is not None:
            head += "Content-Type: application/json\r\nContent-Length: {}\r\n".format(len(body))
        head += "\r\n"
        return self.raw(head.encode("utf-8") + (body or b""))


# --------------------------------------------------------------------- pure logic

def test_safe_filename() -> None:
    print("  -- output names cannot escape the chosen folder --")
    # Every one of these used to survive into the output path, and the write helper
    # then created whatever directory the name implied.
    hazardous = {
        # Each of these must end up as ONE component inside the chosen folder. The
        # leading `..\..\Public\` is discarded rather than sanitised into the name.
        r"..\..\Public\evil": "evil",
        "../../etc/passwd": "passwd",
        "C:\\Windows\\Temp\\boot": "boot",
        r"a\b\c": "c",
        "con": "_con",
        "CON.mp4": "_CON.mp4",
        "NUL": "_NUL",
        "com1": "_com1",
        'bad<>:"|?*name': "bad_______name",
        "..": "",
        "   ": "",
    }
    for raw, expected in hazardous.items():
        got = paths_mod.safe_filename(raw)
        check("safe_filename({!r}) -> {!r}".format(raw, expected), got == expected,
              "got {!r}".format(got))

    long_name = "x" * 400 + ".mp4"
    trimmed = paths_mod.safe_filename(long_name)
    check("an over-long name is truncated, keeping the extension",
          len(trimmed) <= paths_mod.MAX_NAME_LEN and trimmed.endswith(".mp4"),
          "len={} {!r}".format(len(trimmed), trimmed[:20]))

    # render_name is the public entry point the planner uses; the traversal must be
    # gone by the time it returns.
    rendered = paths_mod.render_name("{stem}", {"stem": r"..\..\Public\evil"})
    check("render_name strips traversal from {stem}", rendered == "evil", rendered)


def test_extra_args_guard() -> None:
    print("  -- expert extra_args cannot break the keyframe invariant --")
    for bad in (["-g", "9999"], ["-g:v", "9999"], ["-force_key_frames", "expr:gte(t,99)"],
                ["-x265-params", "keyint=500"], ["-svtav1-params", "irefresh-type=1"],
                ["-movflags", "-faststart"], ["-c:v", "libx265"]):
        try:
            planner_mod.check_extra_args(bad)
            check("refused: {}".format(" ".join(bad)), False, "accepted silently")
        except planner_mod.PlanError as exc:
            check("refused: {}".format(" ".join(bad)), bool(str(exc)))
    check("harmless flags still pass",
          planner_mod.check_extra_args(["-tune", "film", "-movflags", "+faststart"]) == [])
    warned = planner_mod.check_extra_args(["-g", "9999"], allow_unsafe=True)
    check("专家模式 unlocks it WITH a warning",
          len(warned) == 1 and "专家模式" in warned[0], str(warned))
    try:
        planner_mod.check_extra_args("not-a-list")
        check("a non-list extra_args is refused", False, "accepted")
    except planner_mod.PlanError:
        check("a non-list extra_args is refused", True)


def test_gop_clamp() -> None:
    print("  -- gop_seconds is clamped in ONE place and used everywhere --")
    check("clamp floors at 0.5s", planner_mod.clamp_gop_seconds(0.1) == 0.5)
    check("clamp caps at 2.0s", planner_mod.clamp_gop_seconds(9) == 2.0)
    check("clamp survives a string", planner_mod.clamp_gop_seconds("1.5") == 1.5)
    check("clamp survives garbage", planner_mod.clamp_gop_seconds("abc") == 1.0)
    check("clamp survives NaN", planner_mod.clamp_gop_seconds(float("nan")) == 1.0)
    check("compute_gop uses the clamped value",
          planner_mod.compute_gop(25, 9) == planner_mod.compute_gop(25, 2.0))

    ov = planner_mod.Overrides(gop_seconds=9.0)
    profile = {"id": "PX", "encoder": "libsvtav1", "crf": 40, "preset": 8,
               "container": "mp4"}
    args, _warn = planner_mod._video_args("libsvtav1", profile, ov,
                                          planner_mod.compute_gop(25, 9.0),
                                          "yuv420p", 25.0, gop_seconds=9.0)
    expr = args[args.index("-force_key_frames") + 1]
    check("force_key_frames uses the CLAMPED seconds", expr == "expr:gte(t,n_forced*2)",
          expr)
    params = args[args.index("-svtav1-params") + 1]
    check("SVT-AV1 keyint uses the CLAMPED seconds", "keyint=2s" in params, params)


def test_h264_level() -> None:
    print("  -- H.264 level matches the output geometry --")
    cases = {
        (1280, 720, 30): "3.1",
        (1920, 1080, 30): "4.0",
        (1920, 1080, 60): "4.2",
        (3840, 2160, 30): "5.1",
    }
    for (w, h, fps), expected in cases.items():
        got = planner_mod.h264_level_for(w, h, fps)
        check("level {}x{}@{} -> {}".format(w, h, fps, expected), got == expected, got)
    check("an unknown frame rate yields no claim",
          planner_mod.h264_level_for(1920, 1080, 0) == "")

    profile = {"id": "P2", "encoder": "libx264", "crf": 23, "preset": "medium",
               "container": "mp4", "extra": {"profile:v": "high", "level": "4.0"}}
    args, warnings = planner_mod._video_args("libx264", profile, planner_mod.Overrides(),
                                             60, "yuv420p", 60.0,
                                             out_size=(1920, 1080))
    level = args[args.index("-level") + 1]
    check("a too-low declared level is raised to 4.2", level == "4.2", level)
    check("and the plan says why", any("Level" in w for w in warnings), str(warnings))


def _fake_info(path: str) -> MediaInfo:
    info = MediaInfo(path=path, ok=True)
    # `MediaInfo.video` is a derived property, so the stream has to go in the list.
    info.streams.append(StreamInfo(kind="video", index=0, codec="h264", width=1920,
                                   height=1080, fps=30.0, duration=10.0))
    info.size_bytes = 10 * 1024 * 1024
    return info


def test_plan_refusals() -> None:
    print("  -- plans that could only fail are refused up front --")
    info = _fake_info(r"E:\视频压缩\app\tests\_work\e2e_src.mp4")
    copy_profile = {"id": "PC", "encoder": "copy", "container": "mp4"}
    ov = planner_mod.Overrides(location="beside", scale_percent=50)
    try:
        planner_mod.make_plan(info, copy_profile, ov, caps=_FakeCaps())
        check("copy + a geometry filter is refused", False, "built anyway")
    except planner_mod.PlanError as exc:
        check("copy + a geometry filter is refused", "滤镜" in str(exc), str(exc))

    # strip_metadata must clear chapters too: `-map_metadata -1` alone leaves them.
    ov2 = planner_mod.Overrides(location="beside", strip_metadata=True)
    plan = planner_mod.make_plan(info, {"id": "P3", "encoder": "libx264", "crf": 23,
                                        "preset": "medium", "container": "mp4"},
                                 ov2, caps=_FakeCaps())
    check("清除元数据 also drops chapters (-map_chapters -1)",
          plan.argv[plan.argv.index("-map_chapters") + 1] == "-1", plan.command)

    # The forbidden extra_args must be rejected through make_plan as well, not only by
    # the helper -- that is the path a request actually takes.
    ov3 = planner_mod.Overrides(location="beside", extra_args=["-g", "9999"])
    try:
        planner_mod.make_plan(info, {"id": "P3", "encoder": "libx264", "crf": 23,
                                     "preset": "medium", "container": "mp4"},
                              ov3, caps=_FakeCaps())
        check("make_plan refuses a -g override", False, "built anyway")
    except planner_mod.PlanError:
        check("make_plan refuses a -g override", True)


class _FakeCaps:
    ffmpeg = "ffmpeg"

    def has_encoder(self, name: str) -> bool:
        return name != "copy"

    def has_filter(self, name: str) -> bool:
        return True


def test_audio_peak_basis() -> None:
    print("  -- the loudness check names the peak it compared --")
    # Two-section form (some builds print sample peak AND true peak).
    two_section = """
  Integrated loudness:
    I:         -16.0 LUFS
    Threshold: -26.0 LUFS

  Sample peak:
    Peak:      -2.0 dBFS

  True peak:
    Peak:      -0.5 dBFS
"""
    match = audio_mod._TRUE_PEAK_SECTION.search(two_section)
    check("the True peak section is found", bool(match))
    value = audio_mod._last_number(match.group(1), r"Peak:\s+(-?[\d.]+)\s+dB(?:FS|TP)")
    check("the value comes from the TRUE peak section, not the sample one",
          value == -0.5, str(value))

    loud = audio_mod.Loudness(path="x", ok=True, integrated=-16.0, true_peak=0.4,
                              peak_basis="真峰值（ebur128 True peak）")
    status, detail = audio_mod.verdict(loud, -16.0, -1.0)
    check("a true peak over the ceiling is a warning", status == "warn", detail)
    check("the detail names the basis", "真峰值" in detail, detail)


def test_recycle_fail_closed() -> None:
    print("  -- 删源 refuses when it cannot prove safety --")
    good = actions_mod.safe_to_recycle(r"E:\a\src.mp4", r"E:\out\src_P3.mp4", "passed")
    check("a verified delivery in another folder is allowed", good == "", good)
    check("a risky verdict is refused",
          actions_mod.safe_to_recycle(r"E:\a\src.mp4", r"E:\out\x.mp4", "risky") != "")

    # The "is the source inside the output folder?" guard can only fail by RAISING
    # (unreadable parent, disconnected drive), and that used to `pass` -- i.e. an
    # unprovable case was treated as a proven-safe one. Path.resolve is very tolerant
    # on this Python build, so the branch is forced with a stub rather than hunted for.
    real_path = actions_mod.Path

    class _Unresolvable:
        calls = 0

        def __init__(self, value: object) -> None:
            self.value = str(value)

        def resolve(self):  # noqa: ANN201
            _Unresolvable.calls += 1
            if "boom" in self.value and _Unresolvable.calls > 1:
                raise OSError("无法解析路径")
            return real_path(self.value).resolve()

    try:
        actions_mod.Path = _Unresolvable  # type: ignore[assignment]
        _Unresolvable.calls = 0
        refused = actions_mod.safe_to_recycle("E:\\a\\boom.mp4", r"E:\out\x.mp4", "passed")
    finally:
        actions_mod.Path = real_path  # type: ignore[assignment]
    check("an unprovable path is refused, not allowed",
          "谨慎" in refused, repr(refused))


def test_history_expected_count() -> None:
    print("  -- bulk delete refuses a scope the client did not confirm --")
    result = history_mod.remove_many(all_matching=True, query="", delete_output=False,
                                     expected_count=999)
    check("a count mismatch deletes nothing",
          result.get("ok") is False and result.get("removed") == 0, str(result))
    check("and says what to do", "重新确认" in (result.get("error") or ""),
          result.get("error", ""))


# ----------------------------------------------------------------- HTTP surface

def test_http(port: int) -> None:
    client = Client(port)
    print("  -- write paths are validated, not just read paths --")

    for bad in ("C:\\Windows\\Temp\\vcs-probe", "Q:\\vcs-probe\\out"):
        status, _h, body = client.request(
            "POST", "/api/preflight",
            json.dumps({"paths": [str(SRC)], "location": "custom",
                        "out_dir": bad}).encode())
        text = body.decode("utf-8", "replace")
        check("preflight refuses out_dir {}".format(bad),
              status == 400 and "允许范围" in text, "{} {}".format(status, text[:120]))

    payload = json.dumps({"paths": [r"E:\视频压缩\app\tests\_work\e2e_src.mp4"],
                          "overrides": {"location": "custom",
                                        "out_dir": "C:\\Windows\\Temp\\vcs"}}).encode()
    status, _h, body = client.request("POST", "/api/jobs", payload)
    text = body.decode("utf-8", "replace")
    check("POST /api/jobs refuses an out-of-whitelist out_dir",
          status == 400 and "允许范围" in text, "{} {}".format(status, text[:160]))

    payload = json.dumps({"default_out_dir": "C:\\Windows\\Temp\\vcs-settings"}).encode()
    status, _h, body = client.request("POST", "/api/settings", payload)
    text = body.decode("utf-8", "replace")
    check("POST /api/settings refuses it too",
          status == 403 and "允许范围" in text, "{} {}".format(status, text[:160]))
    check("…and does NOT create the directory it refused",
          not os.path.isdir("C:\\Windows\\Temp\\vcs-settings"))

    print("  -- resource endpoints reject cross-site requests --")
    # Percent-encode the whole path: the request line is ASCII on the wire, and a raw
    # non-ASCII path arrives mojibake'd (which is how an earlier version of this test
    # got a 404 for a file that plainly exists).
    quoted = urllib.parse.quote(str(SRC), safe="")
    status, _h, _b = client.request("GET", "/media?path=" + quoted,
                                    headers={"Sec-Fetch-Site": "cross-site"})
    check("/media refuses Sec-Fetch-Site: cross-site", status == 403, str(status))
    status, _h, _b = client.request("GET", "/media?path=" + quoted,
                                    headers={"Sec-Fetch-Site": "same-site"})
    check("/media refuses same-site too (another port is another trust domain)",
          status == 403, str(status))
    status, _h, _b = client.request("GET", "/media?path=" + quoted,
                                    headers={"Sec-Fetch-Site": "same-origin"})
    check("/media still serves the app's own playback", status in (200, 206), str(status))
    status, _h, _b = client.request("GET", "/media?path=" + quoted,
                                    headers={"Origin": "http://evil.example"})
    check("/media refuses a foreign Origin", status == 403, str(status))
    status, headers, _b = client.request("GET", "/media?path=" + quoted)
    check("media responses carry nosniff",
          headers.get("x-content-type-options") == "nosniff", str(headers))

    print("  -- transport edge cases --")
    status, _h, _b = client.request("HEAD", "/api/frame?path=" + quoted + "&t=1")
    check("HEAD /api/frame is answered (not desynchronised)", status in (200, 400),
          str(status))
    # The real assertion: the socket must not contain a JPEG after the HEAD response.
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        sock.sendall(("HEAD /api/frame?path=" + quoted + "&t=1 HTTP/1.1\r\n"
                      "Host: 127.0.0.1\r\nConnection: close\r\n\r\n").encode("utf-8"))
        data = b""
        sock.settimeout(5)
        while True:
            try:
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            data += chunk
        body_after_headers = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
        check("HEAD /api/frame sends NO body", body_after_headers == b"",
              "{} bytes leaked".format(len(body_after_headers)))
    finally:
        sock.close()

    status, _h, _b = client.raw(
        b"POST /api/jobs/clear HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        b"7\r\n{\"a\":\r\n0\r\n\r\n")[0:3]
    check("a chunked body is refused, not silently read as empty", status == 411,
          str(status))

    status, _h, _b = client.raw(
        b"POST /api/jobs/clear HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Length: 2000000000\r\nConnection: close\r\n\r\n")[0:3]
    check("an oversized Content-Length is refused immediately", status == 413,
          str(status))

    status, _h, body = client.request("OPTIONS", "/api/jobs")
    check("OPTIONS answers JSON, not HTML 501",
          status == 405 and body.lstrip().startswith(b"{"), "{} {}".format(status, body[:60]))
    status, _h, body = client.request("PUT", "/api/jobs")
    check("PUT answers JSON 405 too", status == 405, str(status))

    print("  -- destructive endpoints need an explicit act --")
    for path in ("/api/history/clear", "/api/inbox/clear", "/api/jobs/cancel-all"):
        status, _h, body = client.request("POST", path, b"{}")
        check("{} without confirm: true is refused".format(path), status == 400,
              "{} {}".format(status, body[:100]))
    status, _h, body = client.request(
        "POST", "/api/history/clear", json.dumps({"confirm": True}).encode())
    check("…and accepted with it", status == 200, "{} {}".format(status, body[:120]))

    status, _h, body = client.request(
        "POST", "/api/history/remove",
        json.dumps({"all_matching": True, "query": "", "delete_output": True,
                    "confirm_delete_output": True}).encode())
    check("bulk delete of all outputs needs expected_count", status == 400,
          "{} {}".format(status, body[:120]))

    print("  -- non-hexadecimal job ids are reachable --")
    status, _h, _b = client.request("GET", "/api/jobs/probe00000002/log")
    check("a record like probe00000002 is routed (404 = not found, not 'unknown route')",
          status == 404, str(status))

    print("  -- per-file override submission contract (the 'profile' bug) --")
    # A per-file/group override carries `profile` PLUS Overrides fields (`crf`, ...). The
    # page used to spread the whole object into `overrides`, which put a `profile` KEY
    # inside the overrides dict -- and the unknown-key guard (added to stop silent typos)
    # then rejected the ENTIRE submission with "不认识的参数：profile". Every batch with a
    # per-file or per-group profile failed to queue, while the dropdown showed the choice.
    # These two requests pin both halves of the contract the fix depends on.
    payload = json.dumps({
        "paths": [str(SRC)], "profile": "P2",
        "overrides": {"location": "beside", "out_dir": "", "crf": 51},
    }).encode()
    status, _h, body = client.request("POST", "/api/jobs", payload)
    text = body.decode("utf-8", "replace")
    check("a per-file crf override is accepted (2xx, not a 400 unknown-parameter)",
          status in (200, 201), "{} {}".format(status, text[:160]))
    if status in (200, 201):
        jobs = json.loads(text).get("jobs") or []
        command = (jobs[0].get("command") if jobs else "") or ""
        # libx264 takes the quality target as -crf.
        check("…and it reaches the planned command (-crf 51)", "-crf 51" in command,
              command[-160:])

    # NVENC has no -crf: the same override must map to its own constant-quality flag
    # (-cq). Pinning both mappings keeps a future refactor from "fixing" one and breaking
    # the other.
    payload = json.dumps({
        "paths": [str(SRC)], "profile": "P1",
        "overrides": {"location": "beside", "out_dir": "", "crf": 44},
    }).encode()
    status, _h, body = client.request("POST", "/api/jobs", payload)
    text = body.decode("utf-8", "replace")
    if status in (200, 201):
        jobs = json.loads(text).get("jobs") or []
        command = (jobs[0].get("command") if jobs else "") or ""
        check("NVENC maps the same override to -cq 44", "-cq 44" in command,
              command[-160:])
    else:
        check("NVENC maps the same override to -cq 44", False,
              "{} {}".format(status, text[:120]))

    payload = json.dumps({
        "paths": [str(SRC)], "profile": "P1",
        "overrides": {"location": "beside", "profile": "P1"},
    }).encode()
    status, _h, body = client.request("POST", "/api/jobs", payload)
    text = body.decode("utf-8", "replace")
    check("· `profile` inside overrides is refused, and the message names it",
          status == 400 and "profile" in text,
          "{} {}".format(status, text[:160]))


def main() -> int:
    print("\nhardening self-test\n")
    test_safe_filename()
    test_extra_args_guard()
    test_gop_clamp()
    test_h264_level()
    test_plan_refusals()
    test_audio_peak_basis()
    test_recycle_fail_closed()
    test_history_expected_count()

    httpd = server_mod.create_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print("测试服务器: 127.0.0.1:{}".format(port))
    original = load_settings()
    try:
        test_http(port)
    finally:
        try:
            save_settings(original)
        except Exception as exc:  # noqa: BLE001
            print("  warn 还原 settings 失败: {}".format(exc))
        httpd.shutdown()
        httpd.server_close()

    print()
    print("{} checks passed".format(len(PASSED)))
    if FAILED:
        print("{} FAILED:".format(len(FAILED)))
        for label in FAILED:
            print("  - {}".format(label))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
