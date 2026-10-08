import copy
import gc
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import nas_danmaku as d

MOVIE = {'title': '电影', 'links': {'bilibili1': 'https://www.bilibili.com/bangumi/play/ep250583'}}


class UXFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        for mock in (patch.dict(os.environ, {'LOCALAPPDATA': self.tmp.name}),
                     patch.object(d, 'local_backup_folder', return_value=self.folder / 'backups')):
            mock.start()
            self.addCleanup(mock.stop)


class UXDataTests(UXFixture, unittest.TestCase):

    def test_partial_source_retry_preserves_old_on_failure_and_replaces_on_complete(self):
        result = d.ScanResult(self.folder / 'film.mkv', {}, {}, (0, 0), workspace=self.folder)
        old = d.DanmakuSource('bilibili1', [d.Comment(0, '旧')], 'B 站（部分获取）', cache_path='old.json')
        result.source_catalog[d.movie_source_key(MOVIE)] = [old]
        with patch.object(d, 'fetch_public_danmaku', side_effect=d.ToolError('断线')) as fetch:
            failed = d.discover_danmaku_sources(result, MOVIE, retry_failed=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(failed[0].comments, old.comments)
        self.assertEqual(failed[0].cache_path, old.cache_path)
        self.assertIn('可重试', failed[0].label)
        with patch.object(d, 'fetch_public_danmaku', return_value=([d.Comment(0, '完整')], '直连 XML＋分段', 'url')):
            good = d.discover_danmaku_sources(result, MOVIE, retry_failed=True)
        self.assertFalse(good[0].partial)
        self.assertEqual(good[0].comments[0].text, '完整')

    def test_first_source_is_published_while_second_is_still_waiting(self):
        ready, release = threading.Event(), threading.Event()
        movie = {'links': {'qq': 'https://v.qq.com/test', 'qiyi': 'https://www.iqiyi.com/test'}}
        result = d.ScanResult(self.folder / 'film.mkv', {}, {}, (0, 0), workspace=self.folder)
        snapshots, errors = [], []
        def fetch(movie, progress, platform):
            if platform == 'qiyi':
                release.wait(3)
            return [d.Comment(0, platform)], platform, 'url'
        def update(options):
            snapshots.append(copy.deepcopy(options))
            if any(row.platform == 'qq' for row in options):
                ready.set()
        def run():
            try:
                d.discover_danmaku_sources(result, movie, on_update=update)
            except Exception as exc:
                errors.append(exc)
        with patch.object(d, 'fetch_public_danmaku', side_effect=fetch):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(ready.wait(2))
                self.assertTrue(worker.is_alive())
                self.assertEqual(len(snapshots[-1]), 1)
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(errors)
        self.assertEqual(len(snapshots[-1]), 2)

    def test_preferences_roundtrip_offsets_and_backup(self):
        settings = dict(d.DM_DEFAULTS, font_size=24, area=35, duration=18)
        offsets = {d.video_preference_key(self.folder / 'a.mkv'): 2.5}
        d.store_preferences(settings, 8, offsets)
        loaded, density, saved = d.load_preferences()
        self.assertEqual((loaded['font_size'], loaded['duration'], density), (24, 18, 8))
        self.assertEqual(saved, offsets)
        self.assertEqual(saved.get(d.video_preference_key(self.folder / 'b.mkv'), 0), 0)
        d.store_preferences(dict(settings, area=40), 8, offsets)
        self.assertEqual(len(list((self.folder / 'backups').glob('*.json'))), 1)

    def test_config_backup_failure_preserves_original(self):
        d.store_preferences(d.DM_DEFAULTS, 6, {})
        path = d.filter_rules_path().with_name('preferences.local.json')
        before = path.read_bytes()
        with patch.object(d.shutil, 'copy2', side_effect=OSError('备份盘不可用')), self.assertRaises(d.ToolError):
            d.store_preferences(dict(d.DM_DEFAULTS, font_size=30), 6, {})
        self.assertEqual(path.read_bytes(), before)

    def test_pending_records_survive_reload_and_success_only_removes_matching_job(self):
        video = self.folder / 'film.mkv'
        video.write_bytes(b'video')
        first = self.folder / 'first.ass'
        second = self.folder / 'second.ass'
        first.write_text('first', encoding='utf-8')
        second.write_text('second', encoding='utf-8')
        value = dict(local_output=str(first), video=str(video), signature=d.file_signature(video),
                     target=str(self.folder / 'target.ass'), saved=False)
        other = dict(value, local_output=str(second))
        d.remember_output(value)
        d.remember_output(other)
        self.assertEqual(len(d.pending_outputs()), 2)
        done = d.publish_cached(value)
        self.assertTrue(done['saved'])
        self.assertEqual([r['local_output'] for r in d.pending_outputs()], [str(second)])
        self.assertEqual((self.folder / 'target.ass').read_text(), 'first')

    def test_filter_breakdown_accounts_for_every_omission(self):
        comments = [d.Comment(-1, '负数'), d.Comment(0, '正常'), d.Comment(1, '正常'),
                    d.Comment(2, '固定', mode=5), d.Comment(3, '垃圾'), d.Comment(4, '空间不够')]
        stats = {}
        doc, omitted = d.render_comments(comments, (1920, 1080), density=1, block_fixed=True,
                                         block_noise=False, block_keywords='垃圾', filter_stats=stats)
        self.assertEqual(len(doc.events), 1)
        self.assertEqual(sum(stats.values()), omitted)
        self.assertEqual(stats, dict(time=1, types=1, duplicates=1, keywords=1, noise=0, density=1))


class UXGuiTests(UXFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest('没有 Tk 显示环境')
        self.root.withdraw()
        self.app = d.App(self.root)
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        self.root.destroy()
        del self.app
        gc.collect()

    def wait(self):
        until = time.monotonic() + 4
        while self.app.busy and time.monotonic() < until:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.app.busy)

    def test_cancelled_old_worker_cannot_override_new_task(self):
        release, started = threading.Event(), threading.Event()
        results = []
        def slow():
            started.set()
            release.wait(3)
            return '旧任务'
        self.app.background(slow, results.append, cancellable=True)
        self.assertTrue(started.wait(1))
        self.app.cancel_task()
        self.app.background(lambda: '新任务', results.append)
        release.set()
        self.wait()
        self.app.poll()
        self.assertEqual(results, ['新任务'])

    def test_stale_snapshot_and_progress_are_discarded(self):
        before = self.app.status.get()
        self.app.tasks.put(('progress', '旧状态', None, -1))
        self.app.tasks.put(('snapshot', lambda _: self.app.status.set('旧快照'), None, -1))
        self.app.poll()
        self.assertEqual(self.app.status.get(), before)

    def test_scan_can_generate_from_early_result_without_waiting_for_slow_source(self):
        video = self.folder / 'film.mkv'
        video.write_bytes(b'video')
        self.app.path.set(str(video))
        movie = dict(MOVIE, year='', duration='1:00', links={
            'qq': 'https://v.qq.com/test', 'qiyi': 'https://www.iqiyi.com/test'})
        subtitle = d.SubtitleChoice('字幕', 'online', doc=d.parse_srt('1\n00:00:01,000 --> 00:00:03,000\n台词'))
        release, slow_finished = threading.Event(), threading.Event()
        def fetch(movie, progress, platform):
            if platform == 'qiyi':
                release.wait(4)
                slow_finished.set()
            return [d.Comment(1, platform)], platform, movie['links'][platform]
        with patch.object(d, 'inspect_video', return_value={'format': {'duration': 60}}), \
                patch.object(d, 'discover_subtitles', return_value=([subtitle], [])), \
                patch.object(d, 'search_movies', return_value=[movie]), \
                patch.object(d, 'fetch_public_danmaku', side_effect=fetch), patch.object(d.messagebox, 'showinfo'):
            self.app.scan()
            try:
                deadline = time.monotonic() + 2
                while (not self.app.result or not self.app.result.comments) and time.monotonic() < deadline:
                    self.root.update()
                    time.sleep(.01)
                self.assertTrue(self.app.busy)
                self.assertEqual(str(self.app.generate_button['state']), 'normal')
                self.app.generate()
                self.wait()
                self.assertFalse(slow_finished.is_set())
                self.assertTrue(self.app.last_output['saved'])
                self.assertIn('qq', Path(self.app.last_output['output']).read_text(encoding='utf-8'))
            finally:
                release.set()
                slow_finished.wait(2)
            self.app.poll()
            self.assertEqual(self.app.result.selected_platform, 'qq')

    def test_app_reloads_pending_output_and_display_preferences(self):
        settings = dict(d.DM_DEFAULTS, area=35, font_size=24)
        d.store_preferences(settings, 8, {})
        value = dict(local_output=str(self.folder / 'local.ass'), output=str(self.folder / 'local.ass'),
                     video=str(self.folder / 'film.mkv'), signature=[1, 2], target=str(self.folder / 'out.ass'), saved=False)
        d.remember_output(value)
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = d.App(self.root)
        self.assertEqual(self.app.render_settings['area'], 35)
        self.assertEqual(self.app.density.get(), '8')
        self.assertEqual(self.app.pending_output['local_output'], value['local_output'])
        self.assertEqual(str(self.app.retry_copy_button['state']), 'normal')

    def test_failed_manual_import_keeps_old_data(self):
        old = d.ScanResult(self.folder / 'film.mkv', {}, {}, (0, 0), workspace=self.folder,
                           comments=[d.Comment(1, '原弹幕')])
        self.app.result = old
        imported = self.folder / 'dm.json'
        imported.write_text('[{"time":2,"text":"新弹幕"}]', encoding='utf-8')
        with patch.object(d.filedialog, 'askopenfilename', return_value=str(imported)), \
                patch.object(d, 'cache_danmaku', side_effect=OSError('磁盘已满')), \
                patch.object(d.messagebox, 'showerror'):
            self.app.pick_danmaku()
            self.wait()
        self.assertIs(self.app.result, old)
        self.assertEqual(self.app.result.comments[0].text, '原弹幕')

    def test_rules_save_applies_even_if_outer_settings_dialog_is_cancelled(self):
        self.app.open_settings()
        parent = self.app.settings_dialog
        parent.window.withdraw()
        parent.edit_keywords()
        dialog = parent.rules_dialog
        dialog.window.withdraw()
        dialog.selected = None
        dialog.rules = []
        dialog.save()
        parent.window.destroy()
        self.assertEqual(self.app.render_settings['filter_rules'], [])
        self.assertEqual(d.load_filter_rules(), [])

    def test_small_screen_has_scrollable_body_and_footer_actions(self):
        top = tk.Toplevel(self.root)
        top.withdraw()
        with patch.object(top, 'winfo_screenwidth', return_value=800), patch.object(top, 'winfo_screenheight', return_value=600):
            body, footer = d.scrollable_window(top, 900, 780)
        self.assertEqual(top.minsize(), (560, 380))
        self.assertIsInstance(body.master, tk.Canvas)
        self.assertIs(footer.master, top)
        top.destroy()

    def test_dandan_dialog_keeps_disabled_setting_and_never_prefills_secret(self):
        d.save_local_json('dandanplay.local.json', dict(version=1, app_id='testapp', enabled=False, protected_secret='encrypted'))
        self.app.open_dandan_settings()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets = list(descendants(self.app.dandan_dialog))
        entries = [w for w in widgets if isinstance(w, d.ttk.Entry)]
        self.assertEqual(self.app.dandan_summary.get(), '弹弹play · 已停用')
        self.assertEqual(entries[0].get(), 'testapp')
        self.assertEqual(entries[1].get(), '')
        self.assertTrue(entries[1].cget('show'))
        self.assertEqual(entries[0].master.winfo_manager(), '')
        edit = next(w for w in widgets if isinstance(w, d.ttk.Button) and w.cget('text') == '修改凭证…')
        edit.invoke()
        self.assertEqual(entries[0].master.winfo_manager(), 'pack')
        enabled = next(w for w in widgets if isinstance(w, d.ttk.Checkbutton))
        self.assertFalse(enabled.instate(['selected']))

    def test_saved_dandan_status_is_visible_without_decryption_or_network(self):
        d.save_local_json('dandanplay.local.json', dict(version=1, app_id='testapp', enabled=True, protected_secret='encrypted'))
        with patch.object(d, 'dandan_protect') as decrypt, patch.object(d, 'dandan_request') as network:
            self.app.open_dandan_settings()
        self.assertEqual(self.app.dandan_summary.get(), '弹弹play · 已启用')
        state = d.dandan_setup_state()
        self.assertTrue(state['configured'])
        self.assertNotIn('protected_secret', state)
        self.assertNotIn('secret', state)
        decrypt.assert_not_called()
        network.assert_not_called()

    def test_dandan_disable_preserves_key_and_refreshes_main_status(self):
        d.save_local_json('dandanplay.local.json', dict(version=1, app_id='testapp', enabled=True, protected_secret='encrypted'))
        self.app.open_dandan_settings()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets = list(descendants(self.app.dandan_dialog))
        enabled = next(w for w in widgets if isinstance(w, d.ttk.Checkbutton))
        enabled.invoke()
        save = next(w for w in widgets if isinstance(w, d.ttk.Button) and w.cget('text') == '保存并验证')
        with patch.object(d, 'dandan_request') as network:
            save.invoke()
            self.wait()
        network.assert_not_called()
        saved = d.load_local_json('dandanplay.local.json', {})
        self.assertEqual(saved['protected_secret'], 'encrypted')
        self.assertFalse(saved['enabled'])
        self.assertEqual(self.app.dandan_summary.get(), '弹弹play · 已停用')

    def test_unconfigured_dandan_dialog_shows_inputs_and_corrupt_config_is_explicit(self):
        self.app.open_dandan_settings()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        entries = [w for w in descendants(self.app.dandan_dialog) if isinstance(w, d.ttk.Entry)]
        self.assertEqual(self.app.dandan_summary.get(), '弹弹play · 未配置')
        self.assertEqual(entries[0].master.winfo_manager(), 'pack')
        self.app.dandan_dialog.destroy()
        path = d.filter_rules_path().with_name('dandanplay.local.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{broken', encoding='utf-8')
        self.app.open_dandan_settings()
        self.assertEqual(self.app.dandan_summary.get(), '弹弹play · 配置异常')
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')

    def test_multiple_official_episodes_stay_unselected_until_user_chooses(self):
        video = self.folder / '电影.mkv'
        video.write_bytes(b'video')
        self.app.path.set(str(video))
        movies = d.dandan_candidates(d.dandan_movies({'animes': [{'animeTitle': '电影', 'episodes': [
            {'episodeId': 1}, {'episodeId': 2}]}]}, search=True), '电影')
        result = d.ScanResult(video, dict(title='电影', year='', source='文件名'), {}, d.file_signature(video),
                              movies=movies, workspace=self.folder)
        with patch.object(d, 'scan_movie', return_value=result):
            self.app.scan()
            self.wait()
        self.assertEqual(self.app.movie_box.current(), -1)
        self.assertIn('选择具体', self.app.source_status.get())
        self.app.movie_box.current(1)
        with patch.object(d, 'fetch_public_danmaku', return_value=([d.Comment(1, '第二集')], '官方', 'url')):
            self.app.change_movie()
            self.wait()
        self.assertEqual(self.app.result.comments[0].text, '第二集')
        self.assertEqual(self.app.result.selected_movie_key, d.movie_source_key(movies[1]))


if __name__ == '__main__':
    unittest.main()
