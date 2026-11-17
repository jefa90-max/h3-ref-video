import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from h3ref import frames


class FrameStaging(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/staging.json').read_text(encoding='utf-8'))
        self.script = self.fixture['film']

    def test_same_actor_changes_pose_between_shots(self):
        texts = [frames.prompt(self.script, self.script['shots'][i], self.script['cast'], False, self.fixture[key])
                 for i,key in enumerate(('transfer_start','transfer_end'))]
        for text,key in zip(texts, ('transfer_start','transfer_end')):
            for field in ('appearance','position','body_facing','gaze','pose'):
                self.assertIn(self.fixture[key]['characters'][0][field],text)
            self.assertNotIn('正面站立', text)
            self.assertNotIn('毛巾挂脖', text)
        self.assertNotEqual(*texts)

    def test_character_reference_is_identity_only(self):
        text = frames.prompt(self.script, self.script['shots'][0], self.script['cast'],False,self.fixture['transfer_start'])
        self.assertIn('背对镜头',text)
        self.assertIn('参考图片1是阿青',text)
        self.assertNotIn('正面站立',text)
        self.assertNotIn('递出者已经松手',text)

    def test_llm_receives_cast_and_previous_staging(self):
        previous = self.fixture['transfer_start']
        scenes = {'stall': {k:previous[k] for k in ('world_frame','fixed_objects')}}
        response = {'choices':[{'message':{'content':json.dumps(self.fixture['transfer_end'])}}]}
        with patch.object(frames.http,'request',return_value=response) as request, patch.object(frames.config,'llm_key',return_value='offline'):
            composition = frames.describe_composition(self.script,self.script['shots'][1],previous_composition=previous,known_scenes=scenes)
        self.assertEqual(composition,self.fixture['transfer_end'])
        body = request.call_args.args[2]
        context = json.loads(body['messages'][1]['content'])
        self.assertEqual(context['cast'], self.script['cast'])
        self.assertEqual(context['previous_composition'],previous)
        self.assertEqual(context['known_scenes'],scenes)
        self.assertEqual(context['current_shot']['camera'],self.script['shots'][1]['camera'])
        self.assertEqual(request.call_args.kwargs,{'timeout':600,'tries':1})
        bad = copy.deepcopy(response)
        invalid = copy.deepcopy(self.fixture['transfer_end'])
        del invalid['characters'][0]['gaze']
        bad['choices'][0]['message']['content']=json.dumps(invalid)
        with patch.object(frames.http,'request',return_value=bad) as bad_req, patch.object(frames.config,'llm_key',return_value='offline'), self.assertRaisesRegex(frames.CompositionFormatError,'gaze'):
            frames.describe_composition(self.script,self.script['shots'][1])
        self.assertEqual(bad_req.call_count, 4)

    def test_board_optional_reference_numbers_match_upload_order(self):
        for board,first in ((False,1),(True,2)):
            text = frames.prompt(self.script,self.script['shots'][0],list(reversed(self.script['cast'])),board,self.fixture['transfer_start'])
            self.assertIn(f'参考图片{first}是阿禾',text)
            self.assertIn(f'参考图片{first+1}是阿青',text)

    def test_crop_does_not_turn_knot_into_bottom(self):
        text = frames.prompt(self.script,self.script['shots'][0],self.script['cast'],False,self.fixture['reverse_closeup'])
        for phrase in ('朝世界上方','朝世界下方','袋体顶部的袋口处','袋底超出画面下边缘'):
            self.assertIn(phrase,text)

    def test_legacy_composition_stays_readable(self):
        with patch.object(frames.http,'request',side_effect=AssertionError('network')):
            text = frames.prompt(self.script,self.script['shots'][0],self.script['cast'],False,dict(scene='旧场景',frame='旧首帧',orientation='背对镜头'))
        for phrase in ('旧场景','旧首帧','背对镜头'):
            self.assertIn(phrase,text)

    def test_manual_regeneration_keeps_real_prompt_and_invalidates_review(self):
        import tempfile
        from h3ref import film
        from workbench import storyboard
        from workbench.jobs import write_json
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);(out/'inputs').mkdir();(out/'frames').mkdir()
            script=copy.deepcopy(self.script)
            for c in script['cast']:
                c['picture']=str(out/(c['name']+'.png'));Path(c['picture']).write_bytes(b'reference')
            (out/'inputs/film.json').write_text(json.dumps(script))
            request=dict(output_dir=str(out),demo=False,film=script,frames='shot',lang='en')
            previous=dict(prompt='automatic',composition=self.fixture['transfer_start'],composition_prompt='automatic',composition_reference_version='old',cny=0)
            film.Job(out).put('frames','s1-1',**previous)
            write_json(out/'inputs/review.json',{'dirty_segments':[],'confirmed_version':'old'})
            characters=[c['picture'] for c in reversed(script['cast'])]
            write_json(out/'inputs/frame-edit.json',dict(key='s1-1',prompt='User actual words',revision='manual',previous=previous,character_reference_paths=characters))
            def draw(text,refs,aspect,output,**kwargs):
                self.assertEqual(text,'User actual words')
                self.assertEqual(list(map(str,refs)),characters)
                output.write_bytes(b'new frame');return 0.1
            with patch.object(frames,'describe_composition',side_effect=AssertionError('replanned manual text')),patch.object(frames,'draw',side_effect=draw):
                storyboard.regenerate(request)
            record=film.Job(out).state['frames']['s1-1']
            self.assertEqual(record['prompt'],'User actual words')
            self.assertEqual(record['composition_prompt'],'automatic')
            self.assertEqual(storyboard.review_state(out)['dirty_segments'],[1])
            self.assertNotIn('confirmed_version',storyboard.review_state(out))
            self.assertEqual(storyboard.view(request,[],True)[0]['prompt'],'User actual words')
