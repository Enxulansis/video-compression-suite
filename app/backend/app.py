"""Video Compression Suite -- application entry point.

Run:  python app.py [--port 8787] [--no-browser] [--verbose]

Serves the UI on http://127.0.0.1:<port> (loopback only) and starts the job
manager worker thread.  No third-party packages are required.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.jobs import get_manager       # noqa: E402
from server import create_server        # noqa: E402

BANNER = r"""
  ___  ___  ___ ___  ___ ___  ___ ___  ___ ___  ___
 |   |/ _ \| __/ __|/ __/ _ \| _ \ _ \/ __| __|/ __|   Video Compression Suite
 | | | (_) | _| (__| (_| (_) |  _/   / (__| _| \__ \   批量视频压缩 · 可拖动进度条
 |_|_|\___/|___\___|\___\___/|_| |_|_\___|___||___/   version 0.1
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Video Compression Suite")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (loopback only)")
    parser.add_argument("--port", type=int, default=8787, help="HTTP port")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--verbose", action="store_true", help="log every HTTP request")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="override the auto-detected parallel limits (1-16)")
    args = parser.parse_args()

    if args.verbose:
        os.environ["VCS_VERBOSE"] = "1"

    print(BANNER)
    # Only override the auto-detected pool limits when the user asked for it.
    if args.concurrency:
        manager = get_manager()
        applied = manager.set_concurrency(args.concurrency)
        print("  并行   : 已按 --concurrency {} 覆盖为 {}".format(args.concurrency, applied))
    else:
        print("  界面先就绪，编码能力在页面后台检测；恢复队列仍需显式放行。")

    try:
        server = create_server(args.host, args.port)
    except OSError as exc:
        print("\n  无法监听 {}:{} —— {}".format(args.host, args.port, exc))
        print("  端口可能已被占用，请换一个：python app.py --port 8899\n")
        return 2

    url = "http://{}:{}/".format(args.host, args.port)
    print("\n  页面地址: {}\n  按 Ctrl+C 退出\n".format(url))

    if not args.no_browser:
        threading.Thread(
            target=lambda: (time.sleep(0.6), webbrowser.open(url)), daemon=True
        ).start()

    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\n  正在退出……")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
