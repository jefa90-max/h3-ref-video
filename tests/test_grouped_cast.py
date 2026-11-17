import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from h3ref import config, film, frames, rewrite
from workbench import demo, inputs, references, segments, storyboard
from workbench.i18n import InputError
from workbench.server import create_app


def _make_png_bytes(color="blue"):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    buf.seek(0)
    return buf


class GroupedCastTest(unittest.TestCase):
    def setUp(self):
        env_patch = patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True)
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

        # 2. inputs.validate rejects shot where board (1) + 9 unique cast pictures = 10 > 9 even in mode="shot"
        board_path = self.root / "b.png"
        Image.new("RGB", (8, 8), "white").save(board_path)
        nine_cast = [{"name": f"人{i}", "picture": str(ten_paths[i])} for i in range(9)]
        over_shot_payload = {
            "kind": "film",
            "demo": False,
            "frames": "shot",
            "film": {
                "aspect": "9:16",
                "board": str(board_path),
                "cast": nine_cast,
                "shots": [{"start": 0, "end": 5, "action": "众人站立"}],
            },
        }
        with self.assertRaises(InputError):
            inputs.validate(over_shot_payload, self.root)

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
        finally:
            jobs.close()


if __name__ == "__main__":
    unittest.main()
