from pathlib import Path
import tempfile
import unittest

import nas_danmaku as d


class NoiseFilterTests(unittest.TestCase):
    def test_common_checkins_time_presence_and_companions_are_filtered(self):
        samples = [
            '2026年10月7日观看', '２０２６年１０月７日觀看', '2026.10.7 打卡',
            '2026/10/07 01:23:45观看', '20261007', '10月7日签到',
            '二〇二六年十月七日打卡', '2026年10月7日20:30打卡',
            '2026年了还有人在看吗', '2026还有在看的吗？', '观看时间2026-10-07',
            '现在是23:58', '北京时间凌晨3点20分', '00:12:34', '晚上十点打卡',
            '第一次观看', '二刷', '我来三刷了', '打卡', '前排',
            '今天和女朋友一起看', '陪老婆看的', '我和张三一起看这部电影',
            '和谁一起看', '我和女朋友一起看过这部电影', '2026年10月7日和女友一起看',
            '在吗？在吗？', '在 吗 在 吗', '在\u200b嗎？', '有人在看吗', '还有人在看吗？',
            'AAAAAAA', '１２１２１２１２１２１２', '111111111', '加QQ群123456789',
            '关注我领取资源', '私信我获取全集', '微信:abc12345',
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertTrue(d.blocked_comment_reason(text))

    def test_plot_discussion_dates_and_normal_reactions_are_kept(self):
        samples = [
            '1998年上映的电影，今天看仍然震撼', '1998', '2026年楚门发现真相',
            '2026年10月7日男主发现了真相', '故事发生在1998年10月7日',
            '12:34这个镜头真好', '五分钟后这个伏笔就揭晓', '凌晨3点是剧情的转折',
            '他一直在吗？这句台词很有意思', '有人看懂这个结尾吗', '谁知道他为什么回头',
            '男主和妻子一起看日落', '和女朋友一起看懂这个伏笔',
            '我和楚门一起看到了自由', '二刷才注意到这里的摄像头',
            '第一次看懂结尾', '我女朋友说这个镜头特别美',
            '路过的人都是演员', '他用微信联系了家人', '群演演得太好了',
            '哈哈哈哈哈哈', '啊啊啊啊啊啊', '666', '2333', '？？？？？？',
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(d.blocked_comment_reason(text), '')

    def test_custom_keywords_are_literal_normalized_and_independent_of_switch(self):
        words = d.compile_block_keywords(' 剧透\nＡＢＣ\na.*b\n\n剧透')
        self.assertEqual(words, ('剧透', 'abc', 'a.*b'))
        for text in ('这里是剧透', 'ａ\u200bＢＣ', 'literal a.*b phrase'):
            self.assertEqual(d.blocked_comment_reason(text, enabled=False, keywords=words), '自定义关键词')
        self.assertEqual(d.blocked_comment_reason('axxxb', keywords=words), '')
        self.assertEqual(d.blocked_comment_reason('在吗在吗', enabled=False), '')
        for bad in (None, 'x' * 81, '\n'.join(str(i) for i in range(101)), 'a' * 10001):
            with self.subTest(bad=str(bad)[:30]), self.assertRaises(d.ToolError):
                d.compile_block_keywords(bad)

    def test_filter_runs_before_lane_and_entry_budget_without_changing_raw_comments(self):
        comments = [d.Comment(0, '2026年10月7日打卡'), d.Comment(.1, '正常剧情讨论'),
                    d.Comment(1.5, '在吗在吗'), d.Comment(1.6, '这个结尾很精彩'),
                    d.Comment(3, '不想看见的指定词')]
        stats = {}
        doc, omitted = d.render_comments(comments, (1920, 1080),
                                         block_keywords='指定词', filter_stats=stats)
        self.assertEqual(stats, {'noise': 2, 'keywords': 1})
        self.assertEqual(omitted, 3)
        self.assertEqual([row['Start'] for row in doc.events], ['0:00:00.10', '0:00:01.60'])
        self.assertEqual(len(comments), 5)
        self.assertEqual(comments[0].text, '2026年10月7日打卡')
        unfiltered, omitted = d.render_comments([comments[0]], (1920, 1080), block_noise=False)
        self.assertEqual(omitted, 0)
        self.assertIn('打卡', unfiltered.events[0]['Text'])

    def test_ambiguous_token_prefixes_do_not_require_exponential_regex_backtracking(self):
        for stem in ('有人', '看过', '我来', '现在', '正在'):
            text = '2026年' + stem * 50 + '剧情分析'
            self.assertEqual(d.blocked_comment_reason(text), '')

    def test_synthesis_reports_noise_counts_and_never_filters_dialogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            nas = folder / 'nas'
            nas.mkdir()
            cache = folder / 'cache'
            cache.mkdir()
            video = nas / '测试.mkv'
            video.write_bytes(b'fixture')
            dialogue = d.parse_srt('1\n00:00:01,000 --> 00:00:03,000\n2026年10月7日观看\n')
            original = dialogue.dumps()
            result = d.ScanResult(video, {}, {}, d.file_signature(video), workspace=cache,
                                  subtitles=[d.SubtitleChoice('原台词', 'online', doc=dialogue)],
                                  comments=[d.Comment(0, '在吗在吗'), d.Comment(1, '保留的剧情评论'),
                                            d.Comment(3, '屏蔽的关键词')])
            value = d.synthesize(result, block_keywords='关键词')
            self.assertTrue(value['saved'])
            self.assertEqual(value['noise_filtered'], 1)
            self.assertEqual(value['keyword_filtered'], 1)
            self.assertEqual(value['danmaku_lines'], 1)
            self.assertEqual(dialogue.dumps(), original)
            merged = d.parse_ass(Path(value['output']).read_text(encoding='utf-8'))
            sub = next(row for row in merged.events if row['Style'].startswith('SUB_'))
            self.assertIn('2026年10月7日观看', sub['Text'])


if __name__ == '__main__':
    unittest.main()
