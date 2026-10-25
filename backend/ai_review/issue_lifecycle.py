"""PR 问题台账：跨任务保留身份和 diff 证据，不读取工程源码。"""
import contextlib
import json
import re
import time

from .common import ReviewError, Stale, AccessRevoked, digest, encode
from .gitea import assert_version

LABELS = {'present': '仍存在', 'fixed': '已修复', 'obsolete': '已失效', 'uncertain': '待确认'}


def migrate(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS review_issue_summaries (
      repository INTEGER NOT NULL, number INTEGER NOT NULL, body TEXT NOT NULL, pending TEXT,
      PRIMARY KEY(repository,number));
    CREATE TABLE IF NOT EXISTS review_issue_snapshots (
      id TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS review_issues (
      id TEXT PRIMARY KEY, repository INTEGER NOT NULL, number INTEGER NOT NULL,
      origin_job TEXT NOT NULL, origin_snapshot TEXT, finding TEXT NOT NULL,
      status TEXT NOT NULL, reason TEXT NOT NULL, checked_snapshot TEXT,
      generation INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS review_issues_pr ON review_issues(repository,number,created,id);
    CREATE TABLE IF NOT EXISTS review_issue_checks (
      job TEXT NOT NULL, issue TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
      input TEXT NOT NULL, result TEXT, error TEXT NOT NULL DEFAULT '',
      started REAL, finished REAL, metrics TEXT NOT NULL DEFAULT '{}',
      PRIMARY KEY(job,issue));
    CREATE TABLE IF NOT EXISTS review_issue_observations (
      job TEXT NOT NULL, issue TEXT NOT NULL, snapshot TEXT NOT NULL,
      value TEXT NOT NULL, PRIMARY KEY(job,issue));
    CREATE TABLE IF NOT EXISTS review_issue_comments (
      repository INTEGER NOT NULL, number INTEGER NOT NULL, review_id INTEGER NOT NULL,
      comment_id INTEGER NOT NULL, issue TEXT NOT NULL, original_body TEXT NOT NULL,
      last_body TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(repository,comment_id));
    CREATE TABLE IF NOT EXISTS review_issue_imports (
      repository INTEGER NOT NULL, number INTEGER NOT NULL, review_id INTEGER NOT NULL,
      PRIMARY KEY(repository,number,review_id));
    CREATE TABLE IF NOT EXISTS review_issue_operations (
      job TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'queued', receipt TEXT, error TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(job,key));
    ''')


def freeze(c, snapshot):
    if not snapshot or 'diff' not in snapshot:
        return None
    value = {k: snapshot[k] for k in ('id', 'head', 'base', 'merge_base', 'diff', 'anchors')}
    c.execute('INSERT OR IGNORE INTO review_issue_snapshots VALUES (?,?)', (value['id'], encode(value)))
    return value['id']


def snapshot_for(c, ident):
    row = c.execute('SELECT value FROM review_issue_snapshots WHERE id=?', (ident,)).fetchone()
    return json.loads(row[0]) if row else None


def issue_marker(ident):
    return '<!-- ai-review:issue:' + ident + ' -->'


def insert_issue(c, job, snapshot, finding, ident, status='present', reason='首次审查提出的问题'):
    now = time.time()
    snap = freeze(c, snapshot)
    c.execute('''INSERT OR IGNORE INTO review_issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
              (ident, job['repository'], job['number'], job['id'], snap, encode(finding),
               status, reason, snap, job['generation'], now, now))


def adopt_history(store, client, job, repo):
    """只接管本 PR 可核对的机器人评论；清理后的历史证据不以源码补齐。"""
    path = client.prefix + f"/pulls/{job['number']}/reviews"
    for review in client.pages(path):
        store.active(job['id'], job['owner'])
        if review.get('user', {}).get('id') != repo['bot_id'] or review.get('state') != 'COMMENT':
            continue
        with contextlib.closing(store.connect()) as c:
            if c.execute('SELECT 1 FROM review_issue_imports WHERE repository=? AND number=? AND review_id=?',
                         (job['repository'], job['number'], review['id'])).fetchone():
                continue
            match = re.search(r'<!-- ai-review:v1:job:([0-9a-f]{32})(?::page:[0-9]+)? -->', review.get('body', ''))
            source = c.execute('SELECT * FROM jobs WHERE id=? AND repository=? AND number=?',
                               (match[1], job['repository'], job['number'])).fetchone() if match else None
        if not source or source['id'] == job['id']:
            continue
        source = dict(source)
        snapshot = json.loads(source['snapshot']) if source['snapshot'] else None
        if snapshot and snapshot.get('head') != review.get('commit_id'):
            raise ReviewError('历史 Review 与固定版本不一致，未接管')
        rows = client.pages(path + '/' + str(review['id']) + '/comments')
        from .publish import finding_body, marker
        result = json.loads(source['result']) if source['result'] else {}
        findings = result.get('publication', result).get('findings', [])
        for comment in rows:
            body = comment.get('body', '')
            if comment.get('user', {}).get('id') != repo['bot_id']:
                continue
            with store.tx() as c:
                store._active(c, job['id'], job['owner'])
                if c.execute('SELECT 1 FROM review_issue_comments WHERE repository=? AND comment_id=?',
                             (job['repository'], comment['id'])).fetchone():
                    continue
                stable = re.search(r'<!-- ai-review:issue:([0-9a-f]{32}) -->', body)
                if stable and c.execute('SELECT 1 FROM review_issues WHERE id=? AND repository=? AND number=? AND origin_job=?',
                    (stable[1], job['repository'], job['number'], source['id'])).fetchone():
                    # 创建成功但保存回执前换代：按冻结发布正文接回原 ID，不能导入成第二个问题。
                    plans = c.execute("SELECT payload FROM review_issue_operations WHERE job=? AND kind='create'", (source['id'],)).fetchall()
                    expected = next((item for plan in plans for item in json.loads(plan[0])['comments']
                                     if item['issue'] == stable[1] and item['body'] == body), None)
                    if expected:
                        c.execute('INSERT INTO review_issue_comments VALUES (?,?,?,?,?,?,?,?)',
                            (job['repository'], job['number'], review['id'], comment['id'], stable[1], expected['original_body'], body, ''))
                        continue
                finding = next((f for f in findings if finding_body(f, source) == body), None)
                trusted = finding is not None
                finding = finding or {'title': '历史问题（原始结构或正文已变化）', 'path': comment.get('path', ''),
                                      'comment_markdown': body, 'severity': '', 'anchor': None}
                ident = digest(['历史评论', job['repository'], job['number'], comment['id']])[:32]
                # 只合并同一完整 diff 中逐字段相同的历史意见，不按标题近似猜测。
                if trusted and snapshot and 'diff' in snapshot:
                    old = c.execute('''SELECT i.id,i.finding FROM review_issues i JOIN review_issue_snapshots s ON s.id=i.origin_snapshot
                        WHERE i.repository=? AND i.number=? AND json_extract(s.value,'$.diff')=?''',
                        (job['repository'], job['number'], snapshot['diff'])).fetchall()
                    def content(f):
                        return {k: v for k, v in f.items() if k not in ('fingerprint', 'candidate_id', 'issue_id', 'existing_review_id', 'anchor')}, {
                            k: v for k, v in (f.get('anchor') or {}).items() if k not in ('id', 'snapshot_id')}
                    duplicate = next((r for r in old if content(json.loads(r['finding'])) == content(finding)), None)
                    if duplicate:
                        ident = duplicate['id']
                insert_issue(c, source, snapshot, finding, ident, 'uncertain', '历史意见等待当前 diff 复核')
                c.execute('INSERT INTO review_issue_comments VALUES (?,?,?,?,?,?,?,?)',
                          (job['repository'], job['number'], review['id'], comment['id'], ident, body, body,
                           '' if trusted else '历史正文或证据无法完整核对，只在总览显示状态'))
        with store.tx() as c:
            store._active(c, job['id'], job['owner'])
            c.execute('INSERT OR IGNORE INTO review_issue_imports VALUES (?,?,?)',
                      (job['repository'], job['number'], review['id']))


def evidence_lines(snapshot, prefix):
    # 行编号指向保存的 diff 原文，包括元信息和上下文，不是猜测的源码行号。
    return {prefix + str(i): line for i, line in enumerate(snapshot['diff'].splitlines(), 1)}


def check_input(issue, origin, snapshot, candidates):
    finding = json.loads(issue['finding'])
    def relevant(source, prefix):
        if not source:
            return {}
        if not snapshot.get('batch_protocol'):
            return evidence_lines(source, prefix)
        # 原问题及当前同路径修改块；缺失或重命名不推断修复。
        path = finding.get('path') or (finding.get('anchor') or {}).get('path')
        from .diff import parse_diff
        import re
        lines = source['diff'].splitlines(keepends=True)
        starts = [i for i, line in enumerate(lines) if line.startswith('diff --git ')] + [len(lines)]
        selected = {}
        for start, end in zip(starts, starts[1:]):
            patches = parse_diff(''.join(lines[start:end]).encode())
            if patches and path in (patches[0].old_path, patches[0].new_path):
                selected.update({prefix+str(i+1): lines[i].rstrip('\r\n') for i in range(start, end)})
        return selected
    return {'issue_id': issue['id'], 'snapshot_id': snapshot['id'],
            'original_finding': finding, 'previous_status': issue['status'],
            'historical_version': {k: origin[k] for k in ('head', 'base', 'merge_base')} if origin else None,
            'current_version': {k: snapshot[k] for k in ('head', 'base', 'merge_base')},
            'historical_diff': relevant(origin, 'H'),
            'current_diff': relevant(snapshot, 'C'), 'candidates': candidates}


def uncertain(reason, candidates):
    return {'status': 'uncertain', 'reason': reason, 'evidence': [],
            'relations': {f['candidate_id']: 'uncertain' for f in candidates}, 'anchor_id': None}


def validate_check(raw, payload, snapshot):
    """核对格式、范围和原文；不把格式通过当作语义判断已经被证明。"""
    if not isinstance(raw, dict) or raw.get('issue_id') != payload['issue_id'] or raw.get('snapshot_id') != snapshot['id']:
        raise ReviewError('复核问题或版本编号不匹配')
    state, reason = raw.get('status'), raw.get('reason')
    if state not in LABELS or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ReviewError('复核状态或理由无效')
    relations = raw.get('relations')
    expected = {f['candidate_id'] for f in payload['candidates']}
    if (not isinstance(relations, dict) or set(relations) != expected or
            any(v not in ('same', 'different', 'uncertain') for v in relations.values())):
        raise ReviewError('复核未完整说明本轮候选与旧问题的关系')
    evidence = raw.get('evidence')
    if not isinstance(evidence, list) or len(evidence) > 100:
        raise ReviewError('复核证据格式无效')
    prefixes = set()
    for entry in evidence:
        if not isinstance(entry, dict) or set(entry) != {'id', 'quote'} or not isinstance(entry['id'], str):
            raise ReviewError('复核证据必须引用 diff 编号和原文')
        ident = entry['id']; prefix = ident[:1]
        source = payload['historical_diff'] if prefix == 'H' else payload['current_diff'] if prefix == 'C' else {}
        if ident not in source or not source[ident].strip() or source[ident] != entry['quote']:
            raise ReviewError('复核引用不属于固定 diff 原文')
        prefixes.add(prefix)
    if state == 'present' and 'C' not in prefixes:
        raise ReviewError('仍存在必须引用当前 diff 证据')
    if state == 'fixed' and not {'H', 'C'} <= prefixes:
        raise ReviewError('已修复必须引用修复前后 diff 证据')
    if state == 'obsolete' and ('H' not in prefixes or raw.get('obsolete_reason') not in ('withdrawn', 'out_of_scope')):
        raise ReviewError('已失效必须说明撤回或退出 PR 范围的依据')
    if state in ('fixed', 'obsolete') and any(v == 'same' for v in relations.values()):
        raise ReviewError('关闭结论与本轮仍报告相同缺陷相矛盾')
    anchor = raw.get('anchor_id')
    if anchor is not None and (not isinstance(anchor, str) or anchor not in snapshot.get('anchors', {})):
        raise ReviewError('当前定位编号无效')
    return {k: raw.get(k) for k in ('status', 'reason', 'evidence', 'relations', 'anchor_id', 'obsolete_reason')}


def reconcile(store, client, job, repo, snapshot, result, runner=None):
    if result.get('lifecycle'):
        return result
    from .issue_checks import run_check
    runner = runner or run_check
    assert_version(client, job['number'], snapshot)
    adopt_history(store, client, job, repo)
    candidates = [dict(f, candidate_id='N' + str(i)) for i, f in enumerate(result['findings'], 1)]
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        freeze(c, snapshot)
        issues = [dict(r) for r in c.execute('SELECT * FROM review_issues WHERE repository=? AND number=? ORDER BY created,id',
                                           (job['repository'], job['number']))]
        for issue in issues:
            payload = check_input(issue, snapshot_for(c, issue['origin_snapshot']), snapshot, candidates)
            c.execute('INSERT OR IGNORE INTO review_issue_checks(job,issue,input) VALUES (?,?,?)',
                      (job['id'], issue['id'], encode(payload)))
    checks = {}
    for index, issue in enumerate(issues):
        store.active(job['id'], job['owner'])
        assert_version(client, job['number'], snapshot)
        store.update(job['id'], job['owner'], progress_stage='issue_checks', units_total=len(issues),
                     units_completed=index, stage=f'复核旧问题 {index + 1}/{len(issues)}')
        runner(store, job, issue['id'], snapshot)
        with contextlib.closing(store.connect()) as c:
            row = c.execute('SELECT result FROM review_issue_checks WHERE job=? AND issue=?', (job['id'], issue['id'])).fetchone()
        checks[issue['id']] = json.loads(row[0]) if row and row[0] else uncertain('复核未取得可信结果', candidates)
    # 只有整代结论冻结后才能更新台账；过期任务不能覆盖最新代次。
    assert_version(client, job['number'], snapshot)
    new, deferred, observations = [], [], {}
    for ident, check in checks.items():
        observations[ident] = dict(check)
    for candidate in candidates:
        cid = candidate['candidate_id']
        same = [ident for ident, check in checks.items() if check['relations'].get(cid) == 'same']
        unsure = [ident for ident, check in checks.items() if check['relations'].get(cid) == 'uncertain']
        if len(same) == 1 and not unsure:
            candidate['issue_id'] = same[0]
            observations[same[0]].update(anchor_id=(candidate.get('anchor') or {}).get('id'))
        elif same or unsure:
            deferred.append(dict(candidate, reason='与旧问题的关系待确认，未重复发布'))
            for ident in same:
                observations[ident] = uncertain('本轮候选关联存在冲突，保留旧问题待确认', candidates)
        else:
            candidate['issue_id'] = digest([job['repository'], job['number'], job['id'], cid])[:32]
            new.append(candidate)
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        for ident, value in observations.items():
            c.execute('INSERT OR REPLACE INTO review_issue_observations VALUES (?,?,?,?)',
                      (job['id'], ident, snapshot['id'], encode(value)))
            c.execute('UPDATE review_issues SET status=?,reason=?,checked_snapshot=?,generation=?,updated=? WHERE id=? AND generation<=?',
                      (value['status'], value['reason'], snapshot['id'], job['generation'], time.time(), ident, job['generation']))
        for candidate in new:
            insert_issue(c, job, snapshot, candidate, candidate['issue_id'])
            value = {'status': 'present', 'reason': '本轮首次提出', 'anchor_id': (candidate.get('anchor') or {}).get('id'), 'evidence': []}
            c.execute('INSERT OR IGNORE INTO review_issue_observations VALUES (?,?,?,?)',
                      (job['id'], candidate['issue_id'], snapshot['id'], encode(value)))
        lifecycle = {'version': 1, 'snapshot_id': snapshot['id'], 'checked': len(issues), 'total': len(issues),
                     'new_ids': [f['issue_id'] for f in new], 'deferred': deferred,
                     'untracked_notice': '未形成独立问题条目的文字及待确认事项保留在完整报告，不计入台账。'}
        # 上代明确未发送或此前无定位的意见，第一次得到可靠定位时允许首次发布。
        publish_ids = set(lifecycle['new_ids'])
        for candidate in candidates:
            ident = candidate.get('issue_id')
            if not ident or not candidate.get('anchor') or observations.get(ident, {}).get('status') != 'present':
                continue
            has_comment = c.execute('SELECT 1 FROM review_issue_comments WHERE issue=?', (ident,)).fetchone()
            attempted = c.execute("""SELECT 1 FROM review_issue_operations p,json_each(p.payload,'$.comments') x
                WHERE p.kind='create' AND p.state!='queued' AND json_extract(x.value,'$.issue')=? LIMIT 1""", (ident,)).fetchone()
            if not has_comment and not attempted:
                publish_ids.add(ident)
        lifecycle['publish_ids'] = sorted(publish_ids)
        result = dict(result, findings=candidates, lifecycle=lifecycle)
        c.execute('UPDATE jobs SET result=?,units_completed=?,units_total=?,updated=? WHERE id=?',
                  (encode(result), len(issues), len(issues), time.time(), job['id']))
    return result


def listing(store, repository, number, page=1, job=None):
    with contextlib.closing(store.connect()) as c:
        args = [repository, number]
        where = 'repository=? AND number=?'
        if job:
            where += ''' AND (origin_job=? OR id IN (SELECT issue FROM review_issue_observations WHERE job=?)
                          OR id IN (SELECT issue FROM review_issue_checks WHERE job=?))'''
            args += [job, job, job]
        total = c.execute('SELECT count(*) FROM review_issues WHERE ' + where, args).fetchone()[0]
        rows = c.execute('SELECT * FROM review_issues WHERE ' + where + ' ORDER BY created,id LIMIT 50 OFFSET ?',
                         args + [(page - 1) * 50]).fetchall()
        state = c.execute('SELECT generation,signature FROM pr_state WHERE repository=? AND number=?', [repository, number]).fetchone()
        version = json.loads(state['signature'])[0] if state else {}
        output = []
        for row in rows:
            row = dict(row); finding = json.loads(row.pop('finding'))
            observed = c.execute('SELECT snapshot,value FROM review_issue_observations WHERE job=? AND issue=?', (job, row['id'])).fetchone() if job else c.execute(
                'SELECT o.snapshot,o.value FROM review_issue_observations o JOIN jobs j ON j.id=o.job WHERE o.issue=? AND o.snapshot=? ORDER BY j.generation DESC LIMIT 1',
                (row['id'], row['checked_snapshot'])).fetchone()
            snap = snapshot_for(c, observed['snapshot'] if observed else row['checked_snapshot'])
            value = json.loads(observed['value']) if observed else {}
            status = value.get('status', row['status'])
            stale = bool(job and not observed) or not snap or not isinstance(version, dict) or any(snap.get(k) != version.get(k) for k in ('head', 'base')) or row['generation'] != (state['generation'] if state else None)
            links = [dict(r) for r in c.execute('SELECT review_id,comment_id,error FROM review_issue_comments WHERE issue=?', (row['id'],))]
            output.append(dict(id=row['id'], title=finding['title'], path=finding.get('path', ''), severity=finding.get('severity', ''),
                               status=status, label=LABELS[status], reason=value.get('reason', row['reason']),
                               head=snap['head'] if snap else '', base=snap['base'] if snap else '', stale=stale,
                               comments=links, evidence=value.get('evidence', []), origin_job=row['origin_job']))
        checks = c.execute('SELECT state,count(*) AS n FROM review_issue_checks WHERE job=? GROUP BY state', (job,)).fetchall() if job else []
    return {'items': output, 'total': total, 'page': page, 'checks': {r['state']: r['n'] for r in checks}}


def overview(store, repo, job, pr):
    from urllib.parse import urlsplit, quote
    from .publish import prose
    data = listing(store, repo['id'], job['number'])
    if not data['total'] and not (job.get('result') or {}).get('lifecycle'):
        return ''
    text = '\n\n### 问题状态台账\n\n'
    text += '共 ' + str(data['total']) + ' 个历史问题。状态仅对所列版本有效；待确认不代表已修复。\n\n'
    for item in data['items']:
        stale = item['stale'] or item['head'] != pr['head']['sha'] or item['base'] != pr['base']['sha']
        state = '上一版本：' + item['label'] + '；当前待复核' if stale else item['label']
        text += '- `' + item['id'][:12] + '` **' + state + '** · ' + prose(item['title'][:100])
        text += ' · 依据 `' + item['head'][:12] + '`'
        if item['comments']:
            text += ' [原评论](#issuecomment-' + str(item['comments'][0]['comment_id']) + ')'
        if any(r['error'] for r in item['comments']):
            text += ' · 评论同步待核对'
        text += '\n'
    if data['total'] > len(data['items']):
        text += '\n此处展示前 50 项；完整台账在 Manager 分页查看。\n'
    lifecycle = (job.get('result') or {}).get('lifecycle', {})
    if lifecycle:
        text += f"\n本轮新增 {len(lifecycle['new_ids'])} 项；候选关系待确认 {len(lifecycle['deferred'])} 项。\n"
    text += '\n未解析成独立条目的文字仍保留在完整报告，不计入台账。'
    callback = repo.get('webhook_url', '')
    suffix = '/api/ai-review/webhooks/' + str(repo['id'])
    url = urlsplit(callback)
    if callback.endswith(suffix) and url.scheme in ('http', 'https') and url.netloc and not url.username:
        address = callback[:-len(suffix)] + '/ai-reviews?task=' + job['id']
        text += '\n\n[查看 Manager 完整报告与问题台账](' + quote(address, safe=':/?=&') + ')'
    else:
        text += '\n\n完整报告：Manager → AI 审查 → 任务 `' + job['id'] + '`。'
    return text
