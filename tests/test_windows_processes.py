"""Windows task recovery must query a process without terminating it."""
import ctypes
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from workbench import jobs, processes


class WindowsProcesses(unittest.TestCase):
    def setUp(self):
        self.handle = 0x123456789
        self.kernel = SimpleNamespace(OpenProcess=Mock(return_value=self.handle),
                                      GetExitCodeProcess=Mock(side_effect=self.running), CloseHandle=Mock(return_value=1))
        self.signal = Mock(side_effect=AssertionError("Windows os.kill(pid, 0) would terminate the process"))
        self.patches = [patch.object(jobs, "os", SimpleNamespace(name="nt", kill=self.signal)),
                        patch.object(ctypes, "WinDLL", return_value=self.kernel, create=True),
                        patch.object(ctypes, "get_last_error", return_value=87, create=True),
                        patch.object(ctypes, "WinError", side_effect=lambda code: OSError(code, f"Windows error {code}"), create=True)]
        for mocked in self.patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    @staticmethod
    def running(handle, exit_code):
        exit_code._obj.value = 259
        return 1

    def test_live_process_is_checked_without_sending_a_signal(self):
        self.assertTrue(jobs.process_alive(45394))
        self.signal.assert_not_called()
        self.kernel.OpenProcess.assert_called_once_with(0x1000, False, 45394)
        self.assertIs(self.kernel.OpenProcess.restype, wintypes.HANDLE)
        self.assertEqual(self.kernel.GetExitCodeProcess.argtypes[0], wintypes.HANDLE)
        self.assertEqual(self.kernel.CloseHandle.argtypes, [wintypes.HANDLE])
        self.kernel.CloseHandle.assert_called_once_with(self.handle)

    def test_exited_process_releases_its_handle_and_returns_false(self):
        def exited(handle, exit_code):
            exit_code._obj.value = 0
            return 1

        self.kernel.GetExitCodeProcess.side_effect = exited
        self.assertFalse(jobs.process_alive(45394))
        self.kernel.CloseHandle.assert_called_once_with(self.handle)

    def test_missing_process_returns_false_without_closing_an_invalid_handle(self):
        self.kernel.OpenProcess.return_value = None
        self.assertFalse(jobs.process_alive(45394))
        self.kernel.GetExitCodeProcess.assert_not_called()
        self.kernel.CloseHandle.assert_not_called()

    def test_access_denied_is_not_mistaken_for_a_stopped_process(self):
        self.kernel.OpenProcess.return_value = None
        with patch.object(ctypes, "get_last_error", return_value=5):
            self.assertTrue(jobs.process_alive(45394))
        self.kernel.CloseHandle.assert_not_called()

    def test_unexpected_open_error_is_reported(self):
        self.kernel.OpenProcess.return_value = None
        with patch.object(ctypes, "get_last_error", return_value=8):
            with self.assertRaisesRegex(OSError, "Windows error 8"):
                jobs.process_alive(45394)

    def test_failed_exit_query_reports_error_and_closes_handle(self):
        self.kernel.GetExitCodeProcess.side_effect = None
        self.kernel.GetExitCodeProcess.return_value = 0
        with patch.object(ctypes, "get_last_error", return_value=6):
            with self.assertRaisesRegex(OSError, "Windows error 6"):
                jobs.process_alive(45394)
        self.kernel.CloseHandle.assert_called_once_with(self.handle)

    def test_empty_pid_never_opens_or_signals_a_process(self):
        self.assertFalse(jobs.process_alive(None))
        self.assertFalse(jobs.process_alive(0))
        self.kernel.OpenProcess.assert_not_called()
        self.signal.assert_not_called()


class WindowsTreeCleanup(unittest.TestCase):
    def test_worker_overrides_disabled_utf8_mode_in_the_actual_child_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[1]
            folder = Path(directory)
            worker = folder / "encoding.py"
            worker.write_text("import json, locale, os, sys\n"
                              "print(json.dumps({'utf8_mode': sys.flags.utf8_mode, "
                              "'encoding': locale.getpreferredencoding(False), "
                              "'pythonutf8': os.environ.get('PYTHONUTF8')}), flush=True)\n", encoding="utf-8")
            runner = jobs.Jobs(root, folder / "storage")
            self.addCleanup(runner.close)
            record = {"id": "utf8-worker", "status": "queued", "kind": "one", "demo": True,
                      "output_dir": str(folder), "lang": "en"}
            runner.records[record["id"]] = record
            original_popen = subprocess.Popen
            environments = []

            def start_worker(*args, **options):
                environments.append(dict(options["env"]))
                child = original_popen(*args, **options)
                self.addCleanup(child.stdout.close)
                return child

            with patch.object(jobs, "command", return_value=[sys.executable, "-u", str(worker)]), \
                 patch.object(jobs.subprocess, "Popen", side_effect=start_worker):
                runner.run(record, {"kind": "one", "demo": True}, {**os.environ, "PYTHONUTF8": "0"})
            self.assertEqual(record["status"], "succeeded", record.get("error"))
            log = (runner.storage / "jobs" / "utf8-worker.log").read_text(encoding="utf-8")
            observed = next(json.loads(line) for line in log.splitlines() if line.startswith("{"))
            self.assertEqual(observed, {"utf8_mode": 1, "encoding": "utf-8", "pythonutf8": "1"})
            self.assertEqual(environments[0]["PYTHONUTF8"], "1")

    def test_exited_posix_worker_still_stops_its_process_group(self):
        child = Mock(pid=12345)
        child.poll.return_value = 0
        signal_group = Mock(side_effect=[None, None, ProcessLookupError()])
        with patch.object(jobs, "os", SimpleNamespace(name="posix", killpg=signal_group)), \
             patch.object(jobs, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9)):
            jobs.stop_child(child)
        self.assertEqual(signal_group.call_args_list,
                         [call(12345, 15), call(12345, 9), call(12345, 0)])

    def test_concurrent_posix_cleanup_signals_the_owned_group_only_once(self):
        child = Mock(pid=12345)
        child.poll.return_value = 0
        signal_group = Mock(side_effect=[None, None, ProcessLookupError()])
        with patch.object(jobs, "os", SimpleNamespace(name="posix", killpg=signal_group)), \
             patch.object(jobs, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9)):
            with ThreadPoolExecutor(max_workers=2) as callers:
                list(callers.map(jobs.stop_child, [child, child]))
            jobs.stop_child(child)
        self.assertEqual(signal_group.call_count, 3)

    def test_exited_worker_does_not_skip_its_remaining_job_processes(self):
        child = Mock()
        child.poll.return_value = 0
        with patch.object(jobs, "os", SimpleNamespace(name="nt")):
            jobs.stop_child(child)
        child.windows_job.stop.assert_called_once_with(child)
        child.terminate.assert_not_called()

    def test_running_worker_stops_the_job_not_only_the_root_process(self):
        child = Mock()
        child.poll.return_value = None
        with patch.object(jobs, "os", SimpleNamespace(name="nt")):
            jobs.stop_child(child)
        child.windows_job.stop.assert_called_once_with(child)
        child.terminate.assert_not_called()

    def test_failed_tree_cleanup_keeps_new_work_blocked_when_root_has_exited(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = jobs.Jobs(Path(directory), Path(directory) / "storage")
            child = Mock()
            child.poll.return_value = 0
            runner.children["unfinished"] = child
            runner.records["unfinished"] = {"status": "failed", "pid": 12345}
            try:
                with patch.object(jobs, "process_alive", return_value=False):
                    with self.assertRaises(jobs.InputError):
                        runner.available()
            finally:
                runner.children.clear()
                runner.close()


class WindowsJobOwnership(unittest.TestCase):
    def setUp(self):
        self.kernel = SimpleNamespace(**{name: Mock(return_value=1) for name in
            ("CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject",
             "TerminateJobObject", "QueryInformationJobObject", "CloseHandle")})
        self.kernel.CreateJobObjectW.return_value = 0x123456789
        self.patches = [patch.object(processes, "windows_job_api", return_value=self.kernel),
                        patch.object(ctypes, "get_last_error", return_value=5, create=True),
                        patch.object(ctypes, "WinError", side_effect=lambda code: OSError(code, f"Windows error {code}"), create=True)]
        for mocked in self.patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_worker_joins_and_releases_inherited_handle_before_business_code(self):
        events = []
        self.kernel.AssignProcessToJobObject.side_effect = lambda *args: events.append("assign") or 1
        self.kernel.CloseHandle.side_effect = lambda *args: events.append("close") or 1
        with patch.object(sys, "argv", []), patch.object(processes.runpy, "run_module", side_effect=lambda *a, **k: events.append("run")):
            processes.run_windows_worker(42, ["-m", "workbench.demo", "request.json"])
        self.assertEqual(events, ["assign", "close", "run"])

    def test_failed_assignment_closes_handle_without_running_business_code(self):
        self.kernel.AssignProcessToJobObject.return_value = 0
        with patch.object(processes.runpy, "run_module") as run:
            with self.assertRaises(OSError):
                processes.run_windows_worker(42, ["-m", "workbench.demo", "request.json"])
        run.assert_not_called()
        self.kernel.CloseHandle.assert_called_once_with(42)

    def test_stop_waits_for_descendants_and_closes_handle_only_once(self):
        active = iter([2, 0])
        self.kernel.QueryInformationJobObject.side_effect = lambda handle, kind, accounting, size, written: (
            setattr(accounting._obj, "ActiveProcesses", next(active)) or 1)
        owned = processes.WindowsJob()
        child = Mock(pid=12345)
        child.poll.return_value = 0
        with patch.object(processes.time, "sleep"):
            first = threading.Thread(target=owned.stop, args=(child,))
            second = threading.Thread(target=owned.stop, args=(child,))
            first.start()
            second.start()
            first.join(timeout=5)
            second.join(timeout=5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.kernel.TerminateJobObject.assert_called_once_with(0x123456789, 1)
        self.assertEqual(self.kernel.QueryInformationJobObject.call_count, 2)
        self.kernel.CloseHandle.assert_called_once_with(0x123456789)
        self.assertIsNone(owned.handle)
        child.terminate.assert_not_called()

    def test_failed_cleanup_retains_handle_and_reports_error(self):
        owned = processes.WindowsJob()
        child = Mock()
        child.poll.return_value = 0
        self.kernel.QueryInformationJobObject.return_value = 0
        with self.assertRaises(OSError):
            owned.stop(child)
        self.assertEqual(owned.handle, 0x123456789)
        self.kernel.CloseHandle.assert_not_called()

    def test_launch_shares_handle_only_with_bootstrap_and_restores_noninheritance(self):
        inherited = Mock()
        child = Mock()
        with patch.object(processes.os, "set_handle_inheritable", inherited, create=True), \
             patch.object(processes.subprocess, "STARTUPINFO", create=True) as startup, \
             patch.object(processes.subprocess, "Popen", return_value=child) as popen, \
             patch.object(processes.threading, "Thread"):
            processes.start_windows_child(["python.exe", "-u", "h3ref.py", "one"])
        startup.assert_called_once_with(lpAttributeList={"handle_list": [0x123456789]})
        self.assertEqual(inherited.call_args_list, [call(0x123456789, True), call(0x123456789, False)])
        self.assertTrue(popen.call_args.kwargs["close_fds"])
        self.assertEqual(popen.call_args.args[0][-2:], ["h3ref.py", "one"])


@unittest.skipIf(os.name == "nt", "requires POSIX process groups")
class PosixProcessTreeIntegration(unittest.TestCase):
    def test_jobs_run_finishes_after_worker_exits_with_descendant_holding_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "descendant-ready"
            descendant = ("import signal,time; from pathlib import Path; "
                          "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                          f"Path({str(ready)!r}).touch(); time.sleep(60)")
            worker = root / "worker.py"
            worker.write_text("import os, subprocess, sys, time\nfrom pathlib import Path\n"
                              f"subprocess.Popen([sys.executable, '-c', {descendant!r}])\n"
                              "deadline = time.monotonic() + 5\n"
                              f"while not Path({str(ready)!r}).exists():\n"
                              "    if time.monotonic() >= deadline: raise TimeoutError('descendant not ready')\n"
                              "    time.sleep(0.01)\nos._exit(23)\n", encoding="utf-8")
            runner = jobs.Jobs(root, root / "storage")
            record = {"id": "pipe-owner", "status": "queued", "kind": "one", "demo": True,
                      "output_dir": str(root), "lang": "en"}
            runner.records[record["id"]] = record
            request = {"kind": "one", "demo": True}
            started = threading.Event()
            children = []
            original_popen = subprocess.Popen

            def start_owned_worker(*args, **kwargs):
                child = original_popen(*args, **kwargs)
                children.append(child)
                started.set()
                return child

            with patch.object(jobs, "command", return_value=[sys.executable, "-u", str(worker)]), \
                 patch.object(jobs.subprocess, "Popen", side_effect=start_owned_worker):
                worker_thread = threading.Thread(target=runner.run, args=(record, request, dict(os.environ)), daemon=True)
                worker_thread.start()
                try:
                    self.assertTrue(started.wait(timeout=5), "task process never started")
                    child = children[0]
                    self.assertEqual(child.wait(timeout=5), 23)
                    self.assertTrue(ready.exists())
                    worker_thread.join(timeout=5)
                    print(json.dumps({"probe": "posix-inherited-log-pipe", "worker_exit": child.returncode,
                                      "runner_thread_alive": worker_thread.is_alive(), "status": record["status"]}))
                    self.assertFalse(worker_thread.is_alive(), "root exited but Jobs.run is blocked on the descendant's log pipe")
                    self.assertEqual(record["status"], "failed")
                    self.assertIn("23", record["error"])
                    self.assertNotIn("pid", record)
                    self.assertFalse(runner.children)
                    persisted = json.loads((runner.storage / "jobs" / "pipe-owner.json").read_text(encoding="utf-8"))
                    self.assertEqual(persisted["status"], "failed")
                    runner.available()
                finally:
                    for child in children:
                        jobs.stop_child(child)
                    worker_thread.join(timeout=5)
                    runner.close()
                    for child in children:
                        child.stdout.close()
                    self.assertFalse(worker_thread.is_alive(), "owned test worker did not stop during cleanup")

    def test_exited_worker_still_kills_a_descendant_ignoring_sigterm(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            descendant = ("import signal,time; from pathlib import Path; "
                          "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                          f"Path({str(ready)!r}).touch(); time.sleep(60)")
            worker = ("import os,subprocess,sys; "
                      f"subprocess.Popen([sys.executable,'-c',{descendant!r}], "
                      "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); os._exit(0)")
            child = subprocess.Popen([sys.executable, "-c", worker], start_new_session=True)
            try:
                self.assertEqual(child.wait(timeout=5), 0)
                deadline = time.monotonic() + 5
                while not ready.exists():
                    if time.monotonic() >= deadline:
                        self.fail("descendant did not install its SIGTERM handler")
                    time.sleep(0.01)
                os.killpg(child.pid, 0)
                jobs.stop_child(child)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(child.pid, 0)
            finally:
                jobs.stop_child(child)


@unittest.skipUnless(os.name == "nt", "requires actual Windows Job Objects")
class WindowsProcessTreeIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.script = self.root / "tree.py"
        self.script.write_text("""import os, subprocess, sys, time
from pathlib import Path
mode, folder = sys.argv[1], Path(sys.argv[2])
(folder / (mode + '.pid')).write_text(str(os.getpid()))
if mode != 'grandchild':
    subprocess.Popen([sys.executable, '-u', __file__, 'child' if mode == 'worker' else 'grandchild', str(folder)])
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    if mode == 'worker' and (folder / 'worker-exit').exists():
        os._exit(0)
    time.sleep(0.01)
""", encoding="utf-8")
        self.process_ids = []
        self.addCleanup(self.remove_survivors)

    def wait_until(self, predicate, description):
        deadline = time.monotonic() + 10
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(description)
            time.sleep(0.01)

    def tree_ready(self):
        self.wait_until(lambda: (self.root / "grandchild.pid").exists(), "grandchild never started")
        for mode in ("worker", "child", "grandchild"):
            path = self.root / f"{mode}.pid"
            self.wait_until(lambda: path.stat().st_size > 0, f"{mode} PID was not written")
            self.process_ids.append(int(path.read_text()))
        self.assertTrue(all(processes.windows_process_alive(pid) for pid in self.process_ids))

    def remove_survivors(self):
        # Only PIDs from this test's owned, bounded fixtures are eligible for cleanup.
        for path in self.root.glob("*.pid"):
            pid = int(path.read_text())
            if processes.windows_process_alive(pid):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10)

    def assert_tree_stopped(self):
        self.wait_until(lambda: not any(processes.windows_process_alive(pid) for pid in self.process_ids),
                        "a worker descendant survived cleanup")

    def start_worker(self):
        child = processes.start_windows_child([sys.executable, "-u", str(self.script), "worker", str(self.root)],
                                              cwd=Path(__file__).resolve().parents[1], env=dict(os.environ),
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                              encoding="utf-8", errors="replace")
        self.addCleanup(child.stdout.close)
        self.addCleanup(jobs.stop_child, child)
        return child

    def test_stop_waits_for_actual_child_and_grandchild(self):
        child = self.start_worker()
        self.tree_ready()
        jobs.stop_child(child)
        self.assert_tree_stopped()

    def test_root_exit_cleans_descendants_that_still_hold_the_log_pipe(self):
        child = self.start_worker()
        self.tree_ready()
        (self.root / "worker-exit").touch()
        child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0)
        jobs.stop_child(child)
        self.assert_tree_stopped()

    def test_owner_crash_closes_the_only_job_handle_and_kills_the_tree(self):
        driver = self.root / "owner.py"
        driver.write_text("""import os, subprocess, sys, time
from pathlib import Path
from workbench.processes import start_windows_child
folder = Path(sys.argv[1])
child = start_windows_child([sys.executable, '-u', str(folder / 'tree.py'), 'worker', str(folder)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 60
while not (folder / 'owner-exit').exists() and time.monotonic() < deadline:
    time.sleep(0.01)
os._exit(33)
""", encoding="utf-8")
        owner = subprocess.Popen([sys.executable, "-u", str(driver), str(self.root)],
                                 cwd=Path(__file__).resolve().parents[1],
                                 env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        self.addCleanup(owner.wait, timeout=10)
        self.addCleanup(lambda: owner.kill() if owner.poll() is None else None)
        self.tree_ready()
        (self.root / "owner-exit").touch()
        self.assertEqual(owner.wait(timeout=10), 33)
        self.assert_tree_stopped()


if __name__ == "__main__":
    unittest.main()
