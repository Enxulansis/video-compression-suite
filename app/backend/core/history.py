"""Compression history, indexed from the per-job ``report.json`` files.

Why this module exists
----------------------
Every finished job already writes ``store/jobs/<id>/report.json`` and nothing ever
deletes it -- ``clear_finished()`` only empties the queue, not the archive.  So the
history data has been on disk all along (39 records on this machine the first time
it was counted, 22 with the output file still in place); what was missing was an
index and a way to see it.  Users concluded the tool "has no memory" and re-encoded
files they had already compressed.

This module is read-only about the archives, plus two explicit user actions
(``remove`` / ``clear``).  Deleting a history *record* never touches a video; only
when the caller explicitly asks for it does ``remove`` also unlink the output file,
and even then the source file is never a candidate.

No new storage format is introduced -- if a report is unreadable it is skipped
rather than failing the whole list.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .jobs import JOBS_DIR
from .actions import identity_matches

#: A finished job's report is immutable, so an entry can be reused until the file's
#: mtime changes.  Keyed by job id.
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.RLock()


def _read_report(job_dir: Path) -> Optional[Dict[str, Any]]:
    """Parse one ``report.json``; returns None when it is missing or corrupt."""
    report = job_dir / "report.json"
    try:
        stamp = report.stat().st_mtime
    except OSError:
        return None

    with _CACHE_LOCK:
        cached = _CACHE.get(job_dir.name)
        if cached and cached.get("_mtime") == stamp:
            return cached

    try:
        raw = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    src = str(raw.get("src") or "")
    out_path = str(raw.get("out_path") or "")
    src_size = int(raw.get("src_size") or 0)
    total_size = int(raw.get("total_size") or 0)
    verify = raw.get("verify") if isinstance(raw.get("verify"), dict) else {}

    entry: Dict[str, Any] = {
        "id": str(raw.get("id") or job_dir.name),
        "src": src,
        "out_path": out_path,
        "src_name": str(raw.get("src_name") or (Path(src).name if src else "")),
        "out_name": str(raw.get("out_name") or (Path(out_path).name if out_path else "")),
        "src_size": src_size,
        "total_size": total_size,
        "ratio": (total_size / src_size) if (src_size > 0 and total_size > 0) else None,
        "saved_bytes": max(0, src_size - total_size) if src_size and total_size else 0,
        "mode": str(raw.get("mode") or "compress"),
        "profile_id": str(raw.get("profile_id") or ""),
        "encoder": str(raw.get("encoder") or ""),
        "resource": str(raw.get("resource") or ""),
        "elapsed": float(raw.get("elapsed") or 0.0),
        "duration": float(raw.get("duration") or 0.0),
        "created": float(raw.get("created") or 0.0),
        "finished": float(raw.get("finished") or raw.get("created") or 0.0),
        "status": str(raw.get("status") or ""),
        "error": str(raw.get("error") or ""),
        "verdict": str(verify.get("verdict") or ""),
        "source_identity": raw.get('source_identity') or {},
        "output_identity": raw.get('output_identity') or {},
        "max_keyframe_gap": verify.get("max_keyframe_gap"),
        "keyframe_count": int(verify.get("keyframe_count") or 0),
        "checks": verify.get("checks") or [],
        # Existence is checked per listing, never cached: a user moving or deleting
        # an output file must be reflected immediately, not after the TTL.
        "src_exists": bool(src) and Path(src).is_file(),
        "out_exists": bool(out_path) and Path(out_path).is_file(),
    }
    entry["_mtime"] = stamp
    with _CACHE_LOCK:
        _CACHE[entry["id"]] = entry
    return entry


def _strip_internal(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def index(limit: int = 200, query: str = "",
          only_existing: bool = False) -> List[Dict[str, Any]]:
    """History entries, newest first.

    ``query`` matches the file names case-insensitively; ``only_existing`` keeps
    only entries whose output file is still where we put it.
    """
    entries: List[Dict[str, Any]] = []
    try:
        dirs = list(JOBS_DIR.iterdir())
    except OSError:
        return []

    needle = query.strip().lower()
    for job_dir in dirs:
        if not job_dir.is_dir():
            continue
        entry = _read_report(job_dir)
        if entry is None:
            continue
        if only_existing and not entry["out_exists"]:
            continue
        if needle:
            haystack = "{} {}".format(entry["src_name"], entry["out_name"]).lower()
            if needle not in haystack:
                continue
        entries.append(entry)

    # Newest first; jobs with no finished stamp (very old reports) sort by creation.
    entries.sort(key=lambda e: e["finished"] or e["created"], reverse=True)

    # Drop cache rows whose directory is gone, so a long-running process does not
    # grow a map of deleted ids.
    live = {e["id"] for e in entries}
    with _CACHE_LOCK:
        for key in list(_CACHE):
            if key not in live and not (JOBS_DIR / key).is_dir():
                _CACHE.pop(key, None)

    if limit and limit > 0:
        entries = entries[:limit]
    return [_strip_internal(e) for e in entries]


def stats() -> Dict[str, int]:
    """Totals over the WHOLE archive, not just the requested page."""
    entries = index(limit=0)
    present = [e for e in entries if e["out_exists"]]
    return {
        "total": len(entries),
        "available": len(present),
        "missing": len(entries) - len(present),
        "bytes_in": sum(e["src_size"] for e in present),
        "bytes_out": sum(e["total_size"] for e in present),
        "bytes_saved": sum(e["saved_bytes"] for e in present),
    }


def get(job_id: str) -> Optional[Dict[str, Any]]:
    entry = _read_report(JOBS_DIR / str(job_id))
    return _strip_internal(entry) if entry else None


def latest_for_source(src_path: str) -> Optional[Dict[str, Any]]:
    """Newest passed record whose source and delivery still match their snapshots.

    Drives "跳过已完成" (G6b): re-running a folder should not re-encode everything
    just because the queue was cleared. Only records whose file still exists count --
    a missing output means the work is genuinely not done.
    """
    wanted = _norm_path(src_path)
    best: Optional[Dict[str, Any]] = None
    for entry in index(limit=0):
        if not entry["out_exists"] or entry['status'] != 'done' or entry['verdict'] != 'passed':
            continue
        if _norm_path(entry["src"]) != wanted:
            continue
        if (not identity_matches(src_path, entry['source_identity']) or
                not identity_matches(entry['out_path'], entry['output_identity'])):
            continue
        if best is None or (entry["finished"] or 0) > (best["finished"] or 0):
            best = entry
    return best


def _norm_path(path: str) -> str:
    """Compare paths case-insensitively; Windows treats them that way."""
    return str(path or "").strip().replace("/", "\\").rstrip("\\").lower()


def _drop_record(job_id: str) -> bool:
    """Delete ``store/jobs/<id>/`` -- the report only, never a video."""
    job_dir = JOBS_DIR / str(job_id)
    # Guard against a crafted id escaping JOBS_DIR.
    try:
        if job_dir.resolve().parent != JOBS_DIR.resolve():
            return False
    except OSError:
        return False
    if not job_dir.is_dir():
        return False
    try:
        shutil.rmtree(job_dir, ignore_errors=True)
    except OSError:
        return False
    with _CACHE_LOCK:
        _CACHE.pop(str(job_id), None)
    return not job_dir.exists()


def remove(job_id: str, delete_output: bool = False) -> Dict[str, Any]:
    """Remove one history record.

    ``delete_output=True`` additionally unlinks the compressed file.  That is
    destructive and irreversible, so the caller must have obtained an explicit
    second confirmation; the *source* file is never eligible for deletion, and the
    output must be a regular file that still exists.
    """
    entry = get(job_id)
    if entry is None:
        return {"ok": False, "error": "记录不存在"}

    result: Dict[str, Any] = {"ok": True, "id": entry["id"], "output_deleted": False,
                              "record_deleted": False}
    out_path = entry["out_path"]
    if delete_output and out_path:
        if entry["src"] and Path(out_path).resolve() == Path(entry["src"]).resolve():
            result["output_error"] = "输出路径与源文件相同，已跳过删除"
        elif not entry["out_exists"]:
            result["output_error"] = "成品文件已不在原位置"
        else:
            target = Path(out_path)
            try:
                if not target.is_file():
                    result["output_error"] = "目标不是普通文件，已跳过删除"
                else:
                    target.unlink()
                    result["output_deleted"] = True
                    result["freed_bytes"] = int(entry["total_size"] or 0)
            except OSError as exc:
                result["output_error"] = "删除成品失败：{}".format(exc)

    result["record_deleted"] = _drop_record(entry["id"])
    return result


def remove_many(ids: Optional[List[str]] = None, query: str = "",
                only_existing: bool = False, all_matching: bool = False,
                delete_output: bool = False,
                expected_count: Optional[int] = None) -> Dict[str, Any]:
    """Remove several records at once -- the only sane way to clear test clutter.

    Two selection modes:
      * ``ids``               -- exactly these records
      * ``all_matching=True`` -- every record matching ``query``/``only_existing``,
                                 resolved server-side so a client page limit cannot
                                 silently leave records behind

    ``expected_count`` is the "confirm what you are about to destroy" binding: the
    caller states how many records it believes it is removing, and a mismatch aborts
    BEFORE anything is deleted. Without it, `all_matching` plus a delete flag meant the
    server resolved a scope the client never saw -- the confirmation dialog could say
    "3 selected" while the request deleted all 200.

    Deletion semantics match :func:`remove` exactly: records go, videos stay unless
    ``delete_output`` is explicitly set, and a source file is never eligible.
    """
    if all_matching:
        targets = [e["id"] for e in index(limit=0, query=query,
                                          only_existing=only_existing)]
    else:
        wanted = {str(i) for i in (ids or [])}
        targets = [e["id"] for e in index(limit=0) if e["id"] in wanted]

    if expected_count is not None and int(expected_count) != len(targets):
        return {
            "ok": False,
            "error": "记录数与你确认的不一致：你确认 {} 条，当前匹配 {} 条；"
                     "请刷新列表后重新确认".format(expected_count, len(targets)),
            "requested": len(targets),
            "removed": 0,
            "outputs_deleted": 0,
            "freed_bytes": 0,
            "skipped_source": 0,
            "missing_output": 0,
            "failures": [],
            "count_mismatch": True,
        }

    removed = 0
    outputs_deleted = 0
    freed = 0
    failures: List[Dict[str, str]] = []
    skipped_source = 0
    missing_output = 0
    for job_id in targets:
        result = remove(job_id, delete_output=delete_output)
        if result.get("record_deleted"):
            removed += 1
        else:
            failures.append({"id": job_id, "error": "记录删除失败"})
        if result.get("output_deleted"):
            outputs_deleted += 1
            freed += int(result.get("freed_bytes") or 0)
        elif result.get("output_error"):
            # An output that is already gone is the expected state for a stale
            # record, not a failure -- reporting it as one made a clean batch look
            # like it had errored.
            if "源文件相同" in result["output_error"]:
                skipped_source += 1
            elif "已不在原位置" in result["output_error"]:
                missing_output += 1
            else:
                failures.append({"id": job_id, "error": result["output_error"]})

    return {
        "ok": True,
        "requested": len(targets),
        "removed": removed,
        "outputs_deleted": outputs_deleted,
        "freed_bytes": freed,
        "skipped_source": skipped_source,
        "missing_output": missing_output,
        "failures": failures[:10],
    }


def clear(only_missing: bool = False) -> Dict[str, Any]:
    """Delete records -- only those whose output is gone, or all of them.

    Never deletes a video file, so this cannot be used to free disk space; it only
    tidies the list.
    """
    removed = 0
    skipped = 0
    for entry in index(limit=0):
        if only_missing and entry["out_exists"]:
            skipped += 1
            continue
        if _drop_record(entry["id"]):
            removed += 1
        else:
            skipped += 1
    return {"ok": True, "removed": removed, "skipped": skipped,
            "only_missing": bool(only_missing)}


def invalidate() -> None:
    """Forget the cache (used by tests and after an in-place report rewrite)."""
    with _CACHE_LOCK:
        _CACHE.clear()
