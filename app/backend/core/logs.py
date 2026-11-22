"""Structured job logs and the "prove it yourself" diagnostic bundle (F8).

The point of this module is that a user who hits a problem should be able to hand over
everything needed to explain it, without knowing which internals matter:

* a **timeline** of what happened and when (`Job.events`, appended by `jobs.add_event`),
* the **delivery checks** with their numbers,
* the exact **command** that ran,
* the raw **ffmpeg tail**, and
* the **environment** facts that explain most "works for you, not for me" reports
  (ffmpeg build, encoder actually used, app version).

It also keeps logs bounded: an unattended long encode writes a lot of stderr, and the
beginning of it is the input header we already store. `trim_log` keeps the tail and
leaves a marker, so a shortened log never looks like a complete one.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: How much of an encode's raw stderr is kept on disk (see trim_log).
LOG_KEEP_BYTES = 512 * 1024
#: How much the API hands to the viewer by default.
LOG_TAIL_BYTES = 64 * 1024

#: Human labels for the timeline kinds, so the viewer does not print internal words.
KIND_LABEL = {
    "created": "入队",
    "planned": "生成命令",
    "started": "开始编码",
    "paused": "已暂停",
    "resumed": "已继续",
    "verify": "校验",
    "loudness": "响度",
    "warning": "提醒",
    "done": "完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
    "log_trimmed": "日志截断",
}


def kind_label(kind: str) -> str:
    return KIND_LABEL.get(str(kind), str(kind))


def trim_log(path: Path, keep: int = LOG_KEEP_BYTES) -> Dict[str, Any]:
    """Keep only the tail of a large log, with a marker saying it was shortened."""
    try:
        size = path.stat().st_size
    except OSError:
        return {"trimmed": False, "kept_bytes": 0, "original_bytes": 0}
    if size <= keep:
        return {"trimmed": False, "kept_bytes": size, "original_bytes": size}
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, size - keep))
            tail = handle.read()
        marker = ("[... 日志过大，已只保留最后 {} KB（原始 {} KB）；"
                  "完整输出可复制该任务的命令自行重跑 ...]\n").format(
                      keep // 1024, size // 1024).encode("utf-8")
        path.write_bytes(marker + tail)
        return {"trimmed": True, "kept_bytes": len(marker) + len(tail),
                "original_bytes": size}
    except OSError as exc:
        return {"trimmed": False, "kept_bytes": size, "original_bytes": size,
                "error": str(exc)}


def read_tail(path: Path, limit: int = LOG_TAIL_BYTES) -> Tuple[str, int, bool]:
    """(text, total_bytes, truncated). A missing log is an empty string, not an error."""
    try:
        size = path.stat().st_size
    except OSError:
        return "", 0, False
    try:
        with open(path, "rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            raw = handle.read()
    except OSError:
        return "", size, False
    return raw.decode("utf-8", "replace"), size, size > limit


def _fmt_time(value: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return "-"


def build_diagnostics(job: Dict[str, Any], caps: Optional[Dict[str, Any]] = None,
                      log_tail: str = "", events: Optional[List[Dict[str, Any]]] = None,
                      app_version: str = "") -> str:
    """One paste-ready text block that explains a job without further questions.

    Ordered by how often each part answers the actual question: what was asked for, what
    ran, what the delivery gate said, what the encoder printed, and what the environment
    was. Deliberately plain text -- it is meant to be read by a person, or pasted into a
    bug report, not parsed.
    """
    caps = caps or {}
    events = events if events is not None else (job.get("events") or [])
    lines: List[str] = []
    add = lines.append

    add("=== 视频压缩套件 · 任务诊断 ===")
    add("应用版本 : {}".format(app_version or "未知"))
    add("ffmpeg   : {}".format(caps.get("ffmpeg_version") or "未知"))
    add("任务 id  : {}".format(job.get("id") or "?"))
    add("生成时间 : {}".format(_fmt_time(time.time())))
    add("")
    add("-- 任务 --")
    add("源文件   : {}".format(job.get("src") or ""))
    add("输出     : {}".format(job.get("out_path") or ""))
    add("档位     : {}".format(job.get("profile_id") or ""))
    add("编码器   : {}".format(job.get("encoder") or ""))
    add("状态     : {} {}".format(job.get("status") or "",
                                  ("（降级/风险）" if job.get("degraded") else "")))
    add("源大小   : {} 字节".format(job.get("src_size") or 0))
    add("输出大小 : {} 字节".format(job.get("total_size") or 0))
    add("时长     : {:.3f} s".format(float(job.get("duration") or 0.0)))
    add("用时     : {:.1f} s{}".format(float(job.get("elapsed") or 0.0),
                                        ("（暂停 {:.0f} s 已扣除）".format(job["paused_total"])
                                         if job.get("paused_total") else "")))
    if job.get("error"):
        add("错误     : {}".format(job["error"]))
    add("")

    if events:
        add("-- 时间线（{} 条）--".format(len(events)))
        for event in events:
            add("{}  {:>6}  {}".format(_fmt_time(event.get("t")),
                                       kind_label(event.get("kind") or ""),
                                       event.get("text") or ""))
        add("")

    if job.get("rules_applied"):
        add("-- 已应用规则 --")
        for rule in job["rules_applied"]:
            add("  · {}".format(rule))
        add("")

    if job.get("warnings"):
        add("-- 提醒 --")
        for warning in job["warnings"]:
            add("  ⚠ {}".format(warning))
        add("")

    verify = job.get("verify") or {}
    if verify.get("checks"):
        add("-- 交付校验：{} --".format(verify.get("verdict") or "?"))
        for check in verify["checks"]:
            add("  [{}] {} {} — {}".format(check.get("status"), check.get("id"),
                                           check.get("label"), check.get("detail") or ""))
        if verify.get("loudness"):
            add("  响度实测：{}".format(verify["loudness"]))
        if verify.get("loudness_applied"):
            add("  响度应用：{}".format(verify["loudness_applied"]))
        add("")

    overrides = job.get("overrides") or {}
    if overrides:
        add("-- 本次参数 --")
        for key in sorted(overrides):
            value = overrides[key]
            if value in ("", 0, 0.0, False, None, [], {}):
                continue
            add("  {}={}".format(key, value))
        add("")

    if job.get("command"):
        add("-- 命令（可直接复制重跑）--")
        add(job["command"])
        add("")

    if log_tail:
        add("-- ffmpeg 输出（末尾）--")
        add(log_tail.strip()[-8000:])
    else:
        add("-- ffmpeg 输出：（无）--")
    if job.get("log_trimmed"):
        add("注意：该日志曾被截断，只保留尾部（原始 {} KB）".format(
            int((job["log_trimmed"] or {}).get("original_bytes", 0)) // 1024))
    return "\n".join(lines)
