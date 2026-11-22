"""Self-test the desktop launcher's "is the running backend stale?" rule.

The desktop shortcut used to reuse whatever server already held the port, with no
check at all -- so after the code changed, double-clicking kept serving the OLD
backend and the user concluded the update had not been applied.  `start.ps1` had
the check; the desktop path did not.

The rule that matters for safety is the second half: a stale backend that still has
jobs in flight must NEVER be killed.  That is a pure decision, so it is pinned here
rather than discovered by interrupting a real encode.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_launcher_stale.py
"""

from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import desktop as D  # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def main() -> int:
    print("\ndesktop launcher staleness self-test\n")

    host = object.__new__(D.DesktopHost)      # no __init__ side effects
    code_mtime = host._code_mtime()
    check("code mtime looks like a real timestamp",
          code_mtime > 1_600_000_000, "%.0f".format(code_mtime))

    print("  -- staleness --")
    old = {"started": code_mtime - 3600, "active_jobs": 0}
    fresh = {"started": code_mtime + 3600, "active_jobs": 0}
    check("a backend older than the code is stale", host._is_stale(old) is True)
    check("a backend newer than the code is not stale", host._is_stale(fresh) is False)
    check("missing started is treated as not-stale (never kill on a guess)",
          host._is_stale({}) is False)
    check("started=0 is treated as not-stale", host._is_stale({"started": 0}) is False)
    check("garbage started is treated as not-stale",
          host._is_stale({"started": "not-a-number"}) is False)
    check("None started is treated as not-stale",
          host._is_stale({"started": None}) is False)

    print("  -- the decision --")
    check("a current backend is reused", host._stale_action(fresh) == "reuse", host._stale_action(fresh))
    check("no info at all means reuse (never kill what we cannot identify)",
          host._stale_action({}) == "reuse")
    check("a stale idle backend is replaced", host._stale_action(old) == "replace",
          host._stale_action(old))
    busy = {"started": code_mtime - 3600, "active_jobs": 3}
    check("a stale BUSY backend is left alone", host._stale_action(busy) == "busy",
          host._stale_action(busy))
    check("one in-flight job is enough to protect it",
          host._stale_action({"started": code_mtime - 3600, "active_jobs": 1}) == "busy")
    check("unknown active jobs protects an old backend",
          host._stale_action({"started": code_mtime - 3600}) == "reuse")
    check("invalid active job count protects an old backend",
          host._stale_action({"started": code_mtime - 3600, "active_jobs": "x"}) == "reuse")

    print("  -- kill guards --")
    check("pid 0 is refused", host._stop_stale(0) is False)
    check("a negative pid is refused", host._stop_stale(-1) is False)
    check("our own pid is refused", host._stop_stale(os.getpid()) is False)
    check("a non-numeric pid is refused", host._stop_stale("abc") is False)
    check("None is refused", host._stop_stale(None) is False)
    check("a pid that does not exist is refused", host._stop_stale(999_999) is False)

    print("  -- server contract --")
    # server.py must actually expose what the launcher reads, or the check silently
    # degrades to "reuse" forever (which is the bug this test exists for).
    server_src = (Path(__file__).resolve().parent.parent / "backend" / "server.py").read_text(
        encoding="utf-8")
    for key in ("pid", "active_jobs", "started", "asset_signature"):
        check("/api/version reports {}".format(key),
              '"{}"'.format(key) in server_src)
    jobs_src = (Path(__file__).resolve().parent.parent / "backend" / "core" / "jobs.py").read_text(
        encoding="utf-8")
    check("JobManager exposes active_count", "def active_count" in jobs_src)
    check("active_count counts queued as well as in-flight work",
          '"queued"' in jobs_src and '"verifying"' in jobs_src)

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
