import json
from pathlib import Path
import re
import tempfile
import unittest

import nas_danmaku as d

SRT = "1\n00:00:01,000 --> 00:00:06,000\n第一行台词\n<i>第二行台词</i>\n\n2\n00:00:08,500 --> 00:00:12,000\n保留原台词时间\n"
XML = '<i><d p="2,1,25,16711680,0,0,u,1">红色弹幕</d><d p="4,5,25,65280,0,0,u,2">顶部转滚动</d></i>'


class CoreTests(unittest.TestCase):
    def test_srt_multiline_and_italic(self):
        doc = d.parse_srt(SRT)
        self.assertEqual(len(doc.events), 2)
        self.assertIn(r"\N{\i1}", doc.events[0]["Text"])
        self.assertEqual(d.parse_ass(doc.dumps()).events, doc.events)

    def test_original_ass_style_and_inline_reset_preserved(self):
        sub = d.parse_srt(SRT)
        sub.events[0]["Text"] = r"{\rDefault\i1}台词,有逗号"
        dm = d.parse_srt(SRT)
        dm.events[0]["Text"] = r"{\rDefault}弹幕"
        merged = d.merge_ass(sub, dm)
        self.assertIn(r"\rSUB_0", merged.events[0]["Text"])
        self.assertIn(r"\rDM_0", merged.events[2]["Text"])
        self.assertEqual(merged.events[0]["Layer"], "0")
        self.assertEqual(merged.events[2]["Layer"], "1")
        self.assertEqual(sub.events[0]["Style"], "Default")
        self.assertEqual(len(d.parse_ass(merged.dumps()).events), 4)

    def test_ass_mismatched_resolution_rejected(self):
        sub, dm = d.parse_srt(SRT), d.parse_srt(SRT)
        dm.info["PlayResX"] = "1280"
        with self.assertRaisesRegex(d.ToolError, "画布不同"):
            d.merge_ass(sub, dm)

    def test_ass_missing_resolution_rejected(self):
        with self.assertRaisesRegex(d.ToolError, "PlayRes"):
            d.parse_ass(d.parse_srt(SRT).dumps().replace("PlayResX: 1920\n", ""))

    def test_dandan_and_bili_colors(self):
        xml, _ = d.parse_comments(XML)
        js, _ = d.parse_comments(json.dumps({"comments": [{"p": "1.2,1,255,user", "m": "蓝色"}]}))
        self.assertEqual(xml[0].color, 0xFF0000)
        self.assertEqual(js[0].color, 255)
        doc, _ = d.render_comments(xml + js, (1920, 1080))
        self.assertIn(r"\c&HFF0000&", doc.events[0]["Text"])
        self.assertIn(r"\c&H0000FF&", doc.events[1]["Text"])

    def test_density_no_overlap_and_bottom_reserved(self):
        comments = [d.Comment(t / 10, f"独特弹幕{t}") for t in range(200)]
        doc, omitted = d.render_comments(comments, (1920, 1080), density=3)
        self.assertGreater(omitted, 100)
        for centisec in range(3000):
            count = sum(d.stamp(e["Start"]) <= centisec < d.stamp(e["End"]) for e in doc.events)
            self.assertLessEqual(count, 3)
        for e in doc.events:
            y = float(re.search(r"\\move\([^,]+,([^,]+)", e["Text"])[1])
            self.assertLess(y + 44, 1080 * .68)

    def test_dedup_and_ass_injection(self):
        comments = [d.Comment(1, r"{\pos(1,1)}\N"), d.Comment(2, r"{\pos(1,1)}\N")]
        doc, omitted = d.render_comments(comments, (1920, 1080))
        self.assertEqual(omitted, 1)
        self.assertNotIn(r"\pos(1,1)", doc.events[0]["Text"])
        self.assertIn("｛＼pos", doc.events[0]["Text"])

    def test_negative_offset_does_not_shift_subtitle(self):
        sub = d.parse_srt(SRT)
        dm, _ = d.render_comments([d.Comment(1, "丢弃"), d.Comment(4, "保留")], sub.resolution, offset=-2)
        merged = d.merge_ass(sub, dm)
        self.assertEqual(merged.events[0]["Start"], "0:00:01.00")
        self.assertEqual(merged.events[2]["Start"], "0:00:02.00")

    def test_time_and_invalid_values(self):
        self.assertEqual(d.stamp("12:34:56.789"), 4529679)
        with self.assertRaises(d.ToolError):
            d.stamp("1:99:00.00")
        for val in (float("inf"), float("nan")):
            with self.assertRaises(d.ToolError):
                d.render_comments([d.Comment(1, "测试")], (1920, 1080), offset=val)

    def test_malformed_and_empty_danmaku(self):
        for text in ('{"success":false}', '{"comments":[]}', '<i><d p="1,7,25,1">特殊</d></i>', '<!DOCTYPE i><i/>'):
            with self.assertRaises(d.ToolError):
                d.parse_comments(text)

    def test_non_overwrite_and_end_to_end_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "原字幕.srt").write_text(SRT, encoding="utf-8")
            (root / "弹幕.xml").write_text(XML, encoding="utf-8")
            result = d.build(subtitle=root / "原字幕.srt", danmaku=root / "弹幕.xml", out_dir=root, name="电影")
            first = Path(result["output"])
            self.assertEqual(first.name, "弹幕版-电影.ass")
            before = first.read_bytes()
            result2 = d.build(subtitle=root / "原字幕.srt", danmaku=root / "弹幕.xml", out_dir=root, name="电影", offset=5)
            self.assertTrue(result2["output"].endswith("-v2.ass"))
            self.assertEqual(first.read_bytes(), before)
            self.assertEqual(len(d.parse_ass(before.decode()).events), 3)  # 默认屏蔽固定弹幕
            self.assertEqual((root / "原字幕.srt").read_text(encoding="utf-8"), SRT)

    def test_filename_guard(self):
        for name in ("../bad", "C:\\file", "con", "bad.", "", "LPT1.ass"):
            with self.assertRaises(d.ToolError):
                d.safe_name(name)

    def test_encoding_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "原字幕.srt"
            for encoding in ("utf-8-sig", "utf-16", "gb18030"):
                path.write_bytes(SRT.encode(encoding))
                self.assertEqual(len(d.load_subtitle(path).events), 2)
