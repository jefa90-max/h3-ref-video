import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from h3ref import frames, http, rewrite, rh
from scripts import ab_eval, ab_variants
from scripts.ab_state import ExperimentError, Journal, money


class ABRecoveryRegressions(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.journal_path = self.root / "ab_state.json"
        self.picture = self.root / "opening.png"
        self.picture.write_bytes(b"opening")

    def run_session(self, *, resume=False, cap="10"):
        journal = Journal(self.journal_path, "run", "rules", money(cap), resume=resume)
        quotes = {"image": "0.5", "composition.llm": "0.1", "rewrite.llm": "0.1", "video_per_second": "0.1"}
        return ab_eval.Run(self.root, {"video": {"provider": "runninghub"}}, rh, None,
                           quotes, "Guide fixture.", dry_run=False, journal=journal)

    def test_image_upload_failure_can_retry_on_resume_before_generation(self):
        def upload(url, key, path):
            return {"url": "https://example.invalid/opening.jpg"}

        def download(url, path):
            path.write_bytes(b"completed image")

        with patch.object(frames.media, "shrink", return_value=self.picture), \
             patch.object(frames.config, "apimart_key", return_value="test-key"), \
             patch.object(http, "upload", side_effect=OSError("temporary upload failure")) as upload_request, \
             patch.object(http, "request", return_value={"data": [{"task_id": "image-task"}]}) as submit, \
             patch.object(frames, "poll", return_value={"state": "done", "url": "https://example.invalid/image.png", "cny": 0.2}), \
             patch.object(http, "download", side_effect=download):
            run = self.run_session()
            with self.assertRaisesRegex(OSError, "temporary upload failure"):
                run.image("side/image-1", "Opening scene", [self.picture], "16:9")
            submit.assert_not_called()
            resumed = self.run_session(resume=True)
            self.assertEqual(resumed.journal.committed_cny(), money("0.5"))
            upload_request.side_effect = upload
            result = resumed.image("side/image-1", "Opening scene", [self.picture], "16:9")
            self.assertEqual(result.read_bytes(), b"completed image")
            self.assertEqual(resumed.journal.committed_cny(), money("0.2"))
            submit.assert_called_once()

    def test_video_upload_failure_can_retry_on_resume_before_generation(self):
        def download(url, path):
            path.write_bytes(b"completed video")

        with patch.object(rh, "upload", side_effect=OSError("temporary upload failure")) as upload, \
             patch.object(rh, "submit", return_value="video-task") as submit, \
             patch.object(rh, "poll", return_value={"state": "done", "url": "https://example.invalid/video.mp4",
                                                  "cny": 0.2, "billing_known": True}), \
             patch.object(http, "download", side_effect=download):
            with self.assertRaisesRegex(OSError, "temporary upload failure"):
                self.run_session().video("side/video", "Opening scene", [self.picture], 5, "16:9", 2)
            submit.assert_not_called()
            upload.side_effect = None
            upload.return_value = "opening.jpg"
            path = self.run_session(resume=True).video("side/video", "Opening scene", [self.picture], 5, "16:9", 2)
            self.assertEqual(path.read_bytes(), b"completed video")
            submit.assert_called_once()

    def test_image_response_loss_after_dispatch_remains_unknown_on_resume(self):
        with patch.object(frames.config, "apimart_key", return_value="test-key"), \
             patch.object(http, "request", side_effect=TimeoutError("submit response lost")) as submit:
            with self.assertRaisesRegex(TimeoutError, "submit response lost"):
                self.run_session().image("side/image-1", "Opening scene", [], "16:9")
            with self.assertRaisesRegex(ExperimentError, "submission_unknown"):
                self.run_session(resume=True).image("side/image-1", "Opening scene", [], "16:9")
            submit.assert_called_once()

    def test_video_response_loss_after_dispatch_remains_unknown_on_resume(self):
        with patch.object(rh, "upload", return_value="opening.jpg"), \
             patch.object(rh, "submit", side_effect=TimeoutError("submit response lost")) as submit:
            with self.assertRaisesRegex(TimeoutError, "submit response lost"):
                self.run_session().video("side/video", "Opening scene", [self.picture], 5, "16:9", 2)
            with self.assertRaisesRegex(ExperimentError, "submission_unknown"):
                self.run_session(resume=True).video("side/video", "Opening scene", [self.picture], 5, "16:9", 2)
            submit.assert_called_once()

    def test_resume_keeps_reported_image_charge_when_poll_omits_billing(self):
        self.assert_resumed_charge_blocks_next_submission("image")

    def test_resume_keeps_reported_video_charge_when_poll_omits_billing(self):
        self.assert_resumed_charge_blocks_next_submission("video")

    def test_resume_keeps_video_charge_when_only_coins_are_returned(self):
        output = {"fileUrl": "https://example.invalid/video.mp4", "fileType": "mp4", "consumeCoins": 0}
        polls = [{"code": 0, "data": [{**output, "consumeMoney": "0.80"}]}, {"code": 0, "data": [output]}]
        arguments = ("Opening scene", [self.picture], 5, "16:9", 2)

        def download(url, path):
            path.write_bytes(b"completed video")

        with patch.object(rh, "_post", side_effect=polls), \
             patch.object(rh, "upload", return_value="opening.jpg"), \
             patch.object(rh, "submit", return_value="video-task") as submit, \
             patch.object(http, "download", side_effect=OSError("download interrupted")) as fetch:
            with self.assertRaisesRegex(OSError, "download interrupted"):
                self.run_session(cap="1.00").video("first", *arguments)
            fetch.side_effect = download
            resumed = self.run_session(resume=True, cap="1.00")
            resumed.video("first", *arguments)
            self.assertEqual(resumed.journal.committed_cny(), money("0.80"))
            with self.assertRaisesRegex(ExperimentError, "budget_exhausted"):
                resumed.video("second", *arguments)
            submit.assert_called_once()

    def test_forbidden_reply_with_choices_stops_ab_composition(self):
        import io
        from urllib.error import HTTPError

        fixture = {"id": "plain-room", "film": {"shots": [{"start": 0, "end": 5, "action": "亮灯。"}]}}
        reply = b'{"error":{"message":"Forbidden"},"choices":null,"_http_status":null}'

        def forbidden(*args, **kwargs):
            raise HTTPError("https://example.invalid/chat/completions", 403, "Forbidden", {}, io.BytesIO(reply))

        with patch.object(frames.config, "llm_key", return_value="test-key"), \
             patch.object(http.urllib.request, "urlopen", side_effect=forbidden) as send:
            with self.assertRaises(http.HTTPResponseError) as rejected:
                ab_eval.run_pairs(self.run_session(), [("3B", fixture, "llm")], {}, 2)
            self.assertEqual(rejected.exception.status_code, 403)
            send.assert_called_once()

    def test_nonfinite_response_is_cached_as_invalid_without_resubmission(self):
        run = self.run_session()
        with patch.object(frames, "draw") as draw, \
             patch.object(frames.config, "llm_key", return_value="test-key"), \
             patch.object(http, "request", return_value={"choices": None, "usage": {"tokens": float("nan")}}) as send:
            script = {"shots": [{"start": 0, "end": 5, "action": "亮灯。"}]}
            self.assertIsNone(ab_eval.stage_composition(run, "side/compose", "llm", script,
                                                        script["shots"][0], (None, {}), "Rules"))
            resumed = self.run_session(resume=True)
            self.assertIsNone(ab_eval.stage_composition(resumed, "side/compose", "llm", script,
                                                        script["shots"][0], (None, {}), "Rules"))
            self.assertEqual(resumed.journal.steps["side/compose"]["status"], "invalid_response")
            send.assert_called_once()
            draw.assert_not_called()

    def assert_resumed_charge_blocks_next_submission(self, kind):
        def download(url, path):
            path.write_bytes(b"completed media")

        sent = {"data": [{"task_id": "image-task"}]} if kind == "image" else "video-task"
        provider = frames if kind == "image" else rh
        submit_target, submit_method = (http, "request") if kind == "image" else (rh, "submit")
        polls = [{"state": "done", "url": "https://example.invalid/media", "cny": 0.8, "billing_known": True},
                 {"state": "done", "url": "https://example.invalid/media"},
                 {"state": "done", "url": "https://example.invalid/media", "cny": 0.5, "billing_known": True}]
        arguments = ("Opening scene", [], "16:9") if kind == "image" else ("Opening scene", [self.picture], 5, "16:9", 2)
        with patch.object(frames.config, "apimart_key", return_value="test-key"), \
             patch.object(rh, "upload", return_value="opening.jpg"), \
             patch.object(submit_target, submit_method, return_value=sent) as submit, \
             patch.object(provider, "poll", side_effect=polls), \
             patch.object(http, "download", side_effect=OSError("download interrupted")) as fetch:
            run = self.run_session(cap="1.00")
            with self.assertRaisesRegex(OSError, "download interrupted"):
                getattr(run, kind)("first", *arguments)
            self.assertEqual(run.journal.committed_cny(), money("0.80"))

            resumed = self.run_session(resume=True, cap="1.00")
            fetch.side_effect = download
            media_path = getattr(resumed, kind)("first", *arguments)
            self.assertEqual(media_path.read_bytes(), b"completed media")
            with self.assertRaisesRegex(ExperimentError, "budget_exhausted"):
                getattr(resumed, kind)("second", *arguments)
            self.assertEqual(resumed.journal.committed_cny(), money("0.80"))
            submit.assert_called_once()

    def test_invalid_composition_stops_only_its_side_and_is_not_resent(self):
        fixture = {"id": "plain-room", "description": "A light turns on.",
                   "film": {"aspect": "16:9", "shots": [{"start": 0, "end": 5, "action": "客厅亮起灯。"}]}}
        cases = [("3B", fixture, "llm")]
        invalid = {"_http_status": None, "choices": [{"message": {"content": "not JSON"}}]}
        valid = {"choices": [{"message": {"content": '{"version":3,"camera":"全景","scene":"客厅","characters":[]}'}}]}

        def draw(prompt, references, aspect, path, *, record, task, settle):
            record(submission_unknown=True)
            record(task="completed-image")
            settle(0.2)
            path.write_bytes(b"completed image")

        with patch.object(frames.config, "llm_key", return_value="test-key"), \
             patch.object(http, "request", side_effect=[invalid, valid]) as request, \
             patch.object(frames, "draw", side_effect=draw) as submit:
            run = self.run_session()
            pairs = ab_eval.run_pairs(run, cases, {}, 2)
            self.assertEqual([side["complete"] for side in pairs[0]["sides"]], [False, True])
            self.assertEqual(run.journal.committed_cny(), money("0.4"))
            failed = run.journal.steps["3B/plain-room/llm/baseline/compose-1"]
            self.assertEqual(failed["status"], "invalid_response")
            self.assertTrue(failed["validation_error"])
            self.assertEqual(ab_eval.run_pairs(self.run_session(resume=True), cases, {}, 2), pairs)
            self.assertEqual(request.call_count, 2)
            submit.assert_called_once()

    def test_invalid_rewrite_skips_video_only_for_its_side(self):
        self.assert_invalid_rewrite_isolated("Not an H3 prompt")

    def test_empty_successful_rewrite_skips_video_only_for_its_side(self):
        self.assert_invalid_rewrite_isolated("")

    def assert_invalid_rewrite_isolated(self, invalid):
        fixture = {"id": "plain-room", "description": "A light turns on.", "frozen_frames": ["asset:opening"],
                   "film": {"aspect": "16:9", "shots": [{"start": 0, "end": 5, "action": "客厅亮起灯。"}]}}
        cases = [("3D", fixture, "llm")]
        resolved = {("plain-room", "opening"): self.picture}
        valid = ab_variants.stub_rewrite(1, "16:9")

        def download(url, path):
            path.write_bytes(b"completed video")

        replies = [{"choices": [{"message": {"content": reply}}]} for reply in (invalid, valid, valid)]
        with patch.object(rewrite.config, "llm_key", return_value="test-key"), \
             patch.object(http, "request", side_effect=replies) as request, \
             patch.object(rh, "upload", return_value="opening.jpg"), \
             patch.object(rh, "submit", return_value="video-task") as submit, \
             patch.object(rh, "poll", return_value={"state": "done", "url": "https://example.invalid/video.mp4",
                                                  "cny": 0.2, "billing_known": True}), \
             patch.object(http, "download", side_effect=download):
            run = self.run_session()
            pairs = ab_eval.run_pairs(run, cases, resolved, 2)
            self.assertEqual([[side["complete"] for side in pair["sides"]] for pair in pairs],
                             [[False, True], [False, True]])
            self.assertEqual(run.journal.committed_cny(), money("0.7"))
            failed = run.journal.steps["3D/plain-room/llm/baseline/rewrite"]
            self.assertEqual(failed["status"], "invalid_response")
            self.assertTrue(failed["validation_error"])
            self.assertEqual(ab_eval.run_pairs(self.run_session(resume=True), cases, resolved, 2), pairs)
            self.assertEqual(request.call_count, 3)
            self.assertEqual(submit.call_count, 2)
