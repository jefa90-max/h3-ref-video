import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from h3ref import film, frames


class WearableContinuity(unittest.TestCase):
    def setUp(self):
        self.actor = {
            'name': '阿宁', 'look': '短黑发，佩戴细圆框眼镜，浅灰衬衣', 'picture': 'reference.png',
        }
        self.first = {'start': 0, 'end': 5, 'action': '阿宁戴着圆框眼镜坐在木桌边。'}
        self.second = {'start': 5, 'end': 10, 'action': '切镜时阿宁已经摘下圆框眼镜，右手握着合拢的镜腿。'}
        self.script = {'aspect': '9:16', 'cast': [self.actor], 'shots': [self.first, self.second]}
        self.initial_look = '短黑发，佩戴细圆框眼镜，浅灰衬衣'
        self.opening_pose = '坐在桌边，鼻梁与眼周露出，右手握着合拢的镜腿。'

    def composition(self, pose, look=None):
        return {'version': 3, 'camera': '中近景', 'scene': '窗边木桌', 'characters': [
            {'name': '阿宁', 'locked_look': look or self.initial_look, 'placement_and_pose': pose},
        ]}

    def describe(self, shot, composition, **kwargs):
        response = {'choices': [{'message': {'content': json.dumps(composition)}}]}
        with patch.object(frames.config, 'llm_key', return_value='offline'), \
             patch.object(frames.http, 'request', return_value=response):
            return frames.describe_composition(self.script, shot, max_retries=0, **kwargs)

    def test_first_lock_preserves_glasses_only_in_first_opening(self):
        first = self.describe(self.first, self.composition('端坐在木桌边，右手搭在桌面上。'))
        lock = first['characters'][0]['locked_look']
        self.assertEqual(lock, '短黑发，浅灰衬衣')
        text = frames.prompt(self.script, self.first, [self.actor], False, first)
        self.assertIn('戴', text)
        self.assertIn('细圆框眼镜', text)

        second = self.describe(self.second, self.composition(self.opening_pose),
                               previous_composition=first, locked_looks={'阿宁': lock})
        text = frames.prompt(self.script, self.second, [self.actor], False, second)
        self.assertIn('右手握着合拢的镜腿', text)
        self.assertIn('鼻梁与眼周露出', text)
        self.assertNotIn('佩戴细圆框眼镜', text)

    def test_legacy_lock_cannot_override_a_later_cut(self):
        second = self.describe(self.second, self.composition(self.opening_pose, '短发，白色衬衣'),
                               locked_looks={'阿宁': self.initial_look})
        self.assertEqual(second['characters'][0]['locked_look'], '短黑发，浅灰衬衣')
        legacy = self.composition(self.opening_pose)
        text = frames.prompt(self.script, self.second, [self.actor], False, legacy)
        self.assertNotIn('佩戴细圆框眼镜', text)
        self.assertEqual(legacy['characters'][0]['locked_look'], self.initial_look)

    def test_permanent_glasses_and_other_actors_keep_their_look(self):
        other = {'name': '小李', 'look': self.initial_look, 'picture': 'other.png'}
        self.script['cast'].append(other)
        self.second['action'] = '阿宁看向小李。小李摘下眼镜。阿宁仍戴着自己的眼镜。'
        first = self.describe(self.first, self.composition('端坐在木桌边。'))
        self.assertEqual(first['characters'][0]['locked_look'], self.initial_look)
        second = self.describe(self.second, self.composition('端坐在木桌边。'),
                               locked_looks={'阿宁': self.initial_look})
        self.assertIn('佩戴细圆框眼镜', frames.prompt(self.script, self.second, [self.actor], False, second))

    def test_towel_removed_within_shot_is_still_worn_at_opening(self):
        self.first['action'] = '阿宁一把扯下脖子上的深蓝纯棉毛巾，开始擦脸。'
        self.second['action'] = '阿宁继续装牛肉。'
        look = '短黑发，颈上挂着深蓝纯棉毛巾，浅灰衬衣'
        first = self.describe(self.first, self.composition('站在案板后看向前方。', look))
        self.assertEqual(first['characters'][0]['locked_look'], '短黑发，浅灰衬衣')
        self.assertIn('颈上挂着深蓝纯棉毛巾', frames.prompt(self.script, self.first, [self.actor], False, first))
        second = self.describe(self.second, self.composition('将牛肉装袋。', look),
                               locked_looks={'阿宁': first['characters'][0]['locked_look']})
        self.assertNotIn('颈上挂着', frames.prompt(self.script, self.second, [self.actor], False, second))

    def test_other_actors_opening_wear_does_not_restore_this_actors_glasses(self):
        self.script['cast'].append({'name': '小李', 'look': self.initial_look, 'picture': 'other.png'})
        self.second['action'] = '小李戴着眼镜。阿宁已经摘下圆框眼镜，右手握着合拢的镜腿。'
        second = self.describe(self.second, self.composition(self.opening_pose))
        self.assertNotIn('佩戴细圆框眼镜', frames.prompt(self.script, self.second, [self.actor], False, second))

    def test_unnamed_removal_in_adjacent_sentence_uses_the_named_actor(self):
        self.initial_look = '短黑发，外穿蓝色薄外套，内搭米色针织衫'
        self.first['action'] = '阿宁穿蓝外套半跪在木椅前。'
        self.second['action'] = '切镜时阿宁仍穿着蓝外套。在本镜动作中，她向后耸肩脱下外套。'
        third = {'start': 10, 'end': 15, 'action': '阿宁穿着米色针织衫站直，蓝外套拿在手上。'}
        self.script['shots'].append(third)
        first = self.describe(self.first, self.composition('半跪在木椅前。'))
        lock = first['characters'][0]['locked_look']
        self.assertEqual(lock, '短黑发，内搭米色针织衫')
        self.assertIn('外穿蓝色薄外套', frames.prompt(self.script, self.first, [self.actor], False, first))

        second = self.describe(self.second, self.composition('保持半跪。'), locked_looks={'阿宁': lock})
        self.assertIn('外穿蓝色薄外套', frames.prompt(self.script, self.second, [self.actor], False, second))
        final = self.describe(third, self.composition('站直，右手拿着蓝外套。'), locked_looks={'阿宁': lock})
        text = frames.prompt(self.script, third, [self.actor], False, final)
        self.assertIn('右手拿着蓝外套', text)
        self.assertIn('米色针织衫', text)
        self.assertNotIn('外穿蓝色薄外套', text)

    def test_pronoun_does_not_carry_across_a_cut_or_an_ambiguous_sentence(self):
        self.script['cast'].append({'name': '小李', 'look': '短发', 'picture': 'other.png'})
        for action in ('她摘下圆框眼镜。', '阿宁和小李坐在桌边。随后，她摘下圆框眼镜。'):
            with self.subTest(action=action):
                self.second['action'] = action
                first = self.describe(self.first, self.composition('坐在木桌旁。'))
                self.assertEqual(first['characters'][0]['locked_look'], self.initial_look)

    def test_video_compile_does_not_promote_legacy_wearing_to_segment_identity(self):
        segment, = film.plan(self.script['shots'])
        refs = film.refs(self.script, segment, 'shot')
        text = film.compile_prompt(self.script, segment, refs, 1, compositions={
            's1-1': self.composition('坐在桌边。'), 's1-2': self.composition(self.opening_pose),
        })
        self.assertIn('参考图片1只约束阿宁身份：短黑发，浅灰衬衣。', text)
        self.assertIn('戴着圆框眼镜坐在木桌边', text)
        self.assertIn('已经摘下圆框眼镜', text)

    def test_unsubmitted_legacy_automatic_prompt_is_refreshed_from_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            job = film.Job(Path(directory))
            reference = job.dir / 'reference.png'
            reference.write_bytes(b'non-network reference fixture')
            self.actor['picture'] = str(reference)
            segments = film.plan(self.script['shots'])
            refs = {0: [{'kind': 'frame', 'shot': 1}]}
            versions = film.frame_reference_versions(self.script, segments, job.state['frames'])
            old = '旧生图请求：阿宁佩戴细圆框眼镜，同时右手握着合拢的镜腿。'
            job.put('frames', 's1-2', composition=self.composition(self.opening_pose),
                    prompt=old, composition_prompt=old, composition_reference_version=versions['s1-2'])

            def draw(text, images, aspect, output, **kwargs):
                self.assertIn('右手握着合拢的镜腿', text)
                self.assertNotIn('佩戴细圆框眼镜', text)
                output.write_bytes(b'non-network frame fixture')
                return 0.0

            with patch.object(frames.http, 'request', side_effect=AssertionError('composition must use cache')), \
                 patch.object(frames, 'draw', side_effect=draw):
                self.assertEqual(film.stage_frames(job, self.script, segments, refs), [])
            record = job.state['frames']['s1-2']
            self.assertEqual(record['prompt'], record['composition_prompt'])
            self.assertNotIn('佩戴细圆框眼镜', record['prompt'])
