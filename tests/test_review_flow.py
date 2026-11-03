"""A film must stop for explicit confirmation of the current storyboard before video."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from workbench.server import create_app
from workbench import storyboard


class ReviewFlow(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root / "storage", self.root / ".env")
        self.client = self.app.test_client()
        self.jobs = self.app.extensions["jobs"]
        self.headers = {"X-Workbench": "1"}
        self.client.post("/api/config", json={"APIMART_API_KEY": "fake-image-only", "persist": False}, headers=self.headers)

    def tearDown(self):
        self.jobs.close()
        self.temporary.cleanup()

    def create_film(self, count=2, mode="shot", ready=True):
        script = {"shots": [{"start": i * 2, "end": (i + 1) * 2, "action": f"Object {i} moves"} for i in range(count)]}
        with patch.object(self.jobs, "launch") as launch:
            response = self.client.post("/api/jobs", json={"kind": "film", "demo": False,
                "operation": "pipeline", "frames": mode, "film": script,
                "output_dir": str(self.root / "output")}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(launch.call_args.args[1]["operation"], "frames")
        job = response.json
        if ready:
            for frame in job["storyboard"]:
                path = Path(frame["path"])
                path.parent.mkdir(exist_ok=True)
                Image.new("RGB", (8, 8), "red").save(path)
            record = self.jobs.find(job["id"])
            record["status"] = "succeeded"
            self.jobs.save(record)
        return self.jobs.snapshot(job["id"])

    def confirm(self, job):
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch") as launch:
            response = self.client.post(f"/api/jobs/{job['id']}/continue", json={"approved": True,
                "review_version": job["review"]["version"]}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1]["operation"], "pipeline")
        return response.json

    def test_pipeline_creation_stops_at_frames_and_continue_cannot_bypass_confirmation(self):
        job = self.create_film()
        self.assertEqual(job["operation"], "frames")
        self.assertFalse(job["review"]["complete"])
        with patch.object(self.jobs, "launch") as launch:
            blocked = self.client.post(f"/api/jobs/{job['id']}/continue", json={}, headers=self.headers)
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("确认", blocked.json["error"])
        launch.assert_not_called()
        self.assertFalse((Path(job["output_dir"]) / "film.mp4").exists())

    def test_120_frames_confirm_once_persist_and_changed_image_requires_reconfirmation(self):
        job = self.create_film(120)
        self.assertEqual(len(job["storyboard"]), 120)
        self.assertEqual(job["review"]["total"], 120)
        self.assertTrue(job["review"]["ready"])
        confirmed = self.confirm(job)
        self.assertTrue(confirmed["review"]["complete"])
        restored = create_app(self.root / "storage", self.root / ".env")
        try:
            after_restart = restored.extensions["jobs"].snapshot(job["id"])
            self.assertTrue(after_restart["review"]["complete"])
        finally:
            restored.extensions["jobs"].close()
        Image.new("RGB", (9, 9), "blue").save(job["storyboard"][30]["path"])
        current = self.jobs.snapshot(job["id"])
        self.assertFalse(current["review"]["complete"])

    def test_none_requires_explicit_script_confirmation(self):
        job = self.create_film(mode="none")
        self.assertEqual(job["review"]["kind"], "script")
        self.assertFalse(job["review"]["complete"])
        self.assertEqual(len(job["review"]["plan"]["segments"][0]["shots"]), 2)
        blocked = self.client.post(f"/api/jobs/{job['id']}/continue", json={}, headers=self.headers)
        self.assertEqual(blocked.status_code, 400)
        self.assertTrue(self.confirm(job)["review"]["complete"])

    def test_outdated_confirmation_cannot_start_video_after_an_image_changes(self):
        job = self.create_film()
        Image.new("RGB", (9, 9), "blue").save(job["storyboard"][0]["path"])
        with patch.object(self.jobs, "launch") as launch:
            response = self.client.post(f"/api/jobs/{job['id']}/continue", json={"approved": True,
                "review_version": job["review"]["version"]}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        launch.assert_not_called()
        self.assertFalse(self.jobs.snapshot(job["id"])["review"]["complete"])

    def test_incomplete_or_missing_frames_cannot_be_confirmed(self):
        job = self.create_film(ready=False)
        for status in ("running", "succeeded"):
            self.jobs.find(job["id"])["status"] = status
            with patch.object(self.jobs, "launch") as launch:
                response = self.client.post(f"/api/jobs/{job['id']}/continue", json={"review_version": job["review"]["version"],
                    "approved": True}, headers=self.headers)
            self.assertEqual(response.status_code, 400)
            launch.assert_not_called()

    def test_zero_frame_budget_uses_script_confirmation_but_missing_frames_do_not(self):
        script = {"aspect": "9:16", "cast": [{"name": f"Actor {i}", "picture": f"/unused/{i}.png"} for i in range(9)],
                  "shots": [{"start": 0, "end": 5, "action": "everyone walks"}]}
        request = {"output_dir": str(self.root), "film": script, "frames": "shot"}
        summary = storyboard.review_status(request, [])
        self.assertEqual(summary["kind"], "script")
        self.assertFalse(summary["complete"])
        (self.root / "inputs").mkdir()
        (self.root / "inputs" / "review.json").write_text(json.dumps({"confirmed_version": summary["version"]}))
        self.assertTrue(storyboard.review_status(request, [])["complete"])
        request["film"]["cast"] = []
        missing = storyboard.review_status(request, [])
        self.assertEqual(missing["kind"], "frames")
        self.assertEqual(missing["total"], 1)
        self.assertFalse(missing["ready"])
        self.assertFalse(missing["complete"])

    def test_resuming_unreviewed_old_pipeline_returns_to_frames(self):
        job = self.create_film()
        path = Path(job["output_dir"]) / "inputs" / "request.json"
        request = json.loads(path.read_text())
        request.update(operation="pipeline", redo=[1], reset_applied=False)
        path.write_text(json.dumps(request))
        self.jobs.find(job["id"])["status"] = "failed"
        with patch.object(self.jobs, "launch") as launch:
            resumed = self.client.post(f"/api/jobs/{job['id']}/resume", json={}, headers=self.headers)
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(launch.call_args.args[1]["operation"], "frames")
        self.assertNotIn("redo", launch.call_args.args[1])

    def test_resuming_confirmed_pipeline_keeps_the_video_stage(self):
        job = self.create_film()
        self.confirm(job)
        self.jobs.find(job["id"])["status"] = "failed"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch") as launch:
            resumed = self.client.post(f"/api/jobs/{job['id']}/resume", json={}, headers=self.headers)
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(launch.call_args.args[1]["operation"], "pipeline")

    def test_regeneration_invalidates_confirmation_and_keeps_other_images(self):
        job = self.create_film()
        self.confirm(job)
        self.jobs.find(job["id"])["status"] = "succeeded"
        target = job["storyboard"][1]
        unchanged = Path(job["storyboard"][0]["path"]).read_bytes()
        with patch.object(self.jobs, "launch"):
            response = self.client.post(f"/api/jobs/{job['id']}/frames/{target['key']}", json={"prompt": "New light"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        out = Path(job["output_dir"])
        request = json.loads((out / "inputs" / "request.json").read_text())
        def draw(prompt, refs, aspect, output, **kwargs):
            Image.new("RGB", (8, 8), "blue").save(output)
            return 0
        with patch.object(storyboard.frames, "draw", side_effect=draw):
            storyboard.regenerate(request)
        self.assertNotIn("confirmed_version", storyboard.review_state(out))
        self.assertEqual(Path(job["storyboard"][0]["path"]).read_bytes(), unchanged)


if __name__ == "__main__":
    unittest.main()
