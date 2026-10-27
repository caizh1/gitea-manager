"""输出用尽的拆分必须有已保存证据，不接受截断内容或减少关系/候选。"""
import json

import pytest

from ai_review import batch_flow, batch_store, batch_worker, diff_inline
from ai_review.common import digest
from test_ai_review_batches import state
from test_ai_review_batch_flow import snapshot, completed, example_candidate


def save_failure(store, job, value, unit, reason='length', raw=False):
    job['batch_flow_version'] = 4
    value = dict(value, units=value['units'] + [unit], association=1, max_calls=20)
    value['hash'] = digest({k: v for k, v in value.items() if k != 'hash'})
    batch_store.save(store, job, value, store.job_config(job['id']))
    with store.tx() as db:
        db.execute("UPDATE review_batch_units SET state='failed' WHERE job=? AND id=?", (job['id'], unit['id']))
        db.execute('INSERT INTO review_batch_attempts(job,unit,attempt,session_id,state,session,raw,created) VALUES(?,?,1,?,?,?,?,0)',
                   (job['id'], unit['id'], 'length-session', 'failed',
                    json.dumps({} if raw else {'diagnostic': {'finish_reason': reason}}),
                    json.dumps({'_review_completion': {'complete': False, 'finish_reason': reason}}) if raw else None))
    return batch_store.get(store, job['id'], private=True), value


@pytest.mark.parametrize('raw', [False, True])
def test_length_splits_all_associations_once(state, raw):
    store, job, _ = state
    snap = snapshot('int first(void) { return 1; }\nint second(void) { return first(); }\nint third(void) { return second(); }\n')
    cfg = store.job_config(job['id']);value = batch_flow.build(snap, cfg)
    relations = [r for r in value['relations'] if r['kind'] == 'call']
    assert len(relations) == 2
    unit = batch_flow.make_unit(snap, 'association', [r for s in value['scopes'].values() for r in s['evidence']], connections=relations)
    plan, _ = save_failure(store, job, value, unit, raw=raw)
    record = next(u for u in plan['units'] if u['id'] == unit['id'])
    assert batch_store.adjust_output(store, job, record, {}, cfg, snap)
    after = batch_store.get(store, job['id'], private=True)
    children = [u['value'] for u in after['units'] if u['value'].get('output_parent') == unit['id']]
    assert len(children) == 2
    assert sorted(digest(r) for u in children for r in u['connections']) == sorted(digest(r) for r in relations)
    assert next(u for u in after['units'] if u['id'] == unit['id'])['state'] == 'replaced'
    assert not batch_store.adjust_output(store, job, record, {}, cfg, snap)
    assert store.job(job['id'])['model_calls'] == 0


def test_unknown_result_is_not_split(state):
    store, job, _ = state;snap = snapshot('int f(void) { return 0; }\n');cfg = store.job_config(job['id'])
    value = batch_flow.build(snap, cfg);unit = batch_flow.make_unit(snap, 'association', value['units'][0]['evidence'], connections=[{}, {}])
    plan, _ = save_failure(store, job, value, unit, reason='unknown')
    record = next(u for u in plan['units'] if u['id'] == unit['id'])
    assert not batch_store.adjust_output(store, job, record, {'finish_reason': 'length'}, cfg, snap)


def test_refutation_split_preserves_candidate_coverage_and_aggregate(state):
    store, job, _ = state;snap = snapshot('int f(void) { return 0; }\n');cfg = store.job_config(job['id'])
    value = batch_flow.build(snap, cfg);first = value['units'][0]
    a = example_candidate(first);b = dict(a, id=a['id']+'b', title='不同问题')
    saved = completed(value);saved['units'][0]['result']['findings'] = [a, b]
    unit = batch_flow.refutations(snap, saved, cfg)[0]
    plan, _ = save_failure(store, job, value, unit)
    with store.tx() as db:
        db.execute("UPDATE review_batch_units SET state='done',result=? WHERE job=? AND id=?",
                   (json.dumps(saved['units'][0]['result']), job['id'], first['id']))
    record = next(u for u in plan['units'] if u['id'] == unit['id'])
    assert batch_store.adjust_output(store, job, record, {}, cfg, snap)
    with store.tx() as db:
        for row in db.execute("SELECT id,value FROM review_batch_units WHERE job=? AND kind='refutation' AND state='pending'", (job['id'],)).fetchall():
            child = json.loads(row['value']);checks = [{'id': c['id'], 'decision': 'confirmed', 'reason': '已核对', 'evidence_ids': c['evidence_ids']}for c in child['candidates']]
            db.execute("UPDATE review_batch_units SET state='done',result=? WHERE job=? AND id=?", (json.dumps(checks), job['id'], row['id']))
    snap['anchors'], _ = diff_inline.index(snap)
    report = batch_worker.aggregate(snap, batch_store.get(store, job['id'], private=True))
    assert len(report['findings']) == 2


def test_split_depth_is_bounded(state):
    store, job, _ = state;snap = snapshot('int f(void) { return 0; }\n');cfg = store.job_config(job['id'])
    value = batch_flow.build(snap, cfg);unit = batch_flow.make_unit(snap, 'association', value['units'][0]['evidence'], connections=[{}, {}]);unit['output_split_depth'] = 2
    plan, _ = save_failure(store, job, value, unit)
    assert not batch_store.adjust_output(store, job, next(u for u in plan['units'] if u['id'] == unit['id']), {}, cfg, snap)


def test_old_jobs_do_not_get_a_new_recovery_plan(state):
    store, job, _ = state;snap = snapshot('int f(void) { return 0; }\n');cfg = store.job_config(job['id'])
    value = batch_flow.build(snap, cfg);unit = batch_flow.make_unit(snap, 'association', value['units'][0]['evidence'], connections=[{}, {}])
    plan, _ = save_failure(store, job, value, unit)
    job['batch_flow_version'] = 3
    assert not batch_store.adjust_output(store, job, next(u for u in plan['units'] if u['id'] == unit['id']), {}, cfg, snap)


@pytest.mark.parametrize('matching', [False, True])
def test_legacy_terminal_diagnostic_must_match_failed_session(state, matching):
    store, job, _ = state
    snap = snapshot('int first(void) { return 1; }\nint second(void) { return first(); }\nint third(void) { return second(); }\n')
    cfg = store.job_config(job['id']);value = batch_flow.build(snap, cfg)
    relations = [r for r in value['relations'] if r['kind'] == 'call']
    unit = batch_flow.make_unit(snap, 'association', [r for s in value['scopes'].values() for r in s['evidence']], connections=relations)
    plan, _ = save_failure(store, job, value, unit, reason='unknown')
    record = next(u for u in plan['units'] if u['id'] == unit['id'])
    diagnostic = {'finish_reason': 'length', 'session_id': 'length-session' if matching else 'another-session'}
    assert batch_store.adjust_output(store, job, record, diagnostic, cfg, snap) is matching


def test_native_failure_diagnostic_is_saved_with_attempt(state):
    from unittest.mock import patch
    from ai_review.common import ReviewError
    store, job, _ = state
    snap = snapshot('int f(void) { return 0; }\n');cfg = store.job_config(job['id'])
    value = batch_flow.build(snap, cfg)
    batch_store.save(store, job, value, cfg)
    unit = batch_store.get(store, job['id'], private=True)['units'][0]
    def fail(*args, **kwargs):
        store.update(job['id'], job['owner'], session_status='failed', diagnostic={'source': 'model_transport'})
        error = ReviewError('输出额度耗尽')
        error.diagnostic = {'source': 'native_result', 'finish_reason': 'length'}
        raise error
    with patch('ai_review.native_review.execute', side_effect=fail), patch('ai_review.batch_counter.run'):
        with pytest.raises(ReviewError):
            batch_worker._execute_unit(store, job, cfg, snap, unit, 500, value['profile'])
    with store.connect() as db:
        attempt = db.execute('SELECT state,session,session_id FROM review_batch_attempts WHERE job=?', (job['id'],)).fetchone()
    failure = json.loads(attempt['session'])['failure_diagnostic']
    assert attempt['state'] == 'failed'
    assert failure['finish_reason'] == 'length' and failure['session_id'] == attempt['session_id']
