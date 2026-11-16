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
        import os
        env_patch = patch.dict(os.environ, {'PATH': os.environ.get('PATH', '')}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        which_patch = patch('shutil.which', return_value='/fake/ffmpeg')
        which_patch.start()
        self.addCleanup(which_patch.stop)
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

    def test_extra_camera_metadata_does_not_prevent_frame_drawing(self):
        self.script['shots'] = self.script['shots'][:1]
        self.configure()
        composition = copy.deepcopy(self.fixture['transfer_start'])
        composition['camera']['movement'] = 'slow push in'
        failures, _, draw = self.run_frames([composition])
        self.assertEqual(failures, [])
        self.assertEqual(draw.call_count, 1)
        self.assertTrue((self.out / 'frames/s1-1.png').is_file())
        saved = film.Job(self.out).state['frames']['s1-1']
        self.assertFalse(saved.get('error'))
        self.assertIn(composition['camera']['position'], saved['prompt'])

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

    def test_video_uses_matching_shot_states_without_global_pose(self):
        self.script['shots'][0]['end']=4
        self.script['shots'][1].update(start=4,end=8)
        self.configure()
        compositions={'s1-1':self.fixture['transfer_start'],'s1-2':self.fixture['transfer_end']}
        text=film.compile_prompt(self.script,self.segments[0],self.pictures[0],1,compositions=compositions)
        self.assertNotIn('正面站立',text)
        self.assertNotIn('毛巾挂脖',text)
        self.assertNotIn('场景与关键道具（全片统一）',text)
        self.assertIn('接好了',text)
        self.assertIn('递出者已经松手',text)
        first,second=text.split('分镜：\n')[1].split('4–8秒：')
        self.assertIn(self.fixture['transfer_start']['characters'][0]['pose'],first)
        self.assertIn(self.fixture['transfer_end']['characters'][0]['pose'],second)
        self.assertIn('双手托住红袋，阿青随后松手',first)

    def test_manual_prompt_change_excludes_stale_composition(self):
        self.run_frames()
        records=self.job.state['frames']
        versions=film.frame_reference_versions(self.script,self.segments,records)
        self.assertEqual(len(film.saved_compositions(records,reference_versions=versions)),2)
        records['s1-1']['prompt']='手工改词'
        self.assertEqual(set(film.saved_compositions(records,reference_versions=versions)),{'s2-1'})
        records['s2-1'].pop('composition_prompt')
        self.assertEqual(film.saved_compositions(records,reference_versions=versions),{})

    def test_reference_edit_preserves_uploaded_order(self):
        self.run_frames()
        records=self.job.state['frames']
        records['s1-1']['character_reference_paths']=[c['picture'] for c in reversed(self.script['cast'])]
        versions=film.frame_reference_versions(self.script,self.segments,records)
        self.assertNotIn('s1-1',film.saved_compositions(records,reference_versions=versions))
        Path(self.script['cast'][1]['picture']).write_bytes(b'changed reference version')
        versions=film.frame_reference_versions(self.script,self.segments,records)
        self.assertEqual(film.saved_compositions(records,reference_versions=versions),{})

    def test_mixed_legacy_and_v2_do_not_force_migration(self):
        self.script['shots'][0]['end']=4;self.script['shots'][1].update(start=4,end=8);self.configure()
        original=film.compile_prompt(self.script,self.segments[0],self.pictures[0],1)
        self.assertEqual(original,film.compile_prompt(self.script,self.segments[0],self.pictures[0],1,compositions={}))
        mixed=film.compile_prompt(self.script,self.segments[0],self.pictures[0],1,compositions={'s1-1':self.fixture['transfer_start']})
        self.assertIn(self.script['shots'][1]['action'],mixed)
        self.assertNotIn('场景与关键道具（全片统一）',mixed)
        self.assertEqual(mixed.count(self.script['setting']),1)

    def test_preview_and_preflight_do_not_replan(self):
        from workbench import inputs,storyboard
        self.run_frames()
        (self.out/'inputs').mkdir(exist_ok=True)
        (self.out/'inputs/film.json').write_text(json.dumps(self.script))
        request=dict(kind='film',film=self.script,output_dir=str(self.out),demo=False,frames='shot',operation='pipeline',lang='zh-CN')
        with patch.object(film.rewrite,'rewrite',return_value='cached h3'):
            film.stage_prompts(self.job,self.script,self.segments,self.pictures)
        for forbidden in (frames.http,'request'),(frames,'describe_composition'),(frames,'draw'),(film.rewrite,'rewrite'):
            context=patch.object(*forbidden,side_effect=AssertionError('network on preview'))
            context.start();self.addCleanup(context.stop)
        inputs.preflight(request,{'RUNNINGHUB_API_KEY':'offline'})
        view=storyboard.view(request,[],True)
        self.assertEqual(view[0]['prompt'],self.job.state['frames']['s1-1']['prompt'])
        film.stage_prompts(self.job,self.script,self.segments,self.pictures)

    def test_existing_task_unknown_and_completed_prompts_are_frozen(self):
        for flags in ({'task':'accepted'},{'submission_unknown':True},{'file':str(self.out/'clips/seg1.mp4'),'cny':1}):
            with self.subTest(flags=flags):
                self.job.state['segs']={'0':dict(zh='original zh',h3='original h3',**flags),'1':dict(zh='other',h3='other',task='other-task')}
                self.job.save()
                with patch.object(film.rewrite,'rewrite',side_effect=AssertionError('repaid rewrite')):
                    film.stage_prompts(self.job,self.script,self.segments,self.pictures)
                self.assertEqual(self.job.state['segs']['0']['zh'],'original zh')
                self.assertFalse((self.out/'prompts/seg1.zh.txt').exists())

    def test_rewrite_request_retains_staging_rules_with_mock_backend(self):
        from h3ref import rewrite
        text=film.compile_prompt(self.script,self.segments[0],self.pictures[0],2,compositions={'s1-1':self.fixture['transfer_start']})
        with patch.object(rewrite,'guide',return_value='local offline guide'):
            request=rewrite.request(text,len(self.pictures[0]),8,'9:16')
        self.assertIn('body facing',request)
        self.assertIn('opening direction',request)
        self.assertIn(self.fixture['transfer_start']['action'],request)
