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
            yield store, store.claim(), repo


def snapshot(config):
    return diff_review.capture(Client(), 1, config, batched=True)


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


def test_candidate_requires_independent_refutation_before_publication(state):
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
        return {'review_scope':'diff','report_markdown':json.dumps(answer), '_review_completion':{'complete':True},
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
