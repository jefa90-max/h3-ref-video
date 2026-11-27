"""Stage 3 tests: cross-platform Colab runtime controller (h3ref/colab_runtime.py)."""
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from h3ref import colab_runtime


class ColabRuntimeTest(unittest.TestCase):
    def _make_pipe_proc(self, before_lines, after_lines=(), *, wait_for_stdin=False, returncode=0):
        stdin_event = threading.Event()
        killed_event = threading.Event()
        stdin_writes = []
        r_fd, w_fd = os.pipe()
        stdout_file = os.fdopen(r_fd, "r", encoding="utf-8", errors="replace")
        writer_file = os.fdopen(w_fd, "w", encoding="utf-8")

        class FakeStdin:
            def write(self, data):
                stdin_writes.append(data)
                stdin_event.set()

            def flush(self):
                pass

            def close(self):
                stdin_event.set()

        proc = MagicMock()
        proc.pid = 54321
        proc.stdout = stdout_file
        proc.stdin = FakeStdin()
        proc.stdin_writes = stdin_writes
        proc.killed_event = killed_event
        state = {"rc": None}

        def feed():
            try:
                for line in before_lines:
                    writer_file.write(line)
                    writer_file.flush()
                if wait_for_stdin:
                    while not stdin_event.is_set() and not killed_event.is_set():
                        time.sleep(0.01)
                if not killed_event.is_set():
                    for line in after_lines:
                        writer_file.write(line)
                        writer_file.flush()
                    state["rc"] = returncode
                else:
                    state["rc"] = -9
            finally:
                try:
                    writer_file.close()
                except OSError:
                    pass

        def poll():
            return state["rc"]

        def wait(timeout=None):
            deadline = None if timeout is None else time.monotonic() + timeout
            while state["rc"] is None:
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("colab", timeout)
                time.sleep(0.01)
            return state["rc"]

        def kill():
            killed_event.set()
            state["rc"] = -9

        proc.poll.side_effect = poll
        proc.wait.side_effect = wait
        proc.terminate.side_effect = kill
        proc.kill.side_effect = kill
        threading.Thread(target=feed, daemon=True).start()
        return proc

    def test_online_short_circuits_without_invoking_colab_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = colab_runtime.ColabRuntimeManager(Path(tmp))
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": "h3c_ok",
            }
            with patch.object(colab_runtime, "find_colab_bin", return_value=None), \
                 patch.object(colab_runtime, "probe_comfyui", return_value={"online": True, "devices": [{"name": "A100"}]}) as probe, \
                 patch("subprocess.Popen", side_effect=AssertionError("CLI must not run when already online!")):
                st = mgr.status(env)
                self.assertEqual(st["state"], "online")
                self.assertFalse(st["cli_available"])
                started = mgr.start_async(env)
                self.assertEqual(started["state"], "online")
                self.assertGreaterEqual(probe.call_count, 1)

    def test_cli_unavailable_degrades_gracefully_and_windows_paths_are_probed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgr = colab_runtime.ColabRuntimeManager(root / ".workbench")
            env = {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "https://demo.ngrok-free.dev"}
            with patch.object(colab_runtime, "find_colab_bin", return_value=None), \
                 patch.object(colab_runtime, "probe_comfyui", return_value={"online": False}):
                st = mgr.status(env)
            self.assertEqual(st["state"], "cli_unavailable")
            self.assertFalse(st["cli_available"])

            # Verify Windows candidate path discovery (.venv\Scripts\colab.exe & APPDATA)
            appdata = root / "AppData" / "Roaming"
            win_bin = appdata / "Python" / "Scripts" / "colab.exe"
            win_bin.parent.mkdir(parents=True)
            win_bin.write_bytes(b"MZ")
            with patch.object(colab_runtime, "os", SimpleNamespace(name="nt", environ={"APPDATA": str(appdata), "USERPROFILE": str(root)})), \
                 patch.object(colab_runtime.config, "ROOT", root), \
                 patch("shutil.which", return_value=None):
                found = colab_runtime.find_colab_bin({"APPDATA": str(appdata), "USERPROFILE": str(root)})
            self.assertEqual(found, str(win_bin))

    def test_posix_and_windows_popen_flags_utf8_env_and_global_auth_flag_order(self):
        posix_kw = colab_runtime._popen_kwargs({"PATH": "/usr/bin"}, os_name="posix")
        self.assertTrue(posix_kw["start_new_session"])
        self.assertNotIn("creationflags", posix_kw)
        self.assertEqual(posix_kw["env"]["PYTHONUTF8"], "1")
        self.assertEqual(posix_kw["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(posix_kw["env"]["PYTHONUNBUFFERED"], "1")

        nt_kw = colab_runtime._popen_kwargs({"PATH": "C:\\Windows"}, os_name="nt")
        self.assertNotIn("start_new_session", nt_kw)
        self.assertEqual(nt_kw["creationflags"], 0x08000000)
        self.assertEqual(nt_kw["env"]["PYTHONUTF8"], "1")

        argv = colab_runtime._build_argv("/bin/colab", "adc", ["sessions"])
        self.assertEqual(argv, ["/bin/colab", "--auth", "adc", "sessions"])

    def test_full_startup_pauses_for_drive_auth_retries_120s_mount_timeout_and_passes_zero_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            mgr = colab_runtime.ColabRuntimeManager(Path(tmp))
            secret = "h3c_never_in_cli_history_secret"
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": secret,
                "COLAB_SESSION": "comfy-a100",
                "COLAB_BIN": str(fake_bin),
            }
            calls = []
            online_flag = {"up": False}

            def fake_probe(e=None):
                return {"online": online_flag["up"], "devices": [{"name": "A100"}] if online_flag["up"] else []}

            def fake_popen(argv, **kwargs):
                calls.append((list(argv), kwargs))
                sub = argv[-1] if argv[-1] == "sessions" else argv[1]
                if "sessions" in argv:
                    return self._make_pipe_proc(["[colab] No active sessions found on server.\n"])
                if "new" in argv:
                    return self._make_pipe_proc(["[colab] Creating session 'comfy-a100'...\n", "[colab] Session READY.\n"])
                if "drivemount" in argv:
                    mount_count = sum(1 for a, _ in calls if "drivemount" in a)
                    if mount_count == 1:
                        return self._make_pipe_proc(
                            [
                                "[colab] REQUIRED: Google Drive Authorization needed.\n",
                                "Please visit:\n\n",
                                "https://accounts.google.com/o/oauth2/v2/auth?client_id=test123\n\n",
                                "Press Enter after you have granted access... ",
                            ],
                            [
                                "\n[colab] Credentials propagated. Resuming mount...\n",
                                "ValueError: mount failed\n",
                            ],
                            wait_for_stdin=True,
                        )
                    return self._make_pipe_proc(["Mounted at /content/drive\n"])
                if "upload" in argv:
                    return self._make_pipe_proc(["[colab] Uploaded 'start_colab.py' to 'content/drive/MyDrive/MiniMax_ComfyUI/start_colab.py'\n"])
                if "exec" in argv:
                    online_flag["up"] = True
                    return self._make_pipe_proc(["[start_colab] TUNNEL READY (AUTH PROTECTED): https://demo.ngrok-free.dev\n"])
                raise AssertionError(f"Unexpected argv: {argv}")

            with patch.object(colab_runtime, "probe_comfyui", side_effect=fake_probe), \
                 patch("subprocess.Popen", side_effect=fake_popen):
                initial = mgr.start_async(env)
                self.assertIn(initial["state"], ("provisioning", "awaiting_drive_auth", "starting_comfy"))

                # Wait until awaiting_drive_auth is reached
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    cur = mgr.status(env)
                    if cur["state"] == "awaiting_drive_auth":
                        break
                    time.sleep(0.02)
                self.assertEqual(cur["state"], "awaiting_drive_auth")
                self.assertEqual(cur["auth_url"], "https://accounts.google.com/o/oauth2/v2/auth?client_id=test123")

                ack = mgr.submit_drive_authorized()
                self.assertTrue(ack["accepted"])

                # Wait until online
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    cur = mgr.status(env)
                    if cur["state"] == "online":
                        break
                    time.sleep(0.02)
                self.assertEqual(cur["state"], "online")
                if mgr._worker_thread is not None:
                    mgr._worker_thread.join(timeout=5.0)

            # Verify default GPU G4 omits --high-mem, start_colab.py is synced to Drive before exec, drivemount retried once, and NO secret leaked
            new_calls = [a for a, _ in calls if "new" in a]
            self.assertEqual(len(new_calls), 1)
            self.assertEqual(new_calls[0][1:], ["new", "-s", "comfy-a100", "--gpu", "G4"])
            self.assertNotIn("--high-mem", new_calls[0])
            self.assertEqual(cur["gpu"], "G4")
            upload_calls = [a for a, _ in calls if "upload" in a]
            self.assertEqual(len(upload_calls), 1)
            self.assertTrue(upload_calls[0][-2].endswith("start_colab.py"))
            self.assertEqual(upload_calls[0][-1], "content/drive/MyDrive/MiniMax_ComfyUI/start_colab.py")
            mount_calls = [a for a, _ in calls if "drivemount" in a]
            self.assertEqual(len(mount_calls), 2)
            for argv, kwargs in calls:
                self.assertFalse(any(secret in str(x) for x in argv))

    def test_colab_gpu_selection_builds_expected_new_session_args(self):
        self.assertEqual(
            colab_runtime._new_session_args("comfy-a100", "G4"),
            ["new", "-s", "comfy-a100", "--gpu", "G4"],
        )
        self.assertEqual(
            colab_runtime._new_session_args("comfy-a100", "H100"),
            ["new", "-s", "comfy-a100", "--gpu", "H100"],
        )
        self.assertEqual(
            colab_runtime._new_session_args("comfy-a100", "A100"),
            ["new", "-s", "comfy-a100", "--gpu", "A100", "--high-mem"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            mgr = colab_runtime.ColabRuntimeManager(Path(tmp))
            for gpu, want_high_mem in (("A100", True), ("H100", False)):
                env = {
                    "VIDEO_PROVIDER": "comfyui",
                    "COMFYUI_URL": "https://demo.ngrok-free.dev",
                    "COLAB_GPU": gpu,
                    "COLAB_BIN": str(fake_bin),
                }
                new_calls = []
                online = {"up": False}

                def fake_popen(argv, **kw):
                    if "sessions" in argv:
                        return self._make_pipe_proc(["[colab] No active sessions found on server.\n"])
                    if "new" in argv:
                        new_calls.append(list(argv))
                        return self._make_pipe_proc(["[colab] Session READY.\n"])
                    if "drivemount" in argv:
                        return self._make_pipe_proc(["Mounted at /content/drive\n"])
                    if "upload" in argv:
                        return self._make_pipe_proc(["Uploaded\n"])
                    if "exec" in argv:
                        online["up"] = True
                        return self._make_pipe_proc(["TUNNEL READY\n"])
                    raise AssertionError(argv)

                with patch.object(colab_runtime, "probe_comfyui", side_effect=lambda *a, **kw: {"online": online["up"], "devices": [{"name": gpu}] if online["up"] else []}), \
                     patch("subprocess.Popen", side_effect=fake_popen):
                    st = mgr.ensure_started(env, timeout=5.0)
                    if mgr._worker_thread is not None:
                        mgr._worker_thread.join(timeout=5.0)
                self.assertEqual(st["gpu"], gpu)
                self.assertEqual(len(new_calls), 1)
                self.assertIn("--gpu", new_calls[0])
                self.assertEqual(new_calls[0][new_calls[0].index("--gpu") + 1], gpu)
                self.assertEqual("--high-mem" in new_calls[0], want_high_mem)

    def test_drive_auth_timeout_recovers_to_error_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            mgr = colab_runtime.ColabRuntimeManager(Path(tmp), drive_auth_timeout=0.15)
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COLAB_BIN": str(fake_bin),
            }
            mount_proc = None

            def fake_popen(argv, **kwargs):
                nonlocal mount_proc
                if "sessions" in argv:
                    return self._make_pipe_proc(["[comfy-a100] m-s-1 | Hardware: A100 | Shape: High-RAM | Variant: GPU\n"])
                if "ls" in argv:
                    return self._make_pipe_proc([""], returncode=1)
                if "drivemount" in argv:
                    mount_proc = self._make_pipe_proc(
                        [
                            "[colab] REQUIRED: Google Drive Authorization needed.\n",
                            "https://accounts.google.com/o/oauth2/v2/auth?client_id=timeout\n",
                            "Press Enter after you have granted access... ",
                        ],
                        wait_for_stdin=True,
                    )
                    return mount_proc
                raise AssertionError(f"Unexpected argv: {argv}")

            with patch.object(colab_runtime, "probe_comfyui", return_value={"online": False}), \
                 patch("subprocess.Popen", side_effect=fake_popen), \
                 patch.object(colab_runtime, "_kill_process_tree", side_effect=lambda p: p.kill()):
                mgr.start_async(env)
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    st = mgr.status(env)
                    if st["state"] == "error":
                        break
                    time.sleep(0.03)
                self.assertEqual(st["state"], "error")
                if mgr._worker_thread is not None:
                    mgr._worker_thread.join(timeout=5.0)
                self.assertIsNotNone(mount_proc)
                self.assertTrue(mount_proc.killed_event.is_set())

    def test_orphan_session_warning_lock_protection_worker_guard_and_inflight_stop_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            storage = Path(tmp) / ".workbench"
            mgr = colab_runtime.ColabRuntimeManager(storage)
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": "h3c_key",
                "COLAB_BIN": str(fake_bin),
            }

            # 1. Orphan [?] session detected in status()
            with patch.object(colab_runtime, "probe_comfyui", return_value={"online": False}), \
                 patch("subprocess.Popen", side_effect=lambda argv, **kw: self._make_pipe_proc(
                     ["[?] m-s-orphan1 | Hardware: A100 | Shape: High-RAM | Variant: GPU\n"]
                 )):
                st = mgr.status(env)
            self.assertEqual(st["state"], "orphan_warning")
            self.assertEqual(len(st["orphan_sessions"]), 1)

            # 2. H3_WORKBENCH_WORKER=1 never spawns colab subprocesses
            with patch.dict(os.environ, {"H3_WORKBENCH_WORKER": "1"}, clear=False), \
                 patch.object(colab_runtime, "probe_comfyui", return_value={"online": False}), \
                 patch("subprocess.Popen", side_effect=AssertionError("Worker must not spawn colab!")):
                with self.assertRaises(colab_runtime.RuntimeUnavailableError):
                    mgr.start_async(env)

            # 3. Cross-process file lock blocks concurrent A100 provisioning
            mgr2 = colab_runtime.ColabRuntimeManager(storage)
            with colab_runtime.runtime_file_lock(storage):
                with patch.object(colab_runtime, "probe_comfyui", return_value={"online": False}):
                    with self.assertRaises(colab_runtime.RuntimeBusyError):
                        mgr2.start_async(env)

            # 4. In-flight stop guard requires force=True when local_busy or remote queue non-empty
            stop_called = {"count": 0}

            def fake_stop_popen(argv, **kw):
                if "stop" in argv:
                    stop_called["count"] += 1
                    return self._make_pipe_proc(["[colab] Session terminated.\n"])
                return self._make_pipe_proc(["[colab] No active sessions found on server.\n"])

            with patch.object(colab_runtime, "probe_remote_queue_count", return_value=2), \
                 patch.object(colab_runtime, "probe_comfyui", return_value={"online": True, "configured": True, "devices": [], "nodes": [], "online_count": 1, "total_count": 1}), \
                 patch("subprocess.Popen", side_effect=fake_stop_popen):
                blocked = mgr.stop(environ=env, force=False, local_busy=True)
                self.assertFalse(blocked["stopped"])
                self.assertTrue(blocked["confirmation_required"])
                self.assertEqual(blocked["remote_queue"], 2)
                self.assertEqual(stop_called["count"], 0)

                forced = mgr.stop(environ=env, force=True, local_busy=True)
                self.assertTrue(forced["stopped"])
                self.assertEqual(stop_called["count"], 1)

    def test_exec_connection_lost_hung_cli_is_reaped_retried_and_remote_boot_is_single_instance(self):
        # 1. REQUEST_TIMEOUT defaults to >=90s so jupyter_kernel_client WebSocket proxy handshakes do not abort at 10s
        default_kw = colab_runtime._popen_kwargs({"PATH": "/usr/bin"}, os_name="posix")
        self.assertEqual(default_kw["env"]["REQUEST_TIMEOUT"], "90")
        custom_kw = colab_runtime._popen_kwargs({"PATH": "/usr/bin", "REQUEST_TIMEOUT": "120"}, os_name="posix")
        self.assertEqual(custom_kw["env"]["REQUEST_TIMEOUT"], "120")

        # 2. Hung `colab exec` emitting Rich `RuntimeError: Connection was lost.` (non-daemon WS thread leak) is reaped and retried
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            mgr = colab_runtime.ColabRuntimeManager(Path(tmp), crash_grace_sec=0.05, retry_delay_sec=0.02)
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": "h3c_key",
                "COLAB_SESSION": "comfy-a100",
                "COLAB_BIN": str(fake_bin),
            }
            exec_procs = []
            online = {"up": False}

            class HungCrashedExecProc:
                def __init__(self):
                    self.pid = 80904
                    self.killed_event = threading.Event()
                    r_fd, w_fd = os.pipe()
                    self.stdout = os.fdopen(r_fd, "r", encoding="utf-8", errors="replace")
                    self._w_fd = w_fd
                    self.stdin = MagicMock()
                    threading.Thread(target=self._emit_crash_then_hang, daemon=True).start()

                def _emit_crash_then_hang(self):
                    os.write(
                        self._w_fd,
                        (
                            "╭───────────────────── Traceback (most recent call last) ──────────────────────╮\n"
                            "│   304 │   │   │   │   if not client.connection_ready.is_set():               │\n"
                            "│ ❱ 305 │   │   │   │   │   raise RuntimeError(\"Connection was lost.\")         │\n"
                            "╰──────────────────────────────────────────────────────────────────────────────╯\n"
                            "RuntimeError: Connection was lost.\n"
                        ).encode("utf-8"),
                    )
                    # Deliberately keep _w_fd open and never exit until killed, reproducing leaked KernelWebSocketClient thread
                    self.killed_event.wait(timeout=10.0)
                    try:
                        os.close(self._w_fd)
                    except OSError:
                        pass

                def poll(self):
                    return -9 if self.killed_event.is_set() else None

                def wait(self, timeout=None):
                    if not self.killed_event.wait(timeout=timeout):
                        raise subprocess.TimeoutExpired("colab", timeout)
                    return -9

                def kill(self):
                    self.killed_event.set()

            def fake_popen(argv, **kwargs):
                if "sessions" in argv:
                    return self._make_pipe_proc(["[comfy-a100] m-s-1 | Hardware: A100 | Shape: High-RAM | Variant: GPU\n"])
                if "ls" in argv:
                    return self._make_pipe_proc(["MyDrive/\n"])
                if "upload" in argv:
                    return self._make_pipe_proc(["[colab] Uploaded\n"])
                if "exec" in argv:
                    if not exec_procs:
                        hung = HungCrashedExecProc()
                        exec_procs.append(hung)
                        return hung
                    ok_proc = self._make_pipe_proc(["[start_colab] TUNNEL READY (AUTH PROTECTED): https://demo.ngrok-free.dev\n"])
                    exec_procs.append(ok_proc)
                    online["up"] = True
                    return ok_proc
                raise AssertionError(f"Unexpected argv: {argv}")

            with patch.object(colab_runtime, "probe_comfyui", side_effect=lambda *a, **kw: {"online": online["up"], "devices": [{"name": "A100"}] if online["up"] else []}), \
                 patch("subprocess.Popen", side_effect=fake_popen), \
                 patch.object(colab_runtime, "_kill_process_tree", side_effect=lambda p: p.kill()):
                st = mgr.ensure_started(env, timeout=5.0)
                if mgr._worker_thread is not None:
                    mgr._worker_thread.join(timeout=5.0)

            self.assertEqual(st["state"], "online")
            self.assertEqual(len(exec_procs), 2)
            self.assertTrue(exec_procs[0].killed_event.is_set(), "Hung crashed colab exec must be killed instead of blocking 2400s")

        # 3. Real execution of REMOTE_START_CODE: mid-flight client disconnect re-attaches without spawning a duplicate start_colab.py
        if os.name != "nt":
            import sys
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                count_file = tmp_path / "invocations.txt"
                started_file = tmp_path / "started.flag"
                fake_start = tmp_path / "start_colab.py"
                fake_start.write_text(
                    "import pathlib, time\n"
                    f"c = pathlib.Path({str(count_file)!r})\n"
                    "n = int(c.read_text()) + 1 if c.exists() else 1\n"
                    "c.write_text(str(n))\n"
                    f"pathlib.Path({str(started_file)!r}).write_text('1')\n"
                    "print('[start_colab] Copying models...', flush=True)\n"
                    "time.sleep(0.4)\n"
                    "print('[start_colab] TUNNEL READY (AUTH PROTECTED): https://demo.ngrok-free.dev', flush=True)\n",
                    encoding="utf-8",
                )
                boot_env = {
                    **os.environ,
                    "H3_BOOT_TMP": str(tmp_path),
                    "H3_START_COLAB_SCRIPT": str(fake_start),
                }
                client1 = subprocess.Popen(
                    [sys.executable, "-u", "-c", colab_runtime.REMOTE_START_CODE],
                    env=boot_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and not started_file.exists():
                    time.sleep(0.02)
                self.assertTrue(started_file.exists())
                # Simulate WebSocket / kernel cell disconnect killing client1 while start_colab.py continues in detached session
                client1.kill()
                client1.wait(timeout=2.0)
                if client1.stdout:
                    client1.stdout.close()

                client2 = subprocess.run(
                    [sys.executable, "-u", "-c", colab_runtime.REMOTE_START_CODE],
                    env=boot_env,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                self.assertEqual(client2.returncode, 0, client2.stdout + client2.stderr)
                self.assertIn("TUNNEL READY (AUTH PROTECTED)", client2.stdout)
                self.assertEqual(count_file.read_text(encoding="utf-8").strip(), "1")

    def test_kernel_keepalive_only_fires_when_queue_busy_after_25m_and_respects_locks_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            storage = root / ".workbench"
            clock_state = {"now": 1000.0}
            mgr = colab_runtime.ColabRuntimeManager(
                storage,
                clock=lambda: clock_state["now"],
            )
            self.assertEqual(mgr.keepalive_interval_sec, 480.0)
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": "h3c_keepalive_key",
                "COLAB_SESSION": "comfy-a100",
                "COLAB_BIN": str(fake_bin),
            }
            calls = []

            def fake_run_cmd(colab_bin, auth_mode, sub_args, **kwargs):
                calls.append((colab_bin, auth_mode, list(sub_args), kwargs.get("stdin_text"), kwargs.get("timeout")))
                return 0, "keepalive\n"

            with patch.object(mgr, "_run_command", side_effect=fake_run_cmd):
                # 1. Under 8 min -> never sends keepalive even when queue is busy
                clock_state["now"] = 1000.0 + 479.0
                with patch.object(colab_runtime, "probe_remote_queue_count", return_value=1):
                    self.assertFalse(mgr.maybe_send_kernel_keepalive(env))
                self.assertEqual(calls, [])

                # 2. >= 8 min but queue is empty -> MUST NOT send keepalive (avoid burning compute units when idle)
                clock_state["now"] = 1000.0 + 480.0
                with patch.object(colab_runtime, "probe_remote_queue_count", return_value=0):
                    self.assertFalse(mgr.maybe_send_kernel_keepalive(env))
                self.assertEqual(calls, [])

                # 3. >= 8 min and queue is busy -> sends lightweight colab exec --timeout 30 heartbeat
                with patch.object(colab_runtime, "probe_remote_queue_count", return_value=2):
                    self.assertTrue(mgr.maybe_send_kernel_keepalive(env))
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][2], ["exec", "-s", "comfy-a100", "--timeout", "30"])
                self.assertIn("keepalive", calls[0][3])

                # 4. Immediately afterwards while still busy -> waits another 8 min
                clock_state["now"] += 300.0
                with patch.object(colab_runtime, "probe_remote_queue_count", return_value=1):
                    self.assertFalse(mgr.maybe_send_kernel_keepalive(env))
                self.assertEqual(len(calls), 1)

                # 5. Once idle grace window expires with empty queue -> stops keepalive even while ComfyUI is online
                clock_state["now"] += mgr.keepalive_idle_grace_sec
                with patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                     patch.object(colab_runtime, "probe_comfyui", return_value={"online": True, "configured": True, "devices": [], "nodes": [], "online_count": 1, "total_count": 1}):
                    self.assertFalse(mgr.maybe_send_kernel_keepalive(env))
                self.assertEqual(len(calls), 1)

                # 5b. Launching a new video job after >= 8 min idle immediately refreshes kernel via force_busy=True
                with patch.object(colab_runtime, "probe_remote_queue_count", side_effect=AssertionError("force_busy skips /queue probe")):
                    self.assertTrue(mgr.maybe_send_kernel_keepalive(env, force_busy=True))
                self.assertEqual(len(calls), 2)
                clock_state["now"] += 480.0

                # 6. Concurrency / lock guards: active startup/stop state, active subprocess, file lock, worker env
                with patch.object(colab_runtime, "probe_remote_queue_count", return_value=1):
                    mgr._active_state = "starting_comfy"
                    self.assertFalse(mgr.maybe_send_kernel_keepalive(env))
                    mgr._active_state = None

                    mgr._active_proc = object()
                    self.assertFalse(mgr.maybe_send_kernel_keepalive(env))
                    mgr._active_proc = None

                    with colab_runtime.runtime_file_lock(storage):
                        self.assertFalse(mgr.maybe_send_kernel_keepalive(env))

                    with patch.dict(os.environ, {"H3_WORKBENCH_WORKER": "1"}, clear=False):
                        self.assertFalse(mgr.maybe_send_kernel_keepalive(env))
                self.assertEqual(len(calls), 2)

            # 7. Heartbeat failure never raises or interrupts tasks; logs error and backs off during cooldown
            with patch.object(colab_runtime, "probe_remote_queue_count", return_value=1), \
                 patch.object(mgr, "_run_command", return_value=(1, "WebSocket timeout")) as fail_cmd:
                self.assertFalse(mgr.maybe_send_kernel_keepalive(env))
                self.assertEqual(fail_cmd.call_count, 1)
                self.assertTrue(any("keepalive" in line.lower() and "failed" in line.lower() for line in mgr._logs_tail()))
                # Within cooldown (60s), does not hammer failed keepalive
                clock_state["now"] += 30.0
                self.assertFalse(mgr.maybe_send_kernel_keepalive(env))
                self.assertEqual(fail_cmd.call_count, 1)

            # 8. Background keepalive monitor thread triggers keepalive when queue is busy and stops cleanly
            fired = threading.Event()

            def fake_monitor_run(colab_bin, auth_mode, sub_args, **kwargs):
                fired.set()
                return 0, "keepalive\n"

            clock_state["now"] += 2000.0
            with patch.object(colab_runtime, "probe_remote_queue_count", return_value=1), \
                 patch.object(mgr, "_run_command", side_effect=fake_monitor_run):
                mgr.start_keepalive_monitor(lambda: env, poll_sec=0.05)
                try:
                    self.assertTrue(fired.wait(timeout=2.0), "Keepalive monitor thread should fire when queue is busy")
                finally:
                    mgr.stop_keepalive_monitor()
            self.assertIsNone(mgr._keepalive_thread)

    def test_kernel_keepalive_keeps_warm_during_local_work_and_grace_window_then_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            storage = root / ".workbench"
            clock_state = {"now": 1000.0}
            mgr = colab_runtime.ColabRuntimeManager(
                storage,
                clock=lambda: clock_state["now"],
            )
            self.assertEqual(mgr.keepalive_idle_grace_sec, 45 * 60.0)
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": "h3c_keepalive_key",
                "COLAB_SESSION": "comfy-a100",
                "COLAB_BIN": str(fake_bin),
            }
            calls = []

            def fake_run_cmd(colab_bin, auth_mode, sub_args, **kwargs):
                calls.append(list(sub_args))
                return 0, "keepalive\n"

            with patch.object(mgr, "_run_command", side_effect=fake_run_cmd), \
                 patch.object(colab_runtime, "probe_remote_queue_count", return_value=0):
                # 1. Local storyboard/frame job running while ComfyUI queue=0 and ComfyUI online -> sends keepalive
                clock_state["now"] = 1000.0 + 480.0
                with patch.object(colab_runtime, "probe_comfyui", return_value={"online": True}):
                    self.assertTrue(mgr.maybe_send_kernel_keepalive(env, local_busy=True))
                self.assertEqual(len(calls), 1)
                self.assertIn("local_active", mgr._logs_tail()[-1])

                # 2. Local job finishes; user reviews frames for 16 min (within 45 min grace) -> still keeps warm
                clock_state["now"] += 960.0
                with patch.object(colab_runtime, "probe_comfyui", return_value={"online": True}):
                    self.assertTrue(mgr.maybe_send_kernel_keepalive(env, local_busy=False))
                self.assertEqual(len(calls), 2)
                self.assertIn("idle_grace", mgr._logs_tail()[-1])

                # 3. If ComfyUI is actually offline during grace window -> never calls colab exec
                clock_state["now"] += 480.0
                with patch.object(colab_runtime, "probe_comfyui", return_value={"online": False}):
                    self.assertFalse(mgr.maybe_send_kernel_keepalive(env, local_busy=False))
                self.assertEqual(len(calls), 2)

                # 4. Once 45 min passes since last real activity (idle_grace does not reset activity timer) -> stops!
                clock_state["now"] = 1000.0 + 480.0 + 45 * 60.0
                with patch.object(colab_runtime, "probe_comfyui", return_value={"online": True}) as probe_mock:
                    self.assertFalse(mgr.maybe_send_kernel_keepalive(env, local_busy=False))
                probe_mock.assert_not_called()
                self.assertEqual(len(calls), 2)

                # 5. User edits a frame -> mark_local_activity refreshes the grace window
                mgr.mark_local_activity()
                clock_state["now"] += 480.0
                with patch.object(colab_runtime, "probe_comfyui", return_value={"online": True}):
                    self.assertTrue(mgr.maybe_send_kernel_keepalive(env, local_busy=False))
                self.assertEqual(len(calls), 3)

            # 6. Session lost (404/401) during keepalive clears stale sessions cache and activity grace
            mgr._sessions_cache = (clock_state["now"], {"named": [{"name": "comfy-a100"}], "orphans": []})
            clock_state["now"] += 480.0
            with patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                 patch.object(colab_runtime, "probe_comfyui", return_value={"online": True}), \
                 patch.object(
                     mgr,
                     "_run_command",
                     return_value=(1, "[colab] Session 'comfy-a100' appears to be lost (404/401). Cleaning up.\n"),
                 ):
                self.assertFalse(mgr.maybe_send_kernel_keepalive(env, local_busy=False))
            self.assertIsNone(mgr._sessions_cache)
            self.assertIsNone(mgr._last_activity_at)

            # 7. Background monitor passes local_busy_provider so frames/review stay warm with queue=0
            fired = threading.Event()

            def fake_monitor_run(colab_bin, auth_mode, sub_args, **kwargs):
                fired.set()
                return 0, "keepalive\n"

            clock_state["now"] += 1000.0
            with patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                 patch.object(colab_runtime, "probe_comfyui", return_value={"online": True}), \
                 patch.object(mgr, "_run_command", side_effect=fake_monitor_run):
                mgr.start_keepalive_monitor(lambda: env, poll_sec=0.05, local_busy_provider=lambda: True)
                try:
                    self.assertTrue(fired.wait(timeout=2.0), "Keepalive monitor should fire when local_busy_provider is True")
                finally:
                    mgr.stop_keepalive_monitor()

    def test_secondary_colab_node_cloudflared_ephemeral_url_lifecycle_keepalive_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            storage = root / ".workbench"
            clock_state = {"now": 5000.0}
            url_events: list[tuple[str, str | None, str | None]] = []
            mgr = colab_runtime.ColabRuntimeManager(
                storage,
                clock=lambda: clock_state["now"],
                on_ephemeral_url_change=lambda slot, old_u, new_u: url_events.append((slot, old_u, new_u)),
            )

            # 1. Missing COMFYUI_API_KEY fails fast for secondary public tunnel
            with self.assertRaises(ValueError):
                mgr.start_async(
                    {
                        "VIDEO_PROVIDER": "comfyui",
                        "COMFYUI_URL": "https://node1.ngrok-free.dev",
                        "COLAB_BIN": str(fake_bin),
                    },
                    slot="secondary",
                )

            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://node1.ngrok-free.dev",
                "COMFYUI_API_KEY": "shared-secret-key-999",
                "COLAB_SESSION": "comfy-a100",
                "COLAB_GPU": "G4",
                "COLAB_BIN": str(fake_bin),
            }
            sec_url_1 = "https://alpha-one.trycloudflare.com"
            sec_url_2 = "https://beta-two.trycloudflare.com"
            sec_online = {"url": None}
            exec_stdins: list[str] = []
            cmd_log: list[list[str]] = []

            def fake_probe(environ=None, *, timeout=12.0):
                urls = colab_runtime.config.video_settings(environ)["comfyui_urls"]
                nodes = []
                for u in urls:
                    is_up = (u == "https://node1.ngrok-free.dev") or (sec_online["url"] is not None and u == sec_online["url"])
                    nodes.append({
                        "url": u,
                        "label": u.removeprefix("https://"),
                        "online": is_up,
                        "auth_failed": False,
                        "devices": [{"name": "RTX PRO 6000"}] if is_up else [],
                        "queue_running": 0,
                        "queue_pending": 0,
                    })
                online_nodes = [n for n in nodes if n["online"]]
                return {
                    "online": bool(online_nodes),
                    "configured": bool(urls),
                    "devices": [d for n in online_nodes for d in n["devices"]],
                    "nodes": nodes,
                    "online_count": len(online_nodes),
                    "total_count": len(nodes),
                }

            current_random = {"url": sec_url_1}

            def fake_run_cmd(colab_bin, auth_mode, sub_args, **kwargs):
                cmd_log.append(list(sub_args))
                if sub_args == ["sessions"]:
                    lines = ["[comfy-a100] m-s-1 | Hardware: G4"]
                    if sec_online["url"]:
                        lines.append("[comfy-a100-2] m-s-2 | Hardware: G4")
                    return 0, "\n".join(lines) + "\n"
                if sub_args[:3] == ["new", "-s", "comfy-a100-2"]:
                    return 0, "Session READY\n"
                if sub_args[:2] == ["upload", "-s"]:
                    return 0, "Uploaded\n"
                if sub_args[:3] == ["exec", "-s", "comfy-a100-2"]:
                    stdin_text = kwargs.get("stdin_text") or ""
                    exec_stdins.append(stdin_text)
                    if "keepalive" in stdin_text:
                        return 0, "keepalive\n"
                    sec_online["url"] = current_random["url"]
                    return 0, f"[start_colab] TUNNEL READY (AUTH PROTECTED): {current_random['url']}\n"
                if sub_args == ["stop", "-s", "comfy-a100-2"]:
                    sec_online["url"] = None
                    return 0, "Stopped\n"
                return 0, ""

            with patch.object(colab_runtime, "probe_comfyui", side_effect=fake_probe), \
                 patch.object(mgr, "_run_command", side_effect=fake_run_cmd), \
                 patch.object(mgr, "_run_drivemount_once", return_value=(0, "Mounted at /content/drive\n", False)):
                # 2. Start secondary node even while primary is already online
                mgr.start_async(env, slot="secondary")
                worker = mgr._slots["secondary"].worker_thread
                self.assertIsNotNone(worker)
                worker.join(timeout=5.0)
                self.assertFalse(worker.is_alive())

                # Verify cloudflared tunnel provider injected & ephemeral URL remembered
                self.assertTrue(any("H3_TUNNEL_PROVIDER" in s and "cloudflared" in s for s in exec_stdins))
                saved = mgr.remembered_node("secondary")
                self.assertIsNotNone(saved)
                self.assertEqual(saved["url"], sec_url_1)
                self.assertEqual(saved["session"], "comfy-a100-2")
                self.assertEqual(url_events[-1], ("secondary", None, sec_url_1))

                # 3. Keepalive works independently for secondary session comfy-a100-2
                clock_state["now"] += 480.0
                with patch.object(colab_runtime, "probe_remote_queue_count", return_value=1):
                    self.assertTrue(mgr.maybe_send_kernel_keepalive(env, slot="secondary"))
                self.assertIn(["exec", "-s", "comfy-a100-2", "--timeout", "30"], cmd_log)

                # 4. Stop secondary node enforces queue check, then stops session and clears ephemeral URL
                with patch.object(colab_runtime, "probe_remote_queue_count", return_value=1):
                    need_confirm = mgr.stop(environ=env, force=False, local_busy=False, slot="secondary")
                self.assertFalse(need_confirm["stopped"])
                self.assertTrue(need_confirm["confirmation_required"])

                with patch.object(colab_runtime, "probe_remote_queue_count", return_value=0):
                    stopped = mgr.stop(environ=env, force=True, local_busy=False, slot="secondary")
                self.assertTrue(stopped["stopped"])
                self.assertIsNone(mgr.remembered_node("secondary"))
                self.assertEqual(url_events[-1], ("secondary", sec_url_1, None))

                # 5. Re-starting replaces any stale remembered URL cleanly, and dead URL auto-cleans on status()
                mgr._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": sec_url_1, "gpu": "G4", "tunnel": "cloudflared"})
                current_random["url"] = sec_url_2
                mgr.start_async(env, slot="secondary")
                mgr._slots["secondary"].worker_thread.join(timeout=5.0)
                self.assertEqual(mgr.remembered_node("secondary")["url"], sec_url_2)
                self.assertEqual(url_events[-1], ("secondary", sec_url_1, sec_url_2))

                # Simulate VM reclaimed externally -> status() probes offline, sees no comfy-a100-2 session, cleans up dead URL
                sec_online["url"] = None
                st = mgr.status({**env, "COMFYUI_URL": f"https://node1.ngrok-free.dev,{sec_url_2}"})
                self.assertIsNone(mgr.remembered_node("secondary"))
                self.assertNotIn(sec_url_2, st["comfyui_urls"])
                self.assertEqual(url_events[-1], ("secondary", sec_url_2, None))

    def test_provision_retries_5_times_on_503_fails_fast_on_400_and_isolates_query_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            storage = root / ".workbench"

            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://node1.ngrok-free.dev",
                "COMFYUI_API_KEY": "secret-k-123",
                "COLAB_SESSION": "comfy-a100",
                "COLAB_GPU": "G4",
                "COLAB_BIN": str(fake_bin),
            }

            # Verify defaults: 5 retries (6 attempts total)
            default_mgr = colab_runtime.ColabRuntimeManager(storage)
            self.assertEqual(default_mgr.provision_retries, 5)
            self.assertEqual(len(default_mgr.provision_retry_delays), 5)

            # 1. Transient 503 succeeds on 4th attempt (after 3 retries) with COLAB_CLI_ASSIGN_RETRIES=0
            mgr = colab_runtime.ColabRuntimeManager(storage, provision_retry_delays=(0.01, 0.01, 0.01, 0.01, 0.01))
            new_envs = []
            new_count = {"n": 0}
            online = {"up": False}
            query_record_flags = []

            def fake_run_retry_success(colab_bin, auth_mode, sub_args, **kwargs):
                if sub_args == ["sessions"]:
                    query_record_flags.append(kwargs.get("record_slot"))
                    return 0, "[colab] No active sessions found on server.\n"
                if sub_args[:2] == ["new", "-s"]:
                    new_count["n"] += 1
                    new_envs.append((kwargs.get("environ") or {}).get("COLAB_CLI_ASSIGN_RETRIES"))
                    if new_count["n"] <= 3:
                        return 1, "[colab] Creating session 'comfy-a100'...\nColabRequestError: 503 Service Unavailable\n"
                    return 0, "[colab] Creating session 'comfy-a100'...\n[colab] Session READY.\n"
                if sub_args[:2] == ["exec", "-s"]:
                    online["up"] = True
                    return 0, "[start_colab] TUNNEL READY (AUTH PROTECTED): https://node1.ngrok-free.dev\n"
                return 0, ""

            with patch.object(colab_runtime, "probe_comfyui", side_effect=lambda *a, **kw: {"online": online["up"], "devices": []}), \
                 patch.object(mgr, "_run_command", side_effect=fake_run_retry_success), \
                 patch.object(mgr, "_run_drivemount_once", return_value=(0, "Mounted at /content/drive\n", False)):
                mgr.start_async(env)
                mgr._worker_thread.join(timeout=5.0)
            self.assertEqual(new_count["n"], 4)
            self.assertEqual(new_envs, ["0", "0", "0", "0"])
            self.assertEqual(query_record_flags, [False])
            self.assertIsNone(mgr._last_error)
            self.assertTrue(any("503 Service Unavailable" in line for line in mgr._logs_tail()))

            # 2. Persistent 503 inside Rich traceback (whose source frame shows `except TooManyAssignmentsError:`)
            # MUST still be recognized as retryable 503 and exhaust all 6 attempts (1 initial + 5 retries)
            rich_503_output = (
                "[colab] Creating session 'comfy-a100'...\n"
                "╭───────────────────── Traceback (most recent call last) ──────────────────────╮\n"
                "│ /site-packages/colab_cli/commands/session.py:167 in new                      │\n"
                "│   167 │   res = state.client.assign(...)                                     │\n"
                "│   170 │   except TooManyAssignmentsError:                                    │\n"
                "╰──────────────────────────────────────────────────────────────────────────────╯\n"
                "ColabRequestError: Failed to issue request POST \n"
                "https://colab.research.google.com/tun/m/assign?variant=GPU&accelerator=G4: Service Unavailable\n"
            )
            self.assertTrue(colab_runtime._is_retryable_provision_failure(1, rich_503_output))
            mgr2 = colab_runtime.ColabRuntimeManager(storage, provision_retry_delays=(0.01,))
            attempts2 = {"n": 0}

            def fake_run_always_503(colab_bin, auth_mode, sub_args, **kwargs):
                if sub_args == ["sessions"]:
                    return 0, "[colab] No active sessions found on server.\n"
                if sub_args[:2] == ["new", "-s"]:
                    attempts2["n"] += 1
                    return 1, rich_503_output
                return 0, ""

            with patch.object(colab_runtime, "probe_comfyui", return_value={"online": False}), \
                 patch.object(mgr2, "_run_command", side_effect=fake_run_always_503):
                mgr2.start_async(env)
                mgr2._worker_thread.join(timeout=5.0)
            self.assertEqual(attempts2["n"], 6)
            self.assertIn("6", mgr2._last_error or "")
            self.assertIn("503 Service Unavailable", mgr2._last_error or "")

            # 3. Non-retryable 400 / 412 fails fast on attempt 1 without wasting 5 retries
            mgr3 = colab_runtime.ColabRuntimeManager(storage, provision_retry_delays=(0.01,))
            attempts3 = {"n": 0}

            def fake_run_400(colab_bin, auth_mode, sub_args, **kwargs):
                if sub_args == ["sessions"]:
                    return 0, "[colab] No active sessions found on server.\n"
                if sub_args[:2] == ["new", "-s"]:
                    attempts3["n"] += 1
                    return 1, "[colab] Backend rejected accelerator 'G4'.\n"
                return 0, ""

            with patch.object(colab_runtime, "probe_comfyui", return_value={"online": False}), \
                 patch.object(mgr3, "_run_command", side_effect=fake_run_400):
                mgr3.start_async(env)
                mgr3._worker_thread.join(timeout=5.0)
            self.assertEqual(attempts3["n"], 1)
            self.assertIn("Backend rejected accelerator", mgr3._last_error or "")

    def test_cli_auth_detection_and_cross_device_code_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / ".workbench"
            marker = root / "logged_in"
            fake_cli = root / "fake_colab.py"
            fake_cli.write_text(
                "import os, sys\n"
                "from pathlib import Path\n"
                "marker = Path(os.environ['FAKE_COLAB_MARKER'])\n"
                "if marker.exists():\n"
                "    print('[colab] No active sessions found on server.')\n"
                "    sys.exit(0)\n"
                "print('To authorize colab-cli, visit this URL in any browser:\\n', file=sys.stderr, flush=True)\n"
                "print('  https://accounts.google.com/o/oauth2/auth?client_id=test&token_usage=remote\\n', file=sys.stderr, flush=True)\n"
                "sys.stdout.write('Enter the authorization code: ')\n"
                "sys.stdout.flush()\n"
                "code = sys.stdin.readline().strip()\n"
                "if not code:\n"
                "    print('EOFError: EOF when reading a line', file=sys.stderr)\n"
                "    sys.exit(1)\n"
                "if code == 'valid-code-secret-999':\n"
                "    if os.environ.get('FAKE_KEYRING_WARN') == '1':\n"
                "        print('Warning: Could not save the OAuth token to the OS keyring (limit)', file=sys.stderr)\n"
                "    marker.write_text('ok', encoding='utf-8')\n"
                "    print('[colab] No active sessions found on server.')\n"
                "    sys.exit(0)\n"
                "print(f'invalid_grant for {code}', file=sys.stderr)\n"
                "sys.exit(1)\n",
                encoding="utf-8",
            )
            fake_bin = self._write_fake_colab_wrapper(root, fake_cli)

            mgr = colab_runtime.ColabRuntimeManager(storage, cli_auth_timeout=10.0)
            self.addCleanup(mgr.close)
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": "k-secret",
                "COLAB_BIN": str(fake_bin),
                "FAKE_COLAB_MARKER": str(marker),
                "H3_LANG": "en",
            }
            with patch.object(colab_runtime, "probe_comfyui", return_value={"online": False, "configured": True, "nodes": [], "online_count": 0, "total_count": 1}):
                st0 = mgr.status(env)
                self.assertEqual(st0["state"], "cli_auth_required")
                self.assertEqual(st0["cli_auth"]["state"], "required")

                # ADC mode rejects web OAuth code flow
                with self.assertRaises(ValueError):
                    mgr.start_cli_auth({**env, "COLAB_AUTH": "adc"})

                # Start OAuth flow -> returns URL without leaking secrets
                started = mgr.start_cli_auth(env)
                self.assertTrue(started["started"])
                self.assertFalse(started["already_authenticated"])
                self.assertEqual(
                    started["auth_url"],
                    "https://accounts.google.com/o/oauth2/auth?client_id=test&token_usage=remote",
                )
                self.assertEqual(started["status"]["state"], "awaiting_cli_auth")

                # Submitting wrong code fails cleanly and NEVER leaks the submitted code
                bad = mgr.submit_cli_auth_code("wrong-secret-code-123", env)
                self.assertFalse(bad["accepted"])
                self.assertFalse(bad["authenticated"])
                self.assertNotIn("wrong-secret-code-123", bad["error"] or "")

                # Restart, cancel cleanly
                started2 = mgr.start_cli_auth(env, restart=True)
                self.assertTrue(started2["started"])
                cancelled = mgr.cancel_cli_auth(env)
                self.assertTrue(cancelled["cancelled"])
                self.assertEqual(cancelled["status"]["state"], "cli_auth_required")

                # Start again and submit valid code with keyring warning check
                started3 = mgr.start_cli_auth({**env, "FAKE_KEYRING_WARN": "1"})
                self.assertTrue(started3["started"])
                ok = mgr.submit_cli_auth_code("valid-code-secret-999", {**env, "FAKE_KEYRING_WARN": "1"})
                self.assertTrue(ok["accepted"])
                self.assertTrue(ok["authenticated"])
                self.assertTrue(ok["keyring_warning"])
                self.assertEqual(ok["status"]["state"], "offline")

                # Starting again when already logged in reports already_authenticated
                again = mgr.start_cli_auth(env)
                self.assertFalse(again["started"])
                self.assertTrue(again["already_authenticated"])

    @staticmethod
    def _write_fake_colab_wrapper(root: Path, script_path: Path) -> Path:
        if os.name == "nt":
            wrapper = root / "colab.cmd"
            wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n', encoding="utf-8")
            return wrapper
        wrapper = root / "colab"
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable!r} {str(script_path)!r} \"$@\"\n", encoding="utf-8")
        wrapper.chmod(0o755)
        return wrapper

    def test_cli_auth_review_regressions_lock_secondary_node_concurrency_deadline_and_scrubbing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / ".workbench"
            marker = root / "logged_in"
            pids_file = root / "pids.txt"
            fake_cli = root / "fake_colab_edge.py"
            fake_cli.write_text(
                "import os, sys, time, urllib.parse\n"
                "from pathlib import Path\n"
                "with open(os.environ['PIDS_FILE'], 'a', encoding='utf-8') as fh:\n"
                "    fh.write(f'{os.getpid()}\\n')\n"
                "marker = Path(os.environ['FAKE_COLAB_MARKER'])\n"
                "if marker.exists():\n"
                "    print('[colab] No active sessions found on server.')\n"
                "    sys.exit(0)\n"
                "delay = float(os.environ.get('START_DELAY', '0'))\n"
                "if delay:\n"
                "    time.sleep(delay)\n"
                "print('To authorize colab-cli, visit:', file=sys.stderr, flush=True)\n"
                "print('  https://accounts.google.com/o/oauth2/auth?x=edge', file=sys.stderr, flush=True)\n"
                "sys.stdout.write('Enter the authorization code: ')\n"
                "sys.stdout.flush()\n"
                "code = sys.stdin.readline().strip()\n"
                "if not code:\n"
                "    sys.exit(1)\n"
                "verify_delay = float(os.environ.get('VERIFY_DELAY', '0'))\n"
                "if verify_delay:\n"
                "    time.sleep(verify_delay)\n"
                "if code == '4/0A-good-code':\n"
                "    marker.write_text('ok', encoding='utf-8')\n"
                "    print('[colab] No active sessions found on server.')\n"
                "    sys.exit(0)\n"
                "enc = urllib.parse.quote(code, safe='')\n"
                "print(f'HTTP error body code={enc} raw={code} access_token=ya29.secret_tok', file=sys.stderr)\n"
                "sys.exit(1)\n",
                encoding="utf-8",
            )
            fake_bin = self._write_fake_colab_wrapper(root, fake_cli)
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://primary.ngrok-free.dev,https://sec-ephem.trycloudflare.com",
                "COMFYUI_API_KEY": "k-secret",
                "COLAB_BIN": str(fake_bin),
                "FAKE_COLAB_MARKER": str(marker),
                "PIDS_FILE": str(pids_file),
                "H3_LANG": "en",
            }
            mgr = colab_runtime.ColabRuntimeManager(storage, cli_auth_timeout=10.0)
            self.addCleanup(mgr.close)
            mgr._set_remembered_node(
                "secondary",
                {"url": "https://sec-ephem.trycloudflare.com", "session": "h3-comfy-2"},
            )
            removed_urls = []
            mgr.set_url_change_callback(lambda slot, old, new: removed_urls.append((slot, old, new)))

            offline_probe = {
                "online": False, "configured": True, "devices": [], "nodes": [],
                "online_count": 0, "total_count": 2,
            }

            # 1) Active CLI auth status() must NOT wipe remembered secondary node when sessions query is skipped
            with patch.object(colab_runtime, "probe_comfyui", return_value=offline_probe):
                mgr.start_cli_auth(env)
                st = mgr.status(env)
            self.assertEqual(st["state"], "awaiting_cli_auth")
            self.assertEqual((mgr.remembered_node("secondary") or {}).get("url"), "https://sec-ephem.trycloudflare.com")
            self.assertEqual(removed_urls, [])

            # 2) Reusing active awaiting_cli_auth via start_cli_auth(restart=False) must release self._lock before probe_comfyui
            lock_free = []
            def probe_checks_lock(*_a, **_kw):
                def check():
                    got = mgr._lock.acquire(blocking=False)
                    if got:
                        mgr._lock.release()
                    lock_free.append(got)
                t = threading.Thread(target=check)
                t.start()
                t.join(2.0)
                return offline_probe

            with patch.object(colab_runtime, "probe_comfyui", side_effect=probe_checks_lock):
                reused = mgr.start_cli_auth(env, restart=False)
            self.assertTrue(reused["started"])
            self.assertEqual(lock_free, [True])

            # 3) Generic failure echoes URL-encoded %2F code and OAuth tokens -> both must be scrubbed
            bad = mgr.submit_cli_auth_code("4/0A-secret-code", env)
            self.assertFalse(bad["authenticated"])
            self.assertNotIn("4/0A-secret-code", bad["error"] or "")
            self.assertNotIn("4%2F0A-secret-code", bad["error"] or "")
            self.assertNotIn("ya29.secret_tok", bad["error"] or "")

            # 4) Concurrent start_cli_auth() must serialize without leaking orphan subprocesses or returning auth_url=None
            pids_file.write_text("", encoding="utf-8")
            slow_env = {**env, "START_DELAY": "0.15"}
            barrier = threading.Barrier(2)
            concurrent_results = []
            def worker():
                barrier.wait()
                try:
                    concurrent_results.append(("ok", mgr.start_cli_auth(slow_env)))
                except Exception as exc:
                    concurrent_results.append(("err", exc))
            with patch.object(colab_runtime, "probe_comfyui", return_value=offline_probe):
                t1 = threading.Thread(target=worker)
                t2 = threading.Thread(target=worker)
                t1.start(); t2.start()
                t1.join(5.0); t2.join(5.0)
                spawned = [int(x) for x in pids_file.read_text(encoding="utf-8").splitlines() if x.strip()]
                mgr.cancel_cli_auth(env)
            self.assertEqual(sorted(k for k, _ in concurrent_results), ["err", "ok"])
            for kind, val in concurrent_results:
                if kind == "ok":
                    self.assertTrue(val["auth_url"])
                else:
                    self.assertIsInstance(val, colab_runtime.RuntimeBusyError)
            self.assertEqual(len(spawned), 1, f"Expected serialized single spawn, got pids={spawned}")

            # 5) Calling start_cli_auth(restart=False) during verifying_cli_auth must raise RuntimeBusyError,
            #    deadline must extend past short cli_auth_timeout after submit, and stale slot auth_required error must clear
            mgr_short = colab_runtime.ColabRuntimeManager(root / ".wb2", cli_auth_timeout=0.35)
            self.addCleanup(mgr_short.close)
            mgr_short._slots["primary"].last_error = colab_runtime.message("colab_cli_auth_required")
            mgr_short._slots["primary"].auth_required_error = True
            verify_env = {**env, "VERIFY_DELAY": "0.5"}
            with patch.object(colab_runtime, "probe_comfyui", return_value=offline_probe):
                mgr_short.start_cli_auth(verify_env)
                submit_out = {}
                def do_submit():
                    submit_out["res"] = mgr_short.submit_cli_auth_code("4/0A-good-code", verify_env)
                st_thread = threading.Thread(target=do_submit)
                st_thread.start()
                deadline_wait = time.monotonic() + 2.0
                while time.monotonic() < deadline_wait:
                    if mgr_short._cli_auth.active_state == "verifying_cli_auth":
                        break
                    # gstack-timing-ok: polling worker thread transition into verifying_cli_auth within 2s timeout
                    time.sleep(0.01)
                with self.assertRaises(colab_runtime.RuntimeBusyError):
                    mgr_short.start_cli_auth(verify_env, restart=False)
                st_thread.join(5.0)
            self.assertTrue(submit_out["res"]["authenticated"])
            self.assertEqual(submit_out["res"]["status"]["state"], "offline")
            self.assertIsNone(mgr_short._slots["primary"].last_error)

            # 6) Timeout constants must exceed google-colab-cli's 30s KEYRING_TIMEOUT_SECONDS
            self.assertGreaterEqual(colab_runtime.CLI_AUTH_START_WAIT_SEC, 40.0)
            self.assertGreaterEqual(colab_runtime.CLI_AUTH_SUBMIT_TIMEOUT_SEC, 60.0)

            # 7) Windows _kill_process_tree invokes taskkill /T /F /PID
            fake_proc = MagicMock(pid=43210)
            with patch.object(colab_runtime.os, "name", "nt"), \
                 patch.object(colab_runtime.subprocess, "run") as run_mock:
                colab_runtime._kill_process_tree(fake_proc)
            run_mock.assert_called_once()
            cmd_args = run_mock.call_args.args[0]
            self.assertEqual(cmd_args[:4], ["taskkill", "/T", "/F", "/PID"])
            self.assertEqual(cmd_args[4], "43210")

    def test_pass2_cli_auth_cancel_during_spawn_short_code_scrub_and_stop_auth_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_cli = root / "fake_colab_p2.py"
            fake_cli.write_text(
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if 'stop' in args:\n"
                "    print('To authorize colab-cli, visit: https://accounts.google.com/o/oauth2/auth?stop=1', file=sys.stderr)\n"
                "    print('EOFError: EOF when reading a line', file=sys.stderr)\n"
                "    sys.exit(1)\n"
                "print('To authorize colab-cli: https://accounts.google.com/o/oauth2/auth?p2=1', file=sys.stderr, flush=True)\n"
                "sys.stdout.write('Enter the authorization code: ')\n"
                "sys.stdout.flush()\n"
                "code = sys.stdin.readline().strip()\n"
                "print('OAuth error: invalid_grant: malformed auth code', file=sys.stderr)\n"
                "sys.exit(1)\n",
                encoding="utf-8",
            )
            fake_bin = self._write_fake_colab_wrapper(root, fake_cli)
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://primary.ngrok-free.dev",
                "COMFYUI_API_KEY": "k-secret",
                "COLAB_BIN": str(fake_bin),
                "H3_LANG": "en",
            }
            mgr = colab_runtime.ColabRuntimeManager(root / ".workbench", cli_auth_timeout=10.0)
            self.addCleanup(mgr.close)
            offline_probe = {"online": False, "configured": True, "devices": [], "nodes": [], "online_count": 0, "total_count": 1}

            # 1) cancel_cli_auth() called while start_cli_auth() is inside subprocess.Popen must kill spawned proc and not resurrect state
            orig_popen = colab_runtime.subprocess.Popen
            entered_popen = threading.Event()
            release_popen = threading.Event()
            spawned_procs = []

            def delayed_popen(*args, **kwargs):
                proc = orig_popen(*args, **kwargs)
                if not spawned_procs:
                    spawned_procs.append(proc)
                    entered_popen.set()
                    release_popen.wait(timeout=5.0)
                return proc

            start_outcome = {}
            def run_start():
                try:
                    start_outcome["res"] = mgr.start_cli_auth(env)
                except Exception as exc:
                    start_outcome["err"] = exc

            with patch.object(colab_runtime, "probe_comfyui", return_value=offline_probe), \
                 patch.object(colab_runtime.subprocess, "Popen", side_effect=delayed_popen):
                t = threading.Thread(target=run_start)
                t.start()
                self.assertTrue(entered_popen.wait(timeout=3.0))
                mgr.cancel_cli_auth(env)
                release_popen.set()
                t.join(timeout=5.0)
            self.assertIn("err", start_outcome, f"Cancelled start_cli_auth must not succeed: {start_outcome}")
            self.assertIsNone(mgr._cli_auth.active_state)
            self.assertEqual(len(spawned_procs), 1)
            self.assertIsNotNone(spawned_procs[0].poll(), "Spawned proc must be terminated after cancel-during-spawn")

            # 2) Short mis-pasted code 'a' must NOT corrupt 'invalid_grant' classification
            with colab_runtime.use_lang("en"):
                with patch.object(colab_runtime, "probe_comfyui", return_value=offline_probe):
                    mgr.start_cli_auth(env)
                    bad_short = mgr.submit_cli_auth_code("a", env)
                self.assertEqual(bad_short["error"], colab_runtime.message("colab_cli_auth_invalid_code"))
                self.assertNotIn("[REDACTED]", bad_short["error"] or "")

                # 3) Online ComfyUI + expired CLI token during stop() must surface colab_cli_auth_required and expose cli_auth.state='required'
                online_probe = {
                    "online": True, "configured": True, "devices": [{"name": "G4"}],
                    "nodes": [{"url": "https://primary.ngrok-free.dev", "online": True, "auth_failed": False, "devices": [{"name": "G4"}]}],
                    "online_count": 1, "total_count": 1,
                }
                with patch.object(colab_runtime, "probe_comfyui", return_value=online_probe), \
                     patch.object(colab_runtime, "probe_remote_queue_count", return_value=0):
                    with self.assertRaises(RuntimeError) as stop_err:
                        mgr.stop(environ=env, force=True)
                    self.assertEqual(str(stop_err.exception), colab_runtime.message("colab_cli_auth_required"))
                    st_after = mgr.status(env)
                self.assertEqual(st_after["state"], "online")
                self.assertEqual(st_after["cli_auth"]["state"], "required")

    def test_ship_pass1_auth_id_sticky_hint_keepalive_start_race_and_scrubbing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "logged_in"
            fake_cli = root / "fake_colab_p1.py"
            fake_cli.write_text(
                "import os, sys, urllib.parse\n"
                "from pathlib import Path\n"
                "marker = Path(os.environ['FAKE_COLAB_MARKER'])\n"
                "args = sys.argv[1:]\n"
                "if 'exec' in args:\n"
                "    if not marker.exists():\n"
                "        print('To authorize colab-cli, visit: https://accounts.google.com/o/oauth2/auth?ka=1', file=sys.stderr)\n"
                "        print('Enter the authorization code:', file=sys.stderr)\n"
                "        sys.exit(1)\n"
                "    print('keepalive')\n"
                "    sys.exit(0)\n"
                "if marker.exists():\n"
                "    print('[colab] No active sessions found on server.')\n"
                "    sys.exit(0)\n"
                "print('To authorize colab-cli: https://accounts.google.com/o/oauth2/auth?tab=1', file=sys.stderr, flush=True)\n"
                "sys.stdout.write('Enter the authorization code: ')\n"
                "sys.stdout.flush()\n"
                "code = sys.stdin.readline().strip()\n"
                "if not code:\n"
                "    sys.exit(1)\n"
                "low_enc = urllib.parse.quote(code, safe='').lower()\n"
                "print(f'oauth err lower={low_enc}', file=sys.stderr)\n"
                "sys.exit(1)\n",
                encoding="utf-8",
            )
            fake_bin = self._write_fake_colab_wrapper(root, fake_cli)
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://primary.ngrok-free.dev",
                "COMFYUI_API_KEY": "k-secret",
                "COLAB_BIN": str(fake_bin),
                "FAKE_COLAB_MARKER": str(marker),
                "H3_LANG": "en",
            }
            offline_probe = {"online": False, "configured": True, "devices": [], "nodes": [], "online_count": 0, "total_count": 1}
            online_probe = {
                "online": True, "configured": True, "devices": [{"name": "G4"}],
                "nodes": [{"url": "https://primary.ngrok-free.dev", "online": True, "auth_failed": False, "devices": [{"name": "G4"}]}],
                "online_count": 1, "total_count": 1,
            }
            mgr = colab_runtime.ColabRuntimeManager(root / ".workbench", cli_auth_timeout=10.0)
            self.addCleanup(mgr.close)

            # 1) Cross-tab auth_id guards stale submit and stale cancel after restart; start_async rejected during active CLI auth
            with patch.object(colab_runtime, "probe_comfyui", return_value=offline_probe):
                s1 = mgr.start_cli_auth(env)
                id1 = s1["auth_id"]
                self.assertTrue(id1)
                self.assertEqual(s1["status"]["cli_auth"]["auth_id"], id1)
                with self.assertRaises(colab_runtime.RuntimeBusyError):
                    mgr.start_async(env)

                s2 = mgr.start_cli_auth(env, restart=True)
                id2 = s2["auth_id"]
                self.assertNotEqual(id1, id2)

                # Stale tab cancel must NOT kill active session id2
                stale_cancel = mgr.cancel_cli_auth(env, auth_id=id1)
                self.assertFalse(stale_cancel["cancelled"])
                self.assertTrue(stale_cancel["stale"])
                self.assertEqual(mgr._cli_auth.auth_id, id2)
                self.assertEqual(mgr._cli_auth.active_state, "awaiting_cli_auth")

                # Stale tab submit must raise ValueError without consuming id2's process
                with self.assertRaises(ValueError):
                    mgr.submit_cli_auth_code("4/0A-stale", env, auth_id=id1)
                self.assertEqual(mgr._cli_auth.active_state, "awaiting_cli_auth")

                # Malformed codes rejected (empty, NUL, newline, >2048)
                for bad_code in ("", "   ", "a\nb", "a\x00b", "x" * 2049):
                    with self.assertRaises(ValueError):
                        mgr.submit_cli_auth_code(bad_code, env, auth_id=id2)

                # Lowercase percent-encoded auth code (%2f) must be scrubbed from failure output
                bad_res = mgr.submit_cli_auth_code("4/0A-LowerScrub", env, auth_id=id2)
                self.assertFalse(bad_res["authenticated"])
                self.assertNotIn("4%2f0a-lowerscrub", (bad_res["error"] or "").lower())

            # 2) Unauthenticated keepalive detects OAuth prompt, sets _cli_auth_required_hint, and skips subsequent keepalive calls
            with patch.object(colab_runtime, "probe_comfyui", return_value=online_probe), \
                 patch.object(colab_runtime, "probe_remote_queue_count", return_value=1):
                mgr._cli_auth_required_hint = False
                mgr._slots["primary"].last_keepalive_at = 0.0
                mgr._slots["primary"].last_keepalive_fail_at = None
                self.assertFalse(mgr.maybe_send_kernel_keepalive(env, now=1000.0, force_busy=True))
                self.assertTrue(mgr._cli_auth_required_hint)

                with patch.object(mgr, "_run_cmd_for_slot", side_effect=AssertionError("keepalive must skip while cli_auth_required_hint is set")):
                    self.assertFalse(mgr.maybe_send_kernel_keepalive(env, now=2000.0, force_busy=True))

                # 3) Switching to COLAB_AUTH=adc must clear sticky cli_auth_required state
                st_adc = mgr.status({**env, "COLAB_AUTH": "adc"})
                self.assertEqual(st_adc["cli_auth"]["state"], "idle")

                # 4) Online node with sticky cli_auth_required_hint re-queries sessions and clears once terminal login completes
                mgr._cli_auth_required_hint = True
                mgr._sessions_cache = None
                marker.write_text("ok", encoding="utf-8")
                st_recovered = mgr.status(env)
                self.assertEqual(st_recovered["state"], "online")
                self.assertEqual(st_recovered["cli_auth"]["state"], "idle")
                self.assertFalse(mgr._cli_auth_required_hint)

            # 4) submit_cli_auth_code() must reject submission if auth_url was never emitted
            mgr._cli_auth = colab_runtime._CliAuthState()
            mgr._cli_auth.active_state = "awaiting_cli_auth"
            mgr._cli_auth.auth_url = None
            mgr._cli_auth.proc = MagicMock(pid=None, poll=MagicMock(return_value=None), stdin=MagicMock())
            with self.assertRaises(ValueError):
                mgr.submit_cli_auth_code("4/0A-no-url-yet", env)

    def test_kill_process_tree_ignores_non_int_and_unsafe_pids(self):
        # On Linux glibc, os.killpg(1, SIGKILL) executes kill(-1, SIGKILL) and kills
        # every process owned by the current user (including the CI runner when a
        # MagicMock's default __index__() == 1 leaks into _kill_process_tree).
        with patch.object(colab_runtime.os, "name", "posix"), \
             patch.object(colab_runtime.os, "getpgrp", return_value=7777, create=True), \
             patch.object(colab_runtime.os, "killpg", create=True) as killpg_mock, \
             patch.object(colab_runtime.signal, "SIGKILL", 9, create=True):
            unsafe_procs = [
                MagicMock(),  # implicit MagicMock .pid with int(.pid) == 1
                SimpleNamespace(pid=None, kill=MagicMock()),
                SimpleNamespace(pid=False, kill=MagicMock()),
                SimpleNamespace(pid=True, kill=MagicMock()),
                SimpleNamespace(pid=-1, kill=MagicMock()),
                SimpleNamespace(pid=0, kill=MagicMock()),
                SimpleNamespace(pid=1, kill=MagicMock()),
                SimpleNamespace(pid=7777, kill=MagicMock()),  # caller's own pgrp
            ]
            for proc in unsafe_procs:
                colab_runtime._kill_process_tree(proc)
                proc.kill.assert_called_once()
            killpg_mock.assert_not_called()

            safe_proc = SimpleNamespace(pid=99999, kill=MagicMock())
            colab_runtime._kill_process_tree(safe_proc)
            killpg_mock.assert_called_once_with(99999, 9)
            safe_proc.kill.assert_called_once()

        with patch.object(colab_runtime.os, "name", "nt"), \
             patch.object(colab_runtime.subprocess, "run") as run_mock:
            for proc in (MagicMock(), SimpleNamespace(pid=1, kill=MagicMock()), SimpleNamespace(pid=0, kill=MagicMock())):
                colab_runtime._kill_process_tree(proc)
                proc.kill.assert_called_once()
            run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
