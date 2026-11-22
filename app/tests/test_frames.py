"""Self-test the 1:1 comparison frame extraction (V2) and the difference map (V6).

Two things here decide whether the comparison is meaningful, and both are pure:

1. **Filter order.** `scale to the common resolution` must come BEFORE `crop`. Crop
   first and the two sides are cut from different-sized pictures, so the panels show
   different content and the user draws a conclusion from two different places.
2. **The crop rectangle.** Zoom means "a centred sub-rectangle of the common
   resolution", and it has to be even-sized (yuv420p) and inside the frame.

The JPEG marker parser is also pinned, because a wrong width/height silently scales
the "1:1" view in the browser and quietly invalidates the whole feature.

For V6 the make-or-break invariant is measured, not asserted by inspection: differencing
a file against ITSELF must come out perfectly black. If the two inputs are not seeked and
paired identically the map lights up and every pixel in it becomes a lie, so the check is
run through real ffmpeg (`YAVG` must be exactly 0).

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_frames.py
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from core import frames as F  # noqa: E402
from core.caps import get_caps, subprocess_kwargs  # noqa: E402

WORK = Path(__file__).resolve().parent / "_work"
SOURCE = WORK / "e2e_src.mp4"
DAMAGED = WORK / "v6_damaged.mp4"       # same content, visibly compressed
SMALLER = WORK / "v6_smaller.mp4"       # different resolution (alignment case)

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def minimal_jpeg(width: int, height: int) -> bytes:
    """A JPEG header with a SOF0 frame of the given size (no image data)."""
    out = bytearray(b"\xff\xd8")                      # SOI
    out += b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\0" + b"\0" * 9
    out += b"\xff\xc0" + (17).to_bytes(2, "big") + bytes([8])
    out += height.to_bytes(2, "big") + width.to_bytes(2, "big")
    out += bytes([3]) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    out += b"\xff\xd9"                                 # EOI
    return bytes(out)


def _clean_kwargs() -> dict:
    """subprocess kwargs for a call that pipes data in and captures output.

    `subprocess_kwargs()` deliberately redirects stdin/stdout/stderr (an ffmpeg that
    reads stdin can hang a batch), and `capture_output`/`input` cannot be combined
    with those -- so drop them and keep only the Windows no-window flags.
    """
    kwargs = dict(subprocess_kwargs())
    for key in ("encoding", "errors", "stdin", "stdout", "stderr"):
        kwargs.pop(key, None)
    return kwargs


def mean_luma(jpeg: bytes) -> float:
    """Average luma of a JPEG, via ffmpeg's signalstats. -1.0 when unavailable."""
    argv = [get_caps().ffmpeg, "-hide_banner", "-v", "info", "-f", "image2pipe",
            "-i", "pipe:0", "-vf", "signalstats,metadata=print", "-frames:v", "1",
            "-f", "null", "-"]
    try:
        proc = subprocess.run(argv, input=jpeg, capture_output=True, timeout=180,
                              **_clean_kwargs())
    except (OSError, subprocess.SubprocessError):
        return -1.0
    match = re.search(r"YAVG=([\d.]+)", proc.stderr.decode("utf-8", "replace"))
    return float(match.group(1)) if match else -1.0


def build_pair() -> bool:
    """A visibly-damaged and a differently-sized version of the sample (cached)."""
    if not SOURCE.is_file():
        return False
    ffmpeg = get_caps().ffmpeg
    kwargs = _clean_kwargs()
    if not DAMAGED.is_file():
        argv = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(SOURCE),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "45",
                "-pix_fmt", "yuv420p", "-g", "30", "-an", str(DAMAGED)]
        if subprocess.run(argv, capture_output=True, timeout=600, **kwargs).returncode:
            return False
    if not SMALLER.is_file():
        argv = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(SOURCE),
                "-vf", "scale=320:180:flags=bilinear", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-g", "30", "-an", str(SMALLER)]
        if subprocess.run(argv, capture_output=True, timeout=600, **kwargs).returncode:
            return False
    return True


def main() -> int:
    print("\n1:1 comparison frame self-test\n")

    print("  -- filter order (the make-or-break detail) --")
    chain = F.build_filters((1280, 720), (640, 360, 320, 180))
    check("scale comes before crop",
          chain.index("scale=") < chain.index("crop="), chain)
    check("the exact chain is scale-then-crop",
          chain == "scale=1280:720:flags=lanczos,crop=640:360:320:180", chain)
    check("scale-only works", F.build_filters((1280, 720)) == "scale=1280:720:flags=lanczos")
    check("crop-only works", F.build_filters(None, (640, 360, 320, 180)) == "crop=640:360:320:180")
    check("neither yields an empty chain", F.build_filters() == "")

    print("  -- crop parsing --")
    check("a valid crop parses", F.parse_crop("640:360:320:180") == (640, 360, 320, 180))
    check("a missing crop is None", F.parse_crop("") is None)
    check("None is None", F.parse_crop(None) is None)
    check("junk is rejected", F.parse_crop("640x360") is None)
    check("a short tuple is rejected", F.parse_crop("640:360:320") is None)
    check("a negative offset still parses (ffmpeg clamps it)",
          F.parse_crop("640:360:-20:0") == (640, 360, -20, 0))
    check("a decimal is rejected", F.parse_crop("640.5:360:0:0") is None)

    print("  -- zoom -> centred rectangle --")
    check("2x on 1280x720 is 640x360 at the centre",
          F.centered_crop(1280, 720, 2.0) == (640, 360, 320, 180),
          str(F.centered_crop(1280, 720, 2.0)))
    check("4x on 1280x720 is 320x180", F.centered_crop(1280, 720, 4.0) == (320, 180, 480, 270))
    check("1x means the whole frame", F.centered_crop(1280, 720, 1.0) is None)
    check("zooms below 1 are treated as no crop", F.centered_crop(1280, 720, 0.5) is None)
    for zoom in (2.0, 4.0, 8.0):
        box = F.centered_crop(1920, 1080, zoom)
        check("{}x is even-sized and inside the frame".format(zoom),
              box is not None and box[0] % 2 == 0 and box[1] % 2 == 0
              and box[0] + box[2] <= 1920 and box[1] + box[3] <= 1080, str(box))
    check("an extreme zoom on a small frame declines rather than producing a sliver",
          F.centered_crop(320, 180, 8.0) is None, str(F.centered_crop(320, 180, 8.0)))
    check("a degenerate size declines", F.centered_crop(0, 0, 2.0) is None)
    # Centring must be symmetric, or the two sides are cropped at different offsets
    # once one of them has odd dimensions.
    box = F.centered_crop(1281, 721, 2.0)
    check("odd frames still centre symmetrically",
          box is not None and abs((1281 - box[0]) - 2 * box[2]) <= 2
          and abs((721 - box[1]) - 2 * box[3]) <= 2, str(box))

    print("  -- JPEG size parsing --")
    check("width/height are read from SOF0",
          F._jpeg_size(minimal_jpeg(640, 360)) == (640, 360),
          str(F._jpeg_size(minimal_jpeg(640, 360))))
    check("a non-square frame is not transposed",
          F._jpeg_size(minimal_jpeg(1920, 1080)) == (1920, 1080),
          str(F._jpeg_size(minimal_jpeg(1920, 1080))))
    check("an empty buffer yields 0x0 rather than raising",
          F._jpeg_size(b"") == (0, 0))
    check("garbage yields 0x0 rather than raising",
          F._jpeg_size(b"not a jpeg at all") == (0, 0))

    print("  -- error surface --")
    check("FrameError is a RuntimeError subclass", issubclass(F.FrameError, RuntimeError))
    check("the binary kwargs drop text mode",
          "encoding" not in F._binary_kwargs() and "errors" not in F._binary_kwargs())
    # Binary stdout with text mode would corrupt the JPEG; the Windows console
    # suppression flags must survive the drop.
    if os.name == "nt":
        check("the no-window flags survive the binary adjustment",
              "creationflags" in F._binary_kwargs() and "startupinfo" in F._binary_kwargs())

    print("  -- V6 difference map: gain, saturation, common size --")
    check("gain defaults to a mild 4x when absent", F.parse_gain(None) == F.DIFF_DEFAULT_GAIN)
    check("gain is clamped to at least 1", F.parse_gain("0") == 1 and F.parse_gain(-9) == 1)
    check("gain is clamped to the maximum", F.parse_gain("9999") == F.DIFF_MAX_GAIN)
    check("junk gain falls back to the default", F.parse_gain("abc") == F.DIFF_DEFAULT_GAIN)
    # The saturation threshold is shown in the UI, so it has to be right.
    check("sat threshold: x1 clips nothing (255)", F.saturation_value(1) == 255)
    check("sat threshold: x8 clips at 32", F.saturation_value(8) == 32)
    check("sat threshold: x4 clips at 64", F.saturation_value(4) == 64)
    check("sat threshold rounds up (x16 -> 16)", F.saturation_value(16) == 16)
    check("common size picks the smaller side (no upscaling)",
          F.common_size((1280, 720), (640, 360)) == (640, 360))
    check("common size stays at the source when the output is larger",
          F.common_size((1280, 720), (3840, 2160)) == (1280, 720))
    check("common size tolerates a missing side",
          F.common_size((1280, 720), (0, 0)) == (1280, 720)
          and F.common_size((0, 0), (640, 360)) == (640, 360))

    print("  -- V6 difference map: the filter chain --")
    chain = F.diff_filters((640, 360), (320, 180, 160, 90), 8,
                           size_a=(1280, 720), size_b=(640, 360))
    check("both sides are normalised with setpts first",
          chain.count("setpts=PTS-STARTPTS") == 2, chain)
    check("the side that needs it is scaled, the side that does not is NOT",
          chain.count("scale=") == 1 and "scale=640:360:flags=bilinear" in chain, chain)
    check("scale uses bilinear, not the 1:1 view's lanczos",
          "flags=bilinear" in chain and "lanczos" not in chain, chain)
    check("crop comes after scale on the same side",
          chain.index("scale=640:360") < chain.index("crop=320:180:160:90"), chain)
    check("the blend mode is an absolute difference",
          "blend=all_mode=difference" in chain, chain)
    check("the comparison is luma-only (no chroma upsampling confound)",
          chain.count("format=gray") == 2, chain)
    check("the gain is spelled out as a clip", "clip(val*8,0,255)" in chain, chain)
    check("the chain ends in the labelled output pad", chain.endswith("[o]"), chain)
    plain = F.diff_filters((640, 360), None, 1)
    check("gain 1 adds no lut at all", "lutyuv" not in plain, plain)
    check("no crop requested means no crop filter", "crop=" not in plain, plain)

    print("  -- V6 difference map: the frame-pairing gate --")
    same = {"fps": 30.0, "duration": 20.0}
    check("identical sides pass", F.pairing_problem(same, same) == "")
    msg = F.pairing_problem({"fps": 30, "frames": 600}, {"fps": 30, "frames": 300})
    check("a frame-count mismatch is refused", "帧数" in msg, msg)
    msg = F.pairing_problem({"fps": 30}, {"fps": 15})
    # framesync does not error here -- it silently pairs the wrong frames, which is why
    # this gate exists at all.
    check("a frame-rate mismatch is refused", "帧率" in msg, msg)
    msg = F.pairing_problem({}, {})
    check("unknown frame rate AND unknown count is refused", msg != "", msg)
    msg = F.pairing_problem({"fps": 30, "duration": 20.0}, {"fps": 30, "duration": 20.5})
    check("a duration gap beyond one frame is refused", "时长" in msg, msg)
    check("a sub-frame duration gap is tolerated",
          F.pairing_problem({"fps": 30, "duration": 20.0},
                            {"fps": 30, "duration": 20.02}) == "")

    print("  -- V6 difference map: measured against real ffmpeg --")
    if not build_pair():
        check("需要 tests/_work/e2e_src.mp4（先跑 test_e2e.py 生成）", False, str(SOURCE))
    else:
        size = (1280, 720)
        # THE invariant of this feature: a file differenced against itself must be
        # black. Anything else means the two inputs were not paired on the same frame,
        # and then every bright pixel in every map is meaningless.
        self_diff = F.difference(str(SOURCE), str(SOURCE), 5, size=size, gain=1)
        check("a JPEG came back", self_diff[:2] == b"\xff\xd8" and len(self_diff) > 500,
              "{} bytes".format(len(self_diff)))
        check("the frame size is the requested common size",
              F._jpeg_size(self_diff) == size, str(F._jpeg_size(self_diff)))
        value = mean_luma(self_diff)
        check("self-diff is EXACTLY black (same frame, same seek, same scale)",
              value == 0.0, "YAVG={}".format(value))
        self_gain = F.difference(str(SOURCE), str(SOURCE), 5, size=size, gain=16)
        check("amplifying black is still black",
              mean_luma(self_gain) == 0.0, "YAVG={}".format(mean_luma(self_gain)))

        raw = F.difference(str(SOURCE), str(DAMAGED), 5, size=size, gain=1)
        raw_value = mean_luma(raw)
        check("a damaged partner produces a non-black map", raw_value > 0.5,
              "YAVG={}".format(raw_value))
        boosted = mean_luma(F.difference(str(SOURCE), str(DAMAGED), 5, size=size, gain=8))
        check("the gain really multiplies the map", boosted > raw_value * 3,
              "x1={} x8={}".format(raw_value, boosted))

        # Resolution mismatch: the common size is 320x180 here, and the SOURCE is the
        # side that must be scaled down. Letting ffmpeg fail (-22) instead of scaling
        # explicitly is the documented safety net, never the intended path.
        small_size = F.common_size((1280, 720), (320, 180))
        check("the common size for a 320x180 partner is 320x180",
              small_size == (320, 180), str(small_size))
        mixed = F.difference(str(SOURCE), str(SMALLER), 5, size=small_size,
                             crop=(160, 90, 80, 45), gain=1,
                             size_a=(1280, 720), size_b=(320, 180))
        check("a differently-sized pair still produces a map (explicit scale)",
              mixed[:2] == b"\xff\xd8", "{} bytes".format(len(mixed)))
        check("the crop is applied at the common resolution",
              F._jpeg_size(mixed) == (160, 90), str(F._jpeg_size(mixed)))

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
