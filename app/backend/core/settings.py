"""Small persisted app settings (store/settings.json).

Only things the user explicitly chooses live here.  The file sits inside
backend/store/ which is gitignored, so machine-specific paths never get committed.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

from . import runtime

#: Must resolve exactly like `jobs.STORE_DIR`, including the `VCS_STORE_DIR` override
#: (see the comment there) -- otherwise an isolated probe instance would still write
#: its settings into the user's real store.
STORE_DIR = runtime.store_dir()
SETTINGS_FILE = STORE_DIR / "settings.json"

DEFAULTS: Dict[str, Any] = {
    "default_out_dir": "",   # empty => fall back to "beside the source"
    "location": "beside",    # beside | default | custom
    "concurrency": 1,
    "show_advanced": False,
    "show_extreme": False,
    "compatibility": "modern",
    "slow_av1": False,
    "limits": {},             # empty => automatic pool recommendations
    # False = packet-level scan (seconds even on huge files).  True = decode the
    # whole output, which also catches corruption inside the video stream itself.
    "deep_verify": False,
    # Sampled VMAF after each encode. Off by default: it is the slowest check we
    # have, and the keyframe gate already covers the failure that matters most.
    "vmaf_check": False,
    # ---- memory guard (core/memory.py) ----
    # On by default. Two 1080p SVT-AV1 jobs on a machine whose RAM was already busy froze
    # the desktop; the guard holds a job in the queue (with a visible reason) until the
    # memory is really there, and suspends a running encode if memory runs out mid-run.
    "memory_guard": True,
    #: Free memory that must remain for the rest of the machine (MB).
    "memory_margin_mb": 2500,
    #: Below this free-memory level a running software encode is suspended (MB).
    "memory_low_water_mb": 1500,
}

_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {}


def load() -> Dict[str, Any]:
    with _LOCK:
        if _CACHE:
            return dict(_CACHE)
        data = dict(DEFAULTS)
        if SETTINGS_FILE.is_file():
            try:
                stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    for key in DEFAULTS:
                        if key in stored:
                            data[key] = stored[key]
            except (OSError, ValueError):
                pass
        _CACHE.update(data)
        return dict(data)


def save(patch: Dict[str, Any]) -> Dict[str, Any]:
    with _LOCK:
        data = load()
        for key, value in (patch or {}).items():
            if key in DEFAULTS:
                data[key] = value
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(SETTINGS_FILE))
        _CACHE.update(data)  # Only durable settings may be reported as saved.
        return dict(data)


def default_out_dir() -> str:
    """The configured shared output folder, or '' when unset."""
    return str(load().get("default_out_dir") or "")


def resolve_default_candidate() -> str:
    """A sensible suggestion for the default folder when the user has not set one."""
    configured = default_out_dir()
    if configured:
        return configured
    videos = Path.home() / "Videos"
    base = videos if videos.is_dir() else Path.home()
    return str(base / "Compressed")
