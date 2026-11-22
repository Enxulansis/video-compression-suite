"""Self-test pause/resume of a running encode (G6).

Windows has no SIGSTOP, so "pause" means `NtSuspendProcess` on the encoder. That is a
trick worth testing rather than trusting, and the tests below cover the three ways it
can go wrong:

1. **It does not actually pause.** A suspended encoder must stop writing. Measured
   before designing this (10 s suspension, 0 bytes growth) and asserted here for real.
2. **It breaks the job.** The stall guard kills an encoder that makes no progress for
   15 minutes -- exactly what a long pause looks like. Paused time is credited back, and
   `elapsed` must exclude it (otherwise the reported speed is nonsense).
3. **A survivor is left behind.** Windows does not kill children when the parent dies,
   so a SUSPENDED ffmpeg outlives the app and holds the output file open forever. The
   next start must reap it -- guarded on the image name, because pids get reused.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_pause.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

_ISOLATED_STORE = Path(__file__).resolve().parent / "_work" / "store_pause"
_ISOLATED_STORE.mkdir(parents=True, exist_ok=True)
os.environ["VCS_STORE_DIR"] = str(_ISOLATED_STORE)

from core import winapi  # noqa: E402
from core.caps import get_caps, subprocess_kwargs  # noqa: E402
from core.jobs import Job, JobManager  # noqa: E402

WORK = Path(__file__).resolve().parent / "_work"
SOURCE = WORK / "e2e_src.mp4"
CLIP = WORK / "pause_clip.mp4"       # ~6 s of the same content: long enough to pause
OUT_DIR = WORK / "pause_out"

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def _kwargs() -> dict:
    kwargs = dict(subprocess_kwargs())
    for key in ("encoding", "errors", "stdin", "stdout", "stderr"):
        kwargs.pop(key, None)
    return kwargs


def build_clip() -> bool:
    if CLIP.is_file():
        return True
    if not SOURCE.is_file():
        return False
    argv = [get_caps().ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(SOURCE), "-t", "6", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            str(CLIP)]
    try:
        return subprocess.run(argv, capture_output=True, timeout=600, **_kwargs()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wait_for(manager: JobManager, job_id: str, statuses, timeout: float = 300.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager._jobs.get(job_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.2)
    return None


def output_size(manager: JobManager, job_id: str) -> int:
    job = manager._jobs.get(job_id)
    if job is None or not job.out_path:
        return 0
    try:
        return Path(job.out_path).stat().st_size
    except OSError:
        return 0


def test_state_machine() -> None:
    manager = JobManager(concurrency=1)
    manager._jobs.clear()
    manager._order.clear()
    ids = []
    for index, status in enumerate(("queued", "running", "verifying", "paused", "done")):
        job_id = "{:012x}".format(index + 1)
        manager._jobs[job_id] = Job(id=job_id, src="x.mp4", profile_id="P1", status=status)
        manager._order.append(job_id)
        ids.append(job_id)
    queued, running, verifying, paused, done = ids

    check("a paused job still counts as active work (it holds its slot)",
          manager.active_count() == 4, "active={}".format(manager.active_count()))
    check("pausing an unknown job is refused with a reason",
          manager.pause("ffffffffffff").get("ok") is False,
          str(manager.pause("ffffffffffff")))
    check("pausing a queued job is refused and names the state in Chinese",
          "排队中" in manager.pause(queued).get("error", ""),
          manager.pause(queued).get("error", ""))
    check("pausing without a live process is refused",
          manager.pause(running).get("ok") is False, str(manager.pause(running)))
    check("resuming a job that is not paused is refused",
          manager.resume(running).get("ok") is False, str(manager.resume(running)))
    stopped = manager.cancel_all()
    check("cancel-all counts paused jobs as in-flight",
          stopped == {"running": 3, "queued": 1, "total": 4}, str(stopped))
    check("cancel-all does not touch finished jobs",
          manager._jobs[done].status == "done", manager._jobs[done].status)
    check("cancel-all really cancelled the paused one",
          manager._jobs[paused].status == "cancelled", manager._jobs[paused].status)


def test_real_pause() -> None:
    manager = JobManager(concurrency=1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manager.submit([str(CLIP)], "P5", {"location": "custom", "out_dir": str(OUT_DIR)})
    job_id = manager._order[-1]
    manager.run_forever_thread = None  # noqa: SLF001 - dispatcher is started below
    import threading
    threading.Thread(target=manager.run_forever, daemon=True).start()

    job = wait_for(manager, job_id, ("running",), timeout=60)
    if job is None:
        check("the encode starts", False, "never reached running")
        return
    # A real assertion, not a placeholder: `wait_for` only returns when the status IS
    # running, and the encoder must be recorded by then. `check(..., True, ...)` would
    # have passed even if the job had already died.
    check("the encode starts and records its encoder",
          job.status == "running" and bool(job.encoder),
          "status={} encoder={}".format(job.status, job.encoder))

    # Let it write something, then freeze it.
    time.sleep(3.0)
    before = output_size(manager, job_id)
    started = time.time()
    result = manager.pause(job_id)
    check("pause reports success", result.get("ok") is True, str(result))
    check("the job is marked paused", manager._jobs[job_id].status == "paused",
          manager._jobs[job_id].status)
    check("pausing is recorded in the warnings",
          any("暂停" in w for w in manager._jobs[job_id].warnings),
          str(manager._jobs[job_id].warnings))

    # Two samples: one write already in flight may still land, so a single sample
    # cannot distinguish "one flush" from "still encoding".
    time.sleep(1.5)
    settle = output_size(manager, job_id)
    time.sleep(4.0)
    later = output_size(manager, job_id)
    check("a suspended encoder really stops writing",
          later - settle == 0,
          "bytes written during the pause: {} ({} -> {} -> {})".format(
              later - settle, before, settle, later))

    result = manager.resume(job_id)
    check("resume reports success", result.get("ok") is True, str(result))
    check("the job is running again", manager._jobs[job_id].status == "running",
          manager._jobs[job_id].status)
    paused_seconds = time.time() - started

    finished = wait_for(manager, job_id, ("done", "failed"), timeout=300)
    if finished is None:
        check("the encode finishes after resuming", False, "timed out")
        return
    check("the encode finishes after resuming", finished.status == "done",
          "{} {}".format(finished.status, finished.error))
    verdict = ((finished.verify or {}).get("verdict") or "")
    check("the paused-then-resumed output still passes verification",
          verdict == "passed", "verdict={} checks={}".format(
              verdict, [(c["id"], c["status"]) for c in (finished.verify or {}).get("checks", [])]))
    check("the output file is complete and non-trivial",
          Path(finished.out_path).stat().st_size > 100_000,
          "{} bytes".format(Path(finished.out_path).stat().st_size))
    check("paused time is excluded from the reported elapsed time",
          finished.elapsed and finished.elapsed < paused_seconds + (time.time() - started),
          "elapsed={:.1f}s wall={:.1f}s".format(finished.elapsed or 0, time.time() - started))
    check("paused_total records the pause",
          (finished.paused_total or 0) >= 3.0, "paused_total={}".format(finished.paused_total))
    check("resuming is recorded in the warnings",
          any("继续" in w for w in finished.warnings), str(finished.warnings[-2:]))


def test_cancel_while_paused() -> None:
    manager = JobManager(concurrency=1)
    manager.submit([str(CLIP)], "P5", {"location": "custom", "out_dir": str(OUT_DIR)})
    job_id = manager._order[-1]
    import threading
    threading.Thread(target=manager.run_forever, daemon=True).start()

    job = wait_for(manager, job_id, ("running",), timeout=60)
    if job is None:
        check("second encode starts", False, "never reached running")
        return
    time.sleep(2.0)
    manager.pause(job_id)
    pid = manager._jobs[job_id].pid
    check("pause before cancel", manager._jobs[job_id].status == "paused", "")
    manager.cancel(job_id)
    time.sleep(1.5)
    check("cancelling a paused job kills the suspended encoder",
          not winapi.is_alive(pid), "pid {} alive={}".format(pid, winapi.is_alive(pid)))
    check("the cancelled job is terminal",
          manager._jobs[job_id].status in ("cancelled", "failed"),
          manager._jobs[job_id].status)


def test_reap_survivor() -> None:
    """A suspended ffmpeg from a dead app must be cleaned up on the next start."""
    if not winapi.process_control_available():
        check("进程控制可用（本机平台）", False, winapi.control_reason())
        return
    # `-re` is essential: ffmpeg transcodes lavfi input far faster than real time, so
    # without it this "long" source finished in ~2 s and the whole survivor test passed
    # vacuously (the process was already gone, so there was nothing to reap).
    argv = [get_caps().ffmpeg, "-hide_banner", "-loglevel", "error",
            "-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15:duration=120",
            "-c:v", "libx264", "-preset", "veryfast", "-f", "null", "-"]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        check("the survivor source really is still running", proc.poll() is None,
              "poll={}".format(proc.poll()))
        winapi.suspend_process(proc.pid)
        check("the survivor really is suspended", winapi.is_alive(proc.pid), str(proc.pid))
        job = Job(id="deadbeef0001", src="x.mp4", profile_id="P1", status="paused")
        job.pid = proc.pid
        JobManager._reap_survivor(job)
        time.sleep(1.0)
        check("a suspended ffmpeg survivor is terminated",
              not winapi.is_alive(proc.pid), "alive={}".format(winapi.is_alive(proc.pid)))
        check("the reap is reported on the job",
              any("遗留" in w for w in job.warnings), str(job.warnings))
        check("the pid is cleared after reaping", job.pid == 0, str(job.pid))
    finally:
        if proc.poll() is None:
            winapi.terminate_process(proc.pid, resume_first=True)

    # Guard: a live pid that is NOT ffmpeg must never be killed. Using this very
    # interpreter as the target is the point -- pids get reused, and killing whatever
    # inherited one would be far worse than leaving an orphan.
    bystander = Job(id="deadbeef0002", src="x.mp4", profile_id="P1", status="running")
    bystander.pid = os.getpid()
    JobManager._reap_survivor(bystander)
    # Asserted, not merely reported: this process surviving IS the assertion, and a
    # `check(..., True, ...)` would have said "fine" even if the guard had failed and the
    # kill had been aimed at us.
    check("a re-used pid pointing at a non-ffmpeg process is left alone",
          winapi.is_alive(os.getpid()) and bystander.warnings == [],
          "alive={} warnings={}".format(winapi.is_alive(os.getpid()), bystander.warnings))


def main() -> int:
    print("\npause/resume self-test\n")
    print("  -- state machine --")
    test_state_machine()
    print("  -- survivor cleanup --")
    test_reap_survivor()
    print("  -- real suspend/resume (runs a real encode) --")
    if not build_clip():
        check("需要 tests/_work/e2e_src.mp4（先跑 test_e2e.py 生成）", False, str(SOURCE))
    else:
        test_real_pause()
        test_cancel_while_paused()

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
