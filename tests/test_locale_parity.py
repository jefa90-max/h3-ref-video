import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class LocaleParityTest(unittest.TestCase):
    def _assert_mapping_parity(self, zh_map: dict, en_map: dict, label: str):
        self.assertEqual(set(zh_map.keys()), set(en_map.keys()), f"Key mismatch in {label}")
        for key in sorted(zh_map.keys()):
            zh_val, en_val = zh_map[key], en_map[key]
            self.assertEqual(type(zh_val), type(en_val), f"Type mismatch at {label}.{key}")
            if isinstance(zh_val, dict):
                self._assert_mapping_parity(zh_val, en_val, f"{label}.{key}")
            elif isinstance(zh_val, str):
                self.assertTrue(zh_val != "" or en_val == "", f"Unexpected empty string at {label}.{key}")
                self.assertEqual(
                    set(PLACEHOLDER_RE.findall(zh_val)),
                    set(PLACEHOLDER_RE.findall(en_val)),
                    f"Placeholder mismatch at {label}.{key}: {zh_val!r} vs {en_val!r}",
                )

    def test_all_four_locale_suites_have_matching_keys_and_placeholders(self):
        pairs = [
            ("h3ref", ROOT / "h3ref/messages/zh-CN.json", ROOT / "h3ref/messages/en.json"),
            ("workbench", ROOT / "workbench/messages/zh-CN.json", ROOT / "workbench/messages/en.json"),
            ("static", ROOT / "workbench/static/messages/zh-CN.json", ROOT / "workbench/static/messages/en.json"),
        ]
        for label, zh_path, en_path in pairs:
            zh_data = json.loads(zh_path.read_text(encoding="utf-8"))
            en_data = json.loads(en_path.read_text(encoding="utf-8"))
            self._assert_mapping_parity(zh_data, en_data, label)
            if label == "static":
                self.assertIn("player", zh_data)
                self._assert_mapping_parity(zh_data["player"], en_data["player"], "static.player")
                # Also assert Phase 4 new UI keys exist
                for required_key in (
                    "optimizeStoryboard",
                    "optimizeStoryboardHint",
                    "filmCastGroupSummary",
                    "mergeSelectedCast",
                    "splitCastGroup",
                    "segmentRewriteH3Live",
                    "rewriteMissingPrompts",
                    "rewriteAllPrompts",
                    "frameReferencesAtLimit",
                ):
                    self.assertIn(required_key, zh_data)
                    self.assertIn(required_key, en_data)
                for tpl in (ROOT / "workbench/templates").glob("*.html"):
                    html = tpl.read_text(encoding="utf-8")
                    for attr_val in re.findall(r'data-i18n(?:-\w+)?="([A-Za-z0-9_]+)"', html):
                        self.assertIn(attr_val, zh_data, f"Missing {attr_val} in zh-CN from {tpl.name}")
                        self.assertIn(attr_val, en_data, f"Missing {attr_val} in en from {tpl.name}")


if __name__ == "__main__":
    unittest.main()
