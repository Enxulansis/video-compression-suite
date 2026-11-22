"""Self-test the naming template, including the post-encode tokens.

The interesting part is the two-phase name: ffmpeg must be handed a real path before
`{filesize_mb}` can possibly be known, so the plan gets a *provisional* name and the
file is renamed once the values exist. Both phases are pure string work, so they are
pinned here rather than discovered by running a batch and squinting at filenames.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_naming.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import paths as P  # noqa: E402

results = []
BASE = {"stem": "影片", "profile": "P6", "height": 720, "codec": "libsvtav1"}
POST = {"filesize": "14.8MB", "filesize_mb": "14.8", "filesize_bytes": "15523456",
        "video_bitrate": "808k", "encode_time": "14m26s", "encode_secs": "866",
        "ratio": "21"}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def main() -> int:
    print("\nnaming template self-test\n")

    print("  -- plan-time tokens --")
    check("the default template is unchanged",
          P.render_name("{stem}_{profile}_{height}p", BASE) == "影片_P6_720p")
    check("{codec} is available",
          P.render_name("{stem}_{codec}", BASE) == "影片_libsvtav1")
    check("a template with no post tokens is not post-processed",
          P.needs_post_encode("{stem}_{profile}_{height}p") is False)
    check("a template with a post token is post-processed",
          P.needs_post_encode("{stem}_{filesize_mb}MB") is True)
    check("every documented token is recognised as post-encode",
          all(P.needs_post_encode("{stem}_{" + t + "}") for t in P.POST_ENCODE_TOKENS),
          ", ".join(P.POST_ENCODE_TOKENS))

    print("  -- provisional phase (what ffmpeg writes) --")
    prov = P.render_name("{stem}_{filesize_mb}MB_{encode_time}", BASE, provisional=True)
    check("post tokens are blanked provisionally", prov == "影片_MB", prov)
    check("no braces survive into a filename", "{" not in prov and "}" not in prov, prov)
    check("separator runs collapse",
          P.render_name("{stem}__{profile}__{height}p", BASE, provisional=True) == "影片_P6_720p")
    check("a trailing separator is stripped",
          P.render_name("{stem}_{filesize_mb}", BASE, provisional=True) == "影片")
    check("a leading separator is stripped",
          P.render_name("{filesize_mb}_{stem}", BASE, provisional=True) == "影片")

    print("  -- final phase (what the user sees) --")
    final = P.render_name("{stem}_{filesize_mb}MB_{encode_time}", {**BASE, **POST})
    check("post tokens are filled in", final == "影片_14.8MB_14m26s", final)
    check("a token-only template still works",
          P.render_name("{filesize_mb}MB", {**BASE, **POST}) == "14.8MB")
    check("all documented tokens render without braces",
          all("{" not in P.render_name("{stem}_" + t, {**BASE, **POST})
              for t in P.POST_ENCODE_TOKENS))

    print("  -- robustness --")
    check("an unknown token does not raise, it blanks",
          P.render_name("{stem}_{nope}x", BASE) == "影片_x")
    check("a stray brace does not raise",
          isinstance(P.render_name("{stem}_{", BASE), str))
    check("an empty template yields an empty string",
          P.render_name("", BASE) == "")
    check("a missing value blanks rather than saying 'None'",
          "None" not in P.render_name("{stem}_{filesize_mb}", BASE))
    check("a None value blanks",
          "None" not in P.render_name("{stem}_{x}", {**BASE, "x": None}))

    print("  -- helpers --")
    check("pretty_bytes has no space (shell/URL friendly)",
          P.pretty_bytes(15523456) == "14.8MB", P.pretty_bytes(15523456))
    check("pretty_bytes handles zero and bytes",
          P.pretty_bytes(0) == "0B" and P.pretty_bytes(512) == "512B",
          "{} / {}".format(P.pretty_bytes(0), P.pretty_bytes(512)))
    check("pretty_bytes handles GB", P.pretty_bytes(5 * 1024 ** 3) == "5.0GB")
    check("pretty_duration seconds", P.pretty_duration(8) == "8s")
    check("pretty_duration minutes", P.pretty_duration(866) == "14m26s",
          P.pretty_duration(866))
    check("pretty_duration hours", P.pretty_duration(3725) == "1h02m",
          P.pretty_duration(3725))
    check("pretty_duration tolerates None", P.pretty_duration(None) == "0s")

    print("  -- the generated name is a legal filename --")
    for token, value in POST.items():
        # Braces matter: "{stem}_" + token would render the token's NAME literally and
        # test nothing at all.
        rendered = P.render_name("{stem}_{" + token + "}", {**BASE, **POST})
        illegal = set('<>:"/\\|?*')
        check("{} substitutes and stays legal".format(token),
              value in rendered and not (illegal & set(rendered)), rendered)

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
