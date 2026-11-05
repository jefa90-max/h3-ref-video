"""A Windows reader must not lose an accepted paid task during atomic state publication."""
import errno
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from h3ref import film


class StateSave(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.job = film.Job(Path(directory.name) / "制作短视频")
        self.job.put("segs", "0", h3="saved prompt")
        self.previous = self.job.file.read_bytes()
        self.temporary = self.job.file.with_suffix(".tmp")

    def denied(self, code):
        error = PermissionError(errno.EACCES, "state file is in use", str(self.temporary))
        error.winerror = code
        return error

    def test_temporary_windows_conflict_preserves_old_state_until_task_is_published(self):
        replace = Path.replace
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                previous = self.job.file.read_bytes()
                attempts = []

                def busy_then_release(source, target):
                    attempts.append(source)
                    if len(attempts) <= 2:
                        self.assertEqual(target.read_bytes(), previous)
                        raise self.denied(code)
                    return replace(source, target)

                with patch.object(Path, "replace", busy_then_release), patch.object(film.time, "sleep"):
                    self.job.put("segs", "0", task=f"accepted-task-{code}")
                saved = json.loads(self.job.file.read_text())
                self.assertEqual(saved["segs"]["0"]["task"], f"accepted-task-{code}")
                self.assertFalse(self.temporary.exists())

    def test_persistent_conflict_keeps_both_snapshots_and_raises_original_error(self):
        denied = self.denied(5)
        attempts, waits = [], []

        def always_busy(source, target):
            attempts.append(source)
            self.assertLessEqual(len(attempts), 50, "state publication retries indefinitely")
            raise denied

        with patch.object(Path, "replace", always_busy), patch.object(film.time, "sleep", waits.append):
            with self.assertRaises(PermissionError) as raised:
                self.job.put("segs", "0", task="accepted-task")
        self.assertIs(raised.exception, denied)
        self.assertGreater(len(attempts), 1)
        self.assertGreater(sum(waits), 0)
        self.assertLessEqual(sum(waits), 5)
        self.assertEqual(self.job.file.read_bytes(), self.previous)
        self.assertEqual(json.loads(self.temporary.read_text())["segs"]["0"]["task"], "accepted-task")

    def test_unrelated_errors_fail_immediately_without_losing_pending_task(self):
        for error in (PermissionError(errno.EACCES, "POSIX permission denied"), self.denied(87),
                      OSError(errno.ENOSPC, "disk full")):
            with self.subTest(error=error), patch.object(Path, "replace", side_effect=error), \
                    patch.object(film.time, "sleep", side_effect=AssertionError("retried unrelated error")):
                with self.assertRaises(type(error)) as raised:
                    self.job.put("segs", "0", task="accepted-task")
                self.assertIs(raised.exception, error)
                self.assertEqual(self.job.file.read_bytes(), self.previous)
                self.assertEqual(json.loads(self.temporary.read_text())["segs"]["0"]["task"], "accepted-task")

    def test_concurrent_segments_keep_all_task_ids_when_publication_is_retried(self):
        replace = Path.replace
        busy = True

        def intermittent_reader(source, target):
            nonlocal busy
            busy = not busy
            if not busy:
                raise self.denied(32)
            return replace(source, target)

        def save_segment(index):
            self.job.put("segs", str(index), task=f"accepted-task-{index}")

        with patch.object(Path, "replace", intermittent_reader), patch.object(film.time, "sleep"), \
                ThreadPoolExecutor(4) as workers:
            list(workers.map(save_segment, range(20)))
        records = json.loads(self.job.file.read_text())["segs"]
        self.assertEqual(len(records), 20)
        for index in range(20):
            self.assertEqual(records[str(index)]["task"], f"accepted-task-{index}")
        self.assertFalse(self.temporary.exists())

    def test_save_conflict_after_submission_resumes_same_task_without_paying_again(self):
        (self.job.dir / "clips").mkdir()
        story = {"aspect": "9:16", "shots": [{"start": 0, "end": 5, "action": "A cat walks."}]}
        segment = film.plan(story["shots"])[0]
        replace = Path.replace
        busy = True

        def busy_after_acceptance(source, target):
            nonlocal busy
            if busy and json.loads(source.read_text())["segs"]["0"].get("task"):
                busy = False
                raise self.denied(5)
            return replace(source, target)

        def interrupted_poll(task):
            self.assertEqual(task, "accepted-task")
            self.assertEqual(json.loads(self.job.file.read_text())["segs"]["0"]["task"], task)
            raise RuntimeError("poll interrupted")

        with patch.object(film.rh, "submit_with_retry", return_value="accepted-task"), \
                patch.object(film.rh, "graph", return_value={}), \
                patch.object(Path, "replace", busy_after_acceptance), patch.object(film.time, "sleep"), \
                patch.object(film.rh, "poll", interrupted_poll):
            with self.assertRaisesRegex(RuntimeError, "poll interrupted"):
                film.gen_segment(self.job, story, segment, [])

        with patch.object(film.rh, "submit_with_retry", side_effect=AssertionError("paid for task twice")), \
                patch.object(film.rh, "poll", return_value={"state": "done", "url": "fake://clip", "cny": 0.1}), \
                patch.object(film.http, "download", side_effect=lambda url, out: out.write_bytes(b"saved video")), \
                patch.object(film.media, "last_frame"):
            result = film.gen_segment(film.Job(self.job.dir), story, segment, [])
        self.assertEqual(result["task"], "accepted-task")
        self.assertEqual((self.job.dir / "clips/seg1.mp4").read_bytes(), b"saved video")

    @unittest.skipUnless(os.name == "nt", "requires Windows file sharing semantics")
    def test_windows_read_handle_is_released_before_atomic_replace_retry(self):
        with self.job.file.open("rb") as reader:
            with patch.object(film.time, "sleep", side_effect=lambda delay: reader.close()):
                self.job.put("segs", "0", task="accepted-task")
                self.assertTrue(reader.closed, "the open handle did not trigger a replacement conflict")
        self.assertEqual(json.loads(self.job.file.read_text())["segs"]["0"]["task"], "accepted-task")
        self.assertFalse(self.temporary.exists())


if __name__ == "__main__":
    unittest.main()
