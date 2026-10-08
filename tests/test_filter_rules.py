import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import nas_danmaku as d


def custom_rule(pattern, kind='search', name='我的规则'):
    return dict(id='custom', name=name, kind=kind, parts={'pattern': pattern}, enabled=True, builtin=False,
                description='测试规则', example='', keep_example='')


class FilterRulesTests(unittest.TestCase):
    def test_all_builtin_rules_expose_real_patterns_and_examples(self):
        rules = d.default_filter_rules()
        self.assertEqual(len(rules), 6)
        d.compile_filter_rules(rules)
        for rule in rules:
            with self.subTest(name=rule['name']):
                self.assertTrue(rule['description'])
                self.assertEqual(set(rule['parts']), set(d.RULE_FIELDS[rule['kind']]))
                self.assertTrue(d.evaluate_filter_rules([rule['example']], [rule], all_matches=True)[0])
                self.assertFalse(d.evaluate_filter_rules([rule['keep_example']], [rule], all_matches=True)[0])

    def test_disable_delete_and_edit_change_real_matching(self):
        rules = d.default_filter_rules()
        presence = next(rule for rule in rules if rule['id'] == 'presence')
        self.assertTrue(d.blocked_comment_reason('在吗在吗', rules=rules))
        presence['enabled'] = False
        self.assertFalse(d.blocked_comment_reason('在吗在吗', rules=rules))
        presence['enabled'] = True
        presence['parts']['pattern'] = '只屏蔽这句话'
        self.assertFalse(d.blocked_comment_reason('在吗在吗', rules=rules))
        self.assertEqual(d.blocked_comment_reason('只屏蔽这句话', rules=rules), presence['name'])
        self.assertFalse(d.blocked_comment_reason('2026年10月7日打卡', rules=[]))
        self.assertTrue(d.blocked_comment_reason('在吗在吗', rules=d.default_filter_rules()))

    def test_date_component_changes_apply_and_are_not_hidden_fallbacks(self):
        rules = d.default_filter_rules()
        date = next(rule for rule in rules if rule['id'] == 'date')
        date['parts']['words'] = '只屏蔽这个打卡词'
        self.assertFalse(d.blocked_comment_reason('2026年10月7日打卡', rules=rules))
        date['enabled'] = False
        self.assertFalse(d.blocked_comment_reason('2026年10月7日20:30观看', rules=rules))

    def test_custom_literal_and_regex_are_different_and_all_hits_are_named(self):
        keyword = custom_rule('a.*b', 'keyword', '字面关键词')
        regex = custom_rule(r'a.*b', 'search', '正则规律')
        regex['id'] = 'regex'
        hits = d.evaluate_filter_rules(['A.*B', 'axxxb'], [keyword, regex], all_matches=True)
        self.assertEqual([row['name'] for row in hits[0]], ['字面关键词', '正则规律'])
        self.assertEqual([row['name'] for row in hits[1]], ['正则规律'])
        whole = custom_rule('在吗', 'full')
        self.assertTrue(d.evaluate_filter_rules(['在吗？'], [whole])[0])
        self.assertFalse(d.evaluate_filter_rules(['他在吗'], [whole])[0])

    def test_master_switch_only_controls_builtin_rows(self):
        rule = custom_rule('在吗', 'keyword')
        rules = d.default_filter_rules() + [rule]
        hits = d.evaluate_filter_rules(['在吗', '2026年10月7日打卡'], rules, enabled=False)
        self.assertEqual(hits[0][0]['id'], 'custom')
        self.assertFalse(hits[1])
        rule['enabled'] = False
        self.assertFalse(d.evaluate_filter_rules(['在吗'], rules, enabled=False)[0])

    def test_invalid_regex_and_schema_are_rejected_before_saving(self):
        for pattern in ('(', '('*600 + 'x' + ')'*600):
            with self.subTest(pattern=pattern[:10]), self.assertRaisesRegex(d.ToolError, '我的规则.*正则'):
                d.compile_filter_rules([custom_rule(pattern)])
        for rules in (None, [custom_rule('x'), custom_rule('y')], [dict(custom_rule('x'), enabled='yes')],
                      [dict(custom_rule('x'), parts={})], [dict(custom_rule('x'), kind=[])]):
            with self.assertRaises(d.ToolError):
                d.compile_filter_rules(rules)

    def test_modified_regex_has_execution_timeout(self):
        with self.assertRaisesRegex(d.ToolError, '正则匹配超时'):
            d.evaluate_filter_rules(['a' * 45 + '!'], [custom_rule('(a+)+$')], timeout=.8)

    def test_default_patterns_do_not_start_worker_process(self):
        rules = d.default_filter_rules()
        rules[0]['name'] = '仅修改名称'
        with patch.object(d.subprocess, 'run', side_effect=AssertionError('默认模式不应启动子进程')):
            hits = d.evaluate_filter_rules(['AAAAAAA', '正常剧情讨论'], rules)
        self.assertEqual(hits[0][0]['name'], '仅修改名称')
        self.assertFalse(hits[1])

    def test_saved_rules_survive_reload_and_old_config_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings' / 'rules.json'
            backups = Path(tmp) / 'backups'
            rules = d.default_filter_rules()
            d.save_filter_rules(rules, path, backups)
            original = path.read_bytes()
            self.assertIn('日期', original.decode('utf-8'))
            self.assertEqual(d.load_filter_rules(path), rules)
            self.assertFalse(backups.exists())
            rules[2]['enabled'] = False
            d.save_filter_rules(rules, path, backups)
            files = list(backups.glob('*.json'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), original)
            self.assertFalse(d.blocked_comment_reason('在吗', rules=d.load_filter_rules(path)))
            d.save_filter_rules(rules, path, backups)
            self.assertEqual(len(list(backups.glob('*.json'))), 1)
            d.save_filter_rules([], path, backups)
            self.assertEqual(d.load_filter_rules(path), [])

    def test_backup_or_replace_failure_leaves_old_config_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, backups = Path(tmp) / 'rules.json', Path(tmp) / 'backups'
            rules = d.default_filter_rules()
            d.save_filter_rules(rules, path, backups)
            original = path.read_bytes()
            changed = copy.deepcopy(rules)
            changed[0]['enabled'] = False
            for tool in ('copy2', 'replace'):
                owner = d.shutil if tool == 'copy2' else d.os
                with patch.object(owner, tool, side_effect=OSError('测试写入失败')), self.assertRaises(d.ToolError):
                    d.save_filter_rules(changed, path, backups)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(list(Path(tmp).glob('*.tmp')))

    def test_bad_config_is_reported_and_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rules.json'
            for content in ('{bad', json.dumps({'version': 99, 'rules': []}), '[]'):
                path.write_text(content, encoding='utf-8')
                with self.assertRaises(d.ToolError):
                    d.load_filter_rules(path)
                self.assertEqual(path.read_text(encoding='utf-8'), content)

    def test_render_uses_edited_rules_and_counts_custom_regex(self):
        rules = [custom_rule(r'广告\d+', name='数字广告')]
        stats = {}
        comments = [d.Comment(0, '广告123'), d.Comment(.1, '正常剧情'), d.Comment(2.2, '在吗')]
        doc, omitted = d.render_comments(comments, (1920, 1080), filter_rules=rules, filter_stats=stats)
        self.assertEqual(omitted, 1)
        self.assertEqual(stats, {'noise': 0, 'keywords': 1, 'types': 0, 'duplicates': 0, 'density': 0, 'time': 0})
        self.assertEqual([row['Start'] for row in doc.events], ['0:00:00.10', '0:00:02.20'])


if __name__ == '__main__':
    unittest.main()
