import io
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import nas_danmaku as d


SRT = '\n\n'.join(f'{i}\n00:00:{i:02},000 --> 00:00:{i + 1:02},000\n测试台词{i}' for i in range(1, 15)) + '\n'
VIDEO = Path('测试电影.Test.Movie.2020.mkv')
IDENTITY = {'title': '测试电影 Test Movie', 'year': '2020'}
META = {'format': {'duration': '20'}}


def zip_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


class BackupSourceTests(unittest.TestCase):
    def test_fast_source_success_does_not_query_backups(self):
        found = [d.SubtitleChoice('迅雷', 'online', doc=d.parse_srt(SRT))]
        with patch.object(d, 'title_subtitles', return_value=found), \
                patch.object(d, 'subhd_subtitles') as subhd, \
                patch.object(d, 'online_subtitles') as shooter, \
                patch.object(d, 'subtitlecat_subtitles') as cat:
            result = d.fetch_subtitle_backups(VIDEO, IDENTITY, META, lambda _: None, [])
        self.assertIs(result, found)
        for source in (subhd, shooter, cat):
            source.assert_not_called()

    def test_failure_and_no_result_fall_through_in_order(self):
        order, warnings = [], []
        def first(*args):
            order.append('迅雷')
            raise d.ToolError('超时')
        def subhd(*args):
            order.append('SubHD')
            return []
        def shooter(*args):
            order.append('射手')
            return []
        def cat(*args):
            order.append('SubtitleCat')
            return [d.SubtitleChoice('可能机翻', 'online', doc=d.parse_srt(SRT))]
        with patch.object(d, 'title_subtitles', side_effect=first), patch.object(d, 'subhd_subtitles', side_effect=subhd), \
                patch.object(d, 'online_subtitles', side_effect=shooter), patch.object(d, 'subtitlecat_subtitles', side_effect=cat):
            result = d.fetch_subtitle_backups(VIDEO, IDENTITY, META, lambda _: None, warnings)
        self.assertEqual(order, ['迅雷', 'SubHD', '射手', 'SubtitleCat'])
        self.assertEqual(len(result), 1)
        self.assertTrue(any('超时' in w for w in warnings))
        self.assertTrue(any('机翻' in w for w in warnings))

    def test_subhd_follows_prepare_page_and_down_before_download(self):
        session = MagicMock(base='https://subhd.tv')
        session.page.side_effect = ['<a href="/a/TestId">测试电影 Test Movie 2020</a>', '<html>detail</html>', '<html>down</html>']
        session.post.side_effect = [{'success': True, 'url': '/down/TestId'},
                                   {'success': True, 'pass': True, 'url': 'https://example.test/sub.zip'}]
        session.get.return_value = zip_bytes({'Test.Movie.2020.chs.srt': SRT})
        with patch.object(d, 'SubtitleSession', return_value=session):
            choices = d.subhd_subtitles(VIDEO, IDENTITY, META)
        self.assertEqual(len(choices), 1)
        self.assertIn('SubHD', choices[0].label)
        self.assertEqual([c.args[0] for c in session.post.call_args_list], ['/api/sub/prepare-download', '/api/sub/down'])
        self.assertEqual(session.get.call_args.args[0], 'https://example.test/sub.zip')

    def test_subhd_verification_gate_is_not_bypassed(self):
        session = MagicMock(base='https://subhd.tv')
        session.page.side_effect = ['<a href="/a/TestId">测试电影 2020</a>', 'detail', 'down']
        session.post.side_effect = [{'success': True, 'url': '/down/TestId'}, {'success': True, 'pass': False}]
        with patch.object(d, 'SubtitleSession', return_value=session):
            with self.assertRaisesRegex(d.ToolError, '网页完成验证'):
                d.subhd_subtitles(VIDEO, IDENTITY, META)
        session.get.assert_not_called()

    def test_subhd_wrong_movie_or_year_never_downloaded(self):
        session = MagicMock(base='https://subhd.tv')
        session.page.return_value = '<a href="/a/Wrong">其他电影 2020</a><a href="/a/Year">测试电影 1999</a>'
        with patch.object(d, 'SubtitleSession', return_value=session):
            self.assertEqual(d.subhd_subtitles(VIDEO, IDENTITY, META), [])
        session.post.assert_not_called()

    def test_subtitlecat_downloads_existing_chinese_and_labels_machine_translation(self):
        session = MagicMock(base='https://www.subtitlecat.com')
        session.page.side_effect = ['<a href="subs/123/Test.Movie.2020.html">Test Movie 2020</a>',
                                   '<a id="download_zh-CN" href="/subs/234/Test Movie 2020-zh-CN.srt">Download</a>']
        session.get.return_value = SRT.encode()
        with patch.object(d, 'SubtitleSession', return_value=session):
            choices = d.subtitlecat_subtitles(VIDEO, IDENTITY, META)
        self.assertIn('search=Test+Movie', session.page.call_args_list[0].args[0])
        self.assertIn('可能机翻', choices[0].label)
        self.assertIn('Test%20Movie', session.get.call_args.args[0])

    def test_subtitlecat_does_not_trigger_translation_or_use_english(self):
        session = MagicMock(base='https://www.subtitlecat.com')
        session.page.side_effect = ['<a href="subs/123/Test.Movie.2020.html">Test Movie 2020</a>',
                                   '<a id="translate_zh-CN" href="/translate/1">Translate</a><a id="download_en" href="/subs/123/en.srt">Download</a>']
        with patch.object(d, 'SubtitleSession', return_value=session):
            with self.assertRaisesRegex(d.ToolError, '没有可直接下载'):
                d.subtitlecat_subtitles(VIDEO, IDENTITY, META)
        session.get.assert_not_called()

    def test_source_session_preserves_cookie_opener_and_checks_budget(self):
        with patch.object(d.time, 'monotonic', return_value=100):
            session = d.SubtitleSession('测试', 'https://example.test', None, budget=10)
        with patch.object(d.time, 'monotonic', return_value=101), patch.object(d, 'web_bytes', return_value=b'{"success":true}') as web:
            session.get('https://example.test/page')
            session.post('/api', {'sid': 'test'}, 'https://example.test/page')
        calls = web.call_args_list
        self.assertIs(calls[0].kwargs['opener'], calls[1].kwargs['opener'])
        self.assertEqual(calls[1].kwargs['request_headers']['Content-Type'], 'application/json; charset=utf-8')
        with patch.object(d.time, 'monotonic', return_value=111), patch.object(d, 'web_bytes') as web:
            with self.assertRaises(d.ToolError):
                session.get('https://example.test/next')
            web.assert_not_called()


class ArchiveTests(unittest.TestCase):
    def test_zip_chinese_choice_rejects_english_and_partial_disc(self):
        raw = zip_bytes({'Test.Movie.2020.chs.srt': SRT, 'Test.Movie.2020.eng.srt': SRT.replace('测试台词', 'English'),
                         'Test.Movie.2020.CD1.srt': SRT, 'readme.txt': 'ignored'})
        choices = d.downloaded_choices(raw, '测试', 'Test Movie', META)
        self.assertEqual(len(choices), 1)
        self.assertIn('chs.srt', choices[0].label)

    def test_archive_paths_and_size_limits(self):
        raw = zip_bytes({'../escape.srt': SRT, '/absolute.srt': SRT, 'C:\\escape.srt': SRT, '@list.srt': SRT, 'safe.srt': SRT})
        self.assertEqual([name for name, _ in d.subtitle_archive_members(raw)], ['safe.srt'])
        with patch.object(d, 'MAX_BYTES', 100):
            with self.assertRaisesRegex(d.ToolError, '总量'):
                list(d.subtitle_archive_members(raw))
        raw = zip_bytes({f'{i}.txt': '' for i in range(101)})
        with self.assertRaisesRegex(d.ToolError, '文件数'):
            list(d.subtitle_archive_members(raw))

    def test_sevenzip_missing_is_a_clear_failure(self):
        with patch.object(d.shutil, 'which', return_value=None), patch.object(Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(d.ToolError, '7-Zip'):
                list(d.subtitle_archive_members(b"7z\xbc\xaf\x27\x1c"))

    @unittest.skipUnless(shutil.which('7z') or shutil.which('7zz'), '需要 7-Zip')
    def test_real_sevenzip_package_is_read_without_extracting_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            subtitle = folder / 'sample.chs.srt'
            subtitle.write_text(SRT, encoding='utf-8')
            archive = folder / 'sample.7z'
            subprocess.run([shutil.which('7z') or shutil.which('7zz'), 'a', str(archive), str(subtitle)],
                           check=True, capture_output=True, timeout=20)
            choices = d.downloaded_choices(archive.read_bytes(), '测试', 'Test Movie', META)
            self.assertEqual(len(choices), 1)
            self.assertEqual(len(choices[0].doc.events), 14)


if __name__ == '__main__':
    unittest.main()
