"""Self-test the per-resource parallel scheduler.

The point of the two-pool design is that NVENC jobs (video engine) and software
encoder jobs (CPU) do not compete, so they must overlap instead of queueing. That
is observable: sample manager.stats() while both kinds of job are in flight and
check that the GPU pool and the CPU pool are busy at the same time.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_parallel.py
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

# Isolate the store BEFORE importing core.jobs (STORE_DIR is resolved at import time),
# so a test run cannot leave records in the user's 历史记录.  See test_e2e.py.
os.environ["VCS_STORE_DIR"] = str(Path(tempfile.mkdtemp(prefix="vcs-parallel-store-")))

from core.caps import get_caps, subprocess_kwargs  # noqa: E402
from core.jobs import get_manager  # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def make_clip(path: Path, seconds: int = 4) -> None:
    caps = get_caps()
    argv = [caps.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration={}".format(seconds),
            "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv420p",
            str(path)]
    import subprocess
    subprocess.run(argv, timeout=300,
                   **{k: v for k, v in subprocess_kwargs().items() if k != "stdin"})


def main() -> int:
    print("\nparallel scheduler self-test\n")
    caps = get_caps()
    if not caps.available():
        print("  FFmpeg unavailable"); return 2

    work = Path(tempfile.mkdtemp(prefix="vcs_par_"))
    try:
        clip = work / "src.mp4"
        make_clip(clip)
        print("  clip ready: {:.1f} MB".format(clip.stat().st_size / 1048576))

        manager = get_manager()
        manager.clear_successful()
        limits = manager.limits()
        print("  pool limits: {}".format(limits))
        check("both pools configured", limits.get("gpu", 0) >= 1 and limits.get("cpu", 0) >= 1,
              str(limits))

        # Force at least 2 GPU slots so overlap is possible even on a 1-slot default.
        manager.set_limits({"gpu": max(2, limits.get("gpu", 2)),
                            "cpu": max(1, limits.get("cpu", 1))})
        print("  effective limits: {}".format(manager.limits()))

        # 2 NVENC jobs (GPU pool) + 1 SVT-AV1 job (CPU pool).
        gpu_jobs = []
        for index in range(2):
            created = manager.submit([str(clip)], "P1",
                                     {"location": "custom", "out_dir": str(work / "out"),
                                      "name_template": "{stem}_gpu" + str(index)})
            gpu_jobs.append(created[0])
        cpu_jobs = manager.submit([str(clip)], "P5",
                                  {"location": "custom", "out_dir": str(work / "out"),
                                   "name_template": "{stem}_cpu"})
        cpu_job = cpu_jobs[0]

        check("GPU jobs classified as gpu", all(j["resource"] == "gpu" for j in gpu_jobs),
              str([j["resource"] for j in gpu_jobs]))
        check("CPU job classified as cpu", cpu_job["resource"] == "cpu", cpu_job["resource"])

        # Watch the pools while the batch runs.
        overlap = False
        peak = {"gpu": 0, "cpu": 0}
        deadline = time.time() + 600
        while time.time() < deadline:
            stats = manager.stats()
            busy = stats.get("busy") or {}
            for key in ("gpu", "cpu"):
                peak[key] = max(peak[key], int(busy.get(key, 0)))
            if int(busy.get("gpu", 0)) >= 1 and int(busy.get("cpu", 0)) >= 1:
                overlap = True
            states = (stats.get("counts") or {})
            active = sum(v for k, v in states.items()
                         if k in ("queued", "running", "verifying"))
            if active == 0:
                break
            time.sleep(0.25)

        print("  peak busy: {}".format(peak))
        check("GPU pool ran more than one job at once", peak["gpu"] >= 2, "peak gpu={}".format(peak["gpu"]))
        check("GPU and CPU pools overlapped", overlap, "peak cpu={}".format(peak["cpu"]))

        final = {j["id"]: j for j in manager.list_jobs()}
        done = [final[j["id"]] for j in gpu_jobs + cpu_jobs if j["id"] in final]
        check("all jobs reached a terminal state",
              all(j["status"] in ("done", "failed") for j in done),
              " | ".join("{}:{}".format(j["profile_id"], j["status"]) for j in done))
        check("no job failed", all(j["status"] == "done" for j in done),
              " | ".join(j.get("error", "")[:60] for j in done if j["status"] != "done"))

        ok = sum(1 for r in results if r)
        print("\n  {}/{} checks passed\n".format(ok, len(results)))
        return 0 if ok == len(results) else 1
    finally:
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
