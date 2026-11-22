"""Batch-level arithmetic: totals, auto grouping, and budget allocation.

Pure on purpose. Every function here takes already-probed `MediaInfo` objects and returns
plain dicts, so the numbers can be tested without ffmpeg and reused by the API, the UI and
the tests from one implementation.

Three user-facing features live here:

* **批量总览 (A1)** -- "how big will this batch be, and how long will it take". The size
  uses the SAME formula as the per-file estimate (`planner.estimate_output_bytes`), so the
  total and the individual rows can never disagree. The time is a coefficient-based
  estimate and is labelled as one; the precise path is a real sample encode per file
  (`/api/measure`), which the UI offers as a separate button.
* **自动分组 (C3)** -- files with the same shape (resolution class / duration class /
  codec / HDR) get the same profile, because that is the decision the user actually makes
  once per group rather than once per file.
* **总预算分配 (C1)** -- "this batch must fit in N GB": the budget is divided in proportion
  to `duration x pixels` (how much work each file represents), and every row carries the
  resulting bits-per-pixel and a verdict, because the honest part of a budget is saying
  which files pay for it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .planner import (Overrides, PlanError, compute_scale, effective_fps, effective_max_edge,
                      estimate_output_bytes)

# --------------------------------------------------------------------------- speed
#
# MEASURED on this machine (2026-10-06): a 20 s / 1280x720 / 30 fps clip (0.92 MP,
# 600 frames) encoded with the settings below, timing the pure encode:
#
#     h264_nvenc p4   0.9 s   ->  615 Mpx/s
#     hevc_nvenc p5   1.0 s   ->  553 Mpx/s
#     libx264 medium  2.0 s   ->  276 Mpx/s
#     libsvtav1 p6    4.7 s   ->  118 Mpx/s
#     libsvtav1 p2   21.1 s   ->   26 Mpx/s      (from the A7 measurement, same clip)
#
# Throughput is treated as constant in megapixels per second, which is the simplest model
# that matches the measurement and stays explainable. It is optimistic at 4K (memory
# bandwidth starts to matter), so the estimate says so instead of pretending otherwise.
_MPPS_MEASURED: Dict[str, float] = {
    "h264_nvenc:p4": 615.0,
    "hevc_nvenc:p5": 553.0,
    "libx264:medium": 276.0,
    "libsvtav1:6": 118.0,
    "libsvtav1:2": 26.0,
}
#: Per-encoder fallback when the exact preset was not measured, relative to the measured
#: preset above. Direction is what matters: NVENC presets differ by tens of percent, x264's
#: by roughly an order of magnitude from ultrafast to veryslow.
_ENCODER_BASE = {
    "h264_nvenc": (615.0, "p4"),
    "hevc_nvenc": (553.0, "p5"),
    "av1_nvenc": (420.0, "p5"),
    "libx264": (276.0, "medium"),
    "libx265": (60.0, "medium"),
    "libsvtav1": (118.0, "6"),
    "libaom-av1": (15.0, ""),
    "librav1e": (30.0, ""),
}
_PRESET_FACTOR = {
    "ultrafast": 4.0, "superfast": 3.2, "veryfast": 2.6, "faster": 2.2,
    "fast": 1.8, "medium": 1.0, "slow": 0.6, "slower": 0.42, "veryslow": 0.3,
    "p1": 2.2, "p2": 1.9, "p3": 1.6, "p4": 1.3, "p5": 1.0, "p6": 0.75, "p7": 0.55,
}


def _mpps(encoder: str, preset: Any) -> Tuple[float, str]:
    """(megapixels per second, basis) for an encoder/preset pair."""
    key = "{}:{}".format(encoder, preset)
    if key in _MPPS_MEASURED:
        return _MPPS_MEASURED[key], "本机实测"
    base, base_preset = _ENCODER_BASE.get(encoder, (100.0, ""))
    name = str(preset or base_preset).lower()
    # SVT-AV1 presets are numbers 0-13: bigger is faster, and the curve is steep.
    if encoder == "libsvtav1":
        try:
            number = int(float(name))
        except (TypeError, ValueError):
            number = 6
        factor = 2.0 ** ((6 - number) / 2.0)      # measured around p6 = 118 Mpx/s
        return base * factor, "按 preset {} 换算".format(number)
    factor = _PRESET_FACTOR.get(name, 1.0)
    return base * factor, "按 preset {} 换算".format(name or "默认")


def estimate_seconds(info, profile: Dict[str, Any], ov: Optional[Overrides] = None,
                     scale: Optional[tuple] = None) -> Tuple[float, str]:
    """Rough encode time for one file, plus the basis of the number."""
    if not info.video or not info.duration or info.duration <= 0:
        return 0.0, ""
    encoder = str(profile.get("encoder") or "")
    if encoder == "copy":
        # Stream copy re-muxes: I/O bound only, and effectively instant next to an encode.
        return max(0.5, info.duration * 0.01), "复制编码（不重编码，几乎不耗时）"
    if scale is None and ov is not None:
        scale = compute_scale(info, ov)
    width = scale[0] if scale else info.video.display_width
    height = scale[1] if scale else info.video.display_height
    fps = effective_fps(info, profile) or info.video.fps or 0.0
    if not fps:
        return 0.0, ""
    mpps, basis = _mpps(encoder, ov.preset if ov and ov.preset is not None
                        else profile.get("preset"))
    frames = max(1.0, info.duration * fps)
    megapixels = frames * (max(1, width) * max(1, height)) / 1_000_000.0
    return megapixels / max(1.0, mpps), basis


def resolve_per_file(info, profile: Dict[str, Any], ov: Optional[Overrides],
                      per_file: Optional[Dict[str, Dict[str, Any]]] = None,
                      profiles: Optional[Dict[str, Dict[str, Any]]] = None
                      ) -> Tuple[Dict[str, Any], Optional[Overrides]]:
    """Effective (profile, overrides) for ONE file, honouring its own settings.

    The batch panel used to be computed from the main profile alone, so once the user put a
    group (or a single file) on a different profile -- a feature this very panel drives --
    the totals and the budget table described a batch that would never be encoded. Silent
    disagreement between the panel and the encoder is the failure mode this project keeps
    removing, so the per-file settings are resolved here, in ONE place, for all three
    endpoints.
    """
    own = dict((per_file or {}).get(info.path) or {})
    chosen = profile
    wanted = own.pop("profile", "")
    if wanted and profiles:
        chosen = profiles.get(str(wanted)) or profile
    if ov is None:
        return chosen, None
    if (wanted or own) and ov.intent:
        ov = replace(ov, intent={})  # explicit per-file profile is a manual selection
    allowed = {f for f in Overrides().__dict__.keys()}
    changes = {k: v for k, v in own.items() if k in allowed and v is not None}
    if not changes:
        return chosen, ov
    try:
        return chosen, replace(ov, **changes)
    except (TypeError, ValueError):
        return chosen, ov


def estimate_one(info, profile: Dict[str, Any], ov: Optional[Overrides] = None) -> Dict[str, Any]:
    """One row of the batch estimate (size, time AND peak memory)."""
    from .memory import estimate_instance_mb
    plan, decision = None, []
    if ov is not None and ov.intent:
        from .caps import get_caps
        from .planner import resolve_selection, make_plan
        caps = get_caps()
        requested_ov = ov
        profile, ov, decision = resolve_selection(info, profile, ov, caps)
        plan = make_plan(info, profile, requested_ov, caps=caps, exists_probe=lambda p: False)
    scale = compute_scale(info, ov) if ov is not None else None
    seconds, basis = estimate_seconds(info, profile, ov, scale=scale)
    est = estimate_output_bytes(info, profile, ov, scale=scale)
    out_w = scale[0] if scale else (info.video.display_width if info.video else 0)
    out_h = scale[1] if scale else (info.video.display_height if info.video else 0)
    preset = (ov.preset if ov is not None and ov.preset is not None
              else profile.get("preset"))
    ten_bit = "10le" in str(profile.get("pix_fmt") or "")
    mem_mb, mem_basis = estimate_instance_mb(profile.get("encoder", ""), out_w, out_h,
                                             ten_bit, preset)
    if plan is not None:
        est, mem_mb, mem_basis = plan.est_output_bytes, plan.est_memory_mb, plan.est_memory_basis
    return {
        "path": info.path,
        "name": info.path.replace("\\", "/").rsplit("/", 1)[-1],
        "duration": round(float(info.duration or 0), 3),
        "src_bytes": int(info.size_bytes or 0),
        "est_bytes": int(est),
        "est_low": int(est * 0.7),
        "est_high": int(est * 1.4),
        "encoder": profile.get("encoder", ""),
        "container": (ov.container if ov else "") or profile.get("container", ""),
        "decision": decision,
        "warnings": plan.warnings if plan else [],
        "out_path": plan.out_path if plan else "",
        "est_seconds": round(float(seconds), 1),
        "est_memory_mb": int(mem_mb),
        "memory_basis": mem_basis,
        "speed_basis": basis,
        "profile": profile.get("id", ""),
        "resolution": "{}x{}".format(info.video.display_width, info.video.display_height)
        if info.video else "",
        "output_resolution": "{}x{}".format(out_w, out_h) if out_w else "",
        "codec": info.video.codec if info.video else "",
    }


def estimate_batch(infos: Sequence[Any], profile: Dict[str, Any],
                   ov: Optional[Overrides] = None,
                   per_file: Optional[Dict[str, Dict[str, Any]]] = None,
                   profiles: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Per-file estimates plus the totals the user actually asked for."""
    rows = []
    blocked = []
    for info in infos:
        own_profile, own_ov = resolve_per_file(info, profile, ov, per_file, profiles)
        try:
            row = estimate_one(info, own_profile, own_ov)
        except (ValueError, PlanError) as exc:
            blocked.append({"path": info.path, "error": str(exc)})
            continue
        row["own_profile"] = own_profile.get("id", "") != profile.get("id", "")
        rows.append(row)
    src_bytes = sum(r["src_bytes"] for r in rows)
    est_bytes = sum(r["est_bytes"] for r in rows)
    seconds = sum(r["est_seconds"] for r in rows)
    bases = {r["speed_basis"] for r in rows if r["speed_basis"]}
    mixed = sorted({r["profile"] for r in rows if r.get("profile")})
    # Memory picture for the whole batch: the PEAK single task decides whether one job can
    # run at all, and the suggested concurrency is what fits in the memory available NOW
    # (measured live, because the guard decides on the real number, not on a plan).
    from .memory import (SAFETY_MARGIN_MB, machine_memory, safe_concurrency)
    peak_mb = max((r.get("est_memory_mb") or 0) for r in rows) if rows else 0
    mem = machine_memory()
    limits = {}
    try:
        from .jobs import get_manager
        limits = get_manager().limits()
    except Exception:  # noqa: BLE001 - the estimate must not need a scheduler
        limits = {}
    configured = int(limits.get("cpu", 1) or 1)
    suggested = safe_concurrency(mem, peak_mb, configured, SAFETY_MARGIN_MB)
    if not rows:
        size_basis, time_basis = "", ""
    else:
        size_basis = "按档位压缩系数估算（与单文件预估同一套公式）"
        if len(mixed) > 1:
            size_basis += "；已按各文件自己的档位分别计算（{}）".format("、".join(mixed))
        time_basis = ("经验系数预算用时：{}".format("、".join(sorted(bases)))
                      if bases else "无法估算用时（缺少帧率或时长）")
    return {
        "ok": True,
        "files": len(rows),
        "rows": rows,
        "blocked": blocked,
        "complete": not blocked,
        "requested": len(infos),
        "totals": {
            "src_bytes": src_bytes,
            "est_bytes": est_bytes,
            # A range, because a single number would overstate what a coefficient knows.
            "est_low": sum(r["est_low"] for r in rows),
            "est_high": sum(r["est_high"] for r in rows),
            "est_seconds": round(seconds, 1),
            "ratio": round(est_bytes / src_bytes, 4) if src_bytes else 0.0,
            "peak_memory_mb": int(peak_mb),
            "sum_memory_mb": int(sum(r.get("est_memory_mb") or 0 for r in rows)),
        },
        "memory": {
            "peak_mb": int(peak_mb),
            "free_mb": int(mem.get("free_mb") or 0),
            "total_mb": int(mem.get("total_mb") or 0),
            "available": int(mem.get("available") or 0),
            "configured_concurrency": configured,
            "suggested_concurrency": suggested,
            "margin_mb": SAFETY_MARGIN_MB,
            "note": ("按当前可用内存，本批最多能同时跑 {} 个（单任务峰值 {:.1f} GB，"
                     "可用 {:.1f} GB，安全余量 {:.1f} GB）".format(
                         suggested, peak_mb / 1024.0, (mem.get("free_mb") or 0) / 1024.0,
                         SAFETY_MARGIN_MB / 1024.0)) if mem.get("available") else
                    "无法读取可用内存，未给出并发建议",
        },
        "size_basis": size_basis,
        "time_basis": time_basis,
        "profile": profile.get("id", ""),
        "profiles_used": mixed,
    }


# ------------------------------------------------------------------------- grouping

def _resolution_class(info) -> str:
    if not info.video:
        return "无视频流"
    edge = max(info.video.display_width, info.video.display_height)
    if edge >= 3800:
        return "4K 及以上"
    if edge >= 2500:
        return "2K/1440p"
    if edge >= 1800:
        return "1080p"
    if edge >= 1200:
        return "720p"
    if edge >= 800:
        return "576p/480p"
    return "小尺寸（<480p）"


def _duration_class(info) -> str:
    seconds = float(info.duration or 0)
    if seconds <= 0:
        return "时长未知"
    if seconds < 60:
        return "短片（<1 分钟）"
    if seconds < 600:
        return "中等（1–10 分钟）"
    if seconds < 3600:
        return "长片（10–60 分钟）"
    return "超长（>1 小时）"


def _suggest(infos: Sequence[Any], profiles: Sequence[Dict[str, Any]]) -> Tuple[str, str]:
    """Pick a profile for a group, and say why.

    The rules are deliberately few and stated in the reason, because a suggestion the user
    cannot audit is just a mystery button: HDR sources need a codec that carries HDR,
    long/heavy groups prefer hardware speed, small short clips can afford the extreme
    ladder, and everything else takes the balanced default.
    """
    ids = {p.get("id") for p in profiles}
    hdr = any(getattr(i, "is_hdr", False) for i in infos)
    total_seconds = sum(float(i.duration or 0) for i in infos)
    biggest = max((i.video.display_width * i.video.display_height)
                  for i in infos if i.video) if any(i.video for i in infos) else 0
    if hdr and "P4" in ids:
        return "P4", "源是 HDR：需要能承载 HDR 的档位，否则会被压成灰片"
    if total_seconds > 1800 and "P3" in ids:
        return "P3", "总时长较长（{:.0f} 分钟）：优先硬件编码，先把时间压下来".format(total_seconds / 60)
    if biggest >= 3800 * 2160 and "A4" in ids:
        return "A4", "4K 源：极限阶梯里这一档同时降分辨率与帧率，体积收益最大"
    if biggest >= 1920 * 1080 and "A5" in ids:
        return "A5", "1080p 及以上：A5 是「720p + 极限 CRF」档，适合以体积为目标的整组"
    if "P3" in ids:
        return "P3", "默认平衡档（硬件 HEVC）：速度与体积的最佳折中"
    return (profiles[0].get("id", "") if profiles else ""), "没有可用档位信息，保持第一档"


def group_files(infos: Sequence[Any], profiles: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Auto-group files by shape, with a suggested profile per group.

    Grouping key = resolution class + duration class + codec family + HDR. Those are the
    axes that change what a sensible profile is; grouping by exact resolution would split
    a folder of phone videos into a dozen groups for no reason.
    """
    buckets: Dict[Tuple[str, str, str, bool], List[Any]] = {}
    for info in infos:
        codec = (info.video.codec if info.video else "") or "?"
        key = (_resolution_class(info), _duration_class(info), codec,
               bool(getattr(info, "is_hdr", False)))
        buckets.setdefault(key, []).append(info)

    groups: List[Dict[str, Any]] = []
    for (res, dur, codec, hdr), members in sorted(
            buckets.items(), key=lambda kv: (-len(kv[1]), kv[0][0])):
        suggested, reason = _suggest(members, profiles)
        src_bytes = sum(int(i.size_bytes or 0) for i in members)
        groups.append({
            "key": "{}|{}|{}|{}".format(res, dur, codec, "hdr" if hdr else "sdr"),
            "resolution": res,
            "duration": dur,
            "codec": codec,
            "hdr": hdr,
            "count": len(members),
            "paths": [i.path for i in members],
            "names": [i.path.replace("\\", "/").rsplit("/", 1)[-1] for i in members],
            "src_bytes": src_bytes,
            "total_seconds": round(sum(float(i.duration or 0) for i in members), 1),
            "suggested_profile": suggested,
            "reason": reason,
        })
    return {"ok": True, "groups": groups, "files": len(infos)}


# ------------------------------------------------------------------------- budget

def allocate_budget(infos: Sequence[Any], total_bytes: int, profile: Dict[str, Any],
                    ov: Optional[Overrides] = None,
                    per_file: Optional[Dict[str, Dict[str, Any]]] = None,
                    profiles: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Split a batch size budget across files, and say which ones get squeezed.

    The weight is `duration x pixels`: how much video each file actually contains. A
    duration-only split gives a 4K clip the same bytes as a 480p clip of the same length,
    which is not a budget but a coin flip.

    Each row reports the resulting bits per pixel (`bpp`) because that is the number that
    predicts quality, and the verdict comes from the SAME five-band scale the target-size
    solver uses (`target.reachability`) so the two features cannot disagree.
    """
    from .target import reachability

    wanted = max(1, int(total_bytes))
    rows = []
    weights = []
    for info in infos:
        if not info.video:
            weights.append(0.0)
            continue
        fps = float(info.video.fps or 0) or 30.0
        pixels = max(1, info.video.display_width) * max(1, info.video.display_height)
        weights.append(max(1.0, float(info.duration or 0)) * pixels * (fps / 30.0))
    total_weight = sum(weights) or 1.0

    # Largest-remainder distribution: integer shares never add up to the budget exactly
    # (truncation alone lost 1 byte in the first test), and "the parts don't sum to the
    # whole" is exactly the kind of small lie this project keeps removing.
    shares = [wanted * (w / total_weight) for w in weights]
    allocated_floors = [int(s) for s in shares]
    leftover = wanted - sum(allocated_floors)
    if leftover > 0:
        order = sorted(range(len(shares)), key=lambda i: shares[i] - allocated_floors[i],
                       reverse=True)
        for index in order[:leftover]:
            allocated_floors[index] += 1

    for info, weight, allocated in zip(infos, weights, allocated_floors):
        share = weight / total_weight
        # `reachability` takes BYTES and computes bpp itself, from the same five-band scale
        # the target-size solver uses -- so a file the solver would refuse cannot be
        # quietly blessed here.
        verdict = reachability(allocated, info)
        # The per-file profile decides what this file would have been encoded at, which is
        # what `tightness` compares the budget against.
        own_profile, own_ov = resolve_per_file(info, profile, ov, per_file, profiles)
        est = estimate_output_bytes(info, own_profile, own_ov)
        rows.append({
            "path": info.path,
            "name": info.path.replace("\\", "/").rsplit("/", 1)[-1],
            "duration": round(float(info.duration or 0), 2),
            "src_bytes": int(info.size_bytes or 0),
            "weight": round(weight, 1),
            "share": round(share, 4),
            "allocated_bytes": allocated,
            "bpp": verdict.get("bpp", 0.0),
            "verdict": verdict.get("tier", "unknown"),
            "verdict_plain": verdict.get("message", ""),
            "profile": own_profile.get("id", ""),
            "estimate_at_profile": int(est),
            # How much harder this file is pressed than the profile would have done on its
            # own: below 1 means the budget is tighter than what the档位 would produce.
            "tightness": (round(allocated / est, 3) if est else 0.0),
        })

    rows.sort(key=lambda r: (r["tightness"] or 0.0))
    squeezed = [r for r in rows if r["tightness"] and r["tightness"] < 0.8]
    unreachable = [r for r in rows if r["verdict"] == "impossible"]
    return {
        "ok": True,
        "total_bytes": wanted,
        "allocated_bytes": sum(allocated_floors),
        "src_bytes": sum(int(i.size_bytes or 0) for i in infos),
        "files": len(rows),
        "rows": rows,
        "squeezed": len(squeezed),
        "squeezed_names": [r["name"] for r in squeezed][:20],
        "unreachable": len(unreachable),
        "basis": ("按「时长 × 像素」加权分配：更长的、分辨率更高的文件分到更多预算。"
                  "bpp = 分配到的比特 ÷ 像素数 ÷ 帧率 ÷ 时长，是预测画质的那个量。"),
        "profile": profile.get("id", ""),
    }
