from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import nas_danmaku as d


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), '需要 FFmpeg 和 ffprobe')
class FFmpegTests(unittest.TestCase):
    def test_generated_video_extract_merge_and_pgs_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            subtitle = folder / 'sample.srt'
            subtitle.write_text('1\n00:00:00,500 --> 00:00:02,500\n测试台词\n', encoding='utf-8')
            video = folder / 'sample.mkv'
            subprocess.run([
                shutil.which('ffmpeg'), '-v', 'error', '-nostdin',
                '-f', 'lavfi', '-i', 'color=c=black:s=320x180:r=5:d=3',
                '-i', str(subtitle), '-map', '0:v', '-map', '1:0',
                '-c:v', 'mpeg4', '-c:s', 'ass', '-metadata:s:s:0', 'language=chi',
                '-t', '3', str(video),
            ], check=True, capture_output=True, timeout=30)
            tracks = d.subtitle_tracks(video)
            self.assertEqual(tracks[0]['codec_name'], 'ass')
            doc = d.extract_subtitle(video, tracks[0]['index'])
            self.assertEqual(len(doc.events), 1)
            self.assertIn('测试台词', doc.events[0]['Text'])
            meta = d.inspect_video(video)
            result = d.ScanResult(video, {}, meta, d.file_signature(video),
                                  subtitles=d.embedded_choices(meta),
                                  comments=[d.Comment(1, '测试弹幕'), d.Comment(99, '超出片长')])
            output = d.synthesize(result)
            self.assertEqual(output['subtitle_lines'], 1)
            self.assertEqual(output['danmaku_lines'], 1)
            self.assertEqual(Path(output['output']).parent, folder)
            with patch.object(d, 'subtitle_tracks', return_value=[{'index': 2, 'codec_name': 'hdmv_pgs_subtitle'}]):
                with self.assertRaisesRegex(d.ToolError, '图片字幕'):
                    d.extract_subtitle(video, 2)
