"""Memory-guard self-test: cost model, admission arithmetic, low-water decisions.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_memory.py

Deliberately PURE: nothing here starts an encoder or allocates gigabytes. The guard exists
because a real batch froze the machine, so its test must be runnable on that same machine
without repeating the accident. Every number that came from a measurement is asserted
against its measured anchor, and every threshold decision is exercised at its boundary.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import memory as M  # noqa: E402

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


def main() -> int:
    print("\nmemory guard self-test\n")

    print("  -- machine memory is readable and sane --")
    mem = M.machine_memory()
    print("     {}", {k: mem[k] for k in ("total_mb", "free_mb", "commit_used_mb",
                                          "commit_limit_mb")})
    if mem.get("available"):
        check("读到物理内存总量（> 8 GB）", mem["total_mb"] > 8 * 1024, str(mem["total_mb"]))
        check("可用内存 ≤ 总量", 0 <= mem["free_mb"] <= mem["total_mb"], str(mem))
        check("提交上限 ≥ 物理内存", mem["commit_limit_mb"] >= mem["total_mb"] - 64,
              "{} vs {}".format(mem["commit_limit_mb"], mem["total_mb"]))
    else:
        check("非 Windows 时返回 available=0 而不是崩溃",
              mem["total_mb"] == 0 and mem["available"] == 0, str(mem))

    print("\n  -- 成本模型：与实测锚点对齐 --")
    # The anchor that must not drift: 720p 10-bit preset 4 measured 3,443 MB.
    mb_720 = M.estimate_instance_mb("libsvtav1", 1280, 720, True, 4)[0]
    check("720p/10bit/preset4 ≈ 实测 3443 MB（±20%）",
          3443 * 0.8 <= mb_720 <= 3443 * 1.2, "{} MB".format(mb_720))
    mb_8bit = M.estimate_instance_mb("libsvtav1", 1280, 720, False, 4)[0]
    check("8bit 约为 10bit 的 0.75 倍",
          abs(mb_8bit / mb_720 - 0.75) < 0.02, "{} vs {}".format(mb_8bit, mb_720))

    print("\n  -- 单调性：面积↑ / 10bit / preset 更低 / 未知编码器 → 更大 --")
    small = M.estimate_instance_mb("libsvtav1", 854, 480, True, 4)[0]
    large = M.estimate_instance_mb("libsvtav1", 1920, 1080, True, 4)[0]
    check("分辨率越高估算越大", small < mb_720 < large,
          "{} < {} < {}".format(small, mb_720, large))
    check("1080p 约为 720p 的 2.25 倍（面积比）",
          2.0 <= large / mb_720 <= 2.5, "{:.2f}x".format(large / mb_720))
    p2 = M.estimate_instance_mb("libsvtav1", 854, 480, True, 2)[0]
    p6 = M.estimate_instance_mb("libsvtav1", 854, 480, True, 6)[0]
    check("preset 2 比 preset 4 吃更多内存", p2 > small, "{} > {}".format(p2, small))
    check("preset 6 比 preset 4 更省", p6 < small, "{} < {}".format(p6, small))
    unknown = M.estimate_instance_mb("some_new_encoder", 1920, 1080, True, 4)[0]
    check("未知编码器按最重的软件编码器保守取值", unknown >= large, str(unknown))
    nvenc = M.estimate_instance_mb("hevc_nvenc", 3840, 2160, False, "p5")[0]
    check("硬件编码只收固定的小额主机开销（不随分辨率增长）",
          nvenc <= 300 and nvenc == M.estimate_instance_mb("hevc_nvenc", 1280, 720, False, "p5")[0],
          str(nvenc))
    check("依据里写明实测/未实测", "实测" in M.estimate_instance_mb("libsvtav1", 1280, 720, True, 4)[1]
          and "未实测" in M.estimate_instance_mb("libx265", 1280, 720, False, "medium")[1])

    print("\n  -- 4K 无上限档位就是危险区（本次事故的形态）--")
    mb_4k = M.estimate_instance_mb("libsvtav1", 3840, 2160, True, 4)[0]
    print("     4K/10bit/preset4 ≈ {:.1f} GB".format(mb_4k / 1024.0))
    check("4K 单实例估算 ≥ 20 GB（说明必须拦住）", mb_4k >= 20 * 1024, str(mb_4k))
    total = mem.get("total_mb") or 31 * 1024
    check("4K 任务被判为危险（≥ 物理内存 25%）",
          M.is_dangerous(mb_4k, {"total_mb": total, "available": 1}) is True)
    check("720p 任务在 31.6 GB 机器上不算危险",
          M.is_dangerous(mb_720, {"total_mb": total, "available": 1}) is False,
          "{} vs {}".format(mb_720, total))
    note = M.danger_note(mb_4k, {"total_mb": total, "available": 1})
    check("危险说明含 GB 与百分比，可直接给用户看",
          "GB" in note and "%" in note, note[:80])

    print("\n  -- 准入算术：排队而不是拒绝（已确认的产品决策）--")
    # Machine at the moment of the incident: 4 GB free, margin 2.5 GB -> 1.5 GB usable.
    incident = {"free_mb": 4096, "total_mb": 31 * 1024, "available": 1}
    verdict = M.admission(incident, reserved_mb=0, need_mb=mb_720)
    check("可用 4 GB 时连一个 720p 任务都不放行（这就是当时的真相）",
          verdict["ok"] is False, verdict.get("reason", "")[:70])
    check("拒绝理由说清「需要/已用/可用/余量」四件事",
          all(k in verdict["reason"] for k in ("需要", "已运行", "可用", "安全余量")),
          verdict["reason"])
    check("理由里带 GB 数字，用户能据此关应用", "GB" in verdict["reason"])

    roomy = {"free_mb": 10240, "total_mb": 31 * 1024, "available": 1}
    check("可用 10 GB 时放行一个 720p 任务",
          M.admission(roomy, 0, mb_720)["ok"] is True)
    check("可用 10 GB 时两个 720p 任务仍然放行（2×3.1 GB < 7.7 GB 余量）",
          M.admission(roomy, mb_720, mb_720)["ok"] is True)
    check("可用 10 GB 时第三个 720p 任务要等（3×3.1 GB > 7.7 GB 余量）",
          M.admission(roomy, 2 * mb_720, mb_720)["ok"] is False,
          str(M.admission(roomy, 2 * mb_720, mb_720)["needed_mb"]))
    huge = {"free_mb": 25600, "total_mb": 31 * 1024, "available": 1}
    check("空闲时可同时放行四个 720p 任务",
          M.admission(huge, 3 * mb_720, mb_720)["ok"] is True)
    check("空闲时两个 1080p 任务刚好放得下（2×7 GB < 23 GB 余量）",
          M.admission(huge, large, large)["ok"] is True,
          str(M.admission(huge, large, large)["needed_mb"]))
    check("但四个 1080p 任务必须等（4×7 GB > 23 GB 余量）",
          M.admission(huge, 3 * large, large)["ok"] is False,
          str(M.admission(huge, 3 * large, large)["needed_mb"]))
    check("读不到内存时等待，避免未知资源情况下启动",
          M.admission({"available": 0}, 0, mb_720)["ok"] is False)

    print("\n  -- 内存感知的并发上限 --")
    check("可用 4 GB → 0 个 720p 实例", M.safe_concurrency(incident, mb_720, 4) == 0,
          str(M.safe_concurrency(incident, mb_720, 4)))
    check("可用 10 GB → 2 个 720p 实例（(10240-2500)/3443 = 2）",
          M.safe_concurrency(roomy, mb_720, 4) == 2,
          str(M.safe_concurrency(roomy, mb_720, 4)))
    check("空闲 → 也绝不超过用户设置的上限",
          M.safe_concurrency(huge, mb_720, 2) == 2,
          str(M.safe_concurrency(huge, mb_720, 2)))
    check("大任务（1080p ≈7 GB）在 10 GB 可用时只能 1 个",
          M.safe_concurrency(roomy, large, 4) == 1,
          str(M.safe_concurrency(roomy, large, 4)))
    check("参数为 0 时不除零（返回原上限）",
          M.safe_concurrency(roomy, 0, 3) == 3, str(M.safe_concurrency(roomy, 0, 3)))

    print("\n  -- 低水位阈值关系必须自洽（否则会抖动或永远不恢复）--")
    check("恢复水位高于低水位（有回差，不会来回抖）",
          M.LOW_WATER_MB + M.RESUME_HYSTERESIS_MB > M.LOW_WATER_MB)
    check("危急水位低于低水位", M.CRITICAL_MB < M.LOW_WATER_MB)
    check("安全余量大于低水位（准入比运行期更保守）",
          M.SAFETY_MARGIN_MB > M.LOW_WATER_MB)
    check("宽限期是正数秒", M.GRACE_SECONDS > 0)

    print("\n{} checks passed".format(PASSED))
    if FAILED:
        print("{} FAILED:".format(len(FAILED)))
        for name in FAILED:
            print("  - " + name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
