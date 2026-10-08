import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import nas_danmaku as d


class DandanplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        for mock in (patch.dict(os.environ, {'LOCALAPPDATA': self.tmp.name, 'DANDANPLAY_APP_ID': '', 'DANDANPLAY_APP_SECRET': ''}),
                     patch.object(d, 'local_backup_folder', return_value=self.folder / 'backups'),
                     patch.object(d, 'search_danmubox', return_value=[])):
            mock.start()
            self.addCleanup(mock.stop)
        self.config = dict(enabled=True, app_id='testapp', secret='testsecret')

    def test_signature_uses_binary_sha256_base64_and_path_without_query(self):
        headers = d.dandan_headers('testapp', 'testsecret', '/api/v2/comment/123?withRelated=true', 123456)
        expected = base64.b64encode(hashlib.sha256(b'testapp123456/api/v2/comment/123testsecret').digest()).decode()
        self.assertEqual(headers['X-Signature'], expected)
        self.assertEqual(headers['X-Timestamp'], '123456')
        self.assertNotIn('X-AppSecret', headers)

    def test_cross_host_redirect_strips_authentication_headers(self):
        request = d.urllib.request.Request('https://api.dandanplay.net/api/v2/comment/123',
            headers=d.dandan_headers('testapp', 'testsecret', '/api/v2/comment/123'))
        result = d.SafeRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://cdn.example.test/comments')
        self.assertFalse(any(key.lower().startswith('x-') for key in result.headers))

    def test_request_is_cached_and_reuses_data_without_another_api_call(self):
        with patch.object(d, 'dandan_config', return_value=self.config), \
                patch.object(d, 'web_bytes', return_value=b'{"success":true,"matches":[]}') as network:
            d.dandan_request('/api/v2/match', {'fileName': 'film'})
            d.dandan_request('/api/v2/match', {'fileName': 'film'})
            self.assertEqual(network.call_count, 1)
            self.assertEqual(network.call_args.kwargs['request_headers']['Content-Type'], 'application/json')
            self.assertEqual(json.loads(network.call_args.kwargs['data']), {'fileName': 'film'})
        self.assertEqual(len(list((d.filter_rules_path().parent / 'cache/dandanplay').glob('*.json'))), 1)

    def test_auth_failure_is_actionable_and_never_cached(self):
        with patch.object(d, 'dandan_config', return_value=self.config), \
                patch.object(d, 'web_bytes', side_effect=d.ToolError('api.dandanplay.net 返回 HTTP 403')):
            with self.assertRaisesRegex(d.ToolError, '电脑时间'):
                d.dandan_request('/api/v2/search/episodes?anime=test')
        self.assertFalse((d.filter_rules_path().parent / 'cache/dandanplay').exists())

    def test_unsupported_endpoints_and_missing_credentials_do_not_send_requests(self):
        with patch.object(d, 'web_bytes') as network:
            with self.assertRaises(d.ToolError):
                d.dandan_request('/api/v2/login')
            with self.assertRaises(d.ToolError):
                d.dandan_request('/api/v2/match')
        network.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_secret_is_encrypted_on_disk_and_blank_retains_existing_key(self):
        d.save_dandan_config('testapp', 'only-a-test-secret', True)
        path = d.filter_rules_path().with_name('dandanplay.local.json')
        self.assertNotIn('only-a-test-secret', path.read_text(encoding='utf-8'))
        self.assertEqual(d.dandan_config()['secret'], 'only-a-test-secret')
        d.save_dandan_config('testapp', '', True)
        self.assertEqual(d.dandan_config()['secret'], 'only-a-test-secret')
        d.save_dandan_config('testapp', '', False)
        self.assertFalse(d.dandan_config()['enabled'])

    def test_match_hashes_only_first_16mb_and_omits_directory_and_extension(self):
        video = self.folder / 'Movie.mkv'
        head = b'A' * (16 * 1024 * 1024)
        video.write_bytes(head + b'not-part-of-hash')
        with patch.object(d, 'dandan_request', return_value={'matches': []}) as request:
            d.dandan_match(video, {'format': {'duration': '123.5'}})
        payload = request.call_args.args[1]
        self.assertEqual(payload['fileHash'], hashlib.md5(head).hexdigest())
        self.assertEqual(payload['fileName'], 'Movie')
        self.assertEqual(payload['videoDuration'], 123)
        self.assertEqual(payload['fileSize'], video.stat().st_size)

    def test_search_and_match_results_become_distinct_selectable_sources(self):
        match = d.dandan_movies({'isMatched': True, 'matches': [{'episodeId': 123, 'animeTitle': '电影', 'shift': 2.5}]})[0]
        search = d.dandan_movies({'animes': [{'animeTitle': '电影', 'episodes': [{'episodeId': 123, 'episodeTitle': '正片'}]}]}, search=True)[0]
        self.assertIn('dandanplay', match['links'])
        self.assertTrue(match['official_exact'])
        self.assertEqual(match['official_shift'], 2.5)
        self.assertNotEqual(d.movie_source_key(match), d.movie_source_key(search))

    def test_official_comments_include_related_and_apply_shift_once(self):
        movie = d.dandan_movies({'matches': [{'episodeId': 123, 'animeTitle': '电影', 'shift': 2}]})[0]
        data = {'count': 1, 'comments': [{'p': '1.5,5,16777215,1', 'm': '弹幕'}]}
        with patch.object(d, 'dandan_request', return_value=data) as request:
            comments, source, _ = d.fetch_public_danmaku(movie, None, 'dandanplay')
        self.assertIn('withRelated=true', request.call_args.args[0])
        self.assertEqual((comments[0].time, comments[0].mode), (3.5, 5))
        self.assertIn('已应用匹配偏移', source)
        self.assertIn('弹弹play', d.DanmakuSource('dandanplay', comments, source).label)

    def test_scan_prefers_official_match_and_fetches_only_that_selected_episode(self):
        video = self.folder / 'film.mkv'
        video.write_bytes(b'video')
        match = d.dandan_movies({'isMatched': True, 'matches': [{'episodeId': 123, 'animeTitle': '电影'}]})
        with patch.object(d, 'dandan_config', return_value=self.config), \
                patch.object(d, 'inspect_video', return_value={}), \
                patch.object(d, 'discover_subtitles', return_value=([], [])), \
                patch.object(d, 'search_movies', return_value=[]), \
                patch.object(d, 'dandan_match', return_value=match), \
                patch.object(d, 'dandan_request', return_value={'comments': [{'p': '1,1,16777215,1', 'm': '官方'}]}):
            result = d.scan_movie(video)
        self.assertEqual(result.selected_platform, 'dandanplay')
        self.assertEqual(result.comments[0].text, '官方')

    def test_fuzzy_search_rejects_unrelated_titles_and_handles_bilingual_filenames(self):
        movies = d.dandan_movies({'animes': [
            {'animeTitle': '如果历史是一群喵 (3)', 'episodes': [{'episodeId': 1}]},
            {'animeTitle': '楚门的世界', 'episodes': [{'episodeId': 2, 'episodeTitle': '电影正片'}]}
        ]}, search=True)
        for title in ('楚门的世界', '楚门的世界 The Truman Show'):
            with self.subTest(title=title):
                selected = d.dandan_candidates(movies, title)
                self.assertEqual([m['official_title'] for m in selected], ['楚门的世界'])
                self.assertFalse(selected[0]['official_confirm'])

    def test_title_only_search_uses_official_catalog_without_matching_a_file(self):
        movies = d.dandan_movies({'animes': [{'animeTitle': '楚门的世界', 'episodes': [
            {'episodeId': 1000371650001, 'episodeTitle': '电影正片'}]}]}, search=True)
        with patch.object(d, 'dandan_config', return_value=self.config), \
                patch.object(d, 'discover_subtitles', return_value=([], [])), \
                patch.object(d, 'search_movies', return_value=[]), \
                patch.object(d, 'dandan_match') as match, \
                patch.object(d, 'dandan_search', return_value=movies) as search, \
                patch.object(d, 'dandan_request', return_value={'comments': [{'p': '1,1,16777215,1', 'm': '官方'}]}):
            result = d.scan_movie(None, '楚门的世界 1998')
        match.assert_not_called()
        self.assertEqual(search.call_args.args[0], '楚门的世界')
        self.assertEqual(search.call_args.args[2], '1998')
        self.assertIsNone(result.video)
        self.assertEqual(result.comments[0].text, '官方')
        self.assertEqual(result.selected_platform, 'dandanplay')

    def test_exact_hash_match_can_keep_a_title_in_another_language(self):
        movies = d.dandan_movies({'isMatched': True, 'matches': [
            {'episodeId': 1, 'animeTitle': 'The Truman Show'}]})
        self.assertEqual(len(d.dandan_candidates(movies, '楚门的世界')), 1)
        self.assertEqual(d.dandan_candidates(d.dandan_movies({'matches': [
            {'episodeId': 1, 'animeTitle': 'The Truman Show'}]}), '楚门的世界'), [])

    def test_multiple_episodes_require_a_choice_without_mutating_cached_candidates(self):
        movies = d.dandan_movies({'animes': [{'animeTitle': '电影', 'episodes': [
            {'episodeId': 1}, {'episodeId': 2}]}]}, search=True)
        selected = d.dandan_candidates(movies, '电影')
        self.assertTrue(all(m['official_confirm'] for m in selected))
        self.assertNotIn('official_confirm', movies[0])

    def test_scan_does_not_download_the_first_of_multiple_official_episodes(self):
        video = self.folder / '电影.mkv'
        video.write_bytes(b'video')
        movies = d.dandan_movies({'animes': [{'animeTitle': '电影', 'episodes': [
            {'episodeId': 1}, {'episodeId': 2}]}]}, search=True)
        with patch.object(d, 'dandan_config', return_value=self.config), \
                patch.object(d, 'inspect_video', return_value={}), \
                patch.object(d, 'discover_subtitles', return_value=([], [])), \
                patch.object(d, 'search_movies', return_value=[]), \
                patch.object(d, 'dandan_search', return_value=movies), \
                patch.object(d, 'discover_danmaku_sources') as download:
            result = d.scan_movie(video, override='电影')
        download.assert_not_called()
        self.assertEqual(len(result.movies), 2)
        self.assertFalse(result.comments)
        self.assertTrue(any('多个候选' in warning for warning in result.warnings))

    def test_unrelated_official_results_cannot_replace_existing_movie_source(self):
        video = self.folder / '楚门的世界.mkv'
        video.write_bytes(b'video')
        movies = d.dandan_movies({'matches': [{'episodeId': 1, 'animeTitle': '如果历史是一群喵 (3)'}]})
        fallback = dict(title='楚门的世界', year='1998', duration='', links={'bilibili1': 'https://www.bilibili.com/bangumi/play/ep250583'})
        with patch.object(d, 'dandan_config', return_value=self.config), \
                patch.object(d, 'inspect_video', return_value={}), \
                patch.object(d, 'discover_subtitles', return_value=([], [])), \
                patch.object(d, 'search_movies', return_value=[fallback]), \
                patch.object(d, 'dandan_match', return_value=movies), \
                patch.object(d, 'dandan_search', return_value=movies), \
                patch.object(d, 'discover_danmaku_sources', return_value=[]) as download:
            result = d.scan_movie(video)
        self.assertEqual(result.movies, [fallback])
        self.assertEqual(download.call_args.args[1], fallback)
        self.assertTrue(any('片名相符' in warning for warning in result.warnings))

    def test_empty_official_comments_have_a_specific_message(self):
        movie = d.dandan_movies({'matches': [{'episodeId': 1, 'animeTitle': '电影'}]})[0]
        with patch.object(d, 'dandan_request', return_value={'count': 0, 'comments': []}):
            with self.assertRaisesRegex(d.ToolError, '此节目暂时没有弹幕'):
                d.fetch_public_danmaku(movie, None, 'dandanplay')

    def test_empty_comments_do_not_block_an_explicit_retry_with_a_cached_zero(self):
        empty = b'{"count":0,"comments":[]}'
        filled = b'{"count":1,"comments":[{"p":"1,1,16777215,1","m":"test"}]}'
        with patch.object(d, 'dandan_config', return_value=self.config), \
                patch.object(d, 'web_bytes', side_effect=[empty, filled]) as network:
            d.dandan_request('/api/v2/comment/123')
            retried = d.dandan_request('/api/v2/comment/123')
        self.assertEqual(network.call_count, 2)
        self.assertEqual(retried['count'], 1)

    def test_movie_catalog_search_finds_truman_and_keeps_large_episode_id(self):
        catalog = {'animes': [{'animeTitle': '楚门的世界', 'bangumiId': 'tmdb-movie-37165', 'startDate': '1998-06-04'}]}
        detail = {'bangumi': {'animeTitle': '楚门的世界', 'titles': [{'title': 'The Truman Show'}],
                             'episodes': [{'episodeId': 1000371650001, 'episodeTitle': '楚门的世界'}]}}
        for title in ('楚门的世界', 'The Truman Show', '楚门的世界 The Truman Show'):
            with self.subTest(title=title), patch.object(d, 'dandan_request', side_effect=[catalog, detail]) as request:
                movies = d.dandan_search(title, year='1998')
                self.assertEqual(len(movies), 1)
                self.assertEqual(movies[0]['title'], '楚门的世界')
                self.assertEqual(movies[0]['year'], '1998')
                self.assertTrue(movies[0]['links']['dandanplay'].endswith('/1000371650001'))
                self.assertFalse(movies[0]['official_exact'])
                self.assertFalse(movies[0]['official_confirm'])
                self.assertEqual(request.call_count, 2)
                self.assertTrue(request.call_args_list[0].args[0].startswith('/api/v2/search/tmdb?keyword='))
                self.assertEqual(request.call_args_list[1].args[0], '/api/v2/bangumi/tmdb-movie-37165')

    def test_movie_search_skips_tv_wrong_year_and_unsafe_detail_ids(self):
        catalog = {'animes': [
            {'bangumiId': 'tmdb-tv-1', 'animeTitle': '电影', 'startDate': '1998-01-01'},
            {'bangumiId': 'tmdb-movie-1', 'animeTitle': '电影', 'startDate': '2020-01-01'},
            {'bangumiId': 'tmdb-movie-2/../login', 'animeTitle': '电影', 'startDate': '1998-01-01'}]}
        with patch.object(d, 'dandan_request', side_effect=[catalog, {'animes': []}]) as request:
            self.assertEqual(d.dandan_search('电影', year='1998'), [])
        self.assertEqual(request.call_count, 2)
        self.assertTrue(request.call_args_list[1].args[0].startswith('/api/v2/search/episodes?'))

    def test_movie_search_limits_details_and_rejects_unrelated_titles(self):
        catalog = {'animes': [{'animeTitle': '不相关', 'bangumiId': f'tmdb-movie-{i}'} for i in range(1, 8)]}
        detail = {'bangumi': {'animeTitle': '不相关动画', 'episodes': [{'episodeId': 123}]}}
        with patch.object(d, 'dandan_request', side_effect=[catalog, detail, detail, detail, {'animes': []}]) as request:
            self.assertEqual(d.dandan_search('楚门的世界'), [])
        self.assertEqual(request.call_count, 5)

    def test_movie_catalog_failure_can_fall_back_to_episode_search(self):
        episodes = {'animes': [{'animeTitle': '动画电影', 'episodes': [{'episodeId': 123}]}]}
        with patch.object(d, 'dandan_request', side_effect=[d.ToolError('暂时断线'), episodes]):
            movies = d.dandan_search('动画电影')
        self.assertEqual(movies[0]['official_title'], '动画电影')

    def test_movie_catalog_failure_is_not_reported_as_no_match(self):
        with patch.object(d, 'dandan_request', side_effect=[d.ToolError('暂时断线'), {'animes': []}]):
            with self.assertRaisesRegex(d.ToolError, '电影搜索未完成'):
                d.dandan_search('楚门的世界')

    def test_documented_movie_routes_are_signed_but_arbitrary_routes_are_rejected(self):
        with patch.object(d, 'dandan_config', return_value=self.config), \
                patch.object(d, 'web_bytes', return_value=b'{"success":true}') as network:
            for path in ('/api/v2/search/tmdb?keyword=movie', '/api/v2/bangumi/tmdb-movie-37165'):
                d.dandan_request(path)
            for path in ('/api/v2/bangumi/../login', '/api/v2/bangumi/tmdb-movie-37165/send'):
                with self.assertRaises(d.ToolError):
                    d.dandan_request(path)
        self.assertEqual(network.call_count, 2)
        self.assertIn('X-Signature', network.call_args.kwargs['request_headers'])

    def test_file_match_failure_still_queries_movie_title(self):
        video = self.folder / '楚门的世界.1998.mkv'
        video.write_bytes(b'video')
        movies = d.dandan_movies({'animes': [{'animeTitle': '楚门的世界', 'year': '1998',
            'episodes': [{'episodeId': 1000371650001}]}]}, search=True)
        with patch.object(d, 'dandan_config', return_value=self.config), \
                patch.object(d, 'inspect_video', return_value={}), \
                patch.object(d, 'discover_subtitles', return_value=([], [])), \
                patch.object(d, 'search_movies', return_value=[]), \
                patch.object(d, 'dandan_match', side_effect=d.ToolError('暂时断线')), \
                patch.object(d, 'dandan_search', return_value=movies) as search, \
                patch.object(d, 'discover_danmaku_sources', return_value=[]):
            result = d.scan_movie(video)
        self.assertEqual(search.call_args.args[0], '楚门的世界')
        self.assertEqual(search.call_args.args[2], '1998')
        self.assertEqual(result.movies[0]['title'], '楚门的世界')
        self.assertTrue(any('继续按片名查询' in warning for warning in result.warnings))
