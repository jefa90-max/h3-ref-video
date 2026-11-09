"""A Windows reader must not lose an accepted paid task during atomic state publication."""
import errno
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from h3ref import film
from workbench import storyboard

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("h3ref_encoding_cli", ROOT / "h3ref.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


@contextlib.contextmanager
def legacy_windows_text_encoding():
    with patch.object(io, "text_encoding", side_effect=lambda encoding, *args: "cp936" if encoding in (None, "locale") else encoding):
        yield


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

    def test_chinese_frame_state_stays_utf8_when_windows_default_is_cp936(self):
        expected = {"prompt": "小林走进厨房", "task": "accepted-frame-task"}
        with legacy_windows_text_encoding():
            self.job.put("frames", "s1-1", **expected)
            try:
                published = storyboard.frame_records({"output_dir": str(self.job.dir), "demo": False})
                resumed = film.Job(self.job.dir).state["frames"]
            except UnicodeError as exc:
                self.fail(f"Windows locale broke UTF-8 state publication or resume: {exc}")
        self.assertEqual(published["s1-1"], expected)
        self.assertEqual(resumed["s1-1"], expected)

    def test_single_clip_resumes_and_saves_utf8_state_when_windows_default_is_cp936(self):
        state = {"h3": "小林走进厨房"}
        self.job.file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        prompt = self.job.dir / "input.txt"
        prompt.write_text("小林走进厨房", encoding="utf-8")
        args = SimpleNamespace(out=str(self.job.dir), image=[], prompt=str(prompt),
                               seed=7, seconds=5, aspect="9:16", check=False)
        graph_prompts = []

        def graph(prompt, images, seconds, aspect, seed):
            graph_prompts.append(prompt)
            return {}

        with legacy_windows_text_encoding(), contextlib.redirect_stdout(io.StringIO()), \
             patch.object(CLI.rh, "graph", side_effect=graph), \
             patch.object(CLI.rh, "submit_with_retry", return_value="accepted-clip-task"), \
             patch.object(CLI.rh, "poll", return_value={"state": "done", "url": "fake://clip", "cny": 0.1}), \
             patch.object(CLI.http, "download", side_effect=lambda url, path: path.write_bytes(b"saved video")), \
             patch.object(CLI.media, "duration", return_value=5):
            try:
                CLI.cmd_one(args)
                published = storyboard.read_json(self.job.file, {})
            except UnicodeError as exc:
                self.fail(f"Windows locale broke UTF-8 single-clip state: {exc}")
        self.assertEqual(graph_prompts, ["小林走进厨房"])
        self.assertEqual(published["h3"], "小林走进厨房")
        self.assertEqual(published["task"], "accepted-clip-task")
        self.assertEqual((self.job.dir / "clip.mp4").read_bytes(), b"saved video")

    def test_legacy_non_utf8_state_is_rejected_without_rewriting_it(self):
        previous = json.dumps({"frames": {"s1-1": {"prompt": "小林走进厨房"}}, "segs": {}}, ensure_ascii=False).encode("cp936")
        self.job.file.write_bytes(previous)
        with legacy_windows_text_encoding(), self.assertRaises(UnicodeDecodeError):
            film.Job(self.job.dir)
        self.assertEqual(self.job.file.read_bytes(), previous)

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
