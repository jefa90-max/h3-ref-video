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
                    self.assertEqual(bool(references), index > 0 and 0 not in failed)
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

    def test_missing_first_frame_keeps_board_and_character_references_for_later_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            board, actor = out / "board.png", out / "actor.png"
            for path in (board, actor):
                Image.new("RGB", (8, 8), "blue").save(path)
            script = self.script()
            script.update(board=str(board), cast=[{"name": "Object", "picture": str(actor)}])
            segments = film.plan(script["shots"])
            pictures = {segment["idx"]: film.refs(script, segment, "shot") for segment in segments}
            def draw(prompt, references, aspect, output, **kwargs):
                self.assertEqual(list(map(str, references)), [str(board), str(actor)])
                if output.stem == "s1-1":
                    raise frames.ContentPolicyError("Rejected")
                Image.new("RGB", (8, 8), "red").save(output)
                return 0
            with patch.object(frames, "draw", side_effect=draw) as drawing:
                self.assertEqual(film.stage_frames(film.Job(out), script, segments, pictures), ["s1-1"])
            self.assertEqual(drawing.call_count, 4)

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

    def test_partial_batch_exposes_editable_failure_and_preserves_actual_anchor_references(self):
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
