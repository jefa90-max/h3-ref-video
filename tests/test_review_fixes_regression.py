import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from h3ref import film, http, rewrite
from workbench import segments, storyboard
from workbench.jobs import write_json
from workbench.server import create_app


def _valid_h3(tag="ok", n_images=1):
    pic = "<Picture 1>" if n_images >= 1 else "reference"
    parts = {
        "subject_definitions": f"<Subject 1> Character from {pic} ({tag}).",
        "summary": f"[reference generation] {tag}.",
        "retention_analysis": "<Subject 1>: fully_preserved.",
        "detailed_description": f"Vertical 9:16 ({tag}).\n[Shot 1] <Subject 1> stands still.",
        "overall_soundscape": "Quiet room tone.",
        "non_diegetic_music": "N/A",
    }
    return "\n\n".join(f"{name}:\n{parts[name]}" for name in rewrite.SECTIONS)


class ReviewFixesRegressionTest(unittest.TestCase):
    def setUp(self):
        env_patch = patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.actor = self.root / "actor.png"
        Image.new("RGB", (8, 8), "blue").save(self.actor)
        self.app = create_app(self.root / "storage", self.root / ".env")
        self.jobs = self.app.extensions["jobs"]
        self.addCleanup(self.jobs.close)
        self.client = self.app.test_client()
        self.headers = {"X-Workbench": "1", "Accept-Language": "zh-CN"}
        self.client.post("/api/config", json={"LLM_API_KEY": "sk-test", "RUNNINGHUB_API_KEY": "rh-test", "persist": False}, headers=self.headers)

    def _setup_two_seg_job(self, job_id):
        out = self.root / f"out-{job_id}"
        for folder in ("inputs", "frames", "clips", "prompts"):
            (out / folder).mkdir(parents=True, exist_ok=True)
        script = {
            "aspect": "9:16",
            "cast": [{"name": "阿青", "look": "青衫", "picture": str(self.actor)}],
            "shots": [
                {"start": 0, "end": 8, "action": "阿青进门"},
                {"start": 8, "end": 16, "action": "阿青坐下"},
            ],
        }
        request = {
            "kind": "film",
            "demo": False,
            "frames": "none",
            "optimize_storyboard": False,
            "lang": "zh-CN",
            "output_dir": str(out),
            "film": script,
            "operation": "pipeline",
        }
        write_json(out / "inputs/film.json", script)
        write_json(out / "inputs/request.json", request)
        review = storyboard.review_status(request, [])
        write_json(out / "inputs/review.json", {"dirty_segments": [], "confirmed_version": review["version"]})
        self.jobs.records[job_id] = {
            "id": job_id,
            "kind": "film",
            "demo": False,
            "status": "failed",
            "operation": "pipeline",
            "output_dir": str(out),
            "lang": "zh-CN",
            "created_at": "2026-11-16T00:00:00+0000",
            "error": "failed",
        }
        self.jobs.save(self.jobs.records[job_id])
        return out, script, request

    def test_clipless_failed_segment_retains_can_regenerate_after_manual_h3_rewrite(self):
        # Finding 1: seg 1 had a pre-task failure (no mp4), seg 2 has terminal_failed (no mp4).
        # After manually rewriting seg 1's H3 prompt, seg 1 MUST remain can_regenerate=True and pass regeneration_plan!
        out, _, _ = self._setup_two_seg_job("f1-job")
        fjob = film.Job(out)
        fjob.put("segs", "0", zh="阿青进门旧词", error="RewriteError: bad output")
        fjob.put("segs", "1", zh="阿青坐下", h3=_valid_h3("seg2", 1), h3_source="阿青坐下", task="dead-2", terminal_failed=True, error="OOM")

        snap0 = self.client.get("/api/jobs/f1-job").json
        self.assertTrue(snap0["segments"][0]["can_regenerate"])

        with patch.object(self.jobs, "launch"):
            r = self.client.post(
                "/api/jobs/f1-job/segments/1/rewrite",
                json={
                    "prompt": "阿青推门而入",
                    "prompt_format": "source",
                    "prompt_version": snap0["segments"][0]["prompt_version"],
                },
                headers=self.headers,
            )
        self.assertEqual(r.status_code, 200, r.json)
        req = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        with patch.object(rewrite, "rewrite", return_value=_valid_h3("seg1-fixed", 1)):
            segments.rewrite_segment(req)
        self.jobs.records["f1-job"].update(status="succeeded", error=None, operation="rewrite_segment")

        snap1 = self.client.get("/api/jobs/f1-job").json
        card1 = snap1["segments"][0]
        self.assertIsNone(card1["version"])
        self.assertTrue(card1["can_regenerate"], "Clipless segment after manual H3 rewrite must remain independently regenerable")
        # Submitting single-segment regenerate hits reuse_h3=True cleanly
        with patch("workbench.segments.shutil.which", return_value="/fake/ffmpeg"), \
             patch.object(self.jobs, "launch"):
            regen = self.client.post(
                "/api/jobs/f1-job/segments/1/regenerate",
                json={
                    "version": card1["version"],
                    "prompt_version": card1["prompt_version"],
                    "prompt": card1["source_prompt"],
                    "prompt_format": "source",
                },
                headers=self.headers,
            )
        self.assertEqual(regen.status_code, 200, regen.json)
        seg_edit = json.loads((out / "inputs/segment-edit.json").read_text(encoding="utf-8"))
        self.assertTrue(seg_edit["reuse_h3"])

    def test_missing_h3_count_matches_prepare_rewrite_prompts_for_direct_h3_and_invalid_h3(self):
        # Finding 3: direct valid H3 override is NOT counted missing; paired but malformed H3 IS counted missing
        out, script, _ = self._setup_two_seg_job("f3-job")
        (out / "clips/seg1.mp4").write_bytes(b"C1")
        (out / "clips/seg1-last.jpg").write_bytes(b"T1")
        (out / "clips/seg2.mp4").write_bytes(b"C2")
        (out / "clips/seg2-last.jpg").write_bytes(b"T2")
        planned = film.plan(script["shots"])
        fjob = film.Job(out)

        # Seg 1: direct H3 override (valid H3, no paired zh) -> must NOT count as missing!
        direct_h3 = _valid_h3("direct", 1)
        fjob.put(
            "segs", "0",
            zh="", h3=direct_h3, h3_source=None,
            prompt_override={"format": "h3", "text": direct_h3, "source_version": "v"},
            file=str(out / "clips/seg1.mp4"), cny=0.1,
        )
        seg2_zh = film.compile_prompt(script, planned[1], film.refs(script, planned[1], "none"), 2)
        fjob.put(
            "segs", "1",
            zh=seg2_zh, h3=_valid_h3("seg2-ok", 2), h3_source=seg2_zh,
            file=str(out / "clips/seg2.mp4"), cny=0.1,
        )
        self.jobs.records["f3-job"].update(status="succeeded", error=None)
        snap_clean = self.client.get("/api/jobs/f3-job").json
        self.assertEqual(snap_clean["missing_h3_count"], 0, "Direct valid H3 override must not count as missing")

        # Now Seg 2 has paired zh, but H3 references <Picture 9> (out of bounds for 2 images) -> MUST count as missing!
        broken_h3 = _valid_h3("broken", 1).replace("<Picture 1>", "<Picture 9>")
        fjob = film.Job(out)
        fjob.put(
            "segs", "1",
            zh=seg2_zh, h3=broken_h3, h3_source=seg2_zh,
            file=str(out / "clips/seg2.mp4"), cny=0.1,
        )
        snap = self.client.get("/api/jobs/f3-job").json
        self.assertEqual(snap["missing_h3_count"], 1, "Malformed paired H3 must count as missing")
        req = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        segments.prepare_rewrite_prompts(req, scope="missing")
        plan_data = json.loads((out / "inputs/prompt-rewrite.json").read_text(encoding="utf-8"))
        self.assertEqual([t["number"] for t in plan_data["targets"]], [2])

    def test_rewrite_treats_429_and_http_errors_as_fatal_without_polluting_llm_prompt(self):
        # Finding 5: HTTP 429 and 400 API errors abort immediately via RewriteFatalError instead of looping 7x into === FIX ===
        for code in (429, 400):
            with patch.object(rewrite, "backend", return_value="llm"), \
                 patch.object(rewrite.http, "request", side_effect=http.HTTPResponseError(code, f"HTTP {code}")) as req_mock, \
                 patch.object(rewrite.config, "llm_key", return_value="sk-test"), \
                 patch.object(rewrite, "guide", return_value="guide"):
                with self.assertRaises(rewrite.RewriteFatalError):
                    rewrite.rewrite("中文提示词", 1, 8, "9:16")
                self.assertEqual(req_mock.call_count, 1)

    def test_failed_manual_rewrite_preserves_draft_visibility_and_does_not_prematurely_supersede_state(self):
        # Finding 6:
        # (a) Pre-existing failed segment-edit.json must NOT overwrite state.json['segs'] during prepare_rewrite_segment
        # (b) If rewrite_segment fails, GET /api/jobs/<id> surfaces the pending rewrite text and error on the card!
        out, _, _ = self._setup_two_seg_job("f6-job")
        (out / "clips/seg1.mp4").write_bytes(b"C1")
        (out / "clips/seg1-last.jpg").write_bytes(b"T1")
        fjob = film.Job(out)
        fjob.put("segs", "0", zh="原生效中文词", h3=_valid_h3("orig", 1), h3_source="原生效中文词", file=str(out / "clips/seg1.mp4"), cny=0.1)
        fjob.put("segs", "1", zh="第二段", h3=_valid_h3("s2", 2), h3_source="第二段", file=str(out / "clips/seg2.mp4"), cny=0.1)
        # Simulate an unapplied failed video edit on seg 1
        write_json(out / "inputs/segment-edit.json", {
            "number": 1, "revision": "old-failed-vid", "version": "v", "prompt": "旧失败视频尝试词",
            "prompt_format": "source", "explicit_prompt": True, "previous": fjob.state["segs"]["0"],
            "applied": False, "error": "video failed",
        })
        self.jobs.records["f6-job"].update(status="failed", error="video failed")

        snap0 = self.client.get("/api/jobs/f6-job").json
        with patch.object(self.jobs, "launch"):
            resp = self.client.post(
                "/api/jobs/f6-job/segments/1/rewrite",
                json={
                    "prompt": "新待转化中文草稿",
                    "prompt_format": "source",
                    "prompt_version": snap0["segments"][0]["prompt_version"],
                },
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200, resp.json)
        # Before rewrite_segment runs, state.json['segs']['0']['zh'] must STILL be '原生效中文词', NOT '旧失败视频尝试词'!
        self.assertEqual(film.Job(out).state["segs"]["0"]["zh"], "原生效中文词")

        # Now rewrite_segment fails (e.g. LLM error)
        req = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        with patch.object(rewrite, "rewrite", side_effect=rewrite.RewriteFatalError("401 bad key")):
            with self.assertRaises(rewrite.RewriteFatalError):
                segments.rewrite_segment(req)
        self.jobs.records["f6-job"].update(status="failed", error="401 bad key", operation="rewrite_segment")

        # Snapshot after failure shows '新待转化中文草稿' in card['prompt'] and surfaces the rewrite error!
        snap_failed = self.client.get("/api/jobs/f6-job").json
        card1_failed = snap_failed["segments"][0]
        self.assertEqual(card1_failed["prompt"], "新待转化中文草稿")
        self.assertEqual(card1_failed["source_prompt"], "原生效中文词")
        self.assertIn("401 bad key", card1_failed["error"])


if __name__ == "__main__":
    unittest.main()
