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
            runtime._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": rand_1, "gpu": "G4", "tunnel": "cloudflared"})
            cfg1 = client.get("/api/bootstrap").get_json()["config"]
            self.assertEqual(cfg1["COMFYUI_URLS"], ["https://node1.ngrok-free.dev", rand_1])
            self.assertEqual(cfg1["effective_comfyui_concurrency"], 2)
            self.assertTrue(cfg1["keys"]["COMFYUI_API_KEY"])
            self.assertEqual(config.read_env_file(env_path)["COMFYUI_URL"], f"https://node1.ngrok-free.dev,{rand_1}")

            # New ephemeral URL replaces old one (never accumulates dead URLs)
            runtime._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": rand_2, "gpu": "G4", "tunnel": "cloudflared"})
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


if __name__ == "__main__":
    unittest.main()
