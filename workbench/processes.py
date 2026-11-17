"""Windows process queries and owned task trees."""

import ctypes
from ctypes import wintypes
import os
import runpy
import subprocess
import sys
import threading
import time


def windows_process_alive(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    # PROCESS_QUERY_LIMITED_INFORMATION never grants permission to terminate.
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no process with this PID.
            return False
        if error == 5:  # ERROR_ACCESS_DENIED: do not treat an inaccessible task as dead.
            return True
        raise ctypes.WinError(error)
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


class JobBasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class JobExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JobBasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class JobAccounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", ctypes.c_uint32), ("TotalProcesses", ctypes.c_uint32),
                ("ActiveProcesses", ctypes.c_uint32), ("TotalTerminatedProcesses", ctypes.c_uint32)]


def windows_job_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.c_void_p], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


class WindowsJob:
    def __init__(self):
        self.kernel = windows_job_api()
        self.lock = threading.Lock()
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = JobExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close_handle()
            raise error

    def close_handle(self):
        if not self.kernel.CloseHandle(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = None

    def stop(self, child):
        # Shutdown and the worker's finally block may both reach this method.
        with self.lock:
            if self.handle is None:
                return
            # Also stop a bootstrap that has not entered the job yet.
            if child.poll() is None:
                child.terminate()
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            child.wait(timeout=5)
            deadline = time.monotonic() + 5
            while True:
                accounting = JobAccounting()
                if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting),
                                                             ctypes.sizeof(accounting), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not accounting.ActiveProcesses:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Windows task pid={child.pid} still has {accounting.ActiveProcesses} active processes")
                time.sleep(0.01)
            self.close_handle()

    def wait_for_exit(self, child):
        child.wait()
        # A dead worker's descendants may still hold stdout open. Stop the job
        # independently of the log reader so it can receive EOF and finish.
        self.stop(child)


def start_windows_child(argv, **options):
    job = WindowsJob()
    child = None
    try:
        startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [job.handle]})
        os.set_handle_inheritable(job.handle, True)
        try:
            # Business code only runs after the bootstrap has joined the job.
            child = subprocess.Popen([argv[0], "-u", "-m", "workbench.processes", str(job.handle), *argv[2:]],
                                     startupinfo=startup, close_fds=True, **options)
        finally:
            os.set_handle_inheritable(job.handle, False)
        child.windows_job = job
        threading.Thread(target=job.wait_for_exit, args=(child,), daemon=True).start()
        return child
    except BaseException:
        if child is None:
            job.close_handle()
        else:
            job.stop(child)
        raise


def run_windows_worker(handle, argv):
    kernel = windows_job_api()
    try:
        if not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(-1)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        # The service must be the only handle owner before any descendants run.
        # If it already died, closing this last handle also terminates this worker.
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    if argv[0] == "-m":
        sys.argv = argv[1:]
        runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
    else:
        sys.argv = argv
        runpy.run_path(argv[0], run_name="__main__")


if __name__ == "__main__":
    run_windows_worker(int(sys.argv[1]), sys.argv[2:])
