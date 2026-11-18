import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from h3ref import film, frames, rewrite
from workbench import demo, inputs, references, storyboard
from workbench.i18n import InputError
from workbench.server import create_app


def _make_png_bytes(color="blue"):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    buf.seek(0)
    return buf


class GroupedCastTest(unittest.TestCase):
    def setUp(self):
        base_env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "SystemRoot") if k in os.environ}
        env_patch = patch.dict(os.environ, base_env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_group_cast_by_picture_and_anti_swap_absent_prompts(self):
        p_group = str(self.root / "group_trio.png")
        p_solo = str(self.root / "queen.png")
        cast = [
            {"name": "女王", "look": "凤冠红袍", "picture": p_solo},
            {"name": "唐僧", "look": "五佛冠锦襕袈裟", "picture": p_group},
            {"name": "孙悟空", "look": "金冠金甲毛脸", "picture": p_group},
            {"name": "猪八戒", "look": "黑僧衣长嘴大耳", "picture": p_group},
        ]
        script = {
            "aspect": "9:16",
            "look": "古装电影感",
            "board": str(self.root / "board.png"),
            "cast": cast,
            "shots": [
                {"start": 0, "end": 5, "camera": "全景", "action": "女王与唐僧、孙悟空在殿内交谈"},
            ],
        }
        who = [c for c in cast if film.in_shots(c, script["shots"])]
        groups = film.group_cast_by_picture(who, cast)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["path"], p_solo)
        self.assertEqual([c["name"] for c in groups[0]["active"]], ["女王"])
        self.assertEqual(groups[0]["absent"], [])
        self.assertEqual(groups[1]["path"], p_group)
        self.assertEqual([c["name"] for c in groups[1]["active"]], ["唐僧", "孙悟空"])
        self.assertEqual([c["name"] for c in groups[1]["absent"]], ["猪八戒"])
        self.assertEqual(groups[1]["name"], "唐僧 / 孙悟空 / 猪八戒")

        # Frame prompt: board is 参考图片1, solo 女王 is 参考图片2 (unchanged single-role wording), trio sheet is 参考图片3
        f_prompt = frames.prompt(script, script["shots"][0], who, True, None)
        self.assertIn("参考图片2是女王：凤冠红袍。", f_prompt)
        self.assertIn("参考图片3是多角色合图", f_prompt)
        self.assertIn("唐僧（五佛冠锦襕袈裟）", f_prompt)
        self.assertIn("孙悟空（金冠金甲毛脸）", f_prompt)
        self.assertIn("未出场的猪八戒", f_prompt)
        self.assertIn("严禁将未出场角色画进画面", f_prompt)
        self.assertNotIn("参考图片4", f_prompt)

        # Video segment prompt: deduplicated refs, anti-swap + absent warning, accurate active character count (3 roles)
        segs = film.plan(script["shots"])
        seg_refs = film.refs(script, segs[0], "shot")
        # board (1) + solo (1) + group (1) + shot frame (1) = 4 pictures total
        self.assertEqual(len(seg_refs), 4)
        v_prompt = film.compile_prompt(script, segs[0], seg_refs, 1)
        self.assertIn("参考图片2只约束女王身份：凤冠红袍。", v_prompt)
        self.assertIn("参考图片3是多角色合图", v_prompt)
        self.assertIn("唐僧（五佛冠锦襕袈裟）", v_prompt)
        self.assertIn("孙悟空（金冠金甲毛脸）", v_prompt)
        self.assertIn("未出场的猪八戒", v_prompt)
        self.assertIn("只有这3名角色", v_prompt)

        # rewrite.RULES documents multi-Subject mapping from a single <Picture N> and omitting absent characters
        self.assertIn("多角色合图", rewrite.RULES)
        self.assertIn("absent", rewrite.RULES.lower())

    def test_twelve_characters_grouped_into_four_sheets_pass_full_pipeline_and_demo(self):
        uploads = self.root / "uploads"
        uploads.mkdir()
        sheet_ids = []
        for idx in range(4):
            name = f"sheet{idx}.png"
            Image.new("RGB", (8, 8), "green").save(uploads / name)
            sheet_ids.append(name)
        Image.new("RGB", (8, 8), "white").save(uploads / "board.png")

        cast = [{"name": f"角色{i + 1}", "look": f"服饰{i + 1}"} for i in range(12)]
        raw_script = {
            "aspect": "9:16",
            "board": True,
            "cast": cast,
            "shots": [
                {"start": 0, "end": 5, "action": "众人齐聚大殿"},
                {"start": 5, "end": 10, "action": "角色1与角色4对话"},
            ],
        }
        bindings = {"board": "board.png"}
        groups = []
        for i in range(12):
            bindings[f"cast:{i}"] = sheet_ids[i // 3]
        for g in range(4):
            groups.append([f"cast:{g * 3 + k}" for k in range(3)])

        bound = references.bind_references(
            {
                "kind": "film",
                "demo": False,
                "frames": "shot",
                "optimize_storyboard": False,
                "film": raw_script,
                "reference_bindings": bindings,
                "reference_groups": groups,
            },
            uploads,
        )
        validated = inputs.validate(bound, self.root)
        self.assertIs(validated["optimize_storyboard"], False)
        # optimize_storyboard=False with operation=frames needs APIMART_API_KEY only, no LLM key
        inputs.preflight({**validated, "operation": "frames"}, {"APIMART_API_KEY": "ak-test"})

        # Run stage_frames on 12 characters -> each shot sends board + at most 4 group images = 5 <= 9
        out_dir = self.root / "twelve-job"
        job = film.Job(out_dir)
        segs = film.plan(validated["film"]["shots"])
        pics = {s["idx"]: film.refs(validated["film"], s, "shot") for s in segs}
        submitted_refs = []

        def fake_draw(text, refs_list, aspect, output, **kwargs):
            submitted_refs.append([str(p) for p in refs_list])
            output.write_bytes(b"png")
            return 0.1

        with patch.object(frames, "draw", side_effect=fake_draw):
            failures = film.stage_frames(job, validated["film"], segs, pics, optimize_storyboard=False)
        self.assertEqual(failures, [])
        self.assertEqual(len(submitted_refs[0]), 5)  # board + 4 sheets for "众人"
        self.assertEqual(len(submitted_refs[1]), 3)  # board + sheet0 + sheet1 for 角色1 & 角色4

        # Demo mode with reference_groups and demo_placeholders=True shares placeholder paths across grouped roles
        demo_bound = references.bind_references(
            {
                "kind": "film",
                "demo": True,
                "frames": "shot",
                "operation": "frames",
                "film": raw_script,
                "reference_bindings": {},
                "reference_groups": groups,
                "demo_placeholders": True,
            },
            uploads,
        )
        demo_validated = inputs.validate(demo_bound, self.root)
        demo_out = self.root / "demo-twelve"
        (demo_out / "inputs").mkdir(parents=True)
        demo_req = {**demo_validated, "output_dir": str(demo_out), "lang": "zh-CN", "operation": "frames"}
        demo.run(demo_req)
        demo_saved = json.loads((demo_out / "inputs" / "demo-film.json").read_text(encoding="utf-8"))
        unique_demo_cast_pics = {c["picture"] for c in demo_saved["cast"]}
        self.assertEqual(len(unique_demo_cast_pics), 4)

    def test_nine_image_limit_enforced_across_validate_edit_frame_submit_and_refs(self):
        # 1. frames.submit rejects > 9 refs before upload or before_submit
        ten_paths = [self.root / f"r{i}.png" for i in range(10)]
        for p in ten_paths:
            Image.new("RGB", (8, 8), "red").save(p)
        flag = {"called": False}
        with self.assertRaises(frames.SubmissionRejected):
            frames.submit("prompt", ten_paths, "9:16", before_submit=lambda: flag.update(called=True))
        self.assertFalse(flag["called"])

        # 2. inputs.validate matches film.refs: in mode="first" board (1) + 9 cast + frame (1) = 11 > 9 rejects;
        # in mode="shot" when s1-1 IS scheduled (1 active cast in segment) and saved_frames binds 9 cast + board = 10 > 9, rejects;
        # whereas in mode="shot" with 9 cast + board + group-word shot (film.refs drops board & omits frames -> 9 <= 9), succeeds!
        board_path = self.root / "b.png"
        Image.new("RGB", (8, 8), "white").save(board_path)
        nine_cast = [{"name": f"人{i}", "picture": str(ten_paths[i])} for i in range(9)]
        over_first_payload = {
            "kind": "film",
            "demo": False,
            "frames": "first",
            "film": {
                "aspect": "9:16",
                "board": str(board_path),
                "cast": nine_cast,
                "shots": [{"start": 0, "end": 5, "action": "众人站立"}],
            },
        }
        with self.assertRaises(InputError):
            inputs.validate(over_first_payload, self.root)
        saved_over_dir = self.root / "saved-over"
        saved_over_dir.mkdir(exist_ok=True)
        (saved_over_dir / "state.json").write_text(
            json.dumps({"frames": {"s1-1": {"character_reference_paths": [str(ten_paths[i]) for i in range(9)]}}}),
            encoding="utf-8",
        )
        with self.assertRaises(InputError):
            inputs.validate(
                {
                    "kind": "film",
                    "demo": False,
                    "frames": "shot",
                    "output_dir": str(saved_over_dir),
                    "film": {
                        "aspect": "9:16",
                        "board": str(board_path),
                        "cast": nine_cast,
                        "shots": [{"start": 0, "end": 5, "action": "人0独自站立"}],
                    },
                },
                self.root,
            )

        # 3. jobs.edit_frame rejects selecting 9 unique cast sheets when board is present (1 + 9 = 10 > 9)
        app = create_app(self.root / "storage", self.root / ".env")
        client = app.test_client()
        jobs = app.extensions["jobs"]
        headers = {"X-Workbench": "1"}
        try:
            client.post("/api/config", json={"APIMART_API_KEY": "ak-test", "persist": False}, headers=headers)
            uploaded_cast = []
            for i in range(9):
                res = client.post("/api/uploads", data={"images": (_make_png_bytes(), f"c{i}.png")}, headers=headers)
                uploaded_cast.append(res.json["files"][0])
            up_board = client.post("/api/uploads", data={"images": (_make_png_bytes("yellow"), "board.png")}, headers=headers).json["files"][0]

            # Shot 1 only mentions 人0 & 人1 (who share uploaded_cast[0]), while 人2..人9 use uploaded_cast[1..8]
            # Total unique cast sheets in catalog = 9; Shot 1 initially uses board + uploaded_cast[0] = 2 <= 9
            script = {
                "aspect": "9:16",
                "board": True,
                "cast": [
                    {"name": "甲"},
                    {"name": "乙"},
                    *[{"name": f"路人{k}"} for k in range(8)],
                ],
                "shots": [{"start": 0, "end": 5, "action": "甲与乙交谈"}],
            }
            bindings = {
                "board": up_board["id"],
                "cast:0": uploaded_cast[0]["id"],
                "cast:1": uploaded_cast[0]["id"],
            }
            for k in range(8):
                bindings[f"cast:{k + 2}"] = uploaded_cast[k + 1]["id"]

            with patch.object(jobs, "launch"):
                created = client.post(
                    "/api/jobs",
                    json={
                        "kind": "film",
                        "demo": False,
                        "frames": "shot",
                        "optimize_storyboard": False,
                        "film": script,
                        "reference_bindings": bindings,
                        "output_dir": str(self.root / "job-ui"),
                    },
                    headers=headers,
                )
            self.assertEqual(created.status_code, 201, created.json)
            jid = created.json["id"]
            out = Path(created.json["output_dir"])
            (out / "frames").mkdir(exist_ok=True)
            Image.new("RGB", (8, 8), "gray").save(out / "frames" / "s1-1.png")
            jobs.find(jid)["status"] = "succeeded"
            jobs.save(jobs.find(jid))

            snap = client.get(f"/api/jobs/{jid}").json
            # Character picker deduplicated 10 cast into 9 unique picture entries, first named "甲 / 乙"
            self.assertEqual(len(snap["character_references"]), 9)
            self.assertEqual(snap["character_references"][0]["name"], "甲 / 乙")
            # Storyboard s1-1 has board + 1 grouped character ref ("甲 / 乙")
            self.assertEqual(len(snap["storyboard"][0]["references"]), 2)
            self.assertEqual(snap["storyboard"][0]["references"][1]["name"], "甲 / 乙")
            ref_resp = client.get(snap["storyboard"][0]["references"][1]["url"])
            self.assertEqual(ref_resp.status_code, 200)
            ref_resp.close()

            # Video segment reference list also deduplicates and displays "甲 / 乙"
            cast_ref = next(r for r in snap["segments"][0]["reference_images"] if r["kind"] == "cast")
            self.assertEqual(cast_ref["name"], "甲 / 乙")

            # Editing s1-1 to attach all 9 cast sheets + 1 board = 10 > 9 -> rejected with 400
            all_nine_paths = [c["path"] for c in snap["character_references"]]
            over_resp = client.post(
                f"/api/jobs/{jid}/frames/s1-1",
                json={"prompt": "修改后的分镜词", "character_reference_paths": all_nine_paths},
                headers=headers,
            )
            self.assertEqual(over_resp.status_code, 400)
            self.assertIn("9", over_resp.json["error"])

            # 4. Unhashable reference_bindings value alongside reference_groups returns 400 InputError (not HTTP 500 TypeError)
            bad_group_resp = client.post(
                "/api/jobs",
                json={
                    "kind": "film",
                    "demo": True,
                    "frames": "shot",
                    "film": {"aspect": "9:16", "cast": [{"name": "A"}, {"name": "B"}], "shots": [{"action": "A talks"}]},
                    "reference_bindings": {"cast:0": ["unhashable-list"]},
                    "reference_groups": [["cast:0", "cast:1"]],
                },
                headers=headers,
            )
            self.assertEqual(bad_group_resp.status_code, 400)

            # 5. Null/empty edit fields on POST /segments/1/rewrite return 400 instead of silently falling back to unedited
            review = storyboard.review_status(
                json.loads((out / "inputs/request.json").read_text(encoding="utf-8")),
                storyboard.view(json.loads((out / "inputs/request.json").read_text(encoding="utf-8")), jobs.artifacts(jobs.find(jid)), False),
            )
            from workbench.jobs import write_json
            write_json(out / "inputs/review.json", {"dirty_segments": [], "confirmed_version": review["version"]})
            client.post("/api/config", json={"LLM_API_KEY": "sk-test", "persist": False}, headers=headers)
            null_rw = client.post(
                f"/api/jobs/{jid}/segments/1/rewrite",
                json={"prompt": None, "prompt_format": None, "prompt_version": None},
                headers=headers,
            )
            self.assertEqual(null_rw.status_code, 400)
            empty_fmt_rw = client.post(
                f"/api/jobs/{jid}/segments/1/rewrite",
                json={"prompt": "新词", "prompt_format": "", "prompt_version": "v"},
                headers=headers,
            )
            self.assertEqual(empty_fmt_rw.status_code, 400)
        finally:
            jobs.close()

        # 6. Jobs.resume() on an interrupted pipeline job respects saved state.json frame character_reference_paths
        #    (a legacy/repaired job whose shot had 9 cast + board in film.json but was trimmed in state.json['frames'] must resume!)
        app2 = create_app(self.root / "storage2", self.root / ".env2")
        jobs2 = app2.extensions["jobs"]
        client2 = app2.test_client()
        try:
            client2.post("/api/config", json={"RUNNINGHUB_API_KEY": "rh-test", "LLM_API_KEY": "sk-test", "persist": False}, headers=headers)
            out_r = self.root / "resume-repaired-job"
            for d in ("inputs", "frames", "clips", "prompts"):
                (out_r / d).mkdir(parents=True, exist_ok=True)
            nine_roles = [{"name": f"角{i}", "picture": str(ten_paths[i])} for i in range(9)]
            script_r = {
                "aspect": "9:16",
                "board": str(board_path),
                "cast": nine_roles,
                "shots": [{"start": 0, "end": 5, "action": "众人站立"}],
            }
            Image.new("RGB", (8, 8), "gray").save(out_r / "frames/s1-1.png")
            fjob_r = film.Job(out_r)
            fjob_r.put("frames", "s1-1", prompt="p", file=str(out_r / "frames/s1-1.png"), character_reference_paths=[str(ten_paths[0])])
            fjob_r.put("segs", "0", zh="词", h3="subject_definitions:\nA\n\nsummary:\nB\n\nretention_analysis:\nC\n\ndetailed_description:\n[Shot 1] D\n\noverall_soundscape:\nE\n\nnon_diegetic_music:\nN/A", h3_source="词", task="rh-inflight-legacy", submitted=1700000000.0, phase="poll")
            req_r = {
                "kind": "film", "demo": False, "frames": "shot", "optimize_storyboard": False,
                "lang": "zh-CN", "output_dir": str(out_r), "film": script_r, "operation": "pipeline",
            }
            from workbench.jobs import write_json
            write_json(out_r / "inputs/film.json", script_r)
            write_json(out_r / "inputs/request.json", req_r)
            desc_r = storyboard.view(req_r, jobs2.artifacts({"id": "leg-r", "output_dir": str(out_r)}), False)
            rev_r = storyboard.review_status(req_r, desc_r)
            write_json(out_r / "inputs/review.json", {"dirty_segments": [], "confirmed_version": rev_r["version"]})
            jobs2.records["leg-r"] = {
                "id": "leg-r", "kind": "film", "demo": False, "status": "interrupted", "operation": "pipeline",
                "output_dir": str(out_r), "lang": "zh-CN", "created_at": "2026-11-16T00:00:00+0000", "error": "interrupted",
            }
            jobs2.save(jobs2.records["leg-r"])
            with patch("shutil.which", return_value="/fake/ffmpeg"), patch.object(jobs2, "launch"):
                res_r = client2.post("/api/jobs/leg-r/resume", json={}, headers=headers)
            self.assertEqual(res_r.status_code, 200, res_r.json)
        finally:
            jobs2.close()

    def test_reference_groups_negative_guards_and_symlink_path_canonicalization(self):
        uploads = self.root / "u"
        uploads.mkdir()
        for name in ("a.png", "b.png"):
            Image.new("RGB", (8, 8), "red").save(uploads / name)
        base_film = {"aspect": "9:16", "board": True, "cast": [{"name": "A"}, {"name": "B"}, {"name": "C"}], "shots": [{"action": "A"}]}
        for bad_groups, bad_bindings in (
            ([["cast:0"]], {}),
            ([["board", "cast:0"]], {}),
            ([["cast:99", "cast:0"]], {}),
            ([["cast:0", "cast:1"], ["cast:1", "cast:2"]], {}),
            ([["cast:0", "cast:1"]], {"cast:0": "a.png", "cast:1": "b.png"}),
        ):
            with self.assertRaises(InputError):
                references.bind_references(
                    {"kind": "film", "demo": True, "demo_placeholders": True, "film": base_film,
                     "reference_bindings": dict(bad_bindings), "reference_groups": bad_groups},
                    uploads,
                )
        with self.assertRaises(InputError):
            inputs.validate({"kind": "film", "demo": True, "optimize_storyboard": "false", "film": {"aspect": "9:16", "shots": [{"action": "A"}]}}, self.root)

        # Non-canonical / symlinked paths resolve identically in film.load, deduplicate_paths, and group_cast_by_picture
        real_dir = self.root / "real_refs"
        real_dir.mkdir()
        sheet = real_dir / "sheet.png"
        Image.new("RGB", (8, 8), "green").save(sheet)
        alias_dir = self.root / "alias_refs"
        alias_dir.symlink_to(real_dir)
        non_canon_a = str(alias_dir / "sheet.png")
        non_canon_b = str(real_dir / ".." / "real_refs" / "sheet.png")
        self.assertEqual(len(film.deduplicate_paths([non_canon_a, non_canon_b])), 1)
        grouped = film.group_cast_by_picture(
            [{"name": "甲", "picture": non_canon_a}, {"name": "乙", "picture": non_canon_b}]
        )
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["name"], "甲 / 乙")
        fjson = self.root / "canon_film.json"
        fjson.write_text(json.dumps({
            "aspect": "9:16",
            "cast": [{"name": "甲", "picture": non_canon_a}],
            "shots": [{"action": "甲站立"}],
        }, ensure_ascii=False), encoding="utf-8")
        loaded = film.load(fjson)
        self.assertEqual(loaded["cast"][0]["picture"], str(sheet.resolve()))

    # Generated by /ship coverage audit
    # Value: protects=jobs.command, storyboard.run, and workbench.segments CLI wire rewrite ops and no-optimize-storyboard; fails_when=worker CLI drops rewrite_segment/rewrite_prompts dispatch or omits optimize_storyboard=False flag; why_new=prior workbench tests patched jobs.launch and invoked Python helpers in-process without CLI dispatch; seam=none; no_claim=live cloud LLM or video API execution (verifies worker CLI subprocess and mocked stage_frames)
    def test_worker_command_storyboard_run_and_segments_cli_wire_rewrite_and_no_optimize(self):
        import subprocess
        from h3ref import config
        from workbench import jobs as jobs_mod, segments as segments_mod
        from workbench.jobs import write_json

        out = self.root / "worker-cli-job"
        for folder in ("inputs", "frames", "clips", "prompts"):
            (out / folder).mkdir(parents=True, exist_ok=True)
        actor = self.root / "cli-actor.png"
        Image.new("RGB", (8, 8), "cyan").save(actor)
        script = {
            "aspect": "9:16",
            "cast": [{"name": "阿青", "picture": str(actor)}],
            "shots": [{"start": 0, "end": 6, "action": "阿青转身"}],
        }
        write_json(out / "inputs/film.json", script)
        write_json(out / "inputs/demo-film.json", script)

        # 1. jobs.command routes rewrite_segment/rewrite_prompts to workbench.segments and passes --no-optimize-storyboard to h3ref.py
        for op in ("rewrite_segment", "rewrite_prompts"):
            cmd = jobs_mod.command({"output_dir": str(out), "operation": op, "kind": "film", "demo": False}, config.ROOT)
            self.assertEqual(cmd[2:4], ["-m", "workbench.segments"])
        live_no_opt = jobs_mod.command(
            {"output_dir": str(out), "operation": "pipeline", "kind": "film", "demo": False, "frames": "shot", "optimize_storyboard": False},
            config.ROOT,
        )
        self.assertIn("--no-optimize-storyboard", live_no_opt)
        live_opt = jobs_mod.command(
            {"output_dir": str(out), "operation": "pipeline", "kind": "film", "demo": False, "frames": "shot", "optimize_storyboard": True},
            config.ROOT,
        )
        self.assertNotIn("--no-optimize-storyboard", live_opt)

        # 2. storyboard.run forwards optimize_storyboard=False in both operation="frames" and operation="pipeline"
        frames_req = {
            "kind": "film", "demo": False, "frames": "shot", "optimize_storyboard": False,
            "lang": "zh-CN", "output_dir": str(out), "film": script, "operation": "frames",
        }
        write_json(out / "inputs/request.json", frames_req)
        with patch.object(frames, "describe_composition", side_effect=AssertionError("must not call composition LLM")), \
             patch.object(frames, "draw", side_effect=lambda t, r, a, o, **k: (o.write_bytes(b"png") or 0.1)):
            storyboard.run(frames_req)
        self.assertIs(film.Job(out).state.get("optimize_storyboard"), False)

        pipe_req = {**frames_req, "operation": "pipeline", "reset_applied": True}
        with patch.object(film, "run", return_value=out / "film.mp4") as film_run_mock:
            storyboard.run(pipe_req)
        self.assertIs(film_run_mock.call_args.kwargs.get("optimize_storyboard"), False)

        # 3. Real `python -m workbench.segments <request.json>` subprocess dispatches rewrite_prompts cleanly offline
        demo_req = {
            "kind": "film", "demo": True, "frames": "shot", "optimize_storyboard": False,
            "lang": "zh-CN", "output_dir": str(out), "film": script, "operation": "rewrite_prompts", "rewrite_scope": "all",
        }
        demo_req["segment_revision"] = segments_mod.prepare_rewrite_prompts(demo_req, scope="all")
        req_file = out / "inputs/request.json"
        write_json(req_file, demo_req)
        proc = subprocess.run(
            jobs_mod.command(demo_req, config.ROOT),
            cwd=config.ROOT,
            env={
                **os.environ,
                "HOME": str(self.root),
                "USERPROFILE": str(self.root),
                "H3REF_CACHE": str(self.root / "cache"),
                "H3_SKIP_DOTENV": "1",
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        plan_after = json.loads((out / "inputs/prompt-rewrite.json").read_text(encoding="utf-8"))
        self.assertTrue(plan_after["applied"])
        h3_preview = (out / "prompts/seg1.h3.txt").read_text(encoding="utf-8")
        self.assertEqual(rewrite.problems(h3_preview, 2), [])


if __name__ == "__main__":
    unittest.main()
