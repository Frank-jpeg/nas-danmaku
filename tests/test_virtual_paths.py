from pathlib import Path
import gc
import os
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

import nas_danmaku as d


def unsupported_volume():
    error = OSError('virtual drive cannot resolve final path')
    error.winerror = 1005
    return error


class VirtualPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fixture = patch.object(d, 'local_workspace', side_effect=lambda: Path(tempfile.mkdtemp(dir=tmp.name)))
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_fallback_normalizes_relative_path(self):
        selected = Path('folder') / '..' / 'movie.mkv'
        expected = Path(os.path.abspath(selected))
        with patch.object(Path, 'resolve', side_effect=unsupported_volume()):
            self.assertEqual(d.normalize_path(selected), expected)

    def test_unrelated_errors_are_not_hidden(self):
        with patch.object(Path, 'resolve', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                d.normalize_path('movie.mkv')

    def test_scan_and_output_survive_unsupported_final_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            video = folder / '电影.2020.mkv'
            video.write_bytes(b'video-fixture')
            subtitle = video.with_suffix('.chs.srt')
            subtitle.write_text('1\n00:00:01,000 --> 00:00:04,000\n原台词\n', encoding='utf-8')
            meta = {'format': {'duration': '10'}, 'streams': []}
            movie = {'title': '电影', 'year': '2020', 'duration': '0:00:10'}
            with patch.object(Path, 'resolve', side_effect=unsupported_volume()), \
                 patch.object(d, 'inspect_video', return_value=meta), \
                 patch.object(d, 'search_movies', return_value=[movie]), \
                 patch.object(d, 'fetch_public_danmaku', return_value=([d.Comment(2, '弹幕')], '测试源', '')):
                result = d.scan_movie(video)
                self.assertEqual(result.video, video)
                self.assertFalse(list(folder.glob('*字幕加弹幕*')))
                output = d.synthesize(result)
            self.assertEqual(Path(output['output']).parent, folder)
            self.assertEqual(output['danmaku_lines'], 1)
            self.assertEqual(video.read_bytes(), b'video-fixture')

    def test_manual_build_does_not_fail_after_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            subtitle = folder / 'sub.srt'
            subtitle.write_text('1\n00:00:01,000 --> 00:00:04,000\n台词\n', encoding='utf-8')
            comments = folder / 'comments.xml'
            comments.write_text('<i><d p="2,1,25,16777215">弹幕</d></i>', encoding='utf-8')
            with patch.object(Path, 'resolve', side_effect=unsupported_volume()):
                output = d.build(subtitle=subtitle, danmaku=comments, out_dir=folder, name='电影')
            self.assertTrue(Path(output['output']).is_file())
            self.assertEqual(len(list(folder.glob('*字幕加弹幕*'))), 1)

    def test_gui_generation_and_failed_scan_labels(self):
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest('没有可用的 Tk 显示环境')
        self.addCleanup(lambda: (root.destroy(), gc.collect()))
        root.withdraw()
        app = d.App(root)
        video = Path(os.path.abspath('movie.mkv'))
        app.path.set(str(video))
        app.result = d.ScanResult(video, {}, {}, ())
        with patch.object(Path, 'resolve', side_effect=unsupported_volume()), \
             patch.object(app, 'background') as background, \
             patch.object(d.messagebox, 'showerror') as alert:
            app.generate()
            background.assert_called_once()
            alert.assert_not_called()
        app.result = None
        app.subtitle.set('正在识别…')
        app.movie.set('正在查找…')
        app.dm_text.set('正在获取…')
        app.set_busy(True)
        app.tasks.put(('error', '测试：路径不可访问', None))
        with patch.object(d.messagebox, 'showerror'):
            app.poll()
        self.assertFalse(app.busy)
        self.assertNotIn('正在', app.subtitle.get() + app.movie.get() + app.dm_text.get())
        self.assertEqual(str(app.generate_button['state']), 'disabled')
