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


if __name__ == '__main__':
    unittest.main()
