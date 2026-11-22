"""Drop-in inbox: watch a folder and auto-add whatever lands in it.

This is the no-dialog answer to "the browser cannot give me paths": the user drops
files into one known folder and the app picks them up by itself. It works from
Explorer, from a download manager, from anywhere -- dragging files *into a folder*
has no security restriction, unlike dragging them into a web page.

Only files whose size has stopped changing are reported, so a file still being
copied in is not grabbed half-written.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

VIDEO_EXT = {
    "mp4", "m4v", "mkv", "mov", "avi", "wmv", "flv", "f4v", "ts", "m2ts", "mts",
    "webm", "mpg", "mpeg", "m2v", "vob", "rm", "rmvb", "3gp", "3g2", "ogv",
    "asf", "mxf", "dv", "divx", "dat", "m2p", "tp", "trp", "amv", "y4m",
}

POLL_SECONDS = 1.5
STABLE_SECONDS = 2.0


class Inbox:
    def __init__(self, directory: str, on_new: Callable[[List[str]], None],
                 seed_existing: bool = True) -> None:
        self.directory = Path(directory)
        self.on_new = on_new
        # seed_existing=True: files already in the folder when watching starts are
        # treated as "already known" and NOT announced. That avoids re-queuing a
        # backlog on every launch, but it also means a file the user dropped while
        # the app was closed is skipped -- use the "扫描收件箱" action or clear the
        # folder if you want existing files picked up. Tests pass False to observe
        # detection of files that are already there.
        self.seed_existing = seed_existing
        self._seen: Dict[str, float] = {}      # path -> first time we saw this size
        self._sizes: Dict[str, int] = {}
        self._announced: Set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        if self.seed_existing:
            try:
                for item in self.directory.iterdir():
                    if item.is_file():
                        self._announced.add(str(item))
            except OSError:
                pass
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def scan_once(self, announce_existing: bool = False) -> List[str]:
        """Run one scan and return anything newly available.

        `announce_existing=True` ignores the seeded set, which is what the UI's
        "扫描收件箱" action uses: the user explicitly asks for whatever is sitting
        in the folder right now.
        """
        if announce_existing:
            announced, self._announced = self._announced, set()
            try:
                return self._scan_once()
            finally:
                self._announced = announced
        return self._scan_once()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------- intern

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self._scan_once()
            except Exception:  # noqa: BLE001 - the watcher must never kill the app
                pass

    def _scan_once(self) -> List[str]:
        ready: List[str] = []
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return ready

        for item in entries:
            try:
                if not item.is_file():
                    continue
                name = item.name
                if name.startswith(".") or name.startswith("~$"):
                    continue
                if item.suffix.lstrip(".").lower() not in VIDEO_EXT:
                    continue
                stat = item.stat()
            except OSError:
                continue

            key = str(item)
            if key in self._announced:
                continue

            previous = self._sizes.get(key)
            if previous != stat.st_size:
                # still being written
                self._sizes[key] = stat.st_size
                self._seen[key] = time.time()
                continue
            if time.time() - self._seen.get(key, time.time()) < STABLE_SECONDS:
                continue

            self._announced.add(key)
            ready.append(key)

        if ready:
            try:
                self.on_new(sorted(ready))
            except Exception:  # noqa: BLE001
                pass
        return ready
