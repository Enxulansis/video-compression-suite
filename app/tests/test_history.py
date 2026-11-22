"""Self-test the history indexer against a synthetic archive.

The indexer reads `store/jobs/<id>/report.json`. Testing it against the user's real
archive would be flaky (it changes as they work), so the test points `history.JOBS_DIR`
at a temp directory and builds known reports -- including the awkward cases that
actually occur: a directory with no report, a corrupt report, an output file that has
been moved away, and a record whose source and output are the same path.

The destructive paths get the most attention: `remove`/`clear` must never unlink a
source file, and must refuse to delete anything that is not the recorded output.

Run:  D:\\anaconda3\\envs\\vcs\\python.exe tests\\test_history.py
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import history as H  # noqa: E402
from core import actions as A

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", name, " -- " + detail if detail else ""))


def write_report(root: Path, job_id: str, **over) -> None:
    job_dir = root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": job_id, "src": "", "out_path": "", "profile_id": "P3",
        "status": "done", "src_size": 0, "total_size": 0, "elapsed": 0.0,
        "created": 1_700_000_000.0, "finished": 1_700_000_000.0,
        "encoder": "libsvtav1", "mode": "compress", "resource": "cpu",
        "verify": {"verdict": "passed", "max_keyframe_gap": 1.0, "keyframe_count": 10},
    }
    data.update(over)
    data.setdefault('source_identity', A.file_identity(data['src']))
    data.setdefault('output_identity', A.file_identity(data['out_path']))
    (job_dir / "report.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_bytes(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def main() -> int:
    print("\nhistory indexer self-test\n")

    tmp = Path(tempfile.mkdtemp(prefix="vcs-hist-"))
    real_jobs_dir = H.JOBS_DIR
    H.JOBS_DIR = tmp
    H.invalidate()
    try:
        src_a = tmp / "media" / "a.mp4"
        out_a = tmp / "media" / "a_P3.mp4"
        make_bytes(src_a, 1000)
        make_bytes(out_a, 400)

        src_b = tmp / "media" / "b.mp4"
        out_b = tmp / "media" / "b_P3.mp4"          # deliberately never created
        make_bytes(src_b, 2000)

        write_report(tmp, "jobA", src=str(src_a), out_path=str(out_a),
                     src_name="a.mp4", out_name="a_P3.mp4", src_size=1000,
                     total_size=400, elapsed=12.0, finished=1_700_000_500.0)
        write_report(tmp, "jobB", src=str(src_b), out_path=str(out_b),
                     src_name="b.mp4", out_name="b_P3.mp4", src_size=2000,
                     total_size=800, finished=1_700_000_900.0)
        # A record whose output IS its source: the guard must protect it.
        write_report(tmp, "jobSame", src=str(src_a), out_path=str(src_a),
                     src_name="a.mp4", out_name="a.mp4", src_size=1000,
                     total_size=1000, finished=1_700_000_100.0)
        # Directories that must not break the listing.
        (tmp / "emptyDir").mkdir()
        (tmp / "corrupt").mkdir()
        (tmp / "corrupt" / "report.json").write_text("{not json", encoding="utf-8")
        write_report(tmp, "jobNoOutPath", src=str(src_a), out_path="",
                     src_name="a.mp4", out_name="", src_size=1000, total_size=0)

        print("  -- indexing --")
        entries = H.index(limit=0)
        check("only parseable reports are listed", len(entries) == 4, "{} entries".format(len(entries)))
        check("newest first", entries[0]["id"] == "jobB", entries[0]["id"])
        check("missing output is flagged, not hidden",
              next(e for e in entries if e["id"] == "jobB")["out_exists"] is False)
        check("present output is flagged",
              next(e for e in entries if e["id"] == "jobA")["out_exists"] is True)
        check("source existence is reported",
              next(e for e in entries if e["id"] == "jobA")["src_exists"] is True)

        a = next(e for e in entries if e["id"] == "jobA")
        check("ratio computed from the real sizes", abs(a["ratio"] - 0.4) < 1e-9, str(a["ratio"]))
        check("saved_bytes computed", a["saved_bytes"] == 600, str(a["saved_bytes"]))
        check("verify fields surfaced", a["verdict"] == "passed" and a["keyframe_count"] == 10)
        check("internal cache key is not leaked", "_mtime" not in a)

        no_out = next(e for e in entries if e["id"] == "jobNoOutPath")
        check("empty out_path yields ratio=None not a crash", no_out["ratio"] is None)

        print("  -- filtering --")
        # jobNoOutPath has no output path at all, so it has no *existing* output:
        # only jobA and jobSame qualify.
        check("only_existing filters out missing outputs",
              {e["id"] for e in H.index(limit=0, only_existing=True)} == {"jobA", "jobSame"},
              str(sorted(e["id"] for e in H.index(limit=0, only_existing=True))))
        check("query matches source name", [e["id"] for e in H.index(limit=0, query="b.mp4")] == ["jobB"])
        check("query matches output name", [e["id"] for e in H.index(limit=0, query="a_p3")] == ["jobA"])
        check("query is case-insensitive and trims", len(H.index(limit=0, query="  A_P3  ")) == 1)
        check("query with no match is empty", H.index(limit=0, query="zzz") == [])
        check("limit truncates", len(H.index(limit=2)) == 2)

        # jobA (1000->400) and jobSame (1000->1000) are the two available records.
        st = H.stats()
        check("stats count the whole archive, not one page",
              st["total"] == 4 and st["available"] == 2 and st["missing"] == 2, str(st))
        check("stats only sum available outputs",
              st["bytes_in"] == 2000 and st["bytes_out"] == 1400, str(st))
        check("stats saved is the real difference", st["bytes_saved"] == 600, str(st))

        print("  -- cache correctness --")
        H.invalidate()
        first = H.index(limit=0)
        t0 = time.time()
        H.index(limit=0)
        warm = time.time() - t0
        check("warm index is fast", warm < 1.0, "{:.4f}s".format(warm))
        check("warm index returns the same payload", first == H.index(limit=0))
        # Rewriting a report must be picked up even while the cache is warm.
        time.sleep(0.01)
        write_report(tmp, "jobA", src=str(src_a), out_path=str(out_a),
                     src_name="a.mp4", out_name="a_RENAMED.mp4", src_size=1000,
                     total_size=250, finished=1_700_000_500.0)
        fresh = next(e for e in H.index(limit=0) if e["id"] == "jobA")
        check("rewritten report invalidates its cache row",
              fresh["out_name"] == "a_RENAMED.mp4" and fresh["total_size"] == 250,
              fresh["out_name"])

        print("  -- deletion guards --")
        check("unknown id is refused", H.remove("nope")["ok"] is False)
        same = H.remove("jobSame", delete_output=True)
        check("source==output is never deleted",
              same["output_deleted"] is False and src_a.exists(),
              same.get("output_error", ""))
        gone = H.remove("jobB", delete_output=True)
        check("missing output is not a hard error",
              gone["output_deleted"] is False and gone["record_deleted"] is True,
              str(gone.get("output_error", "")))
        real = H.remove("jobA", delete_output=True)
        check("existing output is deleted when asked",
              real["output_deleted"] is True and not out_a.exists(), str(real))
        check("deleting a record keeps the source file", src_a.exists())
        check("record directory is gone", not (tmp / "jobA").exists()
              and not (tmp / "jobB").exists())

        print("  -- batch delete --")
        out_c = tmp / "media" / "c_P3.mp4"
        out_d = tmp / "media" / "d_P3.mp4"
        make_bytes(out_c, 300)
        make_bytes(out_d, 700)
        write_report(tmp, "jobC", src=str(src_b), out_path=str(out_c),
                     src_name="c.mp4", out_name="c_P3.mp4", src_size=3000,
                     total_size=300, finished=1_700_001_000.0)
        write_report(tmp, "jobD", src=str(src_b), out_path=str(out_d),
                     src_name="d.mp4", out_name="d_P3.mp4", src_size=3000,
                     total_size=700, finished=1_700_001_100.0)
        H.invalidate()

        only_ids = H.remove_many(ids=["jobC"], delete_output=False)
        check("batch by ids removes exactly those records",
              only_ids["removed"] == 1 and only_ids["requested"] == 1
              and H.get("jobC") is None and H.get("jobD") is not None,
              str(only_ids))
        check("batch without delete_output keeps the file", out_c.exists())

        by_ids_del = H.remove_many(ids=["jobD"], delete_output=True)
        check("batch can delete outputs and report the space freed",
              by_ids_del["outputs_deleted"] == 1 and by_ids_del["freed_bytes"] == 700
              and not out_d.exists(),
              str(by_ids_del))

        # all_matching must be resolved server-side, ignoring any client page limit.
        out_e = tmp / "media" / "e_P3.mp4"
        out_f = tmp / "media" / "e_P3_2.mp4"
        make_bytes(out_e, 100)
        make_bytes(out_f, 100)
        write_report(tmp, "jobE1", src=str(src_b), out_path=str(out_e),
                     src_name="e.mp4", out_name="e_P3.mp4", src_size=1000,
                     total_size=100, finished=1_700_001_200.0)
        write_report(tmp, "jobE2", src=str(src_b), out_path=str(out_f),
                     src_name="e.mp4", out_name="e_P3_2.mp4", src_size=1000,
                     total_size=100, finished=1_700_001_300.0)
        make_bytes(out_d, 700)
        write_report(tmp, "jobKeep", src=str(src_b), out_path=str(out_d),
                     src_name="keep.mp4", out_name="keep_P3.mp4", src_size=1000,
                     total_size=100, finished=1_700_001_400.0)
        H.invalidate()
        matched = H.remove_many(all_matching=True, query="e.mp4", delete_output=False)
        check("batch by filter removes every match", matched["removed"] == 2, str(matched))
        check("batch by filter spares non-matching records",
              H.get("jobKeep") is not None and H.get("jobE1") is None and H.get("jobE2") is None)

        # Source protection must hold in the batch path too.
        write_report(tmp, "jobSelf", src=str(src_b), out_path=str(src_b),
                     src_name="b.mp4", out_name="b.mp4", src_size=2000,
                     total_size=2000, finished=1_700_001_500.0)
        H.invalidate()
        self_del = H.remove_many(ids=["jobSelf"], delete_output=True)
        check("batch never deletes a file that is its own source",
              self_del["skipped_source"] == 1 and src_b.exists(), str(self_del))
        check("empty selection is a no-op", H.remove_many(ids=[])["removed"] == 0)

        print("  -- latest_for_source (drives 跳过已完成) --")
        H.invalidate()
        make_bytes(out_a, 400)
        write_report(tmp, "jobSrc", src=str(src_a), out_path=str(out_a),
                     src_name="a.mp4", out_name="a.mp4", src_size=1000,
                     total_size=1000, finished=1_700_002_000.0)
        H.invalidate()
        check("a completed record is found by its source path",
              (H.latest_for_source(str(src_a)) or {}).get("id") == "jobSrc",
              str(H.latest_for_source(str(src_a))))
        check("the lookup is case-insensitive",
              (H.latest_for_source(str(src_a).upper()) or {}).get("id") == "jobSrc")
        check("forward slashes in the query still match",
              (H.latest_for_source(str(src_a).replace("\\", "/")) or {}).get("id") == "jobSrc")
        check("an unknown source finds nothing", H.latest_for_source(r"E:\nope.mp4") is None)
        # A record whose output is gone is NOT "done" -- the work still needs doing.
        src_a.unlink()
        H.invalidate()
        check("a record with a missing output does not count as done",
              H.latest_for_source(str(src_a)) is None)
        make_bytes(src_a, 1000)
        H.invalidate()
        check("a replaced source is not skipped using the old report", H.latest_for_source(str(src_a)) is None)
        write_report(tmp, "jobSrc", src=str(src_a), out_path=str(out_a),
                     src_size=1000, total_size=400, finished=1_700_003_000.0)
        H.invalidate()
        check("a newer record wins",
              (H.latest_for_source(str(src_a)) or {}).get("id") == "jobSrc")

        print("  -- clear --")
        cleared = H.clear(only_missing=True)
        check("only_missing clears just the stale records", cleared["removed"] >= 0, str(cleared))
        check("an available record survives only_missing clear",
              H.get("jobKeep") is not None, str([e["id"] for e in H.index(limit=0)]))
        check("clear never deletes a video file", out_d.exists() and src_b.exists())

        allgone = H.clear(only_missing=False)
        check("full clear empties the list", H.index(limit=0) == [], str(allgone))
        check("full clear still leaves the media alone", out_d.exists() and src_b.exists())

        print("  -- path-traversal guard --")
        outside = tmp.parent / "vcs-hist-outside"
        outside.mkdir(exist_ok=True)
        escaped = H.remove("..\\{}".format(outside.name))
        check("an id that escapes JOBS_DIR is refused", escaped["ok"] is False, str(escaped))
        shutil.rmtree(outside, ignore_errors=True)
    finally:
        H.JOBS_DIR = real_jobs_dir
        H.invalidate()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
