import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from h3ref import frames, http
from scripts import ab_eval
from scripts.ab_state import ExperimentError, Journal, money


class ABTextResponseRegression(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.url = "https://provider.example.invalid"

    def session(self, *, resume=False):
        journal = Journal(self.directory / "ab_state.json", "run", "fixture", money("1"), resume=resume)
        return ab_eval.Run(self.directory, {}, None, None, {"composition.llm": "0.10"}, "",
                           dry_run=False, journal=journal)

    def compose(self, run):
        shot = {"start": 0, "end": 5, "action": "The room light turns on."}
        with patch.object(frames.config, "llm_settings", return_value={"base_url": self.url, "composition_model": "fixture"}), \
             patch.object(frames.config, "llm_key", return_value="fixture-key"):
            return ab_eval.stage_composition(run, "baseline", "llm", {"shots": [shot]}, shot, (None, {}), None)

    def test_composition_caches_received_non_json_without_resending(self):
        run = self.session()
        with patch.object(http._OPENER, "open", return_value=BytesIO(b"<html>temporary gateway page</html>")) as request:
            try:
                result = self.compose(run)
            except RuntimeError as exc:
                self.fail(f"A received malformed response aborted the run: {exc}")
            self.assertIsNone(result)
            self.assertEqual(run.journal.steps["baseline"]["status"], "invalid_response")
            self.assertEqual(run.journal.committed_cny(), money("0.10"))
            self.assertIsNone(self.compose(self.session(resume=True)))
            request.assert_called_once()

    def test_http_or_transport_failure_does_not_mark_response_invalid(self):
        root = self.directory
        failures = (
            (HTTPError(self.url, 403, "forbidden", {}, BytesIO(b"<html>forbidden</html>")), http.HTTPResponseError),
            (URLError("connection failed"), http.TransportError),
        )
        for index, (failure, error_type) in enumerate(failures):
            with self.subTest(error=error_type), patch.object(http._OPENER, "open", side_effect=failure) as request:
                self.directory = root / str(index)
                run = self.session()
                with self.assertRaises(error_type):
                    self.compose(run)
                self.assertEqual(run.journal.steps["baseline"]["status"], "submission_unknown")
                with self.assertRaisesRegex(ExperimentError, "submission_unknown"):
                    self.compose(self.session(resume=True))
                request.assert_called_once()

    def test_http_request_retries_invalid_json_for_existing_callers(self):
        with patch.object(http._OPENER, "open", side_effect=[BytesIO(b"invalid"), BytesIO(b'{"ok":true}')]) as request, \
             patch.object(http.time, "sleep"):
            self.assertEqual(http.request(self.url, "", tries=2), {"ok": True})
            self.assertEqual(request.call_count, 2)
