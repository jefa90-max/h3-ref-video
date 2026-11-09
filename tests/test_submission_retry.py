"""Submission failures must be visible, bounded, and safe to resume without paying twice."""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from h3ref import film, rh

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("h3ref_cli", ROOT / "h3ref.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class SubmissionRetry(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.job = film.Job(Path(self.directory.name))
        (self.job.dir / "clips").mkdir()
        (self.job.dir / "frames").mkdir()
        self.frame = self.job.dir / "frames/s1-1.png"
        self.frame.write_bytes(b"existing approved frame")
        self.story = {"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "A cat walks."}]}
        self.segment = film.plan(self.story["shots"])[0]
        self.pictures = [{"kind": "frame", "shot": 0}]
        self.job.put("frames", "s1-1", file=str(self.frame))
        self.job.put("segs", "0", h3="saved H3 prompt",
                     zh=film.compile_prompt(self.story, self.segment, self.pictures, 1))
        self.output = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.output))
        stack.enter_context(patch.dict(os.environ, {"H3_LANG": "en"}))
        stack.enter_context(patch.object(rh, "upload", return_value="uploaded.png"))
        self.sleep = stack.enter_context(patch.object(film.time, "sleep"))

    def generate(self):
        return film.gen_segment(self.job, self.story, self.segment, self.pictures)

    def reject_after_limit(self, path, body):
        self.attempts += 1
        self.assertLessEqual(self.attempts, 5, "submission kept retrying beyond its limit")
        return {"code": 421, "msg": "TASK_QUEUE_MAXED", "data": None}

    def test_film_stops_after_five_rejections_and_records_each_reason(self):
        self.attempts = 0
        with patch.object(rh, "_post", side_effect=self.reject_after_limit):
            with self.assertRaisesRegex(RuntimeError, "5.*421.*TASK_QUEUE_MAXED"):
                self.generate()
        log = (self.job.dir / "run.log").read_text()
        for attempt in range(1, 6):
            self.assertIn(f"{attempt}/5", log)
        self.assertEqual(log.count("TASK_QUEUE_MAXED"), 5)
        self.assertIn("seg1", log)
        self.assertEqual([call.args for call in self.sleep.call_args_list], [(60,)] * 4)
        self.assertNotIn("task", self.job.state["segs"]["0"])
        self.assertEqual(self.frame.read_bytes(), b"existing approved frame")

    def test_single_clip_uses_the_same_retry_limit(self):
        self.attempts = 0
        (self.job.dir / "state.json").write_text(json.dumps({"h3": "saved H3 prompt"}))
        prompt = self.job.dir / "input.txt"
        prompt.write_text("original prompt")
        args = SimpleNamespace(out=str(self.job.dir), image=[str(self.frame)], prompt=str(prompt),
                               seed=7, seconds=5, aspect="9:16", check=False)
        with patch.object(rh, "_post", side_effect=self.reject_after_limit):
            with self.assertRaisesRegex(RuntimeError, "5.*421.*TASK_QUEUE_MAXED"):
                CLI.cmd_one(args)
        self.assertIn("1/5", self.output.getvalue())
        self.assertEqual(self.sleep.call_count, 4)

    def test_single_clip_skips_vision_while_paused_even_with_force_check(self):
        prompt = self.job.dir / "input.txt"
        prompt.write_text("original prompt")
        for forced in (False, True):
            with self.subTest(check=forced):
                self.job.file.write_text(json.dumps({"h3": "cached prompt with 中文对白", "task": "saved-task"}))
                args = SimpleNamespace(out=str(self.job.dir), image=[str(self.frame)], prompt=str(prompt),
                                       seed=7, seconds=5, aspect="9:16", check=forced)
                with patch.object(rh, "poll", return_value={"state": "done", "url": "fake://clip", "cny": 0.1}), \
                     patch.object(CLI.http, "download", side_effect=lambda url, path: path.write_bytes(b"video")), \
                     patch.object(CLI.media, "duration", return_value=5), \
                     patch.object(CLI.check, "subtitles", return_value={"checked": True}) as inspect:
                    CLI.cmd_one(args)
                inspect.assert_not_called()
                self.assertEqual((self.job.dir / "clip.mp4").read_bytes(), b"video")
                self.assertNotIn("subtitles", json.loads(self.job.file.read_text()))

    def test_temporary_rejection_resumes_saved_assets_and_saves_task_before_poll(self):
        responses = iter([{"code": 415, "msg": "TASK_INSTANCE_MAXED", "data": None},
                          {"code": 0, "msg": "success", "data": {"taskId": "accepted-task"}}])

        def poll(task):
            persisted = json.loads(self.job.file.read_text())
            self.assertEqual(persisted["segs"]["0"]["task"], task)
            return {"state": "done", "url": "https://example.test/clip.mp4", "cny": 0.1}

        with patch.object(rh, "_post", side_effect=lambda *args: next(responses)), \
                patch.object(rh, "poll", side_effect=poll), \
                patch.object(film.http, "download", side_effect=lambda url, out: out.write_bytes(b"video")), \
                patch.object(film.media, "last_frame"), \
                patch.object(film.frames, "draw", side_effect=AssertionError("redrew saved frame")), \
                patch.object(film.rewrite, "rewrite", side_effect=AssertionError("rewrote saved prompt")):
            film.stage_frames(self.job, self.story, [self.segment], {0: self.pictures})
            film.stage_prompts(self.job, self.story, [self.segment], {0: self.pictures})
            result = self.generate()
        self.assertEqual(result["task"], "accepted-task")
        self.assertEqual(result["h3"], "saved H3 prompt")
        self.assertEqual((self.job.dir / "clips/seg1.mp4").read_bytes(), b"video")
        self.assertEqual(self.frame.read_bytes(), b"existing approved frame")
        log = (self.job.dir / "run.log").read_text()
        self.assertIn("1/5", log)
        self.assertIn("TASK_INSTANCE_MAXED", log)
        self.assertIn("60", log)

    def test_existing_task_is_polled_without_upload_or_resubmission(self):
        self.job.put("segs", "0", task="existing-task")
        with patch.object(rh, "upload", side_effect=AssertionError("uploaded again")), \
                patch.object(rh, "_post", side_effect=AssertionError("submitted again")), \
                patch.object(rh, "poll", side_effect=RuntimeError("poll reached")):
            with self.assertRaisesRegex(RuntimeError, "poll reached"):
                self.generate()
        self.assertEqual(self.job.state["segs"]["0"]["task"], "existing-task")

    def test_network_failure_is_logged_without_repeating_submission(self):
        with patch.object(rh, "_post", side_effect=RuntimeError("connection timed out")) as request:
            with self.assertRaisesRegex(RuntimeError, "connection timed out"):
                self.generate()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.sleep.call_count, 0)
        self.assertTrue((self.job.dir / "run.log").exists(), "submission failure was not logged")
        self.assertIn("connection timed out", (self.job.dir / "run.log").read_text())

    def test_balance_errors_are_terminal_and_keys_are_redacted(self):
        for code, reason in [(605, "NOT_ENOUGH_BALANCE"), (416, "TASK_CREATE_FAILED_BY_NOT_ENOUGH_WALLET"),
                             (812, "CORPAPIKEY_INSUFFICIENT_FUNDS"), (999, "NOT_ENOUGH_BALANCE")]:
            with self.subTest(code=code), patch.object(rh.config, "rh_key", return_value="secret-test-key"), \
                    patch.object(rh.http, "request", return_value={"code": code, "msg": reason,
                                                                  "data": {"detail": "secret-test-key"}}):
                with self.assertRaises(RuntimeError) as raised:
                    rh.submit({})
                self.assertNotIsInstance(raised.exception, rh.QueueFull)
                self.assertIn(str(code), str(raised.exception))
                self.assertIn(reason, str(raised.exception))
                self.assertNotIn("secret-test-key", str(raised.exception))

    def test_submission_error_uses_the_task_language(self):
        for lang, phrase in [("en", "RunningHub submission failed"), ("zh-CN", "RunningHub 提交失败")]:
            # Each language exercises a fresh submission, not an ambiguous retry.
            self.job.put("segs", "0", submission_unknown=False)
            with self.subTest(lang=lang), patch.dict(os.environ, {"H3_LANG": lang}), \
                    patch.object(rh, "_post", side_effect=RuntimeError("connection timed out")):
                self.output.seek(0)
                self.output.truncate()
                with self.assertRaisesRegex(RuntimeError, "connection timed out"):
                    self.generate()
                self.assertIn(phrase, self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
