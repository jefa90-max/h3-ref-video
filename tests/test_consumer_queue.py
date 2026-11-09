"""Consumer queue waits must outlast a running clip without losing submission safety."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from h3ref import film, rh


class ConsumerQueue(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.job = film.Job(Path(temporary.name))
        (self.job.dir / "frames").mkdir()
        (self.job.dir / "clips").mkdir()
        self.frame = self.job.dir / "frames/s1-1.png"
        self.frame.write_bytes(b"approved frame")
        self.story = {"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "A cat walks."}]}
        self.segment = film.plan(self.story["shots"])[0]
        self.pictures = [{"kind": "frame", "shot": 0}]
        self.job.put("segs", "0", h3="cached H3 prompt")
        self.rejection = {"code": 421, "msg": "TASK_QUEUE_MAXED", "data": None}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(patch.object(rh, "upload", return_value="approved.png"))
        self.sleep = stack.enter_context(patch.object(rh.time, "sleep", side_effect=self.check_wait_state))

    def check_wait_state(self, seconds):
        self.assertEqual(seconds, 60)
        self.assertIs(json.loads(self.job.file.read_text(encoding="utf-8"))["segs"]["0"]["submission_unknown"], False)

    def generate(self):
        return film.gen_segment(self.job, self.story, self.segment, self.pictures)

    def test_six_queue_rejections_then_success_reuses_assets_and_saves_one_task(self):
        responses = [self.rejection] * 6 + [{"code": 0, "data": {"taskId": "accepted-after-wait"}}]

        def poll(task):
            saved = json.loads(self.job.file.read_text(encoding="utf-8"))["segs"]["0"]
            self.assertEqual(saved["task"], task)
            self.assertIs(saved["submission_unknown"], False)
            return {"state": "done", "url": "https://example.test/clip.mp4", "cny": 0.1}

        with patch.object(rh, "_post", side_effect=responses) as create, \
                patch.object(rh, "poll", side_effect=poll), \
                patch.object(film.http, "download", side_effect=lambda url, out: out.write_bytes(b"video")), \
                patch.object(film.media, "last_frame"):
            try:
                result = self.generate()
            except rh.SubmissionRejected as exc:
                self.fail(f"Queue wait stopped before the available seventh submission: {exc}")
            self.assertEqual(result["task"], "accepted-after-wait")
            self.generate()
        self.assertEqual(create.call_count, 7)
        self.assertEqual(self.sleep.call_count, 6)
        self.assertEqual(self.frame.read_bytes(), b"approved frame")
        self.assertEqual(result["h3"], "cached H3 prompt")

    def test_persistent_queue_rejections_are_bounded_and_can_resume(self):
        with patch.object(rh, "_post", return_value=self.rejection) as create:
            with self.assertRaisesRegex(rh.SubmissionRejected, "421.*TASK_QUEUE_MAXED"):
                self.generate()
        self.assertEqual(create.call_count, 31)
        self.assertEqual(self.sleep.call_count, 30)
        saved = json.loads(self.job.file.read_text(encoding="utf-8"))["segs"]["0"]
        self.assertIs(saved["submission_unknown"], False)
        self.assertNotIn("task", saved)
        with patch.object(rh, "_post", return_value={"code": 0, "data": {"taskId": "resumed-task"}}) as create, \
                patch.object(rh, "poll", side_effect=InterruptedError("poll interrupted")):
            with self.assertRaisesRegex(InterruptedError, "poll interrupted"):
                self.generate()
        self.assertEqual(create.call_count, 1)
        self.assertEqual(self.job.state["segs"]["0"]["task"], "resumed-task")

    def test_lost_response_after_long_wait_blocks_submission_on_resume(self):
        responses = [self.rejection] * 6 + [RuntimeError("acceptance response lost")]
        with patch.object(rh, "_post", side_effect=responses) as create:
            with self.assertRaisesRegex(RuntimeError, "acceptance response lost"):
                self.generate()
            with self.assertRaises(RuntimeError):
                self.generate()
        self.assertEqual(create.call_count, 7)
        saved = json.loads(self.job.file.read_text(encoding="utf-8"))["segs"]["0"]
        self.assertIs(saved["submission_unknown"], True)
        self.assertNotIn("task", saved)

    def test_queue_wait_does_not_extend_machine_rejection_budget(self):
        machine_full = {"code": 415, "msg": "TASK_INSTANCE_MAXED", "data": None}
        responses = [self.rejection] * 6 + [machine_full] * 5
        with patch.object(rh, "_post", side_effect=responses) as create:
            with self.assertRaisesRegex(rh.SubmissionRejected, "415.*TASK_INSTANCE_MAXED"):
                self.generate()
        self.assertEqual(create.call_count, 11)
        self.assertEqual(self.sleep.call_count, 10)

    def test_alternating_capacity_errors_do_not_reset_their_budgets(self):
        machine_full = {"code": 415, "msg": "TASK_INSTANCE_MAXED", "data": None}
        responses = [self.rejection, machine_full] * 4 + [machine_full]
        with patch.object(rh, "_post", side_effect=responses) as create:
            with self.assertRaisesRegex(rh.SubmissionRejected, "415.*TASK_INSTANCE_MAXED"):
                self.generate()
        self.assertEqual(create.call_count, 9)
        self.assertEqual(self.sleep.call_count, 8)


if __name__ == "__main__":
    unittest.main()
