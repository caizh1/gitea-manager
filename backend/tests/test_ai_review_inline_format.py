"""用户反馈的定位写法，以及格式兼容后的误提取和历史发布边界。"""
import copy
import unittest

from ai_review import diff_inline, diff_review
from ai_review.publish import expected_comments, summary
from test_ai_review_diff import Client


class InlineFormatTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = diff_review.capture(Client(), 1, {'revision': 1})

    def result(self, text):
        return diff_review.adapt({'review_scope': 'diff', 'report_markdown': text,
            '_review_completion': {'complete': True}}, self.snapshot)

    def test_user_report_plain_lowercase_and_optional_location_separator(self):
        # 用户只提供格式摘要；用固定 diff 中真实存在的编号和人工正文验证解析。
        text = 'p1 除数为零 定位R2\n调用除法时失败，需保留非零值。\np1 旧侧约束删除 定位 R1\n该约束被删除。'
        result = self.result(text)
        self.assertEqual([(f['severity'], f['anchor']['id']) for f in result['findings']], [('P1', 'R2'), ('P1', 'R1')])
        self.assertEqual(len(expected_comments({'id': 'test'}, result)), 2)
        self.assertEqual(result['report_markdown'], text)

    def test_heading_markup_and_location_variations_have_identical_meaning(self):
        for title in ('# [p1] 标题', '## [P1] 标题', '### **[P1] 标题**',
                      '#### [P1] 标题', '- **P1 标题**', '1. [P1] 标题', 'p1 标题'):
            for location in ('定位R2', '定位 R2', '定位: R2', '定位：`R2`', '**定位：** R2',
                             '- **定位**：`r2`', '定位：R2'):
                with self.subTest(title=title, location=location):
                    result = self.result(title+'\n'+location+'\n正文含 `value = 0`，代码保持原样。')
                    self.assertEqual(len(result['findings']), 1)
                    f = result['findings'][0]
                    self.assertEqual((f['severity'], f['title'], f['anchor']['id']), ('P1', '标题', 'R2'))
                    self.assertIn('`value = 0`', f['comment_markdown'])

    def test_unknown_multiple_range_and_contradictory_locations_are_rejected(self):
        for location in ('定位R9999', '定位R1、R2', '定位R1-R2', '定位R2.c',
                         '定位R02', '定位R2\n定位R1', '定位R2\n定位R999', '定位 ../R2'):
            with self.subTest(location=location):
                self.assertEqual(self.result('p1 标题\n'+location+'\n正文')['findings'], [])
        self.assertEqual(self.result('p1 标题 定位R1\n定位R2\n正文')['findings'], [])

    def test_examples_and_unverified_sections_are_not_promoted_to_comments(self):
        block = 'p1 标题\n定位R2\n正文\n'
        for text in ('```markdown\n'+block+'```', '> p1 标题\n> 定位R2\n> 正文',
                     '    p1 标题\n    定位R2\n    正文', '## 待确认\n'+block,
                     '## 待验证\n### [P1] 标题\n定位R2\n正文'):
            with self.subTest(text=text):
                self.assertEqual(self.result(text)['findings'], [])
        text = '## 待确认\n'+block+'## 确定问题\n'+block
        self.assertEqual(len(self.result(text)['findings']), 1)

    def test_body_mentions_alone_are_not_location_fields(self):
        for text in ('p1 标题\n正文提到了 R2。', 'p1 标题\n例子中的定位 R2 未确认。',
                     'p1 标题\n```\n定位R2\n```\n正文', 'p1 标题 定位 R2'):
            self.assertEqual(self.result(text)['findings'], [])

    def test_unanchored_lifecycle_option_is_preserved(self):
        text = '## [P1] 无位置问题\n**定位：** 无\n原因与影响。'
        self.assertEqual(diff_inline.extract(text, self.snapshot, True), [])
        findings = diff_inline.extract(text, self.snapshot, True, include_unanchored=True)
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0]['anchor'])

    def test_statistics_distinguish_duplicate_unrecognized_and_unavailable(self):
        result = self.result('p1 标题\n定位R2\n正文')
        self.assertIn('新增行内评论 1 条', summary({'id': 'test'}, self.snapshot, result))
        result['findings'][0]['existing_review_id'] = 7
        text = summary({'id': 'test'}, self.snapshot, result)
        self.assertIn('新增行内评论 0 条', text)
        self.assertIn('历史重复 1 项', text)
        result = self.result('普通报告')
        self.assertIn('未从报告提取到可发布的定位', summary({'id': 'test'}, self.snapshot, result))
        snapshot = dict(self.snapshot, anchors={}, inline_unavailable='定位索引超出容量')
        self.assertIn('定位索引超出容量', summary({'id': 'test'}, snapshot, result))

    def test_frozen_r1_summary_remains_unchanged_during_recovery(self):
        result = self.result('普通报告')
        result.pop('inline_parser_version', None)
        text = summary({'id': 'test'}, self.snapshot, result)
        self.assertIn('本次行内评论 0 条；未定位或待确认的内容保留在下方报告。', text)
        self.assertNotIn('新增行内评论', text)


if __name__ == '__main__': unittest.main()
