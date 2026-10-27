"""分批协议边界测试；模型协议替身不代表真实模型效果验收。"""
import copy
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from ai_review import batch_plan, batch_store, batch_worker, token_budget, diff_review
from ai_review.store import Store
from ai_review.common import ReviewError
from ai_review.session_events import create_session, receive, query
from test_ai_review_diff import Client, DIFF


@pytest.fixture
def state():
    with tempfile.TemporaryDirectory() as folder:
        key = Path(folder)/'key'; key.write_bytes(Fernet.generate_key())
        with patch.dict(os.environ, AI_REVIEW_MASTER_KEY_FILE=str(key), AI_REVIEW_WORKSPACE_ROOT=folder):
            store = Store(Path(folder)/'review.db')
            store.save_config({'revision': 1, 'enabled': True, 'model': '测试模型', 'debounce_seconds': 0})
            repo = store.save_repo(1, 7, {'enabled': True, 'bot_token': store.seal('测试'), 'webhook_secret': store.seal('测试'), 'repo_name': 'test/code', 'bot_id': 9})
            job_id = store.enqueue(repo['id'], 1, {'head': 'a'*40, 'base': 'b'*40})
            # 本文件锁定旧版计划恢复语义；新建任务默认版本在第三版测试中单独验证。
            with store.tx() as conn:
                conn.execute('UPDATE jobs SET batch_flow_version=2 WHERE id=?', (job_id,))
            yield store, store.claim(), repo


def snapshot(config):
    return diff_review.capture(Client(), 1, config, batched=True)


def test_fresh_budget_defaults_are_persisted_and_can_plan(state):
    from ai_review.budgets import validate_context
    store, job, _ = state
    config = store.job_config(job['id'])
    assert config['batch_input_tokens'] == 65536
    assert config['model_max_output_tokens'] == 32768
    validate_context(config)
    plan = batch_plan.build(snapshot(config), config)
    assert plan['primary'] > 0
    assert not plan['gaps']
    assert all(unit['input_tokens'] <= token_budget.capacity(config) for unit in plan['units'])
    with store.connect() as conn:
        saved = json.loads(conn.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
    assert saved['batch_input_tokens'] == 65536
    assert saved['model_max_output_tokens'] == 32768


@pytest.mark.parametrize('input_tokens,output_tokens', [(32768, 0), (32768, 8192), (98304, 24576)])
def test_saved_budgets_and_frozen_jobs_survive_reopen(state, input_tokens, output_tokens):
    store, _, repo = state
    store.save_config({'revision': store.config()['revision'], 'batch_input_tokens': input_tokens,
                       'model_max_output_tokens': output_tokens})
    ident = store.enqueue(repo['id'], 2, {'head': 'c'*40, 'base': 'b'*40})
    reopened = Store(store.path)
    assert reopened.config()['batch_input_tokens'] == input_tokens
    assert reopened.config()['model_max_output_tokens'] == output_tokens
    reopened.save_config({'revision': reopened.config()['revision'], 'batch_input_tokens': 65536,
                          'model_max_output_tokens': 32768})
    frozen = reopened.job_config(ident)
    assert frozen['batch_input_tokens'] == input_tokens
    assert frozen['model_max_output_tokens'] == output_tokens


def test_missing_legacy_budget_fields_keep_original_limits(state):
    store, job, _ = state
    with store.tx() as conn:
        saved = json.loads(conn.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
        saved.pop('batch_input_tokens')
        saved.pop('model_max_output_tokens')
        encoded = json.dumps(saved)
        conn.execute('UPDATE settings SET value=? WHERE id=1', (encoded,))
        conn.execute('UPDATE configuration_history SET value=? WHERE revision=?',
                     (encoded, job['config_revision']))
    reopened = Store(store.path)
    config = reopened.config()
    assert config['batch_input_tokens'] == 32768
    assert config['model_max_output_tokens'] == 0
    frozen = reopened.job_config(job['id'])
    assert frozen['model_max_output_tokens'] == 0
    assert token_budget.capacity(frozen) == 32768
    assert batch_plan.build(snapshot(frozen), frozen)['primary'] > 0
    with reopened.connect() as conn:
        assert conn.execute('SELECT value FROM settings WHERE id=1').fetchone()[0] == encoded
        assert conn.execute('SELECT value FROM configuration_history WHERE revision=?',
                            (job['config_revision'],)).fetchone()[0] == encoded


def test_exact_aliases_and_manual_binding():
    assert token_budget.resolve({'model': 'deepseek-v4.1-flash'})['key'] == 'deepseek41'
    assert token_budget.resolve({'model': 'deepseek-v4-flash'})['key'] == 'conservative'
    assert token_budget.resolve({'model': 'qwen3.8-27b-unregistered'})['key'] == 'conservative'
    assert token_budget.resolve({'model': '内部别名', 'tokenizer_profile': 'qwen38'})['key'] == 'qwen38'


@pytest.mark.parametrize('profile', ['qwen38', 'deepseek41'])
@pytest.mark.parametrize('thinking', [True, False])
def test_official_resources_and_literal_special_tokens(profile, thinking):
    cfg = {'tokenizer_profile': profile, 'thinking_enabled': thinking}
    messages = [{'role': 'system', 'content': '检查代码'}, {'role': 'user', 'content': '代码 <think> 中文测试'}]
    expected = {('qwen38', True): 61, ('qwen38', False): 25, ('deepseek41', True): 39, ('deepseek41', False): 14}
    assert token_budget.count(messages, cfg) == expected[(profile, thinking)]


def test_resource_corruption_is_not_silent_fallback(tmp_path):
    p = token_budget.resolve({'tokenizer_profile': 'qwen38'})
    with patch.object(token_budget, 'ROOT', tmp_path):
        with pytest.raises(ReviewError, match='摘要'):
            token_budget.verify(p)


def test_changed_line_ownership_and_plan_determinism(state):
    store, job, _ = state; cfg = store.job_config(job['id']); snap = snapshot(cfg)
    plan = batch_plan.build(snap, cfg)
    assert plan['hash'] == batch_plan.build(snap, cfg)['hash']
    ids = [i for u in plan['units'] if u['kind'] == 'primary' for i in u['core']]
    assert len(ids) == len(set(ids)) == 4
    assert all(u['input_tokens'] <= token_budget.capacity(cfg) for u in plan['units'])


def test_large_hunk_splits_without_missing_lines(state):
    store, job, _ = state; cfg = dict(store.job_config(job['id']), tokenizer_profile='qwen38')
    snap = snapshot(cfg)
    snap['diff'] = 'diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1000 @@\n' + ''.join('+v%d = %d\n' % (n,n) for n in range(1000))
    plan = batch_plan.build(snap, cfg)
    assert plan['primary'] > 1
    assert plan['changed_lines'] == 1000
    ids = [i for u in plan['units'] if u['kind'] == 'primary' for i in u['core']]
    assert len(set(ids)) == len(ids) == 1000
    assert not plan['gaps']


def test_single_long_line_is_explicit_failure(state):
    store, job, _ = state; cfg = store.job_config(job['id']); snap = snapshot(cfg)
    snap['diff'] = 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+' + 'x'*100000 + '\n'
    with pytest.raises(ReviewError, match='无法容纳'):
        batch_plan.build(snap, cfg)


def test_invalid_output_not_zero_findings(state):
    store, job, _ = state; cfg = store.job_config(job['id']); snap = snapshot(cfg); unit = batch_plan.build(snap, cfg)['units'][0]
    for raw in [{'report_markdown': '没有问题'}, {'_review_completion': {'complete': True}, 'report_markdown': '{}'}]:
        with pytest.raises(ReviewError): batch_plan.parse_report(raw, snap, unit)


def test_cost_threshold_and_stale_confirmation(state):
    store, job, _ = state; cfg = store.job_config(job['id']); plan = batch_plan.build(snapshot(cfg), cfg)
    plan['max_calls'] = 9
    batch_store.save(store, job, plan, cfg)
    assert batch_store.get(store, job['id'])['authorized_calls'] == 0
    batch_store.transition(store, job, 'awaiting_cost', '等待确认')
    with pytest.raises(ReviewError): batch_store.control(store, job['id'], {'plan_hash': '过期'}, '测试', True)
    batch_store.control(store, job['id'], {'plan_hash': plan['hash']}, '测试', True)
    batch_store.control(store, job['id'], {'plan_hash': plan['hash']}, '测试', True)
    assert batch_store.get(store, job['id'])['authorized_calls'] == 9


def test_cost_summary_list_is_scoped_and_confirms_only_displayed_plan(state):
    store, job, repo = state; cfg = store.job_config(job['id'])
    plan = batch_plan.build(snapshot(cfg), cfg)
    plan['max_calls'] = 9
    plan['config']['api_base_url'] = '私有地址不能出现在列表中'
    plan['units'][0]['evidence'][0]['text'] = '代码证据不能出现在列表中'
    batch_store.save(store, job, plan, cfg)
    batch_store.transition(store, job, 'awaiting_cost', '等待确认')
    listing = store.jobs(allowed_jobs=[job['id']], allowed_repositories=[repo['id']])
    summary = listing['items'][0]['cost_summary']
    assert summary['plan_hash'] == plan['hash']
    assert summary['max_calls'] == 9 and summary['base_calls'] == plan['base_calls']
    assert summary['input_tokens'] == plan['input_tokens']
    assert summary['model'] == cfg['model']
    assert summary['output_tokens'] == (plan['config']['model_max_output_tokens'] or 8192)
    assert summary['gap_count'] == len(plan['gaps'])
    assert summary['unsupported_count'] == len(plan['unsupported'])
    assert '私有地址不能出现在列表中' not in json.dumps(listing, ensure_ascii=False)
    assert '代码证据不能出现在列表中' not in json.dumps(listing, ensure_ascii=False)
    assert store.jobs(allowed_jobs=[], allowed_repositories=[repo['id']])['items'] == []
    assert store.jobs(allowed_jobs=[job['id']], allowed_repositories=[])['items'] == []
    with store.tx() as connection:
        connection.execute('UPDATE review_batch_plans SET hash=? WHERE job=?', ('新摘要', job['id']))
    with pytest.raises(ReviewError, match='摘要已经变化'):
        batch_store.control(store, job['id'], {'plan_hash': summary['plan_hash']}, '测试', True)
    assert store.job(job['id'])['status'] == 'awaiting_cost'
    updated = store.jobs()['items'][0]['cost_summary']
    assert updated['plan_hash'] == '新摘要'
    batch_store.control(store, job['id'], {'plan_hash': updated['plan_hash']}, '测试', True)
    row = store.jobs()['items'][0]
    assert row['status'] == 'queued' and 'cost_summary' not in row


def test_waiting_task_without_usable_plan_has_no_confirmable_summary(state):
    store, job, _ = state
    batch_store.transition(store, job, 'awaiting_cost', '等待确认')
    assert store.jobs()['items'][0]['cost_summary'] is None
    with store.tx() as connection:
        connection.execute('INSERT INTO review_batch_plans(job,hash,value,budget_seconds) VALUES(?,?,?,?)',
                           (job['id'], '缺失字段', '{}', 3600))
    assert store.jobs()['items'][0]['cost_summary'] is None


def test_cost_summary_cache_reuses_plan_but_refreshes_budget_and_authorization(state):
    store, job, _ = state; cfg = store.job_config(job['id'])
    plan = batch_plan.build(snapshot(cfg), cfg); plan['max_calls'] = 9
    batch_store.save(store, job, plan, cfg)
    batch_store.transition(store, job, 'awaiting_cost', '等待确认')
    queries = []
    with store.connect() as connection:
        connection.set_trace_callback(queries.append)
        first = batch_store.cost_summaries(connection, [job['id']])[job['id']]
        with store.tx() as writer:
            writer.execute('UPDATE review_batch_plans SET budget_seconds=7200,authorized_calls=3 WHERE job=?', (job['id'],))
        second = batch_store.cost_summaries(connection, [job['id']])[job['id']]
    assert first['max_calls'] == second['max_calls'] == 9
    assert second['budget_seconds'] == 7200 and second['authorized_calls'] == 3
    assert sum('json_extract(value' in query for query in queries) == 1


def test_adjusted_legacy_plan_recalculates_initial_input_in_list_and_detail(state):
    store, job, _ = state; cfg = store.job_config(job['id'])
    plan = batch_plan.build(snapshot(cfg), cfg); plan['max_calls'] = 9
    batch_store.save(store, job, plan, cfg)
    batch_store.transition(store, job, 'awaiting_cost', '等待确认')
    old_unit = plan['units'][0]
    plan['adjustments'] = [{'unit': old_unit['id'], 'reason': '测试上下文拒绝'}]
    plan['hash'] = '调整后的摘要'
    with store.tx() as connection:
        connection.execute('UPDATE review_batch_plans SET hash=?,value=? WHERE job=?',
                           (plan['hash'], json.dumps(plan, ensure_ascii=False), job['id']))
        connection.execute("UPDATE review_batch_units SET state='replaced' WHERE job=? AND id=?", (job['id'], old_unit['id']))
        for position, tokens in enumerate((17, 23, 31), 100):
            unit = {'input_tokens': tokens, 'core': []}
            connection.execute('INSERT INTO review_batch_units(job,id,position,kind,value) VALUES(?,?,?,?,?)',
                               (job['id'], '新单元' + str(position), position,
                                'association' if tokens == 31 else 'primary', json.dumps(unit)))
    expected = plan['input_tokens'] - old_unit['input_tokens'] + 17 + 23 + 31
    assert store.jobs()['items'][0]['cost_summary']['input_tokens'] == expected
    assert batch_store.get(store, job['id'])['value']['input_tokens'] == expected


def test_two_sessions_each_start_at_one(state):
    store, job, _ = state
    first = []
    for i in range(2):
        if i:
            with store.tx() as c: batch_store.reset_session(c, job['id'])
        sid, token, _ = create_session(store, job['id'], job['owner'], time.time()+600, '/tmp/test')
        event = dict(job_id=job['id'], session_id=sid, seq=1, event_id=sid+':1', type='session.running', status='running')
        assert receive(store, token, event) == 1
        assert receive(store, token, event) == 0
        first.append(sid)
    for sid in first:
        assert len(query(store, job['id'], session_id=sid)['items']) == 1


def test_unconfirmed_plan_has_zero_model_calls(state):
    store, job, _ = state
    with store.tx() as c:
        cfg = store.job_config(job['id'], c); cfg['batch_auto_calls'] = 1
        c.execute('UPDATE configuration_history SET value=? WHERE revision=?', (json.dumps(cfg), job['config_revision']))
    with patch('ai_review.worker.client_for', return_value=Client()), patch('ai_review.issue_lifecycle.adopt_history'), patch('ai_review.native_review.execute') as model:
        batch_worker.process(store, job)
    assert store.job(job['id'])['status'] == 'awaiting_cost'
    model.assert_not_called()


def test_successful_unit_is_not_replayed_and_publication_waits(state):
    store, job, _ = state
    raw = {'review_scope': 'diff', 'report_markdown': json.dumps({'summary': '已检查修改', 'findings': [], 'limitations': []}),
           '_review_completion': {'complete': True}, '_native_result_sha256': 'f'*64, '_native_final_event_seq': 1}
    with patch('ai_review.worker.client_for', side_effect=lambda *a: Client()), patch('ai_review.issue_lifecycle.adopt_history'), \
         patch('ai_review.native_review.execute', return_value=raw) as model, patch('ai_review.native_review.acknowledge_result'), \
         patch('ai_review.publish.Publisher.publish') as publish, patch('ai_review.worker.refresh_pr'):
        batch_worker.process(store, job)
        assert store.job(job['id'])['status'] == 'queued'
        publish.assert_not_called()
        next_job = store.claim(); batch_worker.process(store, next_job)
        assert store.job(job['id'])['status'] == 'succeeded', store.job(job['id'])['error']
        assert model.call_count == 1
        assert publish.call_count == 1


@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('anchor_references', [False, True])
def test_candidate_requires_independent_refutation_before_publication(state, wrapped, anchor_references):
    store, job, _ = state
    def respond(_store, _job, _config, workspace, token, diff_snapshot):
        payload = json.loads(diff_snapshot['_batch_messages'][1]['content'])
        row = next(r for r in payload['evidence'] if r['anchor_id'])
        if 'candidates' in payload:
            answer = {'checks': [{'id': f['id'], 'decision': 'confirmed', 'reason': '已核对保护路径',
                                  'evidence_ids': f['evidence_ids']} for f in payload['candidates']]}
        else:
            answer = {'summary': '检查完成', 'limitations': [], 'findings': [{'severity':'P1','title':'测试候选',
                'evidence_ids':[row['id']], 'anchor_id':row['anchor_id'], 'trigger':'测试条件',
                'explanation':'测试因果链', 'impact':'测试影响', 'recommendation':'测试建议'}]}
        if anchor_references:
            for item in answer.get('findings', answer.get('checks', [])):
                item['evidence_ids'] = [row['anchor_id']]
        text = json.dumps(answer)
        if wrapped:
            text = '<think>固定思考片段</think>\n```json\n' + text + '\n```'
        return {'review_scope':'diff','report_markdown':text, '_review_completion':{'complete':True},
                '_native_result_sha256':'a'*64, '_native_final_event_seq':1}
    with patch('ai_review.worker.client_for', side_effect=lambda *a: Client()), patch('ai_review.issue_lifecycle.adopt_history'), \
         patch('ai_review.native_review.execute', side_effect=respond) as model, patch('ai_review.native_review.acknowledge_result'), \
         patch('ai_review.publish.Publisher.publish') as publish, patch('ai_review.worker.refresh_pr'):
        batch_worker.process(store, job)
        publish.assert_not_called()
        batch_worker.process(store, store.claim())
        publish.assert_not_called()
        batch_worker.process(store, store.claim())
        assert store.job(job['id'])['status'] == 'succeeded', store.job(job['id'])['error']
        assert model.call_count == 2
        assert publish.call_count == 1
        assert len(store.job(job['id'])['result']['findings']) == 1


def test_invalid_wrapped_report_keeps_safe_diagnostic_and_never_publishes(state):
    store, job, _ = state
    secret = '不可进入公开诊断的固定思考片段'
    raw = {'review_scope': 'diff', 'report_markdown': '<think>'+secret+'</think>\n无法解析的报告',
           '_review_completion': {'complete': True}, '_native_result_sha256': 'f'*64,
           '_native_final_event_seq': 1}
    with patch('ai_review.worker.client_for', side_effect=lambda *a: Client()), patch('ai_review.issue_lifecycle.adopt_history'), \
         patch('ai_review.native_review.execute', return_value=raw), patch('ai_review.native_review.acknowledge_result'), \
         patch('ai_review.publish.Publisher.publish') as publish:
        batch_worker.process(store, job)
    failed = store.job(job['id'])
    assert failed['status'] == 'incomplete'
    assert failed['diagnostic']['status'] == 'failed'
    assert failed['diagnostic']['reason'] == 'json_syntax'
    assert failed['diagnostic']['json_line'] == 1
    assert secret not in json.dumps(failed['diagnostic'], ensure_ascii=False)
    publish.assert_not_called()


def test_ack_loss_reuses_persisted_result_without_replaying_model(state):
    store, job, _ = state
    raw = {'review_scope':'diff','report_markdown':json.dumps({'summary':'完成','findings':[],'limitations':[]}),
           '_review_completion':{'complete':True},'_native_result_sha256':'a'*64,'_native_final_event_seq':1}
    error = token_budget.failure('EXECUTOR_RESULT_ACK_UNKNOWN', '回收回执丢失')
    with patch('ai_review.worker.client_for', side_effect=lambda *a: Client()), patch('ai_review.issue_lifecycle.adopt_history'), \
         patch('ai_review.native_review.execute', return_value=raw) as model, \
         patch('ai_review.native_review.acknowledge_result', side_effect=[error, None]):
        batch_worker.process(store, job)
        plan = batch_store.get(store, job['id'])
        assert store.job(job['id'])['status'] == 'incomplete'
        assert plan['units'][0]['state'] == 'unknown'
        batch_store.control(store, job['id'], {'plan_hash':plan['hash'], 'extra_seconds':0}, '测试')
        batch_worker.process(store, store.claim())
        assert model.call_count == 1
        assert batch_store.get(store, job['id'])['units'][0]['state'] == 'done'


def test_pending_unit_blocks_direct_publication(state):
    from ai_review.publish import Publisher
    store, job, repo = state; cfg = store.job_config(job['id']); snap = snapshot(cfg)
    plan = batch_plan.build(snap, cfg); batch_store.save(store, job, plan, cfg)
    report = {'findings': [], 'coverage': dict(snap['coverage'], complete=True), 'review_scope':'diff','report_markdown':'测试'}
    with pytest.raises(ReviewError, match='禁止发布'):
        Publisher(store, Client(), job, repo, snap, report).publish()


def test_eight_calls_auto_approved(state):
    store, job, _ = state; cfg = store.job_config(job['id']); plan = batch_plan.build(snapshot(cfg), cfg)
    plan['max_calls'] = 8
    batch_store.save(store, job, plan, cfg)
    assert batch_store.get(store, job['id'])['authorized_calls'] == 8


def test_missing_history_is_explicit_uncertainty_without_model_call(state):
    from ai_review.issue_checks import run_check
    store, job, _ = state
    payload = {'historical_diff': {}, 'candidates': []}
    with store.tx() as c:
        c.execute('INSERT INTO review_issue_checks(job,issue,input) VALUES(?,?,?)',
                  (job['id'], 'historical_issue', json.dumps(payload)))
    with patch('ai_review.broker.issue_completion') as model:
        run_check(store, job, 'historical_issue', snapshot(store.job_config(job['id'])))
    model.assert_not_called()
    with store.connect() as c:
        row = c.execute('SELECT state,result FROM review_issue_checks WHERE job=?', (job['id'],)).fetchone()
    assert row['state'] == 'done'
    assert json.loads(row['result'])['status'] == 'uncertain'


def test_expired_lease_records_unknown_interval(state):
    store, job, _ = state
    with store.tx() as c:
        c.execute('UPDATE jobs SET lease=? WHERE id=?', (time.time()-1, job['id']))
        c.execute('UPDATE review_batch_intervals SET verified_until=? WHERE job=? AND ended IS NULL',
                  (time.time()-2, job['id']))
    store.claim()
    assert batch_store.timing(store, job['id'])['uncertain_seconds'] >= 2


def test_cancelling_waiting_plan_revokes_authorization(state):
    from ai_review.cancellation import request_cancel
    store, job, _ = state; cfg = store.job_config(job['id']); plan = batch_plan.build(snapshot(cfg), cfg)
    batch_store.save(store, job, plan, cfg); batch_store.transition(store, job, 'awaiting_cost', '等待确认')
    assert request_cancel(store, job['id'], '测试') == 202
    with pytest.raises(ReviewError):
        batch_store.control(store, job['id'], {'plan_hash':plan['hash']}, '测试', True)


def test_auto_adjustment_only_for_explicit_context_rejection(state):
    store, job, _ = state; cfg = store.job_config(job['id']); snap = snapshot(cfg)
    plan = batch_plan.build(snap, cfg); batch_store.save(store, job, plan, cfg)
    record = batch_store.get(store, job['id'], private=True)['units'][0]
    assert not batch_store.adjust_context(store, job, record, {'upstream_error_code':'timeout'}, cfg, snap)


@pytest.mark.parametrize('raw', [b'+x\r\n', b'+x\n\\ No newline at end of file\n'])
def test_line_endings_are_preserved(state, raw):
    store, job, _ = state; cfg = store.job_config(job['id']); snap = snapshot(cfg)
    snap['diff'] = (b'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n'+raw).decode()
    plan = batch_plan.build(snap, cfg)
    assert plan['changed_lines'] == 1
    text = plan['units'][0]['evidence'][0]['text']
    assert text == ('x\r\n' if b'\r' in raw else 'x')
