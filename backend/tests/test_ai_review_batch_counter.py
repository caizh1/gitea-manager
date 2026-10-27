"""大请求在计数进程延迟读取时仍应完整送达，不依赖短超时管道重放。"""
import subprocess
import sys
from unittest.mock import patch

from ai_review import batch_counter


def test_large_request_survives_delayed_reader_and_poll_timeout(tmp_path):
    original = subprocess.Popen
    def delayed_reader(command, **kwargs):
        return original([sys.executable, '-c',
            'import time; time.sleep(.5); from ai_review.batch_counter import main; main()'], **kwargs)
    config = {'model': '测试模型', 'batch_input_tokens': 900000, 'model_context_tokens': 1048576,
              'model_max_output_tokens': 1024, 'batch_preprocess_seconds': 5, 'batch_memory_mib': 512}
    messages = [{'role': 'user', 'content': '长请求输入' * 30000}]
    with patch.object(subprocess, 'Popen', side_effect=delayed_reader):
        result = batch_counter.run('count', {'messages': messages}, config, lock_dir=tmp_path)
    assert result['input_tokens'] > 400000
    assert result['input_tokens'] <= result['limit']
    assert not list(tmp_path.glob('batch-input-*'))
