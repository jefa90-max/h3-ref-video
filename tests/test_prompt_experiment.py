import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from h3ref import frames, rewrite
from scripts.ab_state import ExperimentError, Journal, money


class PromptExperimentTests(unittest.TestCase):
    def test_neutralize_override_does_not_change_defaults_or_allow_banned_character(self):
        plain = frames.neutralize_text("角色死命抓住栏杆，衣服沾有鲜血")
        variant = frames.neutralize_text("角色死命抓住栏杆，衣服沾有鲜血", neutral_words={"鲜血": "深红痕迹"})
        self.assertIn("深色污渍", plain)
        self.assertIn("深红痕迹", variant)
        self.assertNotIn("死", frames.neutralize_text("死命", neutral_words={"死命": "死"}))
        self.assertEqual(frames.neutralize_text("角色死命抓住栏杆，衣服沾有鲜血"), plain)

    def test_rewrite_backend_reads_runtime_override(self):
        with patch.dict("os.environ", {"H3_REWRITER": "llm"}):
            self.assertEqual(rewrite.backend(), "llm")
        with patch.dict("os.environ", {"H3_REWRITER": "claude"}):
            self.assertEqual(rewrite.backend(), "claude")
        self.assertEqual(rewrite.backend("llm"), "llm")

    def test_lost_response_stays_reserved_and_is_never_dispatched_again(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ab_state.json"
            journal = Journal(path, "run", "fingerprint", money("1"), resume=False)
            journal.reserve("one", money("0.6"))
            resumed = Journal(path, "run", "fingerprint", money("1"), resume=True)
            with self.assertRaisesRegex(ExperimentError, "submission_unknown"):
                resumed.reserve("one", money("0.6"))
            with self.assertRaisesRegex(ExperimentError, "budget"):
                resumed.reserve("two", money("0.5"))
            with self.assertRaisesRegex(ExperimentError, "fingerprint"):
                Journal(path, "run", "different", money("1"), resume=True)

    def test_missing_charge_keeps_reservation_and_actual_cost_controls_next_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ab_state.json"
            journal = Journal(path, "run", "fingerprint", money("1"), resume=False)
            journal.reserve("one", money("0.4"))
            journal.update("one", status="done", actual_cny=None)
            self.assertEqual(journal.committed_cny(), money("0.4"))
            journal.reserve("two", money("0.2"))
            journal.update("two", status="done", actual_cny="0.5")
            self.assertEqual(journal.committed_cny(), money("0.9"))
            with self.assertRaisesRegex(ExperimentError, "budget"):
                journal.reserve("three", money("0.2"))

    def test_runninghub_missing_billing_is_distinct_from_explicit_zero(self):
        from h3ref import rh

        item = {"fileUrl": "https://example.invalid/output.mp4", "fileType": "mp4"}
        with patch.object(rh, "_post", return_value={"code": 0, "data": [item]}):
            self.assertFalse(rh.poll("task", report_billing=True)["billing_known"])
            self.assertNotIn("billing_known", rh.poll("task"))
            item["consumeCoins"] = 0
            self.assertTrue(rh.poll("task", report_billing=True)["billing_known"])

    def test_same_source_remains_one_reference_after_freezing(self):
        from scripts.ab_eval import freeze_assets
        from scripts.ab_state import file_sha256

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            picture = root / "picture.png"
            picture.write_bytes(b"source contents")
            entry = (picture, file_sha256(picture))
            frozen = freeze_assets(root / "run", {("fixture", "one"): entry, ("fixture", "two"): entry})
            self.assertEqual(frozen["fixture", "one"], frozen["fixture", "two"])

    def test_resume_polls_the_original_image_and_dispatches_only_the_remaining_variant(self):
        import json
        from PIL import Image
        from scripts.ab_eval import execute, parser

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quotes = root / "quotes.json"
            quotes.write_text('{"image": "0.3"}')
            args = parser().parse_args([
                "--arm", "3A", "--fixture", "staged-shove", "--out", str(root / "run"),
                "--quotes", str(quotes), "--max-cny", "1",
            ])
            seen = []

            def draw(text, references, aspect, out, record, task=None, *, settle):
                seen.append(task)
                if len(seen) == 1:
                    record(submission_unknown=True)
                    record(task="accepted-image-task", submission_unknown=False)
                    raise RuntimeError("interrupted during poll")
                if task is None:
                    record(submission_unknown=True)
                    record(task="other-variant-task", submission_unknown=False)
                settle(0.15)
                Image.new("RGB", (6, 6)).save(out)
                return 0.15

            with patch.dict("os.environ", {"APIMART_API_KEY": "secret-fixture-value"}), patch.object(frames, "draw", side_effect=draw):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    execute(args)
                args.resume = True
                result = execute(args)
            self.assertEqual(seen, [None, "accepted-image-task", None])
            self.assertEqual(result["reserved_or_charged_cny"], "0.30")
            html = (root / "run/ab_review.html").read_text(encoding="utf-8")
            self.assertNotIn("secret-fixture-value", html)
            self.assertNotIn("accepted-image-task", html)
            state = json.loads((root / "run/ab_state.json").read_text(encoding="utf-8"))
            self.assertTrue(all(step["status"] == "done" and step["prompt_sha256"] for step in state["steps"].values()))

    def test_full_preview_refuses_network_and_never_writes_a_paid_ledger(self):
        import json
        import subprocess
        import sys

        workspace = Path(__file__).resolve().parents[1]
        guard = """
import os, runpy, sys
entry = sys.argv[1]
def audit(event, args):
    if event.startswith('socket.') or event == 'subprocess.Popen':
        raise AssertionError(event)
    if event == 'open' and isinstance(args[0], str) and os.path.basename(args[0]) == '.env':
        raise AssertionError('dotenv read')
sys.addaudithook(audit)
sys.argv = sys.argv[1:]
runpy.run_path(entry, run_name='__main__')
"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview"
            done = subprocess.run([sys.executable, "-B", "-S", "-c", guard,
                                   str(workspace / "scripts/ab_eval.py"), "--dry-run", "--out", str(output)],
                                  text=True, capture_output=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            draft = json.loads((output / "dry_run.json").read_text(encoding="utf-8"))
            self.assertEqual(draft["charged_cny"], "0")
            self.assertEqual(draft["pairs"], 130)
            self.assertEqual(len(draft["missing_assets"]), 16)
            self.assertEqual(sum(draft["calls"].values()), 436)
            self.assertTrue(all(item["within_15_percent"] for item in draft["prompt_growth"].values()))
            self.assertFalse((output / "ab_state.json").exists())
            self.assertEqual(json.loads((output / "answer_key.json").read_text())["dry_run"], True)

    def test_locked_look_survives_experiment_with_changed_opening_state(self):
        import json
        from scripts import ab_variants as variants

        cast = {"name": "小林", "look": "蓝外套，黑框眼镜", "picture": "/tmp/portrait.jpg"}
        first_shot = {"action": "小林整理蓝色外套。"}
        next_shot = {"action": "小林已脱下外套，坐在椅上。"}
        film = {"aspect": "16:9", "cast": [cast], "shots": [first_shot, next_shot]}
        fixed = "黑框眼镜，蓝外套"
        first = {"version": 3, "camera": "中景", "scene": "室内椅子", "characters": [
            {"name": "小林", "locked_look": fixed, "placement_and_pose": "站在椅边。"},
        ]}
        reply = {**first, "characters": [{"name": "小林", "locked_look": "白衬衫",
                                         "placement_and_pose": "在椅上坐直，穿白衬衫，外套搭在椅背。"}]}
        composition = frames.describe_composition(film, next_shot, previous_composition=first,
            locked_looks={"小林": fixed}, rules_override=variants.prompt_options("2A", "rules", "")["composition_rules"],
            infer=lambda messages: {"choices": [{"message": {"content": json.dumps(reply)}}]}, max_retries=0)
        self.assertEqual(composition["characters"][0]["locked_look"], "黑框眼镜")
        self.assertIn("外套搭在椅背", frames.prompt(
            film, next_shot, [cast], False, composition, shot_state_note=variants.IMAGE_STATE_NOTE))

    def test_manual_h3_visual_negation_retains_existing_validation(self):
        from scripts.ab_variants import stub_rewrite

        prompt = stub_rewrite(0, "16:9").replace("The scene follows the supplied shot.", "The light does not glow.")
        self.assertEqual(rewrite.cached_problems(prompt, 0), [])
        self.assertEqual(rewrite.problems(prompt, 0), [])

    def test_insufficient_budget_fails_before_provider_access(self):
        from scripts.ab_eval import execute, parser

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quotes = root / "quotes.json"
            quotes.write_text('{"image": "0.3"}')
            args = parser().parse_args([
                "--arm", "3A", "--fixture", "staged-shove", "--out", str(root / "run"),
                "--quotes", str(quotes), "--max-cny", "0.5",
            ])
            with patch("scripts.ab_eval.live_account_fingerprints") as credentials, patch.object(frames, "draw") as draw:
                with self.assertRaisesRegex(ExperimentError, "budget_exhausted"):
                    execute(args)
            credentials.assert_not_called()
            draw.assert_not_called()
            self.assertFalse((root / "run/ab_state.json").exists())

    def test_interrupted_asset_copy_does_not_publish_a_partial_reference(self):
        from scripts.ab_eval import freeze_assets
        from scripts.ab_state import file_sha256

        def interrupted(source, target):
            target.write(b"partial")
            raise OSError("interrupted")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            picture = root / "picture.png"
            picture.write_bytes(b"complete")
            assets = {("fixture", "ref"): (picture, file_sha256(picture))}
            with patch("scripts.ab_eval.shutil.copyfileobj", side_effect=interrupted):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    freeze_assets(root / "run", assets)
            self.assertFalse((root / "run/inputs/fixture/ref.png").exists())
            self.assertEqual(freeze_assets(root / "run", assets)["fixture", "ref"].read_bytes(), b"complete")
