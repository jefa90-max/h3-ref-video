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
        self.assertEqual(settings["comfyui_preset"], "4step")
        self.assertEqual(settings["comfyui_concurrency"], 1)
        self.assertEqual(settings["concurrency"], settings["runninghub_concurrency"])
        self.assertEqual(settings["colab_session"], "comfy-a100")
        self.assertFalse(settings["colab_auto_start"])
        self.assertEqual(settings["colab_bin"], "")
        self.assertEqual(settings["colab_auth"], "")
        self.assertEqual(config.SIZES, {"9:16": (544, 960), "16:9": (960, 544), "1:1": (736, 736)})
        self.assertEqual(config.COMFY_SIZES, {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (992, 992)})

    def test_comfyui_settings_parse_valid_options_and_strip_trailing_slash(self):
        settings = config.video_settings({
            "VIDEO_PROVIDER": "ComfyUI",
            "COMFYUI_URL": "https://demo.ngrok-free.dev/",
            "COMFYUI_API_KEY": "  h3c_secret_token_value  ",
            "COMFYUI_PRESET": "8step",
            "COMFYUI_CONCURRENCY": "2",
            "COLAB_SESSION": "my-a100",
            "COLAB_AUTO_START": "1",
            "COLAB_BIN": "/opt/bin/colab",
            "COLAB_AUTH": "ADC",
        })
        self.assertEqual(settings["provider"], "comfyui")
        self.assertEqual(settings["comfyui_url"], "https://demo.ngrok-free.dev")
        self.assertEqual(settings["comfyui_api_key"], "h3c_secret_token_value")
        self.assertEqual(settings["comfyui_preset"], "8step")
        self.assertEqual(settings["comfyui_concurrency"], 2)
        self.assertEqual(settings["concurrency"], 2)
        self.assertEqual(settings["colab_session"], "my-a100")
        self.assertTrue(settings["colab_auto_start"])
        self.assertEqual(settings["colab_bin"], "/opt/bin/colab")
        self.assertEqual(settings["colab_auth"], "adc")

    def test_invalid_enum_and_url_values_fail_fast_without_leaking_credentials(self):
        bad_cases = [
            ({"VIDEO_PROVIDER": "sora"}, "VIDEO_PROVIDER='sora'"),
            ({"COMFYUI_PRESET": "12step"}, "COMFYUI_PRESET='12step'"),
            ({"COMFYUI_CONCURRENCY": "0"}, "COMFYUI_CONCURRENCY='0'"),
            ({"COMFYUI_CONCURRENCY": "-2"}, "COMFYUI_CONCURRENCY='-2'"),
            ({"COMFYUI_CONCURRENCY": "two"}, "COMFYUI_CONCURRENCY='two'"),
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
        self.assertEqual(env_example.get("COMFYUI_PRESET"), "4step")
        self.assertEqual(env_example.get("COLAB_AUTO_START"), "0")
        raw_text = (config.ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("NGROK_AUTHTOKEN=", raw_text)

    def test_comfy_workflow_graph_is_clean_api_format(self):
        self.assertTrue(config.COMFY_GRAPH.is_file())
        graph = json.loads(config.COMFY_GRAPH.read_text(encoding="utf-8"))
        self.assertNotIn("nodes", graph)
        self.assertEqual(graph["136"]["class_type"], "MiniMaxH3ReferenceToVideo")
        self.assertEqual(graph["138"]["inputs"]["value"], "")
        loads = [n["inputs"].get("image") for n in graph.values() if n.get("class_type") == "LoadImage"]
        self.assertEqual(len(loads), 9)
        self.assertTrue(all(x == "None" for x in loads))


if __name__ == "__main__":
    unittest.main()
