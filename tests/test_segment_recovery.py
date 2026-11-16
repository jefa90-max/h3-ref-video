"""Regressions for interrupted backups and ambiguous paid submissions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from h3ref import film, media, rh
from workbench import segments
from workbench.jobs import write_json


class SegmentRecovery(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.out = Path(self.temporary.name)
        (self.out / 'inputs').mkdir()
        (self.out / 'clips').mkdir()
        self.clip = self.out / 'clips' / 'seg1.mp4'
        self.clip.write_bytes(b'ORIGINAL COMPLETE CLIP')
        self.tail = self.out / 'clips' / 'seg1-last.jpg'
        self.tail.write_bytes(b'ORIGINAL TAIL')
        self.final = self.out / 'film.mp4'
        self.final.write_bytes(b'ORIGINAL COMPLETE FILM')
        self.request = {'kind': 'film', 'demo': True, 'frames': 'none', 'lang': 'en', 'output_dir': str(self.out),
                        'film': {'aspect': '9:16', 'shots': [{'start': 0, 'end': 5, 'action': 'A ball rolls.'}]}}
        film.Job(self.out).put('segs', '0', h3='original prompt', zh='original', file=str(self.clip), cny=0.1, task='old-task')

    def prepare_clip(self, live=False):
        self.request.update(operation='segment', segment=1, demo=not live)
        self.request['segment_revision'] = segments.prepare_regeneration(self.request, 1, segments.clip_version(self.clip, self.out))

    def test_interrupted_clip_backup_cannot_replace_the_only_complete_old_clip(self):
        self.prepare_clip()
        copy_file = segments.shutil.copy2
        def interrupted_copy(source, target, *args, **kwargs):
            if Path(source) == self.clip:
                Path(target).write_bytes(b'ORIG')
                raise OSError('disk interrupted')
            return copy_file(source, target, *args, **kwargs)
        def render(output, *args):
            output.write_bytes(b'NEW CLIP')
        with patch('workbench.demo.render_clip', side_effect=render), patch.object(media, 'last_frame', side_effect=lambda source, output: output.write_bytes(b'NEW TAIL')), patch.object(media, 'duration', return_value=5):
            with patch.object(segments.shutil, 'copy2', side_effect=interrupted_copy):
                with self.assertRaisesRegex(OSError, 'disk interrupted'):
                    segments.regenerate(self.request)
            self.assertEqual(self.clip.read_bytes(), b'ORIGINAL COMPLETE CLIP')
            segments.regenerate(self.request)
        self.assertEqual(self.clip.read_bytes(), b'NEW CLIP')
        backup = next((self.out / 'inputs').glob('segment-*/previous-seg1.mp4'))
        self.assertEqual(backup.read_bytes(), b'ORIGINAL COMPLETE CLIP')

    def test_interrupted_film_backup_cannot_replace_the_only_complete_old_film(self):
        self.request['operation'] = 'assemble'
        segments.prepare_assembly(self.request, segments.assembly_status(self.request)['version'])
        copy_file = segments.shutil.copy2
        def interrupted_copy(source, target, *args, **kwargs):
            if Path(source) == self.final:
                Path(target).write_bytes(b'ORIG')
                raise OSError('disk interrupted')
            return copy_file(source, target, *args, **kwargs)
        with patch.object(media, 'join', side_effect=lambda paths, output: output.write_bytes(b'NEW FILM')), patch.object(media, 'duration', return_value=5):
            with patch.object(segments.shutil, 'copy2', side_effect=interrupted_copy):
                with self.assertRaisesRegex(OSError, 'disk interrupted'):
                    segments.assemble(self.request)
            self.assertEqual(self.final.read_bytes(), b'ORIGINAL COMPLETE FILM')
            segments.assemble(self.request)
        self.assertEqual(self.final.read_bytes(), b'NEW FILM')
        self.assertEqual(next((self.out / 'inputs').glob('previous-film-*.mp4')).read_bytes(), b'ORIGINAL COMPLETE FILM')

    def test_lost_submission_response_never_resubmits_on_resume(self):
        self.prepare_clip(live=True)
        with patch.object(rh, '_post', side_effect=[RuntimeError('response lost'), {'code': 0, 'data': {'taskId': 'duplicate-task'}}]) as submitted, patch.object(rh, 'poll', side_effect=RuntimeError('poll reached')):
            with self.assertRaises(RuntimeError):
                segments.regenerate(self.request)
            with self.assertRaises(RuntimeError):
                segments.regenerate(self.request)
        self.assertEqual(submitted.call_count, 1, 'An unknown accepted task must not be submitted again')
        self.assertEqual(self.clip.read_bytes(), b'ORIGINAL COMPLETE CLIP')

    def test_explicit_rejection_allows_a_new_submission_on_resume(self):
        self.prepare_clip(live=True)
        with patch.object(rh, '_post', side_effect=[{'code': 605, 'msg': 'NOT_ENOUGH_BALANCE'}, {'code': 0, 'data': {'taskId': 'accepted-task'}}]) as submitted, patch.object(rh, 'poll', side_effect=RuntimeError('poll reached')):
            with self.assertRaisesRegex(RuntimeError, '605'):
                segments.regenerate(self.request)
            with self.assertRaisesRegex(RuntimeError, 'poll reached'):
                segments.regenerate(self.request)
        self.assertEqual(submitted.call_count, 2)
        attempt = next((self.out / 'inputs').glob('segment-*/state.json'))
        self.assertEqual(json.loads(attempt.read_text())['segs']['0']['task'], 'accepted-task')

    def test_interrupted_queue_backoff_allows_submission_on_resume(self):
        self.prepare_clip(live=True)
        responses = [{'code': 421, 'msg': 'QUEUE_MAXED'}, {'code': 0, 'data': {'taskId': 'accepted-after-resume'}}]
        with patch.object(rh, '_post', side_effect=responses) as submitted, patch.object(rh, 'poll', side_effect=RuntimeError('poll reached')):
            with patch.object(rh.time, 'sleep', side_effect=InterruptedError('backoff interrupted')):
                with self.assertRaisesRegex(InterruptedError, 'backoff interrupted'):
                    segments.regenerate(self.request)
            self.assertEqual(self.clip.read_bytes(), b'ORIGINAL COMPLETE CLIP')
            with self.assertRaisesRegex(RuntimeError, 'poll reached'):
                segments.regenerate(self.request)
        self.assertEqual(submitted.call_count, 2)
        attempt = next((self.out / 'inputs').glob('segment-*/state.json'))
        self.assertEqual(json.loads(attempt.read_text())['segs']['0']['task'], 'accepted-after-resume')

    def test_queue_retry_rearms_unknown_marker_before_ambiguous_submission(self):
        self.prepare_clip(live=True)
        responses = [{'code': 421, 'msg': 'QUEUE_MAXED'}, RuntimeError('response lost')]
        with patch.object(rh, '_post', side_effect=responses) as submitted, patch.object(rh.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'response lost'):
                segments.regenerate(self.request)
            with self.assertRaises(RuntimeError):
                segments.regenerate(self.request)
        self.assertEqual(submitted.call_count, 2, 'Resume must not repeat the ambiguous second submission')
        self.assertEqual(self.clip.read_bytes(), b'ORIGINAL COMPLETE CLIP')

    def test_preparing_a_retry_does_not_mark_a_stale_legacy_film_current(self):
        os.utime(self.final, ns=(1_000_000_000, 1_000_000_000))
        os.utime(self.clip, ns=(2_000_000_000, 2_000_000_000))
        self.assertTrue(segments.assembly_status(self.request)['pending'])
        self.prepare_clip()
        self.assertTrue(segments.assembly_status(self.request)['pending'])

    def test_uncommitted_publication_blocks_assembly_without_consuming_mixed_clips(self):
        write_json(self.out / 'inputs/segment-edit.json', {'number': 1, 'publishing': True, 'applied': False, 'previous': film.Job(self.out).state['segs']['0']})
        with patch.object(media, 'join', side_effect=AssertionError('assembled mixed version')):
            with self.assertRaises(ValueError):
                segments.prepare_assembly(self.request, 'old-version')
            with self.assertRaises(ValueError):
                segments.assemble(self.request)
        self.assertEqual(self.final.read_bytes(), b'ORIGINAL COMPLETE FILM')

    def test_explicit_rejection_resume_completes_publication_and_marks_applied(self):
        self.prepare_clip(live=True)
        with patch.object(rh, '_post', side_effect=[{'code': 605, 'msg': 'NOT_ENOUGH_BALANCE'}, {'code': 0, 'data': {'taskId': 'ok-task'}}]), \
             patch.object(rh, 'upload', return_value='up.png'), \
             patch.object(rh, 'poll', return_value={'state': 'done', 'url': 'https://cdn.example/clip.mp4', 'cny': 0.2}), \
             patch.object(film.http, 'download', side_effect=lambda u, p: p.write_bytes(b'RESUMED CLIP')), \
             patch.object(media, 'last_frame', side_effect=lambda c, p: p.write_bytes(b'RESUMED TAIL')), \
             patch.object(media, 'duration', return_value=5):
            with self.assertRaisesRegex(RuntimeError, '605'):
                segments.regenerate(self.request)
            segments.regenerate(self.request)
        self.assertEqual(self.clip.read_bytes(), b'RESUMED CLIP')
        self.assertEqual(film.Job(self.out).state['segs']['0']['task'], 'ok-task')
        self.assertTrue(json.loads((self.out / 'inputs/segment-edit.json').read_text())['applied'])

    def _app_with_job(self, status='failed', operation='assemble'):
        from workbench import storyboard
        from workbench.server import create_app
        root = self.out.parent
        write_json(self.out / 'inputs/film.json', self.request['film'])
        req = {**self.request, 'operation': operation}
        write_json(self.out / 'inputs/request.json', req)
        review = storyboard.review_status(req, [])
        write_json(self.out / 'inputs/review.json', {'dirty_segments': [], 'confirmed_version': review['version']})
        app = create_app(root / f'st-{id(req)}', root / '.env')
        self.addCleanup(app.extensions['jobs'].close)
        jobs = app.extensions['jobs']
        jobs.records['job-a'] = {'id': 'job-a', 'kind': 'film', 'demo': True, 'status': status,
                                 'operation': operation, 'output_dir': str(self.out), 'lang': 'en', 'created_at': 1, 'error': 'err'}
        return app.test_client(), jobs, {'X-Workbench': '1', 'Accept-Language': 'en'}

    def test_failed_ungenerated_assembly_resumes_via_http_and_settles(self):
        client, jobs, headers = self._app_with_job(status='failed', operation='assemble')
        segments.prepare_assembly(self.request, segments.assembly_status(self.request)['version'])
        with patch('workbench.jobs.preflight'), patch.object(jobs, 'launch'):
            res = client.post('/api/jobs/job-a/resume', json={}, headers=headers)
        self.assertEqual(res.status_code, 200, res.json)
        req = json.loads((self.out / 'inputs/request.json').read_text(encoding='utf-8'))
        with patch.object(media, 'join', side_effect=lambda paths, out: out.write_bytes(b'JOINED RESUME')), \
             patch.object(media, 'duration', return_value=5):
            segments.assemble(req)
        self.assertEqual(self.final.read_bytes(), b'JOINED RESUME')
        self.assertTrue((self.out / 'inputs/assembly.json').is_file())
        self.assertTrue(json.loads((self.out / 'inputs/assembly-edit.json').read_text())['applied'])

    def test_failed_ungenerated_assembly_can_reassemble_fresh_revision(self):
        client, jobs, headers = self._app_with_job(status='failed', operation='assemble')
        segments.prepare_assembly(self.request, segments.assembly_status(self.request)['version'])
        old_rev = json.loads((self.out / 'inputs/assembly-edit.json').read_text())['revision']
        snap = client.get('/api/jobs/job-a').json
        self.assertTrue(snap['can_assemble'])
        with patch('workbench.jobs.preflight'), patch.object(jobs, 'launch'):
            res = client.post('/api/jobs/job-a/assemble', json={'version': snap['assembly']['version']}, headers=headers)
        self.assertEqual(res.status_code, 200, res.json)
        new_pending = json.loads((self.out / 'inputs/assembly-edit.json').read_text())
        self.assertNotEqual(new_pending['revision'], old_rev)
        req = json.loads((self.out / 'inputs/request.json').read_text(encoding='utf-8'))
        with patch.object(media, 'join', side_effect=lambda paths, out: out.write_bytes(b'JOINED REASSEMBLE')), \
             patch.object(media, 'duration', return_value=5):
            segments.assemble(req)
        self.assertEqual(self.final.read_bytes(), b'JOINED REASSEMBLE')
        self.assertTrue(json.loads((self.out / 'inputs/assembly-edit.json').read_text())['applied'])

    def test_interrupted_generated_assembly_resumes_via_http_without_rejoining(self):
        client, jobs, headers = self._app_with_job(status='interrupted', operation='assemble')
        segments.prepare_assembly(self.request, segments.assembly_status(self.request)['version'])
        copy_file = segments.shutil.copy2
        def interrupt_backup(src, dst, *a, **kw):
            if Path(src) == self.final:
                raise OSError('backup interrupted')
            return copy_file(src, dst, *a, **kw)
        req = json.loads((self.out / 'inputs/request.json').read_text(encoding='utf-8'))
        with patch.object(media, 'join', side_effect=lambda paths, out: out.write_bytes(b'CANDIDATE FILM')), \
             patch.object(media, 'duration', return_value=5), \
             patch.object(segments.shutil, 'copy2', side_effect=interrupt_backup):
            with self.assertRaises(OSError):
                segments.assemble(req)
        self.assertTrue(json.loads((self.out / 'inputs/assembly-edit.json').read_text())['generated'])
        with patch('workbench.jobs.preflight'), patch.object(jobs, 'launch'):
            res = client.post('/api/jobs/job-a/resume', json={}, headers=headers)
        self.assertEqual(res.status_code, 200, res.json)
        with patch.object(media, 'join', side_effect=AssertionError('rejoined existing candidate')):
            segments.assemble(json.loads((self.out / 'inputs/request.json').read_text(encoding='utf-8')))
        self.assertEqual(self.final.read_bytes(), b'CANDIDATE FILM')
        self.assertTrue(json.loads((self.out / 'inputs/assembly-edit.json').read_text())['applied'])

    def test_interrupted_generated_assembly_can_reassemble_fresh_revision(self):
        client, jobs, headers = self._app_with_job(status='interrupted', operation='assemble')
        segments.prepare_assembly(self.request, segments.assembly_status(self.request)['version'])
        pending_path = self.out / 'inputs/assembly-edit.json'
        pending = json.loads(pending_path.read_text())
        old_rev = pending['revision']
        (self.out / 'inputs' / f"assembled-{old_rev}.mp4").write_bytes(b'OLD CANDIDATE')
        pending['generated'] = True
        write_json(pending_path, pending)
        snap = client.get('/api/jobs/job-a').json
        self.assertTrue(snap['can_assemble'])
        with patch('workbench.jobs.preflight'), patch.object(jobs, 'launch'):
            res = client.post('/api/jobs/job-a/assemble', json={'version': snap['assembly']['version']}, headers=headers)
        self.assertEqual(res.status_code, 200, res.json)
        new_rev = json.loads(pending_path.read_text())['revision']
        self.assertNotEqual(new_rev, old_rev)
        with patch.object(media, 'join', side_effect=lambda paths, out: out.write_bytes(b'FRESH CANDIDATE')), \
             patch.object(media, 'duration', return_value=5):
            segments.assemble(json.loads((self.out / 'inputs/request.json').read_text(encoding='utf-8')))
        self.assertEqual(self.final.read_bytes(), b'FRESH CANDIDATE')
        self.assertTrue(json.loads(pending_path.read_text())['applied'])

    def test_stale_pending_assembly_rejects_resume_without_overwriting_film(self):
        from workbench.i18n import InputError
        self.request['operation'] = 'assemble'
        segments.prepare_assembly(self.request, segments.assembly_status(self.request)['version'])
        self.clip.write_bytes(b'UPDATED CLIP BYTES AFTER PREPARE')
        with self.assertRaises(InputError) as caught:
            segments.assemble(self.request)
        self.assertEqual(caught.exception.key, 'assembly_changed')
        self.assertEqual(self.final.read_bytes(), b'ORIGINAL COMPLETE FILM')
        self.assertFalse(json.loads((self.out / 'inputs/assembly-edit.json').read_text())['applied'])

    def test_stale_failed_assembly_can_reassemble_with_updated_clip_version(self):
        client, jobs, headers = self._app_with_job(status='failed', operation='assemble')
        segments.prepare_assembly(self.request, segments.assembly_status(self.request)['version'])
        self.clip.write_bytes(b'UPDATED CLIP BYTES AFTER FAILED ASSEMBLE')
        snap = client.get('/api/jobs/job-a').json
        self.assertTrue(snap['can_assemble'], 'Failed assemble job with ready clips must allow fresh reassemble')
        with patch('workbench.jobs.preflight'), patch.object(jobs, 'launch'):
            res = client.post('/api/jobs/job-a/assemble', json={'version': snap['assembly']['version']}, headers=headers)
        self.assertEqual(res.status_code, 200, res.json)
        with patch.object(media, 'join', side_effect=lambda paths, out: out.write_bytes(b'REASSEMBLED UPDATED')), \
             patch.object(media, 'duration', return_value=5):
            segments.assemble(json.loads((self.out / 'inputs/request.json').read_text(encoding='utf-8')))
        self.assertEqual(self.final.read_bytes(), b'REASSEMBLED UPDATED')
        self.assertEqual(
            json.loads((self.out / 'inputs/assembly.json').read_text())['version'],
            segments.assembly_status(self.request)['version'],
        )
        self.assertTrue(json.loads((self.out / 'inputs/assembly-edit.json').read_text())['applied'])


if __name__ == '__main__':
    unittest.main()
