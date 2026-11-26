"""Replays of the legacy prompt, staging and timestamp failures found in review."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from h3ref import film, rewrite, staging
from workbench import inputs, segments


# Source saved by the pre-wearable compiler (37b7410) for the two-shot fixture below.
LEGACY_SOURCE = """根据下面文字分镜生成一段10秒竖屏视频，9:16。没有原视频、截图或原音频，画面从这些文字和参考图来。
参考图片1只约束阿宁身份：短发，戴黑框眼镜。
只有这一名角色，保持各自发型、五官、服饰纹样与比例。
分镜：
0–5秒：阿宁戴着黑框眼镜坐下。
5–10秒：阿宁摘下眼镜。
完整表现动作的原因、接触和结果，动作真实连续，不要静态立绘晃动；角色不互换外观，道具不凭空出现或消失，不增加额外人物。画风全片统一为上面写的风格；每个角色全片是同一位演员，长相、发型、妆容和服装前后一致；画面里不出现字幕和文字，包装和道具上不印可读的字，只用图案和颜色区分；不出现任何真实品牌的标志。
声音：这一段没有台词，角色不说话；只有动作音效和环境声；不要背景音乐、旁白、唱歌、字幕和水印。情绪只用表情和肢体表现，无说话口型。"""
LEGACY_H3 = rewrite.assemble({
    "subject_definitions": "Person shown in <Picture 1>.",
    "summary": "A person sits and removes their glasses.",
    "retention_analysis": "Stable identity and wardrobe.",
    "detailed_description": "[Shot 1] A person sits. [Shot 2] They remove their glasses.",
    "overall_soundscape": "Soft room tone.",
})


class LegacyPromptRecovery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name).resolve()
        (self.output / "inputs").mkdir()
        self.picture = self.output / "character.png"
        self.picture.write_bytes(b"original reference")
        self.script = {
            "aspect": "9:16",
            "cast": [{"name": "阿宁", "look": "短发，戴黑框眼镜", "picture": str(self.picture)}],
            "shots": [
                {"action": "阿宁戴着黑框眼镜坐下。", "start": 0, "end": 5},
                {"action": "阿宁摘下眼镜。", "start": 5, "end": 10},
            ],
        }
        self.planned = film.plan(self.script["shots"])
        self.pictures = {0: film.refs(self.script, self.planned[0], "none")}
        self.job = film.Job(self.output)
        self.job.put("segs", "0", zh=LEGACY_SOURCE, h3_source=LEGACY_SOURCE, h3=LEGACY_H3,
                     h3_reference_version=film.reference_version([self.picture]))
        self.request = {"kind": "film", "demo": False, "frames": "none", "lang": "en",
                        "output_dir": str(self.output), "film": self.script, "operation": "pipeline"}

    def test_resume_reuses_historical_pair_without_rewriting(self):
        with patch.object(rewrite, "rewrite", return_value="would be charged again") as paid_rewrite:
            film.stage_prompts(self.job, self.script, self.planned, self.pictures)
        self.assertEqual(paid_rewrite.call_count, 0)
        self.assertEqual((self.output / "prompts" / "seg1.zh.txt").read_text(encoding="utf-8"), LEGACY_SOURCE)
        self.assertEqual(film.Job(self.output).state["segs"]["0"]["h3"], LEGACY_H3)

    def test_workbench_preflight_and_missing_scope_respect_historical_cache(self):
        with patch("shutil.which", return_value="ffmpeg"):
            inputs.preflight(self.request, {"RUNNINGHUB_API_KEY": "offline"})
        card, = segments.view(self.request, [], True)
        self.assertFalse(card["h3_missing"])
        segments.prepare_rewrite_prompts(self.request, scope="missing")
        plan = self.output / "inputs" / "prompt-rewrite.json"
        self.assertEqual(json.loads(plan.read_text(encoding="utf-8"))["targets"], [])
        segments.prepare_rewrite_prompts(self.request, scope="all")
        target, = json.loads(plan.read_text(encoding="utf-8"))["targets"]
        self.assertIn("参考图片1只约束阿宁身份：短发。", target["prompt"])

    def test_changed_reference_invalidates_legacy_pair(self):
        self.picture.write_bytes(b"new reference with a different size")
        with patch.object(rewrite, "rewrite", return_value="new H3 prompt") as paid_rewrite:
            film.stage_prompts(self.job, self.script, self.planned, self.pictures)
        self.assertEqual(paid_rewrite.call_count, 1)
        self.assertIn("参考图片1只约束阿宁身份：短发。", paid_rewrite.call_args.args[0])
        self.assertEqual(film.Job(self.output).state["segs"]["0"]["h3"], "new H3 prompt")


class StagingAndTimestampRecovery(unittest.TestCase):
    def test_wearable_removal_keeps_subject_when_pronoun_mentions_bystander(self):
        script = {"cast": [{"name": "阿宁"}, {"name": "小李"}]}
        cases = {
            "阿宁坐在桌边。他转向小李，摘下眼镜。": {"阿宁": {"眼镜"}},
            "阿宁坐在桌边，他转向小李，摘下眼镜。": {"阿宁": {"眼镜"}},
            "阿宁坐在桌边。他转向小李，小李摘下眼镜。": {"小李": {"眼镜"}},
            "阿宁坐在桌边。他转向小李，随后小李摘下眼镜。": {"小李": {"眼镜"}},
            "阿宁和小李对视。他转向小李，摘下眼镜。": {},
        }
        for action, expected in cases.items():
            with self.subTest(action=action):
                self.assertEqual(staging.changing_wearables({**script, "shots": [{"action": action}]}), expected)

    def test_removed_glasses_leave_legacy_locked_look_when_followed_by_description(self):
        self.assertEqual(staging.stable_locked_look("短发，眼镜挂在鼻梁上，灰衬衣", {"眼镜"}), "短发，灰衬衣")
        self.assertEqual(staging.stable_locked_look("短发，眼镜戴在鼻梁上，灰衬衣", {"眼镜"}), "短发，灰衬衣")
        self.assertEqual(staging.stable_locked_look("短发，戴着眼镜微笑，灰衬衣", {"眼镜"}), "短发，灰衬衣")
        self.assertEqual(staging.stable_locked_look("短发，眼镜盒在包里，灰衬衣", {"眼镜"}), "短发，眼镜盒在包里，灰衬衣")

    def test_fixed_appearance_adjacent_to_wearable_stays_locked(self):
        cases = {
            "短发戴黑框眼镜，灰衬衣": "短发，灰衬衣",
            "身穿灰衬衣戴着黑框眼镜": "身穿灰衬衣",
            "戴着黑框眼镜身穿灰衬衣": "身穿灰衬衣",
        }
        for look, expected in cases.items():
            with self.subTest(look=look):
                self.assertEqual(staging.stable_locked_look(look, {"眼镜"}), expected)

    def test_compound_wearables_follow_their_own_opening_states(self):
        opening = {"action": "阿宁戴着黑框眼镜，站在窗前。"}
        script = {"cast": [{"name": "阿宁"}], "shots": [opening, {"action": "阿宁摘下眼镜。阿宁摘下帽子。"}]}
        composition = {"version": 3, "camera": "中景", "scene": "客厅", "characters": [
            {"name": "阿宁", "locked_look": "短发，戴着黑框眼镜并戴着灰帽子", "placement_and_pose": "站在窗前"},
        ]}
        scoped = staging.scope_wearable_look(composition, script, opening, staging.changing_wearables(script))
        character, = scoped["characters"]
        self.assertEqual(character["locked_look"], "短发")
        self.assertEqual(character["placement_and_pose"], "戴着黑框眼镜，站在窗前")

    def test_single_out_of_range_timestamp_uses_same_default_as_preflight(self):
        for raw_time in ({"start": 3601}, {"end": 3601}):
            with self.subTest(raw_time=raw_time):
                script = {"shots": [{"action": "起身", **raw_time}]}
                self.assertEqual(film.inspect_dialogue_budget(script)["meta"]["default_duration_shots"], 1)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "film.json"
                    path.write_text(json.dumps(script), encoding="utf-8")
                    shot, = film.load(path)["shots"]
                self.assertEqual((shot["start"], shot["end"]), (0, 2))
