"""Read-only application resources and per-user installed application data."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

APP_ID = "VideoCompressionSuite"
APP_MUTEX = "VideoCompressionSuite.Desktop"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    if frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


def data_root() -> Path:
    override = os.environ.get("VCS_DATA_DIR")
    if override:
        return Path(override).resolve()
    if frozen():
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_ID
    return resource_root()


def store_dir() -> Path:
    override = os.environ.get("VCS_STORE_DIR")
    if override:
        return Path(override)
    return data_root() / ("store" if frozen() or os.environ.get("VCS_DATA_DIR") else "backend/store")


def profiles_path() -> Path:
    return resource_root() / ("profiles/profiles.json" if frozen() else "backend/profiles/profiles.json")


def instance_id() -> str:
    identity = "{}|{}".format(frozen(), os.path.normcase(str(store_dir().resolve())))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
