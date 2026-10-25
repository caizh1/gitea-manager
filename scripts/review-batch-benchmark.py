#!/usr/bin/env python3
"""固定中文代码样本的 CPU 计数基准；失败如实记录，不替代目标宿主机验收。"""
import argparse
import json
import os
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from ai_review.batch_counter import run
from ai_review.common import ReviewError

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', required=True)
parser.add_argument('--memory', type=int, default=512)
args = parser.parse_args()
output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
result = {'说明': '独立计数进程固定基准；宿主机、容器 CPU 限额与原始数值应一起解释',
          '平台': platform.platform(), '处理器': platform.processor(), '可见CPU': os.cpu_count(),
          '内存预算MiB': args.memory, '样本': []}
for profile in ('qwen38', 'deepseek41'):
    for mib in (1, 10, 64):
        row = {'计数配置': profile, '输入MiB': mib}
        try:
            row.update(run('benchmark', {'mib': mib}, {'tokenizer_profile': profile,
                'batch_memory_mib': args.memory, 'batch_preprocess_seconds': 120}, lock_dir=output.parent))
            row['热计数P95秒'] = max(row['热计数秒'])
            row['结果'] = '完成'
        except ReviewError as exc:
            row.update(结果='受控失败', 原因=str(exc))
        result['样本'].append(row)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
        print(profile, mib, row['结果'], flush=True)
