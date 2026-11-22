"""Bounded auxiliary requests, shared deadlines and queue resource reservations."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace

REQUEST_SECONDS = 300
FRAME_SECONDS = 30
_requests = threading.BoundedSemaphore(1)
_frame_waiters = threading.BoundedSemaphore(3)
_children = threading.BoundedSemaphore(2)


@contextmanager
def startup_probe_slot(timeout):
    """Share the child cap with startup probes; waiting consumes their step timeout."""
    deadline = time.monotonic() + timeout
    if not _children.acquire(timeout=max(0, timeout)):
        raise TimeoutError('启动探测等待辅助进程名额超时')
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('启动探测等待辅助进程名额超时')
        yield remaining
    finally:
        _children.release()


class Busy(RuntimeError):
    status = 429


class Expired(RuntimeError):
    status = 504


@dataclass
class Budget:
    manager: object
    deadline: float
    sources: dict = field(default_factory=dict)
    failure: object = None


_budget = ContextVar('auxiliary_budget', default=None)


@contextmanager
def background(manager, timeout=REQUEST_SECONDS):
    token = _budget.set(Budget(manager, time.monotonic() + timeout))
    try:
        yield
        check()
    except Exception:
        check()
        raise
    finally:
        _budget.reset(token)


@contextmanager
def request(manager, timeout=REQUEST_SECONDS, *, wait=False):
    with background(manager, timeout):
        if wait:
            if not _frame_waiters.acquire(blocking=False):
                raise Busy('取帧等待已满，请稍后重试')
            try:
                acquired = _requests.acquire(timeout=check())
            finally:
                _frame_waiters.release()
        else:
            acquired = _requests.acquire(blocking=False)
        if not acquired:
            check()
            raise Busy('另一个辅助测算正在运行，请完成后重试')
        try:
            check()
            yield
        finally:
            _requests.release()


def check():
    budget = _budget.get()
    if budget is None:
        return None
    if budget.failure:
        raise budget.failure
    remaining = budget.deadline - time.monotonic()
    if remaining <= 0:
        budget.failure = Expired('辅助任务已达到总时限，已停止，请减少测算范围后重试')
        raise budget.failure
    return remaining


def note_media(info):
    budget = _budget.get()
    if budget is not None and info.ok and info.video:
        video = info.video
        budget.sources[str(Path(info.path).resolve())] = (
            (video.display_width, video.display_height), video.pix_fmt)


def ensure_media(path):
    budget = _budget.get()
    if budget is not None and str(Path(path).resolve()) not in budget.sources:
        from .probe import probe
        info = probe(path)
        if not info.ok or not info.video:
            raise Busy(info.error or '无法安全估算输入内存')


def _need(argv, budget):
    from . import memory as M
    if '-skip_frame' in argv or '-show_frames' in argv:
        data = budget.sources.get(str(Path(argv[-1]).resolve()))
        if data is None:
            raise Busy('关键帧扫描缺少输入尺寸，无法安全估算解码内存')
        size, fmt = data
        return 64 + M.decoder_reserve_mb(*size, fmt), 'cpu'
    if Path(str(argv[0])).stem.lower() != 'ffmpeg':
        return 64, None  # metadata probe: separate child limit, no encode slot
    if '-vn' in argv:
        return 64, 'cpu'
    sources = []
    for index, token in enumerate(argv[:-1]):
        if token == '-i':
            data = budget.sources.get(str(Path(argv[index + 1]).resolve()))
            if data is None:
                raise Busy('辅助任务缺少输入尺寸，无法安全估算内存，请重新探测文件')
            sources.append(data)
    if not sources:
        raise Busy('辅助任务缺少媒体输入，无法安全估算内存')
    encoder = M.last_option(argv, ('-c:v', '-codec:v', '-vcodec', '-c'))
    decoded = sum(M.decoder_reserve_mb(*size, fmt) for size, fmt in sources)
    output_size = M.encoder_geometry(argv, sources[0][0])
    if min(output_size) <= 0:
        raise Busy('无法确定辅助滤镜的最终尺寸，请使用明确尺寸后重试')
    encoded = 64
    if encoder and encoder not in ('copy', 'mjpeg', 'png', 'rawvideo', 'wrapped_avframe'):
        # The shared plan estimate includes the first input and filter pools.
        # Add only other input decoder pools, without counting the first twice.
        encoded = M.estimate_plan_mb(SimpleNamespace(
            argv=argv, encoder=encoder, out_size=sources[0][0], source_size=sources[0][0],
            source_pix_fmt=sources[0][1]))[0]
        decoded -= M.decoder_reserve_mb(*sources[0][0],sources[0][1])
    else:
        encoded += M.filter_reserve_mb(argv,sources[0][0],sources[0][1])[0]
    pool = 'gpu' if encoder.endswith(('_nvenc', '_qsv', '_amf')) else 'cpu'
    return encoded + decoded, pool


def run(argv, *, timeout, **kwargs):
    """subprocess.run with one shared deadline; run itself kills/reaps on timeout."""
    budget = _budget.get()
    if budget is None:
        return subprocess.run(argv, timeout=timeout, **kwargs)
    try:
        remaining = check()
        if '-skip_frame' in argv or '-show_frames' in argv:
            ensure_media(argv[-1])
        if not _children.acquire(blocking=False):
            raise Busy('辅助探测进程已满，请稍后重试')
        try:
            need, pool = _need(argv, budget)
            with budget.manager.auxiliary_slot(need, pool):
                result = subprocess.run(argv, timeout=min(timeout, check()), **kwargs)
                check()
                return result
        finally:
            _children.release()
    except subprocess.TimeoutExpired as exc:
        budget.failure = Expired('辅助子进程超时，已终止并回收，请减少测算范围后重试')
        raise budget.failure from exc
    except (Busy, Expired) as exc:
        budget.failure = exc
        raise


@contextmanager
def stream(argv, *, timeout, **kwargs):
    """Bound silent stdout reads and reap only the child started here."""
    budget = _budget.get()
    acquired = False
    expired = threading.Event()
    try:
        remaining = check()
        if budget is not None:
            acquired = _children.acquire(blocking=False)
            if not acquired:
                raise Busy('辅助探测进程已满，请稍后重试')
            need, pool = _need(argv, budget)
            slot = budget.manager.auxiliary_slot(need, pool)
        else:
            from contextlib import nullcontext
            slot = nullcontext()
        with slot:
            proc = subprocess.Popen(argv, **kwargs)
            def stop():
                if proc.poll() is None:
                    expired.set()
                    try:
                        proc.kill()
                    except OSError:
                        pass
            timer = threading.Timer(min(timeout, remaining) if remaining else timeout, stop)
            timer.daemon = True
            timer.start()
            try:
                yield proc
            finally:
                timer.cancel()
                timer.join(timeout=2)
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
            if expired.is_set():
                raise Expired('辅助扫描超时，已终止并回收')
            check()
    except (Busy, Expired) as exc:
        if budget is not None:
            budget.failure = exc
        raise
    finally:
        if acquired:
            _children.release()
