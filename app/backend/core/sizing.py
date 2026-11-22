"""Hardware sizing: how many jobs can actually run at once.

Measured on this machine (RTX 4060 Laptop, i9-13980HX, FFmpeg 8.0.1), 8 x 1080p
HEVC clips through NVENC:

    concurrency   total     throughput     per clip
        1         16.2s     29.7 /min      2.02s
        2         10.2s     46.8 /min      1.28s
        3          8.3s     58.2 /min      1.03s
        4          7.1s     68.0 /min      0.88s
        8          5.6s     85.3 /min      0.70s

All 8 sessions started fine (modern drivers lifted the old 3-session cap on
consumer cards), but returns diminish sharply: 8x the concurrency buys 2.9x the
throughput, and each individual clip gets ~3x slower. 4 is the sweet spot for
responsiveness; 8 is the most this GPU is worth.

The other half of the design is that GPU and CPU jobs do not compete: NVENC work
runs on the video engine, software encoders (SVT-AV1, x265) run on CPU cores. They
are tracked as separate slot pools so a GPU job and a CPU job can overlap instead
of queueing behind each other.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from .caps import get_caps

# Measured ceiling for this class of GPU; kept as the hard cap for the UI.
GPU_CONCURRENCY_CAP = 8
GPU_CONCURRENCY_RECOMMENDED = 4
CPU_CONCURRENCY_CAP = 4


def resolve_resource(encoder: str) -> str:
    """Which slot pool an encoder needs: 'gpu' or 'cpu'."""
    if encoder.endswith("_nvenc") or encoder.endswith("_qsv") or encoder.endswith("_amf"):
        return "gpu"
    if encoder.endswith("_mf") or encoder.endswith("_vaapi") or encoder.endswith("_vulkan"):
        return "gpu"
    return "cpu"


def physical_cores() -> int:
    try:
        import psutil  # type: ignore
        return psutil.cpu_count(logical=False) or os.cpu_count() or 4
    except Exception:  # noqa: BLE001
        pass
    # No psutil: on Windows use the environment, else fall back to logical/2.
    logical = os.cpu_count() or 4
    return max(1, logical // 2)


def recommend() -> Dict[str, Any]:
    """Recommended concurrency per resource pool, based on the local hardware."""
    caps = get_caps()
    cores = physical_cores()
    has_nvenc = any(caps.has_encoder(name) for name in
                    ("av1_nvenc", "hevc_nvenc", "h264_nvenc"))
    has_qsv = any(caps.has_encoder(name) for name in
                  ("av1_qsv", "hevc_qsv", "h264_qsv"))

    # CPU encoders (SVT-AV1 above all) saturate every core, so more than one at a
    # time just splits the same silicon: 2 slots on a 24-core part is a sane ceiling.
    cpu_slots = 1 if cores <= 8 else 2
    cpu_slots = min(cpu_slots, CPU_CONCURRENCY_CAP)

    # Keep one unused slot when no hardware encoder works, matching manager/UI limits.
    gpu_slots = 1
    if has_nvenc or has_qsv:
        gpu_slots = GPU_CONCURRENCY_RECOMMENDED

    return {
        "physical_cores": cores,
        "logical_cores": os.cpu_count() or cores,
        "has_nvenc": has_nvenc,
        "has_qsv": has_qsv,
        "gpu": gpu_slots,
        "cpu": cpu_slots,
        "gpu_cap": GPU_CONCURRENCY_CAP,
        "cpu_cap": CPU_CONCURRENCY_CAP,
        "basis": ("实测：8 路 NVENC 并发吞吐 85 路/分（单路 29.7），但边际递减，故推荐 {}/{}".format(
            gpu_slots, GPU_CONCURRENCY_CAP) if has_nvenc else
            "QSV 可用，保留硬件／软件独立槽位" if has_qsv else
            "当前无可运行的硬件编码器；GPU 槽位保留 1，不代表硬件可用"),
    }


def describe() -> Dict[str, Any]:
    """Compact payload for the UI / API."""
    return recommend()
