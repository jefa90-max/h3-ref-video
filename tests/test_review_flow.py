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
        import os
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_patch = patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.app = create_app(self.root / "storage", self.root / ".env")
        self.client = self.app.test_client()
        self.jobs = self.app.extensions["jobs"]
        self.headers = {"X-Workbench": "1"}
        self.client.post("/api/config", json={"LLM_PROVIDER": "apimart", "APIMART_API_KEY": "fake-image-only", "persist": False}, headers=self.headers)

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

    def test_video_override_survives_real_frame_edit_review_and_pipeline(self):
        from h3ref import film, frames, media, rewrite, rh
        from workbench import segments
        job = self.confirm(self.create_film())
        out = Path(job['output_dir']); (out / 'clips').mkdir(exist_ok=True)
        (out / 'clips/seg1.mp4').write_bytes(b'old clip')
        (out / 'clips/seg1-last.jpg').write_bytes(b'old tail')
        (out / 'film.mp4').write_bytes(b'old film')
        h3 = '\n\n'.join(name + ':\n' + ('[Shot 1] The recipient kneels.' if name == 'detailed_description' else 'N/A') for name in rewrite.SECTIONS)
        film.Job(out).put('segs', '0', zh='old source', h3=h3, file=str(out / 'clips/seg1.mp4'), cny=1)
        self.jobs.find(job['id'])['status'] = 'succeeded'
        card = self.jobs.snapshot(job['id'])['segments'][0]
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch'):
            response = self.client.post(f"/api/jobs/{job['id']}/segments/1/regenerate", json=dict(version=card['version'], prompt_version=card['prompt_version'], prompt=h3, prompt_format='h3'), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        submitted = []
        with patch.object(rh, 'upload', return_value='uploaded'), patch.object(rh, 'submit_with_retry', side_effect=lambda graph, **kw: submitted.append(graph) or 'edited-task'), patch.object(rh, 'poll', return_value=dict(state='done', url='offline://video', cny=1)), patch.object(film.http, 'download', side_effect=lambda url, path: path.write_bytes(f'new video {len(submitted)}'.encode())), patch.object(media, 'last_frame', side_effect=lambda clip, path: path.write_bytes(b'new tail')), patch.object(media, 'duration', return_value=4), patch.object(media, 'join', side_effect=lambda paths, path: path.write_bytes(b'assembled')), patch.object(rewrite, 'rewrite', side_effect=AssertionError('rewrote H3 override')):
            segments.regenerate(json.loads((out / 'inputs/request.json').read_text()))
            self.jobs.find(job['id'])['status'] = 'succeeded'
            with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch'):
                response = self.client.post(f"/api/jobs/{job['id']}/frames/s1-1", json={'prompt':'changed opening'}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
            with patch.object(frames, 'draw', side_effect=lambda text, refs, aspect, path, **kw: (Image.new('RGB', (10, 10), 'blue').save(path) or 0)):
                storyboard.regenerate(json.loads((out / 'inputs/request.json').read_text()))
            self.jobs.find(job['id'])['status'] = 'succeeded'
            current = self.jobs.snapshot(job['id'])
            self.assertTrue(current['pending_video'])
            self.assertTrue(current['segments'][0]['source_changed'])
            self.confirm(current)
            storyboard.run(json.loads((out / 'inputs/request.json').read_text()))
        with self.client.get(self.jobs.snapshot(job['id'])['segments'][0]['url']) as response:
            self.assertEqual(response.data, b'new video 2')
        self.assertEqual(len(submitted), 2)
        self.assertEqual(submitted[-1][rh.PROMPT]['inputs']['prompt'], h3)
        self.assertEqual(film.Job(out).state['segs']['0']['prompt_override']['text'], h3)

    def test_dirty_first_mode_frame_approval_regenerates_chained_suffix(self):
        from h3ref import film, frames, media, rewrite, rh
        script = {"shots": [{"start": i * 8, "end": (i + 1) * 8, "action": f"Shot {i}"} for i in range(3)]}
        with patch.object(self.jobs, "launch"):
            job = self.client.post("/api/jobs", json={"kind": "film", "demo": False, "frames": "first",
                "film": script, "output_dir": str(self.root / "first-out")}, headers=self.headers).json
        out = Path(job["output_dir"])
        (out / "clips").mkdir()
        for frame in job["storyboard"]:
            Path(frame["path"]).parent.mkdir(exist_ok=True)
            Image.new("RGB", (8, 8), "red").save(frame["path"])
        for i in range(3):
            clip = out / f"clips/seg{i + 1}.mp4"
            clip.write_bytes(f"old-{i + 1}".encode())
            (out / f"clips/seg{i + 1}-last.jpg").write_bytes(b"tail")
            film.Job(out).put("segs", str(i), zh="src", h3="h3", file=str(clip), cny=1, task=f"t-{i}")
        self.jobs.find(job["id"])["status"] = "succeeded"
        self.confirm(self.jobs.snapshot(job["id"]))
        self.jobs.find(job["id"])["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{job['id']}/frames/s2-1", json={"prompt": "updated s2"}, headers=self.headers)
        with patch.object(frames, "draw", side_effect=lambda *a, **k: (Image.new("RGB", (8, 8), "blue").save(a[3]) or 0)):
            storyboard.regenerate(json.loads((out / "inputs/request.json").read_text()))
        self.jobs.find(job["id"])["status"] = "succeeded"
        self.confirm(self.jobs.snapshot(job["id"]))
        with patch.object(rewrite, "rewrite", return_value="h3"), \
             patch.object(rh, "upload", return_value="u"), \
             patch.object(rh, "submit_with_retry", return_value="new-t"), \
             patch.object(rh, "poll", return_value={"state": "done", "url": "https://cdn.example/v.mp4", "cny": 1}), \
             patch.object(film.http, "download", side_effect=lambda u, p: p.write_bytes(b"new")), \
             patch.object(media, "last_frame", side_effect=lambda c, p: p.write_bytes(b"new-tail")), \
             patch.object(media, "join", side_effect=lambda ps, p: p.write_bytes(b"film")), \
             patch.object(media, "duration", return_value=24):
            storyboard.run(json.loads((out / "inputs/request.json").read_text()))
        self.assertEqual((out / "clips/seg1.mp4").read_bytes(), b"old-1")
        self.assertEqual((out / "clips/seg2.mp4").read_bytes(), b"new")
        self.assertEqual((out / "clips/seg3.mp4").read_bytes(), b"new")

    def test_dirty_shot_mode_frame_approval_regenerates_only_dirty_segment_and_clears_dirty(self):
        from h3ref import film, frames, media, rewrite, rh
        script = {"shots": [{"start": i * 8, "end": (i + 1) * 8, "action": f"Shot {i}"} for i in range(3)]}
        with patch.object(self.jobs, "launch"):
            job = self.client.post("/api/jobs", json={"kind": "film", "demo": False, "frames": "shot",
                "film": script, "output_dir": str(self.root / "shot-out")}, headers=self.headers).json
        out = Path(job["output_dir"])
        (out / "clips").mkdir()
        for frame in job["storyboard"]:
            Path(frame["path"]).parent.mkdir(exist_ok=True)
            Image.new("RGB", (8, 8), "red").save(frame["path"])
        for i in range(3):
            clip = out / f"clips/seg{i + 1}.mp4"
            clip.write_bytes(f"old-shot-{i + 1}".encode())
            (out / f"clips/seg{i + 1}-last.jpg").write_bytes(b"tail")
            film.Job(out).put("segs", str(i), zh="src", h3="h3", file=str(clip), cny=1, task=f"t-{i}")
        self.jobs.find(job["id"])["status"] = "succeeded"
        self.confirm(self.jobs.snapshot(job["id"]))
        self.jobs.find(job["id"])["status"] = "succeeded"
        with patch("workbench.jobs.preflight"), patch.object(self.jobs, "launch"):
            self.client.post(f"/api/jobs/{job['id']}/frames/s2-1", json={"prompt": "updated shot s2"}, headers=self.headers)
        with patch.object(frames, "draw", side_effect=lambda *a, **k: (Image.new("RGB", (8, 8), "blue").save(a[3]) or 0)):
            storyboard.regenerate(json.loads((out / "inputs/request.json").read_text()))
        self.jobs.find(job["id"])["status"] = "succeeded"
        confirmed = self.confirm(self.jobs.snapshot(job["id"]))
        self.assertTrue(confirmed["review"]["complete"])
        req = json.loads((out / "inputs/request.json").read_text())
        self.assertEqual(req["redo"], [2])
        with patch("subprocess.Popen") as popen, patch("workbench.jobs.stop_posix_descendants_on_exit"), patch("workbench.jobs.stop_child"):
            def fake_worker(*_a, **_kw):
                with patch.object(rewrite, "rewrite", return_value="h3"), \
                     patch.object(rh, "upload", return_value="u"), \
                     patch.object(rh, "submit_with_retry", return_value="new-s2"), \
                     patch.object(rh, "poll", return_value={"state": "done", "url": "https://cdn.example/v.mp4", "cny": 1}), \
                     patch.object(film.http, "download", side_effect=lambda u, p: p.write_bytes(b"new-shot-2")), \
                     patch.object(media, "last_frame", side_effect=lambda c, p: p.write_bytes(b"new-tail")), \
                     patch.object(media, "join", side_effect=lambda ps, p: p.write_bytes(b"film")), \
                     patch.object(media, "duration", return_value=24):
                    storyboard.run(json.loads((out / "inputs/request.json").read_text()))
                from unittest.mock import MagicMock
                return MagicMock(pid=12345, stdout=iter([]), wait=lambda *_x, **_y: 0)
            popen.side_effect = fake_worker
            self.jobs.run(self.jobs.find(job["id"]), json.loads((out / "inputs/request.json").read_text()), {})
        self.assertEqual((out / "clips/seg1.mp4").read_bytes(), b"old-shot-1")
        self.assertEqual((out / "clips/seg2.mp4").read_bytes(), b"new-shot-2")
        self.assertEqual((out / "clips/seg3.mp4").read_bytes(), b"old-shot-3")
        self.assertEqual(json.loads((out / "inputs/review.json").read_text())["dirty_segments"], [])


if __name__ == "__main__":
    unittest.main()
