import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from h3ref import config
from scripts import package_windows
from workbench import inputs, segments, server
from workbench.i18n import InputError


def _make_png(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), (40, 80, 120)).save(path, format="PNG")
    return path


class WorkbenchComfyTest(unittest.TestCase):
    def test_inputs_and_segments_preflight_comfyui_vs_runninghub_and_cross_provider_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pic = _make_png(root / "actor.png")
            base_llm_env = {
                "APIMART_API_KEY": "apimart-test-key-1234",
                "LLM_API_KEY": "llm-test-key-1234",
            }
            one_job = {
                "kind": "one",
                "demo": False,
                "operation": "pipeline",
                "prompt": "Picture 1 smiles.",
                "seconds": 5,
                "aspect": "9:16",
                "images": [str(pic)],
                "output_dir": str(root / "out-one"),
            }

            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
                # Missing COMFYUI_URL fails with COMFYUI_URL, never RUNNINGHUB_API_KEY
                with self.assertRaises(InputError) as ctx:
                    inputs.preflight(one_job, {**base_llm_env, "VIDEO_PROVIDER": "comfyui"})
                self.assertEqual(ctx.exception.key, "keys")
                self.assertIn("COMFYUI_URL", ctx.exception.values["names"])
                self.assertNotIn("RUNNINGHUB_API_KEY", ctx.exception.values["names"])

                # Local loopback ComfyUI passes without COMFYUI_API_KEY or RUNNINGHUB_API_KEY
                inputs.preflight(
                    one_job,
                    {**base_llm_env, "VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "http://127.0.0.1:8188"},
                )

                # Remote HTTPS tunnel requires COMFYUI_API_KEY
                with self.assertRaises(InputError) as ctx:
                    inputs.preflight(
                        one_job,
                        {**base_llm_env, "VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "https://demo.ngrok-free.dev"},
                    )
                self.assertEqual(ctx.exception.key, "keys")
                self.assertIn("COMFYUI_API_KEY", ctx.exception.values["names"])

                inputs.preflight(
                    one_job,
                    {
                        **base_llm_env,
                        "VIDEO_PROVIDER": "comfyui",
                        "COMFYUI_URL": "https://demo.ngrok-free.dev",
                        "COMFYUI_API_KEY": "comfy-secret-token-123",
                    },
                )

                # Cross-provider inflight resume: legacy RunningHub task requires RUNNINGHUB_API_KEY even if env switched to comfyui
                legacy_out = root / "out-legacy"
                legacy_out.mkdir()
                (legacy_out / "state.json").write_text(
                    json.dumps({"h3": "already rewritten", "task": "rh-task-1"}),
                    encoding="utf-8",
                )
                with self.assertRaises(InputError) as ctx:
                    inputs.preflight(
                        {**one_job, "output_dir": str(legacy_out)},
                        {
                            **base_llm_env,
                            "VIDEO_PROVIDER": "comfyui",
                            "COMFYUI_URL": "https://demo.ngrok-free.dev",
                            "COMFYUI_API_KEY": "comfy-secret-token-123",
                        },
                    )
                self.assertIn("RUNNINGHUB_API_KEY", ctx.exception.values["names"])

                # Segment regeneration preflight in comfyui mode does not require RUNNINGHUB_API_KEY
                seg_out = root / "out-seg"
                (seg_out / "inputs").mkdir(parents=True)
                (seg_out / "state.json").write_text(json.dumps({"frames": {}, "segs": {}}), encoding="utf-8")
                (seg_out / "inputs" / "segment-edit.json").write_text(
                    json.dumps({"number": 1, "revision": "rev1", "prompt": "new", "prompt_format": "h3", "applied": False}),
                    encoding="utf-8",
                )
                seg_req = {
                    "kind": "film",
                    "demo": False,
                    "operation": "segment",
                    "segment": 1,
                    "frames": "none",
                    "output_dir": str(seg_out),
                    "lang": "zh-CN",
                    "film": {
                        "aspect": "9:16",
                        "cast": [{"name": "A", "look": "tall", "aliases": [], "picture": str(pic)}],
                        "shots": [{"start": 0.0, "end": 5.0, "action": "A waves", "camera": "wide", "line": ""}],
                    },
                }
                segments.preflight(
                    seg_req,
                    {
                        **base_llm_env,
                        "VIDEO_PROVIDER": "comfyui",
                        "COMFYUI_URL": "https://demo.ngrok-free.dev",
                        "COMFYUI_API_KEY": "comfy-secret-token-123",
                    },
                )

    def test_config_endpoints_comfyui_fields_redaction_and_origin_change_clears_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / ".env"
            env_path.write_text("COLAB_AUTO_START=1\nVIDEO_PROVIDER=comfyui\nCOMFYUI_URL=https://a.ngrok-free.dev\n", encoding="utf-8")
            with mock.patch("h3ref.colab_runtime.ColabRuntimeManager.start_async") as start_mock:
                app = server.create_app(storage=root / ".workbench", env_path=env_path)
                client = app.test_client()
                res = client.get("/api/bootstrap")
                self.assertEqual(res.status_code, 200)
                start_mock.assert_not_called()
                cfg = res.get_json()["config"]
                self.assertEqual(cfg["VIDEO_PROVIDER"], "comfyui")
                self.assertEqual(cfg["COMFYUI_RESOLUTION"], "768p")
                self.assertEqual(cfg["COMFYUI_UPSCALE"], "0")
                self.assertEqual(cfg["COLAB_GPU"], "G4")
                self.assertEqual(cfg["COLAB_AUTO_START"], "1")

            headers = {"X-Workbench": "1"}
            res = client.post(
                "/api/config",
                headers=headers,
                json={
                    "VIDEO_PROVIDER": "comfyui",
                    "COMFYUI_URL": "https://tunnel-a.ngrok-free.dev/",
                    "COMFYUI_API_KEY": "super-secret-comfy-key-999",
                    "COMFYUI_PRESET": "8step",
                    "COMFYUI_RESOLUTION": "544p",
                    "COMFYUI_UPSCALE": "1",
                    "COLAB_GPU": "H100",
                    "COLAB_AUTO_START": "0",
                    "persist": True,
                },
            )
            self.assertEqual(res.status_code, 200)
            raw_text = res.get_data(as_text=True)
            self.assertNotIn("super-secret-comfy-key-999", raw_text)
            body = res.get_json()
            self.assertEqual(body["VIDEO_PROVIDER"], "comfyui")
            self.assertEqual(body["COMFYUI_URL"], "https://tunnel-a.ngrok-free.dev")
            self.assertEqual(body["COMFYUI_PRESET"], "8step")
            self.assertEqual(body["COMFYUI_RESOLUTION"], "544p")
            self.assertEqual(body["COMFYUI_UPSCALE"], "1")
            self.assertEqual(body["COLAB_GPU"], "H100")
            self.assertEqual(body["COLAB_AUTO_START"], "0")
            disk_env = config.read_env_file(env_path)
            self.assertEqual(disk_env.get("COMFYUI_RESOLUTION"), "544p")
            self.assertEqual(disk_env.get("COMFYUI_UPSCALE"), "1")
            self.assertEqual(disk_env.get("COLAB_GPU"), "H100")
            self.assertTrue(body["keys"]["COMFYUI_API_KEY"])

            for bad_payload in (
                {"COMFYUI_UPSCALE": "ultra", "persist": False},
                {"COMFYUI_RESOLUTION": "1080p", "persist": False},
                {"COLAB_GPU": "T4", "persist": False},
            ):
                bad_res = client.post("/api/config", headers=headers, json=bad_payload)
                self.assertEqual(bad_res.status_code, 400, bad_payload)

            # Changing COMFYUI_URL origin without supplying COMFYUI_API_KEY clears the stored key
            res2 = client.post(
                "/api/config",
                headers=headers,
                json={"COMFYUI_URL": "https://tunnel-b.ngrok-free.dev", "persist": True},
            )
            self.assertEqual(res2.status_code, 200)
            self.assertFalse(res2.get_json()["keys"]["COMFYUI_API_KEY"])
            self.assertNotIn("COMFYUI_API_KEY", config.read_env_file(env_path))

            # Reject embedded credentials in COMFYUI_URL
            bad = client.post(
                "/api/config",
                headers=headers,
                json={"COMFYUI_URL": "https://user:leak@tunnel-b.ngrok-free.dev", "persist": False},
            )
            self.assertEqual(bad.status_code, 400)
            self.assertNotIn("leak", bad.get_data(as_text=True))

    def test_runtime_endpoints_csrf_drive_ack_stop_confirmation_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / ".env"
            env_path.write_text(
                "VIDEO_PROVIDER=comfyui\nCOMFYUI_URL=https://demo.ngrok-free.dev\nCOMFYUI_API_KEY=my-secret-token-7777\n",
                encoding="utf-8",
            )
            app = server.create_app(storage=root / ".workbench", env_path=env_path)
            client = app.test_client()
            headers = {"X-Workbench": "1"}

            # CSRF check on POST
            self.assertEqual(client.post("/api/runtime/start", json={}).status_code, 400)

            runtime = app.extensions["colab_runtime"]
            with mock.patch.object(
                runtime,
                "status",
                return_value={
                    "state": "error",
                    "provider": "comfyui",
                    "comfyui_url": "https://demo.ngrok-free.dev",
                    "session": "comfy-a100",
                    "cli_available": True,
                    "devices": [],
                    "auth_url": None,
                    "error": "Auth failed with my-secret-token-7777",
                    "orphan_sessions": [],
                    "logs": ["header my-secret-token-7777 rejected"],
                },
            ):
                st = client.get("/api/runtime/status")
                self.assertEqual(st.status_code, 200)
                text = st.get_data(as_text=True)
                self.assertNotIn("my-secret-token-7777", text)
                self.assertIn("[REDACTED]", text)

            with mock.patch.object(runtime, "submit_drive_authorized", return_value={"accepted": True, "state": "starting_comfy"}), \
                 mock.patch.object(runtime, "status", return_value={"state": "starting_comfy", "logs": [], "error": None}):
                ack = client.post("/api/runtime/drive-ack", headers=headers, json={})
                self.assertEqual(ack.status_code, 200)
                self.assertTrue(ack.get_json()["accepted"])

            # Simulate an active local video job -> stop(force=False) returns confirmation_required
            jobs = app.extensions["jobs"]
            jobs.records["live-1"] = {
                "id": "live-1",
                "kind": "one",
                "demo": False,
                "operation": "pipeline",
                "status": "running",
                "output_dir": str(root / "live-1"),
                "created_at": "2026-11-17T00:00:00+0000",
            }
            with mock.patch("h3ref.colab_runtime.find_colab_bin", return_value="/usr/local/bin/colab"), \
                 mock.patch("h3ref.colab_runtime.probe_remote_queue_count", return_value=0), \
                 mock.patch.object(runtime, "status", return_value={"state": "online", "logs": [], "error": None}):
                stop1 = client.post("/api/runtime/stop", headers=headers, json={"force": False})
                self.assertEqual(stop1.status_code, 200)
                self.assertTrue(stop1.get_json()["confirmation_required"])
                self.assertFalse(stop1.get_json()["stopped"])

            with mock.patch("h3ref.colab_runtime.find_colab_bin", return_value="/usr/local/bin/colab"), \
                 mock.patch("h3ref.colab_runtime.probe_remote_queue_count", return_value=0), \
                 mock.patch.object(runtime, "_run_command", return_value=(0, "Stopped")):
                stop2 = client.post("/api/runtime/stop", headers=headers, json={"force": True})
                self.assertEqual(stop2.status_code, 200)
                self.assertTrue(stop2.get_json()["stopped"])

    def test_video_entry_triggers_auto_start_when_enabled_or_reports_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pic = _make_png(root / "actor.png")
            env_path = root / ".env"
            env_path.write_text(
                "\n".join([
                    "VIDEO_PROVIDER=comfyui",
                    "COMFYUI_URL=https://demo.ngrok-free.dev",
                    "COMFYUI_API_KEY=comfy-secret-token-123",
                    "APIMART_API_KEY=apimart-secret-123",
                    "LLM_API_KEY=llm-secret-123",
                    "COLAB_AUTO_START=0",
                ]) + "\n",
                encoding="utf-8",
            )
            app = server.create_app(storage=root / ".workbench", env_path=env_path)
            client = app.test_client()
            headers = {"X-Workbench": "1"}
            runtime = app.extensions["colab_runtime"]
            self.addCleanup(runtime.stop_keepalive_monitor)

            one_payload = {
                "kind": "one",
                "demo": False,
                "prompt": "Picture 1 waves.",
                "seconds": 5,
                "aspect": "9:16",
                "images": [str(pic)],
                "output_dir": str(root / "one-job"),
            }
            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch("h3ref.colab_runtime.probe_comfyui", return_value={"online": False, "auth_failed": False}), \
                 mock.patch.object(runtime, "start_async") as start_mock:
                offline_res = client.post("/api/jobs", headers=headers, json=one_payload)
                self.assertEqual(offline_res.status_code, 400)
                start_mock.assert_not_called()
                self.assertFalse((root / "one-job").exists())

            # Enable COLAB_AUTO_START=1 -> triggers start_async and returns friendly 400 while booting
            client.post("/api/config", headers=headers, json={"COLAB_AUTO_START": "1", "persist": False})
            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch("h3ref.colab_runtime.probe_comfyui", return_value={"online": False, "auth_failed": False}), \
                 mock.patch("h3ref.colab_runtime.find_colab_bin", return_value="/usr/local/bin/colab"), \
                 mock.patch.object(runtime, "start_async", return_value={"state": "provisioning"}) as start_mock:
                boot_res = client.post("/api/jobs", headers=headers, json=one_payload)
                self.assertEqual(boot_res.status_code, 400)
                start_mock.assert_called_once()
                self.assertFalse((root / "one-job").exists())

            # Film job creation only runs frames, so it proceeds even while ComfyUI is offline
            film_payload = {
                "kind": "film",
                "demo": False,
                "frames": "shot",
                "film_base_dir": str(root),
                "output_dir": str(root / "film-job"),
                "film": {
                    "aspect": "9:16",
                    "cast": [{"name": "A", "look": "tall", "aliases": [], "picture": str(pic)}],
                    "shots": [{"start": 0, "end": 5, "action": "A walks", "camera": "wide", "line": ""}],
                },
            }
            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch("h3ref.colab_runtime.probe_comfyui", return_value={"online": False, "auth_failed": False}), \
                 mock.patch.object(app.extensions["jobs"], "launch") as launch_mock:
                film_res = client.post("/api/jobs", headers=headers, json=film_payload)
                self.assertEqual(film_res.status_code, 201)
                launch_mock.assert_called_once()
            app.extensions["jobs"].records[film_res.get_json()["id"]]["status"] = "succeeded"

            # Once ComfyUI is online, live one job launches cleanly
            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch("h3ref.colab_runtime.probe_comfyui", return_value={"online": True, "auth_failed": False}), \
                 mock.patch.object(app.extensions["jobs"], "launch") as launch_mock:
                ok_res = client.post("/api/jobs", headers=headers, json=one_payload)
                self.assertEqual(ok_res.status_code, 201)
                launch_mock.assert_called_once()

    def test_workbench_tracks_non_video_active_work_and_marks_activity_only_on_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / ".env"
            env_path.write_text(
                "VIDEO_PROVIDER=comfyui\nCOMFYUI_URL=https://demo.ngrok-free.dev\nCOMFYUI_API_KEY=comfy-secret-token-123\n",
                encoding="utf-8",
            )
            app = server.create_app(storage=root / ".workbench", env_path=env_path)
            client = app.test_client()
            jobs = app.extensions["jobs"]
            runtime = app.extensions["colab_runtime"]
            self.addCleanup(runtime.stop_keepalive_monitor)

            # Passive GET /api/runtime/status must not refresh local activity grace
            with mock.patch.object(runtime, "status", return_value={"state": "online", "logs": [], "error": None}):
                res = client.get("/api/runtime/status")
                self.assertEqual(res.status_code, 200)
            self.assertIsNone(runtime._last_activity_at)
            self.assertEqual(runtime._local_busy_provider, jobs.has_active_work)

            # Running storyboard frames job counts as active work (keeps Colab warm) even though not active video work
            jobs.records["frames-1"] = {
                "id": "frames-1",
                "kind": "film",
                "demo": False,
                "operation": "frames",
                "status": "running",
                "output_dir": str(root / "frames-1"),
                "created_at": "2026-11-22T00:00:00+0000",
            }
            self.assertTrue(jobs.has_active_work())
            self.assertFalse(jobs.has_active_video_work())

            # Real user POST mutation on /api/runtime/start marks local activity
            with mock.patch.object(runtime, "start_async", return_value={"state": "provisioning", "logs": [], "error": None}):
                start_res = client.post("/api/runtime/start", headers={"X-Workbench": "1"}, json={})
                self.assertEqual(start_res.status_code, 200)
            self.assertIsNotNone(runtime._last_activity_at)

            # POST /api/runtime/stop must refuse unforced stop while a video launch probe is in flight
            jobs._probing_launch = True
            try:
                fake_bin = root / "colab"
                fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                fake_bin.chmod(0o755)
                with mock.patch.object(server.colab_runtime, "find_colab_bin", return_value=str(fake_bin)), \
                     mock.patch.object(server.colab_runtime, "probe_remote_queue_count", return_value=0), \
                     mock.patch.object(runtime, "status", return_value={"state": "online", "logs": []}), \
                     mock.patch.object(runtime, "_run_cmd_for_slot") as stop_cmd:
                    stop_res = client.post("/api/runtime/stop", headers={"X-Workbench": "1"}, json={"force": False})
                self.assertEqual(stop_res.status_code, 200)
                stop_payload = stop_res.get_json()
                self.assertFalse(stop_payload["stopped"])
                self.assertTrue(stop_payload["confirmation_required"])
                self.assertTrue(stop_payload["local_busy"])
                stop_cmd.assert_not_called()
            finally:
                jobs._probing_launch = False

            # p3/red-team#1: ensure_video_runtime refuses video launch while managed slot is stopping
            runtime._slots["primary"].active_state = "stopping"
            try:
                with mock.patch.object(server.colab_runtime, "probe_comfyui", return_value={"online": True}) as probe_mock:
                    with self.assertRaises(InputError) as stop_ctx:
                        jobs.before_video_launch(
                            {"kind": "one", "operation": "pipeline", "demo": False, "output_dir": str(root / "out-stop"), "lang": "en"},
                            {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "https://node1.ngrok-free.dev", "COMFYUI_API_KEY": "comfy-secret-token-123"},
                        )
                self.assertEqual(stop_ctx.exception.key, "comfy_runtime_action")
                probe_mock.assert_not_called()
            finally:
                runtime._slots["primary"].active_state = None

            # fix-check#3: ensure_video_runtime re-checks stopping state AFTER probe_comfyui completes
            def probe_enters_stopping(*args, **kwargs):
                runtime._slots["primary"].active_state = "stopping"
                return {"online": True}
            try:
                with mock.patch.object(server.colab_runtime, "probe_comfyui", side_effect=probe_enters_stopping):
                    with self.assertRaises(InputError) as post_stop_ctx:
                        jobs.before_video_launch(
                            {"kind": "one", "operation": "pipeline", "demo": False, "output_dir": str(root / "out-stop2"), "lang": "en"},
                            {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "https://node1.ngrok-free.dev", "COMFYUI_API_KEY": "comfy-secret-token-123"},
                        )
                self.assertEqual(post_stop_ctx.exception.key, "comfy_runtime_action")
            finally:
                runtime._slots["primary"].active_state = None

            # p3/red-team#3 + fix-check#1: rotating COMFYUI_API_KEY on restart or via POST /api/config strips stale secondary URL
            restart_storage = root / ".workbench-restart"
            restart_env = root / ".env-restart"
            restart_env.write_text(
                "VIDEO_PROVIDER=comfyui\nCOMFYUI_URL=https://node1.ngrok-free.dev\nCOMFYUI_API_KEY=shared-comfy-key-1234\n",
                encoding="utf-8",
            )
            seed_rt = server.colab_runtime.ColabRuntimeManager(restart_storage)
            seed_rt._set_remembered_node(
                "secondary",
                {
                    "session": "comfy-a100-2",
                    "url": "https://stale-99.trycloudflare.com",
                    "key_fp": server.colab_runtime.comfy.key_fingerprint("comfy-secret-token-123"),
                },
            )
            restarted_app = server.create_app(storage=restart_storage, env_path=restart_env)
            self.addCleanup(restarted_app.extensions["colab_runtime"].stop_keepalive_monitor)
            restarted_cfg = restarted_app.test_client().get("/api/bootstrap").get_json()["config"]
            self.assertEqual(restarted_cfg["COMFYUI_URLS"], ["https://node1.ngrok-free.dev"])

            # POST /api/config rotating COMFYUI_API_KEY (including UI FormData that also submits pre-filled COMFYUI_URL)
            # strips remembered secondary URL from env and .env, while persist=False leaves .env and colab-nodes.json untouched
            restarted_rt = restarted_app.extensions["colab_runtime"]
            restarted_rt._set_remembered_node(
                "secondary",
                {
                    "session": "comfy-a100-2",
                    "url": "https://live-sec-55.trycloudflare.com",
                    "key_fp": server.colab_runtime.comfy.key_fingerprint("shared-comfy-key-1234"),
                },
            )
            # 1) persist=False rotates key in memory only (leaves .env and colab-nodes.json untouched)
            np_res = restarted_app.test_client().post(
                "/api/config",
                headers={"X-Workbench": "1"},
                json={"COMFYUI_API_KEY": "comfy-secret-token-123", "persist": False},
            )
            self.assertEqual(np_res.status_code, 200)
            self.assertEqual(np_res.get_json()["COMFYUI_URLS"], ["https://node1.ngrok-free.dev"])
            self.assertIsNotNone(restarted_rt.remembered_node("secondary"))
            self.assertEqual(
                config.read_env_file(restart_env)["COMFYUI_URL"],
                "https://node1.ngrok-free.dev,https://live-sec-55.trycloudflare.com",
            )
            # 1b) Unrelated persisted save (e.g. COLAB_GPU) after persist=False must NOT wipe .env secondary URL or colab-nodes.json
            unrel_res = restarted_app.test_client().post(
                "/api/config",
                headers={"X-Workbench": "1"},
                json={"COLAB_GPU": "A100", "persist": True},
            )
            self.assertEqual(unrel_res.status_code, 200)
            self.assertIsNotNone(restarted_rt.remembered_node("secondary"))
            self.assertEqual(
                config.read_env_file(restart_env)["COMFYUI_URL"],
                "https://node1.ngrok-free.dev,https://live-sec-55.trycloudflare.com",
            )
            # 1b) persist=True submitting COMFYUI_URL with sec_url (no key) after persist=False keeps .env & remembered node in sync
            sync_url_res = restarted_app.test_client().post(
                "/api/config",
                headers={"X-Workbench": "1"},
                json={
                    "COMFYUI_URL": "https://node1.ngrok-free.dev,https://live-sec-55.trycloudflare.com",
                    "persist": True,
                },
            )
            self.assertEqual(sync_url_res.status_code, 200)
            self.assertEqual(sync_url_res.get_json()["COMFYUI_URLS"], ["https://node1.ngrok-free.dev"])
            self.assertIsNotNone(restarted_rt.remembered_node("secondary"))
            self.assertEqual(
                config.read_env_file(restart_env)["COMFYUI_URL"],
                "https://node1.ngrok-free.dev,https://live-sec-55.trycloudflare.com",
            )
            # 2) persist=True saving the SAME rotated key (omitting COMFYUI_URL) still strips sec_url from .env & forgets node
            persist_same_res = restarted_app.test_client().post(
                "/api/config",
                headers={"X-Workbench": "1"},
                json={"COMFYUI_API_KEY": "comfy-secret-token-123", "persist": True},
            )
            self.assertEqual(persist_same_res.status_code, 200)
            self.assertEqual(persist_same_res.get_json()["COMFYUI_URLS"], ["https://node1.ngrok-free.dev"])
            self.assertIsNone(restarted_rt.remembered_node("secondary"))
            self.assertEqual(config.read_env_file(restart_env)["COMFYUI_URL"], "https://node1.ngrok-free.dev")

            # 3) UI FormData persist=True rotating key WHILE also submitting pre-filled COMFYUI_URL containing sec_url
            restarted_rt._set_remembered_node(
                "secondary",
                {
                    "session": "comfy-a100-2",
                    "url": "https://live-sec-55.trycloudflare.com",
                    "key_fp": server.colab_runtime.comfy.key_fingerprint("comfy-secret-token-123"),
                },
            )
            rot_res = restarted_app.test_client().post(
                "/api/config",
                headers={"X-Workbench": "1"},
                json={
                    "COMFYUI_URL": "https://node1.ngrok-free.dev,https://live-sec-55.trycloudflare.com",
                    "COMFYUI_API_KEY": "shared-pool-key-1234",
                    "persist": True,
                },
            )
            self.assertEqual(rot_res.status_code, 200)
            self.assertEqual(rot_res.get_json()["COMFYUI_URLS"], ["https://node1.ngrok-free.dev"])
            self.assertIsNone(restarted_rt.remembered_node("secondary"))
            self.assertEqual(config.read_env_file(restart_env)["COMFYUI_URL"], "https://node1.ngrok-free.dev")

            # 4) Process-env COMFYUI_API_KEY (not in .env): unrelated persist=True must preserve .env URL & remembered node
            env_key_env = root / ".env-process-key"
            env_key_env.write_text(
                "VIDEO_PROVIDER=comfyui\n"
                "COMFYUI_URL=https://node1.ngrok-free.dev,https://live-sec-55.trycloudflare.com\n",
                encoding="utf-8",
            )
            env_key_storage = root / ".workbench-process-key"
            with mock.patch.dict(os.environ, {"COMFYUI_API_KEY": "shared-pool-key-1234"}, clear=False):
                env_app = server.create_app(storage=env_key_storage, env_path=env_key_env)
                env_rt = env_app.extensions["colab_runtime"]
                self.addCleanup(env_rt.stop_keepalive_monitor)
                env_rt._set_remembered_node(
                    "secondary",
                    {
                        "session": "comfy-a100-2",
                        "url": "https://live-sec-55.trycloudflare.com",
                        "key_fp": server.colab_runtime.comfy.key_fingerprint("shared-pool-key-1234"),
                    },
                )
                env_unrel = env_app.test_client().post(
                    "/api/config",
                    headers={"X-Workbench": "1"},
                    json={"COLAB_GPU": "A100", "persist": True},
                )
                self.assertEqual(env_unrel.status_code, 200)
                self.assertIsNotNone(env_rt.remembered_node("secondary"))
                self.assertEqual(
                    config.read_env_file(env_key_env)["COMFYUI_URL"],
                    "https://node1.ngrok-free.dev,https://live-sec-55.trycloudflare.com",
                )

            # 5) ROOT/.env with stale process-env COMFYUI_API_KEY overridden by persisted UI key: unrelated persist=True preserves secondary
            root_env_dir = root / "prod-root"
            root_env_dir.mkdir()
            prod_env = root_env_dir / ".env"
            prod_env.write_text(
                "VIDEO_PROVIDER=comfyui\n"
                "COMFYUI_URL=https://node1.ngrok-free.dev\n",
                encoding="utf-8",
            )
            with mock.patch.object(config, "ROOT", root_env_dir), \
                 mock.patch.dict(os.environ, {"COMFYUI_API_KEY": "comfy-secret-token-123"}, clear=False):
                prod_app = server.create_app(storage=root / ".workbench-prod-root", env_path=prod_env)
                prod_rt = prod_app.extensions["colab_runtime"]
                self.addCleanup(prod_rt.stop_keepalive_monitor)
                rot_prod = prod_app.test_client().post(
                    "/api/config",
                    headers={"X-Workbench": "1"},
                    json={"COMFYUI_API_KEY": "shared-pool-key-1234", "persist": True},
                )
                self.assertEqual(rot_prod.status_code, 200)
                prod_rt._set_remembered_node(
                    "secondary",
                    {
                        "session": "comfy-a100-2",
                        "url": "https://live-sec-55.trycloudflare.com",
                        "key_fp": server.colab_runtime.comfy.key_fingerprint("shared-pool-key-1234"),
                    },
                )
                prod_unrel = prod_app.test_client().post(
                    "/api/config",
                    headers={"X-Workbench": "1"},
                    json={"COLAB_GPU": "A100", "persist": True},
                )
                self.assertEqual(prod_unrel.status_code, 200)
                self.assertIsNotNone(prod_rt.remembered_node("secondary"))
                self.assertEqual(
                    config.read_env_file(prod_env)["COMFYUI_URL"],
                    "https://node1.ngrok-free.dev,https://live-sec-55.trycloudflare.com",
                )

    def test_secondary_colab_node_endpoints_sync_env_and_auto_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / ".env"
            env_path.write_text(
                "\n".join([
                    "VIDEO_PROVIDER=comfyui",
                    "COMFYUI_URL=https://node1.ngrok-free.dev",
                    "COMFYUI_API_KEY=shared-comfy-key-1234",
                ]) + "\n",
                encoding="utf-8",
            )
            storage = root / ".workbench"
            app = server.create_app(storage=storage, env_path=env_path)
            client = app.test_client()
            headers = {"X-Workbench": "1"}
            runtime = app.extensions["colab_runtime"]
            self.addCleanup(runtime.stop_keepalive_monitor)

            # Invalid slot rejected with 400
            self.assertEqual(client.post("/api/runtime/start", headers=headers, json={"slot": "third"}).status_code, 400)

            # POST /api/runtime/start with slot="secondary" routes cleanly
            with mock.patch.object(runtime, "start_async", return_value={"state": "online", "managed_nodes": [], "logs": []}) as start_mock:
                res = client.post("/api/runtime/start", headers=headers, json={"slot": "secondary"})
                self.assertEqual(res.status_code, 200)
                self.assertEqual(start_mock.call_args.kwargs.get("slot"), "secondary")

            # POST /api/runtime/drive-ack with slot="secondary" routes cleanly
            with mock.patch.object(runtime, "submit_drive_authorized", return_value={"accepted": True, "slot": "secondary", "state": "starting_comfy"}) as ack_mock, \
                 mock.patch.object(runtime, "status", return_value={"state": "online", "managed_nodes": [], "logs": []}):
                ack = client.post("/api/runtime/drive-ack", headers=headers, json={"slot": "secondary"})
                self.assertEqual(ack.status_code, 200)
                self.assertEqual(ack_mock.call_args.kwargs.get("slot"), "secondary")

            # Remembering a random URL automatically updates live config, .env, and auto-concurrency (1 -> 2)
            rand_1 = "https://wind-11.trycloudflare.com"
            rand_2 = "https://rain-22.trycloudflare.com"
            sec_fp = server.colab_runtime.comfy.key_fingerprint("shared-comfy-key-1234")
            runtime._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": rand_1, "gpu": "G4", "tunnel": "cloudflared", "key_fp": sec_fp})
            cfg1 = client.get("/api/bootstrap").get_json()["config"]
            self.assertEqual(cfg1["COMFYUI_URLS"], ["https://node1.ngrok-free.dev", rand_1])
            self.assertEqual(cfg1["effective_comfyui_concurrency"], 2)
            self.assertTrue(cfg1["keys"]["COMFYUI_API_KEY"])
            self.assertEqual(config.read_env_file(env_path)["COMFYUI_URL"], f"https://node1.ngrok-free.dev,{rand_1}")

            # New ephemeral URL replaces old one (never accumulates dead URLs)
            runtime._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": rand_2, "gpu": "G4", "tunnel": "cloudflared", "key_fp": sec_fp})
            cfg2 = client.get("/api/bootstrap").get_json()["config"]
            self.assertEqual(cfg2["COMFYUI_URLS"], ["https://node1.ngrok-free.dev", rand_2])
            self.assertEqual(config.read_env_file(env_path)["COMFYUI_URL"], f"https://node1.ngrok-free.dev,{rand_2}")

            # Workbench restart reloads remembered secondary URL even if .env only had primary
            env_path.write_text(
                "VIDEO_PROVIDER=comfyui\nCOMFYUI_URL=https://node1.ngrok-free.dev\nCOMFYUI_API_KEY=shared-comfy-key-1234\n",
                encoding="utf-8",
            )
            app2 = server.create_app(storage=storage, env_path=env_path)
            self.addCleanup(app2.extensions["colab_runtime"].stop_keepalive_monitor)
            cfg_restarted = app2.test_client().get("/api/bootstrap").get_json()["config"]
            self.assertEqual(cfg_restarted["COMFYUI_URLS"], ["https://node1.ngrok-free.dev", rand_2])

            # Clearing secondary removes the dead ephemeral URL and reverts concurrency back to 1
            app2.extensions["colab_runtime"]._set_remembered_node("secondary", None)
            cfg3 = app2.test_client().get("/api/bootstrap").get_json()["config"]
            self.assertEqual(cfg3["COMFYUI_URLS"], ["https://node1.ngrok-free.dev"])
            self.assertEqual(cfg3["effective_comfyui_concurrency"], 1)

    def test_windows_package_includes_comfy_modules_workflow_and_start_colab(self):
        files = package_windows.package_files(config.ROOT)
        for expected in (
            "h3ref/comfy.py",
            "h3ref/colab_runtime.py",
            "workflows/comfy-h3-ref2va.json",
            "scripts/start_colab.py",
        ):
            self.assertIn(expected, files)
            self.assertTrue(files[expected].is_file())

    # Value: protects=workbench live_progress projection and ComfyUI segment metadata cleanup on reconciliation; fails_when=Jobs.snapshot leaks live_progress after completion, segments.view exposes non-pending live_progress, or preview_reconciled_segments drops provider/comfyui_url on terminal failure or leaves stale comfyui_key_fp/comfyui_url on reset; why_new=no Python workbench test previously asserted live_progress projection or ComfyUI metadata reconciliation; seam=none; no_claim=does not test browser DOM rendering
    def test_workbench_snapshot_and_segment_reconcile_live_progress_and_comfy_metadata(self):
        from workbench.jobs import Jobs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pic = _make_png(root / "actor.png")
            manager = Jobs(config.ROOT, root / ".workbench")
            self.addCleanup(manager.close)

            # 1. Single-clip ("one") job exposes live_progress only while running
            one_out = root / "one-live"
            one_out.mkdir(parents=True)
            one_progress = {"stage": "sampling", "step": 5, "max_steps": 20, "vram_used_gb": 30.0, "vram_total_gb": 80.0}
            (one_out / "state.json").write_text(json.dumps({"task": "pid-one", "live_progress": one_progress}), encoding="utf-8")
            manager.records["one-1"] = {
                "id": "one-1", "kind": "one", "demo": False, "operation": "pipeline",
                "status": "running", "output_dir": str(one_out), "created_at": "2026-11-23T00:00:00+0000",
            }
            self.assertEqual(manager.snapshot("one-1")["live_progress"], one_progress)
            manager.records["one-1"]["status"] = "succeeded"
            self.assertIsNone(manager.snapshot("one-1")["live_progress"])

            # 2. Film job surfaces active segment live_progress in snapshot + segments.view only while task_pending & running
            film_out = root / "film-live"
            (film_out / "inputs").mkdir(parents=True)
            seg_progress = {"stage": "sampling", "step": 8, "max_steps": 20, "node_label": "node-b.example.com"}
            film_req = {
                "kind": "film", "demo": False, "operation": "pipeline", "frames": "none",
                "output_dir": str(film_out), "lang": "en",
                "film": {
                    "aspect": "9:16",
                    "cast": [{"name": "A", "look": "tall", "aliases": [], "picture": str(pic)}],
                    "shots": [{"start": 0.0, "end": 5.0, "action": "A waves", "camera": "wide", "line": ""}],
                },
            }
            (film_out / "inputs" / "request.json").write_text(json.dumps(film_req), encoding="utf-8")
            (film_out / "inputs" / "film.json").write_text(json.dumps(film_req["film"]), encoding="utf-8")
            (film_out / "state.json").write_text(json.dumps({
                "frames": {},
                "segs": {
                    "0": {
                        "h3": "six section h3", "task": "pid-seg-1", "provider": "comfyui",
                        "comfyui_url": "https://node-b.example.com", "live_progress": seg_progress,
                    },
                },
            }), encoding="utf-8")
            manager.records["film-1"] = {
                "id": "film-1", "kind": "film", "demo": False, "operation": "pipeline",
                "status": "running", "output_dir": str(film_out), "created_at": "2026-11-23T00:00:00+0000",
            }
            snap_running = manager.snapshot("film-1")
            self.assertEqual(snap_running["live_progress"], {"segment": 1, **seg_progress})
            self.assertEqual(snap_running["segments"][0]["live_progress"], seg_progress)

            manager.records["film-1"]["status"] = "succeeded"
            self.assertIsNone(manager.snapshot("film-1")["live_progress"])

            # Non-pending segment (no task) must not expose stale live_progress in segments.view
            (film_out / "state.json").write_text(json.dumps({
                "frames": {},
                "segs": {"0": {"h3": "six section h3", "live_progress": seg_progress}},
            }), encoding="utf-8")
            cards_no_task = segments.view(film_req, [], True)
            self.assertIsNone(cards_no_task[0]["live_progress"])

            # 3. preview_reconciled_segments preserves provider/comfyui_url and drops live_progress on terminal_failed,
            #    and strips stale provider/comfyui_url/comfyui_key_fp/live_progress on reset
            rec_out = root / "film-reconcile"
            staged_dir = rec_out / "inputs" / "segment-rev1"
            staged_dir.mkdir(parents=True)
            (rec_out / "inputs" / "segment-edit.json").write_text(json.dumps({
                "number": 1, "revision": "rev1", "applied": False, "generated": False,
            }), encoding="utf-8")
            (staged_dir / "state.json").write_text(json.dumps({
                "segs": {
                    "0": {
                        "task": "pid-dead", "terminal_failed": True, "error": "OOM",
                        "provider": "comfyui", "comfyui_url": "https://node-a.example.com",
                        "live_progress": seg_progress,
                    },
                },
            }), encoding="utf-8")
            base_records = {"0": {"h3": "old", "live_progress": seg_progress}}
            reconciled_fail, changed_fail, _ = segments.preview_reconciled_segments(rec_out, base_records)
            self.assertTrue(changed_fail)
            self.assertEqual(reconciled_fail["0"]["provider"], "comfyui")
            self.assertEqual(reconciled_fail["0"]["comfyui_url"], "https://node-a.example.com")
            self.assertNotIn("live_progress", reconciled_fail["0"])

            # Abandoned unsubmitted staged attempt resets stale ComfyUI fields when reconciling
            (staged_dir / "state.json").write_text(json.dumps({"segs": {"0": {}}}), encoding="utf-8")
            stale_records = {
                "0": {
                    "h3": "old", "task": "pid-old", "terminal_failed": True, "provider": "comfyui",
                    "comfyui_url": "https://node-old.example.com", "comfyui_key_fp": "deadbeef12345678",
                    "live_progress": seg_progress,
                },
            }
            (rec_out / "inputs" / "segment-edit.json").write_text(json.dumps({
                "number": 1, "revision": "rev1", "applied": False, "generated": False,
                "previous": stale_records["0"],
            }), encoding="utf-8")
            reconciled_reset, changed_reset, _ = segments.preview_reconciled_segments(rec_out, stale_records)
            self.assertTrue(changed_reset)
            for cleared_key in ("task", "provider", "comfyui_url", "comfyui_key_fp", "live_progress", "terminal_failed"):
                self.assertNotIn(cleared_key, reconciled_reset["0"])


if __name__ == "__main__":
    unittest.main()
