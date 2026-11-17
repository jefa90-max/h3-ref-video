import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from h3ref import film, frames, http, media, rewrite, rh
from workbench import inputs, segments, storyboard
from workbench.i18n import InputError
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

    def _setup_two_seg_job(self, job_id, *, frames_mode="none"):
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
            "frames": frames_mode,
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

    def test_regenerate_after_manual_h3_rewrite_preserves_paired_chinese_source(self):
        out, _, _ = self._setup_two_seg_job("pair-keep-job")
        (out / "clips/seg1.mp4").write_bytes(b"C1")
        (out / "clips/seg1-last.jpg").write_bytes(b"T1")
        fjob = film.Job(out)
        fjob.put("segs", "0", zh="初始中文词", h3=_valid_h3("init", 1), h3_source="初始中文词", file=str(out / "clips/seg1.mp4"), cny=0.1)
        self.jobs.records["pair-keep-job"].update(status="succeeded", error=None)

        # User edits Chinese source prompt and converts to English H3
        snap0 = self.client.get("/api/jobs/pair-keep-job").json
        with patch.object(self.jobs, "launch"):
            r_rw = self.client.post(
                "/api/jobs/pair-keep-job/segments/1/rewrite",
                json={"prompt": "女王轻推婚书望向唐僧", "prompt_format": "source", "prompt_version": snap0["segments"][0]["prompt_version"]},
                headers=self.headers,
            )
        self.assertEqual(r_rw.status_code, 200, r_rw.json)
        converted_h3 = _valid_h3("converted-queen", 1)
        with patch.object(rewrite, "rewrite", return_value=converted_h3):
            segments.rewrite_segment(json.loads((out / "inputs/request.json").read_text(encoding="utf-8")))
        self.jobs.records["pair-keep-job"].update(status="succeeded", error=None, operation="rewrite_segment")

        # UI switches to H3 preview; even if client posts prompt_format="h3" with the unmodified paired H3,
        # backend must preserve the paired Chinese source prompt on disk instead of wiping zh to ""!
        snap1 = self.client.get("/api/jobs/pair-keep-job").json
        card1 = snap1["segments"][0]
        with patch("workbench.segments.shutil.which", return_value="/fake/ffmpeg"), patch.object(self.jobs, "launch"):
            r_regen = self.client.post(
                "/api/jobs/pair-keep-job/segments/1/regenerate",
                json={
                    "version": card1["version"],
                    "prompt_version": card1["prompt_version"],
                    "prompt": converted_h3,
                    "prompt_format": "h3",
                },
                headers=self.headers,
            )
        self.assertEqual(r_regen.status_code, 200, r_regen.json)
        req_seg = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))

        def fake_gen(staged_job, f_dict, seg, pics, *, reference_paths=None):
            cand = staged_job.dir / "clips" / "seg1.mp4"
            cand.write_bytes(b"NEW_MP4")
            (staged_job.dir / "clips" / "seg1-last.jpg").write_bytes(b"NEW_TAIL")
            staged_job.put("segs", "0", file=str(cand), cny=0.2, task="rh-ok", error=None)
            return staged_job.state["segs"]["0"]

        with patch.object(film, "gen_segment", side_effect=fake_gen), \
             patch("h3ref.media.duration", return_value=8.0):
            segments.regenerate(req_seg)
        self.jobs.records["pair-keep-job"].update(status="succeeded", error=None, operation="segment")

        saved_seg = film.Job(out).state["segs"]["0"]
        self.assertEqual(saved_seg["zh"], "女王轻推婚书望向唐僧")
        self.assertEqual((out / "prompts/seg1.zh.txt").read_text(encoding="utf-8"), "女王轻推婚书望向唐僧")
        self.assertEqual((saved_seg.get("prompt_override") or {}).get("format"), "source")
        snap_after = self.client.get("/api/jobs/pair-keep-job").json
        self.assertEqual(snap_after["segments"][0]["source_prompt"], "女王轻推婚书望向唐僧")
        self.assertFalse(snap_after["segments"][0]["source_rewrite_required"])

    def test_succeeded_regeneration_and_sibling_rewrite_reconcile_prompt_rewrite_and_terminal_state(self):
        # 1. Failed single-segment prompt rewrite must be cleared/superseded when subsequent video regeneration succeeds,
        #    and overwriting a failed single-segment rewrite via another segment's rewrite archives its draft to unapplied_edit.
        out, _, _ = self._setup_two_seg_job("supersede-rw-job")
        (out / "clips/seg1.mp4").write_bytes(b"C1")
        (out / "clips/seg1-last.jpg").write_bytes(b"T1")
        (out / "clips/seg2.mp4").write_bytes(b"C2")
        (out / "clips/seg2-last.jpg").write_bytes(b"T2")
        fjob = film.Job(out)
        fjob.put("segs", "0", zh="一段原词", h3=_valid_h3("s1", 1), h3_source="一段原词", file=str(out / "clips/seg1.mp4"), cny=0.1)
        fjob.put("segs", "1", zh="二段原词", h3=_valid_h3("s2", 2), h3_source="二段原词", file=str(out / "clips/seg2.mp4"), cny=0.1)
        self.jobs.records["supersede-rw-job"].update(status="succeeded", error=None)

        snap0 = self.client.get("/api/jobs/supersede-rw-job").json
        with patch.object(self.jobs, "launch"):
            self.client.post(
                "/api/jobs/supersede-rw-job/segments/1/rewrite",
                json={"prompt": "一段失败草稿", "prompt_format": "source", "prompt_version": snap0["segments"][0]["prompt_version"]},
                headers=self.headers,
            )
        req_rw = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        with patch.object(rewrite, "rewrite", side_effect=rewrite.RewriteFatalError("401 bad key")):
            with self.assertRaises(rewrite.RewriteFatalError):
                segments.rewrite_segment(req_rw)
        self.jobs.records["supersede-rw-job"].update(status="failed", error="401 bad key", operation="rewrite_segment")

        # Rewriting segment 2 archives segment 1's failed draft into unapplied_edit instead of losing it
        snap_fail1 = self.client.get("/api/jobs/supersede-rw-job").json
        with patch.object(self.jobs, "launch"):
            self.client.post(
                "/api/jobs/supersede-rw-job/segments/2/rewrite",
                json={"prompt": "二段新草稿", "prompt_format": "source", "prompt_version": snap_fail1["segments"][1]["prompt_version"]},
                headers=self.headers,
            )
        snap_after_s2_prep = self.client.get("/api/jobs/supersede-rw-job").json
        self.assertEqual(snap_after_s2_prep["segments"][0]["prompt"], "一段失败草稿")

        # Now fail segment 2 rewrite too, then regenerate segment 2 directly with H3 -> must NOT reactivate stale error/draft!
        req_rw2 = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        with patch.object(rewrite, "rewrite", side_effect=rewrite.RewriteFatalError("401 bad key s2")):
            with self.assertRaises(rewrite.RewriteFatalError):
                segments.rewrite_segment(req_rw2)
        self.jobs.records["supersede-rw-job"].update(status="failed", error="401 bad key s2", operation="rewrite_segment")

        snap_fail2 = self.client.get("/api/jobs/supersede-rw-job").json
        direct_h3_s2 = _valid_h3("direct-s2", 2)
        with patch("workbench.segments.shutil.which", return_value="/fake/ffmpeg"), patch.object(self.jobs, "launch"):
            r_reg = self.client.post(
                "/api/jobs/supersede-rw-job/segments/2/regenerate",
                json={
                    "version": snap_fail2["segments"][1]["version"],
                    "prompt_version": snap_fail2["segments"][1]["prompt_version"],
                    "prompt": direct_h3_s2,
                    "prompt_format": "h3",
                },
                headers=self.headers,
            )
        self.assertEqual(r_reg.status_code, 200, r_reg.json)
        req_seg2 = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))

        def fake_gen2(staged_job, f_dict, seg, pics, *, reference_paths=None):
            cand = staged_job.dir / "clips" / "seg2.mp4"
            cand.write_bytes(b"NEW_C2")
            (staged_job.dir / "clips" / "seg2-last.jpg").write_bytes(b"NEW_T2")
            staged_job.put("segs", "1", file=str(cand), cny=0.2, task="rh-s2-ok", error=None)
            return staged_job.state["segs"]["1"]

        with patch.object(film, "gen_segment", side_effect=fake_gen2), \
             patch("h3ref.media.duration", return_value=8.0):
            segments.regenerate(req_seg2)
        self.jobs.records["supersede-rw-job"].update(status="succeeded", error=None, operation="segment")

        snap_clean2 = self.client.get("/api/jobs/supersede-rw-job").json
        card2_clean = snap_clean2["segments"][1]
        self.assertIsNone(card2_clean["error"])
        self.assertEqual(card2_clean["prompt_format"], "h3")
        self.assertEqual(card2_clean["prompt"], direct_h3_s2)

        # 2. Mismatched or missing prompt-rewrite.json revision is rejected instead of silently exiting 0
        with self.assertRaises(segments.InputError):
            segments.rewrite_segment({**req_rw2, "segment_revision": "wrong-revision"})

        # 3. Clipless staged terminal_failed in segment-edit.json is preserved across rewrite_prompts (anti-double-charge)
        #    AND sibling unapplied segment-edit.json is reconciled so unfinished pipeline stays resumable!
        out_t, _, _ = self._setup_two_seg_job("term-rw-job")
        fjob_t = film.Job(out_t)
        fjob_t.put("segs", "0", zh="一段无视频", error="RewriteError")
        fjob_t.put("segs", "1", zh="二段待生成")
        staged_t = film.Job(out_t / "inputs" / "segment-revdead")
        staged_t.put("segs", "0", zh="一段无视频", h3=_valid_h3("dead", 1), h3_source="一段无视频", task="rh-dead-1", terminal_failed=True, error="Remote OOM")
        write_json(out_t / "inputs/segment-edit.json", {
            "number": 1, "revision": "revdead", "version": None, "prompt": "一段用户改后词",
            "prompt_format": "source", "explicit_prompt": True, "previous": {"zh": "一段无视频", "error": "RewriteError"},
            "applied": False, "error": "failed",
        })
        self.jobs.records["term-rw-job"].update(status="failed", operation="segment")

        with patch.object(self.jobs, "launch"):
            r_batch = self.client.post("/api/jobs/term-rw-job/segments/rewrite", json={"scope": "all"}, headers=self.headers)
        self.assertEqual(r_batch.status_code, 200, r_batch.json)
        req_batch = json.loads((out_t / "inputs/request.json").read_text(encoding="utf-8"))
        seen_zh = []
        with patch.object(rewrite, "rewrite", side_effect=lambda zh, n, *a: seen_zh.append(zh) or _valid_h3("b", n)):
            segments.rewrite_prompts(req_batch)
        self.jobs.records["term-rw-job"].update(status="succeeded", error=None, operation="rewrite_prompts")

        self.assertIn("一段用户改后词", seen_zh, "Batch rewrite must not discard unapplied source edit")
        seg1_state = film.Job(out_t).state["segs"]["0"]
        self.assertEqual(seg1_state["zh"], "一段用户改后词")
        self.assertTrue(seg1_state.get("terminal_failed"), "Clipless staged terminal_failed must survive batch rewrite")
        self.assertEqual(seg1_state.get("task"), "rh-dead-1")

    def test_inflight_sibling_task_remains_resumable_after_manual_rewrite_succeeds_or_fails(self):
        out, script, request = self._setup_two_seg_job("inflight-rw-job")
        # Add 3rd shot so we have 3 segments: seg 1 terminal_failed (no clip), seg 2 in-flight paid RH task, seg 3 editable
        script["shots"].append({"start": 16, "end": 24, "action": "阿青起身"})
        write_json(out / "inputs/film.json", script)
        write_json(out / "inputs/request.json", request)
        review = storyboard.review_status(request, [])
        write_json(out / "inputs/review.json", {"dirty_segments": [], "confirmed_version": review["version"]})
        (out / "clips/seg3.mp4").write_bytes(b"C3")
        (out / "clips/seg3-last.jpg").write_bytes(b"T3")
        fjob = film.Job(out)
        fjob.put("segs", "0", zh="一段", h3=_valid_h3("s1", 1), h3_source="一段", task="rh-dead-1", terminal_failed=True, error="OOM")
        fjob.put("segs", "1", zh="二段", h3=_valid_h3("s2", 2), h3_source="二段", task="rh-live-2", submitted=1700000000.0, phase="poll")
        fjob.put("segs", "2", zh="三段", h3=_valid_h3("s3", 2), h3_source="三段", file=str(out / "clips/seg3.mp4"), cny=0.1)
        self.jobs.records["inflight-rw-job"].update(status="interrupted", operation="pipeline")

        snap0 = self.client.get("/api/jobs/inflight-rw-job").json
        with patch.object(self.jobs, "launch"):
            r_rw = self.client.post(
                "/api/jobs/inflight-rw-job/segments/3/rewrite",
                json={"prompt": "三段新词", "prompt_format": "source", "prompt_version": snap0["segments"][2]["prompt_version"]},
                headers=self.headers,
            )
        self.assertEqual(r_rw.status_code, 200, r_rw.json)
        req_rw = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))

        # Case A: rewrite_segment FAILS -> Seg 2's paid in-flight task must STILL be resumable via pipeline without LLM key!
        with patch.object(rewrite, "rewrite", side_effect=rewrite.RewriteFatalError("402 quota")):
            with self.assertRaises(rewrite.RewriteFatalError):
                segments.rewrite_segment(req_rw)
        self.jobs.records["inflight-rw-job"].update(status="failed", error="402 quota", operation="rewrite_segment")
        snap_failed_rw = self.client.get("/api/jobs/inflight-rw-job").json
        self.assertTrue(snap_failed_rw["can_resume_pipeline"])
        self.assertTrue(snap_failed_rw["can_resume_video"])

        # Case B: rewrite_segment SUCCEEDS -> Seg 2's paid in-flight task must ALSO keep can_resume_video=True!
        with patch.object(rewrite, "rewrite", return_value=_valid_h3("s3-new", 2)):
            segments.rewrite_segment(req_rw)
        self.jobs.records["inflight-rw-job"].update(status="succeeded", error=None, operation="rewrite_segment")
        snap_ok_rw = self.client.get("/api/jobs/inflight-rw-job").json
        self.assertTrue(snap_ok_rw["can_resume_pipeline"])
        self.assertTrue(snap_ok_rw["can_resume_video"], "Succeeded manual rewrite must not disable Resume for in-flight paid sibling task")

        # Case C: submission_unknown on a sibling blocks POST /resume when can_resume_video is False on rewrite_segment
        fjob = film.Job(out)
        fjob.put("segs", "1", task=None, submission_unknown=True)
        self.jobs.records["inflight-rw-job"].update(status="failed", error="interrupted", operation="rewrite_segment")
        snap_unk = self.client.get("/api/jobs/inflight-rw-job").json
        self.assertFalse(snap_unk["can_resume_video"])
        res_blocked = self.client.post("/api/jobs/inflight-rw-job/resume", json={}, headers=self.headers)
        self.assertEqual(res_blocked.status_code, 400)

    def test_batch_rewrite_and_pipeline_resume_preserve_failed_single_rewrite_drafts(self):
        # 1. Single-segment rewrite fails (no segment-edit.json) -> missing_h3_count counts the unapplied source draft,
        #    unedited {} POST /segments/1/rewrite refuses to clobber the pending draft without prompt_version,
        #    and POST /segments/rewrite {"scope": "missing"} or {"scope": "all"} uses the new draft (not old zh)!
        out, _, _ = self._setup_two_seg_job("batch-after-single-fail")
        (out / "clips/seg1.mp4").write_bytes(b"C1")
        (out / "clips/seg1-last.jpg").write_bytes(b"T1")
        (out / "clips/seg2.mp4").write_bytes(b"C2")
        (out / "clips/seg2-last.jpg").write_bytes(b"T2")
        fjob = film.Job(out)
        fjob.put("segs", "0", zh="一段旧词", h3=_valid_h3("s1", 1), h3_source="一段旧词", file=str(out / "clips/seg1.mp4"), cny=0.1)
        fjob.put("segs", "1", zh="二段旧词", h3=_valid_h3("s2", 2), h3_source="二段旧词", file=str(out / "clips/seg2.mp4"), cny=0.1)
        self.jobs.records["batch-after-single-fail"].update(status="succeeded", error=None)

        snap0 = self.client.get("/api/jobs/batch-after-single-fail").json
        with patch.object(self.jobs, "launch"):
            r_rw = self.client.post(
                "/api/jobs/batch-after-single-fail/segments/1/rewrite",
                json={"prompt": "一段待转新草稿", "prompt_format": "source", "prompt_version": snap0["segments"][0]["prompt_version"]},
                headers=self.headers,
            )
        self.assertEqual(r_rw.status_code, 200, r_rw.json)
        with patch.object(rewrite, "rewrite", side_effect=rewrite.RewriteFatalError("401 bad key")):
            with self.assertRaises(rewrite.RewriteFatalError):
                segments.rewrite_segment(json.loads((out / "inputs/request.json").read_text(encoding="utf-8")))
        self.jobs.records["batch-after-single-fail"].update(status="failed", error="401 bad key", operation="rewrite_segment")

        snap_after_fail = self.client.get("/api/jobs/batch-after-single-fail").json
        self.assertEqual(snap_after_fail["missing_h3_count"], 1, "Unconverted single-rewrite source draft must count toward missing_h3_count")

        # Empty-body {} unedited rewrite must NOT overwrite a pending draft without prompt_version
        clobber_resp = self.client.post("/api/jobs/batch-after-single-fail/segments/1/rewrite", json={}, headers=self.headers)
        self.assertEqual(clobber_resp.status_code, 400)

        with patch.object(self.jobs, "launch"):
            r_batch = self.client.post(
                "/api/jobs/batch-after-single-fail/segments/rewrite",
                json={"scope": "missing"},
                headers=self.headers,
            )
        self.assertEqual(r_batch.status_code, 200, r_batch.json)
        batch_plan = json.loads((out / "inputs/prompt-rewrite.json").read_text(encoding="utf-8"))
        self.assertEqual([(t["number"], t["prompt"]) for t in batch_plan["targets"]], [(1, "一段待转新草稿")])
        self.assertIn("previous", batch_plan["targets"][0])
        self.jobs.records["batch-after-single-fail"].update(status="succeeded", error=None)

        # 2. Clipless segment failed single rewrite promoted to zh/prompt_override when superseded -> pipeline resumes,
        #    so film.stage_prompts rewrites the user's edited prompt!
        out_c, script_c, _ = self._setup_two_seg_job("clipless-promo-job")
        (out_c / "clips/seg1.mp4").write_bytes(b"C1")
        (out_c / "clips/seg1-last.jpg").write_bytes(b"T1")
        fjob_c = film.Job(out_c)
        fjob_c.put("segs", "0", zh="一段完成", h3=_valid_h3("s1", 1), h3_source="一段完成", file=str(out_c / "clips/seg1.mp4"), cny=0.1)
        fjob_c.put("segs", "1", zh="二段旧词")
        self.jobs.records["clipless-promo-job"].update(status="interrupted", operation="pipeline")

        snap_c0 = self.client.get("/api/jobs/clipless-promo-job").json
        with patch.object(self.jobs, "launch"):
            r_c_rw = self.client.post(
                "/api/jobs/clipless-promo-job/segments/2/rewrite",
                json={"prompt": "阿青拔剑新词", "prompt_format": "source", "prompt_version": snap_c0["segments"][1]["prompt_version"]},
                headers=self.headers,
            )
        self.assertEqual(r_c_rw.status_code, 200, r_c_rw.json)
        with patch.object(rewrite, "rewrite", side_effect=rewrite.RewriteFatalError("502 bad gateway")):
            with self.assertRaises(rewrite.RewriteFatalError):
                segments.rewrite_segment(json.loads((out_c / "inputs/request.json").read_text(encoding="utf-8")))
        self.jobs.records["clipless-promo-job"].update(status="failed", error="502 bad gateway", operation="rewrite_segment")

        # Sibling Seg 1 rewrite archives clipless Seg 2's draft into zh/prompt_override, then pipeline resume uses it!
        snap_c1 = self.client.get("/api/jobs/clipless-promo-job").json
        with patch.object(self.jobs, "launch"):
            r_s1_rw = self.client.post(
                "/api/jobs/clipless-promo-job/segments/1/rewrite",
                json={"prompt": "一段新词", "prompt_format": "source", "prompt_version": snap_c1["segments"][0]["prompt_version"]},
                headers=self.headers,
            )
        self.assertEqual(r_s1_rw.status_code, 200, r_s1_rw.json)
        with patch.object(rewrite, "rewrite", return_value=_valid_h3("s1-new", 1)):
            segments.rewrite_segment(json.loads((out_c / "inputs/request.json").read_text(encoding="utf-8")))
        self.jobs.records["clipless-promo-job"].update(status="succeeded", error=None, operation="rewrite_segment")

        self.client.post("/api/config", json={"RUNNINGHUB_API_KEY": "rh-key", "LLM_API_KEY": "sk-key", "persist": False}, headers=self.headers)
        with patch("shutil.which", return_value="/fake/ffmpeg"), patch.object(self.jobs, "launch"):
            res_c = self.client.post("/api/jobs/clipless-promo-job/resume", json={}, headers=self.headers)
        self.assertEqual(res_c.status_code, 200, res_c.json)

        planned_c = film.plan(script_c["shots"])
        pics_c = {s["idx"]: film.refs(script_c, s, "none") for s in planned_c}
        seen_c = []
        with patch.object(rewrite, "rewrite", side_effect=lambda zh, n, *a: seen_c.append(zh) or _valid_h3("s2-ok", n)):
            film.stage_prompts(film.Job(out_c), script_c, planned_c, pics_c)
        self.assertEqual(seen_c, ["阿青拔剑新词"])
        self.assertNotIn("unapplied_edit", film.Job(out_c).state["segs"]["1"])

    def test_failed_rewrite_segment_with_inflight_sibling_drains_without_paid_submission_and_scopes_unknown(self):
        # Part 1: seg1 task_pending=True, seg2 clip-backed rewrite_segment fails (applied=False), seg3 clipless.
        # Clicking Resume must drain seg1 without requiring LLM_API_KEY and MUST NOT submit new paid clips for seg3!
        out, script, request = self._setup_two_seg_job("drain-no-submit")
        script["shots"].append({"start": 16, "end": 24, "action": "阿青远望"})
        write_json(out / "inputs/film.json", script)
        write_json(out / "inputs/request.json", request)
        review = storyboard.review_status(request, [])
        write_json(out / "inputs/review.json", {"dirty_segments": [], "confirmed_version": review["version"]})
        (out / "clips/seg2.mp4").write_bytes(b"C2")
        (out / "clips/seg2-last.jpg").write_bytes(b"T2")
        fjob = film.Job(out)
        fjob.put("segs", "0", zh="一段", h3=_valid_h3("s1", 1), h3_source="一段", task="rh-live-1", submitted=1700000000.0, phase="poll")
        fjob.put("segs", "1", zh="二段", h3=_valid_h3("s2", 2), h3_source="二段", file=str(out / "clips/seg2.mp4"), cny=0.1)
        fjob.put("segs", "2", zh="三段")
        self.jobs.records["drain-no-submit"].update(status="interrupted", operation="pipeline")

        self.client.post("/api/config", json={"LLM_API_KEY": "sk-temp", "persist": False}, headers=self.headers)
        snap0 = self.client.get("/api/jobs/drain-no-submit").json
        with patch.object(self.jobs, "launch"):
            r_rw = self.client.post(
                "/api/jobs/drain-no-submit/segments/2/rewrite",
                json={"prompt": "二段未完成改写词", "prompt_format": "source", "prompt_version": snap0["segments"][1]["prompt_version"]},
                headers=self.headers,
            )
        self.assertEqual(r_rw.status_code, 200, r_rw.json)
        with patch.object(rewrite, "rewrite", side_effect=rewrite.RewriteFatalError("402 quota")):
            with self.assertRaises(rewrite.RewriteFatalError):
                segments.rewrite_segment(json.loads((out / "inputs/request.json").read_text(encoding="utf-8")))
        self.jobs.records["drain-no-submit"].update(status="failed", error="402 quota", operation="rewrite_segment")

        # Clear LLM_API_KEY, keep ONLY RUNNINGHUB_API_KEY -> Resume must pass preflight and drain ONLY seg 1!
        self.client.post("/api/config", json={"LLM_API_KEY": "", "RUNNINGHUB_API_KEY": "rh-only", "persist": False}, headers=self.headers)
        with patch("shutil.which", return_value="/fake/ffmpeg"), patch.object(self.jobs, "launch"):
            res_drain = self.client.post("/api/jobs/drain-no-submit/resume", json={}, headers=self.headers)
        self.assertEqual(res_drain.status_code, 200, res_drain.json)

        def fake_download(url, dest):
            Path(dest).write_bytes(b"SEG1-MP4")

        with patch.object(rh, "poll", return_value={"state": "done", "url": "https://example.com/seg1.mp4", "cny": 0.1, "coins": 10, "run_seconds": 5.0}), \
             patch.object(http, "download", side_effect=fake_download), \
             patch.object(media, "last_frame", side_effect=lambda src, dst: Path(dst).write_bytes(b"T1")), \
             patch.object(rh, "submit_with_retry", side_effect=AssertionError("must not submit new paid clip for seg 3")), \
             patch.object(rewrite, "rewrite", side_effect=AssertionError("must not call rewrite while draining")):
            with self.assertRaises(RuntimeError):
                film.run(out, out / "inputs/film.json", mode="none", optimize_storyboard=False)
        self.assertTrue((out / "clips/seg1.mp4").is_file(), "In-flight seg 1 must finish draining")
        self.jobs.records["drain-no-submit"].update(status="succeeded", error=None)

        # Part 2: seg1 submission_unknown=True in state.json; seg2 rewrite_segment fails (applied=False) -> can_resume_video=True!
        out_u, _, _ = self._setup_two_seg_job("unk-sibling-rw")
        (out_u / "clips/seg2.mp4").write_bytes(b"C2")
        (out_u / "clips/seg2-last.jpg").write_bytes(b"T2")
        fjob_u = film.Job(out_u)
        fjob_u.put("segs", "0", zh="一段", h3=_valid_h3("s1", 1), h3_source="一段", task=None, submission_unknown=True)
        fjob_u.put("segs", "1", zh="二段", h3=_valid_h3("s2", 2), h3_source="二段", file=str(out_u / "clips/seg2.mp4"), cny=0.1)
        self.jobs.records["unk-sibling-rw"].update(status="failed", error="submission_unknown", operation="pipeline")
        self.client.post("/api/config", json={"LLM_API_KEY": "sk-ok", "persist": False}, headers=self.headers)
        snap_u0 = self.client.get("/api/jobs/unk-sibling-rw").json
        with patch.object(self.jobs, "launch"):
            r_u_rw = self.client.post(
                "/api/jobs/unk-sibling-rw/segments/2/rewrite",
                json={"prompt": "二段独立改写词", "prompt_format": "source", "prompt_version": snap_u0["segments"][1]["prompt_version"]},
                headers=self.headers,
            )
        self.assertEqual(r_u_rw.status_code, 200, r_u_rw.json)
        with patch.object(rewrite, "rewrite", side_effect=rewrite.RewriteError("502 gateway")):
            with self.assertRaises(rewrite.RewriteError):
                segments.rewrite_segment(json.loads((out_u / "inputs/request.json").read_text(encoding="utf-8")))
        self.jobs.records["unk-sibling-rw"].update(status="failed", error="502 gateway", operation="rewrite_segment")

        snap_u_fail = self.client.get("/api/jobs/unk-sibling-rw").json
        self.assertTrue(snap_u_fail["can_resume_video"], "Unapplied rewrite_segment on seg 2 must stay resumable despite seg 1 submission_unknown")
        with patch.object(self.jobs, "launch"):
            res_u = self.client.post("/api/jobs/unk-sibling-rw/resume", json={}, headers=self.headers)
        self.assertEqual(res_u.status_code, 200, res_u.json)
        self.assertEqual(json.loads((out_u / "inputs/request.json").read_text(encoding="utf-8"))["operation"], "rewrite_segment")
        self.jobs.records["unk-sibling-rw"].update(status="succeeded", error=None)

    def test_archived_single_rewrite_survives_applied_segment_edit_and_same_segment_unapplied_edit(self):
        # Part 1: Previously regenerated segment has segment-edit.json with applied=True, publishing=False, revision='R1'.
        # A subsequent failed single rewrite archived into state.json[idx].unapplied_edit must NOT be clobbered by read_context!
        out, _, _ = self._setup_two_seg_job("applied-seg-clobber")
        (out / "clips/seg1.mp4").write_bytes(b"C1")
        (out / "clips/seg1-last.jpg").write_bytes(b"T1")
        (out / "clips/seg2.mp4").write_bytes(b"C2")
        (out / "clips/seg2-last.jpg").write_bytes(b"T2")
        active_seg0 = {
            "zh": "一段旧生效词",
            "h3": _valid_h3("s1", 1),
            "h3_source": "一段旧生效词",
            "file": str(out / "clips/seg1.mp4"),
            "cny": 0.1,
            "revision": "rev-applied-1",
        }
        fjob = film.Job(out)
        fjob.state["segs"]["0"] = dict(active_seg0)
        fjob.state["segs"]["1"] = {"zh": "二段词", "h3": _valid_h3("s2", 2), "h3_source": "二段词", "file": str(out / "clips/seg2.mp4"), "cny": 0.1}
        fjob.save()
        write_json(out / "inputs/segment-edit.json", {
            "number": 1,
            "revision": "rev-applied-1",
            "applied": True,
            "publishing": False,
            "active": dict(active_seg0),
        })
        self.client.post("/api/config", json={"LLM_API_KEY": "sk-test", "persist": False}, headers=self.headers)
        snap0 = self.client.get("/api/jobs/applied-seg-clobber").json
        with patch.object(self.jobs, "launch"):
            r1 = self.client.post(
                "/api/jobs/applied-seg-clobber/segments/1/rewrite",
                json={"prompt": "一段归档新草稿", "prompt_format": "source", "prompt_version": snap0["segments"][0]["prompt_version"]},
                headers=self.headers,
            )
        self.assertEqual(r1.status_code, 200, r1.json)
        with patch.object(rewrite, "rewrite", side_effect=rewrite.RewriteError("502")):
            with self.assertRaises(rewrite.RewriteError):
                segments.rewrite_segment(json.loads((out / "inputs/request.json").read_text(encoding="utf-8")))
        self.jobs.records["applied-seg-clobber"].update(status="failed", error="502", operation="rewrite_segment")

        # Trigger Seg 2 rewrite -> archives Seg 1's failed rewrite into state.json["segs"]["0"]["unapplied_edit"]
        snap1 = self.client.get("/api/jobs/applied-seg-clobber").json
        with patch.object(self.jobs, "launch"):
            r2 = self.client.post(
                "/api/jobs/applied-seg-clobber/segments/2/rewrite",
                json={"prompt": "二段新词", "prompt_format": "source", "prompt_version": snap1["segments"][1]["prompt_version"]},
                headers=self.headers,
            )
        self.assertEqual(r2.status_code, 200, r2.json)
        self.jobs.records["applied-seg-clobber"].update(status="succeeded", error=None)
        snap2 = self.client.get("/api/jobs/applied-seg-clobber").json
        self.assertEqual(snap2["segments"][0]["prompt"], "一段归档新草稿", "Archived unapplied_edit must not be overwritten by applied segment-edit.json")

        # Part 2: Same-segment unapplied segment-edit.json superseded by single rewrite -> clear_all must NOT resurrect stale segment-edit prompt!
        write_json(out / "inputs/segment-edit.json", {
            "number": 1,
            "revision": "rev-unapplied-old",
            "applied": False,
            "prompt": "过期的重生成草稿",
            "prompt_format": "source",
            "previous": dict(fjob.Job(out).state["segs"]["0"]) if hasattr(fjob, "Job") else dict(film.Job(out).state["segs"]["0"]),
        })
        snap_p2 = self.client.get("/api/jobs/applied-seg-clobber").json
        with patch.object(self.jobs, "launch"):
            r_p2 = self.client.post(
                "/api/jobs/applied-seg-clobber/segments/1/rewrite",
                json={"prompt": "更新后的单段改写草稿", "prompt_format": "source", "prompt_version": snap_p2["segments"][0]["prompt_version"]},
                headers=self.headers,
            )
        self.assertEqual(r_p2.status_code, 200, r_p2.json)
        req_p2 = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        segments.reconcile_pending_prompt_rewrite(out, req_p2, archive_single_except=0, clear_all=True)
        self.jobs.records["applied-seg-clobber"].update(status="succeeded", error=None)
        snap_p2_after = self.client.get("/api/jobs/applied-seg-clobber").json
        self.assertEqual(snap_p2_after["segments"][0]["prompt"], "更新后的单段改写草稿")

    def test_edit_frame_then_continue_film_promotes_archived_unapplied_source_edit_on_redo_segments(self):
        out, script, _ = self._setup_two_seg_job("frame-edit-promo", frames_mode="shot")
        Image.new("RGB", (8, 8), "green").save(out / "frames/s1-1.png")
        Image.new("RGB", (8, 8), "green").save(out / "frames/s2-1.png")
        (out / "clips/seg1.mp4").write_bytes(b"C1")
        (out / "clips/seg1-last.jpg").write_bytes(b"T1")
        (out / "clips/seg2.mp4").write_bytes(b"C2")
        (out / "clips/seg2-last.jpg").write_bytes(b"T2")
        fjob = film.Job(out)
        fjob.put("segs", "0", zh="一段原词", h3=_valid_h3("s1", 2), h3_source="一段原词", file=str(out / "clips/seg1.mp4"), cny=0.1)
        fjob.put("segs", "1", zh="二段原词", h3=_valid_h3("s2", 2), h3_source="二段原词", file=str(out / "clips/seg2.mp4"), cny=0.1)
        req_cur = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        st_cards = storyboard.view(req_cur, self.jobs.artifacts({"id": "frame-edit-promo", "output_dir": str(out)}), True, "frame-edit-promo")
        rev = storyboard.review_status(req_cur, st_cards)
        write_json(out / "inputs/review.json", {"dirty_segments": [], "confirmed_version": rev["version"]})

        self.client.post("/api/config", json={"LLM_API_KEY": "sk-test", "APIMART_API_KEY": "api-test", "RUNNINGHUB_API_KEY": "rh-test", "persist": False}, headers=self.headers)
        snap0 = self.client.get("/api/jobs/frame-edit-promo").json
        with patch.object(self.jobs, "launch"):
            r_rw = self.client.post(
                "/api/jobs/frame-edit-promo/segments/1/rewrite",
                json={"prompt": "修帧前手改的一段源词", "prompt_format": "source", "prompt_version": snap0["segments"][0]["prompt_version"]},
                headers=self.headers,
            )
        self.assertEqual(r_rw.status_code, 200, r_rw.json)
        self.jobs.records["frame-edit-promo"].update(status="failed", error="502", operation="rewrite_segment")

        # Edit frame s1-1 -> archives Seg 1's failed rewrite into unapplied_edit and marks Seg 1 dirty
        with patch.object(self.jobs, "launch"):
            r_fe = self.client.post(
                "/api/jobs/frame-edit-promo/frames/s1-1",
                json={"prompt": "新分镜画面词"},
                headers=self.headers,
            )
        self.assertEqual(r_fe.status_code, 200, r_fe.json)
        fe_data = json.loads((out / "inputs/frame-edit.json").read_text(encoding="utf-8"))
        fe_data["applied"] = True
        write_json(out / "inputs/frame-edit.json", fe_data)
        write_json(out / "inputs/review.json", {"dirty_segments": [1], "confirmed_version": None})
        self.jobs.records["frame-edit-promo"].update(status="succeeded", error=None, operation="frame")

        snap_review = self.client.get("/api/jobs/frame-edit-promo").json
        with patch("shutil.which", return_value="/fake/ffmpeg"), patch.object(self.jobs, "launch"):
            r_cont = self.client.post(
                "/api/jobs/frame-edit-promo/continue",
                json={"approved": True, "review_version": snap_review["review"]["version"]},
                headers=self.headers,
            )
        self.assertEqual(r_cont.status_code, 200, r_cont.json)

        cont_req = json.loads((out / "inputs/request.json").read_text(encoding="utf-8"))
        run_job = film.Job(out)
        film.invalidate_segments(run_job, cont_req["frames"], cont_req["redo"])
        planned = film.plan(script["shots"])
        pics = {s["idx"]: film.refs(script, s, "shot") for s in planned}
        rewritten = []
        with patch.object(rewrite, "rewrite", side_effect=lambda zh, n, *a: rewritten.append(zh) or _valid_h3("s1-cont", n)):
            film.stage_prompts(run_job, script, planned, pics)
        self.assertEqual(rewritten, ["修帧前手改的一段源词"])

    def test_batch_rewrite_uses_updated_automatic_source_without_fabricating_override_and_preflight_guards_h3(self):
        # Part 1: Clipless/invalidated un-overridden segment with stale compiler rec['zh'] uses current automatic['source'] without fake override
        out, script, req = self._setup_two_seg_job("stale-auto-zh")
        (out / "clips/seg2.mp4").write_bytes(b"C2")
        (out / "clips/seg2-last.jpg").write_bytes(b"T2")
        fjob = film.Job(out)
        fjob.put("segs", "0", zh="旧版自动编译词", h3=_valid_h3("old1", 1), h3_source="旧版自动编译词")
        planned = film.plan(script["shots"])
        auto2 = film.compile_prompt(script, planned[1], film.refs(script, planned[1], "none"), len(planned))
        fjob.put("segs", "1", zh=auto2, h3=_valid_h3("ok2", 2), h3_source=auto2, file=str(out / "clips/seg2.mp4"), cny=0.1)
        (out / "prompts/seg1.zh.txt").write_text("旧版自动编译词", encoding="utf-8")
        (out / "prompts/seg1.h3.txt").write_text(_valid_h3("old1", 1), encoding="utf-8")
        (out / "prompts/seg2.zh.txt").write_text(auto2, encoding="utf-8")
        (out / "prompts/seg2.h3.txt").write_text(_valid_h3("ok2", 2), encoding="utf-8")

        self.client.post("/api/config", json={"LLM_API_KEY": "sk-test", "persist": False}, headers=self.headers)
        snap = self.client.get("/api/jobs/stale-auto-zh").json
        self.assertEqual(snap["missing_h3_count"], 1, "Un-overridden clipless segment whose zh lags automatic_source_prompt must count as missing_h3")
        with patch.object(self.jobs, "launch"):
            r_batch = self.client.post("/api/jobs/stale-auto-zh/segments/rewrite", json={"scope": "missing"}, headers=self.headers)
        self.assertEqual(r_batch.status_code, 200, r_batch.json)
        with patch.object(rewrite, "rewrite", side_effect=lambda zh, n, *a: _valid_h3("fresh", n)):
            segments.rewrite_prompts(json.loads((out / "inputs/request.json").read_text(encoding="utf-8")))
        self.jobs.records["stale-auto-zh"].update(status="succeeded", error=None)
        seg0_after = film.Job(out).state["segs"]["0"]
        self.assertNotIn("prompt_override", seg0_after, "Batch rewrite of un-overridden segment must not fabricate prompt_override")
        self.assertEqual(seg0_after["zh"], snap["segments"][0]["automatic_source_prompt"])

        # Part 2: preserve_paired_source is False in h3 mode when reference_version changed
        (out / "clips/seg1.mp4").write_bytes(b"C1")
        (out / "clips/seg1-last.jpg").write_bytes(b"T1")
        fjob2 = film.Job(out)
        fjob2.state["segs"]["0"]["file"] = str(out / "clips/seg1.mp4")
        fjob2.state["segs"]["0"]["cny"] = 0.1
        fjob2.state["segs"]["0"]["manual_rewrite"] = True
        fjob2.state["segs"]["0"]["h3_reference_version"] = "stale-ref-ver"
        fjob2.save()
        snap_ref = self.client.get("/api/jobs/stale-auto-zh").json
        card0 = snap_ref["segments"][0]
        plan_h3 = segments.regeneration_plan(
            req,
            1,
            card0["version"],
            prompt=card0["h3_prompt"],
            prompt_format="h3",
            prompt_version=card0["prompt_version"],
        )
        self.assertFalse(plan_h3["preserve_paired_source"], "Stale reference_version must disable preserve_paired_source in h3 format")

        # Part 3: inputs.preflight rejects malformed cached paired H3 when LLM_API_KEY is missing
        out_pf, script_pf, req_pf = self._setup_two_seg_job("pf-malformed-h3")
        planned_pf = film.plan(script_pf["shots"])
        zh_pf = film.compile_prompt(script_pf, planned_pf[0], film.refs(script_pf, planned_pf[0], "none"), len(planned_pf))
        bad_h3 = _valid_h3("bad-pic", 1).replace("<Picture 1>", "<Picture 9>")
        film.Job(out_pf).put("segs", "0", zh=zh_pf, h3=bad_h3, h3_source=zh_pf)
        with patch("shutil.which", return_value="/fake/ffmpeg"):
            with self.assertRaises(InputError) as pf_err:
                inputs.preflight({**req_pf, "operation": "pipeline"}, {"RUNNINGHUB_API_KEY": "rh-only"})
        self.assertEqual(pf_err.exception.key, "keys")

    def test_h3_wrapper_validation_execution_parity_unclosed_think_and_linear_fence_perf(self):
        # 1. Outer text outside code fence or before subject_definitions is rejected
        body = _valid_h3("strict", 1)
        suffixed = f"<think>draft</think>\n```text\n{body}\n```\nExtra <Picture 99>"
        prefixed = f"Use <Picture 99>\n{body}"
        self.assertFalse(rewrite.is_rewritten(suffixed))
        self.assertTrue(rewrite.problems(suffixed, 1))
        self.assertFalse(rewrite.is_rewritten(prefixed))
        self.assertTrue(rewrite.problems(prefixed, 1))

        # 2. Unclosed <think> (truncated reasoning) is rejected in both rewrite and frames
        unclosed_h3 = f"<think>\n{body}"
        self.assertFalse(rewrite.is_rewritten(unclosed_h3))
        self.assertTrue(rewrite.problems(unclosed_h3, 1))
        unclosed_comp = {"choices": [{"message": {"content": '<think>\n```json\n{"version":3,"camera":"中景","scene":"大厅","characters":[]}\n```'}}]}
        with patch.object(frames.http, "request", return_value=unclosed_comp), \
             patch.object(frames.config, "llm_key", return_value="k"):
            with self.assertRaises(frames.CompositionFormatError):
                frames.describe_composition({"shots": [{"action": "a"}]}, {"action": "a"}, max_retries=0)

        # 3. Valid closed <think> + outer fence in direct H3 regeneration strips wrapper before persistence & RunningHub
        out, _, req = self._setup_two_seg_job("h3-strip-exec")
        (out / "clips/seg1.mp4").write_bytes(b"C1")
        (out / "clips/seg1-last.jpg").write_bytes(b"T1")
        film.Job(out).put("segs", "0", zh="原词", h3=body, h3_source="原词", file=str(out / "clips/seg1.mp4"), cny=0.1)
        card = self.client.get("/api/jobs/h3-strip-exec").json["segments"][0]
        wrapped_direct = f"<think>Use <Picture 999> with subtitles.</think>\n```text\n{body}\n```"
        rev = segments.prepare_regeneration(
            req, 1, card["version"], prompt=wrapped_direct, prompt_format="h3", prompt_version=card["prompt_version"]
        )
        req_seg = {**req, "operation": "segment", "segment": 1, "segment_revision": rev}
        submitted_graphs = []
        with patch("workbench.segments.shutil.which", return_value="/fake/ffmpeg"), \
             patch.object(rh, "upload", return_value="up.png"), \
             patch.object(rh, "submit_with_retry", side_effect=lambda g, **kw: submitted_graphs.append(g) or "t-clean"), \
             patch.object(rh, "poll", return_value={"state": "done", "url": "https://v/1.mp4", "cny": 0.1}), \
             patch.object(film.http, "download", side_effect=lambda u, p: p.write_bytes(b"NEW")), \
             patch.object(media, "last_frame", side_effect=lambda p, o: o.write_bytes(b"TAIL")), \
             patch.object(media, "duration", return_value=5):
            segments.regenerate(req_seg)
        sent_prompt = submitted_graphs[0][rh.PROMPT]["inputs"]["prompt"]
        self.assertNotIn("<think>", sent_prompt)
        self.assertNotIn("```", sent_prompt)
        self.assertNotIn("<Picture 999>", sent_prompt)

        # 4. cmd_one rejects out-of-bounds <Picture 99> in 6-section input and strips closed <think> on valid input
        spec = importlib.util.spec_from_file_location("h3ref_cli_one", Path(__file__).parents[1] / "h3ref.py")
        cli_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli_mod)
        img_one = self.root / "one.png"
        Image.new("RGB", (8, 8)).save(img_one)
        bad_p = self.root / "bad_h3.txt"
        bad_p.write_text(body.replace("<Picture 1>", "<Picture 99>"), encoding="utf-8")
        with self.assertRaises(rewrite.RewriteError):
            cli_mod.cmd_one(SimpleNamespace(out=str(self.root / "one-bad"), image=[str(img_one)], prompt=str(bad_p), seed=1, seconds=5, aspect="9:16", check=False))
        wrapped_p = self.root / "valid_wrapped_h3.txt"
        wrapped_p.write_text(f"<think>draft <Picture 99></think>\n```text\n{body}\n```", encoding="utf-8")
        out_one_ok = self.root / "one-ok"
        with patch.object(cli_mod.rh, "upload", side_effect=RuntimeError("stop-after-h3-save")):
            with self.assertRaisesRegex(RuntimeError, "stop-after-h3-save"):
                cli_mod.cmd_one(SimpleNamespace(out=str(out_one_ok), image=[str(img_one)], prompt=str(wrapped_p), seed=1, seconds=5, aspect="9:16", check=False))
        saved_one_h3 = (out_one_ok / "prompt.h3.txt").read_text(encoding="utf-8")
        self.assertNotIn("<think>", saved_one_h3)
        self.assertNotIn("```", saved_one_h3)
        self.assertNotIn("<Picture 99>", saved_one_h3)

        # 5. Linear-time O(N) fence handling on unclosed fences + whitespace runs (no O(N^3)/O(N^2) hang)
        t0 = time.perf_counter()
        bad_fence = {"choices": [{"message": {"content": "```json\n" + (" " * 5000) + '{"version":3'}}]}
        with patch.object(frames.http, "request", return_value=bad_fence), \
             patch.object(frames.config, "llm_key", return_value="k"):
            with self.assertRaises(frames.CompositionFormatError):
                frames.describe_composition({"shots": [{"action": "a"}]}, {"action": "a"}, max_retries=0)
        self.assertLess(time.perf_counter() - t0, 2.0)

        t1 = time.perf_counter()
        self.assertFalse(rewrite.is_rewritten("```\n" + (" " * 80000) + "tail"))
        self.assertFalse(rewrite.is_rewritten("subject_definitions:\n" + (" " * 80000) + "x"))
        self.assertLess(time.perf_counter() - t1, 2.0)

        # 6. rewrite.problems() parses sections() once, and source_rewrite_required catches missing [Shot 1]
        with patch.object(rewrite, "sections", wraps=rewrite.sections) as sec_spy:
            self.assertEqual(rewrite.problems(body, 1), [])
        self.assertEqual(sec_spy.call_count, 1)
        no_shot_h3 = body.replace("[Shot 1] ", "")
        self.assertTrue(
            segments.source_rewrite_required(
                {"zh": "词", "h3": no_shot_h3, "h3_source": "词"},
                {"source": "词", "h3": no_shot_h3, "paired": True},
                None,
                n_pictures=1,
            )
        )

    def test_preserve_paired_source_not_wiped_by_preview_reconciled_segments(self):
        out, script, req = self._setup_two_seg_job("preserve-zh")
        (out / "clips/seg1.mp4").write_bytes(b"C1")
        (out / "clips/seg1-last.jpg").write_bytes(b"T1")
        planned = film.plan(script["shots"])
        auto_zh = film.compile_prompt(script, planned[0], film.refs(script, planned[0], "none"), len(planned))
        valid_h3 = _valid_h3("paired", 1)
        film.Job(out).put(
            "segs", "0", zh=auto_zh, h3=valid_h3, h3_source=auto_zh,
            file=str(out / "clips/seg1.mp4"), cny=0.1, manual_rewrite=True,
        )
        card = self.client.get("/api/jobs/preserve-zh").json["segments"][0]
        rev = segments.prepare_regeneration(
            req, 1, card["version"], prompt=valid_h3, prompt_format="h3", prompt_version=card["prompt_version"]
        )
        seg_edit = json.loads((out / "inputs/segment-edit.json").read_text(encoding="utf-8"))
        self.assertTrue(seg_edit["preserve_paired_source"])
        # Simulate supersede=True reconciliation before staged state has prompt_override (or on clipless failure)
        reconciled, _, _ = segments.preview_reconciled_segments(out, keep_pending=False, supersede=True)
        self.assertEqual(reconciled["0"]["zh"], auto_zh, "Paired Chinese source must not be wiped to ''")
        self.assertNotEqual((reconciled["0"].get("prompt_override") or {}).get("format"), "h3")

    def test_pass1_ship_review_regressions_parity_clipless_regenerate_and_unicode_think(self):
        from workbench import inputs as wb_inputs

        # 1. red-team#1: 9 cast sheets + board + group-word shot ('众人') in frames='shot' mode
        # film.refs drops board and omits frame slots (9 refs total), so workbench inputs.validate must succeed (CLI parity)
        nine_sheets = []
        for idx in range(9):
            p = self.root / f"sheet_{idx}.png"
            Image.new("RGB", (8, 8), "green").save(p)
            nine_sheets.append(p)
        board_p = self.root / "board_9.png"
        Image.new("RGB", (8, 8), "blue").save(board_p)
        nine_cast = [
            {"name": f"角色{idx * 2 + 1}", "picture": str(nine_sheets[idx])}
            for idx in range(9)
        ] + [
            {"name": f"角色{idx * 2 + 2}", "picture": str(nine_sheets[idx])}
            for idx in range(9)
        ]
        validated = wb_inputs.validate(
            {
                "kind": "film",
                "demo": False,
                "frames": "shot",
                "output_root": str(self.root),
                "film": {
                    "board": str(board_p),
                    "cast": nine_cast,
                    "shots": [{"start": 0, "end": 6, "action": "众人在议事厅集合商议"}],
                },
            },
            self.root,
        )
        self.assertEqual(validated["frames"], "shot")

        # 2. red-team#2: Clipless segment with saved stage_prompts H3 (no manual_rewrite, no error) AND clean clipless direct H3 edit
        out, script, req = self._setup_two_seg_job("clipless-stage-prompts")
        planned = film.plan(script["shots"])
        zh0 = film.compile_prompt(script, planned[0], film.refs(script, planned[0], "none"), len(planned))
        h3_0 = _valid_h3("stage_prompts_seg1", 1)
        film.Job(out).put("segs", "0", zh=zh0, h3=h3_0, h3_source=zh0)
        snap = self.client.get("/api/jobs/clipless-stage-prompts").json
        card1, card2 = snap["segments"][0], snap["segments"][1]
        self.assertTrue(card1["can_regenerate"], "Segment with saved stage_prompts H3 must be regenerable without redundant rewrite")
        self.assertTrue(card2["can_regenerate"], "Editable clipless segment must allow direct regeneration without stranding H3 drafts")
        plan1 = segments.regeneration_plan(
            req, 1, card1["version"], prompt=card1["prompt"], prompt_format="source", prompt_version=card1["prompt_version"]
        )
        self.assertTrue(plan1["reuse_h3"], "Saved stage_prompts H3 must be reused without extra LLM rewrite cost")
        direct_h3_2 = _valid_h3("direct_seg2", 1)
        plan2 = segments.regeneration_plan(
            req, 2, card2["version"], prompt=direct_h3_2, prompt_format="h3", prompt_version=card2["prompt_version"]
        )
        self.assertEqual(plan2["prompt_format"], "h3")

        # 3. red-team#4: Unicode casefold expansion (U+0130 'İ' -> 'i\u0307' in .lower()) inside <think>...</think>
        valid_body = _valid_h3("unicode_istanbul", 1)
        wrapped_h3 = f"<THINK>İstanbul İİİİİ reasoning block</ThInK>\n{valid_body}"
        stripped = rewrite.strip_h3_wrappers(wrapped_h3)
        self.assertTrue(stripped.startswith("subject_definitions:\n"), f"Truncated header: {stripped[:40]!r}")
        self.assertEqual(rewrite.problems(stripped, 1), [])

        comp_reply = {
            "choices": [
                {
                    "message": {
                        "content": (
                            "<THINK>İstanbul İİİİİ camera check</think>"
                            '{"version":3,"camera":"中景平视","scene":"室内木桌",'
                            '"characters":[{"name":"阿青","locked_look":"青衫束发","placement_and_pose":"微笑站立在桌前"}]}'
                        )
                    }
                }
            ]
        }
        with patch.object(frames.http, "request", return_value=comp_reply), \
             patch.object(frames.config, "llm_key", return_value="k"):
            comp = frames.describe_composition(script, script["shots"][0], max_retries=0)
        self.assertEqual(comp["version"], 3)


if __name__ == "__main__":
    unittest.main()
