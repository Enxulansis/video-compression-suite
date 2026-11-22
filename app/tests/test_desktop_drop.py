"""Self-test the drag-drop wiring for the desktop window (no drag required).

The actual drop needs a human, but what MAKES a drop work is mechanical and
testable: pywebview only accepts dropped files once a `drop` listener exists on a
DOM element (it drives an internal listener count which tells the native host to
enable file drops). If that registration silently fails, dragging does nothing and
prints no error -- the exact bug this checks for.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_desktop_drop.py
"""

from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import desktop  # noqa: E402

PORT = 0  # Always bind a private ephemeral port; never inspect/reuse a live service.
results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


class Probe(desktop.DesktopHost):
    """DesktopHost that runs the page just long enough to inspect the wiring."""

    def __init__(self) -> None:
        super().__init__(port=PORT)
        self.probe = {}

    def inspect(self) -> None:
        time.sleep(4)
        try:
            from webview.dom import element as _element
            self.probe["num_listeners"] = _element._dnd_state.get("num_listeners")
            self.probe["shell_flag"] = self.window.evaluate_js("!!window.__vcsDesktopShell")
            self.probe["add_paths"] = self.window.evaluate_js(
                "typeof window.__vcsAddPaths === 'function'")
            self.probe["drain"] = self.window.evaluate_js(
                "typeof window.__vcsDrainDropped === 'function'")
        except Exception as exc:  # noqa: BLE001
            self.probe["error"] = repr(exc)


def main() -> int:
    print("\ndesktop drag-drop wiring self-test\n")

    check("pywebview importable", desktop.available(), desktop.version())

    # The event payload parser: pywebview injects `pywebviewFullPath` per file.
    cases = [
        ({"dataTransfer": {"files": [{"name": "a.mp4", "pywebviewFullPath": r"E:\a.mp4"}]}},
         [r"E:\a.mp4"]),
        ({"dataTransfer": {"files": [{"name": "a.mp4"}]}}, []),
        (None, []),
    ]
    for event, expected in cases:
        check("extract_paths({})".format(str(event)[:44]),
              desktop.DesktopHost.extract_paths(event) == expected)

    host = Probe()
    if not host.start_backend():
        check("backend started", False, "start_backend returned False")
        return 1
    # Assert the server actually ANSWERS, not merely that start_backend() returned True:
    # a bound socket with a dead app thread looks identical from the outside, and that is
    # exactly the "the window opens but nothing works" failure this suite exists for.
    answered: object = False
    try:
        import urllib.request
        # `base_url` already carries a query string ("?app=1&desktop=1"), so appending a
        # path to it requests "/" and silently gets index.html back -- which is how a
        # health check can "pass" while proving nothing.
        root = host.base_url.split("?")[0].rstrip("/")
        with urllib.request.urlopen(root + "/api/version", timeout=20) as reply:
            body = reply.read()
            answered = reply.status == 200 and b'"version"' in body
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        answered = "{}: {}".format(type(exc).__name__, exc)
    check("backend answers /api/version on port {}".format(host.port), answered is True,
          str(answered))

    import webview
    host.window = webview.create_window(desktop.WINDOW_TITLE, host.base_url,
                                       width=800, height=600, js_api=host._js_api())
    host.window.events.loaded += host._on_loaded
    threading.Thread(target=host.inspect, daemon=True).start()

    def closer() -> None:
        time.sleep(9)
        try:
            host.window.destroy()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=closer, daemon=True).start()
    try:
        webview.start(gui="edgechromium", debug=False)
    except TypeError:
        webview.start(debug=False)

    probe = host.probe
    print("    probe:", probe)

    check("drop listener registered on body", host.bound_drop_events == ["body.drop"],
          str(host.bound_drop_events))
    check("pywebview dnd listener count > 0",
          isinstance(probe.get("num_listeners"), int) and probe["num_listeners"] > 0,
          "num_listeners={}".format(probe.get("num_listeners")))
    check("page knows it is the desktop shell", probe.get("shell_flag") is True)
    check("__vcsAddPaths exposed to the host", probe.get("add_paths") is True)
    check("__vcsDrainDropped exposed to the host", probe.get("drain") is True)

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed\n".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
