import importlib.util
import json
import os
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from PIL import Image

from h3ref import colab_runtime, comfy, config, film, http, rh
from scripts import start_colab
from workbench import inputs, segments, server


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("h3ref_cli_review", config.ROOT / "h3ref.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), (90, 120, 150)).save(path, format="PNG")
    return path


class ComfyReviewFixesTest(unittest.TestCase):
    def test_start_colab_ngrok_omits_invalid_flag_and_unassign_header_and_retry(self):
        # 1. Verify _unassign_colab_runtime sends X-Colab-Tunnel: Google and returns False when /unassign fails
        captured_headers = {}

        def fake_urlopen_fail(req, timeout=10.0):
            captured_headers.update({k.lower(): v for k, v in req.header_items()})
            raise ConnectionRefusedError("runtime endpoint unreachable")

        logs = []
        with mock.patch.dict(os.environ, {"TBE_RUNTIME_ADDR": "127.0.0.1:9999"}, clear=False), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen_fail):
            ok = start_colab._unassign_colab_runtime(logs.append)
        self.assertEqual(captured_headers.get("x-colab-tunnel"), "Google")
        self.assertFalse(ok)

        # 2. Verify spawn_ngrok in start_colab.py never passes nonexistent --authtoken-from-env flag
        src = (config.ROOT / "scripts" / "start_colab.py").read_text(encoding="utf-8")
        self.assertNotIn("--authtoken-from-env", src)

        # 3. Verify _parallel_copy_file copies multi-stripe payload byte-for-byte and stage_models_to_local_ssd symlinks when disk headroom is low
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drive_base = root / "drive"
            local_base = root / "local"
            for rel in start_colab.MODEL_FILES:
                p = drive_base / "models" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(os.urandom(64 * 1024) if "qwen3vl" not in rel else b"Q" * (128 * 1024))

            with mock.patch.object(start_colab, "BASE", drive_base), \
                 mock.patch.object(start_colab, "LOCAL", local_base), \
                 mock.patch.object(start_colab, "SSD_HEADROOM_BYTES", 1024 * 1024), \
                 mock.patch("shutil.disk_usage", side_effect=[
                     *[types.SimpleNamespace(free=10 * 1024 * 1024) for _ in range(len(start_colab.MODEL_FILES) - 1)],
                     types.SimpleNamespace(free=512 * 1024),  # < 128KB + 1MB headroom -> symlink!
                 ]):
                start_colab.stage_models_to_local_ssd()

            first_rel = start_colab.MODEL_FILES[0]
            last_rel = start_colab.MODEL_FILES[-1]
            self.assertFalse((local_base / "models" / first_rel).is_symlink())
            self.assertEqual(
                (local_base / "models" / first_rel).read_bytes(),
                (drive_base / "models" / first_rel).read_bytes(),
            )
            self.assertTrue((local_base / "models" / last_rel).is_symlink())
            self.assertEqual((local_base / "models" / last_rel).read_bytes(), b"Q" * (128 * 1024))

    def test_comfy_rejection_then_switch_to_runninghub_polls_runninghub_not_comfy(self):
        # Reproduces Critical #2: ComfyUI SubmissionRejected leaves provider="comfyui" in state.json;
        # switching VIDEO_PROVIDER=runninghub must overwrite provider="runninghub" and poll rh.poll, never comfy.poll.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pic = _make_png(root / "actor.png")
            out_seg = root / "film-fallback"
            job = film.Job(out_seg)
            job.put("segs", "0", h3="subject_definitions:\nPicture 1: A.\n")
            film_doc = {"aspect": "9:16", "cast": [{"name": "A", "picture": str(pic)}], "shots": []}
            seg_spec = {"idx": 0, "seconds": 5, "shots": [{"action": "A waves"}]}
            pics = [{"kind": "cast", "cast": {"name": "A", "picture": str(pic)}}]

            # Step 1: ComfyUI rejects submission (HTTP 400 / node_errors)
            with mock.patch.dict(
                os.environ,
                {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "https://demo.ngrok-free.dev", "COMFYUI_API_KEY": "k1"},
                clear=False,
            ), mock.patch("h3ref.comfy.upload", return_value="hash.png"), \
               mock.patch("h3ref.comfy.http.request", side_effect=http.HTTPResponseError(400, '{"node_errors":{"127":"bad"}}')):
                with self.assertRaises(comfy.SubmissionRejected):
                    film._generate_segment(job, film_doc, seg_spec, pics, reference_paths=[pic])

            # Step 2: User switches VIDEO_PROVIDER=runninghub and reruns
            comfy_polls = []
            rh_polls = []
            with mock.patch.dict(
                os.environ,
                {"VIDEO_PROVIDER": "runninghub", "RUNNINGHUB_API_KEY": "rh-key"},
                clear=False,
            ), mock.patch("h3ref.rh.upload", return_value="rh-img.png"), \
               mock.patch("h3ref.rh.submit_with_retry", side_effect=lambda g, log=None, record_submission=None: (
                   record_submission and record_submission(True),
                   "rh-paid-task-99",
               )[1]), \
               mock.patch("h3ref.comfy.poll", side_effect=lambda tid: comfy_polls.append(tid) or {"state": "failed", "error": "wrong backend"}), \
               mock.patch("h3ref.rh.poll", side_effect=lambda tid: rh_polls.append(tid) or {"state": "done", "url": "https://rh/v.mp4", "cny": 0.5, "coins": 100, "run_seconds": 12.0}), \
               mock.patch("h3ref.http.download", side_effect=lambda u, dest: (dest.parent.mkdir(parents=True, exist_ok=True), dest.write_bytes(b"mp4"))), \
               mock.patch("h3ref.media.last_frame"):
                res_rec = film._generate_segment(job, film_doc, seg_spec, pics, reference_paths=[pic])

            self.assertEqual(comfy_polls, [])
            self.assertEqual(rh_polls, ["rh-paid-task-99"])
            self.assertEqual(res_rec.get("provider"), "runninghub")

            # Step 3: Same check for CLI cmd_one
            cli = _load_cli_module()
            out_one = root / "one-fallback"
            out_one.mkdir()
            (out_one / "state.json").write_text(json.dumps({"h3": "subject_definitions:\nPicture 1: A.\n"}), encoding="utf-8")
            prompt_file = root / "prompt.txt"
            prompt_file.write_text("Picture 1 waves.", encoding="utf-8")
            args = types.SimpleNamespace(
                prompt=str(prompt_file),
                image=[str(pic)],
                seconds=5,
                aspect="9:16",
                seed=42,
                out=str(out_one),
                check=False,
            )
            with mock.patch.dict(
                os.environ,
                {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "https://demo.ngrok-free.dev", "COMFYUI_API_KEY": "k1"},
                clear=False,
            ), mock.patch("h3ref.comfy.upload", return_value="hash.png"), \
               mock.patch("h3ref.comfy.http.request", side_effect=http.HTTPResponseError(400, '{"error":"bad"}')):
                with self.assertRaises(comfy.SubmissionRejected):
                    cli.cmd_one(args)

            one_comfy_polls = []
            one_rh_polls = []
            with mock.patch.dict(
                os.environ,
                {"VIDEO_PROVIDER": "runninghub", "RUNNINGHUB_API_KEY": "rh-key"},
                clear=False,
            ), mock.patch("h3ref.rh.upload", return_value="rh-img.png"), \
               mock.patch("h3ref.rh.submit_with_retry", return_value="rh-one-task-77"), \
               mock.patch("h3ref.comfy.poll", side_effect=lambda tid: one_comfy_polls.append(tid) or {"state": "failed"}), \
               mock.patch("h3ref.rh.poll", side_effect=lambda tid: one_rh_polls.append(tid) or {"state": "done", "url": "https://rh/1.mp4", "cny": 0.4, "run_seconds": 10.0}), \
               mock.patch("h3ref.http.download", side_effect=lambda u, dest: dest.write_bytes(b"mp4")), \
               mock.patch("h3ref.media.duration", return_value=5.0):
                cli.cmd_one(args)

            self.assertEqual(one_comfy_polls, [])
            self.assertEqual(one_rh_polls, ["rh-one-task-77"])
            saved_one = json.loads((out_one / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_one.get("provider"), "runninghub")

    def test_colab_runtime_cancel_during_drive_auth_locale_binding_and_secret_stripping(self):
        # 1. REMOTE_START_CODE streams stdout via Popen + PIPE
        self.assertIn("Popen", colab_runtime.REMOTE_START_CODE)
        self.assertIn("PIPE", colab_runtime.REMOTE_START_CODE)

        # 2. CLAUDE_CODE_OAUTH_TOKEN and NGROK_AUTHTOKEN are stripped from child env
        sanitized = colab_runtime._popen_kwargs({
            "PATH": "/usr/bin",
            "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret",
            "NGROK_AUTHTOKEN": "ngrok-secret",
        })["env"]
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", sanitized)
        self.assertNotIn("NGROK_AUTHTOKEN", sanitized)

        # 3. Calling stop() while startup is paused in awaiting_drive_auth cancels startup without RuntimeBusyError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_colab = root / "colab"
            fake_colab.write_text("#!/bin/sh\n", encoding="utf-8")
            fake_colab.chmod(0o755)
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": "secret-token-123",
                "COLAB_BIN": str(fake_colab),
                "H3_LANG": "en",
            }
            mgr = colab_runtime.ColabRuntimeManager(root / ".workbench")
            stop_invoked = []

            class HangingDriveProc:
                def __init__(self):
                    self.pid = 43210
                    self._killed = threading.Event()
                    r_fd, w_fd = os.pipe()
                    os.write(
                        w_fd,
                        b"Open https://accounts.google.com/o/oauth2/auth?client=1\nPress Enter after you have granted access: ",
                    )
                    self._w_fd = w_fd
                    self.stdout = os.fdopen(r_fd, "r", encoding="utf-8")
                    self.stdin = mock.MagicMock()

                def poll(self):
                    return -9 if self._killed.is_set() else None

                def wait(self, timeout=None):
                    self._killed.wait(timeout=timeout)
                    return -9

                def kill(self):
                    self._killed.set()
                    if self._w_fd is not None:
                        os.close(self._w_fd)
                        self._w_fd = None

            def fake_kill_tree(proc):
                proc.kill()

            def fake_run_cmd(colab_bin, auth_mode, sub_args, **kwargs):
                if sub_args[0] == "sessions":
                    return 0, "[comfy-a100] m-s-1 | Hardware: A100\n"
                if sub_args[0] == "stop":
                    stop_invoked.append(sub_args)
                    return 0, "Stopped\n"
                return 0, ""

            with mock.patch("h3ref.colab_runtime.probe_comfyui", return_value={"online": False, "auth_failed": False}), \
                 mock.patch("h3ref.colab_runtime.probe_remote_queue_count", return_value=0), \
                 mock.patch.object(mgr, "_run_command", side_effect=fake_run_cmd), \
                 mock.patch("subprocess.Popen", side_effect=lambda *a, **kw: HangingDriveProc()), \
                 mock.patch("h3ref.colab_runtime._kill_process_tree", side_effect=fake_kill_tree):
                mgr.start_async(env)
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and mgr.status(env)["state"] != "awaiting_drive_auth":
                    time.sleep(0.02)
                self.assertEqual(mgr.status(env)["state"], "awaiting_drive_auth")

                res = mgr.stop(environ=env, force=True, local_busy=False)
                self.assertTrue(res["stopped"])
                self.assertEqual(len(stop_invoked), 1)
                self.assertEqual(mgr.status(env)["state"], "offline")

            # 3b. Cold A100 kernel WebSocket warmup ("Connection was lost.") retries drivemount automatically
            mount_attempts = []

            def flaky_drivemount(cb, am, sn, ec):
                mount_attempts.append(1)
                if len(mount_attempts) == 1:
                    return 1, "RuntimeError: Connection was lost.\n", False
                return 0, "Mounted at /content/drive\n", False

            with mock.patch("h3ref.colab_runtime.probe_comfyui", side_effect=[
                {"online": False, "auth_failed": False},
                {"online": True, "auth_failed": False, "devices": [{"name": "A100"}]},
                {"online": True, "auth_failed": False, "devices": [{"name": "A100"}]},
            ]), \
                 mock.patch.object(mgr, "_run_drivemount_once", side_effect=flaky_drivemount), \
                 mock.patch.object(mgr, "_run_command", side_effect=lambda cb, am, args, **kw: (
                     (0, "[comfy-a100] m-s-1 | Hardware: A100\n") if args[0] == "sessions" else (0, "TUNNEL READY\n")
                 )), \
                 mock.patch("h3ref.colab_runtime.time.sleep"):
                st_online = mgr.ensure_started(env, timeout=5.0)
                self.assertEqual(st_online["state"], "online")
                self.assertEqual(len(mount_attempts), 2)

            # 4. English Accept-Language returns English runtime error on missing CLI
            app = server.create_app(storage=root / ".wb2", env_path=root / ".env-empty")
            client = app.test_client()
            with mock.patch("h3ref.colab_runtime.find_colab_bin", return_value=None), \
                 mock.patch("h3ref.colab_runtime.probe_comfyui", return_value={"online": False, "auth_failed": False}):
                client.post(
                    "/api/config",
                    headers={"X-Workbench": "1", "Accept-Language": "en"},
                    json={"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "https://demo.ngrok-free.dev", "COMFYUI_API_KEY": "k12345678", "persist": False},
                )
                err_res = client.post(
                    "/api/runtime/start",
                    headers={"X-Workbench": "1", "Accept-Language": "en"},
                    json={},
                )
                self.assertEqual(err_res.status_code, 400)
                self.assertIn("google-colab-cli", err_res.get_json()["error"])

    def test_draining_legacy_runninghub_segment_alongside_terminal_blocker_does_not_require_comfyui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pic = _make_png(root / "actor.png")
            out = root / "drain-job"
            (out / "frames").mkdir(parents=True)
            _make_png(out / "frames" / "s1-1.png")
            _make_png(out / "frames" / "s2-1.png")
            _make_png(out / "frames" / "s3-1.png")
            (out / "state.json").write_text(
                json.dumps({
                    "frames": {
                        "s1-1": {"prompt": "p1", "composition": "c1"},
                        "s2-1": {"prompt": "p2", "composition": "c2"},
                        "s3-1": {"prompt": "p3", "composition": "c3"},
                    },
                    "segs": {
                        "0": {"h3": "h3-1", "task": "rh-inflight-1"},
                        "1": {"h3": "h3-2", "task": "rh-dead-2", "terminal_failed": True, "error": "boom"},
                        "2": {"h3": "h3-3"},
                    },
                }),
                encoding="utf-8",
            )
            job_req = {
                "kind": "film",
                "demo": False,
                "operation": "pipeline",
                "frames": "shot",
                "output_dir": str(out),
                "lang": "en",
                "film": {
                    "aspect": "9:16",
                    "cast": [{"name": "A", "look": "tall", "aliases": [], "picture": str(pic)}],
                    "shots": [
                        {"start": 0.0, "end": 6.0, "action": "A stands", "camera": "wide", "line": ""},
                        {"start": 6.0, "end": 12.0, "action": "A walks", "camera": "wide", "line": ""},
                        {"start": 12.0, "end": 18.0, "action": "A sits", "camera": "wide", "line": ""},
                    ],
                },
            }
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": "comfy-secret-123",
                "RUNNINGHUB_API_KEY": "rh-secret-123",
                "APIMART_API_KEY": "apimart-secret-123",
                "LLM_API_KEY": "llm-secret-123",
            }
            providers = inputs.needed_video_providers(job_req, env)
            self.assertEqual(providers, ["runninghub"])

    def test_comfy_full20_omits_lora_node_and_poll_tolerates_transient_ngrok_reconnect(self):
        g20 = comfy.graph("p", ["a.png"], 5, "9:16", 1, preset="full20")
        self.assertNotIn(comfy.LORA, g20)
        self.assertEqual(g20["126"]["inputs"]["model"], ["127", 0])

        calls = {"n": 0}

        def flaky_request(url, key, body=None, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http.HTTPResponseError(404, "<html>ERR_NGROK_3200 tunnel offline</html>")
            if url.endswith("/history/pid-reconnect"):
                return {}
            return {"queue_running": [[1, "pid-reconnect"]], "queue_pending": []}

        with mock.patch.dict(os.environ, {"COMFYUI_URL": "https://demo.ngrok-free.dev", "COMFYUI_API_KEY": "k"}, clear=False), \
             mock.patch("h3ref.comfy.http.request", side_effect=flaky_request):
            st1 = comfy.poll("pid-reconnect")
            self.assertEqual(st1["state"], "running")
            st2 = comfy.poll("pid-reconnect")
            self.assertEqual(st2["state"], "running")

        # Transient TCP reset on /queue is tolerated up to 3 consecutive polls, then raises on the 4th
        comfy._TRANSIENT_COUNTS.clear()
        reset_err = RuntimeError("https://demo.ngrok-free.dev/queue: ConnectionResetError: [Errno 54] Connection reset by peer")
        queue_calls = {"n": 0}

        def flaky_queue_reset(url, key, body=None, **kwargs):
            if "/history/" in url:
                return {}
            queue_calls["n"] += 1
            if queue_calls["n"] <= 4:
                raise reset_err
            return {"queue_running": [[1, "pid-reset"]], "queue_pending": []}

        with mock.patch.dict(os.environ, {"COMFYUI_URL": "https://demo.ngrok-free.dev", "COMFYUI_API_KEY": "k"}, clear=False), \
             mock.patch("h3ref.comfy.http.request", side_effect=flaky_queue_reset):
            for _ in range(3):
                self.assertEqual(comfy.poll("pid-reset")["state"], "running")
            with self.assertRaisesRegex(RuntimeError, "ConnectionResetError"):
                comfy.poll("pid-reset")

    def test_stage_models_auto_downloads_missing_upscaler_atomically_and_syncs_to_ssd(self):
        import io

        upscale_rel = "upscale_models/RealESRGAN_x2plus.pth"
        expected_url = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth"
        fake_weights = b"ESRGAN_X2PLUS_WEIGHTS_" * 4096

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drive_base = root / "drive"
            local_base = root / "local"
            for rel in start_colab.MODEL_FILES:
                if rel == upscale_rel:
                    continue
                p = drive_base / "models" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"M" * 4096)

            downloaded_urls = []

            class FakeResp:
                def __init__(self, payload: bytes):
                    self._buf = io.BytesIO(payload)

                def read(self, n=-1):
                    return self._buf.read(n)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            def fake_urlopen(req, timeout=180.0):
                downloaded_urls.append(req.full_url)
                return FakeResp(fake_weights)

            with mock.patch.object(start_colab, "BASE", drive_base), \
                 mock.patch.object(start_colab, "LOCAL", local_base), \
                 mock.patch.object(start_colab, "SSD_HEADROOM_BYTES", 1024), \
                 mock.patch("shutil.disk_usage", return_value=types.SimpleNamespace(free=100 * 1024 * 1024)), \
                 mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                start_colab.stage_models_to_local_ssd()

            drive_file = drive_base / "models" / upscale_rel
            ssd_file = local_base / "models" / upscale_rel
            self.assertEqual(downloaded_urls, [expected_url])
            self.assertTrue(drive_file.is_file())
            self.assertFalse(drive_file.with_suffix(drive_file.suffix + ".tmp").exists())
            self.assertEqual(drive_file.read_bytes(), fake_weights)
            self.assertTrue(ssd_file.is_file() and not ssd_file.is_symlink())
            self.assertEqual(ssd_file.read_bytes(), fake_weights)

            # Second run must not re-download when Drive file already exists
            with mock.patch.object(start_colab, "BASE", drive_base), \
                 mock.patch.object(start_colab, "LOCAL", local_base), \
                 mock.patch.object(start_colab, "SSD_HEADROOM_BYTES", 1024), \
                 mock.patch("shutil.disk_usage", return_value=types.SimpleNamespace(free=100 * 1024 * 1024)), \
                 mock.patch("urllib.request.urlopen", side_effect=AssertionError("Must not re-download existing weight!")):
                start_colab.stage_models_to_local_ssd()

            # Interrupted download cleans up .tmp and leaves target absent
            drive_file.unlink()
            with mock.patch("urllib.request.urlopen", side_effect=OSError("connection reset")):
                with self.assertRaises(OSError):
                    start_colab._download_file_atomic(expected_url, drive_file, upscale_rel)
            self.assertFalse(drive_file.exists())
            self.assertFalse(drive_file.with_suffix(drive_file.suffix + ".tmp").exists())

    def test_watchdog_queue_busy_ws_and_timeout_protection_and_passive_probes(self):
        import io
        import socket

        # 1. WS progress_snapshot status=="running" immediately reports busy without querying /queue
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Should short-circuit on WS running")):
            self.assertTrue(start_colab._queue_busy(lambda: {"status": "running", "node": "155"}))
            self.assertTrue(start_colab._queue_busy({"status": "running"}))

        # 2. /queue timeout or transient error while ComfyUI process is alive must report busy (never false idle)
        with mock.patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")), \
             mock.patch.object(start_colab, "_comfy_process_alive", return_value=True):
            self.assertTrue(start_colab._queue_busy(lambda: {"status": "finished"}))

        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("transient reset")), \
             mock.patch.object(start_colab, "_comfy_process_alive", return_value=True):
            self.assertTrue(start_colab._queue_busy())

        # 3. /queue failure when ComfyUI process is dead reports idle so watchdog can unassign dead instance
        with mock.patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("dead")), \
             mock.patch.object(start_colab, "_comfy_process_alive", return_value=False):
            self.assertFalse(start_colab._queue_busy(lambda: {}))

        # 4. /queue returning empty lists reports idle; non-empty running or pending reports busy
        class FakeQueueResp:
            def __init__(self, obj):
                self._raw = json.dumps(obj).encode("utf-8")

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch("urllib.request.urlopen", return_value=FakeQueueResp({"queue_running": [], "queue_pending": []})):
            self.assertFalse(start_colab._queue_busy(lambda: {"status": "success"}))
        with mock.patch("urllib.request.urlopen", return_value=FakeQueueResp({"queue_running": [], "queue_pending": [[1, "pid"]]})):
            self.assertTrue(start_colab._queue_busy(lambda: {"status": "success"}))

        # 5. Passive probe paths never count as active work; real work paths do
        for path in ("/system_stats", "/system_stats?x=1", "/queue", "/queue/", "/h3_progress"):
            self.assertTrue(start_colab._is_passive_probe("GET", path), path)
            self.assertTrue(start_colab._is_passive_probe("HEAD", path), path)
        for method, path in (("POST", "/prompt"), ("POST", "/upload/image"), ("GET", "/view?filename=out.mp4"), ("POST", "/queue")):
            self.assertFalse(start_colab._is_passive_probe(method, path), f"{method} {path}")


if __name__ == "__main__":
    unittest.main()
