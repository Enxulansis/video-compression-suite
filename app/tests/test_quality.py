"""Self-test the quality-score plumbing (F5).

`libvmaf` itself is ffmpeg's job; what this pins is everything around it that can
mislead: the score-to-words mapping, the parser picking the summary line rather than a
per-frame line, and -- most importantly -- the basis text. A VMAF measured against the
source scaled to the output resolution and one measured at native resolution are
different claims, and the UI is required to say which one it is showing.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_quality.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import quality as Q  # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


class FakeVideo:
    def __init__(self, w, h):
        self.width, self.height = w, h


class FakeInfo:
    def __init__(self, w, h):
        self.video = FakeVideo(w, h)


def main() -> int:
    print("\nquality score self-test\n")

    print("  -- score -> plain language --")
    check("99 reads as visually lossless", Q.plain_language(99)["plain"] == "肉眼无损")
    check("the 95 boundary is visually lossless", Q.plain_language(95.0)["plain"] == "肉眼无损")
    check("94.9 drops to the next band", Q.plain_language(94.9)["plain"] == "很好",
          Q.plain_language(94.9)["plain"])
    check("a real measured 93.04 reads as 很好",
          Q.plain_language(93.04)["plain"] == "很好", Q.plain_language(93.04)["plain"])
    check("88 is the top of 良好", Q.plain_language(88)["plain"] == "良好")
    check("87.9 drops to 一般", Q.plain_language(87.9)["plain"] == "一般")
    check("64 reads as 很差", Q.plain_language(64.75)["plain"] == "很差",
          Q.plain_language(64.75)["plain"])
    check("0 does not crash", Q.plain_language(0)["plain"] == "很差")
    check("nonsense input is labelled unknown, not silently lost",
          Q.plain_language("abc")["plain"] == "未知")
    check("None is labelled unknown", Q.plain_language(None)["plain"] == "未知")
    check("every band carries an explanation",
          all(Q.plain_language(t)["explanation"] for t, _, _ in Q.PLAIN_BANDS))
    check("bands are ordered from best to worst",
          all(Q.PLAIN_BANDS[i][0] > Q.PLAIN_BANDS[i + 1][0]
              for i in range(len(Q.PLAIN_BANDS) - 1)))

    print("  -- parsing ffmpeg output --")
    check("a plain summary line parses",
          Q.parse_score("VMAF score: 93.03") == 93.03)
    check("the LAST score wins (per-frame lines come first)",
          Q.parse_score("VMAF score: 10.0\nVMAF score: 95.5") == 95.5)
    check("spacing variants parse",
          Q.parse_score("VMAF score:   88.1234") == 88.1234)
    check("no score yields None", Q.parse_score("nothing here") is None)
    check("an empty log yields None", Q.parse_score("") is None)
    check("a malformed number yields None", Q.parse_score("VMAF score: abc") is None)

    print("  -- basis labelling (the part that must never be omitted) --")
    native = Q.basis_text(FakeInfo(1280, 720), 1280, 720)
    check("a same-resolution comparison says so", "同分辨率" in native, native)
    check("a same-resolution comparison mentions the size", "1280x720" in native, native)
    scaled = Q.basis_text(FakeInfo(3840, 2160), 1280, 720)
    check("a downscaled comparison names both resolutions",
          "3840x2160" in scaled and "1280x720" in scaled, scaled)
    check("a downscaled comparison warns the resolution loss is NOT included",
          "不包含降分辨率" in scaled, scaled)
    check("the two bases are different strings", native != scaled)
    check("a missing source video still produces a basis",
          bool(Q.basis_text(FakeInfo(0, 0), 1280, 720)))

    print("  -- V1: per-frame ranking --")
    frames = [Q.FrameScore(time=t / 10.0, vmaf=v) for t, v in
              [(0, 95.0), (1, 90.0), (2, 88.0), (3, 60.0), (4, 92.0),
               (5, 99.0), (6, 70.0), (7, 91.0)]]
    worst = Q.select_worst(frames, count=3, min_gap=0.15)
    # Greedy from the worst score upward: 0.3 (worst), then 0.6, then 0.2 is rejected
    # (only 0.1 s from 0.3) so 0.1 is taken instead. Distance from the ALREADY KEPT
    # frames is what matters, not rank order.
    check("the worst frames are selected by score",
          [round(f.time, 1) for f in worst] == [0.1, 0.3, 0.6],
          str([round(f.time, 1) for f in worst]))
    check("the selection is returned in chronological order",
          [f.time for f in worst] == sorted(f.time for f in worst))
    # Frames 0.02 s apart are ONE problem, not six: a list of near-identical
    # timestamps is not actionable. The distant frame is still kept.
    clustered = [Q.FrameScore(time=1.0 + i * 0.02, vmaf=50.0 + i) for i in range(6)]
    clustered.append(Q.FrameScore(time=9.0, vmaf=80.0))
    picked = Q.select_worst(clustered, count=3, min_gap=1.0)
    check("a cluster of near-identical timestamps collapses to one entry",
          len([f for f in picked if f.time < 5.0]) == 1, str([f.time for f in picked]))
    check("a far-away frame is still kept alongside the cluster",
          [f.time for f in picked] == [1.0, 9.0], str([f.time for f in picked]))
    check("the count is respected when enough frames are spread out",
          len(Q.select_worst(frames, count=2, min_gap=0.05)) == 2)
    check("an empty input yields an empty selection", Q.select_worst([]) == [])
    check("fewer frames than requested is not an error",
          len(Q.select_worst(frames[:1], count=8)) == 1)

    print("  -- V1: pooling --")
    pooled = Q._pool([90.0, 80.0, 100.0])
    check("min/max/mean are computed", (pooled["min"], pooled["max"]) == (80.0, 100.0)
          and pooled["mean"] == 90.0, str(pooled))
    check("the harmonic mean is reported and is <= the mean",
          pooled["harmonic_mean"] <= pooled["mean"],
          "{} <= {}".format(pooled["harmonic_mean"], pooled["mean"]))
    check("zero scores are ignored rather than poisoning the pool",
          Q._pool([0.0, 90.0])["mean"] == 90.0, str(Q._pool([0.0, 90.0])))
    check("an all-zero input pools to nothing", Q._pool([0.0, 0.0]) == {})

    print("  -- V4: banding bands (CAMBI) --")
    check("a low CAMBI reads as no banding", Q.plain_banding(0.3)["plain"] == "无色带")
    check("4.9 is still no banding (the paper's <5 line)",
          Q.plain_banding(4.9)["plain"] == "无色带", Q.plain_banding(4.9)["plain"])
    check("5.0 crosses into 轻微色带", Q.plain_banding(5.0)["plain"] == "轻微色带")
    check("10.0 crosses into 明显色带", Q.plain_banding(10.0)["plain"] == "明显色带")
    check("20.0 crosses into 严重色带", Q.plain_banding(20.0)["plain"] == "严重色带")
    check("our measured gradient case (22.38) reads as 严重色带",
          Q.plain_banding(22.376)["plain"] == "严重色带")
    check("nonsense is labelled unknown, not silently banded",
          Q.plain_banding("abc")["plain"] == "未知")
    check("every band carries an explanation",
          all(Q.plain_banding(t)["explanation"] for t, _, _ in Q.BANDING_BANDS))
    check("bands are ordered from worst to best",
          all(Q.BANDING_BANDS[i][0] > Q.BANDING_BANDS[i + 1][0]
              for i in range(len(Q.BANDING_BANDS) - 1)))

    print("  -- V4: summarising a scan --")
    # The verdict uses the MAX, not the mean: one badly banded gradient is what a
    # viewer notices, and averaging it over a whole film is how "VMAF 92 but striped"
    # happens.
    frames = [{"time": t, "cambi": c, "vmaf": 95.0} for t, c in
              [(0.0, 0.2), (2.0, 0.3), (4.0, 24.0), (9.0, 22.0), (20.0, 0.4)]]
    summary = Q.summarise_banding([f["cambi"] for f in frames], frames)
    check("the banding verdict follows the worst frame, not the mean",
          summary["plain"] == "严重色带" and summary["worst_score"] == 24.0,
          "mean={} worst={} verdict={}".format(
              summary["pooled"]["mean"], summary["worst_score"], summary["plain"]))
    check("banding hot-spots list only genuinely banded frames",
          [f["time"] for f in summary["worst"]] == [4.0, 9.0],
          str([f["time"] for f in summary["worst"]]))
    check("a clean clip has no banding hot-spots to list",
          Q.summarise_banding([0.1, 0.2, 0.3], [{"time": 1.0, "cambi": 0.2, "vmaf": 99.0},
                                                {"time": 9.0, "cambi": 0.3, "vmaf": 99.0}]
                              )["worst"] == [],
          "near-zero CAMBI frames are noise, not findings")
    check("banding pooling reports no harmonic mean (CAMBI's floor is 0)",
          "harmonic_mean" not in summary["pooled"], str(summary["pooled"]))
    check("a clean scan summarises to no banding",
          Q.summarise_banding([0.1, 0.2, 0.3])["plain"] == "无色带")
    check("an empty scan summarises to nothing",
          Q.summarise_banding([]) == {})
    check("zero CAMBI values are kept (0 is a real reading, unlike VMAF)",
          Q.summarise_banding([0.0, 0.0])["pooled"]["mean"] == 0.0)
    check("the summary carries a dict ready for the API",
          isinstance(Q.BandingResult().to_dict(), dict)
          and "worst_score" in Q.BandingResult().to_dict())

    print("  -- result shape --")
    empty = Q.QualityResult()
    check("a fresh result is not ok and serialises", empty.ok is False
          and isinstance(empty.to_dict(), dict))
    check("the serialised result carries the basis key", "basis" in empty.to_dict())
    check("sampling windows are stated constants",
          len(Q.SAMPLE_FRACTIONS) >= 2 and Q.SAMPLE_SECONDS > 0,
          "{} windows x {}s".format(len(Q.SAMPLE_FRACTIONS), Q.SAMPLE_SECONDS))

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
