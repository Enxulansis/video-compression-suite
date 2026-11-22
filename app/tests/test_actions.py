"""Self-test the completion actions (G8): notify / recycle / power.

Three actions, three different risk levels, so they are tested differently:

* **recycle** is exercised for real on a copy of the test clip, and the test proves the
  file is RECOVERABLE (a matching `$R…` entry appears in the volume's Recycle Bin). A
  test that only asserted "the file is gone" would pass just as happily on a permanent
  delete, which is the one outcome this feature must never produce.
* **the refusal rules** are pure and are tested exhaustively: they are what stands
  between "成功才删源" and someone's irreplaceable original.
* **shutdown is NOT executed.** Scheduling a shutdown on a real machine to test it is
  not an acceptable thing to do to a user, so only the command construction and the
  policy plumbing are asserted. The live path needs one explicit manual run (documented
  in specs/SCOPE.md), and this test says so rather than implying coverage it lacks.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_actions.py
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

_ISOLATED_STORE = Path(__file__).resolve().parent / "_work" / "store_actions"
_ISOLATED_STORE.mkdir(parents=True, exist_ok=True)
os.environ["VCS_STORE_DIR"] = str(_ISOLATED_STORE)

from core import actions as A  # noqa: E402
from core.caps import get_caps, subprocess_kwargs  # noqa: E402
from core.jobs import DEFAULT_ON_SUCCESS, Job, JobManager  # noqa: E402

WORK = Path(__file__).resolve().parent / "_work"
SOURCE = WORK / "e2e_src.mp4"
CLIP = WORK / "g8_clip.mp4"
VICTIM = WORK / "g8_victim.mp4"
OUT_DIR = WORK / "g8_out"

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
    """A deliberately FAT source, so re-encoding it is a genuine size win.

    The completion guard only recycles sources whose delivery PASSED, and the verifier
    counts "output did not get smaller" as `risky`. A small, already-efficient source
    therefore never reaches the recycle path -- which is correct behaviour, and made an
    earlier version of this test fail for the right reason. Near-lossless CRF 10 keeps
    the source large enough that any profile shows a real benefit.
    """
    if CLIP.is_file():
        return True
    if not SOURCE.is_file():
        return False
    argv = [get_caps().ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i",
            str(SOURCE), "-t", "3", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            str(CLIP)]
    try:
        return subprocess.run(argv, capture_output=True, timeout=600, **_kwargs()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def recoverable_in_bin(path: Path, within: float = 180.0) -> int:
    """How many freshly-created $R entries exist on the volume holding `path`.

    This is the difference between "recycled" and "deleted": after a recycle there is a
    new entry in `<volume>\\$Recycle.Bin\\<sid>\\$R*`.
    """
    root = Path(path.resolve().anchor)
    bin_dir = root / "$Recycle.Bin"
    if not bin_dir.is_dir():
        return -1
    now = time.time()
    found = 0
    for child in bin_dir.iterdir():
        try:
            for item in child.glob("$R*"):
                try:
                    if now - item.stat().st_mtime <= within:
                        found += 1
                except OSError:
                    continue
        except (OSError, PermissionError):
            continue
    return found


def test_notify() -> None:
    result = A.notify()
    check("a system notification sound is produced in-process",
          result.get("ok") is True, str(result))
    check("the notification reports which method it used",
          result.get("method") == "system-sound", str(result.get("method")))


def test_refusals() -> None:
    good_src = str(WORK / "somewhere" / "a.mp4")
    good_out = str(WORK / "out" / "a_out.mp4")
    check("a passed job with a distinct output is allowed",
          A.safe_to_recycle(good_src, good_out, "passed") == "", "")
    cases = [
        ("成品未通过校验", A.safe_to_recycle(good_src, good_out, "risky"), "未通过校验"),
        ("成品失败", A.safe_to_recycle(good_src, good_out, "failed"), "未通过校验"),
        ("校验结论为空", A.safe_to_recycle(good_src, good_out, ""), "未通过校验"),
        ("源与成品同一文件", A.safe_to_recycle(good_out, good_out, "passed"), "同一个文件"),
        ("成品路径为空", A.safe_to_recycle(good_src, "", "passed"), "成品路径为空"),
        ("源路径为空", A.safe_to_recycle("", good_out, "passed"), "源路径为空"),
        ("源已不存在", A.safe_to_recycle(good_src, good_out, "passed", exists=False), "不存在"),
        ("源在输出目录内",
         A.safe_to_recycle(str(WORK / "out" / "a.mp4"), good_out, "passed"), "输出目录内"),
    ]
    for label, reason, expect in cases:
        check("refused: {}".format(label), expect in reason, "{} -> {!r}".format(label, reason))

    ok, why = A.recycle_bin_available(str(WORK / "x.mp4"))
    check("this volume does have a Recycle Bin (so the refusals above are not vacuous)",
          ok is True, why)
    ok, why = A.recycle_bin_available(r"\\server\share\x.mp4")
    check("a network path is refused as having no Recycle Bin", ok is False, why)
    ok, why = A.recycle_bin_available("Z:\\nope\\x.mp4")
    check("an unavailable drive is refused", ok is False, why)


def test_recycle_for_real() -> None:
    """Recycle a real file and prove it is recoverable."""
    bin_before = recoverable_in_bin(WORK)
    victim = WORK / "g8_recycle_probe.mp4"
    victim.write_bytes(b"x" * 4096)
    size = victim.stat().st_size
    before = recoverable_in_bin(WORK)
    result = A.recycle(str(victim))
    check("recycling a real file reports success", result.get("ok") is True, str(result))
    check("the file is no longer at its original path", not victim.exists(), str(victim))
    after = recoverable_in_bin(WORK)
    if before < 0:
        check("the Recycle Bin could be enumerated (recoverability check is meaningful)",
              False, "bin not readable")
    else:
        check("a new entry appeared in the Recycle Bin (it is RECOVERABLE, not deleted)",
              after > before, "bin entries {} -> {}".format(before, after))
    check("the recycled content really is a file that still exists somewhere",
          size == 4096 and after > before, "size={} bin={}->{}".format(size, before, after))

    missing = A.recycle(str(WORK / "definitely_not_here.mp4"))
    check("recycling a missing file fails cleanly instead of raising",
          missing.get("ok") is False, str(missing))
    twice = A.recycle(str(victim))
    check("recycling the same path twice fails cleanly", twice.get("ok") is False, str(twice))
    check("the bin grew by exactly one entry for one file",
          after - bin_before == 1, "delta={}".format(after - bin_before))


def test_power_is_not_executed_here() -> None:
    """Command construction only -- nothing here schedules anything."""
    argv = A.power_command("shutdown", 60, "批量完成")
    check("the shutdown command carries a delay and a reason",
          argv[:4] == ["shutdown", "/s", "/t", "60"] and "/c" in argv and "批量完成" in argv,
          " ".join(argv))
    check("the delay is clamped to a safety window",
          A.clamp_delay(1) == A.MIN_POWER_DELAY
          and A.clamp_delay(10 ** 9) == A.MAX_POWER_DELAY
          and A.clamp_delay("x") == A.DEFAULT_POWER_DELAY,
          "{}/{}/{}".format(A.clamp_delay(1), A.clamp_delay(10 ** 9), A.clamp_delay("x")))
    check("only cancellable actions are offered (sleep/hibernate are not)",
          A.POWER_ACTIONS == ("shutdown",), str(A.POWER_ACTIONS))
    check("asking for an unoffered action is refused, not silently mapped",
          A.power_action("sleep").get("ok") is False, str(A.power_action("sleep")))
    check("the cancel command is the documented one",
          A.cancel_command() == ["shutdown", "/a"], str(A.cancel_command()))
    check("the action is described in words for the UI",
          "自动关机" in A.describe_action("shutdown", 60)
          and "可取消" in A.describe_action("shutdown", 60),
          A.describe_action("shutdown", 60))
    check("an unknown action has no description", A.describe_action("sleep") == "")


def test_policy() -> None:
    manager = JobManager(concurrency=1)
    check("every completion action is OFF by default",
          manager.on_success_policy() == DEFAULT_ON_SUCCESS,
          str(manager.on_success_policy()))
    check("an unknown policy key is refused",
          manager.set_on_success({"rm_rf": True}).get("ok") is False,
          str(manager.set_on_success({"rm_rf": True})))
    check("source consent cannot be set globally",
          manager.set_on_success({"recycle_source": True}).get("ok") is False)
    saved = manager.set_on_success({"notify": True, "power": "shutdown", "power_delay": 5})
    check("a valid policy is stored", saved.get("ok") is True, str(saved))
    policy = manager.on_success_policy()
    check("a too-short delay is clamped up", policy["power_delay"] == A.MIN_POWER_DELAY,
          str(policy["power_delay"]))
    check("an unsupported power action falls back to none",
          manager.set_on_success({"power": "hibernate"}).get("on_success", {}).get("power")
          == "none", str(manager.on_success_policy()["power"]))
    check("cancelling with nothing armed is a success, not an error",
          manager.cancel_power().get("ok") is True, str(manager.cancel_power()))
    # The batch actions must run exactly once even when several workers finish together.
    calls = []
    original = manager._run_completion_actions_locked
    manager._run_completion_actions_locked = lambda: (calls.append(1), time.sleep(0.05))
    threads = [threading.Thread(target=manager._run_completion_actions) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    manager._run_completion_actions_locked = original
    check("concurrent finishers run the batch actions only once", len(calls) == 1,
          "ran {} time(s)".format(len(calls)))
    check("nothing is armed when the queue is empty",
          manager._run_completion_actions() is None and manager.power_state() == {},
          str(manager.power_state()))


def test_per_file_recycle() -> None:
    """End to end: a job that PASSES recycles its source; the guard blocks the rest."""
    if not build_clip():
        check("需要 tests/_work/e2e_src.mp4（先跑 test_e2e.py 生成）", False, str(SOURCE))
        return
    shutil.copyfile(CLIP, VICTIM)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manager = JobManager(concurrency=1)
    manager.submit_batch([{'path': str(VICTIM), 'profile': 'P1',
                           'overrides': {"location": "custom", "out_dir": str(OUT_DIR)}}],
                         recycle_sources=True, confirm_recycle=True)
    job_id = manager._order[-1]
    threading.Thread(target=manager.run_forever, daemon=True).start()

    deadline = time.time() + 300
    job = manager._jobs.get(job_id)
    while time.time() < deadline and job.status not in ("done", "failed"):
        time.sleep(0.3)
        job = manager._jobs.get(job_id)
    check("the job finished so the recycle decision could be made",
          job.status == "done", "{} {}".format(job.status, job.error))
    verdict = (job.verify or {}).get("verdict")
    # Quote the checks that are not ok: "risky" alone does not say WHICH gate objected,
    # and the usual culprit (V5, output not smaller) is a fixture problem, not a bug.
    complaints = [(c["id"], c["status"], c["detail"]) for c in
                  (job.verify or {}).get("checks", []) if c["status"] != "ok"]
    check("the delivery passed, which is the precondition", verdict == "passed",
          "verdict={} not-ok={}".format(verdict, complaints))
    # The recycle decision happens AFTER the status flips to `done` (status is set, then
    # the report is written, then the source is recycled), so reading `job.warnings` the
    # instant the status changes is a race: it made this file fail ~1 run in 20 with an
    # empty warning list even though the file WAS recycled a moment later. Wait for the
    # decision to be recorded instead of assuming it already happened.
    recycle_deadline = time.time() + 60
    while time.time() < recycle_deadline:
        if job.source_cleanup in ('recycled', 'kept'):
            break
        time.sleep(0.2)
    check("the source was recycled", not VICTIM.exists(), str(VICTIM))
    check("the recycle is reported on the job with the restore hint",
          any("回收站" in w and "还原" in w for w in job.warnings), str(job.warnings))

    # A separate, explicitly authorized risky job still has to keep its source.
    keeper = WORK / "g8_keeper.mp4"
    shutil.copyfile(CLIP, keeper)
    fake = Job(id="abcdef000001", src=str(keeper), profile_id="P1", status="done",
               out_path=job.out_path, cleanup_batch='risk-batch', recycle_source=True,
               source_cleanup='pending', source_identity=A.file_identity(str(keeper)))
    fake.verify = {"verdict": "risky"}
    manager._jobs[fake.id] = fake
    manager._order.append(fake.id)
    manager._run_source_cleanups()
    check("a RISKY delivery does NOT recycle the source", keeper.exists(), str(keeper))
    check("the refusal explains why", any("校验风险" in w for w in fake.warnings),
          str(fake.warnings))
    keeper.unlink(missing_ok=True)

    # And when the feature is off, nothing is touched even for a passed job.
    shutil.copyfile(CLIP, keeper)
    passed = Job(id="abcdef000002", src=str(keeper), profile_id="P1", status="done")
    passed.verify = {"verdict": "passed"}
    manager._jobs[passed.id] = passed
    manager._order.append(passed.id)
    manager._run_source_cleanups()
    check("with the option off the source is untouched and silent",
          keeper.exists() and passed.warnings == [], str(passed.warnings))
    keeper.unlink(missing_ok=True)

    check("the one-shot consent was consumed by the finished batch",
          job.recycle_source is False and job.source_cleanup == 'recycled',
          str(job.source_cleanup))


def main() -> int:
    print("\ncompletion actions self-test\n")
    print("  -- notify --")
    test_notify()
    print("  -- refusal rules (the guard in front of a destructive action) --")
    test_refusals()
    print("  -- recycle, for real --")
    if A.recycle_bin_available(str(WORK / "x.mp4"))[0]:
        test_recycle_for_real()
    else:
        check("回收站在本机可用（否则跳过真实回收测试）", False,
              A.recycle_bin_available(str(WORK / "x.mp4"))[1])
    print("  -- power (command only: nothing is scheduled) --")
    test_power_is_not_executed_here()
    print("  -- policy --")
    test_policy()
    print("  -- per-file '成功才删源' --")
    test_per_file_recycle()

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
