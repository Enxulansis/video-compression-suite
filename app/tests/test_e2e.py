"""End-to-end smoke test: submit real jobs and check the verifier verdicts.

Run:  D:\\anaconda3\\python.exe tests\\test_e2e.py [profile ...]

It builds a small synthetic source, pushes it through the real queue manager
(ffmpeg subprocess + progress parsing + verifier) and asserts that every output
passes the keyframe-interval gate -- the AV1 seek regression guard.
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

# Isolate the store BEFORE importing core.jobs: STORE_DIR is resolved at import time.
# Without this, every run of this test wrote real records into backend/store/jobs and
# they showed up in the user's 历史记录 as "e2e_src.mp4" clutter (19 of them by the
# time it was noticed).  Tests must never leave traces in the user's archive.
_ISOLATED_STORE = Path(__file__).resolve().parent / "_work" / "store"
_ISOLATED_STORE.mkdir(parents=True, exist_ok=True)
os.environ["VCS_STORE_DIR"] = str(_ISOLATED_STORE)

from core.caps import get_caps, subprocess_kwargs   # noqa: E402
from core.jobs import get_manager                    # noqa: E402
from core.probe import probe                         # noqa: E402
from core.verifier import keyframe_times, _max_gap   # noqa: E402

WORK = Path(__file__).resolve().parent / "_work"
WORK.mkdir(parents=True, exist_ok=True)
SOURCE = WORK / "e2e_src.mp4"


def build_source(seconds: int = 20) -> str:
    if SOURCE.is_file() and SOURCE.stat().st_size > 0:
        return str(SOURCE)
    caps = get_caps()
    argv = [
        caps.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration={}".format(seconds),
        "-f", "lavfi", "-i", "sine=frequency=440:duration={}".format(seconds),
        "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", str(SOURCE),
    ]
    proc = subprocess.run(argv, timeout=600, **subprocess_kwargs())
    assert proc.returncode == 0, proc.stderr
    return str(SOURCE)


def wait_for(manager, job_id: str, timeout: float = 1800) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        job = next((j for j in manager.list_jobs() if j["id"] == job_id), None)
        if job is None:
            raise RuntimeError("job disappeared: " + job_id)
        line = "{} {:.1f}% {}x".format(job["status"], job.get("progress", 0), job.get("speed", 0))
        if line != last:
            print("      -> " + line, flush=True)
            last = line
        if job["status"] in ("done", "failed", "cancelled", "interrupted"):
            return job
        time.sleep(1.0)
    raise TimeoutError(job_id)


def main() -> int:
    profiles = sys.argv[1:] or ["P1", "P3", "P5"]
    caps = get_caps(refresh=True)
    if not caps.available():
        print("FFmpeg 不可用:", caps.error)
        return 2

    src = build_source()
    info = probe(src)
    print("源文件: {}  {}  {:.1f}s  {:.1f} MB".format(
        src, info.summary()["resolution"], info.duration, info.size_bytes / 1048576.0))

    manager = get_manager()
    failures = []
    for pid in profiles:
        print("\n" + "=" * 78)
        print("档位", pid)
        jobs = manager.submit([src], pid, {})
        job = jobs[0]
        if job["status"] == "failed":
            print("  提交即失败:", job["error"])
            failures.append((pid, "submit: " + job["error"]))
            continue
        print("  命令:", job["command"][:220], "…")
        final = wait_for(manager, job["id"])
        print("  状态:", final["status"], "| 输出:",
              "{:.2f} MB".format(final.get("total_size", 0) / 1048576.0),
              "| 用时 {:.1f}s".format(final.get("elapsed", 0)))
        for check in (final.get("verify") or {}).get("checks", []):
            if check["status"] != "skipped":
                print("    [{}] {}: {}".format(check["status"], check["label"], check["detail"]))
        if final["status"] != "done":
            failures.append((pid, final.get("error", "unknown")))
            continue
        verdict = (final.get("verify") or {}).get("verdict")
        gap = (final.get("verify") or {}).get("max_keyframe_gap")
        print("  校验: {} | 最大关键帧间隔 {:.2f}s".format(verdict, gap or 0))
        if gap is None or gap > 2.35:
            failures.append((pid, "关键帧间隔 {}s 超出上限".format(gap)))

    print("\n" + "=" * 78)
    if failures:
        print("失败项:")
        for pid, why in failures:
            print("  {}: {}".format(pid, why))
        return 1
    print("全部通过 ✅  ({} 个档位)".format(len(profiles)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
