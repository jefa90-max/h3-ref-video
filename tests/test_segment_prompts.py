"""Local prompt edits exercise actual state, rewrite and RH graph boundaries without paid calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from h3ref import film, frames, media, rewrite, rh
from workbench import segments, storyboard
from workbench.jobs import write_json
from workbench.server import create_app


def h3(text='The recipient stands in front of the counter.',picture=True):
    parts=dict(subject_definitions='<Subject 1> Person '+('<Picture 1>' if picture else ''),summary=text,
               retention_analysis='Identity and action.',detailed_description='[Shot 1] '+text,
               overall_soundscape='Footsteps.',non_diegetic_music='N/A')
    return '\n\n'.join(name+':\n'+parts[name] for name in rewrite.SECTIONS)


class SegmentPrompts(unittest.TestCase):
    def setUp(self):
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
        with patch.dict('os.environ',{},clear=True),patch.object(self.jobs,'launch') as launch:
            card=self.card()
            response=self.client.post(self.url,json=dict(version=card['version'],prompt_version=card['prompt_version'],prompt='new source',prompt_format='source'),headers=self.headers)
        self.assertEqual(response.status_code,400,response.json)
        self.assertFalse((self.out/'inputs/segment-edit.json').exists())
        launch.assert_not_called()

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
        with patch.object(segments,'prepare_regeneration',side_effect=OSError('disk error key=VERY_SECRET')),patch.object(self.jobs,'launch') as launch:
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
        with self.assertRaises(ValueError):segments.preflight(self.request,{'RUNNINGHUB_API_KEY':'offline'})
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
