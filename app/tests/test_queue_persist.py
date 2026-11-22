"""Self-test that the queue survives a restart.

Regression test for a bug that made "reopen the app and your queue is still there"
silently false: `Job.to_dict()` adds derived keys (`src_name`, `out_name`) that are
not dataclass fields, so `_load()`'s `Job(**item)` raised TypeError for every record
and the bare `except TypeError: continue` dropped them all.  The file on disk looked
healthy the whole time, which is exactly why this needed a test rather than a look.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_queue_persist.py
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import jobs as J  # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def main() -> int:
    print("\nqueue persistence self-test\n")

    tmp = Path(tempfile.mkdtemp(prefix="vcs-queue-"))
    real_store, real_jobs, real_queue = J.STORE_DIR, J.JOBS_DIR, J.QUEUE_FILE
    J.STORE_DIR = tmp
    J.JOBS_DIR = tmp / "jobs"
    J.QUEUE_FILE = tmp / "queue.json"
    try:
        # -- a real persisted payload, built through the real writer ------------
        manager = J.JobManager()
        manager._jobs["aa11bb22cc33"] = J.Job(
            id="aa11bb22cc33", src=r"E:\videos\a.mp4", profile_id="P3",
            out_path=r"E:\videos\compressed\a_P3_1080p.mp4", status="done",
            src_size=1000, total_size=250, elapsed=12.5, mode="compress",
            verify={"verdict": "passed", "max_keyframe_gap": 1.0, "keyframe_count": 10},
        )
        manager._order.append("aa11bb22cc33")
        manager._jobs["dd44ee55ff66"] = J.Job(
            id="dd44ee55ff66", src=r"E:\videos\b.mp4", profile_id="P5",
            status="verifying", progress=99.9,
        )
        manager._order.append("dd44ee55ff66")
        manager._jobs["1122334455aa"] = J.Job(
            id="1122334455aa", src=r"E:\videos\c.mp4", profile_id="REPAIR",
            status="running", progress=42.0, mode="repair",
        )
        manager._order.append("1122334455aa")
        # A job that had NOT started yet -- this is the record that used to auto-start the
        # moment the app opened (the reported "opening it immediately lags the machine").
        manager._jobs["55aa66bb77cc"] = J.Job(
            id="55aa66bb77cc", src=r"E:\videos\d.mp4", profile_id="A3",
            status="queued", command="ffmpeg -i d.mp4 out.mp4", encoder="libsvtav1",
            est_memory_mb=7050,
        )
        manager._order.append("55aa66bb77cc")
        manager._persist()

        raw = json.loads(J.QUEUE_FILE.read_text(encoding="utf-8"))
        check("queue file written with all jobs", len(raw.get("jobs", [])) == 4,
              "{} records".format(len(raw.get("jobs", []))))
        check("persisted records really do carry derived keys",
              "src_name" in raw["jobs"][0] and "out_name" in raw["jobs"][0],
              ", ".join(sorted(k for k in raw["jobs"][0] if k in ("src_name", "out_name"))))

        # -- restart -----------------------------------------------------------
        revived = J.JobManager()
        restored = revived.list_jobs()
        check("a restart restores every job", len(restored) == 4,
              "{} of 4 restored".format(len(restored)))

        by_id = {j["id"]: j for j in restored}
        done = by_id.get("aa11bb22cc33")
        check("finished job keeps its paths and sizes",
              bool(done) and done["out_path"].endswith("a_P3_1080p.mp4")
              and done["src_size"] == 1000 and done["total_size"] == 250,
              json.dumps(done, ensure_ascii=False)[:120] if done else "missing")
        check("finished job keeps its verification result",
              bool(done) and (done.get("verify") or {}).get("verdict") == "passed")
        check("derived names are regenerated, not required in storage",
              bool(done) and done["src_name"] == "a.mp4" and done["out_name"] == "a_P3_1080p.mp4",
              "{} / {}".format(done.get("src_name"), done.get("out_name")) if done else "")

        verifying = by_id.get("dd44ee55ff66")
        check("an in-flight verification becomes interrupted, not lost",
              bool(verifying) and verifying["status"] == "interrupted"
              and "中断" in (verifying.get("error") or ""),
              (verifying or {}).get("status", "missing"))
        running = by_id.get("1122334455aa")
        check("an in-flight encode becomes interrupted too",
              bool(running) and running["status"] == "interrupted")
        check("repair jobs keep their mode",
              bool(running) and running.get("mode") == "repair",
              (running or {}).get("mode", "missing"))

        # -- opening the app must NOT start encoding ---------------------------
        # Reported from real use: "I open the app and it immediately starts running and the
        # machine becomes very laggy". Jobs still marked `queued` on disk were handed to the
        # dispatcher the moment the server came up. A restored queue is now HELD until the
        # user releases it, with a banner saying how many tasks were restored.
        state = revived.restore_state()
        check("restored queue is held, not auto-started",
              state["held"] is True and state["queued"] >= 1, json.dumps(state))
        check("the dispatcher refuses to hand out a job while held",
              revived._next_queued() is None,
              str(revived._next_queued()))
        released = revived.release_restored()
        check("releasing it (the banner button) lets the queue run",
              released["released"] >= 1 and revived.restore_state()["held"] is False,
              json.dumps(released))
        check("after release the dispatcher DOES offer the job",
              _offers_with_memory(revived))
        check("order is preserved", [j["id"] for j in restored] ==
              ["aa11bb22cc33", "dd44ee55ff66", "1122334455aa", "55aa66bb77cc"],
              str([j["id"] for j in restored]))

        # -- tolerance: an unknown/newer key must not drop the record ----------
        payload = json.loads(J.QUEUE_FILE.read_text(encoding="utf-8"))
        payload["jobs"][0]["a_field_from_the_future"] = 1
        del payload["jobs"][1]["argv"]
        J.QUEUE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tolerant = J.JobManager()
        check("unknown keys are ignored rather than dropping the job",
              len(tolerant.list_jobs()) == 4,
              "{} of 4".format(len(tolerant.list_jobs())))

        # -- junk must not explode, and a valid neighbour must still come back --
        J.QUEUE_FILE.write_text(
            '{"jobs": [3, "x", null, {"id": "zz"}, '
            '{"id": "0badc0de1234", "src": "a.mp4", "profile_id": "P3"}]}',
            encoding="utf-8")
        try:
            junk = J.JobManager()
            restored_junk = junk.list_jobs()
            check("a malformed entry is skipped without raising",
                  [j["id"] for j in restored_junk] == ["0badc0de1234"],
                  "{} restored: {}".format(len(restored_junk),
                                           [j["id"] for j in restored_junk]))
        except Exception as exc:  # noqa: BLE001
            check("a malformed entry is skipped without raising", False, repr(exc))

        J.QUEUE_FILE.write_text("{not json", encoding="utf-8")
        try:
            broken = J.JobManager()
            check("an unreadable queue file yields an empty queue, not a crash",
                  broken.list_jobs() == [])
        except Exception as exc:  # noqa: BLE001
            check("an unreadable queue file yields an empty queue, not a crash", False, repr(exc))
    finally:
        J.STORE_DIR, J.JOBS_DIR, J.QUEUE_FILE = real_store, real_jobs, real_queue
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


def _offers_with_memory(manager) -> bool:
    # Recovery is independent of whatever other programs are consuming RAM today.
    # Memory denial is exercised separately in test_resource_guard.py.
    with patch("core.memory.machine_memory", return_value={
            "available": 1, "free_mb": 16000, "total_mb": 32000}):
        return manager._next_queued() is not None


if __name__ == "__main__":
    sys.exit(main())
