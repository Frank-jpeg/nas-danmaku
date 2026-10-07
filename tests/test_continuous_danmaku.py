import re
import unittest

import nas_danmaku as d


def trajectory(row):
    coords = [float(v) for v in re.search(r'\\move\(([^)]+)\)', row['Text'])[1].split(',')]
    x1, y, x2, _ = coords
    length = -min(x1, x2)
    return d.stamp(row['Start']) / 100, d.stamp(row['End']) / 100, x1, x2, y, length


class ContinuousDanmakuTests(unittest.TestCase):
    def test_dense_source_enters_continuously_with_same_screen_limit(self):
        comments = [d.Comment(i / 10, f'连续弹幕 {i}') for i in range(400)]
        doc, omitted = d.render_comments(comments, (1920, 1080))
        paths = [trajectory(row) for row in doc.events]
        starts = [row[0] for row in paths]
        self.assertGreater(len(starts), 25)
        # 不再出现每 8 秒一批；原始评论有足量数据时持续约每 1.4 秒进入。
        self.assertTrue(all(1.3 < b - a < 1.5 for a, b in zip(starts, starts[1:])))
        self.assertEqual(len(doc.events) + omitted, len(comments))
        for moment in (i / 100 for i in range(4800)):
            self.assertLessEqual(sum(start <= moment < end for start, end, *_ in paths), 6)
        # 至少有一行在前一条还没离场时接纳了后一条。
        self.assertTrue(any(a[4] == b[4] and a[0] < b[0] < a[1]
                            for a in paths for b in paths))

    def test_fast_long_follower_does_not_catch_short_leader(self):
        for mode in (1, 6):
            comments = [d.Comment(0, '短', mode=mode), d.Comment(2, '长' * 60, mode=mode),
                        d.Comment(6, '长' * 60, mode=mode)]
            doc, omitted = d.render_comments(comments, (1920, 1080), area=10, font_size=40, density=30)
            self.assertEqual([row['Start'] for row in doc.events], ['0:00:00.00', '0:00:06.00'])
            self.assertEqual(omitted, 1)

    def test_long_tail_must_clear_entry_before_short_follower(self):
        comments = [d.Comment(0, '长' * 60), d.Comment(2, '短'), d.Comment(6, '短')]
        doc, omitted = d.render_comments(comments, (1920, 1080), area=10, font_size=40, density=30)
        self.assertEqual([row['Start'] for row in doc.events], ['0:00:00.00', '0:00:06.00'])
        self.assertEqual(omitted, 1)

    def test_varying_widths_keep_separation_along_rendered_trajectories(self):
        for mode in (1, 6):
            comments = [d.Comment(i * .29, ('长弹幕' * (i % 18 + 1)) + str(i), mode=mode) for i in range(200)]
            doc, _ = d.render_comments(comments, (1920, 1080), density=30, area=10, font_size=40)
            paths = [trajectory(row) for row in doc.events]
            self.assertGreater(len(paths), 8)
            for i, a in enumerate(paths):
                for b in paths[i + 1:]:
                    begin, finish = max(a[0], b[0]), min(a[1], b[1])
                    if a[4] != b[4] or begin >= finish:
                        continue
                    for step in range(101):
                        moment = begin + (finish - begin) * step / 100
                        def bounds(path):
                            start, end, x1, x2, _, length = path
                            left = x1 + (x2 - x1) * (moment - start) / (end - start)
                            return left, left + length
                        left_a, right_a = bounds(a)
                        left_b, right_b = bounds(b)
                        self.assertTrue(right_a + 59 <= left_b or right_b + 59 <= left_a,
                                        (mode, moment, a, b))

    def test_fixed_and_opposite_directions_wait_for_empty_lane(self):
        for first_mode in (1, 4, 5, 6):
            next_mode = 6 if first_mode == 1 else 1
            comments = [d.Comment(0, '前一条', mode=first_mode), d.Comment(4, '等待', mode=next_mode),
                        d.Comment(8, '下一条', mode=next_mode)]
            doc, omitted = d.render_comments(comments, (1920, 1080), density=30, area=10,
                                            font_size=40, block_fixed=False)
            self.assertEqual([row['Start'] for row in doc.events], ['0:00:00.00', '0:00:08.00'])
            self.assertEqual(omitted, 1)

    def test_sparse_source_and_quantized_times_are_not_shifted_or_filled(self):
        source_times = [.004, 2.347, 21.999, 37.125]
        doc, omitted = d.render_comments([d.Comment(t, str(t)) for t in source_times], (1920, 1080))
        self.assertEqual(omitted, 0)
        self.assertEqual([d.stamp(row['Start']) for row in doc.events], [round(t * 100) for t in source_times])
        self.assertTrue(all(d.stamp(row['End']) - d.stamp(row['Start']) == 800 for row in doc.events))


if __name__ == '__main__':
    unittest.main()
