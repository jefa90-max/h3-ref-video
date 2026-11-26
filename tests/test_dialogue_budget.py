"""Offline expectations for the Wave 1 dialogue-budget contract."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from h3ref import film


ROOT = Path(__file__).resolve().parents[1]


class DialogueBudgetTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"H3_DIALOGUE_RATE_CONFIG": "", "H3_SKIP_PREFLIGHT": "", "H3_MAX_SHOTS": "1000"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_normalization_preserves_raw_shot_and_source(self):
        source = {"speech": " ZH_CN ", "cast": [{"name": "小林"}], "shots": [
            {"action": " "},
            {"action": "抬头", "start": 10, "end": 11, "line": "【表演】小林：（哽咽）我真的已经没有时间了……"},
            {"action": "转身", "start": True, "end": 1},
            {"action": 3},
        ]}
        original = copy.deepcopy(source)
        result = film.inspect_dialogue_budget(source)
        self.assertEqual(source, original)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["meta"]["checked_shots"], 2)
        self.assertEqual(result["meta"]["skipped_shots"], 2)
        self.assertEqual(result["meta"]["default_duration_shots"], 1)
        warning, = result["warnings"]
        self.assertEqual((warning["shot"], warning["raw_shot"], warning["raw_shot_index"]), (1, 2, 1))
        self.assertEqual((warning["code"], warning["count"], warning["min_seconds"]),
                         ("dialogue_slow_over_budget", 10, 3.7))

    def test_clamped_budget_and_mixed_unsupported_script(self):
        source = {"speech": "en", "shots": [
            {"action": "speaks", "start": 0, "end": 20, "line": "go " * 50},
            {"action": "speaks", "start": 20, "end": 22, "line": "Привет 2026"},
        ]}
        result = film.inspect_dialogue_budget(source)
        self.assertEqual(result["status"], "partial")
        over, unsupported = result["warnings"]
        self.assertEqual((over["duration"], over["effective_duration"], over["min_seconds"]), (20, 15, 15.7))
        self.assertEqual(unsupported["code"], "dialogue_unsupported_script")

    def test_missing_speech_and_draft_states(self):
        shots = [{"action": ""}, {"action": "moves", "line": "A line"}]
        warning, = film.check_dialogue_budget({"shots": shots, "speech": 3})
        self.assertEqual(warning["affected_shots"], [1])
        self.assertEqual(warning["affected_raw_shots"], [2])
        for draft, state in (({}, "invalid"), ({"shots": []}, "partial"), ({"shots": {}}, "invalid")):
            self.assertEqual(film.inspect_dialogue_budget(draft)["status"], state)

    def test_cli_accepts_stdin_without_loading_generation_configuration(self):
        source = {"speech": "en", "shots": [{"action": "whispered", "line": "Wait: don’t go away now."}]}
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-B", str(ROOT / "h3ref.py"), "preflight", "--film", "-", "--json", "--fail-on-warn"],
                input=json.dumps(source), text=True, capture_output=True, cwd=directory,
                env={**os.environ, "H3_STEPS": "invalid", "H3_SKIP_PREFLIGHT": "1"}, check=False,
            )
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(completed.stderr, "")
        warning, = json.loads(completed.stdout)["warnings"]
        self.assertEqual((warning["count"], warning["code"]), (5, "dialogue_slow_over_budget"))
        invalid = subprocess.run(
            [sys.executable, str(ROOT / "h3ref.py"), "preflight", "--json"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual((invalid.returncode, invalid.stdout), (1, ""))
        self.assertIn("dialogue_arguments_invalid", invalid.stderr)

    def test_reference_api_preserves_draft_and_reports_problem_details(self):
        from workbench.server import create_app

        source = {"speech": "en", "shots": [{"action": ""}, {"action": "whispers", "line": "Please don't turn around."}]}
        headers = {"X-Workbench": "1", "Accept-Language": "en"}
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(directory, Path(directory) / ".env")
            client = app.test_client()
            url = "/api/film/references"
            response = client.post(url, json={"film": source}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["film"]["shots"], source["shots"])
            warning, = response.json["warnings"]
            self.assertEqual(warning["raw_shot"], 2)
            self.assertIn("Slow delivery", warning["message"])
            for film_source, status in (({}, "invalid"), ({"shots": None}, "invalid"), ({"shots": []}, "partial")):
                response = client.post(url, json={"film": film_source}, headers=headers)
                self.assertEqual(response.json["preflight_status"], status)
            response = client.post(url, json={"film": {"shots": {}}}, headers=headers)
            self.assertEqual(response.json["code"], "dialogue_shots_invalid")
            self.assertEqual(response.json["field"], "film.shots")
            for body, content_type, status in (("{", "application/json", 400), ("{}", "text/plain", 415)):
                response = client.post(url, data=body, content_type=content_type, headers=headers)
                self.assertEqual((response.status_code, response.mimetype), (status, "application/problem+json"))
            response = client.post(url, json={"film": {"shots": [{}] * 2001}}, headers=headers)
            self.assertEqual((response.status_code, response.json["code"]), (400, "dialogue_references_limit"))
            with patch("workbench.server.describe_references", side_effect=RuntimeError("private diagnostic text")):
                response = client.post(url, json={"film": source}, headers=headers)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.mimetype, "application/problem+json")
            trace = (Path(directory) / "server.log").read_text()
            self.assertIn(response.json["instance"], trace)
            self.assertNotIn("private diagnostic text", trace + response.get_data(as_text=True))
            app.config["MAX_CONTENT_LENGTH"] = 8
            response = client.post(url, json={"film": source}, headers=headers)
            self.assertEqual((response.status_code, response.mimetype), (413, "application/problem+json"))

    def test_workbench_snapshot_is_written_before_launch_with_raw_indices(self):
        from workbench.jobs import Jobs

        source = {"speech": "zh", "shots": [{"action": ""}, {"action": "低声", "line": "我们现在就必须离开这里。"}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            jobs = Jobs(root, root / "storage")
            with patch.object(jobs, "launch"), patch.object(jobs, "snapshot"):
                jobs.create({"kind": "film", "demo": True, "output_dir": str(output), "film": source}, {}, "en")
            path = output / "inputs" / "preflight.json"
            original = path.read_bytes()
            snapshot = json.loads(original)
            self.assertEqual(snapshot, film.inspect_dialogue_budget(source))
            self.assertEqual(snapshot["warnings"][0]["raw_shot"], 2)
            normalized = json.loads((output / "inputs" / "film.json").read_text())
            final = output / "film.mp4"
            with patch.object(film.media, "duration", return_value=4.0) as duration, \
                 patch.dict(os.environ, {"H3_DIALOGUE_RATE_CONFIG": '{"chars":{"slow_rate":999}}'}):
                result = film.report(film.Job(output), normalized, [], "shot", final)
            duration.assert_called_once_with(final)
            self.assertIn("我们现在就必须离开这里。", result.read_text())
            self.assertEqual(path.read_bytes(), original)

    def test_scripts_prefixes_and_pause_tokens_use_distinct_budgets(self):
        cases = [
            ("ja", "Narrator：【役】㐀々かなABC３？！", 6, 1.4),
            ("ko", "VO:안녕 ABC５……", 4, 1.5),
            ("en", "Narrator: don’t go... wait-- now?!", 4, 3.1),
            ("zh", "１２：３０，记住：快跑", 8, 1.6),
        ]
        for speech, line, count, seconds in cases:
            with self.subTest(speech=speech):
                warning, = film.check_dialogue_budget({"speech": speech, "shots": [
                    {"action": "speak", "start": 0, "end": 0.5, "line": line},
                ]})
                self.assertEqual((warning["count"], warning["min_seconds"]), (count, seconds))
        self.assertEqual(film.check_dialogue_budget({"speech": "zh", "shots": [
            {"action": "pause", "line": "【旁白】……"},
        ]}), [])

    def test_example_and_isolation_under_audit_hooks(self):
        guard = """
import os, runpy, sys
script, example = sys.argv[1:]
def audit(event, args):
    if event == 'open':
        path, mode, flags = args
        if (isinstance(path, str) and os.path.basename(path).startswith('.env')
                or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
            raise AssertionError((event, path))
    if event.startswith('socket.') or event in ('os.mkdir', 'os.remove', 'os.rename', 'subprocess.Popen'):
        raise AssertionError(event)
sys.addaudithook(audit)
sys.argv = [script, 'preflight', '--film', example, '--json']
try:
    runpy.run_path(script, run_name='__main__')
finally:
    assert 'h3ref.config' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", guard, str(ROOT / "h3ref.py"), str(ROOT / "examples/dialogue-budget.json")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["meta"]["calibration_status"], "provisional")
        self.assertEqual([warning["code"] for warning in payload["warnings"]],
                         ["dialogue_over_budget", "dialogue_slow_over_budget"])

    def test_invalid_rate_override_is_advisory_in_pipeline_only(self):
        from h3ref import preflight
        from workbench.server import create_app

        source = {"speech": "zh", "shots": [{"action": "run", "start": 0, "end": 1, "line": "不要回头继续跑下去。"}]}
        bad_config = '{"chars":{"regular_rate":false}}'
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"H3_DIALOGUE_RATE_CONFIG": bad_config}):
            root = Path(directory)
            app = create_app(root / "storage", root / ".env")
            client = app.test_client()
            self.assertEqual(client.get("/api/film/example").status_code, 200)
            result = client.post("/api/film/references", json={"film": source}, headers={"X-Workbench": "1"})
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.json["preflight_meta"]["rate_config_fallback"])
            self.assertIn("dialogue_rate_config_fallback", [w["code"] for w in result.json["warnings"]])
            explicit = subprocess.run([sys.executable, str(ROOT / "h3ref.py"), "preflight",
                                       "--film", "-", "--json"], input=json.dumps(source),
                                      capture_output=True, text=True, check=False)
            self.assertEqual((explicit.returncode, explicit.stdout), (1, ""))
            self.assertIn("dialogue_rate_config_invalid", explicit.stderr)
            self.assertNotIn("false", explicit.stderr)

            script = root / "script.json"
            script.write_text(json.dumps(source))
            job = film.Job(root / "job")
            film.log_created_preflight(job, script)
            result = json.loads((job.dir / "inputs/preflight.json").read_text())
            self.assertEqual(result["meta"]["calibration_status"], "provisional")
            self.assertTrue(result["meta"]["rate_config_fallback"])

            config_path = root / "rates.json"
            config_path.write_text('{"chars":{"regular_rate":12}}')
            custom = preflight.inspect_with_env(source, {"H3_DIALOGUE_RATE_CONFIG": str(config_path)})
            self.assertEqual(custom["meta"]["calibration_status"], "custom")
            self.assertNotEqual(custom["meta"]["effective_config_sha256"], result["meta"]["effective_config_sha256"])
            config_path.write_bytes(b" " * 65537)
            with self.assertRaises(film.FilmValidationError):
                preflight.inspect_with_env(source, {"H3_DIALOGUE_RATE_CONFIG": str(config_path)})
            if hasattr(os, "mkfifo"):
                fifo = root / "rates.fifo"
                os.mkfifo(fifo)
                with self.assertRaises(film.FilmValidationError):
                    preflight.inspect_with_env(source, {"H3_DIALOGUE_RATE_CONFIG": str(fifo)})

    def test_cli_run_restores_saved_legacy_prompt_without_rewriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, speech in enumerate(("zh-CN", "ZH", "zh_CN", "   ")):
                source = root / "source.json"
                source.write_text(json.dumps({"speech": speech, "shots": [
                    {"action": "", "start": 0, "end": 1},
                    {"action": "turns", "start": 1, "end": 2, "line": "现在就跟我一起走。"},
                ]}))
                script = film.load(source)
                segments = film.plan(script["shots"])
                job = film.Job(root / f"job-{index}")
                legacy = film.compile_prompt(script, segments[0], [], 1)
                job.put("segs", "0", zh=legacy, h3="existing H3 prompt")
                with patch.object(film.rewrite, "rewrite") as rewrite, patch.object(film, "stage_frames", return_value=[]), \
                        patch.object(film, "stage_generate", side_effect=lambda job, *_: job.put("segs", "0", file="clip.mp4")), \
                        patch.object(film.media, "join"), patch.object(film.media, "duration", return_value=5.0), \
                        patch.object(film.config, "SUBTITLE_CHECKS_ENABLED", False):
                    film.run(job.dir, source, "none")
                rewrite.assert_not_called()
                self.assertEqual((job.dir / "prompts/seg1.h3.txt").read_text(), "existing H3 prompt")
                result = json.loads((job.dir / "inputs/preflight.json").read_text())
                warning, = result["warnings"]
                if speech.strip():
                    self.assertEqual(warning["raw_shot_index"], 1)
                else:
                    self.assertEqual(warning["affected_raw_shots"], [2])
                self.assertIn("preflight [WARN]", (job.dir / "run.log").read_text())

    def test_workbench_can_reload_normalized_timeline_past_raw_input_limit(self):
        from workbench.jobs import Jobs
        from workbench.inputs import validate
        from workbench.storyboard import plan

        source = {"speech": "zh", "shots": [
            {"action": "first", "start": 0, "end": 3599.9},
            {"action": "second", "start": 3599.9, "end": 3600, "line": "现在就跟我一起走。"},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            jobs = Jobs(root, root / "storage")
            with patch.object(jobs, "launch"), patch.object(jobs, "snapshot"):
                jobs.create({"kind": "film", "demo": False, "frames": "none",
                             "output_dir": str(output), "film": source}, {"RUNNINGHUB_API_KEY": "test"}, "en")
            request = json.loads((output / "inputs" / "request.json").read_text())
            script, _, _, _ = plan(request)
            self.assertEqual(script["shots"][-1]["end"], 3600.4)
            self.assertEqual(validate(request, root, normalized_times=True)["film"]["shots"], script["shots"])
            with self.assertRaises(film.FilmValidationError):
                film.load(output / "inputs" / "film.json")

    def test_failed_cli_load_does_not_freeze_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            job = root / "job"
            source.write_text('{"shots":[]}')
            with self.assertRaises(film.FilmValidationError):
                film.run(job, source, "none")
            self.assertFalse((job / "inputs" / "preflight.json").exists())

            source.write_text('{"speech":"en","shots":[{"action":"wait","line":"Wait."}]}')
            with patch.object(film, "stage_frames", return_value=["intentional-stop"]):
                film.run(job, source, "none")
            result = json.loads((job / "inputs" / "preflight.json").read_text())
            self.assertEqual(result["meta"]["checked_shots"], 1)

    def test_nonstring_dialogue_fields_do_not_reach_prompt_compilation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "film.json"
            path.write_text('{"speech": ["en"], "shots":[{"action":"looks up","line":42}]}')
            script = film.load(path)
            segments = film.plan(script["shots"])
            self.assertEqual(script["shots"][0]["line"], "")
            self.assertEqual(script["speech"], "")
            self.assertIn("looks up", film.compile_prompt(script, segments[0], [], 1))

    def test_log_and_report_preview_removes_control_characters_and_credentials(self):
        preview = film.sanitize_line_preview("Hi|there\u001b\nAPIMART_API_KEY=private-token")
        self.assertEqual(preview, "Hi|there […]")
        self.assertEqual(film.escape_markdown_cell(preview), r"Hi\|there \[…\]")

    def test_cli_changed_source_checks_current_script_without_overwriting_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text('{"speech":"en","shots":[{"action":"wait","start":0,"end":5,"line":"Wait."}]}')
            second.write_text('{"speech":"en","shots":[{"action":"whispered","start":0,"end":1,"line":"Wait here for the next train."}]}')
            job = root / "job"
            with patch.object(film, "stage_frames", return_value=["intentional-stop"]):
                film.run(job, first, "none")
                path = job / "inputs" / "preflight.json"
                original = path.read_bytes()
                self.assertEqual(json.loads(original)["warnings"], [])
                film.run(job, second, "none")
                film.run(job, None, "none")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual((job / "run.log").read_text().count("Wait here for the next train."), 2)
