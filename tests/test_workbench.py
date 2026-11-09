"""Offline regressions for the UI's CLI boundary; never contact paid services."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from h3ref import config, film, frames
from workbench.inputs import preflight, validate
from workbench.jobs import command
from workbench.settings import save_env
from workbench import storyboard

ENGLISH_H3 = """subject_definitions:
A person.
summary:
A person walks.
retention_analysis:
Identity preserved.
detailed_description:
[Shot 1] A person walks outdoors.
overall_soundscape:
Footsteps.
non_diegetic_music:
N/A"""


class CliParity(unittest.TestCase):
    def test_paused_forced_check_does_not_require_a_vision_key(self):
        request = {"kind": "one", "demo": False, "prompt": ENGLISH_H3, "check": True}
        with patch("shutil.which", return_value="/fake/ffmpeg"):
            preflight(request, {"RUNNINGHUB_API_KEY": "fake-rh"})

    def test_english_six_sections_need_no_vision_or_rewrite_key(self):
        request = {"kind": "one", "demo": False, "prompt": ENGLISH_H3}
        with patch("shutil.which", return_value="/fake/ffmpeg"):
            preflight(request, {"RUNNINGHUB_API_KEY": "fake-rh"})

    def test_film_normalization_matches_cli_instead_of_extra_ui_rules(self):
        script = {"shots": [{"action": ""}, {"action": "walk", "start": 8},
                            {"action": "turn", "start": 0, "end": 0.1},
                            {"action": "stop", "start": "0", "end": "2.125"}]}
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "film.json"
            source.write_text(json.dumps(script), encoding="utf-8")
            actual = validate({"kind": "film", "demo": False, "film": script}, Path(temp))
            self.assertEqual(actual["film"]["shots"], film.load(source)["shots"])

    def test_film_reference_paths_use_the_json_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "story"
            base.mkdir()
            Image.new("RGB", (10, 10)).save(base / "actor.png")
            script = {"cast": [{"name": "actor", "picture": "actor.png"}], "shots": [{"action": "actor walks"}]}
            actual = validate({"kind": "film", "demo": False, "film": script, "film_base_dir": "story"}, Path(temp))
            self.assertEqual(actual["film"]["cast"][0]["picture"], str((base / "actor.png").resolve()))

    def test_one_optional_flags_are_passed_to_the_original_cli(self):
        request = {"kind": "one", "demo": False, "output_dir": "/tmp/example", "seconds": 12,
                   "aspect": "9:16", "images": ["/tmp/actor.png"], "seed": 123, "check": True}
        argv = command(request, config.ROOT)
        self.assertIn("--check", argv)
        self.assertEqual(argv[argv.index("--seed") + 1], "123")

    def test_cached_film_resume_does_not_require_unused_model_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            script = {"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "walk"}]}
            segment = film.plan(script["shots"])[0]
            job = film.Job(out)
            job.put("segs", "0", h3=ENGLISH_H3, zh=film.compile_prompt(script, segment, [], 1), task="already-submitted")
            with patch("shutil.which", return_value="/fake/ffmpeg"):
                preflight({"kind": "film", "demo": False, "film": script, "frames": "none", "output_dir": str(out)},
                          {"RUNNINGHUB_API_KEY": "fake-rh"})


class ReviewState(unittest.TestCase):
    def test_interrupted_frame_backup_retains_complete_original(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            (out / "inputs").mkdir()
            (out / "frames").mkdir()
            script = {"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "A ball rolls."}]}
            (out / "inputs" / "demo-film.json").write_text(json.dumps(script))
            target = out / "frames" / "s1-1.png"
            Image.new("RGB", (8, 8), "red").save(target)
            original = target.read_bytes()
            request = {"kind": "film", "demo": True, "frames": "shot", "operation": "frame",
                       "output_dir": str(out), "lang": "en"}
            pending = {"key": "s1-1", "prompt": "new light", "revision": "review",
                       "previous": {"cny": 0.2}, "task": None}
            (out / "inputs" / "frame-edit.json").write_text(json.dumps(pending))

            def interrupted_copy(source, destination):
                Path(destination).write_bytes(original[:10])
                raise OSError("backup interrupted")

            with patch.object(storyboard.shutil, "copy2", side_effect=interrupted_copy):
                with self.assertRaisesRegex(OSError, "backup interrupted"):
                    storyboard.regenerate(request)
            self.assertEqual(target.read_bytes(), original)
            # A previous version may have left its partial copy at the final backup path.
            (out / "inputs" / "previous-frame-review.png").write_bytes(original[:10])
            storyboard.regenerate(request)
            self.assertEqual((out / "inputs" / "previous-frame-review.png").read_bytes(), original)
            self.assertNotEqual(target.read_bytes(), original)
            storyboard.regenerate(request)
            self.assertAlmostEqual(storyboard.frame_records(request)["s1-1"]["cny"], 0.2)

    def test_finished_outputs_remain_viewable_when_source_image_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp).resolve()
            (out / "inputs").mkdir()
            (out / "frames").mkdir()
            path = out / "frames" / "s1-1.png"
            Image.new("RGB", (10, 10)).save(path)
            source = {"cast": [{"name": "actor", "picture": str(out / "moved-away.png")}],
                      "shots": [{"action": "actor walks", "start": 0, "end": 5}]}
            (out / "inputs" / "film.json").write_text(json.dumps(source))
            request = {"output_dir": str(out), "demo": False, "frames": "shot"}
            cards = storyboard.view(request, [{"path": str(path), "url": "/frame.png", "kind": "image"}], True)
            self.assertEqual(cards[0]["url"], "/frame.png")
            self.assertFalse(cards[0]["editable"])

    def test_frame_failure_keeps_old_image_and_resume_reuses_new_task(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp).resolve()
            (out / "inputs").mkdir()
            (out / "frames").mkdir()
            script = {"aspect": "9:16", "shots": [{"action": "walk", "start": 0, "end": 2},
                                                   {"action": "turn", "start": 2, "end": 4}]}
            (out / "inputs" / "film.json").write_text(json.dumps(script))
            anchor, target = out / "frames" / "s1-1.png", out / "frames" / "s1-2.png"
            Image.new("RGB", (10, 10), "red").save(anchor)
            Image.new("RGB", (10, 10), "green").save(target)
            original_anchor, original_target = anchor.read_bytes(), target.read_bytes()
            request = {"kind": "film", "demo": False, "operation": "frame", "frames": "shot", "output_dir": str(out), "lang": "en"}
            previous = {"task": "old-cloud-task", "prompt": "old prompt", "cny": 0.2}
            pending = {"key": "s1-2", "prompt": "new image prompt", "revision": "revision-1", "previous": previous, "task": None}
            (out / "inputs" / "frame-edit.json").write_text(json.dumps(pending))
            film.Job(out).put("frames", "s1-2", **previous)

            def download(url, path):
                Image.new("RGB", (10, 10), "blue").save(path)

            with patch.object(frames, "submit", return_value="new-cloud-task") as submit:
                with patch.object(frames, "poll", side_effect=RuntimeError("network disconnected")):
                    with self.assertRaises(RuntimeError):
                        storyboard.regenerate(request)
                self.assertEqual(target.read_bytes(), original_target)
                self.assertEqual(json.loads((out / "inputs" / "frame-edit.json").read_text())["task"], "new-cloud-task")
                with patch.object(frames, "poll", side_effect=[{"state": "running"}, {"state": "done", "url": "fake://image", "cny": 0.3}]), patch.object(frames.http, "download", side_effect=download):
                    storyboard.regenerate(request)
                storyboard.regenerate(request)  # Already applied: no second payment or cost addition.
                submit.assert_called_once_with("new image prompt", [anchor], "9:16")
            self.assertEqual(anchor.read_bytes(), original_anchor)
            self.assertNotEqual(target.read_bytes(), original_target)
            rec = film.Job(out).state["frames"]["s1-2"]
            self.assertEqual(rec["prompt"], "new image prompt")
            self.assertEqual(rec["task"], "new-cloud-task")
            self.assertAlmostEqual(rec["cny"], 0.5)
            self.assertEqual(storyboard.review_state(out)["dirty_segments"], [1])

    def test_cli_redo_rule_invalidates_only_the_required_segments(self):
        for mode, expected in (("shot", {"1"}), ("first", {"1", "2"})):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                job = film.Job(Path(temp))
                (job.dir / "clips").mkdir()
                for index in range(3):
                    job.put("segs", str(index), task=f"task-{index}", h3="original", zh="script", subtitles={"checked": True})
                    (job.dir / "clips" / f"seg{index + 1}.mp4").touch()
                film.invalidate_segments(job, mode, [2])
                for index in range(3):
                    self.assertEqual("task" not in job.state["segs"][str(index)], str(index) in expected)
                    self.assertEqual((job.dir / "clips" / f"seg{index + 1}.mp4").exists(), str(index) not in expected)

    def test_retry_after_redo_keeps_the_new_cloud_task(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            (out / "inputs").mkdir()
            job = film.Job(out)
            job.put("segs", "0", task="old-task", h3=ENGLISH_H3, zh="original")
            request = {"kind": "film", "demo": False, "operation": "pipeline", "output_dir": str(out),
                       "frames": "shot", "redo": [1]}

            def first_run(*args):
                resumed = film.Job(out)
                self.assertNotIn("task", resumed.state["segs"]["0"])
                resumed.put("segs", "0", task="new-paid-task")
                raise RuntimeError("simulated network failure after submit")

            with patch.object(film, "run", side_effect=first_run):
                with self.assertRaises(RuntimeError):
                    storyboard.run(request)
            request = json.loads((out / "inputs" / "request.json").read_text())
            with patch.object(film, "run") as original_pipeline:
                storyboard.run(request)
                original_pipeline.assert_called_once()
            self.assertEqual(film.Job(out).state["segs"]["0"]["task"], "new-paid-task")


class LocalSettings(unittest.TestCase):
    def test_env_save_is_private_and_preserves_unrelated_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text("# comment\nRUNNINGHUB_API_KEY=old\nIMAGE_QUALITY=high\nRUNNINGHUB_API_KEY=duplicate\n")
            save_env(path, {"RUNNINGHUB_API_KEY": "fake-new", "APIMART_API_KEY": "fake-image"})
            saved = path.read_text()
            self.assertIn("# comment\n", saved)
            self.assertIn("IMAGE_QUALITY=high", saved)
            self.assertEqual(saved.count("RUNNINGHUB_API_KEY="), 1)
            self.assertIn("APIMART_API_KEY=fake-image", saved)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
