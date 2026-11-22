"""Self-test the blocking/blur measurements (V5).

The measurement itself is ffmpeg's; what this pins is the honesty layer around it,
because these two filters are the easiest place in the project to over-claim:

* `blockdetect` has **no accepted absolute threshold** -- so a verdict must come from
  the change against the source, never from an absolute band.
* `blurdetect` has only content-dependent empirical anchors, so the same applies, and
  the numbers must be reported alongside the words.
* Both are resolution-dependent, so a source measured at 1280x720 cannot be compared
  with an output measured at 640x360 -- the basis has to be stated.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_artifacts.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import artifacts as A  # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


SAMPLE = """frame:0    pts:0       pts_time:0
lavfi.block=9.876096
lavfi.blur=4.666276
frame:1    pts:512     pts_time:0.0333333
lavfi.block=9.895621
lavfi.blur=4.640182
frame:2    pts:1024    pts_time:0.0666667
lavfi.block=10.5
lavfi.blur=5.1
"""


def main() -> int:
    print("\nblocking / blur measurement self-test\n")

    print("  -- parsing metadata=print output --")
    frames = A.parse_metadata(SAMPLE)
    check("every frame is parsed", len(frames) == 3, "{} frames".format(len(frames)))
    check("frame indices are read", [f.index for f in frames] == [0, 1, 2])
    check("timestamps are read", frames[1].time == 0.033, str(frames[1].time))
    check("block values are read", frames[0].block == 9.876096, str(frames[0].block))
    check("blur values are read", frames[0].blur == 4.666276, str(frames[0].blur))
    check("the parser carries state across lines (both keys per frame)",
          all(f.block > 0 and f.blur > 0 for f in frames),
          str([(f.block, f.blur) for f in frames]))
    check("an empty log yields nothing", A.parse_metadata("") == [])
    check("a header with no metrics still yields a frame",
          len(A.parse_metadata("frame:0 pts:0 pts_time:0")) == 1)
    check("a non-numeric value is skipped, not fatal",
          len(A.parse_metadata("frame:0\nlavfi.block=abc\nlavfi.blur=5.0")) == 1)
    check("an fps fallback fills missing timestamps",
          A.parse_metadata("frame:0\nframe:1\n", fps=25.0)[1].time == 0.04,
          str(A.parse_metadata("frame:0\nframe:1\n", fps=25.0)[1].time))

    print("  -- the verdict is relative, never absolute --")
    same = A.relative_verdict("块效应", 10.0, 10.2, A.BLOCK_NOTABLE, A.BLOCK_ALARMING,
                              "越大越明显")
    check("an unchanged metric reads ok", same["level"] == "ok", str(same))
    notable = A.relative_verdict("块效应", 10.0, 12.0, A.BLOCK_NOTABLE, A.BLOCK_ALARMING,
                                 "越大越明显")
    check("+20% crosses the notable line", notable["level"] == "warn", str(notable))
    alarming = A.relative_verdict("块效应", 10.0, 16.0, A.BLOCK_NOTABLE, A.BLOCK_ALARMING,
                                  "越大越明显")
    check("+60% crosses the alarming line", alarming["level"] == "bad", str(alarming))
    better = A.relative_verdict("块效应", 10.0, 8.0, A.BLOCK_NOTABLE, A.BLOCK_ALARMING,
                                "越大越明显")
    check("a clear improvement is labelled as such", better["level"] == "better",
          str(better))
    check("the detail quotes both numbers and the percentage",
          "源 10.00" in notable["detail"] and "成品 12.00" in notable["detail"]
          and "+20%" in notable["detail"], notable["detail"])
    # A source with no usable reading must not be silently treated as zero, or every
    # output would look infinitely worse.
    missing = A.relative_verdict("块效应", 0.0, 5.0, A.BLOCK_NOTABLE, A.BLOCK_ALARMING,
                                 "越大越明显")
    check("a missing source reading is reported, not compared against zero",
          missing["level"] == "unknown", str(missing))

    print("  -- the thresholds are ours, and labelled as ours --")
    check("block thresholds are stated as empirical constants",
          A.BLOCK_NOTABLE == 0.15 and A.BLOCK_ALARMING == 0.50,
          "{} / {}".format(A.BLOCK_NOTABLE, A.BLOCK_ALARMING))
    check("blur thresholds are stated", A.BLUR_NOTABLE == 0.10 and A.BLUR_ALARMING == 0.30)
    check("the empirical blur anchors are recorded",
          A.BLUR_REFERENCE_SHARP == 4.6 and A.BLUR_REFERENCE_BLURRED == 11.2)

    print("  -- pooling --")
    pooled = A._pool([1.0, 2.0, 3.0])
    check("mean and max are reported", pooled == {"mean": 2.0, "max": 3.0}, str(pooled))
    check("an empty pool yields nothing", A._pool([]) == {})
    check("the serialised scan exposes the worst frames",
          set(A.ArtifactScan().to_dict()) >= {"ok", "pooled", "worst_block", "worst_blur"})

    print("  -- worst-frame selection --")
    spread = [A.FrameArtifacts(index=i, time=float(i), block=float(i), blur=1.0)
              for i in range(10)]
    worst = A.ArtifactScan(frames=spread).worst("block", 3)
    check("the most blocked frames come first, spread out in time",
          [f["time"] for f in worst] == [7.0, 8.0, 9.0], str([f["time"] for f in worst]))
    clustered = [A.FrameArtifacts(index=i, time=1.0 + i * 0.05, block=99.0, blur=1.0)
                 for i in range(6)]
    clustered.append(A.FrameArtifacts(index=9, time=20.0, block=5.0, blur=1.0))
    picked = A.ArtifactScan(frames=clustered).worst("block", 3)
    check("a burst of adjacent frames collapses to one entry",
          len([f for f in picked if f["time"] < 5.0]) == 1, str([f["time"] for f in picked]))

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
