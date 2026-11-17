import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from h3ref import config, film, frames, http, rewrite, staging


def _sample_v3():
    return {
        "version": 3,
        "camera": "高角度斜俯拍广角，对称构图",
        "scene": "西梁女国金銮宝殿内部，红柱金砖，中央摆放一张雕花长案，案上放着明黄色的通关文牒与大红婚书，背景为垂落的珠帘与宝座",
        "characters": [
            {
                "name": "女王",
                "locked_look": "头戴九凤朝阳金冠，身穿大红绣金凤袍，容貌端庄华贵",
                "placement_and_pose": "画面左侧站着，侧身面向中央长案，右手轻按婚书",
            },
            {
                "name": "唐僧",
                "locked_look": "头戴五佛宝冠，身披大红织金锦襕袈裟，内着白僧衣，面容白净",
                "placement_and_pose": "画面中央坐着，双手合十，神情克制",
            },
            {
                "name": "孙悟空",
                "locked_look": "金冠金甲，毛脸雷公嘴",
                "placement_and_pose": "画面右侧站着，手持金箍棒立于身侧",
            },
        ],
    }


def _valid_h3(detail="[Shot 1] Wide shot of the hall."):
    parts = {
        "subject_definitions": "<Subject 1> Queen from <Picture 1>.",
        "summary": "A royal hall scene.",
        "retention_analysis": "<Subject 1>: fully_preserved.",
        "detailed_description": f"A vertical 9:16 drama.\n{detail}",
        "overall_soundscape": "Soft footsteps on stone.",
        "non_diegetic_music": "N/A",
    }
    return "\n\n".join(f"{name}:\n{parts[name]}" for name in rewrite.SECTIONS)


class CompositionV3AndRetriesTest(unittest.TestCase):
    def setUp(self):
        env_patch = patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.script = {
            "aspect": "9:16",
            "look": "古装电影质感",
            "setting": "金銮宝殿",
            "cast": [
                {"name": "女王", "aliases": ["西梁女王"], "look": "大红凤袍正面立绘", "picture": "/tmp/queen.png"},
                {"name": "唐僧", "aliases": ["御弟哥哥"], "look": "袈裟正面合十", "picture": "/tmp/monk.png"},
                {"name": "孙悟空", "aliases": ["悟空"], "look": "金甲持棒正面", "picture": "/tmp/wukong.png"},
            ],
            "shots": [
                {"start": 0, "end": 4, "camera": "全景", "action": "女王轻推婚书望向唐僧"},
                {"start": 4, "end": 8, "camera": "近景", "action": "女王收回右手静静注视"},
            ],
        }

    def test_v3_validation_and_natural_chinese_rendering(self):
        comp = _sample_v3()
        comp["characters"][0]["name"] = "西梁女王"
        validated = staging.validate_composition(comp, self.script, self.script["shots"][0])
        self.assertEqual(validated["version"], 3)
        self.assertEqual(validated["characters"][0]["name"], "女王")

        rendered = staging.render_opening(validated)
        self.assertEqual(
            rendered,
            "机位与构图：高角度斜俯拍广角，对称构图。\n"
            "场景为西梁女国金銮宝殿内部，红柱金砖，中央摆放一张雕花长案，案上放着明黄色的通关文牒与大红婚书，背景为垂落的珠帘与宝座。\n"
            "画面左侧站着女王（头戴九凤朝阳金冠，身穿大红绣金凤袍，容貌端庄华贵），侧身面向中央长案，右手轻按婚书。"
            "画面中央坐着唐僧（头戴五佛宝冠，身披大红织金锦襕袈裟，内着白僧衣，面容白净），双手合十，神情克制。"
            "画面右侧站着孙悟空（金冠金甲，毛脸雷公嘴），手持金箍棒立于身侧。",
        )

        # Empty-character scenic shot is valid and produces 2 clean lines
        scenic = staging.validate_composition(
            {"version": 3, "camera": "远景航拍", "scene": "清晨薄雾笼罩的西梁城门", "characters": []},
            self.script,
            self.script["shots"][0],
        )
        self.assertEqual(
            staging.render_opening(scenic),
            "机位与构图：远景航拍。\n场景为清晨薄雾笼罩的西梁城门。",
        )

        # Missing required fields or duplicate characters reject with ValueError
        for broken in (
            {**_sample_v3(), "camera": "  "},
            {**_sample_v3(), "scene": ""},
            {**_sample_v3(), "characters": [{"name": "女王", "locked_look": "", "placement_and_pose": "画面左侧站着"}]},
            {
                **_sample_v3(),
                "characters": [
                    {"name": "女王", "locked_look": "凤袍", "placement_and_pose": "左侧站着"},
                    {"name": "西梁女王", "locked_look": "凤袍", "placement_and_pose": "右侧站着"},
                ],
            },
        ):
            with self.assertRaises(ValueError):
                staging.validate_composition(broken, self.script, self.script["shots"][0])

    def test_v3_frame_prompt_and_video_compile_prompt_are_concise_and_v2_compatible(self):
        v3 = staging.validate_composition(_sample_v3(), self.script, self.script["shots"][0])
        frame_text = frames.prompt(self.script, self.script["shots"][0], self.script["cast"], True, v3)
        self.assertIn("机位与构图：高角度斜俯拍广角，对称构图。", frame_text)
        self.assertIn("画面左侧站着女王（头戴九凤朝阳金冠，身穿大红绣金凤袍，容貌端庄华贵）", frame_text)
        self.assertNotIn("大红凤袍正面立绘", frame_text)
        self.assertNotIn("画的是这个镜头刚开始的一瞬间", frame_text)

        segs = film.plan(self.script["shots"])
        pics = film.refs(self.script, segs[0], "shot")
        v2_fixture = json.loads((Path(__file__).parent / "fixtures/staging.json").read_text(encoding="utf-8"))["transfer_start"]
        compiled_v3 = film.compile_prompt(
            self.script,
            segs[0],
            pics,
            1,
            compositions={"s1-1": v3, "s1-2": v2_fixture},
        )
        self.assertIn("动作过程：女王轻推婚书望向唐僧", compiled_v3)
        self.assertIn("头戴九凤朝阳金冠，身穿大红绣金凤袍，容貌端庄华贵", compiled_v3)
        self.assertNotIn("大红凤袍正面立绘", compiled_v3)
        # Historical v2 cached composition in s1-2 still renders via v2 branch
        self.assertIn(v2_fixture["world_frame"], compiled_v3)

    def test_programmatic_locked_look_persists_and_overrides_drift_across_shots_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            script = copy.deepcopy(self.script)
            for idx, c in enumerate(script["cast"]):
                p = out / f"c{idx}.png"
                p.write_bytes(b"img")
                c["picture"] = str(p)
            segs = film.plan(script["shots"])
            pics = {s["idx"]: film.refs(script, s, "shot") for s in segs}

            shot1_v3 = _sample_v3()
            shot2_drifted = {
                "version": 3,
                "camera": "近景平视",
                "scene": "西梁女国金銮宝殿内部，雕花长案前",
                "characters": [
                    {
                        "name": "女王",
                        "locked_look": "身穿素白便服，披散长发",  # LLM drifted outfit!
                        "placement_and_pose": "画面左侧站着，收回右手静静注视前方",
                    }
                ],
            }
            calls = []

            def fake_describe(f, shot, **kwargs):
                calls.append(kwargs)
                return shot1_v3 if len(calls) == 1 else shot2_drifted

            job = film.Job(out / "job")
            with patch.object(frames, "describe_composition", side_effect=fake_describe), \
                 patch.object(frames, "draw", side_effect=lambda t, r, a, o, **k: (o.write_bytes(b"png") or 0.1)):
                failures = film.stage_frames(job, script, segs, pics)
            self.assertEqual(failures, [])
            self.assertEqual(calls[0].get("locked_looks"), {})
            self.assertEqual(
                calls[1]["locked_looks"]["女王"],
                "头戴九凤朝阳金冠，身穿大红绣金凤袍，容貌端庄华贵",
            )
            saved_s2 = job.state["frames"]["s1-2"]
            self.assertEqual(
                saved_s2["composition"]["characters"][0]["locked_look"],
                "头戴九凤朝阳金冠，身穿大红绣金凤袍，容貌端庄华贵",
            )
            self.assertIn("头戴九凤朝阳金冠，身穿大红绣金凤袍，容貌端庄华贵", saved_s2["prompt"])
            self.assertNotIn("身穿素白便服", saved_s2["prompt"])

            # Resume after adding/rerunning shot 2 recovers locked_looks from cached shot 1
            job.state["frames"].pop("s1-2")
            (job.dir / "frames/s1-2.png").unlink()
            job.save()
            resumed_job = film.Job(out / "job")
            resume_calls = []

            def fake_resume_describe(f, shot, **kwargs):
                resume_calls.append(kwargs)
                return shot2_drifted

            with patch.object(frames, "describe_composition", side_effect=fake_resume_describe), \
                 patch.object(frames, "draw", side_effect=lambda t, r, a, o, **k: (o.write_bytes(b"png") or 0.1)):
                self.assertEqual(film.stage_frames(resumed_job, script, segs, pics), [])
            self.assertEqual(len(resume_calls), 1)
            self.assertEqual(
                resume_calls[0]["locked_looks"]["女王"],
                "头戴九凤朝阳金冠，身穿大红绣金凤袍，容貌端庄华贵",
            )
            self.assertEqual(
                resumed_job.state["frames"]["s1-2"]["composition"]["characters"][0]["locked_look"],
                "头戴九凤朝阳金冠，身穿大红绣金凤袍，容貌端庄华贵",
            )

    def test_describe_composition_retries_3_times_falls_back_and_aborts_on_fatal_http(self):
        # 3 bad outputs then 4th valid -> succeeds on attempt 4
        responses = [
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": "not json"}}]},
            {"choices": [{"message": {"content": json.dumps({"version": 3, "camera": "", "scene": "殿内", "characters": []})}}]},
            {"choices": [{"message": {"content": json.dumps(_sample_v3(), ensure_ascii=False)}}]},
        ]
        with patch.object(frames.http, "request", side_effect=responses) as req, \
             patch.object(frames.config, "llm_key", return_value="sk-test"):
            parsed = frames.describe_composition(self.script, self.script["shots"][0])
        self.assertEqual(parsed["version"], 3)
        self.assertEqual(req.call_count, 4)
        # Retry messages include feedback from prior validation error
        final_messages = req.call_args_list[3].args[2]["messages"]
        self.assertGreater(len(final_messages), 2)
        self.assertIn("camera", json.dumps(final_messages, ensure_ascii=False))

        # 4 consecutive bad outputs -> raises CompositionFormatError after exactly 4 requests
        always_empty = {"choices": [{"message": {"content": "   "}}]}
        with patch.object(frames.http, "request", return_value=always_empty) as req_fail, \
             patch.object(frames.config, "llm_key", return_value="sk-test"):
            with self.assertRaises(frames.CompositionFormatError):
                frames.describe_composition(self.script, self.script["shots"][0])
        self.assertEqual(req_fail.call_count, 4)

        # Fatal HTTP statuses (401, 402, 403, 404, 429) raise immediately without retrying
        for code in (401, 402, 403, 404, 429):
            with patch.object(frames.http, "request", side_effect=http.HTTPResponseError(code, f"HTTP {code}")) as req_err, \
                 patch.object(frames.config, "llm_key", return_value="sk-test"):
                with self.assertRaises(http.HTTPResponseError) as caught:
                    frames.describe_composition(self.script, self.script["shots"][0])
                self.assertEqual(caught.exception.status_code, code)
                self.assertEqual(req_err.call_count, 1)

        # stage_frames catches CompositionFormatError, falls back to raw prompt, draws frame, preserves previous_composition
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            script = copy.deepcopy(self.script)
            script["shots"].append({"start": 8, "end": 12, "camera": "特写", "action": "唐僧低眉合十"})
            for idx, c in enumerate(script["cast"]):
                p = out / f"c{idx}.png"
                p.write_bytes(b"img")
                c["picture"] = str(p)
            segs = film.plan(script["shots"])
            pics = {s["idx"]: film.refs(script, s, "shot") for s in segs}
            job = film.Job(out / "fallback-job")
            s1_comp = staging.validate_composition(_sample_v3(), script, script["shots"][0])
            s3_comp = copy.deepcopy(s1_comp)
            seen_prev = []

            def side_effect(f, shot, **kwargs):
                seen_prev.append(kwargs.get("previous_composition"))
                if len(seen_prev) == 1:
                    return s1_comp
                if len(seen_prev) == 2:
                    raise frames.CompositionFormatError("empty output after 4 tries")
                return s3_comp

            with patch.object(frames, "describe_composition", side_effect=side_effect), \
                 patch.object(frames, "draw", side_effect=lambda t, r, a, o, **k: (o.write_bytes(b"png") or 0.1)) as draw_mock:
                failures = film.stage_frames(job, script, segs, pics)
            self.assertEqual(failures, [])
            self.assertEqual(draw_mock.call_count, 3)
            self.assertIsNone(job.state["frames"]["s1-2"].get("composition"))
            self.assertIn("女王收回右手静静注视", job.state["frames"]["s1-2"]["prompt"])
            self.assertFalse(job.state["frames"]["s1-2"].get("error"))
            # Shot 3 still receives Shot 1's composition as previous_composition
            self.assertEqual(seen_prev[2], s1_comp)
            self.assertIn("fallback", (job.dir / "run.log").read_text(encoding="utf-8"))

            # 404 on describe_composition in stage_frames aborts immediately like 401/402/403/429
            job_404 = film.Job(out / "abort-404")
            with patch.object(frames, "describe_composition", side_effect=http.HTTPResponseError(404, "model not found")) as desc_404, \
                 patch.object(frames, "draw") as draw_404:
                with self.assertRaises(http.HTTPResponseError):
                    film.stage_frames(job_404, script, segs, pics)
            self.assertEqual(desc_404.call_count, 1)
            draw_404.assert_not_called()

    def test_optimize_storyboard_false_skips_llm_and_cli_flag_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            script = copy.deepcopy(self.script)
            for idx, c in enumerate(script["cast"]):
                p = out / f"c{idx}.png"
                p.write_bytes(b"img")
                c["picture"] = str(p)
            film_file = out / "film.json"
            film_file.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
            segs = film.plan(script["shots"])
            pics = {s["idx"]: film.refs(script, s, "shot") for s in segs}
            job = film.Job(out / "no-opt")
            with patch.object(frames, "describe_composition", side_effect=AssertionError("must not call LLM")), \
                 patch.object(frames, "draw", side_effect=lambda t, r, a, o, **k: (o.write_bytes(b"png") or 0.1)) as draw_mock:
                failures = film.stage_frames(job, script, segs, pics, optimize_storyboard=False)
            self.assertEqual(failures, [])
            self.assertEqual(draw_mock.call_count, 2)
            self.assertIn("女王轻推婚书望向唐僧", job.state["frames"]["s1-1"]["prompt"])

            # CLI --no-optimize-storyboard passes optimize_storyboard=False to film.run
            spec = importlib.util.spec_from_file_location("h3ref_cli_opt", Path(__file__).parents[1] / "h3ref.py")
            cli = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cli)
            cli_job_dir = out / "cli-no-opt"
            cli_job_dir.mkdir()
            with patch.object(film, "run", return_value=cli_job_dir / "film.mp4") as run_mock, \
                 patch.object(sys, "argv", ["h3ref.py", "film", str(cli_job_dir), "--film", str(film_file), "--no-optimize-storyboard"]):
                cli.main()
            self.assertIs(run_mock.call_args.kwargs.get("optimize_storyboard"), False)

    def test_rewrite_rejects_empty_sections_retries_6_times_and_stops_on_fatal_errors(self):
        # Empty string or empty section body fails sections/problems/is_rewritten
        self.assertTrue(rewrite.problems("", 1))
        empty_summary = _valid_h3().replace("A royal hall scene.", "   ")
        issues = rewrite.problems(empty_summary, 1)
        self.assertTrue(any("summary" in x for x in issues), issues)
        self.assertFalse(rewrite.is_rewritten(empty_summary))

        # 6 bad attempts (empty + bad picture + empty section) followed by 7th valid -> succeeds on 7th call
        attempts = [
            "",
            "subject_definitions:\nonly one",
            empty_summary,
            _valid_h3().replace("<Picture 1>", "<Picture 9>"),
            _valid_h3(detail="No Shot tag here."),
            _valid_h3().replace("Soft footsteps on stone.", "Soft footsteps with no background music."),
            _valid_h3(),
        ]
        call_inputs = []

        def fake_llm(prompt):
            call_inputs.append(prompt)
            val = attempts[len(call_inputs) - 1]
            if not val:
                raise rewrite.RewriteError("改写模型没有返回内容")
            return val

        with patch.object(rewrite, "backend", return_value="llm"), \
             patch.object(rewrite, "_llm", side_effect=fake_llm), \
             patch.object(rewrite, "guide", return_value="guide"):
            out = rewrite.rewrite("中文提示词", 1, 8, "9:16")
        self.assertEqual(len(call_inputs), 7)
        self.assertTrue(rewrite.is_rewritten(out))
        self.assertIn("=== FIX ===", call_inputs[1])
        self.assertIn("<Picture 9>", call_inputs[4])

        # 7 consecutive bad attempts -> raises RewriteError after 7 calls
        with patch.object(rewrite, "backend", return_value="llm"), \
             patch.object(rewrite, "_llm", return_value="   ") as bad_llm, \
             patch.object(rewrite, "guide", return_value="guide"):
            with self.assertRaises(rewrite.RewriteError):
                rewrite.rewrite("中文提示词", 1, 8, "9:16")
        self.assertEqual(bad_llm.call_count, 7)

        # Fatal errors (missing claude binary, HTTP 401/402/403/404) raise RewriteFatalError immediately without retry
        with patch.object(rewrite, "backend", return_value="claude"), \
             patch.object(rewrite, "claude_bin", return_value=None), \
             patch.object(rewrite, "guide", return_value="guide"):
            with self.assertRaises(rewrite.RewriteFatalError):
                rewrite.rewrite("中文提示词", 1, 8, "9:16")

        for code in (401, 402, 403, 404):
            with patch.object(rewrite, "backend", return_value="llm"), \
                 patch.object(rewrite.http, "request", side_effect=http.HTTPResponseError(code, f"HTTP {code}")) as fatal_req, \
                 patch.object(rewrite.config, "llm_key", return_value="sk-test"), \
                 patch.object(rewrite, "guide", return_value="guide"):
                with self.assertRaises(rewrite.RewriteFatalError):
                    rewrite.rewrite("中文提示词", 1, 8, "9:16")
                self.assertEqual(fatal_req.call_count, 1)


if __name__ == "__main__":
    unittest.main()
