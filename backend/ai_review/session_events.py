"""原生审查会话事件：单任务单调序号、幂等接收、持久化及脱敏导出。"""
import hashlib
import hmac
import json
import re
import secrets
import time

from .common import ReviewError, encode

SESSION_RE = re.compile(r'^[A-Za-z0-9_-]{16,100}$')
EVENT_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,180}$')
SECRET_KEYS = re.compile(r'(authorization|cookie|token|secret|password|api[_-]?key|credential)', re.I)
SECRET_TEXT = re.compile(r'(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{8,}')
SAFE_TOKEN_COUNTS = {'model_context_tokens', 'upstream_output_tokens', 'configured_output_tokens', 'compaction_threshold_tokens'}
EVENT_LIMIT = 256 * 1024
SESSION_LIFECYCLE = {
    'session.starting': {'running'},
    'session.running': {'running'},
    'session.completed': {'completed'},
    'session.error': {'failed', 'aborted', 'unknown'},
}


def migrate(c):
    fields = {
        'session_id': "TEXT NOT NULL DEFAULT ''",
        'session_status': "TEXT NOT NULL DEFAULT ''",
        'current_action': "TEXT NOT NULL DEFAULT ''",
        'deadline_at': 'REAL',
        'last_heartbeat': 'REAL',
        'log_synced_at': 'REAL',
        'event_token_hash': "TEXT NOT NULL DEFAULT ''",
        'event_count': 'INTEGER NOT NULL DEFAULT 0',
        'final_event_seq': 'INTEGER',
        'event_acked_seq': 'INTEGER NOT NULL DEFAULT 0',
        'log_stream_closed': 'INTEGER NOT NULL DEFAULT 0',
        'native_result_sha256': "TEXT NOT NULL DEFAULT ''",
        'native_result_released': 'INTEGER NOT NULL DEFAULT 0',
        'session_counters': "TEXT NOT NULL DEFAULT '{}'",
        'workspace_path': "TEXT NOT NULL DEFAULT ''",
        'session_submitted_at': 'REAL',
    }
    existing = {row[1] for row in c.execute('PRAGMA table_info(jobs)')}
    for name, definition in fields.items():
        if name not in existing:
            c.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS review_session_events (
      job TEXT NOT NULL,
      session_id TEXT NOT NULL,
      seq INTEGER NOT NULL,
      event_id TEXT NOT NULL,
      type TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT '',
      target TEXT NOT NULL DEFAULT '',
      summary TEXT NOT NULL DEFAULT '',
      created_at REAL NOT NULL,
      payload TEXT NOT NULL DEFAULT '{}',
      PRIMARY KEY(job,seq),
      UNIQUE(job,event_id)
    );
    CREATE INDEX IF NOT EXISTS review_session_events_search
      ON review_session_events(job,type,seq);
    CREATE TABLE IF NOT EXISTS review_session_tool_calls (
      job TEXT NOT NULL,
      tool_id TEXT NOT NULL,
      PRIMARY KEY(job,tool_id)
    );
    CREATE TABLE IF NOT EXISTS review_native_bindings (
      token_hash TEXT PRIMARY KEY,
      job TEXT NOT NULL,
      session_id TEXT NOT NULL,
      model TEXT NOT NULL,
      deadline REAL NOT NULL,
      active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS review_native_calls (
      call_id TEXT PRIMARY KEY,
      token_hash TEXT NOT NULL,
      job TEXT NOT NULL,
      session_id TEXT NOT NULL,
      request_hash TEXT NOT NULL,
      status TEXT NOT NULL,
      created REAL NOT NULL,
      updated REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS review_native_calls_request
      ON review_native_calls(token_hash,request_hash,status);
    ''')
    setting = c.execute('SELECT value FROM settings WHERE id=1').fetchone()
    if setting:
        config = json.loads(setting[0])
        if config.get('engine') != 'opencode_native':
            # 引擎收口是一次升级迁移，不是用户更换模型/凭据。保留旧执行代次，
            # 否则下一轮仓库核对会把全部旧 PR 当成新配置自动重审。
            previous_execution_revision = int(config.get('execution_revision', config.get('revision', 1)))
            config['revision'] = int(config.get('revision', 1)) + 1
            config['engine'] = 'opencode_native'
            config['execution_revision'] = previous_execution_revision
            c.execute('UPDATE settings SET value=? WHERE id=1', (encode(config),))
            public = dict(config)
            public['api_key_configured'] = bool(public.pop('api_key', ''))
            c.execute('INSERT OR IGNORE INTO configuration_history VALUES (?,?,?)',
                      (public['revision'], encode(public), time.time()))
            now = time.time()
            c.execute("UPDATE jobs SET status='superseded',stage='升级已停止旧引擎任务',error='原生 OpenCode 升级不会自动重放旧任务',owner='',lease=0,updated=? WHERE status IN ('queued','running')", (now,))
            c.execute("UPDATE jobs SET status='publish_unknown',stage='升级后核对旧发布',error='旧引擎发布阶段被升级中断，只读核对实际结果',owner='',lease=0,updated=? WHERE status IN ('publishing','recovering')", (now,))
            c.execute("DELETE FROM capabilities WHERE job IN (SELECT id FROM jobs WHERE status IN ('superseded','publish_unknown'))")
            c.execute('UPDATE pr_state SET progress_seq=progress_seq+1')


def _redact(value, key=''):
    if key in SAFE_TOKEN_COUNTS and type(value) is int and 0 <= value <= 2**63-1:
        return value
    if SECRET_KEYS.search(key):
        return '[已脱敏]'
    if isinstance(value, dict):
        return {str(k)[:200]: _redact(v, str(k)) for k, v in list(value.items())[:200]}
    if isinstance(value, list):
        return [_redact(v) for v in value[:500]]
    if isinstance(value, str):
        return SECRET_TEXT.sub(lambda match: match.group(1) + ' [已脱敏]', value[:16000])
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def create_session(store, job, owner, deadline_at, workspace):
    """首次执行前先固定 Manager 会话 ID 和事件令牌，请求回执丢失也能只读核对。"""
    session_id = 'rvw_' + secrets.token_urlsafe(24).replace('-', '_')
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with store.tx() as c:
        store._active(c, job, owner)
        row = c.execute('SELECT session_id,deadline_at,event_token_hash FROM jobs WHERE id=?', (job,)).fetchone()
        if row['session_id']:
            return row['session_id'], None, row['deadline_at']
        c.execute('''UPDATE jobs SET session_id=?,session_status='prepared',current_action='准备原生审查会话',
                     deadline_at=?,event_token_hash=?,workspace_path=?,last_heartbeat=?,updated=?
                     WHERE id=? AND owner=?''',
                  (session_id, deadline_at, token_hash, workspace, time.time(), time.time(), job, owner))
    return session_id, token, deadline_at


def mark_submitted(store, job, owner, event_token=None):
    with store.tx() as c:
        store._active(c, job, owner)
        row = c.execute('SELECT session_submitted_at,event_token_hash FROM jobs WHERE id=?', (job,)).fetchone()
        if row['session_submitted_at']:
            raise ReviewError('原生会话已提交，禁止重放原始请求')
        if event_token is None:
            event_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(event_token.encode()).hexdigest()
        if row['event_token_hash'] and not hmac.compare_digest(row['event_token_hash'], token_hash):
            # 仅允许在从未提交时换发；旧令牌不再可用。
            pass
        c.execute("UPDATE jobs SET session_submitted_at=?,session_status='submitted',current_action='启动原生审查会话',event_token_hash=?,updated=? WHERE id=? AND owner=?",
                  (time.time(), token_hash, time.time(), job, owner))
    return event_token


def _validated(data):
    if not isinstance(data, dict):
        raise ReviewError('会话事件需要 JSON 对象')
    try:
        seq = int(data.get('seq'))
    except (TypeError, ValueError):
        raise ReviewError('会话事件序号无效') from None
    session_id, event_id = data.get('session_id'), data.get('event_id')
    if not SESSION_RE.fullmatch(session_id or '') or not EVENT_RE.fullmatch(event_id or ''):
        raise ReviewError('会话或事件标识无效')
    if not 1 <= seq <= 2**63-1 or event_id != f'{session_id}:{seq}':
        raise ReviewError('会话事件序号与稳定标识不一致')
    kind = data.get('type')
    if not isinstance(kind, str) or not EVENT_RE.fullmatch(kind):
        raise ReviewError('会话事件类型无效')
    payload = _redact(data.get('payload') if isinstance(data.get('payload'), (dict, list)) else {})
    if len(encode(payload).encode()) > EVENT_LIMIT:
        raise ReviewError('会话事件超过 256 KiB 限额')
    now = time.time()
    created = data.get('created_at', now)
    if not isinstance(created, (int, float)) or not now-86400 <= created <= now+300:
        created = now
    text = {}
    for key, limit in (('status', 80), ('target', 500), ('summary', 2000)):
        value = data.get(key, '')
        text[key] = _redact(value[:limit]) if isinstance(value, str) else ''
    return dict(job=data.get('job_id'), session_id=session_id, seq=seq, event_id=event_id,
                type=kind, created_at=float(created), payload=payload, **text)


def _receive(c, store, token_hash, event, now):
    job = c.execute('SELECT session_id,event_token_hash,session_counters,current_action FROM jobs WHERE id=?', (event['job'],)).fetchone()
    if (not job or not job['event_token_hash'] or not hmac.compare_digest(job['event_token_hash'], token_hash)
            or job['session_id'] != event['session_id']):
        raise ReviewError('会话事件授权无效')
    serialized = encode(event['payload'])
    existing = c.execute('SELECT * FROM review_session_events WHERE job=? AND (seq=? OR event_id=?)',
                         (event['job'], event['seq'], event['event_id'])).fetchone()
    if existing:
        same = (existing['seq'] == event['seq'] and existing['event_id'] == event['event_id']
                and existing['session_id'] == event['session_id'] and existing['type'] == event['type']
                and existing['status'] == event['status'] and existing['target'] == event['target']
                and existing['summary'] == event['summary'] and existing['payload'] == serialized)
        if not same:
            raise ReviewError('会话事件序号或标识冲突')
        return False
    previous = c.execute('SELECT max(seq) FROM review_session_events WHERE job=?', (event['job'],)).fetchone()[0] or 0
    if event['seq'] != previous + 1:
        raise ReviewError(f'会话事件序号不连续，当前需要 {previous + 1}')
    c.execute('INSERT INTO review_session_events VALUES (?,?,?,?,?,?,?,?,?,?)',
              (event['job'], event['session_id'], event['seq'], event['event_id'], event['type'],
               event['status'], event['target'], event['summary'], event['created_at'], serialized))
    heartbeat = event['type'] == 'heartbeat' or event['type'].endswith('.heartbeat')
    action = event['summary'] or event['target'] or event['type']
    allowed_statuses = SESSION_LIFECYCLE.get(event['type'], set())
    session_status = event['status'] if event['status'] in allowed_statuses else ''
    counters = json.loads(job['session_counters'] or '{}')
    if event['type'] == 'session.compaction':
        phase = event['payload'].get('phase') or event['status']
        active = phase == 'running'
        if active and not counters.get('compaction_active'):
            counters['compactions'] = counters.get('compactions', 0) + 1
        counters['compaction_active'] = active
    elif event['type'] in ('session.completed', 'session.error', 'session.failed', 'session.aborted'):
        counters['compaction_active'] = False
    if counters.get('compaction_active'):
        action = '模型正在压缩上下文，请等待；压缩完成后继续审查'
    category = event['type'].split('.', 1)[0]
    counters[category + '_events'] = counters.get(category + '_events', 0) + 1
    if category == 'tool':
        # running/completed/error 是同一次工具调用的状态，UI 计数按稳定调用 ID 去重。
        tool_id = event['payload'].get('call_id') or event['payload'].get('part_id')
        if isinstance(tool_id, str) and 0 < len(tool_id) <= 200:
            inserted_tool = c.execute('INSERT OR IGNORE INTO review_session_tool_calls VALUES (?,?)',
                                      (event['job'], tool_id)).rowcount
            if inserted_tool:
                counters['tool_calls'] = counters.get('tool_calls', 0) + 1
    c.execute('''UPDATE jobs SET session_status=CASE WHEN ?!='' THEN ? ELSE session_status END,
                 current_action=CASE WHEN ? THEN current_action ELSE ? END,
                 last_heartbeat=CASE WHEN ? THEN ? ELSE last_heartbeat END,
                 log_synced_at=?,event_count=event_count+1,session_counters=?,
                 updated=CASE WHEN updated>? THEN updated ELSE ? END WHERE id=?''',
              (session_status, session_status, heartbeat, action[:500], heartbeat, now, now, encode(counters),
               now, now, event['job']))
    return True


def receive(store, token, data, return_ack=False):
    rows = data if isinstance(data, list) else [data]
    if not 1 <= len(rows) <= 100:
        raise ReviewError('会话事件批次需要 1 至 100 条')
    events = [_validated(row) for row in rows]
    if any(not isinstance(event['job'], str) for event in events):
        raise ReviewError('会话事件缺少任务标识')
    identities = {(event['job'], event['session_id']) for event in events}
    if len(identities) != 1:
        raise ReviewError('会话事件批次不能混合任务或会话')
    token_hash = hashlib.sha256((token or '').encode()).hexdigest()
    now = time.time()
    inserted = 0
    with store.tx() as c:
        for event in events:
            inserted += int(_receive(c, store, token_hash, event, now))
    if return_ack:
        # 只有整批事务提交后才返回水位；因为 _receive 强制
        # seq 连续，该序号同时证明此前事件已持久化。
        return {'inserted': inserted, 'session_id': events[-1]['session_id'],
                'acked_seq': events[-1]['seq']}
    return inserted


def register(app, store):
    from flask import jsonify, request

    @app.post('/internal/sessions/events')
    def native_session_event():
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        try:
            result = receive(store, token, request.get_json(silent=True), return_ack=True)
            return jsonify(ok=True, **result)
        except ReviewError as exc:
            return jsonify(error=str(exc)), 403


def project(job):
    native = bool(job.get('session_id'))
    job['native_session'] = native
    if not native:
        # 迁移前的历史任务没有原生会话，不伪造空会话或永不完成的日志同步。
        for key in ('session', 'deadline', 'counters', 'log_sync'):
            job.pop(key, None)
        return job
    counters = job.get('session_counters') or {}
    counters.setdefault('model_calls', job.get('model_calls', 0))
    counters.setdefault('events', job.get('event_count', 0))
    job['session'] = {'id': job.get('session_id') or '', 'status': job.get('session_status') or '',
                      'result_released': bool(job.get('native_result_released', 0))}
    job['deadline'] = job.get('deadline_at')
    job['counters'] = counters
    final_seq = job.get('final_event_seq')
    acked_seq = job.get('event_acked_seq', 0)
    stream_closed = bool(job.get('log_stream_closed', 0))
    job['log_sync'] = {'last_seq': job.get('event_count', 0), 'final_seq': final_seq,
                       'acked_seq': acked_seq, 'stream_closed': stream_closed,
                       'synced_at': job.get('log_synced_at'),
                       'complete': bool(final_seq and job.get('event_count', 0) >= final_seq
                                        and acked_seq >= final_seq and stream_closed)}
    return job


def query(store, job, after=None, before=None, kinds=None, text='', limit=200):
    if after is not None and before is not None:
        raise ReviewError('after 与 before 不能同时使用')
    clauses, args = ['job=?'], [job]
    if after is not None:
        clauses.append('seq>?'); args.append(after)
    if before is not None:
        clauses.append('seq<?'); args.append(before)
    if kinds:
        clauses.append('type IN (' + ','.join('?' for _ in kinds) + ')'); args.extend(kinds)
    if text:
        escaped = text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        clauses.append("(summary LIKE ? ESCAPE '\\' OR target LIKE ? ESCAPE '\\' OR payload LIKE ? ESCAPE '\\')")
        args.extend(['%' + escaped + '%'] * 3)
    where = ' AND '.join(clauses)
    descending = before is not None or after is None
    with store.connect() as c:
        if not c.execute('SELECT 1 FROM jobs WHERE id=?', (job,)).fetchone():
            raise ReviewError('任务不存在')
        rows = [dict(row) for row in c.execute(
            f'SELECT * FROM review_session_events WHERE {where} ORDER BY seq {"DESC" if descending else "ASC"} LIMIT ?',
            args + [limit + 1]).fetchall()]
    more = len(rows) > limit
    rows = rows[:limit]
    if descending:
        rows.reverse()
    for row in rows:
        row['id'] = row['event_id']
        row['payload'] = json.loads(row['payload'])
        row.pop('job', None)
    return {'items': rows, 'next_after': rows[-1]['seq'] if rows else after,
            'next_before': rows[0]['seq'] if rows else before, 'has_more': more}


def iter_export(store, job, kinds=None, text=''):
    cursor = 0
    while True:
        page = query(store, job, after=cursor, kinds=kinds, text=text, limit=500)
        if not page['items']:
            return
        for event in page['items']:
            yield (json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        cursor = page['items'][-1]['seq']
        if not page['has_more']:
            return
