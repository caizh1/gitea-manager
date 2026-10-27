#!/usr/bin/env python3
"""在后端容器中导出单任务基线；排除访问凭据，保留原始计划和报告供对照。"""
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

job_id = sys.argv[1]
with sqlite3.connect(Path(os.environ['AI_REVIEW_DB']).resolve().as_uri() + '?mode=ro', uri=True) as db:
    db.row_factory = sqlite3.Row
    job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
    plan = json.loads(db.execute('SELECT value FROM review_batch_plans WHERE job=?', (job_id,)).fetchone()[0])
    plan['config'] = {k: v for k, v in plan['config'].items() if k not in ('api_key', 'api_base_url')}
    units = {r['id']: dict(r) for r in db.execute('SELECT * FROM review_batch_units WHERE job=?', (job_id,))}
    calls = []
    for row in db.execute('SELECT * FROM review_batch_attempts WHERE job=? ORDER BY created', (job_id,)):
        raw = json.loads(row['raw']) if row['raw'] else None
        calls.append({'单元': row['unit'], '类型': units[row['unit']]['kind'], '尝试': row['attempt'],
                      '状态': row['state'], '会话': row['session_id'], '开始': row['created'], '结束': row['finished'],
                      '秒数': row['finished'] - row['created'] if row['finished'] else None, '响应': raw})
    measurements = [json.loads(r[0]) for r in db.execute('SELECT value FROM review_batch_measurements WHERE job=?', (job_id,))]
    print(json.dumps({'任务': job_id, '状态': job['status'], '调用数': job['model_calls'],
                      '分类次数': dict(Counter(c['类型'] for c in calls)), '计划': plan, '调用明细': calls,
                      '输入计量': measurements, '报告': json.loads(job['result']) if job['result'] else None,
                      '输出Token': '待从原生会话用量核对，不使用字符数冒充 Token'}, ensure_ascii=False, indent=2))
