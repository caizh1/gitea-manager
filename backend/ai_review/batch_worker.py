"""串行分批执行：每次领取至多执行一个单元，结果落库后才能 ACK。"""
import contextlib
import hashlib
import json
import os
import secrets
import threading
import time

from .common import ReviewError, Stale, PublishUnknown, encode, digest
from . import batch_store as state, batch_plan as planner, batch_counter, diff_inline, token_budget, batch_references, batch_locations
from .gitea import assert_version


class YieldUnit(Exception):
    pass


def _new_attempt(store, job, unit, config, budget):
    token = secrets.token_urlsafe(48)
    now = time.time()
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        previous = c.execute('SELECT * FROM review_batch_attempts WHERE job=? AND unit=? ORDER BY attempt DESC LIMIT 1',
                             (job['id'], unit['id'])).fetchone()
        if previous and previous['state'] != 'failed':
            return dict(previous), None
        state.reset_session(c, job['id'])
        sid = 'rvw_' + secrets.token_urlsafe(24).replace('-', '_')
        workspace = os.path.join(os.environ.get('AI_REVIEW_WORKSPACE_ROOT', '/app/review-workspaces'), job['id'])
        deadline = now + min(config.get('model_request_timeout_seconds', 600) + 40, budget)
        c.execute("UPDATE jobs SET session_id=?,session_status='prepared',event_token_hash=?,deadline_at=?,workspace_path=? WHERE id=?",
                  (sid, hashlib.sha256(token.encode()).hexdigest(), deadline, workspace, job['id']))
        attempt = (previous['attempt']+1) if previous else 1
        c.execute('INSERT INTO review_batch_attempts(job,unit,attempt,session_id,created) VALUES(?,?,?,?,?)',
                  (job['id'], unit['id'], attempt, sid, now))
        c.execute("UPDATE review_batch_units SET state='running' WHERE job=? AND id=?", (job['id'], unit['id']))
        return dict(c.execute('SELECT * FROM review_batch_attempts WHERE session_id=?', (sid,)).fetchone()), token


def _execute_unit(store, job, config, snapshot, record, budget, profile):
    from .native_review import execute, acknowledge_result, inspect_result
    from .diff_review import prepare
    unit = json.loads(record['value']) if isinstance(record['value'], str) else record['value']
    candidates = unit.get('candidates')
    with store.connect() as c:
        previous = c.execute("SELECT session,raw FROM review_batch_attempts WHERE job=? AND unit=? AND state='failed' "
                             "AND json_extract(session,'$.reference_validation') IS NOT NULL ORDER BY attempt DESC LIMIT 1",
                             (job['id'], unit['id'])).fetchone()
    correction = (planner._decode_output(json.loads(previous['raw']))
                  if previous and json.loads(previous['session']).get('reference_validation') else None)
    messages = planner.messages(snapshot, unit, candidates, correction=correction)
    batch_counter.run('count', {'messages': messages, 'profile': profile}, config,
                      lambda: store.active(job['id'], job['owner']), store.path.parent)
    attempt, token = _new_attempt(store, job, unit, config, budget)
    current = store.job(job['id'], evidence=False)
    job = dict(job, session_id=attempt['session_id'], deadline_at=current['deadline_at'])
    try:
        if attempt['raw']:
            raw = json.loads(attempt['raw'])
        else:
            if attempt['state'] == 'unknown' or (current.get('session_submitted_at') and time.time() >= current['deadline_at']-40):
                raw = inspect_result(store, job, config)
            else:
                raw = execute(store, job, config, prepare(current['workspace_path']), token,
                              diff_snapshot=dict(snapshot, _batch_messages=messages))
            if not raw.get('_native_result_sha256') or not raw.get('_native_final_event_seq'):
                raise token_budget.failure('BATCH_RECEIPT_UNKNOWN', '单元缺少完整事件及结果摘要，未 ACK')
            with store.tx() as c:
                store._active(c, job['id'], job['owner'])
                session = dict(c.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
                c.execute("UPDATE review_batch_attempts SET raw=?,session=?,state='received' WHERE session_id=?",
                          (encode(raw), encode({k: session[k] for k in state.SESSION_FIELDS}), attempt['session_id']))
        acknowledge_result(store, job, raw['_native_result_sha256'], raw['_native_final_event_seq'])
        result = planner.parse_refutation(raw, unit, candidates) if candidates is not None else planner.parse_report(raw, snapshot, unit)
        if correction and not batch_references.unchanged_report(correction, planner._decode_output(raw),
                field='checks' if candidates is not None else 'findings', locations=unit.get('reference_protocol') == batch_locations.PROTOCOL):
            error = token_budget.failure('BATCH_OUTPUT_INVALID', '引用纠正改变了意见内容或数量，未接受结果')
            error.diagnostic = {'source': 'batch_result', 'reason': 'batch_repair_changed_report'}
            raise error
        with store.tx() as c:
            store._active(c, job['id'], job['owner'])
            c.execute("UPDATE review_batch_attempts SET state='done',finished=? WHERE session_id=?", (time.time(), attempt['session_id']))
            c.execute("UPDATE jobs SET session_status='completed' WHERE id=?", (job['id'],))
            c.execute("UPDATE review_batch_units SET state='done',result=?,error='' WHERE job=? AND id=?", (encode(result), job['id'], unit['id']))
            if candidates is None and result['findings'] and unit.get('flow_version') != 'diff-batches-v3':
                refutation = dict(unit, id=unit['id']+'-check', kind='refutation', candidates=result['findings'], core=[], parent=unit['id'])
                position = c.execute('SELECT max(position)+1 FROM review_batch_units WHERE job=?', (job['id'],)).fetchone()[0]
                c.execute('INSERT OR IGNORE INTO review_batch_units(job,id,position,kind,value) VALUES(?,?,?,?,?)',
                          (job['id'], refutation['id'], position, 'refutation', encode(refutation)))
        return True
    except Stale:
        raise
    except ReviewError as exc:
        current = store.job(job['id'], evidence=False)
        known = (current['session_status'] in ('failed', 'aborted', 'completed')
                 and not getattr(exc, 'error_code', '').startswith('EXECUTOR_RESULT_ACK'))
        with store.tx() as c:
            store._active(c, job['id'], job['owner'])
            saved_session = {k: current[k] for k in state.SESSION_FIELDS}
            saved_session['failure_diagnostic'] = dict(getattr(exc, 'diagnostic', {}), session_id=attempt['session_id'])
            reason = getattr(exc, 'diagnostic', {}).get('reason')
            if reason in batch_references.RETRY_REASONS | batch_locations.RETRY_REASONS:
                saved_session['reference_validation'] = dict(exc.diagnostic, error_code=exc.error_code)
            retry = (known and current['session_status'] == 'completed' and attempt['attempt'] == 1
                     and unit.get('reference_protocol') in (batch_references.PROTOCOL, batch_locations.PROTOCOL)
                     and reason in batch_references.RETRY_REASONS | batch_locations.RETRY_REASONS)
            c.execute('UPDATE review_batch_attempts SET state=?,session=?,finished=? WHERE session_id=?',
                      ('failed' if known else 'unknown', encode(saved_session), time.time(), attempt['session_id']))
            c.execute('UPDATE review_batch_units SET state=?,error=? WHERE job=? AND id=?',
                      ('pending' if retry else 'failed' if known else 'unknown', str(exc), job['id'], unit['id']))
        if retry:
            return False
        raise


def aggregate(snapshot, plan):
    findings, sections, limitations = [], [], []
    if plan['value']['version'] == 'diff-batches-v3':
        from .batch_flow import candidates
        original = {c['id']: c for u in plan['units'] if u['kind'] in ('primary', 'association') and u['state'] != 'replaced'
                    for c in u['result']['findings']}
        # 反证候选编号以已保存的分组为准，恢复时不按当前代码重新编号。
        collected = [c for u in plan['units'] if u['kind'] == 'refutation' and u['state'] != 'replaced' for c in u['value']['candidates']]
        origins = [ident for c in collected for ident in c['origins']]
        expected_origins = {ident for ident, c in original.items() if c['severity'] in ('P0', 'P1')}
        if set(origins) != expected_origins or len(origins) != len(expected_origins):
            raise token_budget.failure('BATCH_OUTPUT_INVALID', '反证分组遗漏或重复原始候选，禁止发布')
        for candidate in collected:
            content = {k: v for k, v in candidate.items() if k not in ('id', 'origins', 'source_units')}
            if any(content != {k: v for k, v in original[ident].items() if k != 'id'} for ident in candidate['origins']):
                raise token_budget.failure('BATCH_OUTPUT_INVALID', '保存的反证候选与原始意见不一致')
        collected += [c for c in candidates(plan) if c['severity'] not in ('P0', 'P1')]
        checks = [check for unit in plan['units'] if unit['kind'] == 'refutation' and unit['state'] == 'done'
                  for check in unit['result']]
        expected = {c['id'] for c in collected if c['severity'] in ('P0', 'P1')}
        if {c['id'] for c in checks} != expected or len(checks) != len(expected):
            raise token_budget.failure('BATCH_OUTPUT_INVALID', '反证阶段遗漏或重复候选，禁止发布')
        checks += [{'id': c['id'], 'decision': 'confirmed', 'reason': '非阻塞意见，未单独反证，不影响 PASS/FAIL'}
                   for c in collected if c['severity'] not in ('P0', 'P1')]
        for unit in plan['units']:
            if unit['kind'] in ('primary', 'association') and unit['state'] == 'done':
                limitations.extend(str(s) for s in unit['result'].get('limitations', []))
                sections.append(unit['result']['summary'])
        limitations.extend(str(s.get('reason', '')) for s in plan['value'].get('limitations', []))
        rows = list({r['id']: r for u in plan['units'] for r in u['value'].get('evidence', [])}.values())
        plan = dict(plan, units=[{'id': 'collected', 'kind': 'primary', 'state': 'done',
                                 'value': {'evidence': rows}, 'result': {'summary': '', 'findings': collected}},
                                {'kind': 'refutation', 'state': 'done', 'value': {'parent': 'collected'}, 'result': checks}])
    by_parent = {u['value'].get('parent'): u for u in plan['units'] if u['kind'] == 'refutation'}
    anchors = snapshot['anchors']
    for u in plan['units']:
        if u['state'] == 'replaced':
            continue
        if u['kind'] not in ('primary', 'association'):
            continue
        value = u['result']
        limitations.extend(str(v) for v in value.get('limitations', []))
        sections.append(value['summary'])
        checks = {c['id']: c for c in by_parent[u['id']]['result']} if value['findings'] else {}
        for candidate in value['findings']:
            check = checks[candidate['id']]
            if check['decision'] != 'confirmed':
                if check['decision'] == 'uncertain':
                    limitations.append(candidate['title'] + '：' + check['reason'])
                continue
            anchor = anchors.get(candidate['anchor_id'])
            body = candidate['body'] if 'body' in candidate else '\n\n'.join(candidate[k] for k in ('trigger', 'explanation', 'impact', 'recommendation'))
            body += '\n\n反证核对：' + check['reason']
            finding = {'kind': 'diff_inline', 'severity': candidate['severity'], 'title': candidate['title'],
                       'comment_markdown': body, 'anchor': anchor, 'quote': anchor['quote'] if anchor else '',
                       'path': anchor['path'] if anchor else '', 'scope': 'PR diff'}
            if candidate.get('origins'):
                finding['origins'] = candidate['origins']
            finding['fingerprint'] = digest(finding)
            # 仅合并逐字段完全相同的结论，不根据标题近似删除意见。
            if not any(f['fingerprint'] == finding['fingerprint'] for f in findings):
                findings.append(finding)
    report = '## 分批审查结果\n\n' + '\n\n'.join(sections)
    for f in findings:
        report += f"\n\n### [{f['severity']}] {f['title']}\n\n{f['comment_markdown']}"
    if limitations:
        report += '\n\n## 待确认与范围限制\n\n' + '\n\n'.join(limitations)
    if plan['value']['version'] == 'diff-batches-v3':
        verdict = 'FAIL' if any(f['severity'] in ('P0', 'P1') for f in findings) else 'PASS'
        report = 'VERDICT: ' + verdict + '\n\n' + report
        report += '\n\n覆盖固定 PR 变更及记录范围内的两版源码；文本覆盖率不代表缺陷识别率，工程关系限制见上文。'
    else:
        report += '\n\n仅覆盖固定 PR diff；文本覆盖率不代表缺陷识别率，未检查 diff 外工程依赖。'
    coverage = dict(snapshot['coverage'], complete=True, files_reviewed=len({r['path'] for u in plan['units'] if u['kind'] == 'primary' and u['state'] == 'done' for r in u['value']['evidence']}),
                    changed_lines=plan['value']['changed_lines'], reviewed_lines=plan['value']['changed_lines'],
                    unsupported=plan['value']['unsupported'])
    return {'review_scope': 'diff', 'report_markdown': report, 'findings': findings, 'coverage': coverage,
            'inline_parser_version': 2, '_review_completion': {'complete': True}}


def process(store, job):
    from .worker import client_for, refresh_pr, _finish_failure
    from .diff_review import capture
    from .issue_lifecycle import adopt_history, reconcile
    from .publish import Publisher
    from .authorization import authorize
    repo, config = store.repo(job['repository']), store.job_config(job['id'])
    config = dict(config, model_max_output_tokens=config.get('model_max_output_tokens') or 8192)
    single_mode = job.get('batch_flow_version', 2) >= 5 and not config.get('batch_splitting_enabled', False)
    client = client_for(store, repo, job)
    stop = threading.Event()
    started = time.monotonic()
    metered = [started]
    def heartbeat():
        while not stop.wait(10):
            try:
                store.heartbeat(job['id'], job['owner'])
                store.health('worker', '正在执行分批审查')
                with store.tx() as c:
                    now = time.monotonic()
                    changed = c.execute('UPDATE review_batch_plans SET active_seconds=active_seconds+? WHERE job=?', (now-metered[0], job['id'])).rowcount
                    if changed: metered[0] = now
                    c.execute('UPDATE review_batch_intervals SET verified_until=? WHERE job=? AND ended IS NULL', (time.time(), job['id']))
            except Stale:
                client.interrupt()
                return
    thread = threading.Thread(target=heartbeat, daemon=True); thread.start()
    try:
        authorize(repo, job)
        from services.upgrade_maintenance import active as maintenance
        if maintenance():
            state.transition(store, job, 'budget_paused', '升级维护已开始，暂停分批审查')
            return
        snapshot = job.get('snapshot') or capture(client, job['number'], config, batched=True)
        assert_version(client, job['number'], snapshot)
        if not job.get('snapshot'):
            store.update(job['id'], job['owner'], snapshot=snapshot, stage='计算单次审查计划' if single_mode else '计算分批计划')
        plan = state.get(store, job['id'], private=True)
        if not plan:
            version = job.get('batch_flow_version', 2)
            if version >= 6 or version == 5 and single_mode:
                from .batch_flow import validate_single_size
                validate_single_size(snapshot, net_limit=version == 5)
            if job.get('batch_flow_version', 2) >= 4:
                snapshot = dict(snapshot, batch_reference_protocol=batch_locations.PROTOCOL)
                store.update(job['id'], job['owner'], snapshot=snapshot)
            if job.get('batch_flow_version', 2) >= 3 and 'analysis_sources' not in snapshot:
                from .batch_graph import capture_sources
                sources, limits = capture_sources(client, snapshot, lambda: store.active(job['id'], job['owner']))
                snapshot = dict(snapshot, analysis_sources=sources, analysis_limitations=limits)
                store.update(job['id'], job['owner'], snapshot=snapshot)
            adopt_history(store, client, job, repo)
            with store.connect() as c:
                history = [r[0] for r in c.execute('SELECT id FROM review_issues WHERE repository=? AND number=? ORDER BY created,id', (job['repository'], job['number']))]
            value = batch_counter.run('plan', {'snapshot': snapshot, 'history': history, 'flow_version': job.get('batch_flow_version', 2)}, config,
                                      lambda: store.active(job['id'], job['owner']), store.path.parent)
            state.save(store, job, value, config)
            plan = state.get(store, job['id'], private=True)
        if not snapshot.get('anchors'):
            # 从计划已经校验过的原始行恢复全局定位，不为批次重新编号。
            anchors = {}
            for unit in plan['value']['units']:
                for row in unit['evidence']:
                    ident = row.get('anchor_id')
                    if ident:
                        side = 'new' if row['kind'] == '+' else 'old'
                        anchors[ident] = {'id': ident, 'snapshot_id': snapshot['id'], 'path': row['path'],
                            'old_path': row['old_path'], 'new_path': row['new_path'], 'hunk': row['hunk'],
                            'side': side, 'line': row[side], 'quote': row['text'].removesuffix('\n').removesuffix('\r')}
            snapshot['anchors'] = anchors
            store.update(job['id'], job['owner'], snapshot=snapshot)
        if plan['value']['gaps']:
            state.transition(store, job, 'incomplete', '预检存在未覆盖范围：' + plan['value']['gaps'][0]['reason'])
            return
        if plan['authorized_calls'] < plan['value']['max_calls']:
            state.transition(store, job, 'awaiting_cost', '等待确认调用上限' if single_mode else '等待确认分批调用上限')
            return
        remaining = plan['budget_seconds'] - plan['active_seconds'] - (time.monotonic()-started)
        if remaining < 60:
            state.transition(store, job, 'budget_paused', '有效执行预算不足，追加预算后继续')
            return
        pending = [u for u in plan['units'] if u['kind'] != 'history' and u['state'] not in ('done', 'replaced')]
        store.update(job['id'], job['owner'], units_total=len(plan['units']), units_completed=sum(u['state'] == 'done' for u in plan['units']))
        if pending:
            unit = pending[0]
            if unit['state'] == 'failed':
                if state.adjust_output(store, job, unit, store.job(job['id'], evidence=False).get('diagnostic') or {}, config, snapshot):
                    state.transition(store, job, 'queued', '已保留输出超限记录，等待确认更小的关系或反证任务')
                    return
                state.transition(store, job, 'incomplete', '单元已确认失败，需选择该单元重试')
                return
            label = ('执行一次完整主审' if unit['kind'] == 'primary' else '执行独立反证') if unit['value'].get('review_mode') == 'single' else '执行分批单元 '+unit['id']
            store.update(job['id'], job['owner'], stage=label)
            try:
                completed = _execute_unit(store, job, config, snapshot, unit, remaining, plan['value']['profile'])
            except ReviewError:
                actual = store.job(job['id'], evidence=False)
                if state.adjust_output(store, job, unit, actual.get('diagnostic') or {}, config, snapshot):
                    state.transition(store, job, 'queued', '已保留输出超限记录，等待确认更小的关系或反证任务')
                    return
                if state.adjust_context(store, job, unit, actual.get('diagnostic') or {}, config, snapshot):
                    state.transition(store, job, 'queued', '服务拒绝上下文长度，已生成一次缩小计划')
                    return
                raise
            fresh = state.get(store, job['id'], private=True)
            done = [u for u in fresh['units'] if u['kind'] == 'primary' and u['state'] == 'done']
            lines = sum(len(u['value']['core']) for u in done)
            coverage = dict(snapshot['coverage'], complete=False, reviewed_lines=lines,
                            changed_lines=plan['value']['changed_lines'], files_reviewed=0)
            store.update(job['id'], job['owner'], result={'coverage': coverage, 'findings': [], 'review_scope': 'diff'},
                         units_completed=sum(u['state']=='done' for u in fresh['units']), units_total=len(fresh['units']))
            state.transition(store, job, 'queued', '本单元完成，等待下一执行位置' if completed else '引用校验失败，保留首次结果并执行一次纠正')
            return
        if plan['value']['version'] == 'diff-batches-v3' and not plan['value'].get('refutation_planned'):
            groups = batch_counter.run('refutations', {'snapshot': snapshot, 'plan': plan}, config,
                                       lambda: store.active(job['id'], job['owner']), store.path.parent)
            state.save_refutations(store, job, plan, groups)
            state.transition(store, job, 'queued', '候选汇总完成，已按证据与保护条件分组反证')
            return
        saved_result = store.job(job['id']).get('result') or {}
        result = saved_result if saved_result.get('lifecycle', {}).get('snapshot_id') == snapshot['id'] else aggregate(snapshot, plan)
        store.update(job['id'], job['owner'], result=result, deadline_at=None, stage='核对历史问题')
        job['_batch_deadline'] = time.time() + remaining
        checked = 0
        def historical_runner(store, current_job, issue, current_snapshot):
            nonlocal checked
            from .issue_checks import run_check
            with store.connect() as c:
                row = c.execute('SELECT state FROM review_issue_checks WHERE job=? AND issue=?', (job['id'], issue)).fetchone()
            if row[0] in ('failed', 'unknown', 'started'):
                raise token_budget.failure('HISTORY_INCOMPLETE', '历史问题复核失败或结果未知，未正式发布')
            if row[0] == 'done':
                with store.tx() as c:
                    c.execute("UPDATE review_batch_units SET state='done',result=(SELECT result FROM review_issue_checks WHERE job=? AND issue=?) WHERE job=? AND id=?",
                              (job['id'], issue, job['id'], 'history-'+issue))
                return
            if checked:
                raise YieldUnit()
            run_check(store, current_job, issue, current_snapshot)
            checked += 1
            with store.tx() as c:
                result_row = c.execute('SELECT state,result,error FROM review_issue_checks WHERE job=? AND issue=?', (job['id'], issue)).fetchone()
                status = result_row['state']
                c.execute('UPDATE review_batch_units SET state=?,result=?,error=? WHERE job=? AND id=?',
                          (status, result_row['result'], result_row['error'], job['id'], 'history-'+issue))
            if status != 'done':
                raise token_budget.failure('HISTORY_INCOMPLETE', '历史问题复核未完成，未正式发布')
        result = reconcile(store, client, job, repo, snapshot, result, runner=historical_runner)
        assert_version(client, job['number'], snapshot)
        if time.time() >= job['_batch_deadline'] or maintenance():
            state.transition(store, job, 'budget_paused', '发布前预算耗尽或进入升级维护')
            return
        store.update(job['id'], job['owner'], result=result, units_total=len(plan['units']), units_completed=len(plan['units']))
        Publisher(store, client, job, repo, snapshot, result).publish()
        store.finish(job['id'], job['owner'], 'succeeded')
        refresh_pr(store, repo, job)
    except YieldUnit:
        state.transition(store, job, 'queued', '历史复核单元完成，等待下一执行位置')
    except Stale as exc:
        actual = store.job(job['id'], evidence=False)
        if actual['status'] not in ('cancelling', 'cancelled'):
            store.finish(job['id'], job['owner'], 'publish_unknown' if actual['status'] in ('publishing', 'recovering') else 'superseded', str(exc))
    except Exception as exc:
        actual = store.job(job['id'], evidence=False)
        if actual['status'] in ('running', 'publishing', 'recovering'):
            if isinstance(exc, PublishUnknown) or actual['status'] in ('publishing', 'recovering'):
                _finish_failure(store, actual, exc, job, 'publish_unknown')
            else:
                from .worker import _failure_diagnostic
                diagnostic, message = _failure_diagnostic(actual, exc, job, store=store)
                store.update(job['id'], job['owner'], diagnostic=diagnostic)
                state.transition(store, job, 'incomplete', message)
    finally:
        stop.set(); thread.join(timeout=2); client.close()
        with store.tx() as c:
            c.execute('UPDATE review_batch_plans SET active_seconds=active_seconds+? WHERE job=?', (time.monotonic()-metered[0], job['id']))
        actual = store.job(job['id'], evidence=False)
        if actual['status'] in ('superseded', 'succeeded'):
            from .worker import _cleanup_owned
            _cleanup_owned(store, job)
