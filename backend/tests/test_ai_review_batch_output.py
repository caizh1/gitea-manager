"""分批输出包装兼容与严格内容边界；固定响应不代表真实模型效果。"""
import copy
import json
import unittest

from ai_review.batch_plan import parse_report, parse_refutation
from ai_review.common import ReviewError
from ai_review.diagnostics import safe_remote, message_for


class BatchOutputTests(unittest.TestCase):
    def setUp(self):
        self.unit = {'id': 'unit', 'evidence': [{'id': 'F1H1L1', 'anchor_id': 'R1'}]}
        self.finding = {'severity': 'P1', 'title': '固定样本问题', 'trigger': '固定触发条件',
                        'explanation': '固定因果链', 'impact': '固定影响', 'recommendation': '固定建议',
                        'evidence_ids': ['F1H1L1'], 'anchor_id': 'R1'}
        self.report = {'summary': '字符串中的 <think> 标签必须原样保留',
                       'findings': [self.finding], 'limitations': []}
        self.text = json.dumps(self.report, ensure_ascii=False)

    def raw(self, text, complete=True):
        return {'report_markdown': text,
                '_review_completion': {'complete': complete, 'finish_reason': 'stop' if complete else 'length'}}

    def test_complete_wrappers_preserve_report_and_original_response(self):
        wrappers = [lambda value: value, lambda value: '\ufeff'+value,
                    lambda value: ' \n\ufeff'+value+'\n',
                    lambda value: '<think>格式兼容测试</think>\n'+value,
                    lambda value: '```json\n'+value+'\n```',
                    lambda value: '```JSON\r\n'+value+'\r\n```',
                    lambda value: '```\n'+value+'\n```',
                    lambda value: '<think>'+'固定思考片段。'*3000+'</think>\n```json\n'+value+'\n```']
        for wrap in wrappers:
            raw = self.raw(wrap(self.text)); before = copy.deepcopy(raw)
            with self.subTest(包装长度=len(raw['report_markdown'])):
                result = parse_report(raw, {}, self.unit)
                self.assertEqual(result['summary'], self.report['summary'])
                self.assertEqual(result['findings'][0]['id'], 'unit:1')
                self.assertEqual(result['findings'][0]['anchor_id'], 'R1')
                self.assertEqual(raw, before)

    def test_thinking_json_is_never_selected_as_the_report(self):
        decoy = json.dumps({'summary': '思考中的假结论', 'findings': [], 'limitations': []})
        raw = self.raw('<think>'+decoy+'</think>\n'+self.text)
        result = parse_report(raw, {}, self.unit)
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual(result['summary'], self.report['summary'])

    def test_incomplete_or_ambiguous_outputs_remain_failures(self):
        cases = ['<think>'+self.text, '<think>'+self.text+'</think>',
                 '<think>尚未结束\n'+self.text, '<analysis>内容</analysis>'+self.text,
                 '<div>'+self.text+'</div>', '以下是结果：\n'+self.text,
                 '```json\n'+self.text, '```python\n'+self.text+'\n```',
                 self.text+'\n'+self.text, self.text+'\n额外说明',
                 '<think>完整前缀</think>\n'+self.text[:-1],
                 '<think>一</think><think>二</think>'+self.text, '[]', 'null', '', '   ']
        for text in cases:
            with self.subTest(样本长度=len(text)), self.assertRaises(ReviewError) as caught:
                parse_report(self.raw(text), {}, self.unit)
            self.assertEqual(caught.exception.error_code, 'BATCH_OUTPUT_INVALID')

    def test_length_finish_is_not_repaired_by_wrapper_normalization(self):
        with self.assertRaises(ReviewError) as caught:
            parse_report(self.raw('<think>内容</think>'+self.text, complete=False), {}, self.unit)
        self.assertEqual(caught.exception.error_code, 'BATCH_OUTPUT_INCOMPLETE')

    def test_wrappers_do_not_bypass_schema_or_evidence_checks(self):
        for change in ({'evidence_ids': ['不存在的证据']}, {'anchor_id': 'R999'},
                       {'title': ''}, {'severity': '严重'}):
            report = copy.deepcopy(self.report); report['findings'][0].update(change)
            text = '<think>固定思考</think>\n```json\n'+json.dumps(report)+'\n```'
            with self.subTest(变更字段=list(change)), self.assertRaises(ReviewError) as caught:
                parse_report(self.raw(text), {}, self.unit)
            self.assertEqual(caught.exception.error_code, 'BATCH_OUTPUT_INVALID')

    def test_refutation_uses_same_wrappers_and_checks_every_candidate(self):
        checks = {'checks': [{'id': 'unit:1', 'decision': 'confirmed',
                              'reason': '固定核对依据', 'evidence_ids': ['F1H1L1']}]}
        text = '<think>固定思考</think>\n```json\n'+json.dumps(checks)+'\n```'
        result = parse_refutation(self.raw(text), self.unit, [{'id': 'unit:1'}])
        self.assertEqual(result, checks['checks'])
        with self.assertRaises(ReviewError):
            parse_refutation(self.raw(text), self.unit, [{'id': 'unit:2'}])
        with self.assertRaises(ReviewError) as caught:
            parse_refutation(self.raw(text, complete=False), self.unit, [{'id': 'unit:1'}])
        self.assertEqual(caught.exception.error_code, 'BATCH_OUTPUT_INCOMPLETE')

    def test_diagnostics_keep_location_without_thinking_or_report_text(self):
        secret = '不得进入诊断的固定私有片段'
        with self.assertRaises(ReviewError) as caught:
            parse_report(self.raw('<think>'+secret+'</think>\n{"summary":'), {}, self.unit)
        diagnostic = safe_remote(caught.exception.diagnostic)
        self.assertEqual(diagnostic['reason'], 'json_syntax')
        self.assertEqual(diagnostic['json_line'], 1)
        self.assertNotIn(secret, json.dumps(diagnostic, ensure_ascii=False))
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(message_for(caught.exception.error_code, diagnostic['reason']),
                         '模型输出存在 JSON 语法错误，审查未完成')


if __name__ == '__main__':
    unittest.main()
