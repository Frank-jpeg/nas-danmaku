import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import nas_danmaku as d


MOVIE = dict(title='测试电影', year='2020', duration='90:00', links={
    'qq': 'https://v.qq.com/x/cover/test.html',
    'qiyi': 'https://www.iqiyi.com/v_test.html',
    'bilibili1': 'https://www.bilibili.com/video/BVtest',
    'youku': 'https://v.youku.com/v_show/id_test.html',
    'imgo': 'https://www.mgtv.com/b/test.html'})


class SourceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.result = d.ScanResult(self.folder / '影片.mkv', {}, {}, (0, 0), workspace=self.folder)

    @staticmethod
    def fetched(movie, progress, platform=None):
        if platform in ('qq', 'qiyi'):
            raise d.ToolError('主源/备用均未取得可用弹幕')
        count = list(d.PLATFORMS).index(platform) + 1
        return ([d.Comment(i, f'{platform}弹幕{i}') for i in range(count)],
                d.PLATFORMS[platform] + ' · 公共弹幕库', movie['links'][platform])

    def test_all_platforms_are_verified_and_only_real_data_has_counts(self):
        updates = []
        with patch.object(d, 'fetch_public_danmaku', side_effect=self.fetched) as fetch:
            options = d.discover_danmaku_sources(self.result, MOVIE, updates.append)
        self.assertEqual(fetch.call_count, 5)  # 前两个失败，后续来源仍会查
        self.assertEqual([row.platform for row in options], list(d.PLATFORMS))
        self.assertEqual([row.platform for row in options if row.available], ['bilibili1', 'youku', 'imgo'])
        self.assertIn('腾讯视频 · 未取得', d.source_summary(options))
        self.assertIn('哔哩哔哩 · 3 条', d.source_summary(options))
        self.assertNotIn('0 条', d.source_summary(options))
        self.assertEqual([update.current for update in updates], list(range(6)))
        self.assertTrue(all(update.total == 5 for update in updates))
        self.assertTrue(updates[-1].complete)
        for row in options:
            if row.available:
                comments, _ = d.parse_comments(Path(row.cache_path).read_text(encoding='utf-8'))
                self.assertEqual(comments, row.comments)
            else:
                self.assertFalse(row.cache_path)
                self.assertTrue(row.error)
        label = d.movie_choice_label(MOVIE, self.result.source_catalog)
        self.assertIn('哔哩哔哩 · 3 条', label)
        self.assertNotIn('来源待查询', label)

    def test_switching_and_returning_to_movie_reuses_cache_without_network_or_nas(self):
        with patch.object(d, 'fetch_public_danmaku', side_effect=self.fetched):
            first = d.discover_danmaku_sources(self.result, MOVIE)
        with patch.object(d, 'fetch_public_danmaku', side_effect=AssertionError('不得再次下载')), \
                patch.object(d, 'validate_video', side_effect=AssertionError('不得读取 NAS')), \
                patch.object(d, 'cached_comment_file', side_effect=AssertionError('不得重复写缓存')):
            self.assertIs(d.discover_danmaku_sources(self.result, MOVIE), first)
            selected = d.select_danmaku_source(self.result, MOVIE)
            self.assertEqual(selected.platform, 'bilibili1')
            d.select_danmaku_source(self.result, MOVIE, 'imgo')
            self.assertIs(self.result.comments, first[-1].comments)
            self.assertEqual(self.result.danmaku_url, MOVIE['links']['imgo'])
            self.assertEqual(self.result.selected_platform, 'imgo')

    def test_retry_only_failed_sources_preserves_success_and_updates_counts(self):
        with patch.object(d, 'fetch_public_danmaku', side_effect=self.fetched):
            original = d.discover_danmaku_sources(self.result, MOVIE)
        def recovered(movie, progress, platform=None):
            if platform == 'qiyi':
                raise d.ToolError('连接超时')
            return [d.Comment(0, '恢复后的弹幕')], '腾讯视频 · 公共弹幕库备用', movie['links'][platform]
        with patch.object(d, 'fetch_public_danmaku', side_effect=recovered) as fetch:
            retried = d.discover_danmaku_sources(self.result, MOVIE, retry_failed=True)
        self.assertEqual({call.kwargs['platform'] for call in fetch.call_args_list}, {'qq', 'qiyi'})
        self.assertTrue(retried[0].available)
        self.assertFalse(retried[1].available)
        for i in range(2, 5):
            self.assertIs(retried[i], original[i])
        self.assertIn('连接超时', retried[1].error)

    def test_failed_selection_does_not_erase_current_usable_comments(self):
        with patch.object(d, 'fetch_public_danmaku', side_effect=self.fetched):
            d.discover_danmaku_sources(self.result, MOVIE)
        selected = d.select_danmaku_source(self.result, MOVIE)
        with self.assertRaises(d.ToolError):
            d.select_danmaku_source(self.result, MOVIE, 'qq')
        self.assertIs(self.result.comments, selected.comments)
        self.assertEqual(self.result.selected_platform, selected.platform)

    def test_movie_version_and_links_separate_catalogs(self):
        other = copy.deepcopy(MOVIE)
        other['year'] = '2021'
        changed_links = copy.deepcopy(MOVIE)
        changed_links['links']['youku'] = 'https://v.youku.com/v_show/id_other.html'
        with patch.object(d, 'fetch_public_danmaku', side_effect=self.fetched) as fetch:
            for movie in (MOVIE, other, changed_links):
                d.discover_danmaku_sources(self.result, movie)
            d.discover_danmaku_sources(self.result, copy.deepcopy(MOVIE))
        self.assertEqual(fetch.call_count, 15)
        self.assertEqual(len(self.result.source_catalog), 3)
        fresh = dict(MOVIE, title='另一部电影')
        self.assertIn('来源待查询', d.movie_choice_label(fresh, self.result.source_catalog))
        d.select_danmaku_source(self.result, MOVIE)
        d.clear_selected_danmaku(self.result)
        self.assertFalse(self.result.comments)
        self.assertFalse(self.result.selected_platform)
        self.assertIsNone(self.result.selected_movie_key)

    def test_empty_response_and_cache_failure_are_not_selectable(self):
        movie = dict(MOVIE, links={'qq': MOVIE['links']['qq']})
        with patch.object(d, 'fetch_public_danmaku', return_value=([], '腾讯视频', 'url')):
            options = d.discover_danmaku_sources(self.result, movie)
        self.assertFalse(options[0].available)
        self.assertIn('未取得弹幕', d.movie_choice_label(movie, self.result.source_catalog))
        with patch.object(d, 'fetch_public_danmaku', return_value=([d.Comment(0, '弹幕')], '腾讯视频', 'url')), \
                patch.object(d, 'cached_comment_file', side_effect=OSError('磁盘已满')):
            options = d.discover_danmaku_sources(self.result, movie, retry_failed=True)
        self.assertFalse(options[0].available)
        self.assertIn('磁盘已满', options[0].error)
        with self.assertRaises(d.ToolError):
            d.select_danmaku_source(self.result, movie)

    def test_query_concurrency_is_bounded_and_order_does_not_depend_on_finish(self):
        lock = threading.Lock()
        start_three = threading.Event()
        active, highest, started = 0, 0, []
        def fetch(movie, progress, platform=None):
            nonlocal active, highest
            with lock:
                active += 1
                highest = max(highest, active)
                started.append(platform)
                if len(started) >= 3:
                    start_three.set()
            if not start_three.wait(2):
                raise AssertionError('前三个来源应并发查询')
            with lock:
                active -= 1
            return [d.Comment(0, platform)], d.PLATFORMS[platform], movie['links'][platform]
        with patch.object(d, 'fetch_public_danmaku', side_effect=fetch):
            options = d.discover_danmaku_sources(self.result, MOVIE)
        self.assertEqual(highest, 3)
        self.assertEqual([row.platform for row in options], list(d.PLATFORMS))
        self.assertEqual(d.select_danmaku_source(self.result, MOVIE).platform, 'qq')

    def test_movie_without_supported_links_has_explicit_empty_result(self):
        movie = dict(MOVIE, links={'unsupported': 'https://example.test/movie'})
        with patch.object(d, 'fetch_public_danmaku') as fetch:
            self.assertEqual(d.discover_danmaku_sources(self.result, movie), [])
        fetch.assert_not_called()
        self.assertIn('没有受支持', d.source_summary([]))


if __name__ == '__main__':
    unittest.main()
