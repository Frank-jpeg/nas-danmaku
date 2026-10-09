import gc
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

import nas_danmaku as d


SRT = '\n\n'.join(f'{i}\n00:00:{i:02},000 --> 00:00:{i + 1:02},000\n测试台词{i}' for i in range(1, 15)) + '\n'
ROW = {'name': '测试电影.2020.BluRay.chs.srt', 'simple_name': '中文[SRT]测试电影(2020)',
       'ext': 'srt', 'url': 'https://example.test/sub.srt', 'duration': 15000, 'languages': ['简体']}


class LocalPipelineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name).resolve()
        self.nas = self.folder / 'nas'
        self.nas.mkdir()
        self.video = self.nas / '测试电影.2020.mkv'
        self.video.write_bytes(b'fixture-video')
        self.cache = self.folder / 'local'
        self.cache.mkdir()
        fixture = patch.object(d, 'local_workspace', return_value=self.cache)
        fixture.start()
        self.addCleanup(fixture.stop)
        for name in ('subhd_subtitles', 'subtitlecat_subtitles', 'search_danmubox'):
            fixture = patch.object(d, name, return_value=[])
            fixture.start()
            self.addCleanup(fixture.stop)
        self.meta = {'format': {'duration': '20'}, 'streams': [{'index': 3, 'codec_type': 'subtitle',
                     'codec_name': 'subrip', 'tags': {'language': 'chi'}}]}

    def result(self):
        return d.ScanResult(self.video, {'title': '测试电影', 'year': '2020'}, self.meta, d.file_signature(self.video),
                            subtitles=[d.SubtitleChoice('在线字幕', 'online', doc=d.parse_srt(SRT))],
                            comments=[d.Comment(2, '测试弹幕')], workspace=self.cache)

    def test_title_query_needs_no_video_reads_and_filters_wrong_candidates(self):
        rows = [dict(ROW, name='测试电影.2020.CD2.srt', url='https://example.test/disc.srt'),
                dict(ROW, name='别的电影.2020.srt', simple_name='别的电影', url='https://example.test/wrong.srt'),
                dict(ROW, name='测试电影.1999.srt', simple_name='测试电影(1999)', url='https://example.test/year.srt'),
                dict(ROW, duration=1000, url='https://example.test/short.srt'), ROW]
        with patch.object(d, 'web_json', return_value={'code': 0, 'data': rows}) as search, \
                patch.object(d, 'web_bytes', return_value=SRT.encode()) as download, \
                patch.object(Path, 'open', side_effect=AssertionError('不能读视频')), \
                patch.object(d, 'extract_subtitle', side_effect=AssertionError('不能提取内封')):
            found = d.title_subtitles(self.video, {'title': '测试电影', 'year': '2020'}, self.meta)
        self.assertEqual(len(found), 1)
        self.assertEqual(download.call_args.args[0], ROW['url'])
        self.assertNotIn(str(self.nas), search.call_args.args[0])
        self.assertEqual(len(found[0].doc.events), 14)

    def test_title_lookup_failure_is_not_claimed_as_no_movie_resources(self):
        with patch.object(d, 'web_json', return_value={'code': 500, 'data': []}):
            with self.assertRaisesRegex(d.ToolError, '字幕源未返回有效列表'):
                d.title_subtitles(self.video, {'title': '测试电影'}, self.meta)

    def test_candidate_download_error_is_reported_distinctly(self):
        with patch.object(d, 'web_json', return_value={'code': 0, 'data': [ROW]}), \
                patch.object(d, 'web_bytes', side_effect=d.ToolError('连接超时')):
            with self.assertRaisesRegex(d.ToolError, '已找到候选字幕.*下载或解析失败.*连接超时'):
                d.title_subtitles(self.video, {'title': '测试电影', 'year': '2020'}, self.meta)

    def test_directory_listing_failure_does_not_block_online_query(self):
        choice = d.SubtitleChoice('在线', 'online', doc=d.parse_srt(SRT))
        with patch.object(d, 'sidecar_choices', side_effect=OSError('目录暂离线')), \
                patch.object(d, 'title_subtitles', return_value=[choice]):
            choices, warnings = d.discover_subtitles(self.video, self.meta, lambda _: None)
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].kind, 'online')
        self.assertTrue(any('继续查询在线字幕' in warning for warning in warnings))

    def test_existing_chinese_sidecar_is_cached_and_preferred(self):
        sidecar = self.video.with_suffix('.chs.srt')
        sidecar.write_text(SRT, encoding='utf-8')
        with patch.object(d, 'title_subtitles') as online:
            choices, _ = d.discover_subtitles(self.video, self.meta, lambda _: None, folder=self.cache)
        online.assert_not_called()
        sidecar.unlink()
        self.assertEqual(len(d.materialize_subtitle(choices[0], self.video).events), 14)
        self.assertEqual(Path(choices[0].path).parent, self.cache)

    def test_scan_probe_timeout_still_finds_and_caches_online_subtitle(self):
        candidate = d.SubtitleChoice('在线', 'online', doc=d.parse_srt(SRT))
        with patch.object(d, 'inspect_video', side_effect=d.ToolError('读取超时')), \
                patch.object(d, 'title_subtitles', return_value=[candidate]), \
                patch.object(d, 'search_movies', return_value=[]), \
                patch.object(d, 'online_subtitles') as fingerprint:
            result = d.scan_movie(self.video)
        self.assertEqual(result.subtitles[0].kind, 'online')
        self.assertEqual(result.metadata, {})
        self.assertTrue(Path(result.subtitles[0].path).exists())
        fingerprint.assert_not_called()
        self.assertEqual(list(self.nas.iterdir()), [self.video])

    def test_no_online_match_never_silently_selects_embedded_track(self):
        with patch.object(d, 'title_subtitles', return_value=[]), patch.object(d, 'online_subtitles', return_value=[]):
            choices, warnings = d.discover_subtitles(self.video, self.meta, lambda _: None)
        self.assertEqual(choices, [])
        self.assertIn('手动', warnings[-1])

    def test_offline_synthesis_keeps_local_result_and_retry_only_copies(self):
        result = self.result()
        self.video.unlink()
        with patch.object(d, 'extract_subtitle', side_effect=AssertionError('不允许读取内封')), \
                patch.object(d, 'web_bytes', side_effect=AssertionError('不允许重新下载')):
            value = d.synthesize(result)
        self.assertFalse(value['saved'])
        local = Path(value['local_output'])
        self.assertEqual(len(d.parse_ass(local.read_text(encoding='utf-8')).events), 15)
        self.assertFalse(list(self.nas.glob('*.ass')))
        self.video.write_bytes(b'fixture-video')
        import os
        os.utime(self.video, ns=(result.signature[1], result.signature[1]))
        with patch.object(d, 'materialize_subtitle', side_effect=AssertionError('重试不能重新处理字幕')), \
                patch.object(d, 'web_bytes', side_effect=AssertionError('重试不能重新下载')):
            retried = d.publish_cached(value)
            again = d.publish_cached(value)
        self.assertTrue(retried['saved'])
        self.assertTrue(again['output'].endswith('-v2.ass'))
        self.assertEqual(Path(retried['output']).read_bytes(), local.read_bytes())
        self.assertTrue(local.exists())
        self.assertFalse(list(self.nas.glob('*.part')))

    def test_publish_failure_keeps_local_file_and_exposes_no_partial_ass(self):
        result = self.result()
        with patch.object(Path, 'rename', side_effect=OSError('NAS 断线')), \
                patch.object(d.os, 'link', side_effect=OSError('NAS 断线')):
            value = d.synthesize(result)
        self.assertFalse(value['saved'])
        self.assertIn('NAS 断线', value['write_error'])
        self.assertTrue(Path(value['local_output']).exists())
        self.assertFalse(list(self.nas.glob('*.ass')))
        self.assertFalse(list(self.nas.glob('*.part')))

    def test_no_video_synthesis_stays_local_keeps_late_comments_and_existing_pending_jobs(self):
        result = self.result()
        result.video, result.signature = None, ()
        result.comments.append(d.Comment(100, '台词结束后的弹幕'))
        pending = dict(local_output=str(self.cache / 'older.ass'), video=str(self.video),
                       signature=d.file_signature(self.video), target=str(self.nas / 'older.ass'), saved=False)
        d.remember_output(pending)
        previous_pending = d.pending_outputs()
        with patch.object(d, 'file_signature', side_effect=AssertionError('不能检查影片')), \
                patch.object(d, 'extract_subtitle', side_effect=AssertionError('不能提取影片')), \
                patch.object(d, 'web_bytes', side_effect=AssertionError('不能联网')), \
                patch.object(d, 'remember_output', side_effect=AssertionError('不应修改待写回任务')), \
                patch.object(d, 'publish_cached', side_effect=AssertionError('不应写回')):
            value = d.synthesize(result, offset=1.5)
            before = Path(value['output']).read_bytes()
            again = d.synthesize(result, offset=1.5)
        self.assertTrue(value['saved'])
        self.assertTrue(value['local_only'])
        self.assertEqual(Path(value['output']).parent, self.cache)
        self.assertEqual(Path(value['output']).name, '弹幕版-测试电影.2020.ass')
        self.assertEqual(Path(again['output']).name, '弹幕版-测试电影.2020-v2.ass')
        self.assertEqual(Path(value['output']).read_bytes(), before)
        parsed = d.parse_ass(before.decode('utf-8'))
        self.assertEqual(len(parsed.events), 16)
        late = next(row for row in parsed.events if '台词结束后的弹幕' in row['Text'])
        self.assertEqual(late['Start'], '0:01:41.50')
        self.assertEqual(d.pending_outputs(), previous_pending)
        self.assertEqual(list(self.nas.iterdir()), [self.video])

    def test_local_filename_uses_selected_movie_and_cannot_escape_workspace(self):
        result = self.result()
        result.video, result.signature = None, ()
        movie = dict(title='让子弹飞', year='2010', links={'qq': 'https://v.qq.com/test'})
        result.movies, result.selected_movie_key = [movie], d.movie_source_key(movie)
        self.assertEqual(Path(d.synthesize(result)['output']).name, '弹幕版-让子弹飞.2010.ass')
        result.movies, result.selected_movie_key = [], None
        result.identity['title'] = '../CON:/\\影片*?' + '很长' * 150
        value = d.synthesize(result)
        self.assertEqual(Path(value['output']).parent, self.cache)
        self.assertLessEqual(len(Path(value['output']).name), 128)
        d.safe_name(Path(value['output']).name)

    def test_local_synthesis_requires_both_inputs_and_reports_save_failures(self):
        result = self.result()
        result.video, result.signature = None, ()
        subtitles, comments = result.subtitles, result.comments
        result.subtitles = []
        with self.assertRaisesRegex(d.ToolError, '文字字幕'):
            d.synthesize(result)
        result.subtitles, result.comments = subtitles, []
        with self.assertRaisesRegex(d.ToolError, '弹幕'):
            d.synthesize(result)
        result.comments = comments
        result.subtitles = [d.SubtitleChoice('内封', 'embedded', index=1)]
        with patch.object(d, 'file_signature') as signature, self.assertRaisesRegex(d.ToolError, '内封字幕需要影片文件'):
            d.synthesize(result)
        signature.assert_not_called()
        result.subtitles = subtitles
        with patch.object(d, 'save_new', side_effect=OSError('磁盘写入失败')), \
                patch.object(d, 'remember_output') as remember, self.assertRaisesRegex(OSError, '磁盘写入失败'):
            d.synthesize(result)
        remember.assert_not_called()

    def test_failed_rename_never_reports_copy_complete(self):
        source = self.cache / 'out.ass'
        source.write_text(SRT, encoding='utf-8')
        updates = []
        with patch.object(Path, 'rename', side_effect=OSError('rename failed')), \
                patch.object(d.os, 'link', side_effect=OSError('link failed')):
            with self.assertRaises(OSError):
                d.copy_to_video_dir(source, self.video.with_suffix('.ass'), updates.append)
        self.assertFalse(any(u.complete for u in updates))

    def test_cached_delay_is_applied_once(self):
        choice = d.SubtitleChoice('在线', 'online', doc=d.parse_srt(SRT), delay=2)
        d.cache_subtitle(choice, self.cache)
        for _ in range(2):
            self.assertEqual(d.materialize_subtitle(choice, self.video).events[0]['Start'], '0:00:03.00')
        self.assertEqual(choice.doc.events[0]['Start'], '0:00:01.00')

    def test_hidden_gui_embedded_is_explicit_and_failed_write_can_retry(self):
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest('没有可用的 Tk 显示环境')
        self.addCleanup(lambda: (root.destroy(), gc.collect()))
        root.withdraw()
        app = d.App(root)
        app.result = self.result()
        app.use_embedded()
        self.assertEqual(app.result.subtitles[0].kind, 'embedded')
        self.assertIn('异地较慢', app.result.subtitles[0].label)
        value = dict(output='local.ass', local_output='local.ass', saved=False, write_error='offline',
                     subtitle_lines=14, danmaku_lines=1, filtered=0)
        with patch.object(d.messagebox, 'showwarning'):
            app.show_output(value)
        self.assertEqual(str(app.retry_copy_button['state']), 'normal')
        self.assertIn('本机合成成功', app.status.get())
        with patch.object(app, 'background') as background, patch.object(d, 'publish_cached', return_value=value) as publish:
            app.retry_copy()
            background.call_args.args[0]()
        publish.assert_called_once_with(value, app.progress)


if __name__ == '__main__':
    unittest.main()
