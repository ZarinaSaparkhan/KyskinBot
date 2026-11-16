"""Проверка импорта на искусственных файлах, без личной галереи и Telegram."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_media as media


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kyskin-import-test-')
        self.root = Path(self.tmp.name)
        self.source = self.root/'originals'
        self.output = self.root/'ready'
        self.source.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def make_photo(self, name='photo.heic'):
        ppm = self.root/'image.ppm'
        ppm.write_bytes(b'P6\n64 48\n255\n'+bytes([55, 180, 105])*64*48)
        source = self.source/name
        media.execute(['sips', '-s', 'format', 'heic' if name.endswith('.heic') else 'png', str(ppm), '--out', str(source)])
        return source

    def make_movie(self, *, hdr=False, full_range=False):
        source = self.source/('hdr.mov' if hdr else 'movie.mov')
        args = [media.ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-f', 'lavfi', '-i', 'testsrc2=size=96x128:rate=24:duration=1',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                '-c:v', 'libx265', '-threads', '1', '-x265-params',
                'pools=1:frame-threads=1:log-level=error'+(':colorprim=9:transfer=18:colormatrix=9' if hdr else '')+(':range=full' if full_range else ''),
                '-pix_fmt', 'yuv420p10le', '-c:a', 'aac', '-tag:v', 'hvc1']
        if hdr:
            args += ['-color_primaries', 'bt2020', '-color_trc', 'arib-std-b67', '-colorspace', 'bt2020nc']
        if full_range:
            args += ['-color_range', 'pc']
        media.execute(args+[str(source)])
        return source

    def test_heic_hevc_and_resume_keep_originals(self):
        photo = self.make_photo()
        movie = self.make_movie()
        before = {p.name:media.digest(p) for p in (photo, movie)}
        with contextlib.redirect_stdout(io.StringIO()):
            result = media.import_folder(self.source, self.output)
        self.assertEqual(result, {'videos':1, 'photos':1})
        self.assertEqual(before, {p.name:media.digest(p) for p in (photo, movie)})
        video = next(self.output.glob('*.mp4'))
        info = media.video_info(video)
        self.assertTrue(info['h264'] and info['yuv420p'])
        self.assertAlmostEqual(info['duration'], 1, delta=0.1)
        probe = media.execute([media.ffmpeg_exe(), '-hide_banner', '-i', str(video)], allow_probe=True)
        self.assertIn('Audio: aac', probe)
        data = video.read_bytes()
        self.assertLess(data.index(b'moov'), data.index(b'mdat'))
        self.assertEqual(len(list(self.output.glob('*.jpg'))), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            again = media.import_folder(self.source, self.output)
        self.assertEqual(again, {'already_imported':2})
        video.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            after_delete = media.import_folder(self.source, self.output)
        self.assertEqual(after_delete, {'previously_removed':1, 'already_imported':1})
        self.assertFalse(video.exists())

    def test_hdr_becomes_standard_video(self):
        source = self.make_movie(hdr=True)
        self.assertTrue(media.video_info(source)['hdr'])
        target = self.root/'standard.mp4'
        media.convert_video(source, target)
        self.assertFalse(media.video_info(target)['hdr'])
        self.assertTrue(media.video_info(target)['h264'])
        probe = media.execute([media.ffmpeg_exe(), '-hide_banner', '-i', str(target)], allow_probe=True)
        self.assertIn('bt709', probe)

    def test_full_range_iphone_video_is_accepted(self):
        source = self.make_movie(full_range=True)
        target = self.root/'compatible.mp4'
        media.convert_video(source,target,edge=720,crf=26)
        result = media.video_info(target)
        self.assertTrue(result['h264'] and result['yuv420p'])
        self.assertAlmostEqual(result['duration'],media.video_info(source)['duration'],delta=0.1)
        probe = media.execute([media.ffmpeg_exe(),'-hide_banner','-i',str(target)],allow_probe=True)
        self.assertNotIn('yuvj420p',probe)

    def test_invalid_input_does_not_prevent_other_imports(self):
        self.make_photo('valid.png')
        (self.source/'broken.mov').write_bytes(b'broken video')
        (self.source/'.hidden').mkdir()
        (self.source/'.hidden'/'not_a_video.mov').write_bytes(b'not a video')
        with contextlib.redirect_stdout(io.StringIO()):
            summary = media.import_folder(self.source, self.output)
        self.assertEqual(summary, {'errors':1, 'photos':1})
        errors = json.loads((self.source/'import_errors.json').read_text())
        self.assertEqual(errors[0]['file'], 'broken.mov')
        self.assertFalse(list(self.output.glob('.preparing-*')))

    def test_identical_existing_file_is_not_added(self):
        source = self.make_photo('same.png')
        self.output.mkdir()
        shutil.copy2(source, self.output/'existing.png')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(media.import_folder(self.source, self.output), {'already_present':1})
        self.assertEqual(len(list(self.output.glob('*.png'))), 1)

    def test_low_space_stops_before_reading_and_resumes(self):
        source = self.make_photo('safe.png')
        original = media.digest(source)
        with patch.object(media, 'free_space', return_value=0), patch.object(media, 'digest', side_effect=AssertionError('Original must not be loaded')):
            with contextlib.redirect_stdout(io.StringIO()):
                summary = media.import_folder(self.source, self.output)
        self.assertEqual(summary, {'pending_space':1})
        report = json.loads((self.output/'.import_report.json').read_text())
        self.assertEqual(report['pending_space'], ['safe.png'])
        self.assertFalse(list(self.output.glob('*.jpg')))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(media.import_folder(self.source, self.output), {'photos':1})
        self.assertEqual(media.digest(source), original)
        with patch.object(media, 'digest', side_effect=AssertionError('Completed originals should not be loaded again')):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(media.import_folder(self.source, self.output), {'already_imported':1})

    def test_webp_is_imported_as_photo(self):
        photo = self.make_photo('image.png')
        webp = self.source/'web.webp'
        media.execute([media.ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(photo), '-frames:v', '1', str(webp)])
        photo.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(media.import_folder(self.source, self.output), {'photos':1})
        self.assertTrue(webp.is_file())

    def test_skipped_source_is_not_read_and_changed_export_is_retried(self):
        source = self.make_photo('skip.png')
        meta = source.stat()
        self.output.mkdir()
        (self.output/'.import_skipped.json').write_text(json.dumps({
            source.name: {'source_size': meta.st_size, 'source_mtime_ns': meta.st_mtime_ns}
        }))
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path.resolve() == source.resolve():
                raise AssertionError('Skipped source must not be prefetched')
            return original_open(path, *args, **kwargs)
        with patch.object(media, 'digest', side_effect=AssertionError('Skipped source must not be downloaded')):
            with patch.object(Path, 'open', guarded_open), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(media.import_folder(self.source, self.output), {'skipped': 1})
        self.assertTrue(source.is_file())
        import os
        os.utime(source, ns=(meta.st_atime_ns, meta.st_mtime_ns + 1_000_000_000))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(media.import_folder(self.source, self.output), {'photos': 1})
        self.assertTrue(source.is_file())

    def test_cloud_timestamp_rounding_does_not_reload_processed_original(self):
        self.make_photo('cloud.png')
        with contextlib.redirect_stdout(io.StringIO()):
            media.import_folder(self.source,self.output)
        manifest_path=self.output/'.import_manifest.json'
        manifest=json.loads(manifest_path.read_text())
        entry=next(iter(manifest.values()))
        entry['source_mtime_ns']+=128
        manifest_path.write_text(json.dumps(manifest))
        with patch.object(media,'digest',side_effect=AssertionError('Nanosecond rounding must not trigger a download')):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(media.import_folder(self.source,self.output),{'already_imported':1})
        entry['source_mtime_ns']+=1_000_000_000
        manifest_path.write_text(json.dumps(manifest))
        with patch.object(media,'digest',wraps=media.digest) as hashing:
            with contextlib.redirect_stdout(io.StringIO()):
                media.import_folder(self.source,self.output)
        self.assertTrue(hashing.called,'Actual timestamp changes must still verify content')


if __name__ == '__main__':
    unittest.main(verbosity=2)
