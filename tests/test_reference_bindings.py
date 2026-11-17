"""Regressions for films that describe references without knowing local paths."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from workbench.i18n import InputError
from workbench.inputs import validate
from workbench.references import bind_references, describe_references
from workbench.server import create_app


class ReferenceBindings(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        Image.new("RGB", (8, 8), "red").save(self.uploads / "actor.png")
        Image.new("RGB", (8, 8), "blue").save(self.uploads / "board.png")
        self.script = {"cast": [{"name": "小林", "look": "灰色卫衣"}], "board": None,
                       "setting": "楼道与快递箱", "shots": [{"action": "小林打开箱子"}]}

    def tearDown(self):
        self.temporary.cleanup()

    def payload(self, demo=False, **values):
        return {"kind": "film", "demo": demo, "film": copy.deepcopy(self.script),
                "reference_bindings": {"cast:0": "actor.png"}, **values}

    def test_pathless_cast_is_listed_and_upload_supplies_cli_picture(self):
        with self.assertRaises(InputError):
            validate({"kind": "film", "demo": False, "film": copy.deepcopy(self.script)}, self.root)
        described = describe_references(self.script)
        self.assertEqual([slot["id"] for slot in described["slots"]], ["cast:0", "board"])
        self.assertEqual(described["slots"][0]["description"], "灰色卫衣")
        self.assertFalse(described["slots"][1]["required"])
        self.assertNotIn("picture", self.script["cast"][0])
        for demo in (False, True):
            bound = bind_references(self.payload(demo), self.uploads)
            normalized = validate(bound, self.root)
            self.assertEqual(normalized["film"]["cast"][0]["picture"], str(self.uploads / "actor.png"))
            self.assertIsNone(normalized["film"]["board"])
            self.assertNotIn("reference_bindings", normalized)

    def test_old_paths_are_replaced_without_mutating_story_source(self):
        self.script["cast"][0]["picture"] = "/another-computer/missing.jpg"
        self.script["board"] = "missing-board.png"
        described = describe_references(self.script)
        self.assertNotIn("picture", described["film"]["cast"][0])
        self.assertIs(described["film"]["board"], True)
        self.assertTrue(described["slots"][1]["required"])
        bound = bind_references(self.payload(reference_bindings={"cast:0": "actor.png", "board": "board.png"}), self.uploads)
        normalized = validate(bound, self.root)
        self.assertEqual(normalized["film"]["board"], str(self.uploads / "board.png"))
        self.assertEqual(self.script["board"], "missing-board.png")

    def test_missing_required_slots_report_names_even_if_old_path_exists(self):
        self.script["cast"][0]["picture"] = str(self.uploads / "actor.png")
        self.script["cast"].append({"name": "小陈"})
        self.script["board"] = True
        with self.assertRaises(InputError) as caught:
            bind_references(self.payload(reference_bindings={}), self.uploads)
        error = caught.exception.localized("zh-CN")
        self.assertIn("小林", error)
        self.assertIn("小陈", error)
        self.assertIn("场景", error)

    def test_demo_placeholders_are_explicit_and_never_enable_live_missing_images(self):
        for demo in (False, True):
            with self.assertRaises(InputError):
                bind_references(self.payload(demo, reference_bindings={}), self.uploads)
        with self.assertRaises(InputError):
            bind_references(self.payload(False, reference_bindings={}, demo_placeholders=True), self.uploads)
        normalized = validate(bind_references(self.payload(True, reference_bindings={}, demo_placeholders=True), self.uploads), self.root)
        self.assertIn("demo-placeholders", normalized["film"]["cast"][0]["picture"])

    def test_stale_or_outside_uploads_are_rejected_before_pipeline(self):
        for bindings in ({"cast:7": "actor.png"}, {"cast:0": "gone.png"}, {"cast:0": "../actor.png"}):
            with self.subTest(bindings=bindings), self.assertRaises(InputError):
                bind_references(self.payload(reference_bindings=bindings), self.uploads)
        (self.uploads / "invalid.png").write_text("not an image")
        with self.assertRaises(InputError):
            bind_references(self.payload(reference_bindings={"cast:0": "invalid.png"}), self.uploads)

    def test_invalid_cast_cannot_become_an_empty_upload_checklist(self):
        for cast in ({}, "actor", [None], [{"name": ""}]):
            with self.subTest(cast=cast), self.assertRaises(InputError):
                describe_references({"cast": cast})

    def test_http_upload_binding_reaches_original_film_input_in_both_modes(self):
        import os
        with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
            app = create_app(self.root / "storage", self.root / ".env")
            client = app.test_client()
            headers = {"X-Workbench": "1"}
            jobs = app.extensions["jobs"]
            try:
                parsed = client.post("/api/film/references", json={"film": self.script}, headers=headers)
                self.assertEqual(parsed.status_code, 200)
                image = io.BytesIO()
                Image.new("RGB", (8, 8)).save(image, "PNG")
                image.seek(0)
                uploaded = client.post("/api/uploads", data={"images": (image, "actor.png")}, headers=headers).json["files"][0]
                client.post("/api/config", json={"APIMART_API_KEY": "fake-never-called", "persist": False}, headers=headers)
                for demo in (True, False):
                    with patch.object(jobs, "launch") as launch:
                        response = client.post("/api/jobs", json={"kind": "film", "demo": demo,
                            "operation": "frames", "film": parsed.json["film"],
                            "reference_bindings": {"cast:0": uploaded["id"]},
                            "output_dir": str(self.root / f"out-{demo}")}, headers=headers)
                    self.assertEqual(response.status_code, 201, response.json)
                    launch.assert_called_once()
                    saved = json.loads((Path(response.json["output_dir"]) / "inputs" / "film.json").read_text())
                    self.assertEqual(saved["cast"][0]["picture"], uploaded["path"])
                    jobs.records[response.json["id"]]["status"] = "succeeded"
            finally:
                jobs.close()


if __name__ == "__main__":
    unittest.main()
