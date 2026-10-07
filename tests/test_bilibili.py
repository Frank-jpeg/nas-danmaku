import unittest
import zlib
from unittest.mock import patch

import nas_danmaku as d


def varint(n):
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128)
        n >>= 7
    return bytes(result + bytes([n]))


def field(n, value):
    if isinstance(value, bytes):
        return varint(n * 8 + 2) + varint(len(value)) + value
    return varint(n * 8) + varint(value)


def segment(identity, text='测试', at=2000):
    return field(1, field(1, identity) + field(2, at) + field(3, 1) + field(7, text.encode()))


XML = '<i><d p="2,1,25,16777215,0,0,user,9007199254740993">测试</d></i>'.encode()
URL = 'https://www.bilibili.com/bangumi/play/ep250583'
MOVIE = {'links': {'bilibili1': URL}}


class BilibiliTests(unittest.TestCase):
    def network(self, url, **kwargs):
        if '.xml' in url:
            return zlib.compress(XML)[2:-4]
        if '/web/view?' in url:
            return field(4, field(1, 360000) + field(2, 2))
        if 'segment_index=1' in url:
            return segment(9007199254740993)
        if 'segment_index=2' in url:
            return segment(9007199254740994, '第二段', 400000)
        raise AssertionError(url)

    def metadata(self):
        return patch.object(d, 'web_json', return_value={
            'code': 0, 'result': {'episodes': [{'id': 250583, 'cid': 123, 'aid': 456}]}})

    def test_merge_all_segments_and_exact_large_ids(self):
        with self.metadata(), patch.object(d, 'web_bytes', side_effect=self.network):
            comments, source, url = d.fetch_public_danmaku(MOVIE, None, 'bilibili1')
        self.assertEqual([(c.time, c.text) for c in comments], [(2, '测试'), (400, '第二段')])
        self.assertIn('直连', source)
        self.assertNotIn('部分', source)
        self.assertEqual(url, URL)

    def test_partial_failure_preserves_data_and_labels_it(self):
        def network(url, **kwargs):
            if 'segment_index=2' in url:
                raise d.ToolError('超时')
            return self.network(url, **kwargs)
        with self.metadata(), patch.object(d, 'web_bytes', side_effect=network):
            comments, source, _ = d.fetch_bilibili_danmaku(URL)
        self.assertEqual(len(comments), 1)
        self.assertIn('部分获取', source)
        self.assertIn('分段 2', source)

    def test_xml_failure_keeps_segments(self):
        def network(url, **kwargs):
            if '.xml' in url:
                return b'bad xml'
            return self.network(url, **kwargs)
        with self.metadata(), patch.object(d, 'web_bytes', side_effect=network):
            comments, source, _ = d.fetch_bilibili_danmaku(URL)
        self.assertEqual(len(comments), 2)
        self.assertIn('部分获取', source)

    def test_direct_failure_uses_existing_public_fallback(self):
        with patch.object(d, 'fetch_bilibili_danmaku', side_effect=d.ToolError('失败')), \
                patch.object(d, 'web_json', return_value={'code': 23, 'danmuku': [[2, 'right', '#ffffff', '', '公共弹幕']]}):
            comments, source, _ = d.fetch_public_danmaku(MOVIE, None, 'bilibili1')
        self.assertEqual(comments[0].text, '公共弹幕')
        self.assertIn('公益', source)

    def test_p_parameter_is_preserved_and_uses_selected_cid(self):
        movie = {'links': {'bilibili1': 'https://www.bilibili.com/video/BV123?p=2&tracking=x'}}
        metadata = {'code': 0, 'data': {'aid': 456, 'pages': [{'page': 1, 'cid': 99}, {'page': 2, 'cid': 123}]}}
        with patch.object(d, 'web_json', return_value=metadata), \
                patch.object(d, 'web_bytes', side_effect=self.network) as fetch:
            _, _, url = d.fetch_public_danmaku(movie, None, 'bilibili1')
        self.assertTrue(url.endswith('?p=2'))
        self.assertIn('/123.xml', fetch.call_args_list[0].args[0])

    def test_xml_plain_deflate_zlib_and_gzip(self):
        gzip = zlib.compressobj(wbits=31)
        for raw in [XML, zlib.compress(XML), zlib.compress(XML)[2:-4], gzip.compress(XML) + gzip.flush()]:
            self.assertEqual(d.bili_xml_rows(raw)[0][0], '9007199254740993')

    def test_invalid_protobuf_and_decompression_limit(self):
        for raw in [b'\x80', b'\x0a\x08short', b'\x00', b'\x0f']:
            with self.assertRaises(d.ToolError):
                list(d.bili_fields(raw))
        with patch.object(d, 'MAX_BYTES', 10), self.assertRaises(d.ToolError):
            d.bili_xml_rows(zlib.compress(XML))


if __name__ == '__main__':
    unittest.main()
