"""Self-test black-bar detection decisions (G2).

Detection itself needs ffmpeg, but every decision that can silently ruin a video is
pure: how the `crop=w:h:x:y` line is parsed, whether a rectangle is safe to apply,
and whether the samples agree. Those are pinned here.

The bar to clear is asymmetric -- cropping real picture off someone's video is much
worse than leaving bars in -- so most of these checks assert that something is
REFUSED.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_crop.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import crop as C  # noqa: E402

results = []
SRC_W, SRC_H = 3840, 2160


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def main() -> int:
    print("\nblack-bar detection self-test\n")

    print("  -- parsing cropdetect output --")
    log = ("[Parsed_cropdetect_0 @ 0x1] x1:0 x2:3839 y1:139 y2:2019 w:3840 h:1880 "
           "x:0 y:140 pts:100 t:3.3 limit:24.0\n"
           "[Parsed_cropdetect_0 @ 0x1] x1:0 x2:3839 y1:139 y2:2019 w:3840 h:1880 "
           "x:0 y:140 pts:200 t:6.6 limit:24.0\n"
           "crop=3840:1880:0:140\n")
    check("the last crop= line wins (cropdetect converges)", C.parse_cropdetect(log) == (3840, 1880, 0, 140),
          str(C.parse_cropdetect(log)))
    check("output without a crop line yields None", C.parse_cropdetect("nothing here") is None)
    check("an empty string yields None", C.parse_cropdetect("") is None)
    check("a malformed crop line yields None", C.parse_cropdetect("crop=abc:def") is None)

    print("  -- what is accepted --")
    ok, why, box = C.validate(3840, 1880, 0, 140, SRC_W, SRC_H)
    check("a real 2.39:1 letterbox is accepted", ok, why or str(box))
    check("the accepted rectangle is returned even-sized",
          box[0] % 2 == 0 and box[1] % 2 == 0 and box[2] % 2 == 0 and box[3] % 2 == 0, str(box))
    ok2, _, box2 = C.validate(1424, 1080, 248, 0, 1920, 1080)
    check("a real pillarbox is accepted", ok2, str(box2))

    print("  -- what must be refused --")
    ok3, why3, _ = C.validate(3840, 2160, 0, 0, SRC_W, SRC_H)
    check("a full-frame rectangle (no bars) is refused", not ok3, why3)
    ok4, why4, _ = C.validate(3836, 2160, 2, 0, SRC_W, SRC_H)
    check("a 2-pixel 'bar' is refused as noise", not ok4, why4)
    ok5, why5, _ = C.validate(3840, 1200, 0, 480, SRC_W, SRC_H)
    check("removing 40% of the height is refused as greedy", not ok5, why5)
    ok6, why6, _ = C.validate(300, 160, 0, 0, SRC_W, SRC_H)
    check("a rectangle below the minimum keep size is refused", not ok6, why6)
    ok7, why7, _ = C.validate(1424, 1080, 0, 0, 1920, 1080)
    check("an off-centre crop is refused (dark object, not bars)", not ok7, why7)
    ok8, why8, _ = C.validate(3840, 1880, 0, 140, 0, 0)
    check("an unknown source resolution is refused", not ok8, why8)
    # A crop that touches the frame edge is what bars actually look like: x=0 or y=0
    # must NOT trip the centring test.
    ok9, _, _ = C.validate(3840, 1880, 0, 140, SRC_W, SRC_H)
    check("a crop flush to the left/top edge is not treated as off-centre", ok9)

    print("  -- odd values --")
    ok10, _, box10 = C.validate(3839, 1879, 1, 141, SRC_W, SRC_H)
    check("odd values are rounded to even", ok10 and box10 == (3838, 1878, 0, 140), str(box10))
    _, _, clamped = C.validate(9999, 9999, 9999, 9999, SRC_W, SRC_H)
    check("a rectangle larger than the frame is clamped inside it",
          clamped[0] <= SRC_W and clamped[1] <= SRC_H, str(clamped))

    print("  -- sample agreement --")
    agree, share = C.consensus([(3840, 1880, 0, 140), (3840, 1882, 0, 140),
                                (3840, 1878, 0, 142), (3840, 1880, 0, 140)])
    check("small jitter still counts as agreement", share == 1.0 and agree[1] in (1878, 1880, 1882),
          "{} @ {:.0%}".format(agree, share))
    _, split = C.consensus([(3840, 1880, 0, 140), (3000, 1600, 300, 200)])
    check("disagreeing samples report no agreement", split == 0.0, "{:.0%}".format(split))
    empty, share0 = C.consensus([])
    check("no samples yields None", empty is None and share0 == 0.0)
    _, one = C.consensus([(3840, 1880, 0, 140)])
    check("a single sample is full agreement", one == 1.0)

    print("  -- filter composition --")
    chain = C.apply_crop_filter(["scale=1280:720:flags=lanczos"], "3840:1880:0:140")
    check("crop goes FIRST, before scale", chain[0] == "crop=3840:1880:0:140"
          and chain[1].startswith("scale="), ",".join(chain))
    check("cropping before scaling is what preserves the aspect ratio",
          chain.index("crop=3840:1880:0:140") < chain.index("scale=1280:720:flags=lanczos"))
    check("a malformed crop string is ignored rather than passed to ffmpeg",
          C.apply_crop_filter(["bwdif"], "not:a:crop") == ["bwdif"])
    check("an empty crop leaves the chain untouched",
          C.apply_crop_filter(["bwdif"], "") == ["bwdif"])
    check("the existing chain is preserved",
          C.apply_crop_filter(["bwdif", "hqdn3d=4:3:6:4.5"], "1:1:0:0")
          == ["crop=1:1:0:0", "bwdif", "hqdn3d=4:3:6:4.5"])

    print("  -- thresholds --")
    check("the minimum bar is 2%", C.MIN_BAR_FRACTION == 0.02)
    check("the greedy cap is 30%", C.MAX_CROP_FRACTION == 0.30)
    check("the minimum kept size is 320x180",
          (C.MIN_KEEP_WIDTH, C.MIN_KEEP_HEIGHT) == (320, 180))

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
