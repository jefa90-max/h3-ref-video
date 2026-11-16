"""Offline regressions for dual-LLM runtime isolation, security guards, Windows atomic persistence,
HTTP boundary hardening, output directories, Windows packaging, and endpoint performance."""
import ast
import errno
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from h3ref import config, film, frames, http, media, rewrite, rh


def _load_cli():
    spec = importlib.util.spec_from_file_location("h3ref_cli", Path(__file__).resolve().parents[1] / "h3ref.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
from scripts import package_windows
from workbench import directories, inputs, segments, settings
from workbench.i18n import InputError
from workbench.inputs import preflight
from workbench.jobs import Jobs, write_json
from workbench.server import create_app

SAMPLE_V2_COMPOSITION = {
    "version": 2,
    "scene": "安静的厨房",
    "frame": "小林站在桌旁",
    "orientation": "正面朝向镜头",
    "scene_id": "kitchen",
    "world_frame": "灶台在北侧，木桌在中央",
    "action": "小林伸手拿起纸袋",
    "fixed_objects": [{"name": "木桌", "position": "房间中央"}],
    "camera": {"position": "木桌南侧", "view_direction": "朝北", "framing": "中景固定", "axis_side": "不适用"},
    "characters": [{
        "name": "小林", "appearance": "灰色卫衣", "position": "木桌北侧",
        "body_facing": "朝南", "gaze": "木桌上的纸袋", "pose": "站立伸右手",
        "expression": "平静", "screen_position": "画面中央", "visibility": "visible",
    }],
    "props": [{
        "name": "纸袋", "holder": "无人", "contact": "置于桌面", "position": "木桌中央",
        "orientation": "直立", "state": "闭合", "opening_direction": "朝上",
        "base_direction": "贴桌面朝下", "knot_position": None,
        "screen_position": "画面下方中央", "visibility": "visible",
    }],
    "expected_end": {
        "characters": [{
            "name": "小林", "appearance": "灰色卫衣", "position": "木桌北侧",
            "body_facing": "朝南", "gaze": "纸袋", "pose": "右手握住纸袋",
            "expression": "平静", "screen_position": "画面中央", "visibility": "visible",
        }],
        "props": [{
            "name": "纸袋", "holder": "小林", "contact": "右手握住袋口", "position": "木桌上方",
            "orientation": "直立", "state": "被提起", "opening_direction": "朝上",
            "base_direction": "朝下", "knot_position": None,
            "screen_position": "画面中央", "visibility": "visible",
        }],
    },
}


class LlmProviderRuntimeTest(unittest.TestCase):
    def test_gemini_isolates_base_url_models_and_key(self):
        env = {
            "LLM_PROVIDER": "gemini",
            "LLM_BASE_URL": "https://custom.example/v1",
            "LLM_API_KEY": "legacy-llm",
            "APIMART_API_KEY": "legacy-apimart",
            "GEMINI_COMPOSITION_MODEL": "gemini-2.5-pro",
            "GEMINI_REWRITE_MODEL": "gemini-2.5-flash",
        }
        resolved = config.llm_settings(env)
        self.assertEqual(resolved["base_url"], "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertEqual((resolved["composition_model"], resolved["rewrite_model"]), ("gemini-2.5-pro", "gemini-2.5-flash"))
        self.assertEqual(resolved["key_names"], ("GEMINI_API_KEY",))
        with patch.dict("os.environ", env, clear=True), self.assertRaises(SystemExit):
            config.llm_key()

    def test_unknown_provider_raises_value_error(self):
        with self.assertRaises(ValueError):
            config.llm_settings({"LLM_PROVIDER": "openai"})

    def test_dynamic_provider_env_uses_matching_base_url_and_models(self):
        env = {
            "LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "gk-secret-12345",
            "GEMINI_COMPOSITION_MODEL": "gemini-2.5-pro",
            "GEMINI_REWRITE_MODEL": "gemini-2.5-flash",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch.object(rewrite.http, "request", return_value={"choices": [{"message": {"content": "ok"}}]}) as req:
                self.assertEqual(rewrite._llm("prompt"), "ok")
                self.assertEqual(req.call_args.args[0], "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
                self.assertEqual(req.call_args.args[1], "gk-secret-12345")
                self.assertEqual(req.call_args.args[2]["model"], "gemini-2.5-flash")

            fenced = "```json\n" + json.dumps(SAMPLE_V2_COMPOSITION, ensure_ascii=False) + "\n```"
            script = {"cast": [{"name": "小林", "picture": "/a.png"}], "shots": [{"action": "小林拿起纸袋"}]}
            with patch.object(frames.http, "request", return_value={"choices": [{"message": {"content": fenced}}]}) as creq:
                parsed = frames.describe_composition(script, script["shots"][0])
                self.assertEqual(parsed["version"], 2)
                self.assertEqual(creq.call_args.args[0], "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
                self.assertEqual(creq.call_args.args[1], "gk-secret-12345")
                self.assertEqual(creq.call_args.args[2]["model"], "gemini-2.5-pro")

    def test_gemini_json_array_4xx_raises_rewrite_error_and_redacts_key(self):
        secret = "AIzaSySecretKeyValue999"
        body = json.dumps([{"error": {"code": 400, "message": f"Invalid key {secret}"}}]).encode("utf-8")
        err = urllib.error.HTTPError("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                                     400, "Bad Request", {}, io.BytesIO(body))
        self.addCleanup(err.close)
        with patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": secret}, clear=True), \
             patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(rewrite.RewriteError) as caught:
                rewrite._llm("test prompt")
            self.assertNotIn(secret, str(caught.exception))

    def test_stage_frames_auth_error_does_not_poison_shots_with_fallback_prompt_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = film.Job(Path(tmp))
            script = {"aspect": "9:16", "shots": [{"start": 0, "end": 2, "action": "shot 1"},
                                                  {"start": 2, "end": 4, "action": "shot 2"}]}
            segs = film.plan(script["shots"])
            pics = {s["idx"]: film.refs(script, s, "shot") for s in segs}
            auth_err = http.HTTPResponseError(401, "401 invalid api key")
            with patch.object(frames, "describe_composition", side_effect=auth_err) as desc, \
                 patch.object(frames, "draw") as draw:
                with self.assertRaises(http.HTTPResponseError):
                    film.stage_frames(job, script, segs, pics)
            self.assertEqual(desc.call_count, 1)
            draw.assert_not_called()
            self.assertEqual(job.state["frames"], {})

    def test_custom_llm_base_url_isolates_apimart_key_and_origin_change_clears_saved_llm_key(self):
        isolated = config.llm_settings({
            "LLM_PROVIDER": "apimart",
            "LLM_BASE_URL": "http://192.168.1.10:11434/v1",
            "APIMART_API_KEY": "sk-apimart-secret",
        })
        self.assertEqual(isolated["key_names"], ("LLM_API_KEY",))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = root / ".env"
            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                app = create_app(root / "st", env_file)
                client = app.test_client()
                headers = {"X-Workbench": "1"}
                client.post("/api/config", json={
                    "LLM_BASE_URL": "https://one.example/v1",
                    "LLM_API_KEY": "sk-first-origin-secret",
                    "persist": True,
                }, headers=headers)
                res = client.post("/api/config", json={
                    "LLM_BASE_URL": "https://two.example/v1",
                    "persist": True,
                }, headers=headers)
                self.assertEqual(res.status_code, 200)
                self.assertFalse(res.json["keys"]["LLM_API_KEY"])
                self.assertNotIn("sk-first-origin-secret", env_file.read_text(encoding="utf-8"))


class GeminiPreflightAndConfigReloadTest(unittest.TestCase):
    def test_rebuilt_create_app_loads_persisted_env_path_and_isolates_ambient_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st, env_file = root / "storage", root / ".env"
            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", ""), "LLM_PROVIDER": "apimart"}, clear=True):
                app1 = create_app(st, env_file)
                res = app1.test_client().post("/api/config", json={
                    "LLM_PROVIDER": "gemini",
                    "GEMINI_API_KEY": "gk-persisted-secret",
                    "GEMINI_COMPOSITION_MODEL": "gemini-2.5-pro",
                    "GEMINI_REWRITE_MODEL": "gemini-2.5-flash",
                    "persist": True,
                }, headers={"X-Workbench": "1"})
                self.assertEqual(res.status_code, 200)
                app1.extensions["jobs"].close()

            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                app2 = create_app(st, env_file)
                try:
                    cfg = app2.test_client().get("/api/bootstrap").json["config"]
                    self.assertEqual(cfg["LLM_PROVIDER"], "gemini")
                    self.assertTrue(cfg["keys"]["GEMINI_API_KEY"])
                    self.assertEqual(cfg["llm_providers"]["gemini"]["composition_model"], "gemini-2.5-pro")
                    self.assertEqual(cfg["llm_providers"]["gemini"]["rewrite_model"], "gemini-2.5-flash")
                    self.assertEqual(app2.extensions["jobs"].redact("err gk-persisted-secret"), "err [REDACTED]")
                finally:
                    app2.extensions["jobs"].close()

    def test_gemini_preflight_key_isolation_and_output_dir_none_guard(self):
        script = {"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "walk"}]}
        with patch("shutil.which", return_value="/fake/ffmpeg"):
            # Missing output_dir on pipeline must not raise TypeError
            preflight({"kind": "film", "demo": False, "film": script, "frames": "shot"},
                      {"RUNNINGHUB_API_KEY": "rh", "APIMART_API_KEY": "am"})
            # Gemini frames require both GEMINI_API_KEY and APIMART_API_KEY when uncached
            with self.assertRaises(InputError) as caught:
                preflight({"kind": "film", "demo": False, "operation": "frames", "frames": "shot", "film": script},
                          {"LLM_PROVIDER": "gemini", "APIMART_API_KEY": "img-only", "LLM_API_KEY": "legacy"})
            self.assertIn("GEMINI_API_KEY", caught.exception.values["names"])
            # frames="none" and kind="one" succeed with RUNNINGHUB_API_KEY + GEMINI_API_KEY without APIMART_API_KEY
            preflight({"kind": "film", "demo": False, "operation": "pipeline", "frames": "none", "film": script},
                      {"LLM_PROVIDER": "gemini", "RUNNINGHUB_API_KEY": "rh", "GEMINI_API_KEY": "gk"})
            preflight({"kind": "one", "demo": False, "prompt": "中文提示词"},
                      {"LLM_PROVIDER": "gemini", "RUNNINGHUB_API_KEY": "rh", "GEMINI_API_KEY": "gk"})
            # operation="frame" manual edit needs only APIMART_API_KEY
            preflight({"kind": "film", "demo": False, "operation": "frame", "frames": "shot", "film": script},
                      {"LLM_PROVIDER": "gemini", "APIMART_API_KEY": "am"})


class SecurityAndHttpHardeningTest(unittest.TestCase):
    def test_config_rejects_unicode_line_separators_and_unexpected_error_validates_log_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st, env_file = root / "st", root / ".env"
            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                app = create_app(st, env_file)
                try:
                    client = app.test_client()
                    headers = {"X-Workbench": "1"}
                    for sep in ("\u2028", "\u2029", "\v", "\f", "\x85"):
                        res = client.post("/api/config", json={"LLM_MODEL": f"m{sep}CLAUDE_BIN=/tmp/evil"}, headers=headers)
                        self.assertEqual(res.status_code, 400)
                    self.assertFalse(env_file.exists())
                    with self.assertRaises(InputError):
                        settings.save_env(env_file, {"LLM_MODEL": "a\u2028CLAUDE_BIN=/tmp/evil"})

                    # Missing X-Workbench, foreign Origin, redacted secret on 400, symlinked .env, upload traversal
                    self.assertEqual(client.post("/api/config", json={"LLM_MODEL": "x"}).status_code, 400)
                    self.assertEqual(client.post("/api/config", json={"LLM_MODEL": "x"},
                                                 headers={"X-Workbench": "1", "Origin": "https://evil.example"}).status_code, 400)
                    bad_secret = client.post("/api/config", json={"GEMINI_API_KEY": "my-secret-val\ninj=1"}, headers=headers)
                    self.assertEqual(bad_secret.status_code, 400)
                    self.assertNotIn("my-secret-val", bad_secret.get_data(as_text=True))

                    target_env = root / "real.env"
                    target_env.write_text("", encoding="utf-8")
                    env_file.symlink_to(target_env)
                    with self.assertRaises(InputError):
                        settings.save_env(env_file, {"LLM_MODEL": "ok"})
                    env_file.unlink()

                    self.assertEqual(client.get("/api/uploads/..%2fsecret.png").status_code, 404)

                    # Unregistered / traversed identifier in unexpected_error must not create orphan .log
                    with patch.object(app.extensions["jobs"], "snapshot", side_effect=RuntimeError("boom")):
                        res500 = client.get("/api/jobs/..%5c..%5coutside")
                    self.assertEqual(res500.status_code, 500)
                    self.assertEqual(list((st / "jobs").glob("*.log")), [])
                finally:
                    app.extensions["jobs"].close()

    def test_remember_secrets_ignores_short_non_credential_tokens_and_redacts_snapshot_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st, env_file = root / "st", root / ".env"
            secret = "sk-live-apimart-secret-987654"
            with patch.dict(os.environ, {
                "PATH": os.environ.get("PATH", ""),
                "TOKENIZERS_PARALLELISM": "1",
                "HF_HUB_DISABLE_IMPLICIT_TOKEN": "false",
                "KEYBOARD": "us",
                "APIMART_API_KEY": secret,
            }, clear=True):
                app = create_app(st, env_file)
                try:
                    jobs = app.extensions["jobs"]
                    self.assertEqual(jobs.redact("[Shot 1] <Picture 1> false us"), "[Shot 1] <Picture 1> false us")
                    self.assertEqual(jobs.redact(f"Bearer {secret}"), "Bearer [REDACTED]")

                    out = root / "job1"
                    (out / "inputs").mkdir(parents=True)
                    script = {"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "walk"}]}
                    req = {"kind": "film", "demo": False, "frames": "shot", "operation": "frames", "output_dir": str(out), "lang": "en", "film": script}
                    (out / "inputs" / "film.json").write_text(json.dumps(script), encoding="utf-8")
                    (out / "inputs" / "request.json").write_text(json.dumps(req), encoding="utf-8")
                    film.Job(out).put("frames", "s1-1", error=f"401 key={secret}")
                    film.Job(out).put("segs", "0", error=f"seg err {secret}")
                    jobs.records["job1"] = {"id": "job1", "kind": "film", "demo": False, "output_dir": str(out),
                                            "created_at": "2026-11-15T00:00:00+0000", "status": "failed", "error": None, "lang": "en", "operation": "frames"}
                    jobs.save(jobs.records["job1"])
                    snap = jobs.snapshot("job1")
                    self.assertNotIn(secret, json.dumps(snap))
                finally:
                    app.extensions["jobs"].close()

    def test_http_5xx_slice_redaction_upload_stdin_config_and_download_fail_flags(self):
        secret = "sk-very-secret-token-value-12345"
        long_prefix = "x" * 220
        err500 = urllib.error.HTTPError("https://api.example/v1/chat", 502, "Bad Gateway", {},
                                        io.BytesIO(f"{long_prefix}{secret}".encode("utf-8")))
        self.addCleanup(err500.close)
        with patch("urllib.request.urlopen", side_effect=err500), patch.object(http.time, "sleep"):
            with self.assertRaises(RuntimeError) as caught:
                http.request("https://api.example/v1/chat", secret, {"a": 1}, tries=1)
            self.assertNotIn(secret, str(caught.exception))

        with tempfile.TemporaryDirectory() as tmp:
            tricky = Path(tmp) / "semi;colon,comma.png"
            Image.new("RGB", (8, 8), "red").save(tricky)
            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                captured["input"] = kwargs.get("input", "")
                return MagicMock(returncode=0, stdout='{"url": "https://cdn.example/ok.png"}', stderr="")

            with patch("subprocess.run", side_effect=fake_run):
                res = http.upload("https://api.example/upload", secret, tricky)
            self.assertEqual(res["url"], "https://cdn.example/ok.png")
            self.assertFalse(any(secret in str(arg) for arg in captured["cmd"]), "API key leaked in curl argv")

            # Download rejects non-http(s) schemes and passes -f --proto =http,https
            with self.assertRaises(RuntimeError):
                http.download("file:///etc/passwd", Path(tmp) / "out.bin")

            dl_cmd = {}
            def fake_dl(cmd, **kwargs):
                dl_cmd["cmd"] = cmd
                return MagicMock(returncode=22)

            with patch("subprocess.run", side_effect=fake_dl):
                with self.assertRaises(RuntimeError):
                    http.download("https://cdn.example/err.png", Path(tmp) / "out.png")
            self.assertIn("-f", dl_cmd["cmd"])
            self.assertIn("--", dl_cmd["cmd"])


class WindowsRetryPollingAndLifecycleTest(unittest.TestCase):
    def test_windows_permission_error_retried_in_write_json_and_publish_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "frame-edit.json"
            src = root / "source.bin"
            dst = root / "dest.bin"
            src.write_bytes(b"payload")
            orig_replace = Path.replace
            calls = {"count": 0}

            def flaky_replace(self_path, target_path):
                calls["count"] += 1
                if calls["count"] in (1, 3):
                    err = PermissionError(errno.EACCES, "in use", str(target_path))
                    err.winerror = 32
                    raise err
                return orig_replace(self_path, target_path)

            with patch.object(Path, "replace", flaky_replace), patch("time.sleep"):
                write_json(target, {"task": "paid-task-1"})
                segments.publish_file(src, dst)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["task"], "paid-task-1")
            self.assertEqual(dst.read_bytes(), b"payload")

    def test_non_json_4xx_create_rejections_clear_submission_unknown_and_poll_4xx_fails_fast(self):
        # RunningHub non-JSON 401 on create must raise SubmissionRejected and clear submission_unknown
        unknown = {"val": None}
        with patch.object(rh.config, "rh_key", return_value="bad-rh"), \
             patch.object(rh.http, "request", side_effect=http.HTTPResponseError(401, "401 Unauthorized")):
            with self.assertRaises(rh.SubmissionRejected):
                rh.submit_with_retry({}, log=lambda *_: None, record_submission=lambda v: unknown.update(val=v))
        self.assertIs(unknown["val"], False)

        # APIMart non-JSON 429 on create must raise SubmissionRejected
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(frames.config, "apimart_key", return_value="k"), \
             patch.object(frames.http, "request", side_effect=http.HTTPResponseError(429, "429 Too Many Requests")):
            rec_state = {}
            with self.assertRaises(frames.SubmissionRejected):
                frames.draw("p", [], "9:16", Path(tmp) / "f.png", record=lambda **kw: rec_state.update(kw))
            self.assertIs(rec_state.get("submission_unknown"), False)

        # frames.poll and rh.poll fail fast on 404/canceled, while 401/403 raises RuntimeError so paid tasks stay resumable
        with patch.object(frames.config, "apimart_key", return_value="k"), \
             patch.object(frames.http, "request", return_value={"error": {"code": 404, "message": "not found"}}):
            self.assertEqual(frames.poll("expired-task")["state"], "failed")
        with patch.object(frames.config, "apimart_key", return_value="k"), \
             patch.object(frames.http, "request", side_effect=http.HTTPResponseError(401, "401 Unauthorized")):
            with self.assertRaises(http.HTTPResponseError):
                frames.poll("auth-error-task")
        with patch.object(frames.config, "apimart_key", return_value="k"), \
             patch.object(frames.http, "request", return_value={"data": {"status": "canceled"}}):
            self.assertEqual(frames.poll("canceled-task")["state"], "failed")
        with patch.object(rh, "_post", return_value={"code": 401, "msg": "apikey error"}):
            with self.assertRaises(RuntimeError):
                rh.poll("bad-key-task")
        with patch.object(rh, "_post", return_value={"code": 0, "data": [{"fileType": "mp4", "fileUrl": ""}]}):
            self.assertEqual(rh.poll("empty-url-task")["state"], "failed")

    def test_upload_two_stage_escaping_http_200_scrub_redirect_strip_and_reference_allowlist(self):
        secret = "sk-secret-200-body-99999"
        # HTTP 200 error envelope echoing key must be scrubbed by http.request
        fake_resp = MagicMock()
        fake_resp.__enter__.return_value.read.return_value = json.dumps({"error": f"bad {secret}"}).encode("utf-8")
        with patch("h3ref.http._OPENER.open", return_value=fake_resp):
            out = http.request("https://api.example/v1/chat", secret, {"a": 1}, tries=1)
        self.assertNotIn(secret, json.dumps(out))

        # Cross-origin redirect strips Authorization; https->http downgrade raises
        handler = http._SafeRedirectHandler()
        orig = urllib.request.Request("https://api.example/v1/chat", headers={"Authorization": f"Bearer {secret}"})
        redirected = handler.redirect_request(orig, None, 302, "Found", {}, "https://other.example/v1/chat")
        self.assertFalse(redirected.has_header("Authorization"))
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(orig, None, 302, "Found", {}, "http://api.example/v1/chat")

        # Upload uses form-string for fields, -f flag, and double-escapes Windows backslashes & quotes
        captured = {}
        def fake_upload_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input", "")
            return MagicMock(returncode=0, stdout='{"url": "https://cdn.example/win.png"}', stderr="")

        with patch("subprocess.run", side_effect=fake_upload_run):
            http.upload("https://api.example/up", secret, Path(r"C:\Users\temp\frames\s1-1.png"), fields={"apiKey": "@not-a-file;x"})
        self.assertIn("-w", captured["cmd"])
        self.assertIn("--proto", captured["cmd"])
        self.assertIn("form-string =", captured["input"])
        self.assertIn(r"C:\\\\Users\\\\temp\\\\frames\\\\s1-1.png", captured["input"])

        # Reference endpoints reject tampered non-image / non-cast paths (e.g. .env)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st, env_file = root / "st", root / ".env"
            env_file.write_text("GEMINI_API_KEY=top-secret-env-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                app = create_app(st, env_file)
                jobs = app.extensions["jobs"]
                client = app.test_client()
                try:
                    # Session-only origin change sets empty string in env so child workers with H3_SKIP_DOTENV never reload .env
                    client.post("/api/config", json={"LLM_BASE_URL": "https://one.example/v1", "LLM_API_KEY": "sk-session-12345", "persist": False}, headers={"X-Workbench": "1"})
                    r_clear = client.post("/api/config", json={"LLM_BASE_URL": "https://two.example/v1", "persist": False}, headers={"X-Workbench": "1"})
                    self.assertFalse(r_clear.json["keys"]["LLM_API_KEY"])

                    out_dir = root / "tampered"
                    (out_dir / "inputs").mkdir(parents=True)
                    actor = root / "actor.png"
                    Image.new("RGB", (8, 8), "red").save(actor)
                    script = {"aspect": "9:16", "cast": [{"name": "Alice", "picture": str(actor)}], "shots": [{"start": 0, "end": 4, "action": "Alice walks"}]}
                    req = {"kind": "film", "demo": False, "frames": "shot", "operation": "frames", "output_dir": str(out_dir), "lang": "en", "film": script}
                    (out_dir / "inputs" / "film.json").write_text(json.dumps(script), encoding="utf-8")
                    (out_dir / "inputs" / "request.json").write_text(json.dumps(req), encoding="utf-8")
                    film.Job(out_dir).put("frames", "s1-1", character_reference_paths=[str(env_file)])
                    jobs.records["t1"] = {"id": "t1", "kind": "film", "demo": False, "output_dir": str(out_dir),
                                          "created_at": "2026-11-15T00:00:00+0000", "status": "succeeded", "error": None, "lang": "en", "operation": "frames"}
                    self.assertEqual(client.get("/api/jobs/t1/frames/s1-1/references/0").status_code, 404)
                finally:
                    jobs.close()

    def test_stage_frames_400_falls_back_per_shot_and_stage_prompts_clears_stale_h3_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = film.Job(root / "fjob")
            script = {"aspect": "9:16", "shots": [{"start": 0, "end": 2, "action": "shot 1"}, {"start": 2, "end": 4, "action": "shot 2"}]}
            segs = film.plan(script["shots"])
            pics = {s["idx"]: film.refs(script, s, "shot") for s in segs}
            calls = {"n": 0}
            def desc_side_effect(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise http.HTTPResponseError(400, "400 safety filter on shot 1")
                return SAMPLE_V2_COMPOSITION
            with patch.object(frames, "describe_composition", side_effect=desc_side_effect), \
                 patch.object(frames, "draw", side_effect=lambda p, r, a, o, **k: (o.parent.mkdir(exist_ok=True) or Image.new("RGB", (8, 8), "blue").save(o) or 0.1)):
                failures = film.stage_frames(job, script, segs, pics)
            self.assertEqual(failures, ["s1-1"])
            self.assertTrue(job.state["frames"]["s1-1"].get("prompt"))
            self.assertTrue((job.dir / "frames" / "s1-2.png").is_file())

            # stage_prompts clears stale h3 when zh changes and rewrite fails
            pjob = film.Job(root / "pjob")
            pjob.put("segs", "0", zh="old zh", h3="stale-old-h3", h3_source="old zh")
            with patch.object(rewrite, "rewrite", side_effect=rewrite.RewriteError("llm down")):
                with self.assertRaises(rewrite.RewriteError):
                    film.stage_prompts(pjob, script, segs[:1], {0: []})
            self.assertFalse(pjob.state["segs"]["0"].get("h3"))

    def test_jobs_run_finally_cleans_children_on_stop_child_error_and_init_pops_stale_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st = root / "st"
            (st / "jobs").mkdir(parents=True)
            stale = {"id": "stale1", "kind": "one", "demo": True, "output_dir": str(root / "out"),
                     "created_at": "2026-11-15T00:00:00+0000", "status": "running", "pid": os.getpid(), "error": None, "lang": "en"}
            (st / "jobs" / "stale1.json").write_text(json.dumps(stale), encoding="utf-8")
            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                app = create_app(st, root / ".env")
                jobs = app.extensions["jobs"]
                try:
                    self.assertNotIn("pid", jobs.records["stale1"])
                    jobs.available()  # Must not fail because os.getpid() is alive!

                    # Simulate stop_child raising PermissionError while child is still alive vs exited
                    rec = {"id": "run1", "kind": "one", "demo": True, "output_dir": str(root / "out2"),
                           "created_at": "2026-11-15T00:00:00+0000", "status": "queued", "error": None, "lang": "en", "operation": "pipeline"}
                    (root / "out2").mkdir()
                    jobs.records["run1"] = rec
                    fake_proc = MagicMock(pid=999999, stdout=iter(["hello\n"]), wait=lambda *a, **k: 0)
                    with patch("subprocess.Popen", return_value=fake_proc), \
                         patch("workbench.jobs.stop_posix_descendants_on_exit"), \
                         patch("workbench.jobs.stop_child", side_effect=PermissionError("EPERM")):
                        jobs.run(rec, {"kind": "one", "demo": True, "output_dir": str(root / "out2")}, {})
                    self.assertNotIn("run1", jobs.children)
                    self.assertEqual(jobs.records["run1"].get("pid"), 999999, "Failed stop_child must retain pid until process_alive confirms exit")
                    with patch("workbench.jobs.process_alive", return_value=True):
                        with self.assertRaises(InputError) as live_ctx:
                            jobs.available()
                        self.assertEqual(live_ctx.exception.key, "alive")
                    with patch("workbench.jobs.process_alive", return_value=False):
                        jobs.available()
                    self.assertNotIn("pid", jobs.records["run1"])
                finally:
                    jobs.close()

    def test_last_frame_retries_wider_sseof_and_invalidation_clears_stale_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = film.Job(root)
            (root / "clips").mkdir()
            (root / "clips" / "seg1.mp4").write_bytes(b"old")
            tail = root / "clips" / "seg1-last.jpg"
            tail.write_bytes(b"stale-tail")
            job.put("segs", "0", zh="s", h3="h", file=str(root / "clips" / "seg1.mp4"), cny=1)
            film.invalidate_segments(job, "first", [1])
            self.assertFalse(tail.exists())

            offsets = []
            out_tail = root / "extracted.jpg"
            def fake_ff(*args):
                idx = args.index("-sseof")
                offset = args[idx + 1]
                offsets.append(offset)
                if offset != "-0.15":
                    Image.new("RGB", (8, 8), "green").save(out_tail)

            with patch.object(media, "ff", side_effect=fake_ff):
                media.last_frame(root / "video.mp4", out_tail)
            self.assertGreater(len(offsets), 1)
            self.assertTrue(out_tail.is_file())


class DirectoriesPackagingAndPerfTest(unittest.TestCase):
    def test_output_directories_validation_collision_and_picker_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st = root / ".workbench"
            st.mkdir()
            out_root = root / "outputs"
            out_root.mkdir()
            with self.assertRaises(InputError):
                directories.validate_output_root(str(st / "nested"), root, st)
            with patch.object(directories.time, "strftime", return_value="2026-11-15_12-00-00"):
                d1 = directories.create_output_directory(str(out_root), root, st)
                d2 = directories.create_output_directory(str(out_root), root, st)
            self.assertEqual(d1.name, "2026-11-15_12-00-00")
            self.assertEqual(d2.name, "2026-11-15_12-00-00-001")

            saved = directories.save_output_root(str(out_root), root, st)
            self.assertEqual(directories.saved_output_root(root, st), saved)
            (st / "preferences.json").write_text("{corrupt-json", encoding="utf-8")
            self.assertEqual(directories.saved_output_root(root, st), root / "jobs")

            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=str(out_root) + "\n", stderr="")), \
                 patch.object(directories.sys, "platform", "darwin"):
                self.assertEqual(directories.choose_output_folder(out_root, "en"), str(out_root))

            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                app = create_app(st, root / ".env")
                try:
                    # Rewrite guide 0-byte / whitespace / atomic cache
                    cache_dir = root / "cache"
                    (cache_dir).mkdir()
                    (cache_dir / "ref-en.txt").write_bytes(b"")
                    resp_ws = MagicMock()
                    resp_ws.__enter__.return_value.read.return_value = b"   \n  "
                    with patch.object(rewrite.config, "GUIDE_FILE", ""), patch.object(rewrite.config, "CACHE", cache_dir):
                        with patch("urllib.request.urlopen", return_value=resp_ws):
                            with self.assertRaises(rewrite.RewriteError):
                                rewrite.guide()
                        resp_ok = MagicMock()
                        resp_ok.__enter__.return_value.read.return_value = b"OFFICIAL GUIDE TEXT"
                        with patch("urllib.request.urlopen", return_value=resp_ok):
                            self.assertEqual(rewrite.guide(), "OFFICIAL GUIDE TEXT")

                    # Single-instance lock rejects concurrent second lock
                    from workbench.instance import single_instance
                    lock_dir = root / "lock-st"
                    with single_instance(lock_dir):
                        with self.assertRaises(SystemExit):
                            with single_instance(lock_dir):
                                pass

                    # Demo run produces demo-film.json, demo-frames.json, seg1-last.jpg, and report.md
                    from workbench import demo
                    demo_out = root / "demo-out"
                    (demo_out / "inputs").mkdir(parents=True)
                    demo_req = {"kind": "film", "demo": True, "frames": "first", "operation": "pipeline",
                                "output_dir": str(demo_out), "lang": "en",
                                "film": {"aspect": "9:16", "cast": [{"name": "A", "picture": str(demo_out / "missing.png")}],
                                         "shots": [{"start": 0, "end": 5, "action": "A walks"}]}}
                    with patch("shutil.which", return_value="/fake/ffmpeg"), \
                         patch("workbench.demo.subprocess.run", side_effect=lambda cmd, **kw: Path(cmd[-1]).write_bytes(b"mp4")), \
                         patch.object(media, "last_frame", side_effect=lambda c, o: o.write_bytes(b"jpg")):
                        demo.run(demo_req)
                    self.assertTrue((demo_out / "inputs" / "demo-frames.json").is_file())
                    self.assertTrue((demo_out / "clips" / "seg1-last.jpg").is_file())
                    self.assertTrue((demo_out / "report.md").is_file())

                    # Folder picker lock contention & POSIX worker.lock orphan detection
                    import threading
                    headers = {"Origin": "http://localhost", "X-Workbench": "1", "Accept-Language": "en"}
                    entered = threading.Event()
                    release_picker = threading.Event()

                    def slow_picker(*_a, **_kw):
                        entered.set()
                        release_picker.wait(timeout=5)
                        return None

                    with patch("workbench.server.choose_output_folder", side_effect=slow_picker):
                        t1 = threading.Thread(target=lambda: app.test_client().post("/api/output-folder/pick", json={}, headers=headers))
                        t1.start()
                        self.assertTrue(entered.wait(timeout=5))
                        busy_pick = app.test_client().post("/api/output-folder/pick", json={}, headers=headers)
                        self.assertEqual(busy_pick.status_code, 400)
                        self.assertIn("already open", busy_pick.get_json()["error"])
                        release_picker.set()
                        t1.join(timeout=5)

                    if os.name != "nt":
                        import fcntl
                        from workbench import jobs as jobs_mod
                        orphan_out = root / "orphan-out"
                        (orphan_out / "inputs").mkdir(parents=True)
                        with (orphan_out / "inputs" / "worker.lock").open("a+b") as lock_fh:
                            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            self.assertTrue(jobs_mod.worker_locked(str(orphan_out)))
                            app.extensions["jobs"].records["orphan"] = {
                                "id": "orphan", "kind": "one", "demo": True, "output_dir": str(orphan_out),
                                "created_at": "2026-11-15T00:00:00+0000", "status": "interrupted", "error": None, "lang": "en",
                            }
                            with self.assertRaises(InputError) as ctx:
                                app.extensions["jobs"].available()
                            self.assertEqual(ctx.exception.key, "alive")
                            app.extensions["jobs"].records.pop("orphan", None)
                        self.assertFalse(jobs_mod.worker_locked(str(orphan_out)))
                finally:
                    app.extensions["jobs"].close()

    def test_windows_package_excludes_secrets_rejects_symlinks_and_normalizes_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            for rel in package_windows.FILES:
                p = repo / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("line1\nline2\n", encoding="utf-8")
            for d in package_windows.DIRECTORIES:
                (repo / d).mkdir(parents=True, exist_ok=True)
                (repo / d / "mod.py").write_text("x = 1\n", encoding="utf-8")
                (repo / d / "__pycache__").mkdir(exist_ok=True)
                (repo / d / "__pycache__" / "mod.pyc").write_bytes(b"pyc")
            blank = repo / "workbench/static/vendor/plyr/blank.mp4"
            blank.parent.mkdir(parents=True, exist_ok=True)
            blank.write_bytes(b"mp4")
            (repo / ".env").write_text("GEMINI_API_KEY=secret\n", encoding="utf-8")
            (repo / ".workbench").mkdir()
            (repo / "jobs").mkdir()

            dest = Path(tmp) / "bundle.zip"
            package_windows.build_package(repo, dest)
            with zipfile.ZipFile(dest) as zf:
                names = set(zf.namelist())
                self.assertNotIn("h3-ref-video/.env", names)
                self.assertFalse(any("__pycache__" in n or ".workbench" in n or "/jobs/" in n for n in names))
                self.assertIn(b"\r\n", zf.read("h3-ref-video/start-windows.cmd"))
                self.assertIn("h3-ref-video/SHA256SUMS.txt", names)

            bad_link = repo / "h3ref" / "linked.py"
            bad_link.symlink_to(repo / "h3ref" / "mod.py")
            with self.assertRaises(ValueError):
                package_windows.package_files(repo)

    def test_backend_summaries_limit_single_frame_ref_and_artifact_etag_caching(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st, env_file = root / "st", root / ".env"
            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                app = create_app(st, env_file)
                jobs = app.extensions["jobs"]
                client = app.test_client()
                try:
                    for idx in range(25):
                        jid = f"j{idx:02d}"
                        jobs.records[jid] = {"id": jid, "kind": "one", "demo": True, "output_dir": str(root / jid),
                                             "created_at": f"2026-11-15T00:00:{idx:02d}+0000", "status": "succeeded", "error": None, "lang": "en"}
                    self.assertEqual(len(jobs.summaries(limit=10)), 10)

                    # Single frame reference endpoint does not replan all shots via frames.prompt
                    out = root / "filmjob"
                    (out / "inputs").mkdir(parents=True)
                    (out / "frames").mkdir()
                    actor = root / "actor.png"
                    Image.new("RGB", (8, 8), "red").save(actor)
                    frame_png = out / "frames" / "s1-1.png"
                    Image.new("RGB", (8, 8), "blue").save(frame_png)
                    script = {"aspect": "9:16", "cast": [{"name": "小林", "picture": str(actor)}],
                              "shots": [{"start": 0, "end": 2, "action": "小林走"}, {"start": 2, "end": 4, "action": "小林停"}]}
                    req = {"kind": "film", "demo": False, "frames": "shot", "operation": "frames", "output_dir": str(out), "lang": "en", "film": script}
                    (out / "inputs" / "film.json").write_text(json.dumps(script), encoding="utf-8")
                    (out / "inputs" / "request.json").write_text(json.dumps(req), encoding="utf-8")
                    jobs.records["f1"] = {"id": "f1", "kind": "film", "demo": False, "output_dir": str(out),
                                          "created_at": "2026-11-15T01:00:00+0000", "status": "succeeded", "error": None, "lang": "en", "operation": "frames"}
                    with patch("h3ref.frames.prompt", side_effect=AssertionError("replanned all shots")):
                        with client.get("/api/jobs/f1/frames/s1-1/references/0") as r_ref:
                            self.assertEqual(r_ref.status_code, 200)

                    # Artifact endpoint supports ETag 304 without Cache-Control: no-store overriding media ETag
                    with patch.object(jobs, "artifacts", side_effect=AssertionError("scanned all artifacts")):
                        with client.get("/api/jobs/f1/files/frames/s1-1.png") as r_media:
                            self.assertEqual(r_media.status_code, 200)
                            self.assertNotEqual(r_media.headers.get("Cache-Control"), "no-store")
                            etag = r_media.headers.get("ETag")
                            self.assertTrue(etag)
                    with client.get("/api/jobs/f1/files/frames/s1-1.png", headers={"If-None-Match": etag}) as r_304:
                        self.assertEqual(r_304.status_code, 304)
                finally:
                    jobs.close()

    def test_python_310_ast_syntax_compatibility_across_all_source_files(self):
        repo_root = Path(__file__).resolve().parents[1]
        targets = [repo_root / "h3ref.py", repo_root / "workbench.py"]
        for folder in ("h3ref", "workbench", "scripts", "tests"):
            targets.extend(sorted((repo_root / folder).rglob("*.py")))
        for path in targets:
            with self.subTest(path=str(path.relative_to(repo_root))):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))

    def test_401_429_json_errors_short_key_upload_4xx_and_submit_shapes(self):
        # 1. HTTP 200 with short local key ("2") must not corrupt JSON before json.loads
        ok_resp = MagicMock()
        ok_resp.__enter__.return_value.read.return_value = b'{"version": 2, "choices": [{"message": {"content": "ok"}}]}'
        with patch("urllib.request.urlopen", return_value=ok_resp):
            parsed = http.request("http://localhost:11434/v1/chat/completions", "2", {"model": "local"})
        self.assertEqual(parsed["version"], 2)

        # 2. stage_frames aborts on 401 JSON dict (invalid_api_key) and Gemini 400 API_KEY_INVALID without poisoning shots
        for code, body in (
            (401, {"error": {"code": "invalid_api_key", "message": "Incorrect API key"}}),
            (400, [{"error": {"code": 400, "message": "API key not valid", "details": [{"reason": "API_KEY_INVALID"}]}}]),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                job = film.Job(Path(tmp))
                script = {"aspect": "9:16", "shots": [{"start": 0, "end": 2, "action": "s1"}, {"start": 2, "end": 4, "action": "s2"}]}
                segs = film.plan(script["shots"])
                err = urllib.error.HTTPError("https://api.example/v1/chat/completions", code, "Auth", {},
                                             io.BytesIO(json.dumps(body).encode("utf-8")))
                try:
                    with patch.dict(os.environ, {"LLM_PROVIDER": "apimart", "LLM_API_KEY": "bad-secret-123"}, clear=True), \
                         patch("urllib.request.urlopen", side_effect=err):
                        with self.assertRaises(http.HTTPResponseError):
                            film.stage_frames(job, script, segs, {s["idx"]: film.refs(script, s, "shot") for s in segs})
                    self.assertEqual(job.state["frames"], {})
                finally:
                    err.close()

        # 3. 429 JSON error during frames.poll raises transient RuntimeError instead of marking task failed
        err_429 = urllib.error.HTTPError("https://api.apimart.ai/v1/tasks/t-1", 429, "Too Many Requests", {},
                                         io.BytesIO(b'{"error": {"code": "rate_limit_exceeded", "message": "Slow down"}}'))
        try:
            with patch.dict(os.environ, {"APIMART_API_KEY": "am-key"}, clear=True), \
                 patch("urllib.request.urlopen", side_effect=err_429), \
                 patch("time.sleep"):
                with self.assertRaises(RuntimeError):
                    frames.poll("t-1")
        finally:
            err_429.close()

        # 4. Unexpected data container shapes in frames.submit and rh.submit raise SubmissionRejected / QueueFull cleanly
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "r.png"
            Image.new("RGB", (8, 8)).save(img)
            with patch.dict(os.environ, {"APIMART_API_KEY": "am-key"}, clear=True), \
                 patch.object(frames, "_upload", return_value="https://img/r.jpg"), \
                 patch.object(frames.http, "request", return_value={"error": {"message": "bad req"}, "data": "not-a-list"}):
                with self.assertRaises(frames.SubmissionRejected):
                    frames.submit("prompt", [img], "9:16")
            with patch.object(rh, "_post", return_value={"code": 421, "msg": "QUEUE_MAXED", "data": "busy"}):
                with self.assertRaises(rh.QueueFull):
                    rh.submit({})

        # 5. http.upload preserves 4xx JSON moderation body without 4x retry
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "m.jpg"
            img.write_bytes(b"jpg")
            fake_curl = MagicMock(returncode=0, stdout='{"error":{"message":"content_policy_violation"}}\n400', stderr="")
            with patch("subprocess.run", return_value=fake_curl) as run_mock, \
                 patch.dict(os.environ, {"APIMART_API_KEY": "am-key"}, clear=True):
                with self.assertRaises(frames.ContentPolicyError):
                    frames._upload(img)
                self.assertEqual(run_mock.call_count, 1)

    def test_session_then_persist_origin_change_seed_zero_and_english_lang_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st, env_file = root / "st", root / ".env"
            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                app = create_app(st, env_file)
                client = app.test_client()
                headers = {"X-Workbench": "1"}
                try:
                    # Save origin A + LLM_API_KEY to disk, then switch to origin B in memory (persist=False), then persist=True
                    r1 = client.post("/api/config", json={"LLM_BASE_URL": "https://a.example/v1", "LLM_API_KEY": "sk-secret-origin-a", "persist": True}, headers=headers)
                    self.assertEqual(r1.status_code, 200)
                    r2 = client.post("/api/config", json={"LLM_BASE_URL": "https://b.example/v1", "persist": False}, headers=headers)
                    self.assertEqual(r2.status_code, 200)
                    r3 = client.post("/api/config", json={"LLM_BASE_URL": "https://b.example/v1", "LLM_MODEL": "m2", "persist": True}, headers=headers)
                    self.assertEqual(r3.status_code, 200)
                    self.assertNotIn("sk-secret-origin-a", env_file.read_text(encoding="utf-8"))
                    # Malformed bracketed / hostless URLs return 400 instead of 500
                    for bad_url in ("http://[invalid", "http://:80/v1", "http://example.com:99999/v1"):
                        self.assertEqual(client.post("/api/config", json={"LLM_BASE_URL": bad_url}, headers=headers).status_code, 400)
                finally:
                    app.extensions["jobs"].close()

            # seed=0 preserved in cmd_one and terminal_failed blocks duplicate rerun
            cli_main = _load_cli()
            out = root / "one-zero"
            img, prompt = root / "a.png", root / "p.txt"
            Image.new("RGB", (8, 8)).save(img)
            h3_text = "\n\n".join(f"{s}:\n<Picture 1> test" if s == "subject_definitions" else f"{s}:\nN/A" if s == "non_diegetic_music" else f"{s}:\nvalid line" for s in rewrite.SECTIONS)
            prompt.write_text(h3_text, encoding="utf-8")
            args = SimpleNamespace(out=str(out), image=[str(img)], prompt=str(prompt), seed=0, seconds=5, aspect="9:16", check=False)
            with patch.object(cli_main.rh, "upload", return_value="a.png"), \
                 patch.object(cli_main.rh, "graph", return_value={}) as graph_fn, \
                 patch.object(cli_main.rh, "submit_with_retry", return_value="t-zero") as sub_fn, \
                 patch.object(cli_main.rh, "poll", return_value={"state": "failed", "error": "boom"}):
                with self.assertRaises(RuntimeError):
                    cli_main.cmd_one(args)
                self.assertEqual(graph_fn.call_args.args[4], 0)
                with self.assertRaises(RuntimeError):
                    cli_main.cmd_one(args)
                self.assertEqual(sub_fn.call_count, 1)

            # English job preflight matches H3_LANG="en" compiled v2 prompt
            en_out = root / "en-film"
            (en_out / "frames").mkdir(parents=True)
            Image.new("RGB", (8, 8)).save(en_out / "frames" / "s1-1.png")
            en_script = {"aspect": "9:16", "cast": [{"name": "小林", "picture": str(img)}], "shots": [{"start": 0, "end": 5, "action": "走"}]}
            en_segs = film.plan(en_script["shots"])
            en_pics = film.refs(en_script, en_segs[0], "shot")
            ref_ver = film.reference_version(film.picture_paths(en_out, en_segs[0], en_pics))
            frame_ref_ver = film.frame_reference_versions(en_script, en_segs, {})["s1-1"]
            with patch.dict(os.environ, {"H3_LANG": "en"}, clear=True):
                en_zh = film.compile_prompt(en_script, en_segs[0], en_pics, 1, compositions={"s1-1": SAMPLE_V2_COMPOSITION})
            (en_out / "state.json").write_text(json.dumps({
                "frames": {"s1-1": {"composition": SAMPLE_V2_COMPOSITION, "prompt": "p", "composition_prompt": "p", "composition_reference_version": frame_ref_ver}},
                "segs": {"0": {"zh": en_zh, "h3": h3_text, "h3_source": en_zh, "h3_reference_version": ref_ver}},
            }), encoding="utf-8")
            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True), \
                 patch("shutil.which", return_value="/usr/bin/ffmpeg"):
                preflight({"kind": "film", "demo": False, "frames": "shot", "operation": "pipeline", "reset_applied": True,
                           "output_dir": str(en_out), "lang": "en", "film": en_script}, {"RUNNINGHUB_API_KEY": "rh-ok"})

    def test_config_unchanged_custom_origin_keeps_key_and_separates_mem_disk_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st, env_file = root / "st", root / ".env"
            env_file.write_text(
                "LLM_BASE_URL=https://custom-proxy.example/v1\nLLM_API_KEY=sk-custom-origin-secret\n",
                encoding="utf-8",
            )
            seeded = {"LLM_BASE_URL": "https://custom-proxy.example/v1", "LLM_API_KEY": "sk-custom-origin-secret"}
            with patch.object(config, "_SEEDED_DOTENV", seeded), \
                 patch.dict(os.environ, {"PATH": os.environ.get("PATH", ""), "_H3_DOTENV_LOADED": "1", **seeded}, clear=True):
                app = create_app(st, env_file)
                client = app.test_client()
                headers = {"X-Workbench": "1"}
                try:
                    res = client.post("/api/config", json={
                        "LLM_BASE_URL": "https://custom-proxy.example/v1",
                        "LLM_MODEL": "deepseek-chat",
                        "persist": True,
                    }, headers=headers)
                    self.assertEqual(res.status_code, 200)
                    self.assertTrue(res.json["keys"]["LLM_API_KEY"], "Unchanged custom origin must not wipe LLM_API_KEY in memory")
                    self.assertIn("sk-custom-origin-secret", env_file.read_text(encoding="utf-8"))

                    # Session-only new origin + new key, followed by persisting new origin without re-typing key:
                    # disk must clear old origin's key while memory keeps new origin's key.
                    r_mem = client.post("/api/config", json={
                        "LLM_BASE_URL": "https://second-proxy.example/v1",
                        "LLM_API_KEY": "sk-second-session-secret",
                        "persist": False,
                    }, headers=headers)
                    self.assertEqual(r_mem.status_code, 200)
                    r_persist = client.post("/api/config", json={
                        "LLM_BASE_URL": "https://second-proxy.example/v1",
                        "LLM_MODEL": "deepseek-reasoner",
                        "persist": True,
                    }, headers=headers)
                    self.assertEqual(r_persist.status_code, 200)
                    self.assertTrue(r_persist.json["keys"]["LLM_API_KEY"], "Persisting current memory origin must not wipe memory key")
                    self.assertNotIn("sk-custom-origin-secret", env_file.read_text(encoding="utf-8"))
                finally:
                    app.extensions["jobs"].close()

    def test_stage_frames_planning_moderation_and_short_key_http_upload_4xx(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = film.Job(Path(tmp) / "mod-job")
            script = {"aspect": "9:16", "shots": [{"start": 0, "end": 3, "action": "walk"}]}
            segs = film.plan(script["shots"])
            pics = {s["idx"]: film.refs(script, s, "shot") for s in segs}
            with patch.object(frames, "describe_composition", side_effect=frames.ContentPolicyError("content_policy_violation")), \
                 patch.object(frames, "draw") as draw_mock:
                failures = film.stage_frames(job, script, segs, pics)
            self.assertEqual(failures, ["s1-1"])
            draw_mock.assert_not_called()
            self.assertEqual(job.state["frames"]["s1-1"].get("moderation_error"), "content_policy_violation")

            # Short local key ("4") must not corrupt 4xx JSON or curl %{http_code}
            err_400 = urllib.error.HTTPError(
                "http://localhost:11434/v1/chat/completions", 400, "Bad Request", {},
                io.BytesIO(b'{"error": {"code": 401, "message": "bad model"}}'),
            )
            try:
                with patch("urllib.request.urlopen", side_effect=err_400):
                    parsed_err = http.request("http://localhost:11434/v1/chat/completions", "4", {"model": "m"})
                self.assertEqual(parsed_err["error"]["code"], 401)
                self.assertEqual(parsed_err["_http_status"], 400)
            finally:
                err_400.close()

            upload_file = Path(tmp) / "u.png"
            Image.new("RGB", (8, 8)).save(upload_file)
            fake_curl = MagicMock(returncode=0, stdout='{"error": {"code": 400, "message": "bad"}}\n400', stderr="")
            with patch("subprocess.run", return_value=fake_curl) as run_mock:
                up_res = http.upload("http://localhost:8080/upload", "4", upload_file)
            self.assertEqual(run_mock.call_count, 1)
            self.assertEqual(up_res["_http_status"], 400)

    def test_artifact_negative_paths_lock_scope_and_reference_etag_stat_caching(self):
        import threading
        from workbench import storyboard as sb_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st, env_file = root / "st", root / ".env"
            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                app = create_app(st, env_file)
                jobs = app.extensions["jobs"]
                client = app.test_client()
                try:
                    out = root / "fjob"
                    for sub in ("inputs", "frames", "clips", "prompts"):
                        (out / sub).mkdir(parents=True)
                    actor = root / "actor.png"
                    Image.new("RGB", (8, 8), "red").save(actor)
                    shots = []
                    for idx in range(20):
                        Image.new("RGB", (8, 8), "blue").save(out / "frames" / f"s{idx + 1}-1.png")
                        shots.append({"start": idx * 5, "end": (idx + 1) * 5, "action": "小林走"})
                    script = {"aspect": "9:16", "cast": [{"name": "小林", "picture": str(actor)}], "shots": shots}
                    req = {"kind": "film", "demo": False, "frames": "shot", "operation": "frames", "output_dir": str(out), "lang": "en", "film": script}
                    (out / "inputs" / "film.json").write_text(json.dumps(script), encoding="utf-8")
                    (out / "inputs" / "request.json").write_text(json.dumps(req), encoding="utf-8")
                    (out / "state.json").write_text(json.dumps({"frames": {}, "segs": {}}), encoding="utf-8")
                    (out / "run.log").write_text("internal log\n", encoding="utf-8")
                    jobs.records["f1"] = {"id": "f1", "kind": "film", "demo": False, "output_dir": str(out),
                                          "created_at": "2026-11-15T01:00:00+0000", "status": "succeeded", "error": None, "lang": "en", "operation": "frames"}

                    # 1. Negative artifact allowlist / traversal tests
                    for forbidden in ("state.json", "run.log", "inputs/request.json", "inputs/film.json",
                                      "frames/../state.json", "frames%2f..%2fstate.json"):
                        with self.subTest(forbidden=forbidden):
                            self.assertEqual(client.get(f"/api/jobs/f1/files/{forbidden}").status_code, 404)

                    # 2. Artifact serving skips reading state.json when segment-edit.json is absent
                    orig_read_json = segments.read_json
                    state_reads = []
                    def track_read_json(path, default):
                        if Path(path).name == "state.json":
                            state_reads.append(str(path))
                        return orig_read_json(path, default)
                    with patch.object(segments, "read_json", side_effect=track_read_json):
                        with client.get("/api/jobs/f1/files/frames/s1-1.png") as resp_img:
                            self.assertEqual(resp_img.status_code, 200)
                    self.assertEqual(state_reads, [], "Artifact endpoint must not parse state.json when segment-edit.json is absent")

                    # 3. jobs.snapshot does not hold jobs.lock during storyboard.view disk I/O
                    orig_view = sb_mod.view
                    lock_free = {"ok": False}
                    def probe_lock_view(*args, **kwargs):
                        entered = threading.Event()
                        def try_lock():
                            if jobs.lock.acquire(blocking=False):
                                lock_free["ok"] = True
                                jobs.lock.release()
                            entered.set()
                        t = threading.Thread(target=try_lock)
                        t.start()
                        entered.wait(timeout=2)
                        t.join(timeout=2)
                        return orig_view(*args, **kwargs)
                    with patch.object(sb_mod, "view", side_effect=probe_lock_view):
                        jobs.snapshot("f1")
                    self.assertTrue(lock_free["ok"], "jobs.snapshot must release jobs.lock before view disk I/O")

                    # 4. Reference endpoints support ETag 304 and avoid re-running verify_image on unchanged files
                    verify_calls = {"count": 0}
                    from workbench import server as server_mod
                    orig_verify = server_mod.verify_image
                    def counted_verify(p):
                        verify_calls["count"] += 1
                        return orig_verify(p)
                    with patch.object(server_mod, "verify_image", side_effect=counted_verify):
                        with client.get("/api/jobs/f1/characters/0") as rc1:
                            self.assertEqual(rc1.status_code, 200)
                            c_etag = rc1.headers.get("ETag")
                            self.assertTrue(c_etag)
                        with client.get("/api/jobs/f1/characters/0", headers={"If-None-Match": c_etag}) as rc304:
                            self.assertEqual(rc304.status_code, 304)
                        with client.get("/api/jobs/f1/frames/s1-1/references/0") as rf1:
                            self.assertEqual(rf1.status_code, 200)
                            f_etag = rf1.headers.get("ETag")
                            self.assertTrue(f_etag)
                        with client.get("/api/jobs/f1/frames/s1-1/references/0", headers={"If-None-Match": f_etag}) as rf304:
                            self.assertEqual(rf304.status_code, 304)
                    self.assertEqual(verify_calls["count"], 1, "Unchanged verified reference image must be verified once and cached")

                    # 5. storyboard.view caches shared reference stat() calls across 20 shots
                    orig_stat = Path.stat
                    actor_resolved = actor.resolve()
                    actor_stats = {"count": 0}
                    def counted_stat(p, *a, **kw):
                        if p == actor or p == actor_resolved:
                            actor_stats["count"] += 1
                        return orig_stat(p, *a, **kw)
                    arts = jobs.artifacts(jobs.records["f1"])
                    with patch.object(Path, "stat", counted_stat):
                        sb_mod.view(req, arts, True, "f1", records={})
                    self.assertLessEqual(actor_stats["count"], 4, f"Expected cached actor.png stat calls, got {actor_stats['count']}")
                finally:
                    jobs.close()

    def test_frames_poll_200_500_envelope_and_rh_poll_proxy_404_remain_transient(self):
        with patch.object(frames.config, "apimart_key", return_value="k"), \
             patch.object(frames.http, "request", return_value={"code": 500, "error": {"code": 500, "message": "Internal server error"}}):
            with self.assertRaises(RuntimeError):
                frames.poll("transient-500-task")
        with patch.object(frames.config, "apimart_key", return_value="k"), \
             patch.object(frames.http, "request", return_value={"error": {"code": 404, "message": "not found"}}):
            self.assertEqual(frames.poll("missing-404-task")["state"], "failed")
        with patch.object(rh, "_post", side_effect=http.HTTPResponseError(404, "404 Not Found")):
            with self.assertRaises(http.HTTPResponseError):
                rh.poll("proxy-404-video-task")
        with patch.object(rh, "_post", return_value={"_http_status": 404, "error": "gateway route missing"}):
            with self.assertRaises(RuntimeError):
                rh.poll("json-404-route-task")
        with patch.object(rh, "_post", return_value={"code": 807, "msg": "task not exist"}):
            self.assertEqual(rh.poll("missing-video-task")["state"], "failed")

    # Generated by /ship coverage audit
    # Value: protects=GET /api/film/example strips local paths and HTML/download routes enforce CSP and attachment headers; fails_when=example JSON leaks cast picture paths, pages omit CSP/no-store headers, or download=1 skips attachment; why_new=existing server tests cover JSON APIs and ETag media but never exercise /, /jobs/<id>, /api/film/example, or download=1; seam=none; no_claim=real browser CSP enforcement or OS file-save dialog behavior (Flask test_client unit boundary only)
    def test_html_shell_csp_film_example_sanitization_and_download_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st, env_file = root / "st", root / ".env"
            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                app = create_app(st, env_file)
                jobs = app.extensions["jobs"]
                client = app.test_client()
                try:
                    with client.get("/") as r_index:
                        self.assertEqual(r_index.status_code, 200)
                        self.assertEqual(r_index.headers.get("Cache-Control"), "no-store")
                        self.assertEqual(r_index.headers.get("X-Content-Type-Options"), "nosniff")
                        csp = r_index.headers.get("Content-Security-Policy", "")
                        self.assertIn("script-src 'self'", csp)
                        self.assertIn("frame-ancestors 'none'", csp)
                        self.assertIn('id="job-form"', r_index.get_data(as_text=True))

                    with client.get("/jobs/job-42") as r_res:
                        self.assertEqual(r_res.status_code, 200)
                        self.assertEqual(r_res.headers.get("Cache-Control"), "no-store")
                        self.assertIn('data-job-id="job-42"', r_res.get_data(as_text=True))

                    with client.get("/api/film/example") as r_ex:
                        self.assertEqual(r_ex.status_code, 200)
                        self.assertEqual(r_ex.mimetype, "application/json")
                        self.assertIn('attachment; filename="film.example.json"', r_ex.headers.get("Content-Disposition", ""))
                        example = json.loads(r_ex.get_data(as_text=True))
                    self.assertTrue(example.get("shots"))
                    self.assertTrue(example.get("cast"))
                    for member in example["cast"]:
                        self.assertNotIn("picture", member)
                    r_refs = client.post("/api/film/references", json={"film": example}, headers={"X-Workbench": "1"})
                    self.assertEqual(r_refs.status_code, 200)
                    self.assertGreaterEqual(len(r_refs.json["slots"]), len(example["cast"]))

                    out = root / "dl-job"
                    out.mkdir()
                    (out / "film.mp4").write_bytes(b"MP4VIDEO" * 4)
                    jobs.records["dl1"] = {
                        "id": "dl1", "kind": "film", "demo": True, "output_dir": str(out),
                        "created_at": "2026-11-16T00:00:00+0000", "status": "succeeded", "error": None, "lang": "en", "operation": "pipeline",
                    }
                    with client.get("/api/jobs/dl1/files/film.mp4") as r_inline:
                        self.assertEqual(r_inline.status_code, 200)
                        self.assertNotIn("attachment", r_inline.headers.get("Content-Disposition", ""))
                    with client.get("/api/jobs/dl1/files/film.mp4?download=1") as r_att:
                        self.assertEqual(r_att.status_code, 200)
                        self.assertIn("attachment", r_att.headers.get("Content-Disposition", ""))
                        self.assertIn("film.mp4", r_att.headers.get("Content-Disposition", ""))
                finally:
                    jobs.close()

    def test_ship_review_fixes_shot_caps_402_429_abort_and_segment_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1. D3: MAX_SHOTS and MAX_SEGMENTS cap unbounded paid fan-out in validate() and film.load/plan
            with patch.dict(os.environ, {"H3_MAX_SHOTS": "4", "H3_MAX_SEGMENTS": "2"}, clear=False):
                five_shots = [{"action": f"shot {i}", "start": i * 2, "end": (i + 1) * 2} for i in range(5)]
                with self.assertRaises(InputError):
                    inputs.validate({"kind": "film", "demo": False, "film": {"shots": five_shots}}, root)
                three_segs = [{"action": f"shot {i}", "start": i * 12, "end": (i + 1) * 12} for i in range(3)]
                with self.assertRaises(InputError):
                    inputs.validate({"kind": "film", "demo": False, "film": {"shots": three_segs}}, root)

            # 2. Auto-fix: boolean, negative, and huge start/end timestamps reject cleanly as 400 InputError
            for bad_shot in (
                {"action": "walk", "start": True, "end": 2},
                {"action": "walk", "start": -1, "end": 2},
                {"action": "walk", "start": 0, "end": 1e308},
            ):
                with self.assertRaises(InputError):
                    inputs.validate({"kind": "film", "demo": False, "film": {"shots": [bad_shot]}}, root)

            # 3. D4: stage_frames aborts early on pre-task 402 and 429 without poisoning remaining shots
            for code in (402, 429):
                fdir = root / f"abort-{code}"
                job = film.Job(fdir)
                script = {"aspect": "9:16", "shots": [{"start": 0, "end": 2, "action": "s1"}, {"start": 2, "end": 4, "action": "s2"}]}
                segs = film.plan(script["shots"])
                with patch.object(frames, "describe_composition", side_effect=http.HTTPResponseError(code, f"HTTP {code}")) as desc_mock:
                    with self.assertRaises(http.HTTPResponseError):
                        film.stage_frames(job, script, segs, {s["idx"]: film.refs(script, s, "shot") for s in segs})
                self.assertEqual(desc_mock.call_count, 1)
                self.assertEqual(job.state["frames"], {})

            # 4. Auto-fix: film.run blocks unapplied segment-edit.json with unresolved staged task
            run_dir = root / "locked-film"
            (run_dir / "inputs" / "segment-rev1").mkdir(parents=True)
            film_path = root / "f.json"
            film_path.write_text(json.dumps({"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "walk"}]}), encoding="utf-8")
            (run_dir / "inputs" / "segment-edit.json").write_text(
                json.dumps({"number": 1, "revision": "rev1", "applied": False}), encoding="utf-8"
            )
            (run_dir / "inputs" / "segment-rev1" / "state.json").write_text(
                json.dumps({"segs": {"0": {"task": "t-inflight", "submitted": True}}}), encoding="utf-8"
            )
            with self.assertRaises(RuntimeError):
                film.run(run_dir, film_path, mode="none")

            # 5. Auto-fix: cmd_one and Jobs.resume poll already-submitted single-clip task after source image moved
            cli_main = _load_cli()
            one_dir = root / "one-moved"
            one_dir.mkdir(parents=True)
            missing_img = root / "moved.png"
            prompt_file = root / "one.txt"
            prompt_file.write_text("walk", encoding="utf-8")
            (one_dir / "state.json").write_text(
                json.dumps({"h3": "subject_definitions:\nA\n\nsummary:\nB\n\nretention_analysis:\nC\n\ndetailed_description:\n[Shot 1] D\n\noverall_soundscape:\nE\n\nnon_diegetic_music:\nN/A", "task": "t-submitted"}),
                encoding="utf-8",
            )
            args = SimpleNamespace(out=str(one_dir), image=[str(missing_img)], prompt=str(prompt_file), seed=1, seconds=5, aspect="9:16", check=False)
            with patch.object(cli_main.rh, "poll", return_value={"state": "done", "url": "https://v/1.mp4", "cny": 0.1, "coins": 45, "run_s": 10}), \
                 patch.object(cli_main.http, "download", side_effect=lambda u, p: p.write_bytes(b"MP4")):
                cli_main.cmd_one(args)
            self.assertTrue((one_dir / "clip.mp4").is_file())

            # 6. D5: Jobs._snapshot() allows pipeline resume when seg1 has a valid published clip + dead regen attempt and seg2 is unstarted
            st_dir = root / "wb-storage"
            job_out = root / "wb-film"
            (job_out / "inputs" / "segment-revdead").mkdir(parents=True)
            (job_out / "clips").mkdir(parents=True)
            (job_out / "clips" / "seg1.mp4").write_bytes(b"VALID_CLIP_1")
            film_spec = {
                "aspect": "9:16",
                "shots": [
                    {"start": 0, "end": 12, "action": "first segment"},
                    {"start": 12, "end": 24, "action": "second segment"},
                ],
            }
            req_spec = {"kind": "film", "demo": False, "frames": "none", "operation": "segment", "segment": 1, "output_dir": str(job_out), "film": film_spec}
            (job_out / "inputs" / "request.json").write_text(json.dumps(req_spec), encoding="utf-8")
            (job_out / "inputs" / "film.json").write_text(json.dumps(film_spec), encoding="utf-8")
            fjob = film.Job(job_out)
            fjob.put("segs", "0", revision="orig1", file=str(job_out / "clips" / "seg1.mp4"), cny=0.1, zh="s1", h3="h3-1")
            from workbench.storyboard import review_state
            (job_out / "inputs" / "segment-edit.json").write_text(
                json.dumps({"number": 1, "revision": "revdead", "applied": False, "previous": {"revision": "orig1"}, "prompt": "retry s1", "prompt_format": "source"}),
                encoding="utf-8",
            )
            (job_out / "inputs" / "segment-revdead" / "state.json").write_text(
                json.dumps({"segs": {"0": {"task": "t-dead", "terminal_failed": True, "error": "failed"}}}),
                encoding="utf-8",
            )
            jobs_mgr = Jobs(st_dir, root)
            try:
                jobs_mgr.records["f-rec"] = {
                    "id": "f-rec", "kind": "film", "demo": False, "output_dir": str(job_out),
                    "created_at": "2026-11-16T00:00:00+0000", "status": "failed", "error": "seg1 regen failed",
                    "lang": "en", "operation": "segment",
                }
                snap0 = jobs_mgr.snapshot("f-rec")
                rev = review_state(job_out)
                rev["confirmed_version"] = snap0["review"]["version"]
                (job_out / "inputs" / "review.json").write_text(json.dumps(rev), encoding="utf-8")
                snap = jobs_mgr.snapshot("f-rec")
                self.assertTrue(snap["can_resume_pipeline"])
                self.assertTrue(snap["can_resume_video"])

                # 7. D6: Resumable pre-task segment error keeps single-segment Resume instead of flipping to pipeline
                (job_out / "inputs" / "segment-revdead" / "state.json").write_text(
                    json.dumps({"segs": {"0": {"error": "upload timeout", "h3": "paid-h3"}}}),
                    encoding="utf-8",
                )
                snap_pretask = jobs_mgr.snapshot("f-rec")
                self.assertFalse(snap_pretask["can_resume_pipeline"])
                self.assertEqual(snap_pretask["segment_number"], 1)
                self.assertTrue(snap_pretask["can_resume_video"])
            finally:
                jobs_mgr.close()

            # 8. D7: Unaffected preexisting blocker prevents --redo / storyboard.run from deleting paid clips
            from workbench import storyboard as wb_storyboard
            redo_dir = root / "redo-preserve"
            (redo_dir / "inputs").mkdir(parents=True)
            (redo_dir / "clips").mkdir(parents=True)
            (redo_dir / "clips" / "seg2.mp4").write_bytes(b"PAID_SEG2")
            redo_spec = {"aspect": "9:16", "shots": [{"start": 0, "end": 12, "action": "s1"}, {"start": 12, "end": 24, "action": "s2"}]}
            (redo_dir / "inputs" / "film.json").write_text(json.dumps(redo_spec), encoding="utf-8")
            redo_req = {"kind": "film", "demo": False, "frames": "none", "operation": "pipeline", "redo": [2], "reset_applied": False, "output_dir": str(redo_dir), "film": redo_spec, "lang": "en"}
            (redo_dir / "inputs" / "request.json").write_text(json.dumps(redo_req), encoding="utf-8")
            rjob = film.Job(redo_dir)
            rjob.put("segs", "0", task="dead-1", terminal_failed=True, error="oom")
            rjob.put("segs", "1", file=str(redo_dir / "clips" / "seg2.mp4"), cny=0.2, zh="s2", h3="h2")
            with self.assertRaises(RuntimeError):
                wb_storyboard.run(dict(redo_req))
            self.assertEqual((redo_dir / "clips" / "seg2.mp4").read_bytes(), b"PAID_SEG2")
            self.assertFalse(json.loads((redo_dir / "inputs" / "request.json").read_text(encoding="utf-8"))["reset_applied"])

            # 9. D8: Reconciling a fresh pre-task clipless repair clears stale terminal_failed/task fields
            rec_dir = root / "reconcile-clipless"
            (rec_dir / "inputs" / "segment-rev2").mkdir(parents=True)
            (rec_dir / "inputs" / "film.json").write_text(json.dumps({"aspect": "9:16", "shots": [{"start": 0, "end": 8, "action": "s1"}]}), encoding="utf-8")
            (rec_dir / "inputs" / "request.json").write_text(json.dumps({"kind": "film", "demo": False, "frames": "none", "output_dir": str(rec_dir), "film": {"aspect": "9:16", "shots": [{"start": 0, "end": 8, "action": "s1"}]}}), encoding="utf-8")
            cjob = film.Job(rec_dir)
            cjob.put("segs", "0", revision="r0", task="old-dead", terminal_failed=True, error="old oom", zh="old", h3="old-h3")
            (rec_dir / "inputs" / "segment-edit.json").write_text(
                json.dumps({"number": 1, "revision": "rev2", "applied": False, "explicit_prompt": True, "prompt": "new repair", "prompt_format": "source", "previous": {"revision": "r0"}, "error": "transient upload err"}),
                encoding="utf-8",
            )
            (rec_dir / "inputs" / "segment-rev2" / "state.json").write_text(json.dumps({"segs": {"0": {"error": "transient upload err"}}}), encoding="utf-8")
            segments.reconcile_pending_segment(rec_dir, keep_pending=False, supersede=False, drain_siblings=True)
            reconciled_seg = film.Job(rec_dir).state["segs"]["0"]
            self.assertNotIn("terminal_failed", reconciled_seg)
            self.assertNotIn("task", reconciled_seg)
            self.assertTrue(reconciled_seg.get("attempt_blocked"))
            self.assertEqual(reconciled_seg.get("zh"), "new repair")

            # 10. D9/Step 11: Live CLI task on rec is preserved during pre-task reconciliation
            live_dir = root / "reconcile-live-cli"
            (live_dir / "inputs" / "segment-rev3").mkdir(parents=True)
            (live_dir / "inputs" / "film.json").write_text(json.dumps({"aspect": "9:16", "shots": [{"start": 0, "end": 8, "action": "s1"}]}), encoding="utf-8")
            (live_dir / "inputs" / "request.json").write_text(json.dumps({"kind": "film", "demo": False, "frames": "none", "output_dir": str(live_dir), "film": {"aspect": "9:16", "shots": [{"start": 0, "end": 8, "action": "s1"}]}}), encoding="utf-8")
            ljob = film.Job(live_dir)
            ljob.put("segs", "0", revision="r0", task="accepted-cli-task", submitted=1.0, zh="cli-zh", h3="cli-h3")
            (live_dir / "inputs" / "segment-edit.json").write_text(
                json.dumps({"number": 1, "revision": "rev3", "applied": False, "explicit_prompt": True, "prompt": "stale edit", "prompt_format": "source", "previous": {"revision": "r0"}, "error": "pre-task err"}),
                encoding="utf-8",
            )
            (live_dir / "inputs" / "segment-rev3" / "state.json").write_text(json.dumps({"segs": {"0": {"error": "pre-task err"}}}), encoding="utf-8")
            segments.reconcile_pending_segment(live_dir, keep_pending=False, supersede=False)
            self.assertEqual(film.Job(live_dir).state["segs"]["0"].get("task"), "accepted-cli-task")

            # 11. D9/Step 11: unaffected_segment_blocker ignores orphan trailing segments and blocks submission_unknown inside redo
            orphan_dir = root / "orphan-redo"
            orphan_dir.mkdir(parents=True)
            ojob = film.Job(orphan_dir)
            ojob.put("segs", "0", file="seg1.mp4", cny=0.1)
            ojob.put("segs", "1", file="seg2.mp4", cny=0.1)
            ojob.put("segs", "2", terminal_failed=True, task="orphan-dead")
            self.assertIsNone(film.unaffected_segment_blocker(ojob, "shot", [1], planned_count=2))
            ojob.put("segs", "1", file=None, cny=None, task=None, submission_unknown=True)
            self.assertEqual(film.unaffected_segment_blocker(ojob, "first", [1], planned_count=2), {"idx": 1})

            # 12. D9/Step 11: film.run checks worker.lock unconditionally and blocks unapplied frame-edit.json
            flock_dir = root / "frame-locked"
            (flock_dir / "inputs").mkdir(parents=True)
            (flock_dir / "inputs" / "frame-edit.json").write_text(
                json.dumps({"key": "s1-1", "applied": False, "downloaded": True}), encoding="utf-8"
            )
            with self.assertRaises(RuntimeError):
                film.run(flock_dir, film_path, mode="shot")

            # 13. D9/Step 11: Unaffected terminal_failed segment stays regenerable when pending_video=True after deferred redo
            unlk_out = root / "unlock-deferred"
            (unlk_out / "inputs").mkdir(parents=True)
            (unlk_out / "frames").mkdir(parents=True)
            (unlk_out / "clips").mkdir(parents=True)
            for fk in ("s1-1.png", "s2-1.png"):
                Image.new("RGB", (8, 8)).save(unlk_out / "frames" / fk)
            (unlk_out / "clips" / "seg2.mp4").write_bytes(b"PAID2")
            unlk_spec = {"aspect": "9:16", "shots": [{"start": 0, "end": 10, "action": "s1"}, {"start": 10, "end": 20, "action": "s2"}]}
            unlk_req = {"kind": "film", "demo": False, "frames": "shot", "operation": "pipeline", "redo": [2], "reset_applied": False, "output_dir": str(unlk_out), "film": unlk_spec}
            (unlk_out / "inputs" / "request.json").write_text(json.dumps(unlk_req), encoding="utf-8")
            (unlk_out / "inputs" / "film.json").write_text(json.dumps(unlk_spec), encoding="utf-8")
            ujob = film.Job(unlk_out)
            ujob.put("segs", "0", task="dead-s1", terminal_failed=True, error="oom", zh="s1-zh", h3="s1-h3")
            ujob.put("segs", "1", file=str(unlk_out / "clips" / "seg2.mp4"), cny=0.2, zh="s2-zh", h3="s2-h3")
            jm2 = Jobs(root / "st2", root)
            try:
                jm2.records["u1"] = {"id": "u1", "kind": "film", "demo": False, "output_dir": str(unlk_out), "created_at": "2026-11-16T00:00:00+0000", "status": "failed", "error": "oom", "lang": "en", "operation": "pipeline"}
                s_init = jm2.snapshot("u1")
                (unlk_out / "inputs" / "review.json").write_text(json.dumps({"dirty_segments": [2], "confirmed_version": s_init["review"]["version"]}), encoding="utf-8")
                s_after = jm2.snapshot("u1")
                self.assertTrue(s_after["pending_video"])
                self.assertTrue(s_after["segments"][0]["can_regenerate"])
                self.assertFalse(s_after["segments"][1]["can_regenerate"])
                self.assertFalse(s_after["can_assemble"])
            finally:
                jm2.close()


if __name__ == "__main__":
    unittest.main()
