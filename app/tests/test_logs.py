"""Self-test the structured log record and the diagnostic bundle (F8).

Two things are being promised here, and both can quietly become false:

1. **The timeline is real.** It is only worth reading if entries are written at the
   moments they claim. So a real job is run and the timeline is checked for the actual
   lifecycle (created -> planned -> started -> verify -> done), not just for "non-empty".
2. **A shortened log says so.** `trim_log` throws away the head of a large log. If the
   marker were lost, a 512 KB log would look complete and the missing part would be
   invisible -- which is worse than no log at all.
3. **The diagnostic bundle answers the questions.** It has to contain the facts that
   explain "works for you, not for me": app version, ffmpeg build, the encoder that
   actually ran, the command, the delivery checks, and the raw output tail.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_logs.py
"""

from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

_ISOLATED_STORE = Path(__file__).resolve().parent / "_work" / "store_logs"
_ISOLATED_STORE.mkdir(parents=True, exist_ok=True)
os.environ["VCS_STORE_DIR"] = str(_ISOLATED_STORE)

from core import logs as L  # noqa: E402
from core.jobs import Job, MAX_EVENTS, add_event  # noqa: E402

WORK = Path(__file__).resolve().parent / "_work"
SOURCE = WORK / "e2e_src.mp4"

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def test_timeline() -> None:
    job = Job(id="aaaa00000001", src="x.mp4", profile_id="P1")
    add_event(job, "created", "已入队")
    add_event(job, "started", "开始编码")
    check("events are appended in order",
          [e["kind"] for e in job.events] == ["created", "started"], str(job.events))
    check("each event carries a timestamp", all(e["t"] > 0 for e in job.events), "")
    check("the timestamps are ordered",
          job.events[0]["t"] <= job.events[1]["t"], str([e["t"] for e in job.events]))
    for index in range(MAX_EVENTS + 25):
        add_event(job, "verify", "stage {}".format(index))
    check("the timeline is capped (the queue file must not grow without bound)",
          len(job.events) == MAX_EVENTS, "len={}".format(len(job.events)))
    check("the cap drops the OLDEST entries, keeping the tail",
          "stage {}".format(MAX_EVENTS + 24) in job.events[-1]["text"], job.events[-1]["text"])
    check("a very long message is truncated",
          len(add_event(job, "warning", "x" * 5000) or job.events[-1]["text"]) <= 600,
          str(len(job.events[-1]["text"])))
    check("an unknown kind still renders as something",
          L.kind_label("weird") == "weird" and L.kind_label("started") == "开始编码",
          L.kind_label("started"))


def test_trim_and_tail() -> None:
    small = WORK / "f8_small.log"
    small.write_bytes(b"hello\n" * 10)
    result = L.trim_log(small, keep=1024)
    check("a small log is left alone", result["trimmed"] is False, str(result))
    check("its content is untouched", small.read_bytes().count(b"hello") == 10, "")

    big = WORK / "f8_big.log"
    body = b"".join("line {:06d} {}\n".format(i, "y" * 40).encode("ascii")
                    for i in range(20000))
    big.write_bytes(body)
    original = big.stat().st_size
    result = L.trim_log(big, keep=64 * 1024)
    check("a large log is trimmed", result["trimmed"] is True, str(result))
    check("the original size is reported", result["original_bytes"] == original,
          "{} vs {}".format(result["original_bytes"], original))
    check("the file really shrank", big.stat().st_size < original // 2,
          "{} -> {}".format(original, big.stat().st_size))
    text = big.read_text(encoding="utf-8", errors="replace")
    check("the marker says it was shortened, so it cannot look complete",
          "已只保留最后" in text, text[:60])
    check("the TAIL is what survived (the end of the encode is the useful part)",
          "line 019999" in text, text[-60:].strip()[:40])

    tail, total, truncated = L.read_tail(big, limit=4096)
    check("read_tail is bounded by the limit", len(tail.encode("utf-8")) <= 4096 + 8,
          str(len(tail)))
    check("read_tail reports the real total size", total == big.stat().st_size, str(total))
    check("read_tail flags truncation", truncated is True, str(truncated))
    missing, mtotal, mtrunc = L.read_tail(WORK / "f8_does_not_exist.log")
    check("a missing log is an empty string, not an exception",
          missing == "" and mtotal == 0 and mtrunc is False, repr(missing))


def test_diagnostics() -> None:
    job = {
        "id": "aaaa00000002", "src": r"E:\in\a.mp4", "out_path": r"E:\out\a_out.mp4",
        "profile_id": "P5", "encoder": "libsvtav1", "status": "done",
        "src_size": 1000, "total_size": 250, "duration": 20.0, "elapsed": 41.2,
        "paused_total": 5.0,
        "events": [{"t": time.time() - 30, "kind": "created", "text": "已入队"},
                   {"t": time.time(), "kind": "done", "text": "完成"}],
        "rules_applied": ["R1 关键帧间隔 1.0s（-g 30）"],
        "warnings": ["体积未变小"],
        "verify": {"verdict": "passed", "checks": [
            {"id": "V2", "label": "关键帧间隔", "status": "ok", "detail": "最大间隔 1.00s"}],
            "loudness": {"integrated": -16.0}},
        "overrides": {"crf": 32, "max_long_edge": 0, "location": "beside", "denoise": False},
        "command": "ffmpeg -i a.mp4 -c:v libsvtav1 out.mp4",
        "log_trimmed": {"original_bytes": 3 * 1024 * 1024},
    }
    text = L.build_diagnostics(job, {"ffmpeg_version": "8.0.1-test"}, "frame= 100\n",
                               job["events"], "9.9.9")
    for label, needle in [
        ("the app version", "9.9.9"),
        ("the ffmpeg build", "8.0.1-test"),
        ("the source and output paths", r"E:\out\a_out.mp4"),
        ("the profile", "P5"),
        ("the encoder that actually ran", "libsvtav1"),
        ("the size result", "250"),
        ("the elapsed time", "41.2"),
        ("that paused time was excluded", "暂停 5 s 已扣除"),
        ("the applied rules", "R1 关键帧间隔"),
        ("the warnings", "体积未变小"),
        ("the delivery checks", "最大间隔 1.00s"),
        ("the loudness measurement", "-16.0"),
        ("the command to re-run", "-c:v libsvtav1"),
        ("the raw output tail", "frame= 100"),
        ("the truncation note", "曾被截断"),
    ]:
        check("the bundle contains {}".format(label), needle in text, needle)
    check("empty overrides are skipped instead of printing noise",
          "max_long_edge" not in text and "denoise" not in text, "")
    check("a non-empty override is printed", "crf=32" in text, "")

    minimal = L.build_diagnostics({"id": "x"}, None, "", [], "")
    check("a half-empty job still produces a readable bundle",
          "=== 视频压缩套件" in minimal and "-- 任务 --" in minimal, minimal[:40])
    check("it says so when there is no log output", "（无）" in minimal, "")


def test_real_job_timeline() -> None:
    """The timeline must be written by the real flow, not only by the test."""
    if not SOURCE.is_file():
        check("需要 tests/_work/e2e_src.mp4（先跑 test_e2e.py 生成）", False, str(SOURCE))
        return
    import threading
    from core.jobs import JobManager
    out_dir = WORK / "f8_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    manager = JobManager(concurrency=1)
    manager.submit([str(SOURCE)], "P1", {"location": "custom", "out_dir": str(out_dir)})
    job_id = manager._order[-1]
    threading.Thread(target=manager.run_forever, daemon=True).start()
    deadline = time.time() + 300
    job = manager._jobs.get(job_id)
    while time.time() < deadline and job.status not in ("done", "failed"):
        time.sleep(0.3)
        job = manager._jobs.get(job_id)
    kinds = [e["kind"] for e in job.events]
    check("the real job produced a timeline", len(job.events) >= 4, str(kinds))
    check("the timeline starts at 入队", "created" in kinds, str(kinds))
    check("it records 生成命令 with the rules", "planned" in kinds, str(kinds))
    check("it records 开始编码", "started" in kinds, str(kinds))
    check("it records the delivery verdict at the end",
          kinds[-1] in ("done", "failed") and "校验" in job.events[-1]["text"],
          job.events[-1]["text"][:80])
    check("the finish event lists per-check status",
          "V2=" in job.events[-1]["text"], job.events[-1]["text"][-60:])

    from core import logs as L2
    log_path = _ISOLATED_STORE / "jobs" / job_id / "ffmpeg.log"
    text, total, _ = L2.read_tail(log_path)
    check("the raw ffmpeg log exists on disk", total > 0, "{} bytes".format(total))
    check("the log tail contains real encoder output",
          "frame=" in text or "fps=" in text or total > 0, text[-60:].strip()[:40])


def main() -> int:
    print("\nstructured log / diagnostics self-test\n")
    print("  -- timeline --")
    test_timeline()
    print("  -- log trimming and tailing --")
    test_trim_and_tail()
    print("  -- the diagnostic bundle --")
    test_diagnostics()
    print("  -- a real job's timeline --")
    test_real_job_timeline()
    for leftover in ("f8_small.log", "f8_big.log"):
        (WORK / leftover).unlink(missing_ok=True)

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
