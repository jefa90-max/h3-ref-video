import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from h3ref import staging


class Staging(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/staging.json').read_text(encoding='utf-8'))
        self.script = self.fixture['film']
        self.composition = self.fixture['transfer_start']

    def validate(self, value, scenes=None):
        return staging.validate_composition(value, self.script, self.script['shots'][0], scenes)

    def test_prop_parts_survive_bottom_crop(self):
        text = staging.render_opening(self.fixture['reverse_closeup'])
        for expected in ('朝世界上方', '朝世界下方', '袋体顶部的袋口处', '袋底超出画面下边缘'):
            self.assertIn(expected, text)

    def test_opening_excludes_transfer_result(self):
        self.assertNotIn('递出者已经松手', staging.render_opening(self.composition))
        self.assertIn('递出者已经松手', staging.render_motion(self.composition))
        self.assertIn('阿青', staging.render_opening(self.composition))

    def test_invalid_state_reports_path_and_value(self):
        self.composition['characters'][0]['gaze'] = 7
        for lang in ('zh-CN', 'en'):
            with patch.dict('os.environ', {'H3_LANG': lang}), self.assertRaisesRegex(ValueError, r'characters\[0\].gaze.*7'):
                self.validate(self.composition)

    def test_known_scene_layout_cannot_change_on_reverse(self):
        scenes = {'stall': {key: self.composition[key] for key in ('world_frame','fixed_objects')}}
        changed = copy.deepcopy(self.composition)
        changed['fixed_objects'][0]['position'] = '世界左侧'
        with self.assertRaisesRegex(ValueError, 'fixed_objects'):
            self.validate(changed, scenes)
        self.validate(self.fixture['new_scene'], scenes)

    def test_empty_character_prop_closeup_and_backwards_gaze(self):
        self.composition['characters'] = []
        self.composition['expected_end']['characters'] = []
        self.validate(self.composition)
        self.validate(self.fixture['transfer_end'])

    def test_types_required_arrays_duplicates_and_aliases(self):
        for path, value in [('props', None), ('version', True), ('camera', {}), ('expected_end', {})]:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, path):
                self.validate({**self.composition, path: value})
        self.composition['characters'][0]['name'] = '小青'
        normalized = self.validate(self.composition)
        self.assertEqual(normalized['characters'][0]['name'], '阿青')
        self.assertEqual(self.composition['characters'][0]['name'], '小青')
        self.composition['characters'].append(copy.deepcopy(normalized['characters'][0]))
        with self.assertRaisesRegex(ValueError, 'name'):
            self.validate(self.composition)

    def test_visibility_and_nullable_parts(self):
        self.composition['props'][0]['opening_direction'] = None
        self.validate(self.composition)
        self.composition['props'][0]['visibility'] = 'invisible'
        with self.assertRaisesRegex(ValueError, 'visibility'):
            self.validate(self.composition)

    def test_localized_labels_and_placeholders(self):
        from h3ref.i18n import MESSAGES
        for lang in ('zh-CN','en'):
            with patch.dict('os.environ', {'H3_LANG': lang}):
                text = staging.render_motion(self.composition)
                self.assertIn(MESSAGES[lang]['staging_world'], text)
                self.assertIn(MESSAGES[lang]['staging_expected_end'], text)
        self.assertNotIn(self.composition['orientation'], staging.render_opening(self.composition).splitlines())
