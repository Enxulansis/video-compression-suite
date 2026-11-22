"""Size metadata invariants for every profile.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_profile_size.py

Why this file exists
--------------------
`ratio` carried TWO meanings in one field: a 1..6 class code on the P tiers, and the
research's compression MULTIPLE (3, 6, 9, 14, 21, 38, 58) on the A ladder -- while
`estimate_output_bytes` looked it up in the class table. Keys 3 and 6 happened to exist, so
A2..A6 all fell back to 40% and EVERY A rung predicted a bigger file than the balanced P3
(32%). Two user-visible symptoms, one cause:

  * switching between A2 and A6 changed nothing in the estimate panel;
  * "极限压缩阶梯" promised a smaller file than "平衡" and delivered a bigger one.

The browser probe only asserted "the number changed", which that bug satisfied by accident.
So the invariants that make the ladder a LADDER are pinned here, mechanically, together with
a printed table: a failure should explain itself without re-reading the code.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core.planner import (Overrides, estimate_output_bytes, expected_fraction,  # noqa: E402
                          load_profiles, profile_max_edge)
from core.probe import MediaInfo, StreamInfo  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print("  [PASS] {}{}".format(name, " -- " + detail if detail else ""))
    else:
        FAILED.append(name)
        print("  [FAIL] {}{}".format(name, " -- " + detail if detail else ""))


def sample(width: int = 1920, height: int = 1080, seconds: float = 60.0) -> MediaInfo:
    info = MediaInfo(path="E:/sample.mp4", ok=True)
    info.streams.append(StreamInfo(kind="video", index=0, codec="h264", width=width,
                                   height=height, fps=30.0, duration=seconds))
    info.duration = seconds
    info.size_bytes = 100 * 1024 * 1024
    return info


def main() -> int:
    print("\nprofile size metadata self-test\n")
    profiles = load_profiles()
    by_id = {p["id"]: p for p in profiles}
    ov = Overrides(location="beside")
    info = sample()

    print("  -- the ladder as a table (this is the whole point: it must descend) --")
    print("  {:<4} {:<16} {:>10} {:>9}".format("id", "encoder", "est MB", "% of src"))
    rows = {}
    for pid in ["P3", "P1", "P2", "P4", "P5", "P6", "P7", "P8",
                "A0", "A1", "A2", "A3", "A4", "A5", "A6"]:
        profile = by_id.get(pid)
        if not profile:
            continue
        est = estimate_output_bytes(info, profile, ov)
        rows[pid] = est
        print("  {:<4} {:<16} {:>10.1f} {:>8.1f}%".format(
            pid, str(profile.get("encoder"))[:16], est / 1024 / 1024,
            est / info.size_bytes * 100))
    print()

    print("  -- every profile declares a usable size expectation --")
    for profile in profiles:
        frac = expected_fraction(profile)
        check("{} 有可用的 expected_fraction（{:.4f}）".format(profile["id"], frac),
              0.005 <= frac <= 1.0, str(frac))
    check("A 阶梯不再依赖有歧义的 ratio 字段",
          all("ratio" not in p for p in profiles if p["id"].startswith("A")),
          str([p["id"] for p in profiles if p["id"].startswith("A") and "ratio" in p]))
    check("A 阶梯写明自己的压缩倍率（expected_multiple）",
          all(p.get("expected_multiple") for p in profiles if p["id"].startswith("A")),
          str([p["id"] for p in profiles
               if p["id"].startswith("A") and not p.get("expected_multiple")]))

    print("  -- the A ladder must DESCEND, rung by rung --")
    ladder = [pid for pid in ["A0", "A1", "A2", "A3", "A4", "A5", "A6"] if pid in rows]
    for lower, upper in zip(ladder, ladder[1:]):
        check("{} 的预计体积小于 {}（阶梯必须向下）".format(upper, lower),
              rows[upper] < rows[lower],
              "{} -> {}".format(rows[lower], rows[upper]))
    check("每一级都彼此不同（否则选了也没变化）",
          len({rows[pid] for pid in ladder}) == len(ladder),
          str({pid: rows[pid] for pid in ladder}))

    print("  -- 极限阶梯必须比「平衡」更小（用户实测提出的问题） --")
    balanced = rows.get("P3")
    for pid in ladder:
        check("{} 比平衡档 P3 更小".format(pid), rows[pid] < balanced,
              "{} vs {}".format(rows[pid], balanced))

    print("  -- P 档的相对关系也要合理 --")
    check("P8（仅重封装）最大", rows["P8"] == max(rows.values()), str(rows["P8"]))
    check("P1（极速）比 P2 大、比 P3 大", rows["P1"] > rows["P2"] and rows["P1"] > rows["P3"])
    check("P6 是 P 档里最小的", rows["P6"] == min(rows[p] for p in
                                                 ["P1", "P2", "P3", "P4", "P5", "P6", "P7"]))

    print("  -- 分辨率下调只算一次（阶梯倍率本身已含降分辨率） --")
    # A5 caps the long edge at 854. Its fraction is calibrated AT that cap, so for a source
    # already smaller than the cap the estimate must not shrink again.
    small = sample(width=640, height=360)
    for pid in ["A4", "A5", "A6"]:
        profile = by_id[pid]
        est = estimate_output_bytes(small, profile, ov)
        expected = int(small.size_bytes * expected_fraction(profile))
        check("{} 对小源不再二次缩小".format(pid), est == expected,
              "{} vs {}".format(est, expected))
        check("{} 确实声明了分辨率上限".format(pid), profile_max_edge(profile) > 0,
              str(profile_max_edge(profile)))

    print("  -- 用户收紧上限时仍然要变小 --")
    tight = Overrides(location="beside", max_long_edge=640)
    loose = Overrides(location="beside")
    for pid in ["A3", "A4"]:
        profile = by_id[pid]
        check("{} 在用户收紧到 640 后预计更小".format(pid),
              estimate_output_bytes(info, profile, tight)
              < estimate_output_bytes(info, profile, loose),
              "{} vs {}".format(estimate_output_bytes(info, profile, tight),
                                estimate_output_bytes(info, profile, loose)))

    print("\n{} checks passed".format(PASSED))
    if FAILED:
        print("{} FAILED:".format(len(FAILED)))
        for name in FAILED:
            print("  - " + name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
