"""台账发布：仅新问题创建 Review，状态使用原评论；写入计划先持久化。"""
import contextlib
import json
import time

from .common import ReviewError, Stale, AccessRevoked, PublishUnknown, encode
from .gitea import assert_version
from .issue_lifecycle import LABELS, issue_marker, snapshot_for
from .publish import finding_body, marker, prose, verify_hunk


def status_block(ident, status, reason, snapshot, anchor=None):
    text = '\n\n<!-- ai-review:status:' + ident + ':begin -->\n'
    text += '**问题状态：' + LABELS[status] + '**\n\n' + prose(reason)
    text += '\n\n依据提交：`' + snapshot['head'] + '`；基线：`' + snapshot['base'] + '`；比较点：`' + snapshot['merge_base'] + '`。'
    if anchor:
        text += '\n\n本轮定位：' + prose(anchor['path']) + f" · {'新' if anchor['side'] == 'new' else '旧'}侧第 {anchor['line']} 行。原讨论仍保留首次发布位置。"
    text += '\n\n这是上述版本的判断；后续提交请以固定总览的复核状态为准。'
    return text + '\n<!-- ai-review:status:' + ident + ':end -->'


def operation(store, job, key, kind, payload):
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        c.execute('INSERT OR IGNORE INTO review_issue_operations(job,key,kind,payload) VALUES (?,?,?,?)',
                  (job['id'], key, kind, encode(payload)))
        row = dict(c.execute('SELECT * FROM review_issue_operations WHERE job=? AND key=?', (job['id'], key)).fetchone())
    row['payload'] = json.loads(row['payload'])
    return row


def change_operation(store, job, key, state, receipt=None, error=''):
    # 写入回执即使在版本切换后也必须保留，但不能推进新代台账。
    with store.tx() as c:
        c.execute('UPDATE review_issue_operations SET state=?,receipt=?,error=? WHERE job=? AND key=?',
                  (state, encode(receipt) if receipt is not None else None, error, job['id'], key))


def check_review(publisher, review, payload):
    if (review.get('user', {}).get('id') != publisher.repo['bot_id'] or review.get('state') != 'COMMENT' or
            review.get('body') != payload['body'] or review.get('commit_id') != publisher.snapshot['head']):
        raise PublishUnknown('新增问题 Review 身份、版本或正文不一致')
    actual = publisher.comments(review)
    by_body = {r['body']: r for r in actual}
    if len(actual) != len(payload['comments']) or len(by_body) != len(actual):
        raise PublishUnknown('新增问题评论集合不完整或重复')
    for expected in payload['comments']:
        row = by_body.get(expected['body'])
        if not row or row.get('user', {}).get('id') != publisher.repo['bot_id']:
            raise PublishUnknown('新增问题评论内容或作者不符')
        verify_hunk(row, expected['anchor'], publisher.snapshot)
    return by_body


def publish_new(publisher, recovery):
    store, job, snapshot = publisher.store, publisher.job, publisher.snapshot
    result = publisher.full_result
    new_ids = set(result['lifecycle'].get('publish_ids', result['lifecycle']['new_ids']))
    findings = list({f['issue_id']: f for f in result['findings']
                     if f.get('issue_id') in new_ids and f.get('anchor')}.values())
    first = None
    planned = []
    # 每页仅包含首次意见，不再分页重复整篇文字报告。
    for offset in range(0, len(findings), publisher.page_size):
        group = findings[offset:offset + publisher.page_size]
        key = 'new-' + str(offset)
        mark = marker(job) + '\n<!-- ai-review:publication:' + job['id'] + ':' + key + ' -->'
        comments = []
        for finding in group:
            ident = finding['issue_id']
            original = finding_body(finding, job) + '\n' + issue_marker(ident)
            with contextlib.closing(store.connect()) as c:
                observed = c.execute('SELECT value FROM review_issue_observations WHERE job=? AND issue=?', (job['id'], ident)).fetchone()
            value = json.loads(observed[0])
            body = original + status_block(ident, value['status'], value['reason'], snapshot, finding['anchor'])
            # 超长单条意见只保留台账和报告，不能拆成重复问题评论。
            if len(body) > 12000:
                continue
            a = finding['anchor']
            comments.append({'body': body, 'path': a['path'], 'old_position': a['line'] if a['side'] == 'old' else 0,
                             'new_position': a['line'] if a['side'] == 'new' else 0, 'anchor': a,
                             'issue': ident, 'original_body': original})
        if not comments:
            continue
        payload = {'body': mark + f'\n新增 {len(comments)} 个问题；完整报告与历次状态见 Manager，当前状态见固定总览。',
                   'comments': comments, 'commit_id': snapshot['head']}
        planned.append(operation(store, job, key, 'create', payload))
    # 所有页面在首个 POST 前冻结，恢复时能区分未发送页面和回执未知页面。
    for op in planned:
        key = op['key']
        payload = op['payload']
        mark = marker(job) + '\n<!-- ai-review:publication:' + job['id'] + ':' + key + ' -->'
        reviews = publisher.client.pages(publisher.path)
        mine = [r for r in reviews if mark in r.get('body', '') and r.get('user', {}).get('id') == publisher.repo['bot_id']]
        if len(mine) > 1:
            raise PublishUnknown('同一新增问题发布记录出现多个 Review，停止自动写入')
        if mine:
            published = mine[0]
        else:
            if recovery or op['state'] != 'queued':
                raise PublishUnknown('新增问题创建结果尚未找到，未重复发送')
            if any(r.get('state') == 'PENDING' and r.get('user', {}).get('id') == publisher.repo['bot_id'] for r in reviews):
                raise PublishUnknown('机器人存在待处理草稿，未混入当前问题')
            assert_version(publisher.client, job['number'], snapshot)
            store.update(job['id'], job['owner'], status='publishing', stage='发布首次问题意见')
            change_operation(store, job, key, 'started')
            published = publisher.client.request('POST', publisher.path, {
                'event': 'COMMENT', 'commit_id': snapshot['head'], 'body': payload['body'],
                'comments': [{k: r[k] for k in ('path', 'body', 'old_position', 'new_position')} for r in payload['comments']]})
        by_body = check_review(publisher, published, payload)
        save_comments(publisher, published, payload, by_body)
        change_operation(store, job, key, 'done', {'review_id': published['id']})
        assert_version(publisher.client, job['number'], snapshot)
        first = first or published
    if first:
        store.update(job['id'], job['owner'], **publisher.reference(first))
    return first


def save_comments(publisher, review, payload, by_body):
    with publisher.store.tx() as c:
        for expected in payload['comments']:
            row = by_body[expected['body']]
            c.execute('INSERT OR IGNORE INTO review_issue_comments VALUES (?,?,?,?,?,?,?,?)',
                      (publisher.job['repository'], publisher.job['number'], review['id'], row['id'], expected['issue'],
                       expected['original_body'], expected['body'], ''))


def recover(publisher):
    """过期代次也可只读收集回执；不续发未发送页面，不覆写当前问题状态。"""
    with contextlib.closing(publisher.store.connect()) as c:
        operations = [dict(r) for r in c.execute("SELECT * FROM review_issue_operations WHERE job=? AND kind='create' ORDER BY key", (publisher.job['id'],))]
    first, incomplete = None, False
    reviews = publisher.client.pages(publisher.path)
    for op in operations:
        if op['state'] == 'queued':
            incomplete = True
            continue
        payload = json.loads(op['payload'])
        matches = [r for r in reviews if r.get('body') == payload['body'] and r.get('user', {}).get('id') == publisher.repo['bot_id']]
        if len(matches) != 1:
            raise PublishUnknown('已尝试创建的问题尚未找到唯一回执，未重发')
        actual = matches[0]
        by_body = check_review(publisher, actual, payload)
        save_comments(publisher, actual, payload, by_body)
        change_operation(publisher.store, publisher.job, op['key'], 'done', {'review_id': actual['id']})
        first = first or actual
    if not operations:
        raise PublishUnknown('缺少冻结发布计划，需核对原请求')
    reference = publisher.reference(first) if first else {'review_id': None, 'review_fragment': ''}
    return reference, incomplete


def sync_comments(publisher):
    store, client, job, snapshot = publisher.store, publisher.client, publisher.job, publisher.snapshot
    with contextlib.closing(store.connect()) as c:
        rows = [dict(r) for r in c.execute('''SELECT x.*,o.value FROM review_issue_comments x
            JOIN review_issue_observations o ON o.issue=x.issue AND o.job=?
            WHERE x.repository=? AND x.number=? ORDER BY x.review_id,x.comment_id''',
            (job['id'], job['repository'], job['number']))]
    for link in rows:
        store.active(job['id'], job['owner'])
        value = json.loads(link['value'])
        desired = link['original_body'] + status_block(link['issue'], value['status'], value['reason'], snapshot,
                                                       snapshot.get('anchors', {}).get(value.get('anchor_id')))
        op = operation(store, job, 'edit-' + str(link['comment_id']), 'edit',
                       {'comment_id': link['comment_id'], 'review_id': link['review_id'],
                        'before': link['last_body'], 'body': desired})
        if op['state'] == 'done':
            continue
        desired = op['payload']['body']
        error = ''
        try:
            if link['error'].startswith('历史正文'):
                raise ReviewError(link['error'])
            path = publisher.path + '/' + str(link['review_id']) + '/comments'
            actual = next((r for r in client.pages(path) if r['id'] == link['comment_id']), None)
            if not actual:
                raise ReviewError('原评论已删除，未重新创建')
            if actual.get('user', {}).get('id') != publisher.repo['bot_id']:
                raise ReviewError('原评论作者不匹配，未覆盖')
            # 旧代未知写入必须先确认回执；否则迟到的 PATCH 可能覆盖新代状态。
            with contextlib.closing(store.connect()) as c:
                pending = [dict(r) for r in c.execute("SELECT * FROM review_issue_operations WHERE kind='edit' AND key=? AND job!=? AND state IN ('started','unknown')",
                    (op['key'], job['id']))]
            for previous in pending:
                payload = json.loads(previous['payload'])
                # 评论 ID 只在同一仓库内唯一。
                old_job = store.job(previous['job'])
                if not old_job or old_job['repository'] != job['repository']:
                    continue
                if actual['body'] != payload['body']:
                    raise ReviewError('历史版本状态写入仍无确定回执，暂停后续评论修改；以总览为准')
                change_operation(store, old_job, previous['key'], 'done', {'comment_id': link['comment_id']})
                with store.tx() as c:
                    c.execute('UPDATE review_issue_comments SET last_body=? WHERE repository=? AND comment_id=?',
                              (actual['body'], job['repository'], link['comment_id']))
                if op['state'] == 'queued':
                    op['payload']['before'] = actual['body']
                    with store.tx() as c:
                        c.execute('UPDATE review_issue_operations SET payload=? WHERE job=? AND key=?',
                                  (encode(op['payload']), job['id'], op['key']))
            if actual['body'] != desired:
                if actual['body'] != op['payload']['before']:
                    raise ReviewError('原评论被人工编辑或已有其他状态，未覆盖；以总览为准')
                if op['state'] != 'queued':
                    raise ReviewError('上次状态写入结果未确认，仅回读而不重复写入')
                assert_version(client, job['number'], snapshot)
                change_operation(store, job, op['key'], 'started')
                op['state'] = 'started'
                client.request('PATCH', client.prefix + '/issues/comments/' + str(link['comment_id']), {'body': desired})
                actual = next((r for r in client.pages(path) if r['id'] == link['comment_id']), None)
                if not actual or actual.get('body') != desired or actual.get('user', {}).get('id') != publisher.repo['bot_id']:
                    raise ReviewError('状态评论回读不一致，待核对')
            with store.tx() as c:
                c.execute('UPDATE review_issue_comments SET last_body=?,error=? WHERE repository=? AND comment_id=?',
                          (desired, '', job['repository'], link['comment_id']))
            change_operation(store, job, op['key'], 'done', {'comment_id': link['comment_id']})
            assert_version(client, job['number'], snapshot)
        except (Stale, AccessRevoked):
            raise
        except ReviewError as exc:
            error = str(exc)
            change_operation(store, job, op['key'], 'unknown' if op['state'] == 'started' else 'conflict', error=error)
            with store.tx() as c:
                c.execute('UPDATE review_issue_comments SET error=? WHERE repository=? AND comment_id=?',
                          (error, job['repository'], link['comment_id']))


def publish(publisher, recovery=False):
    with publisher.store.lock(f"发布-{publisher.job['repository']}-{publisher.job['number']}"):
        publisher.store.active(publisher.job['id'], publisher.job['owner'])
        publisher.validate_local()
        first = publish_new(publisher, recovery)
        sync_comments(publisher)
        assert_version(publisher.client, publisher.job['number'], publisher.snapshot)
        return first['id'] if first else None
