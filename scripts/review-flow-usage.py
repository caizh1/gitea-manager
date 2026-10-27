#!/usr/bin/env python3
"""在隔离执行器中从保存的原始响应计量，不输出请求、源码或秘密。"""
import json
import sys
from pathlib import Path

job_id = sys.argv[1]
root = Path('/app/review-native')
calls = []
for path in sorted((root / 'sessions').glob('*.json')):
    session = json.loads(path.read_text())
    if session.get('job_id') != job_id:
        continue
    for response in sorted((root / 'diagnostics' / path.stem).glob('model-response-*.raw')):
        usages = []
        for line in response.read_text(errors='replace').splitlines():
            if not line.startswith('data:'):
                continue
            try:
                value = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(value, dict) and value.get('usage'):
                usages.append(value['usage'])
        calls.append({'会话': path.stem, '调用': response.stem, '用量': usages[-1] if usages else None})
print(json.dumps({'任务': job_id, '调用': calls, '有用量调用数': sum(c['用量'] is not None for c in calls),
                  '输入Token': sum(c['用量'].get('prompt_tokens', 0) for c in calls if c['用量']),
                  '输出Token': sum(c['用量'].get('completion_tokens', 0) for c in calls if c['用量'])}, ensure_ascii=False, indent=2))
