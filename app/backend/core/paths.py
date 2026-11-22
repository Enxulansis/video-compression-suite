"""Output location policy -- one place that decides where a result lands.

The UI offers three choices, and a single helper resolves them so compression and
repair can never drift apart:

    beside   next to the source file, in a subfolder (compressed/ or repaired/)
             -- the default, and the only mode that is per-file
    default  one fixed folder for everything (user-configurable, persisted)
    custom   a folder the user types in for this run

All three keep two guarantees from specs (R9 / NFR-8):
  * the output can never be the source file itself
  * an existing file is never overwritten -- a numeric suffix is added instead
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# Subfolder created next to the source in "beside" mode.
SUBFOLDER = {"compress": "compressed", "repair": "repaired"}

# Mode identifiers accepted from the API.
LOCATIONS = ("beside", "default", "custom")

DESCRIPTIONS = {
    "beside": "源文件旁边的 compressed / repaired 子目录（每个文件各自存放）",
    "default": "统一保存到一个固定目录（可在下方修改）",
    "custom": "这次运行指定一个目录",
}


def resolve_out_dir(src_path: str, location: str, out_dir: str = "",
                    default_out_dir: str = "", kind: str = "compress") -> Path:
    """Return the directory the output should go into.

    Falls back to "beside the source" whenever a requested directory is missing
    or unusable, so a batch never fails merely because a path field was empty.
    """
    mode = (location or "beside").strip().lower()
    if mode not in LOCATIONS:
        mode = "beside"

    if mode == "custom" and out_dir.strip():
        return Path(out_dir.strip())
    if mode == "default" and (default_out_dir or out_dir).strip():
        return Path((default_out_dir or out_dir).strip())
    if mode == "beside" and out_dir.strip():
        # Tolerate an explicit out_dir while in beside mode (older clients).
        return Path(out_dir.strip())

    return Path(src_path).parent / SUBFOLDER.get(kind, "compressed")


def unique_path(directory: Path, filename: str,
                exists: Optional[Callable[[str], bool]] = None) -> Path:
    """Never overwrite: add _2, _3, ... before the extension when taken."""
    exists = exists or os.path.lexists
    candidate = directory / filename
    if not exists(str(candidate)):
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 2
    while True:
        candidate = directory / "{}_{}{}".format(stem, index, suffix)
        if not exists(str(candidate)):
            return candidate
        index += 1


def is_same_file(a: Path, b: Path) -> bool:
    try:
        if a.resolve() == b.resolve():
            return True
        return os.path.samefile(a, b)
    except OSError:
        return False


CONTAINERS = frozenset(('mp4', 'mkv', 'mov', 'webm', 'm4v', '3gp', '3g2', 'f4v'))


def validate_container(value: str) -> str:
    name = str(value).lower()
    if name not in CONTAINERS:
        raise ValueError('不支持的输出容器：{}；请选择 MP4 / MKV / MOV / WebM'.format(value))
    return name


def rename_without_replace(source: Path, target: Path) -> None:
    """On Windows rename is atomic and refuses an existing destination."""
    if os.name != 'nt':
        # link creates the destination exclusively; unlink only our old name.
        os.link(source, target)
        source.unlink()
    else:
        os.rename(source, target)


# ---------------------------------------------------------------- safe names
#
# The naming template is a *file* name, but nothing said so: `{stem}` on a source
# called `..\..\Public\evil` (or a template that literally contains a path separator)
# produced an output path outside the folder the user chose, and the write helper then
# created whatever directory that implied. Treating the rendered name as a single path
# component -- and stripping the characters Windows cannot store anyway -- closes that.

#: Reserved DOS device names: `CON.mp4` is still the console device on Windows.
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_RESERVED_NAMES |= {"COM{}".format(i) for i in range(1, 10)}
_RESERVED_NAMES |= {"LPT{}".format(i) for i in range(1, 10)}
#: Characters Windows forbids in a file name, plus control characters.
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: Longest name we will produce, leaving room for `_2` suffixes and the extension.
MAX_NAME_LEN = 150


def safe_filename(name: str) -> str:
    """Reduce a rendered template to ONE safe file-name component.

    Returns "" when nothing usable is left, so the caller can fall back to the source
    stem instead of writing somewhere unexpected.
    """
    text = str(name or "")
    # Normalise both separators first: a Windows-style `..\\..\\x` and a POSIX
    # `../../x` must behave the same, and on Windows `Path("a\\b").name` is `a\\b`.
    text = text.replace("\\", "/")
    text = text.split("/")[-1]
    text = _ILLEGAL_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(". ")
    if text in ("", ".", ".."):
        return ""
    if Path(text).stem.upper() in _RESERVED_NAMES:
        text = "_" + text
    if len(text) > MAX_NAME_LEN:
        suffix = Path(text).suffix
        text = text[:max(1, MAX_NAME_LEN - len(suffix))].rstrip(". ") + suffix
    return text if text not in ("", ".", "..") else ""


# ---------------------------------------------------------------- name template
#
# Two classes of token live in the naming template:
#
#   known now      {stem} {profile} {height} {codec}   -- filled before encoding
#   known later    {filesize} {filesize_mb} {video_bitrate} {encode_time} ...
#
# The second class cannot be substituted at plan time, but ffmpeg must be handed a
# real path to write to. So the plan gets a *provisional* name with those tokens
# blanked, and the file is renamed once the values exist. The user only ever sees
# the final name; the provisional one exists for the duration of the encode.

#: Tokens whose value only exists after the encode has finished.
POST_ENCODE_TOKENS = (
    "filesize",        # 14.8MB      (human, no space so it stays shell-friendly)
    "filesize_mb",     # 14.8
    "filesize_bytes",  # 15523456
    "video_bitrate",   # 808k        (video stream only, not the whole file)
    "encode_time",     # 14m26s
    "encode_secs",     # 866
    "ratio",           # 21          (output/source, in percent, no sign)
)

#: Tokens available before the encode starts.
PLAN_TOKENS = ("stem", "profile", "height", "codec")


class _BlankMissing(dict):
    """Unknown tokens render as empty rather than raising KeyError.

    A typo in the template must not fail a whole batch; the worst case is a shorter
    name, which `_tidy_name` then cleans up.
    """

    def __missing__(self, key: str) -> str:  # noqa: D105
        return ""


def needs_post_encode(template: str) -> bool:
    """True when the template uses at least one post-encode token."""
    text = template or ""
    return any("{" + token + "}" in text for token in POST_ENCODE_TOKENS)


def pretty_bytes(num: int) -> str:
    """`14.8MB` -- no space, so a generated filename stays shell- and URL-friendly."""
    value = float(max(0, int(num)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return "{:.1f}{}".format(value, unit) if unit != "B" else "{:.0f}B".format(value)
        value /= 1024
    return "{:.1f}TB".format(value)


def pretty_duration(seconds: float) -> str:
    """`14m26s` / `1h02m` / `8s` -- compact and readable inside a filename."""
    total = int(max(0, round(float(seconds or 0))))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "{}h{:02d}m".format(hours, minutes)
    if minutes:
        return "{}m{:02d}s".format(minutes, secs)
    return "{}s".format(secs)


def _tidy_name(name: str) -> str:
    """Collapse the holes a blanked token leaves behind.

    `{stem}_{filesize_mb}MB` with the token blanked would otherwise give `a_MB`;
    `{stem}__{profile}` would give double separators. Neither is wrong, but a name
    the user did not design is a small betrayal, so tidy it.
    """
    cleaned = re.sub(r"[_\-\s]{2,}", "_", name)
    cleaned = re.sub(r"^[_\-\s.]+", "", cleaned)
    cleaned = re.sub(r"[_\-\s.]+$", "", cleaned)
    return cleaned


def render_name(template: str, values: Dict[str, Any], *,
                provisional: bool = False) -> str:
    """Fill a naming template.

    With ``provisional=True`` the post-encode tokens are left blank, producing the
    name ffmpeg writes to; otherwise they are filled from ``values``.
    """
    mapping = _BlankMissing()
    for key, value in (values or {}).items():
        mapping[str(key)] = "" if value is None else str(value)
    if provisional:
        for token in POST_ENCODE_TOKENS:
            mapping[token] = ""
    try:
        rendered = (template or "").format_map(mapping)
    except (IndexError, ValueError):
        # A malformed template (`{`, `{0}`) must degrade, not break the batch.
        rendered = template or ""
    return safe_filename(_tidy_name(rendered))


def describe(src_path: str, location: str, out_dir: str = "",
             default_out_dir: str = "", kind: str = "compress") -> Dict[str, str]:
    """Human-readable summary for the UI: what the rule resolves to right now."""
    directory = resolve_out_dir(src_path, location, out_dir, default_out_dir, kind)
    return {
        "location": location or "beside",
        "directory": str(directory),
        "description": DESCRIPTIONS.get(location or "beside", DESCRIPTIONS["beside"]),
    }
