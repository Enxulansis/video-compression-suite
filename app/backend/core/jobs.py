"""Job manager: queue, subprocess supervision, progress parsing, verification.

One worker thread per queue slot (default: 1 GPU slot, 1 CPU slot).  A job goes
through a strictly forward-only state machine:

    queued -> running -> verifying -> done
                      \\-> failed | cancelled

State is persisted to store/queue.json after every transition, so closing the
browser (or killing the server) never loses the queue: on restart, jobs that
were mid-flight are marked `interrupted` and can be retried with one click.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict, fields, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from .caps import get_caps, subprocess_kwargs
from .planner import Overrides, Plan, get_profile, make_plan
from .probe import MediaInfo, probe
from .quality import measure as measure_quality
from .sizing import resolve_resource
from . import crop
from . import audio
from . import logs
from . import actions
from . import paths
from . import winapi
from . import space
from . import runtime
from .verifier import VerifyResult, verify

#: The live queue/archive lives under backend/store.  `VCS_STORE_DIR` redirects it so
#: an automated UI probe can drive a second instance without touching the user's real
#: queue and settings -- running the probe against the live port used to clear the
#: user's finished jobs and flip their saved options.
STORE_DIR = runtime.store_dir()
JOBS_DIR = STORE_DIR / "jobs"
QUEUE_FILE = STORE_DIR / "queue.json"

TERMINAL = ("done", "failed", "cancelled", "interrupted")

#: Human-readable status names for messages that reach the user. The UI has its own
#: copy of this for rendering; the server needs one too, because "当前：queued" in an
#: error message is an internal word leaking into the interface.
STATUS_LABEL = {
    "queued": "排队中",
    "running": "压缩中",
    "paused": "已暂停",
    "verifying": "校验中",
    "done": "完成",   # 与前端 STATUS_TEXT 保持一致（两处不一致时同一个状态会显示成两个词）
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}


def status_label(status: str) -> str:
    return STATUS_LABEL.get(str(status), str(status))


#: Default completion policy (G8). Everything off: a tool that beeps, shuts down or
#: deletes things without being asked is a tool nobody trusts with a batch.
DEFAULT_ON_SUCCESS: Dict[str, Any] = {
    "notify": False,             # system sound when the batch drains
    "power": "none",             # none | shutdown
    "power_delay": actions.DEFAULT_POWER_DELAY,
    "power_only_if_all_ok": True,
}


#: Overrides the SERVER fills in after measuring the source. A client may not set them:
#: a stale or invented loudness measurement would silently produce the wrong static gain
#: in the two-pass filter, and the whole point of measuring is that the number is real.
INTERNAL_OVERRIDE_KEYS = {"loudness_measured"}

#: Lifecycle timeline cap. A long encode with many verify stages must not grow the queue
#: file without bound; the tail is what a diagnosis needs anyway.
MAX_EVENTS = 200



def add_event(job: Job, kind: str, text: str = "") -> None:
    """Append one timeline entry (F8). Never raises, never blocks."""
    try:
        job.events.append({"t": round(time.time(), 3), "kind": str(kind),
                           "text": str(text)[:600]})
        if len(job.events) > MAX_EVENTS:
            del job.events[0:len(job.events) - MAX_EVENTS]
    except Exception:  # noqa: BLE001 - logging must never break an encode
        pass


def trim_log(path: Path, keep: int = None) -> Dict[str, Any]:
    """Kept as a thin alias so old callers keep working; the logic lives in core.logs."""
    return logs.trim_log(path, keep if keep is not None else logs.LOG_KEEP_BYTES)


# NVENC on consumer GPUs historically allows a limited number of concurrent
# sessions; keep GPU work serialised unless the user says otherwise.
DEFAULT_CONCURRENCY = 1


@dataclass
class Job:
    id: str
    src: str
    profile_id: str
    out_path: str = ""
    command: str = ""
    argv: List[str] = field(default_factory=list)
    status: str = "queued"
    progress: float = 0.0
    fps: float = 0.0
    speed: float = 0.0
    out_time: float = 0.0
    total_size: int = 0
    duration: float = 0.0
    src_size: int = 0
    encoder: str = ""
    warnings: List[str] = field(default_factory=list)
    rules_applied: List[str] = field(default_factory=list)
    error: str = ""
    verify: Optional[Dict[str, Any]] = None
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    elapsed: float = 0.0
    overrides: Dict[str, Any] = field(default_factory=dict)
    media: Dict[str, Any] = field(default_factory=dict)
    degraded: bool = False        # safety rules relaxed or auto-fallback used
    mode: str = "compress"        # compress | repair
    seek: Optional[Dict[str, Any]] = None   # repair-mode diagnosis (before/after)
    resource: str = "cpu"         # gpu | cpu -- which slot pool this job needs
    verify_stage: str = ""        # probe | decode | keyframes (during verification)
    #: Encoder process id while this job is in flight. Persisted for one reason: if the
    #: app is killed mid-encode the process survives (Windows does not kill children),
    #: and a SUSPENDED survivor holds the output file forever. The next start reads this
    #: back and cleans it up.
    pid: int = 0
    pid_created: int = 0
    #: Pause bookkeeping (G6). `paused_total` is subtracted from both the elapsed time
    #: and the stall guard, so a long pause is never mistaken for a stuck encoder.
    paused_at: float = 0.0
    paused_total: float = 0.0
    pause_reason: str = ""        # user | memory (automatic resume only for memory)
    #: Structured lifecycle timeline (F8). Warnings and errors say WHAT happened; this
    #: says WHEN, which is the difference between "it failed" and "it failed right after
    #: the disk check". Capped by `add_event`.
    events: List[Dict[str, Any]] = field(default_factory=list)
    #: Set when a large ffmpeg log was shortened (F8): {trimmed, kept_bytes,
    #: original_bytes}. Reported so a short log is never mistaken for a complete one.
    log_trimmed: Dict[str, Any] = field(default_factory=dict)
    verify_percent: Optional[float] = None
    verify_frames: int = 0
    #: Estimated peak committed memory of this encode (MB), copied from the plan.
    est_memory_mb: int = 0
    #: Why a queued job has not started yet ("等待内存：…"). Empty when it is just waiting
    #: its turn. Shown on the card so "nothing is happening" always has a reason.
    waiting_reason: str = ""
    intent: Dict[str, Any] = field(default_factory=dict)
    decision: List[str] = field(default_factory=list)
    est_output_bytes: int = 0
    cleanup_batch: str = ""
    recycle_source: bool = False
    source_identity: Dict[str, Any] = field(default_factory=dict)
    output_identity: Dict[str, Any] = field(default_factory=dict)
    source_cleanup: str = ""      # pending | processing | recycled | kept | review
    source_cleanup_note: str = ""
    source_recovery_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["src_name"] = Path(self.src).name
        data["out_name"] = Path(self.out_path).name if self.out_path else ""
        if self.status in TERMINAL and self.started and not self.elapsed:
            data["elapsed"] = (self.finished or time.time()) - self.started
        # Derived, read-only fields for the UI. They are NOT part of the dataclass, so
        # `_load` filters them back out (see there) -- adding them here is what broke
        # queue restore once, and the filter is what makes it safe.
        data["eta_seconds"] = self.eta_seconds()
        data["paused_seconds"] = self.paused_seconds()
        return data

    def paused_seconds(self) -> float:
        """How long this job has been sitting paused RIGHT NOW (0 when not paused).

        The card used to say only "已暂停", so a pause left overnight looked the same as
        one from ten seconds ago.
        """
        if self.status != "paused" or not self.paused_at:
            return 0.0
        return max(0.0, time.time() - self.paused_at)

    def eta_seconds(self) -> float:
        """Remaining encode time, from the progress the encoder itself reported.

        Deliberately returns 0 until there is enough information to say anything: at
        0–2% progress an ETA computed from `elapsed / progress` is dominated by startup
        (probing, filter init, the first GOP), and a number that swings between 3 minutes
        and 3 hours is worse than no number. 0 means "unknown", and the UI simply omits
        the field.
        """
        if self.status != "running":
            return 0.0
        progress = float(self.progress or 0.0)
        elapsed = float(self.elapsed or 0.0)
        if progress < 2.0 or elapsed <= 1.0:
            return 0.0
        total = elapsed * 100.0 / progress
        remaining = total - elapsed
        # Below a second is noise; cap at a week so a stalled job cannot print a silly
        # number (the stall watchdog handles a genuinely stuck encode).
        if remaining < 1.0:
            return 0.0
        return min(remaining, 7 * 24 * 3600.0)


class JobManager:
    def __init__(self, concurrency: Optional[int] = None) -> None:
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._procs: Dict[str, subprocess.Popen] = {}
        self._working_ids: set[str] = set()
        self._submitting_sources: Dict[str, List[str]] = {}
        self._submitting_batches: set[str] = set()
        # Concurrency is tracked per resource pool, not as one global number: NVENC
        # jobs occupy the video engine while software encoders occupy CPU cores, and
        # they do not compete, so one GPU job and one CPU job can (and should) run
        # side by side instead of queueing behind each other.
        self._limits: Dict[str, int] = dict(self._auto_limits())
        self._busy: Dict[str, int] = {"gpu": 0, "cpu": 0}
        self._aux_reserved_mb = 0
        self._seek_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vcs-seek")
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        # Seekability scan: results stay here so a repeated /api/probe is instant,
        # and the in-flight set prevents duplicate scans of the same file.
        self.seek_results: Dict[str, Dict[str, Any]] = {}
        self._seek_scanning: set = set()
        self._retain_probe: Dict[str, MediaInfo] = {}
        #: path|mtime|size -> MediaInfo, so batch totals need no extra ffprobe runs.
        self._probe_cache: Dict[str, MediaInfo] = {}
        # ---- memory guard (see core/memory.py and the incident investigation) ----------
        #: The guard can be switched off, but ON by default: leaving it off is what froze a
        #: 31.6 GB machine with two 1080p SVT-AV1 jobs on a box already holding ~27 GB.
        self._memory_guard: bool = True
        #: Safety margin in MB (None = core.memory's default).
        self._memory_margin_mb: Optional[int] = None
        #: Start of a continuous CRITICAL-memory interval for each automatic pause.
        self._memory_critical_since: Dict[str, float] = {}
        #: Low-water threshold in MB (None = core.memory default), settable from the UI.
        self._memory_low_water_mb: Optional[int] = None
        #: A queue restored from disk is HELD until the user says go (see `_load`).
        self._restored_ids: set[str] = set()
        # Completion policy for the CURRENT batch (G8): run when the queue drains.
        # Persisted with the queue, because "shut down when done" has to survive a
        # restart mid-batch or the user sits waiting for something that will not happen.
        self._on_success: Dict[str, Any] = dict(DEFAULT_ON_SUCCESS)
        self._power_pending: Dict[str, Any] = {}
        #: Set when the queue file could not be parsed (surfaced instead of silent loss).
        self._load_error = ""
        self._persist_seq = itertools.count(1)
        # Non-blocking mutex so the batch actions run exactly once (see
        # _run_completion_actions): every finishing worker calls it.
        self._completion_lock = threading.Lock()
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self._load()
        from . import settings
        saved = settings.load()
        saved_limits = saved.get("limits") or {}
        if isinstance(saved_limits, dict):
            self.set_limits(saved_limits)
        else:
            self._load_error += " 已保存的并行设置无效，使用推荐值。"
        try:
            self.set_memory_policy(saved.get("memory_guard", True),
                                   saved.get("memory_margin_mb", 2500),
                                   saved.get("memory_low_water_mb", 1500))
        except ValueError as exc:
            self._load_error += " 已保存的内存设置无效，使用安全默认值：{}".format(exc)
        if concurrency is not None:
            self.set_concurrency(concurrency)

    @staticmethod
    def _auto_limits() -> Dict[str, int]:
        try:
            from .sizing import recommend
            rec = recommend()
            return {"gpu": max(1, int(rec.get("gpu") or 1)),
                    "cpu": max(1, int(rec.get("cpu") or 1))}
        except Exception:  # noqa: BLE001
            return {"gpu": 1, "cpu": 1}

    @property
    def _concurrency(self) -> int:
        """Total slots, kept for the existing UI/reporting."""
        return sum(self._limits.values())

    # ------------------------------------------------------------- persistence

    def _load(self) -> None:
        """Restore the queue written by `_persist`.

        The stored dicts come from `Job.to_dict()`, which adds derived keys on top of
        the dataclass fields (`src_name`, `out_name`).  Rebuilding with `Job(**item)`
        therefore raised `TypeError: unexpected keyword argument 'src_name'` for EVERY
        record, and the bare `except TypeError: continue` swallowed it -- so a restart
        silently restored an empty queue while the file on disk still looked healthy.
        That is why "reopen the app and your queue is still there" did not hold.

        Filtering to the declared field names fixes it and stays correct when fields
        are added or removed later.
        """
        if not QUEUE_FILE.is_file():
            return
        try:
            raw = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("jobs", []), list):
                raise ValueError("队列根节点必须是对象，jobs 必须是数组")
        except (OSError, ValueError) as exc:
            # A queue that cannot be parsed used to be treated as EMPTY, so a torn or
            # truncated write silently discarded every job (with the archive still on
            # disk, which made it look like nothing had ever been queued). Keep the
            # evidence and say so instead.
            self._load_error = "队列文件无法解析（{}），已保留为 .corrupt 并当作空队列：{}".format(
                type(exc).__name__, exc)
            try:
                QUEUE_FILE.replace(QUEUE_FILE.with_suffix(".json.corrupt"))
            except OSError:
                pass
            return
        # Power consent survives only an unfinished queue. Source consent is handled
        # separately on each job below and never resumes a destructive action at boot.
        stored_policy = raw.get("on_success")
        if isinstance(stored_policy, dict):
            for key, default in DEFAULT_ON_SUCCESS.items():
                value = stored_policy.get(key, default)
                self._on_success[key] = value if isinstance(value, type(default)) else default
        allowed = {f.name for f in fields(Job)}
        for item in raw.get("jobs", []):
            if not isinstance(item, dict):
                continue
            # A truncated or hand-edited record must not stop the app from starting:
            # Job() still requires `id`, `src` and `profile_id`.
            try:
                job = Job(**{k: v for k, v in item.items() if k in allowed})
            except (TypeError, ValueError):
                continue
            if (not all(isinstance(getattr(job, key), str) and getattr(job, key)
                        for key in ("id", "src", "profile_id"))
                    or not isinstance(job.status, str) or job.status not in STATUS_LABEL
                    or not isinstance(job.resource, str) or job.resource not in ("cpu", "gpu")
                    or type(job.est_memory_mb) is not int or job.est_memory_mb < 0
                    or any(type(getattr(job, key)) not in (int, float)
                           or not math.isfinite(getattr(job, key))
                           for key in ("started", "finished", "elapsed", "paused_at", "paused_total", "progress"))
                    or type(job.pid) is not int or job.id in self._jobs):
                continue
            if job.status == "queued":
                self._restored_ids.add(job.id)
            if job.status in ("running", "verifying", "paused"):
                # A record still marked in-flight means the previous process died before
                # it could finish -- and on Windows its ffmpeg did NOT die with it.
                self._reap_survivor(job)
                was_paused = job.status == "paused"
                job.status = "interrupted"
                job.pause_reason = ""
                job.paused_at = 0.0
                job.error = ("服务重启时该任务处于暂停状态，编码进程已终止，可点击重试"
                             if was_paused else "服务重启，任务被中断，可点击重试")
            if job.source_cleanup in ("pending", "processing") or job.recycle_source:
                # Opening the app must never resume a destructive action by itself.
                interrupted_cleanup = job.source_cleanup == 'processing'
                job.recycle_source = False
                job.source_cleanup = 'review' if interrupted_cleanup else 'kept'
                job.source_cleanup_note = ('上次源文件处理被中断，结果待核查；请检查原路径与回收站'
                                           if interrupted_cleanup else
                                           '应用已重启，源文件处理授权已取消；请确认成品后手动清理')
                if interrupted_cleanup and isinstance(job.source_recovery_path, str) and job.source_recovery_path:
                    job.source_cleanup_note += '；临时恢复副本位置：' + job.source_recovery_path
                job.warnings.append(job.source_cleanup_note)
            self._jobs[job.id] = job
            self._order.append(job.id)

        # A RESTORED queue must not start encoding by itself.
        #
        # Reported from real use: "I open the app and it immediately starts running, and the
        # machine becomes very laggy". The cause was this restore path -- jobs still marked
        # `queued` on disk were handed straight to the dispatcher the moment the server
        # came up (and the queue held 8 A-tier jobs from the frozen session). Opening the
        # app must be a quiet action: the user gets a banner with a button, and nothing is
        # encoded until they press it.

        # Nothing was in flight, so the batch the user consented for is over: drop back
        # to the defaults. Without this, "shut down when done" ticked once would still be
        # armed a week later and would fire on the next single-file job.
        if not any(j.status in ("queued", "running", "verifying", "paused")
                   for j in self._jobs.values()):
            if self._on_success != DEFAULT_ON_SUCCESS:
                self._on_success = dict(DEFAULT_ON_SUCCESS)
                self._persist()

    def release_restored(self) -> Dict[str, Any]:
        """Let a RESTORED queue start running (the user's explicit "go")."""
        with self._lock:
            count = self.restore_state()["queued"]
            self._restored_ids.clear()
        self._kick()
        return {"ok": True, "released": count}

    def restore_state(self) -> Dict[str, Any]:
        """Whether a restored queue is being held (for the UI banner)."""
        with self._lock:
            self._restored_ids.intersection_update(
                jid for jid, job in self._jobs.items() if job.status == "queued")
            return {"held": bool(self._restored_ids), "queued": len(self._restored_ids)}

    def discard_restored(self) -> Dict[str, Any]:
        """Remove only restored pending records; never release work or delete media."""
        with self._lock:
            self.restore_state()
            ids = set(self._restored_ids)
            for jid in ids:
                self._jobs.pop(jid, None)
                self._retain_probe.pop(jid, None)
            self._order[:] = [jid for jid in self._order if jid not in ids]
            self._restored_ids.clear()
        self._persist()
        self._kick()
        return {"ok": True, "removed": len(ids)}

    def _pid_is_tracked_ffmpeg(self, job_id: str, pid: int) -> bool:
        """Keep identity and control on the same owned process handle."""
        with self._lock:
            proc = self._procs.get(job_id)
            job = self._jobs.get(job_id)
            if proc is not None and job is not None and proc.pid == pid and proc.poll() is None:
                handle = getattr(proc, '_handle', None)
                if handle is not None:
                    image = winapi.process_image_path(pid, handle=int(handle))
                    return bool(image and Path(image).resolve() == Path(job.argv[0]).resolve())
        return False

    @staticmethod
    def _handle_args(proc):
        handle = getattr(proc, '_handle', None)
        return {'handle': int(handle)} if handle is not None else {}

    @staticmethod
    def _reap_survivor(job: Job) -> None:
        """Kill the encoder a dead app left behind.

        Windows does not kill children when the parent dies, so an encode that was in
        flight (or WORSE: suspended by pause) keeps running and keeps the output file
        open. A suspended survivor never finishes, so without this the file stays locked
        until reboot -- the concrete trap behind "pause, then close the app".

        Full path and creation time are checked on the same handle used to terminate.
        """
        pid = int(job.pid or 0)
        if not pid:
            return
        try:
            if not job.pid_created or not job.argv:
                job.warnings.append('遗留进程 pid {} 缺少身份记录，未执行终止，请检查上次编码进程'.format(pid))
                return
            result = winapi.terminate_process(pid, resume_first=True,
                                             expected_image=job.argv[0], expected_created=job.pid_created)
            if result.get("ok"):
                job.warnings.append("已清理上次退出时遗留的编码进程（pid {}）".format(pid))
            elif winapi.is_alive(pid):
                job.warnings.append('遗留进程未清理：{}'.format(result.get('error') or '身份无法确认'))
        finally:
            # The pid is a post-mortem record either way: once we have looked, it is
            # stale whether the process was gone, re-used by something else, or killed.
            job.pid = 0
            job.pid_created = 0

    def _persist(self) -> bool:
        """Write the queue to disk, atomically and thread-safely.

        Two defects were fixed here after the 2026-10-05 review:

        * **No lock and a FIXED temp filename.** Callers persist from the HTTP threads and
          from the worker threads, so two writers could interleave on the same
          `queue.json.tmp` -- one `os.replace` succeeding and the other raising, or worse,
          a torn payload being swapped in as the real queue.
        * **A malformed file was silently read as an empty queue** (`_load` swallowed the
          ValueError), so a torn write would have made every job disappear with no error.
          A bad file is now moved aside and reported.
        """
        # One reentrant lock serialises both snapshots and writes. A separate write
        # lock taken before the queue lock deadlocks with pause/watchdog callers that
        # already own the queue lock and need to persist their transition.
        with self._lock:
            payload = {
                "version": 1,
                "saved": time.time(),
                "jobs": [self._jobs[jid].to_dict()
                         for jid in self._order if jid in self._jobs],
                "on_success": dict(self._on_success),
                "power": dict(self._power_pending),
            }
            # Unique name per write: two writers can never share a temp file.
            tmp = QUEUE_FILE.with_name("{}.{}.{}.tmp".format(
                QUEUE_FILE.name, os.getpid(), next(self._persist_seq)))
            try:
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                               encoding="utf-8")
                os.replace(str(tmp), str(QUEUE_FILE))
                return True
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return False

    # ------------------------------------------------------- completion (G8) ---

    def on_success_policy(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._on_success)

    def set_on_success(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Update the completion policy. Unknown keys are refused, not ignored.

        Source recycling is authorized only by a confirmed batch submission.
        """
        unknown = [k for k in (patch or {}) if k not in DEFAULT_ON_SUCCESS]
        if unknown:
            return {"ok": False, "error": "不认识的完成动作参数：{}".format("、".join(unknown))}
        with self._lock:
            for key, value in (patch or {}).items():
                default = DEFAULT_ON_SUCCESS[key]
                if isinstance(default, bool):
                    self._on_success[key] = bool(value)
                elif isinstance(default, int):
                    try:
                        self._on_success[key] = int(value)
                    except (TypeError, ValueError):
                        return {"ok": False, "error": "{} 必须是整数".format(key)}
                else:
                    self._on_success[key] = value
            if self._on_success.get("power") not in ("none",) + actions.POWER_ACTIONS:
                self._on_success["power"] = "none"
            self._on_success["power_delay"] = actions.clamp_delay(
                self._on_success.get("power_delay"))
            policy = dict(self._on_success)
        self._persist()
        self.broadcast({"event": "on_success", "on_success": policy})
        return {"ok": True, "on_success": policy}

    @contextmanager
    def _source_submission(self, selected):
        token = uuid.uuid4().hex
        with self._lock:
            self._submitting_sources[token] = list(map(str, selected))
        try:
            yield
        finally:
            with self._lock:
                self._submitting_sources.pop(token, None)
            self._run_source_cleanups()

    def submit_batch(self, entries, *, recycle_sources=False, confirm_recycle=False):
        """One consent scope across all selections; every partial outcome is returned."""
        if type(recycle_sources) is not bool or (recycle_sources and confirm_recycle is not True):
            raise ValueError("处理源文件需要本次提交的明确二次确认")
        batch = uuid.uuid4().hex
        created, errors = [], []
        with self._lock:
            self._submitting_batches.add(batch)
        try:
            with self._source_submission([e['path'] for e in entries]):
                for entry in entries:
                    try:
                        created.extend(self.submit([entry['path']], entry['profile'], entry['overrides'],
                                                   cleanup_batch=batch, recycle_source=recycle_sources))
                    except Exception as exc:
                        errors.append({'path': entry['path'], 'error': str(exc)})
        finally:
            with self._lock:
                self._submitting_batches.discard(batch)
            self._persist()
        self._run_source_cleanups()
        return {'jobs': created, 'errors': errors, 'batch_id': batch}

    def _cleanup_result(self, job, state, note):
        job.source_cleanup = state
        job.source_cleanup_note = note
        if state != 'pending':
            job.recycle_source = False
            job.warnings.append(note)
            add_event(job, 'source_cleanup', note)
        report_path = JOBS_DIR / job.id / 'report.json'
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding='utf-8'))
                report.update(job.to_dict())
                temp = report_path.with_suffix('.cleanup.tmp')
                temp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
                os.replace(temp, report_path)
            except (OSError, ValueError) as exc:
                job.warnings.append('源文件处理报告更新失败：{}'.format(exc))
        self._emit(job)

    def _run_source_cleanups(self):
        """Settle only sealed batches; serialize against submission, retry and clear."""
        with self._lock:
            changed = False
            for job in list(self._jobs.values()):
                if not job.recycle_source or job.source_cleanup != 'pending':
                    continue
                members = [j for j in self._jobs.values() if j.cleanup_batch == job.cleanup_batch]
                if (not job.cleanup_batch or job.cleanup_batch in self._submitting_batches or
                        any(j.status not in TERMINAL or j.id in self._working_ids for j in members)):
                    continue
                aliases = [j for j in self._jobs.values()
                           if paths.is_same_file(Path(j.src), Path(job.src))]
                submitting = any(paths.is_same_file(Path(p), Path(job.src))
                                 for selected in self._submitting_sources.values() for p in selected)
                if submitting or any(j.status not in TERMINAL or j.id in self._working_ids for j in aliases):
                    note = '等待同源任务完成后处理源文件'
                    if job.source_cleanup_note != note:
                        self._cleanup_result(job, 'pending', note)
                    continue
                targets = [j for j in aliases if j.recycle_source and j.source_cleanup == 'pending'
                           and os.path.normcase(os.path.abspath(j.src)) == os.path.normcase(os.path.abspath(job.src))]
                if any(j.cleanup_batch in self._submitting_batches or
                       any(m.cleanup_batch == j.cleanup_batch and
                           (m.status not in TERMINAL or m.id in self._working_ids)
                           for m in self._jobs.values()) for j in targets):
                    continue
                reason = ''
                if any(j.status != 'done' or (j.verify or {}).get('verdict') != 'passed' for j in aliases):
                    reason = '同源任务有失败、取消、中断或校验风险，保留源文件以供检查／重试'
                for target in targets:
                    if reason:
                        break
                    reason = actions.safe_to_recycle(target.src, target.out_path,
                                                     (target.verify or {}).get('verdict', ''),
                                                     exists=Path(target.src).is_file())
                    if not reason and not actions.identity_matches(target.src, target.source_identity):
                        reason = '源文件身份或内容已变化，或无法确认，保留源文件'
                    if not reason and not actions.identity_matches(target.out_path, target.output_identity):
                        reason = '成品已不存在、被修改或身份无法确认，保留源文件'
                if reason:
                    for target in targets:
                        self._cleanup_result(target, 'kept', '未处理源文件：' + reason)
                    changed = True
                    continue
                recovery_path = str(Path(job.src).with_name('.vcs-recovery-' + uuid.uuid4().hex))
                for target in targets:
                    target.source_cleanup = 'processing'
                    target.source_recovery_path = recovery_path
                if not self._persist():
                    for target in targets:
                        self._cleanup_result(target, 'kept', '未处理源文件：无法保存本次授权的处理状态')
                    changed = True
                    continue
                # Recheck after the durable state write, immediately before the shell call.
                if (not actions.identity_matches(job.src, job.source_identity) or
                        any(not actions.identity_matches(t.out_path, t.output_identity) for t in targets)):
                    result = {'ok': False, 'error': '文件在处理前发生变化，已保留源文件'}
                else:
                    try:
                        result = actions.recycle(job.src, recovery_path=recovery_path,
                                                 expected_identity=job.source_identity)
                    except Exception as exc:
                        result = {'ok': False, 'error': str(exc)}
                note = ('源文件已移入回收站（可还原，清空回收站后释放空间）' if result.get('ok') else
                        '未处理源文件：' + (result.get('error') or '回收站操作失败'))
                for target in targets:
                    self._cleanup_result(target, 'recycled' if result.get('ok') else 'kept', note)
                changed = True
            if changed:
                self._persist()

    def _run_completion_actions(self) -> None:
        """Fire the batch-level actions once the queue has drained.

        Guarded by a NON-blocking lock: every job's worker thread calls this as it
        finishes, and when the last two jobs end together they would both observe an
        empty queue and each arm a shutdown (or beep twice). Only one caller runs the
        actions; the rest return immediately.
        """
        if not self._completion_lock.acquire(blocking=False):
            return
        try:
            self._run_completion_actions_locked()
        finally:
            self._completion_lock.release()

    def _run_completion_actions_locked(self) -> None:
        with self._lock:
            if self.active_count() > 0:
                return
            policy = dict(self._on_success)
            done = [j for j in self._jobs.values() if j.status == "done"]
            failed = [j for j in self._jobs.values() if j.status == "failed"]
            if not done and not failed:
                return
            if self._power_pending.get("armed"):
                return          # already scheduled; do not schedule twice
        summary = {"done": len(done), "failed": len(failed),
                   "bytes_in": sum(j.src_size for j in done),
                   "bytes_out": sum(j.total_size for j in done)}

        if policy.get("notify"):
            result = actions.notify()
            summary["notify"] = result.get("ok")

        power = str(policy.get("power") or "none")
        if power != "none":
            if policy.get("power_only_if_all_ok") and failed:
                summary["power"] = "skipped:有失败任务"
            else:
                reason = "视频压缩套件：{} 个完成、{} 个失败".format(len(done), len(failed))
                result = actions.power_action(power, int(policy.get("power_delay") or 60),
                                              reason)
                summary["power"] = "armed" if result.get("ok") else result.get("error")
                if result.get("ok"):
                    with self._lock:
                        self._power_pending = {
                            "armed": True,
                            "kind": power,
                            "delay": result.get("delay"),
                            "at": time.time(),
                            "summary": summary,
                        }
        self.broadcast({"event": "on_success_ran", "summary": summary})
        # Power consent is one-shot; notify remains enabled. Source consent belongs
        # to the separately confirmed job batch, not this queue-wide policy.
        with self._lock:
            used = []
            if self._on_success.get("power") not in (None, "none"):
                used.append("关机")
                self._on_success["power"] = DEFAULT_ON_SUCCESS["power"]
            if used:
                summary["reset"] = "、".join(used)
                self._persist()
        if summary.get("reset"):
            self.broadcast({"event": "on_success_ran", "summary": summary})
        return

    def cancel_power(self) -> Dict[str, Any]:
        """Cancel a scheduled shutdown and clear the armed flag."""
        result = actions.cancel_power()
        with self._lock:
            armed = dict(self._power_pending)
            self._power_pending = {}
        self.broadcast({"event": "power_cancelled"})
        was_armed = bool(armed.get("armed"))
        if result.get("ok"):
            return {"ok": True, "cancelled": True, "was_armed": was_armed}
        # "Nothing was pending" is SUCCESS, not an error: the caller asked for the machine
        # to stay on and it will. `shutdown /a` exits non-zero when no shutdown is
        # pending, which is a fact about the Windows CLI, not about the outcome.
        if not was_armed:
            return {"ok": True, "cancelled": False, "was_armed": False,
                    "note": "当前没有待执行的关机"}
        return {"ok": False, "error": result.get("error") or "取消失败"}

    def power_state(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._power_pending)

    # -------------------------------------------------------------- listeners

    def add_listener(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _emit(self, job: Job, event: str = "update") -> None:
        payload = {"event": event, "job": job.to_dict()}
        for fn in list(self._listeners):
            try:
                fn(payload)
            except Exception:  # noqa: BLE001 - a dead listener must not kill the queue
                pass

    def broadcast(self, payload: Dict[str, Any]) -> None:
        """Push an arbitrary event to every listener (used for seek scan results)."""
        for fn in list(self._listeners):
            try:
                fn(payload)
            except Exception:  # noqa: BLE001
                pass

    def scan_seek_async(self, paths: List[str]) -> None:
        """Scan keyframe layout off the request thread and stream the answers.

        The seekability scan decodes the keyframe index of the whole file, so it is
        orders of magnitude slower than the metadata probe. Letting it block
        /api/probe made adding a large folder feel frozen; the file list now shows
        up immediately with the check running behind it.
        """
        from . import work
        def worker(path) -> None:
            from .repair import diagnose
            try:
                with work.background(self):
                    diag = diagnose(path)
                    payload = {"verdict": diag.verdict, "seek_ok": diag.seek_ok,
                               "keyframes": diag.keyframes, "max_keyframe_gap": diag.max_keyframe_gap,
                               "summary": diag.summary(), "reasons": diag.reasons}
            except Exception as exc:
                payload = {"verdict": "unknown", "seek_ok": None,
                           "summary": "无法判定，请重试: {}".format(exc), "reasons": []}
            finally:
                with self._lock:
                    self._seek_scanning.discard(path)
            self.seek_results[path] = payload
            self.broadcast({"event": "seek", "path": path, "seek": payload})

        with self._lock:
            for path in dict.fromkeys(paths):
                if path in self._seek_scanning:
                    continue
                if len(self._seek_scanning) >= 32:
                    payload = {"verdict": "unknown", "seek_ok": None,
                               "summary": "后台检查队列已满，请稍后重试", "reasons": []}
                    self.seek_results[path] = payload
                    self.broadcast({"event": "seek", "path": path, "seek": payload})
                    continue
                self._seek_scanning.add(path)
                self._seek_pool.submit(worker, path)

    # ------------------------------------------------------------------- query

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._jobs[jid].to_dict() for jid in self._order if jid in self._jobs]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            counts: Dict[str, int] = {}
            for job in self._jobs.values():
                counts[job.status] = counts.get(job.status, 0) + 1
            saved = 0
            original = 0
            for job in self._jobs.values():
                if job.status == "done":
                    original += job.src_size
                    saved += max(0, job.src_size - job.total_size)
            return {
                "counts": counts,
                "total": len(self._jobs),
                "busy": dict(self._busy),
                "limits": dict(self._limits),
                "concurrency": self._concurrency,
                "bytes_in": original,
                "bytes_out": max(0, original - saved),
                "bytes_saved": saved,
            }

    def active_count(self) -> int:
        """Jobs that are queued or in flight -- i.e. work a restart would destroy.

        The desktop launcher consults this before replacing a stale backend: it is
        never acceptable to kill a running encode just because newer code exists.
        A PAUSED job counts: its encoder is still alive and still holds its slot.
        """
        with self._lock:
            return sum(1 for job in self._jobs.values()
                       if job.status in ("queued", "running", "verifying", "paused"))

    # -------------------------------------------------------------- submission

    def submit(self, paths: List[str], profile_id: str,
               overrides: Optional[Dict[str, Any]] = None, *, cleanup_batch="",
               recycle_source=False) -> List[Dict[str, Any]]:
        with self._source_submission(paths):
            return self._submit(paths, profile_id, overrides, cleanup_batch, recycle_source)

    def _submit(self, paths, profile_id, overrides, cleanup_batch, recycle_source):
        profile = get_profile(profile_id)
        ov = self._make_overrides(overrides or {})
        created: List[Job] = []

        for path in paths:
            identity = actions.file_identity(path)
            info = self.cached_probe(path) or probe(path)
            self.remember_probe(info)
            job_id = uuid.uuid4().hex[:12]
            job = Job(id=job_id, src=str(Path(path)), profile_id=profile["id"])
            job.cleanup_batch = cleanup_batch
            job.recycle_source = recycle_source
            job.source_identity = identity
            job.source_cleanup = 'pending' if recycle_source else ''
            job.source_cleanup_note = '本批结束且交付通过后移入回收站' if recycle_source else ''
            job.overrides = ov.to_dict()
            job.media = info.to_dict() if info.ok else {"ok": False, "error": info.error}
            job.duration = info.duration
            job.src_size = info.size_bytes
            add_event(job, "created", "已入队：档位 {}".format(profile["id"]))

            if not info.ok:
                job.status = "failed"
                job.error = info.error or "无法探测该文件"
                job.finished = time.time()
                add_event(job, "failed", "探测失败：{}".format(job.error))
                with self._lock:
                    self._jobs[job_id] = job
                    self._order.append(job_id)
                self._emit(job, "added")
                created.append(job)
                continue

            # Autocrop is a PER-FILE decision: bars differ from video to video, so the
            # detection runs here, once per source, and the result travels in that
            # job's own overrides. A single batch-wide crop string would be wrong for
            # every file but one.
            job_ov = ov
            if ov.autocrop:
                detected = crop.detect(path, info)
                job_ov = replace(ov, crop=detected.crop if detected.ok else "")
                if detected.ok:
                    job.warnings.append("自动裁边：" + detected.message)
                    job.overrides["crop"] = detected.crop
                else:
                    job.warnings.append("自动裁边：未裁剪（{}）".format(
                        detected.reason or detected.message))

            # Loudness is also PER-FILE: the whole point is that different sources
            # arrive at different levels. Measured here so the planner can emit the
            # linear two-pass filter instead of the dynamic single-pass one, and so the
            # user is told the actual numbers ("源 -21.8 LUFS → 目标 -16，+5.8 dB")
            # rather than just "已标准化".
            if audio.clamp_target(ov.loudness) < 0 and info.audio_streams:
                measured = audio.measure(path, ov.loudness, ov.loudness_tp)
                job_ov = replace(job_ov, loudness_measured=measured.to_dict())
                job.overrides["loudness"] = job_ov.loudness
                job.overrides["loudness_tp"] = job_ov.loudness_tp
                job.warnings.append(audio.describe(measured, ov.loudness, ov.loudness_tp))
                if not measured.ok:
                    job.warnings.append(
                        "响度无法测量，将退化为单遍动态模式（会改变动态范围）")
            elif audio.clamp_target(ov.loudness) < 0:
                job.warnings.append("响度标准化已忽略：该文件没有音轨")
                job_ov = replace(job_ov, loudness=0.0)

            try:
                plan = make_plan(info, profile, job_ov, exists_probe=os.path.isfile)
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = str(exc)
                job.finished = time.time()
                with self._lock:
                    self._jobs[job_id] = job
                    self._order.append(job_id)
                self._emit(job, "added")
                created.append(job)
                continue

            job.out_path = plan.out_path
            job.profile_id = plan.profile_id
            job.intent = plan.intent
            job.decision = plan.decision
            job.est_output_bytes = plan.est_output_bytes
            job.overrides = plan.resolved_overrides
            job.command = plan.command
            job.est_memory_mb = plan.est_memory_mb
            job.argv = plan.argv
            # Extend, never replace: anything noted before planning (autocrop's
            # decision, for one) would otherwise be silently discarded -- and a
            # discarded "we cropped 25% of your picture" note is exactly the kind of
            # thing the user needs to see.
            job.warnings = list(job.warnings) + list(plan.warnings)
            job.rules_applied = plan.rules_applied
            job.encoder = plan.encoder
            job.resource = resolve_resource(plan.encoder)
            add_event(job, "planned", "命令已生成：{} · {}".format(
                plan.encoder, "、".join(plan.rules_applied[:4])))
            self._retain_probe[job_id] = info

            with self._lock:
                self._allocate_output(job)
                self._jobs[job_id] = job
                self._order.append(job_id)
            self._emit(job, "added")
            created.append(job)

        self._persist()
        self._kick()
        return [j.to_dict() for j in created]

    #: Override keys whose value must be a number, and the range it must sit in. A
    #: string `crf` used to reach the front end as `value="50" onfocus=...`, because it
    #: was interpolated into an attribute: the TYPE is part of the message, not a detail
    #: to normalise away silently. Bounds come from the encoders' own accepted ranges.
    _NUMERIC_OVERRIDES = {
        "crf": (0, 63),
        "max_long_edge": (0, 16384),
        "scale_percent": (0, 1000),
        "gop_seconds": (0.1, 60.0),
        "loudness": (-70.0, 0.0),
        "loudness_tp": (-9.0, 0.0),
    }
    _BOOL_OVERRIDES = ("strip_metadata", "deinterlace", "denoise", "autocrop",
                       "hdr_to_sdr", "keep_timestamps", "allow_unsafe")
    _STR_OVERRIDES = ("location", "container", "name_template", "out_dir",
                      "default_out_dir", "audio_mode", "subtitle_mode", "effort")

    @classmethod
    def coerce_overrides(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Type-check override values at the boundary. Raises ValueError with a reason.

        Two failures came from not doing this: a JSON string where a number belongs
        reached the page and was interpolated into an HTML attribute (an injection
        vector), and `gop_seconds: "1"` blew up deep inside the planner with a
        TypeError, i.e. a 500 for a request that should have been a 400.
        """
        clean: Dict[str, Any] = {}
        for key, value in (raw or {}).items():
            if value is None:
                continue
            if key in cls._NUMERIC_OVERRIDES:
                low, high = cls._NUMERIC_OVERRIDES[key]
                if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                    raise ValueError("参数 {} 必须是数字".format(key))
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise ValueError("参数 {} 必须是数字".format(key))
                if key != "gop_seconds" and key != "loudness" and key != "loudness_tp":
                    number = int(round(number))
                if key in ("crf", "max_long_edge", "scale_percent") and number < 0:
                    raise ValueError("参数 {} 不能为负数".format(key))
                clean[key] = number
            elif key in cls._BOOL_OVERRIDES:
                if isinstance(value, str):
                    clean[key] = value.strip().lower() in ("1", "true", "yes", "on")
                else:
                    clean[key] = bool(value)
            elif key in cls._STR_OVERRIDES:
                if not isinstance(value, str):
                    raise ValueError("参数 {} 必须是字符串".format(key))
                clean[key] = value
            elif key == "extra_args":
                if not isinstance(value, (list, tuple)):
                    raise ValueError("参数 extra_args 必须是字符串数组")
                clean[key] = [str(a) for a in value]
            elif key == "intent":
                from .planner import validate_intent
                clean[key] = validate_intent(value)
            elif key == "loudness_measured":
                if not isinstance(value, dict):
                    raise ValueError("参数 loudness_measured 必须是对象")
                clean[key] = value
            elif key == "ten_bit":
                clean[key] = None if value is None else bool(value)
            else:
                clean[key] = value
        return clean

    @classmethod
    def _make_overrides(cls, raw: Dict[str, Any]) -> Overrides:
        allowed = {f for f in Overrides().__dict__.keys()}
        picked = {k: v for k, v in raw.items() if k in allowed}
        ov = Overrides(**cls.coerce_overrides(picked))
        if ov.intent:
            # Resolve before per-file auxiliary work: hidden loudness/crop controls
            # must not start a measurement merely because simple mode was requested.
            return Overrides(intent=ov.intent, out_dir=ov.out_dir,
                             default_out_dir=ov.default_out_dir, location=ov.location,
                             name_template=ov.name_template)
        return ov

    def submit_repair(self, paths: List[str], strategy: str = "",
                      options: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        with self._source_submission(paths):
            return self._submit_repair(paths, strategy, options)

    def _submit_repair(self, paths, strategy, options):
        """Queue 'seekability repair' jobs for already-compressed files.

        Repair is a different operation from compression: the source is inspected,
        a strategy is chosen from what the file actually needs, and the resulting
        plan is still held to the same keyframe gate by the verifier.
        """
        from .repair import (available_strategies, build_repair_plan, diagnose,
                             pick_default_strategy)

        options = dict(options or {})
        created: List[Job] = []

        for path in paths:
            job_id = uuid.uuid4().hex[:12]
            job = Job(id=job_id, src=str(Path(path)), profile_id="REPAIR")
            job.mode = "repair"
            job.overrides = options

            diag = diagnose(path)
            job.media = {"ok": diag.ok, "error": diag.error, "diagnosis": diag.to_dict()}
            job.duration = diag.duration
            job.src_size = diag.size_bytes

            if not diag.ok:
                job.status = "failed"
                job.error = diag.error or "无法探测该文件"
                job.finished = time.time()
                with self._lock:
                    self._jobs[job_id] = job
                    self._order.append(job_id)
                self._emit(job, "added")
                created.append(job)
                continue

            chosen = strategy or pick_default_strategy(diag, available_strategies(diag))
            try:
                plan = build_repair_plan(diag, chosen, options)
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = str(exc)
                job.finished = time.time()
                with self._lock:
                    self._jobs[job_id] = job
                    self._order.append(job_id)
                self._emit(job, "added")
                created.append(job)
                continue

            job.seek = {
                "verdict": diag.verdict,
                "keyframes": diag.keyframes,
                "max_keyframe_gap": diag.max_keyframe_gap,
                "summary": diag.summary(),
                "strategy": plan['strategy']['id'],
                "reasons": diag.reasons,
            }
            job.out_path = plan["out_path"]
            job.command = plan["command"]
            job.argv = plan["argv"]
            job.warnings = list(job.warnings) + list(plan["warnings"])
            job.rules_applied = plan["rules_applied"]
            job.encoder = self._argv_encoder(job.argv)
            # Repair strategies are named ('same', 'compat', ...), so read the actual
            # encoder out of the generated argv instead of the strategy id.
            job.resource = resolve_resource(self._argv_encoder(plan["argv"]) or chosen)
            job.est_memory_mb = self._job_memory_mb(job)
            self._retain_probe[job_id] = probe(path)

            with self._lock:
                self._allocate_output(job)
                self._jobs[job_id] = job
                self._order.append(job_id)
            self._emit(job, "added")
            created.append(job)

        self._persist()
        self._kick()
        return [j.to_dict() for j in created]

    # ---------------------------------------------------------------- controls

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in TERMINAL:
                return False
            proc = self._procs.get(job_id)
            if proc is None:
                job.status = "cancelled"
                job.finished = time.time()
                add_event(job, "cancelled", "已取消（未在编码中）")
                self._persist()
                self._emit(job)
                self._run_source_cleanups()
                return True
            paused = job.status == "paused"
            if paused:
                result = winapi.terminate_process(proc.pid, resume_first=True, **self._handle_args(proc))
                if not result.get('ok'):
                    add_event(job, 'warning', '取消失败：{}'.format(result.get('error') or '进程无法终止'))
                    return False
            else:
                try:
                    proc.terminate()
                except OSError as exc:
                    add_event(job, 'warning', '取消失败：{}'.format(exc))
                    return False
            if paused:
                job.paused_total += job.paused_seconds()
            job.paused_at = 0.0
            job.pause_reason = ''
            job.status = 'cancelled'
            job.finished = time.time()
            self._memory_critical_since.pop(job_id, None)
            add_event(job, 'cancelled', '已取消，编码进程已终止')
            self._persist()
            self._emit(job)
        return True

    # ------------------------------------------------------------------ pause (G6)

    def pause(self, job_id: str, *, reason: str = "user") -> Dict[str, Any]:
        """Freeze a running encode.

        Only the ENCODING stage can be paused: verification runs its own short-lived
        ffmpeg that this class does not own, and claiming a pause there would be a
        fiction. A paused job keeps its resource slot, because its encoder is still
        alive and still holding the session.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"ok": False, "error": "任务不存在"}
            if job.status == "paused":
                if reason == "user" and job.pause_reason == "memory":
                    job.pause_reason = "user"
                    self._memory_critical_since.pop(job_id, None)
                    add_event(job, "paused", "用户接管暂停，不再自动继续")
                    self._persist()
                    self._emit(job)
                return {"ok": True, "job": job.to_dict()}
            if job.status != "running":
                return {"ok": False, "error": "只有正在编码的任务可以暂停（当前：{}）".format(
                    status_label(job.status))}
            proc = self._procs.get(job_id)
            if proc is None or proc.poll() is not None:
                return {"ok": False, "error": "该任务的编码进程已不在运行"}
            pid = proc.pid
            if not self._pid_is_tracked_ffmpeg(job_id, pid):
                return {"ok": False, "error": "编码进程已退出（pid 可能已被复用），未执行暂停"}
            result = winapi.suspend_process(pid, **self._handle_args(proc))
            if not result.get("ok"):
                return {"ok": False, "error": result.get("error") or "暂停失败"}
            job.status = "paused"
            job.paused_at = time.time()
            job.pause_reason = reason
            job.warnings.append("已暂停：编码进程被挂起，输出文件处于未完成状态")
            add_event(job, "paused", "已挂起编码进程 pid {}".format(pid))
        self._persist()
        self._emit(job)
        return {"ok": True, "job": job.to_dict()}

    def resume(self, job_id: str, *, automatic: bool = False) -> Dict[str, Any]:
        """Thaw a paused encode and credit the paused time against the stall guard."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"ok": False, "error": "任务不存在"}
            if job.status != "paused":
                return {"ok": False, "error": "该任务没有在暂停中（当前：{}）".format(
                    status_label(job.status))}
            if automatic and job.pause_reason != "memory":
                return {"ok": False, "error": "用户暂停的任务不会自动继续"}
            if job.pause_reason == "memory" and self._memory_guard:
                from . import memory
                mem = memory.machine_memory()
                resume_at = self.memory_policy()["low_water_mb"] + memory.RESUME_HYSTERESIS_MB
                if not mem.get("available") or mem["free_mb"] < resume_at:
                    return {"ok": False, "error": "内存尚未恢复，需可用内存达到 {} MB 后继续".format(resume_at)}
            proc = self._procs.get(job_id)
            if proc is None:
                return {"ok": False, "error": "该任务的编码进程已不在运行"}
            pid = proc.pid
            if not self._pid_is_tracked_ffmpeg(job_id, pid):
                return {"ok": False, "error": "编码进程已退出（pid 可能已被复用），未执行继续"}
            result = winapi.resume_process(pid, **self._handle_args(proc))
            if not result.get("ok"):
                return {"ok": False, "error": result.get("error") or "继续失败"}
            paused_for = max(0.0, time.time() - (job.paused_at or time.time()))
            job.paused_total += paused_for
            job.paused_at = 0.0
            job.status = "running"
            job.pause_reason = ""
            self._memory_critical_since.pop(job_id, None)
            # The stall guard compares against the last progress line, which is now
            # `paused_for` seconds in the past. Without this credit a long pause would
            # look exactly like a hung encoder and kill the job on resume.
            if paused_for >= 1.0:
                job.warnings.append("已继续：本次暂停 {:.0f} 秒（不计入编码用时）".format(
                    paused_for))
            add_event(job, "resumed", "已继续（暂停 {:.0f} 秒）".format(paused_for))
        self._persist()
        self._emit(job)
        return {"ok": True, "job": job.to_dict()}

    def remember_probe(self, info: MediaInfo) -> None:
        """Cache a completed probe by path, keyed on (path, mtime, size).

        The batch endpoints (totals / grouping / budget) need metadata for every selected
        file, and the file list already probes each one when it is added. Without this
        cache the batch total would re-shell out to ffprobe for the whole selection on
        every refresh -- slow enough to make the feature unusable, which is how a "nice
        extra" turns into a spinner.
        """
        if not info or not getattr(info, "ok", False):
            return
        key = self._probe_key(info.path)
        if key:
            with self._lock:
                self._probe_cache[key] = info

    @staticmethod
    def _probe_key(path: str) -> str:
        try:
            stat = os.stat(path)
        except OSError:
            return ""
        return "{}|{}|{}".format(path, stat.st_mtime_ns, stat.st_size)

    def cached_probe(self, path: str) -> Optional[MediaInfo]:
        """The remembered probe for `path`, if the file has not changed since."""
        key = self._probe_key(path)
        if not key:
            return None
        with self._lock:
            return self._probe_cache.get(key)

    def batch_probes(self, paths: Sequence[str], cap: int = 200) -> List[MediaInfo]:
        """Probes for a batch request: cached when possible, fresh when necessary."""
        out: List[MediaInfo] = []
        for raw in list(paths)[:cap]:
            info = self.cached_probe(str(raw))
            if info is None:
                info = probe(str(raw))
                self.remember_probe(info)
            out.append(info)
        return out

    def _ids_with_status(self, *statuses: str) -> List[str]:
        """Queue-ordered ids currently in any of `statuses`."""
        wanted = set(statuses)
        with self._lock:
            return [jid for jid in list(self._order)
                    if (self._jobs.get(jid) is not None
                        and self._jobs[jid].status in wanted)]

    def pause_all(self) -> Dict[str, Any]:
        """Pause every running encode. The counterpart of `resume_all`.

        Reported because the batch form has a failure mode the single form does not: one
        encoder may exit between the listing and the suspend, and the user needs to know
        that N of M were actually suspended rather than being told "done".
        """
        targets = self._ids_with_status("running", "paused")
        paused, failed = 0, []
        for job_id in targets:
            result = self.pause(job_id)
            if result.get("ok"):
                paused += 1
            else:
                failed.append({"id": job_id, "error": result.get("error") or "暂停失败"})
        return {"ok": True, "paused": paused, "requested": len(targets),
                "failures": failed[:10]}

    def resume_all(self) -> Dict[str, Any]:
        """Resume every paused encode -- the missing half of "I paused everything"."""
        targets = self._ids_with_status("paused")
        resumed, failed = 0, []
        for job_id in targets:
            result = self.resume(job_id)
            if result.get("ok"):
                resumed += 1
            else:
                failed.append({"id": job_id, "error": result.get("error") or "继续失败"})
        return {"ok": True, "resumed": resumed, "requested": len(targets),
                "failures": failed[:10]}

    def cancel_all(self) -> Dict[str, int]:
        """Stop the whole batch -- the "all" half of stop-current vs stop-all.

        Cancelling one job leaves the rest running, which is the right default for a
        mistake in a single file; this is the deliberate "I want it to stop" action,
        so it must also stop what is only QUEUED, not just what is in flight. Queued
        jobs never started, so cancelling them is free and immediately visible in the
        queue rather than a surprise later.
        """
        running = 0
        queued = 0
        with self._lock:
            targets = [(jid, self._jobs.get(jid)) for jid in list(self._order)]
        for job_id, job in targets:
            if job is None or job.status in TERMINAL:
                continue
            if job.status in ("running", "verifying", "paused"):
                running += 1
                self.cancel(job_id)
            elif job.status == "queued":
                queued += 1
                with self._lock:
                    job.status = "cancelled"
                    job.finished = time.time()
                self._emit(job)
        if queued:
            self._persist()
        self._run_source_cleanups()
        return {"running": running, "queued": queued, "total": running + queued}

    def move(self, job_id: str, before_id: str = "") -> bool:
        """Move a QUEUED job in the execution order (drag-and-drop in the queue).

        Only queued jobs move: a running job's position is a fact, not a preference,
        and shuffling it would misreport what is actually happening. `before_id` of ""
        means "to the end".
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != "queued":
                return False
            if job_id not in self._order:
                return False
            if before_id and before_id == job_id:
                return False
            if before_id:
                anchor = self._jobs.get(before_id)
                # The anchor must itself be queued AND still in the order list, or the
                # index lookup below would raise ValueError.
                if not anchor or anchor.status != "queued" or before_id not in self._order:
                    return False
            self._order.remove(job_id)
            if before_id:
                # Resolve the anchor index AFTER the removal. An index taken before it is
                # stale whenever the moved job sat before the anchor: removing it shifts
                # every later entry down by one, so inserting at the old index drops the
                # job AFTER the anchor -- the exact opposite of "before". Dragging a row
                # UP the queue (the common case) hit this every time.
                self._order.insert(self._order.index(before_id), job_id)
            else:
                self._order.append(job_id)
        self._persist()
        return True

    @staticmethod
    def unknown_override_keys(raw: Dict[str, Any]) -> List[str]:
        """Override keys the planner does not understand.

        `_make_overrides` drops unknown keys silently, which turned a typo into a lie:
        `{"max_edge": 640}` (the real name is `max_long_edge`) was accepted, reported
        success, and produced a command with no scaling at all. The API boundary uses
        this to reject the request instead; internal callers stay lenient because they
        build the dict themselves.
        """
        allowed = {f for f in Overrides().__dict__.keys()} - INTERNAL_OVERRIDE_KEYS
        return sorted(k for k, v in (raw or {}).items()
                      if k not in allowed and v is not None)

    def update_job(self, job_id: str, patch: Dict[str, Any],
                   profile_id: str = "") -> Dict[str, Any]:
        """Change the encoding settings of a job that has not started yet.

        Changing a running job's settings would either be ignored or, worse, leave the
        report describing something other than what was encoded -- so it is refused,
        with a reason the UI can show.
        """
        unknown = self.unknown_override_keys(patch)
        if unknown:
            return {"ok": False, "error": "不认识的参数：{}".format("、".join(unknown))}
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"ok": False, "error": "任务不存在"}
            if job.status != "queued":
                return {"ok": False, "error": "只有排队中的任务可以改参数（当前：{}）".format(
                    job.status)}

        # Resolve the profile inside a guard: get_profile raises KeyError for an
        # unknown id, which would surface as a 500 instead of a message the user can
        # act on. (Caught by the self-test, not by reading the code.)
        try:
            profile = get_profile(profile_id or job.profile_id)
        except KeyError as exc:
            return {"ok": False, "error": str(exc).strip("'\"") or "未知档位"}
        merged = dict(job.overrides or {})
        if profile_id and profile["id"] != job.profile_id:
            # Resolved automatic jobs carry hardware presets (e.g. p5). A newly
            # selected software template must not inherit them or its old quality cap.
            for key in ("crf", "preset", "ten_bit", "effort", "max_long_edge", "scale_percent"):
                merged.pop(key, None)
        allowed = {f for f in Overrides().__dict__.keys()}
        for key, value in (patch or {}).items():
            if key in allowed:
                merged[key] = value
        try:
            ov = self._make_overrides(merged)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        info = probe(job.src)
        if not info.ok:
            return {"ok": False, "error": info.error or "无法探测源文件"}
        try:
            plan = make_plan(info, profile, ov, exists_probe=os.path.isfile)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

        with self._lock:
            # Re-check INSIDE the write: the check above released the lock while `probe`
            # and `make_plan` ran (both shell out, so that is hundreds of milliseconds),
            # and in that window the job may have been started by the scheduler. Writing
            # the new settings then would leave the report describing something other
            # than what the encoder is actually running -- the exact outcome the "only
            # queued jobs may be edited" rule exists to prevent.
            if job.status != "queued":
                return {"ok": False,
                        "error": "任务已经开始（{}），参数未改动".format(job.status)}
            job.profile_id = plan.profile_id
            job.overrides = plan.resolved_overrides
            job.intent = plan.intent
            job.decision = plan.decision
            job.est_output_bytes = plan.est_output_bytes
            job.media = info.to_dict()
            job.out_path = plan.out_path
            job.command = plan.command
            job.est_memory_mb = plan.est_memory_mb
            job.argv = plan.argv
            job.warnings = list(plan.warnings)
            job.rules_applied = plan.rules_applied
            job.encoder = plan.encoder
            job.resource = resolve_resource(plan.encoder)
            job.duration = info.duration
            job.src_size = info.size_bytes
            self._retain_probe[job_id] = info
            self._allocate_output(job)
        self._persist()
        self._emit(job)
        return {"ok": True, "job": job.to_dict()}

    def retry(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if (not job or job.status not in TERMINAL or job.id in self._working_ids
                    or job.source_cleanup in ('processing', 'review')):
                return False
            self._allocate_output(job)
            job.recycle_source = False
            job.cleanup_batch = ''
            job.source_identity = {}
            job.output_identity = {}
            job.source_cleanup = ''
            job.source_cleanup_note = ''
            job.source_recovery_path = ''
            job.status = "queued"
            job.error = ""
            job.verify = None
            job.progress = 0.0
            job.out_time = 0.0
            job.fps = 0.0
            job.speed = 0.0
            job.total_size = 0
            job.started = 0.0
            job.finished = 0.0
            job.elapsed = 0.0
            # Runtime state that a retry MUST clear, or the second run inherits the
            # first one's history: `paused_total` (time credited back to a run that no
            # longer exists) and `pid` (a dead pid the UI and the watchdog would treat
            # as this job's process). `events` and the trimmed-log marker are cleared
            # too, so the log viewer shows THIS run rather than two runs interleaved.
            job.paused_total = 0.0
            job.paused_at = 0.0
            job.pause_reason = ""
            job.pid = 0
            job.pid_created = 0
            job.events = []
            job.log_trimmed = {}
            job.verify_percent = None
            job.verify_frames = 0
            job.verify_stage = ''
            job.waiting_reason = ''
            job.degraded = False
        self._persist()
        self._emit(job)
        self._kick()
        return True

    def remove(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if (not job or job.status not in TERMINAL or job.id in self._working_ids
                    or job.source_cleanup in ('pending', 'processing', 'review')):
                return False
            self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
            self._retain_probe.pop(job_id, None)
        self._persist()
        return True

    def clear_successful(self) -> List[str]:
        removed = []
        with self._lock:
            for jid in list(self._order):
                job = self._jobs.get(jid)
                if (job and job.status == 'done' and (job.verify or {}).get('verdict') == 'passed'
                        and job.id not in self._working_ids
                        and job.source_cleanup not in ('pending', 'processing', 'review')):
                    self._jobs.pop(jid, None)
                    self._order.remove(jid)
                    self._retain_probe.pop(jid, None)
                    removed.append(jid)
        if removed:
            self._persist()
        return removed

    @staticmethod
    def _argv_encoder(argv: List[str]) -> str:
        """The encoder actually used in an ffmpeg argv (`-c:v <x>`)."""
        from .memory import last_option
        return last_option(argv, ('-c:v', '-codec:v', '-vcodec', '-c', '-codec'))

    def set_concurrency(self, value: int) -> int:
        """Legacy single-number setter: splits the value across both pools.

        Kept so existing callers keep working; `set_limits` is the real API now.
        """
        value = max(1, min(16, int(value)))
        gpu = max(1, round(value / 2)) if value > 1 else 1
        return sum(self.set_limits({"gpu": gpu, "cpu": max(1, value - gpu)}).values())

    def set_limits(self, limits: Dict[str, int]) -> Dict[str, int]:
        """Set the concurrency of each resource pool independently."""
        from .sizing import CPU_CONCURRENCY_CAP, GPU_CONCURRENCY_CAP
        caps = {"gpu": GPU_CONCURRENCY_CAP, "cpu": CPU_CONCURRENCY_CAP}
        with self._lock:
            for key, value in (limits or {}).items():
                if key not in self._limits:
                    continue
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                self._limits[key] = max(1, min(caps.get(key, 4), number))
            result = dict(self._limits)
        self._kick()
        return result

    @staticmethod
    def _deep_verify() -> bool:
        """Read the persisted switch at verification time.

        Deliberately re-read per job (not cached at construction) so toggling the
        option in the UI affects the very next job without a restart.  A settings
        failure must never break verification, hence the broad guard.
        """
        try:
            from . import settings as _settings
            return bool(_settings.load().get("deep_verify"))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _vmaf_enabled() -> bool:
        """Same contract as `_deep_verify`, for the quality-score switch."""
        try:
            from . import settings as _settings
            return bool(_settings.load().get("vmaf_check"))
        except Exception:  # noqa: BLE001
            return False

    def limits(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._limits)

    def save_settings(self, patch: Dict[str, Any], output_directory=None) -> Dict[str, Any]:
        """Validate, persist, then apply together before releasing the scheduler lock."""
        from . import settings
        from .memory import validate_policy
        from .sizing import CPU_CONCURRENCY_CAP, GPU_CONCURRENCY_CAP
        changed = dict(patch)
        with self._lock:
            if 'limits' in changed:
                limits = changed['limits']
                if not isinstance(limits, dict) or not limits or set(limits) - {'gpu', 'cpu'}:
                    raise ValueError('并行设置必须包含 GPU 或 CPU 数量')
                for key, value in limits.items():
                    cap = GPU_CONCURRENCY_CAP if key == 'gpu' else CPU_CONCURRENCY_CAP
                    if type(value) is not int or not 1 <= value <= cap:
                        raise ValueError('{} 并行数量必须为 1–{} 的整数'.format(key.upper(), cap))
                changed['limits'] = dict(self._limits, **limits)
            keys = {'memory_guard':'guard', 'memory_margin_mb':'margin_mb', 'memory_low_water_mb':'low_water_mb'}
            policy = None
            if any(key in changed for key in keys):
                proposed = self.memory_policy()
                proposed.update({target:changed[key] for key,target in keys.items() if key in changed})
                policy = validate_policy(**proposed)
                changed.update({key:policy[target] for key,target in keys.items()})
            if output_directory is not None:
                Path(output_directory).mkdir(parents=True, exist_ok=True)
            saved = settings.save(changed)
            if 'limits' in changed:
                self._limits = dict(changed['limits'])
            if policy is not None:
                self._memory_guard = policy['guard']
                self._memory_margin_mb = policy['margin_mb']
                self._memory_low_water_mb = policy['low_water_mb']
            saved['limits'] = dict(self._limits)
            saved['memory'] = self.memory_policy()
        if 'limits' in changed or policy is not None:
            self._kick()
        return saved

    # ------------------------------------------------------------- scheduling

    def _kick(self) -> None:
        with self._cv:
            self._cv.notify_all()

    def set_memory_policy(self, guard: Optional[bool] = None,
                          margin_mb: Optional[int] = None,
                          low_water_mb: Optional[int] = None) -> Dict[str, Any]:
        """Apply the memory-protection settings coming from the settings panel."""
        from .memory import validate_policy
        with self._lock:
            current = self.memory_policy()
            applied = validate_policy(guard if guard is not None else current["guard"],
                                      margin_mb if margin_mb is not None else current["margin_mb"],
                                      low_water_mb if low_water_mb is not None else current["low_water_mb"])
            self._memory_guard = applied["guard"]
            self._memory_margin_mb = applied["margin_mb"]
            self._memory_low_water_mb = applied["low_water_mb"]
        self._kick()
        return self.memory_policy()

    def memory_policy(self) -> Dict[str, Any]:
        from .memory import LOW_WATER_MB, SAFETY_MARGIN_MB
        return {
            "guard": bool(self._memory_guard),
            "margin_mb": int(self._memory_margin_mb
                             if self._memory_margin_mb is not None else SAFETY_MARGIN_MB),
            "low_water_mb": int(self._memory_low_water_mb
                                if self._memory_low_water_mb is not None else LOW_WATER_MB),
        }

    def _has_slot(self, resource: Optional[str]) -> bool:
        pool = resource or "cpu"
        return self._busy.get(pool, 0) < self._limits.get(pool, 1)

    def _job_memory_mb(self, job: Job) -> int:
        """This job's estimated peak commit, from the plan it was built with.

        Use the current plan, including queued edits. Repair/older records can derive
        the estimate from their saved geometry and actual encoder argv.
        """
        need = int(getattr(job, "est_memory_mb", 0) or 0)
        media = job.media or {}
        geometry = media.get("video") or media.get("diagnosis") or {}
        width, height = geometry.get("width", 0), geometry.get("height", 0)
        encoder = self._argv_encoder(job.argv) or job.encoder
        if not width or not height or not encoder:
            return max(0, need)
        from .memory import estimate_plan_mb
        need, _ = estimate_plan_mb(SimpleNamespace(argv=job.argv, encoder=encoder,
                                                  out_size=(width, height), source_size=(width, height),
                                                  source_pix_fmt=geometry.get("pix_fmt", "")))
        job.est_memory_mb = need
        return need

    def _pending_memory_mb(self) -> tuple[int, int]:
        """Unrealised peak resident/private memory of dispatched software encodes."""
        from .memory import process_memory_mb
        from .caps import startup_probe_reserve_mb
        # Early page readiness can overlap a queue start with secondary device
        # validation. Keep that probe's future peak inside physical/commit admission.
        resident = committed = self._aux_reserved_mb + startup_probe_reserve_mb()
        for jid, job in list(self._jobs.items()):
            if job.status in ("running", "paused"):
                peak = self._job_memory_mb(job)
                proc = self._procs.get(jid)
                usage = process_memory_mb(proc.pid) if proc is not None else None
                resident += max(0, peak - (usage or {}).get("resident_mb", 0))
                committed += max(0, peak - (usage or {}).get("private_mb", 0))
        return resident, committed

    def memory_gate(self, job: Job) -> Dict[str, Any]:
        """May this job start now? (Admission control -- the confirmed policy is WAIT.)

        Returns the decision so the caller can park the job with a visible reason instead of
        silently starting something that would push the machine into paging.
        """
        from .memory import (SAFETY_MARGIN_MB, admission, machine_memory)
        need = self._job_memory_mb(job)
        margin = self._memory_margin_mb if self._memory_margin_mb is not None else SAFETY_MARGIN_MB
        if not self._memory_guard:
            return {"ok": True, "reason": "", "waiting": False}
        if need <= 0:
            return {"ok": False, "reason": "等待内存估算：缺少媒体尺寸或编码参数，请重新添加任务",
                    "waiting": True}
        pending, commit_pending = self._pending_memory_mb()
        # Sample free memory AFTER process usage: a process growing between samples
        # then makes the estimate conservative instead of charging an old free figure.
        mem = machine_memory()
        decision = admission(mem, pending, need, margin, commit_pending)
        decision["waiting"] = not decision["ok"]
        decision["need_mb"] = need
        return decision

    def memory_snapshot(self) -> Dict[str, Any]:
        """Live telemetry only; no source/child process and no scheduler mutation."""
        from . import memory
        with self._lock:
            pending, committed = self._pending_memory_mb()
            policy = self.memory_policy()
            current = memory.machine_memory()
        gate = memory.admission(current, pending, 64, policy['margin_mb'], committed)
        if not policy['guard']:
            gate = dict(gate, ok=True, reason='')
        return {'memory': current, 'policy': policy, 'reserved_mb': pending,
                'commit_reserved_mb': committed, 'probe_gate': gate,
                'sampled_at': time.time()}

    @contextmanager
    def auxiliary_slot(self, need_mb: int, pool: Optional[str]):
        """Reserve an auxiliary child before launch; release after timeout/exit."""
        from . import memory, work
        with self._cv:
            if pool and not self._has_slot(pool):
                raise work.Busy("编码资源正被队列使用，请稍后重试辅助测算")
            if self._memory_guard:
                pending, committed = self._pending_memory_mb()
                gate = memory.admission(memory.machine_memory(), pending, need_mb,
                                        self.memory_policy()["margin_mb"], committed)
                if not gate["ok"]:
                    raise work.Busy(gate["reason"])
            self._aux_reserved_mb += need_mb
            if pool:
                self._busy[pool] += 1
        try:
            yield
        finally:
            with self._cv:
                self._aux_reserved_mb -= need_mb
                if pool:
                    self._busy[pool] -= 1
                self._cv.notify_all()

    def _next_queued(self) -> Optional[Job]:
        """Oldest queued job that fits in a free slot AND in the available memory."""
        self.restore_state()
        for jid in self._order:
            job = self._jobs.get(jid)
            if job is not None and job.status == 'queued':
                encoder = self._argv_encoder(job.argv)
                if encoder:
                    job.resource = resolve_resource(encoder)
            if (not job or job.status != "queued" or jid in self._restored_ids
                    or not self._has_slot(job.resource)):
                continue
            gate = self.memory_gate(job)
            if gate["ok"]:
                if job.waiting_reason:
                    with self._lock:
                        job.waiting_reason = ""
                    self._emit(job)
                return job
            # Keep the reason on the job so the card can say WHY it has not started, and
            # so the answer is the same for every dispatcher pass (no flicker).
            if job.waiting_reason != gate["reason"]:
                with self._lock:
                    job.waiting_reason = gate["reason"]
                self._emit(job)
        return None

    def _memory_watchdog_tick(self, job: Job, proc: subprocess.Popen,
                              now: Optional[float] = None) -> bool:
        """One bounded memory check. True means the encoder was terminated."""
        from . import memory
        with self._lock:
            if (job.resource or "cpu") != "cpu" or job.status not in ("running", "paused"):
                return False
            if self._procs.get(job.id) is not proc or proc.poll() is not None:
                return False
            if not self._memory_guard:
                self._memory_critical_since.pop(job.id, None)
                if job.pause_reason == "memory":
                    self.resume(job.id, automatic=True)
                return False
            if job.status == "paused" and job.pause_reason != "memory":
                self._memory_critical_since.pop(job.id, None)
                return False
            mem = memory.machine_memory()
            if not mem.get("available"):
                self._memory_critical_since.pop(job.id, None)
                return False
            free = mem["free_mb"]
            low = self.memory_policy()["low_water_mb"]
            if job.status == "running" and free < low:
                result = self.pause(job.id, reason="memory")
                if result.get("ok"):
                    job.warnings.append("内存不足（可用 {:.1f} GB），已自动挂起；"
                                        "可用内存达到 {:.1f} GB 后自动继续".format(
                                            free / 1024.0, (low + memory.RESUME_HYSTERESIS_MB) / 1024.0))
                    add_event(job, "paused", job.warnings[-1])
                    self._persist()
                    self._emit(job)
            if job.status != "paused" or job.pause_reason != "memory":
                return False
            if free >= memory.CRITICAL_MB:
                self._memory_critical_since.pop(job.id, None)
            if free >= low + memory.RESUME_HYSTERESIS_MB:
                result = self.resume(job.id, automatic=True)
                if result.get("ok"):
                    add_event(job, "resumed", "内存已恢复，自动继续")
                    self._persist()
                return False
            if free >= memory.CRITICAL_MB:
                return False
            stamp = time.time() if now is None else now
            since = self._memory_critical_since.setdefault(job.id, stamp)
            if stamp - since < memory.GRACE_SECONDS:
                return False
            if not self._pid_is_tracked_ffmpeg(job.id, proc.pid):
                return False
            # A suspended process must first be resumed before termination.
            result = winapi.terminate_process(proc.pid, resume_first=True, **self._handle_args(proc))
            if not result.get("ok"):
                add_event(job, "warning", "低内存终止失败：{}".format(result.get("error", "未知原因")))
                return False
            job.error = "内存连续低于 {} MB 超过 {} 秒，已终止以免系统卡死".format(
                memory.CRITICAL_MB, memory.GRACE_SECONDS)
            job.paused_total += max(0.0, stamp - (job.paused_at or stamp))
            job.paused_at = 0.0
            job.pause_reason = ""
            job.status = "failed"
            self._memory_critical_since.pop(job.id, None)
            add_event(job, "failed", job.error)
            self._persist()
            self._emit(job)
            return True

    @staticmethod
    def _stall_seconds(job: Job, last_progress: float, paused_at_progress: float,
                       now: Optional[float] = None) -> float:
        if job.status == "paused":
            return 0.0
        return max(0.0, (time.time() if now is None else now) - last_progress
                   - max(0.0, job.paused_total - paused_at_progress))

    def run_forever(self) -> None:
        """Dispatcher loop; run it in a daemon thread.

        Each job gets its own worker thread. Running the encode inline here would
        serialise everything no matter how many slots were configured -- which is
        exactly what the first version of this did (the pool counters looked right
        while only one job ever ran).

        One slot is taken per running job, from that job's resource pool, so a GPU
        job and a CPU job overlap instead of queueing behind each other.
        """
        while True:
            with self._cv:
                while True:
                    job = self._next_queued()
                    if job is not None:
                        break
                    self._cv.wait(timeout=1.0)
                pool = job.resource or "cpu"
                self._busy[pool] = self._busy.get(pool, 0) + 1
                job.status = "running"
                self._working_ids.add(job.id)
                job.started = time.time()
            self._emit(job, "started")
            try:
                threading.Thread(target=self._job_worker, args=(job, pool),
                                 daemon=True, name="vcs-job-" + job.id).start()
            except RuntimeError as exc:
                with self._cv:
                    self._working_ids.discard(job.id)
                    self._busy[pool] = max(0, self._busy[pool] - 1)
                    job.status = "failed"
                    job.error = "无法启动编码线程：{}".format(exc)
                    job.finished = time.time()
                    self._cv.notify_all()
                self._persist()
                self._emit(job, "finished")
                self._run_source_cleanups()

    def _job_worker(self, job: Job, pool: str) -> None:
        try:
            self._run_job(job)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                if job.status != 'cancelled':
                    job.status = "failed"
                    job.error = "内部错误: {}".format(exc)
                job.finished = time.time()
            self._emit(job, "finished")
        finally:
            # Belt and braces: if _run_job raised between Popen and wait(), the encoder is
            # still running while the job already reads "failed" and its slot has been
            # released -- the UI would say failed while ffmpeg kept burning the GPU and
            # writing the output file. Anything still registered here is killed.
            leftover = self._procs.pop(job.id, None)
            if leftover is not None:
                try:
                    if leftover.poll() is None:
                        leftover.kill()
                        add_event(job, "failed", "任务异常结束，已终止遗留的编码进程")
                    leftover.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                job.pid = 0
                job.pid_created = 0
            # A released slot means this run is durably settled, including failures.
            try:
                self._persist()
            except Exception as exc:  # bookkeeping failure remains visible
                job.warnings.append('任务状态保存失败：{}'.format(exc))
            with self._cv:
                self._working_ids.discard(job.id)
                self._busy[pool] = max(0, self._busy.get(pool, 1) - 1)
                self._cv.notify_all()
        self._run_source_cleanups()
        # Deliberately OUTSIDE the lock: this may play a sound or shell out to
        # `shutdown`, and holding the queue lock across a subprocess would freeze every
        # other job's progress reporting for as long as it takes.
        try:
            self._run_completion_actions()
        except Exception:  # noqa: BLE001 - a completion action must never crash a batch
            pass
        # Persist the finished job. This used to be indented INSIDE the `except` above
        # (after `pass`), so the queue on disk kept saying "queued" for every job that
        # completed -- and a restart re-encoded finished work. Measured, not theorised:
        # store_f8/queue.json still held status=queued for a job whose report.json said
        # done. The `finally` covers normal exits, exceptions and cancellations alike.
        try:
            self._persist()
        except Exception:  # noqa: BLE001 - never fail a finished job over bookkeeping
            pass

    # -------------------------------------------------------------- execution

    def _output_taken(self, path: str, exclude: str = '') -> bool:
        candidate = Path(path)
        if os.path.lexists(path):
            return True
        return any(
            (other.src and paths.is_same_file(candidate, Path(other.src))) or
            (other.id != exclude and other.status not in TERMINAL and other.out_path and
             paths.is_same_file(candidate, Path(other.out_path)))
            for other in self._jobs.values())

    @staticmethod
    def _set_output(job: Job, output: str) -> None:
        if not job.argv or job.argv[-1] != job.out_path:
            raise ValueError('输出路径与保存的命令不一致，请重新添加任务')
        job.out_path = output
        job.argv[-1] = output
        job.command = ' '.join(shlex.quote(a) for a in job.argv)

    def _allocate_output(self, job: Job) -> None:
        """Called under the job lock; live queue paths reserve names before launch."""
        if not job.out_path:
            return
        current = Path(job.out_path)
        output = paths.unique_path(current.parent, current.name,
                                   exists=lambda p: self._output_taken(p, job.id))
        if str(output) != job.out_path:
            self._set_output(job, str(output))

    @staticmethod
    def _validate_execution(job: Job) -> None:
        """Reject malformed restored commands before any media path is opened."""
        from .planner import _EXTRA_VALUE_OPTIONS, option_base
        if not job.src or not job.out_path or not job.argv:
            raise ValueError('任务缺少源文件、输出或编码命令，请重新添加')
        if paths.is_same_file(Path(job.src), Path(job.out_path)):
            raise ValueError('输出与源文件是同一文件，已拒绝编码')
        paths.validate_container(Path(job.out_path).suffix.lstrip('.'))
        flags = _EXTRA_VALUE_OPTIONS | {'-i', '-map', '-map_metadata', '-map_chapters',
                                        '-af', '-progress', '-forced-idr', '-disposition', '-metadata',
                                        '-channel_layout', '-mapping_family'}
        switches = {'-hide_banner', '-nostdin', '-nostats', '-y', '-n', '-re'}
        inputs, outputs = [], []
        index = 1
        while index < len(job.argv):
            token = job.argv[index]
            if token in switches:
                index += 1
                continue
            if token.startswith('-'):
                if option_base(token) not in flags or index + 1 >= len(job.argv):
                    raise ValueError('任务命令含不支持的选项：{}'.format(token))
                value = job.argv[index + 1]
                if token == '-i':
                    inputs.append(value)
                if token == '-progress' and value != 'pipe:1':
                    raise ValueError('任务命令进度通道不安全')
                index += 2
            else:
                outputs.append(token)
                index += 1
        if (len(inputs) != 1 or not paths.is_same_file(Path(inputs[0]), Path(job.src)) or
                outputs != [job.out_path] or job.argv[-1] != job.out_path):
            raise ValueError('任务命令必须只含登记的源文件和唯一输出，请重新添加任务')

    def _finalize_name(self, job: Job) -> None:
        """Rename the output once the post-encode values are known.

        The plan's filename had `{filesize_mb}`-style tokens blanked because ffmpeg
        needed a real path before any of those values existed. Now they do, so the
        file is renamed to what the user actually asked for.

        Cosmetics must never cost a good encode: the whole body is guarded, and a
        failure is recorded as a warning. (It was not, at first -- a missing import
        turned a perfectly good 9.5 MB output into a "failed" job, which is exactly
        the behaviour this docstring promises against.)
        """
        try:
            self._finalize_name_inner(job)
        except Exception as exc:  # noqa: BLE001 - naming is cosmetic, never fatal
            job.warnings.append("改名失败（成品保留原名 {}）：{}".format(
                Path(job.out_path).name, exc))

    def _copy_timestamps(self, job: Job) -> None:
        """Give the output the source's file timestamps (F7).

        Measured gap this closes: the container's `creation_time` survives
        (`-map_metadata 0`), but the FILE's modification time was the encode time -- so a
        library sorted by date showed every freshly compressed file as "today" instead of
        the date it was shot, which is the actual complaint behind "压完拍摄日期没了".

        Only atime/mtime are copied. Windows' *creation* time is left alone: it records
        when this file appeared on this disk, and rewriting it would be a lie about
        something other than the video.
        """
        if not (job.overrides or {}).get("keep_timestamps", True):
            return
        try:
            src = Path(job.src)
            out = Path(job.out_path)
            if not out.is_file() or paths.is_same_file(src, out):
                return
            stat = src.stat()
            os.utime(str(out), (stat.st_atime, stat.st_mtime))
        except OSError as exc:
            job.warnings.append("保留源文件时间戳失败：{}".format(exc))

    def _finalize_name_inner(self, job: Job) -> None:
        template = str((job.overrides or {}).get("name_template") or "")
        if not template or not paths.needs_post_encode(template):
            return
        current = Path(job.out_path)
        if not current.is_file():
            return

        elapsed = job.elapsed or (time.time() - (job.started or time.time()))
        total = current.stat().st_size

        # `{video_bitrate}` must mean the video stream, not the file average, or the
        # name would quietly lie about what it is quoting.
        video_kbps = ""
        try:
            out_info = probe(str(current))
            if out_info.ok and out_info.video and out_info.video.bit_rate:
                video_kbps = "{}k".format(int(round(out_info.video.bit_rate / 1000.0)))
        except Exception:  # noqa: BLE001 - naming must never fail the job
            video_kbps = ""
        if not video_kbps and job.duration:
            video_kbps = "{}k".format(int(round(total * 8 / job.duration / 1000.0)))

        values = {
            "filesize": paths.pretty_bytes(total),
            "filesize_mb": "{:.1f}".format(total / 1048576.0),
            "filesize_bytes": str(total),
            "video_bitrate": video_kbps,
            "encode_time": paths.pretty_duration(elapsed),
            "encode_secs": str(int(elapsed)),
            "ratio": ("{:.0f}".format(total / job.src_size * 100)
                      if job.src_size else ""),
        }
        stem = Path(job.src).stem
        video_meta = (job.media or {}).get("video") or {}
        render = paths.render_name(template, {
            "stem": stem,
            "profile": job.profile_id,
            "height": (job.media or {}).get("height") or video_meta.get("height") or 0,
            "codec": job.encoder or "",
            **values,
        })
        if not render or not render.strip():
            return
        wanted = current.with_name("{}{}".format(render, current.suffix))
        if wanted.name == current.name:
            return
        try:
            with self._lock:
                while True:
                    target = paths.unique_path(current.parent, wanted.name,
                                               exists=lambda p: self._output_taken(p, job.id))
                    try:
                        paths.rename_without_replace(current, target)
                        break
                    except FileExistsError:
                        continue
                self._set_output(job, str(target))
                add_event(job, 'renamed', '成品改名：{} → {}'.format(current.name, target.name))
                command_file = JOBS_DIR / job.id / 'cmd.txt'
                if command_file.is_file():
                    command_file.write_text(job.command + '\n', encoding='utf-8')
        except OSError as exc:
            job.warnings.append("改名失败（成品保留原名）：{}".format(exc))
            return
        if job.verify and isinstance(job.verify, dict):
            job.verify["renamed_from"] = current.name

    def _run_job(self, job: Job) -> None:
        with self._lock:
            if job.status == 'cancelled':
                return
        caps = get_caps()
        with self._lock:
            if job.status == 'cancelled':
                return
            self._validate_execution(job)
            if Path(job.argv[0]).resolve() != Path(caps.ffmpeg).resolve():
                raise ValueError('任务编码程序与当前 FFmpeg 不一致，请重新添加任务')
            self._allocate_output(job)
            job.argv = ['-n' if token == '-y' else token for token in job.argv]
            job.command = ' '.join(shlex.quote(a) for a in job.argv)
            self._persist()
        argv = list(job.argv)
        job_dir = JOBS_DIR / job.id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "cmd.txt").write_text(job.command + "\n", encoding="utf-8")
        Path(job.out_path).parent.mkdir(parents=True, exist_ok=True)

        # Refuse to start on a nearly-full volume. ffmpeg would otherwise write until
        # ENOSPC and leave a truncated file behind, with an error that says nothing
        # about how much room was needed.
        free = space.free_bytes(str(Path(job.out_path).parent))
        if free and free < space.MIN_FREE_BYTES:
            job.status = "failed"
            job.error = "磁盘空间不足：{} 只剩 {}，至少需要 {} 才能开始编码。".format(
                Path(job.out_path).parent, space.format_bytes(free),
                space.format_bytes(space.MIN_FREE_BYTES))
            job.finished = time.time()
            add_event(job, "failed", job.error)
            self._emit(job, "finished")
            return

        log_path = job_dir / "ffmpeg.log"
        stderr_tail: List[str] = []
        started = time.time()
        add_event(job, "started", "开始编码：{}".format(job.encoder or "?"))

        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            log.write(job.command + "\n\n")
            log.flush()
            try:
                with self._lock:
                    if job.status == 'cancelled':
                        return
                    proc = subprocess.Popen(
                        argv,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        **({k: v for k, v in subprocess_kwargs().items()
                            if k in ("creationflags", "startupinfo")}),
                    )
                    self._procs[job.id] = proc
                    job.pid = proc.pid
                    job.pid_created = winapi.process_creation_time(proc.pid, **self._handle_args(proc))
                    self._persist()
            except OSError as exc:
                job.status = "failed"
                job.error = "无法启动 ffmpeg: {}".format(exc)
                job.finished = time.time()
                self._emit(job, "finished")
                return

            reader = threading.Thread(
                target=self._drain_stderr, args=(proc, log, stderr_tail), daemon=True
            )
            reader.start()

            last_emit = 0.0
            last_progress_at = time.time()
            last_space_check = time.time()
            last_out_time = -1.0
            saw_out_time_us = False
            stall_timeout = 900.0     # 15 min without any progress => give up
            # Shared with the watchdog thread below: the in-loop stall check can only
            # run when a progress line arrives, so it cannot notice an encoder that has
            # gone completely silent -- which is precisely the "hangs forever holding a
            # slot" case. One mutable cell, updated by the reader loop.
            progress_cell = [last_progress_at, job.paused_total]
            stop_watchdog = threading.Event()

            def _watchdog() -> None:
                # Self-terminating: it exits once the encoder is gone, so an encode never
                # leaves a watcher thread behind and no explicit stop signal is needed.
                #
                # Two duties:
                #   1. stall detection (below);
                #   2. MEMORY low-water protection. A software encode commits several GB up
                #      front, and if the machine runs out of physical memory it thrashes
                #      instead of failing -- the user sees a frozen desktop. When free memory
                #      drops under the low-water mark this job is SUSPENDED (reusing the G6
                #      pause machinery, which is measured to hold the file open and use no
                #      CPU) and resumed automatically once memory comes back. Only if it
                #      stays critical past the grace period is the job cancelled, with the
                #      reason written on it.
                while not stop_watchdog.wait(15.0):
                    if proc.poll() is not None:
                        return
                    if self._memory_watchdog_tick(job, proc):
                        return
                    idle = self._stall_seconds(job, progress_cell[0], progress_cell[1])
                    if idle <= stall_timeout:
                        continue
                    job.error = "编码停滞超过 {} 分钟（完全没有输出），已终止".format(
                        int(stall_timeout / 60))
                    add_event(job, "failed", job.error + "（看门狗线程；暂停时间已抵扣）")
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    return

            threading.Thread(target=_watchdog, daemon=True,
                             name="vcs-watch-" + job.id).start()

            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key in ("out_time_us", "out_time_ms"):
                    # ffmpeg >= 6 prints BOTH fields with the SAME microsecond
                    # value (measured on 8.0.1: out_time_us=5333333 and
                    # out_time_ms=5333333 for 5.3333 s). Dividing the _ms field by
                    # 1000 therefore scaled elapsed time up 1000x -- the progress
                    # bar pinned at 99.5% instantly and showed times like 2519:27
                    # for a 5-minute source. Use _us when present and only fall
                    # back to _ms as a millisecond value for older builds.
                    use_us = key == "out_time_us"
                    try:
                        raw = int(value)
                    except ValueError:
                        continue
                    if not use_us:
                        if saw_out_time_us:
                            continue          # duplicate of the value we already used
                        seconds = raw / 1000.0   # legacy ffmpeg: genuinely milliseconds
                    else:
                        saw_out_time_us = True
                        seconds = raw / 1_000_000.0
                    job.out_time = max(0.0, seconds)
                    if job.duration > 0:
                        job.progress = max(job.progress, min(99.5, job.out_time / job.duration * 100))
                    if job.out_time > last_out_time + 0.01:
                        last_out_time = job.out_time
                        last_progress_at = time.time()
                        # Shared with the watchdog thread; see progress_cell above.
                        progress_cell[0] = last_progress_at
                        progress_cell[1] = job.paused_total
                elif key == "fps":
                    try:
                        job.fps = float(value)
                    except ValueError:
                        pass
                elif key == "speed":
                    try:
                        job.speed = float(value.rstrip("x")) if value not in ("N/A", "") else 0.0
                    except ValueError:
                        job.speed = 0.0
                elif key == "total_size":
                    try:
                        job.total_size = int(value)
                    except ValueError:
                        pass
                elif key == "progress" and value == "end":
                    break

                now = time.time()
                if now - last_emit >= 0.25:
                    last_emit = now
                    # Same pause credit as the final figure: otherwise the UI's 用时
                    # jumps up by the whole pause on resume and drops again at the end.
                    job.elapsed = now - started - job.paused_total
                    self._emit(job)

                # Disk guard: once there is enough progress to project the final size,
                # stop BEFORE the volume fills up. Checking free space is cheap, and
                # comparing it against bytes-written-so-far needs no size model.
                if now - last_space_check >= 5.0:
                    last_space_check = now
                    free_now = space.free_bytes(str(Path(job.out_path).parent))
                    # 0 means "could not be determined" (see space.free_bytes), NOT "no
                    # space left". Treating the two the same killed healthy encodes on a
                    # transient stat failure and told the user their disk was empty.
                    short = (space.project_shortfall(free_now, job.total_size, job.progress)
                             if free_now else None)
                    if short:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        job.status = "failed"
                        job.error = short["message"]
                        break

                # Paused time is credited back: while suspended the loop below is
                # blocked and no progress line can arrive, so without this a pause of
                # more than 15 minutes would look like a hung encoder and kill the job
                # the moment it resumed.
                if self._stall_seconds(job, last_progress_at, progress_cell[1], now) > stall_timeout:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    job.status = "failed"
                    job.error = "编码停滞超过 {} 分钟，已终止".format(int(stall_timeout / 60))
                    add_event(job, "failed", job.error + "（看门狗；暂停时间已抵扣）")
                    break

            code = proc.wait()
            stop_watchdog.set()
            reader.join(timeout=5)
            with self._lock:
                self._procs.pop(job.id, None)
                job.pid = 0
                job.pid_created = 0
            # F8: keep a large log's TAIL rather than all of it. ffmpeg's opening lines
            # are the input header (already in report.json), so nothing diagnostic is
            # lost, and an unattended long encode no longer leaves megabytes behind.
            try:
                trimmed = trim_log(log_path)
                if trimmed.get("trimmed"):
                    job.log_trimmed = trimmed
                    add_event(job, "log_trimmed", "日志 {:.0f} KB → {:.0f} KB（只保留尾部）".format(
                        trimmed["original_bytes"] / 1024.0, trimmed["kept_bytes"] / 1024.0))
            except Exception:  # noqa: BLE001 - housekeeping must not fail a job
                pass

        # Apply post-encode naming tokens ({filesize_mb}, {encode_time}, ...) BEFORE
        # verification, so every later stage -- verifier, preview, compare, report --
        # sees the final path and nothing has to be re-pointed afterwards.
        with self._lock:
            if job.status == 'cancelled':
                job.elapsed = max(0.0, time.time() - started - job.paused_total)
                self._emit(job, 'finished')
                return

        # F7 -- carry the source's file timestamps onto the output. Done after renaming
        # (a rename keeps mtime, but doing it last means the check below inspects the
        # file that will actually be delivered).

        # Paused time is not encoding time: a job paused for an hour and encoded for one
        # minute took one minute, and reporting otherwise would make the speed column
        # (`speed`, computed from elapsed) nonsense.
        job.elapsed = time.time() - started - job.paused_total

        if job.status == "failed":
            job.finished = time.time()
            self._emit(job, "finished")
            return

        if code != 0:
            tail = "\n".join(stderr_tail[-12:]) or "ffmpeg 退出码 {}".format(code)
            job.status = 'failed'
            job.error = (job.error or tail.strip())[-800:]
            job.finished = time.time()
            add_event(job, job.status, "ffmpeg 退出码 {}：{}".format(
                code, (job.error or "无错误输出")[:300]))
            self._emit(job, "finished")
            return

        with self._lock:
            if job.status == 'cancelled':
                return
            self._finalize_name(job)
        self._copy_timestamps(job)

        # -- verification gate -------------------------------------------------
        # Verification decodes the whole output, which on a big file takes a while.
        # Report its own progress so the UI shows movement instead of a frozen 99.9%.
        with self._lock:
            if job.status == 'cancelled':
                return
            job.status = "verifying"
        job.progress = 99.9
        job.verify_stage = "probe"
        job.verify_percent = 0.0
        self._emit(job)
        source = self._retain_probe.get(job.id)
        if source is None:
            source = probe(job.src)
        verify_identity = actions.file_identity(job.out_path)

        last_verify_emit = 0.0
        last_verify_stage = job.verify_stage

        def on_verify(payload: Dict[str, Any]) -> None:
            nonlocal last_verify_emit, last_verify_stage
            job.verify_stage = str(payload.get("stage") or "")
            percent = payload.get("percent")
            job.verify_percent = float(percent) if isinstance(percent, (int, float)) else None
            job.verify_frames = int(payload.get("frames") or 0)
            now = time.time()
            # A stage change is always reported immediately: throttling it would mean
            # the user keeps reading "读取输出信息" while the decode is already under
            # way, and on a fast file the whole stage could pass unseen. The throttle
            # exists to limit frame-by-frame chatter, not to hide transitions.
            if job.verify_stage != last_verify_stage or now - last_verify_emit >= 0.4:
                last_verify_emit = now
                if job.verify_stage != last_verify_stage:
                    # Record the TRANSITION, not every progress tick: the timeline is for
                    # "what happened when", and 400 progress updates would bury it.
                    add_event(job, "verify", "校验阶段：{}".format(
                        job.verify_stage or "完成"))
                last_verify_stage = job.verify_stage
                self._emit(job)

        mapped = [job.argv[i + 1] for i, arg in enumerate(job.argv[:-1]) if arg == '-map']
        from .verifier import stream_expectations
        result: VerifyResult = verify(job.out_path, source, on_progress=on_verify,
                                      deep=self._deep_verify(),
                                      # V2 only relaxes for a remux; a re-encode with a
                                      # long keyframe gap means something bypassed the
                                      # keyframe rules this project always sets.
                                      reencoded=(self._argv_encoder(job.argv) != "copy"),
                                      expected_audio=len(source.audio_streams) if '0:a?' in mapped else 0,
                                      expected_subtitles=len(source.subtitle_streams) if '0:s?' in mapped else 0,
                                      expected_streams=stream_expectations(source,job.argv),
                                      keep_metadata=not bool(job.overrides.get('strip_metadata')),
                                      keep_timestamps=bool(
                                          (job.overrides or {}).get("keep_timestamps", True)),
                                      loudness=(audio.clamp_target(job.overrides.get("loudness"))
                                                if audio.clamp_target(
                                                    job.overrides.get("loudness")) < 0
                                                else None),
                                      loudness_tp=job.overrides.get("loudness_tp"))
        with self._lock:
            if job.status == 'cancelled':
                return
            job.verify = result.to_dict()
        # What `loudnorm` actually did, read back from the encode log. It falls back
        # from Linear to Dynamic on its own when the gain would break the true-peak
        # ceiling, and that rewrites the mix's dynamics -- so it gets reported.
        if job.overrides.get("loudness"):
            # `stderr_tail` holds the last 200 lines of this encode (see _drain_stderr)
            # and the loudnorm summary is printed at the very end of the apply pass.
            applied = audio.parse_apply("\n".join(stderr_tail))
            if applied:
                job.verify["loudness_applied"] = applied
                add_event(job, "loudness", "响度标准化实际模式：{}（成品自报 {:.1f} LUFS）".format(
                    applied.get("mode"), float(applied.get("output_i") or 0.0)))
            for note in audio.apply_warnings(applied, job.overrides.get("loudness_measured") or {},
                                             job.overrides.get("loudness")):
                job.warnings.append(note)
                add_event(job, "warning", note)
        job.total_size = result.size_bytes or job.total_size
        job.verify_stage = ""
        job.verify_percent = 100.0
        job.progress = 100.0

        # Quality score (F5), opt-in because it is the slowest check we run. It is
        # attached to the verification report rather than replacing it: the gate says
        # "this file is deliverable", the score says "and here is what you traded".
        if result.verdict != "failed" and self._vmaf_enabled():
            try:
                job.verify_stage = "quality"
                job.verify_percent = 0.0
                self._emit(job)
                quality = measure_quality(job.out_path, job.src, source)
                job.verify["vmaf"] = quality.to_dict()
                if not quality.ok and quality.error:
                    job.warnings.append("质量评分未完成：{}".format(quality.error))
            except Exception as exc:  # noqa: BLE001 - scoring must never fail a job
                job.warnings.append("质量评分未完成：{}".format(exc))
            finally:
                job.verify_stage = ""
                job.verify_percent = 100.0

        with self._lock:
            if job.status == 'cancelled':
                return
            if result.verdict == "failed":
                job.status = "failed"
                first_fail = next((c for c in result.checks if c.status == "fail"), None)
                job.error = "交付校验未通过：{}".format(first_fail.detail if first_fail else "未知原因")
            else:
                job.status = "done"
                if result.verdict == "risky":
                    job.degraded = True
        add_event(job, job.status, "{}；校验 {}（{}）".format(
            "编码完成" if job.status == "done" else "交付校验未通过",
            result.verdict,
            "、".join("{}={}".format(c.id, c.status) for c in result.checks)))
        if actions.identity_matches(job.out_path, verify_identity):
            job.output_identity = verify_identity
        job.finished = time.time()

        report = job.to_dict()
        report["source"] = source.to_dict() if source else {}
        (JOBS_DIR / job.id / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self._emit(job, "finished")

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen, log, tail: List[str]) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            log.write(line)
            log.flush()
            tail.append(line.rstrip("\n"))
            if len(tail) > 200:
                del tail[0]


_MANAGER: Optional[JobManager] = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> JobManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = JobManager()
            thread = threading.Thread(target=_MANAGER.run_forever, daemon=True)
            thread.start()
        return _MANAGER
