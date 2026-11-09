"""Content rejection must wait for an explicit prompt edit, never another paid attempt."""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from h3ref import film, frames
from workbench import storyboard
from workbench.server import create_app


class FrameModeration(unittest.TestCase):
    def setUp(self):
        key = patch.object(frames.config, "apimart_key", return_value="synthetic-apimart-key")
        key.start()
        self.addCleanup(key.stop)

    def test_intensifiers_containing_forbidden_character_are_rephrased_naturally(self):
        for action, expected in (("死命抓住扶手", "使劲抓住扶手"), ("死盯着门口", "紧盯着门口"),
                                 ("死寂的走廊", "寂静的走廊"), ("死撑着站起来", "勉强支撑着站起来")):
            with self.subTest(action=action):
                prompt = frames.prompt({"aspect": "9:16"}, {"action": action}, [], False, False)
                self.assertNotIn("死", prompt)
                self.assertIn(expected, prompt)

    def test_automatic_prompt_uses_neutral_visible_descriptions(self):
        script = {"aspect": "9:16", "look": "血腥风格", "setting": "尸体旁有血迹"}
        shot = {"camera": "特写", "action": "角色死死盯着对方，拼死挣扎后倒地死亡"}
        who = [{"name": "主角", "look": "浑身鲜血，伤口明显"}]
        original = json.dumps([script, shot, who], ensure_ascii=False)
        prompt = frames.prompt(script, shot, who, True, True)
        for word in ("死", "尸体", "血腥", "血迹", "鲜血", "伤口"):
            self.assertNotIn(word, prompt)
        self.assertIn("紧紧盯着", prompt)
        self.assertIn("参考图片3", prompt)
        self.assertEqual(json.dumps([script, shot, who], ensure_ascii=False), original)

    def test_submission_and_polling_rejections_preserve_classification(self):
        for response in ({"error": {"code": "content_policy_violation", "message": "Rejected"}},
                         {"code": 400, "message": "内容审核不通过"}):
            with self.subTest(response=response), patch.object(frames.http, "request", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "Rejected|内容审核") as caught:
                    frames.submit("prompt", [], "9:16")
                self.assertEqual(type(caught.exception).__name__, "ContentPolicyError")
        with patch.object(frames.http, "request", return_value={"data": {"status": "failed",
                "error": {"code": "content_policy_violation", "message": "Rejected"}}}):
            result = frames.poll("task")
        self.assertTrue(result.get("moderation_failed"))
        self.assertIn("Rejected", result["error"])

    def test_wrapped_http_rejection_does_not_retry_the_generation_request(self):
        error = urllib.error.HTTPError("https://api.example/images/generations", 500, "Upstream rejected", {},
            io.BytesIO(b'{"error":{"code":"content_policy_violation","message":"Rejected"}}'))
        with patch("urllib.request.urlopen", side_effect=error) as opened, patch.object(frames.http.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "content_policy_violation") as caught:
                frames.submit("prompt", [], "9:16")
        error.close()
        self.assertEqual(type(caught.exception).__name__, "ContentPolicyError")
        opened.assert_called_once()

    def test_poll_endpoint_rejection_is_terminal_even_without_task_status(self):
        with patch.object(frames.http, "request", return_value={"error": {"code": "content_filter", "message": "Rejected"}}):
            with self.assertRaisesRegex(RuntimeError, "Rejected"):
                frames.poll("old-task")

    def test_rejected_old_preview_cannot_be_confirmed_for_video(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "frame.png"
            Image.new("RGB", (8, 8), "red").save(image)
            request = {"output_dir": temp, "frames": "shot", "film": {"shots": [{"start": 0, "end": 5, "action": "walk"}]}}
            summary = storyboard.review_status(request, [{"key": "s1-1", "path": str(image), "url": "/old.png", "error": "Rejected"}])
            self.assertFalse(summary["ready"])
            self.assertFalse(summary["complete"])

    def test_batch_rejection_is_not_resubmitted_even_after_reloading_job(self):
        with tempfile.TemporaryDirectory() as temp:
            script = {"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "walk"}]}
            segments = film.plan(script["shots"])
            pictures = {0: film.refs(script, segments[0], "shot")}
            with patch.object(frames, "submit", return_value="rejected-task") as submit, patch.object(frames, "poll",
                    return_value={"state": "failed", "error": "content_policy_violation", "moderation_failed": True}):
                for _ in range(2):
                    self.assertEqual(film.stage_frames(film.Job(Path(temp)), script, segments, pictures), ["s1-1"])
                self.assertEqual(submit.call_count, 1)
            record = film.Job(Path(temp)).state["frames"]["s1-1"]
            self.assertTrue(record.get("moderation_error"))
            self.assertEqual(record["task"], "rejected-task")

    def test_single_frame_resume_keeps_rejection_and_old_pixels(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            (out / "inputs").mkdir()
            (out / "frames").mkdir()
            script = {"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "walk"}]}
            (out / "inputs" / "film.json").write_text(json.dumps(script))
            target = out / "frames" / "s1-1.png"
            Image.new("RGB", (8, 8), "red").save(target)
            original = target.read_bytes()
            previous = {"prompt": "old prompt", "task": "old-task"}
            film.Job(out).put("frames", "s1-1", **previous)
            (out / "inputs" / "frame-edit.json").write_text(json.dumps({"key": "s1-1", "prompt": "rejected prompt",
                "revision": "edit", "previous": previous, "task": None}))
            request = {"output_dir": str(out), "demo": False, "frames": "shot", "lang": "en"}
            with patch.object(frames, "submit", return_value="rejected-task") as submit, patch.object(frames, "poll",
                    return_value={"state": "failed", "error": "content_policy_violation", "moderation_failed": True}):
                for _ in range(2):
                    with self.assertRaises(RuntimeError):
                        storyboard.regenerate(request)
                self.assertEqual(submit.call_count, 1)
            self.assertEqual(target.read_bytes(), original)
            pending = json.loads((out / "inputs" / "frame-edit.json").read_text())
            self.assertEqual(pending["task"], "rejected-task")
            self.assertTrue(pending.get("moderation_error"))
            record = film.Job(out).state["frames"]["s1-1"]
            self.assertEqual(record["prompt"], "old prompt")
            self.assertTrue(record.get("moderation_error"))


class ManualModerationRecovery(unittest.TestCase):
    def test_uncertain_task_cannot_be_overwritten_by_another_manual_edit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = create_app(root / "storage", root / ".env")
            jobs = app.extensions["jobs"]
            client = app.test_client()
            headers = {"X-Workbench": "1"}
            try:
                with patch("workbench.jobs.preflight"), patch.object(jobs, "launch"):
                    response = client.post("/api/jobs", json={"kind": "film", "demo": False, "frames": "shot",
                        "film": {"shots": [{"action": "walk"}]}, "output_dir": str(root / "output")}, headers=headers)
                identifier = response.json["id"]
                out = Path(response.json["output_dir"])
                (out / "frames").mkdir()
                Image.new("RGB", (8, 8), "red").save(out / "frames" / "s1-1.png")
                jobs.find(identifier)["status"] = "succeeded"
                with patch("workbench.jobs.preflight"), patch.object(jobs, "launch"):
                    client.post(f"/api/jobs/{identifier}/frames/s1-1", json={"prompt": "new prompt"}, headers=headers)
                pending_path = out / "inputs" / "frame-edit.json"
                pending = json.loads(pending_path.read_text())
                pending["task"] = "uncertain-cloud-task"
                pending_path.write_text(json.dumps(pending))
                original = pending_path.read_bytes()
                # A prior rejection must not unlock the uncertain replacement task either.
                film.Job(out).put("frames", "s1-1", moderation_error="older rejection", failed_prompt="old prompt")
                jobs.find(identifier)["status"] = "failed"
                self.assertFalse(jobs.snapshot(identifier)["storyboard"][0]["references_editable"])
                with patch("workbench.jobs.preflight"), patch.object(jobs, "launch") as launch:
                    response = client.post(f"/api/jobs/{identifier}/frames/s1-1", json={"prompt": "another prompt"}, headers=headers)
                self.assertEqual(response.status_code, 400)
                launch.assert_not_called()
                self.assertEqual(pending_path.read_bytes(), original)
            finally:
                jobs.close()

    def test_first_rejected_frame_can_be_edited_then_remaining_frames_can_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = create_app(root / "storage", root / ".env")
            jobs = app.extensions["jobs"]
            client = app.test_client()
            headers = {"X-Workbench": "1"}
            script = {"shots": [{"start": 0, "end": 2, "action": "walk"}, {"start": 2, "end": 4, "action": "turn"}]}
            try:
                with patch("workbench.jobs.preflight"), patch.object(jobs, "launch"):
                    response = client.post("/api/jobs", json={"kind": "film", "demo": False, "frames": "shot",
                        "film": script, "output_dir": str(root / "output")}, headers=headers)
                self.assertEqual(response.status_code, 201, response.json)
                identifier = response.json["id"]
                out = Path(response.json["output_dir"])
                jobs.find(identifier)["status"] = "failed"
                film.Job(out).put("frames", "s1-1", prompt="rejected prompt", moderation_error="Rejected")
                current = jobs.snapshot(identifier)
                self.assertTrue(current["storyboard"][0]["editable"])
                self.assertTrue(current["storyboard"][0].get("error"))
                self.assertFalse(current["storyboard"][1]["editable"])
                with patch("workbench.jobs.preflight"), patch.object(jobs, "launch"):
                    unchanged = client.post(f"/api/jobs/{identifier}/frames/s1-1", json={"prompt": "rejected prompt"}, headers=headers)
                    self.assertEqual(unchanged.status_code, 400)
                    response = client.post(f"/api/jobs/{identifier}/frames/s1-1", json={"prompt": "A person stands."}, headers=headers)
                self.assertEqual(response.status_code, 200, response.json)
                request = json.loads((out / "inputs" / "request.json").read_text())
                def download(url, path):
                    Image.new("RGB", (8, 8), "blue").save(path)
                with patch.object(frames, "submit", return_value="new-task") as submit, patch.object(frames, "poll",
                        return_value={"state": "done", "url": "fake://image"}), patch.object(frames.http, "download", side_effect=download):
                    storyboard.regenerate(request)
                    submit.assert_called_once()
                self.assertFalse(film.Job(out).state["frames"]["s1-1"].get("moderation_error"))
                jobs.find(identifier)["status"] = "succeeded"
                self.assertTrue(jobs.snapshot(identifier).get("can_resume_frames"))
                with patch("workbench.jobs.preflight"), patch.object(jobs, "launch") as launch:
                    response = client.post(f"/api/jobs/{identifier}/resume", json={}, headers=headers)
                self.assertEqual(response.status_code, 200, response.json)
                self.assertEqual(launch.call_args.args[1]["operation"], "frames")
            finally:
                jobs.close()
