import copy
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from h3ref import film, frames


class StagingFlow(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/staging.json').read_text(encoding='utf-8'))
        self.script = self.fixture['film']
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.out = Path(self.temporary.name)
        for i,c in enumerate(self.script['cast']):
            c['picture'] = str(self.out / f'actor{i}.png')
            Path(c['picture']).write_bytes(b'original reference')
        self.job = film.Job(self.out)
        self.configure()

    def configure(self, mode='shot'):
        self.segments = film.plan(self.script['shots'])
        self.pictures = {s['idx']:film.refs(self.script,s,mode) for s in self.segments}

    def draw(self, text, references, aspect, output, record, task=None):
        output.write_bytes(b'frame')
        return 0.1

    def run_frames(self, descriptions=None, **kwargs):
        with patch.object(frames,'describe_composition',side_effect=descriptions or [self.fixture['transfer_start'],self.fixture['transfer_end']]) as describe, patch.object(frames,'draw',side_effect=self.draw) as draw:
            failures = film.stage_frames(self.job,self.script,self.segments,self.pictures,**kwargs)
        return failures,describe,draw

    def test_planning_is_ordered_across_segment_boundary(self):
        failures,describe,draw = self.run_frames()
        self.assertEqual(failures,[])
        self.assertEqual(describe.call_args_list[1].kwargs['previous_composition'],self.fixture['transfer_start'])
        self.assertEqual(describe.call_args_list[1].kwargs['known_scenes']['stall']['fixed_objects'],self.fixture['transfer_start']['fixed_objects'])
        self.assertEqual(draw.call_count,2)
        saved = film.Job(self.out).state['frames']['s2-1']
        self.assertEqual(saved['composition_prompt'],saved['prompt'])
        self.assertTrue(saved['composition_reference_version'])

    def test_drawing_starts_before_second_planning_finishes(self):
        started = threading.Event()
        def describe(*args,**kwargs):
            if args[1] == self.script['shots'][1]:
                self.assertTrue(started.wait(3),'drawing waited for whole-film text barrier')
                return self.fixture['transfer_end']
            return self.fixture['transfer_start']
        def draw(*args,**kwargs):
            started.set()
            return self.draw(*args,**kwargs)
        with patch.object(frames,'describe_composition',side_effect=describe),patch.object(frames,'draw',side_effect=draw):
            self.assertEqual(film.stage_frames(self.job,self.script,self.segments,self.pictures),[])

    def test_cut_and_return_keep_world_layout(self):
        self.script['shots'].append(dict(start=16,end=24,action='阿青返回摊位',camera='反打'))
        self.configure()
        failures,describe,_ = self.run_frames([self.fixture[k] for k in ('transfer_start','new_scene','return_scene')])
        self.assertEqual(failures,[])
        scenes = describe.call_args_list[2].kwargs['known_scenes']
        self.assertEqual(set(scenes),{'stall','street'})
        self.assertEqual(scenes['stall']['fixed_objects'],self.fixture['transfer_start']['fixed_objects'])

    def test_planning_failure_does_not_become_previous_state(self):
        self.script['shots'].append(dict(start=16,end=24,action='阿青托袋'))
        self.configure()
        failures,describe,draw = self.run_frames([self.fixture['transfer_start'],ValueError('characters[0].gaze=7'),self.fixture['transfer_end']])
        self.assertEqual(failures,['s2-1'])
        self.assertEqual(draw.call_count,2)
        self.assertEqual(describe.call_args_list[2].kwargs['previous_composition'],self.fixture['transfer_start'])
        self.assertIn('gaze',film.Job(self.out).state['frames']['s2-1']['error'])
        self.assertTrue(film.Job(self.out).state['frames']['s2-1']['prompt'])

    def test_future_cache_does_not_pollute_previous_context(self):
        self.run_frames()
        first = self.job.state['frames'].pop('s1-1')
        (self.out/'frames/s1-1.png').unlink()
        self.job.save()
        _,describe,_ = self.run_frames([self.fixture['transfer_start']])
        self.assertIsNone(describe.call_args.kwargs['previous_composition'])
        self.assertEqual(describe.call_args.kwargs['known_scenes'],{})

    def test_resume_does_not_replan_or_resubmit(self):
        self.job.put('frames','s1-1',prompt='saved actual prompt',task='accepted',error='poll lost',reference_paths=[])
        with patch.object(frames,'describe_composition',return_value=self.fixture['transfer_end']) as describe, patch.object(frames,'draw',side_effect=self.draw) as draw:
            film.stage_frames(self.job,self.script,self.segments,self.pictures,resume_tasks=True)
        self.assertEqual(describe.call_count,1)
        accepted = next(c for c in draw.call_args_list if c.kwargs.get('task'))
        self.assertEqual(accepted.args[0],'saved actual prompt')
        self.assertEqual(accepted.kwargs['task'],'accepted')

    def test_crash_after_planning_reuses_saved_text(self):
        def crash(*args,**kwargs):
            raise KeyboardInterrupt()
        with patch.object(frames,'describe_composition',return_value=self.fixture['transfer_start']),patch.object(frames,'draw',side_effect=crash),self.assertRaises(KeyboardInterrupt):
            film.stage_frames(self.job,self.script,self.segments,self.pictures)
        saved = film.Job(self.out).state['frames']['s1-1']['prompt']
        self.job = film.Job(self.out)
        with patch.object(frames,'describe_composition',side_effect=AssertionError('paid text again')),patch.object(frames,'draw',side_effect=self.draw) as draw:
            self.assertEqual(film.stage_frames(self.job,self.script,self.segments,self.pictures),[])
        self.assertEqual(draw.call_args_list[0].args[0],saved)

    def test_one_opening_per_wanted_shot_and_no_tail(self):
        self.script['shots'] = [dict(start=i*3,end=i*3+3,action='阿青托袋') for i in range(4)]
        self.configure()
        _,_,draw = self.run_frames([copy.deepcopy(self.fixture['transfer_start']) for _ in range(4)])
        self.assertEqual(draw.call_count,4)
        self.assertEqual(set(self.job.state['frames']),{'s1-1','s1-2','s1-3','s1-4'})
        for call in draw.call_args_list:
            self.assertEqual(list(map(str,call.args[1])),[self.script['cast'][0]['picture']])
        self.assertEqual(list(self.out.glob('**/*last*')),[])

    def test_first_none_and_nine_image_limit(self):
        for mode in ('first','none','shot'):
            self.script['shots']=[dict(start=i,end=i+1,action='阿青阿禾托袋') for i in range(12)]
            self.script['board']=str(self.out/'board.png')
            self.configure(mode)
            expected=sum(p['kind']=='frame' for pics in self.pictures.values() for p in pics)
            with tempfile.TemporaryDirectory() as tmp:
                self.job=film.Job(Path(tmp))
                _,describe,draw=self.run_frames([copy.deepcopy(self.fixture['transfer_start']) for _ in range(expected)])
            self.assertEqual(draw.call_count,expected)
            self.assertEqual(describe.call_count,expected)
            self.assertLessEqual(expected,7)

    def test_cli_reports_partial_failure_with_nonzero_exit(self):
        module_spec = importlib.util.spec_from_file_location('h3ref_cli',Path(__file__).parents[1]/'h3ref.py')
        cli = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(cli)
        self.job.state['frame_failures']=['s1-1']; self.job.save()
        with patch.object(film,'run',return_value=self.out),patch.object(sys,'argv',['h3ref','film',str(self.out)]),self.assertRaisesRegex(SystemExit,'FRAME_BATCH_INCOMPLETE'):
            cli.main()
