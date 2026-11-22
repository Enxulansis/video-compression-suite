"""Self-test metadata and timestamp preservation (F7) -- and the geometry bugs next to it.

Both halves were found by measuring rather than assuming, and both are the kind of thing
that only shows up on *someone else's* footage:

1. **Rotation.** A portrait phone video is stored landscape with a display matrix. ffmpeg
   autorotates the frames before filtering, but `probe()` reported the STORED size, so the
   planner computed a landscape scale box for a portrait picture. Measured: a 640x360
   source with rotation 90 (shown 360x640) came out **180x100** -- a 1.8:1 frame holding a
   9:16 image. The fix is to do every geometry decision in DISPLAY coordinates.
2. **Encoder minimums.** `h264_nvenc` refuses frames narrower than 145 px (measured:
   128x128, 144x144 and 100x180 all fail; 145x49 works). A resolution cap that landed
   below that produced "Frame Dimension less than the minimum supported value" and a
   failed job with no hint about what to change.
3. **Timestamps.** The container's `creation_time` survives `-map_metadata 0` (measured),
   but the FILE's mtime was the encode time, so a library sorted by date showed freshly
   compressed files as "today". Both are checked on delivery now (V4b).

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_metadata.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from core.caps import get_caps, subprocess_kwargs  # noqa: E402
from core.planner import (Overrides, _ceil_even, _round_up_even,  # noqa: E402
                          compute_scale, encoder_min_dimensions, get_profile, make_plan)
from core.probe import MediaInfo, StreamInfo, probe  # noqa: E402
from core.verifier import VerifyResult, _metadata_check  # noqa: E402

WORK = Path(__file__).resolve().parent / "_work"
SOURCE = WORK / "e2e_src.mp4"
PORTRAIT = WORK / "f7_portrait.mp4"
OUT_DIR = WORK / "f7_out"

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def _kwargs() -> dict:
    kwargs = dict(subprocess_kwargs())
    for key in ("encoding", "errors", "stdin", "stdout", "stderr"):
        kwargs.pop(key, None)
    return kwargs


def _ffmpeg(args: list, timeout: int = 600) -> bool:
    argv = [get_caps().ffmpeg, "-y", "-hide_banner", "-loglevel", "error"] + args
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout,
                              **_kwargs()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


CHAPTERS = WORK / "f7_chapters.txt"


def build_portrait() -> bool:
    """A portrait phone-like clip: stored landscape, displayed portrait, with metadata."""
    if PORTRAIT.is_file():
        return True
    if not SOURCE.is_file():
        return False
    CHAPTERS.write_text(
        ";FFMETADATA1\ntitle=测试影片\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=第一章\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=第二章\n",
        encoding="utf-8")
    landscape = WORK / "f7_landscape.mp4"
    if not _ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-i", str(CHAPTERS), "-map_metadata", "2",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
                    "-pix_fmt", "yuv420p", "-g", "15", "-c:a", "aac", "-b:a", "128k",
                    "-shortest", "-metadata", "creation_time=2019-06-01T10:30:00Z",
                    "-metadata", "comment=原始注释", str(landscape)]):
        return False
    # -display_rotation is an INPUT option; it writes the display matrix. `-map_metadata 0`
    # matters: without it this remux DROPPED creation_time, and the later comparison then
    # passed vacuously by comparing "" with "".
    return _ffmpeg(["-display_rotation", "90", "-i", str(landscape),
                    "-map_metadata", "0", "-c", "copy", str(PORTRAIT)])


def main() -> int:
    print("\nmetadata / timestamp self-test\n")

    print("  -- probe: rotation and display size --")
    plain = MediaInfo(path="x", streams=[StreamInfo(index=0, kind="video", width=640, height=360)])
    rotated = MediaInfo(path="x", streams=[StreamInfo(index=0, kind="video", width=640, height=360,
                                                      rotation=90)])
    turned = MediaInfo(path="x", streams=[StreamInfo(index=0, kind="video", width=640, height=360,
                                                     rotation=-180)])
    check("no rotation keeps the stored size",
          (plain.video.display_width, plain.video.display_height) == (640, 360),
          str((plain.video.display_width, plain.video.display_height)))
    check("90 degrees swaps width and height",
          (rotated.video.display_width, rotated.video.display_height) == (360, 640),
          str((rotated.video.display_width, rotated.video.display_height)))
    check("180 degrees does not swap",
          (turned.video.display_width, turned.video.display_height) == (640, 360),
          str((turned.video.display_width, turned.video.display_height)))
    check("-90 also swaps", StreamInfo(index=0, kind="video", width=1920, height=1080,
                                       rotation=-90).is_rotated is True)
    check("the summary reports display size and rotation",
          rotated.summary()["resolution"] == "360x640"
          and rotated.summary()["rotation"] == 90, str(rotated.summary()["resolution"]))
    check("the coded size is still reported separately",
          rotated.summary()["coded_resolution"] == "640x360",
          rotated.summary()["coded_resolution"])

    print("  -- planner: geometry uses DISPLAY coordinates --")
    ov = Overrides(max_long_edge=180)
    scale = compute_scale(rotated, ov)
    check("a portrait source is scaled as portrait",
          scale is not None and scale[1] > scale[0],
          "{}x{}".format(*(scale or (0, 0))))
    if scale:
        source_ratio = 360 / 640.0
        out_ratio = scale[0] / float(scale[1])
        check("the aspect ratio is preserved (this is the squashing bug)",
              abs(out_ratio - source_ratio) < 0.03,
              "source {:.3f} vs target {:.3f}".format(source_ratio, out_ratio))
    check("the long edge respects the cap",
          scale is not None and max(scale) <= 180, str(scale))
    check("a landscape source still behaves as before",
          compute_scale(plain, Overrides(max_long_edge=180)) == (180, 100),
          str(compute_scale(plain, Overrides(max_long_edge=180))))

    print("  -- planner: encoder minimum dimensions --")
    check("NVENC has a 145x49 floor", encoder_min_dimensions("h264_nvenc") == (145, 49),
          str(encoder_min_dimensions("h264_nvenc")))
    check("software encoders have no such floor",
          encoder_min_dimensions("libx264")[0] < 145
          and encoder_min_dimensions("libsvtav1")[0] < 145,
          str(encoder_min_dimensions("libsvtav1")))
    # The helper that broke the first attempt: it FLOORS, and using it to raise a value
    # produced 144 for a minimum of 145 -- one pixel short, and the encode failed anyway.
    check("_round_up_even floors (used for caps)", _round_up_even(145) == 144)
    check("_ceil_even raises (used for minimums)", _ceil_even(145) == 146)
    check("_ceil_even stays put on an even value", _ceil_even(144) == 144)
    check("_ceil_even rounds a fraction up", _ceil_even(100.2) == 102)

    print("  -- V4b: the metadata check itself --")
    src_info = MediaInfo(path="s", mtime=time.time() - 86400)
    src_info.tags = {"creation_time": "2019-06-01T10:30:00Z"}
    src_info.chapters = [{"id": 0}, {"id": 1}]
    out_ok = MediaInfo(path="o")
    out_ok.tags = dict(src_info.tags)
    out_ok.chapters = list(src_info.chapters)
    out_file = WORK / "f7_stat_probe.mp4"
    out_file.write_bytes(b"x")
    os.utime(str(out_file), (src_info.mtime, src_info.mtime))

    result = VerifyResult()
    _metadata_check(result, out_file, src_info, out_ok, True)
    entry = result.checks[-1]
    check("a preserved delivery reports ok", entry.status == "ok", "{}: {}".format(
        entry.status, entry.detail))
    check("the detail names the shooting time and the chapter count",
          "2019-06-01" in entry.detail and "章节 2" in entry.detail, entry.detail)
    check("the file-timestamp copy is confirmed", "文件时间已保留" in entry.detail, entry.detail)

    out_lost = MediaInfo(path="o")
    result2 = VerifyResult()
    _metadata_check(result2, out_file, src_info, out_lost, False)
    check("lost metadata is a warning, not a silent pass",
          result2.checks[-1].status == "warn", result2.checks[-1].detail)
    check("the warning says what was lost",
          "拍摄时间丢失" in result2.checks[-1].detail, result2.checks[-1].detail)
    result3 = VerifyResult()
    _metadata_check(result3, out_file, src_info, out_ok, False)
    check("timestamps are not judged when the feature is off",
          result3.checks[-1].status == "ok" and "文件时间" not in result3.checks[-1].detail,
          result3.checks[-1].detail)

    print("  -- real encode of a portrait source --")
    if not build_portrait():
        check("需要 tests/_work/e2e_src.mp4（先跑 test_e2e.py 生成）", False, str(SOURCE))
    else:
        info = probe(str(PORTRAIT))
        check("the fixture really is rotated", info.video.rotation == 90,
              "rotation={}".format(info.video.rotation))
        check("the fixture carries chapters and a shooting time",
              len(info.chapters) == 2 and bool(info.creation_time),
              "chapters={} creation={}".format(len(info.chapters), info.creation_time))
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        old = time.mktime((2019, 6, 1, 10, 30, 0, 0, 0, -1))
        os.utime(str(PORTRAIT), (old, old))
        info = probe(str(PORTRAIT))          # probed AFTER the mtime is set
        plan = make_plan(info, get_profile("P5"),
                         Overrides(location="custom", out_dir=str(OUT_DIR),
                                   max_long_edge=180), exists_probe=os.path.isfile)
        joined = " ".join(plan.argv)
        # Software encoders have no minimum-size floor, so the cap is honoured exactly.
        check("a software tier keeps the requested cap (no clamping)",
              "scale=100:180" in joined and "最小尺寸" not in " ".join(plan.warnings),
              " ".join(plan.warnings)[:80])
        # The clamp itself, at plan level (cheap, no encode): the same portrait source on
        # an NVENC tier cannot be 100 px wide, so it must be raised to the 145 floor.
        clamped = make_plan(info, get_profile("P1"),
                            Overrides(location="custom", out_dir=str(OUT_DIR),
                                      max_long_edge=180), exists_probe=os.path.isfile)
        clamped_joined = " ".join(clamped.argv)
        check("an NVENC tier raises a too-small cap to the encoder floor",
              "scale=146:262" in clamped_joined
              or "scale=145:262" in clamped_joined, clamped_joined[-70:])
        check("and it says why", any("最小尺寸" in w for w in clamped.warnings),
              str([w for w in clamped.warnings if "最小" in w])[:110])
        ok = _ffmpeg(plan.argv[1:])
        check("the encode succeeds", ok, str(plan.out_path))
        if ok:
            delivered = probe(plan.out_path)
            ratio = delivered.video.display_width / float(delivered.video.display_height)
            check("the delivered video is still portrait (not squashed)",
                  ratio < 1.0 and abs(ratio - 360 / 640.0) < 0.05,
                  "{}x{} ratio={:.3f}".format(delivered.video.display_width,
                                              delivered.video.display_height, ratio))
            # Non-vacuous: the source must HAVE a creation_time for this to mean anything.
            check("the source really has a shooting time to preserve",
                  bool(info.creation_time), repr(info.creation_time))
            check("creation_time survived the encode",
                  bool(info.creation_time) and delivered.creation_time == info.creation_time,
                  "{!r} vs {!r}".format(delivered.creation_time, info.creation_time))
            check("chapters survived the encode",
                  len(delivered.chapters) == len(info.chapters) == 2,
                  "{} vs {}".format(len(delivered.chapters), len(info.chapters)))

            # The file-time copy lives in the job manager, so exercise THAT (a bare ffmpeg
            # run never copies timestamps -- asserting on one tested nothing).
            from core.jobs import Job, JobManager
            manager = JobManager(concurrency=1)
            job = Job(id="f7timestamps1", src=str(PORTRAIT), profile_id="P5",
                      out_path=plan.out_path, overrides={"keep_timestamps": True})
            os.utime(plan.out_path, (time.time(), time.time()))
            manager._copy_timestamps(job)
            check("the job manager copies the source's file time onto the output",
                  abs(Path(plan.out_path).stat().st_mtime - old) < 2.0,
                  "{:.0f} vs {:.0f}".format(Path(plan.out_path).stat().st_mtime, old))
            check("a successful copy adds no warning", job.warnings == [], str(job.warnings))

            off = Job(id="f7timestamps2", src=str(PORTRAIT), profile_id="P5",
                      out_path=plan.out_path, overrides={"keep_timestamps": False})
            now = time.time()
            os.utime(plan.out_path, (now, now))
            manager._copy_timestamps(off)
            check("with the option off the output time is left alone",
                  abs(Path(plan.out_path).stat().st_mtime - now) < 5.0, "")
    out_file.unlink(missing_ok=True)

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
