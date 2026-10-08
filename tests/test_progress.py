from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import io
import gc
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

import nas_danmaku as d


BODY = b'x' * 160000


class DownloadHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        if self.path != '/unknown':
            self.send_header('Content-Length', str(d.MAX_BYTES + 1 if self.path == '/large' else len(BODY)))
        self.end_headers()
        if self.path == '/large':
            return
        self.wfile.write(BODY[:1000] if self.path == '/truncated' else BODY)

    def log_message(self, *_):
        pass


class DownloadProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), DownloadHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_known_length_counts_received_bytes(self):
        updates = []
        self.assertEqual(d.web_bytes(self.url + '/known', progress=updates.append), BODY)
        measured = [u for u in updates if u.current is not None]
        self.assertTrue(any(0 < u.current < len(BODY) for u in measured))
        self.assertEqual([u.current for u in measured], sorted(u.current for u in measured))
        self.assertTrue(all(u.total == len(BODY) for u in measured))
        self.assertTrue(measured[-1].complete)
        self.assertEqual(measured[-1].current, len(BODY))
        self.assertTrue(all(u.percent is None or u.percent < 100 for u in updates[:-1]))

    def test_unknown_length_does_not_invent_percentage(self):
        updates = []
        self.assertEqual(d.web_bytes(self.url + '/unknown', progress=updates.append), BODY)
        self.assertTrue(all(u.percent is None for u in updates[:-1]))
        self.assertEqual(updates[-1].current, len(BODY))
        self.assertTrue(updates[-1].complete)

    def test_truncation_and_size_limit_never_complete(self):
        for endpoint in ('/truncated', '/large'):
            with self.subTest(endpoint=endpoint):
                updates = []
                with self.assertRaises(d.ToolError):
                    d.web_bytes(self.url + endpoint, progress=updates.append)
                self.assertFalse(any(u.complete for u in updates))


class WorkProgressTests(unittest.TestCase):
    def test_filtering_counts_all_input_rows(self):
        comments = [d.Comment(i / 10, '相同内容') for i in range(100)]
        updates = []
        doc, omitted = d.render_comments(comments, (1920, 1080), progress=updates.append)
        self.assertEqual(omitted + len(doc.events), len(comments))
        self.assertGreater(omitted, 90)
        self.assertEqual(updates[-1].current, len(comments))
        self.assertEqual(updates[-1].total, len(comments))
        self.assertTrue(updates[-1].complete)

    def test_empty_render_does_not_complete(self):
        updates = []
        with self.assertRaises(d.ToolError):
            d.render_comments([d.Comment(0, '过滤掉')], (1920, 1080), offset=-1, progress=updates.append)
        self.assertFalse(any(u.complete for u in updates))

    def test_save_counts_utf8_bytes_and_finishes_after_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'sample.ass'
            content = '中文台词\n' * 18000
            updates = []
            def progress(update):
                updates.append(update)
                if update.complete:
                    self.assertEqual(target.read_bytes(), content.encode('utf-8'))
                    # Windows 上若写入文件未关闭，独占访问/删除可能失败。
                    target.rename(target.with_suffix('.verified'))
            d.save_new(target, content, progress=progress)
            self.assertTrue(any(0 < u.current < u.total for u in updates))
            self.assertEqual(updates[-1].current, len(content.encode('utf-8')))
            self.assertTrue(updates[-1].complete)

    def test_close_error_does_not_report_completion(self):
        class FailingClose(io.BytesIO):
            def close(self):
                super().close()
                raise OSError('远程写入失败')
        updates = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, 'open', return_value=FailingClose()):
            with self.assertRaises(OSError):
                d.save_new(Path(tmp) / 'sample.ass', '测试', progress=updates.append)
        self.assertFalse(any(u.complete for u in updates))
        self.assertLess(updates[-1].percent, 100)

    def test_scan_reports_completed_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp).resolve() / 'sample.mkv'
            video.write_bytes(b'test')
            updates = []
            with patch.object(d, 'inspect_video', return_value={}), \
                    patch.object(d, 'search_danmubox', return_value=[]), \
                    patch.object(d, 'local_workspace', return_value=Path(tmp).resolve()), \
                    patch.object(d, 'discover_subtitles', return_value=([], [])), \
                    patch.object(d, 'search_movies', return_value=[]):
                result = d.scan_movie(video, progress=updates.append)
            counts = [u.current for u in updates if isinstance(u, d.ProgressUpdate)]
            self.assertEqual(counts, [0, 1, 2])
            self.assertTrue(result.warnings)


class GuiProgressTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest('没有可用的 Tk 显示环境')
        self.root.withdraw()
        self.addCleanup(self.cleanup_gui)
        self.app = d.App(self.root)

    def cleanup_gui(self):
        self.root.destroy()
        del self.app
        gc.collect()

    def test_elapsed_time_never_advances_percentage(self):
        app = self.app
        app.set_busy(True)
        self.assertEqual(str(app.progress_bar['mode']), 'determinate')
        app.apply_progress(d.ProgressUpdate('下载弹幕', 25, 100, '字节'))
        log_before = app.log_box.get('1.0', 'end')
        # 避开浮点减法的整秒边界，某些 Windows 时钟会得到 59.999999 秒。
        with patch.object(d.time, 'monotonic', return_value=app.step_started + 60.5):
            app.refresh_progress_time()
        self.assertEqual(float(app.progress_bar['value']), 25)
        self.assertIn('00:01:00', app.progress_text.get())
        app.apply_progress(d.ProgressUpdate('下载弹幕', 50, 100, '字节'))
        self.assertEqual(app.log_box.get('1.0', 'end'), log_before)
        app.apply_progress('等待接口响应')
        self.assertEqual(float(app.progress_bar['value']), 0)
        self.assertIn('暂无法计算百分比', app.progress_text.get())
        app.apply_progress(d.ProgressUpdate('接收弹幕', 2048, unit='字节'))
        self.assertEqual(float(app.progress_bar['value']), 0)
        self.assertIn('2.0 KB', app.progress_text.get())

    def test_failure_stops_progress_and_success_finishes(self):
        app = self.app
        app.set_busy(True)
        app.apply_progress(d.ProgressUpdate('写入文件', 90, 100, '字节'))
        app.tasks.put(('error', '连接中断', None))
        with patch.object(d.messagebox, 'showerror'):
            app.poll()
        self.assertFalse(app.busy)
        self.assertEqual(float(app.progress_bar['value']), 0)
        self.assertIn('已停止', app.progress_text.get())
        app.set_busy(True)
        app.tasks.put(('done', lambda _: app.status.set('完成'), None))
        app.poll()
        self.assertFalse(app.busy)
        self.assertEqual(float(app.progress_bar['value']), 100)
        self.assertIn('处理结束', app.progress_text.get())


if __name__ == '__main__':
    unittest.main()
