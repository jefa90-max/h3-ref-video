"""Stage 2 tests: h3ref/http.py extensions, h3ref/comfy.py graph/upload/submit/poll, and dual-backend routing."""
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from h3ref import comfy, config, film, http, rh

_CLI_SPEC = importlib.util.spec_from_file_location("h3ref_cli", config.ROOT / "h3ref.py")
cli_main = importlib.util.module_from_spec(_CLI_SPEC)
_CLI_SPEC.loader.exec_module(cli_main)


class ComfyBackendTest(unittest.TestCase):
    def setUp(self):
        comfy._MISSING_COUNTS.clear()

    def test_dynamic_reference_graph_for_1_2_and_9_images_and_bounds(self):
        g1 = comfy.graph("prompt one", ["hash1.png"], 6, "9:16", 42)
        load_nodes_1 = [nid for nid, n in g1.items() if n.get("class_type") == "LoadImage"]
        self.assertEqual(load_nodes_1, ["137"])
        self.assertEqual(g1["137"]["inputs"]["image"], "hash1.png")
        ref_keys_1 = [k for k in g1[comfy.SIZE]["inputs"] if k.startswith("ref_images.")]
        self.assertEqual(ref_keys_1, ["ref_images.ref_image_0"])
        self.assertEqual((g1[comfy.SIZE]["inputs"]["width"], g1[comfy.SIZE]["inputs"]["height"]), (768, 1344))
        self.assertEqual(g1[comfy.PROMPT]["inputs"]["value"], "prompt one")
        self.assertEqual(g1[comfy.DURATION]["inputs"]["value"], 6.0)
        self.assertEqual(g1[comfy.SEED]["inputs"]["noise_seed"], 42)

        g2 = comfy.graph("prompt two", ["a.png", "b.jpg"], 12, "16:9", 99)
        load_nodes_2 = [nid for nid, n in g2.items() if n.get("class_type") == "LoadImage"]
        self.assertEqual(load_nodes_2, ["137", "139"])
        self.assertEqual((g2[comfy.SIZE]["inputs"]["width"], g2[comfy.SIZE]["inputs"]["height"]), (1344, 768))
        ref_keys_2 = sorted(k for k in g2[comfy.SIZE]["inputs"] if k.startswith("ref_images."))
        self.assertEqual(ref_keys_2, ["ref_images.ref_image_0", "ref_images.ref_image_1"])

        names9 = [f"img{i}.png" for i in range(9)]
        g9 = comfy.graph("prompt nine", names9, 15, "1:1", 7)
        load_nodes_9 = [nid for nid in comfy.IMAGES if nid in g9]
        self.assertEqual(len(load_nodes_9), 9)
        self.assertEqual((g9[comfy.SIZE]["inputs"]["width"], g9[comfy.SIZE]["inputs"]["height"]), (992, 992))
        ref_keys_9 = [k for k in g9[comfy.SIZE]["inputs"] if k.startswith("ref_images.")]
        self.assertEqual(len(ref_keys_9), 9)

        # RunningHub graph keeps 544p untouched
        rh_g = rh.graph("prompt rh", ["a.png"], 6, "9:16", 42)
        self.assertEqual((rh_g[rh.SIZE]["inputs"]["width"], rh_g[rh.SIZE]["inputs"]["height"]), (544, 960))

        for bad_images in ([], ["x.png"] * 10):
            with self.assertRaises(ValueError):
                comfy.graph("p", bad_images, 6, "9:16", 1)
        with self.assertRaises(ValueError):
            comfy.graph("p", ["a.png"], 6, "4:3", 1)

    def test_comfy_presets_map_lora_and_steps_atomically(self):
        g4 = comfy.graph("p", ["a.png"], 5, "9:16", 1, preset="4step")
        self.assertTrue(g4[comfy.ENABLE_LORA]["inputs"]["value"])
        self.assertEqual(g4[comfy.TURBO_STEPS]["inputs"]["value"], 4)
        self.assertEqual(
            g4[comfy.LORA]["inputs"]["lora_name"],
            "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
        )

        g8 = comfy.graph("p", ["a.png"], 5, "9:16", 1, preset="8step")
        self.assertTrue(g8[comfy.ENABLE_LORA]["inputs"]["value"])
        self.assertEqual(g8[comfy.TURBO_STEPS]["inputs"]["value"], 8)
        self.assertEqual(
            g8[comfy.LORA]["inputs"]["lora_name"],
            "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        )

        g20 = comfy.graph("p", ["a.png"], 5, "9:16", 1, preset="full20")
        self.assertFalse(g20[comfy.ENABLE_LORA]["inputs"]["value"])
        self.assertEqual(g20[comfy.FULL_STEPS]["inputs"]["value"], 20)

        with self.assertRaises(ValueError):
            comfy.graph("p", ["a.png"], 5, "9:16", 1, preset="invalid")

    def test_real_curl_loopback_upload_and_download_with_unicode_path_and_auth_headers(self):
        received = {}
        video_payload = b"0" * 2048

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                received["upload_path"] = self.path
                received["upload_auth"] = self.headers.get("Authorization")
                received["upload_ngrok"] = self.headers.get("ngrok-skip-browser-warning")
                received["upload_ctype"] = self.headers.get("Content-Type")
                received["upload_body"] = self.rfile.read(length)
                resp = b'{"name": "uploaded_hash.png", "subfolder": "", "type": "input"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def do_GET(self):
                received["dl_path"] = self.path
                received["dl_auth"] = self.headers.get("Authorization")
                received["dl_ngrok"] = self.headers.get("ngrok-skip-browser-warning")
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(video_payload)))
                self.end_headers()
                self.wfile.write(video_payload)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        secret = "h3c_secret_tunnel_token_abc"
        with tempfile.TemporaryDirectory() as tmp:
            tricky_dir = Path(tmp) / "中文 路径;测试,目录"
            tricky_dir.mkdir(parents=True)
            img = tricky_dir / "s1-1 角色图.PNG"
            Image.new("RGB", (8, 8), "green").save(img)
            expected_prefix = hashlib.sha256(img.read_bytes()).hexdigest()[:16] + ".png"

            with patch.dict(os.environ, {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": f"http://127.0.0.1:{port}",
                "COMFYUI_API_KEY": secret,
            }, clear=False):
                remote_name = comfy.upload(img)
                self.assertEqual(remote_name, "uploaded_hash.png")
                out_mp4 = Path(tmp) / "clip.mp4"
                comfy.download(f"http://127.0.0.1:{port}/view?filename=out.mp4&type=output", out_mp4)
                self.assertEqual(out_mp4.read_bytes(), video_payload)

        self.assertEqual(received["upload_path"], "/upload/image")
        self.assertEqual(received["upload_auth"], f"Bearer {secret}")
        self.assertEqual(received["upload_ngrok"], "1")
        body_text = received["upload_body"].decode("latin-1")
        self.assertIn('name="image"', body_text)
        self.assertIn(f'filename="{expected_prefix}"', body_text)
        self.assertNotIn("角色图", body_text)
        self.assertEqual(received["dl_auth"], f"Bearer {secret}")
        self.assertEqual(received["dl_ngrok"], "1")

    def test_windows_nt_upload_escapes_backslashes_and_keeps_key_out_of_argv(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input", "")
            return MagicMock(returncode=0, stdout='{"name": "abcd1234abcd1234.png"}\n200', stderr="")

        win_path = PureWindowsPath(r"D:\工具\H3 工作台\jobs\frames\s1-1.png")
        secret = "h3c_win_secret_token_999"
        with patch("subprocess.run", side_effect=fake_run), patch.object(http, "os", SimpleNamespace(name="nt")):
            res = http.upload(
                "https://demo.ngrok-free.dev/upload/image",
                secret,
                win_path,
                fields={"type": "input", "overwrite": "true"},
                file_field_name="image",
                filename="abcd1234abcd1234.png",
                extra_headers={"ngrok-skip-browser-warning": "1"},
            )
        self.assertEqual(res["name"], "abcd1234abcd1234.png")
        self.assertFalse(any(secret in str(a) for a in captured["cmd"]))
        self.assertIn(r"D:\\工具\\H3 工作台\\jobs\\frames\\s1-1.png", captured["input"])
        self.assertNotIn(r"D:\\\\工具", captured["input"])
        self.assertIn('filename=\\"abcd1234abcd1234.png\\"', captured["input"])
        self.assertIn('ngrok-skip-browser-warning: 1', captured["input"])

    def test_poll_handles_images_and_videos_outputs_queue_and_missing_prompt_fast_fail(self):
        env_vars = {
            "VIDEO_PROVIDER": "comfyui",
            "COMFYUI_URL": "https://demo.ngrok-free.dev",
            "COMFYUI_API_KEY": "h3c_poll_key",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            # 1. SaveVideo PreviewVideo output under 'images'
            hist_images = {
                "pid-img": {
                    "outputs": {
                        "92": {
                            "images": [{"filename": "MiniMax_H3_00001_.mp4", "subfolder": "video", "type": "output"}],
                            "animated": [True],
                        }
                    },
                    "status": {
                        "status_str": "success",
                        "completed": True,
                        "messages": [
                            ["execution_start", {"timestamp": 10000}],
                            ["execution_success", {"timestamp": 52500}],
                        ],
                    },
                }
            }
            with patch.object(comfy.http, "request", return_value=hist_images):
                done1 = comfy.poll("pid-img")
            self.assertEqual(done1["state"], "done")
            self.assertEqual(done1["cny"], 0.0)
            self.assertAlmostEqual(done1["run_seconds"], 42.5)
            self.assertEqual(
                done1["url"],
                "https://demo.ngrok-free.dev/view?filename=MiniMax_H3_00001_.mp4&type=output&subfolder=video",
            )

            # 2. Output under 'videos'
            hist_videos = {
                "pid-vid": {
                    "outputs": {
                        "92": {"videos": [{"filename": "clip.mp4", "subfolder": "", "type": "output"}]}
                    },
                    "status": {"status_str": "success", "completed": True},
                }
            }
            with patch.object(comfy.http, "request", return_value=hist_videos):
                done2 = comfy.poll("pid-vid")
            self.assertEqual(done2["state"], "done")
            self.assertEqual(done2["url"], "https://demo.ngrok-free.dev/view?filename=clip.mp4&type=output")

            # 3. In queue_pending then queue_running
            with patch.object(comfy.http, "request", side_effect=[
                {},
                {"queue_running": [], "queue_pending": [[1, "pid-q", {}]]},
                {},
                {"queue_running": [[1, "pid-q", {}]], "queue_pending": []},
            ]):
                q1 = comfy.poll("pid-q")
                q2 = comfy.poll("pid-q")
            self.assertEqual(q1, {"state": "running", "queued": True})
            self.assertEqual(q2, {"state": "running", "queued": False})

            # 4. Missing from both history and queue twice in a row -> fast fail!
            with patch.object(comfy.http, "request", side_effect=[
                {}, {"queue_running": [], "queue_pending": []},
                {}, {"queue_running": [], "queue_pending": []},
            ]):
                miss1 = comfy.poll("pid-lost")
                miss2 = comfy.poll("pid-lost")
            self.assertEqual(miss1["state"], "running")
            self.assertEqual(miss2["state"], "failed")
            self.assertIn("pid-lost", miss2["error"])

    def test_dual_backend_routing_and_cross_provider_resume_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = root / "ref.png"
            Image.new("RGB", (8, 8), "blue").save(img)
            story = {"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "Walk"}]}
            seg = {"idx": 0, "start": 0.0, "end": 5.0, "seconds": 5, "shots": story["shots"]}

            # A) Submit under comfyui, interrupt poll, switch env to runninghub -> must resume via comfy!
            out_a = root / "job-comfy"
            job_a = film.Job(out_a)
            (out_a / "clips").mkdir(parents=True)
            job_a.put("segs", "0", h3="six section h3", zh="中文")

            with patch.dict(os.environ, {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": "h3c_test",
            }, clear=False), \
                 patch.object(comfy, "upload", return_value="abc.png") as c_up, \
                 patch.object(comfy, "submit_with_retry", return_value="comfy-pid-1") as c_sub, \
                 patch.object(comfy, "poll", side_effect=RuntimeError("network blip")):
                with self.assertRaisesRegex(RuntimeError, "network blip"):
                    film.gen_segment(job_a, story, seg, [{"kind": "cast", "path": str(img)}], reference_paths=[img])
            self.assertEqual(c_up.call_count, 1)
            self.assertEqual(c_sub.call_count, 1)
            self.assertEqual(film.Job(out_a).state["segs"]["0"]["provider"], "comfyui")

            # Now switch VIDEO_PROVIDER to runninghub and resume job_a -> must poll comfy, NOT rh!
            with patch.dict(os.environ, {"VIDEO_PROVIDER": "runninghub"}, clear=False), \
                 patch.object(rh, "poll", side_effect=AssertionError("Cross-polled RunningHub for ComfyUI task!")), \
                 patch.object(comfy, "poll", return_value={"state": "done", "url": "https://demo.ngrok-free.dev/view?filename=v.mp4", "cny": 0.0, "run_seconds": 30.0}) as c_poll, \
                 patch.object(comfy, "download", side_effect=lambda url, out: out.write_bytes(b"mp4bytes")) as c_dl, \
                 patch.object(film.media, "last_frame"):
                rec_done = film.gen_segment(film.Job(out_a), story, seg, [{"kind": "cast", "path": str(img)}], reference_paths=[img])
            self.assertEqual(c_poll.call_count, 1)
            self.assertEqual(c_dl.call_count, 1)
            self.assertEqual(rec_done["cny"], 0.0)
            self.assertEqual(rec_done["provider"], "comfyui")

            # B) Legacy RunningHub task (no 'provider' key in state) resumed under VIDEO_PROVIDER=comfyui -> must poll rh!
            out_b = root / "job-rh-legacy"
            job_b = film.Job(out_b)
            (out_b / "clips").mkdir(parents=True)
            job_b.put("segs", "0", h3="six section h3", zh="中文", task="rh-legacy-99")
            with patch.dict(os.environ, {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
            }, clear=False), \
                 patch.object(comfy, "poll", side_effect=AssertionError("Cross-polled ComfyUI for legacy RH task!")), \
                 patch.object(rh, "poll", return_value={"state": "done", "url": "https://rh.example/v.mp4", "cny": 0.12}) as r_poll, \
                 patch.object(film.http, "download", side_effect=lambda url, out: out.write_bytes(b"rhmp4")), \
                 patch.object(film.media, "last_frame"):
                rec_rh = film.gen_segment(film.Job(out_b), story, seg, [{"kind": "cast", "path": str(img)}], reference_paths=[img])
            self.assertEqual(r_poll.call_count, 1)
            self.assertEqual(rec_rh["cny"], 0.12)

    def test_cli_one_and_account_support_comfyui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = root / "ref.png"
            Image.new("RGB", (8, 8), "red").save(img)
            prompt_file = root / "p.txt"
            prompt_file.write_text(
                "subject_definitions:\n<Subject 1> in <Picture 1>.\n\n"
                "summary:\nWalk.\n\n"
                "retention_analysis:\n<Subject 1>: kept.\n\n"
                "detailed_description:\n[Shot 1] <Subject 1> walks.\n\n"
                "overall_soundscape:\nFootsteps.\n\n"
                "non_diegetic_music:\nN/A\n",
                encoding="utf-8",
            )
            out_dir = root / "one-out"
            args = SimpleNamespace(
                prompt=str(prompt_file), image=[str(img)], seconds=5,
                aspect="9:16", seed=123, check=False, out=str(out_dir),
            )
            with patch.dict(os.environ, {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": "h3c_cli",
            }, clear=False), \
                 patch.object(comfy, "upload", return_value="up.png"), \
                 patch.object(comfy, "submit_with_retry", return_value="pid-cli"), \
                 patch.object(comfy, "poll", return_value={"state": "done", "url": "https://demo.ngrok-free.dev/view?filename=c.mp4", "cny": 0.0, "run_seconds": 18.0}), \
                 patch.object(comfy, "download", side_effect=lambda url, p: p.write_bytes(b"climp4")), \
                 patch.object(cli_main.media, "duration", return_value=5.0), \
                 patch("sys.stdout", new_callable=io.StringIO):
                cli_main.cmd_one(args)
            saved = json.loads((out_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["provider"], "comfyui")
            self.assertEqual(saved["cny"], 0.0)

            out_buf = io.StringIO()
            with patch.dict(os.environ, {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://demo.ngrok-free.dev",
                "COMFYUI_API_KEY": "h3c_cli",
            }, clear=False), \
                 patch.object(comfy.http, "request", side_effect=[
                     {"system": {"comfyui_version": "0.9.0"}, "devices": [{"name": "NVIDIA A100-SXM4-80GB", "vram_total": 80, "vram_free": 70}]},
                     {"queue_running": [[1, "pid-1"]], "queue_pending": []},
                 ]), \
                 patch("sys.argv", ["h3ref.py", "account"]), \
                 patch("sys.stdout", out_buf):
                cli_main.main()
            self.assertIn("NVIDIA A100", out_buf.getvalue())
            self.assertIn("'queue_running': 1", out_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
