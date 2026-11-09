"""Saved clips must become a film before optional remote checks can delay completion."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from h3ref import film


class FilmCompletion(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)
        self.source = self.out / "film.json"
        self.source.write_text(json.dumps({"aspect": "9:16", "speech": "zh",
            "shots": [{"start": 0, "end": 10, "action": "A person walks.", "line": "Hello."},
                      {"start": 10, "end": 20, "action": "A person stops.", "line": "Goodbye."}]}))
        script = film.load(self.source)
        job = film.Job(self.out)
        (self.out / "clips").mkdir()
        for segment in film.plan(script["shots"]):
            index = segment["idx"]
            clip = self.out / "clips" / f"seg{index + 1}.mp4"
            clip.write_bytes(f"saved clip {index + 1}".encode())
            pictures = film.refs(script, segment, "none")
            job.put("segs", str(index), h3="cached prompt", zh=film.compile_prompt(script, segment, pictures, 2),
                    file=str(clip), task=f"saved-task-{index}", cny=0.1)
        self.output = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.output))
        for module, function in ((film.rh, "upload"), (film.rh, "submit_with_retry"),
                                 (film.rewrite, "rewrite"), (film.frames, "draw")):
            stack.enter_context(patch.object(module, function, side_effect=AssertionError("paid work repeated")))
        stack.enter_context(patch.object(film.media, "join", side_effect=self.join_saved_clips))
        stack.enter_context(patch.object(film.media, "duration", return_value=20))

    def join_saved_clips(self, paths, output):
        self.assertEqual([path.read_bytes() for path in paths], [b"saved clip 1", b"saved clip 2"])
        output.write_bytes(b"assembled saved clips")

    def test_paused_checks_leave_completed_film_unchecked_without_calling_vision(self):
        with patch.object(film.check, "subtitles", return_value={"checked": True}) as inspect:
            result = film.run(self.out, self.source, "none")
        inspect.assert_not_called()
        self.assertEqual(result.read_bytes(), b"assembled saved clips")
        self.assertTrue((self.out / "report.md").is_file())
        self.assertTrue(all("subtitles" not in record for record in film.Job(self.out).state["segs"].values()))

    def test_paused_checks_also_skip_a_regenerated_segment(self):
        script = film.load(self.source)
        job = film.Job(self.out)
        with patch.object(film.check, "subtitles", return_value={"checked": True}) as inspect:
            film.stage_check(job, script, [film.plan(script["shots"])[1]])
        inspect.assert_not_called()
        self.assertNotIn("subtitles", film.Job(self.out).state["segs"]["1"])

    def test_waiting_for_subtitle_service_does_not_delay_the_full_video(self):
        checking, release = threading.Event(), threading.Event()

        def slow_check(path):
            checking.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test subtitle check was not released")
            return {"checked": True, "burned_text": False, "frames": [], "text": ""}

        with patch.object(film.config, "SUBTITLE_CHECKS_ENABLED", True), \
                patch.object(film.check, "subtitles", side_effect=slow_check), ThreadPoolExecutor(1) as worker:
            running = worker.submit(film.run, self.out, self.source, "none")
            try:
                self.assertTrue(checking.wait(timeout=5), "subtitle check never started")
                self.assertTrue((self.out / "film.mp4").is_file(), "optional check is blocking assembly of saved clips")
                self.assertEqual((self.out / "film.mp4").read_bytes(), b"assembled saved clips")
            finally:
                release.set()
                running.result(timeout=5)
        self.assertTrue((self.out / "report.md").is_file())

    def test_subtitle_failure_is_visible_and_cannot_prevent_assembly(self):
        for language, assembly, checking in (("en", "Assembling", "Checking subtitles"),
                                             ("zh-CN", "正在拼接", "正在检查字幕")):
            with self.subTest(language=language), patch.dict(os.environ, {"H3_LANG": language}):
                job = film.Job(self.out)
                for record in job.state["segs"].values():
                    record.pop("subtitles", None)
                job.save()
                self.output.seek(0)
                self.output.truncate()
                with patch.object(film.config, "SUBTITLE_CHECKS_ENABLED", True), \
                        patch.object(film.check, "subtitles", side_effect=TimeoutError("vision unavailable")):
                    result = film.run(self.out, self.source, "none")
                self.assertEqual(result.read_bytes(), b"assembled saved clips")
                log = self.output.getvalue()
                self.assertIn(assembly, log)
                self.assertIn(checking, log)
                self.assertLess(log.index(assembly), log.index(checking))
                self.assertIn("vision unavailable", log)
                self.assertTrue(all(not record["subtitles"]["checked"] for record in film.Job(self.out).state["segs"].values()))


if __name__ == "__main__":
    unittest.main()
