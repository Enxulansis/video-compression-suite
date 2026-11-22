"""Run a build/test once with a deadline, private log and owned Windows Job tree."""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes as W
import json
from pathlib import Path
import subprocess
import sys
import time


class IO(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]


class BASIC(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong), ('flags', W.DWORD),
                ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('active', W.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', W.DWORD), ('scheduling', W.DWORD)]


class EXTENDED(ctypes.Structure):
    _fields_ = [('basic', BASIC), ('io', IO), ('process_limit', ctypes.c_size_t),
                ('job_limit', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]


class MEMORY(ctypes.Structure):
    _fields_ = [('length', W.DWORD), ('load', W.DWORD)] + [(n, ctypes.c_ulonglong) for n in
                ('total', 'avail', 'commit_total', 'commit_avail', 'virtual_total', 'virtual_avail', 'extended')]


def memory():
    value = MEMORY()
    value.length = ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        raise ctypes.WinError()
    return value.avail // 1048576, value.commit_avail // 1048576


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--timeout', type=int, required=True)
    p.add_argument('--reserve', type=int, default=512)
    p.add_argument('--log', type=Path, required=True)
    p.add_argument('command', nargs=argparse.REMAINDER)
    a = p.parse_args()
    command = a.command[1:] if a.command[0] == '--' else a.command
    start_free, start_commit = memory()
    if min(start_free, start_commit) < 2500 + a.reserve:
        print(json.dumps({'deferred': 'memory admission', 'free_mib': start_free, 'commit_mib': start_commit}))
        return 75
    a.log.parent.mkdir(parents=True, exist_ok=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, W.LPCWSTR]
    kernel.CreateJobObjectW.restype = W.HANDLE
    kernel.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    kernel.TerminateJobObject.argtypes = [W.HANDLE, W.UINT]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = EXTENDED()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel.CloseHandle(job)
        raise ctypes.WinError(ctypes.get_last_error())
    proc = None
    start = time.monotonic()
    minimum = start_free
    try:
        with a.log.open('w', encoding='utf-8') as log:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if not kernel.AssignProcessToJobObject(job, W.HANDLE(int(proc._handle))):
                proc.kill()
                raise ctypes.WinError(ctypes.get_last_error())
            print(json.dumps({'pid': proc.pid, 'log': str(a.log), 'timeout_s': a.timeout}), flush=True)
            while proc.poll() is None:
                free, commit = memory()
                minimum = min(minimum, free)
                if min(free, commit) < 2500:
                    kernel.TerminateJobObject(job, 75)
                    proc.wait(timeout=10)
                    print(json.dumps({'deferred': 'memory low water', 'free_mib': free, 'commit_mib': commit}))
                    return 75
                if time.monotonic() - start >= a.timeout:
                    kernel.TerminateJobObject(job, 124)
                    proc.wait(timeout=10)
                    print('Execution deadline exceeded', flush=True)
                    return 124
                time.sleep(.2)
        print(json.dumps({'exit_code': proc.returncode, 'elapsed_s': round(time.monotonic() - start, 2),
                          'minimum_free_mib': minimum}), flush=True)
        return proc.returncode
    finally:
        kernel.CloseHandle(job)


if __name__ == '__main__':
    raise SystemExit(main())
