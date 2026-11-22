"""Self-test the A0-A6 extreme-compression ladder.

Two things are worth pinning about the ladder:

1. **Its honesty.** Every tier must carry an expected ratio band, an expected VMAF
   band, and a plain-language list of what it costs. The tiers that discard
   resolution or frame rate must be flagged for confirmation, and the ones whose
   numbers are extrapolated rather than measured must say so. A ladder that quietly
   loses its caveats is just a marketing table.

2. **That the promise is actually enforced.** A profile named "≤720p" is a lie unless
   `max_edge` reaches the planner and the user cannot raise it; a "15fps" tier must
   also produce 1-second keyframes at 15fps, not at the source's 30.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_ladder.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core.planner import (Overrides, effective_fps, effective_max_edge,
                          get_profile, load_profiles, make_plan,
                          profile_max_edge)  # noqa: E402
from core.probe import MediaInfo, StreamInfo  # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def fake_media(w=3840, h=2160, fps=30.0, duration=120.0) -> MediaInfo:
    video = StreamInfo(index=0, kind="video", codec="h264", width=w, height=h,
                       fps=fps, pix_fmt="yuv420p", bit_rate=20_000_000)
    audio = StreamInfo(index=1, kind="audio", codec="aac", channels=2,
                       sample_rate=48000, bit_rate=128_000)
    return MediaInfo(path=r"E:\fake\movie.mp4", ok=True, size_bytes=100 * 1024 ** 2,
                     duration=duration, format_name="mov,mp4,m4a,3gp,3g2,mj2",
                     bit_rate=6_000_000, streams=[video, audio])


def clip(args, key):
    return args[args.index(key) + 1] if key in args else ""


def main() -> int:
    print("\nextreme-compression ladder self-test\n")

    ladder = [p for p in load_profiles() if p["id"].startswith("A")]
    ids = [p["id"] for p in ladder]
    check("the ladder has exactly A0-A6", ids == ["A0", "A1", "A2", "A3", "A4", "A5", "A6"],
          ",".join(ids))
    check("the ladder is ordered from best quality to smallest size", ids == sorted(ids))

    print("  -- every tier documents its trade-off --")
    for prof in ladder:
        lad = prof.get("ladder")
        check("{} carries ladder metadata".format(prof["id"]), isinstance(lad, dict))
        if not isinstance(lad, dict):
            continue
        check("{} has a ratio band".format(prof["id"]),
              isinstance(lad.get("expected_ratio"), list) and len(lad["expected_ratio"]) == 2
              and lad["expected_ratio"][0] < lad["expected_ratio"][1],
              str(lad.get("expected_ratio")))
        check("{} has a VMAF band".format(prof["id"]),
              isinstance(lad.get("expected_vmaf"), list) and len(lad["expected_vmaf"]) == 2,
              str(lad.get("expected_vmaf")))
        check("{} states what it costs".format(prof["id"]),
              bool(lad.get("costs")), (lad.get("costs") or "")[:60])
        check("{} says whether the numbers are measured".format(prof["id"]),
              isinstance(lad.get("measured"), bool), str(lad.get("measured")))

    print("  -- the bands are monotonic (each step trades quality for size) --")
    ratios = [p["ladder"]["expected_ratio"][0] for p in ladder]
    vmafs = [p["ladder"]["expected_vmaf"][1] for p in ladder]
    check("the ratio floor rises with every tier",
          all(ratios[i] < ratios[i + 1] for i in range(len(ratios) - 1)), str(ratios))
    check("the VMAF ceiling falls with every tier",
          all(vmafs[i] >= vmafs[i + 1] for i in range(len(vmafs) - 1)), str(vmafs))
    check("the top tier promises visually lossless",
          ladder[0]["ladder"]["expected_vmaf"][0] >= 98, str(ladder[0]["ladder"]["expected_vmaf"]))
    check("the bottom tier admits it is barely watchable",
          ladder[-1]["ladder"]["expected_vmaf"][1] <= 60, str(ladder[-1]["ladder"]["expected_vmaf"]))

    print("  -- confirmation is required exactly where picture is thrown away --")
    needs = [p["id"] for p in ladder if p["ladder"]["needs_confirmation"]]
    check("only the tiers that lose resolution/frame rate need confirmation",
          needs == ["A5", "A6"], ",".join(needs))
    for prof in ladder:
        loses_picture = bool(prof.get("max_edge")) or bool(prof.get("fps_cap"))
        if prof["id"] in ("A0", "A1", "A2", "A3"):
            check("{} keeps the source resolution".format(prof["id"]),
                  not prof.get("max_edge") and not prof.get("fps_cap"))
    check("the unmeasured tiers are marked as extrapolated",
          [p["id"] for p in ladder if not p["ladder"]["measured"]] == ["A0", "A6"],
          ",".join(p["id"] for p in ladder if not p["ladder"]["measured"]))

    print("  -- our seekability invariant holds in every tier --")
    for prof in ladder:
        params = (prof.get("extra") or {}).get("svtav1-params", "")
        check("{} forces closed GOP (irefresh-type=2)".format(prof["id"]),
              "irefresh-type=2" in params, params[:60])
        check("{} is 10-bit (helps at low bitrate)".format(prof["id"]),
              prof.get("pix_fmt") == "yuv420p10le", str(prof.get("pix_fmt")))
        check("{} uses AV1".format(prof["id"]), prof.get("encoder") == "libsvtav1")

    print("  -- the resolution promise is enforced by the planner --")
    big = fake_media(3840, 2160, 30.0)
    check("profile_max_edge reads the profile", profile_max_edge(get_profile("A4")) == 1280)
    check("a profile with no ceiling reports 0",
          profile_max_edge(get_profile("A1")) == 0)

    plan_a4 = make_plan(big, get_profile("A4"),
                        Overrides(location="custom", out_dir=r"E:\fake\out"))
    vf_a4 = clip(plan_a4.argv, "-vf")
    check("A4 scales a 4K source down to 1280", "scale=1280:720" in vf_a4, vf_a4)
    check("A4 warns that it is resizing", any("缩放" in w for w in plan_a4.warnings),
          " | ".join(plan_a4.warnings))

    plan_a6 = make_plan(big, get_profile("A6"),
                        Overrides(location="custom", out_dir=r"E:\fake\out"))
    vf_a6 = clip(plan_a6.argv, "-vf")
    check("A6 scales to 640 wide", "scale=640:360" in vf_a6, vf_a6)
    check("A6 drops the frame rate to 15", "fps=15" in vf_a6, vf_a6)
    check("A6's keyframe interval follows the OUTPUT frame rate (1s at 15fps)",
          clip(plan_a6.argv, "-g") == "15", "got -g " + clip(plan_a6.argv, "-g"))
    check("A6's warning says the frame rate was cut",
          any("帧率" in w for w in plan_a6.warnings), " | ".join(plan_a6.warnings))

    plan_a1 = make_plan(big, get_profile("A1"),
                        Overrides(location="custom", out_dir=r"E:\fake\out"))
    check("A1 leaves a 4K source at 4K", clip(plan_a1.argv, "-vf") == "" or
          "scale=" not in clip(plan_a1.argv, "-vf"), clip(plan_a1.argv, "-vf"))
    check("A1's keyframes are 1s at the source frame rate",
          clip(plan_a1.argv, "-g") == "30", clip(plan_a1.argv, "-g"))

    print("  -- the user can tighten the ceiling but never loosen it --")
    tighter = make_plan(big, get_profile("A4"),
                        Overrides(max_long_edge=640, location="custom", out_dir=r"E:\fake\out"))
    check("a smaller user ceiling wins", "scale=640:360" in clip(tighter.argv, "-vf"),
          clip(tighter.argv, "-vf"))
    looser = make_plan(big, get_profile("A4"),
                       Overrides(max_long_edge=1920, location="custom", out_dir=r"E:\fake\out"))
    check("a larger user ceiling cannot break the profile's promise",
          "scale=1280:720" in clip(looser.argv, "-vf"), clip(looser.argv, "-vf"))
    check("effective_max_edge takes the stricter of the two",
          effective_max_edge(get_profile("A4"), Overrides(max_long_edge=1920)) == 1280
          and effective_max_edge(get_profile("A4"), Overrides(max_long_edge=640)) == 640)

    print("  -- A7: the extreme tiers raise their own effort --")
    # Measured basis (this machine, 720p CRF46, source as reference):
    #   preset 6 -> 2 : 体积 -14.3%, 时间 1.8x, VMAF 93.43 -> 94.62
    #   preset 4 -> 2 : 体积  -8.7%, 时间 1.6x, VMAF 94.34 -> 94.62
    # i.e. the raise buys size AND a little quality, and costs only time -- which is the
    # trade an "extreme compression" tier has already accepted.
    def preset_of(plan_argv):
        joined = " ".join(plan_argv)
        index = joined.find("-preset ")
        return joined[index + 8:].split()[0] if index >= 0 else ""

    plan_a5 = make_plan(big, get_profile("A5"),
                        Overrides(location="custom", out_dir=r"E:\fake\out"))
    plan_a6 = make_plan(big, get_profile("A6"),
                        Overrides(location="custom", out_dir=r"E:\fake\out"))
    check("A5 raises its preset to the auto_effort value", preset_of(plan_a5.argv) == "2",
          preset_of(plan_a5.argv))
    check("A6 raises its preset to the auto_effort value", preset_of(plan_a6.argv) == "2",
          preset_of(plan_a6.argv))
    check("the raise is announced with its measured price",
          any("时间" in w and "体积" in w for w in plan_a5.warnings),
          str([w for w in plan_a5.warnings if "精度" in w])[:110])
    check("the raise is recorded in the applied rules",
          any("A7" in r for r in plan_a5.rules_applied), str(plan_a5.rules_applied))
    check("a general tier is left alone",
          preset_of(make_plan(big, get_profile("P5"),
                              Overrides(location="custom", out_dir=r"E:\fake\out")).argv) == "6",
          "P5")
    check("A4 is left alone (not an extreme tier)",
          preset_of(make_plan(big, get_profile("A4"),
                              Overrides(location="custom", out_dir=r"E:\fake\out")).argv) == "4",
          "A4")
    off = make_plan(big, get_profile("A5"),
                    Overrides(effort="off", location="custom", out_dir=r"E:\fake\out"))
    check("effort=off keeps the tier's own preset", preset_of(off.argv) == "4",
          preset_of(off.argv))
    check("effort=off also drops the announcement",
          not any("精度" in w for w in off.warnings), str(off.warnings)[:80])
    explicit = make_plan(big, get_profile("A5"),
                         Overrides(preset="8", location="custom", out_dir=r"E:\fake\out"))
    check("an explicit preset wins over the automatic raise",
          preset_of(explicit.argv) == "8", preset_of(explicit.argv))

    print("  -- small sources are never upscaled --")
    small = fake_media(640, 360, 30.0)
    plan_small = make_plan(small, get_profile("A4"),
                           Overrides(location="custom", out_dir=r"E:\fake\out"))
    check("A4 on a 360p source adds no scale filter",
          "scale=" not in clip(plan_small.argv, "-vf"), clip(plan_small.argv, "-vf"))
    check("effective_fps never raises the frame rate",
          effective_fps(fake_media(640, 360, 10.0), get_profile("A6")) == 10.0,
          str(effective_fps(fake_media(640, 360, 10.0), get_profile("A6"))))
    check("effective_fps caps a faster source",
          effective_fps(fake_media(640, 360, 60.0), get_profile("A6")) == 15.0)

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
