"""Stage 1 tests: functional video_settings(environ), COMFY_SIZES isolation, fail-fast validation, and secret redaction."""
import json
import tempfile
import unittest
from pathlib import Path

from h3ref import config
from workbench import jobs as wb_jobs
from workbench import server as wb_server


class ComfyConfigTest(unittest.TestCase):
    def test_default_fallback_preserves_runninghub_and_sizes(self):
        settings = config.video_settings({})
        self.assertEqual(settings["provider"], "runninghub")
        self.assertEqual(settings["comfyui_preset"], "full20")
        self.assertEqual(settings["comfyui_resolution"], "768p")
        self.assertFalse(settings["comfyui_upscale"])
        self.assertEqual(settings["comfyui_concurrency"], 1)
        self.assertEqual(settings["concurrency"], settings["runninghub_concurrency"])
        self.assertEqual(settings["colab_session"], "comfy-a100")
        self.assertEqual(settings["colab_gpu"], "G4")
        self.assertFalse(settings["colab_auto_start"])
        self.assertEqual(settings["colab_bin"], "")
        self.assertEqual(settings["colab_auth"], "")
        self.assertEqual(config.SIZES, {"9:16": (544, 960), "16:9": (960, 544), "1:1": (736, 736)})
        self.assertEqual(config.COMFY_SIZES, {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (1024, 1024)})
        self.assertEqual(config.COMFY_RESOLUTIONS["544p"], {"9:16": (544, 960), "16:9": (960, 544), "1:1": (736, 736)})
        self.assertEqual(config.COMFY_RESOLUTIONS["768p"], {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (1024, 1024)})

    def test_comfyui_settings_parse_valid_options_and_strip_trailing_slash(self):
        settings = config.video_settings({
            "VIDEO_PROVIDER": "ComfyUI",
            "COMFYUI_URL": "https://demo.ngrok-free.dev/",
            "COMFYUI_API_KEY": "  h3c_secret_token_value  ",
            "COMFYUI_PRESET": "8step",
            "COMFYUI_RESOLUTION": "544P",
            "COMFYUI_UPSCALE": "1",
            "COMFYUI_CONCURRENCY": "2",
            "COLAB_SESSION": "my-a100",
            "COLAB_GPU": "h100",
            "COLAB_AUTO_START": "1",
            "COLAB_BIN": "/opt/bin/colab",
            "COLAB_AUTH": "ADC",
        })
        self.assertEqual(settings["provider"], "comfyui")
        self.assertEqual(settings["comfyui_url"], "https://demo.ngrok-free.dev")
        self.assertEqual(settings["comfyui_api_key"], "h3c_secret_token_value")
        self.assertEqual(settings["comfyui_preset"], "8step")
        self.assertEqual(settings["comfyui_resolution"], "544p")
        self.assertTrue(settings["comfyui_upscale"])
        self.assertEqual(settings["comfyui_concurrency"], 2)
        self.assertEqual(settings["concurrency"], 2)
        self.assertEqual(settings["colab_session"], "my-a100")
        self.assertEqual(settings["colab_gpu"], "H100")
        self.assertTrue(settings["colab_auto_start"])
        self.assertEqual(settings["colab_bin"], "/opt/bin/colab")
        self.assertEqual(settings["colab_auth"], "adc")

    def test_invalid_enum_and_url_values_fail_fast_without_leaking_credentials(self):
        bad_cases = [
            ({"VIDEO_PROVIDER": "sora"}, "VIDEO_PROVIDER='sora'"),
            ({"COMFYUI_PRESET": "12step"}, "COMFYUI_PRESET='12step'"),
            ({"COMFYUI_RESOLUTION": "1080p"}, "COMFYUI_RESOLUTION='1080p'"),
            ({"COMFYUI_UPSCALE": "maybe"}, "COMFYUI_UPSCALE='maybe'"),
            ({"COMFYUI_CONCURRENCY": "0"}, "COMFYUI_CONCURRENCY='0'"),
            ({"COMFYUI_CONCURRENCY": "-2"}, "COMFYUI_CONCURRENCY='-2'"),
            ({"COMFYUI_CONCURRENCY": "two"}, "COMFYUI_CONCURRENCY='two'"),
            ({"COLAB_GPU": "T4"}, "COLAB_GPU='T4'"),
            ({"COLAB_AUTO_START": "sometimes"}, "COLAB_AUTO_START='sometimes'"),
            ({"COLAB_AUTH": "kerberos"}, "COLAB_AUTH='kerberos'"),
            ({"COMFYUI_URL": "ftp://bad.example"}, "COMFYUI_URL"),
            ({"COMFYUI_URL": "not-a-url"}, "COMFYUI_URL"),
            ({"COMFYUI_URL": "https://example.com?token=1"}, "COMFYUI_URL"),
            ({"COMFYUI_URL": "https://example.com#frag"}, "COMFYUI_URL"),
        ]
        for env_map, expected in bad_cases:
            with self.subTest(env_map=env_map):
                with self.assertRaisesRegex(ValueError, expected):
                    config.video_settings(env_map)

        with self.assertRaises(ValueError) as ctx:
            config.video_settings({"COMFYUI_URL": "https://admin:super_secret_pw@demo.ngrok-free.dev"})
        self.assertNotIn("super_secret_pw", str(ctx.exception))

    def test_comfyui_api_key_registered_and_redacted_in_workbench_logs(self):
        self.assertIn("COMFYUI_API_KEY", wb_jobs.SECRET_NAMES)
        self.assertIn("COMFYUI_API_KEY", wb_server.KEYS)
        with tempfile.TemporaryDirectory() as tmp:
            manager = wb_jobs.Jobs(config.ROOT, Path(tmp) / "wb")
            try:
                manager.remember_secrets({"COMFYUI_API_KEY": "h3c_super_secret_987654"})
                redacted = manager.redact("Tunnel failed with Bearer h3c_super_secret_987654 on /prompt")
                self.assertNotIn("h3c_super_secret_987654", redacted)
                self.assertIn("[REDACTED]", redacted)
            finally:
                manager.close()

    def test_env_example_defaults_to_comfyui_without_exposing_ngrok_authtoken(self):
        env_example = config.read_env_file(config.ROOT / ".env.example")
        self.assertEqual(env_example.get("VIDEO_PROVIDER"), "comfyui")
        self.assertEqual(env_example.get("COMFYUI_PRESET"), "full20")
        self.assertEqual(env_example.get("COMFYUI_RESOLUTION"), "768p")
        self.assertEqual(env_example.get("COMFYUI_UPSCALE"), "0")
        self.assertEqual(env_example.get("COLAB_GPU"), "G4")
        self.assertEqual(env_example.get("COLAB_AUTO_START"), "0")
        raw_text = (config.ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("NGROK_AUTHTOKEN=", raw_text)

    def test_comfy_workflow_graph_is_clean_api_format(self):
        self.assertTrue(config.COMFY_GRAPH.is_file())
        graph = json.loads(config.COMFY_GRAPH.read_text(encoding="utf-8"))
        self.assertNotIn("nodes", graph)
        self.assertEqual(graph["128"]["class_type"], "CLIPLoader")
        self.assertEqual(graph["128"]["inputs"]["clip_name"], "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
        self.assertEqual(graph["136"]["class_type"], "MiniMaxH3ReferenceToVideo")
        self.assertEqual((graph["136"]["inputs"]["width"], graph["136"]["inputs"]["height"]), (768, 1344))
        self.assertEqual(graph["138"]["inputs"]["value"], "")
        self.assertFalse(graph["146"]["inputs"]["value"])
        self.assertEqual(graph["124"]["inputs"]["scheduler"], "beta")
        self.assertEqual(graph["124"]["inputs"]["model"], ["156", 0])
        self.assertEqual(graph["126"]["inputs"]["model"], ["156", 0])
        self.assertEqual(graph["156"]["class_type"], "EasyCache")
        self.assertEqual(graph["156"]["inputs"]["model"], ["127", 0])
        self.assertEqual(graph["156"]["inputs"]["reuse_threshold"], 0.2)
        self.assertEqual(graph["156"]["inputs"]["start_percent"], 0.15)
        self.assertEqual(graph["156"]["inputs"]["end_percent"], 0.9)
        self.assertFalse(graph["156"]["inputs"]["verbose"])
        self.assertEqual(graph["154"]["class_type"], "UpscaleModelLoader")
        self.assertEqual(graph["154"]["inputs"]["model_name"], "RealESRGAN_x2plus.pth")
        self.assertEqual(graph["155"]["class_type"], "ImageUpscaleWithModel")
        self.assertEqual(graph["155"]["inputs"]["upscale_model"], ["154", 0])
        self.assertEqual(graph["155"]["inputs"]["image"], ["122", 0])
        self.assertEqual(graph["130"]["inputs"]["images"], ["122", 0])
        loads = [n["inputs"].get("image") for n in graph.values() if n.get("class_type") == "LoadImage"]
        self.assertEqual(len(loads), 9)
        self.assertTrue(all(x == "None" for x in loads))


if __name__ == "__main__":
    unittest.main()
