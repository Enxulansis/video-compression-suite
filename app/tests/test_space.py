"""Self-test the disk-space guard.

The dangerous bug this prevents is silent: a batch fills the volume, the encode dies
with ENOSPC, and the user is left with a truncated file and a full disk. Nothing about
that is easy to reproduce on demand, so the decision logic is pure and pinned here --
including the projection, which is the part that actually stops a run mid-flight.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_space.py
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import space as S  # noqa: E402

results = []
GB = 1024 ** 3
MB = 1024 ** 2


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def main() -> int:
    print("\ndisk-space guard self-test\n")

    tmp = Path(tempfile.mkdtemp(prefix="vcs-space-"))
    try:
        print("  -- locating the volume --")
        free = S.free_bytes(str(tmp))
        check("free_bytes finds the volume of an existing dir", free > 0,
              "%.1f GB" % (free / GB))
        nested = tmp / "a" / "b" / "c"
        check("free_bytes walks up to an existing ancestor",
              S.free_bytes(str(nested)) == free, "same volume, dir does not exist yet")
        check("free_bytes never invents a number for a bogus drive",
              S.free_bytes(r"Q:\definitely\not\here") == 0)
        check("format_bytes is human readable",
              S.format_bytes(1536) == "1.5 KB" and S.format_bytes(5 * GB) == "5.0 GB",
              "{} / {}".format(S.format_bytes(1536), S.format_bytes(5 * GB)))

        print("  -- advisory evaluation --")
        ev = S.evaluate(str(tmp))
        check("an existing dir reports a known free space", ev["known"] is True)
        check("the message quotes the free space", "可用" in ev["message"] or "仅剩" in ev["message"],
              ev["message"])
        check("level is one of ok/warn/error", ev["level"] in ("ok", "warn", "error"),
              ev["level"])
        check("a missing path still resolves to its volume",
              S.evaluate(str(nested))["known"] is True)

        print("  -- preflight --")
        out = tmp / "out"
        pf = S.preflight(str(out))
        check("preflight creates the output directory", out.is_dir())
        check("preflight reports it writable", pf["writable"] is True)
        check("a healthy preflight has no problems", pf["problems"] == [], str(pf["problems"]))
        bad = S.preflight(str(tmp / "missing-src" / "out"), str(tmp / "nope.mp4"), 100)
        check("preflight flags a missing source file",
              any("源文件不存在" in p for p in bad["problems"]), str(bad["problems"]))
        check("preflight still succeeds on the directory itself", bad["writable"] is True)

        print("  -- projection (the part that stops a run) --")
        check("too early to project", S.project_shortfall(1 * GB, 10 * MB, 5.0) is None)
        check("no bytes written yet means nothing to project",
              S.project_shortfall(1 * GB, 0, 50.0) is None)
        check("plenty of room projects nothing",
              S.project_shortfall(100 * GB, 4 * GB, 50.0) is None)
        check("exactly enough room is fine",
              S.project_shortfall(4 * GB + S.RESERVE_BYTES, 4 * GB, 50.0) is None)

        short = S.project_shortfall(1 * GB, 4 * GB, 50.0)
        check("a shortfall is detected", short is not None)
        if short:
            check("projected total doubles the written bytes at 50%",
                  short["projected_total"] == 8 * GB, str(short["projected_total"]))
            check("remaining is total minus written",
                  short["remaining"] == 4 * GB, str(short["remaining"]))
            check("shortfall accounts for the reserve",
                  short["shortfall"] == 4 * GB + S.RESERVE_BYTES - 1 * GB,
                  str(short["shortfall"]))
            check("the message states written/needed/free",
                  all(token in short["message"] for token in ("已写入", "还需", "可用")),
                  short["message"])
        # A projection that is only just short must still fire: that is the whole
        # point of comparing against the reserve rather than against zero.
        near = S.project_shortfall(4 * GB + S.RESERVE_BYTES - 1, 4 * GB, 50.0)
        check("a one-byte shortfall against the reserve still fires", near is not None)

        # Progress is what gates the projection, so a 9% report must not act on noise.
        noisy = S.project_shortfall(1 * MB, 500 * MB, 9.9)
        check("9.9% progress is not trusted", noisy is None)
        trusted = S.project_shortfall(1 * MB, 500 * MB, 10.0)
        check("10% progress is trusted", trusted is not None)

        print("  -- thresholds --")
        check("the hard floor is a sane size",
              64 * MB < S.MIN_FREE_BYTES < 4 * GB, S.format_bytes(S.MIN_FREE_BYTES))
        check("the reserve is smaller than the floor",
              S.RESERVE_BYTES < S.MIN_FREE_BYTES,
              "{} < {}".format(S.format_bytes(S.RESERVE_BYTES),
                               S.format_bytes(S.MIN_FREE_BYTES)))
        check("the low-free warning line is 10%", S.LOW_FREE_FRACTION == 0.10)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
