"""固定执行器配置、压缩事件与分片读取参数保持回归。"""
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'review-runtime'))
import native_session
from native_session import SessionRun
from native_gateway import NativeGateway, leading_system_messages


class CompactionRuntimeTests(unittest.TestCase):
    def test_single_leading_system_preserves_tool_history(self):
        dialogue = [{'role': 'user', 'content': '审查'},
                    {'role': 'assistant', 'content': None, 'tool_calls': [{'id': '调用'}]},
                    {'role': 'tool', 'content': '证据', 'tool_call_id': '调用'}]
        messages = [{'role': 'system', 'content': '初始规则'}] + dialogue + [{'role': 'system', 'content': '收尾规则'}]
        actual = leading_system_messages(messages)
        self.assertEqual(actual, [{'role': 'system', 'content': '初始规则\n\n收尾规则'}] + dialogue)
        self.assertEqual(messages[-1]['content'], '收尾规则')
        self.assertEqual(leading_system_messages(dialogue), dialogue)
        self.assertEqual(leading_system_messages([{'role': 'system', 'content': [{'type': 'text', 'text': '规则'}]},
                         {'role': 'system', 'content': '补充'}])[0]['content'],
                         [{'type': 'text', 'text': '规则'}, {'type': 'text', 'text': '补充'}])

    def test_version_specific_threshold_without_early_pruning(self):
        with tempfile.TemporaryDirectory() as folder, patch('native_session.EventReporter'), patch('legacy_adapter.prepare'):
            row = SessionRun({'job_id':'job_test_12345678', 'session_id':'session_test_12345678',
                'model':'模型', 'deadline_at':time.time()+600, 'event_url':'http://localhost/events',
                'event_token':'事件', 'capability':'能力', 'prompt':'审查',
                'model_context_tokens':262144, 'compaction_threshold_tokens':210000},
                Path(folder), Path(folder), 'http://localhost/v1')
            row.gateway = SimpleNamespace(url='http://localhost/v1', local_token='本地')
            for version in ('1.18.7', '1.1.51'):
                with self.subTest(version=version), patch('native_session.VERSION', version):
                    config = json.loads(row._env()['OPENCODE_CONFIG_CONTENT'])
                    limits = config['provider']['review']['models']['模型']['limit']
                    if version != '1.1.51': self.assertEqual(limits['context'], 262144)
                    self.assertFalse(config['compaction']['prune'])
                    for tokens in (209999, 210000, 210001):
                        triggered = tokens > limits['context']-min(limits['output'],32000) if version == '1.1.51' else tokens >= limits['input']-config['compaction']['reserved']
                        self.assertEqual(triggered, tokens >= 210000)
            events=[]; row.emit=lambda kind, **values: events.append((kind,values))
            row.native_session_id='原生会话'
            marker={'type':'message.part.updated','properties':{'part':{'id':'压缩标记','type':'compaction'}}}
            row._map_event(marker);row._map_event(marker)
            row._map_event({'type':'session.compacted','properties':{'sessionID':'原生会话'}})
            row._map_event(marker)
            self.assertEqual([e[1]['status'] for e in events], ['running','completed'])
            self.assertIn('模型正在压缩',events[0][1]['summary'])
            self.assertIsNotNone(events[1][1]['payload']['duration_seconds'])

    def test_stream_read_keeps_model_limit_and_omitted_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            gateway=NativeGateway('http://localhost','能力','任务','会话','模型',time.time()+600,folder,retrieval_budget=20)
            try:
                for args in ({'filePath':'sample.c','offset':13,'limit':2000},{'filePath':'sample.c'}):
                    stream=gateway.stream_filter()
                    raw=json.dumps(args);split=len(raw)//2
                    def frame(delta,finish=None):
                        return ('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':finish}]})+'\n\n').encode()
                    stream.feed(frame({'tool_calls':[{'index':0,'id':'工具','type':'function','function':{'name':'read','arguments':raw[:split]}}]}))
                    stream.feed(frame({'tool_calls':[{'index':0,'function':{'arguments':raw[split:]}}]},'tool_calls'))
                    stream.feed(b'data: [DONE]\n\n')
                    output=stream.finish()
                    self.assertEqual(json.loads(gateway._tool_calls('text/event-stream',output)[0]['arguments']),args)
                with self.assertRaisesRegex(ValueError,'tool_path_escape'):
                    gateway.validate_response('application/json',json.dumps({'choices':[{'message':{'tool_calls':[{'function':{'name':'read','arguments':'{"filePath":"/etc/passwd","limit":2000}'}}]}}]}).encode())
            finally: gateway.server.server_close()
