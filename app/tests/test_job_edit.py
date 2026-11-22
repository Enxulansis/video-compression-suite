"""Unit tests for queue editing: per-job parameters (F12a) and reordering (F12b).

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_job_edit.py

These deliberately do NOT go through `submit`: submitting starts real ffmpeg encodes,
and a test whose timing depends on how fast the encoder is cannot assert queue order.
Instead the jobs are inserted into the manager already queued, which is exactly the
state `update_job`/`move`/`cancel_all` are written to handle -- and no encoder runs,
because a re-planned queued job only rebuilds its argv.

The HTTP surface of the same features (status codes, keep-alive behaviour) is covered
by _temp/probe/verify_g5f12.py.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

# Isolate the store BEFORE importing core.jobs: STORE_DIR is resolved at import time.
_ISOLATED_STORE = Path(__file__).resolve().parent / "_work" / "store_edit"
_ISOLATED_STORE.mkdir(parents=True, exist_ok=True)
os.environ["VCS_STORE_DIR"] = str(_ISOLATED_STORE)

from core.jobs import Job, JobManager  # noqa: E402
from core.probe import MediaInfo, StreamInfo

SOURCE = Path(__file__).resolve().parent / "_work" / "e2e_src.mp4"

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(label)
    print("  {} {}{}".format("ok  " if ok else "FAIL", label,
                             "" if ok else "  <- " + detail))


def argv_of(job: Job) -> str:
    return " ".join(job.argv)


def make_manager(count: int = 3) -> tuple[JobManager, list[str]]:
    """A manager holding `count` queued jobs, no encoder involved."""
    manager = JobManager(concurrency=1)
    manager._jobs.clear()
    manager._order.clear()
    ids = []
    for index in range(count):
        # Same shape as production ids (uuid4().hex[:12]) so a future refactor can hand
        # these to the HTTP routes, which only accept hex ids.
        job_id = "{:012x}".format(index + 1)
        manager._jobs[job_id] = Job(id=job_id, src=str(SOURCE), profile_id="P7",
                                    status="queued", created=1000.0 + index)
        manager._order.append(job_id)
        ids.append(job_id)
    return manager, ids


def test_unknown_keys() -> None:
    manager = JobManager(concurrency=1)
    check("未知名参数被识别出来",
          manager.unknown_override_keys({"max_edge": 640}) == ["max_edge"],
          "{}".format(manager.unknown_override_keys({"max_edge": 640})))
    check("合法参数不误报",
          manager.unknown_override_keys({"crf": 30, "max_long_edge": 640}) == [],
          "{}".format(manager.unknown_override_keys({"crf": 30, "max_long_edge": 640})))
    # None means "unset" for every override, so it is dropped by _make_overrides and
    # must not be reported as an unknown key.
    check("值为 None 的键不算未知（与 _make_overrides 一致）",
          manager.unknown_override_keys({"whatever": None}) == [],
          "{}".format(manager.unknown_override_keys({"whatever": None})))
    check("空 dict 不报错", manager.unknown_override_keys({}) == [], "")


def test_update_job_replans() -> None:
    manager, ids = make_manager(3)
    target = ids[0]

    result = manager.update_job(target, {"crf": 42, "max_long_edge": 640}, "")
    check("改参数返回 ok", result.get("ok") is True, "{}".format(result))
    job = manager._jobs[target]
    check("argv 带上 crf 42（libx265 用 -crf）",
          "-crf 42" in argv_of(job), argv_of(job)[-200:])
    check("argv 带上缩放 640（max_long_edge 真的生效）",
          "640" in argv_of(job) and "scale" in argv_of(job), argv_of(job)[-200:])
    check("overrides 落库便于复现",
          job.overrides.get("max_long_edge") == 640,
          "{}".format(job.overrides.get("max_long_edge")))
    check("未改动的任务保持原样",
          manager._jobs[ids[1]].overrides == {} and manager._jobs[ids[1]].argv == [],
          "{}".format(manager._jobs[ids[1]].overrides))

    # The typo case: the old code dropped the key and still reported success, so the
    # job silently ran with settings other than the ones the page showed.
    before = argv_of(manager._jobs[ids[1]])
    result = manager.update_job(ids[1], {"max_edge": 640}, "")
    check("未知参数被拒绝而不是静默忽略",
          result.get("ok") is False and "max_edge" in str(result.get("error")),
          "{}".format(result))
    check("被拒绝时任务未被改动（不是半改）",
          argv_of(manager._jobs[ids[1]]) == before and manager._jobs[ids[1]].overrides == {},
          "{}".format(manager._jobs[ids[1]].overrides))

    result = manager.update_job(ids[2], {"crf": 30}, "NO_SUCH_PROFILE")
    check("未知档位被拒绝", result.get("ok") is False, "{}".format(result))

    result = manager.update_job("nosuchjob00", {"crf": 30}, "")
    check("不存在的任务被拒绝", result.get("ok") is False, "{}".format(result))

    # Refusing a started job is the whole point: its argv is already running.
    manager._jobs[ids[2]].status = "running"
    result = manager.update_job(ids[2], {"crf": 30}, "")
    check("运行中的任务拒绝改参数",
          result.get("ok") is False and "排队" in str(result.get("error")),
          "{}".format(result))


def test_move() -> None:
    manager, ids = make_manager(4)
    a, b, c, d = ids
    check("初始顺序 = 提交顺序", manager._order == [a, b, c, d], "{}".format(manager._order))

    check("末位移到队首", manager.move(d, a) and manager._order == [d, a, b, c],
          "{}".format(manager._order))
    check("before 为空 = 移到队尾", manager.move(d, "") and manager._order == [a, b, c, d],
          "{}".format(manager._order))
    check("移到队尾后顺序复原", manager._order == [a, b, c, d],
          "{}".format(manager._order))
    check("自己移到自己之前被拒绝", manager.move(b, b) is False, "")
    check("锚点不存在被拒绝", manager.move(b, "nosuchjob00") is False, "")
    check("被拒绝的操作不改变顺序", manager._order == [a, b, c, d],
          "{}".format(manager._order))

    # REGRESSION: moving a job DOWN the queue used to land it one slot late, i.e. after
    # the anchor instead of before it, because the anchor index was taken before the
    # moved job was removed (removing an earlier entry shifts every later index by one).
    # The original test only moved the LAST job to the front, which is the direction that
    # happened to work -- so it passed while drag-and-drop was wrong.
    manager._order[:] = [a, b, c, d]
    check("向前移（目标在后面）落到目标之前", manager.move(a, c) and manager._order == [b, a, c, d],
          "{}".format(manager._order))
    manager._order[:] = [a, b, c, d]
    check("相邻且已在目标之前 = 顺序不变（幂等）",
          manager.move(b, c) and manager._order == [a, b, c, d],
          "{}".format(manager._order))
    manager._order[:] = [a, b, c, d]
    check("跨过一位向前移", manager.move(c, b) and manager._order == [a, c, b, d],
          "{}".format(manager._order))
    manager._order[:] = [a, b, c, d]
    check("向后移（目标在前面）落到目标之前", manager.move(d, b) and manager._order == [a, d, b, c],
          "{}".format(manager._order))
    manager._order[:] = [a, b, c, d]
    check("已经在目标之前时也保持正确（幂等位置）",
          manager.move(a, b) and manager._order == [a, b, c, d],
          "{}".format(manager._order))

    manager._order[:] = [a, b, c, d]
    manager._jobs[c].status = "running"
    check("运行中的任务不能移动", manager.move(c, "") is False, "")
    check("不能用运行中的任务当锚点", manager.move(b, c) is False, "")
    check("move 不丢任务也不重复",
          sorted(manager._order) == sorted(ids) and len(manager._order) == len(ids),
          "{}".format(manager._order))

    manager._jobs[a].status = "done"
    check("已完成的任务不能移动", manager.move(a, "") is False, "")
    check("禁用项被拒后顺序仍未变", manager._order == [a, b, c, d],
          "{}".format(manager._order))


def test_cancel_all() -> None:
    manager, ids = make_manager(4)
    manager._jobs[ids[0]].status = "running"
    manager._jobs[ids[1]].status = "verifying"
    manager._jobs[ids[2]].status = "done"

    result = manager.cancel_all()
    check("cancel-all 计数 = 运行中 2 + 排队 1",
          result == {"running": 2, "queued": 1, "total": 3}, "{}".format(result))
    check("运行中的任务被取消",
          manager._jobs[ids[0]].status == "cancelled", manager._jobs[ids[0]].status)
    check("校验中的任务也被取消",
          manager._jobs[ids[1]].status == "cancelled", manager._jobs[ids[1]].status)
    check("排队中的任务被取消",
          manager._jobs[ids[3]].status == "cancelled", manager._jobs[ids[3]].status)
    check("已完成的任务不被计也不被改",
          manager._jobs[ids[2]].status == "done", manager._jobs[ids[2]].status)
    check("取消后没有活跃任务", manager.active_count() == 0,
          "{}".format(manager.active_count()))

    again = manager.cancel_all()
    check("再次 cancel-all 是空操作",
          again == {"running": 0, "queued": 0, "total": 0}, "{}".format(again))


def main() -> int:
    for name, fn in (("未知参数识别", test_unknown_keys),
                     ("改参数 F12a", test_update_job_replans),
                     ("排序 F12b", test_move),
                     ("停止全部 G5", test_cancel_all)):
        print("[{}]".format(name))
        if fn is test_update_job_replans:
            # Replanning only needs metadata, not a fixture left by an earlier encode.
            info = MediaInfo(path=str(SOURCE), ok=True, duration=20, size_bytes=1000000,
                             streams=[StreamInfo(0, "video", codec="h264", width=1280,
                                                 height=720, fps=30, pix_fmt="yuv420p")])
            with patch("core.jobs.probe", return_value=info):
                fn()
        else:
            fn()
    print()
    print("通过 {} / 失败 {}".format(len(PASSED), len(FAILED)))
    for label in FAILED:
        print("  FAILED: {}".format(label))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
