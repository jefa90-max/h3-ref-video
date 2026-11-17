import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from h3ref import film, rewrite, rh
from workbench import segments, storyboard
from workbench.jobs import write_json
from workbench.server import create_app


def _make_h3(tag="default", n_images=1):
    pic = "<Picture 1>" if n_images >= 1 else "reference"
    parts = {
        "subject_definitions": f"<Subject 1> Protagonist from {pic} ({tag}).",
        "summary": f"[reference generation] Scene {tag}.",
        "retention_analysis": "<Subject 1>: fully_preserved.",
        "detailed_description": f"Vertical 9:16 short ({tag}).\n[Shot 1] <Subject 1> speaks calmly.",
        "overall_soundscape": "Soft indoor room tone.",
        "non_diegetic_music": "N/A",
    }
    return "\n\n".join(f"{name}:\n{parts[name]}" for name in rewrite.SECTIONS)


class ManualH3RewriteTest(unittest.TestCase):
    def setUp(self):
        env_patch = patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.actor = self.root / "actor.png"
        Image.new("RGB", (8, 8), "red").save(self.actor)
        self.app = create_app(self.root / "storage", self.root / ".env")
        self.jobs = self.app.extensions["jobs"]
        self.addCleanup(self.jobs.close)
        self.client = self.app.test_client()
        self.headers = {"X-Workbench": "1", "Accept-Language": "zh-CN"}

    def _create_historical_film_job(self, job_id, *, frames_mode="shot", demo=False, with_clips=(1, 2, 3)):
        out = self.root / f"out-{job_id}"
        for folder in ("inputs", "frames", "clips", "prompts"):
            (out / folder).mkdir(parents=True, exist_ok=True)
        script = {
            "aspect": "9:16",
            "cast": [{"name": "阿青", "look": "青色长衫", "picture": str(self.actor)}],
            "shots": [
                {"start": 0, "end": 8, "action": "阿青走进屋内"},
                {"start": 8, "end": 16, "action": "阿青坐下喝茶"},
                {"start": 16, "end": 24, "action": "阿青望向窗外"},
            ],
        }
        if frames_mode == "shot":
            for i in range(3):
                Image.new("RGB", (8, 8), "green").save(out / "frames" / f"s{i + 1}-1.png")
        elif frames_mode == "first":
            for i in range(3):
                Image.new("RGB", (8, 8), "green").save(out / "frames" / f"s{i + 1}-1.png")
        request = {
            "kind": "film",
            "demo": demo,
            "frames": frames_mode,
            "optimize_storyboard": False,
            "lang": "zh-CN",
            "output_dir": str(out),
            "film": script,
            "operation": "pipeline",
        }
        write_json(out / "inputs/film.json", script)
        if demo:
            write_json(out / "inputs/demo-film.json", script)
        write_json(out / "inputs/request.json", request)
        descriptions = storyboard.view(request, self.jobs.artifacts({"id": job_id, "output_dir": str(out)}), False)
        review = storyboard.review_status(request, descriptions)
        write_json(out / "inputs/review.json", {"dirty_segments": [], "confirmed_version": review["version"]})

        fjob = film.Job(out)
        planned = film.plan(script["shots"])
        for seg in planned:
            num = seg["idx"] + 1
            pics = film.refs(script, seg, frames_mode)
            zh = film.compile_prompt(script, seg, pics, len(planned))
            if num in with_clips:
                clip = out / "clips" / f"seg{num}.mp4"
                clip.write_bytes(f"CLIP-{num}".encode())
                (out / "clips" / f"seg{num}-last.jpg").write_bytes(f"TAIL-{num}".encode())
                h3_txt = _make_h3(f"initial-{num}", len(pics))
                paths = film.picture_paths(out, seg, pics)
                ref_ver = film.reference_version(paths)
                fjob.put(
                    "segs",
                    str(seg["idx"]),
                    zh=zh,
                    h3=h3_txt,
                    h3_source=zh,
                    h3_reference_version=ref_ver,
                    file=str(clip),
                    cny=0.1,
                    task=f"rh-old-{num}",
                )
                (out / "prompts" / f"seg{num}.zh.txt").write_text(zh, encoding="utf-8")
                (out / "prompts" / f"seg{num}.h3.txt").write_text(h3_txt, encoding="utf-8")
            else:
                fjob.put("segs", str(seg["idx"]), pics=[p["kind"] for p in pics])

        status = "succeeded" if len(with_clips) == 3 else "interrupted"
        self.jobs.records[job_id] = {
            "id": job_id,
            "kind": "film",
            "demo": demo,
            "status": status,
            "operation": "pipeline",
            "output_dir": str(out),
            "lang": "zh-CN",
            "created_at": "2026-11-16T00:00:00+0000",
            "error": None if status == "succeeded" else "interrupted",
        }
        self.jobs.save(self.jobs.records[job_id])
        return out, script

    def test_single_segment_rewrite_updates_h3_keeps_mp4_and_enables_reuse_h3(self):
        out, _ = self._create_historical_film_job("hist-single")
        # Configure ONLY LLM_API_KEY (NO RUNNINGHUB_API_KEY, NO ffmpeg on PATH!)
        self.client.post("/api/config", json={"LLM_API_KEY": "sk-llm-only", "persist": False}, headers=self.headers)
        original_mp4 = (out / "clips" / "seg2.mp4").read_bytes()

        snap0 = self.client.get("/api/jobs/hist-single").json
        card2 = snap0["segments"][1]
        self.assertTrue(card2["can_rewrite"])

        # Stale prompt_version is rejected with 400 segment_prompt_changed
        bad_ver = self.client.post(
            "/api/jobs/hist-single/segments/2/rewrite",
            json={"prompt": "修改后的第二段中文词", "prompt_format": "source", "prompt_version": "stale-version"},
            headers=self.headers,
        )
        self.assertEqual(bad_ver.status_code, 400)
        self.assertEqual(bad_ver.json["code"], "segment_prompt_changed")

        # Valid single-segment rewrite succeeds without ffmpeg or RUNNINGHUB_API_KEY
        with patch("workbench.segments.shutil.which", return_value=None), \
             patch.object(self.jobs, "launch") as launch_mock:
            resp = self.client.post(
                "/api/jobs/hist-single/segments/2/rewrite",
                json={
                    "prompt": "阿青端起茶盏吹散热气",
                    "prompt_format": "source",
                    "prompt_version": card2["prompt_version"],
                },
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200, resp.json)
        launch_mock.assert_called_once()

        # Execute the child segment rewrite entrypoint directly with RH forbidden
        req = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        self.assertEqual(req["operation"], "rewrite_segment")
        new_h3 = _make_h3("rewritten-seg2", 2)
        with patch.object(rewrite, "rewrite", return_value=new_h3) as rw_mock, \
             patch.object(rh, "upload", side_effect=AssertionError("must not call RH")), \
             patch.object(rh, "submit_with_retry", side_effect=AssertionError("must not submit video")):
            segments.rewrite_segment(req)
        rw_mock.assert_called_once()
        self.jobs.records["hist-single"].update(status="succeeded", error=None)

        # MP4 untouched, H3 & source saved on disk and exposed in snapshot with source_rewrite_required=False
        self.assertEqual((out / "clips" / "seg2.mp4").read_bytes(), original_mp4)
        self.assertEqual((out / "prompts" / "seg2.h3.txt").read_text(encoding="utf-8"), new_h3)
        snap1 = self.client.get("/api/jobs/hist-single").json
        updated_card2 = snap1["segments"][1]
        self.assertEqual(updated_card2["source_prompt"], "阿青端起茶盏吹散热气")
        self.assertEqual(updated_card2["h3_prompt"], new_h3)
        self.assertFalse(updated_card2["source_rewrite_required"])

        # Subsequent single-segment video regeneration hits reuse_h3=True (needs RH key, NOT LLM key, 0 rewrite calls)
        plan_dict = segments.regeneration_plan(
            req,
            2,
            updated_card2["version"],
            prompt=updated_card2["source_prompt"],
            prompt_format="source",
            prompt_version=updated_card2["prompt_version"],
        )
        self.assertTrue(plan_dict["reuse_h3"])
        with patch("workbench.segments.shutil.which", return_value="/fake/ffmpeg"):
            segments.preflight(
                {**req, "operation": "segment", "segment": 2, "_segment_preparation": plan_dict},
                {"RUNNINGHUB_API_KEY": "rh-only"},  # No LLM key needed because reuse_h3 is True!
            )

    def test_batch_rewrite_missing_vs_all_and_chained_resume_handoff(self):
        # Interrupted chained ("none") job: seg 1 has clip+h3, seg 2 has no clip/no h3/no seg1-last.jpg, seg 3 has outdated h3
        out, script = self._create_historical_film_job("hist-batch", frames_mode="none", with_clips=(1,))
        (out / "clips" / "seg1-last.jpg").unlink()  # Even without prev tail image, preflight & rewrite work!
        fjob = film.Job(out)
        fjob.put("segs", "2", zh="阿青旧词", h3=_make_h3("stale", 1), h3_source="更早期的词")
        self.client.post("/api/config", json={"LLM_API_KEY": "sk-llm-only", "persist": False}, headers=self.headers)

        with patch("workbench.segments.shutil.which", return_value=None), \
             patch.object(self.jobs, "launch"):
            resp = self.client.post(
                "/api/jobs/hist-batch/segments/rewrite",
                json={"scope": "missing"},
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200, resp.json)
        req = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        self.assertEqual(req["operation"], "rewrite_prompts")

        rewritten_calls = []

        def fake_rewrite(zh_text, n_images, seconds, aspect):
            rewritten_calls.append((zh_text, n_images))
            return _make_h3(f"batch-{len(rewritten_calls)}", n_images)

        with patch.object(rewrite, "rewrite", side_effect=fake_rewrite):
            segments.rewrite_prompts(req)
        # Only seg 2 (missing) and seg 3 (mismatched h3_source) were rewritten; seg 1 was skipped!
        self.assertEqual(len(rewritten_calls), 2)
        self.jobs.records["hist-batch"].update(status="succeeded", error=None, operation="rewrite_prompts")

        snap = self.client.get("/api/jobs/hist-batch").json
        # Even though seg 2 and seg 3 have NO mp4 (version is None), source_prompt and h3_prompt are visible!
        self.assertIsNone(snap["segments"][1]["version"])
        self.assertTrue(snap["segments"][1]["source_prompt"])
        self.assertIn("batch-", snap["segments"][1]["h3_prompt"])
        self.assertTrue(snap["segments"][2]["h3_prompt"])
        # Unfinished historical job remains resumable into pipeline after manual prompt rewrite!
        self.assertTrue(snap["can_resume_pipeline"])
        self.assertTrue(snap["can_resume_video"])

        # Resuming transitions back to pipeline and stage_prompts reuses the newly rewritten H3 with 0 LLM calls!
        self.client.post("/api/config", json={"RUNNINGHUB_API_KEY": "rh-key", "persist": False}, headers=self.headers)
        with patch("shutil.which", return_value="/fake/ffmpeg"), \
             patch.object(self.jobs, "launch"):
            resumed = self.client.post("/api/jobs/hist-batch/resume", json={}, headers=self.headers)
        self.assertEqual(resumed.status_code, 200, resumed.json)
        resumed_req = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        self.assertEqual(resumed_req["operation"], "pipeline")
        planned = film.plan(script["shots"])
        pics = {s["idx"]: film.refs(script, s, "none") for s in planned}
        with patch.object(rewrite, "rewrite", side_effect=AssertionError("must hit stage_prompts cache")):
            film.stage_prompts(film.Job(out), script, planned, pics)

        # scope="all" rewrites all 3 segments
        self.jobs.records["hist-batch"].update(status="succeeded", error=None, operation="pipeline")
        with patch.object(self.jobs, "launch"):
            resp_all = self.client.post(
                "/api/jobs/hist-batch/segments/rewrite",
                json={"scope": "all"},
                headers=self.headers,
            )
        self.assertEqual(resp_all.status_code, 200, resp_all.json)
        req_all = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        all_calls = []
        with patch.object(rewrite, "rewrite", side_effect=lambda *a: all_calls.append(a) or _make_h3("all", a[1])):
            segments.rewrite_prompts(req_all)
        self.assertEqual(len(all_calls), 3)

    def test_demo_mode_rewrite_generates_valid_six_section_preview_offline(self):
        out, _ = self._create_historical_film_job("demo-rw", demo=True, with_clips=(1,))
        with patch.object(self.jobs, "launch"):
            resp = self.client.post(
                "/api/jobs/demo-rw/segments/rewrite",
                json={"scope": "all"},
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200, resp.json)
        req = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        with patch.object(rewrite, "rewrite", side_effect=AssertionError("demo must stay offline")):
            segments.rewrite_prompts(req)
        self.jobs.records["demo-rw"].update(status="succeeded", error=None, operation="rewrite_prompts")
        snap = self.client.get("/api/jobs/demo-rw").json
        for idx, card in enumerate(snap["segments"]):
            self.assertTrue(card["h3_prompt"])
            self.assertEqual(rewrite.problems(card["h3_prompt"], len(card["reference_images"])), [], f"seg {idx + 1}")

    def test_partial_batch_rewrite_failure_resumes_only_uncompleted_segments(self):
        out, _ = self._create_historical_film_job("batch-resume", with_clips=(1, 2, 3))
        self.client.post("/api/config", json={"LLM_API_KEY": "sk-test", "persist": False}, headers=self.headers)
        with patch.object(self.jobs, "launch"):
            r_start = self.client.post("/api/jobs/batch-resume/segments/rewrite", json={"scope": "all"}, headers=self.headers)
        self.assertEqual(r_start.status_code, 200, r_start.json)
        req = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))

        def flaky_rewrite(zh_text, n_images, seconds, aspect):
            if "喝茶" in zh_text:
                raise rewrite.RewriteError("transient 502 on seg 2")
            return _make_h3("ok-first-pass", n_images)

        with patch.object(rewrite, "rewrite", side_effect=flaky_rewrite):
            with self.assertRaises(rewrite.RewriteError):
                segments.rewrite_prompts(req)
        self.jobs.records["batch-resume"].update(status="failed", error="transient 502 on seg 2", operation="rewrite_prompts")

        saved_plan = json.loads((out / "inputs/prompt-rewrite.json").read_text(encoding="utf-8"))
        self.assertIn("2", saved_plan["errors"])
        already_done = set(saved_plan["completed"])
        self.assertGreaterEqual(len(already_done), 1)

        with patch.object(self.jobs, "launch"):
            res = self.client.post("/api/jobs/batch-resume/resume", json={}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        resumed_req = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        self.assertEqual(resumed_req["operation"], "rewrite_prompts")

        retried = []
        with patch.object(rewrite, "rewrite", side_effect=lambda zh, n, *a: retried.append(zh) or _make_h3("ok-resumed", n)):
            segments.rewrite_prompts(resumed_req)
        self.assertEqual(len(retried), 3 - len(already_done))
        self.assertTrue(json.loads((out / "inputs/prompt-rewrite.json").read_text(encoding="utf-8"))["applied"])


if __name__ == "__main__":
    unittest.main()
