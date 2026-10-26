import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/video-edit/scripts/video_edit.py'
spec = importlib.util.spec_from_file_location('video_edit', SCRIPT)
video_edit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(video_edit)


class VideoEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        for name, rate, audio in [('a', 24, True), ('b', 30, False)]:
            command = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                       f'testsrc2=size=160x90:rate={rate}:duration=3']
            if audio:
                command += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=3']
            video_edit.run(command + ['-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p',
                                      '-c:a', 'aac', str(cls.root / (name + '.mp4'))])
        video_edit.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=yellow:size=320x180',
                        '-frames:v', '1', '-threads', '1', cls.root / 'cover.jpg'])
        video_edit.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=220:sample_rate=48000:duration=3.6', cls.root / 'music.wav'])
        cls.data = {'fps': 30, 'size': [320, 180], 'duration': 3.6,
                    'audio': {'music': 'music.wav'},
                    'cover': {'image': 'cover.jpg', 'hold_seconds': .75, 'fade_seconds': .35, 'embedded_poster': True},
                    'shots': [
                        {'index': 0, 'source': 'a.mp4', 'in': .2, 'duration': 1.6, 'timeline_start': 0,
                         'transition_after': .4, 'label': 'first', 'foreground_audio': True,
                         'overlay': {'image': 'cover.jpg', 'fade_in_start': .1, 'fade_in_seconds': .2,
                                     'fade_out_start': 1.1, 'fade_out_seconds': .2}},
                        {'index': 1, 'source': 'b.mp4', 'in': .1, 'duration': 1.6, 'timeline_start': 1.2,
                         'transition_after': 0, 'label': 'silent source', 'foreground_audio': False},
                        {'index': 2, 'source': 'a.mp4', 'in': 1, 'duration': .8, 'timeline_start': 2.8,
                         'transition_after': 0, 'label': 'last', 'foreground_audio': True}]}
        cls.plan_path = cls.root / 'plan.json'
        video_edit.save_json(cls.plan_path, cls.data)
        cls.plan = video_edit.Plan(cls.plan_path)
        cls.export = cls.root / 'first.mp4'
        video_edit.build_video(cls.plan, cls.export)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_deleted_cache_is_rebuilt_and_unchanged_cache_is_reused(self):
        cache = self.root / 'work/render-cache'
        parts = sorted(cache.glob('*.mov'))
        self.assertEqual(len(parts), 2)
        keep = parts[1]
        timestamp = keep.stat().st_mtime_ns
        parts[0].unlink()
        video_edit.build_video(self.plan, self.root / 'second.mp4')
        self.assertEqual(timestamp, keep.stat().st_mtime_ns)
        shutil.rmtree(cache)
        result = video_edit.build_video(self.plan, self.root / 'rebuilt.mp4')
        self.assertEqual(result['frames'], 108)
        self.assertEqual(result['time_base'], '1/90000')
        self.assertTrue(result['poster'])
        self.assertTrue(result['decoded'])
        self.assertEqual(len(list(cache.glob('*.mov'))), 2)

    def test_atomic_export_keeps_previous_file_when_validation_fails(self):
        output = self.root / 'protected-final.mp4'
        output.write_bytes(b'previous-delivery')
        original = video_edit.verify_media
        def reject_final(path, *args, **kwargs):
            if str(path).endswith('.partial.mp4'):
                raise ValueError('injected export validation failure')
            return original(path, *args, **kwargs)
        with patch.object(video_edit, 'verify_media', side_effect=reject_final):
            with self.assertRaisesRegex(ValueError, 'injected'):
                video_edit.build_video(self.plan, output)
        self.assertEqual(output.read_bytes(), b'previous-delivery')

    def test_mixed_time_base_regression_is_rejected(self):
        paths = []
        for base in [90000, 16000]:
            path = self.root / f'part-{base}.mp4'
            video_edit.run(['ffmpeg', '-v', 'error', '-y', '-i', self.root / 'a.mp4',
                            '-c', 'copy', '-video_track_timescale', str(base), path])
            paths.append(path)
        with self.assertRaisesRegex(ValueError, 'time bases'):
            video_edit.compatible_parts(paths)
        manifest = self.root / 'bad-concat.txt'
        manifest.write_text(''.join(f"file '{p}'\n" for p in paths))
        broken = self.root / 'bad.mp4'
        video_edit.run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0',
                        '-i', manifest, '-c', 'copy', broken])
        with self.assertRaisesRegex(ValueError, 'duration mismatch'):
            video_edit.verify_media(broken)

    def test_full_motion_review_requires_a_reviewer_and_every_shot(self):
        folder = self.root / 'review'
        result = video_edit.review_project(self.plan, folder, self.export)
        self.assertEqual(result['shots'], 3)
        decision_path = folder / 'review.json'
        review = json.loads(decision_path.read_text())
        self.assertTrue(all(s['decision'] == 'pending' for s in review['shots']))
        self.assertTrue(all((folder / s['clip']).exists() for s in review['shots']))
        self.assertIn('video.controls=true', (folder / 'index.html').read_text())
        with self.assertRaisesRegex(ValueError, 'reviewer'):
            video_edit.require_review(self.plan, decision_path)
        review['reviewer'] = 'synthetic-fixture reviewer'
        video_edit.save_json(decision_path, review)
        with self.assertRaisesRegex(ValueError, 'all shots'):
            video_edit.require_review(self.plan, decision_path)
        for shot in review['shots']:
            shot['decision'] = 'passed'
        video_edit.save_json(decision_path, review)
        video_edit.require_review(self.plan, decision_path)
        changed = copy.deepcopy(self.data)
        changed['shots'][0]['gain_db'] = 1
        video_edit.save_json(self.root / 'changed.json', changed)
        with self.assertRaisesRegex(ValueError, 'different plan'):
            video_edit.require_review(video_edit.Plan(self.root / 'changed.json'), decision_path)
        review['shots'].pop()
        video_edit.save_json(decision_path, review)
        with self.assertRaisesRegex(ValueError, 'every shot'):
            video_edit.require_review(self.plan, decision_path)

    def test_plan_and_source_bounds_are_checked(self):
        data = copy.deepcopy(self.data)
        data['shots'][0]['timeline_start'] = 1
        video_edit.save_json(self.root / 'invalid.json', data)
        with self.assertRaisesRegex(ValueError, 'timeline_start'):
            video_edit.Plan(self.root / 'invalid.json')
        data = copy.deepcopy(self.data)
        data['shots'][0]['in'] = 2
        video_edit.save_json(self.root / 'out-of-range.json', data)
        with self.assertRaisesRegex(ValueError, 'exceeds source'):
            video_edit.source_info(video_edit.Plan(self.root / 'out-of-range.json'), None)

    def test_moved_original_directory_is_resolved_by_filename(self):
        data = copy.deepcopy(self.data)
        for shot in data['shots']:
            shot['source'] = '/not-mounted/' + shot['source']
        video_edit.save_json(self.root / 'moved.json', data)
        plan = video_edit.Plan(self.root / 'moved.json')
        with self.assertRaisesRegex(ValueError, 'original source is missing'):
            video_edit.source_info(plan, None)
        metadata = video_edit.source_info(plan, self.root)
        self.assertEqual(set(metadata), {(self.root / 'a.mp4').resolve(), (self.root / 'b.mp4').resolve()})

    def test_stale_transcript_is_rejected_before_review_generation(self):
        transcript = self.root / 'stale-transcript.json'
        video_edit.save_json(transcript, {'input': str(self.export), 'input_fingerprint': ['stale'], 'segments': []})
        with self.assertRaisesRegex(ValueError, 'changed after transcription'):
            video_edit.review_project(self.plan, self.root / 'stale-review', self.export, transcript=transcript)

    def test_cli_reports_failed_validation(self):
        valid = subprocess.run(['python3', str(SCRIPT), 'verify', str(self.export),
                                '--plan', str(self.plan_path)], capture_output=True, text=True)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        data = copy.deepcopy(self.data)
        data['shots'][0]['timeline_start'] = 1
        video_edit.save_json(self.root / 'cli-invalid.json', data)
        result = subprocess.run(['python3', str(SCRIPT), 'verify', str(self.export),
                                 '--plan', str(self.root / 'cli-invalid.json')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('timeline_start', result.stderr)

    def test_cover_is_burned_into_first_video_frame(self):
        pixels = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(self.export),
                                          '-map', '0:v:0', '-frames:v', '1', '-vf',
                                          'crop=2:2,format=rgb24', '-f', 'rawvideo', '-'])
        self.assertGreater(pixels[0], 220)
        self.assertGreater(pixels[1], 220)
        self.assertLess(pixels[2], 30)

    def test_decoding_rejects_truncated_payload_despite_valid_metadata(self):
        broken = self.root / 'truncated.mp4'
        data = self.export.read_bytes()
        broken.write_bytes(data[:len(data)//2])
        video_edit.verify_media(broken, self.plan.duration, self.plan.frames_total, decode=False)
        with self.assertRaises(RuntimeError):
            video_edit.verify_media(broken)


if __name__ == '__main__':
    unittest.main()
