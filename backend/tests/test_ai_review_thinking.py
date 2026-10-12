"""思考协议、冻结配置和持续流进度的回归。"""
import json
import unittest
from ai_review.thinking import DEFAULTS, parameters, validate
from ai_review.common import ReviewError
from ai_review.broker import NativeSSEEnd
from tests.test_ai_review_time_budgets import TimeBudgetTests


class ThinkingTests(unittest.TestCase):
    def test_protocol_parameters(self):
        for enabled in (False, True):
            config = dict(DEFAULTS, thinking_protocol='qwen_vllm', thinking_enabled=enabled)
            validate(config)
            self.assertEqual(parameters(config)['chat_template_kwargs']['enable_thinking'], enabled)
            self.assertEqual('thinking_token_budget' in parameters(config), enabled)
            self.assertEqual(parameters(dict(config, thinking_protocol='deepseek')), {'thinking': {'type': 'enabled' if enabled else 'disabled'}})
        self.assertEqual(parameters(DEFAULTS), {})
        self.assertEqual(parameters({'model':'deepseek-test'}), {'thinking':{'type':'disabled'}})

    def test_invalid_types_and_conflicting_output_budget(self):
        for change in ({'thinking_enabled':1}, {'thinking_token_budget':True}, {'thinking_token_budget':0},
                       {'thinking_protocol':'untrusted'}, {'context_retrieval_budget':0},
                       {'thinking_protocol':'qwen_vllm','thinking_enabled':True,'model_max_output_tokens':4096}):
            with self.subTest(change=change), self.assertRaises(ReviewError):
                validate(dict(DEFAULTS, **change))

    def test_fragmented_reasoning_progress_is_not_completion(self):
        collector = NativeSSEEnd()
        raw = ('data: '+json.dumps({'choices':[{'delta':{'reasoning':'正在核对'},'finish_reason':None}]})+'\n\n').encode()
        for byte in raw: collector.feed(bytes([byte]))
        self.assertEqual(collector.phase, 'thinking')
        self.assertEqual(collector.reasoning_chars,4)
        self.assertFalse(collector.trusted)
        collector.feed(b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n')
        self.assertTrue(collector.trusted)
        self.assertEqual(collector.phase,'answering')


class FrozenThinkingTests(TimeBudgetTests):
    def test_thinking_edit_only_affects_new_jobs(self):
        job_id=self.enqueue(); job=self.store.claim()
        before=self.store.job_config(job_id)
        self.change(thinking_protocol='qwen_vllm',thinking_enabled=True,thinking_token_budget=2048,context_retrieval_budget=10)
        self.store.active(job_id,job['owner'])
        self.assertEqual(before,self.store.job_config(job_id))
        self.assertIsNone(self.enqueue())
        new=self.store.enqueue(self.repo,9,{'head':'new','base':'old'})
        self.assertEqual(self.store.job_config(new)['thinking_token_budget'],2048)
