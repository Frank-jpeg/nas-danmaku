import gc
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import nas_danmaku as d


MOVIE = dict(title='测试电影', year='2020', duration='90:00',
             links={'qq': 'https://v.qq.com/x/cover/test.html', 'qiyi': 'https://www.iqiyi.com/v_test.html'})


class RenderSettingsTests(unittest.TestCase):
    def test_default_quarter_small_text_and_fixed_filtered_at_every_resolution(self):
        comments = [d.Comment(0, f'滚动 {i}') for i in range(30)]
        comments += [d.Comment(10, '顶部固定', mode=5), d.Comment(10, '底部固定', mode=4)]
        for width, height in ((1920, 1080), (1280, 720), (3840, 2160)):
            doc, omitted = d.render_comments(comments, (width, height))
            self.assertLessEqual(len(doc.events), 6)
            self.assertEqual(omitted + len(doc.events), len(comments))
            self.assertAlmostEqual(float(doc.styles['Scroll']['Fontsize']), 32 * height / 1080, places=2)
            for row in doc.events:
                self.assertNotIn('固定', row['Text'])
                y = float(re.search(r'\\move\([^,]+,([^,]+)', row['Text'])[1])
                self.assertLessEqual(y + 32 * height / 1080 * 1.45, height / 4 + .01)

    def test_fixed_reverse_and_scroll_share_lanes_inside_selected_region(self):
        comments = [d.Comment(0, '顶部', mode=5), d.Comment(0, '底部', mode=4),
                    d.Comment(0, '反向', mode=6), d.Comment(2, '滚动')]
        doc, omitted = d.render_comments(comments, (1920, 1080), block_fixed=False)
        self.assertEqual(omitted, 0)
        positions = [float(re.search(r'\\(?:pos|move)\([^,]+,([\d.]+)', row['Text'])[1]) for row in doc.events]
        self.assertEqual(len(set(positions)), 4)
        self.assertTrue(all(y + 32 * 1.45 < 270 for y in positions))
        self.assertIn(r'\pos', doc.events[0]['Text'])
        self.assertIn(r'\move(-', doc.events[2]['Text'])
        limited, _ = d.render_comments(comments, (1920, 1080), density=2, block_fixed=False)
        self.assertEqual(len(limited.events), 2)

    def test_protected_area_and_opacity_never_change_dialogue_style(self):
        comments = [d.Comment(i * .3, f'一行 {i}') for i in range(40)]
        protected, _ = d.render_comments(comments, (1920, 1080), density=30, area=100, opacity=37)
        full, _ = d.render_comments(comments, (1920, 1080), density=30, area=100, avoid_subtitles=False)
        ys = lambda doc: [float(re.search(r'\\move\([^,]+,([^,]+)', e['Text'])[1]) for e in doc.events]
        self.assertLess(max(ys(protected)) + 32 * 1.45, 1080 * .68)
        self.assertGreater(max(ys(full)), 1080 * .68)
        self.assertIn(r'\alpha&HA1&', protected.events[0]['Text'])
        sub = d.parse_srt('1\n00:00:00,000 --> 00:00:04,000\n原台词\n')
        merged = d.merge_ass(sub, protected)
        self.assertEqual(merged.styles['SUB_0']['PrimaryColour'], '&H00FFFFFF')
        self.assertEqual(merged.styles['SUB_0']['Fontsize'], sub.styles['Default']['Fontsize'])
        self.assertEqual(merged.styles['DM_0']['PrimaryColour'], '&HA1FFFFFF')

    def test_type_color_dedup_and_speed_settings(self):
        comments = [d.Comment(1, '白色'), d.Comment(1, '红色', 0xff0000), d.Comment(1, '固定', mode=5)]
        doc, omitted = d.render_comments(comments, (1920, 1080), block_color=True)
        self.assertEqual((len(doc.events), omitted), (1, 2))
        fixed, _ = d.render_comments(comments, (1920, 1080), block_scroll=True, block_fixed=False)
        self.assertIn('固定', fixed.events[0]['Text'])
        repeated = [d.Comment(1, '重复'), d.Comment(2, '重复')]
        unique, _ = d.render_comments(repeated, (1920, 1080))
        all_rows, _ = d.render_comments(repeated, (1920, 1080), deduplicate=False, duration=4, offset=2)
        self.assertEqual(len(unique.events), 1)
        self.assertEqual(len(all_rows.events), 2)
        self.assertEqual(all_rows.events[0]['Start'], '0:00:03.00')
        self.assertEqual(all_rows.events[0]['End'], '0:00:07.00')

    def test_invalid_settings_and_empty_filter_fail_clearly(self):
        for settings in ({'area': 0}, {'area': float('nan')}, {'opacity': 101}, {'opacity': float('inf')},
                         {'density': 1.5}, {'font_size': 100, 'area': 10}, {'block_scroll': True}):
            with self.subTest(settings=settings), self.assertRaises(d.ToolError):
                d.render_comments([d.Comment(0, '文字')], (1920, 1080), **settings)

    def test_public_modes_are_preserved_for_filtering(self):
        rows = [[2 if mode == 'left' else 0, mode, '#fff', '25', mode] for mode in ('right', 'top', 'bottom', 'left', 'advanced')]
        comments = d.parse_public_comments({'code': 23, 'danmuku': rows})
        self.assertEqual([c.mode for c in comments], [1, 5, 4, 6])
        doc, omitted = d.render_comments(comments, (1920, 1080))
        self.assertEqual((len(doc.events), omitted), (2, 2))

    @unittest.skipUnless(shutil.which('ffmpeg'), '需要 FFmpeg 的 ASS 渲染器')
    def test_actual_ass_pixels_stay_in_top_quarter(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc, _ = d.render_comments([d.Comment(0, f'弹幕区域验证 {i}') for i in range(20)], (1920, 1080))
            (Path(tmp) / 'sample.ass').write_text(doc.dumps(), encoding='utf-8')
            result = subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-nostdin', '-f', 'lavfi',
                                     '-i', 'color=c=black:s=1920x1080:r=1:d=5', '-vf', 'ass=sample.ass',
                                     '-ss', '4', '-frames:v', '1', '-pix_fmt', 'gray', '-f', 'rawvideo', '-'],
                                    cwd=tmp, capture_output=True, timeout=30)
            if b'No such filter' in result.stderr:
                self.skipTest('FFmpeg 未编译 ASS 渲染支持')
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertEqual(len(result.stdout), 1920 * 1080)
            occupied = [y for y in range(1080) if max(result.stdout[y * 1920:(y + 1) * 1920]) > 20]
            self.assertTrue(occupied)
            self.assertLess(max(occupied), 270)
            self.assertLess(max(result.stdout), 230)  # 不透明度确实进入了渲染结果


class PlatformTests(unittest.TestCase):
    def test_selected_platform_failure_never_silently_changes_platform(self):
        with patch.object(d, 'web_json', side_effect=d.ToolError('主源失败')) as primary, \
                patch.object(d, 'web_bytes', side_effect=d.ToolError('备用也失败')) as backup:
            with self.assertRaises(d.ToolError):
                d.fetch_public_danmaku(MOVIE, lambda _: None, platform='qiyi')
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(backup.call_count, 1)
        for call, field in ((primary.call_args, 'url'), (backup.call_args, 'id')):
            self.assertIn('iqiyi.com', parse_qs(urlsplit(call.args[0]).query)[field][0])
        with patch.object(d, 'web_json') as web, self.assertRaises(d.ToolError):
            d.fetch_public_danmaku(MOVIE, lambda _: None, platform='youku')
        web.assert_not_called()


class SettingsGuiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest('没有 Tk 显示环境')
        self.root.withdraw()
        self.app = d.App(self.root)
        self.addCleanup(self.cleanup)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name).resolve()
        video = folder / '测试.mkv'
        video.write_bytes(b'fixture')
        self.app.result = d.ScanResult(video, {}, {}, d.file_signature(video),
                                      movies=[MOVIE], workspace=folder,
                                      comments=[d.Comment(0, '旧弹幕')],
                                      subtitles=[d.SubtitleChoice('台词', 'online', doc=d.parse_srt('1\n00:00:01,000 --> 00:00:03,000\n台词'))])
        self.app.path.set(str(video))
        self.app.sub_box.configure(values=['台词'])
        self.app.sub_box.current(0)
        self.app.movie_box.configure(values=['电影'])
        self.app.movie_box.current(0)
        self.app.result.source_catalog[d.movie_source_key(MOVIE)] = [
            d.DanmakuSource('qq', [d.Comment(0, '腾讯弹幕')], '腾讯视频', MOVIE['links']['qq']),
            d.DanmakuSource('qiyi', error='查询超时')]
        d.select_danmaku_source(self.app.result, MOVIE, 'qq')
        self.app.refresh_platforms()
        self.app.update_ready()

    def cleanup(self):
        self.root.destroy()
        del self.app
        gc.collect()

    def wait(self):
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.app.busy)

    def test_only_fetched_sources_can_be_selected_and_switch_uses_cache(self):
        app = self.app
        self.assertEqual(tuple(app.platform_box['values']), ('腾讯视频 · 1 条',))
        self.assertIn('爱奇艺 · 未取得', app.source_status.get())
        new_comments = [d.Comment(1, '爱奇艺弹幕')]
        with patch.object(d, 'fetch_public_danmaku', return_value=(new_comments, '爱奇艺', MOVIE['links']['qiyi'])) as fetch:
            app.retry_sources()
            self.assertEqual(str(app.platform_box['state']), 'disabled')
            self.wait()
        self.assertEqual(fetch.call_args.kwargs['platform'], 'qiyi')
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(app.result.selected_platform, 'qq')
        self.assertEqual(tuple(app.platform_box['values']), ('腾讯视频 · 1 条', '爱奇艺 · 1 条'))
        app.platform_box.current(1)
        with patch.object(d, 'fetch_public_danmaku', side_effect=AssertionError('切换不能重新下载')):
            app.change_platform()
        self.assertFalse(app.busy)
        self.assertEqual(app.result.comments, new_comments)
        self.assertTrue(list(Path(self.tmp.name).glob('danmaku*.json')))
        self.assertEqual(str(app.generate_button['state']), 'normal')

    def test_changed_movie_does_not_reuse_another_candidates_sources(self):
        self.app.result.movies = [dict(MOVIE, links={'qq': MOVIE['links']['qq']})]
        self.app.refresh_platforms()
        self.assertEqual(self.app.platform.get(), '来源待查询')
        self.assertEqual(self.app.platform_keys, [])

    def test_dialog_applies_to_actual_synthesis_and_cancel_keeps_settings(self):
        app = self.app
        app.open_settings()
        dialog = app.settings_dialog
        dialog.window.withdraw()
        self.assertNotIn('请检查', dialog.note.get())
        dialog.variables['font_size'].set(24)
        dialog.variables['opacity'].set(37)
        dialog.variables['speed'].set(200)
        dialog.preview()
        dialog.apply()
        with patch.object(d.messagebox, 'showinfo'):
            app.generate()
            self.wait()
        output = next(Path(self.tmp.name).glob('弹幕版-*.ass'))
        doc = d.parse_ass(output.read_text(encoding='utf-8'))
        self.assertEqual(doc.styles['DM_0']['Fontsize'], '24.0')
        self.assertEqual(doc.styles['DM_0']['PrimaryColour'], '&HA1FFFFFF')
        dm = next(row for row in doc.events if row['Style'].startswith('DM_'))
        self.assertEqual(d.stamp(dm['End']) - d.stamp(dm['Start']), 400)
        app.open_settings()
        app.settings_dialog.window.withdraw()
        app.settings_dialog.reset()
        app.settings_dialog.window.destroy()
        self.assertEqual(app.render_settings['font_size'], 24)

    def test_all_types_blocked_rejected_and_ass_layout_controls_disabled(self):
        app = self.app
        app.open_settings()
        dialog = app.settings_dialog
        dialog.window.withdraw()
        dialog.variables['block_scroll'].set(True)
        dialog.apply()
        self.assertIn('不能同时屏蔽', dialog.note.get())
        app.result.dm_ass = d.Ass()
        app.update_ready()
        self.assertEqual(str(app.settings_button['state']), 'disabled')


if __name__ == '__main__':
    unittest.main()
