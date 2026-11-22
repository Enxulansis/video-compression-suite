"""Self-test the batch arithmetic: totals, auto grouping, budget allocation.

Run:  D:\\anaconda3\\envs\\vsc\\python.exe tests\\test_batch.py   (env: vcs)

These three features answer questions the single-file estimate cannot ("how big is this
batch", "which files can share one profile", "split this total budget"), and they are
pure functions over probed metadata -- so they are pinned here rather than discovered by
running a real batch and reading the numbers off the screen.

The check that matters most is the SHARED-FORMULA one: the batch total must equal the sum
of the per-file numbers the card shows, because two implementations of "expected size"
would drift and the panel would then disagree with the card right below it.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import batch as B  # noqa: E402
from core.planner import Overrides, estimate_output_bytes, make_plan  # noqa: E402
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


def media(path: str, *, width: int = 1920, height: int = 1080, fps: float = 30.0,
          duration: float = 60.0, size_mb: float = 100.0, codec: str = "h264",
          transfer: str = "") -> MediaInfo:
    info = MediaInfo(path=path, ok=True)
    stream = StreamInfo(kind="video", index=0, codec=codec, width=width, height=height,
                        fps=fps, duration=duration, color_transfer=transfer)
    info.streams.append(stream)
    info.duration = duration
    info.size_bytes = int(size_mb * 1024 * 1024)
    info.format_name = "mov,mp4,m4a,3gp,3g2,mj2"
    return info


P3 = {"id": "P3", "encoder": "hevc_nvenc", "crf": 28, "preset": "p5", "ratio": 3,
      "container": "mp4"}
P2 = {"id": "P2", "encoder": "libx264", "crf": 23, "preset": "medium", "ratio": 2,
      "container": "mp4"}
A5 = {"id": "A5", "encoder": "libsvtav1", "crf": 46, "preset": 6, "ratio": 6,
      "container": "mp4", "max_edge": 1280}
PROFILES = [P2, P3, A5, {"id": "P4", "encoder": "hevc_nvenc", "ratio": 3}]


def test_totals() -> None:
    print("\n-- 批量总览 (A1) --")
    files = [media("E:/a.mp4", size_mb=100), media("E:/b.mp4", size_mb=300)]
    result = B.estimate_batch(files, P3)
    check("每行都有体积与用时估计",
          all(r["est_bytes"] > 0 for r in result["rows"]), str(len(result["rows"])))
    check("总数 = 各文件之和（同一个公式）",
          result["totals"]["est_bytes"] == sum(r["est_bytes"] for r in result["rows"]))
    check("源总体积 = 各源之和", result["totals"]["src_bytes"] == 400 * 1024 * 1024)
    check("压缩率 = 预计 ÷ 源",
          abs(result["totals"]["ratio"]
              - result["totals"]["est_bytes"] / result["totals"]["src_bytes"]) < 1e-6)

    # THE invariant: the batch row must equal the planner's per-file estimate, or the
    # panel and the file card would print different numbers for the same file.
    ov = Overrides(location="beside")
    info = media("E:/c.mp4", size_mb=200)
    row = B.estimate_one(info, P3, ov)
    check("单文件行 == planner.estimate_output_bytes",
          row["est_bytes"] == estimate_output_bytes(info, P3, ov),
          "{} vs {}".format(row["est_bytes"], estimate_output_bytes(info, P3, ov)))

    # ...and with scaling, where the two code paths could most easily diverge.
    scaled = media("E:/d.mp4", width=3840, height=2160, size_mb=500)
    ov_scale = Overrides(location="beside", max_long_edge=1280)
    plan = make_plan(scaled, P3, ov_scale)
    row2 = B.estimate_one(scaled, P3, ov_scale)
    check("缩放后仍然一致（批量行 == 计划）",
          row2["est_bytes"] == plan.est_output_bytes,
          "{} vs {}".format(row2["est_bytes"], plan.est_output_bytes))

    bigger = B.estimate_batch([media("E:/e.mp4", size_mb=100)], A5)
    smaller = B.estimate_batch([media("E:/e.mp4", size_mb=100)], P2)
    check("极限档估出的体积小于兼容档",
          bigger["totals"]["est_bytes"] < smaller["totals"]["est_bytes"],
          "{} < {}".format(bigger["totals"]["est_bytes"], smaller["totals"]["est_bytes"]))


def test_speed_model() -> None:
    print("\n-- 用时估算（经验系数，本机实测标定） --")
    mpps, basis = B._mpps("libsvtav1", 6)
    check("svtav1 preset 6 用的是本机实测值", mpps == 118.0 and "实测" in basis, basis)
    mpps2, basis2 = B._mpps("libsvtav1", 2)
    check("preset 2 比 preset 6 慢（数值也在表里）", mpps2 < mpps, "{} < {}".format(mpps2, mpps))
    p2_note = B._mpps("libx264", "veryslow")
    check("未实测的 preset 会被换算并标注来源", "换算" in p2_note[1], p2_note[1])
    check("换算后的 veryslow 比 medium 慢", p2_note[0] < B._mpps("libx264", "medium")[0])

    # Hardware is much faster than software on this machine, and the estimated TIME must
    # reflect that -- otherwise the "how long will this take" number is useless.
    hw = B.estimate_one(media("E:/a.mp4", duration=600), P3)["est_seconds"]
    sw = B.estimate_one(media("E:/a.mp4", duration=600), A5)["est_seconds"]
    check("同一素材：硬件档估时远小于软件档", hw < sw, "{} < {}".format(hw, sw))

    # 4K must cost more time than 1080p for the same duration (constant Mpx/s model).
    small = B.estimate_one(media("E:/s.mp4", width=1920, height=1080, duration=60), P3)
    large = B.estimate_one(media("E:/l.mp4", width=3840, height=2160, duration=60), P3)
    check("同样时长，4K 估时约为 1080p 的 4 倍",
          3.4 < large["est_seconds"] / max(0.01, small["est_seconds"]) < 4.6,
          "{:.1f}s vs {:.1f}s".format(large["est_seconds"], small["est_seconds"]))

    copy_profile = {"id": "PC", "encoder": "copy", "ratio": 1}
    _, copy_basis = B.estimate_seconds(media("E:/a.mp4", duration=600), copy_profile,
                                       Overrides())
    check("复制编码被标注为「几乎不耗时」", "复制" in copy_basis, copy_basis)

    no_video = MediaInfo(path="E:/x.mp4", ok=True)
    secs, basis3 = B.estimate_seconds(no_video, P3, Overrides())
    check("没有视频流时不假装能估时（返回 0）", secs == 0.0 and basis3 == "", repr(basis3))


def test_grouping() -> None:
    print("\n-- 自动分组 (C3) --")
    files = [
        media("E:/phone1.mp4", width=1280, height=720, duration=45, codec="h264"),
        media("E:/phone2.mp4", width=1280, height=720, duration=30, codec="h264"),
        media("E:/movie.mp4", width=1920, height=1080, duration=5400, codec="hevc"),
        media("E:/hdr.mp4", width=3840, height=2160, duration=120, codec="hevc",
              transfer="smpte2084"),
    ]
    result = B.group_files(files, PROFILES)
    groups = result["groups"]
    check("所有文件都被分进某一组", sum(g["count"] for g in groups) == len(files))
    check("两个同形状的手机片段合成一组",
          any(g["count"] == 2 and g["resolution"] == "720p" for g in groups),
          str([(g["resolution"], g["count"]) for g in groups]))
    check("每组都带建议档位与理由",
          all(g["suggested_profile"] and g["reason"] for g in groups))
    hdr_group = next((g for g in groups if g["hdr"]), None)
    check("HDR 单独成组", hdr_group is not None)
    check("HDR 组建议能承载 HDR 的档位（P4）",
          hdr_group and hdr_group["suggested_profile"] == "P4",
          hdr_group and hdr_group["reason"])
    check("长片组优先硬件档（理由里说明了时长）",
          any("总时长较长" in g["reason"] for g in groups),
          str([g["reason"][:20] for g in groups]))
    check("分组返回组内所有路径（前端据此写覆盖）",
          all(len(g["paths"]) == g["count"] for g in groups))
    check("空选择不炸", B.group_files([], PROFILES)["groups"] == [])


def test_budget() -> None:
    print("\n-- 总预算分配 (C1) --")
    short = media("E:/short.mp4", duration=60, size_mb=50)
    long_ = media("E:/long.mp4", duration=600, size_mb=500)
    big = media("E:/big.mp4", width=3840, height=2160, duration=600, size_mb=2000)
    total = 1000 * 1024 * 1024
    result = B.allocate_budget([short, long_, big], total, P3, Overrides())
    rows = {r["name"]: r for r in result["rows"]}
    check("分配总量正好等于预算",
          sum(r["allocated_bytes"] for r in result["rows"]) == total,
          str(sum(r["allocated_bytes"] for r in result["rows"])))
    check("时长 × 像素 加权：4K 长片分到最多",
          rows["big.mp4"]["allocated_bytes"] > rows["long.mp4"]["allocated_bytes"]
          > rows["short.mp4"]["allocated_bytes"],
          str({k: v["allocated_bytes"] for k, v in rows.items()}))
    check("每行给出 bpp", all(r["bpp"] > 0 for r in result["rows"]))
    check("判定用的是 target 的同一套分档",
          all(r["verdict"] in ("comfortable", "normal", "degraded", "extreme", "impossible")
              for r in result["rows"]),
          str([r["verdict"] for r in result["rows"]]))
    check("被压得最狠的排在前面（按 tightness 排序）",
          all(result["rows"][i]["tightness"] <= result["rows"][i + 1]["tightness"]
              for i in range(len(result["rows"]) - 1)))

    tiny = B.allocate_budget([short, long_, big], 2 * 1024 * 1024, P3, Overrides())
    check("极小的预算会被判为不可达（而不是默默接受）",
          tiny["unreachable"] > 0 or any(r["verdict"] == "impossible" for r in tiny["rows"]),
          str([r["verdict"] for r in tiny["rows"]]))
    check("极小预算下仍然如实报告哪些文件被压得最狠",
          tiny["squeezed"] == len(tiny["rows"]), str(tiny["squeezed"]))
    check("incompossible 级别不会被谎报成常规",
          all(r["verdict_plain"] for r in tiny["rows"]))

    huge = B.allocate_budget([short], 100 * 1024 * 1024 * 1024, P3, Overrides())
    check("预算远大于需要时不报「被压狠」", huge["squeezed"] == 0, str(huge["squeezed"]))
    check("预算远大于需要时判定为安全/常规",
          huge["rows"][0]["verdict"] in ("comfortable", "normal"),
          huge["rows"][0]["verdict"])

    nofps = MediaInfo(path="E:/n.mp4", ok=True)
    nofps.streams.append(StreamInfo(kind="video", index=0, codec="h264", width=640,
                                    height=480, fps=0.0, duration=0.0))
    nofps.duration = 0.0
    nofps.size_bytes = 1024
    quiet = B.allocate_budget([nofps], total, P3, Overrides())
    check("缺帧率/时长时不崩，标记为 unknown",
          quiet["rows"][0]["verdict"] == "unknown", quiet["rows"][0]["verdict"])


def test_per_file_settings() -> None:
    print("\n-- 逐文件/分组档位必须反映到整批数字 (用户实测反馈) --")
    profiles = {p["id"]: p for p in PROFILES}
    files = [media("E:/a.mp4", size_mb=100), media("E:/b.mp4", size_mb=100)]
    ov = Overrides(location="beside")

    base = B.estimate_batch(files, P3, ov, None, profiles)
    mixed = B.estimate_batch(files, P3, ov, {"E:/b.mp4": {"profile": "A5"}}, profiles)
    check("逐文件档位会改变整批预计体积（面板不能只按主档位算）",
          mixed["totals"]["est_bytes"] < base["totals"]["est_bytes"],
          "{} vs {}".format(mixed["totals"]["est_bytes"], base["totals"]["est_bytes"]))
    check("汇总里写明用了哪些档位", sorted(mixed["profiles_used"]) == ["A5", "P3"],
          str(mixed["profiles_used"]))
    check("每行标明该文件实际使用的档位",
          [r["profile"] for r in mixed["rows"]] == ["P3", "A5"],
          str([r["profile"] for r in mixed["rows"]]))
    check("依据文案说明是按各文件自己的档位分别计算",
          "各文件自己的档位" in mixed["size_basis"], mixed["size_basis"])

    # The per-file profile also decides `tightness`, which is the whole point of the
    # "which files get squeezed" column.
    b1 = B.allocate_budget(files, 50 * 1024 * 1024, P3, ov, None, profiles)
    b2 = B.allocate_budget(files, 50 * 1024 * 1024, P3, ov,
                           {"E:/b.mp4": {"profile": "A5"}}, profiles)
    t1 = {r["name"]: r["tightness"] for r in b1["rows"]}
    t2 = {r["name"]: r["tightness"] for r in b2["rows"]}
    check("分组档位会改变预算表的「按档位预计/紧度」",
          t2["b.mp4"] != t1["b.mp4"], "{} -> {}".format(t1["b.mp4"], t2["b.mp4"]))
    check("预算行标明该文件用的档位",
          {r["name"]: r["profile"] for r in b2["rows"]} == {"a.mp4": "P3", "b.mp4": "A5"},
          str({r["name"]: r["profile"] for r in b2["rows"]}))

    # A per-file crf must reach the estimate as well (it changes the size), and an unknown
    # key must be ignored rather than blowing up.
    with_crf = B.estimate_batch(files, P3, ov, {"E:/a.mp4": {"crf": 51}}, profiles)
    plain = B.estimate_one(files[0], P3, Overrides(location="beside", crf=51))
    check("逐文件 CRF 也进入逐文件预估",
          with_crf["rows"][0]["est_bytes"] == plain["est_bytes"],
          "{} vs {}".format(with_crf["rows"][0]["est_bytes"], plain["est_bytes"]))
    unknown = B.estimate_batch(files, P3, ov, {"E:/a.mp4": {"nonsense": 1}}, profiles)
    check("未知的逐文件键被忽略而不是崩掉",
          unknown["totals"]["est_bytes"] == base["totals"]["est_bytes"],
          str(unknown["totals"]["est_bytes"]))
    missing = B.estimate_batch(files, P3, ov, {"E:/zzz.mp4": {"profile": "A5"}}, profiles)
    check("per_file 里对不存在的路径不影响计算",
          missing["totals"]["est_bytes"] == base["totals"]["est_bytes"])


def main() -> int:
    print("\nbatch arithmetic self-test\n")
    test_totals()
    test_speed_model()
    test_grouping()
    test_budget()
    test_per_file_settings()
    print("\n{} checks passed".format(PASSED))
    if FAILED:
        print("{} FAILED:".format(len(FAILED)))
        for name in FAILED:
            print("  - " + name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
