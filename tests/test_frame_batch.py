"""A failed storyboard item stays visible while all other images are attempted."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from h3ref import film, frames
from workbench import storyboard
from workbench import jobs as job_module
from workbench.server import create_app


class FrameBatch(unittest.TestCase):
    def setUp(self):
        description = patch.object(frames, "describe_composition", return_value=json.loads((Path(__file__).parent / 'fixtures/staging.json').read_text(encoding='utf-8'))['transfer_start'])
        description.start()
        self.addCleanup(description.stop)

    def script(self):
        return {"aspect": "9:16", "shots": [
            {"start": index * 2, "end": (index + 1) * 2, "action": f"Object {index} moves"}
            for index in range(4)]}

    def test_first_and_middle_failure_do_not_stop_remaining_frames_or_retry_failed_items(self):
        for failed in ({0: frames.ContentPolicyError("Rejected"), 2: RuntimeError("upstream unavailable")},
                       {1: RuntimeError("upstream unavailable")},
                       {0: TimeoutError()},
                       {index: RuntimeError("upstream unavailable") for index in range(4)}):
            with self.subTest(failed=list(failed)), tempfile.TemporaryDirectory() as temporary:
                job = film.Job(Path(temporary))
                script = self.script()
                segments = film.plan(script["shots"])
                pictures = {segment["idx"]: film.refs(script, segment, "shot") for segment in segments}
                attempts = []
                def draw(prompt, references, aspect, output, record, task=None):
                    index = int(output.stem.split("-")[1]) - 1
                    attempts.append(index)
                    record(task=f"task-{index}")
                    self.assertTrue(all(Path(reference).is_file() for reference in references))
                    self.assertEqual(references, [])
                    if index in failed:
                        raise failed[index]
                    Image.new("RGB", (8, 8), "red").save(output)
                    return 0.1
                with patch.object(frames, "draw", side_effect=draw):
                    failures = film.stage_frames(job, script, segments, pictures)
                    self.assertEqual(set(attempts), {0, 1, 2, 3})
                    self.assertEqual(len(attempts), 4)
                    self.assertEqual(failures, [f"s1-{index + 1}" for index in failed])
                    before = len(attempts)
                    self.assertEqual(film.stage_frames(film.Job(job.dir), script, segments, pictures), failures)
                    self.assertEqual(len(attempts), before)
                saved = film.Job(job.dir).state["frames"]
                for index in failed:
                    self.assertTrue(saved[f"s1-{index + 1}"].get("error"))
                    self.assertEqual(saved[f"s1-{index + 1}"]["task"], f"task-{index}")

    def test_cli_finishes_other_frames_without_submitting_video_with_missing_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            source = out / "film.json"
            source.write_text(json.dumps(self.script()))
            attempted = []
            def draw(prompt, references, aspect, output, **kwargs):
                attempted.append(output.stem)
                if output.stem == "s1-1":
                    raise frames.ContentPolicyError("Rejected")
                Image.new("RGB", (8, 8), "red").save(output)
                return 0
            with patch.object(frames, "draw", side_effect=draw), patch.object(film, "stage_prompts") as prompts, patch.object(film, "stage_generate") as video:
                self.assertEqual(film.run(out, source, "shot"), out.resolve())
            self.assertEqual(set(attempted), {"s1-1", "s1-2", "s1-3", "s1-4"})
            prompts.assert_not_called()
            video.assert_not_called()

    def test_existing_first_frame_keeps_only_board_and_character_references_for_later_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            board, actor = out / "board.png", out / "actor.png"
            for path in (board, actor):
                Image.new("RGB", (8, 8), "blue").save(path)
            script = self.script()
            script.update(board=str(board), cast=[{"name": "Object", "picture": str(actor)}])
            segments = film.plan(script["shots"])
            pictures = {segment["idx"]: film.refs(script, segment, "shot") for segment in segments}
            (out / "frames").mkdir()
            Image.new("RGB", (8, 8), "red").save(out / "frames" / "s1-1.png")
            def draw(prompt, references, aspect, output, **kwargs):
                self.assertEqual(list(map(str, references)), [str(board), str(actor)])
                self.assertNotIn("另一个镜头的画面", prompt)
                Image.new("RGB", (8, 8), "red").save(output)
                return 0
            with patch.object(frames, "draw", side_effect=draw) as drawing:
                self.assertEqual(film.stage_frames(film.Job(out), script, segments, pictures), [])
            self.assertEqual(drawing.call_count, 3)

    def test_partial_pipeline_does_not_clear_dirty_segments_or_record_an_assembly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(root / "storage", root / ".env")
            jobs = app.extensions["jobs"]
            try:
                with patch("workbench.jobs.preflight"), patch.object(jobs, "launch"):
                    response = app.test_client().post("/api/jobs", json={"kind": "film", "demo": False, "frames": "shot",
                        "film": self.script(), "output_dir": str(root / "output")}, headers={"X-Workbench": "1"})
                record = jobs.find(response.json["id"])
                out = Path(record["output_dir"])
                request_path = out / "inputs" / "request.json"
                request = json.loads(request_path.read_text())
                request["operation"] = "pipeline"
                request_path.write_text(json.dumps(request))
                (out / "state.json").write_text(json.dumps({"frames": {}, "segs": {}, "frame_failures": ["s1-1"]}))
                (out / "inputs" / "review.json").write_text(json.dumps({"dirty_segments": [1]}))
                child = SimpleNamespace(pid=999999, stdout=io.StringIO("partial storyboard\n"), wait=lambda: 0)
                with patch.object(job_module.subprocess, "Popen", return_value=child), patch.object(job_module, "stop_child"), \
                        patch.object(job_module, "stop_posix_descendants_on_exit"), patch("workbench.segments.record_assembly") as assembly:
                    jobs.run(record, request, dict(os.environ))
                self.assertEqual(record["status"], "succeeded", record.get("error"))
                self.assertEqual(record["operation"], "frames")
                self.assertEqual(json.loads(request_path.read_text())["operation"], "frames")
                self.assertEqual(storyboard.review_state(out)["dirty_segments"], [1])
                assembly.assert_not_called()
            finally:
                jobs.close()

    def test_partial_batch_exposes_editable_failure_without_first_frame_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(root / "storage", root / ".env")
            jobs = app.extensions["jobs"]
            client = app.test_client()
            headers = {"X-Workbench": "1"}
            try:
                with patch("workbench.jobs.preflight"), patch.object(jobs, "launch"):
                    response = client.post("/api/jobs", json={"kind": "film", "demo": False, "frames": "shot",
                        "film": self.script(), "output_dir": str(root / "output")}, headers=headers)
                self.assertEqual(response.status_code, 201, response.json)
                identifier = response.json["id"]
                out = Path(response.json["output_dir"])
                request = json.loads((out / "inputs" / "request.json").read_text())
                def draw(prompt, references, aspect, output, **kwargs):
                    if output.stem == "s1-1":
                        raise frames.ContentPolicyError("Rejected")
                    if output.stem == "s1-2":
                        raise RuntimeError("upstream unavailable")
                    Image.new("RGB", (8, 8), "red").save(output)
                    return 0
                with patch.object(frames, "draw", side_effect=draw):
                    storyboard.run(request)
                jobs.find(identifier)["status"] = "succeeded"
                current = jobs.snapshot(identifier)
                self.assertEqual(current.get("frame_failures"), 2)
                self.assertFalse(current["can_continue"])
                self.assertFalse(current["can_resume_frames"])
                self.assertTrue(all(frame["editable"] for frame in current["storyboard"]))
                self.assertTrue(all(not frame["missing_references"] for frame in current["storyboard"]))
                self.assertFalse(current["storyboard"][1]["moderation_failed"])
                # Restoring the first frame later must not invent an anchor in the
                # reference list of pictures generated while it was unavailable.
                Image.new("RGB", (8, 8), "blue").save(out / "frames" / "s1-1.png")
                after = jobs.snapshot(identifier)
                self.assertEqual(after["storyboard"][2]["references"], [])
                with patch("workbench.jobs.preflight"), patch.object(jobs, "launch"):
                    response = client.post(f"/api/jobs/{identifier}/frames/s1-2",
                        json={"prompt": current["storyboard"][1]["prompt"]}, headers=headers)
                self.assertEqual(response.status_code, 200, response.json)
                request = json.loads((out / "inputs" / "request.json").read_text())
                with patch.object(frames, "draw", side_effect=lambda prompt, references, aspect, output, **kwargs:
                                  (Image.new("RGB", (8, 8), "green").save(output) or 0)):
                    storyboard.regenerate(request)
                self.assertFalse(film.Job(out).state["frames"]["s1-2"].get("error"))
            finally:
                jobs.close()


class BatchTaskRecovery(unittest.TestCase):
    def setUp(self):
        description = patch.object(frames, "describe_composition", return_value=json.loads((Path(__file__).parent / 'fixtures/staging.json').read_text(encoding='utf-8'))['transfer_start'])
        description.start()
        self.addCleanup(description.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        app = create_app(root / "storage", root / ".env")
        self.jobs = app.extensions["jobs"]
        self.addCleanup(self.jobs.close)
        self.client = app.test_client()
        self.headers = {"X-Workbench": "1"}
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            response = self.client.post("/api/jobs", json={"kind": "film", "demo": False, "frames": "shot",
                "film": {"shots": [{"start": 0, "end": 5, "action": "A ball rolls."}]},
                "output_dir": str(root / "output")}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        self.identifier = response.json["id"]
        self.out = Path(response.json["output_dir"])
        self.request_path = self.out / "inputs" / "request.json"
        self.request = json.loads(self.request_path.read_text(encoding="utf-8"))

    def failed_batch(self, terminal=False):
        polling = {"return_value": {"state": "failed", "error": "remote task failed"}} if terminal else {
            "side_effect": RuntimeError("temporary polling outage")}
        with patch.object(frames, "submit", return_value="accepted-frame-task"), patch.object(frames, "poll", **polling):
            storyboard.run(self.request)
        self.jobs.find(self.identifier)["status"] = "succeeded"
        saved = film.Job(self.out).state["frames"]["s1-1"]
        self.assertEqual(saved["task"], "accepted-frame-task")
        self.assertTrue(saved.get("error"))

    def test_unknown_batch_task_cannot_be_overwritten_by_a_manual_edit(self):
        self.failed_batch()
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch") as launch:
            response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                json={"prompt": "A new image."}, headers=self.headers)
        self.assertEqual(response.status_code, 400, "An unknown cloud task was replaced by a new edit")
        launch.assert_not_called()
        self.assertFalse((self.out / "inputs" / "frame-edit.json").exists())
        snapshot = self.jobs.snapshot(self.identifier)
        self.assertTrue(snapshot["can_resume_frames"])
        self.assertFalse(snapshot["storyboard"][0]["references_editable"])

    def test_explicit_resume_only_queries_the_original_task_until_its_terminal_state(self):
        for terminal in ("done", "failed"):
            with self.subTest(terminal=terminal):
                self.failed_batch()
                with patch.object(frames, "poll", side_effect=AssertionError("ordinary rerun queried a failed item")):
                    storyboard.run(self.request)
                with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
                    response = self.client.post(f"/api/jobs/{self.identifier}/resume", json={}, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.json)
                resumed = json.loads(self.request_path.read_text(encoding="utf-8"))
                result = {"state": terminal, "url": "fake://image", "cny": 0.1, "error": "remote task failed"}
                with patch.object(frames, "submit", side_effect=AssertionError("paid for another image")), \
                        patch.object(frames, "poll", return_value=result) as poll, \
                        patch.object(frames.http, "download", side_effect=lambda url, output: Image.new("RGB", (8, 8)).save(output)):
                    storyboard.run(resumed)
                poll.assert_called_once_with("accepted-frame-task")
                self.jobs.find(self.identifier)["status"] = "succeeded"
                saved = film.Job(self.out).state["frames"]["s1-1"]
                self.assertEqual(saved["task"], "accepted-frame-task")
                self.assertEqual(bool(saved.get("error")), terminal == "failed")
                self.assertEqual(bool(saved.get("task_failed")), terminal == "failed")
                snapshot = self.jobs.snapshot(self.identifier)
                self.assertFalse(snapshot["can_resume_frames"])
                self.assertTrue(snapshot["storyboard"][0]["references_editable"])
                # Reset the owned fixture for the other remote terminal state.
                (self.out / "frames" / "s1-1.png").unlink(missing_ok=True)
                film.Job(self.out).put("frames", "s1-1", error=None, task_failed=False)

    def test_confirmed_remote_failure_allows_a_new_manual_generation(self):
        self.failed_batch(terminal=True)
        self.assertTrue(film.Job(self.out).state["frames"]["s1-1"].get("task_failed"))
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                json={"prompt": "A new image."}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        with patch.object(frames, "submit", return_value="replacement-task") as create, \
                patch.object(frames, "poll", return_value={"state": "done", "url": "fake://image", "cny": 0.1}), \
                patch.object(frames.http, "download", side_effect=lambda url, output: Image.new("RGB", (8, 8)).save(output)):
            storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        create.assert_called_once()
        self.assertEqual(film.Job(self.out).state["frames"]["s1-1"]["task"], "replacement-task")

    def test_png_published_before_error_cleanup_can_still_resume_the_saved_task(self):
        self.failed_batch()
        Image.new("RGB", (8, 8)).save(self.out / "frames" / "s1-1.png")
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            response = self.client.post(f"/api/jobs/{self.identifier}/resume", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        with patch.object(frames, "submit", side_effect=AssertionError("paid for another image")), \
                patch.object(frames, "poll", return_value={"state": "done", "url": "fake://image", "cny": 0.1}) as poll, \
                patch.object(frames.http, "download", side_effect=lambda url, output: Image.new("RGB", (8, 8)).save(output)):
            storyboard.run(json.loads(self.request_path.read_text(encoding="utf-8")))
        poll.assert_called_once_with("accepted-frame-task")
        saved = film.Job(self.out).state["frames"]["s1-1"]
        self.assertFalse(saved.get("error"))
        self.jobs.find(self.identifier)["status"] = "succeeded"
        self.assertTrue(self.jobs.snapshot(self.identifier)["can_continue"])

    def test_batch_create_response_loss_cannot_be_replaced_by_a_new_manual_edit(self):
        with patch.object(frames, "submit", side_effect=RuntimeError("acceptance response lost")) as create:
            storyboard.run(self.request)
            self.jobs.find(self.identifier)["status"] = "succeeded"
            with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
                response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                    json={"prompt": "A new image."}, headers=self.headers)
            self.assertEqual(response.status_code, 400, "An unknown accepted image may be submitted twice")
            storyboard.run(self.request)
        self.assertEqual(create.call_count, 1)
        saved = film.Job(self.out).state["frames"]["s1-1"]
        self.assertTrue(saved.get("submission_unknown"))
        self.assertFalse(saved.get("task"))
        self.assertFalse(self.jobs.snapshot(self.identifier)["storyboard"][0]["references_editable"])

    def test_single_frame_create_response_loss_cannot_repeat_a_paid_submission_on_resume(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        previous = target.read_bytes()
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                json={"prompt": "A new image."}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        request = json.loads(self.request_path.read_text(encoding="utf-8"))
        with patch.object(frames, "submit", side_effect=RuntimeError("acceptance response lost")) as create:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    storyboard.regenerate(request)
        self.assertEqual(create.call_count, 1, "Single-frame resume submitted another paid image")
        self.assertEqual(target.read_bytes(), previous)

    def test_explicit_image_submission_rejection_does_not_leave_an_unknown_submission(self):
        saved = {}
        with patch.object(frames.config, "apimart_key", return_value="synthetic"), \
                patch.object(frames.http, "request", return_value={"error": {"code": "insufficient_balance"}}):
            with self.assertRaises(RuntimeError):
                frames.draw("A ball rolls.", [], "9:16", self.out / "candidate.png", record=lambda **values: saved.update(values))
        self.assertIs(saved.get("submission_unknown"), False)
        self.assertFalse(saved.get("task"))
        self.assertFalse((self.out / "candidate.png").exists())

    def test_single_frame_resume_stops_at_remote_failure_until_a_new_manual_edit(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        previous = target.read_bytes()
        film.Job(self.out).put("frames", "s1-1", prompt="previously rejected", moderation_error="older content rejection")
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                json={"prompt": "A new image."}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        request = json.loads(self.request_path.read_text(encoding="utf-8"))
        with patch.object(frames, "submit", return_value="failed-image-task") as create:
            with patch.object(frames, "poll", side_effect=RuntimeError("temporary polling outage")):
                with self.assertRaises(RuntimeError):
                    storyboard.regenerate(request)
            with patch.object(frames, "poll", return_value={"state": "failed", "error": "remote task failed"}):
                for _ in range(2):
                    with self.assertRaises(RuntimeError):
                        storyboard.regenerate(request)
        self.assertEqual(create.call_count, 1, "Recovering a failed task silently paid for a new image")
        self.assertEqual(target.read_bytes(), previous)
        self.jobs.find(self.identifier)["status"] = "failed"
        snapshot = self.jobs.snapshot(self.identifier)
        self.assertTrue(snapshot["storyboard"][0]["references_editable"])
        self.assertFalse(snapshot["storyboard"][0]["moderation_failed"], "An older moderation error leaked into the new ordinary failure")
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                json={"prompt": "A new image."}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)

    def test_resume_reconciles_terminal_pending_before_accepting_a_new_manual_edit(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        previous = target.read_bytes()
        for moderation_error in (None, "content_policy_violation"):
            with self.subTest(moderation_error=moderation_error):
                film.Job(self.out).put("frames", "s1-1", prompt="original", task="original-task",
                    error=None, moderation_error=None, failed_prompt=None, task_failed=False)
                self.jobs.find(self.identifier)["status"] = "succeeded"
                with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
                    response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                        json={"prompt": "A new image."}, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.json)

                pending_path = self.out / "inputs" / "frame-edit.json"
                pending = json.loads(pending_path.read_text(encoding="utf-8"))
                pending.update(task="failed-image-task", task_failed=True,
                    error="remote task failed", moderation_error=moderation_error)
                pending_path.write_text(json.dumps(pending), encoding="utf-8")
                request = json.loads(self.request_path.read_text(encoding="utf-8"))
                self.jobs.find(self.identifier)["status"] = "interrupted"
                with patch.object(frames, "submit", side_effect=AssertionError("paid for another image")):
                    for _ in range(2):
                        with self.assertRaises(RuntimeError):
                            storyboard.regenerate(request)
                saved = film.Job(self.out).state["frames"]["s1-1"]
                self.assertTrue(saved.get("error"), "Pending terminal state was never mirrored to the frame")
                self.assertEqual(saved["task"], "failed-image-task")
                self.assertEqual(saved["moderation_error"], moderation_error)
                self.assertEqual(target.read_bytes(), previous)
                with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
                    response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                        json={"prompt": "Another image."}, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.json)


    def test_moderation_classification_survives_interruption_while_recording_terminal_state(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                json={"prompt": "A new image."}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        pending_path = self.out / "inputs" / "frame-edit.json"
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        pending["task"] = "rejected-task"
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        request = json.loads(self.request_path.read_text(encoding="utf-8"))
        write_json = storyboard.write_json
        def interrupt_after_terminal_write(path, values):
            write_json(path, values)
            if Path(path) == pending_path and values.get("task_failed"):
                raise SystemExit("process interrupted after terminal publication")
        with patch.object(storyboard, "write_json", side_effect=interrupt_after_terminal_write), \
                patch.object(frames, "poll", return_value={"state": "failed", "error": "content_policy_violation", "moderation_failed": True}):
            with self.assertRaises(SystemExit):
                storyboard.regenerate(request)
        with patch.object(frames, "submit", side_effect=AssertionError("paid for another image")):
            with self.assertRaises(RuntimeError):
                storyboard.regenerate(request)
        self.jobs.find(self.identifier)["status"] = "interrupted"
        self.assertTrue(self.jobs.snapshot(self.identifier)["storyboard"][0]["moderation_failed"])
