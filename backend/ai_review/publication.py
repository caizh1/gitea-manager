"""固定发布计划、自动写入与逐项回执恢复。"""
import contextlib
from copy import deepcopy
import json
import threading
import time

from .common import ReviewError, PublishUnknown, WriteRejected, encode


def migrate(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS review_codex_usage (
      call_id TEXT PRIMARY KEY, job TEXT NOT NULL, input_tokens INTEGER NOT NULL,
      output_tokens INTEGER NOT NULL, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS codex_review_phases (
      job TEXT NOT NULL, phase TEXT NOT NULL, session_id TEXT NOT NULL, result TEXT NOT NULL,
      created REAL NOT NULL, PRIMARY KEY(job,phase));
    CREATE TABLE IF NOT EXISTS review_publication_approvals (
      job TEXT PRIMARY KEY, preview_hash TEXT NOT NULL, selected TEXT NOT NULL,
      approved_by TEXT NOT NULL, approved_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS review_publication_plans (
      job TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS review_publication_alerts (
      job TEXT PRIMARY KEY, message TEXT NOT NULL, created REAL NOT NULL);
    """)
    # 保留历史确认记录；待发布任务由新 Worker 校验版本后自动接续。
    c.execute("""UPDATE jobs SET status='queued',available=0,stage='等待自动发布',owner='',lease=0
                 WHERE status='awaiting_publication'""")
    c.execute("UPDATE jobs SET reconcile_state='checking' WHERE status='publish_unknown' AND reconcile_state='needs_confirmation'")


def observations(store, job):
    with contextlib.closing(store.connect()) as c:
        rows = c.execute("""SELECT o.issue,o.value,i.finding FROM review_issue_observations o
            JOIN review_issues i ON i.id=o.issue WHERE o.job=? ORDER BY o.issue""", (job['id'],)).fetchall()
    return [dict(issue=row['issue'], title=json.loads(row['finding']).get('title', ''),
                 **json.loads(row['value'])) for row in rows]


def overview(store, job, result):
    from .progress import SUMMARY_MARK, review_scope_note
    from .issue_lifecycle import LABELS
    from .publish import prose
    body = SUMMARY_MARK + '\n<!-- ai-review:auto-summary:' + job['id'] + ' -->'
    body += '\n### AI Code Review\n\n审查版本 `' + job['snapshot']['head'] + '`。'
    body += '\n\n' + (result.get('report_markdown', '').splitlines() or [''])[0]
    engine = job['snapshot'].get('engine') or (job.get('configuration') or {}).get('engine')
    note = review_scope_note(engine, result.get('review_scope'), job['snapshot'].get('pr_agent_protocol'))
    if note:
        body += '\n\n' + note
    frozen_ids = result.get('_approved_issue_ids')
    for row in observations(store, job):
        if frozen_ids is not None and row['issue'] not in frozen_ids:
            continue
        body += '\n\n- `' + row['issue'][:12] + '` **' + LABELS[row['status']] + '** ' + prose(row['title'])[:300]
        body += '\n  ' + prose(row['reason'])[:600]
    fallback = [f for f in result['findings'] if not f.get('anchor') or len(f.get('comment_markdown', '') + f.get('explanation', '')) > 9000]
    for finding in fallback:
        body += '\n\n- **' + prose(finding.get('severity', '')) + ' ' + prose(finding['title'])[:300] + '**'
        body += '\n  ' + prose(finding.get('explanation') or finding.get('comment_markdown', ''))[:600]
        if finding.get('fallback_reason'):
            body += '\n  ' + prose(finding['fallback_reason'])
    callback = store.repo(job['repository']).get('webhook_url', '')
    base = callback.split('/api/ai-review/webhooks/')[0] if '/api/ai-review/webhooks/' in callback else ''
    link = ('[查看完整分析与证据](' + base + '/ai-reviews?task=' + job['id'] + ')'
            if base else '完整分析与证据见 Manager 任务 `' + job['id'] + '`。')
    return body[:11000] + '\n\n' + link


def plan_summary(store, client, job, repo, body):
    from .issue_publication import operation
    from .progress import ProgressSync
    with contextlib.closing(store.connect()) as c:
        old = c.execute('SELECT summary_id,progress_creating FROM pr_state WHERE repository=? AND number=?',
                        (repo['id'], job['number'])).fetchone()
        managed = c.execute('SELECT body,pending FROM review_issue_summaries WHERE repository=? AND number=?',
                            (repo['id'], job['number'])).fetchone()
    rows = client.pages(client.prefix + f"/issues/{job['number']}/comments")
    sync = ProgressSync(store)
    owned = [row for row in rows if sync.owned(row, repo) and sync.belongs(row, repo, job['number'])]
    target = next((row for row in owned if old and row['id'] == old['summary_id']), None)
    if old and old['progress_creating']:
        # 进度写入和正式总览共用一条评论。先取得原写入的回执，再冻结更新目标。
        confirmed = [row for row in owned if managed and row['body'] == managed['pending']
                     and (not old['summary_id'] or row['id'] == old['summary_id'])]
        if len(confirmed) != 1:
            raise PublishUnknown('进度评论的写入回执尚未确认，稍后自动核对')
        target = confirmed[0]
    elif target is None and len(owned) == 1:
        target = owned[0]
    if (target and managed and managed['pending'] and target['body'] == managed['body']
            and target['body'] != managed['pending']):
        raise PublishUnknown('进度评论的更新回执尚未确认，稍后自动核对')
    editable = target and managed and target['body'] in (managed['body'], managed['pending'])
    payload = {'body': body, 'comment_id': target['id'] if editable else None,
               'before': target['body'] if editable else None, 'previous_id': old['summary_id'] if old else None}
    return operation(store, job, 'summary', 'summary', payload)


def prepare(store, client, job, repo):
    from .publish import Publisher
    from .issue_publication import plan_new, plan_edits, operation
    with contextlib.closing(store.connect()) as c:
        saved = c.execute('SELECT value FROM review_publication_plans WHERE job=?', (job['id'],)).fetchone()
    if saved:
        return json.loads(saved['value'])
    result = deepcopy(job['result'].get('publication') or job['result'])
    if job['snapshot'].get('engine') in ('codex_cli', 'pr_agent') and job['result'].get('analysis_complete') is not True:
        raise ReviewError('审查尚未完整结束，无法自动发布')
    result.pop('publication', None)
    if not job['result'].get('publication'):
        result['_automatic_publication'] = True
        for finding in result.get('findings', []):
            if (finding.get('anchor') or {}).get('gitea_legacy_inline') is False:
                finding['anchor'] = None
                finding['fallback_reason'] = '该位置的 Gitea 行评补丁包含旧编码字节，意见已放入总览。'
    publisher = Publisher(store, client, job, repo, job['snapshot'], result)
    if result.get('_automatic_publication'):
        publisher.page_size = 1
    publisher.validate_local()
    # 旧任务已冻结的选择只影响其原发布载荷，新任务发布所有成立意见。
    plan_new(publisher)
    plan_edits(publisher)
    with contextlib.closing(store.connect()) as c:
        waiting = [row for row in observations(store, job) if not c.execute(
            'SELECT 1 FROM review_issue_comments WHERE issue=?', (row['issue'],)).fetchone()
            and c.execute("""SELECT 1 FROM review_issue_operations o,json_each(o.payload,'$.comments') f
                WHERE o.job!=? AND o.kind='create' AND o.state IN ('started','unknown')
                  AND json_extract(f.value,'$.issue')=?""", (job['id'], row['issue'])).fetchone()]
    for row in waiting:
        operation(store, job, 'link-' + row['issue'], 'link', row)
    value = {'result': result, 'overview': overview(store, job, result)}
    plan_summary(store, client, job, repo, value['overview'])
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        c.execute('INSERT OR IGNORE INTO review_publication_plans VALUES (?,?,?)', (job['id'], encode(value), time.time()))
    store.update(job['id'], job['owner'], result=dict(job['result'], publication=result))
    return value


def resolve_links(store, job):
    """旧版本回执出现后，用本任务已经冻结的状态更新该条讨论。"""
    from .issue_publication import operation, change_operation, status_block
    with contextlib.closing(store.connect()) as c:
        pending = c.execute("SELECT * FROM review_issue_operations WHERE job=? AND kind='link' AND state='queued'",
                            (job['id'],)).fetchall()
    for row in pending:
        desired = json.loads(row['payload'])
        with contextlib.closing(store.connect()) as c:
            comment = c.execute('SELECT * FROM review_issue_comments WHERE issue=?', (desired['issue'],)).fetchone()
        if not comment:
            continue
        body = comment['original_body'] + status_block(desired['issue'], desired['status'], desired['reason'],
            job['snapshot'], job['snapshot'].get('anchors', {}).get(desired.get('anchor_id')))
        operation(store, job, 'edit-' + str(comment['comment_id']), 'edit',
                  {'comment_id': comment['comment_id'], 'review_id': comment['review_id'],
                   'before': comment['last_body'], 'body': body})
        change_operation(store, job, row['key'], 'done', {'comment_id': comment['comment_id']})


def publish_summary(store, client, job, repo):
    from .issue_publication import change_operation
    from .gitea import assert_version
    from .progress import ProgressSync
    with contextlib.closing(store.connect()) as c:
        op = dict(c.execute("SELECT * FROM review_issue_operations WHERE job=? AND key='summary'", (job['id'],)).fetchone())
    payload = json.loads(op['payload'])
    rows = client.pages(client.prefix + f"/issues/{job['number']}/comments")
    sync = ProgressSync(store)
    matches = [r for r in rows if r.get('body') == payload['body'] and sync.owned(r, repo) and sync.belongs(r, repo, job['number'])]
    if len(matches) > 1:
        raise PublishUnknown('总览回执出现多个匹配，继续自动核对')
    if matches:
        actual = matches[0]
    else:
        if op['state'] != 'queued':
            raise PublishUnknown('总览写入回执尚未出现，继续自动核对')
        with contextlib.closing(store.connect()) as c:
            older = c.execute("""SELECT o.* FROM review_issue_operations o JOIN jobs j ON j.id=o.job
                WHERE j.repository=? AND j.number=? AND o.job!=? AND o.kind='summary'
                  AND o.state IN ('started','unknown')""", (repo['id'], job['number'], job['id'])).fetchall()
        for previous in older:
            expected = json.loads(previous['payload'])['body']
            confirmed = [row for row in rows if row.get('body') == expected and sync.owned(row, repo)
                         and sync.belongs(row, repo, job['number'])]
            if len(confirmed) != 1:
                raise PublishUnknown('旧版本总览写入尚未取得回执，当前总览等待自动核对')
            change_operation(store, {'id': previous['job']}, previous['key'], 'done', {'comment_id': confirmed[0]['id']})
        target = next((r for r in rows if r['id'] == payload['comment_id']), None)
        if payload['comment_id'] and (not target or target.get('body') != payload['before'] or not sync.owned(target, repo)):
            payload['comment_id'] = None
            with store.tx() as c:
                c.execute("UPDATE review_issue_operations SET payload=? WHERE job=? AND key='summary'", (encode(payload), job['id']))
        assert_version(client, job['number'], job['snapshot'])
        change_operation(store, job, 'summary', 'started')
        path = client.prefix + ('/issues/comments/' + str(payload['comment_id']) if payload['comment_id'] else f"/issues/{job['number']}/comments")
        try:
            client.request('PATCH' if payload['comment_id'] else 'POST', path, {'body': payload['body']})
        except WriteRejected as exc:
            change_operation(store, job, 'summary', 'queued', error=str(exc))
            raise
        rows = client.pages(client.prefix + f"/issues/{job['number']}/comments")
        matches = [r for r in rows if r.get('body') == payload['body'] and sync.owned(r, repo) and sync.belongs(r, repo, job['number'])]
        if len(matches) != 1:
            raise PublishUnknown('总览写入后尚无唯一回执')
        actual = matches[0]
    change_operation(store, job, 'summary', 'done', {'comment_id': actual['id'], 'previous_id': payload.get('previous_id')})
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        c.execute('INSERT INTO review_issue_summaries VALUES (?,?,?,NULL) ON CONFLICT(repository,number) DO UPDATE SET body=excluded.body,pending=NULL',
                  (repo['id'], job['number'], payload['body']))
        c.execute('UPDATE pr_state SET summary_id=?,progress_synced=progress_seq,progress_creating=0 WHERE repository=? AND number=?',
                  (actual['id'], repo['id'], job['number']))


def publish(store, client, job, repo):
    from .gitea import assert_version
    from .authorization import authorize
    from .publish import Publisher
    from .issue_publication import publish_new, sync_comments
    with (
        store.lock(f"进度-{job['repository']}-{job['number']}", blocking=True),
        store.lock(f"发布-{job['repository']}-{job['number']}"),
    ):
        store.active(job['id'], job['owner'])
        authorize(repo, job, 'write')
        assert_version(client, job['number'], job['snapshot'])
        if job['snapshot'].get('engine') in ('codex_cli', 'pr_agent') and job['result'].get('analysis_complete') is not True:
            raise ReviewError('审查尚未完整结束，无法自动发布')
        store.update(job['id'], job['owner'], status='publishing', stage='自动发布审查结果')
        plan = prepare(store, client, job, repo)
        publisher = Publisher(store, client, job, repo, job['snapshot'], plan['result'])
        if plan['result'].get('_automatic_publication'):
            publisher.page_size = 1
        try:
            publish_new(publisher, recovery=False)
        except PublishUnknown:
            pass
        resolve_links(store, job)
        sync_comments(publisher)
        publish_summary(store, client, job, repo)
        with contextlib.closing(store.connect()) as c:
            pending = c.execute("SELECT 1 FROM review_issue_operations WHERE job=? AND state IN ('queued','started','unknown') LIMIT 1", (job['id'],)).fetchone()
        if pending:
            raise PublishUnknown('部分状态写入回执尚未齐全，继续自动核对')
        assert_version(client, job['number'], job['snapshot'])
        store.finish(job['id'], job['owner'], 'succeeded')


def status(store, job):
    with contextlib.closing(store.connect()) as c:
        rows = c.execute('SELECT kind,state,error,receipt FROM review_issue_operations WHERE job=?', (job['id'],)).fetchall()
        alert = c.execute('SELECT message FROM review_publication_alerts WHERE job=?', (job['id'],)).fetchone()
    return {'automatic': True, 'completed_operations': sum(r['state'] in ('done', 'conflict') for r in rows),
            'total_operations': len(rows), 'comments_published': sum(len(json.loads(r['receipt'] or '{}').get('comment_ids', [])) for r in rows),
            'errors': [r['error'] for r in rows if r['error']], 'alert': alert['message'] if alert and job['status'] == 'publish_unknown' else '',
            'next_retry_at': job['available'] if job['status'] == 'publish_unknown' else None}


def recover_obsolete(store, client, job, repo):
    """过期任务仅接回已发送操作的回执，未发送操作终止。"""
    from .issue_publication import recover, change_operation
    from .publish import Publisher
    result = job['result'].get('publication') or job['result']
    publisher = Publisher(store, client, job, repo, job['snapshot'], result)
    with contextlib.closing(store.connect()) as c:
        legacy = c.execute('SELECT body,pending FROM review_issue_summaries WHERE repository=? AND number=?',
                           (repo['id'], job['number'])).fetchone()
        planned = c.execute("SELECT 1 FROM review_issue_operations WHERE job=? AND kind='summary'", (job['id'],)).fetchone()
    if legacy and legacy['pending'] and not planned:
        from .progress import ProgressSync
        sync = ProgressSync(store)
        matches = [row for row in client.pages(client.prefix + f"/issues/{job['number']}/comments")
                   if row.get('body') == legacy['pending'] and sync.owned(row, repo) and sync.belongs(row, repo, job['number'])]
        if len(matches) != 1:
            raise PublishUnknown('历史总览写入仍在自动核对')
        with store.tx() as c:
            c.execute('UPDATE review_issue_summaries SET body=pending,pending=NULL WHERE repository=? AND number=?',
                      (repo['id'], job['number']))
            c.execute('UPDATE pr_state SET summary_id=?,progress_creating=0 WHERE repository=? AND number=?',
                      (matches[0]['id'], repo['id'], job['number']))
    with store.tx() as c:
        c.execute("UPDATE review_issue_operations SET state='abandoned' WHERE job=? AND state='queued'", (job['id'],))
        rows = [dict(row) for row in c.execute("SELECT * FROM review_issue_operations WHERE job=? AND state IN ('started','unknown')", (job['id'],))]
    if any(row['kind'] == 'create' for row in rows):
        recover(publisher)
    for row in rows:
        payload = json.loads(row['payload'])
        if row['kind'] == 'create':
            continue
        path = (publisher.path + '/' + str(payload['review_id']) + '/comments' if row['kind'] == 'edit'
                else client.prefix + f"/issues/{job['number']}/comments")
        matches = [item for item in client.pages(path) if item.get('body') == payload['body'] and item.get('user', {}).get('id') == repo['bot_id']]
        if len(matches) != 1:
            raise PublishUnknown('旧版本写入尚未获得唯一回执，继续自动核对')
        change_operation(store, job, row['key'], 'done', {'comment_id': matches[0]['id']})
    with store.tx() as c:
        c.execute("UPDATE jobs SET status='superseded',stage='旧版本发布回执已核对',lease=0 WHERE id=? AND status='recovering'", (job['id'],))


def resume(store, job):
    # 发布恢复与普通执行使用同一任务锁，回收器必须等待此调用退出。
    with store.lock('execution-' + job['id']):
        return _resume(store, job)


def _resume(store, job):
    import secrets
    from .common import Stale, AccessRevoked
    from .worker import client_for, refresh_pr
    now = time.time()
    owner = secrets.token_hex(16)
    with store.tx() as c:
        from .concurrency import reserve
        current = c.execute("SELECT status,available FROM jobs WHERE id=?", (job['id'],)).fetchone()
        if not current or current['status'] != 'publish_unknown' or current['available'] > now:
            return
        if not reserve(store, c, job['id'], owner):
            return
        if not c.execute("""UPDATE jobs SET status='recovering',owner=?,lease=?,available=?,
            reconcile_state='checking',reconcile_started_at=?,reconcile_message='后台自动核对发布回执'
            WHERE id=? AND status='publish_unknown' AND available<=?""",
            (owner, now+90, now+60, now, job['id'], now)).rowcount:
            return
    job = store.job(job['id'])
    client = None
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(10):
            store.heartbeat(job['id'], owner)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        repo = store.repo(job['repository'])
        try:
            store.active(job['id'], owner)
        except Stale:
            client = client_for(store, repo)
            with (
                store.lock(f"进度-{job['repository']}-{job['number']}", blocking=True),
                store.lock(f"发布-{job['repository']}-{job['number']}"),
            ):
                recover_obsolete(store, client, job, repo)
            return
        client = client_for(store, repo, job)
        publish(store, client, job, repo)
        refresh_pr(store, repo, job)
    except Stale:
        with store.tx() as c:
            c.execute("UPDATE jobs SET status='publish_unknown',available=?,owner='',lease=0 WHERE id=? AND owner=?", (now+60,job['id'],owner))
        if 'repo' in locals():
            refresh_pr(store, repo, job)
    except Exception as exc:
        permanent = isinstance(exc, AccessRevoked) or isinstance(exc, WriteRejected) and not getattr(exc, 'retryable', False)
        with store.tx() as c:
            c.execute("""UPDATE jobs SET status=?,stage=?,error=?,available=?,owner='',lease=0,
                reconcile_state=?,reconcile_finished_at=?,reconcile_message=? WHERE id=? AND owner=?""",
                ('failed' if permanent else 'publish_unknown', '发布失败' if permanent else '自动核对发布结果',
                 str(exc)[:1000], now+60, 'failed' if permanent else 'checking', now, str(exc)[:1000], job['id'],owner))
            plan = c.execute('SELECT created FROM review_publication_plans WHERE job=?', (job['id'],)).fetchone()
            since = plan['created'] if plan else job['created']
            if not permanent and now-since >= 1800:
                c.execute('INSERT OR IGNORE INTO review_publication_alerts VALUES (?,?,?)',
                          (job['id'], '发布结果自动核对已持续超过 30 分钟', now))
    finally:
        stop.set()
        thread.join(timeout=2)
        if client:
            client.close()
