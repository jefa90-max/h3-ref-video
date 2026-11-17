"""A failed storyboard item stays visible while all other images are attempted."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
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
        import os
        env_patch = patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
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
        import os
        env_patch = patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
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

    def add_reference_board(self):
        board = self.out / "board.png"
        Image.new("RGB", (8, 8), "blue").save(board)
        self.request["film"]["board"] = str(board)
        (self.out / "inputs" / "film.json").write_text(json.dumps(self.request["film"]), encoding="utf-8")
        self.request_path.write_text(json.dumps(self.request), encoding="utf-8")

    def assert_preparation_failure_allows_manual_edit(self, phase):
        self.add_reference_board()
        failure = patch.object(frames.media, "shrink", side_effect=OSError("synthetic shrink failure")) if phase == "shrink" else \
            patch.object(frames, "_upload", side_effect=RuntimeError("synthetic upload failure"))
        with failure, patch.object(frames.http, "request", side_effect=AssertionError("create must not run")) as create, \
                patch.object(frames.http, "upload", side_effect=AssertionError("unexpected upload")):
            storyboard.run(self.request)
        create.assert_not_called()
        saved = film.Job(self.out).state["frames"]["s1-1"]
        self.assertIn(f"synthetic {phase} failure", saved["error"])
        self.assertFalse(saved.get("submission_unknown"))
        self.assertFalse(saved.get("task"))
        self.jobs.find(self.identifier)["status"] = "succeeded"
        card = self.jobs.snapshot(self.identifier)["storyboard"][0]
        self.assertTrue(card["editable"])
        self.assertTrue(card["references_editable"])
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch") as launch:
            response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                json={"prompt": "A repaired image."}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        launch.assert_called_once()

    def test_batch_shrink_failure_before_create_allows_manual_edit(self):
        self.assert_preparation_failure_allows_manual_edit("shrink")

    def test_batch_upload_failure_before_create_allows_manual_edit(self):
        self.assert_preparation_failure_allows_manual_edit("upload")

    def test_single_frame_upload_failure_can_resume_without_marking_create_unknown(self):
        self.add_reference_board()
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        previous = target.read_bytes()
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                json={"prompt": "A repaired image."}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        request = json.loads(self.request_path.read_text(encoding="utf-8"))
        with patch.object(frames, "_upload", side_effect=RuntimeError("synthetic upload failure")), \
                patch.object(frames.http, "request", side_effect=AssertionError("create must not run")) as create:
            with self.assertRaisesRegex(RuntimeError, "synthetic upload failure"):
                storyboard.regenerate(request)
        create.assert_not_called()
        pending_path = self.out / "inputs" / "frame-edit.json"
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        self.assertFalse(pending.get("submission_unknown"))
        self.assertFalse(pending.get("task"))
        self.assertEqual(target.read_bytes(), previous)
        self.jobs.find(self.identifier)["status"] = "failed"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch") as launch:
            response = self.client.post(f"/api/jobs/{self.identifier}/resume", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        launch.assert_called_once()
        self.assertEqual(json.loads(pending_path.read_text(encoding="utf-8"))["revision"], pending["revision"])

    def test_missing_create_key_does_not_mark_an_unknown_submission(self):
        saved = {}
        with patch.object(frames.config, "apimart_key", side_effect=SystemExit("synthetic missing key")), \
                patch.object(frames.http, "request") as create:
            with self.assertRaisesRegex(SystemExit, "synthetic missing key"):
                frames.draw("A ball rolls.", [], "9:16", self.out / "candidate.png", record=lambda **values: saved.update(values))
        create.assert_not_called()
        self.assertFalse(saved.get("submission_unknown"))
        self.assertFalse(saved.get("task"))

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
        with patch.object(frames.config, "apimart_key", return_value="synthetic"), \
                patch.object(frames.http, "request", side_effect=RuntimeError("acceptance response lost")) as create:
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
        with patch.object(frames.config, "apimart_key", return_value="synthetic"), \
                patch.object(frames.http, "request", side_effect=RuntimeError("acceptance response lost")) as create:
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

    def test_plain_text_auth_rejection_keeps_batch_failure_manually_editable(self):
        for status, reason in ((401, "Unauthorized"), (403, "Forbidden")):
            with self.subTest(status=status):
                job = film.Job(self.out)
                job.state["frames"] = {}
                job.save()
                error = urllib.error.HTTPError("https://api.example/images/generations", status, reason, {},
                                               io.BytesIO(reason.encode()))
                self.addCleanup(error.close)
                with patch.object(frames.config, "apimart_key", return_value="synthetic"), \
                        patch("urllib.request.urlopen", side_effect=error) as opened:
                    storyboard.run(self.request)
                opened.assert_called_once()
                saved = film.Job(self.out).state["frames"]["s1-1"]
                self.assertIn(reason, saved["error"])
                self.assertFalse(saved.get("submission_unknown"))
                self.assertFalse(saved.get("task"))
                self.jobs.find(self.identifier)["status"] = "succeeded"
                card = self.jobs.snapshot(self.identifier)["storyboard"][0]
                self.assertTrue(card["editable"])
                self.assertTrue(card["references_editable"])
                with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch") as launch:
                    response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                        json={"prompt": "A repaired image."}, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.json)
                launch.assert_called_once()

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

    def test_single_frame_submission_rejected_allows_edit_frame_and_settles(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Invalid prompt"}, headers=self.headers)
        with patch.object(frames.config, "apimart_key", return_value="k"), \
             patch.object(frames.http, "request", return_value={"error": {"code": "invalid_request_error", "message": "bad prompt"}}):
            with self.assertRaises(frames.SubmissionRejected):
                storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        self.jobs.find(self.identifier)["status"] = "failed"
        self.assertTrue(self.jobs.snapshot(self.identifier)["storyboard"][0]["editable"])
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            res = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Corrected prompt"}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        with patch.object(frames, "draw", side_effect=lambda *a, **k: (Image.new("RGB", (8, 8), "green").save(a[3]) or 0.1)):
            storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        self.assertEqual(film.Job(self.out).state["frames"]["s1-1"]["prompt"], "Corrected prompt")
        self.assertTrue(json.loads((self.out / "inputs/frame-edit.json").read_text())["applied"])

    def test_stopped_video_stage_with_no_unresolved_tasks_allows_storyboard_frame_edit(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        film.Job(self.out).put("frames", "s1-1", prompt="initial", file=str(target))
        film.Job(self.out).put("segs", "0", task="failed-seg", terminal_failed=True, error="bad frame")
        req = json.loads(self.request_path.read_text(encoding="utf-8"))
        req["operation"] = "pipeline"
        self.request_path.write_text(json.dumps(req), encoding="utf-8")
        self.jobs.find(self.identifier)["status"] = "failed"
        self.assertTrue(self.jobs.snapshot(self.identifier)["storyboard"][0]["editable"])
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            res = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Repaired frame"}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)

    def test_single_frame_unknown_submission_blocks_replacement_edit(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "New image."}, headers=self.headers)
        request = json.loads(self.request_path.read_text(encoding="utf-8"))
        with patch.object(frames.config, "apimart_key", return_value="synthetic"), \
             patch.object(frames.http, "request", side_effect=RuntimeError("lost")):
            with self.assertRaises(RuntimeError):
                storyboard.regenerate(request)
        self.jobs.find(self.identifier)["status"] = "failed"
        pending_path = self.out / "inputs" / "frame-edit.json"
        before = pending_path.read_bytes()
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch") as launch:
            response = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Replacement."}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        launch.assert_not_called()
        self.assertEqual(pending_path.read_bytes(), before)

    def test_single_frame_terminal_failure_replacement_settles_row(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "First edit"}, headers=self.headers)
        with patch.object(frames, "submit", return_value="failed-task"), \
             patch.object(frames, "poll", return_value={"state": "failed", "error": "boom"}):
            with self.assertRaises(RuntimeError):
                storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        self.jobs.find(self.identifier)["status"] = "failed"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            res = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Second edit"}, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        with patch.object(frames, "submit", return_value="ok-task"), \
             patch.object(frames, "poll", return_value={"state": "done", "url": "https://cdn.example/img.png", "cny": 0.1}), \
             patch.object(frames.http, "download", side_effect=lambda u, p: Image.new("RGB", (8, 8), "green").save(p)):
            storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        saved = film.Job(self.out).state["frames"]["s1-1"]
        self.assertEqual(saved["task"], "ok-task")
        self.assertFalse(saved["task_failed"])
        self.assertIsNone(saved["error"])
        self.assertTrue(json.loads((self.out / "inputs/frame-edit.json").read_text())["applied"])

    def test_single_frame_pre_task_failure_resume_settles_same_revision(self):
        self.add_reference_board()
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Repaired."}, headers=self.headers)
        pending_path = self.out / "inputs" / "frame-edit.json"
        rev = json.loads(pending_path.read_text(encoding="utf-8"))["revision"]
        with patch.object(frames, "_upload", side_effect=RuntimeError("upload error")):
            with self.assertRaises(RuntimeError):
                storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        self.jobs.find(self.identifier)["status"] = "failed"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.assertEqual(self.client.post(f"/api/jobs/{self.identifier}/resume", json={}, headers=self.headers).status_code, 200)
        with patch.object(frames, "_upload", return_value="https://img/ref.jpg"), \
             patch.object(frames.config, "apimart_key", return_value="k"), \
             patch.object(frames.http, "request", side_effect=[{"data": [{"task_id": "t-1"}]}, {"data": {"status": "completed", "cost": 0.01, "result": {"images": [{"url": ["https://img/out.png"]}]}}}]), \
             patch.object(frames.http, "download", side_effect=lambda u, p: Image.new("RGB", (8, 8), "blue").save(p)):
            storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        after = json.loads(pending_path.read_text(encoding="utf-8"))
        self.assertEqual(after["revision"], rev)
        self.assertTrue(after["applied"])
        self.assertEqual(film.Job(self.out).state["frames"]["s1-1"]["prompt"], "Repaired.")

    def test_interrupted_inflight_frame_edit_blocks_video_ops_and_replacement_edit(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "in-flight"}, headers=self.headers)
        pending_path = self.out / "inputs" / "frame-edit.json"
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        pending["task"] = "inflight-apimart-task"
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        self.jobs.find(self.identifier)["status"] = "interrupted"
        snap = self.jobs.snapshot(self.identifier)
        self.assertFalse(snap["review"]["ready"])
        self.assertTrue(snap["storyboard"][0]["task_pending"])
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            res = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "overwrite"}, headers=self.headers)
        self.assertEqual(res.status_code, 400)

    def test_interrupted_unsubmitted_frame_edit_remains_editable_and_retains_draft_prompt(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Unsubmitted draft prompt"}, headers=self.headers)
        self.jobs.find(self.identifier)["status"] = "interrupted"
        card = self.jobs.snapshot(self.identifier)["storyboard"][0]
        self.assertTrue(card["editable"])
        self.assertEqual(card["prompt"], "Unsubmitted draft prompt")

    def _enable_two_shots(self):
        self.request["film"]["shots"] = [
            {"start": 0, "end": 2, "action": "Shot one"},
            {"start": 2, "end": 4, "action": "Shot two"},
        ]
        (self.out / "inputs/film.json").write_text(json.dumps(self.request["film"]), encoding="utf-8")
        self.request_path.write_text(json.dumps(self.request), encoding="utf-8")
        (self.out / "frames").mkdir(exist_ok=True)
        for key, color in (("s1-1", "red"), ("s1-2", "green")):
            img = self.out / "frames" / f"{key}.png"
            Image.new("RGB", (8, 8), color).save(img)
            film.Job(self.out).put("frames", key, prompt=f"prompt-{key}", file=str(img), cny=0.1, task=f"init-{key}")

    def test_unsubmitted_frame_edit_replaced_by_other_frame_settles_other_and_keeps_first(self):
        self._enable_two_shots()
        first_bytes = (self.out / "frames/s1-1.png").read_bytes()
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Draft s1-1"}, headers=self.headers)
        self.jobs.find(self.identifier)["status"] = "interrupted"
        # Because job was interrupted on frame s1-1, s1-2 is editable once status is succeeded or if video stage stopped;
        # test replacing unapplied unsubmitted s1-1 with s1-2 when status is succeeded:
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            res = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-2", json={"prompt": "Edited s1-2"}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        with patch.object(frames, "draw", side_effect=lambda *a, **k: (Image.new("RGB", (8, 8), "blue").save(a[3]) or 0.1)):
            storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        self.assertEqual((self.out / "frames/s1-1.png").read_bytes(), first_bytes)
        self.assertEqual(film.Job(self.out).state["frames"]["s1-1"]["prompt"], "prompt-s1-1")
        self.assertEqual(film.Job(self.out).state["frames"]["s1-2"]["prompt"], "Edited s1-2")
        self.assertTrue(json.loads((self.out / "inputs/frame-edit.json").read_text())["applied"])

    def test_single_frame_unknown_submission_rejects_http_resume(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        orig = target.read_bytes()
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Ambiguous"}, headers=self.headers)
        with patch.object(frames.config, "apimart_key", return_value="k"), \
             patch.object(frames.http, "request", side_effect=RuntimeError("lost")):
            with self.assertRaises(RuntimeError):
                storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        self.jobs.find(self.identifier)["status"] = "failed"
        snap = self.jobs.snapshot(self.identifier)
        self.assertFalse(snap["can_resume_video"])
        before = (self.out / "inputs/frame-edit.json").read_bytes()
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch") as launch:
            res = self.client.post(f"/api/jobs/{self.identifier}/resume", json={}, headers=self.headers)
        self.assertEqual(res.status_code, 400)
        launch.assert_not_called()
        self.assertEqual((self.out / "inputs/frame-edit.json").read_bytes(), before)
        self.assertEqual(target.read_bytes(), orig)

    def test_single_frame_unknown_submission_blocks_editing_other_frame(self):
        self._enable_two_shots()
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Ambiguous"}, headers=self.headers)
        pending_path = self.out / "inputs/frame-edit.json"
        pending = json.loads(pending_path.read_text())
        pending["submission_unknown"] = True
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        before = pending_path.read_bytes()
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch") as launch:
            res = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-2", json={"prompt": "Other"}, headers=self.headers)
        self.assertEqual(res.status_code, 400)
        launch.assert_not_called()
        self.assertEqual(pending_path.read_bytes(), before)

    def test_interrupted_inflight_frame_edit_resumes_via_http_and_settles(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        film.Job(self.out).put("frames", "s1-1", prompt="initial", file=str(target), cny=0.2, task="init-t")
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Inflight edit"}, headers=self.headers)
        pending_path = self.out / "inputs/frame-edit.json"
        pending = json.loads(pending_path.read_text())
        pending["task"] = "saved-cloud-task"
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        self.jobs.find(self.identifier)["status"] = "interrupted"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            res = self.client.post(f"/api/jobs/{self.identifier}/resume", json={}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        with patch.object(frames, "submit", side_effect=AssertionError("resubmitted paid image")), \
             patch.object(frames, "poll", return_value={"state": "done", "url": "https://cdn.example/f.png", "cny": 0.3}), \
             patch.object(frames.http, "download", side_effect=lambda u, p: Image.new("RGB", (8, 8), "blue").save(p)):
            storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        saved = film.Job(self.out).state["frames"]["s1-1"]
        self.assertAlmostEqual(saved["cny"], 0.5)
        self.assertIn(1, storyboard.review_state(self.out)["dirty_segments"])
        self.assertTrue(json.loads(pending_path.read_text())["applied"])

    def test_inflight_frame_edit_blocks_editing_other_frame(self):
        self._enable_two_shots()
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Inflight"}, headers=self.headers)
        pending_path = self.out / "inputs/frame-edit.json"
        pending = json.loads(pending_path.read_text())
        pending["task"] = "inflight-task-1"
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        before = pending_path.read_bytes()
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch") as launch:
            res = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-2", json={"prompt": "Other"}, headers=self.headers)
        self.assertEqual(res.status_code, 400)
        launch.assert_not_called()
        self.assertEqual(pending_path.read_bytes(), before)

    def test_single_frame_terminal_failure_rejects_http_resume(self):
        target = self.out / "frames" / "s1-1.png"
        target.parent.mkdir()
        Image.new("RGB", (8, 8), "red").save(target)
        orig = target.read_bytes()
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Terminal"}, headers=self.headers)
        with patch.object(frames, "submit", return_value="dead-task"), \
             patch.object(frames, "poll", return_value={"state": "failed", "error": "boom"}):
            with self.assertRaises(RuntimeError):
                storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        self.jobs.find(self.identifier)["status"] = "failed"
        snap = self.jobs.snapshot(self.identifier)
        self.assertFalse(snap["can_resume_video"])
        before = (self.out / "inputs/frame-edit.json").read_bytes()
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch") as launch:
            res = self.client.post(f"/api/jobs/{self.identifier}/resume", json={}, headers=self.headers)
        self.assertEqual(res.status_code, 400)
        launch.assert_not_called()
        self.assertEqual((self.out / "inputs/frame-edit.json").read_bytes(), before)
        self.assertEqual(target.read_bytes(), orig)

    def test_terminal_frame_edit_reconciles_to_state_before_other_frame_replaces_pending(self):
        self._enable_two_shots()
        film.Job(self.out).put("frames", "s1-2", error="s1-2 failed earlier", task_failed=True)
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Crash terminal"}, headers=self.headers)
        pending_path = self.out / "inputs/frame-edit.json"
        pending = json.loads(pending_path.read_text())
        pending.update(task="dead-s1-1", task_failed=True, error="remote GPU crash")
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        self.jobs.find(self.identifier)["status"] = "interrupted"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            res = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-2", json={"prompt": "Fix s1-2"}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        with patch.object(frames, "draw", side_effect=lambda *a, **k: (Image.new("RGB", (8, 8), "yellow").save(a[3]) or 0.1)):
            storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        saved_s1 = film.Job(self.out).state["frames"]["s1-1"]
        self.assertTrue(saved_s1.get("task_failed"), "Unreconciled terminal failure on s1-1 was erased when editing s1-2")
        self.assertEqual(saved_s1.get("task"), "dead-s1-1")
        self.assertTrue(saved_s1.get("error"))
        self.assertTrue(json.loads(pending_path.read_text())["applied"])

    def test_settled_failed_single_frame_edit_allows_resuming_inflight_or_unstarted_sibling_frames(self):
        self.request["film"]["shots"] = [
            {"start": 0, "end": 2, "action": "Shot one"},
            {"start": 2, "end": 4, "action": "Shot two"},
        ]
        (self.out / "inputs/film.json").write_text(json.dumps(self.request["film"]), encoding="utf-8")
        self.request_path.write_text(json.dumps(self.request), encoding="utf-8")
        (self.out / "frames").mkdir(exist_ok=True)
        film.Job(self.out).put("frames", "s1-1", prompt="bad", error="failed", task_failed=True)
        film.Job(self.out).put("frames", "s1-2", prompt="good", task="inflight-s1-2", error="poll lost")
        self.jobs.find(self.identifier)["status"] = "failed"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Retry s1-1"}, headers=self.headers)
        with patch.object(frames, "submit", return_value="dead-retry"), \
             patch.object(frames, "poll", return_value={"state": "failed", "error": "still failing"}):
            with self.assertRaises(RuntimeError):
                storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        self.jobs.find(self.identifier)["status"] = "failed"
        snap = self.jobs.snapshot(self.identifier)
        self.assertTrue(snap["can_resume_frames"], "Sibling in-flight frame task must remain resumable after failed single-frame edit")
        self.assertTrue(snap["can_resume_video"])
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            res = self.client.post(f"/api/jobs/{self.identifier}/resume", json={}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        resumed_req = json.loads(self.request_path.read_text(encoding="utf-8"))
        self.assertEqual(resumed_req["operation"], "frames")
        self.assertTrue(resumed_req.get("resume_frame_tasks"))
        with patch.object(frames, "submit", side_effect=AssertionError("resubmitted dead s1-1")), \
             patch.object(frames, "poll", return_value={"state": "done", "url": "https://cdn.example/s2.png", "cny": 0.1}) as poll_mock, \
             patch.object(frames.http, "download", side_effect=lambda u, p: Image.new("RGB", (8, 8), "green").save(p)):
            storyboard.run(resumed_req)
        poll_mock.assert_called_once_with("inflight-s1-2")
        self.assertTrue((self.out / "frames/s1-2.png").is_file())
        self.assertTrue(film.Job(self.out).state["frames"]["s1-1"].get("task_failed"))

    def test_cli_film_resume_polls_inflight_frame_tasks_and_pre_task_frame_edit_allows_sibling_resume(self):
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("h3ref_cli_mod", Path(__file__).resolve().parents[1] / "h3ref.py")
        cli_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli_mod)

        with tempfile.TemporaryDirectory() as tmp:
            cli_dir = Path(tmp) / "cli-job"
            cli_dir.mkdir()
            film_file = Path(tmp) / "film.json"
            film_file.write_text(json.dumps({"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "walk"}]}), encoding="utf-8")
            job = film.Job(cli_dir)
            job.state.update(film_path=str(film_file), mode="shot")
            job.put("frames", "s1-1", prompt="saved p", task="cli-inflight-frame", error="poll lost", reference_paths=[])
            job.save()
            with patch.object(sys, "argv", ["h3ref.py", "film", str(cli_dir)]), \
                 patch.object(frames, "submit", side_effect=AssertionError("resubmitted")), \
                 patch.object(frames, "poll", return_value={"state": "done", "url": "https://cdn.example/f.png", "cny": 0.1}) as cli_poll, \
                 patch.object(frames.http, "download", side_effect=lambda u, p: Image.new("RGB", (8, 8), "red").save(p)), \
                 patch.object(film, "stage_prompts"), patch.object(film, "stage_generate"), \
                 patch.object(film.media, "join", side_effect=lambda ps, p: p.write_bytes(b"mp4")), \
                 patch.object(film.media, "duration", return_value=5):
                job.put("segs", "0", file=str(cli_dir / "clips/seg1.mp4"), cny=1)
                (cli_dir / "clips").mkdir()
                (cli_dir / "clips/seg1.mp4").write_bytes(b"mp4")
                cli_mod.main()
            cli_poll.assert_called_once_with("cli-inflight-frame")
            self.assertTrue((cli_dir / "frames/s1-1.png").is_file())

        # Pre-task 402 single-frame edit failure on s1-1 while s1-2 has task_pending allows /resume to settle s1-2
        self.request["film"]["shots"] = [
            {"start": 0, "end": 2, "action": "Shot one"},
            {"start": 2, "end": 4, "action": "Shot two"},
        ]
        (self.out / "inputs/film.json").write_text(json.dumps(self.request["film"]), encoding="utf-8")
        self.request_path.write_text(json.dumps(self.request), encoding="utf-8")
        (self.out / "frames").mkdir(exist_ok=True)
        film.Job(self.out).put("frames", "s1-1", prompt="bad", error="402 balance")
        film.Job(self.out).put("frames", "s1-2", prompt="good", task="inflight-s1-2-pre", error="poll lost")
        self.jobs.find(self.identifier)["status"] = "failed"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1", json={"prompt": "Retry 402"}, headers=self.headers)
        with patch.object(frames.config, "apimart_key", return_value="k"), \
             patch.object(frames.http, "request", return_value={"error": {"code": 402, "message": "insufficient balance"}}):
            with self.assertRaises(frames.SubmissionRejected):
                storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        self.jobs.find(self.identifier)["status"] = "failed"
        snap = self.jobs.snapshot(self.identifier)
        self.assertTrue(snap["can_resume_frames"])
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            res = self.client.post(f"/api/jobs/{self.identifier}/resume", json={}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        with patch.object(frames, "submit", side_effect=AssertionError("resubmitted s1-1")), \
             patch.object(frames, "poll", return_value={"state": "done", "url": "https://cdn.example/s2.png", "cny": 0.1}) as poll_s2, \
             patch.object(frames.http, "download", side_effect=lambda u, p: Image.new("RGB", (8, 8), "green").save(p)):
            storyboard.run(json.loads(self.request_path.read_text(encoding="utf-8")))
        poll_s2.assert_called_once_with("inflight-s1-2-pre")
        self.assertTrue((self.out / "frames/s1-2.png").is_file())

    def test_pre_task_frame_edit_on_completed_frame_does_not_repoll_old_task_during_sibling_resume(self):
        self.request["film"]["shots"] = [
            {"start": 0, "end": 2, "action": "Shot one"},
            {"start": 2, "end": 4, "action": "Shot two"},
        ]
        (self.out / "inputs/film.json").write_text(json.dumps(self.request["film"]), encoding="utf-8")
        self.request_path.write_text(json.dumps(self.request), encoding="utf-8")
        (self.out / "frames").mkdir(exist_ok=True)
        s1_img = self.out / "frames/s1-1.png"
        Image.new("RGB", (8, 8), "red").save(s1_img)
        original_bytes = s1_img.read_bytes()
        film.Job(self.out).put("frames", "s1-1", prompt="Original s1-1", file=str(s1_img), cny=0.1,
                               task="done-old-s1-1", error=None, task_failed=False)
        film.Job(self.out).put("frames", "s1-2", prompt="Good s1-2", task="inflight-s1-2", error="poll lost")
        self.jobs.find(self.identifier)["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            res = self.client.post(f"/api/jobs/{self.identifier}/frames/s1-1",
                                   json={"prompt": "Ungenerated 402 prompt"}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        with patch.object(frames.config, "apimart_key", return_value="k"), \
             patch.object(frames.http, "request", return_value={"error": {"code": 402, "message": "insufficient balance"}}):
            with self.assertRaises(frames.SubmissionRejected):
                storyboard.regenerate(json.loads(self.request_path.read_text(encoding="utf-8")))
        self.jobs.find(self.identifier)["status"] = "failed"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.assertEqual(self.client.post(f"/api/jobs/{self.identifier}/resume", json={}, headers=self.headers).status_code, 200)
        polled = []
        with patch.object(frames, "submit", side_effect=AssertionError("resubmitted")), \
             patch.object(frames, "poll", side_effect=lambda t: polled.append(t) or {"state": "done", "url": "https://cdn.example/s2.png", "cny": 0.1}), \
             patch.object(frames.http, "download", side_effect=lambda u, p: Image.new("RGB", (8, 8), "green").save(p)):
            storyboard.run(json.loads(self.request_path.read_text(encoding="utf-8")))
        self.assertEqual(polled, ["inflight-s1-2"])
        self.assertEqual(s1_img.read_bytes(), original_bytes)
        self.assertEqual(film.Job(self.out).state["frames"]["s1-1"]["prompt"], "Original s1-1")

    def test_cli_film_rerun_with_explicit_film_flag_polls_inflight_frame_tasks(self):
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("h3ref_cli_flag_mod", Path(__file__).resolve().parents[1] / "h3ref.py")
        cli_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli_mod)
        with tempfile.TemporaryDirectory() as tmp:
            cli_dir = Path(tmp) / "cli-flag-job"
            cli_dir.mkdir()
            film_file = Path(tmp) / "film.json"
            film_file.write_text(json.dumps({"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "walk"}]}), encoding="utf-8")
            job = film.Job(cli_dir)
            job.state.update(film_path=str(film_file), mode="shot")
            job.put("frames", "s1-1", prompt="saved p", task="cli-flag-inflight", error="poll lost", reference_paths=[])
            (cli_dir / "clips").mkdir()
            (cli_dir / "clips/seg1.mp4").write_bytes(b"mp4")
            job.put("segs", "0", file=str(cli_dir / "clips/seg1.mp4"), cny=1)
            job.save()
            with patch.object(sys, "argv", ["h3ref.py", "film", str(cli_dir), "--film", str(film_file)]), \
                 patch.object(frames, "submit", side_effect=AssertionError("resubmitted")), \
                 patch.object(frames, "poll", return_value={"state": "done", "url": "https://cdn.example/f.png", "cny": 0.1}) as cli_poll, \
                 patch.object(frames.http, "download", side_effect=lambda u, p: Image.new("RGB", (8, 8), "red").save(p)), \
                 patch.object(film, "stage_prompts"), patch.object(film, "stage_generate"), \
                 patch.object(film.media, "join", side_effect=lambda ps, p: p.write_bytes(b"mp4")), \
                 patch.object(film.media, "duration", return_value=5):
                cli_mod.main()
            cli_poll.assert_called_once_with("cli-flag-inflight")
            self.assertTrue((cli_dir / "frames/s1-1.png").is_file())
