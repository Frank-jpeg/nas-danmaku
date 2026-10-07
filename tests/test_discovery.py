from pathlib import Path
import copy
import gc
import importlib.util
import json
import shutil
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import nas_danmaku as d

REAL_ONLINE_SUBTITLES = d.online_subtitles

SRT = '1\n00:00:02,000 --> 00:00:20,000\n这是原台词字幕\n'


# 最小模拟响应，不包含真实接口数据集。
SEARCH_REPLY = {'data': {'longData': {'rows': [
    {'cat_id': '1', 'titleTxt': '流浪地球2', 'year': '2023',
     'playlinks': {'qq': 'https://v.qq.com/x/cover/example2.html'},
     'coverInfo': {'duration': '2:53:00'}, 'en_id': 'example2'},
    {'cat_id': '1', 'titleTxt': '流浪地球', 'year': '2019',
     'playlinks': {'qq': 'https://v.qq.com/x/cover/example1.html'},
     'coverInfo': {'duration': '2:05:00'}, 'en_id': 'example1'},
]}}}


class AutoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.video = self.folder / '流浪地球.2019.1080p.mkv'
        self.video.write_bytes(bytes(range(256)) * 256)
        self.meta = {'streams': [{'index': 1, 'codec_type': 'subtitle', 'codec_name': 'ass', 'tags': {'language': 'chi'}}], 'format': {'duration': '60'}}
        # 文件探测和字幕提取单独由 FFmpeg 集成测试覆盖。
        probe = patch.object(d, 'inspect_video', return_value=self.meta)
        extract = patch.object(d, 'extract_subtitle', side_effect=lambda *_, **__: d.parse_srt(SRT))
        probe.start()
        extract.start()
        self.addCleanup(probe.stop)
        self.addCleanup(extract.stop)
        cache = patch.object(d, 'local_workspace', side_effect=lambda: Path(tempfile.mkdtemp(dir=self.folder)))
        titles = patch.object(d, 'title_subtitles', return_value=[])
        online = patch.object(d, 'online_subtitles', return_value=[])
        for fixture in (cache, titles, online):
            fixture.start()
            self.addCleanup(fixture.stop)
        # 保留真实在线解析函数，单独测试时使用。

    def tearDown(self):
        self.temp.cleanup()

    def test_filename_parsing(self):
        examples = {
            'The.Wandering.Earth.2019.2160p.WEB-DL.H265': ('The Wandering Earth', '2019'),
            '流浪地球.2019.1080p': ('流浪地球', '2019'),
            '1917.2019.1080p': ('1917', '2019'),
            '2012.2009.1080p': ('2012', '2009'),
            'The.Shawshank.Redemption.1994.BluRay': ('The Shawshank Redemption', '1994'),
        }
        for name, expected in examples.items():
            self.assertEqual(d.filename_title(name), expected)

    def test_nfo_title_and_year(self):
        self.video.with_suffix('.nfo').write_text('<movie><title>流浪地球</title><year>2019</year></movie>', encoding='utf-8')
        self.assertEqual(d.identify_movie(self.video), {'title': '流浪地球', 'year': '2019', 'source': '影片 NFO'})
        self.assertEqual(d.identify_movie(self.video, '修正片名')['title'], '修正片名')

    def test_sidecar_only_exact_movie_not_danmaku(self):
        for name in [self.video.stem + '.chs.srt', self.video.stem + '.eng.srt', self.video.stem + '-字幕加弹幕.ass', self.video.stem + '2.srt', '别的电影.srt']:
            (self.folder / name).write_text(SRT, encoding='utf-8')
        choices = d.sidecar_choices(self.video)
        self.assertEqual(len(choices), 2)
        self.assertTrue(choices[0].path.endswith('.chs.srt'))

    def test_language_and_forced_order(self):
        streams = self.meta['streams'] + [{'index': 2, 'codec_type': 'subtitle', 'codec_name': 'hdmv_pgs_subtitle', 'tags': {'language': 'chi'}}, {'index': 3, 'codec_type': 'subtitle', 'codec_name': 'ass', 'tags': {'language': 'eng'}}]
        choices = d.embedded_choices({'streams': streams})
        self.assertEqual([c.index for c in choices], [1, 3])

    def test_chinese_embedded_does_not_skip_online_subtitle_request(self):
        candidate = d.SubtitleChoice('在线测试', 'online', 100, doc=d.parse_srt(SRT))
        with patch.object(d, 'title_subtitles', return_value=[candidate]) as online:
            choices, errors = d.discover_subtitles(self.video, self.meta, lambda _: None)
        online.assert_called_once()
        self.assertEqual(choices[0].kind, 'online')
        self.assertFalse(any(c.kind == 'embedded' for c in choices))

    def test_online_subtitle_search_and_delay(self):
        replies = [json.dumps([{'Desc': '中文', 'Delay': 1500, 'Files': [{'Ext': 'srt', 'Link': 'https://example.com/sub.srt'}]}]).encode(), SRT.encode()]
        with patch.object(d, 'web_bytes', side_effect=replies) as web:
            choices = REAL_ONLINE_SUBTITLES(self.video)
        self.assertEqual(choices[0].delay, 1.5)
        self.assertEqual(len(choices[0].doc.events), 1)
        # 不把用户目录发往字幕源
        payload = web.call_args_list[0].kwargs['data'].decode()
        self.assertNotIn(str(self.folder), payload)
        doc = d.materialize_subtitle(choices[0], self.video)
        self.assertEqual(doc.events[0]['Start'], '0:00:03.50')

    def test_no_online_subtitle_is_not_success(self):
        with patch.object(d, 'web_bytes', return_value=b'\xff'):
            self.assertEqual(REAL_ONLINE_SUBTITLES(self.video), [])

    def test_hash_four_ranges(self):
        import hashlib
        source = self.video.read_bytes()
        size = len(source)
        hashes = [hashlib.md5(source[p:p+4096]).hexdigest() for p in (4096, size//3*2, size//3, size-8192)]
        self.assertEqual(d.shooter_hash(self.video), ';'.join(hashes))

    def test_public_danmaku_shape(self):
        doc = {'code': 23, 'danmuku': [[2, 'right', '#fff', '32', '🔥有 133 条弹幕列队来袭~'], [3, 'right', '#f00', '32px', '观众弹幕']]}
        rows = d.parse_public_comments(doc)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].color, 0xFF0000)
        self.assertEqual(rows[0].text, '观众弹幕')

    def test_search_response_movie_selection(self):
        data = copy.deepcopy(SEARCH_REPLY)
        with patch.object(d, 'web_json', return_value=data):
            rows = d.search_movies('流浪地球', '2019')
        self.assertEqual(rows[0]['title'], '流浪地球')
        self.assertNotEqual(rows[0]['title'], '流浪地球2')
        with patch.object(d, 'web_json', return_value={'data': {'longData': None}}):
            self.assertEqual(d.search_movies('测试'), [])

    def test_platform_url_keeps_youku_vid(self):
        self.assertEqual(d.canonical_platform_url('https://v.youku.com/video?vid=ABC%3D%3D&refer=tracking'), 'https://v.youku.com/video?vid=ABC%3D%3D')
        with self.assertRaises(d.ToolError):
            d.canonical_platform_url('https://qq.com.evil.test/x')

    def test_bilingual_title_retries_chinese_name(self):
        from urllib.parse import parse_qs, urlsplit
        empty = {'data': {'longData': None}}
        with patch.object(d, 'web_json', side_effect=[empty, copy.deepcopy(SEARCH_REPLY)]) as web:
            rows = d.search_movies('流浪地球 The Wandering Earth', '2019')
        queries = [parse_qs(urlsplit(call.args[0]).query)['kw'][0] for call in web.call_args_list]
        self.assertEqual(queries, ['流浪地球 The Wandering Earth', '流浪地球'])
        self.assertEqual(rows[0]['title'], '流浪地球')

    def test_full_scan_then_confirm_original_dir(self):
        data = copy.deepcopy(SEARCH_REPLY)
        comments = [d.Comment(4, '测试弹幕')]
        candidate = d.SubtitleChoice('在线测试', 'online', 100, doc=d.parse_srt(SRT))
        with patch.object(d, 'title_subtitles', return_value=[candidate]), patch.object(d, 'web_json', return_value=data), patch.object(d, 'fetch_public_danmaku', return_value=(comments, '测试源', 'https://v.qq.com/test')):
            result = d.scan_movie(self.video)
        self.assertEqual(result.subtitles[0].kind, 'online')
        self.assertFalse(list(self.folder.glob('*字幕加弹幕*')), '确认前不能写输出')
        before = self.video.read_bytes()
        output = d.synthesize(result)
        target = Path(output['output'])
        self.assertEqual(target.parent, self.video.parent)
        self.assertEqual(output['subtitle_lines'], 1)
        self.assertEqual(output['danmaku_lines'], 1)
        again = d.synthesize(result)
        self.assertTrue(again['output'].endswith('-v2.ass'))
        self.assertEqual(self.video.read_bytes(), before)

    def test_changed_video_and_missing_subtitle_block_output(self):
        result = d.ScanResult(self.video, {}, self.meta, d.file_signature(self.video), comments=[d.Comment(1,'弹幕')])
        with self.assertRaisesRegex(d.ToolError, '文字字幕'):
            d.synthesize(result)
        self.video.write_bytes(self.video.read_bytes() + b'changed')
        result.subtitles = [d.SubtitleChoice('cached', 'online', doc=d.parse_srt(SRT))]
        output = d.synthesize(result)
        self.assertFalse(output['saved'])
        self.assertIn('发生了变化', output['write_error'])
        self.assertFalse(list(self.folder.glob('*字幕加弹幕*')))

    def test_hidden_gui_analyze_confirm_and_stale_path(self):
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest('没有可用的 Tk 显示环境')
        self.addCleanup(lambda: (root.destroy(), gc.collect()))
        root.withdraw()
        app = d.App(root); root.update_idletasks()
        self.assertLessEqual(root.winfo_reqheight(), 690)
        result = d.ScanResult(self.video, {'title':'流浪地球','year':'2019','source':'文件名'}, self.meta,
                              d.file_signature(self.video), subtitles=d.embedded_choices(self.meta), comments=[d.Comment(3,'滚动测试')], danmaku_source='测试')
        app.path.set(str(self.video))
        alerts=[]
        def wait():
            deadline=time.monotonic()+8
            while app.busy and time.monotonic()<deadline:
                root.update();time.sleep(.02)
            self.assertFalse(app.busy)
        with patch.object(d,'scan_movie',return_value=result),patch.object(d.messagebox,'showinfo',side_effect=lambda *a:alerts.append(a)),patch.object(d.messagebox,'showerror',side_effect=lambda *a:alerts.append(a)):
            app.scan();wait()
            self.assertEqual(app.title.get(),'流浪地球')
            self.assertEqual(str(app.generate_button['state']),'normal')
            self.assertFalse(list(self.folder.glob('*字幕加弹幕*')))
            app.generate();wait()
            self.assertEqual(alerts[-1][0],'合成完成')
            app.path.set(str(self.folder/'other.mkv'))
            app.generate()
            self.assertEqual(alerts[-1][0],'请检查输入')


if __name__ == '__main__':
    unittest.main(verbosity=2)
