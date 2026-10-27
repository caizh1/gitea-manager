"""分批计划、执行单元和尝试的事务边界；不复用旧函数审查表。"""
from collections import OrderedDict
import json
import threading
import time
from .common import ReviewError, Stale, encode
from . import token_budget

WAITING = ('awaiting_cost', 'budget_paused', 'incomplete')
_COST_SUMMARY_CACHE = OrderedDict()
_COST_SUMMARY_LOCK = threading.Lock()
SESSION_FIELDS = ('session_id', 'session_status', 'current_action', 'deadline_at', 'last_heartbeat',
                  'log_synced_at', 'event_token_hash', 'event_count', 'final_event_seq', 'event_acked_seq',
                  'log_stream_closed', 'native_result_sha256', 'native_result_released',
                  'session_counters', 'workspace_path', 'session_submitted_at', 'diagnostic', 'raw_diagnostics')


def migrate(c):
    if 'batch_protocol' not in {r[1] for r in c.execute('PRAGMA table_info(jobs)')}:
        c.execute('ALTER TABLE jobs ADD COLUMN batch_protocol INTEGER NOT NULL DEFAULT 0')
    if 'batch_flow_version' not in {r[1] for r in c.execute('PRAGMA table_info(jobs)')}:
        # 升级前已排队但尚未生成计划的任务，也必须继续旧流程。
        c.execute('ALTER TABLE jobs ADD COLUMN batch_flow_version INTEGER NOT NULL DEFAULT 2')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS review_batch_plans (
      job TEXT PRIMARY KEY, hash TEXT NOT NULL, value TEXT NOT NULL,
      authorized_calls INTEGER NOT NULL DEFAULT 0, approved_by TEXT NOT NULL DEFAULT '',
      approved_at REAL, budget_seconds REAL NOT NULL, active_seconds REAL NOT NULL DEFAULT 0,
      uncertain_time INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS review_batch_units (
      job TEXT NOT NULL, id TEXT NOT NULL, position INTEGER NOT NULL, kind TEXT NOT NULL,
      value TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', result TEXT, error TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(job,id));
    CREATE TABLE IF NOT EXISTS review_batch_attempts (
      job TEXT NOT NULL, unit TEXT NOT NULL, attempt INTEGER NOT NULL,
      session_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'prepared',
      session TEXT NOT NULL DEFAULT '{}', raw TEXT, created REAL NOT NULL, finished REAL,
      PRIMARY KEY(job,unit,attempt));
    CREATE TABLE IF NOT EXISTS review_batch_measurements (
      id INTEGER PRIMARY KEY, job TEXT NOT NULL, session_id TEXT NOT NULL, value TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS review_batch_intervals (
      id INTEGER PRIMARY KEY, job TEXT NOT NULL, state TEXT NOT NULL, started REAL NOT NULL,
      ended REAL, verified_until REAL NOT NULL);
    ''')
    # 原序号仅在单会话内有意义。旧数据保留原 session_id 和序号。
    keys = [r[1] for r in c.execute('PRAGMA table_info(review_session_events)') if r[5]]
    if keys == ['job', 'seq']:
        c.executescript('''
        DROP INDEX IF EXISTS review_session_events_search;
        ALTER TABLE review_session_events RENAME TO review_session_events_legacy;
        CREATE TABLE review_session_events (
          job TEXT NOT NULL, session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL,
          type TEXT NOT NULL, status TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '',
          summary TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(job,session_id,seq), UNIQUE(job,event_id));
        INSERT INTO review_session_events SELECT * FROM review_session_events_legacy;
        DROP TABLE review_session_events_legacy;
        CREATE INDEX review_session_events_search ON review_session_events(job,session_id,type,seq);
        ''')
    c.executescript('''
    CREATE TRIGGER IF NOT EXISTS review_batch_interval_insert AFTER INSERT ON jobs WHEN NEW.batch_protocol=1 BEGIN
      INSERT INTO review_batch_intervals(job,state,started,verified_until) VALUES(NEW.id,NEW.status,NEW.created,NEW.created);
    END;
    CREATE TRIGGER IF NOT EXISTS review_batch_interval_state AFTER UPDATE OF status ON jobs
      WHEN NEW.batch_protocol=1 AND NEW.status!=OLD.status BEGIN
      UPDATE review_batch_intervals SET ended=NEW.updated,verified_until=NEW.updated WHERE job=NEW.id AND ended IS NULL;
      INSERT INTO review_batch_intervals(job,state,started,verified_until) VALUES(NEW.id,NEW.status,NEW.updated,NEW.updated);
    END;
    ''')


def get(store, job, private=False):
    with store.connect() as c:
        row = c.execute('SELECT * FROM review_batch_plans WHERE job=?', (job,)).fetchone()
        if not row:
            return None
        result = dict(row); result['value'] = json.loads(result['value'])
        units = [dict(r) for r in c.execute('SELECT * FROM review_batch_units WHERE job=? ORDER BY position', (job,))]
        attempts = [dict(r) for r in c.execute('SELECT unit,attempt,session_id,state,created,finished FROM review_batch_attempts WHERE job=? ORDER BY created', (job,))]
    for unit in units:
        unit['value'] = json.loads(unit['value'])
        unit['result'] = json.loads(unit['result']) if unit['result'] else None
        if not private:
            unit['input_tokens'] = unit['value'].get('input_tokens')
            unit['changed_lines'] = len(unit['value'].get('core', []))
            unit.pop('value')
    result['units'], result['attempts'] = units, attempts
    if not private:
        if result['value'].get('adjustments'):
            result['value']['input_tokens'] = sum(unit['input_tokens'] or 0 for unit in units
                                                  if unit['kind'] in ('primary', 'association') and unit['state'] != 'replaced')
        result['value'].pop('units', None)
        result['value'].pop('scopes', None)
        result['value']['config'].pop('api_base_url', None)
    return result


def cost_summaries(connection, jobs):
    """只返回成本字段；同一计划摘要在轮询时避免重复解析大型证据。"""
    if not jobs:
        return {}
    paths = ('$.primary', '$.association', '$.history', '$.base_calls', '$.max_calls',
             '$.input_tokens', '$.changed_lines', '$.config.model_max_output_tokens',
             '$.profile.basis', '$.profile.model', '$.profile.revision', '$.config.model')
    fields = ('primary', 'association', 'history', 'base_calls', 'max_calls', 'input_tokens',
              'changed_lines', 'output_tokens', 'basis', 'tokenizer_model', 'revision', 'model')
    placeholders = ','.join('?' for _ in jobs)
    paths_sql = ','.join('?' for _ in paths)
    rows = connection.execute(f'''SELECT job,hash,authorized_calls,budget_seconds
                                  FROM review_batch_plans WHERE job IN ({placeholders})''', jobs).fetchall()
    database = connection.execute('PRAGMA database_list').fetchone()[2]
    result = {}
    for row in rows:
        key = (database, row['job'], row['hash'])
        with _COST_SUMMARY_LOCK:
            cached = _COST_SUMMARY_CACHE.get(key)
            if cached is not None:
                _COST_SUMMARY_CACHE.move_to_end(key)
        if cached is None:
            detail = connection.execute(
                f'''SELECT json_extract(value,{paths_sql}) AS summary,
                           json_array_length(value,'$.gaps') AS gap_count,
                           json_array_length(value,'$.unsupported') AS unsupported_count,
                           json_array_length(value,'$.adjustments') AS adjustment_count
                    FROM review_batch_plans WHERE job=? AND hash=? AND json_valid(value)''',
                (*paths, row['job'], row['hash'])).fetchone()
            if not detail:
                continue
            values = json.loads(detail['summary'])
            if values[7] is None:
                values[7] = 0  # 兼容沿用默认输出额度的历史计划。
            if (any(type(value) is not int or value < 0 for value in values[:7])
                    or values[4] == 0 or type(values[7]) is not int or values[7] < 0):
                continue
            cached = dict(zip(fields, values))
            if detail['adjustment_count']:
                cached['input_tokens'] = connection.execute(
                    '''SELECT COALESCE(SUM(json_extract(value,'$.input_tokens')),0)
                       FROM review_batch_units WHERE job=? AND state!='replaced' AND kind IN ('primary','association')''',
                    (row['job'],)).fetchone()[0]
            cached.update(gap_count=detail['gap_count'] or 0, unsupported_count=detail['unsupported_count'] or 0)
            cached['output_tokens'] = cached['output_tokens'] or 8192
            with _COST_SUMMARY_LOCK:
                _COST_SUMMARY_CACHE[key] = cached
                _COST_SUMMARY_CACHE.move_to_end(key)
                if len(_COST_SUMMARY_CACHE) > 128:
                    _COST_SUMMARY_CACHE.popitem(last=False)
        summary = dict(cached)
        summary.update(plan_hash=row['hash'], authorized_calls=row['authorized_calls'],
                       budget_seconds=row['budget_seconds'])
        result[row['job']] = summary
    return result


def save(store, job, plan, config):
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        if c.execute('SELECT 1 FROM review_batch_plans WHERE job=?', (job['id'],)).fetchone():
            return
        authorized = plan['max_calls'] if plan['max_calls'] <= config.get('batch_auto_calls', 8) else 0
        c.execute('INSERT INTO review_batch_plans(job,hash,value,authorized_calls,budget_seconds) VALUES(?,?,?,?,?)',
                  (job['id'], plan['hash'], encode(plan), authorized, config.get('batch_execution_seconds', 3600)))
        for i, unit in enumerate(plan['units']):
            c.execute('INSERT INTO review_batch_units(job,id,position,kind,value) VALUES(?,?,?,?,?)',
                      (job['id'], unit['id'], i, unit['kind'], encode(unit)))
        for i, issue in enumerate(plan.get('history_ids', [])):
            unit = {'id': 'history-'+issue, 'issue': issue, 'kind': 'history', 'core': []}
            c.execute('INSERT INTO review_batch_units(job,id,position,kind,value) VALUES(?,?,?,?,?)',
                      (job['id'], unit['id'], len(plan['units'])+i, unit['kind'], encode(unit)))


def transition(store, job, state, reason):
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        c.execute('DELETE FROM capabilities WHERE job=?', (job['id'],))
        c.execute('UPDATE review_native_bindings SET active=0 WHERE job=?', (job['id'],))
        c.execute('UPDATE jobs SET status=?,stage=?,error=?,owner=\'\',lease=0,available=?,updated=?,attempts=0 WHERE id=?',
                  (state, reason, reason if state == 'incomplete' else '', time.time(), time.time(), job['id']))


def save_refutations(store, job, plan, units):
    """只在所有主审完成后一次性安装反证阶段；恢复不重新生成或调用模型。"""
    from .common import digest
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        row = c.execute('SELECT value FROM review_batch_plans WHERE job=?', (job['id'],)).fetchone()
        current = json.loads(row[0])
        if current.get('refutation_planned'):
            return
        if current['hash'] != plan['hash']:
            raise Stale('生成反证分组期间计划已经变化')
        position = c.execute('SELECT COALESCE(max(position),-1)+1 FROM review_batch_units WHERE job=?', (job['id'],)).fetchone()[0]
        for i, unit in enumerate(units):
            c.execute('INSERT INTO review_batch_units(job,id,position,kind,value) VALUES(?,?,?,?,?)',
                      (job['id'], unit['id'], position + i, 'refutation', encode(unit)))
        current['refutation_planned'] = True
        current['refutation_groups'] = len(units)
        # 真实分组超出预留时扩大可见计划并重新取得授权；不使用目标次数截断。
        current['max_calls'] = max(current['max_calls'], 2 * (current['base_calls'] - current['history'] + len(units)) + current['history'])
        current['hash'] = digest({k: v for k, v in current.items() if k != 'hash'})
        c.execute('UPDATE review_batch_plans SET value=?,hash=? WHERE job=?',
                  (encode(current), current['hash'], job['id']))


def control(store, job_id, data, actor, confirm=False):
    with store.tx() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        plan = c.execute('SELECT * FROM review_batch_plans WHERE job=?', (job_id,)).fetchone()
        if not row or not plan or data.get('plan_hash') != plan['hash']:
            raise ReviewError('计划不存在或摘要已经变化，请刷新')
        current = c.execute('SELECT generation FROM pr_state WHERE repository=? AND number=?', (row['repository'], row['number'])).fetchone()
        config = store.job_config(job_id, c)
        repo = store.repo(row['repository'], c)
        if not current or current[0] != row['generation'] or not repo['enabled'] or repo['revision'] != row['repo_revision'] or not store.config(connection=c)['enabled']:
            raise Stale('计划版本或权限已经过期')
        if confirm:
            if plan['approved_at'] and plan['authorized_calls'] >= json.loads(plan['value'])['max_calls']:
                return  # 同一摘要重复确认不改变运行状态或追加额度。
            if row['status'] not in ('awaiting_cost', 'queued'):
                raise ReviewError('当前任务不处于等待成本确认状态')
            maximum = json.loads(plan['value'])['max_calls']
            c.execute('UPDATE review_batch_plans SET authorized_calls=?,approved_by=?,approved_at=? WHERE job=?',
                      (maximum, actor, time.time(), job_id))
        else:
            if row['status'] not in WAITING:
                raise ReviewError('当前任务不能继续')
            extra = data.get('extra_seconds', 0)
            if type(extra) is not int or not 0 <= extra <= 604800:
                raise ReviewError('追加预算须为 0 至 604800 秒')
            c.execute('UPDATE review_batch_plans SET budget_seconds=budget_seconds+? WHERE job=?', (extra, job_id))
            retry = data.get('retry_unit')
            if retry:
                unit = c.execute('SELECT * FROM review_batch_units WHERE job=? AND id=?', (job_id, retry)).fetchone()
                attempt = c.execute('SELECT * FROM review_batch_attempts WHERE job=? AND unit=? ORDER BY attempt DESC LIMIT 1', (job_id, retry)).fetchone()
                if not unit or unit['state'] != 'failed' or not attempt or attempt['state'] != 'failed':
                    raise ReviewError('仅允许重试已确认失败的单元；未知结果须先核对原会话')
                value = json.loads(plan['value']); value['max_calls'] += 1
                # 重试额度显式增加，不能借继续按钮绕过新的成本确认。
                from .common import digest
                value['hash'] = digest({k: v for k, v in value.items() if k != 'hash'})
                c.execute('UPDATE review_batch_plans SET value=?,hash=? WHERE job=?', (encode(value), value['hash'], job_id))
                c.execute("UPDATE review_batch_units SET state='pending',error='' WHERE job=? AND id=?", (job_id, retry))
        status = 'queued'
        latest = c.execute('SELECT * FROM review_batch_plans WHERE job=?', (job_id,)).fetchone()
        if latest['authorized_calls'] < json.loads(latest['value'])['max_calls']:
            status = 'awaiting_cost'
        c.execute("UPDATE jobs SET status=?,stage='等待继续审查',error='',available=?,updated=?,owner='',lease=0 WHERE id=?",
                  (status, time.time(), time.time(), job_id))


def reset_session(c, job):
    strings = {'session_id', 'session_status', 'current_action', 'event_token_hash', 'native_result_sha256', 'workspace_path'}
    jsons = {'session_counters', 'diagnostic', 'raw_diagnostics'}
    nulls = {'deadline_at', 'last_heartbeat', 'log_synced_at', 'final_event_seq', 'session_submitted_at'}
    values = ['' if k in strings else '{}' if k in jsons else None if k in nulls else 0 for k in SESSION_FIELDS]
    c.execute('UPDATE jobs SET ' + ','.join(k+'=?' for k in SESSION_FIELDS) + ' WHERE id=?', values+[job])


def timing(store, job):
    totals = {'execution_seconds': 0, 'queue_seconds': 0, 'paused_seconds': 0, 'uncertain_seconds': 0}
    now = time.time()
    with store.connect() as c:
        rows = c.execute('SELECT * FROM review_batch_intervals WHERE job=?', (job,)).fetchall()
        task = c.execute('SELECT created,finished_at FROM jobs WHERE id=?', (job,)).fetchone()
    for r in rows:
        end = r['ended'] or now
        category = 'execution_seconds' if r['state'] in ('running', 'publishing', 'recovering') else 'queue_seconds' if r['state'] == 'queued' else 'paused_seconds' if r['state'] in WAITING else 'uncertain_seconds' if r['state'] == 'unknown' else None
        if category:
            totals[category] += max(0, end-r['started'])
    totals['total_seconds'] = max(0, (task['finished_at'] or now)-task['created']) if task else None
    return totals


def adjust_context(store, job, record, diagnostic, config, snapshot):
    """仅处理服务明确的上下文拒绝，保留失败尝试，每个来源最多调整一次。"""
    if diagnostic.get('upstream_error_code') not in ('context_length_exceeded', 'context_window_exceeded'):
        return False
    unit = record['value']
    if unit.get('flow_version') == 'diff-batches-v3':
        # 新版关系图包含完整保护上下文。供应商拒绝已计数的请求时，不能沿用旧版
        # 中点裁剪偷偷改变关系范围；保留失败以便核对实际模型窗口后显式重建任务。
        return False
    if unit['kind'] != 'primary' or unit.get('context_adjusted') or len(unit['evidence']) < 2:
        return False
    from .batch_plan import messages
    from .common import digest
    evidence = unit['evidence']; middle = len(evidence)//2
    parts = []
    for start, end in ((0, middle), (middle, len(evidence))):
        core = [r['id'] for r in evidence[start:end] if r['id'] in set(unit['core'])]
        if not core:
            continue
        part = dict(unit, id=digest([unit['id'], start, end])[:24], context_adjusted=True,
                    evidence=evidence[max(0,start-20):min(len(evidence),end+20)], core=core)
        part['input_tokens'] = token_budget.count(messages(snapshot, part), config)
        if part['input_tokens'] >= unit['input_tokens']:
            return False
        parts.append(part)
    if len(parts) != 2:
        return False
    boundary = dict(unit, id=digest([unit['id'], 'boundary'])[:24], kind='association',
                    context_adjusted=True, evidence=evidence[max(0,middle-20):middle+20], core=[])
    boundary['input_tokens'] = token_budget.count(messages(snapshot, boundary), config)
    parts.append(boundary)
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        saved = c.execute('SELECT value FROM review_batch_plans WHERE job=?', (job['id'],)).fetchone()
        plan = json.loads(saved[0])
        plan['units'] = [u for u in plan['units'] if u['id'] != unit['id']] + parts
        plan['input_tokens'] = sum(u['input_tokens'] for u in plan['units'])
        plan.setdefault('adjustments', []).append({'unit': unit['id'], 'reason': '服务明确拒绝上下文长度'})
        plan['primary'] += 1; plan['association'] += 1
        # 新协议每个审查及反证单元均最多一次引用纠正；旧计划保持原额度算法。
        from .batch_references import PROTOCOL
        plan['base_calls'] += 3
        plan['max_calls'] += 11 if unit.get('reference_protocol') == PROTOCOL else 5
        plan['hash'] = digest({k: v for k, v in plan.items() if k != 'hash'})
        authorization = plan['max_calls'] if plan['max_calls'] <= config.get('batch_auto_calls', 8) else 0
        c.execute('UPDATE review_batch_plans SET hash=?,value=?,authorized_calls=? WHERE job=?',
                  (plan['hash'], encode(plan), authorization, job['id']))
        c.execute("UPDATE review_batch_units SET state='replaced' WHERE job=? AND id=?", (job['id'], unit['id']))
        position = c.execute('SELECT max(position)+1 FROM review_batch_units WHERE job=?', (job['id'],)).fetchone()[0]
        for i, part in enumerate(parts):
            c.execute('INSERT INTO review_batch_units(job,id,position,kind,value) VALUES(?,?,?,?,?)',
                      (job['id'], part['id'], position+i, part['kind'], encode(part)))
    return True


def adjust_output(store, job, record, diagnostic, config, snapshot):
    """已确认输出用尽时拆分关系或候选，保留失败记录，最多递归两层。"""
    unit = record['value']
    if (unit.get('review_mode') == 'single' or job.get('batch_flow_version', 2) < 4 or unit.get('flow_version') != 'diff-batches-v3'
            or unit.get('output_split_depth', 0) >= 2):
        return False
    from . import batch_flow
    from .common import digest
    with store.connect() as c:
        row = c.execute('SELECT value FROM review_batch_plans WHERE job=?', (job['id'],)).fetchone()
        previous = c.execute('SELECT state,session,raw,session_id FROM review_batch_attempts WHERE job=? AND unit=? ORDER BY attempt DESC LIMIT 1',
                             (job['id'], unit['id'])).fetchone()
    if not row:
        return False
    def exhausted(attempt):
        if not attempt or attempt['state'] != 'failed':
            return False
        session = json.loads(attempt['session'])
        saved = session.get('diagnostic') or {}
        if isinstance(saved, str):
            saved = json.loads(saved)
        raw = json.loads(attempt['raw']) if attempt['raw'] else {}
        failure = session.get('failure_diagnostic') or {}
        # 兼容修复前已保存的失败；仅接受与该次尝试会话严格绑定的任务诊断。
        bound = diagnostic if diagnostic.get('session_id') == attempt['session_id'] else {}
        return any(value.get('finish_reason') == 'length' for value in
                   (saved, failure, bound, raw.get('_review_completion', {})))
    if not exhausted(previous):
        return False
    plan = json.loads(row[0])
    if unit['kind'] == 'association':
        connections = unit.get('connections', [])
        if len(connections) < 2:
            return False
        groups = OrderedDict()
        for relation in connections:
            groups.setdefault(tuple(sorted((relation['left'], relation['right']))), []).append(relation)
        parts = list(groups.values())
        if len(parts) == 1:
            middle = len(connections) // 2
            parts = [connections[:middle], connections[middle:]]
        children = []
        for relations in parts:
            evidence = [r for relation in relations for key in (relation['left'], relation['right'])
                        for r in plan['scopes'][key]['evidence']]
            children.append(batch_flow.make_unit(snapshot, 'association', evidence, connections=relations))
    elif unit['kind'] == 'refutation' and len(unit.get('candidates', [])) > 1:
        middle = len(unit['candidates']) // 2
        children = [batch_flow.make_unit(snapshot, 'refutation', unit['evidence'], candidates=items)
                    for items in (unit['candidates'][:middle], unit['candidates'][middle:])]
    else:
        return False
    for index, child in enumerate(children):
        child['output_split_depth'] = unit.get('output_split_depth', 0) + 1
        child['output_parent'] = unit['id']
        child['id'] = digest([unit['id'], index, child['id']])[:24]
        child['input_tokens'] = batch_flow.count(snapshot, child, config, plan['profile'])
        if child['input_tokens'] > token_budget.capacity(config):
            return False
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        state_row = c.execute('SELECT state FROM review_batch_units WHERE job=? AND id=?', (job['id'], unit['id'])).fetchone()
        attempt = c.execute('SELECT state,session,raw,session_id FROM review_batch_attempts WHERE job=? AND unit=? ORDER BY attempt DESC LIMIT 1',
                            (job['id'], unit['id'])).fetchone()
        if not state_row or state_row['state'] != 'failed' or not exhausted(attempt):
            return False
        current = json.loads(c.execute('SELECT value FROM review_batch_plans WHERE job=?', (job['id'],)).fetchone()[0])
        if current['hash'] != plan['hash']:
            raise Stale('拆分期间计划已变更')
        position = c.execute('SELECT COALESCE(max(position),-1)+1 FROM review_batch_units WHERE job=?', (job['id'],)).fetchone()[0]
        c.execute("UPDATE review_batch_units SET state='replaced' WHERE job=? AND id=?", (job['id'], unit['id']))
        for index, child in enumerate(children):
            c.execute('INSERT INTO review_batch_units(job,id,position,kind,value) VALUES(?,?,?,?,?)',
                      (job['id'], child['id'], position + index, child['kind'], encode(child)))
        if unit['kind'] == 'association':
            current['units'] = [u for u in current['units'] if u['id'] != unit['id']] + children
            current['association'] += len(children) - 1
            current['base_calls'] += len(children) - 1
            current['input_tokens'] = sum(u['input_tokens'] for u in current['units'])
        else:
            current['refutation_groups'] = current.get('refutation_groups', 1) + len(children) - 1
        current.setdefault('output_adjustments', []).append({'unit': unit['id'], 'reason': '输出额度耗尽',
                                                            'children': [u['id'] for u in children]})
        current['max_calls'] += 2 * len(children)
        current['hash'] = digest({k: v for k, v in current.items() if k != 'hash'})
        c.execute('UPDATE review_batch_plans SET hash=?,value=? WHERE job=?', (current['hash'], encode(current), job['id']))
        c.execute("UPDATE jobs SET diagnostic='{}' WHERE id=?", (job['id'],))
    return True
