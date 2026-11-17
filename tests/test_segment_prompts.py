"""Local prompt edits exercise actual state, rewrite and RH graph boundaries without paid calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from h3ref import film, frames, media, rewrite, rh
from workbench import segments, storyboard
from workbench.i18n import InputError
from workbench.jobs import write_json
from workbench.server import create_app


def h3(text='The recipient stands in front of the counter.',picture=True):
    parts=dict(subject_definitions='<Subject 1> Person '+('<Picture 1>' if picture else ''),summary=text,
               retention_analysis='Identity and action.',detailed_description='[Shot 1] '+text,
               overall_soundscape='Footsteps.',non_diegetic_music='N/A')
    return '\n\n'.join(name+':\n'+parts[name] for name in rewrite.SECTIONS)


class SegmentPrompts(unittest.TestCase):
    def setUp(self):
        import os
        env_patch = patch.dict(os.environ, {'PATH': os.environ.get('PATH', '')}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        which_patch = patch('workbench.segments.shutil.which', return_value='/fake/ffmpeg')
        which_patch.start()
        self.addCleanup(which_patch.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name).resolve();self.out=root/'output';self.out.mkdir()
        for folder in ('inputs','clips','prompts'):(self.out/folder).mkdir()
        actor=self.out/'actor.png';actor.write_bytes(b'original actor')
        self.script=dict(aspect='9:16',cast=[dict(name='recipient',picture=str(actor))],shots=[dict(start=i*8,end=(i+1)*8,action='recipient stands') for i in range(3)])
        self.request=dict(kind='film',demo=False,frames='none',lang='en',output_dir=str(self.out),film=self.script,operation='pipeline')
        self.job=film.Job(self.out)
        for i in range(3):
            path=self.out/f'clips/seg{i+1}.mp4';path.write_bytes(f'OLD CLIP {i+1}'.encode())
            (self.out/f'clips/seg{i+1}-last.jpg').write_bytes(f'OLD TAIL {i+1}'.encode())
            self.job.put('segs',str(i),zh='recipient stands',h3=h3(),file=str(path),cny=1,task=f'old-{i}')
            (self.out/f'prompts/seg{i+1}.zh.txt').write_text('recipient stands')
            (self.out/f'prompts/seg{i+1}.h3.txt').write_text(h3())
        (self.out/'film.mp4').write_bytes(b'OLD FILM')
        write_json(self.out/'inputs/film.json',self.script)
        write_json(self.out/'inputs/request.json',self.request)
        review=storyboard.review_status(self.request,[])
        write_json(self.out/'inputs/review.json',dict(dirty_segments=[],confirmed_version=review['version']))
        self.app=create_app(root/'storage',root/'.env');self.jobs=self.app.extensions['jobs'];self.addCleanup(self.jobs.close)
        self.identifier='job-1';self.client=self.app.test_client();self.headers={'X-Workbench':'1','Accept-Language':'en'}
        self.jobs.records[self.identifier]=dict(id=self.identifier,kind='film',demo=False,status='succeeded',operation='pipeline',output_dir=str(self.out),lang='en',created_at=1,error=None)
        self.url='/api/jobs/job-1/segments/2/regenerate'

    def card(self,number=2):
        response=self.client.get('/api/jobs/job-1')
        self.assertEqual(response.status_code,200,response.json)
        return response.json['segments'][number-1]

    def prepare(self,prompt='recipient kneels',format='source',number=2):
        card=self.card(number)
        self.request.update(operation='segment',segment=number)
        self.request['segment_revision']=segments.prepare_regeneration(self.request,number,card['version'],prompt=prompt,prompt_format=format,prompt_version=card['prompt_version'])
        return self.request['segment_revision']

    def generate(self,poll=None,submit=None):
        def download(url,path):path.write_bytes(b'NEW CLIP')
        def last_frame(path,out):out.write_bytes(b'NEW TAIL')
        mocks=[patch.object(rewrite,'backend',return_value='llm'),patch.object(rewrite,'guide',return_value='local offline guide'),
               patch.object(rewrite,'_llm',return_value=h3('The recipient kneels beside the counter.')),
               patch.object(rh,'upload',side_effect=lambda path:'uploaded-'+Path(path).name),
               patch.object(rh,'submit_with_retry',side_effect=submit or (lambda graph,**kw:'new-task')),
               patch.object(rh,'poll',**(poll or {'return_value':dict(state='done',url='offline://clip',cny=2)})),
               patch.object(film.http,'download',side_effect=download),patch.object(media,'last_frame',side_effect=last_frame),patch.object(media,'duration',return_value=8)]
        from contextlib import ExitStack
        with ExitStack() as stack:
            active=[stack.enter_context(m) for m in mocks]
            segments.regenerate(self.request)
        return active

    def test_invalidation_preserves_prompt_override_and_pairing(self):
        override=dict(format='source',text='edited',source_version='original-source')
        self.job.put('segs','1',prompt_override=override,h3_source='edited',h3_reference_version='refs-original')
        film.invalidate_segments(self.job,'shot',[2])
        saved=film.Job(self.out).state['segs']['1']
        self.assertEqual(saved['prompt_override'],override)
        self.assertEqual(saved['h3_source'],'edited')
        self.assertEqual(saved['h3_reference_version'],'refs-original')
        self.assertNotIn('task',saved)

    def test_initial_terminal_failure_cannot_be_repaid_by_resume(self):
        segment=film.plan(self.script['shots'])[1];pics=film.refs(self.script,segment,'none')
        (self.out/'clips/seg2.mp4').unlink()
        self.job.state['segs']['1'].pop('task');self.job.save()
        with patch.object(rh,'upload',return_value='upload'),patch.object(rh,'submit_with_retry',return_value='paid-task'),patch.object(rh,'poll',return_value={'state':'failed','error':'remote terminal'}):
            with self.assertRaises(RuntimeError):film.gen_segment(self.job,self.script,segment,pics)
        with patch.object(rh,'submit_with_retry',side_effect=AssertionError('charged twice')),self.assertRaisesRegex(RuntimeError,'terminal|TERMINAL'):
            film.gen_segment(film.Job(self.out),self.script,segment,pics)
        self.assertTrue(film.Job(self.out).state['segs']['1']['terminal_failed'])

    def test_h3_picture_zero_and_upper_bound_rejected(self):
        for number in (0,3):
            text=h3().replace('<Picture 1>',f'<Picture {number}>')
            self.assertTrue(rewrite.problems(text,2),text)
        self.assertEqual(rewrite.problems(h3(),1),[])
        self.assertEqual(rewrite.problems(h3(picture=False),0),[])

    def test_publishing_keeps_previous_version_visible_until_atomic_commit(self):
        before=self.card();old_url=before['url'];old_version=before['version']
        self.request.update(operation='segment',segment=2)
        self.request['segment_revision']=segments.prepare_regeneration(self.request,2,old_version)
        publish=segments.publish_file
        def interrupted(source,target):
            if Path(target)==self.out/'clips/seg2-last.jpg':raise OSError('tail replacement interrupted')
            return publish(source,target)
        with patch.object(segments,'publish_file',side_effect=interrupted),self.assertRaises(OSError):self.generate()
        with self.client.get(old_url) as response:
            self.assertEqual(response.data,b'OLD CLIP 2')
        self.assertEqual(self.card()['version'],old_version)
        self.assertFalse(segments.assembly_status(self.request)['pending'])
        active=self.generate()
        active[3].assert_not_called();active[4].assert_not_called()
        with self.client.get(old_url) as response:
            self.assertEqual(response.data,b'OLD CLIP 2')
        with self.client.get(self.card()['url']) as response:
            self.assertEqual(response.data,b'NEW CLIP')
        self.assertTrue(segments.assembly_status(self.request)['pending'])

    def test_media_version_mismatch_never_returns_other_bytes(self):
        url=self.card()['url'].split('?')[0]+'?v=obsolete'
        self.assertEqual(self.client.get(url).status_code,404)

    def test_segment_snapshot_exposes_saved_prompts_without_network(self):
        with patch.object(frames.http,'request',side_effect=AssertionError('snapshot network')),patch.object(rh,'upload',side_effect=AssertionError('snapshot upload')):
            card=self.card()
        self.assertEqual(card['source_prompt'],'recipient stands')
        self.assertEqual(card['h3_prompt'],h3())
        self.assertEqual(card['prompt_format'],'source')
        self.assertTrue(card['prompt_version'])
        self.assertTrue(card['editable'])
        self.assertEqual([r['number'] for r in card['reference_images']],[1,2])

    def test_edited_source_is_rewritten_before_video_submission(self):
        other=(self.out/'clips/seg1.mp4').read_bytes();final=(self.out/'film.mp4').read_bytes()
        self.prepare()
        captured=[]
        self.generate(submit=lambda graph,**kw:captured.append(graph) or 'edited-task')
        actual=captured[0][rh.PROMPT]['inputs']['prompt']
        self.assertIn('The recipient kneels beside the counter.',actual)
        self.assertNotIn('The recipient stands in front of the counter.',actual)
        self.assertEqual((self.out/'clips/seg1.mp4').read_bytes(),other)
        self.assertEqual((self.out/'film.mp4').read_bytes(),final)
        self.assertTrue(segments.assembly_status(self.request)['pending'])
        self.assertEqual(film.Job(self.out).state['segs']['1']['prompt_override']['text'],'recipient kneels')
        self.assertEqual((self.out/'prompts/seg2.zh.txt').read_text(),'recipient kneels')
        self.assertEqual(self.card()['source_prompt'],'recipient kneels')

    def test_unchanged_source_reuses_h3_without_text_key(self):
        self.prepare('recipient stands')
        segments.preflight(self.request,{'RUNNINGHUB_API_KEY':'offline'})
        active=self.generate()
        active[2].assert_not_called()

    def test_direct_h3_is_not_paired_with_stale_source(self):
        direct=h3('The recipient sits beside the pot.')
        previous_url=next(a['url'] for a in self.jobs.snapshot(self.identifier)['artifacts'] if a['name']=='prompts/seg2.zh.txt')
        self.prepare(direct,'h3')
        segments.preflight(self.request,{'RUNNINGHUB_API_KEY':'offline'})
        captured=[];active=self.generate(submit=lambda graph,**kw:captured.append(graph) or 'direct-task')
        active[2].assert_not_called()
        self.assertEqual(captured[0][rh.PROMPT]['inputs']['prompt'],direct)
        card=self.card()
        self.assertEqual(card['source_prompt'],'')
        with self.client.get(previous_url) as response:
            self.assertEqual(response.get_data(as_text=True),'recipient stands')
        self.assertEqual(card['h3_prompt'],direct)
        self.assertEqual(card['prompt_format'],'h3')
        self.assertTrue(card['automatic_source_prompt'])
        self.assertEqual((self.out/'prompts/seg2.zh.txt').read_text(),'')
        self.prepare('new source after H3','source')
        active=self.generate()
        active[2].assert_called_once()

    def test_h3_only_legacy_edit_validates_before_submission(self):
        self.job.state['segs']['1'].pop('zh');self.job.save();(self.out/'prompts/seg2.zh.txt').unlink()
        self.assertEqual(self.card()['prompt_format'],'h3')
        for text in ('incomplete',h3().replace('<Picture 1>','<Picture 0>'),h3().replace('<Picture 1>','<Picture 3>')):
            before=(self.out/'state.json').read_bytes()
            card=self.card()
            with self.subTest(text=text),self.assertRaises(ValueError):
                segments.prepare_regeneration(self.request,2,card['version'],prompt=text,prompt_format='h3',prompt_version=card['prompt_version'])
            self.assertEqual((self.out/'state.json').read_bytes(),before)
            self.assertFalse((self.out/'inputs/segment-edit.json').exists())

    def test_http_validation_uses_problem_json_and_has_no_side_effect(self):
        card=self.card()
        base=dict(version=card['version'],prompt_version=card['prompt_version'],prompt='new source',prompt_format='source')
        for field,value in [('prompt',''),('prompt',7),('prompt_format','unknown'),('prompt_version','obsolete'),('prompt',None)]:
            for lang in ('en','zh-CN'):
                with self.subTest(field=field,value=value,lang=lang),patch.object(self.jobs,'launch') as launch:
                    response=self.client.post(self.url,json={**base,field:value},headers={**self.headers,'Accept-Language':lang})
                self.assertEqual(response.status_code,400,response.json)
                self.assertEqual(response.mimetype,'application/problem+json')
                self.assertEqual(response.json['field'],field)
                self.assertTrue(response.json['doc_anchor'])
                self.assertEqual(response.json['error'],response.json['detail'])
                launch.assert_not_called()
                self.assertFalse((self.out/'inputs/segment-edit.json').exists())
        response=self.client.post('/api/jobs/job-1/segments/99/regenerate',json=base,headers=self.headers)
        self.assertEqual(response.status_code,400)

    def test_new_source_requires_text_key_before_writing_pending(self):
        import os
        root = self.out.parent
        for provider, expected in (('apimart', 'LLM_API_KEY / APIMART_API_KEY'), ('gemini', 'GEMINI_API_KEY')):
            with self.subTest(provider=provider), patch.dict('os.environ', {'PATH': os.environ.get('PATH', ''), 'RUNNINGHUB_API_KEY': 'offline-rh', 'LLM_PROVIDER': provider}, clear=True), patch('workbench.segments.shutil.which', return_value='/fake/ffmpeg'):
                app = create_app(root / f'storage-{provider}', root / f'.env-{provider}')
                jobs = app.extensions['jobs']
                try:
                    jobs.records[self.identifier] = dict(self.jobs.records[self.identifier])
                    client = app.test_client()
                    card = client.get('/api/jobs/job-1').json['segments'][1]
                    with patch.object(jobs, 'launch') as launch:
                        response = client.post(self.url, json=dict(version=card['version'], prompt_version=card['prompt_version'], prompt='new source', prompt_format='source'), headers=self.headers)
                    self.assertEqual(response.status_code, 400, response.json)
                    self.assertEqual(response.json['code'], 'keys')
                    self.assertIn(expected, response.json['detail'])
                    self.assertFalse((self.out / 'inputs/segment-edit.json').exists())
                    launch.assert_not_called()
                finally:
                    jobs.close()

    def test_accepted_task_only_resumes_original_and_reuses_rewrite(self):
        self.prepare()
        with self.assertRaisesRegex(RuntimeError,'poll disconnected'):
            self.generate(poll={'side_effect':RuntimeError('poll disconnected')})
        failed=self.card()
        self.assertTrue(failed['task_pending'])
        self.assertFalse(failed['can_regenerate'])
        self.assertEqual(failed['source_prompt'],'recipient stands')
        self.assertEqual(failed['prompt'],'recipient kneels')
        with self.assertRaises(ValueError):self.prepare('replace an accepted task')
        active=self.generate()
        active[2].assert_not_called();active[3].assert_not_called();active[4].assert_not_called()
        active[5].assert_called_once_with('new-task')

    def test_unknown_submission_cannot_be_replaced_even_without_task(self):
        self.job.put('segs','1',task=None,submission_unknown=True)
        for legacy in (False,True):
            card=self.card()
            with self.assertRaises(ValueError):
                if legacy:segments.prepare_regeneration(self.request,2,card['version'])
                else:self.prepare('unsafe replacement')
        self.assertTrue(self.card()['submission_unknown'])

    def test_terminal_failure_without_video_is_manually_editable(self):
        (self.out/'clips/seg2.mp4').unlink()
        self.job.put('segs','1',terminal_failed=True,error='terminal',file=None)
        card=self.card()
        self.assertTrue(card['can_regenerate'])
        self.assertIsNone(card['version'])
        self.assertEqual(card['source_prompt'],'')
        self.assertEqual(card['h3_prompt'],'')
        self.assertEqual(card['prompt'],'recipient stands')
        self.prepare()
        self.generate()
        self.assertTrue(self.card()['url'])

    def test_terminal_attempt_resume_does_not_submit_new_revision(self):
        self.prepare()
        with self.assertRaises(RuntimeError):self.generate(poll={'return_value':{'state':'failed','error':'terminal'}})
        self.assertFalse(self.card()['task_pending'])
        self.assertTrue(self.card()['can_regenerate'])
        with patch.object(rh,'submit_with_retry',side_effect=AssertionError('duplicate charge')),self.assertRaises(RuntimeError):segments.regenerate(self.request)
        previous_revision=self.request['segment_revision']
        self.prepare('corrected source')
        self.assertNotEqual(self.request['segment_revision'],previous_revision)
        active=self.generate();active[4].assert_called_once()

    def test_changed_source_flag_is_per_segment_and_override_survives_invalidation(self):
        self.prepare();self.generate()
        self.assertFalse(self.card()['source_changed'])
        self.script['shots'][1]['action']='recipient turns around'
        self.request['film']=self.script;write_json(self.out/'inputs/request.json',self.request)
        card=self.card()
        self.assertTrue(card['source_changed'])
        self.assertEqual(card['prompt'],'recipient kneels')
        job=film.Job(self.out);film.invalidate_segments(job,'shot',[2])
        planned=film.plan(self.script['shots']);pics={s['idx']:film.refs(self.script,s,'none') for s in planned}
        with patch.object(rewrite,'rewrite',side_effect=AssertionError('override changed')):
            film.stage_prompts(job,self.script,planned,pics)
        self.assertEqual(job.state['segs']['1']['zh'],'recipient kneels')

    def test_legacy_version_only_request_still_works(self):
        with patch('workbench.jobs.preflight'),patch.object(self.jobs,'launch'):
            response=self.client.post(self.url,json={'version':self.card()['version']},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)

    def test_unexpected_prepare_error_has_safe_correlation_and_log(self):
        card=self.card()
        with patch('workbench.jobs.preflight'),patch.object(segments,'prepare_regeneration',side_effect=OSError('disk error key=VERY_SECRET')),patch.object(self.jobs,'launch') as launch:
            self.jobs.secrets.add('VERY_SECRET')
            response=self.client.post(self.url,json=dict(version=card['version'],prompt_version=card['prompt_version'],prompt='recipient stands',prompt_format='source'),headers=self.headers)
        self.assertEqual(response.status_code,500)
        self.assertEqual(response.mimetype,'application/problem+json')
        self.assertTrue(response.json['instance'])
        self.assertNotIn('VERY_SECRET',response.get_data(as_text=True))
        launch.assert_not_called()

    def test_unresolved_attempt_on_other_segment_cannot_be_discarded(self):
        self.prepare()
        with self.assertRaises(RuntimeError):self.generate(poll={'side_effect':RuntimeError('offline')})
        previous=(self.out/'inputs/segment-edit.json').read_bytes()
        with self.assertRaises(ValueError):self.prepare(number=1)
        self.assertEqual((self.out/'inputs/segment-edit.json').read_bytes(),previous)

    def test_same_source_with_changed_reference_requires_new_pair(self):
        self.prepare('recipient stands');self.generate()
        old=film.Job(self.out).state['segs']['1']['h3_reference_version']
        Path(self.script['cast'][0]['picture']).write_bytes(b'new actor reference')
        self.prepare('recipient stands')
        with self.assertRaises(InputError):segments.preflight(self.request,{'RUNNINGHUB_API_KEY':'offline'})
        self.generate()
        current=film.Job(self.out).state['segs']['1']['h3_reference_version']
        self.assertNotEqual(old,current)

    def test_source_override_rewrites_when_frame_reference_changes_in_pipeline(self):
        self.prepare('recipient stands');self.generate()
        Path(self.script['cast'][0]['picture']).write_bytes(b'changed identity reference')
        job=film.Job(self.out);film.invalidate_segments(job,'shot',[2])
        planned=film.plan(self.script['shots']);pics={s['idx']:film.refs(self.script,s,'none') for s in planned}
        with patch.object(rewrite,'rewrite',return_value='new paired h3') as rewriting:
            film.stage_prompts(job,self.script,planned,pics)
        rewriting.assert_called_once()
        self.assertEqual(job.state['segs']['1']['h3'],'new paired h3')

    def test_snapshot_reads_state_and_pending_once_and_compiles_each_segment_once(self):
        self.script['shots']=[dict(start=i*1.5,end=(i+1)*1.5,action='recipient moves') for i in range(1000)]
        write_json(self.out/'inputs/film.json',self.script);write_json(self.out/'inputs/request.json',self.request)
        reader=segments.read_json;counts={'state.json':0,'segment-edit.json':0}
        def counted(path,default):
            if path.name in counts:counts[path.name]+=1
            return reader(path,default)
        with patch.object(segments,'read_json',side_effect=counted),patch.object(film,'compile_prompt',wraps=film.compile_prompt) as compiled,patch.object(frames.http,'request',side_effect=AssertionError('network')):
            result=self.jobs.snapshot(self.identifier)
        self.assertEqual(len(result['segments']),100)
        self.assertEqual(counts,{'state.json':1,'segment-edit.json':1})
        self.assertEqual(compiled.call_count,100)

    def test_every_publication_step_has_previous_group_until_applied(self):
        # Each point gets a new revision; the immutable backups must survive retry/restart.
        for step in ('clip','tail','zh','h3','active','applied'):
            with self.subTest(step=step):
                before=self.card();old_h3=before['h3_prompt'];old_source=before['source_prompt']
                old_bytes=(self.out/'clips/seg2.mp4').read_bytes()
                self.prepare('edited '+step)
                original_publish=segments.publish_file;original_save=film.Job.save;original_write=segments.write_json
                def publish(source,target):
                    suffix={'clip':'seg2.mp4','tail':'seg2-last.jpg','zh':'seg2.zh.txt','h3':'seg2.h3.txt'}.get(step)
                    if suffix and Path(target).name==suffix and Path(target).parent in (self.out/'clips',self.out/'prompts'):
                        raise OSError('injected publication '+step)
                    return original_publish(source,target)
                def save(job):
                    if step=='active' and job.dir==self.out and job.state['segs']['1'].get('revision')==self.request['segment_revision']:
                        raise OSError('injected active save')
                    return original_save(job)
                def write(path,value):
                    if step=='applied' and path.name=='segment-edit.json' and value.get('applied'):
                        raise OSError('injected applied commit')
                    return original_write(path,value)
                with patch.object(segments,'publish_file',side_effect=publish),patch.object(film.Job,'save',save),patch.object(segments,'write_json',side_effect=write),self.assertRaises(OSError):self.generate()
                card=self.card()
                self.assertEqual(card['source_prompt'],old_source)
                self.assertEqual(card['h3_prompt'],old_h3)
                with self.client.get(before['url'],headers={'Range':'bytes=0-2'}) as response:
                    self.assertEqual(response.status_code,206)
                    self.assertEqual(response.data,old_bytes[:3])
                with self.assertRaises(ValueError):segments.prepare_assembly(self.request,segments.assembly_status(self.request)['version'])
                mocks=self.generate();mocks[2].assert_not_called();mocks[3].assert_not_called();mocks[4].assert_not_called()
                self.assertEqual(self.card()['source_prompt'],'edited '+step)

    def test_reference_preview_matches_actual_uploads_after_nine_image_trimming(self):
        self.script['shots']=[dict(start=i,end=i+1,action='recipient and seller move') for i in range(10)]
        second=self.out/'seller.png';second.write_bytes(b'seller')
        self.script['cast'].append(dict(name='seller',picture=str(second)))
        board=self.out/'board.png';board.write_bytes(b'board');self.script['board']=str(board)
        self.request['frames']='shot'
        (self.out/'frames').mkdir()
        for i in range(7):(self.out/f'frames/s1-{i+1}.png').write_bytes(b'opening')
        write_json(self.out/'inputs/request.json',self.request);write_json(self.out/'inputs/film.json',self.script)
        descriptions=storyboard.view(self.request,self.jobs.artifacts(self.jobs.find(self.identifier)),False)
        review=storyboard.review_status(self.request,descriptions)
        write_json(self.out/'inputs/review.json',dict(dirty_segments=[],confirmed_version=review['version']))
        card=self.card(1)
        self.assertTrue(card['board_omitted']);self.assertEqual(card['omitted_frames'],3)
        self.assertEqual(len(card['reference_images']),9)
        self.assertEqual([r['kind'] for r in card['reference_images']],['cast','cast']+['frame']*7)
        self.prepare(h3(),'h3',number=1)
        mocks=self.generate()
        actual=[str(call.args[0]) for call in mocks[3].call_args_list]
        expected=[c['picture'] for c in self.script['cast']]+[str(self.out/f'frames/s1-{i+1}.png') for i in range(7)]
        self.assertEqual(actual,expected)
        self.assertTrue(self.card(1)['reference_history_known'])

    def test_http_edit_fail_resume_publish_and_manual_assembly(self):
        self.client.post('/api/config',json={'RUNNINGHUB_API_KEY':'offline-rh','LLM_API_KEY':'offline-text','persist':False},headers=self.headers)
        before=self.card();others={n:(self.out/f'clips/seg{n}.mp4').read_bytes() for n in (1,3)}
        with patch.object(self.jobs,'launch'):
            response=self.client.post(self.url,json=dict(version=before['version'],prompt_version=before['prompt_version'],prompt='recipient kneels',prompt_format='source'),headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.request=json.loads((self.out/'inputs/request.json').read_text())
        with self.assertRaises(RuntimeError):self.generate(poll={'side_effect':RuntimeError('poll connection lost')})
        self.jobs.find(self.identifier)['status']='failed'
        failed=self.card()
        self.assertEqual(failed['source_prompt'],'recipient stands')
        self.assertEqual(failed['prompt'],'recipient kneels')
        self.assertTrue(failed['task_pending'])
        with self.client.get(before['url']) as response:self.assertEqual(response.data,b'OLD CLIP 2')
        with patch.object(self.jobs,'launch'):
            response=self.client.post('/api/jobs/job-1/resume',json={},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.request=json.loads((self.out/'inputs/request.json').read_text());mocks=self.generate()
        for i in (2,3,4):mocks[i].assert_not_called()
        self.jobs.find(self.identifier)['status']='succeeded'
        snapshot=self.client.get('/api/jobs/job-1').json
        self.assertTrue(snapshot['pending_assembly'])
        self.assertEqual((self.out/'film.mp4').read_bytes(),b'OLD FILM')
        with patch.object(self.jobs,'launch'):
            response=self.client.post('/api/jobs/job-1/assemble',json={'version':snapshot['assembly']['version']},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        request=json.loads((self.out/'inputs/request.json').read_text())
        def join(paths,out):
            self.assertEqual([p.read_bytes() for p in paths],[others[1],b'NEW CLIP',others[3]])
            out.write_bytes(b'ASSEMBLED NEW FILM')
        with patch.object(media,'join',side_effect=join),patch.object(media,'duration',return_value=24),patch.object(rh,'submit_with_retry',side_effect=AssertionError('assemble paid')),patch.object(rewrite,'rewrite',side_effect=AssertionError('assemble rewrote')):
            segments.assemble(request)
        self.jobs.find(self.identifier)['status']='succeeded'
        self.assertFalse(self.client.get('/api/jobs/job-1').json['pending_assembly'])
        for n in (1,3):self.assertEqual((self.out/f'clips/seg{n}.mp4').read_bytes(),others[n])

    def test_final_review_repaired_segment_keeps_original_pipeline_recoverable(self):
        for mode in ('shot','first','none'):
            with self.subTest(mode=mode):
                case=SegmentPrompts();case.setUp();self.addCleanup(case.doCleanups)
                case.request['frames']=mode
                from PIL import Image
                Image.new('RGB',(8,8),'blue').save(case.script['cast'][0]['picture'])
                (case.out/'frames').mkdir()
                for i in range(3):(case.out/f'frames/s{i+1}-1.png').write_bytes(b'approved opening')
                planned=film.plan(case.script['shots']);pics={s['idx']:film.refs(case.script,s,mode) for s in planned}
                state=film.Job(case.out)
                for i,seg in enumerate(planned):
                    (case.out/f'clips/seg{i+1}.mp4').unlink();(case.out/f'clips/seg{i+1}-last.jpg').unlink()
                    source=film.compile_prompt(case.script,seg,pics[i],3)
                    state.state['segs'][str(i)]=dict(zh=source,h3=h3(),h3_source=source)
                state.state['segs']['0'].update(terminal_failed=True,error='terminal',task='failed-first')
                state.state['segs']['1'].update(task='accepted-second',error='poll lost')
                state.save()
                write_json(case.out/'inputs/request.json',case.request)
                descriptions=storyboard.view(case.request,case.jobs.artifacts(case.jobs.find(case.identifier)),False)
                review=storyboard.review_status(case.request,descriptions)
                write_json(case.out/'inputs/review.json',dict(dirty_segments=[],confirmed_version=review['version']))
                case.prepare(state.state['segs']['0']['zh'],number=1);case.generate()
                write_json(case.out/'inputs/request.json',case.request)
                case.jobs.find(case.identifier).update(status='succeeded',operation='segment')
                snapshot=case.client.get('/api/jobs/job-1').json
                self.assertTrue(snapshot.get('can_resume_pipeline'),'single repair must not strand accepted or unstarted segments')
                with patch.object(case.jobs,'launch'),patch('workbench.jobs.preflight'):
                    response=case.client.post('/api/jobs/job-1/resume',json={},headers=case.headers)
                self.assertEqual(response.status_code,200,response.json)
                request=json.loads((case.out/'inputs/request.json').read_text());self.assertEqual(request['operation'],'pipeline')
                submitted=[];polled=[]
                with patch.object(rh,'upload',return_value='ref'),patch.object(rh,'submit_with_retry',side_effect=lambda graph,**kw:submitted.append(graph) or 'new-third'),patch.object(rh,'poll',side_effect=lambda task:polled.append(task) or dict(state='done',url='offline://clip',cny=1)),patch.object(film.http,'download',side_effect=lambda url,path:path.write_bytes(b'recovered video')),patch.object(media,'last_frame',side_effect=lambda clip,path:path.write_bytes(b'recovered tail')),patch.object(media,'join',side_effect=lambda paths,path:path.write_bytes(b'film')),patch.object(media,'duration',return_value=24),patch.object(rewrite,'rewrite',side_effect=AssertionError('rewrote saved pipeline')):
                    storyboard.run(request)
                self.assertEqual(len(submitted),1)
                self.assertEqual(sorted(polled),['accepted-second','new-third'])
                case.jobs.find(case.identifier)['status']='succeeded'
                self.assertTrue(all(card['url'] for card in case.client.get('/api/jobs/job-1').json['segments']))

    def test_final_review_version_checked_path_cannot_change_bytes_before_open(self):
        old=self.card();self.prepare();original=segments.visible_file;published=False
        def race(out,name,context,version=None):
            nonlocal published
            source=original(out,name,context,version)
            if name=='clips/seg2.mp4' and version is not None and not published:
                published=True;self.generate()
            return source
        with patch.object(segments,'visible_file',side_effect=race),self.client.get(old['url'],headers={'Range':'bytes=0-2'}) as response:
            self.assertEqual(response.status_code,206)
            self.assertEqual(response.data,b'OLD')

    def test_final_review_snapshot_retries_publication_that_started_after_context(self):
        self.prepare();original=self.jobs.artifacts;published=False
        def race(record,context=None):
            nonlocal published
            if not published:
                published=True;self.generate()
            return original(record,context)
        with patch.object(self.jobs,'artifacts',side_effect=race):card=self.card()
        with self.client.get(card['url']) as response:payload=response.data
        self.assertIn((card['source_prompt'],payload),[('recipient stands',b'OLD CLIP 2'),('recipient kneels',b'NEW CLIP')])

    def test_final_review_initial_upload_and_explicit_rejection_remain_editable(self):
        for phase in ('upload','submit'):
            with self.subTest(phase=phase):
                case=SegmentPrompts();case.setUp();self.addCleanup(case.doCleanups)
                (case.out/'clips/seg2.mp4').unlink()
                job=film.Job(case.out);job.state['segs']['1']={key:value for key,value in job.state['segs']['1'].items() if key in ('zh','h3')};job.save()
                seg=film.plan(case.script['shots'])[1];pics=film.refs(case.script,seg,'none')
                with patch.object(rh,'upload',**({'side_effect':RuntimeError('upload failed')} if phase=='upload' else {'return_value':'uploaded'})),patch.object(rh,'submit_with_retry',side_effect=rh.SubmissionRejected('explicit rejection')):
                    with self.assertRaises(RuntimeError):film.gen_segment(job,case.script,seg,pics)
                record=film.Job(case.out).state['segs']['1']
                self.assertTrue(record.get('error'),'initial attempted failures must be saved on the card')
                case.jobs.find(case.identifier)['status']='failed'
                self.assertTrue(case.card()['editable'])
                case.prepare('corrected source')

    def test_final_review_chain_first_completion_binds_actual_uploaded_sources(self):
        for mode in ('first','none'):
            with self.subTest(mode=mode):
                case=SegmentPrompts();case.setUp();self.addCleanup(case.doCleanups)
                case.request['frames']=mode;(case.out/'frames').mkdir()
                for i in range(3):
                    (case.out/f'frames/s{i+1}-1.png').write_bytes(b'opening')
                    (case.out/f'clips/seg{i+1}.mp4').unlink();(case.out/f'clips/seg{i+1}-last.jpg').unlink()
                job=film.Job(case.out);job.state['segs']={};job.save()
                planned=film.plan(case.script['shots']);pics={s['idx']:film.refs(case.script,s,mode) for s in planned}
                with patch.object(rewrite,'rewrite',return_value=h3()):film.stage_prompts(job,case.script,planned,pics)
                with patch.object(rh,'upload',return_value='ref'),patch.object(rh,'submit_with_retry',return_value='accepted'),patch.object(rh,'poll',return_value=dict(state='done',url='offline://clip',cny=1)),patch.object(film.http,'download',side_effect=lambda url,path:path.write_bytes(b'clip')),patch.object(media,'last_frame',side_effect=lambda clip,path:path.write_bytes(b'tail')):
                    film.stage_generate(job,case.script,planned,pics,mode)
                write_json(case.out/'inputs/request.json',case.request)
                card=case.card();source=film.Job(case.out).state['segs']['1']['zh']
                preparation=segments.regeneration_plan(case.request,2,card['version'],prompt=source,prompt_format='source',prompt_version=card['prompt_version'])
                self.assertTrue(preparation['reuse_h3'],'unchanged completed chain must not charge a new text rewrite')

    def test_final_review_reference_changes_invalidate_prompt_version_and_cost_contract(self):
        for mode in ('first','none'):
            with self.subTest(mode=mode):
                case=SegmentPrompts();case.setUp();self.addCleanup(case.doCleanups)
                case.request['frames']=mode;write_json(case.out/'inputs/request.json',case.request)
                (case.out/'frames').mkdir()
                for i in range(3):(case.out/f'frames/s{i+1}-1.png').write_bytes(b'opening')
                context=segments.read_context(case.request)
                reference=segments.automatic_inputs(case.request,2,context)['reference_version']
                film.Job(case.out).put('segs','1',h3_reference_version=reference,h3_source='recipient stands')
                before=case.card()
                self.assertFalse(before.get('source_rewrite_required',True))
                (case.out/'clips/seg1-last.jpg').write_bytes(b'NEW PREVIOUS TAIL')
                after=case.card()
                self.assertNotEqual(before['prompt_version'],after['prompt_version'])
                self.assertTrue(after['source_rewrite_required'])
                with self.assertRaises(ValueError):
                    segments.regeneration_plan(case.request,2,before['version'],prompt='recipient stands',prompt_format='source',prompt_version=before['prompt_version'])

    def test_final_review_malformed_picture_tokens_rejected_before_pending(self):
        for token in ('-1','1.5','abc','+1',''):
            with self.subTest(token=token):
                text=h3().replace('<Picture 1>',f'<Picture {token}>')
                self.assertTrue(rewrite.problems(text,2))
                with self.assertRaises(ValueError):self.prepare(text,'h3')
                self.assertFalse((self.out/'inputs/segment-edit.json').exists())

    def test_final_review_legacy_seed_change_does_not_create_or_rebind_override(self):
        for override in (None,dict(format='source',text='recipient stands',source_version='original-reviewed-source')):
            with self.subTest(override=override):
                case=SegmentPrompts();case.setUp();self.addCleanup(case.doCleanups)
                reference=segments.automatic_inputs(case.request,2,segments.read_context(case.request))['reference_version']
                record=dict(h3_reference_version=reference,h3_source='recipient stands')
                if override:record['prompt_override']=override
                film.Job(case.out).put('segs','1',**record)
                (case.out/'clips/seg1-last.jpg').write_bytes(b'changed tail after old page loaded')
                card=case.card();case.request.update(operation='segment',segment=2)
                case.request['segment_revision']=segments.prepare_regeneration(case.request,2,card['version'])
                segments.preflight(case.request,{'RUNNINGHUB_API_KEY':'offline'})
                mocks=case.generate();mocks[2].assert_not_called()
                self.assertEqual(film.Job(case.out).state['segs']['1'].get('prompt_override'),override)

    def test_final_review_unchanged_automatic_source_stays_automatic(self):
        self.prepare('recipient stands');self.generate()
        self.assertNotIn('prompt_override',film.Job(self.out).state['segs']['1'])

    def test_applied_frame_redo_allows_failed_segment_retry_after_refresh(self):
        self.request.update(redo=[2],reset_applied=True)
        write_json(self.out/'inputs/request.json',self.request)
        review=storyboard.review_state(self.out)
        review['dirty_segments']=[2]
        write_json(self.out/'inputs/review.json',review)
        self.job.put('segs','1',terminal_failed=True,error='Read timeout',file=None,cny=None)
        (self.out/'clips/seg2.mp4').unlink()
        self.jobs.find(self.identifier)['status']='failed'
        saved_review=(self.out/'inputs/review.json').read_bytes()
        saved_request=(self.out/'inputs/request.json').read_bytes()
        for _ in range(2):
            snapshot=self.client.get('/api/jobs/job-1').json
            self.assertTrue(snapshot['review']['complete'])
            self.assertFalse(snapshot['pending_video'])
            self.assertFalse(snapshot['can_resume_video'],'terminal failure still requires an explicit new attempt')
            self.assertTrue(snapshot['segments'][1]['can_regenerate'])
        self.assertEqual((self.out/'inputs/review.json').read_bytes(),saved_review)
        self.assertEqual((self.out/'inputs/request.json').read_bytes(),saved_request)
        card=snapshot['segments'][1]
        with patch.object(self.jobs,'launch'),patch('workbench.jobs.preflight'):
            response=self.client.post(self.url,json=dict(version=card['version'],prompt_version=card['prompt_version'],
                prompt=card['prompt'],prompt_format=card['prompt_format']),headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertFalse(response.json['pending_video'])
        self.assertEqual(json.loads((self.out/'inputs/review.json').read_text())['dirty_segments'],[])
        self.assertTrue(film.Job(self.out).state['segs']['1']['terminal_failed'])

    def test_unapplied_or_new_frame_changes_still_block_segment_retry(self):
        self.job.put('segs','1',terminal_failed=True,error='Read timeout')
        self.jobs.find(self.identifier)['status']='failed'
        for operation,applied,redo in [('pipeline',False,[2]),('pipeline',True,[1]),('segment',True,[2])]:
            with self.subTest(operation=operation,applied=applied,redo=redo):
                self.request.update(operation=operation,reset_applied=applied,redo=redo)
                write_json(self.out/'inputs/request.json',self.request)
                review=json.loads((self.out/'inputs/review.json').read_text())
                review['dirty_segments']=[2]
                write_json(self.out/'inputs/review.json',review)
                snapshot=self.client.get('/api/jobs/job-1').json
                self.assertTrue(snapshot['pending_video'])
                card=snapshot['segments'][1]
                self.assertFalse(card['can_regenerate'])
                with patch.object(self.jobs,'launch') as launch:
                    response=self.client.post(self.url,json=dict(version=card['version'],prompt_version=card['prompt_version'],
                        prompt=card['prompt'],prompt_format=card['prompt_format']),headers=self.headers)
                self.assertEqual(response.status_code,400,response.json)
                self.assertEqual(response.json['code'],'review_required')
                launch.assert_not_called()

    def test_multiple_failed_segments_can_be_repaired_then_resume_original_pipeline(self):
        from PIL import Image
        Image.new('RGB',(8,8),'blue').save(self.script['cast'][0]['picture'])
        self.script['shots']=[dict(start=i*8,end=(i+1)*8,action='recipient stands') for i in range(5)]
        self.request.update(frames='shot',redo=[2,3,5],reset_applied=True)
        write_json(self.out/'inputs/film.json',self.script)
        write_json(self.out/'inputs/request.json',self.request)
        (self.out/'frames').mkdir()
        for i in range(5):(self.out/f'frames/s{i+1}-1.png').write_bytes(b'approved opening')
        planned=film.plan(self.script['shots'])
        for seg in planned:
            i=seg['idx'];source=film.compile_prompt(self.script,seg,film.refs(self.script,seg,'shot'),5)
            record=dict(zh=source,h3=h3(),h3_source=source)
            if i==0:record.update(file=str(self.out/'clips/seg1.mp4'),cny=1,task='completed-first')
            elif i in (1,2):record.update(task=f'failed-{i+1}',terminal_failed=True,error='Read timeout')
            elif i==3:record.update(task='accepted-fourth')
            self.job.state['segs'][str(i)]=record
        self.job.save()
        for number in (2,3):
            (self.out/f'clips/seg{number}.mp4').unlink()
            (self.out/f'clips/seg{number}-last.jpg').unlink()
        (self.out/'film.mp4').unlink()
        descriptions=storyboard.view(self.request,self.jobs.artifacts(self.jobs.find(self.identifier)),False)
        review=storyboard.review_status(self.request,descriptions)
        write_json(self.out/'inputs/review.json',dict(dirty_segments=[2,3,5],confirmed_version=review['version']))
        self.jobs.find(self.identifier)['status']='failed'
        original_clip=(self.out/'clips/seg1.mp4').read_bytes()
        self.client.post('/api/config',json={'RUNNINGHUB_API_KEY':'offline-rh','persist':False},headers=self.headers)
        for number in (2,3):
            card=self.card(number)
            self.assertTrue(card['can_regenerate'])
            with patch.object(self.jobs,'launch'),patch('workbench.segments.shutil.which',return_value='/offline/ffmpeg'):
                response=self.client.post(f'/api/jobs/job-1/segments/{number}/regenerate',json=dict(version=card['version'],
                    prompt_version=card['prompt_version'],prompt=card['prompt'],prompt_format=card['prompt_format']),headers=self.headers)
            self.assertEqual(response.status_code,200,response.json)
            self.request=json.loads((self.out/'inputs/request.json').read_text())
            self.generate(submit=lambda graph,**kw:f'repaired-{number}')
            self.jobs.find(self.identifier)['status']='succeeded'
            snapshot=self.client.get('/api/jobs/job-1').json
            self.assertFalse(snapshot['pending_video'])
            self.assertTrue(snapshot['can_resume_pipeline'])
            self.assertEqual(snapshot['can_resume_video'],number==3)
            self.assertEqual((self.out/'clips/seg1.mp4').read_bytes(),original_clip)
            self.assertEqual(film.Job(self.out).state['segs']['3']['task'],'accepted-fourth')
        with patch.object(self.jobs,'launch'),patch('workbench.segments.shutil.which',return_value='/offline/ffmpeg'):
            response=self.client.post('/api/jobs/job-1/resume',json={},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        request=json.loads((self.out/'inputs/request.json').read_text())
        self.assertEqual(request['operation'],'pipeline')
        submitted=[];polled=[]
        with patch.object(rh,'upload',return_value='ref'),patch.object(rh,'submit_with_retry',side_effect=lambda graph,**kw:submitted.append(graph) or 'new-fifth'),patch.object(rh,'poll',side_effect=lambda task:polled.append(task) or dict(state='done',url='offline://clip',cny=1)),patch.object(film.http,'download',side_effect=lambda url,path:path.write_bytes(b'recovered video')),patch.object(media,'last_frame',side_effect=lambda clip,path:path.write_bytes(b'recovered tail')),patch.object(media,'join',side_effect=lambda paths,path:path.write_bytes(b'film')),patch.object(media,'duration',return_value=40),patch.object(rewrite,'rewrite',side_effect=AssertionError('rewrote saved pipeline')):
            storyboard.run(request)
        self.assertEqual(len(submitted),1,'only the never-submitted fifth segment may create a task')
        self.assertEqual(sorted(polled),['accepted-fourth','new-fifth'])
        self.assertEqual((self.out/'clips/seg1.mp4').read_bytes(),original_clip)
        self.assertEqual((self.out/'film.mp4').read_bytes(),b'film')

    def test_pre_task_interrupted_repair_on_clipless_segment_remains_editable_and_settles(self):
        (self.out / 'clips/seg2.mp4').unlink()
        self.job.put('segs', '1', terminal_failed=True, error='terminal', file=None)
        self.prepare('interrupted draft')
        with patch.object(rewrite, 'backend', return_value='llm'), patch.object(rewrite, 'guide', return_value='g'), patch.object(rewrite, '_llm', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                segments.regenerate(self.request)
        self.jobs.find(self.identifier)['status'] = 'interrupted'
        self.assertTrue(self.card()['editable'])
        self.assertTrue(self.card()['can_regenerate'])
        self.prepare('replacement source')
        self.generate()
        self.assertEqual((self.out / 'clips/seg2.mp4').read_bytes(), b'NEW CLIP')
        self.assertTrue(json.loads((self.out / 'inputs/segment-edit.json').read_text())['applied'])

    def test_stage_prompts_failure_persists_zh_and_error_for_workbench_repair(self):
        (self.out / 'clips/seg2.mp4').unlink()
        self.job.state['segs'].pop('1', None)
        self.job.save()
        planned = film.plan(self.script['shots'])
        pics = {s['idx']: film.refs(self.script, s, 'shot') for s in planned}
        with patch.object(rewrite, 'rewrite', side_effect=rewrite.RewriteError('twice failed')):
            with self.assertRaises(rewrite.RewriteError):
                film.stage_prompts(self.job, self.script, planned, pics)
        self.jobs.find(self.identifier)['status'] = 'failed'
        card = self.card(2)
        self.assertTrue(card['editable'])
        self.assertTrue(card['can_regenerate'])
        self.assertTrue(card['prompt'])

    def test_applied_publication_clears_publishing_flag_and_does_not_resurrect_after_redo(self):
        (self.out / 'clips/seg2.mp4').unlink()
        self.job.state['segs']['1'] = {'zh': 'recipient stands', 'h3': h3()}
        self.job.save()
        self.job.put('segs', '1', terminal_failed=True, error='boom')
        self.prepare('edited once')
        self.generate()
        pending = json.loads((self.out / 'inputs/segment-edit.json').read_text())
        self.assertTrue(pending['applied'])
        self.assertFalse(pending.get('publishing'))
        film.invalidate_segments(film.Job(self.out), 'shot', [2])
        self.assertIsNone(self.card(2)['url'])

    def test_edit_frame_and_invalidate_protect_unresolved_paid_sibling_and_allow_sibling_resume(self):
        from PIL import Image
        (self.out / 'frames').mkdir(exist_ok=True)
        for seg in range(1, 4):
            Image.new('RGB', (8, 8), 'red').save(self.out / f'frames/s{seg}-1.png')
        self.request['frames'] = 'shot'
        write_json(self.out / 'inputs/request.json', self.request)
        descriptions = storyboard.view(self.request, self.jobs.artifacts(self.jobs.find(self.identifier)), False)
        review = storyboard.review_status(self.request, descriptions)
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[], confirmed_version=review['version']))
        self.job.put('segs', '0', task='paid-inflight-1', file=None, cny=None)
        self.job.put('segs', '1', task='failed-2', terminal_failed=True, error='boom', file=None, cny=None)
        (self.out / 'clips/seg1.mp4').unlink()
        (self.out / 'clips/seg2.mp4').unlink()
        self.jobs.find(self.identifier)['status'] = 'failed'
        snap = self.client.get('/api/jobs/job-1').json
        self.assertTrue(snap['can_resume_video'], 'In-flight paid sibling segment must remain resumable')
        # Even if job status became succeeded after repairing seg2, editing s1-1 must not wipe paid-inflight-1
        self.jobs.find(self.identifier)['status'] = 'succeeded'
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch'):
            res = self.client.post('/api/jobs/job-1/frames/s1-1', json={'prompt': 'replace while task pending'}, headers=self.headers)
        self.assertEqual(res.status_code, 400)
        film.invalidate_segments(film.Job(self.out), 'shot', [1])
        self.assertEqual(film.Job(self.out).state['segs']['0']['task'], 'paid-inflight-1')

    def test_staged_unknown_submission_blocks_same_and_other_segment_regeneration(self):
        self.prepare('ambiguous edit')
        real_submit = rh.submit_with_retry
        with patch.object(rh, '_post', side_effect=RuntimeError('response lost')):
            with self.assertRaises(RuntimeError):
                self.generate(submit=real_submit)
        before = (self.out / 'inputs/segment-edit.json').read_bytes()
        for target in (2, 1):
            with self.assertRaises(InputError):
                self.prepare('replacement', number=target)
        self.assertEqual((self.out / 'inputs/segment-edit.json').read_bytes(), before)

    def test_uncommitted_publication_blocks_same_and_other_segment_regeneration(self):
        self.prepare('publishing edit')
        publish = segments.publish_file
        with patch.object(segments, 'publish_file', side_effect=lambda s, t: (_ for _ in ()).throw(OSError('interrupted')) if Path(t).name == 'seg2-last.jpg' else publish(s, t)):
            with self.assertRaises(OSError):
                self.generate()
        before = (self.out / 'inputs/segment-edit.json').read_bytes()
        for target in (2, 1):
            with self.assertRaises(InputError):
                self.prepare('replacement', number=target)
        self.assertEqual((self.out / 'inputs/segment-edit.json').read_bytes(), before)

    def test_repair_of_terminally_failed_segment_can_resume_after_poll_or_publish_interruption(self):
        from PIL import Image
        (self.out / 'frames').mkdir(exist_ok=True)
        for seg in range(1, 4):
            Image.new('RGB', (8, 8), 'red').save(self.out / f'frames/s{seg}-1.png')
        self.request['frames'] = 'shot'
        write_json(self.out / 'inputs/request.json', self.request)
        descriptions = storyboard.view(self.request, self.jobs.artifacts(self.jobs.find(self.identifier)), False)
        review = storyboard.review_status(self.request, descriptions)
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[], confirmed_version=review['version']))
        self.job.put('segs', '1', terminal_failed=True, error='old terminal', file=None)
        (self.out / 'clips/seg2.mp4').unlink()
        self.prepare('repaired source')
        write_json(self.out / 'inputs/request.json', self.request)
        with self.assertRaises(RuntimeError):
            self.generate(poll={'side_effect': RuntimeError('poll lost')})
        self.jobs.find(self.identifier).update(status='failed', operation='segment')
        snap = self.client.get('/api/jobs/job-1').json
        self.assertTrue(snap['can_resume_video'], 'Active repair attempt must not inherit previous terminal_failed')
        # Editing even an unaffected shot-mode frame (s1-1) must be blocked while segment-edit.json is unresolved
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch'):
            res_frame = self.client.post('/api/jobs/job-1/frames/s1-1', json={'prompt': 'edit other frame'}, headers=self.headers)
        self.assertEqual(res_frame.status_code, 400)
        self.generate()
        self.assertTrue(json.loads((self.out / 'inputs/segment-edit.json').read_text())['applied'])

    def test_continue_film_clears_stale_segment_edit_blocks_inflight_sibling_and_includes_size_in_url(self):
        from PIL import Image
        Image.new('RGB', (8, 8), 'blue').save(self.script['cast'][0]['picture'])
        (self.out / 'frames').mkdir(exist_ok=True)
        for seg in range(1, 4):
            Image.new('RGB', (8, 8), 'red').save(self.out / f'frames/s{seg}-1.png')
        self.request['frames'] = 'shot'
        write_json(self.out / 'inputs/request.json', self.request)
        # Artifact URLs include :st_size alongside st_mtime_ns
        seg2_url = next(a['url'] for a in self.jobs.artifacts(self.jobs.find(self.identifier)) if a['name'] == 'clips/seg2.mp4')
        self.assertRegex(seg2_url, r'[?&]v=\d+(?::|%3A)\d+')

        # If a single-segment repair terminally fails while another pipeline segment has an in-flight paid task,
        # can_resume_pipeline stays available so the paid pipeline task is never stranded
        descriptions = storyboard.view(self.request, self.jobs.artifacts(self.jobs.find(self.identifier)), False)
        review = storyboard.review_status(self.request, descriptions)
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[], confirmed_version=review['version']))
        self.job.put('segs', '2', task='inflight-seg3-task', file=None, cny=None)
        self.prepare('seg2 terminal attempt')
        self.request.update(operation='segment', segment=2)
        write_json(self.out / 'inputs/request.json', self.request)
        with self.assertRaises(RuntimeError):
            self.generate(poll={'return_value': {'state': 'failed', 'error': 'oom'}})
        self.jobs.find(self.identifier).update(status='failed', operation='segment')
        snap_mid = self.client.get('/api/jobs/job-1').json
        self.assertTrue(snap_mid['can_resume_pipeline'], 'In-flight sibling task must remain resumable after terminal single-segment failure')
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch'):
            res_resume = self.client.post('/api/jobs/job-1/resume', json={}, headers=self.headers)
        self.assertEqual(res_resume.status_code, 200, res_resume.json)
        self.assertEqual(json.loads((self.out / 'inputs/request.json').read_text())['operation'], 'pipeline')

        # Clear seg3 task, create failed unapplied segment-edit.json on seg2, then dirty frame s2-1 and continue_film
        self.job.put('segs', '2', task=None, file=str(self.out / 'clips/seg3.mp4'), cny=1)
        self.jobs.find(self.identifier)['status'] = 'succeeded'
        self.prepare('failed single-seg draft')
        pending_path = self.out / 'inputs/segment-edit.json'
        pending_data = json.loads(pending_path.read_text(encoding='utf-8'))
        pending_data['error'] = 'old attempt failed'
        write_json(pending_path, pending_data)
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[2]))
        snap_before = self.client.get('/api/jobs/job-1').json
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch'):
            cont = self.client.post('/api/jobs/job-1/continue',
                                    json={'approved': True, 'review_version': snap_before['review']['version']},
                                    headers=self.headers)
        self.assertEqual(cont.status_code, 200, cont.json)
        self.assertFalse(pending_path.exists(), 'continue_film must clear unapplied non-locked segment-edit.json')

    def _setup_shot_frames_reviewed(self):
        from PIL import Image
        Image.new('RGB', (8, 8), 'blue').save(self.script['cast'][0]['picture'])
        (self.out / 'frames').mkdir(exist_ok=True)
        for seg in range(1, 4):
            Image.new('RGB', (8, 8), 'red').save(self.out / f'frames/s{seg}-1.png')
        self.request['frames'] = 'shot'
        write_json(self.out / 'inputs/request.json', self.request)
        descriptions = storyboard.view(self.request, self.jobs.artifacts(self.jobs.find(self.identifier)), False)
        review = storyboard.review_status(self.request, descriptions)
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[], confirmed_version=review['version']))

    def test_pre_task_failed_segment_edit_resumes_via_http_and_settles_same_revision(self):
        rev = self.prepare('pre-task draft')
        write_json(self.out / 'inputs/request.json', self.request)
        with patch.object(rewrite, 'backend', return_value='llm'), \
             patch.object(rewrite, 'guide', return_value='g'), \
             patch.object(rewrite, '_llm', side_effect=RuntimeError('llm down')):
            with self.assertRaises(RuntimeError):
                segments.regenerate(self.request)
        self.jobs.find(self.identifier).update(status='failed', operation='segment')
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch'):
            res = self.client.post('/api/jobs/job-1/resume', json={}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        self.request = json.loads((self.out / 'inputs/request.json').read_text())
        self.generate()
        pending = json.loads((self.out / 'inputs/segment-edit.json').read_text())
        self.assertEqual(pending['revision'], rev)
        self.assertTrue(pending['applied'])
        self.assertFalse(pending.get('publishing'))
        self.assertEqual((self.out / 'clips/seg2.mp4').read_bytes(), b'NEW CLIP')

    def test_unsubmitted_segment_edit_replaced_by_other_segment_keeps_first_clip(self):
        orig_seg2 = (self.out / 'clips/seg2.mp4').read_bytes()
        orig_state_2 = dict(film.Job(self.out).state['segs']['1'])
        self.prepare('draft on seg2', number=2)
        self.prepare('edit seg1', number=1)
        self.generate()
        self.assertEqual((self.out / 'clips/seg2.mp4').read_bytes(), orig_seg2)
        self.assertEqual(film.Job(self.out).state['segs']['1']['task'], orig_state_2['task'])
        self.assertEqual((self.out / 'clips/seg1.mp4').read_bytes(), b'NEW CLIP')
        self.assertTrue(json.loads((self.out / 'inputs/segment-edit.json').read_text())['applied'])

    def test_continue_dirty_frames_clears_affected_unsubmitted_segment_edit_and_keeps_unaffected(self):
        self._setup_shot_frames_reviewed()
        # 1. Pending unsubmitted edit on seg1 while frame s2-1 is dirtied (unaffected seg1): keep segment-edit.json intact!
        rev1 = self.prepare('unaffected seg1 draft', number=1)
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[2]))
        self.jobs.find(self.identifier)['status'] = 'succeeded'
        snap = self.client.get('/api/jobs/job-1').json
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch'):
            res = self.client.post('/api/jobs/job-1/continue',
                                   json={'approved': True, 'review_version': snap['review']['version']},
                                   headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        kept = json.loads((self.out / 'inputs/segment-edit.json').read_text())
        self.assertEqual(kept['revision'], rev1, 'continue_film must preserve unaffected segment-edit.json')
        self.assertEqual(kept['number'], 1)

        # 2. Pending unsubmitted edit on seg2 while frame s2-1 is dirtied (affected seg2): supersede segment-edit.json!
        self.jobs.find(self.identifier)['status'] = 'succeeded'
        self.prepare('affected seg2 draft', number=2)
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[2]))
        snap2 = self.client.get('/api/jobs/job-1').json
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch'):
            res2 = self.client.post('/api/jobs/job-1/continue',
                                    json={'approved': True, 'review_version': snap2['review']['version']},
                                    headers=self.headers)
        self.assertEqual(res2.status_code, 200, res2.json)
        self.assertFalse((self.out / 'inputs/segment-edit.json').exists())

    def test_staged_unknown_submission_rejects_http_resume(self):
        orig = (self.out / 'clips/seg2.mp4').read_bytes()
        self.prepare('ambiguous edit')
        write_json(self.out / 'inputs/request.json', self.request)
        with patch.object(rh, '_post', side_effect=RuntimeError('response lost')):
            with self.assertRaises(RuntimeError):
                self.generate(submit=rh.submit_with_retry)
        self.jobs.find(self.identifier).update(status='failed', operation='segment')
        snap = self.client.get('/api/jobs/job-1').json
        self.assertFalse(snap['can_resume_video'])
        before = (self.out / 'inputs/segment-edit.json').read_bytes()
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch') as launch:
            res = self.client.post('/api/jobs/job-1/resume', json={}, headers=self.headers)
        self.assertEqual(res.status_code, 400)
        launch.assert_not_called()
        self.assertEqual((self.out / 'inputs/segment-edit.json').read_bytes(), before)
        self.assertEqual((self.out / 'clips/seg2.mp4').read_bytes(), orig)

    def test_staged_unknown_submission_blocks_continue_film(self):
        self._setup_shot_frames_reviewed()
        self.prepare('ambiguous edit', number=1)
        with patch.object(rh, '_post', side_effect=RuntimeError('response lost')):
            with self.assertRaises(RuntimeError):
                self.generate(submit=rh.submit_with_retry)
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[2]))
        self.jobs.find(self.identifier)['status'] = 'succeeded'
        snap = self.client.get('/api/jobs/job-1').json
        before_rev = (self.out / 'inputs/review.json').read_bytes()
        before_seg = (self.out / 'inputs/segment-edit.json').read_bytes()
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch') as launch:
            res = self.client.post('/api/jobs/job-1/continue',
                                   json={'approved': True, 'review_version': snap['review']['version']},
                                   headers=self.headers)
        self.assertEqual(res.status_code, 400)
        launch.assert_not_called()
        self.assertEqual((self.out / 'inputs/review.json').read_bytes(), before_rev)
        self.assertEqual((self.out / 'inputs/segment-edit.json').read_bytes(), before_seg)

    def test_staged_inflight_segment_task_blocks_continue_film(self):
        self._setup_shot_frames_reviewed()
        self.prepare('inflight edit', number=1)
        with self.assertRaises(RuntimeError):
            self.generate(poll={'side_effect': RuntimeError('poll lost')})
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[2]))
        self.jobs.find(self.identifier)['status'] = 'succeeded'
        snap = self.client.get('/api/jobs/job-1').json
        before_rev = (self.out / 'inputs/review.json').read_bytes()
        before_seg = (self.out / 'inputs/segment-edit.json').read_bytes()
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch') as launch:
            res = self.client.post('/api/jobs/job-1/continue',
                                   json={'approved': True, 'review_version': snap['review']['version']},
                                   headers=self.headers)
        self.assertEqual(res.status_code, 400)
        launch.assert_not_called()
        self.assertEqual((self.out / 'inputs/review.json').read_bytes(), before_rev)
        self.assertEqual((self.out / 'inputs/segment-edit.json').read_bytes(), before_seg)

    def test_interrupted_segment_publication_resumes_via_http_without_llm_keys_and_commits(self):
        self.prepare('publishing source')
        write_json(self.out / 'inputs/request.json', self.request)
        publish = segments.publish_file
        with patch.object(segments, 'publish_file', side_effect=lambda s, t: (_ for _ in ()).throw(OSError('interrupted')) if Path(t).name == 'seg2-last.jpg' else publish(s, t)):
            with self.assertRaises(OSError):
                self.generate()
        self.jobs.find(self.identifier).update(status='interrupted', operation='segment')
        with patch.object(self.jobs, 'launch'):
            res = self.client.post('/api/jobs/job-1/resume', json={}, headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        with patch.object(rewrite, 'rewrite', side_effect=AssertionError('rewrote on publish resume')), \
             patch.object(rh, 'submit_with_retry', side_effect=AssertionError('resubmitted on publish resume')):
            segments.regenerate(json.loads((self.out / 'inputs/request.json').read_text()))
        pending = json.loads((self.out / 'inputs/segment-edit.json').read_text())
        self.assertTrue(pending['applied'])
        self.assertFalse(pending['publishing'])
        self.assertEqual((self.out / 'clips/seg2.mp4').read_bytes(), b'NEW CLIP')

    def test_uncommitted_segment_publication_blocks_continue_film(self):
        self._setup_shot_frames_reviewed()
        self.prepare('publishing source', number=1)
        publish = segments.publish_file
        with patch.object(segments, 'publish_file', side_effect=lambda s, t: (_ for _ in ()).throw(OSError('interrupted')) if Path(t).name == 'seg1-last.jpg' else publish(s, t)):
            with self.assertRaises(OSError):
                self.generate()
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[2]))
        self.jobs.find(self.identifier)['status'] = 'succeeded'
        snap = self.client.get('/api/jobs/job-1').json
        before_seg = (self.out / 'inputs/segment-edit.json').read_bytes()
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch') as launch:
            res = self.client.post('/api/jobs/job-1/continue',
                                   json={'approved': True, 'review_version': snap['review']['version']},
                                   headers=self.headers)
        self.assertEqual(res.status_code, 400)
        launch.assert_not_called()
        self.assertEqual((self.out / 'inputs/segment-edit.json').read_bytes(), before_seg)

    def test_staged_terminal_segment_failure_rejects_http_resume(self):
        self.prepare('terminal edit')
        write_json(self.out / 'inputs/request.json', self.request)
        with self.assertRaises(RuntimeError):
            self.generate(poll={'return_value': {'state': 'failed', 'error': 'terminal gpu'}})
        self.jobs.find(self.identifier).update(status='failed', operation='segment')
        snap = self.client.get('/api/jobs/job-1').json
        self.assertFalse(snap['can_resume_video'])
        before = (self.out / 'inputs/segment-edit.json').read_bytes()
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch') as launch:
            res = self.client.post('/api/jobs/job-1/resume', json={}, headers=self.headers)
        self.assertEqual(res.status_code, 400)
        launch.assert_not_called()
        self.assertEqual((self.out / 'inputs/segment-edit.json').read_bytes(), before)

    def test_staged_terminal_failure_preserved_when_other_segment_regenerates(self):
        self._setup_shot_frames_reviewed()
        # Clipless seg2 initially failed pre-submission (error in state.json, terminal_failed not yet set)
        (self.out / 'clips/seg2.mp4').unlink()
        (self.out / 'clips/seg2-last.jpg').unlink()
        self.job.state['segs']['1'] = {'zh': 'recipient stands', 'h3': h3(), 'error': 'upload failed'}
        self.job.save()
        self.prepare('clipless seg2 repair', number=2)
        with self.assertRaises(RuntimeError):
            self.generate(submit=lambda g, **kw: 'dead-seg2-task', poll={'return_value': {'state': 'failed', 'error': 'oom'}})
        # Now regenerate seg1 (replace_other): seg2 must retain terminal_failed=True and dead-seg2-task!
        self.prepare('regenerate seg1', number=1)
        self.generate(submit=lambda g, **kw: 'ok-seg1-task')
        saved_seg2 = film.Job(self.out).state['segs']['1']
        self.assertTrue(saved_seg2.get('terminal_failed'), 'Overwriting segment-edit.json lost clipless seg2 terminal_failed')
        self.assertEqual(saved_seg2.get('task'), 'dead-seg2-task')
        self.assertTrue(saved_seg2.get('error'))
        # Pipeline rerun must refuse to auto-resubmit a duplicate paid task on seg2
        with patch.object(rh, 'submit_with_retry', side_effect=AssertionError('repaid terminal seg2')):
            with self.assertRaises(RuntimeError):
                storyboard.run({**self.request, 'operation': 'pipeline', 'reset_applied': True})

    def test_continue_dirty_frames_preserves_unaffected_staged_terminal_segment_failure(self):
        self._setup_shot_frames_reviewed()
        (self.out / 'clips/seg1.mp4').unlink()
        (self.out / 'clips/seg1-last.jpg').unlink()
        self.job.state['segs']['0'] = {'zh': 'recipient stands', 'h3': h3(), 'error': 'upload failed'}
        self.job.save()
        self.prepare('edited seg1 terminal prompt', number=1)
        with self.assertRaises(RuntimeError):
            self.generate(submit=lambda g, **kw: 'dead-seg1-task', poll={'return_value': {'state': 'failed', 'error': 'oom'}})
        # Dirty frame s2-1 (seg1 is unaffected!) and approve continue_film
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[2]))
        self.jobs.find(self.identifier)['status'] = 'succeeded'
        snap = self.client.get('/api/jobs/job-1').json
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch'):
            res = self.client.post('/api/jobs/job-1/continue',
                                   json={'approved': True, 'review_version': snap['review']['version']},
                                   headers=self.headers)
        self.assertEqual(res.status_code, 200, res.json)
        submitted_segs = []
        def fake_submit(graph, **kw):
            submitted_segs.append(graph)
            return 'new-seg2-task'
        with patch.object(rewrite, 'rewrite', return_value=h3()), \
             patch.object(rh, 'upload', return_value='ref'), \
             patch.object(rh, 'submit_with_retry', side_effect=fake_submit), \
             patch.object(rh, 'poll', return_value={'state': 'done', 'url': 'offline://v', 'cny': 1}), \
             patch.object(film.http, 'download', side_effect=lambda u, p: p.write_bytes(b'SEG2')), \
             patch.object(media, 'last_frame', side_effect=lambda c, p: p.write_bytes(b'TAIL2')), \
             patch.object(media, 'duration', return_value=8):
            with self.assertRaises(RuntimeError):
                storyboard.run(json.loads((self.out / 'inputs/request.json').read_text()))
        self.assertEqual(len(submitted_segs), 0, 'Preexisting terminal failure on seg1 must block submitting new tasks before repair')
        saved_seg1 = film.Job(self.out).state['segs']['0']
        self.assertTrue(saved_seg1.get('terminal_failed'))
        self.assertEqual(saved_seg1.get('task'), 'dead-seg1-task')
        self.assertEqual(self.card(1)['prompt'], 'edited seg1 terminal prompt')

    def test_terminal_segment_repair_with_inflight_sibling_resumes_sibling_without_repaying_terminal(self):
        for mode in ('shot', 'none'):
            with self.subTest(mode=mode):
                case = SegmentPrompts()
                case.setUp()
                self.addCleanup(case.doCleanups)
                if mode == 'shot':
                    case._setup_shot_frames_reviewed()
                else:
                    from PIL import Image
                    Image.new('RGB', (8, 8), 'blue').save(case.script['cast'][0]['picture'])
                    case.request['frames'] = 'none'
                    write_json(case.out / 'inputs/request.json', case.request)
                (case.out / 'clips/seg2.mp4').unlink()
                (case.out / 'clips/seg2-last.jpg').unlink()
                (case.out / 'clips/seg3.mp4').unlink()
                (case.out / 'clips/seg3-last.jpg').unlink()
                case.job.state['segs']['1'] = {'zh': 'recipient stands', 'h3': h3(), 'error': 'initial upload err'}
                case.job.state['segs']['2'] = {'zh': 'recipient stands', 'h3': h3(), 'task': f'inflight-s3-{mode}', 'submitted': 1.0}
                case.job.save()
                case.prepare('seg2 custom repair prompt', number=2)
                write_json(case.out / 'inputs/request.json', case.request)
                with self.assertRaises(RuntimeError):
                    case.generate(submit=lambda g, **kw: f'dead-s2-{mode}', poll={'return_value': {'state': 'failed', 'error': 'oom'}})
                case.jobs.find(case.identifier).update(status='failed', operation='segment')
                snap = case.client.get('/api/jobs/job-1').json
                self.assertTrue(snap['can_resume_pipeline'])
                self.assertTrue(snap['can_resume_video'], 'UI Resume button must be enabled when can_resume_pipeline is True')

                # Failed preflight must NOT delete segment-edit.json!
                with patch('workbench.jobs.preflight', side_effect=InputError('keys', names='RUNNINGHUB_API_KEY')):
                    bad_res = case.client.post('/api/jobs/job-1/resume', json={}, headers=case.headers)
                self.assertEqual(bad_res.status_code, 400)
                self.assertTrue((case.out / 'inputs/segment-edit.json').exists(), 'Failed preflight unlinked segment-edit.json')

                with patch('workbench.jobs.preflight'), patch.object(case.jobs, 'launch'):
                    ok_res = case.client.post('/api/jobs/job-1/resume', json={}, headers=case.headers)
                self.assertEqual(ok_res.status_code, 200, ok_res.json)
                polled = []
                with patch.object(rewrite, 'rewrite', side_effect=AssertionError('rewrote during sibling recovery')), \
                     patch.object(rh, 'submit_with_retry', side_effect=AssertionError('paid duplicate task on seg2')), \
                     patch.object(rh, 'poll', side_effect=lambda t: polled.append(t) or {'state': 'done', 'url': 'offline://s3', 'cny': 1.5}), \
                     patch.object(film.http, 'download', side_effect=lambda u, p: p.write_bytes(b'SETTLED SEG3')), \
                     patch.object(media, 'last_frame', side_effect=lambda c, p: p.write_bytes(b'TAIL3')), \
                     patch.object(media, 'duration', return_value=8):
                    with self.assertRaises(RuntimeError):
                        storyboard.run(json.loads((case.out / 'inputs/request.json').read_text()))
                self.assertEqual(polled, [f'inflight-s3-{mode}'], 'In-flight sibling task must be polled and settled before raising on seg2')
                self.assertEqual((case.out / 'clips/seg3.mp4').read_bytes(), b'SETTLED SEG3')
                saved_s2 = film.Job(case.out).state['segs']['1']
                self.assertTrue(saved_s2.get('terminal_failed'))
                self.assertEqual(saved_s2.get('task'), f'dead-s2-{mode}')
                self.assertEqual(case.card(2)['prompt'], 'seg2 custom repair prompt')

    def test_terminal_segment_repair_replaced_on_same_segment_preserves_inflight_sibling(self):
        self._setup_shot_frames_reviewed()
        (self.out / 'clips/seg2.mp4').unlink()
        (self.out / 'clips/seg3.mp4').unlink()
        self.job.state['segs']['1'] = {'zh': 'recipient stands', 'h3': h3(), 'error': 'err'}
        self.job.state['segs']['2'] = {'zh': 'recipient stands', 'h3': h3(), 'task': 'inflight-seg3', 'submitted': 1.0}
        self.job.save()
        self.prepare('first dead attempt', number=2)
        with self.assertRaises(RuntimeError):
            self.generate(submit=lambda g, **kw: 'dead-2', poll={'return_value': {'state': 'failed', 'error': 'oom'}})
        self.prepare('second good attempt', number=2)
        write_json(self.out / 'inputs/request.json', self.request)
        self.generate(submit=lambda g, **kw: 'good-2')
        self.jobs.find(self.identifier).update(status='succeeded', operation='segment')
        snap = self.client.get('/api/jobs/job-1').json
        self.assertTrue(snap['can_resume_pipeline'])
        self.assertTrue(snap['can_resume_video'])
        self.assertEqual(film.Job(self.out).state['segs']['2']['task'], 'inflight-seg3')

    def test_terminal_segment_with_inflight_sibling_locks_inflight_and_preserves_terminal_on_third_edit(self):
        self._setup_shot_frames_reviewed()
        (self.out / 'clips/seg2.mp4').unlink()
        (self.out / 'clips/seg3.mp4').unlink()
        self.job.state['segs']['1'] = {'zh': 'recipient stands', 'h3': h3(), 'error': 'err'}
        self.job.state['segs']['2'] = {'zh': 'recipient stands', 'h3': h3(), 'task': 'inflight-seg3', 'submitted': 1.0}
        self.job.save()
        self.prepare('dead seg2', number=2)
        with self.assertRaises(RuntimeError):
            self.generate(submit=lambda g, **kw: 'dead-seg2-id', poll={'return_value': {'state': 'failed', 'error': 'oom'}})
        with self.assertRaises(InputError) as locked_ctx:
            self.prepare('cannot touch inflight seg3', number=3)
        self.assertEqual(locked_ctx.exception.key, 'segment_locked')
        self.prepare('regenerate seg1', number=1)
        self.generate(submit=lambda g, **kw: 'ok-seg1')
        st = film.Job(self.out).state['segs']
        self.assertTrue(st['1'].get('terminal_failed'))
        self.assertEqual(st['1'].get('task'), 'dead-seg2-id')
        self.assertEqual(st['2'].get('task'), 'inflight-seg3')

    def test_terminal_segment_with_inflight_sibling_guards_continue_dirty_frames(self):
        self._setup_shot_frames_reviewed()
        (self.out / 'clips/seg2.mp4').unlink()
        (self.out / 'clips/seg3.mp4').unlink()
        self.job.state['segs']['1'] = {'zh': 'recipient stands', 'h3': h3(), 'error': 'err'}
        self.job.state['segs']['2'] = {'zh': 'recipient stands', 'h3': h3(), 'task': 'inflight-seg3', 'submitted': 1.0}
        self.job.save()
        self.prepare('dead seg2', number=2)
        with self.assertRaises(RuntimeError):
            self.generate(submit=lambda g, **kw: 'dead-seg2-id', poll={'return_value': {'state': 'failed', 'error': 'oom'}})
        self.jobs.find(self.identifier)['status'] = 'succeeded'
        # Dirtying s3-1 (in-flight sibling) rejects continue_film with 400
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[3]))
        snap_blocked = self.client.get('/api/jobs/job-1').json
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch') as launch:
            res_blocked = self.client.post('/api/jobs/job-1/continue',
                                           json={'approved': True, 'review_version': snap_blocked['review']['version']},
                                           headers=self.headers)
        self.assertEqual(res_blocked.status_code, 400)
        launch.assert_not_called()
        # Dirtying only s1-1 (neither seg2 nor seg3 affected) accepts and preserves both seg2 terminal_failed and seg3 task
        write_json(self.out / 'inputs/review.json', dict(dirty_segments=[1]))
        snap_ok = self.client.get('/api/jobs/job-1').json
        with patch('workbench.jobs.preflight'), patch.object(self.jobs, 'launch'):
            res_ok = self.client.post('/api/jobs/job-1/continue',
                                      json={'approved': True, 'review_version': snap_ok['review']['version']},
                                      headers=self.headers)
        self.assertEqual(res_ok.status_code, 200, res_ok.json)
        self.assertTrue((self.out / 'inputs/segment-edit.json').exists())
        self.assertTrue(film.Job(self.out).state['segs']['1'].get('terminal_failed'))
        self.assertEqual(film.Job(self.out).state['segs']['2'].get('task'), 'inflight-seg3')

    def test_stage_prompts_and_stage_generate_cancel_unstarted_segments_on_fatal_error(self):
        from h3ref import http as http_mod
        shots = [dict(start=i * 8, end=(i + 1) * 8, action=f'recipient action {i}') for i in range(6)]
        script = {**self.script, 'shots': shots}
        segs = film.plan(script['shots'])
        pics = {s['idx']: [] for s in segs}
        pjob = film.Job(self.out.parent / 'cancel-prompts')
        prompt_calls = []
        def fail_rewrite(zh, *_a):
            prompt_calls.append(zh)
            raise http_mod.HTTPResponseError(401, '401 invalid key')
        with patch.object(rewrite, 'rewrite', side_effect=fail_rewrite):
            with self.assertRaises(http_mod.HTTPResponseError):
                film.stage_prompts(pjob, script, segs, pics)
        self.assertLessEqual(len(prompt_calls), 3, f'Queued segments beyond pool size ran after fatal error: {len(prompt_calls)}')

        gjob = film.Job(self.out.parent / 'cancel-gen')
        for s in segs:
            gjob.put('segs', str(s['idx']), zh='z', h3=h3())
        gen_calls = []
        def fail_submit(graph, **_kw):
            gen_calls.append(graph)
            raise rh.SubmissionRejected('605 NOT_ENOUGH_BALANCE')
        with patch.object(film.config, 'RH_CONCURRENCY', 2), \
             patch.object(rh, 'submit_with_retry', side_effect=fail_submit):
            with self.assertRaises(rh.SubmissionRejected):
                film.stage_generate(gjob, script, segs, pics, 'shot')
        self.assertLessEqual(len(gen_calls), 2, f'Queued segments beyond RH_CONCURRENCY ran after fatal error: {len(gen_calls)}')

    def test_preexisting_blocker_on_later_segment_does_not_rewrite_or_submit_earlier_unstarted_segment(self):
        for mode in ('shot', 'none'):
            with self.subTest(mode=mode):
                case = SegmentPrompts()
                case.setUp()
                self.addCleanup(case.doCleanups)
                if mode == 'shot':
                    case._setup_shot_frames_reviewed()
                else:
                    from PIL import Image
                    Image.new('RGB', (8, 8), 'blue').save(case.script['cast'][0]['picture'])
                for n in (1, 2, 3):
                    (case.out / f'clips/seg{n}.mp4').unlink()
                    (case.out / f'clips/seg{n}-last.jpg').unlink()
                case.job.state['segs'] = {
                    '0': {'zh': 'unstarted seg1 without h3'},
                    '1': {'zh': 'dead seg2', 'h3': h3(), 'task': 'dead-seg2', 'terminal_failed': True, 'error': 'oom'},
                    '2': {'zh': 'inflight seg3', 'h3': h3(), 'task': f'inflight-seg3-{mode}', 'submitted': 1.0},
                }
                case.job.save()
                polled = []
                with patch.object(rewrite, 'rewrite', side_effect=AssertionError('rewrote unstarted seg1 despite blocker on seg2')), \
                     patch.object(rh, 'submit_with_retry', side_effect=AssertionError('submitted unstarted seg1 despite blocker on seg2')), \
                     patch.object(rh, 'poll', side_effect=lambda t: polled.append(t) or {'state': 'done', 'url': 'offline://s3', 'cny': 1.0}), \
                     patch.object(film.http, 'download', side_effect=lambda u, p: p.write_bytes(b'SEG3')), \
                     patch.object(media, 'last_frame', side_effect=lambda c, p: p.write_bytes(b'TAIL3')), \
                     patch.object(media, 'duration', return_value=8):
                    with self.assertRaises(RuntimeError):
                        film.run(case.out, case.out / 'inputs/film.json', mode)
                self.assertEqual(polled, [f'inflight-seg3-{mode}'])
                self.assertEqual((case.out / 'clips/seg3.mp4').read_bytes(), b'SEG3')

    def test_inflight_ambiguous_or_generated_segment_blocks_http_assemble(self):
        for scenario in ('inflight', 'ambiguous', 'generated'):
            with self.subTest(scenario=scenario):
                case = SegmentPrompts()
                case.setUp()
                self.addCleanup(case.doCleanups)
                rev = case.prepare(f'{scenario} edit')
                write_json(case.out / 'inputs/request.json', case.request)
                if scenario == 'inflight':
                    with self.assertRaises(RuntimeError):
                        case.generate(poll={'side_effect': RuntimeError('poll lost')})
                    expected_code = 'segment_locked'
                elif scenario == 'ambiguous':
                    with patch.object(rh, '_post', side_effect=RuntimeError('lost')):
                        with self.assertRaises(RuntimeError):
                            case.generate(submit=rh.submit_with_retry)
                    expected_code = 'segment_locked'
                else:
                    pending_path = case.out / 'inputs/segment-edit.json'
                    pending = json.loads(pending_path.read_text())
                    pending.update(generated=True, publishing=False, applied=False, phase='publish')
                    write_json(pending_path, pending)
                    expected_code = 'segment_publishing'
                case.jobs.find(case.identifier).update(status='failed', operation='segment')
                snap = case.client.get('/api/jobs/job-1').json
                self.assertFalse(snap['can_assemble'])
                before_req = (case.out / 'inputs/request.json').read_bytes()
                with patch('workbench.jobs.preflight'), patch.object(case.jobs, 'launch') as launch:
                    res = case.client.post('/api/jobs/job-1/assemble',
                                           json={'version': snap['assembly']['version']},
                                           headers=case.headers)
                from workbench.i18n import message as wb_message
                self.assertEqual(res.status_code, 400, res.json)
                self.assertEqual(res.json['error'], wb_message(expected_code, 'en', number=2))
                launch.assert_not_called()
                self.assertEqual((case.out / 'inputs/request.json').read_bytes(), before_req)
                if scenario == 'generated':
                    with self.assertRaises(InputError):
                        segments.prepare_assembly(case.request, snap['assembly']['version'])

    def test_continue_film_preflights_reconciled_source_edit_before_deleting_paid_clip(self):
        import os
        self._setup_shot_frames_reviewed()
        self.job.put('segs', '1', prompt_override={'format': 'h3', 'text': h3('H3'), 'source_version': 'v0'})
        self.prepare('new source needing text key', 'source', number=2)
        write_json(self.out / 'inputs/review.json', {'dirty_segments': [2]})
        before_review = (self.out / 'inputs/review.json').read_bytes()
        before_seg = (self.out / 'inputs/segment-edit.json').read_bytes()
        snap = self.client.get('/api/jobs/job-1').json
        with patch.dict(os.environ, {'PATH': os.environ.get('PATH', ''), 'RUNNINGHUB_API_KEY': 'rh', 'APIMART_API_KEY': 'ap', 'LLM_PROVIDER': 'gemini'}, clear=True), \
             patch('shutil.which', return_value='/fake/ffmpeg'):
            app = create_app(self.out.parent / 'st-reconcile', self.out.parent / '.env-reconcile')
            try:
                app.extensions['jobs'].records[self.identifier] = dict(self.jobs.records[self.identifier])
                with patch.object(app.extensions['jobs'], 'launch') as launch:
                    res = app.test_client().post('/api/jobs/job-1/continue',
                                                 json={'approved': True, 'review_version': snap['review']['version']},
                                                 headers=self.headers)
                self.assertEqual(res.status_code, 400, res.json)
                launch.assert_not_called()
                self.assertEqual((self.out / 'inputs/review.json').read_bytes(), before_review)
                self.assertEqual((self.out / 'inputs/segment-edit.json').read_bytes(), before_seg)
            finally:
                app.extensions['jobs'].close()

    def test_pre_task_segment_failure_sibling_drain_freezes_clipless_and_preserves_clipful_draft(self):
        import os
        # Part A: Clip-having seg2 with unapplied draft keeps its edited prompt when seg1 replaces segment-edit.json
        self.prepare('unapplied seg2 draft prompt', 'source', number=2)
        self.prepare('seg1 replacement prompt', 'source', number=1)
        self.generate()
        self.assertEqual(self.card(2)['prompt'], 'unapplied seg2 draft prompt')

        # Part B: Clipless seg1 pre-task failure while seg2 is in-flight drains seg2 without LLM keys or submitting seg1
        case = SegmentPrompts()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case._setup_shot_frames_reviewed()
        (case.out / 'clips/seg1.mp4').unlink()
        (case.out / 'clips/seg1-last.jpg').unlink()
        (case.out / 'clips/seg2.mp4').unlink()
        (case.out / 'clips/seg2-last.jpg').unlink()
        case.job.state['segs']['0'] = {'zh': 'initial s1', 'error': 'initial err'}
        case.job.state['segs']['1'] = {'zh': 's2', 'h3': h3(), 'task': 'inflight-s2', 'submitted': 1.0}
        case.job.save()
        case.prepare('edited s1 failing rewrite', 'source', number=1)
        write_json(case.out / 'inputs/request.json', case.request)
        with patch.object(rewrite, 'backend', return_value='llm'), \
             patch.object(rewrite, 'guide', return_value='g'), \
             patch.object(rewrite, '_llm', side_effect=RuntimeError('llm quota')):
            with self.assertRaises(RuntimeError):
                segments.regenerate(case.request)
        case.jobs.find(case.identifier).update(status='failed', operation='segment')
        # Unmocked preflight with ONLY RUNNINGHUB_API_KEY (NO LLM key!) must allow /resume to drain inflight-s2!
        with patch.dict(os.environ, {'PATH': os.environ.get('PATH', ''), 'RUNNINGHUB_API_KEY': 'rh-only', 'LLM_PROVIDER': 'gemini'}, clear=True), \
             patch('shutil.which', return_value='/fake/ffmpeg'):
            app = create_app(case.out.parent / 'st-drain', case.out.parent / '.env-drain')
            try:
                app.extensions['jobs'].records[case.identifier] = dict(case.jobs.records[case.identifier])
                with patch.object(app.extensions['jobs'], 'launch'):
                    res = app.test_client().post('/api/jobs/job-1/resume', json={}, headers=case.headers)
                self.assertEqual(res.status_code, 200, res.json)
            finally:
                app.extensions['jobs'].close()
        polled = []
        with patch.object(rewrite, 'rewrite', side_effect=AssertionError('rewrote failed s1 during drain')), \
             patch.object(rh, 'submit_with_retry', side_effect=AssertionError('submitted failed s1 during drain')), \
             patch.object(rh, 'poll', side_effect=lambda t: polled.append(t) or {'state': 'done', 'url': 'offline://s2', 'cny': 1.0}), \
             patch.object(film.http, 'download', side_effect=lambda u, p: p.write_bytes(b'DRAINED S2')), \
             patch.object(media, 'last_frame', side_effect=lambda c, p: p.write_bytes(b'TAIL2')), \
             patch.object(media, 'duration', return_value=8):
            with self.assertRaises(RuntimeError):
                storyboard.run(json.loads((case.out / 'inputs/request.json').read_text()))
        self.assertEqual(polled, ['inflight-s2'])
        self.assertEqual((case.out / 'clips/seg2.mp4').read_bytes(), b'DRAINED S2')
        card1 = case.card(1)
        self.assertTrue(card1['editable'])
        self.assertTrue(card1['can_regenerate'])
        self.assertEqual(card1['prompt'], 'edited s1 failing rewrite')

    def test_post_drain_pipeline_resume_clears_attempt_blocked_and_validates_llm_preflight(self):
        import os
        self._setup_shot_frames_reviewed()
        (self.out / 'clips/seg1.mp4').unlink()
        (self.out / 'clips/seg1-last.jpg').unlink()
        self.job.state['segs']['0'] = {
            'zh': 'edited s1 failing rewrite',
            'h3': None,
            'h3_source': None,
            'prompt_override': {'format': 'source', 'text': 'edited s1 failing rewrite', 'source_version': 'sv1'},
            'error': 'RuntimeError: llm quota',
            'attempt_blocked': True,
        }
        self.job.save()
        req = json.loads((self.out / 'inputs/request.json').read_text(encoding='utf-8'))
        req['operation'] = 'pipeline'
        write_json(self.out / 'inputs/request.json', req)
        self.jobs.find(self.identifier).update(status='failed', operation='pipeline')

        # 1. With no sibling task_pending left, /resume without LLM key must fail preflight instead of looping into attempt_blocked crash
        with patch.dict(os.environ, {'PATH': os.environ.get('PATH', ''), 'RUNNINGHUB_API_KEY': 'rh-only', 'LLM_PROVIDER': 'gemini'}, clear=True), \
             patch('shutil.which', return_value='/fake/ffmpeg'):
            app = create_app(self.out.parent / 'st-postdrain-1', self.out.parent / '.env-postdrain-1')
            try:
                app.extensions['jobs'].records[self.identifier] = dict(self.jobs.records[self.identifier])
                with patch.object(app.extensions['jobs'], 'launch') as launch:
                    r_no_llm = app.test_client().post('/api/jobs/job-1/resume', json={}, headers=self.headers)
                self.assertEqual(r_no_llm.status_code, 400, r_no_llm.json)
                launch.assert_not_called()
            finally:
                app.extensions['jobs'].close()

        # 2. With valid LLM + RunningHub keys, /resume clears attempt_blocked so film.run rewrites and generates seg1
        with patch.dict(os.environ, {'PATH': os.environ.get('PATH', ''), 'RUNNINGHUB_API_KEY': 'rh-ok', 'GEMINI_API_KEY': 'gm-ok', 'LLM_PROVIDER': 'gemini'}, clear=True), \
             patch('shutil.which', return_value='/fake/ffmpeg'):
            app = create_app(self.out.parent / 'st-postdrain-2', self.out.parent / '.env-postdrain-2')
            try:
                app.extensions['jobs'].records[self.identifier] = dict(self.jobs.records[self.identifier])
                with patch.object(app.extensions['jobs'], 'launch') as launch:
                    r_ok = app.test_client().post('/api/jobs/job-1/resume', json={}, headers=self.headers)
                self.assertEqual(r_ok.status_code, 200, r_ok.json)
                launch.assert_called_once()
            finally:
                app.extensions['jobs'].close()
        self.assertFalse(film.Job(self.out).state['segs']['0'].get('attempt_blocked'))
        with patch.object(rewrite, 'rewrite', return_value=h3()) as rw_mock, \
             patch.object(rh, 'upload', return_value='ref.png'), \
             patch.object(rh, 'graph', return_value={}), \
             patch.object(rh, 'submit_with_retry', return_value='new-s1-task'), \
             patch.object(rh, 'poll', return_value={'state': 'done', 'url': 'offline://s1', 'cny': 1.0}), \
             patch.object(film.http, 'download', side_effect=lambda u, p: p.write_bytes(b'NEW S1')), \
             patch.object(media, 'last_frame', side_effect=lambda c, p: p.write_bytes(b'TAIL1')), \
             patch.object(media, 'join', side_effect=lambda ps, p: p.write_bytes(b'FINAL')), \
             patch.object(media, 'duration', return_value=16):
            storyboard.run(json.loads((self.out / 'inputs/request.json').read_text(encoding='utf-8')))
        rw_mock.assert_called_once()
        self.assertEqual(rw_mock.call_args.args[0], 'edited s1 failing rewrite')
        self.assertEqual((self.out / 'clips/seg1.mp4').read_bytes(), b'NEW S1')

    def test_reconciled_records_clears_stale_h3_when_staged_wrote_updated_zh_before_failing(self):
        (self.out / 'clips/seg1.mp4').unlink()
        self.job.state['segs']['0'] = {'zh': 'Old zh', 'h3': h3(), 'h3_source': 'Old zh'}
        self.job.save()
        rev = 'rev-stale-h3'
        staged_dir = self.out / 'inputs' / f'segment-{rev}'
        staged_dir.mkdir(parents=True, exist_ok=True)
        write_json(staged_dir / 'state.json', {'segs': {'0': {'zh': 'New zh', 'error': 'RewriteError: partial'}}})
        write_json(self.out / 'inputs/segment-edit.json', {
            'number': 1, 'revision': rev, 'explicit_prompt': True,
            'prompt_format': 'source', 'prompt': 'New zh', 'reuse_h3': False,
            'previous': {'zh': 'Old zh', 'h3': h3(), 'h3_source': 'Old zh'}, 'applied': False,
        })
        reconciled, changed, _ = segments.preview_reconciled_segments(self.out, keep_pending=False, supersede=False)
        self.assertTrue(changed)
        self.assertIsNone(reconciled['0'].get('h3'))
        self.assertIsNone(reconciled['0'].get('h3_source'))
