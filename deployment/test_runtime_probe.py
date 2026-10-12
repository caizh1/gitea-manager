"""慢会话创建只能提交一次，且使用服务检查剩余预算。"""
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'review-runtime'))
import runtime_probe


class ProbeTests(unittest.TestCase):
    def run_probe(self, fail=False):
        now = [0.0]
        calls = []
        original_open = open

        def binary_open(path, *args, **kwargs):
            return io.BytesIO('固定二进制'.encode()) if str(path) == '/opt/review/opencode' else original_open(path, *args, **kwargs)

        class Process:
            pid = 999999999
            returncode = None
            def poll(self): return self.returncode
            def wait(self, timeout): self.returncode = 0

        def launch(args, **kwargs):
            process = Process()
            if '--version' in args:
                kwargs['stdout'].write(b'1.18.7\n')
            return process

        class Session:
            def get(self, url, **kwargs):
                now[0] += 4
                return type('Response', (), {'status_code': 200, 'json': lambda self: {'version': '1.18.7'}})()
            def post(self, url, **kwargs):
                calls.append(kwargs)
                now[0] += 3
                if fail or kwargs['timeout'][1] < 3:
                    raise runtime_probe.requests.ReadTimeout('受控慢会话')
                return type('Response', (), {'status_code': 200, 'json': lambda self: {'id': '固定会话'}})()
            def close(self): pass

        with patch('builtins.open', side_effect=binary_open), patch.object(Path, 'read_text', return_value='flags : sse2'), patch.object(runtime_probe, 'VERSION', '1.18.7'), patch.object(runtime_probe, 'launch', side_effect=launch), patch.object(runtime_probe, 'stop'), patch.object(runtime_probe.requests, 'Session', Session), patch.object(runtime_probe.time, 'monotonic', side_effect=lambda: now[0]):
            result = runtime_probe.probe()
        return result, calls

    def test_slow_session_uses_remaining_budget(self):
        result, calls = self.run_probe()
        self.assertTrue(result['ready'])
        self.assertEqual(len(calls), 1)
        self.assertGreater(calls[0]['timeout'][1], 20)
        self.assertLessEqual(sum(calls[0]['timeout']), 26)

    def test_failed_session_is_not_retried(self):
        result, calls = self.run_probe(fail=True)
        self.assertFalse(result['ready'])
        self.assertEqual(len(calls), 1)
        self.assertIn('受控慢会话', result['reason'])


if __name__ == '__main__':
    unittest.main()
