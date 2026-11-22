"""Self-test loudness normalisation (G1).

The feature makes a promise the user cannot check by eye ("now it is -16 LUFS"), so the
tests are built around the two ways that promise can become a lie:

1. **The number must be real.** A normalisation claim is only worth anything if the
   DELIVERED file is measured. `measure_output` uses `ebur128`, an independent meter,
   because `loudnorm` reported -1.0 dBTP on its own output where ebur128 measured
   0.0 dBFS (measured during development). Its parser is pinned here: reading the first
   `I:` in ebur128's output returns a per-frame momentary value (-70 LUFS on ordinary
   audio) instead of the final summary.
2. **A silent mode change must surface.** When the requested gain is impossible under
   the true-peak ceiling, `loudnorm` switches from Linear to Dynamic by itself and
   compresses the dynamics. That has to appear as a warning, not disappear.

Measured anchors used below (ffmpeg 8.0.1): a lavfi `sine` is about -21.8 LUFS, the same
tone at -30 dB is -51.8 LUFS, and the two-pass form lands on the target exactly.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_audio.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from core import audio as A  # noqa: E402
from core.caps import get_caps, subprocess_kwargs  # noqa: E402

WORK = Path(__file__).resolve().parent / "_work"
QUIET = WORK / "g1_quiet.mp4"      # quiet tone + video, needs ~+24 dB
NOAUDIO = WORK / "g1_noaudio.mp4"  # video only

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def _kwargs() -> dict:
    kwargs = dict(subprocess_kwargs())
    for key in ("encoding", "errors", "stdin", "stdout", "stderr"):
        kwargs.pop(key, None)
    return kwargs


def _ffmpeg(args: list) -> bool:
    argv = [get_caps().ffmpeg, "-y", "-hide_banner", "-loglevel", "error"] + args
    try:
        return subprocess.run(argv, capture_output=True, timeout=600, **_kwargs()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def build_fixtures() -> bool:
    """A quiet clip with video+audio, and the same without audio."""
    if not QUIET.is_file():
        ok = _ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=6",
                      "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                      "-af", "volume=-24dB",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                      "-shortest", str(QUIET)])
        if not ok:
            return False
    if not NOAUDIO.is_file():
        ok = _ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=3",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                      "-pix_fmt", "yuv420p", "-an", str(NOAUDIO)])
        if not ok:
            return False
    return True


def main() -> int:
    print("\nloudness normalisation self-test\n")

    print("  -- presets and clamping --")
    ids = [t["id"] for t in A.known_targets()]
    check("the four delivery presets are offered",
          ids == ["-14", "-16", "-18", "-23"], str(ids))
    check("streaming is -14 LUFS / -1 dBTP",
          A.TARGETS["-14"]["i"] == -14.0 and A.TARGETS["-14"]["tp"] == -1.0)
    check("EBU R128 is -23 LUFS", A.TARGETS["-23"]["i"] == -23.0)
    check("0 means off", A.clamp_target(0) == 0.0 and A.clamp_target("0") == 0.0)
    check("an empty/garbage value is off, not a target of 0 LUFS",
          A.clamp_target(None) == 0.0 and A.clamp_target("abc") == 0.0)
    check("clamped at the loud end", A.clamp_target(-1) == A.MAX_TARGET)
    check("clamped at the quiet end", A.clamp_target(-999) == A.MIN_TARGET)
    check("a custom target survives", A.clamp_target(-17.5) == -17.5)
    check("known targets keep their own true-peak ceiling",
          A.effective_tp(-16, None) == -1.0 and A.effective_tp(-18, None) == -1.5)
    check("an explicit ceiling overrides the preset", A.effective_tp(-16, -2.0) == -2.0)
    check("a nonsensical ceiling falls back to the preset", A.effective_tp(-18, 5) == -1.5)
    check("gain is the distance to the target",
          A.gain_db({"integrated": -21.8}, -16) == 5.8,
          str(A.gain_db({"integrated": -21.8}, -16)))

    print("  -- the filter string --")
    two_pass = A.build_filter({"integrated": -21.75, "true_peak": -14.46, "lra": 0.1,
                               "threshold": -31.75, "target_offset": 0.05}, -16)
    check("with measurements it is the linear two-pass form",
          "measured_I=-21.75" in two_pass and "linear=true" in two_pass, two_pass)
    check("all four measured values are passed through",
          all(k in two_pass for k in
              ("measured_I=", "measured_TP=", "measured_LRA=", "measured_thresh=")),
          two_pass)
    check("the target and ceiling are in the filter",
          "I=-16" in two_pass and "TP=-1" in two_pass, two_pass)
    single = A.build_filter(None, -16)
    check("without measurements it degrades to the single-pass form",
          "measured_I" not in single and "linear" not in single, single)
    check("a partial measurement is not half-applied",
          "measured_I" not in A.build_filter({"integrated": -21.0}, -16),
          A.build_filter({"integrated": -21.0}, -16))
    check("an explicit ceiling reaches the filter",
          "TP=-2" in A.build_filter(None, -16, -2.0))

    print("  -- reading back what loudnorm actually did --")
    applied = A.parse_apply(
        "\n[Parsed_loudnorm_0 @ 0x1]\n{\n\t\"output_i\": \"-15.8\","
        "\n\t\"output_tp\": \"-1.5\"\n}\n"
        "Output Integrated:   -15.8 LUFS\n"
        "Output True Peak:     -1.5 dBTP\n"
        "Output LRA:            2.1 LU\n"
        "Normalization Type:   Dynamic\n"
        "Target Offset:        -0.2 LU")
    check("the mode is read back", applied.get("mode") == "Dynamic", str(applied))
    check("the delivered numbers are read back",
          applied.get("output_i") == -15.8 and applied.get("output_lra") == 2.1,
          str(applied))
    check("an empty log yields nothing rather than raising", A.parse_apply("") == {})
    check("a Linear pass raises no warning",
          A.apply_warnings({"mode": "Linear"}, {}, -16.0) == [])
    notes = A.apply_warnings({"mode": "Dynamic"}, {"integrated": -13.0}, -16.0)
    check("a Dynamic fallback is reported as changing the dynamics",
          len(notes) == 1 and "动态" in notes[0] and "Dynamic" in notes[0], str(notes))
    check("the warning says it is not merely a volume change",
          "不是只调了音量" in notes[0], notes[0])

    print("  -- the verdict on the delivered file --")
    delivered = A.Loudness(ok=True, integrated=-16.0, true_peak=-8.8, lra=1.0)
    status, detail = A.verdict(delivered, -16)
    check("on target with headroom: ok", status == "ok", detail)
    check("the detail quotes the measured number and the target",
          "-16.0 LUFS" in detail and "目标 -16.0" in detail, detail)
    off = A.Loudness(ok=True, integrated=-18.4, true_peak=-8.0, lra=1.0)
    status, detail = A.verdict(off, -16)
    check("off target beyond tolerance: warn", status == "warn", detail)
    check("the off-target warning states the shortfall",
          "未落在目标" in detail, detail)
    loud_peak = A.Loudness(ok=True, integrated=-16.0, true_peak=0.0, lra=1.0)
    status, detail = A.verdict(loud_peak, -16, -1.0)
    check("a delivered peak above the ceiling: warn", status == "warn", detail)
    check("the peak warning names the overshoot", "超过真峰值上限" in detail, detail)
    # The measured development case: loudnorm claimed -1.0 dBTP, ebur128 said 0.0.
    check("a small peak overshoot inside tolerance is not flagged",
          A.verdict(A.Loudness(ok=True, integrated=-16.0, true_peak=-1.2, lra=1.0),
                    -16, -1.0)[0] == "ok")
    status, detail = A.verdict(A.Loudness(ok=False, error="没有音轨"), -16)
    check("an unmeasurable delivery is skipped with the reason",
          status == "skipped" and "没有音轨" in detail, detail)

    print("  -- one-line summary for the job warnings --")
    line = A.describe(A.Loudness(ok=True, integrated=-21.8, true_peak=-14.5, lra=0.1), -16)
    check("the summary quotes source, target and gain",
          "-21.8 LUFS" in line and "-16.0 LUFS" in line and "+5.8 dB" in line, line)
    check("the summary says the audio will be re-encoded",
          "重新编码" in line, line)
    line_bad = A.describe(A.Loudness(ok=False, error="无法测量响度（可能没有音轨）"), -16)
    check("an unmeasurable source is described honestly",
          "无法测量响度" in line_bad, line_bad)

    print("  -- measured against real ffmpeg --")
    if not build_fixtures():
        check("需要 ffmpeg 生成测试素材", False, str(QUIET))
    else:
        measured = A.measure(str(QUIET), -16)
        check("the quiet fixture measures as quiet",
              measured.ok and measured.integrated < -30.0,
              "I={} TP={} err={}".format(measured.integrated, measured.true_peak, measured.error))
        check("a source without audio is reported, not crashed",
              A.measure(str(NOAUDIO), -16).ok is False)
        check("measuring twice uses the cache",
              A.measure(str(QUIET), -16).to_dict() == measured.to_dict())

        # THE promise: two-pass to -16 must deliver -16, verified by the other meter.
        two_pass = A.build_filter(measured.to_dict(), -16)
        produced = WORK / "g1_quiet_norm.mp4"
        ok = _ffmpeg(["-i", str(QUIET), "-af", two_pass, "-c:v", "copy",
                      "-c:a", "aac", "-b:a", "192k", str(produced)])
        check("the two-pass filter runs", ok, str(produced))
        if ok:
            out = A.measure_output(str(produced))
            check("the delivered file is measurable", out.ok, out.error)
            check("delivered loudness lands on the target",
                  abs(out.integrated - (-16.0)) <= A.TOLERANCE_LU,
                  "I={} (target -16.0)".format(out.integrated))
            check("the delivered peak respects the ceiling",
                  out.true_peak <= -1.0 + A.PEAK_TOLERANCE_DB,
                  "TP={}".format(out.true_peak))
            check("the independent verdict agrees",
                  A.verdict(out, -16, -1.0)[0] == "ok", A.verdict(out, -16, -1.0)[1])
            # The parser regression: the first `I:` in ebur128's stream is a per-frame
            # momentary value and read -70 LUFS for perfectly normal audio.
            check("the summary value is used, not the first per-frame value",
                  out.integrated > -40.0, "I={}".format(out.integrated))

    A.clear_cache()
    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
