"""Loudness normalisation (G1) -- two passes, measured, and independently verified.

Why two passes: `loudnorm` in single-pass mode re-measures as it goes and rides the
gain dynamically, which changes the mix's dynamics (measured below: a high-crest clip
left with LRA 0.0 -> 2.1 LU). The two-pass form -- measure the whole file first, then
apply one static gain -- keeps the dynamics where the mixer put them.

Measured anchors on this machine (ffmpeg 8.0.1, `loudnorm` measurement vs `ebur128`
cross-check, the two meters agreed within 0.1 LU):

| clip | measured | after two-pass to -16 |
|---|---|---|
| quiet tone (-30 dB) | -51.8 LUFS / TP -47.5 | **-16.0 LUFS** / TP -11.7 |
| bare `sine` | -21.8 LUFS / TP -14.5 | **-16.0 LUFS** / TP -8.8 |
| quiet clip | -39.8 LUFS / TP -35.6 | — |
| high-crest (quiet body + loud clicks) | -13.0 LUFS / TP **+0.3** | -15.8 LUFS / TP -0.1, **Dynamic** |

Three honesty rules follow from those numbers, and they are the reason this module is
more than a filter string:

1. **Never claim normalisation without measuring the result.** `loudnorm`'s own
   "Output True Peak" said -1.5 dBTP on the high-crest clip while `ebur128` measured
   the delivered peak at -0.1 dBFS -- a 1.4 dB disagreement. The delivery check
   therefore uses our own measurement, not loudnorm's self-report.
2. **A silent Linear -> Dynamic fallback must be reported.** When the requested gain is
   impossible under the true-peak ceiling, `loudnorm` switches modes on its own and
   alters the dynamics. We read `Normalization Type` back out of the encode log and
   surface it as a warning instead of hiding it.
3. **Report the gain that was applied.** "-16 LUFS" means nothing to a user who does
   not know what their file was; "+5.8 dB from -21.8 LUFS" does.
"""

from __future__ import annotations

import json
import re
import subprocess
from . import work
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .caps import get_caps, subprocess_kwargs

#: Delivery presets. The numbers come from the public loudness specs each platform
#: publishes; they are presets, not the one true answer, and the UI says so.
TARGETS: Dict[str, Dict[str, Any]] = {
    "-14": {"label": "流媒体（YouTube / Spotify 等）", "i": -14.0, "tp": -1.0},
    "-16": {"label": "播客 / Apple Podcasts", "i": -16.0, "tp": -1.0},
    "-18": {"label": "保守（多留一点动态余量）", "i": -18.0, "tp": -1.5},
    "-23": {"label": "EBU R128 广播", "i": -23.0, "tp": -1.0},
}

DEFAULT_TP = -1.5
DEFAULT_LRA = 11.0
MIN_TARGET = -40.0
MAX_TARGET = -5.0

#: |measured - target| beyond this counts as "没到位" rather than noise.
TOLERANCE_LU = 0.5
#: Delivered true peak above this is a real problem (clipping risk), not a nitpick.
PEAK_TOLERANCE_DB = 0.3

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[Tuple[str, float, float, float, float], "Loudness"] = {}


@dataclass
class Loudness:
    """One measurement. `ok=False` carries a message fit for the UI."""

    path: str = ""
    ok: bool = False
    error: str = ""
    #: Integrated loudness (LUFS) and true peak (dBTP) of the analysed file.
    integrated: float = 0.0
    true_peak: float = 0.0
    lra: float = 0.0
    threshold: float = 0.0
    #: What `loudnorm` suggests adding to reach the requested target.
    target_offset: float = 0.0
    #: Which meter produced the numbers: "loudnorm" (measurement pass) or "ebur128".
    meter: str = ""
    #: WHICH peak `true_peak` is, in words, for the report. The two are not
    #: interchangeable: a sample peak can sit up to ~1 dB below the true peak on
    #: high-crest material, so comparing one against a ceiling stated in the other
    #: silently loosens the check by that much.
    peak_basis: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def preset_for(target: float) -> Dict[str, Any]:
    """The preset nearest to `target`, or a neutral default for a custom value."""
    key = "{:.0f}".format(float(target))
    if key in TARGETS:
        return dict(TARGETS[key])
    return {"label": "自定义", "i": float(target), "tp": DEFAULT_TP}


def known_targets() -> List[Dict[str, Any]]:
    """Presets for the UI, in a stable order."""
    return [dict(id=key, **value) for key, value in TARGETS.items()]


def clamp_target(value: Any) -> float:
    """A usable LUFS target, or 0.0 meaning "off"."""
    try:
        target = float(value)
    except (TypeError, ValueError):
        return 0.0
    if target == 0.0:
        return 0.0
    return max(MIN_TARGET, min(MAX_TARGET, target))


def effective_tp(target: float, given: Any = None) -> float:
    """True-peak ceiling: the caller's value when sane, else the preset's."""
    try:
        tp = float(given)
    except (TypeError, ValueError):
        tp = 0.0
    if tp < 0:
        return max(-9.0, min(-0.1, tp))
    return float(preset_for(target).get("tp", DEFAULT_TP))


def build_filter(measured: Optional[Dict[str, Any]], target: float,
                 tp: Optional[float] = None,
                 lra: float = DEFAULT_LRA) -> str:
    """The `loudnorm` filter string.

    With measurement values this is the **linear two-pass** form (one static gain, the
    mix's dynamics preserved). Without them it degrades to the single-pass dynamic form
    -- which is exactly the mode that alters dynamics, so callers that have not measured
    must say so rather than pretend the two are equivalent.
    """
    want_i = clamp_target(target)
    want_tp = effective_tp(target, tp)
    base = "loudnorm=I={}:TP={}:LRA={}".format(_num(want_i), _num(want_tp), _num(lra))
    data = measured or {}
    needed = ("integrated", "true_peak", "lra", "threshold")
    if not all(k in data for k in needed):
        return base + ":print_format=summary"
    return (base
            + ":measured_I={}".format(_num(data["integrated"]))
            + ":measured_TP={}".format(_num(data["true_peak"]))
            + ":measured_LRA={}".format(_num(data["lra"]))
            + ":measured_thresh={}".format(_num(data["threshold"]))
            + ":offset={}".format(_num(data.get("target_offset") or 0.0))
            + ":linear=true:print_format=summary")


def _num(value: Any) -> str:
    """ffmpeg wants plain decimals; 6 significant digits is plenty for dB/LUFS."""
    try:
        return "{:.6g}".format(float(value))
    except (TypeError, ValueError):
        return "0"


def gain_db(measured: Dict[str, Any], target: float) -> float:
    """The static gain the two-pass form is expected to apply, in dB."""
    try:
        return round(clamp_target(target) - float(measured.get("integrated") or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------- measuring

def _binary_kwargs() -> Dict[str, Any]:
    kwargs = dict(subprocess_kwargs())
    kwargs.pop("encoding", None)
    kwargs.pop("errors", None)
    return kwargs


def _run(argv: List[str], timeout: int) -> Tuple[int, str]:
    try:
        proc = work.run(argv, timeout=timeout, **subprocess_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "执行失败：{}".format(exc)
    return proc.returncode, (proc.stderr or "")


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)
_JSON_FIELDS = {
    "integrated": "input_i",
    "true_peak": "input_tp",
    "lra": "input_lra",
    "threshold": "input_thresh",
    "target_offset": "target_offset",
}


def measure(path: str, target: float, tp: Optional[float] = None,
            lra: float = DEFAULT_LRA, timeout: int = 1800,
            use_cache: bool = True) -> Loudness:
    """Measure a file's loudness with `loudnorm`'s measurement pass.

    The numbers come from the *target the user asked for* because `target_offset`
    depends on it; the integrated/peak/LRA values themselves do not.
    """
    result = Loudness(path=str(path), meter="loudnorm")
    caps = get_caps()
    if not caps.available():
        result.error = "FFmpeg 不可用"
        return result

    want_i = clamp_target(target)
    want_tp = effective_tp(target, tp)
    key = (str(path), _mtime(path), want_i, want_tp, float(lra))
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit is not None:
            return Loudness(**hit.to_dict())

    argv = [caps.ffmpeg, "-hide_banner", "-nostdin", "-v", "info",
            "-i", str(path),
            "-af", "loudnorm=I={}:TP={}:LRA={}:print_format=json".format(
                _num(want_i), _num(want_tp), _num(lra)),
            "-f", "null", "-"]
    code, log = _run(argv, timeout)
    match = _LOUDNORM_JSON.search(log or "")
    if not match:
        tail = (log or "").strip().splitlines()[-3:]
        result.error = ("无法测量响度（可能没有音轨）: "
                        + " / ".join(t.strip() for t in tail))[:300]
        return result
    try:
        data = json.loads(match.group(0))
    except ValueError:
        result.error = "响度测量结果无法解析"
        return result

    result.ok = True
    for attr, field_name in _JSON_FIELDS.items():
        setattr(result, attr, _to_float(data.get(field_name)))
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = Loudness(**result.to_dict())
    return result


def _mtime(path: str) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


_EBUR128_SUMMARY = re.compile(r"Summary:(.*)$", re.S)
#: The "True peak:" block of the ebur128 summary, up to the next blank-line-separated
#: section. Preferred over a bare `Peak:` search -- see `measure_output`.
_TRUE_PEAK_SECTION = re.compile(r"True peak:(.*?)(?:\n\s*\n|\Z)", re.S)


def measure_output(path: str, timeout: int = 1800) -> Loudness:
    """Measure a DELIVERED file with `ebur128` -- an independent meter.

    Used for verification on purpose: `loudnorm` reporting on its own output was
    measured 1.4 dB optimistic on the true peak of a high-crest clip, so the check that
    the user reads must not come from the filter being checked.
    """
    result = Loudness(path=str(path), meter="ebur128")
    caps = get_caps()
    if not caps.available():
        result.error = "FFmpeg 不可用"
        return result
    argv = [caps.ffmpeg, "-hide_banner", "-nostdin", "-v", "info", "-i", str(path),
            "-af", "ebur128=peak=true", "-f", "null", "-"]
    code, log = _run(argv, timeout)
    section = _EBUR128_SUMMARY.search(log or "")
    if code != 0 or not section:
        result.error = "无法测量成品响度（可能没有音轨）"
        return result
    # Take the LAST value after "Summary:": the per-frame lines above it report
    # momentary values, and reading the first `I:` gave -70 LUFS for perfectly normal
    # audio (the opening silence). That mistake is why this is pinned by a test.
    tail = section.group(1)
    result.integrated = _last_number(tail, r"I:\s+(-?[\d.]+)\s+LUFS")
    result.lra = _last_number(tail, r"LRA:\s+(-?[\d.]+)\s+LU")
    # The peak must be read from the TRUE-PEAK section specifically. ffmpeg prints a
    # `Peak:` line under "True peak:" and, on builds that also show sample peaks, an
    # identical-looking `Peak:` line under "Sample peak:"; a bare `Peak:` search takes
    # whichever comes first, which is the sample peak -- measured up to ~1 dB lower on
    # high-crest material, i.e. the delivery check silently passed files that overshoot
    # the ceiling. Anchoring on the section header removes the ambiguity, and the basis
    # is recorded so the report can say which quantity was compared.
    true_section = _TRUE_PEAK_SECTION.search(tail or "")
    if true_section:
        result.true_peak = _last_number(true_section.group(1),
                                        r"Peak:\s+(-?[\d.]+)\s+dB(?:FS|TP)")
        result.peak_basis = "真峰值（ebur128 True peak）"
    else:
        result.true_peak = _last_number(tail, r"Peak:\s+(-?[\d.]+)\s+dBFS")
        result.peak_basis = "采样峰值（该 ffmpeg 未输出 True peak 段，偏乐观）"
    result.threshold = _last_number(tail, r"Threshold:\s+(-?[\d.]+)\s+LUFS")
    result.ok = result.integrated != 0.0 or result.true_peak != 0.0
    if not result.ok:
        result.error = "成品响度测量结果为空"
    return result


def _last_number(text: str, pattern: str) -> float:
    matches = re.findall(pattern, text or "")
    return _to_float(matches[-1]) if matches else 0.0


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------- reporting

_APPLY_FIELDS = {
    "mode": r"Normalization Type:\s*(\w+)",
    "output_i": r"Output Integrated:\s*(-?[\d.]+)",
    "output_tp": r"Output True Peak:\s*(-?[\d.]+)",
    "output_lra": r"Output LRA:\s*(-?[\d.]+)",
    "offset": r"Target Offset:\s*(-?[\d.]+)",
}


def parse_apply(text: str) -> Dict[str, Any]:
    """Read back what the APPLY pass did.

    The interesting field is `mode`: `loudnorm` falls back from Linear to Dynamic on its
    own when the requested gain would break the true-peak ceiling, which changes the
    dynamics without asking. Reporting it is the difference between a tool that tells
    you what it did and one that quietly rewrites your mix.
    """
    found: Dict[str, Any] = {}
    for key, pattern in _APPLY_FIELDS.items():
        match = re.search(pattern, text or "")
        if not match:
            continue
        found[key] = match.group(1) if key == "mode" else _to_float(match.group(1))
    return found


def apply_warnings(applied: Dict[str, Any], measured: Dict[str, Any],
                   target: float) -> List[str]:
    """Warnings worth showing about the apply pass (never silent mode changes)."""
    out: List[str] = []
    mode = str(applied.get("mode") or "")
    if mode and mode.lower() != "linear":
        out.append(
            "响度标准化用了 {} 模式而不是线性增益：要达到 {} LUFS 又不越过真峰值上限，"
            "只能压缩动态——这意味着影片的动态范围被改变了（不是只调了音量）。".format(
                mode, _num(clamp_target(target))))
    if applied and measured:
        want = gain_db(measured, target)
        got = _to_float(applied.get("offset"))
        if got and abs(got) > 6.0 and abs(got - want) > 1.0:
            out.append("响度标准化实际偏移 {:.1f} LU 与预期 {:.1f} dB 相差较大，"
                       "请核对成品".format(got, want))
    return out


def verdict(delivered: Loudness, target: float,
            tp: Optional[float] = None) -> Tuple[str, str]:
    """Compare the DELIVERED file against the request. (status, detail).

    Status is one of the verifier's words: ok / warn / skipped.
    """
    want_i = clamp_target(target)
    want_tp = effective_tp(target, tp)
    if not delivered.ok:
        return "skipped", delivered.error or "成品响度未能测量"
    delta = delivered.integrated - want_i
    peak_over = delivered.true_peak - want_tp
    # Name the quantity that was compared. ffmpeg labels its true-peak line "dBFS",
    # which reads as the sample peak; saying "真峰值" plus the measured basis is the
    # difference between a number the user can audit and one they have to trust.
    detail = ("实测 {:.1f} LUFS（目标 {:.1f}，差 {:+.1f}）；{} {:.1f} dB"
              "（上限 {:.1f}）".format(delivered.integrated, want_i, delta,
                                       delivered.peak_basis or "真峰值",
                                       delivered.true_peak, want_tp))
    if delivered.peak_basis and "采样峰值" in delivered.peak_basis:
        detail += "（该值偏乐观，实际可能更高）"
    if abs(delta) > TOLERANCE_LU:
        return "warn", detail + " ⚠ 未落在目标 ±{:.1f} LU 内".format(TOLERANCE_LU)
    if peak_over > PEAK_TOLERANCE_DB:
        return "warn", detail + " ⚠ 超过真峰值上限 {:.1f} dB".format(peak_over)
    return "ok", detail


def describe(measured: Loudness, target: float, tp: Optional[float] = None) -> str:
    """One line for the job warnings: what we found, and what will be applied."""
    want_i = clamp_target(target)
    want_tp = effective_tp(target, tp)
    if not measured.ok:
        return "响度标准化：{}".format(measured.error or "源响度未知，将用单遍动态模式")
    return ("响度标准化：源 {:.1f} LUFS / 峰值 {:.1f} dBFS → 目标 {:.1f} LUFS"
            "（上限 {:.1f} dBTP），线性增益 {:+.1f} dB；音频将被重新编码").format(
        measured.integrated, measured.true_peak, want_i, want_tp,
        gain_db(measured.to_dict(), target))


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
