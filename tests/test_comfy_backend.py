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
        comfy.clear_upload_cache()

    def test_dynamic_reference_graph_for_1_2_and_9_images_and_bounds(self):
        g1 = comfy.graph("prompt one", ["hash1.png"], 6, "9:16", 42)
        load_nodes_1 = [nid for nid, n in g1.items() if n.get("class_type") == "LoadImage"]
        self.assertEqual(load_nodes_1, ["137"])
        self.assertEqual(g1["137"]["inputs"]["image"], "hash1.png")
        ref_keys_1 = [k for k in g1[comfy.SIZE]["inputs"] if k.startswith("ref_images.")]
        self.assertEqual(ref_keys_1, ["ref_images.ref_image_0"])
        # Default 768p uses 544p pass-1 + 3D latent upscale/refine to 768x1344
        self.assertEqual((g1[comfy.SIZE]["inputs"]["width"], g1[comfy.SIZE]["inputs"]["height"]), (544, 960))
        self.assertEqual((g1[comfy.REFINE_UPSCALER]["inputs"]["width"], g1[comfy.REFINE_UPSCALER]["inputs"]["height"]), (768, 1344))
        self.assertEqual(g1[comfy.REFINE_UPSCALER]["inputs"]["model_name"], comfy.LATENT_UPSCALE_MODEL_NAME)
        self.assertTrue(g1[comfy.REFINE_UPSCALER]["inputs"]["lock_audio"])
        self.assertEqual(g1[comfy.REFINE_UPSCALER]["inputs"]["model"], [comfy.SPARSE_ATTN, 0])
        self.assertEqual(g1[comfy.REFINE_STEPS]["inputs"]["model"], [comfy.SPARSE_ATTN, 0])
        self.assertEqual(g1[comfy.REFINE_STEPS]["inputs"]["steps"], comfy.REFINE_DEFAULT_STEPS)
        self.assertEqual(g1[comfy.REFINE_STEPS]["inputs"]["denoise"], comfy.REFINE_DEFAULT_DENOISE)
        self.assertEqual(g1[comfy.REFINE_SEED]["inputs"]["noise_seed"], 42 + comfy.REFINE_SEED_OFFSET)
        self.assertEqual(g1[comfy.VAE_DECODE]["inputs"]["samples"], [comfy.REFINE_UPSCALER, 0])
        self.assertEqual(g1[comfy.VAE_DECODE_AUDIO]["inputs"]["samples"], [comfy.REFINE_UPSCALER, 0])
        self.assertEqual(g1[comfy.CLIP]["inputs"]["clip_name"], "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
        self.assertEqual(g1[comfy.PROMPT]["inputs"]["value"], "prompt one")
        self.assertEqual(g1[comfy.DURATION]["inputs"]["value"], 6.0)
        self.assertEqual(g1[comfy.SEED]["inputs"]["noise_seed"], 42)

        g2 = comfy.graph("prompt two", ["a.png", "b.jpg"], 12, "16:9", 99)
        load_nodes_2 = [nid for nid, n in g2.items() if n.get("class_type") == "LoadImage"]
        self.assertEqual(load_nodes_2, ["137", "139"])
        self.assertEqual((g2[comfy.SIZE]["inputs"]["width"], g2[comfy.SIZE]["inputs"]["height"]), (960, 544))
        self.assertEqual((g2[comfy.REFINE_UPSCALER]["inputs"]["width"], g2[comfy.REFINE_UPSCALER]["inputs"]["height"]), (1344, 768))
        ref_keys_2 = sorted(k for k in g2[comfy.SIZE]["inputs"] if k.startswith("ref_images."))
        self.assertEqual(ref_keys_2, ["ref_images.ref_image_0", "ref_images.ref_image_1"])

        names9 = [f"img{i}.png" for i in range(9)]
        g9 = comfy.graph("prompt nine", names9, 15, "1:1", 7)
        load_nodes_9 = [nid for nid in comfy.IMAGES if nid in g9]
        self.assertEqual(len(load_nodes_9), 9)
        self.assertEqual((g9[comfy.SIZE]["inputs"]["width"], g9[comfy.SIZE]["inputs"]["height"]), (736, 736))
        self.assertEqual((g9[comfy.REFINE_UPSCALER]["inputs"]["width"], g9[comfy.REFINE_UPSCALER]["inputs"]["height"]), (1024, 1024))
        ref_keys_9 = [k for k in g9[comfy.SIZE]["inputs"] if k.startswith("ref_images.")]
        self.assertEqual(len(ref_keys_9), 9)

        # 1088p tier uses 544p pass-1 + 3D latent upscale/refine to 1088x1920 / 1920x1088 / 1472x1472
        with patch.dict(os.environ, {"COMFYUI_RESOLUTION": "1088p"}, clear=False):
            g_1088_p = comfy.graph("p", ["a.png"], 6, "9:16", 11)
            g_1088_l = comfy.graph("p", ["a.png"], 6, "16:9", 11)
            g_1088_s = comfy.graph("p", ["a.png"], 6, "1:1", 11)
        self.assertEqual((g_1088_p[comfy.SIZE]["inputs"]["width"], g_1088_p[comfy.SIZE]["inputs"]["height"]), (544, 960))
        self.assertEqual((g_1088_p[comfy.REFINE_UPSCALER]["inputs"]["width"], g_1088_p[comfy.REFINE_UPSCALER]["inputs"]["height"]), (1088, 1920))
        self.assertEqual((g_1088_l[comfy.SIZE]["inputs"]["width"], g_1088_l[comfy.SIZE]["inputs"]["height"]), (960, 544))
        self.assertEqual((g_1088_l[comfy.REFINE_UPSCALER]["inputs"]["width"], g_1088_l[comfy.REFINE_UPSCALER]["inputs"]["height"]), (1920, 1088))
        self.assertEqual((g_1088_s[comfy.SIZE]["inputs"]["width"], g_1088_s[comfy.SIZE]["inputs"]["height"]), (736, 736))
        self.assertEqual((g_1088_s[comfy.REFINE_UPSCALER]["inputs"]["width"], g_1088_s[comfy.REFINE_UPSCALER]["inputs"]["height"]), (1472, 1472))
        self.assertEqual(g_1088_p[comfy.VAE_DECODE]["inputs"]["samples"], [comfy.REFINE_UPSCALER, 0])
        self.assertEqual(g_1088_p[comfy.VAE_DECODE_AUDIO]["inputs"]["samples"], [comfy.REFINE_UPSCALER, 0])

        # 544p fast tier runs single-pass 544x960 / 960x544 / 736x736 and prunes pass-2 refine nodes
        with patch.dict(os.environ, {"COMFYUI_RESOLUTION": "544p"}, clear=False):
            g_544_p = comfy.graph("p", ["a.png"], 6, "9:16", 1)
            g_544_l = comfy.graph("p", ["a.png"], 6, "16:9", 1)
            g_544_s = comfy.graph("p", ["a.png"], 6, "1:1", 1)
        self.assertEqual((g_544_p[comfy.SIZE]["inputs"]["width"], g_544_p[comfy.SIZE]["inputs"]["height"]), (544, 960))
        self.assertEqual((g_544_l[comfy.SIZE]["inputs"]["width"], g_544_l[comfy.SIZE]["inputs"]["height"]), (960, 544))
        self.assertEqual((g_544_s[comfy.SIZE]["inputs"]["width"], g_544_s[comfy.SIZE]["inputs"]["height"]), (736, 736))
        for refine_nid in (comfy.REFINE_STEPS, comfy.REFINE_SEED, comfy.REFINE_UPSCALER):
            self.assertNotIn(refine_nid, g_544_p)
        self.assertEqual(g_544_p[comfy.VAE_DECODE]["inputs"]["samples"], ["125", 0])
        self.assertEqual(g_544_p[comfy.VAE_DECODE_AUDIO]["inputs"]["samples"], ["125", 0])

        g_768_override = comfy.graph("p", ["a.png"], 6, "9:16", 1, resolution="768p")
        self.assertEqual((g_768_override[comfy.SIZE]["inputs"]["width"], g_768_override[comfy.SIZE]["inputs"]["height"]), (544, 960))
        self.assertEqual((g_768_override[comfy.REFINE_UPSCALER]["inputs"]["width"], g_768_override[comfy.REFINE_UPSCALER]["inputs"]["height"]), (768, 1344))
        g_1088_override = comfy.graph("p", ["a.png"], 6, "9:16", 1, resolution="1088p")
        self.assertEqual((g_1088_override[comfy.REFINE_UPSCALER]["inputs"]["width"], g_1088_override[comfy.REFINE_UPSCALER]["inputs"]["height"]), (1088, 1920))

        # RunningHub graph keeps 544p untouched
        rh_g = rh.graph("prompt rh", ["a.png"], 6, "9:16", 42)
        self.assertEqual((rh_g[rh.SIZE]["inputs"]["width"], rh_g[rh.SIZE]["inputs"]["height"]), (544, 960))

        for bad_images in ([], ["x.png"] * 10):
            with self.assertRaises(ValueError):
                comfy.graph("p", bad_images, 6, "9:16", 1)
        with self.assertRaises(ValueError):
            comfy.graph("p", ["a.png"], 6, "4:3", 1)
        with self.assertRaises(ValueError):
            comfy.graph("p", ["a.png"], 6, "9:16", 1, resolution="4k")

    def test_comfy_presets_map_lora_and_steps_atomically(self):
        g4 = comfy.graph("p", ["a.png"], 5, "9:16", 1, preset="4step")
        self.assertTrue(g4[comfy.ENABLE_LORA]["inputs"]["value"])
        self.assertEqual(g4[comfy.TURBO_STEPS]["inputs"]["value"], 4)
        self.assertEqual(g4[comfy.SAMPLER]["inputs"]["sampler_name"], "res_multistep")
        self.assertEqual(g4[comfy.STEPS]["inputs"]["scheduler"], "simple")
        self.assertEqual(g4[comfy.STEPS]["inputs"]["model"], [comfy.SWITCH_MODEL, 0])
        self.assertEqual(g4["126"]["inputs"]["model"], [comfy.SWITCH_MODEL, 0])
        self.assertNotIn(comfy.EASYCACHE, g4)
        self.assertNotIn(comfy.SPARSE_ATTN, g4)
        # Pass-2 latent refine falls back to raw UNET ("127") without Turbo LoRA when LoRA preset is active
        self.assertEqual(g4[comfy.REFINE_UPSCALER]["inputs"]["model"], ["127", 0])
        self.assertEqual(g4[comfy.REFINE_STEPS]["inputs"]["model"], ["127", 0])
        self.assertEqual(
            g4[comfy.LORA]["inputs"]["lora_name"],
            "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
        )

        g8 = comfy.graph("p", ["a.png"], 5, "9:16", 1, preset="8step")
        self.assertTrue(g8[comfy.ENABLE_LORA]["inputs"]["value"])
        self.assertEqual(g8[comfy.TURBO_STEPS]["inputs"]["value"], 8)
        self.assertEqual(g8[comfy.SAMPLER]["inputs"]["sampler_name"], "res_multistep")
        self.assertEqual(g8[comfy.STEPS]["inputs"]["scheduler"], "beta")
        self.assertEqual(g8[comfy.STEPS]["inputs"]["model"], [comfy.SWITCH_MODEL, 0])
        self.assertEqual(g8["126"]["inputs"]["model"], [comfy.SWITCH_MODEL, 0])
        self.assertNotIn(comfy.EASYCACHE, g8)
        self.assertNotIn(comfy.SPARSE_ATTN, g8)
        self.assertEqual(
            g8[comfy.LORA]["inputs"]["lora_name"],
            "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
        )

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMFYUI_PRESET", None)
            g_default = comfy.graph("p", ["a.png"], 5, "9:16", 1)
        g20 = comfy.graph("p", ["a.png"], 5, "9:16", 1, preset="full20")
        for g in (g_default, g20):
            self.assertFalse(g[comfy.ENABLE_LORA]["inputs"]["value"])
            self.assertEqual(g[comfy.FULL_STEPS]["inputs"]["value"], 20)
            self.assertEqual(g[comfy.SAMPLER]["inputs"]["sampler_name"], "res_multistep")
            self.assertEqual(g[comfy.STEPS]["inputs"]["scheduler"], "beta")
            self.assertNotIn(comfy.LORA, g)
            self.assertIn(comfy.SPARSE_ATTN, g)
            self.assertEqual(g[comfy.SPARSE_ATTN]["inputs"]["model"], ["127", 0])
            self.assertEqual(g[comfy.SPARSE_ATTN]["inputs"]["selection"], "sol-attn")
            self.assertIn(comfy.EASYCACHE, g)
            self.assertEqual(g[comfy.EASYCACHE]["inputs"]["model"], [comfy.SPARSE_ATTN, 0])
            self.assertEqual(g[comfy.STEPS]["inputs"]["model"], [comfy.EASYCACHE, 0])
            self.assertEqual(g["126"]["inputs"]["model"], [comfy.EASYCACHE, 0])

        with self.assertRaises(ValueError):
            comfy.graph("p", ["a.png"], 5, "9:16", 1, preset="invalid")

    def test_upscale_graph_toggle_and_progress_stages(self):
        from h3ref.i18n import use_lang

        # Default (COMFYUI_UPSCALE unset -> disabled, 768p latent refine): CreateVideo wired directly to VAEDecode (122)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMFYUI_UPSCALE", None)
            os.environ.pop("COMFYUI_RESOLUTION", None)
            g_default = comfy.graph("p", ["a.png"], 5, "9:16", 1)
        self.assertEqual((g_default[comfy.SIZE]["inputs"]["width"], g_default[comfy.SIZE]["inputs"]["height"]), (544, 960))
        self.assertEqual((g_default[comfy.REFINE_UPSCALER]["inputs"]["width"], g_default[comfy.REFINE_UPSCALER]["inputs"]["height"]), (768, 1344))
        self.assertEqual(comfy.NODE_STAGE_KEYS[comfy.REFINE_UPSCALER], "refine_latent")
        self.assertNotIn(comfy.UPSCALE_LOADER, g_default)
        self.assertNotIn(comfy.UPSCALE_IMAGE, g_default)
        self.assertEqual(g_default[comfy.CREATE_VIDEO]["inputs"]["images"], [comfy.VAE_DECODE, 0])

        # COMFYUI_UPSCALE=1 enables 122 -> 155 (with 154 loader) -> 130 (e.g. 544p -> 2x)
        with patch.dict(os.environ, {"COMFYUI_RESOLUTION": "544p", "COMFYUI_UPSCALE": "1"}, clear=False):
            g_on = comfy.graph("p", ["a.png"], 5, "9:16", 1)
        self.assertEqual((g_on[comfy.SIZE]["inputs"]["width"], g_on[comfy.SIZE]["inputs"]["height"]), (544, 960))
        self.assertIn(comfy.UPSCALE_LOADER, g_on)
        self.assertIn(comfy.UPSCALE_IMAGE, g_on)
        self.assertEqual(g_on[comfy.UPSCALE_LOADER]["inputs"]["model_name"], "RealESRGAN_x2plus.pth")
        self.assertEqual(g_on[comfy.UPSCALE_IMAGE]["inputs"]["upscale_model"], [comfy.UPSCALE_LOADER, 0])
        self.assertEqual(g_on[comfy.UPSCALE_IMAGE]["inputs"]["image"], [comfy.VAE_DECODE, 0])
        self.assertEqual(g_on[comfy.CREATE_VIDEO]["inputs"]["images"], [comfy.UPSCALE_IMAGE, 0])

        # Explicit upscale=False/True keyword overrides env
        with patch.dict(os.environ, {"COMFYUI_UPSCALE": "1"}, clear=False):
            g_override_off = comfy.graph("p", ["a.png"], 5, "9:16", 1, upscale=False)
        self.assertNotIn(comfy.UPSCALE_LOADER, g_override_off)
        self.assertEqual(g_override_off[comfy.CREATE_VIDEO]["inputs"]["images"], [comfy.VAE_DECODE, 0])
        with patch.dict(os.environ, {"COMFYUI_UPSCALE": "0"}, clear=False):
            g_override_on = comfy.graph("p", ["a.png"], 5, "9:16", 1, upscale=True)
        self.assertEqual(g_override_on[comfy.CREATE_VIDEO]["inputs"]["images"], [comfy.UPSCALE_IMAGE, 0])

        # Live progress stage mapping and bilingual formatting for upscale nodes
        self.assertEqual(comfy.NODE_STAGE_KEYS[comfy.UPSCALE_LOADER], "load_upscaler")
        self.assertEqual(comfy.NODE_STAGE_KEYS[comfy.UPSCALE_IMAGE], "upscale_video")

        def fake_req_upscale(url, key, body=None, **kw):
            if url.endswith("/h3_progress"):
                return {"prompt_id": "pid-up", "node": comfy.UPSCALE_IMAGE, "value": 40, "max": 120, "status": "running"}
            if url.endswith("/system_stats"):
                return {"devices": [{"name": "A100", "vram_total": 40 * (1024 ** 3), "vram_free": 20 * (1024 ** 3)}]}
            raise AssertionError(url)

        with patch.dict(os.environ, {"COMFYUI_URL": "https://demo.ngrok-free.dev", "COMFYUI_API_KEY": "h3c_k"}, clear=False), \
             patch.object(comfy.http, "request", side_effect=fake_req_upscale):
            st = comfy.live_progress("pid-up", queued=False)
        self.assertEqual(st["stage"], "upscale_video")
        self.assertEqual(st["step"], 40)
        self.assertEqual(st["max_steps"], 120)
        with use_lang("zh-CN"):
            self.assertIn("超分高清", comfy.format_progress(st, elapsed_s=180))
            self.assertIn("加载超分模型", comfy.format_progress({"stage": "load_upscaler"}))
        with use_lang("en"):
            self.assertIn("Upscaling video", comfy.format_progress(st, elapsed_s=180))
            self.assertIn("Loading upscale model", comfy.format_progress({"stage": "load_upscaler"}))

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

    def test_upload_many_deduplicates_by_content_hash_preserves_order_and_invalidates_on_loadimage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img_a1 = root / "board.png"
            img_a2 = root / "board_copy.png"
            img_b = root / "cast.png"
            Image.new("RGB", (8, 8), "red").save(img_a1)
            img_a2.write_bytes(img_a1.read_bytes())  # Identical content -> same content-hash
            Image.new("RGB", (8, 8), "blue").save(img_b)

            upload_calls = []

            def fake_http_upload(url, key, path, fields, **kw):
                upload_calls.append(kw["filename"])
                return {"name": kw["filename"], "subfolder": "", "type": "input"}

            with patch.dict(os.environ, {"COMFYUI_URL": "https://demo.ngrok-free.dev", "COMFYUI_API_KEY": "k1"}, clear=False), \
                 patch.object(comfy.http, "upload", side_effect=fake_http_upload):
                first = comfy.upload_many([img_a1, img_b, img_a2])
                self.assertEqual(len(first), 3)
                self.assertEqual(first[0], first[2])
                self.assertNotEqual(first[0], first[1])
                self.assertEqual(len(upload_calls), 2)

                # Second batch reuses remote cache without re-uploading any identical files
                second = comfy.upload_many([img_b, img_a1])
                self.assertEqual(second, [first[1], first[0]])
                self.assertEqual(len(upload_calls), 2)

                # LoadImage rejection from /prompt clears endpoint cache so retry re-uploads
                with patch.object(comfy.http, "request", return_value={"node_errors": {"137": {"errors": [{"type": "invalid_image"}]}}}):
                    with self.assertRaises(comfy.SubmissionRejected):
                        comfy.submit({})

                third = comfy.upload_many([img_a1])
                self.assertEqual(third, [first[0]])
                self.assertEqual(len(upload_calls), 3)

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

    def test_comfy_live_progress_ws_tracker_poll_fallback_and_film_state_persistence(self):
        from scripts import start_colab

        # 1. start_colab WS event reducer tracks classic executing/progress AND ComfyUI 0.46+ progress_state
        ws_state = {}
        start_colab._apply_comfy_ws_event(ws_state, {"type": "execution_start", "data": {"prompt_id": "pid-live"}}, now=100.0)
        self.assertEqual(ws_state["prompt_id"], "pid-live")
        self.assertEqual(ws_state["status"], "running")

        start_colab._apply_comfy_ws_event(ws_state, {"type": "executing", "data": {"prompt_id": "pid-live", "node": "125"}}, now=105.0)
        start_colab._apply_comfy_ws_event(ws_state, {"type": "progress", "data": {"prompt_id": "pid-live", "node": "125", "value": 2, "max": 4}}, now=110.0)
        self.assertEqual(ws_state["node"], "125")
        self.assertEqual(ws_state["value"], 2)
        self.assertEqual(ws_state["max"], 4)

        start_colab._apply_comfy_ws_event(ws_state, {
            "type": "progress_state",
            "data": {
                "prompt_id": "pid-live",
                "nodes": {
                    "125": {"state": "finished", "value": 4, "max": 4},
                    "122": {"state": "running", "value": 1, "max": 2},
                },
            },
        }, now=115.0)
        self.assertEqual(ws_state["node"], "122")
        self.assertEqual(ws_state["value"], 1)
        self.assertEqual(ws_state["max"], 2)

        # 2. comfy.live_progress enriches running state with node stage, step/max, and VRAM from /h3_progress + /system_stats
        def fake_req_full(url, key, body=None, **kw):
            if url.endswith("/h3_progress"):
                return {"prompt_id": "pid-live", "node": "125", "value": 2, "max": 4, "status": "running"}
            if url.endswith("/system_stats"):
                return {"devices": [{"name": "NVIDIA A100-SXM4-40GB", "vram_total": 40 * (1024 ** 3), "vram_free": 10 * (1024 ** 3)}]}
            raise AssertionError(url)

        with patch.dict(os.environ, {"COMFYUI_URL": "https://demo.ngrok-free.dev", "COMFYUI_API_KEY": "h3c_k"}, clear=False), \
             patch.object(comfy.http, "request", side_effect=fake_req_full):
            st = comfy.live_progress("pid-live", queued=False)
        self.assertEqual(st["stage"], "sampling")
        self.assertEqual(st["node"], "125")
        self.assertEqual(st["step"], 2)
        self.assertEqual(st["max_steps"], 4)
        self.assertEqual(st["vram_total_gb"], 40.0)
        self.assertEqual(st["vram_used_gb"], 30.0)
        self.assertIn("2/4", comfy.format_progress(st, elapsed_s=75))

        # 3. Graceful fallback when /h3_progress returns 404 (older Drive script or direct local ComfyUI)
        def fake_req_fallback(url, key, body=None, **kw):
            if url.endswith("/queue"):
                return {"queue_running": [], "queue_pending": [[1, "other"], [2, "pid-q"]]}
            if url.endswith("/h3_progress"):
                raise http.HTTPResponseError(404, "Not Found")
            if url.endswith("/system_stats"):
                return {"devices": [{"name": "A100", "vram_total": 40 * (1024 ** 3), "vram_free": 38 * (1024 ** 3)}]}
            raise AssertionError(url)

        with patch.dict(os.environ, {"COMFYUI_URL": "https://demo.ngrok-free.dev", "COMFYUI_API_KEY": "h3c_k"}, clear=False), \
             patch.object(comfy.http, "request", side_effect=fake_req_fallback):
            st_q = comfy.live_progress("pid-q", queued=True)
        self.assertTrue(st_q["queued"])
        self.assertEqual(st_q["queue_position"], 2)
        self.assertEqual(st_q["stage"], "queued")
        self.assertEqual(st_q["vram_used_gb"], 2.0)

        # 4. film._generate_segment persists live_progress during polling, logs progress, and clears it on completion
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = root / "ref.png"
            Image.new("RGB", (8, 8), "blue").save(ref)
            job = film.Job(root / "job-progress")
            (job.dir / "clips").mkdir(parents=True, exist_ok=True)
            job.put("segs", "0", h3="subject_definitions:\n<Picture 1> test\n")
            film_doc = {"aspect": "9:16", "cast": [{"name": "A", "picture": str(ref)}], "shots": []}
            seg = {"idx": 0, "seconds": 5, "shots": [{"action": "walk"}]}
            pics = [{"kind": "cast", "cast": {"name": "A", "picture": str(ref)}}]
            snapshots = []

            poll_replies = [
                {"state": "running", "queued": False, "stage": "sampling", "node": "125", "step": 2, "max_steps": 4, "vram_used_gb": 31.0, "vram_total_gb": 40.0},
                {"state": "done", "url": "https://demo.ngrok-free.dev/view?filename=out.mp4", "cny": 0.0, "coins": 0.0, "money": 0.0, "run_seconds": 35.0},
            ]

            def fake_poll(task):
                item = poll_replies.pop(0)
                return item

            def fake_sleep(_s):
                snapshots.append(dict(job.state["segs"]["0"].get("live_progress") or {}))

            with patch.dict(os.environ, {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "https://demo.ngrok-free.dev", "COMFYUI_API_KEY": "h3c_k"}, clear=False), \
                 patch("h3ref.comfy.upload", return_value="ref.png"), \
                 patch("h3ref.comfy.submit_with_retry", return_value="pid-live-2"), \
                 patch("h3ref.comfy.poll", side_effect=fake_poll), \
                 patch("h3ref.comfy.download", side_effect=lambda u, dest: dest.write_bytes(b"mp4")), \
                 patch("h3ref.media.last_frame"), \
                 patch("h3ref.film.time.sleep", side_effect=fake_sleep):
                rec = film._generate_segment(job, film_doc, seg, pics, reference_paths=[ref])

            self.assertTrue(snapshots and snapshots[0].get("stage") == "sampling")
            self.assertEqual(snapshots[0].get("step"), 2)
            self.assertEqual(snapshots[0].get("max_steps"), 4)
            self.assertNotIn("live_progress", rec)
            run_log = (job.dir / "run.log").read_text(encoding="utf-8")
            self.assertIn("2/4", run_log)

    def test_start_colab_disables_aimdo_flags_retries_fuse_pread_and_film_surfaces_error_reason(self):
        from scripts import start_colab

        # 1. start_comfyui launches main.py with --disable-dynamic-vram and --disable-pinned-memory
        launched_cmds = []

        def fake_popen(cmd, **kwargs):
            launched_cmds.append(list(cmd))
            return object()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(start_colab, "LOCAL", root), \
                 patch.object(start_colab, "COMFY_LOG_FILE", root / "comfyui.log"), \
                 patch.object(start_colab, "_http_status", side_effect=[None, 200]), \
                 patch.object(start_colab.subprocess, "run"), \
                 patch.object(start_colab.subprocess, "Popen", side_effect=fake_popen), \
                 patch.object(start_colab.time, "sleep"):
                start_colab.start_comfyui()

        self.assertEqual(len(launched_cmds), 1)
        self.assertIn("--disable-dynamic-vram", launched_cmds[0])
        self.assertIn("--disable-pinned-memory", launched_cmds[0])

        # 2. _parallel_copy_file retries transient Drive FUSE OSError during pread (POSIX) or copies via fallback (Windows)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "model.safetensors"
            dst = root / "staged.safetensors"
            payload = b"ABCD1234" * 4096
            src.write_bytes(payload)
            if hasattr(os, "pread") and hasattr(os, "pwrite"):
                real_pread = os.pread
                injected = {"failed": False}

                def flaky_pread(fd, n, offset):
                    if not injected["failed"]:
                        injected["failed"] = True
                        raise OSError("transient Drive FUSE read drop")
                    return real_pread(fd, n, offset)

                with patch.object(start_colab.os, "pread", side_effect=flaky_pread), \
                     patch.object(start_colab.time, "sleep"):
                    start_colab._parallel_copy_file(src, dst, len(payload), "text_encoders/qwen3vl.safetensors")

                self.assertTrue(injected["failed"])
            else:
                start_colab._parallel_copy_file(src, dst, len(payload), "text_encoders/qwen3vl.safetensors")
            self.assertEqual(dst.read_bytes(), payload)

        # 3. film._generate_segment logs and surfaces the exact remote error in TERMINAL_FAILED (both live and on resume)
        reason = "MiniMaxH3ReferenceToVideo RuntimeError: HostBuffer.read_file_slice failed"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = root / "ref.png"
            Image.new("RGB", (8, 8), "green").save(ref)
            job = film.Job(root / "job-fail")
            (job.dir / "clips").mkdir(parents=True, exist_ok=True)
            job.put("segs", "0", h3="subject_definitions:\n<Picture 1> test\n")
            film_doc = {"aspect": "9:16", "cast": [{"name": "A", "picture": str(ref)}], "shots": []}
            seg = {"idx": 0, "seconds": 5, "shots": [{"action": "walk"}]}
            pics = [{"kind": "cast", "cast": {"name": "A", "picture": str(ref)}}]

            with patch.dict(os.environ, {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "https://demo.ngrok-free.dev", "COMFYUI_API_KEY": "h3c_k"}, clear=False), \
                 patch("h3ref.comfy.upload", return_value="ref.png"), \
                 patch("h3ref.comfy.submit_with_retry", return_value="pid-fail-1"), \
                 patch("h3ref.comfy.poll", return_value={"state": "failed", "error": reason}):
                with self.assertRaises(RuntimeError) as ctx_first:
                    film._generate_segment(job, film_doc, seg, pics, reference_paths=[ref])
                with self.assertRaises(RuntimeError) as ctx_resume:
                    film._generate_segment(job, film_doc, seg, pics, reference_paths=[ref])

            self.assertIn("TERMINAL_FAILED", str(ctx_first.exception))
            self.assertIn(reason, str(ctx_first.exception))
            self.assertIn("TERMINAL_FAILED", str(ctx_resume.exception))
            self.assertIn(reason, str(ctx_resume.exception))
            self.assertIn(reason, (job.dir / "run.log").read_text(encoding="utf-8"))

    def test_ngrok_offline_404_html_surfaces_err_code_and_localized_message(self):
        from h3ref.i18n import use_lang

        # Realistic >2KB ngrok offline HTML page where <noscript> ERR_NGROK_3200 appears past byte 1500
        preload_padding = "".join(
            f'    <link rel="preload" href="https://assets.ngrok.com/fonts/font-{i}.woff" as="font" crossorigin="anonymous" />\n'
            for i in range(18)
        )
        ngrok_404_html = (
            '<!DOCTYPE html>\n<html class="h-full" lang="en-US" dir="ltr">\n  <head>\n'
            '    <meta charset="utf-8">\n'
            f"{preload_padding}"
            "    <noscript>The endpoint demo-node.ngrok-free.dev is offline. (ERR_NGROK_3200)</noscript>\n"
            "  </head>\n  <body id=\"ngrok\"></body>\n</html>\n"
        )
        self.assertGreater(ngrok_404_html.index("ERR_NGROK_3200"), 1500)

        class OfflineNgrokHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _send_offline(self):
                body = ngrok_404_html.encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "text/html")
                self.send_header("ngrok-error-code", "ERR_NGROK_3200")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _send_offline
            do_POST = _send_offline

        server = HTTPServer(("127.0.0.1", 0), OfflineNgrokHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        base_url = f"http://127.0.0.1:{port}"
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "ref.png"
            Image.new("RGB", (8, 8), "blue").save(img)

            # 1. http.upload extracts ERR_NGROK_3200 instead of truncating at <!DOCTYPE html>
            with self.assertRaises(http.HTTPResponseError) as upload_err:
                http.upload(f"{base_url}/upload/image", "k", img, {"type": "input"}, file_field_name="image")
            self.assertEqual(upload_err.exception.status_code, 404)
            self.assertIn("ERR_NGROK_3200", str(upload_err.exception))
            self.assertIn("offline", str(upload_err.exception))
            self.assertNotIn("<!DOCTYPE html>", str(upload_err.exception))

            # 2. comfy.upload raises localized tunnel-offline HTTPResponseError in zh-CN and en
            with patch.dict(os.environ, {"COMFYUI_URL": base_url, "COMFYUI_API_KEY": "k"}, clear=False):
                with use_lang("zh-CN"), self.assertRaises(http.HTTPResponseError) as zh_err:
                    comfy.upload(img, force=True)
                self.assertEqual(zh_err.exception.status_code, 404)
                self.assertIn("隧道已离线", str(zh_err.exception))
                self.assertIn("ERR_NGROK_3200", str(zh_err.exception))

                with use_lang("en"), self.assertRaises(http.HTTPResponseError) as en_err:
                    comfy.upload(img, force=True)
                self.assertEqual(en_err.exception.status_code, 404)
                self.assertIn("tunnel is offline", str(en_err.exception))
                self.assertIn("ERR_NGROK_3200", str(en_err.exception))

                # 3. comfy.submit surfaces tunnel-offline HTTPResponseError instead of misclassifying as SubmissionRejected
                with use_lang("zh-CN"), self.assertRaises(http.HTTPResponseError) as sub_err:
                    comfy.submit({"1": {"class_type": "Dummy", "inputs": {}}})
                self.assertNotIsInstance(sub_err.exception, comfy.SubmissionRejected)
                self.assertIn("ERR_NGROK_3200", str(sub_err.exception))

                # 4. http.request surfaces ERR_NGROK_3200 so comfy.poll recognizes it as a transient tunnel blip
                comfy._TRANSIENT_COUNTS.clear()
                st = comfy.poll("pid-ngrok-html")
                self.assertEqual(st["state"], "running")

    # Value: protects=HTTPS authenticated download downgrade prevention and Bearer key scrubbing; fails_when=--proto-redir allows http downgrade on https downloads or curl failure leaks COMFYUI_API_KEY; why_new=existing download test only covers loopback HTTP 200 success; seam=none; no_claim=does not test live TLS handshake
    def test_https_download_enforces_https_only_redirects_and_scrubs_bearer_key_on_error(self):
        secret = "h3c_dl_secret_token_888"
        captured = {}

        def fake_ok_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["input"] = kwargs.get("input", "")
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_bytes(b"V" * 2048)
            return MagicMock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "clip.mp4"
            with patch("subprocess.run", side_effect=fake_ok_run):
                http.download(
                    "https://node-a.ngrok-free.dev/view?filename=out.mp4",
                    dst,
                    key=secret,
                    extra_headers={"ngrok-skip-browser-warning": "1"},
                )
            self.assertEqual(dst.read_bytes(), b"V" * 2048)
            self.assertEqual(captured["cmd"][captured["cmd"].index("--proto-redir") + 1], "=https")
            self.assertIn("--config", captured["cmd"])
            self.assertFalse(any(secret in str(arg) for arg in captured["cmd"]), "Bearer key must never appear in curl argv")
            self.assertIn(f"Authorization: Bearer {secret}", captured["input"])
            self.assertIn("ngrok-skip-browser-warning: 1", captured["input"])

            dst_fail = Path(tmp) / "fail.mp4"

            def fake_fail_run(cmd, **kwargs):
                Path(cmd[cmd.index("-o") + 1]).write_bytes(b"partial")
                return MagicMock(
                    returncode=22,
                    stdout="",
                    stderr=f"curl: (22) failed with Bearer {secret} on upstream",
                )

            with patch("subprocess.run", side_effect=fake_fail_run):
                with self.assertRaises(RuntimeError) as err_ctx:
                    http.download(
                        f"https://node-a.ngrok-free.dev/view?token={secret}",
                        dst_fail,
                        key=secret,
                    )
            self.assertNotIn(secret, str(err_ctx.exception))
            self.assertIn("[key]", str(err_ctx.exception))
            self.assertFalse(dst_fail.with_suffix(".mp4.part").exists())


if __name__ == "__main__":
    unittest.main()
