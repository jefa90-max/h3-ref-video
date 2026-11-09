"""Frame edits select characters imported with the film, never unrelated uploads."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from h3ref import film
from workbench import storyboard
from workbench.server import create_app


class FrameCharacters(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.app = create_app(self.root / "storage", self.root / ".env")
        self.client = self.app.test_client()
        self.jobs = self.app.extensions["jobs"]
        self.headers = {"X-Workbench": "1"}
        self.client.post("/api/config", json={"APIMART_API_KEY": "test-only", "persist": False}, headers=self.headers)
        self.alice = self.upload("alice.png", "blue")
        self.bob = self.upload("bob.png", "yellow")
        self.board = self.upload("board.png", "red")
        script = {"cast": [{"name": "Alice"}, {"name": "Bob"}], "board": True,
                  "shots": [{"start": 0, "end": 2, "action": "Alice walks"},
                            {"start": 2, "end": 4, "action": "Alice turns"}]}
        with patch.object(self.jobs, "launch"):
            response = self.client.post("/api/jobs", json={"kind": "film", "demo": False,
                "frames": "shot", "film": script,
                "reference_bindings": {"cast:0": self.alice["id"], "cast:1": self.bob["id"], "board": self.board["id"]},
                "output_dir": str(self.root / "output")}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        self.identifier = response.json["id"]
        self.out = Path(response.json["output_dir"])
        (self.out / "frames").mkdir(exist_ok=True)
        for description in response.json["storyboard"]:
            Image.new("RGB", (8, 8), "green").save(description["path"])
        self.jobs.find(self.identifier)["status"] = "succeeded"
        self.jobs.save(self.jobs.find(self.identifier))
        self.endpoint = f"/api/jobs/{self.identifier}/frames/s1-2"

    def tearDown(self):
        self.jobs.close()
        self.temporary.cleanup()

    def upload(self, name, color):
        image = io.BytesIO()
        Image.new("RGB", (8, 8), color).save(image, "PNG")
        image.seek(0)
        response = self.client.post("/api/uploads", data={"images": (image, name)}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["files"][0]

    def snapshot(self):
        return self.jobs.snapshot(self.identifier)

    def edit(self, paths):
        with patch.object(self.jobs, "launch"):
            return self.client.post(self.endpoint, json={"prompt": "Bob stands by the window",
                "character_reference_paths": paths}, headers=self.headers)

    def regenerate(self):
        request = json.loads((self.out / "inputs/request.json").read_text())
        def draw(prompt, references, aspect, output, **kwargs):
            Image.new("RGB", (9, 9), "purple").save(output)
            return 0
        with patch.object(storyboard.frames, "draw", side_effect=draw) as submitted:
            storyboard.regenerate(request)
        self.jobs.find(self.identifier)["status"] = "succeeded"
        self.jobs.save(self.jobs.find(self.identifier))
        return [str(path) for path in submitted.call_args.args[1]]

    def test_choices_include_all_original_characters_with_previews_but_no_other_uploads(self):
        self.upload("unrelated.png", "white")
        choices = self.snapshot().get("character_references", [])
        self.assertEqual([(c["name"], c["path"]) for c in choices],
                         [("Alice", self.alice["path"]), ("Bob", self.bob["path"])])
        for reference in choices:
            response = self.client.get(reference["url"])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, Path(reference["path"]).read_bytes())
            response.close()

    def test_unrelated_uploaded_image_is_rejected_before_a_frame_task_is_created(self):
        unrelated = self.upload("new-role.png", "white")
        response = self.edit([unrelated["path"]])
        self.assertEqual(response.status_code, 400, response.json)
        self.assertFalse((self.out / "inputs/frame-edit.json").exists())

    def test_legacy_extra_reference_does_not_expand_the_original_character_pool(self):
        unrelated = self.upload("legacy-role.png", "white")
        film.Job(self.out).put("frames", "s1-2", character_reference_paths=[unrelated["path"]])
        self.assertEqual(self.edit([unrelated["path"]]).status_code, 400)
        self.assertEqual(self.edit([self.bob["path"]]).status_code, 200)

    def test_selecting_a_different_imported_character_preserves_fixed_references_and_other_frames(self):
        original = self.snapshot()["storyboard"][0]
        original_bytes = Path(original["path"]).read_bytes()
        self.assertEqual(self.edit([self.bob["path"]]).status_code, 200)
        expected = [self.board["path"], self.bob["path"]]
        self.assertEqual(self.regenerate(), expected)
        current = self.snapshot()
        self.assertEqual(current["storyboard"][1]["reference_paths"], expected)
        self.assertEqual(current["storyboard"][0]["reference_paths"], original["reference_paths"])
        self.assertEqual(Path(original["path"]).read_bytes(), original_bytes)
        restored = create_app(self.root / "storage", self.root / ".env")
        try:
            self.assertEqual(restored.extensions["jobs"].snapshot(self.identifier)["storyboard"][1]["reference_paths"], expected)
        finally:
            restored.extensions["jobs"].close()

    def test_empty_selection_keeps_only_board_and_allows_resume_without_removed_character(self):
        Path(self.alice["path"]).unlink()
        self.assertEqual(self.edit([]).status_code, 200)
        self.jobs.find(self.identifier)["status"] = "failed"
        with patch.object(self.jobs, "launch"):
            response = self.client.post(f"/api/jobs/{self.identifier}/resume", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.regenerate(), [self.board["path"]])

    def test_non_character_references_and_duplicate_characters_are_rejected(self):
        for paths in ([self.board["path"]], [str(self.out / "frames/s1-1.png")], [self.alice["path"], self.alice["path"]]):
            with self.subTest(paths=paths):
                self.assertEqual(self.edit(paths).status_code, 400)
                self.assertFalse((self.out / "inputs/frame-edit.json").exists())

    def test_missing_character_is_marked_unavailable_and_preview_does_not_accept_other_files(self):
        before = self.snapshot().get("character_references", [])
        self.assertEqual(len(before), 2)
        Path(self.bob["path"]).unlink()
        after = self.snapshot()["character_references"]
        self.assertIsNone(after[1]["url"])
        self.assertEqual(self.client.get(before[1]["url"]).status_code, 404)
        self.assertEqual(self.client.get(f"/api/jobs/{self.identifier}/characters/2").status_code, 404)
        self.assertEqual(self.edit([self.bob["path"]]).status_code, 400)


if __name__ == "__main__":
    unittest.main()
