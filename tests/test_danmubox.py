import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import nas_danmaku as d


ENTRY = dict(name='测试电影 [新上架版]', repo='repo007', file='04/' + 'a' * 32 + '.7z', size=1024)
OTHER = dict(ENTRY, name='测试电影 [旧版]', file='04/' + 'b' * 32 + '.7z', size=512)
XML = '<i><d p="1,1,25,16777215,0,0,user,1">真实弹幕</d><d p="2,1,25,16777215,0,0,user,2">另一条</d></i>'.encode()
INDEX_TEXT = '测试电影,1234,0,01/0123456789abcdef0123456789abcdef'
INDEX_ENCRYPTED = b'Cr/L3LgpeD+tMbqXAcGNBjPCPdNPS6r2O7/5YxWa56p9fY4BbrlCXRuCN5YvuP4A6a7G4aRKl25u5xpXqJUmdA=='


def archive(files=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as bundle:
        for name, raw in (files or {'movie.xml': XML}).items():
            bundle.writestr(name, raw)
    return buffer.getvalue()


class DanmuboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve()
        mock = patch.dict(os.environ, {'LOCALAPPDATA': self.tmp.name})
        mock.start()
        self.addCleanup(mock.stop)

    @unittest.skipUnless(os.name == 'nt', '目录解码使用 Windows CNG')
    def test_decoder_matches_independent_aes_fixture_and_rejects_malformed_data(self):
        self.assertEqual(d.decode_danmubox_index(INDEX_ENCRYPTED), INDEX_TEXT)
        for raw in (b'<html>error</html>', b'AAAA', INDEX_ENCRYPTED[:-4], b'A' * (2 * 1024 * 1024 + 4)):
            with self.subTest(raw_length=len(raw)), self.assertRaises(d.ToolError):
                d.decode_danmubox_index(raw)

    def test_catalog_rows_validate_download_paths_and_do_not_trust_remote_urls(self):
        rows = d.parse_danmubox_index(INDEX_TEXT + ';bad,12,0,../../outside', 'repo007')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['file'], '01/0123456789abcdef0123456789abcdef.7z')
        with patch.object(d, 'web_bytes') as network, self.assertRaises(d.ToolError):
            d.fetch_danmubox(dict(ENTRY, file='https://example.test/file.7z'))
        network.assert_not_called()

    def test_search_keeps_editions_separate_and_rejects_sequels_and_wrong_year(self):
        entries = [ENTRY, OTHER, dict(ENTRY, name='测试电影2', file='04/' + 'c' * 32 + '.7z'),
                   dict(ENTRY, name='测试电影 (1999)', file='04/' + 'd' * 32 + '.7z'), dict(ENTRY)]
        with patch.object(d, 'danmubox_catalog', return_value=entries):
            found = d.search_danmubox('测试电影', '2020')
        self.assertEqual(found, [ENTRY, OTHER])
        self.assertTrue(d.danmubox_matches('肖申克的救赎 The Shawshank Redemption(QQ)', 'The Shawshank Redemption', '1994'))
        self.assertFalse(d.danmubox_matches('测试电影预告', '测试电影'))
        self.assertFalse(d.danmubox_matches('不是测试电影', '测试电影'))
        self.assertFalse(d.danmubox_matches('Superman', 'Man'))
        self.assertFalse(d.danmubox_matches('The Shawshank Redemption 2', 'The Shawshank Redemption'))
        self.assertTrue(d.danmubox_matches('2012 (2009)', '2012', '2009'))
        self.assertFalse(d.danmubox_matches('2012 (2010)', '2012', '2009'))

    def test_catalog_cache_avoids_network_and_stale_cache_survives_outage(self):
        with patch.object(d, 'web_bytes', side_effect=[b'repo007', INDEX_ENCRYPTED]) as network, \
                patch.object(d, 'decode_danmubox_index', return_value=INDEX_TEXT):
            first = d.danmubox_catalog()
        self.assertEqual(network.call_count, 2)
        cache = self.folder / 'NasDanmaku' / 'cache' / 'danmubox' / 'catalog.json'
        # The Windows runner's file clock can lead time.time() briefly. Pin
        # cache age so this tests fresh/stale behavior, not clock alignment.
        now = cache.stat().st_mtime + 1
        with patch.object(d.time, 'time', return_value=now), \
                patch.object(d, 'web_bytes', side_effect=AssertionError('不能重复查目录')):
            self.assertEqual(d.danmubox_catalog(), first)
        os.utime(cache, (0, 0))
        updates = []
        with patch.object(d.time, 'time', return_value=now), \
                patch.object(d, 'web_bytes', side_effect=d.ToolError('离线')):
            self.assertEqual(d.danmubox_catalog(updates.append), first)
        self.assertTrue(any('旧目录' in update.message for update in updates))

    def test_catalog_mirror_and_failed_refresh_do_not_cache_partial_directory(self):
        with patch.object(d, 'web_bytes', side_effect=[d.ToolError('主站失败'), b'repo007', INDEX_ENCRYPTED]), \
                patch.object(d, 'decode_danmubox_index', return_value=INDEX_TEXT):
            self.assertEqual(len(d.danmubox_catalog()), 1)
        cache = self.folder / 'NasDanmaku' / 'cache' / 'danmubox' / 'catalog.json'
        cache.unlink()
        with patch.object(d, 'web_bytes', side_effect=[b'repo007', d.ToolError('离线'), d.ToolError('离线')]):
            with self.assertRaises(d.ToolError):
                d.danmubox_catalog()
        self.assertFalse(cache.exists())

    def test_download_fallback_parses_actual_count_and_rejects_multi_file_packages(self):
        with patch.object(d, 'web_bytes', side_effect=[d.ToolError('CDN失败'), archive()]) as network:
            comments, source, url = d.fetch_danmubox(ENTRY)
        self.assertEqual(len(comments), 2)
        self.assertIn('raw.githubusercontent.com/dmrepository/', network.call_args.args[0])
        self.assertIn('历史归档', source)
        self.assertTrue(url.startswith('https://cdn.jsdelivr.net/gh/dmrepository/'))
        with patch.object(d, 'web_bytes', return_value=archive({'a.xml': XML, 'b.xml': XML})):
            with self.assertRaisesRegex(d.ToolError, '多份文件'):
                d.fetch_danmubox(ENTRY)
        with patch.object(d, 'web_bytes', return_value=archive({'../movie.xml': XML})):
            with self.assertRaises(d.ToolError):
                d.fetch_danmubox(ENTRY)
        self.assertFalse((self.folder / 'movie.xml').exists())

    def test_invalid_remote_directory_does_not_prevent_other_movie_sources(self):
        movie = dict(title='测试电影', year='2020', duration='', links={'qq': 'https://v.qq.com/x/test'})
        with patch.object(d, 'dandan_config', return_value={'enabled': False}), \
                patch.object(d, 'search_movies', return_value=[movie]), \
                patch.object(d, 'discover_subtitles', return_value=([], [])), \
                patch.object(d, 'web_bytes', return_value=b'\xff\xfe'), \
                patch.object(d, 'fetch_public_danmaku', return_value=([d.Comment(1, '原来源')], '腾讯', 'url')):
            result = d.scan_movie(None, '测试电影 2020')
        self.assertEqual(result.selected_platform, 'qq')
        self.assertEqual(result.comments[0].text, '原来源')
        self.assertTrue(any('目录格式无效' in warning for warning in result.warnings))

    @unittest.skipUnless(shutil.which('7z') or shutil.which('7zz'), '需要 7-Zip')
    def test_real_sevenzip_archive_can_be_imported(self):
        path = self.folder / 'movie.xml'
        path.write_bytes(XML)
        bundle = self.folder / 'movie.7z'
        subprocess.run([shutil.which('7z') or shutil.which('7zz'), 'a', str(bundle), str(path)],
                       check=True, capture_output=True, timeout=20)
        with patch.object(d, 'web_bytes', return_value=bundle.read_bytes()):
            comments, _, _ = d.fetch_danmubox(ENTRY)
        self.assertEqual(len(comments), 2)

    def test_multiple_archive_sources_switch_without_network_and_retry_only_failed(self):
        movie = dict(title='测试电影', year='2020', links={'qq': 'https://v.qq.com/x/test'}, danmubox=[ENTRY, OTHER])
        result = d.ScanResult(None, {}, {}, (), workspace=self.folder)
        def fetch(movie, progress, platform):
            if platform == d.danmubox_source_id(OTHER):
                raise d.ToolError('归档暂不可用')
            return [d.Comment(1, platform)], platform, 'url'
        with patch.object(d, 'fetch_public_danmaku', side_effect=fetch):
            options = d.discover_danmaku_sources(result, movie)
        self.assertEqual(len(options), 3)
        selected_id = d.danmubox_source_id(ENTRY)
        with patch.object(d, 'fetch_public_danmaku', side_effect=AssertionError('不能重复下载')):
            d.select_danmaku_source(result, movie, selected_id)
            self.assertEqual(result.selected_platform, selected_id)
            self.assertIn('新上架版', options[1].label)
        with patch.object(d, 'fetch_public_danmaku', return_value=([d.Comment(2, '恢复')], '历史归档', 'url')) as fetch:
            retried = d.discover_danmaku_sources(result, movie, retry_failed=True)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs['platform'], d.danmubox_source_id(OTHER))
        self.assertIs(retried[1], options[1])
        self.assertEqual(len([row for row in retried if row.available]), 3)
        self.assertTrue(all(Path(row.cache_path).is_file() for row in retried))

    def test_archive_search_still_works_when_other_movie_services_fail(self):
        with patch.object(d, 'dandan_config', return_value={'enabled': False}), \
                patch.object(d, 'search_movies', side_effect=d.ToolError('其他来源离线')), \
                patch.object(d, 'search_danmubox', return_value=[ENTRY]), \
                patch.object(d, 'discover_subtitles', return_value=([], [])), \
                patch.object(d, 'web_bytes', return_value=archive()):
            result = d.scan_movie(None, '测试电影 2020')
        self.assertEqual(len(result.movies), 1)
        self.assertEqual(result.selected_platform, d.danmubox_source_id(ENTRY))
        self.assertEqual(len(result.comments), 2)

    def test_unrelated_platform_movie_does_not_receive_archives_for_requested_title(self):
        movie = dict(title='另一部电影', year='2020', duration='', links={'qq': 'https://v.qq.com/x/test'})
        with patch.object(d, 'dandan_config', return_value={'enabled': False}), \
                patch.object(d, 'search_movies', return_value=[movie]), \
                patch.object(d, 'search_danmubox', return_value=[ENTRY]), \
                patch.object(d, 'discover_subtitles', return_value=([], [])), \
                patch.object(d, 'discover_danmaku_sources', return_value=[]):
            result = d.scan_movie(None, '测试电影 2020')
        self.assertNotIn('danmubox', result.movies[0])
        self.assertEqual(result.movies[1]['danmubox'], [ENTRY])

    def test_abbreviated_query_uses_resolved_movie_title_and_year_for_archives(self):
        entry = dict(ENTRY, name='肖申克的救赎 [新上架版]')
        movie = dict(title='肖申克的救赎', year='1994', duration='', links={'qq': 'https://v.qq.com/x/test'})
        wrong = dict(movie, title='其他电影', links={})
        def search(title, year='', progress=None):
            return [entry] if (title, year) == ('肖申克的救赎', '1994') else []
        with patch.object(d, 'dandan_config', return_value={'enabled': False}), \
                patch.object(d, 'search_movies', return_value=[movie, dict(movie), wrong]), \
                patch.object(d, 'search_danmubox', side_effect=search) as lookup, \
                patch.object(d, 'discover_subtitles', return_value=([], [])), \
                patch.object(d, 'web_bytes', return_value=archive()):
            result = d.scan_movie(None, '肖申克')
        self.assertEqual(result.movies[0].get('danmubox'), [entry])
        self.assertEqual(result.movies[1].get('danmubox'), [entry])
        self.assertNotIn('danmubox', result.movies[2])
        queries = [call.args[:2] for call in lookup.call_args_list]
        self.assertEqual(queries.count(('肖申克的救赎', '1994')), 1)
        self.assertFalse(any('未找到匹配归档' in warning for warning in result.warnings))
        options = result.source_catalog[d.movie_source_key(result.movies[0])]
        archive_option = next(row for row in options if row.platform.startswith('danmubox:'))
        self.assertTrue(archive_option.available)
        self.assertIn('新上架版', archive_option.label)


if __name__ == '__main__':
    unittest.main()
