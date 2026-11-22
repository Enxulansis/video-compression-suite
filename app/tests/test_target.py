"""Self-test the target-size solver with a synthetic, known bpp curve.

The solver inverts a measured size curve, so it can be validated exactly: feed it a
fake `measure_one` built from a known curve and check it recovers the CRF that
produces the requested size. That is much stronger than eyeballing a real encode,
and it runs in milliseconds.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_target.py
"""

from __future__ import annotations

import io
import math
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import target as T  # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


class FakeInfo:
    duration = 300.0


def make_curve(a: float = -3.0, b: float = -0.12, pixels: int = 1920 * 1080,
               fps: float = 30.0, duration: float = 300.0):
    """size(crf) = pixels*fps*duration/8 * exp(a + b*crf)  -- bytes."""
    def size_of(crf: int) -> int:
        bpp = math.exp(a + b * crf)
        return int(pixels * fps * duration * bpp / 8.0)
    return size_of


def main() -> int:
    print("\ntarget-size solver self-test\n")

    # ---- parse_target ----
    cases = [("200MB", 200 * 1024 ** 2), ("1.5 GB", int(1.5 * 1024 ** 3)),
             ("800m", 800 * 1024 ** 2), ("300000000", 300000000),
             ("700 mb", 700 * 1024 ** 2), ("", 0), ("abc", 0), ("2g", 2 * 1024 ** 3)]
    for text, expected in cases:
        got = T.parse_target(text)
        check("parse_target({!r}) = {}".format(text, T.format_bytes(expected)),
              got == expected, "got {}".format(got))

    # ---- exact inversion ----
    size_of = make_curve()
    desired = size_of(30)
    calls = []

    def measure_one(crf: int) -> int:
        calls.append(crf)
        return size_of(crf)

    result = T.solve_for_target(measure_one, FakeInfo(), 1080, desired, "libsvtav1")
    check("solver returns ok", result.ok, result.note)
    check("recovered the exact CRF", result.crf == 30, "crf={}".format(result.crf))
    check("prediction matches the target", abs(result.predicted_bytes - desired) / desired < 0.01,
          "{} vs {}".format(T.format_bytes(result.predicted_bytes), T.format_bytes(desired)))
    check("used exactly the probe points", len(calls) == len(T.PROBE_CRFS["av1"]),
          "measured at {}".format(calls))
    check("reports a range bracketing the prediction",
          result.low <= result.predicted_bytes <= result.high,
          "{} <= {} <= {}".format(T.format_bytes(result.low),
                                   T.format_bytes(result.predicted_bytes),
                                   T.format_bytes(result.high)))

    # ---- a different target on the same curve ----
    small = size_of(40)
    result2 = T.solve_for_target(measure_one, FakeInfo(), 1080, small, "libsvtav1")
    check("solves a second target too", result2.ok and result2.crf == 40,
          "crf={} (wanted 40)".format(result2.crf))

    # ---- codec-aware bounds: asking for a tiny file in H.264 ----
    tiny = 1024 * 1024  # 1 MB for a 5 minute 1080p clip: impossible
    result3 = T.solve_for_target(measure_one, FakeInfo(), 1080, tiny, "libx264")
    check("clamps to the codec bounds when unreachable", result3.ok and
          result3.crf == T.CRF_BOUNDS["h264"][1], "crf={}".format(result3.crf))
    check("warns that the target is unreachable",
          any("偏小" in w or "难以达到" in w for w in result3.warnings),
          " | ".join(result3.warnings)[:80])

    # ---- huge target: clamps the other way ----
    huge = size_of(30) * 50
    result4 = T.solve_for_target(measure_one, FakeInfo(), 1080, huge, "libsvtav1")
    check("clamps at the high-quality bound for an easy target",
          result4.ok and result4.crf == T.CRF_BOUNDS["av1"][0], "crf={}".format(result4.crf))

    # ---- degenerate inputs must not raise ----
    def broken(crf: int) -> int:
        return 0
    r5 = T.solve_for_target(broken, FakeInfo(), 1080, 100 * 1024 ** 2, "libsvtav1")
    check("all-zero measurements fail cleanly", not r5.ok and bool(r5.warnings),
          " | ".join(r5.warnings))

    def raiser(crf: int) -> int:
        raise RuntimeError("boom")
    r6 = T.solve_for_target(raiser, FakeInfo(), 1080, 100 * 1024 ** 2, "libsvtav1")
    check("raising measurements fail cleanly", not r6.ok and bool(r6.warnings),
          " | ".join(r6.warnings))

    r7 = T.solve_for_target(measure_one, FakeInfo(), 1080, 0, "libsvtav1")
    check("zero target rejected", not r7.ok)

    # ---- overhead: the target is a WHOLE-FILE budget, not a video budget ----
    # Sample encodes run with -an, so without this the solved CRF overshoots the
    # requested size by exactly the audio -- measured at 45% on a real 123 s clip.
    print("\n  -- audio / container overhead --")

    class FakeStream:
        def __init__(self, bit_rate: int) -> None:
            self.bit_rate = bit_rate

    class FakeAudioInfo:
        def __init__(self, duration: float, rates) -> None:
            self.duration = duration
            self.audio_streams = [FakeStream(r) for r in rates]

    audio = FakeAudioInfo(123.433, [317_000])
    copy_bytes = T.audio_bytes(audio, ["-c:a", "copy"])
    check("copy mode uses the source bitrate",
          abs(copy_bytes - 317_000 * 123.433 / 8) < 2, str(copy_bytes))
    check("an explicit -b:a overrides the source bitrate",
          abs(T.audio_bytes(audio, ["-c:a", "aac", "-b:a", "128k"])
              - 128_000 * 123.433 / 8) < 2,
          "{} vs {}".format(T.audio_bytes(audio, ["-c:a", "aac", "-b:a", "128k"]),
                            128_000 * 123.433 / 8))
    check("-an means no audio at all", T.audio_bytes(audio, ["-an"]) == 0)
    check("no audio stream means no audio bytes",
          T.audio_bytes(FakeAudioInfo(100, []), ["-c:a", "copy"]) == 0)
    check("two tracks count twice",
          abs(T.audio_bytes(FakeAudioInfo(100, [100_000, 100_000]), ["-c:a", "copy"])
              - 2 * 100_000 * 100 / 8) < 2)
    check("an unknown source bitrate falls back instead of guessing zero",
          T.audio_bytes(FakeAudioInfo(100, [0]), ["-c:a", "copy"]) > 0)
    check("zero duration yields zero", T.audio_bytes(FakeAudioInfo(0, [317_000]), []) == 0)
    check("container overhead is about 1%",
          abs(T.container_overhead_bytes(100 * 1024 ** 2) - 1024 ** 2 * 1) <= 2,
          str(T.container_overhead_bytes(100 * 1024 ** 2)))
    check("container overhead has a floor for tiny targets",
          T.container_overhead_bytes(0) == 256 * 1024)

    # The solver must aim the VIDEO at (target - overhead) and report the whole file.
    overhead = copy_bytes + T.container_overhead_bytes(11 * 1024 ** 2)
    r_over = T.solve_for_target(measure_one, FakeInfo(), 1080, 11 * 1024 ** 2,
                                "libsvtav1", overhead_bytes=overhead,
                                audio_bytes_value=copy_bytes)
    check("the solved plan accounts for the overhead",
          r_over.ok and r_over.overhead_bytes == overhead and r_over.video_budget_bytes > 0,
          "overhead={} budget={}".format(r_over.overhead_bytes, r_over.video_budget_bytes))
    check("the prediction describes the whole file, not just the video",
          r_over.predicted_bytes > r_over.video_budget_bytes * 0.9,
          "predicted={} target={}".format(r_over.predicted_bytes, r_over.target_bytes))
    check("the note shows the audio/container split",
          "音频" in r_over.note, r_over.note)

    # Solver aims lower when overhead is declared than when it is ignored.
    no_over = T.solve_for_target(measure_one, FakeInfo(), 1080, 11 * 1024 ** 2,
                                 "libsvtav1")
    check("declaring overhead lowers the video budget",
          r_over.video_budget_bytes < no_over.target_bytes,
          "{} < {}".format(r_over.video_budget_bytes, no_over.target_bytes))
    check("a bigger overhead forces a bigger CRF",
          r_over.crf >= no_over.crf, "crf {} vs {}".format(r_over.crf, no_over.crf))

    r_tiny = T.solve_for_target(measure_one, FakeInfo(), 1080, 4 * 1024 ** 2,
                                "libsvtav1", overhead_bytes=5 * 1024 ** 2,
                                audio_bytes_value=5 * 1024 ** 2)
    check("a target smaller than the audio is refused, not silently undershot",
          not r_tiny.ok and any("太小" in w for w in r_tiny.warnings),
          " | ".join(r_tiny.warnings))
    check("the refusal explains that audio alone exceeds the target",
          "音轨" in r_tiny.note or "音频" in r_tiny.note, r_tiny.note)

    # ---- F3: ratio targets and the conservative "must not exceed" mode ----
    print("\n  -- ratio targets + strict mode --")

    source = 100 * 1024 ** 2      # 100 MB
    check("'20%' means 20% of the source",
          T.parse_target("20%", source) == int(source * 0.2))
    check("'5x' means one fifth of the source",
          T.parse_target("5x", source) == int(source / 5))
    check("case and spaces are tolerated",
          T.parse_target(" 2X ", source) == int(source / 2))
    check("a ratio without a source size is refused, not guessed",
          T.parse_target("20%", 0) == 0)
    check("'20%' and '5x' agree (both are one fifth)",
          T.parse_target("20%", source) == T.parse_target("5x", source))
    check("absolute forms still work with a source present",
          T.parse_target("200MB", source) == 200 * 1024 ** 2)
    check("0% is refused with an explanation, not 'needs source size'",
          "大于 0" in T.describe_target("0%", source), T.describe_target("0%", source))
    check("1x is refused as 'same size'", "大于 1" in T.describe_target("1x", source),
          T.describe_target("1x", source))
    check("a ratio explains itself in the UI string",
          "源的 20%" in T.describe_target("20%", source), T.describe_target("20%", source))
    check("a bare number is explained as bytes",
          "按字节解释" in T.describe_target("300000000", source),
          T.describe_target("300000000", source))
    check("nonsense is reported as unrecognised",
          "无法识别" in T.describe_target("abc", source))

    # A curve where the target sits comfortably inside the usable CRF range, so the
    # clamp does not mask the strict-mode difference.
    def mid_curve(crf: int) -> int:
        pixels = 1280 * 720
        return int(pixels * 30 * 120 / 8.0 * math.exp(-1.0 - 0.12 * crf))

    class MidInfo:
        duration = 120.0

        class video:
            width, height, fps = 1280, 720, 30.0

    target = mid_curve(25)
    normal = T.solve_for_target(mid_curve, MidInfo(), 720, target, "libsvtav1")
    strict = T.solve_for_target(mid_curve, MidInfo(), 720, target, "libsvtav1",
                                strict=True)
    check("the ordinary solve lands on the requested size",
          normal.ok and abs(normal.predicted_bytes - target) / target < 0.05,
          "{:.3f}MB vs {:.3f}MB".format(normal.predicted_bytes / 2 ** 20, target / 2 ** 20))
    check("strict mode is marked as such", strict.strict is True and normal.strict is False)
    check("strict mode chooses a coarser CRF than the ordinary solve",
          strict.crf >= normal.crf, "{} vs {}".format(strict.crf, normal.crf))
    check("strict mode's predicted upper bound stays under the target",
          strict.high <= target,
          "high={:.3f}MB target={:.3f}MB".format(strict.high / 2 ** 20, target / 2 ** 20))
    check("strict mode reports the guarantee instead of an 'error'",
          any("全部在目标" in w for w in strict.warnings)
          and not any("预测误差约" in w for w in strict.warnings),
          " | ".join(strict.warnings))
    check("strict mode's note explains the guarantee",
          "不超过目标" in strict.note and "落在目标以下" in strict.note, strict.note)
    check("the ordinary solve's note still quotes the error",
          "误差" in normal.note, normal.note)

    # ---- A1/A2: dual basis and the bpp guard ----
    print("\n  -- dual basis + bpp guard --")

    class FakeVideo:
        def __init__(self, w, h, fps):
            self.width, self.height, self.fps = w, h, fps

    class FakeMedia:
        def __init__(self, w, h, fps, duration):
            self.video = FakeVideo(w, h, fps)
            self.duration = duration

    hd = FakeMedia(1920, 1080, 30.0, 120.0)
    uncompressed = T.uncompressed_bytes(hd)
    expected = 1920 * 1080 * 30 * 120 * 1.5
    check("uncompressed equivalent is W*H*fps*1.5*duration",
          uncompressed == expected, "{} vs {}".format(uncompressed, expected))
    check("uncompressed equivalent is ~10.7 GB for 2 min of 1080p30",
          abs(uncompressed / 1024 ** 3 - 10.43) < 0.05,
          "%.2f GB" % (uncompressed / 1024 ** 3))
    check("a video without fps or duration yields 0",
          T.uncompressed_bytes(FakeMedia(1920, 1080, 0.0, 120.0)) == 0)

    # The headline from the research: one output, two denominators, wildly different
    # numbers. If this ever stops holding, the honesty story has broken.
    three_mbps = int(3_000_000 * 120 / 8)
    ratio_uncompressed = uncompressed / three_mbps
    ratio_vs_lowbitrate = 18_993_000 / 3_000_000
    check("vs uncompressed the ratio looks like 'hundreds of times'",
          200 < ratio_uncompressed < 300, "%.0fx" % ratio_uncompressed)
    check("vs a real source it is a single digit",
          ratio_vs_lowbitrate < 10, "%.1fx" % ratio_vs_lowbitrate)
    check("the two denominators differ by an order of magnitude",
          ratio_uncompressed / ratio_vs_lowbitrate > 20,
          "%.0fx vs %.1fx" % (ratio_uncompressed, ratio_vs_lowbitrate))

    # Cross-check against the published industry figures quoted in
    # _temp/research/超级压缩倍率方法研究.md: 1080p30@10 Mbps = 0.161 bpp,
    # 720p30@5 Mbps = 0.181 bpp. If these drift, the guard's bands are calibrated
    # against the wrong scale.
    check("bpp of a 3 Mbps 1080p30 stream is ~0.048",
          abs(T.bits_per_pixel(three_mbps, hd) - 0.0482) < 0.002,
          "%.4f" % T.bits_per_pixel(three_mbps, hd))
    hd_10mbps = FakeMedia(1920, 1080, 30.0, 120.0)
    check("1080p30 @ 10 Mbps matches the published 0.161 bpp",
          abs(T.bits_per_pixel(int(10_000_000 * 120 / 8), hd_10mbps) - 0.161) < 0.002,
          "%.3f" % T.bits_per_pixel(int(10_000_000 * 120 / 8), hd_10mbps))
    hd720 = FakeMedia(1280, 720, 30.0, 120.0)
    check("720p30 @ 5 Mbps matches the published 0.181 bpp",
          abs(T.bits_per_pixel(int(5_000_000 * 120 / 8), hd720) - 0.181) < 0.002,
          "%.3f" % T.bits_per_pixel(int(5_000_000 * 120 / 8), hd720))
    check("bpp scales linearly with size",
          abs(T.bits_per_pixel(three_mbps * 2, hd)
              - 2 * T.bits_per_pixel(three_mbps, hd)) < 1e-6)

    tiers = [
        (500, "comfortable", False), (200, "comfortable", False),
        (60, "normal", False), (25, "degraded", False),
        (12, "extreme", False), (6, "extreme", False),
        (2, "impossible", True), (0.5, "impossible", True),
    ]
    for mb, want_tier, want_block in tiers:
        r = T.reachability(int(mb * 1024 ** 2), hd)
        check("{:>6.1f} MB -> {} (blocking={})".format(mb, want_tier, want_block),
              r["tier"] == want_tier and T.reachability_is_blocking(r["tier"]) == want_block,
              "got {} bpp={}".format(r["tier"], r["bpp"]))
    check("only the impossible tier blocks the request",
          not any(T.reachability_is_blocking(t)
                  for t in ("comfortable", "normal", "degraded", "extreme", "unknown")))
    check("the message quotes the bpp figure",
          "{:.3f}".format(T.reachability(60 * 1024 ** 2, hd)["bpp"])
          in T.reachability(60 * 1024 ** 2, hd)["message"],
          T.reachability(60 * 1024 ** 2, hd)["message"])
    check("a request without fps/duration is 'unknown', not a crash",
          T.reachability(10 * 1024 ** 2, FakeMedia(1920, 1080, 0.0, 0.0))["tier"] == "unknown")
    check("an impossible target makes the solve refuse rather than guess",
          not T.solve_for_target(measure_one, hd, 1080, 512 * 1024,
                                 "libsvtav1").ok)
    check("the refusal explains the physical limit",
          any("无法保留可辨认" in w for w in
              T.solve_for_target(measure_one, hd, 1080, 512 * 1024,
                                 "libsvtav1").warnings),
          " | ".join(T.solve_for_target(measure_one, hd, 1080, 512 * 1024,
                                        "libsvtav1").warnings)[:120])

    # ---- codec family detection ----
    families = {"libsvtav1": "av1", "av1_nvenc": "av1", "libaom-av1": "av1",
                "libx265": "hevc", "hevc_nvenc": "hevc", "libx264": "h264",
                "h264_nvenc": "h264", "copy": "hevc"}
    for encoder, expected in families.items():
        got = T.codec_family(encoder)
        check("codec_family({}) = {}".format(encoder, expected), got == expected, got)

    ok = sum(1 for r in results if r)
    print("\n  {}/{} checks passed\n".format(ok, len(results)))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
