"""Stage 3 tests: cross-platform Colab runtime controller (h3ref/colab_runtime.py)."""
import io
import os
import subprocess
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

                # 5. Another 8 min passes, queue becomes empty -> stops keepalive immediately
                clock_state["now"] += 300.0
                with patch.object(colab_runtime, "probe_remote_queue_count", return_value=0):
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


if __name__ == "__main__":
    unittest.main()
